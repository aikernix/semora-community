package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestPublicationDoesNotRequireNavigationColumns(t *testing.T) {
	ontology := sourceOntology()
	// A business property and a target object can share a name. No synthetic
	// navigation column is needed by the SQL-native query flow.
	ontology.Objects[0].Properties = append(ontology.Objects[0].Properties,
		Property{Name: "balance", Label: "当前值", Column: "balance_value", Type: "decimal"})
	ontology.Relations = append(ontology.Relations, RelationClass{
		Name: "other_balance", From: "client", To: "balance", Cardinality: "N:1",
		Condition: "client.customer_id = balance.customer_id AND balance.kind='module.object.Record'",
	})
	ontology.Objects[1].PrimaryKey = ""
	snapshot, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.ArtifactVersion != ontologyFormatVersion || snapshot.WrenArtifact != nil {
		t.Fatal("unexpected projection")
	}
	if snapshot.Ontology.Objects[0].PrimaryKey != "" {
		t.Fatal("fabricated a primary key")
	}
	path, relations, found := findRelationshipPath(snapshot.Ontology, "client", "balance")
	if !found || len(path) != 2 || len(relations) != 1 {
		t.Fatal("relation traversal changed")
	}
	payload, _ := json.Marshal(snapshot)
	if strings.Contains(string(payload), "wrenArtifact") {
		t.Fatal("new snapshot exposes unused MDL")
	}
}

func TestPublicationHashIsStableAndCoversDefinitions(t *testing.T) {
	ontology := sourceOntology()
	first, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	ontology.Objects[0], ontology.Objects[1] = ontology.Objects[1], ontology.Objects[0]
	reordered, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if first.ContentHash != reordered.ContentHash {
		t.Fatal("object order changed content hash")
	}
	ontology.Relations[0].Condition += " AND balance.amount > 0"
	changed, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if first.ContentHash == changed.ContentHash {
		t.Fatal("relationship meaning not bound to snapshot")
	}
}

func TestPublicationStillRejectsInvalidDefinitions(t *testing.T) {
	for _, kind := range []string{"endpoint", "metric", "distinct", "duplicate", "source"} {
		t.Run(kind, func(t *testing.T) {
			ontology := sourceOntology()
			switch kind {
			case "endpoint":
				ontology.Relations[0].To = "missing"
			case "metric":
				ontology.Metrics = []Metric{{Name: "bad", Label: "错误", Grain: "client", Expression: "SUM(client.missing)"}}
			case "distinct":
				ontology.Objects[0].Projection = &ObjectProjection{Distinct: true}
				ontology.Objects[0].Properties = nil
			case "duplicate":
				ontology.Objects = append(ontology.Objects, ontology.Objects[0])
			case "source":
				ontology.SourceScopes = map[string]string{"missing": "client.customer_id"}
			}
			if _, err := preparePublication(ontology); err == nil {
				t.Fatal("invalid definition published")
			}
		})
	}
}
