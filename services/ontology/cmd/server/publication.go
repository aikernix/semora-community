package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// Publication binds the business definitions themselves, not an engine projection.
func preparePublication(ontology Ontology) (Snapshot, error) {
	if issues := validateOntology(ontology); len(issues) > 0 {
		return Snapshot{}, &validationError{issues: issues}
	}
	return configurationSnapshot(ontology)
}

// Empty configurations are published only by explicit unbinding, invalidating new queries.
func configurationSnapshot(ontology Ontology) (Snapshot, error) {
	canonical := canonicalOntology(ontology)
	payload, err := json.Marshal(canonical)
	if err != nil {
		return Snapshot{}, err
	}
	digest := sha256.Sum256(payload)
	return Snapshot{
		Ontology: canonical, Domain: canonical.Domain, Version: canonical.Version,
		ContentHash: "sha256:" + hex.EncodeToString(digest[:]), ArtifactVersion: ontologyFormatVersion,
	}, nil
}
