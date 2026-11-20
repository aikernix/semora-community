"""Reject semantic property IDs used as physical columns, without guessing SQL."""
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .sql_errors import SQLIssue


def validate_physical_column_refs(tree, objects):
    tables = {}
    for obj in objects:
        table = obj.get("table")
        if table:
            tables.setdefault(table.casefold(), []).append(obj)
    for scope in traverse_scope(tree):
        for column in scope.columns:
            owner = scope
            if column.table:
                source = None
                while owner is not None:
                    source = next((s for alias, s in owner.sources.items()
                                   if alias.casefold() == column.table.casefold()), None)
                    if source is not None:
                        break
                    owner = owner.parent
            else:
                # An unqualified column may refer to an output alias or a derived
                # relation. Only a single physical source is unambiguous here.
                if len(scope.sources) != 1 or (column.find_ancestor(exp.Order, exp.Group) and any(
                    p.alias and p.alias.casefold() == column.name.casefold()
                    for p in scope.expression.expressions
                )):
                    continue
                source = next(iter(scope.sources.values()))
            if isinstance(source, Scope) or not isinstance(source, exp.Table):
                continue
            name = ".".join(part.name for part in source.parts).casefold()
            props = [p for obj in tables.get(name, []) for p in obj.get("properties", [])]
            physical = {(p.get("column") or p.get("name", "")).casefold() for p in props}
            if column.name.casefold() in physical:
                continue
            mapped = {p["column"] for p in props if p.get("column") and
                      p.get("name", "").casefold() == column.name.casefold()}
            if mapped:
                choices = ", ".join(sorted(mapped))
                raise SQLIssue(
                    f"SQL中的{column.sql()}使用了本体属性name；物理表{source.sql()}的对应column为{choices}。"
                    "source SQL使用物理table和column；逻辑计划才使用本体name。"
                    "修正所有同类物理列引用，并同步调整派生表投影和外层引用；保留输出别名、口径与参数。",
                    stage="column_mapping",
                )
