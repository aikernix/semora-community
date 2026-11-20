package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"sort"
)

func relationAppendError(message string) error {
	return &validationError{issues: []ValidationIssue{{"relations", message}}}
}

// Append against the locked current value; never replace unrelated edits.
func appendRelations(current, added []RelationClass) ([]RelationClass, error) {
	result := append([]RelationClass{}, current...)
	for _, relation := range added {
		record, ok := relation.Metadata["relationshipRecord"].(map[string]any)
		validCardinality := relation.Cardinality == "1:1" || relation.Cardinality == "1:N" || relation.Cardinality == "N:1" || relation.Cardinality == "N:N"
		id, _ := record["id"].(string)
		if !ok || record["status"] != "accepted" || !validCardinality || relation.Name == "" || id == "" {
			return nil, relationAppendError("仅允许追加验证通过的完整关系")
		}
		found := false
		for _, existing := range result {
			if existing.Name != relation.Name {
				continue
			}
			a, b := existing, relation
			a.Metadata, b.Metadata = nil, nil
			if !reflect.DeepEqual(a, b) {
				return nil, relationAppendError("关系标识冲突，原关系已保留")
			}
			found = true
		}
		if !found {
			result = append(result, relation)
		}
	}
	return result, nil
}

func appendPackageRelations(current, addition json.RawMessage) (json.RawMessage, error) {
	var original map[string]json.RawMessage
	var old, delta sourcePackage
	if err := json.Unmarshal(current, &original); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(current, &old); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(addition, &delta); err != nil {
		return nil, err
	}
	relations, err := appendRelations(old.Relations, delta.Relations)
	if err != nil {
		return nil, err
	}
	original["relations"], err = json.Marshal(relations)
	if err != nil {
		return nil, err
	}
	var records []map[string]any
	if raw := original["relationshipRecords"]; len(raw) > 0 && string(raw) != "null" {
		if err = json.Unmarshal(raw, &records); err != nil {
			return nil, err
		}
	}
	seen := map[string]bool{}
	for _, record := range records {
		seen[fmt.Sprint(record["id"])] = true
	}
	for _, relation := range delta.Relations {
		record := relation.Metadata["relationshipRecord"].(map[string]any)
		for _, previous := range records {
			if previous["id"] == record["id"] && previous["status"] == "disabled" {
				return nil, relationAppendError("关系已停用，不自动重新启用")
			}
		}
		if id := fmt.Sprint(record["id"]); !seen[id] {
			records = append(records, record)
			seen[id] = true
		}
	}
	original["relationshipRecords"], err = json.Marshal(records)
	if err != nil {
		return nil, err
	}
	return json.Marshal(original)
}

type crossRelationAppend struct {
	Domain      string                       `json:"domain"`
	Relations   []RelationClass              `json:"relations"`
	TableHashes map[string]map[string]string `json:"tableHashes"`
}

func (s *Store) appendCrossRelations(ctx context.Context, input crossRelationAppend) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	sources := []string{}
	for sid := range input.TableHashes {
		sources = append(sources, sid)
	}
	sort.Strings(sources)
	for _, sid := range sources {
		var id string
		if err = tx.QueryRowContext(ctx, `SELECT id FROM semora_meta.datasources d WHERE id=$1 AND
			to_jsonb(d)->>'status' IS DISTINCT FROM 'deleted' FOR UPDATE`, sid).Scan(&id); err != nil {
			return err
		}
		for table, expected := range input.TableHashes[sid] {
			var actual string
			if err = tx.QueryRowContext(ctx, `SELECT content_hash FROM semora_meta.datasource_tables WHERE datasource_id=$1 AND table_id=$2`, sid, table).Scan(&actual); err != nil {
				return err
			}
			if actual != expected {
				return relationAppendError("表结构已变化，请重新查询")
			}
		}
	}
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	var raw []byte
	if err = tx.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1 AND deleted_at IS NULL FOR UPDATE`, input.Domain).Scan(&raw); err != nil {
		return err
	}
	var value Ontology
	if err = json.Unmarshal(raw, &value); err != nil {
		return err
	}
	packages, err := readPackages(ctx, tx, value.SourcePackageRefs)
	if err != nil {
		return err
	}
	value = composeReferences(value, packages)
	objects := map[string]ObjectClass{}
	for _, obj := range value.Objects {
		objects[obj.Name] = obj
	}
	for _, r := range input.Relations {
		left, lok := objects[r.From]
		right, rok := objects[r.To]
		if !lok || !rok || left.DataSourceID == right.DataSourceID || input.TableHashes[left.DataSourceID][left.Table] == "" || input.TableHashes[right.DataSourceID][right.Table] == "" {
			return relationAppendError("跨源关系不在当前表结构范围内")
		}
	}
	value.Relations, err = appendRelations(value.Relations, input.Relations)
	if err != nil {
		return err
	}
	if _, err = s.saveEffectiveTx(ctx, tx, value); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *server) appendCrossRelations(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input crossRelationAppend
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20)).Decode(&input) != nil || input.Domain == "" || len(input.Relations) == 0 || len(input.TableHashes) == 0 {
		jsonResponse(w, 422, map[string]string{"error": "关系追加参数不完整"})
		return
	}
	if err := s.store.appendCrossRelations(r.Context(), input); err != nil {
		if e, ok := err.(*validationError); ok {
			jsonResponse(w, 409, map[string]string{"error": fmt.Sprint(e.issues)})
		} else {
			jsonResponse(w, 500, map[string]string{"error": "关系追加失败，原配置已保留"})
		}
		return
	}
	jsonResponse(w, 200, map[string]string{"status": "updated"})
}
