package main

import "testing"

func sourceOntology() Ontology {
	return Ontology{Domain: "commerce", Name: "跨源", Version: "1", DataSourceID: "a",
		Objects: []ObjectClass{
			{Name: "client", Label: "客户", Table: "customers", DataSourceID: "a", PrimaryKey: "customer_id", Properties: []Property{{Name: "customer_id", Label: "编号", Column: "customer_id", Type: "string"}, {Name: "amount", Label: "金额", Column: "amount", Type: "decimal"}}},
			{Name: "balance", Label: "账户", Table: "balances", DataSourceID: "b", PrimaryKey: "customer_id", Properties: []Property{{Name: "customer_id", Label: "编号", Column: "customer_id", Type: "string"}, {Name: "amount", Label: "金额", Column: "amount", Type: "decimal"}}},
		}, Relations: []RelationClass{{Name: "client_balance", From: "client", To: "balance", Condition: "client.customer_id = balance.customer_id", Cardinality: "1:N"}},
	}
}

func TestCrossSourceDefinitionsArePublishedWithoutEngineProjection(t *testing.T) {
	ontology := sourceOntology()
	ontology.Relations[0].Condition += " AND client.amount > 0"
	ontology.Metrics = []Metric{{Name: "combined", Label: "合计", Expression: "SUM(client.amount) + SUM(balance.amount)", Grain: "client"}}
	snapshot, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Ontology.Objects) != 2 || len(snapshot.Ontology.Relations) != 1 {
		t.Fatal("cross-source definitions lost")
	}
	if snapshot.WrenArtifact != nil {
		t.Fatal("publication must not generate MDL")
	}
	if snapshot.Ontology.Relations[0].Condition != ontology.Relations[0].Condition {
		t.Fatal("relation changed")
	}
}

func TestSourceScopeMustBelongToItsSource(t *testing.T) {
	ontology := sourceOntology()
	ontology.SourceScopes = map[string]string{"b": "client.customer_id"}
	if _, err := preparePublication(ontology); err == nil {
		t.Fatal("invalid scope accepted")
	}
}

func TestCompositeRelationshipAndConstantsSurvivePublication(t *testing.T) {
	ontology := sourceOntology()
	condition := "client.customer_id = balance.customer_id AND client.amount = balance.amount AND balance.kind = 'module.object.Record'"
	ontology.Relations[0].Condition = condition
	snapshot, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Ontology.Relations[0].Condition != condition {
		t.Fatal("condition was rewritten")
	}
	if len(sqlReferences(condition)) != 5 {
		t.Fatal("string literal interpreted as a field")
	}
}

func TestDistinctObjectRequiresDeclaredProjectionKeys(t *testing.T) {
	ontology := sourceOntology()
	ontology.Objects[0].Properties = ontology.Objects[0].Properties[:1]
	ontology.Objects[0].Projection = &ObjectProjection{Distinct: true}
	snapshot, err := preparePublication(ontology)
	if err != nil {
		t.Fatal(err)
	}
	if !snapshot.Ontology.Objects[1].Projection.Distinct {
		t.Fatal("distinct projection lost")
	}
	ontology.Objects[0].PrimaryKey = "hidden_id"
	if _, err := preparePublication(ontology); err == nil {
		t.Fatal("undeclared projection key accepted")
	}
}
