from types import SimpleNamespace
import pytest
import sqlglot
from app.database_profile import source_profile,validate_features
from app.sql_errors import SQLIssue
from semora_contracts.sql_compat import apply_pagination
from services.executor.app import jdbc_adapter

@pytest.mark.parametrize("kind,version",[("mysql","8.4"),("postgresql","16.15"),("oracle","23"),("sqlserver","2022")])
def test_version_alone_leaves_optional_sql_unknown_and_allows_preflight(kind,version):
    profile=source_profile(SimpleNamespace(type=kind,scan_result={"connection":{"version":version}}))
    assert not any(profile["features"].values())
    assert all(v is None for v in profile["features"].values())
    validate_features(sqlglot.parse_one("SELECT ROW_NUMBER() OVER()"),profile)

def test_probes_enable_individual_features_and_latest_negative_wins():
    source=SimpleNamespace(type="mysql",scan_result={
        "capabilities":{"cte":{"status":"verified"}},
        "connection":{"probes":{"cte":{"status":"unsupported"},"window_functions":{"status":"verified"}}}})
    profile=source_profile(source)
    assert profile["features"]["window_functions"]
    assert not profile["features"]["common_table_expressions"]
    assert not profile["features"]["recursive_common_table_expressions"]

def test_unknown_recursive_sql_is_allowed_but_confirmed_unsupported_is_repaired():
    profile=source_profile(SimpleNamespace(type="postgresql",scan_result={"capabilities":{"cte":{"status":"verified"}}}))
    tree=sqlglot.parse_one("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n<2) SELECT n FROM t",read="postgres")
    validate_features(tree,profile)
    profile["features"]["recursive_common_table_expressions"] = False
    with pytest.raises(SQLIssue,match="递归WITH"):validate_features(tree,profile)

@pytest.mark.parametrize("ending",["LIMIT 3","FETCH FIRST 3 ROWS ONLY"])
def test_oracle_default_limit_preserves_inner_sort(ending):
    tree=apply_pagination(sqlglot.parse_one("SELECT id FROM events ORDER BY id DESC "+ending,read="postgres"),"oracle")
    sql=tree.sql(dialect="oracle")
    assert "ROWNUM <= 3" in sql
    assert sql.index("ORDER BY")<sql.index("ROWNUM")
    assert "FETCH" not in sql

def test_unsupported_paging_is_never_silently_discarded():
    for sql in ["SELECT id FROM events OFFSET 2 LIMIT 3","SELECT id FROM events ORDER BY id FETCH FIRST 3 ROWS WITH TIES"]:
        with pytest.raises(ValueError):apply_pagination(sqlglot.parse_one(sql,read="postgres"),"oracle")

def test_oracle_nested_top_n_preserves_both_limits_and_rejects_nested_offset():
    source=sqlglot.parse_one("SELECT id FROM (SELECT id FROM events ORDER BY id DESC LIMIT 3) t ORDER BY id LIMIT 1")
    sql=apply_pagination(source,"oracle").sql(dialect="oracle")
    assert "FETCH" not in sql and "ROWNUM <= 3" in sql and "ROWNUM <= 1" in sql
    assert "ORDER BY id DESC" in sql and "ORDER BY id" in sql
    assert source.sql().count("LIMIT")==2
    with pytest.raises(ValueError):
        apply_pagination(sqlglot.parse_one("SELECT * FROM (SELECT id FROM events OFFSET 2 LIMIT 3) t"),"oracle")

def test_sqlserver_offset_is_distinct_from_basic_top():
    source=SimpleNamespace(type="sqlserver",scan_result={"capabilities":{"dialect":"tsql"}})
    profile=source_profile(source)
    tree=sqlglot.parse_one("SELECT id FROM events ORDER BY id LIMIT 2 OFFSET 1",read="postgres")
    validate_features(tree,profile)
    source.scan_result["capabilities"]["offset_pagination"]={"status":"unsupported"}
    with pytest.raises(SQLIssue,match="OFFSET/FETCH"):validate_features(tree,source_profile(source))
    source.scan_result["capabilities"]["offset_pagination"]={"status":"verified"}
    validate_features(tree,source_profile(source))

def test_oracle_builtin_and_oceanbase_oracle_use_same_low_sql_shape_but_distinct_drivers(monkeypatch):
    monkeypatch.setattr(jdbc_adapter.drivers,"resolve_driver",lambda key:{"id":key,"builtin":True})
    for cfg in [{"type":"oracle"},{"type":"oceanbase","compatibility_mode":"oracle"}]:
        driver,dialect,strategy=jdbc_adapter.configuration(cfg)
        assert dialect==strategy=="oracle"
        assert driver["effective_pagination"]=="rownum"
        assert driver["id"]==cfg["type"]

def test_server_metadata_change_removes_old_probe_evidence():
    from app.datasources import DataSourceRepository,DataSourceRecord
    repository=DataSourceRepository(dsn="")
    item=DataSourceRecord("x","x","mysql","DATASOURCE_X_URL",scan_result={
        "connection":{"version":"8.0","probes":{"cte":{"status":"verified"}}},
        "capabilities":{"query":"verified","cte":{"status":"verified"}}})
    repository.save(item)
    repository.record_test("x",{"version":"5.7","read_only":True})
    assert not source_profile(item)["features"]["common_table_expressions"]
    assert item.scan_result["scan_stale"]
