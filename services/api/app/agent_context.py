"""Relevant ontology definitions without workbook/import duplication."""

SEMANTIC_KEYS = (
    'name', 'label', 'table', 'column', 'type', 'primaryKey', 'nullable',
    'sensitive', 'sensitivity', 'unit', 'aliases', 'enum', 'enumValues',
    'valueDefinitions', 'defaultFilters', 'filters', 'expression', 'grain',
    'from', 'to', 'condition', 'cardinality', 'joinType', 'optional',
    'description', 'evidenceStatus', 'dataSourceId', 'projection',
)


def compact_definition(value):
    """Keep execution and business meaning, excluding repeated import workbooks."""
    metadata = value.get('metadata') or {}
    result = {key: value[key] if key in value else metadata[key]
              for key in SEMANTIC_KEYS if key in value or key in metadata}
    if 'properties' in value:
        result['properties'] = [compact_definition(prop) for prop in value['properties']]
    return result
