package main

import (
	"context"
	"database/sql"
)

// The configuration's relations are authoritative; retire the old duplicate projection.
func syncCrossRelations(ctx context.Context, tx *sql.Tx, value Ontology) error {
	_, err := tx.ExecContext(ctx, `DELETE FROM ontology_core.domain_cross_source_relations WHERE domain=$1`, value.Domain)
	return err
}
