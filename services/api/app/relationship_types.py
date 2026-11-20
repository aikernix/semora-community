"""Comparison families do not require a lossy SQL export type conversion."""
import re
from .mdl_export import column_type


def relationship_family(column):
    normalized = column_type(column)
    raw = str(column.get('raw_type') or column.get('type') or '').casefold().strip()
    base = re.split(r'\s*\(', raw)[0].strip()
    if normalized in {'smallint','integer','bigint','real','double'} or str(normalized).startswith('decimal') or base in {'number','numeric','decimal','dec'}:
        return 'number'
    if normalized in {'varchar','char'}:
        return 'text'
    return normalized
