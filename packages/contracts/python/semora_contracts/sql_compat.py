"""Shared SQL shape lowering; never relax ordering, offsets or tie semantics."""
from sqlglot import exp
from .database import baseline_pagination

def apply_pagination(tree, dialect, strategy="baseline"):
    if baseline_pagination(dialect,strategy)!="rownum":return tree
    if tree.find(exp.Offset):
        raise ValueError("ROWNUM 基线不支持 OFFSET，请使用确定的键范围分页或已验证的窗口查询")
    result=tree.copy()
    # Lower children first so nested Top N queries cannot retain FETCH syntax.
    for query in reversed(list(result.walk())):
        limit=query.args.get("limit")
        if limit is None:continue
        options=limit.args.get("limit_options")
        if options and (options.args.get("with_ties") or options.args.get("percent")):
            raise ValueError("ROWNUM 基线不支持 WITH TIES / PERCENT，不能删除其语义")
        count=(limit.expression or limit.args.get("count") or exp.Literal.number(1)).copy()
        inner=query.copy();inner.set("limit",None)
        lowered=exp.select("*").from_(inner.subquery("semora_page")).where(exp.LTE(this=exp.column("ROWNUM"),expression=count))
        if query is result:result=lowered
        else:query.replace(lowered)
    return result
