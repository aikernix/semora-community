package main

import (
	"fmt"
	"sort"
	"strings"
)

const ontologyFormatVersion = "semora-ontology-v1"

func validateOntology(ontology Ontology) []ValidationIssue {
	issues := []ValidationIssue{}
	if strings.TrimSpace(ontology.Domain) == "" {
		issues = append(issues, ValidationIssue{"domain", "业务域不能为空"})
	}
	if strings.TrimSpace(ontology.Name) == "" {
		issues = append(issues, ValidationIssue{"name", "本体名称不能为空"})
	}
	if strings.TrimSpace(ontology.Version) == "" {
		issues = append(issues, ValidationIssue{"version", "版本不能为空"})
	}
	if strings.TrimSpace(ontology.DataSourceID) == "" {
		issues = append(issues, ValidationIssue{"dataSourceId", "必须绑定已登记的数据源"})
	}
	if len(ontology.KnowledgeBaseIDs) > 0 {
		for index, value := range ontology.KnowledgeBaseIDs {
			if strings.TrimSpace(value) == "" {
				issues = append(issues, ValidationIssue{fmt.Sprintf("knowledgeBaseIds[%d]", index), "知识库 ID 不能为空"})
			}
		}
	}
	if len(ontology.Objects) == 0 && len(ontology.SourcePackageRefs) == 0 {
		issues = append(issues, ValidationIssue{"objects", "至少需要一个对象类"})
	}
	objects := map[string]ObjectClass{}
	for index, object := range ontology.Objects {
		path := fmt.Sprintf("objects[%d]", index)
		if object.Name == "" || object.Label == "" || object.Table == "" {
			issues = append(issues, ValidationIssue{path, "对象名、中文标签和物理表映射必须完整"})
		}
		if _, exists := objects[object.Name]; exists {
			issues = append(issues, ValidationIssue{path + ".name", "对象名重复"})
		}
		objects[object.Name] = object
		properties := map[string]bool{}
		for propertyIndex, property := range object.Properties {
			propertyPath := fmt.Sprintf("%s.properties[%d]", path, propertyIndex)
			if property.Name == "" || property.Label == "" || property.Column == "" || property.Type == "" {
				issues = append(issues, ValidationIssue{propertyPath, "属性名、中文标签、列映射和类型必须完整"})
			}
			if properties[property.Name] {
				issues = append(issues, ValidationIssue{propertyPath + ".name", "属性名重复"})
			}
			properties[property.Name] = true
		}
	}
	relations := map[string]bool{}
	validCardinality := map[string]bool{"1:1": true, "1:N": true, "N:1": true, "N:N": true, "unknown": true}
	for index, relation := range ontology.Relations {
		path := fmt.Sprintf("relations[%d]", index)
		if relation.Name == "" || relation.Condition == "" {
			issues = append(issues, ValidationIssue{path, "关系名和连接条件必须完整"})
		}
		if relations[relation.Name] {
			issues = append(issues, ValidationIssue{path + ".name", "关系名重复"})
		}
		relations[relation.Name] = true
		if _, ok := objects[relation.From]; !ok {
			issues = append(issues, ValidationIssue{path + ".from", "起点对象不存在"})
		}
		if _, ok := objects[relation.To]; !ok {
			issues = append(issues, ValidationIssue{path + ".to", "终点对象不存在"})
		}
		if !validCardinality[relation.Cardinality] {
			issues = append(issues, ValidationIssue{path + ".cardinality", "基数必须是 1:1、1:N、N:1、N:N 或 unknown"})
		}
	}
	metrics := map[string]bool{}
	for index, metric := range ontology.Metrics {
		path := fmt.Sprintf("metrics[%d]", index)
		if metric.Name == "" || metric.Label == "" || metric.Expression == "" || metric.Grain == "" {
			issues = append(issues, ValidationIssue{path, "指标名、中文标签、表达式和粒度必须完整"})
		}
		if metrics[metric.Name] {
			issues = append(issues, ValidationIssue{path + ".name", "指标名重复"})
		}
		metrics[metric.Name] = true
		if _, ok := objects[metric.Grain]; !ok {
			issues = append(issues, ValidationIssue{path + ".grain", "指标粒度对象不存在"})
		}
	}
	validateFieldReference := func(path, reference string, allowEmpty bool) {
		if reference == "" && allowEmpty {
			return
		}
		parts := strings.Split(reference, ".")
		if len(parts) != 2 {
			issues = append(issues, ValidationIssue{path, "字段必须使用 object.property 格式"})
			return
		}
		object, ok := objects[parts[0]]
		if !ok {
			issues = append(issues, ValidationIssue{path, "字段所属对象不存在"})
			return
		}
		if parts[1] == "id" && object.PrimaryKey != "" {
			return
		}
		for _, property := range object.Properties {
			if property.Name == parts[1] {
				return
			}
		}
		issues = append(issues, ValidationIssue{path, "字段属性不存在"})
	}
	validateFieldReference("scopeField", ontology.ScopeField, true)
	validateFieldReference("timeField", ontology.TimeField, true)
	validOperators := map[string]bool{"eq": true, "ne": true, "gt": true, "gte": true, "lt": true, "lte": true, "in": true, "between": true}
	for index, filter := range ontology.DefaultFilters {
		path := fmt.Sprintf("defaultFilters[%d]", index)
		parts := strings.Split(filter.Field, ".")
		if len(parts) != 2 {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤字段必须使用 object.property 格式"})
			continue
		}
		object, ok := objects[parts[0]]
		if !ok {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤对象不存在"})
			continue
		}
		propertyFound := false
		for _, property := range object.Properties {
			if property.Name == parts[1] {
				propertyFound = true
				break
			}
		}
		if !propertyFound {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤属性不存在"})
		}
		if !validOperators[filter.Operator] {
			issues = append(issues, ValidationIssue{path + ".operator", "默认过滤运算符不受支持"})
		}
	}
	return append(issues, validateMappings(ontology)...)
}

func canonicalOntology(input Ontology) Ontology {
	result := input
	result.Objects = append([]ObjectClass(nil), input.Objects...)
	result.Relations = append([]RelationClass(nil), input.Relations...)
	result.Metrics = append([]Metric(nil), input.Metrics...)
	sort.Slice(result.Objects, func(i, j int) bool { return result.Objects[i].Name < result.Objects[j].Name })
	for index := range result.Objects {
		result.Objects[index].Properties = append([]Property(nil), result.Objects[index].Properties...)
		sort.Slice(result.Objects[index].Properties, func(i, j int) bool {
			return result.Objects[index].Properties[i].Name < result.Objects[index].Properties[j].Name
		})
	}
	sort.Slice(result.Relations, func(i, j int) bool { return result.Relations[i].Name < result.Relations[j].Name })
	sort.Slice(result.Metrics, func(i, j int) bool { return result.Metrics[i].Name < result.Metrics[j].Name })
	return result
}

func findRelationshipPath(ontology Ontology, from, to string) ([]string, []RelationClass, bool) {
	type step struct {
		object    string
		relations []RelationClass
		objects   []string
	}
	queue := []step{{from, nil, []string{from}}}
	visited := map[string]bool{from: true}
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		if current.object == to {
			return current.objects, current.relations, true
		}
		for _, relation := range ontology.Relations {
			next := ""
			if relation.From == current.object {
				next = relation.To
			} else if relation.To == current.object {
				next = relation.From
			}
			if next == "" || visited[next] {
				continue
			}
			visited[next] = true
			queue = append(queue, step{
				next,
				append(append([]RelationClass{}, current.relations...), relation),
				append(append([]string{}, current.objects...), next),
			})
		}
	}
	return nil, nil, false
}
