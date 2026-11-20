"""Read-only uniqueness evidence for the complete scoped target key."""
from sqlglot import exp


def key_probe_queries(table_id, columns, guards, dialect):
    quoted = [exp.column(c, quoted=True) for c in columns]
    where = exp.and_(*(c.copy().is_(exp.null()).not_() for c in quoted))
    for guard in guards:
        if guard['side'] == 'target':
            where = exp.and_(where, exp.EQ(this=exp.column(guard['field'], quoted=True), expression=exp.convert(guard['value'])))
    table = exp.to_table(table_id, dialect=dialect)
    duplicates = exp.select(*(c.copy() for c in quoted)).from_(table.copy()).where(where.copy())
    duplicates = duplicates.group_by(*(c.copy() for c in quoted)).having(
        exp.GT(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(1))).limit(1)
    return [exp.select(exp.Count(this=exp.Star())).from_(duplicates.subquery('duplicates')).limit(1),
            exp.select(exp.Literal.number(1)).from_(table).where(where).limit(1)]
