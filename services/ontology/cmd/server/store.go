package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

type Store struct{ db *sql.DB }

func newStore(dsn string) (*Store, error) {
	store, err := openStore(dsn)
	if err != nil {
		return nil, err
	}
	if err := store.verifySchema(context.Background()); err != nil {
		_ = store.db.Close()
		return nil, err
	}
	return store, nil
}

func openStore(dsn string) (*Store, error) {
	database, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, err
	}
	database.SetMaxOpenConns(10)
	database.SetConnMaxLifetime(30 * time.Minute)
	store := &Store{db: database}
	if err := database.PingContext(context.Background()); err != nil {
		_ = database.Close()
		return nil, err
	}
	return store, nil
}

func migrateStore(ctx context.Context, dsn string, seedDemo bool) error {
	store, err := openStore(dsn)
	if err != nil {
		return err
	}
	defer store.db.Close()
	return store.initialize(ctx, seedDemo)
}

func (s *Store) verifySchema(ctx context.Context) error {
	var drafts, snapshots, candidates, crossRelations sql.NullString
	var deletionColumn bool
	if err := s.db.QueryRowContext(ctx, `SELECT to_regclass('ontology_core.drafts')::text,
		to_regclass('ontology_core.snapshots')::text,to_regclass('ontology_core.candidates')::text,
		to_regclass('ontology_core.domain_cross_source_relations')::text,
		EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='ontology_core'
			AND table_name='drafts' AND column_name='deleted_at')`).Scan(
		&drafts, &snapshots, &candidates, &crossRelations, &deletionColumn,
	); err != nil {
		return err
	}
	if !drafts.Valid || !snapshots.Valid || !candidates.Valid || !crossRelations.Valid || !deletionColumn {
		return errors.New("ontology schema is not initialized; run /ontology migrate first")
	}
	return nil
}

func (s *Store) initialize(ctx context.Context, seedDemo bool) error {
	statements := []string{
		`CREATE SCHEMA IF NOT EXISTS ontology_core`,
		`CREATE TABLE IF NOT EXISTS ontology_core.drafts (
			domain TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, payload JSONB NOT NULL,
			updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`,
		`ALTER TABLE ontology_core.drafts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ`,
		`CREATE TABLE IF NOT EXISTS ontology_core.snapshots (
			id UUID PRIMARY KEY DEFAULT gen_random_uuid(), domain TEXT NOT NULL, version TEXT NOT NULL,
			content_hash TEXT NOT NULL UNIQUE, artifact_version TEXT NOT NULL, payload JSONB NOT NULL,
			wren_artifact JSONB NOT NULL, published_at TIMESTAMPTZ NOT NULL DEFAULT now())`,
		`CREATE INDEX IF NOT EXISTS ontology_snapshots_domain_idx ON ontology_core.snapshots(domain,published_at DESC)`,
		`CREATE TABLE IF NOT EXISTS ontology_core.domain_cross_source_relations (
            domain TEXT NOT NULL REFERENCES ontology_core.drafts(domain) ON DELETE CASCADE,
            id TEXT NOT NULL, source_datasource_id TEXT NOT NULL, target_datasource_id TEXT NOT NULL,
            payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY(domain,id), CHECK(source_datasource_id<>target_datasource_id))`,

		`CREATE TABLE IF NOT EXISTS ontology_core.candidates (
			id UUID PRIMARY KEY DEFAULT gen_random_uuid(), domain TEXT NOT NULL, term TEXT NOT NULL,
			source TEXT NOT NULL, proposal TEXT NOT NULL, evidence JSONB NOT NULL DEFAULT '{}',
			status TEXT NOT NULL DEFAULT 'pending_review', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ)`,
		`ALTER TABLE ontology_core.candidates ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'definition'`,
		`ALTER TABLE ontology_core.candidates ADD COLUMN IF NOT EXISTS patch JSONB NOT NULL DEFAULT '{}'`,
		`ALTER TABLE ontology_core.candidates ADD COLUMN IF NOT EXISTS reviewed_by TEXT`,
		`ALTER TABLE ontology_core.candidates ADD COLUMN IF NOT EXISTS review_note TEXT`,
	}
	for _, statement := range statements {
		if _, err := s.db.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	if !seedDemo {
		return s.migrateReferenceDefinitions(ctx)
	}
	demo := demoOntology()
	var draftCount int
	if err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.drafts WHERE domain=$1`, demo.Domain).Scan(&draftCount); err != nil {
		return err
	}
	if draftCount == 0 {
		if err := s.saveDraft(ctx, demo); err != nil {
			return err
		}
	}
	var snapshotCount int
	if err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.snapshots WHERE domain=$1`, demo.Domain).Scan(&snapshotCount); err != nil {
		return err
	}
	if snapshotCount == 0 {
		if _, err := s.publish(ctx, demo.Domain); err != nil {
			return err
		}
	}
	var candidateCount int
	if err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.candidates WHERE domain=$1`, demo.Domain).Scan(&candidateCount); err != nil {
		return err
	}
	if candidateCount == 0 {
		_, err := s.db.ExecContext(ctx, `INSERT INTO ontology_core.candidates(domain,term,source,proposal,evidence)
		VALUES($1,'90+逾期率','零售信贷指标口径手册 / 分块 1','90天以上逾期金额 / 有效贷款余额',
		'{"document_id":"seed","chunk_id":"metrics"}'::jsonb)`, demo.Domain)
		if err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) saveDraft(ctx context.Context, ontology Ontology) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	if err = ensureDatasourceBindings(ctx, tx, ontology); err != nil {
		return err
	}
	packages, err := readPackages(ctx, tx, ontology.SourcePackageRefs)
	if err != nil {
		return err
	}
	definition := referenceDefinition(ontology, packages)
	ontology = composeReferences(definition, packages)
	if issues := validateOntology(ontology); len(issues) > 0 {
		return &validationError{issues: issues}
	}
	payload, err := json.Marshal(definition)
	if err != nil {
		return err
	}
	result, err := tx.ExecContext(ctx, `INSERT INTO ontology_core.drafts(domain,name,version,payload)
	VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(domain) DO UPDATE SET name=excluded.name,version=excluded.version,payload=excluded.payload,updated_at=now() WHERE ontology_core.drafts.deleted_at IS NULL`,
		ontology.Domain, ontology.Name, ontology.Version, payload)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count == 0 {
		return sql.ErrNoRows
	}
	return tx.Commit()
}

func (s *Store) getDraft(ctx context.Context, domain string) (Ontology, error) {
	var payload []byte
	if err := s.db.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL`, domain).Scan(&payload); err != nil {
		return Ontology{}, err
	}
	var ontology Ontology
	if err := json.Unmarshal(payload, &ontology); err != nil {
		return Ontology{}, err
	}
	return s.resolveReferences(ctx, s.db, ontology)
}

func (s *Store) listDrafts(ctx context.Context) ([]DraftSummary, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT domain,name,version,payload,updated_at FROM ontology_core.drafts WHERE deleted_at IS NULL ORDER BY updated_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []DraftSummary{}
	for rows.Next() {
		var domain, name, version string
		var payload []byte
		var updatedAt time.Time
		if err := rows.Scan(&domain, &name, &version, &payload, &updatedAt); err != nil {
			return nil, err
		}
		var ontology Ontology
		if err := json.Unmarshal(payload, &ontology); err != nil {
			return nil, err
		}
		ontology, err := s.resolveReferences(ctx, s.db, ontology)
		if err != nil {
			return nil, err
		}
		status := "configured"
		if len(ontology.Objects) == 0 {
			status = "unconfigured"
		}
		result = append(result, DraftSummary{domain, name, version, status, len(ontology.Objects), len(ontology.Relations), len(ontology.Metrics), updatedAt})
	}
	return result, rows.Err()
}

func (s *Store) publish(ctx context.Context, domain string) (Snapshot, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Snapshot{}, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return Snapshot{}, err
	}
	var raw []byte
	if err = tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, domain).Scan(&raw); err != nil {
		return Snapshot{}, err
	}
	var ontology Ontology
	if err = json.Unmarshal(raw, &ontology); err != nil {
		return Snapshot{}, err
	}
	ontology, err = s.resolveReferences(ctx, tx, ontology)
	if err != nil {
		return Snapshot{}, err
	}
	snapshot, err := preparePublication(ontology)
	if err != nil {
		return Snapshot{}, err
	}
	payload, err := json.Marshal(snapshot.Ontology)
	if err != nil {
		return Snapshot{}, err
	}
	var storedPayload, storedArtifact []byte
	// The legacy NOT NULL column remains for existing databases. New publications
	// contain no MDL; a repeated content hash preserves the original snapshot.
	err = tx.QueryRowContext(ctx, `INSERT INTO ontology_core.snapshots(domain,version,content_hash,artifact_version,payload,wren_artifact)
    VALUES($1,$2,$3,$4,$5::jsonb,'{}'::jsonb)
    ON CONFLICT(content_hash) DO UPDATE SET content_hash=excluded.content_hash
    RETURNING id::text,domain,version,content_hash,artifact_version,published_at,payload,wren_artifact`,
		snapshot.Domain, snapshot.Version, snapshot.ContentHash, snapshot.ArtifactVersion, payload,
	).Scan(&snapshot.ID, &snapshot.Domain, &snapshot.Version, &snapshot.ContentHash, &snapshot.ArtifactVersion, &snapshot.PublishedAt, &storedPayload, &storedArtifact)
	if err != nil {
		return Snapshot{}, err
	}
	if err := json.Unmarshal(storedPayload, &snapshot.Ontology); err != nil {
		return Snapshot{}, err
	}
	if err := json.Unmarshal(storedArtifact, &snapshot.WrenArtifact); err != nil {
		return Snapshot{}, err
	}
	if err = syncCrossRelations(ctx, tx, snapshot.Ontology); err != nil {
		return Snapshot{}, err
	}
	if err = tx.Commit(); err != nil {
		return Snapshot{}, err
	}
	return snapshot, nil
}

func (s *Store) latestSnapshot(ctx context.Context, domain string) (Snapshot, error) {
	var snapshot Snapshot
	var payload, artifact []byte
	err := s.db.QueryRowContext(ctx, `SELECT id::text,domain,version,content_hash,artifact_version,published_at,payload,wren_artifact
	FROM ontology_core.snapshots AS s WHERE domain=$1 AND EXISTS (SELECT 1 FROM ontology_core.drafts AS d WHERE d.domain=s.domain AND d.deleted_at IS NULL) ORDER BY published_at DESC LIMIT 1`, domain).Scan(
		&snapshot.ID, &snapshot.Domain, &snapshot.Version, &snapshot.ContentHash, &snapshot.ArtifactVersion, &snapshot.PublishedAt, &payload, &artifact)
	if err != nil {
		return Snapshot{}, err
	}
	if err := json.Unmarshal(payload, &snapshot.Ontology); err != nil {
		return Snapshot{}, err
	}
	if err := json.Unmarshal(artifact, &snapshot.WrenArtifact); err != nil {
		return Snapshot{}, err
	}
	return snapshot, nil
}

func (s *Store) listCandidates(ctx context.Context, domain string) ([]Candidate, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id::text,domain,term,source,proposal,kind,patch,status,evidence,created_at,reviewed_at,
	COALESCE(reviewed_by,''),COALESCE(review_note,'')
	FROM ontology_core.candidates WHERE ($1='' OR domain=$1) ORDER BY created_at DESC`, domain)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Candidate{}
	for rows.Next() {
		var candidate Candidate
		var evidence, patch []byte
		if err := rows.Scan(&candidate.ID, &candidate.Domain, &candidate.Term, &candidate.Source, &candidate.Proposal,
			&candidate.Kind, &patch, &candidate.Status, &evidence, &candidate.CreatedAt, &candidate.ReviewedAt,
			&candidate.ReviewedBy, &candidate.ReviewNote); err != nil {
			return nil, err
		}
		_ = json.Unmarshal(evidence, &candidate.Evidence)
		_ = json.Unmarshal(patch, &candidate.Patch)
		result = append(result, candidate)
	}
	return result, rows.Err()
}

func (s *Store) createCandidate(ctx context.Context, input CandidateCreate) (Candidate, error) {
	input.Domain, input.Term, input.Source, input.Proposal, input.Kind = strings.TrimSpace(input.Domain), strings.TrimSpace(input.Term), strings.TrimSpace(input.Source), strings.TrimSpace(input.Proposal), strings.TrimSpace(input.Kind)
	if input.Domain == "" || input.Term == "" || input.Source == "" || input.Proposal == "" {
		return Candidate{}, fmt.Errorf("domain, term, source and proposal are required")
	}
	validKinds := map[string]bool{"definition": true, "metric": true, "object": true, "property": true, "relation": true}
	if !validKinds[input.Kind] {
		return Candidate{}, fmt.Errorf("invalid candidate kind")
	}
	if input.Patch == nil {
		input.Patch = map[string]any{}
	}
	evidence, err := json.Marshal(input.Evidence)
	if err != nil {
		return Candidate{}, err
	}
	patch, err := json.Marshal(input.Patch)
	if err != nil {
		return Candidate{}, err
	}
	var candidate Candidate
	var storedEvidence, storedPatch []byte
	err = s.db.QueryRowContext(ctx, `INSERT INTO ontology_core.candidates(domain,term,source,proposal,kind,patch,evidence)
	SELECT $1,$2,$3,$4,$5,$6::jsonb,$7::jsonb
	WHERE NOT EXISTS (SELECT 1 FROM ontology_core.candidates WHERE domain=$1 AND term=$2 AND source=$3 AND proposal=$4)
	RETURNING id::text,domain,term,source,proposal,kind,patch,status,evidence,created_at,reviewed_at,
	COALESCE(reviewed_by,''),COALESCE(review_note,'')`, input.Domain, input.Term, input.Source, input.Proposal, input.Kind, patch, evidence).Scan(
		&candidate.ID, &candidate.Domain, &candidate.Term, &candidate.Source, &candidate.Proposal, &candidate.Kind,
		&storedPatch, &candidate.Status, &storedEvidence, &candidate.CreatedAt, &candidate.ReviewedAt,
		&candidate.ReviewedBy, &candidate.ReviewNote)
	if errors.Is(err, sql.ErrNoRows) {
		err = s.db.QueryRowContext(ctx, `SELECT id::text,domain,term,source,proposal,kind,patch,status,evidence,created_at,reviewed_at,
		COALESCE(reviewed_by,''),COALESCE(review_note,'') FROM ontology_core.candidates
		WHERE domain=$1 AND term=$2 AND source=$3 AND proposal=$4 ORDER BY created_at DESC LIMIT 1`,
			input.Domain, input.Term, input.Source, input.Proposal).Scan(
			&candidate.ID, &candidate.Domain, &candidate.Term, &candidate.Source, &candidate.Proposal, &candidate.Kind,
			&storedPatch, &candidate.Status, &storedEvidence, &candidate.CreatedAt, &candidate.ReviewedAt,
			&candidate.ReviewedBy, &candidate.ReviewNote)
	}
	if err != nil {
		return Candidate{}, err
	}
	_ = json.Unmarshal(storedPatch, &candidate.Patch)
	_ = json.Unmarshal(storedEvidence, &candidate.Evidence)
	return candidate, nil
}

func bumpPatchVersion(value string) string {
	parts := strings.Split(value, ".")
	if len(parts) == 3 {
		if patch, err := strconv.Atoi(parts[2]); err == nil {
			parts[2] = strconv.Itoa(patch + 1)
			return strings.Join(parts, ".")
		}
	}
	return value + ".1"
}

func applyCandidate(ontology *Ontology, kind string, patch map[string]any) error {
	if kind == "definition" {
		return nil
	}
	payload, err := json.Marshal(patch)
	if err != nil {
		return err
	}
	switch kind {
	case "metric":
		var value Metric
		if err := json.Unmarshal(payload, &value); err != nil {
			return err
		}
		replaced := false
		for index := range ontology.Metrics {
			if ontology.Metrics[index].Name == value.Name {
				ontology.Metrics[index] = value
				replaced = true
			}
		}
		if !replaced {
			ontology.Metrics = append(ontology.Metrics, value)
		}
	case "object":
		var value ObjectClass
		if err := json.Unmarshal(payload, &value); err != nil {
			return err
		}
		replaced := false
		for index := range ontology.Objects {
			if ontology.Objects[index].Name == value.Name {
				ontology.Objects[index] = value
				replaced = true
			}
		}
		if !replaced {
			ontology.Objects = append(ontology.Objects, value)
		}
	case "relation":
		var value RelationClass
		if err := json.Unmarshal(payload, &value); err != nil {
			return err
		}
		replaced := false
		for index := range ontology.Relations {
			if ontology.Relations[index].Name == value.Name {
				ontology.Relations[index] = value
				replaced = true
			}
		}
		if !replaced {
			ontology.Relations = append(ontology.Relations, value)
		}
	case "property":
		var value struct {
			Object   string   `json:"object"`
			Property Property `json:"property"`
		}
		if err := json.Unmarshal(payload, &value); err != nil {
			return err
		}
		foundObject := false
		for objectIndex := range ontology.Objects {
			if ontology.Objects[objectIndex].Name != value.Object {
				continue
			}
			foundObject = true
			replaced := false
			for propertyIndex := range ontology.Objects[objectIndex].Properties {
				if ontology.Objects[objectIndex].Properties[propertyIndex].Name == value.Property.Name {
					ontology.Objects[objectIndex].Properties[propertyIndex] = value.Property
					replaced = true
				}
			}
			if !replaced {
				ontology.Objects[objectIndex].Properties = append(ontology.Objects[objectIndex].Properties, value.Property)
			}
		}
		if !foundObject {
			return fmt.Errorf("property target object does not exist")
		}
	default:
		return fmt.Errorf("unsupported candidate kind")
	}
	return nil
}

func (s *Store) decideCandidate(ctx context.Context, id, status, reviewer, note string) (Candidate, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Candidate{}, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return Candidate{}, err
	}
	var candidate Candidate
	var evidence, patch []byte
	err = tx.QueryRowContext(ctx, `SELECT id::text,domain,term,source,proposal,kind,patch,status,evidence,created_at,reviewed_at,
	COALESCE(reviewed_by,''),COALESCE(review_note,'') FROM ontology_core.candidates WHERE id=$1 FOR UPDATE`, id).Scan(
		&candidate.ID, &candidate.Domain, &candidate.Term, &candidate.Source, &candidate.Proposal, &candidate.Kind,
		&patch, &candidate.Status, &evidence, &candidate.CreatedAt, &candidate.ReviewedAt,
		&candidate.ReviewedBy, &candidate.ReviewNote)
	if err != nil {
		return Candidate{}, err
	}
	_ = json.Unmarshal(evidence, &candidate.Evidence)
	_ = json.Unmarshal(patch, &candidate.Patch)
	if candidate.Status != "pending_review" {
		return Candidate{}, fmt.Errorf("candidate has already been reviewed")
	}
	if status == "accepted" && candidate.Kind != "definition" {
		var draftPayload []byte
		if err := tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, candidate.Domain).Scan(&draftPayload); err != nil {
			return Candidate{}, err
		}
		var ontology Ontology
		if err := json.Unmarshal(draftPayload, &ontology); err != nil {
			return Candidate{}, err
		}
		if err := applyCandidate(&ontology, candidate.Kind, candidate.Patch); err != nil {
			return Candidate{}, err
		}
		ontology.Version = bumpPatchVersion(ontology.Version)
		if issues := validateOntology(ontology); len(issues) > 0 {
			return Candidate{}, &validationError{issues: issues}
		}
		updatedPayload, _ := json.Marshal(ontology)
		if _, err := tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET version=$1,payload=$2::jsonb,updated_at=now() WHERE domain=$3`,
			ontology.Version, updatedPayload, candidate.Domain); err != nil {
			return Candidate{}, err
		}
	}
	err = tx.QueryRowContext(ctx, `UPDATE ontology_core.candidates SET status=$1,reviewed_at=now(),reviewed_by=$2,review_note=$3
	WHERE id=$4 RETURNING reviewed_at`, status, reviewer, note, id).Scan(&candidate.ReviewedAt)
	if err != nil {
		return Candidate{}, err
	}
	candidate.Status, candidate.ReviewedBy, candidate.ReviewNote = status, reviewer, note
	if err := tx.Commit(); err != nil {
		return Candidate{}, err
	}
	return candidate, nil
}

type validationError struct{ issues []ValidationIssue }

func (e *validationError) Error() string { return "ontology validation failed" }
func isNotFound(err error) bool          { return errors.Is(err, sql.ErrNoRows) }
