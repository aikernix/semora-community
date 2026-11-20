"""Optional context is limited to small numeric enumerations declared in metadata."""
import re


def declared_enums(table):
    fields = {}
    for c in table.get('columns', []):
        text = str(c.get('description') or c.get('comment') or '')
        values = sorted({int(v) for v in re.findall(r'(?<![\w.])(-?\d+)\s*[:：-]\s*[^\s,，;；]+', text)})
        if 2 <= len(values) <= 16:
            fields[c['name']] = values
    # Auxiliary statistics have a separate resource limit, never a relation limit.
    return dict(list(fields.items())[:8])
