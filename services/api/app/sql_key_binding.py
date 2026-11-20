"""Lower key-set transport placeholders without assuming row-value IN support."""
from sqlglot import exp


def _combine(expressions, operator):
    # Bound collection sizes must not create a deeply recursive AST.
    while len(expressions) > 1:
        expressions = [operator(this=expressions[i], expression=expressions[i + 1])
                       if i + 1 < len(expressions) else expressions[i]
                       for i in range(0, len(expressions), 2)]
    return expressions[0]


def key_set_predicate(fields, keys, marker):
    if not keys:
        return exp.false()
    if len(fields) == 1:
        present = [key[0] for key in keys if key[0] is not None]
        clauses = []
        if present:
            clauses.append(exp.In(this=fields[0].copy(), expressions=[marker(v) for v in present]))
        if any(key[0] is None for key in keys):
            clauses.append(exp.Is(this=fields[0].copy(), expression=exp.Null()))
    else:
        clauses = [_combine([
            exp.Is(this=field.copy(), expression=exp.Null()) if value is None
            else exp.EQ(this=field.copy(), expression=marker(value))
            for field, value in zip(fields, key)], exp.And) for key in keys]
    return exp.Paren(this=_combine(clauses, exp.Or))


def lower_null_comparisons(tree, dialect):
    # These native forms have long-standing support. Other targets receive a
    # total (never UNKNOWN) boolean expression, including under NOT or CASE.
    if dialect in {'mysql', 'postgres', 'duckdb'}:
        return tree

    def lower(node):
        if not isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ)):
            return node
        left, right = node.this, node.expression
        both_null = exp.and_(exp.Is(this=left.copy(), expression=exp.Null()),
                             exp.Is(this=right.copy(), expression=exp.Null()))
        equal_nonnull = exp.and_(
            exp.Not(this=exp.Is(this=left.copy(), expression=exp.Null())),
            exp.Not(this=exp.Is(this=right.copy(), expression=exp.Null())),
            exp.EQ(this=left.copy(), expression=right.copy()))
        equal = exp.Paren(this=exp.or_(equal_nonnull, both_null))
        return exp.Not(this=equal) if isinstance(node, exp.NullSafeNEQ) else equal

    return tree.transform(lower)
