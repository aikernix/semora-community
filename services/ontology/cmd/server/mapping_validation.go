package main

import (
	"fmt"
	"regexp"
	"strings"
)

var identifierPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
var referencePattern = regexp.MustCompile(`'(?:''|\\.|[^'])*'|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b`)

// References inside string constants are business values, never field mappings.
func sqlReferences(value string) []string {
	result := []string{}
	for _, token := range referencePattern.FindAllString(value, -1) {
		if !strings.HasPrefix(token, "'") {
			result = append(result, token)
		}
	}
	return result
}

// Only validate definition integrity here. Target SQL syntax, functions and
// cross-source execution strategies belong to query generation and the database.
func validateMappings(ontology Ontology) []ValidationIssue {
	issues := []ValidationIssue{}
	objects := map[string]ObjectClass{}
	columns := map[string]map[string]bool{}
	for i, object := range ontology.Objects {
		path := fmt.Sprintf("objects[%d]", i)
		objects[object.Name] = object
		declared := map[string]bool{}
		for j, property := range object.Properties {
			declared[property.Column] = true
			if !identifierPattern.MatchString(property.Name) || !identifierPattern.MatchString(property.Column) {
				issues = append(issues, ValidationIssue{fmt.Sprintf("%s.properties[%d]", path, j), "属性名和物理列必须是有效标识符"})
			}
		}
		columns[object.Name] = declared
		if !identifierPattern.MatchString(object.Name) || (object.PrimaryKey != "" && !identifierPattern.MatchString(object.PrimaryKey)) {
			issues = append(issues, ValidationIssue{path, "对象名和主键必须是有效标识符"})
		}
		parts := strings.Split(object.Table, ".")
		valid := len(parts) >= 1 && len(parts) <= 2
		for _, part := range parts {
			valid = valid && identifierPattern.MatchString(part)
		}
		if !valid {
			issues = append(issues, ValidationIssue{path + ".table", "物理表必须使用有效的 table 或 schema.table 映射"})
		}
		if object.Projection != nil && object.Projection.Distinct {
			if len(declared) == 0 || (object.PrimaryKey != "" && !declared[object.PrimaryKey]) {
				issues = append(issues, ValidationIssue{path + ".projection", "去重对象必须声明物理列，主键不能引用未声明列"})
			}
		}
	}
	resolve := func(reference string) (ObjectClass, string, bool) {
		parts := strings.SplitN(reference, ".", 2)
		object, found := objects[parts[0]]
		if !found {
			return object, "", false
		}
		for _, property := range object.Properties {
			if property.Name == parts[1] || property.Column == parts[1] {
				return object, property.Column, true
			}
		}
		if object.PrimaryKey != "" && (parts[1] == "id" || parts[1] == object.PrimaryKey) {
			return object, object.PrimaryKey, true
		}
		return object, parts[1], false
	}
	for i, relation := range ontology.Relations {
		path := fmt.Sprintf("relations[%d]", i)
		if !identifierPattern.MatchString(relation.Name) {
			issues = append(issues, ValidationIssue{path + ".name", "关系名必须是有效标识符"})
		}
		for _, ref := range sqlReferences(relation.Condition) {
			object, column, _ := resolve(ref)
			if object.Name == "" {
				issues = append(issues, ValidationIssue{path + ".condition", "连接条件引用了不存在的对象：" + ref})
				continue
			}
			// Existing imports may reference physical join columns absent from the
			// display properties. Preserve them, but never expand a DISTINCT grain.
			if object.Projection != nil && object.Projection.Distinct && !columns[object.Name][column] {
				issues = append(issues, ValidationIssue{path + ".condition", "去重对象的关联列必须已声明：" + ref})
			}
		}
	}
	for i, metric := range ontology.Metrics {
		for _, ref := range sqlReferences(metric.Expression) {
			if _, _, found := resolve(ref); !found {
				issues = append(issues, ValidationIssue{fmt.Sprintf("metrics[%d].expression", i), "指标引用了不存在的对象属性：" + ref})
			}
		}
	}
	for source, reference := range ontology.SourceScopes {
		parts := strings.Split(reference, ".")
		valid := len(parts) == 2
		if valid {
			object, _, found := resolve(reference)
			valid = found && objectSource(object, ontology) == source
		}
		if !valid {
			issues = append(issues, ValidationIssue{"sourceScopes." + source, "范围字段必须属于对应数据源的已声明对象属性"})
		}
	}
	return issues
}
