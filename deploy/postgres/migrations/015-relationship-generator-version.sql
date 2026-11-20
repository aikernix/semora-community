-- Analysis protocol versions are descriptive identifiers, not fixed-width codes.
-- Keep existing versions unchanged and allow future relationship protocol names.
ALTER TABLE semora_meta.datasource_ontology_packages
    ALTER COLUMN generator_version TYPE TEXT;
