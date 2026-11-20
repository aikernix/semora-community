package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
)

type deletionConflict string

func (e deletionConflict) Error() string { return string(e) }

func (s *Store) deleteDomain(ctx context.Context, domain string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var locked string
	if err = tx.QueryRowContext(ctx, `SELECT domain FROM ontology_core.drafts
		WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, domain).Scan(&locked); err != nil {
		return err
	}
	var queryRunning, analysisRunning bool
	err = tx.QueryRowContext(ctx, `SELECT
		EXISTS(SELECT 1 FROM semora_meta.runs r JOIN semora_meta.sessions s ON r.session_id=s.id
			WHERE s.deleted_at IS NULL AND (r.payload->>'domain'=$1 OR s.domain=$1)
			AND r.status NOT IN ('completed','failed','clarifying','awaiting_approval')),
		EXISTS(SELECT 1 FROM semora_meta.datasource_onboarding_jobs WHERE domain=$1
			AND status NOT IN ('completed','completed_with_pending','failed','cancelled'))`, domain).Scan(&queryRunning, &analysisRunning)
	if err != nil {
		return err
	}
	if queryRunning {
		return deletionConflict("该业务域仍有查询在运行，请等待完成或取消后再删除。")
	}
	if analysisRunning {
		return deletionConflict("该业务域仍有分析任务，请等待或取消任务后再删除。")
	}
	if _, err = tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET deleted_at=now() WHERE domain=$1`, domain); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) deleteDatasource(ctx context.Context, id string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var locked string
	if err = tx.QueryRowContext(ctx, `SELECT id FROM semora_meta.datasources
		WHERE id=$1 AND status<>'deleted' FOR UPDATE`, id).Scan(&locked); err != nil {
		return err
	}
	// Serialize with configuration writes so a concurrent binding cannot pass the check.
	if _, err = tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN SHARE ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT d.name,d.payload,p.payload FROM ontology_core.drafts d
		LEFT JOIN LATERAL (SELECT payload FROM ontology_core.snapshots WHERE domain=d.domain
			ORDER BY published_at DESC LIMIT 1) p ON true
		WHERE d.deleted_at IS NULL ORDER BY d.name,d.domain`)
	if err != nil {
		return err
	}
	var domains []string
	for rows.Next() {
		var name string
		var draft, published []byte
		if err = rows.Scan(&name, &draft, &published); err != nil {
			rows.Close()
			return err
		}
		bound := false
		for _, payload := range [][]byte{draft, published} {
			if len(payload) == 0 {
				continue
			}
			var value Ontology
			if err = json.Unmarshal(payload, &value); err != nil {
				rows.Close()
				return err
			}
			bound = bound || referencesDatasource(value, id)
		}
		if bound {
			domains = append(domains, name)
		}
	}
	rows.Close()
	if err = rows.Err(); err != nil {
		return err
	}
	if len(domains) > 0 {
		return deletionConflict(fmt.Sprintf("数据源仍被 %s 引用，请先在本体工作室解绑。", strings.Join(domains, "、")))
	}
	var historical, running bool
	err = tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM semora_meta.runs r
		WHERE jsonb_path_exists(r.payload, '$.**.data_source_id ? (@ == $source)',
			jsonb_build_object('source', $1::text))
		OR jsonb_path_exists(r.payload, '$.**.dataSourceId ? (@ == $source)',
			jsonb_build_object('source', $1::text))
		OR EXISTS (SELECT 1 FROM ontology_core.snapshots s
			WHERE s.content_hash=r.payload #>> '{ontology,content_hash}'
			AND jsonb_path_exists(s.payload, '$.**.dataSourceId ? (@ == $source)',
				jsonb_build_object('source', $1::text)))),
		EXISTS(SELECT 1 FROM semora_meta.datasource_onboarding_jobs
			WHERE (datasource_id=$1 OR peer_datasource_id=$1)
			AND status NOT IN ('completed','completed_with_pending','failed','cancelled'))`, id).Scan(&historical, &running)
	if err != nil {
		return err
	}
	if historical {
		return deletionConflict("该数据源被历史查询引用，无法删除。历史查询和数据源均已保留。")
	}
	if running {
		return deletionConflict("该数据源仍有扫描或分析任务，请等待或取消任务后再删除。")
	}
	if _, err = tx.ExecContext(ctx, `UPDATE semora_meta.datasources SET status='deleted' WHERE id=$1`, id); err != nil {
		return err
	}
	return tx.Commit()
}

func referencesDatasource(value Ontology, id string) bool {
	if value.DataSourceID == id || value.SourceScopes[id] != "" {
		return true
	}
	for _, ref := range value.SourcePackageRefs {
		if ref.DataSourceID == id {
			return true
		}
	}
	for _, object := range value.Objects {
		if object.DataSourceID == id {
			return true
		}
	}
	return false
}

func ensureDatasourceBindings(ctx context.Context, tx *sql.Tx, value Ontology) error {
	var registryExists bool
	if err := tx.QueryRowContext(ctx, `SELECT to_regclass('semora_meta.datasources') IS NOT NULL`).Scan(&registryExists); err != nil || !registryExists {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT id FROM semora_meta.datasources d WHERE to_jsonb(d)->>'status'='deleted'`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			return err
		}
		if referencesDatasource(value, id) {
			return deletionConflict("本体配置引用了已删除的数据源，请刷新后重新选择。")
		}
	}
	return rows.Err()
}

func deleteResource(w http.ResponseWriter, r *http.Request, remove func(context.Context, string) error, key string) {
	err := remove(r.Context(), r.PathValue(key))
	var conflict deletionConflict
	switch {
	case err == nil:
		jsonResponse(w, 200, map[string]bool{"deleted": true})
	case isNotFound(err):
		jsonResponse(w, 404, map[string]string{"error": "资源不存在或已删除"})
	case errors.As(err, &conflict):
		jsonResponse(w, 409, map[string]string{"error": conflict.Error()})
	default:
		jsonResponse(w, 500, map[string]string{"error": "无法完成删除，请重试"})
	}
}
