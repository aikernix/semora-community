package main

import "testing"

func TestKnowledgeBaseBindingIsOptional(t *testing.T) {
	ontology := demoOntology()
	ontology.KnowledgeBaseIDs = nil
	if issues := validateOntology(ontology); len(issues) != 0 {
		t.Fatalf("knowledge must be optional: %#v", issues)
	}
}

func TestValidationRejectsUnsafeTable(t *testing.T) {
	ontology := demoOntology()
	ontology.Objects[0].Table = "objects; DROP TABLE records"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unsafe mapping accepted")
	}
}

func TestValidationRejectsUnknownRelationshipEndpoint(t *testing.T) {
	ontology := demoOntology()
	ontology.Relations[0].To = "missing_object"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unknown relationship endpoint must block publication")
	}
}

func TestRelationshipPathFindsCustomerToBranch(t *testing.T) {
	objects, relations, found := findRelationshipPath(demoOntology(), "customer", "branch")
	if !found || len(relations) != 2 {
		t.Fatalf("expected two-hop path, found=%v relations=%d", found, len(relations))
	}
	if objects[0] != "customer" || objects[len(objects)-1] != "branch" {
		t.Fatalf("unexpected path: %#v", objects)
	}
}

func TestMetricCandidateUpdatesDraftAndBumpsPatchVersion(t *testing.T) {
	ontology := demoOntology()
	originalCount := len(ontology.Metrics)
	err := applyCandidate(&ontology, "metric", map[string]any{
		"name": "customer_count", "label": "贷款客户数", "expression": "COUNT(customer.id)",
		"grain": "branch", "dimensions": []any{"branch"}, "format": "integer",
	})
	if err != nil {
		t.Fatalf("candidate should apply: %v", err)
	}
	if len(ontology.Metrics) != originalCount+1 {
		t.Fatalf("candidate should add one metric")
	}
	if issues := validateOntology(ontology); len(issues) != 0 {
		t.Fatalf("candidate result should remain valid: %#v", issues)
	}
	if got := bumpPatchVersion("1.2.9"); got != "1.2.10" {
		t.Fatalf("unexpected bumped version: %s", got)
	}
}

func TestInvalidPropertyCandidateDoesNotApply(t *testing.T) {
	ontology := demoOntology()
	err := applyCandidate(&ontology, "property", map[string]any{
		"object": "missing", "property": map[string]any{
			"name": "x", "column": "x", "type": "string",
		},
	})
	if err == nil {
		t.Fatal("missing target object should be rejected")
	}
}

func TestValidationRejectsUnknownScopeField(t *testing.T) {
	ontology := demoOntology()
	ontology.ScopeField = "missing.id"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unknown scope field must block publication")
	}
}
