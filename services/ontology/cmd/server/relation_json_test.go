package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRelationHistoryOmitsScores(t *testing.T) {
	r := RelationClass{Name: "r", Metadata: map[string]any{"relationshipRecord": map[string]any{"confidence": 0.8, "evidence": []any{map[string]any{"confidence": 0.9, "reason": "constraint"}}}}}
	raw, err := json.Marshal(r)
	if err != nil || strings.Contains(string(raw), "confidence") {
		t.Fatalf("score leaked: %s %v", raw, err)
	}
	if r.Metadata["relationshipRecord"].(map[string]any)["confidence"] != 0.8 {
		t.Fatal("history mutated")
	}
}
