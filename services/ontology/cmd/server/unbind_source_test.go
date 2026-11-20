package main

import (
	"context"
	"encoding/json"
	"os"
	"testing"
)

func TestWithoutSourcePrunesOnlyDetachedReferences(t *testing.T) {
	value := sourceOntology()
	value.SourcePackageRefs = []SourcePackageRef{{DataSourceID: "a"}, {DataSourceID: "b"}}
	value.Metrics = []Metric{{Name: "removed", Expression: "SUM(client.amount)", Grain: "client"}, {Name: "kept", Expression: "SUM(balance.amount)", Grain: "balance"}}
	value.KnowledgeBaseIDs = []string{"knowledge"}
	result := withoutSource(value, "a")
	if len(result.Objects) != 1 || result.Objects[0].DataSourceID != "b" || len(result.Relations) != 0 || len(result.SourcePackageRefs) != 1 {
		t.Fatal("source references not pruned", result)
	}
	if len(result.Metrics) != 1 || result.Metrics[0].Name != "kept" || len(result.KnowledgeBaseIDs) != 1 {
		t.Fatal("unrelated settings lost")
	}
	if len(value.Objects) != 2 || len(value.SourcePackageRefs) != 2 {
		t.Fatal("input mutated")
	}
	empty := withoutSource(result, "b")
	if empty.DataSourceID != "" || len(empty.Objects) != 0 || len(empty.SourcePackageRefs) != 0 {
		t.Fatal("last binding not cleared")
	}
	if _, err := configurationSnapshot(empty); err != nil {
		t.Fatal(err)
	}
}

func TestUnbindLastSourcePublishesEmptyConfiguration(t *testing.T) {
	dsn := os.Getenv("ONTOLOGY_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("requires disposable database")
	}
	ctx := context.Background()
	if err := migrateStore(ctx, dsn, false); err != nil {
		t.Fatal(err)
	}
	store, err := newStore(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer store.db.Close()
	domain := "unbind_test"
	if _, err = store.createDomain(ctx, domain, "Unbind test"); err != nil {
		t.Fatal(err)
	}
	value := sourceOntology()
	value.Domain = domain
	if _, err = store.saveEffective(ctx, value); err != nil {
		t.Fatal(err)
	}
	if _, err = store.unbindSource(ctx, domain, "a"); err != nil {
		t.Fatal(err)
	}
	empty, err := store.unbindSource(ctx, domain, "b")
	if err != nil {
		t.Fatal(err)
	}
	if len(empty.Objects) != 0 || empty.DataSourceID != "" {
		t.Fatal("last source still active")
	}
	var payload []byte
	var snapshot Ontology
	if err = store.db.QueryRowContext(ctx, `SELECT payload FROM ontology_core.snapshots WHERE domain=$1 ORDER BY published_at DESC LIMIT 1`, domain).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(payload, &snapshot); err != nil || len(snapshot.Objects) != 0 || snapshot.DataSourceID != "" {
		t.Fatal("empty query snapshot not published", err, snapshot)
	}
}
