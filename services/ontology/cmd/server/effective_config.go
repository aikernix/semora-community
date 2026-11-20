package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
)

var errDomainExists = errors.New("业务域标识已存在")
var domainIdentifier = regexp.MustCompile(`^[a-z][a-z0-9_]{1,99}$`)

func (s *Store) createDomain(ctx context.Context, domain, name string) (Ontology, error) {
	value := Ontology{Domain: domain, Name: strings.TrimSpace(name), Version: "1.0.0",
		Objects: []ObjectClass{}, Relations: []RelationClass{}, Metrics: []Metric{}, KnowledgeBaseIDs: []string{}}
	if !domainIdentifier.MatchString(domain) || value.Name == "" || len(value.Name) > 600 {
		return Ontology{}, &validationError{issues: []ValidationIssue{{"domain", "请填写名称及有效的业务域标识"}}}
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		return Ontology{}, err
	}
	result, err := s.db.ExecContext(ctx, `INSERT INTO ontology_core.drafts(domain,name,version,payload)
        VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(domain) DO NOTHING`, domain, value.Name, value.Version, string(encoded))
	if err != nil {
		return Ontology{}, err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return Ontology{}, err
	}
	if n == 0 {
		return Ontology{}, errDomainExists
	}
	return value, nil
}

func effectiveOntology(input, previous Ontology) (Ontology, error) {
	// Knowledge bindings have their own endpoint; an ontology editor cannot overwrite them.
	input.KnowledgeBaseIDs = previous.KnowledgeBaseIDs
	input.Version = bumpPatchVersion(previous.Version)
	input.Objects = append([]ObjectClass(nil), input.Objects...)
	for i := range input.Objects {
		if input.Objects[i].DataSourceID == "" {
			input.Objects[i].DataSourceID = input.DataSourceID
		}
		if input.Objects[i].DataSourceID == "" {
			return Ontology{}, &validationError{issues: []ValidationIssue{{"objects.dataSourceId", "请为每个对象选择数据源"}}}
		}
	}
	// Keep the internal compatibility reference; the UI uses explicit per-object mappings.
	if input.DataSourceID == "" && len(input.Objects) > 0 {
		input.DataSourceID = input.Objects[0].DataSourceID
	}
	return input, nil
}

func insertEffectiveSnapshot(ctx context.Context, tx *sql.Tx, snapshot *Snapshot) error {
	encoded, err := json.Marshal(snapshot.Ontology)
	if err != nil {
		return err
	}
	return tx.QueryRowContext(ctx, `INSERT INTO ontology_core.snapshots
        (domain,version,content_hash,artifact_version,payload,wren_artifact,published_at)
        VALUES($1,$2,$3,$4,$5::jsonb,'{}'::jsonb,clock_timestamp())
        RETURNING id::text,published_at`, snapshot.Domain, snapshot.Version, snapshot.ContentHash,
		snapshot.ArtifactVersion, string(encoded)).Scan(&snapshot.ID, &snapshot.PublishedAt)
}

func (s *Store) saveEffective(ctx context.Context, input Ontology) (Ontology, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Ontology{}, err
	}
	defer tx.Rollback()
	value, err := s.saveEffectiveTx(ctx, tx, input)
	if err != nil {
		return Ontology{}, err
	}
	if err = tx.Commit(); err != nil {
		return Ontology{}, err
	}
	return value, nil
}
func (s *Store) saveEffectiveTx(ctx context.Context, tx *sql.Tx, input Ontology) (Ontology, error) {
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return Ontology{}, err
	}
	var encoded []byte
	if err := tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, input.Domain).Scan(&encoded); err != nil {
		return Ontology{}, err
	}
	var previous Ontology
	if err := json.Unmarshal(encoded, &previous); err != nil {
		return Ontology{}, err
	}
	value, err := effectiveOntology(input, previous)
	if err != nil {
		return Ontology{}, err
	}
	if err = ensureDatasourceBindings(ctx, tx, value); err != nil {
		return Ontology{}, err
	}
	packages, err := readPackages(ctx, tx, value.SourcePackageRefs)
	if err != nil {
		return Ontology{}, err
	}
	if err = validateInheritedMappings(value, packages); err != nil {
		return Ontology{}, err
	}
	definition := referenceDefinition(value, packages)
	value = composeReferences(definition, packages)
	snapshot, err := preparePublication(value)
	if err != nil {
		return Ontology{}, err
	}
	encoded, err = json.Marshal(definition)
	if err != nil {
		return Ontology{}, err
	}
	if _, err = tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET name=$1,version=$2,payload=$3::jsonb,updated_at=now() WHERE domain=$4`, value.Name, value.Version, string(encoded), value.Domain); err != nil {
		return Ontology{}, err
	}
	if err = syncCrossRelations(ctx, tx, definition); err != nil {
		return Ontology{}, err
	}
	if err = insertEffectiveSnapshot(ctx, tx, &snapshot); err != nil {
		return Ontology{}, err
	}
	return snapshot.Ontology, nil
}

func (s *Store) bindEffectiveKnowledgeBases(ctx context.Context, domain string, ids []string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	var encoded []byte
	if err = tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, domain).Scan(&encoded); err != nil {
		return err
	}
	var value Ontology
	if err = json.Unmarshal(encoded, &value); err != nil {
		return err
	}
	value.KnowledgeBaseIDs = ids
	value.Version = bumpPatchVersion(value.Version)
	// Resolve references for the execution context; the draft itself remains reference-only.
	effective, err := s.resolveReferences(ctx, tx, value)
	if err != nil {
		return err
	}
	if len(effective.Objects) > 0 || len(effective.SourcePackageRefs) > 0 {
		snapshot, err := preparePublication(effective)
		if err != nil {
			return err
		}
		if err = insertEffectiveSnapshot(ctx, tx, &snapshot); err != nil {
			return err
		}
	}
	encoded, err = json.Marshal(ids)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET version=$1,
        payload=jsonb_set(jsonb_set(payload,'{knowledgeBaseIds}',$2::jsonb,true),'{version}',to_jsonb($1::text),true),
        updated_at=now() WHERE domain=$3`, value.Version, string(encoded), domain)
	if err != nil {
		return err
	}
	return tx.Commit()
}
