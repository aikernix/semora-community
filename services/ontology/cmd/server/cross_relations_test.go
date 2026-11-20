package main

import (
	"context"
	"os"
	"testing"
)

func TestCrossSourceRecordsFollowEffectivePublication(t *testing.T) {
	dsn := os.Getenv("ONTOLOGY_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("requires disposable ONTOLOGY_TEST_DATABASE_URL")
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
	value := sourceOntology()
	value.Domain = "cross_record_publication_test"
	if _, err = store.createDomain(ctx, value.Domain, "Cross records"); err != nil {
		t.Fatal(err)
	}
	saved, err := store.saveEffective(ctx, value)
	if err != nil {
		t.Fatal(err)
	}
	var count int
	if err = store.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.domain_cross_source_relations WHERE domain=$1`, value.Domain).Scan(&count); err != nil || count != 0 {
		t.Fatalf("duplicate cross record: %d %v", count, err)
	}
	invalid := saved
	invalid.Relations = append([]RelationClass(nil), saved.Relations...)
	invalid.Relations[0].Condition = "missing.id = client.customer_id"
	if _, err = store.saveEffective(ctx, invalid); err == nil {
		t.Fatal("invalid publication accepted")
	}
	if err = store.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.domain_cross_source_relations WHERE domain=$1`, value.Domain).Scan(&count); err != nil || count != 0 {
		t.Fatal("failed publication changed cross records")
	}
	saved.Relations = []RelationClass{}
	if _, err = store.saveEffective(ctx, saved); err != nil {
		t.Fatal(err)
	}
	if err = store.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.domain_cross_source_relations WHERE domain=$1`, value.Domain).Scan(&count); err != nil || count != 0 {
		t.Fatal("removed cross records survived publication")
	}
}
