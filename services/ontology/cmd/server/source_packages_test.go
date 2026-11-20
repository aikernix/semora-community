package main

import (
	"context"
	"encoding/json"
	"os"
	"testing"
)

func TestReferenceDefinitionStoresNoGeneratedCopies(t *testing.T) {
	value := sourceOntology()
	base := value.Objects[0]
	base.Metadata = map[string]any{"sourcePackageId": "a"}
	value.Objects[0] = base
	value.Objects[0].Label = "Customized label"
	generated := RelationClass{Name: "generated", From: "client", To: "client", Condition: "client.customer_id = client.customer_id", Cardinality: "1:1", Metadata: map[string]any{"sourcePackageId": "a"}}
	value.Relations = append(value.Relations, generated)
	value.SourcePackageRefs = []SourcePackageRef{{DataSourceID: "a"}}
	packages := map[string]sourcePackage{"a": {Objects: []ObjectClass{base}, Relations: []RelationClass{generated}}}
	definition := referenceDefinition(value, packages)
	if len(definition.Objects) != 1 || len(definition.Relations) != 1 {
		t.Fatal("generated definitions were duplicated")
	}
	composed := composeReferences(definition, packages)
	if len(composed.Objects) != 2 || len(composed.Relations) != 2 || composed.Objects[1].Label != "Customized label" {
		t.Fatal("composition or domain customization lost")
	}
	packages["a"] = sourcePackage{}
	cleaned := pruneInvalidReferences(composeReferences(definition, packages))
	if len(cleaned.Objects) != 1 || len(cleaned.Relations) != 0 {
		t.Fatal("cross-source relation to removed table survived")
	}
}

func TestRemovedFieldAndSourceScopesArePruned(t *testing.T) {
	value := sourceOntology()
	value.Objects[0].Properties = value.Objects[0].Properties[:1]
	value.Relations[0].Condition = "client.amount = balance.amount"
	value.SourceScopes = map[string]string{"a": "client.amount"}
	value.Metrics = []Metric{{Name: "bad", Grain: "client", Expression: "SUM(client.amount)"}}
	value = pruneInvalidReferences(value)
	if len(value.Relations) != 0 || len(value.Metrics) != 0 || len(value.SourceScopes) != 0 {
		t.Fatal("deleted field retained")
	}
}

func TestSourceUpdateTransactionAgainstDisposableDatabase(t *testing.T) {
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
	setup := []string{
		`CREATE SCHEMA IF NOT EXISTS semora_meta`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasources(id text PRIMARY KEY)`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasource_tables(datasource_id text,table_id text,content_hash text)`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasource_ontology_packages(datasource_id text PRIMARY KEY,schema_hash text,status text,object_count int,relation_count int,pending_count int,generator_version text,payload jsonb,error text,generated_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now())`,
		`INSERT INTO semora_meta.datasources VALUES('a'),('b') ON CONFLICT DO NOTHING`,
		`INSERT INTO semora_meta.datasource_tables VALUES('a','customers','h1')`,
	}
	for _, q := range setup {
		if _, err = store.db.ExecContext(ctx, q); err != nil {
			t.Fatal(err)
		}
	}
	value := sourceOntology()
	value.Domain = "reference_atomic_test"
	for _, object := range value.Objects {
		raw, _ := json.Marshal(sourcePackage{Objects: []ObjectClass{object}})
		if _, err = store.db.ExecContext(ctx, `INSERT INTO semora_meta.datasource_ontology_packages(datasource_id,schema_hash,status,payload) VALUES($1,'old','ready',$2)`, object.DataSourceID, string(raw)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = store.createDomain(ctx, value.Domain, "References"); err != nil {
		t.Fatal(err)
	}
	value.SourcePackageRefs = []SourcePackageRef{{DataSourceID: "a"}, {DataSourceID: "b"}}
	for i := range value.Objects {
		value.Objects[i].Metadata = map[string]any{"sourcePackageId": value.Objects[i].DataSourceID}
	}
	if _, err = store.saveEffective(ctx, value); err != nil {
		t.Fatal(err)
	}
	var raw []byte
	if err = store.db.QueryRowContext(ctx, `SELECT payload FROM ontology_core.drafts WHERE domain=$1`, value.Domain).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var stored Ontology
	_ = json.Unmarshal(raw, &stored)
	if len(stored.Objects) != 0 || len(stored.Relations) != 1 {
		t.Fatal("draft stores generated objects")
	}
	updatedObject := value.Objects[0]
	updatedObject.Metadata = map[string]any{"sourcePackageId": "a", "sourceTableId": "customers"}
	updatedPayload, _ := json.Marshal(sourcePackage{Objects: []ObjectClass{updatedObject}})
	if err = store.updateSourcePackage(ctx, sourcePackageUpdate{DataSourceID: "a", SchemaHash: "updated", GeneratorVersion: "current", Payload: updatedPayload, ExpectedTableHashes: map[string]string{"customers": "h1"}}); err != nil {
		t.Fatal(err)
	}
	// A complete scan can change structure without touching the current package or domain.
	if _, err = store.db.ExecContext(ctx, `DELETE FROM semora_meta.datasource_tables WHERE datasource_id='a'`); err != nil {
		t.Fatal(err)
	}
	before, err := store.getDraft(ctx, value.Domain)
	if err != nil || len(before.Objects) != 2 {
		t.Fatal("scan changed relationships before manual update")
	}
	empty, _ := json.Marshal(sourcePackage{Objects: []ObjectClass{}, Relations: []RelationClass{}})
	input := sourcePackageUpdate{DataSourceID: "a", SchemaHash: "new", GeneratorVersion: "current", Payload: empty, ExpectedTableHashes: map[string]string{}}
	if err = store.updateSourcePackage(ctx, input); err != nil {
		t.Fatal(err)
	}
	after, err := store.getDraft(ctx, value.Domain)
	if err != nil || len(after.Objects) != 1 || len(after.Relations) != 0 {
		t.Fatal("removed source table/cross relation retained", err)
	}
	// Bad source structure cannot partially replace the current source package or snapshots.
	invalid, _ := json.Marshal(sourcePackage{Objects: []ObjectClass{{Name: "broken", DataSourceID: "a"}}})
	input.Payload = invalid
	input.SchemaHash = "invalid"
	if err = store.updateSourcePackage(ctx, input); err == nil {
		t.Fatal("invalid source package committed")
	}
	var hash string
	if err = store.db.QueryRowContext(ctx, `SELECT schema_hash FROM semora_meta.datasource_ontology_packages WHERE datasource_id='a'`).Scan(&hash); err != nil || hash != "new" {
		t.Fatal("failed update changed active source package")
	}
	// Inject failure AFTER the package and domain draft writes, at the final execution-context insert.
	if _, err = store.db.ExecContext(ctx, `CREATE FUNCTION public.reject_reference_context() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.domain='reference_atomic_test' THEN RAISE EXCEPTION 'injected publication failure'; END IF; RETURN NEW; END $$`); err != nil {
		t.Fatal(err)
	}
	if _, err = store.db.ExecContext(ctx, `CREATE TRIGGER reject_reference_context BEFORE INSERT ON ontology_core.snapshots FOR EACH ROW EXECUTE FUNCTION public.reject_reference_context()`); err != nil {
		t.Fatal(err)
	}
	input.Payload = empty
	input.SchemaHash = "must_rollback"
	if err = store.updateSourcePackage(ctx, input); err == nil {
		t.Fatal("injected failure did not abort")
	}
	current, err := store.getDraft(ctx, value.Domain)
	if err != nil || current.Version != after.Version {
		t.Fatal("partial domain update committed")
	}
	if err = store.db.QueryRowContext(ctx, `SELECT schema_hash FROM semora_meta.datasource_ontology_packages WHERE datasource_id='a'`).Scan(&hash); err != nil || hash != "new" {
		t.Fatal("partial source update committed")
	}
}
