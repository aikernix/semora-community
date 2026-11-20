package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"regexp"
)

type sourcePackage struct {
	Objects   []ObjectClass   `json:"objects"`
	Relations []RelationClass `json:"relations"`
}
type packageReader interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func readPackages(ctx context.Context, q packageReader, refs []SourcePackageRef) (map[string]sourcePackage, error) {
	result := map[string]sourcePackage{}
	for _, ref := range refs {
		if _, ok := result[ref.DataSourceID]; ok {
			continue
		}
		var raw []byte
		if err := q.QueryRowContext(ctx, `SELECT payload FROM semora_meta.datasource_ontology_packages WHERE datasource_id=$1`, ref.DataSourceID).Scan(&raw); err != nil {
			return nil, err
		}
		var value sourcePackage
		if err := json.Unmarshal(raw, &value); err != nil {
			return nil, err
		}
		result[ref.DataSourceID] = value
	}
	return result, nil
}
func packageTag(metadata map[string]any) string {
	s, _ := metadata["sourcePackageId"].(string)
	return s
}
func copyMetadata(value map[string]any) map[string]any {
	r := map[string]any{}
	for k, v := range value {
		r[k] = v
	}
	return r
}

func validateInheritedMappings(input Ontology, packages map[string]sourcePackage) error {
	for _, relation := range input.Relations {
		sid := packageTag(relation.Metadata)
		pkg, bound := packages[sid]
		if sid == "" || !bound {
			continue
		}
		found := false
		for _, base := range pkg.Relations {
			if relation.Name == base.Name {
				found = true
				if relation.From != base.From || relation.To != base.To || relation.Condition != base.Condition || relation.Cardinality != base.Cardinality || relation.JoinType != base.JoinType || relation.Optional != base.Optional {
					return &validationError{issues: []ValidationIssue{{"relations", "数据源引用关系请在数据源的关联关系中维护"}}}
				}
			}
		}
		if !found {
			return &validationError{issues: []ValidationIssue{{"relations", "数据源关系已变化，请刷新配置"}}}
		}
	}
	return nil
}

// Persist domain definitions, never editable copies of generated mappings.
// Cosmetic object customizations are kept separately from physical mappings.
func referenceDefinition(input Ontology, packages map[string]sourcePackage) Ontology {
	result := input
	result.SourcePackageRefs = append([]SourcePackageRef(nil), input.SourcePackageRefs...)
	for i := range result.SourcePackageRefs {
		result.SourcePackageRefs[i].Version = ""
	}
	result.Metadata = copyMetadata(input.Metadata)
	objects := []ObjectClass{}
	relations := []RelationClass{}
	overrides := map[string]any{}
	if old, ok := input.Metadata["sourceObjectOverrides"].(map[string]any); ok {
		for k, v := range old {
			overrides[k] = v
		}
	}
	for _, object := range input.Objects {
		sid := packageTag(object.Metadata)
		if sid == "" {
			objects = append(objects, object)
			continue
		}
		for _, base := range packages[sid].Objects {
			if base.Name == object.Name {
				patch := map[string]any{}
				if object.Label != base.Label {
					patch["label"] = object.Label
				}
				if !reflect.DeepEqual(object.Aliases, base.Aliases) {
					patch["aliases"] = object.Aliases
				}
				props := map[string]any{}
				for _, p := range object.Properties {
					for _, b := range base.Properties {
						if p.Name == b.Name {
							ppatch := map[string]any{}
							if p.Label != b.Label {
								ppatch["label"] = p.Label
							}
							if p.Description != b.Description {
								ppatch["description"] = p.Description
							}
							if !reflect.DeepEqual(p.Aliases, b.Aliases) {
								ppatch["aliases"] = p.Aliases
							}
							if p.Sensitive != b.Sensitive {
								ppatch["sensitive"] = p.Sensitive
							}
							if len(ppatch) > 0 {
								props[p.Name] = ppatch
							}
						}
					}
				}
				if len(props) > 0 {
					patch["properties"] = props
				}
				if len(patch) > 0 {
					overrides[object.Name] = patch
				}
			}
		}
	}
	for _, relation := range input.Relations {
		if packageTag(relation.Metadata) == "" {
			relations = append(relations, relation)
		}
	}
	if len(overrides) > 0 {
		result.Metadata["sourceObjectOverrides"] = overrides
	} else {
		delete(result.Metadata, "sourceObjectOverrides")
	}
	result.Objects = objects
	result.Relations = relations
	return result
}
func applyObjectOverride(object ObjectClass, metadata map[string]any) ObjectClass {
	all, _ := metadata["sourceObjectOverrides"].(map[string]any)
	patch, _ := all[object.Name].(map[string]any)
	raw, _ := json.Marshal(patch)
	var decoded struct {
		Label      *string   `json:"label"`
		Aliases    *[]string `json:"aliases"`
		Properties map[string]struct {
			Label       *string   `json:"label"`
			Description *string   `json:"description"`
			Aliases     *[]string `json:"aliases"`
			Sensitive   *bool     `json:"sensitive"`
		} `json:"properties"`
	}
	_ = json.Unmarshal(raw, &decoded)
	if decoded.Label != nil {
		object.Label = *decoded.Label
	}
	if decoded.Aliases != nil {
		object.Aliases = *decoded.Aliases
	}
	object.Properties = append([]Property(nil), object.Properties...)
	for i, p := range object.Properties {
		patch, ok := decoded.Properties[p.Name]
		if !ok {
			continue
		}
		if patch.Label != nil {
			p.Label = *patch.Label
		}
		if patch.Description != nil {
			p.Description = *patch.Description
		}
		if patch.Aliases != nil {
			p.Aliases = *patch.Aliases
		}
		if patch.Sensitive != nil {
			p.Sensitive = *patch.Sensitive
		}
		object.Properties[i] = p
	}
	return object
}
func composeReferences(definition Ontology, packages map[string]sourcePackage) Ontology {
	result := definition
	result.Objects = append([]ObjectClass{}, definition.Objects...)
	result.Relations = append([]RelationClass{}, definition.Relations...)
	seen := map[string]bool{}
	for _, ref := range definition.SourcePackageRefs {
		if seen[ref.DataSourceID] {
			continue
		}
		seen[ref.DataSourceID] = true
		for _, object := range packages[ref.DataSourceID].Objects {
			object.Metadata = copyMetadata(object.Metadata)
			object.Metadata["sourcePackageId"] = ref.DataSourceID
			result.Objects = append(result.Objects, applyObjectOverride(object, definition.Metadata))
		}
		for _, relation := range packages[ref.DataSourceID].Relations {
			relation.Metadata = copyMetadata(relation.Metadata)
			relation.Metadata["sourcePackageId"] = ref.DataSourceID
			result.Relations = append(result.Relations, relation)
		}
	}
	if len(result.Objects) > 0 {
		result.DataSourceID = result.Objects[0].DataSourceID
	}
	return result
}

var configurationField = regexp.MustCompile(`"?([\p{L}_][\p{L}\p{N}_]*)"?\s*\.\s*"?([\p{L}_][\p{L}\p{N}_]*)"?`)
var stringLiteral = regexp.MustCompile(`'(?:''|[^'])*'`)

// Called after an explicit successful source-package update, never after scanning.
func pruneInvalidReferences(value Ontology) Ontology {
	fields := map[string]map[string]bool{}
	for _, object := range value.Objects {
		columns := map[string]bool{}
		for _, p := range object.Properties {
			columns[p.Name] = true
		}
		fields[object.Name] = columns
	}
	validExpression := func(expression string) bool {
		for _, m := range configurationField.FindAllStringSubmatch(stringLiteral.ReplaceAllString(expression, ""), -1) {
			if !fields[m[1]][m[2]] {
				return false
			}
		}
		return true
	}
	relations := []RelationClass{}
	for _, r := range value.Relations {
		if fields[r.From] != nil && fields[r.To] != nil && validExpression(r.Condition) {
			relations = append(relations, r)
		}
	}
	value.Relations = relations
	metrics := []Metric{}
	unavailable := []map[string]any{}
	for _, m := range value.Metrics {
		valid := fields[m.Grain] != nil && validExpression(m.Expression)
		for _, d := range m.Dimensions {
			valid = valid && validExpression(d)
		}
		if valid {
			metrics = append(metrics, m)
		} else {
			unavailable = append(unavailable, map[string]any{"name": m.Name, "definition": m, "reason": "引用的表或字段已删除"})
		}
	}
	value.Metrics = metrics
	filters := []DefaultFilter{}
	for _, f := range value.DefaultFilters {
		if validExpression(f.Field) {
			filters = append(filters, f)
		}
	}
	value.DefaultFilters = filters
	if !validExpression(value.ScopeField) {
		value.ScopeField = ""
	}
	if !validExpression(value.TimeField) {
		value.TimeField = ""
	}
	value.SourceScopes = func() map[string]string {
		result := map[string]string{}
		for sid, field := range value.SourceScopes {
			if validExpression(field) {
				result[sid] = field
			}
		}
		return result
	}()
	if len(unavailable) > 0 {
		value.Metadata = copyMetadata(value.Metadata)
		value.Metadata["unavailableMetrics"] = unavailable
	}
	return value
}

func pruneSourceObjects(definition Ontology, packages map[string]sourcePackage) Ontology {
	objects := []ObjectClass{}
	for _, object := range definition.Objects {
		pkg, bound := packages[object.DataSourceID]
		if !bound {
			objects = append(objects, object)
			continue
		}
		var base *ObjectClass
		for _, candidate := range pkg.Objects {
			if candidate.Table == object.Table {
				copy := candidate
				base = &copy
				break
			}
		}
		if base == nil {
			continue
		}
		columns := map[string]bool{}
		for _, p := range base.Properties {
			columns[p.Column] = true
		}
		properties := []Property{}
		primaryExists := false
		for _, p := range object.Properties {
			if columns[p.Column] {
				properties = append(properties, p)
				primaryExists = primaryExists || p.Name == object.PrimaryKey
			}
		}
		object.Properties = properties
		if !primaryExists {
			object.PrimaryKey = ""
		}
		objects = append(objects, object)
	}
	definition.Objects = objects
	return definition
}

func (s *Store) resolveReferences(ctx context.Context, q packageReader, input Ontology) (Ontology, error) {
	packages, err := readPackages(ctx, q, input.SourcePackageRefs)
	if err != nil {
		return Ontology{}, err
	}
	return composeReferences(referenceDefinition(input, packages), packages), nil
}

func (s *Store) migrateReferenceDefinitions(ctx context.Context) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE deleted_at IS NULL ORDER BY domain FOR UPDATE`)
	if err != nil {
		return err
	}
	values := []Ontology{}
	for rows.Next() {
		var raw []byte
		if err = rows.Scan(&raw); err != nil {
			rows.Close()
			return err
		}
		var value Ontology
		if err = json.Unmarshal(raw, &value); err != nil {
			rows.Close()
			return err
		}
		if len(value.SourcePackageRefs) > 0 {
			values = append(values, value)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, value := range values {
		packages, e := readPackages(ctx, tx, value.SourcePackageRefs)
		if e != nil {
			return e
		}
		raw, e := json.Marshal(referenceDefinition(value, packages))
		if e != nil {
			return e
		}
		if _, e = tx.ExecContext(ctx, `UPDATE ontology_core.drafts SET payload=$1::jsonb WHERE domain=$2`, string(raw), value.Domain); e != nil {
			return e
		}
	}
	return tx.Commit()
}

type sourcePackageUpdate struct {
	AppendRelations     bool              `json:"appendRelations,omitempty"`
	DataSourceID        string            `json:"dataSourceId"`
	SchemaHash          string            `json:"schemaHash"`
	GeneratorVersion    string            `json:"generatorVersion"`
	Payload             json.RawMessage   `json:"payload"`
	ExpectedTableHashes map[string]string `json:"expectedTableHashes"`
}

func (s *Store) updateSourcePackage(ctx context.Context, input sourcePackageUpdate) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var sid string
	if err = tx.QueryRowContext(ctx, `SELECT id FROM semora_meta.datasources d WHERE id=$1 AND
		to_jsonb(d)->>'status' IS DISTINCT FROM 'deleted' FOR UPDATE`, input.DataSourceID).Scan(&sid); err != nil {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT table_id,content_hash FROM semora_meta.datasource_tables WHERE datasource_id=$1`, sid)
	if err != nil {
		return err
	}
	actual := map[string]string{}
	for rows.Next() {
		var id, hash string
		if err = rows.Scan(&id, &hash); err != nil {
			rows.Close()
			return err
		}
		actual[id] = hash
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(actual, input.ExpectedTableHashes) {
		return &validationError{issues: []ValidationIssue{{"sourcePackage", "表结构已变化，请重新分析"}}}
	}
	if input.AppendRelations {
		var current []byte
		if err = tx.QueryRowContext(ctx, `SELECT payload FROM semora_meta.datasource_ontology_packages WHERE datasource_id=$1`, sid).Scan(&current); err != nil {
			return err
		}
		input.Payload, err = appendPackageRelations(current, input.Payload)
		if err != nil {
			return err
		}
	}
	if _, err := tx.ExecContext(ctx, `LOCK TABLE ontology_core.drafts IN ROW EXCLUSIVE MODE`); err != nil {
		return err
	}
	domainRows, err := tx.QueryContext(ctx, `SELECT domain,payload FROM ontology_core.drafts WHERE deleted_at IS NULL ORDER BY domain FOR UPDATE`)
	if err != nil {
		return err
	}
	definitions := []Ontology{}
	for domainRows.Next() {
		var domain string
		var raw []byte
		if err = domainRows.Scan(&domain, &raw); err != nil {
			domainRows.Close()
			return err
		}
		var d Ontology
		if err = json.Unmarshal(raw, &d); err != nil {
			domainRows.Close()
			return err
		}
		for _, ref := range d.SourcePackageRefs {
			if ref.DataSourceID == sid {
				definitions = append(definitions, d)
				break
			}
		}
	}
	err = domainRows.Err()
	domainRows.Close()
	if err != nil {
		return err
	}
	// Remove legacy generated copies against the OLD package before replacing it.
	for i, d := range definitions {
		packages, e := readPackages(ctx, tx, d.SourcePackageRefs)
		if e != nil {
			return e
		}
		definitions[i] = referenceDefinition(d, packages)
	}
	var payload sourcePackage
	if err = json.Unmarshal(input.Payload, &payload); err != nil {
		return err
	}
	seenTables := map[string]bool{}
	for _, object := range payload.Objects {
		id, _ := object.Metadata["sourceTableId"].(string)
		if object.DataSourceID != sid || actual[id] == "" || seenTables[id] {
			return &validationError{issues: []ValidationIssue{{"sourcePackage", "数据源包与已扫描表结构不一致"}}}
		}
		seenTables[id] = true
	}
	if len(seenTables) != len(actual) {
		return &validationError{issues: []ValidationIssue{{"sourcePackage", "数据源包未覆盖完整表结构"}}}
	}
	if issues := validateOntology(Ontology{Domain: "source", Name: "Source", Version: "current", DataSourceID: sid,
		Objects: payload.Objects, Relations: payload.Relations, SourcePackageRefs: []SourcePackageRef{{DataSourceID: sid}}}); len(issues) > 0 {
		return &validationError{issues: issues}
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO semora_meta.datasource_ontology_packages
 (datasource_id,schema_hash,status,object_count,relation_count,pending_count,generator_version,payload,error)
 VALUES($1,$2,'ready',$3,$4,0,$5,$6::jsonb,NULL) ON CONFLICT(datasource_id) DO UPDATE SET
 schema_hash=excluded.schema_hash,status='ready',object_count=excluded.object_count,relation_count=excluded.relation_count,
 pending_count=0,generator_version=excluded.generator_version,payload=excluded.payload,error=NULL,generated_at=now(),updated_at=now()`, sid, input.SchemaHash, len(payload.Objects), len(payload.Relations), input.GeneratorVersion, string(input.Payload))
	if err != nil {
		return err
	}
	for _, definition := range definitions {
		for i, ref := range definition.SourcePackageRefs {
			if ref.DataSourceID == sid {
				definition.SourcePackageRefs[i].SchemaHash = input.SchemaHash
				definition.SourcePackageRefs[i].Version = ""
			}
		}
		packages, e := readPackages(ctx, tx, definition.SourcePackageRefs)
		if e != nil {
			return e
		}
		effective := pruneInvalidReferences(composeReferences(pruneSourceObjects(definition, packages), packages))
		if _, e = s.saveEffectiveTx(ctx, tx, effective); e != nil {
			return e
		}
	}
	return tx.Commit()
}
func (s *server) sourcePackage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		jsonResponse(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input sourcePackageUpdate
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 32<<20)).Decode(&input) != nil || input.DataSourceID == "" || input.SchemaHash == "" || input.ExpectedTableHashes == nil {
		jsonResponse(w, 422, map[string]string{"error": "数据源包参数不完整"})
		return
	}
	if err := s.store.updateSourcePackage(r.Context(), input); err != nil {
		var message string
		if e, ok := err.(*validationError); ok {
			message = fmt.Sprint(e.issues)
			jsonResponse(w, 409, map[string]string{"error": message})
		} else {
			jsonResponse(w, 500, map[string]string{"error": "数据源包更新失败，原配置已保留"})
		}
		return
	}
	jsonResponse(w, 200, map[string]string{"status": "updated"})
}
