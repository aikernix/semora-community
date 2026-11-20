"""Read-only SQL parsing, dialect conversion and parameter binding."""

import hashlib
import json
import re
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from semora_contracts import SQLArtifact
from semora_contracts.database import SQL_BASELINE_ID, PRODUCTS, baseline_pagination
from semora_contracts.sql_compat import apply_pagination
from .datasources import datasource_service
from .sql_errors import SQLIssue


def dialect_for(source_id):
    source = datasource_service.repository.get(source_id)
    dialect = PRODUCTS.get(source.type,{}).get("dialect") or ("postgres" if source.type=="postgres" else "")
    if source.type in {"vastbase", "oceanbase"}:
        connection = source.scan_result.get("connection", {})
        detected = connection.get("detected_compatibility_mode") or connection.get("compatibility_mode") or source.scan_result.get("capabilities", {}).get("dialect", "")
        if detected in {"postgres", "oracle", "mysql", "tsql"}:
            dialect = detected
    if not dialect:
        capabilities = source.scan_result.get("capabilities", {})
        if capabilities.get("query") == "verified" and capabilities.get("sql_baseline") != SQL_BASELINE_ID:
            raise SQLIssue("SQL能力基线已更新，请重新验证数据源查询能力："+source.name,kind="adapter")
        if capabilities.get("query") == "verified":
            dialect = capabilities.get("dialect")
    if not dialect:
        raise SQLIssue("尚未配置该数据库的方言与校验适配器：" + source.type, kind="adapter")
    return dialect


def parse_statements(sql, dialect):
    # Named bindings are a transport contract, independent of SQL dialect.
    # Token offsets protect strings, quoted identifiers, comments and PG casts.
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    replacements, names = [], {}
    for left, right in zip(tokens, tokens[1:]):
        if left.token_type.name == "COLON" and right.start == left.end + 1 and re.fullmatch(r"[A-Za-z_]\w*", right.text):
            marker = f"semora_named_param_{len(names)}"
            names[marker] = right.text
            replacements.append((left.start, right.end + 1, ":" + marker))
    source = sql
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    trees = sqlglot.parse(source, read=dialect)
    for tree in trees:
        if tree is not None:
            for node in tree.find_all(exp.Placeholder):
                if node.this in names:
                    node.set("this", names[node.this])
    return trees


def parse_query(sql, dialect="postgres"):
    trees = parse_statements(sql, dialect)
    if len(trees) != 1 or trees[0] is None:
        raise SQLIssue("每步必须是一条查询")
    return trees[0]


def readonly_tree(sql, allowed_tables, dialect="postgres"):
    if "__semora_bind_" in sql:
        raise SQLIssue("SQL使用了保留参数标识")
    try:
        statements = parse_statements(sql, dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as exc:
        raise SQLIssue("SQL解析失败：" + str(exc)[:1500]) from exc
    if len(statements) != 1 or not isinstance(
        statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
    ):
        raise SQLIssue("每步必须是一条只读查询")
    tree = statements[0]
    if tree.find(exp.Parameter) or tree.find(exp.PropertyEQ):
        raise SQLIssue(
            "SQL不允许会话变量或赋值；参数使用:name。分组取最后一条请使用分组关联或相关子查询，不依赖变量求值顺序。"
        )
    forbidden = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Merge,
        exp.Command,
        exp.Into,
        exp.Lock,
    )
    if any(tree.find(t) for t in forbidden):
        raise SQLIssue("查询包含非只读操作")
    allowed = {s.casefold() for s in allowed_tables}
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                if not isinstance(source.this, exp.Identifier):
                    raise SQLIssue("FROM只允许已发布物理表或先前结果表")
                name = ".".join(p.name for p in source.parts)
                if name.casefold() not in allowed:
                    raise SQLIssue("引用了当前步骤资料范围外的表：" + name)
    return tree


def dependency_keys(binding, results, template):
    if template:
        return [tuple(None for _ in binding.source_columns)]
    result = results[binding.source_step]
    if result.truncated:
        raise SQLIssue("前置结果不完整，不能作为完整关联键集合继续执行", kind="incomplete")
    try:
        positions = [result.columns.index(c) for c in binding.source_columns]
    except ValueError as exc:
        raise SQLIssue("前置步骤未输出绑定列：" + str(binding.source_columns)) from exc
    keys, seen = [], set()
    for row in result.rows:
        key = tuple(row[i] for i in positions)
        if any(v is None and column not in binding.null_safe_columns
               for column, v in zip(binding.source_columns, key)):
            continue
        signature = json.dumps(key, ensure_ascii=False, default=str)
        if signature not in seen:
            seen.add(signature)
            keys.append(key)
    if len(keys) > 1000:
        raise SQLIssue("跨源关联键超过1000组，需要在源端缩减或聚合", kind="incomplete")
    return keys


def validate_grouping(tree, objects):
    """Catch definite bare-column GROUP BY errors in the PG baseline.

    Deliberately skip complex grouping expressions and unknown base keys;
    EXPLAIN remains authoritative. Derived-table ordering never picks a row.
    """
    keys = {}
    for obj in objects:
        pk = obj.get("primaryKey")
        keys[obj["table"].casefold()] = next(
            (
                p.get("column") or p["name"]
                for p in obj.get("properties", [])
                if p.get("name") == pk
            ),
            pk,
        )
    for scope in traverse_scope(tree):
        select = scope.expression
        group = select.args.get("group") if isinstance(select, exp.Select) else None
        if not group or not all(isinstance(c, exp.Column) for c in group.expressions):
            continue
        aliases = {p.alias: p.this for p in select.expressions if isinstance(p, exp.Alias)}
        if any(not c.table and c.name in aliases for c in group.expressions):
            continue  # Output aliases can overlap input columns; let EXPLAIN resolve them.
        sources = {name.casefold(): value for name, value in scope.selected_sources.items()}

        def ref(column):
            if not isinstance(column, exp.Column):
                return None
            table = column.table or (
                next(iter(scope.selected_sources)) if len(scope.selected_sources) == 1 else ""
            )
            return (table.casefold(), column.name.casefold()) if table else None

        grouped = {ref(c) for c in group.expressions}
        for projection in select.expressions:
            column = projection.this if isinstance(projection, exp.Alias) else projection
            if not isinstance(column, exp.Column):
                continue
            value = ref(column)
            if value is None or value in grouped or value[0] not in sources:
                continue
            source = sources[value[0]][1]
            if isinstance(source, exp.Table):
                name = ".".join(p.name for p in source.parts).casefold()
                pk = keys.get(name)
                if not isinstance(pk, str) or not pk:
                    continue
                if (value[0], pk.casefold()) in grouped:
                    continue
            raise SQLIssue(
                "非聚合输出列未参与分组："
                + column.sql()
                + "。子查询排序后GROUP BY不能确定每组取哪一条；若需求为每组最后一条，按时间与唯一键明确选行，再执行Top N。"
            )


def compile_step(step, ontology, results=None, *, template=False, row_limit=1000, result_keys=None):
    results = results or {}
    if step.engine == "local":
        allowed = list(results)
        dialect = "duckdb"
        source_label = "结果合并"
    else:
        allowed = [
            o["table"]
            for o in ontology.get("objects", [])
            if (o.get("dataSourceId") or ontology.get("dataSourceId")) == step.data_source_id
        ]
        dialect = dialect_for(step.data_source_id)
        source_label = datasource_service.repository.get(step.data_source_id).name
    tree = readonly_tree(step.sql, allowed, step.dialect or "postgres")
    if step.engine == "source":
        from .sql_column_refs import validate_physical_column_refs

        validate_physical_column_refs(tree, [o for o in ontology.get("objects", [])
            if (o.get("dataSourceId") or ontology.get("dataSourceId")) == step.data_source_id])
    validate_grouping(tree, ontology.get("objects", []))
    from .sql_grain import validate_join_grain
    validate_join_grain(tree, [o for o in ontology.get("objects", []) if step.engine == "source" and (o.get("dataSourceId") or ontology.get("dataSourceId")) == step.data_source_id], result_keys)
    if step.engine == "source":
        from .database_profile import source_profile, validate_features

        validate_features(
            tree, source_profile(datasource_service.repository.get(step.data_source_id))
        )
    values, markers = {}, {}
    params = dict(step.parameters)
    dynamic = {b.parameter: b for b in step.inputs}
    if len(dynamic) != len(step.inputs) or set(dynamic) & set(params):
        raise SQLIssue("动态绑定参数重复或与静态参数重名")

    def marker(value):
        name = f"p{len(values)}"
        values[name] = value
        text = f"__semora_bind_{len(values)}__"
        markers[text] = name
        return exp.Var(this=text)

    used_dynamic, used_static = set(), set()

    def transform(node):
        if isinstance(node, exp.Anonymous) and node.name.upper() == 'SEMORA_KEYS_MATCH':
            arguments = node.expressions
            if len(arguments) < 2 or not isinstance(arguments[0], exp.Placeholder):
                raise SQLIssue('键集条件必须包含一个inputs参数及对应目标列')
            name = arguments[0].this
            if name not in dynamic:
                raise SQLIssue('键集条件未声明对应inputs参数')
            binding = dynamic[name]
            fields = arguments[1:]
            if len(fields) != len(binding.source_columns) or not all(isinstance(f, exp.Column) for f in fields):
                raise SQLIssue('键集目标列必须与source_columns按顺序完整对应')
            used_dynamic.add(name)
            from .sql_key_binding import key_set_predicate
            return key_set_predicate(fields, dependency_keys(binding, results, template), marker)
        if (
            isinstance(node, exp.In)
            and len(node.expressions) == 1
            and isinstance(node.expressions[0], exp.Placeholder)
        ):
            name = node.expressions[0].this
            if name in dynamic:
                used_dynamic.add(name)
                binding = dynamic[name]
                keys = dependency_keys(binding, results, template)
                lhs_width = len(node.this.expressions) if isinstance(node.this, exp.Tuple) else 1
                if lhs_width != len(binding.source_columns):
                    raise SQLIssue("复合键参数与IN左侧字段数不一致")
                # Historical IN collection placeholders use the same lowering
                # and null policy as the current transport protocol.
                fields = node.this.expressions if isinstance(node.this, exp.Tuple) else [node.this]
                from .sql_key_binding import key_set_predicate
                return key_set_predicate(fields, keys, marker)
        if isinstance(node, exp.Placeholder):
            name = node.this
            if name in dynamic:
                raise SQLIssue("动态键参数必须用于键集条件绑定占位")
            if name not in params:
                raise SQLIssue("SQL缺少参数：" + str(name))
            if isinstance(params[name], (dict, list)):
                raise SQLIssue("静态参数必须是标量，键集合使用inputs")
            used_static.add(name)
            return marker(params[name])
        return node

    tree = tree.transform(transform)
    from .sql_key_binding import lower_null_comparisons
    tree = lower_null_comparisons(tree, dialect)
    if used_dynamic != set(dynamic) or used_static != set(params):
        raise SQLIssue("存在未在SQL使用的参数或动态绑定")
    # Fetch one overflow row for complete intermediates; the Executor reports
    # truncation rather than allowing false absence/counts downstream.
    if tree.args.get("limit") is None:
        tree = tree.limit(row_limit + 1)
    if dialect == "mysql" and not step.dialect:
        # MySQL forbids aggregate output aliases inside ORDER BY expressions.
        # Its generator emulates PG null ordering with CASE, so resolve local
        # aggregate aliases before generation rather than deleting null order.
        for scope in traverse_scope(tree):
            select = scope.expression
            if not isinstance(select, exp.Select) or not select.args.get("order"):
                continue
            aliases = {
                p.alias.casefold(): p.this
                for p in select.expressions
                if isinstance(p, exp.Alias)
                and p.this.find(exp.AggFunc)
                and not p.this.find(exp.Window)
            }
            for ordered in select.args["order"].expressions:
                ordered.set(
                    "this",
                    ordered.this.transform(
                        lambda n: aliases[n.name.casefold()].copy()
                        if isinstance(n, exp.Column)
                        and not n.table
                        and n.name.casefold() in aliases
                        else n
                    ),
                )
    pagination_strategy="sqlglot"
    if step.engine=="source" and dialect=="oracle":
        pagination_strategy=datasource_service.repository.get(step.data_source_id).scan_result.get("capabilities",{}).get("pagination_strategy",baseline_pagination(dialect))
    try:
        tree=apply_pagination(tree,dialect,pagination_strategy)
    except ValueError as exc:
        raise SQLIssue(str(exc),kind="adapter",stage="dialect_conversion") from exc
    try:
        sql = tree.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
        # Round-trip parser check. Database EXPLAIN remains authoritative.
        sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError) as exc:
        raise SQLIssue("方言转换失败：" + str(exc)[:1500], stage="dialect_conversion") from exc
    if step.engine == "source":
        sql = sql.replace("%", "%%")
        for text, name in markers.items():
            sql = sql.replace(text, "%(" + name + ")s")
    else:
        # DuckDB named parameters do not use DBAPI's percent syntax.
        for text, name in markers.items():
            sql = sql.replace(text, "$" + name)
    digest = hashlib.sha256(
        json.dumps([sql, values, step.data_source_id], sort_keys=True, default=str).encode()
    ).hexdigest()
    local_tables = []
    if step.engine == "local":
        local_tables = list(
            dict.fromkeys(
                source.name
                for scope in traverse_scope(tree)
                for _, source in scope.selected_sources.values()
                if isinstance(source, exp.Table)
            )
        )
    return SQLArtifact(
        semantic_sql=step.sql,
        physical_sql=sql,
        parameters=values,
        dialect=dialect,
        data_source_id=step.data_source_id,
        artifact_hash=digest,
        compilation={
            "engine": "sql-native-v1",
            "sql_baseline": SQL_BASELINE_ID,
            "baseline_hash": hashlib.sha256(step.sql.encode()).hexdigest(),
            "source_dialect": step.dialect or "postgres",
            "target_dialect": dialect,
            "data_source_label": source_label,
            "local_dependencies": local_tables,
            "input_dependencies": [b.model_dump() for b in step.inputs],
            "inputs_bound": not template,
        },
    )


def step_dialect(step):
    return "duckdb" if step.engine == "local" else dialect_for(step.data_source_id)
