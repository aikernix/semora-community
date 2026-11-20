"""Bounded random index seeks; sampled rows are never advertised as uniform keys."""
import json
import random

from sqlglot import exp


def indexed_sample(table, columns, key, dialect, guards, parameters, budget, run):
    """Use a scanned, single-column integer unique key. Bounds are not samples.

    Random pivots may be biased by key gaps and repeated business keys. Keep
    this explicit in the evidence and never infer exhaustion from this window.
    Filters apply outside each bounded index window, avoiding unbounded seeks
    through sparse/null relation fields or selective guards.
    """
    bounds = exp.select(exp.Min(this=exp.column(key, quoted=True)),
                        exp.Max(this=exp.column(key, quoted=True))).from_(table.copy()).limit(1)
    rows = run(bounds, {}, 'sampling_bounds', 1)['rows']
    if not rows or rows[0][0] is None or rows[0][1] is None:
        return [], False
    lower, upper = map(int, rows[0])
    projection = list(dict.fromkeys([*columns, *(g.field for g in guards)]))
    collected, seen, pivots = [], set(), set()
    for round_index in range(3):
        windows, params = [], dict(parameters)
        for index in range(24):
            pivot = random.SystemRandom().randint(lower, upper)
            if pivot in pivots:
                continue
            pivots.add(pivot)
            name = f'sample_pivot_{round_index}_{index}'
            params[name] = pivot
            window = exp.select(*(exp.column(c, quoted=True) for c in projection)).from_(table.copy())
            window = window.where(exp.GTE(this=exp.column(key, quoted=True), expression=exp.Placeholder(this=name)))
            window = window.order_by(exp.column(key, quoted=True)).limit(32)
            query = exp.select(*(exp.column(c, quoted=True) for c in columns)).from_(window.subquery('sample_window'))
            query = query.where(exp.and_(*(exp.column(c, quoted=True).is_(exp.null()).not_() for c in columns)))
            for index, guard in enumerate(guards):
                query = query.where(exp.EQ(this=exp.column(guard.field, quoted=True),
                    expression=exp.Placeholder(this=f'source_guard_{index}')))
            windows.append(query)
        if not windows:
            break
        union = windows[0]
        for window in windows[1:]:
            union = exp.union(union, window, distinct=False)
        query = exp.select('*').from_(union.subquery('random_windows')).limit(768)
        rows = run(query, params, 'sampling_index_windows', 768)['rows']
        previous = len(seen)
        for row in rows:
            marker = tuple(json.dumps(v, sort_keys=True, default=str) for v in row)
            if all(v is not None for v in row) and marker not in seen:
                seen.add(marker)
                collected.append(row)
        if len(collected) >= budget + 1 or len(seen) == previous:
            break
    random.SystemRandom().shuffle(collected)
    return collected[:budget + 1], False
