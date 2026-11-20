package main

import (
	"context"
	"encoding/json"
	"os"
	"testing"
)

func acceptedAppendRelation(name string) RelationClass {
	return RelationClass{Name: name, From: "left", To: "right", Condition: "left.key = right.key", Cardinality: "N:1",
		Metadata: map[string]any{"relationshipRecord": map[string]any{"id": name, "status": "accepted"}}}
}

func TestAppendRelationsTransactionAgainstDisposableDatabase(t *testing.T) {
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
	for _, q := range []string{
		`CREATE SCHEMA IF NOT EXISTS semora_meta`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasources(id text PRIMARY KEY)`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasource_tables(datasource_id text,table_id text,content_hash text)`,
		`CREATE TABLE IF NOT EXISTS semora_meta.datasource_ontology_packages(datasource_id text PRIMARY KEY,schema_hash text,status text,object_count int,relation_count int,pending_count int,generator_version text,payload jsonb,error text,generated_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now())`,
		`INSERT INTO semora_meta.datasources VALUES('repair_a'),('repair_b')`,
		`INSERT INTO semora_meta.datasource_tables VALUES('repair_a','roots','h1'),('repair_a','details','h2'),('repair_b','remote','h3')`,
	} {
		if _, err = store.db.ExecContext(ctx, q); err != nil {
			t.Fatal(err)
		}
	}
	objects := []ObjectClass{}
	for _, name := range []string{"roots", "details", "remote"} {
		sid := "repair_a"
		if name == "remote" {
			sid = "repair_b"
		}
		objects = append(objects, ObjectClass{Name: name, Label: name, Table: name, DataSourceID: sid, PrimaryKey: "key",
			Metadata: map[string]any{"sourceTableId": name, "sourcePackageId": sid}, Properties: []Property{{Name: "key", Label: "Key", Column: "key", Type: "integer"}}})
	}
	for _, sid := range []string{"repair_a", "repair_b"} {
		selected := []ObjectClass{}
		for _, obj := range objects {
			if obj.DataSourceID == sid {
				selected = append(selected, obj)
			}
		}
		raw, _ := json.Marshal(sourcePackage{Objects: selected, Relations: []RelationClass{}})
		if _, err = store.db.ExecContext(ctx, `INSERT INTO semora_meta.datasource_ontology_packages(datasource_id,schema_hash,status,payload) VALUES($1,'schema','ready',$2)`, sid, string(raw)); err != nil {
			t.Fatal(err)
		}
	}
	domain := "repair_atomic_test"
	value, err := store.createDomain(ctx, domain, "Repair transaction")
	if err != nil {
		t.Fatal(err)
	}
	value.Objects = objects
	value.SourcePackageRefs = []SourcePackageRef{{DataSourceID: "repair_a", SchemaHash: "schema"}, {DataSourceID: "repair_b", SchemaHash: "schema"}}
	if _, err = store.saveEffective(ctx, value); err != nil {
		t.Fatal(err)
	}
	local := acceptedAppendRelation("local")
	local.From = "details"
	local.To = "roots"
	local.Condition = "details.key = roots.key"
	local.JoinType = "many_to_one"
	delta, _ := json.Marshal(sourcePackage{Relations: []RelationClass{local}})
	input := sourcePackageUpdate{DataSourceID: "repair_a", SchemaHash: "schema", GeneratorVersion: "test", AppendRelations: true, Payload: delta, ExpectedTableHashes: map[string]string{"roots": "h1", "details": "h2"}}
	if err = store.updateSourcePackage(ctx, input); err != nil {
		t.Fatal(err)
	}
	if err = store.updateSourcePackage(ctx, input); err != nil {
		t.Fatal(err)
	}
	actual, err := store.getDraft(ctx, domain)
	if err != nil || len(actual.Relations) != 1 {
		t.Fatal("source relation was not composed", err)
	}
	cross := acceptedAppendRelation("cross")
	cross.From = "roots"
	cross.To = "remote"
	cross.Condition = "roots.key = remote.key"
	cross.JoinType = "many_to_one"
	addition := crossRelationAppend{Domain: domain, Relations: []RelationClass{cross}, TableHashes: map[string]map[string]string{"repair_a": {"roots": "h1"}, "repair_b": {"remote": "h3"}}}
	if err = store.appendCrossRelations(ctx, addition); err != nil {
		t.Fatal(err)
	}
	actual, err = store.getDraft(ctx, domain)
	if err != nil || len(actual.Relations) != 2 {
		t.Fatal("cross relation lost source edges", err)
	}
	previousVersion := actual.Version
	addition.TableHashes["repair_b"]["remote"] = "stale"
	if err = store.appendCrossRelations(ctx, addition); err == nil {
		t.Fatal("stale evidence accepted")
	}
	actual, err = store.getDraft(ctx, domain)
	if err != nil || actual.Version != previousVersion || len(actual.Relations) != 2 {
		t.Fatal("failed append changed effective configuration", err)
	}
	var raw []byte
	if err = store.db.QueryRowContext(ctx, `SELECT payload FROM ontology_core.snapshots WHERE domain=$1 ORDER BY published_at DESC LIMIT 1`, domain).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var snapshot Ontology
	if err = json.Unmarshal(raw, &snapshot); err != nil || len(snapshot.Relations) != 2 {
		t.Fatal("new snapshot missing appended relations", err)
	}
}

func TestRelationAppendRetainsCurrentChangesAndIsIdempotent(t *testing.T) {
	old, added := acceptedAppendRelation("existing"), acceptedAppendRelation("new")
	relations, err := appendRelations([]RelationClass{old}, []RelationClass{added})
	if err != nil || len(relations) != 2 {
		t.Fatal(relations, err)
	}
	relations, err = appendRelations(relations, []RelationClass{added})
	if err != nil || len(relations) != 2 {
		t.Fatal("duplicate publication", err)
	}
	added.Condition = "left.other = right.key"
	if _, err = appendRelations(relations, []RelationClass{added}); err == nil {
		t.Fatal("conflicting update replaced an existing edge")
	}
}

func TestUnverifiedRelationsCannotBeAppended(t *testing.T) {
	for _, status := range []string{"unresolved", "rejected", "disabled"} {
		relation := acceptedAppendRelation("r")
		relation.Metadata["relationshipRecord"].(map[string]any)["status"] = status
		if _, err := appendRelations(nil, []RelationClass{relation}); err == nil {
			t.Fatal("unverified publication", status)
		}
	}
	relation := acceptedAppendRelation("r")
	relation.Cardinality = "unknown"
	if _, err := appendRelations(nil, []RelationClass{relation}); err == nil {
		t.Fatal("unknown cardinality published")
	}
}

func TestPackageAppendPreservesObjectsAndCustomMetadata(t *testing.T) {
	current := json.RawMessage(`{"objects":[{"name":"kept"}],"relations":[],"relationshipRecords":[{"id":"old","status":"disabled"}],"custom":{"preserve":true}}`)
	addition, _ := json.Marshal(sourcePackage{Relations: []RelationClass{acceptedAppendRelation("new")}})
	updated, err := appendPackageRelations(current, addition)
	if err != nil {
		t.Fatal(err)
	}
	updated, err = appendPackageRelations(updated, addition)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err = json.Unmarshal(updated, &value); err != nil {
		t.Fatal(err)
	}
	if len(value["objects"].([]any)) != 1 || value["custom"] == nil || len(value["relationshipRecords"].([]any)) != 2 {
		t.Fatal(string(updated))
	}
}
