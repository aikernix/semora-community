"""Conservative, scope-aware detection of aggregate multiplication by joins."""
from __future__ import annotations

from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope, Scope
from .sql_errors import SQLIssue


def validate_join_grain(tree, objects, result_keys=None):
    known = {}
    for obj in objects:
        props = {p['name']: p.get('column') or p['name'] for p in obj.get('properties', [])}
        key = obj.get('primaryKey')
        keys = []
        if isinstance(key, str) and key:
            keys.append({props.get(key, key).casefold()})
        elif isinstance(key, list) and key:
            keys.append({props.get(k, k).casefold() for k in key})
        for constraint in obj.get('unique_constraints', []):
            if constraint.get('columns'):
                keys.append({c.casefold() for c in constraint['columns']})
        known[obj['table'].casefold()] = keys
    known.update({name.casefold(): [set(k) for k in keys] for name, keys in (result_keys or {}).items()})
    output_keys = {}
    for scope in traverse_scope(tree):
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        sources = {alias.casefold(): source for alias, (_, source) in scope.selected_sources.items()}
        keys = {}
        for alias, source in sources.items():
            keys[alias] = output_keys.get(id(source), []) if isinstance(source, Scope) else known.get('.'.join(p.name for p in source.parts).casefold(), []) if isinstance(source, exp.Table) else []
        edges = {alias: {} for alias in sources}
        prior = []
        root = select.args.get('from')
        if root and root.this:
            prior.append(root.this.alias_or_name.casefold())
        for join in select.args.get('joins', []):
            alias = join.this.alias_or_name.casefold()
            on = join.args.get('on')
            pairs = []
            # OR predicates do not establish an equality key contract.
            if on is not None and not on.find(exp.Or):
                for eq in on.find_all(exp.EQ):
                    a, b = eq.this, eq.expression
                    if isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table and b.table:
                        pairs.append((a.table.casefold(), a.name.casefold(), b.table.casefold(), b.name.casefold()))
            linked = set()
            for left in prior:
                left_fields, right_fields = set(), set()
                for a, ac, b, bc in pairs:
                    if a == left and b == alias:
                        left_fields.add(ac); right_fields.add(bc)
                    elif b == left and a == alias:
                        left_fields.add(bc); right_fields.add(ac)
                if left_fields:
                    edges.setdefault(left, {})[alias] = any(k <= right_fields for k in keys.get(alias, []))
                    edges.setdefault(alias, {})[left] = any(k <= left_fields for k in keys.get(left, []))
                    linked.add(left)
            if not linked:
                for left in prior:
                    edges.setdefault(left, {})[alias] = False
                    edges.setdefault(alias, {})[left] = False
            prior.append(alias)
        def multiplied(origin):
            seen, todo = set(), [origin]
            while todo:
                current = todo.pop()
                seen.add(current)
                for neighbor, unique in edges.get(current, {}).items():
                    if neighbor in seen:
                        continue
                    if not unique:
                        return True
                    todo.append(neighbor)
            return False
        for aggregate in select.find_all(exp.AggFunc):
            if aggregate.find_ancestor(exp.Select) is not select or isinstance(aggregate, (exp.Min, exp.Max)):
                continue
            if isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
                continue
            # Window aggregates over a grouped scope require separate grain reasoning.
            columns = list(aggregate.find_all(exp.Column))
            origins = {c.table.casefold() for c in columns if c.table}
            if not origins:
                origins = set(sources)
            if any(multiplied(alias) for alias in origins):
                raise SQLIssue('聚合可能因连接放大而重复计算：' + aggregate.sql() +
                    '。按指标来源和目标粒度先分别聚合，再连接；仅作存在性筛选时使用EXISTS。不能用统一DISTINCT替代粒度修复。',
                    kind='sql', stage='join_grain')
        group = select.args.get('group')
        projections = {}
        for expression in select.expressions:
            value = expression.this if isinstance(expression, exp.Alias) else expression
            projections[value.sql().casefold()] = expression.alias_or_name.casefold()
        if group and all(e.sql().casefold() in projections for e in group.expressions):
            output_keys[id(scope)] = [{projections[e.sql().casefold()] for e in group.expressions}]
        elif not group and any(isinstance(e, exp.AggFunc) or isinstance(e, exp.Alias) and isinstance(e.this, exp.AggFunc) for e in select.expressions):
            output_keys[id(scope)] = [set()]
        elif len(sources) == 1 and not select.args.get('joins'):
            alias = next(iter(sources))
            renamed = {}
            for expression in select.expressions:
                value = expression.this if isinstance(expression, exp.Alias) else expression
                if isinstance(value, exp.Column):
                    renamed[value.name.casefold()] = expression.alias_or_name.casefold()
            output_keys[id(scope)] = [{renamed[c] for c in key} for key in keys[alias] if key <= renamed.keys()]
        else:
            output_keys[id(scope)] = []

    return output_keys.get(id(scope), []) if "scope" in locals() else []
