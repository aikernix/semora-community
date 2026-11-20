package main

import (
	"context"
	"encoding/json"
	"net/http"
)

func withoutSource(value Ontology, sourceID string) Ontology {
	refs := []SourcePackageRef{}
	for _, ref := range value.SourcePackageRefs {
		if ref.DataSourceID != sourceID {
			refs = append(refs, ref)
		}
	}
	value.SourcePackageRefs = refs
	objects := []ObjectClass{}
	removed := map[string]bool{}
	for _, object := range value.Objects {
		sid := object.DataSourceID
		if sid == "" {
			sid = value.DataSourceID
		}
		if sid == sourceID || packageTag(object.Metadata) == sourceID {
			removed[object.Name] = true
			continue
		}
		objects = append(objects, object)
	}
	value.Objects = objects
	value.Metadata = copyMetadata(value.Metadata)
	if overrides, ok := value.Metadata["sourceObjectOverrides"].(map[string]any); ok {
		kept := map[string]any{}
		for key, patch := range overrides {
			if !removed[key] {
				kept[key] = patch
			}
		}
		value.Metadata["sourceObjectOverrides"] = kept
	}
	scopes := map[string]string{}
	for sid, field := range value.SourceScopes {
		if sid != sourceID {
			scopes[sid] = field
		}
	}
	value.SourceScopes = scopes
	if value.DataSourceID == sourceID {
		value.DataSourceID = ""
		if len(objects) > 0 {
			value.DataSourceID = objects[0].DataSourceID
		}
	}
	return pruneInvalidReferences(value)
}

func (s *Store) unbindSource(ctx context.Context, domain, sourceID string) (Ontology, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return Ontology{}, err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return Ontology{}, err
	}
	var raw []byte
	if err = tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, domain).Scan(&raw); err != nil {
		return Ontology{}, err
	}
	var previous Ontology
	if err = json.Unmarshal(raw, &previous); err != nil {
		return Ontology{}, err
	}
	packages, err := readPackages(ctx, tx, previous.SourcePackageRefs)
	if err != nil {
		return Ontology{}, err
	}
	value := withoutSource(composeReferences(previous, packages), sourceID)
	if len(value.Objects) > 0 || len(value.SourcePackageRefs) > 0 {
		value, err = s.saveEffectiveTx(ctx, tx, value)
	} else {
		value.Version = bumpPatchVersion(previous.Version)
		var snapshot Snapshot
		snapshot, err = configurationSnapshot(value)
		if err == nil {
			raw, err = json.Marshal(value)
		}
		if err == nil {
			_, err = tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET version=$1,payload=$2::jsonb,updated_at=now() WHERE domain=$3`, value.Version, string(raw), domain)
		}
		if err == nil {
			err = syncCrossRelations(ctx, tx, value)
		}
		if err == nil {
			err = insertEffectiveSnapshot(ctx, tx, &snapshot)
		}
	}
	if err != nil {
		return Ontology{}, err
	}
	var hasQueue bool
	if err = tx.QueryRowContext(ctx, `SELECT to_regclass('semora_meta.relationship_candidates') IS NOT NULL`).Scan(&hasQueue); err != nil {
		return Ontology{}, err
	}
	if hasQueue {
		if _, err = tx.ExecContext(ctx, `DELETE FROM semora_meta.relationship_candidates WHERE domain=$1 AND scope='cross_source' AND (source_datasource_id=$2 OR target_datasource_id=$2)`, domain, sourceID); err != nil {
			return Ontology{}, err
		}
	}
	var hasJobs bool
	if err = tx.QueryRowContext(ctx, `SELECT to_regclass('semora_meta.datasource_onboarding_jobs') IS NOT NULL`).Scan(&hasJobs); err != nil {
		return Ontology{}, err
	}
	if hasJobs {
		_, err = tx.ExecContext(ctx, `UPDATE semora_meta.datasource_onboarding_jobs
			SET details=jsonb_set(details, '{pair_unbound}', 'true'::jsonb, true)
			WHERE domain=$1 AND kind='cross_source' AND (datasource_id=$2 OR peer_datasource_id=$2)`, domain, sourceID)
		if err != nil {
			return Ontology{}, err
		}
	}
	if err = tx.Commit(); err != nil {
		return Ontology{}, err
	}
	return value, nil
}

func (s *server) unbindSource(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	value, err := s.store.unbindSource(r.Context(), r.PathValue("domain"), r.PathValue("source"))
	if isNotFound(err) {
		jsonResponse(w, 404, map[string]string{"error": "业务域不存在"})
		return
	}
	if err != nil {
		jsonResponse(w, 500, map[string]string{"error": "解除数据源绑定失败"})
		return
	}
	jsonResponse(w, 200, value)
}
