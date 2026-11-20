package main

import (
	"context"
	"os"
	"reflect"
	"testing"
)

func TestEffectiveOntologyPreservesBindingsAndResolvesLegacyMappings(t *testing.T) {
	previous := sourceOntology()
	previous.Version = "1.2.3"
	previous.KnowledgeBaseIDs = []string{"keep"}
	input := sourceOntology()
	input.Objects[0].DataSourceID = ""
	input.KnowledgeBaseIDs = []string{"overwrite"}
	result, err := effectiveOntology(input, previous)
	if err != nil {
		t.Fatal(err)
	}
	if result.Version != "1.2.4" || !reflect.DeepEqual(result.KnowledgeBaseIDs, previous.KnowledgeBaseIDs) {
		t.Fatal("revision or bindings changed incorrectly")
	}
	if result.Objects[0].DataSourceID != "a" || result.Objects[1].DataSourceID != "b" {
		t.Fatal("source mapping lost")
	}
	if input.Objects[0].DataSourceID != "" {
		t.Fatal("input mutated")
	}
	input.DataSourceID = ""
	if _, err := effectiveOntology(input, previous); err == nil {
		t.Fatal("missing mapping accepted")
	}
	input.Objects[0].DataSourceID = "a"
	result, err = effectiveOntology(input, previous)
	if err != nil || result.DataSourceID != "a" {
		t.Fatal("explicit per-object sources must work without a global default")
	}
}

// Run against a disposable database only; never enable this for an application database.
func TestEffectiveSaveTransaction(t *testing.T) {
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
	domain := "effective_save_test"
	if _, err = store.createDomain(ctx, domain, "Save test"); err != nil {
		t.Fatal(err)
	}
	if _, err = store.createDomain(ctx, domain, "Overwrite"); err != errDomainExists {
		t.Fatalf("duplicate: %v", err)
	}
	value := sourceOntology()
	value.Domain = domain
	saved, err := store.saveEffective(ctx, value)
	if err != nil {
		t.Fatal(err)
	}
	var payload []byte
	var hash string
	if err = store.db.QueryRowContext(ctx, `SELECT payload,content_hash FROM ontology_core.snapshots WHERE domain=$1 ORDER BY published_at DESC LIMIT 1`, domain).Scan(&payload, &hash); err != nil {
		t.Fatal(err)
	}
	originalHash := hash
	originalPayload := string(payload)
	value.Objects[0].DataSourceID = ""
	value.DataSourceID = ""
	if _, err = store.saveEffective(ctx, value); err == nil {
		t.Fatal("invalid configuration saved")
	}
	current, err := store.getDraft(ctx, domain)
	if err != nil || current.Version != saved.Version {
		t.Fatal("failed save changed configuration")
	}
	if err = store.bindEffectiveKnowledgeBases(ctx, domain, []string{"keep"}); err != nil {
		t.Fatal(err)
	}
	current, err = store.getDraft(ctx, domain)
	if err != nil {
		t.Fatal(err)
	}
	current.Name = "Updated"
	current.KnowledgeBaseIDs = []string{"overwrite"}
	saved, err = store.saveEffective(ctx, current)
	if err != nil {
		t.Fatal(err)
	}
	if saved.Name != "Updated" || !reflect.DeepEqual(saved.KnowledgeBaseIDs, []string{"keep"}) {
		t.Fatal("save lost binding or configuration")
	}
	if err = store.db.QueryRowContext(ctx, `SELECT content_hash FROM ontology_core.snapshots WHERE domain=$1 ORDER BY published_at DESC LIMIT 1`, domain).Scan(&hash); err != nil {
		t.Fatal(err)
	}
	if hash == originalHash {
		t.Fatal("new configuration not activated")
	}
	if err = store.db.QueryRowContext(ctx, `SELECT payload FROM ontology_core.snapshots WHERE domain=$1 AND content_hash=$2`, domain, originalHash).Scan(&payload); err != nil || string(payload) != originalPayload {
		t.Fatal("historical snapshot changed")
	}
	var count int
	if err = store.db.QueryRowContext(ctx, `SELECT count(*) FROM ontology_core.snapshots WHERE domain=$1`, domain).Scan(&count); err != nil || count != 3 {
		t.Fatalf("expected three atomic saves, got %d: %v", count, err)
	}
}

func TestRelationRemovalPublishesAndPhysicallyRemovesConfigurationRecord(t *testing.T) {
	dsn := os.Getenv("ONTOLOGY_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("requires disposable database")
	}
	ctx := context.Background()
	store, err := newStore(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer store.db.Close()
	value := sourceOntology()
	value.Domain = "relation_crud_contract"
	if _, err = store.createDomain(ctx, value.Domain, "Relations"); err != nil {
		t.Fatal(err)
	}
	saved, err := store.saveEffective(ctx, value)
	if err != nil {
		t.Fatal(err)
	}
	saved.Relations[0].Label = "Updated relation"
	edited, err := store.saveEffective(ctx, saved)
	if err != nil {
		t.Fatal(err)
	}
	if edited.Version == saved.Version {
		t.Fatal("edit did not publish")
	}
	edited.Relations = []RelationClass{}
	deleted, err := store.saveEffective(ctx, edited)
	if err != nil {
		t.Fatal(err)
	}
	current, err := store.getDraft(ctx, value.Domain)
	if err != nil || len(current.Relations) != 0 {
		t.Fatal("relation survived physical configuration removal")
	}
	snapshot, err := store.latestSnapshot(ctx, value.Domain)
	if err != nil || len(snapshot.Ontology.Relations) != 0 || snapshot.Version != deleted.Version {
		t.Fatal("snapshot differs from configuration")
	}
}
