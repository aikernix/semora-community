package main

func objectSource(object ObjectClass, ontology Ontology) string {
	if object.DataSourceID != "" {
		return object.DataSourceID
	}
	return ontology.DataSourceID
}
