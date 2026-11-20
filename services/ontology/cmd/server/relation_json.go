package main

import "encoding/json"

// Scoring is retired from relation contracts. Historical payloads remain readable.
func stripRelationScores(value any) any {
	switch v := value.(type) {
	case map[string]any:
		result := map[string]any{}
		for k, item := range v {
			if k != "confidence" {
				result[k] = stripRelationScores(item)
			}
		}
		return result
	case []any:
		result := make([]any, len(v))
		for i, item := range v {
			result[i] = stripRelationScores(item)
		}
		return result
	default:
		return value
	}
}
func (r RelationClass) MarshalJSON() ([]byte, error) {
	type plain RelationClass
	value := plain(r)
	if r.Metadata != nil {
		value.Metadata = stripRelationScores(r.Metadata).(map[string]any)
	}
	return json.Marshal(value)
}
