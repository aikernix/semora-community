"""Execution capabilities derived from scanned server metadata, not business rules."""

def source_profile(source):
    connection = source.scan_result.get("connection") or {}
    version = str(connection.get("version") or connection.get("server_version") or "")
    vendor = source.type.lower()
    from semora_contracts.database import SQL_BASELINE_ID, SQL_OPTIONAL_FEATURES, normalize_sql_probe
    features = {feature: None for feature in SQL_OPTIONAL_FEATURES.values()}
    feature_status = {feature: "unverified" for feature in features}
    capabilities = dict(source.scan_result.get("capabilities", {}))
    probe_dialect = capabilities.get("dialect") or connection.get("dialect") or {"postgresql": "postgres", "sqlserver": "tsql"}.get(source.type, source.type)
    probes = connection.get("probes", {})
    from .capability_cache import inherited_probes, effective_probes, shared_probes
    inherited = inherited_probes(source.scan_result)
    for key, feature in SQL_OPTIONAL_FEATURES.items():
        probe = probes.get(key) or capabilities.get(key)
        if isinstance(probe, dict) and probe.get("expires_at"):
            from datetime import datetime, timezone
            if datetime.fromisoformat(probe["expires_at"]) <= datetime.now(timezone.utc):
                probe = None
        if not isinstance(probe, dict) or normalize_sql_probe(probe, probe_dialect)["status"] == "unverified":
            probe = inherited.get(key) or probe
        if isinstance(probe, dict):
            normalized = normalize_sql_probe(probe, probe_dialect)
            capabilities[key] = normalized
            state = normalized["status"]
            feature_status[feature] = state
            features[feature] = {"verified": True, "unsupported": False}.get(state)
        else:
            capabilities[key] = {"status": "unverified"}
    # Version metadata is descriptive. It cannot turn an untested feature on.
    if "mariadb" in version.lower(): vendor = "mariadb"
    function_probes = effective_probes(source.scan_result)
    functions = {k: p for k, p in function_probes.items() if k.startswith("fn.")}
    for key, probe in shared_probes(source.scan_result).items():
        if key.startswith("fn.") and key not in functions and probe.get("status") in {"unsupported", "mixed"}:
            functions[key] = {"status": probe["status"], "scope": "baseline_hint", "method": "shared_hint"}
    observed = sorted(((k, p) for k, p in function_probes.items() if k.startswith("observed:")), key=lambda pair: pair[1].get("observed_at", ""))[-16:]
    functions.update(observed)
    capabilities = {k: v for k, v in capabilities.items() if not k.startswith(("fn.", "observed:"))}
    return {"type": source.type, "vendor": connection.get("product") or vendor,
            "version": version or None, "features": features, "feature_status": feature_status,
            "sql_baseline": {"id": SQL_BASELINE_ID,
                "preferred_syntax": "按实际方言、版本和兼容模式编写；优先features为true的已验证能力，false采用等价写法，null表示未验证、允许数据库预检",
                "pagination": capabilities.get("pagination_strategy") or ("rownum" if source.type=="oracle" else "sqlglot")},
            "dialect": capabilities.get("dialect"),"compatibility_mode": connection.get("detected_compatibility_mode") or connection.get("compatibility_mode"),
            "driver_version": connection.get("driver_version"), "capabilities": capabilities,
            "function_capabilities": {k: {key: p[key] for key in ("status", "scope", "method", "function", "argument_kinds", "context") if key in p}
                for k, p in functions.items()},
            "sql_feedback": (source.scan_result.get("sql_feedback") or [])[-4:]}


def validate_features(tree, profile):
    from sqlglot import exp
    from .sql_errors import SQLIssue

    features = profile.get("features", {})
    missing = []
    if features.get("window_functions") is False and tree.find(exp.Window):
        missing.append("窗口函数（含 ROW_NUMBER / OVER）")
    if features.get("common_table_expressions") is False and tree.find(exp.With):
        missing.append("WITH 公共表表达式")
    if features.get("recursive_common_table_expressions") is False and any(w.args.get("recursive") for w in tree.find_all(exp.With)):
        missing.append("递归WITH")
    if profile.get("dialect")=="tsql" and features.get("offset_pagination") is False and tree.find(exp.Offset):
        missing.append("OFFSET/FETCH 分页（普通取数优先 TOP）")
    if missing:
        raise SQLIssue(
            f"目标 {profile['vendor']} {profile['version']} 已确认不支持"
            + "、".join(missing)
            + "。请使用普通分组、派生表或相关子查询实现相同含义，保留原筛选、粒度与Top N先后顺序；不能等价改写时明确报告。"
        )
