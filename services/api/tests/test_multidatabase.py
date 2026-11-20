import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi import HTTPException
from semora_contracts.database import DatabaseConnection, PRODUCTS, native_protocol
from services.executor.app import adapter_registry, jdbc_adapter, drivers
from services.executor.app.connection_settings import ConnectionStore
from app.mdl_export import build_mdl, merge_snapshot, relationship_id

def connection(kind="oracle",**extra):
    return DatabaseConnection(type=kind,host="db",port=1521,database="FREEPDB1",username="reader",password="secret",**extra)

@pytest.mark.parametrize("kind",PRODUCTS)
def test_every_product_is_explicit_and_version_independent(kind):
    cfg=connection(kind,**({"compatibility_mode":"oracle"} if kind=="oceanbase" else {}))
    assert adapter_registry.product(cfg.type)["transport"] in {"native","jdbc"}
    assert "version" not in DatabaseConnection.model_fields
    with pytest.raises(HTTPException):adapter_registry.product("unknown")


@pytest.mark.parametrize("kind,protocol,dialect",[
    ("goldendb","mysql","mysql"),
])
def test_builtin_protocol_products_need_no_driver_selection(kind,protocol,dialect):
    cfg=connection(kind)
    assert native_protocol(kind)==protocol
    assert adapter_registry.protocol(kind)==protocol
    assert adapter_registry.dialect(cfg)==dialect
    dsn=ConnectionStore.dsn(cfg.model_dump(mode="json"))
    assert dsn.startswith("mysql://") if protocol=="mysql" else "host=db" in dsn
    assert cfg.driver_id==""
    with pytest.raises(ValueError,match="JDBC 连接串"):
        connection(kind,jdbc_url="jdbc:vendor://db/test")


def test_vastbase_automatically_uses_vendor_jdbc_without_driver_selection(monkeypatch):
    cfg=connection("vastbase")
    assert PRODUCTS["vastbase"]["transport"]=="jdbc"
    assert PRODUCTS["vastbase"]["driver_id"]=="vastbase"
    assert drivers.BUILTINS["vastbase"]==("cn.com.vastbase.Driver","vastbase-jdbc-2.13.2v.jar")
    assert jdbc_adapter.jdbc_url(cfg.model_dump(mode="json"))=="jdbc:vastbase://db:1521/FREEPDB1"
    monkeypatch.setattr(jdbc_adapter.drivers,"resolve_driver",lambda key:{"id":key,"builtin":True})
    value=cfg.model_dump(mode="json")
    _,dialect,strategy=jdbc_adapter.configuration(value)
    assert dialect==strategy=="postgres"


def test_vastbase_detected_mode_drives_dialect_properties_and_invalidates_validation(monkeypatch):
    monkeypatch.setattr(jdbc_adapter.drivers,"resolve_driver",lambda key:{"id":key,"builtin":True})
    saved={}
    monkeypatch.setattr(jdbc_adapter.connection_store,"save",lambda key,value:saved.update(key=key,value=deepcopy(value)))
    cfg=connection("vastbase").model_dump(mode="json")
    cfg["_validation"]="previous"
    changed=jdbc_adapter.record_detected_compatibility(cfg,{"detected_compatibility_mode":"oracle"},"source-1")
    assert changed and saved["key"]=="source-1"
    assert cfg["compatibility_mode"]=="oracle"
    assert cfg["properties"]["db_compatibility"]=="oracle"
    assert cfg["_validation"]==""
    driver,dialect,strategy=jdbc_adapter.configuration(cfg)
    assert dialect=="oracle" and strategy=="postgres"
    assert driver["effective_pagination"]=="rownum"


def test_vastbase_api_dialect_uses_detected_server_mode(monkeypatch):
    from types import SimpleNamespace
    from app import sql_dialect
    source=SimpleNamespace(type="vastbase",scan_result={"connection":{"detected_compatibility_mode":"oracle"}})
    monkeypatch.setattr(sql_dialect.datasource_service.repository,"get",lambda key:source)
    assert sql_dialect.dialect_for("source-1")=="oracle"


@pytest.mark.parametrize("kind,expected",[
    ("goldendb","MYSQL"),("vastbase","POSTGRES"),
])
def test_protocol_products_keep_native_mdl_target(kind,expected):
    mdl,report=build_mdl(fixture_snapshot(),kind)
    assert mdl is not None,report
    assert mdl["dataSource"]==expected


@pytest.mark.parametrize("kind,table_id,sql_part",[
    ("goldendb","db.report","information_schema.VIEWS"),
])
def test_protocol_products_use_native_relation_definition_reader(kind,table_id,sql_part,monkeypatch):
    from services.executor.app import main
    captured={}
    def query(body,token):
        captured["sql"]=body.sql
        return {"rows":[["SELECT 1"]]}
    monkeypatch.setattr(main,"execute_query",query)
    body=main.RelationDefinitionInput(datasource_id="x",type=kind,secret_ref="DATASOURCE_X_URL",table_id=table_id)
    assert main.relation_definitions(body)["status"]=="available"
    assert sql_part in captured["sql"]

def test_credentials_only_in_secret_fields_and_encrypted_store(tmp_path):
    store=ConnectionStore(str(tmp_path))
    value=store.candidate("a","DATASOURCE_A_URL",connection())
    store.save("a",value)
    assert "secret" not in (tmp_path/"connections.enc").read_text()
    assert "password" not in store.public(value)
    assert store.get("a","DATASOURCE_A_URL","oracle")["password"]=="secret"
    with pytest.raises(ValueError):connection(jdbc_url="jdbc:oracle:thin:reader/password@db:1521:ORCL")
    with pytest.raises(ValueError):connection(properties={"password":"secret"})

def test_oracle_and_oceanbase_use_distinct_protocols():
    assert jdbc_adapter.jdbc_url(connection().model_dump()).startswith("jdbc:oracle:thin:@//")
    ob=connection("oceanbase",compatibility_mode="oracle").model_dump()
    assert jdbc_adapter.jdbc_url(ob).startswith("jdbc:oceanbase://")
    assert ":FREEPDB1" in jdbc_adapter.jdbc_url(connection(service_kind="sid").model_dump())

def test_jdbc_binding_ignores_strings_comments_and_preserves_values():
    sql,values=jdbc_adapter.bind_parameters("SELECT '%%(x)s', %(x)s, %(x)s -- %(y)s\n FROM t WHERE p LIKE '%%ok%%' LIMIT 1",{"x":"a'b"})
    assert sql=="SELECT '%(x)s', ?, ? -- %(y)s\n FROM t WHERE p LIKE '%ok%' LIMIT 1"
    assert values==["a'b","a'b"]
    with pytest.raises(HTTPException):jdbc_adapter.bind_parameters("SELECT %(missing)s LIMIT 1",{})

def test_driver_bundle_hashes_dependencies_enablement_and_restart(tmp_path,monkeypatch):
    monkeypatch.setattr(drivers,"connection_store",ConnectionStore(str(tmp_path)))
    def jar(name):
        out=io.BytesIO()
        with zipfile.ZipFile(out,"w") as z:z.writestr(name,b"class-bytes")
        return out.getvalue()
    cfg=drivers.DriverConfig(name="vendor",driver_class="vendor.Driver",enabled=False)
    saved=drivers.install(cfg,[("driver.jar",jar("vendor/Driver.class")),("dependency.jar",jar("vendor/Dep.class"))])
    assert len(saved["files"])==2
    with pytest.raises(HTTPException):drivers.resolve_driver(saved["id"])
    drivers.update(saved["id"],cfg.model_copy(update={"enabled":True}))
    assert drivers.resolve_driver(saved["id"])["enabled"]
    (tmp_path/"drivers"/saved["id"]/"01.jar").write_bytes(b"tampered")
    with pytest.raises(HTTPException,match="哈希"):drivers.resolve_driver(saved["id"])

def test_generic_jdbc_cannot_execute_without_validation(monkeypatch):
    cfg=connection("jdbc",jdbc_url="jdbc:vendor://db/test").model_dump(mode="json")
    monkeypatch.setattr(jdbc_adapter,"configuration",lambda cfg:({"builtin":False,"classpath":"x"},"",""))
    with pytest.raises(HTTPException,match="仅开放"):jdbc_adapter.run(cfg,"query",sql="SELECT 1 LIMIT 1")
    monkeypatch.setattr(jdbc_adapter,"configuration",lambda cfg:({"builtin":False,"classpath":"x"},"postgres","postgres"))
    with pytest.raises(HTTPException,match="查询验证"):jdbc_adapter.run(cfg,"query",sql="SELECT 1 LIMIT 1")

def fixture_snapshot():
    customer={"id":"a.customer","schema":"a","name":"customer","description":"客户","columns":[{"name":"tenant_id","type":"integer","nullable":False},{"name":"customer_id","type":"bigint","nullable":False}],"primary_keys":[{"name":"pk","columns":["tenant_id","customer_id"]}],"unique_constraints":[]}
    loan={"id":"b.loan","schema":"b","name":"loan","columns":[{"name":"id","type":"bigint"},{"name":"tenant_id","type":"integer"},{"name":"customer_id","type":"bigint"},{"name":"balance","type":"numeric(30,8)"}],"primary_keys":[{"name":"pk","columns":["id"]}],"unique_constraints":[]}
    fk={"name":"fk","source_table":"b.loan","columns":["tenant_id","customer_id"],"target_table":"a.customer","target_columns":["tenant_id","customer_id"]}
    return {"tables":[customer,loan],"foreign_keys":[fk],"permission_gaps":[]}

def test_mdl_deterministic_composite_join_precision_and_real_wren_build():
    source=fixture_snapshot()
    mdl,report=build_mdl(source,"postgresql")
    assert mdl is not None,report
    assert report["schema_validation"]=="passed"
    # Runs real pinned Wren when installed, no mock success.
    import importlib.util
    if importlib.util.find_spec("wren_core"): assert report["wren_build"]=="passed",report
    else: assert report["wren_build"]=="unavailable"
    assert mdl==build_mdl(source,"postgresql")[0]
    assert mdl["models"][0]["primaryKey"]==["tenant_id","customer_id"]
    assert " AND " in mdl["relationships"][0]["condition"]
    assert "decimal(30,8)" in json.dumps(mdl)
    assert report["wren_target_execution"]=="unverified"
    assert "password" not in json.dumps(mdl)

def test_unknown_type_permission_gap_or_bad_join_cannot_export_silently():
    source=fixture_snapshot();source["tables"][0]["columns"][0]["type"]="vendor_geometry"
    assert build_mdl(source,"jdbc")[0] is None
    source=fixture_snapshot();source["permission_gaps"]=["constraints:denied"]
    assert build_mdl(source,"postgresql")[0] is None
    source=fixture_snapshot();source["foreign_keys"][0]["target_columns"]=["missing"]
    assert build_mdl(source,"postgresql")[0] is None

def test_rescan_keeps_human_descriptions_and_rejections():
    source=fixture_snapshot();source["foreign_keys"]=[]
    merged=merge_snapshot({},source);r=merged["relationship_candidates"][0]
    merged["relationship_decisions"]={r["id"]:{"status":"rejected","note":"不是业务关系"}}
    merged["tables"][0]["user_description"]="人工说明"
    again=merge_snapshot(merged,source)
    assert again["tables"][0]["user_description"]=="人工说明"
    assert again["relationship_candidates"][0]["status"]=="rejected"

def test_driver_changes_invalidate_validation_fingerprint():
    cfg={"host":"a","password":"secret"};driver={"version":"v1","files":[{"sha256":"a"}]}
    signature=jdbc_adapter.validation_fingerprint(cfg,driver)
    assert jdbc_adapter.validation_fingerprint({**cfg,"_validation":signature},driver)==signature
    assert jdbc_adapter.validation_fingerprint(cfg,{**driver,"dialect":"oracle"})!=signature

def test_worker_secrets_stay_off_command_line_and_timeout_kills_process(monkeypatch):
    import subprocess
    cfg={"type":"jdbc","jdbc_url":"jdbc:vendor://db/test","username":"reader","password":"very-private"}
    monkeypatch.setattr(jdbc_adapter,"configuration",lambda _:({"builtin":False,"classpath":"lib","driver_class":"vendor.Driver"},"postgres","postgres"))
    captured={}
    class Process:
        returncode=None
        def __init__(self,args,**kwargs):captured["args"]=args
        def communicate(self,data=None,**kwargs):
            if data:
                assert json.loads(data)["connection"]["password"]=="very-private"
                raise subprocess.TimeoutExpired("java",1)
            return None,None
        def kill(self):self.returncode=-9;captured["killed"]=True
        def poll(self):return self.returncode
        def wait(self):return self.returncode
    monkeypatch.setattr(jdbc_adapter.subprocess,"Popen",Process)
    with pytest.raises(HTTPException) as error:jdbc_adapter.run(cfg,"test",timeout_ms=100)
    assert error.value.status_code==504
    assert captured["killed"]
    assert "very-private" not in str(captured["args"])

def test_validation_errors_never_echo_connection_secrets():
    from fastapi.testclient import TestClient
    from services.executor.app.main import app
    response=TestClient(app).post("/internal/v1/datasources/test-configuration",json={
        "datasource_id":"x","type":"oracle","secret_ref":"DATASOURCE_X_URL",
        "connection":{"type":"oracle","host":"","port":1521,"database":"","username":"reader","password":"very-private"}})
    assert response.status_code==422
    assert "very-private" not in response.text
    assert connection(jdbc_url="jdbc:oracle:thin:@//db:1521/ORCL").jdbc_url

def test_cancelled_scan_and_connection_edit_cannot_publish_stale_results(monkeypatch):
    import asyncio
    from app import database_routes as routes
    from app.datasources import DataSourceService,DataSourceRepository
    service=DataSourceService(repository=DataSourceRepository(dsn=""))
    monkeypatch.setattr(routes,"datasource_service",service)
    async def scenario():
        gate=asyncio.Event()
        async def fake(method,path,**kwargs):
            if path=="datasources/scan":await gate.wait();return fixture_snapshot()
            return {}
        monkeypatch.setattr(routes,"executor_request",fake)
        identifier="retail-credit-pg"
        job=await routes.start_scan(identifier,routes.ScanRequest())
        await asyncio.sleep(0)
        service.repository.get(identifier).scan_result={"connection_changed":True}
        gate.set();await routes.TASKS[job["id"]]
        assert service.repository.get(identifier).scan_result=={"connection_changed":True}
        gate.clear()
        job=await routes.start_scan(identifier,routes.ScanRequest())
        await asyncio.sleep(0)
        await routes.cancel_scan(identifier,job["id"])
        await asyncio.sleep(0)
        assert service.repository.get(identifier).scan_result["scan_job"]["status"]=="cancelled"
        assert "tables" not in service.repository.get(identifier).scan_result
    asyncio.run(scenario())

def test_scan_interrupted_after_api_restart_is_explicit(monkeypatch):
    import asyncio
    from app import database_routes as routes
    from app.datasources import DataSourceService,DataSourceRepository
    service=DataSourceService(repository=DataSourceRepository(dsn=""))
    monkeypatch.setattr(routes,"datasource_service",service)
    item=service.repository.get("retail-credit-pg")
    item.scan_result={"scan_job":{"id":"orphan","status":"running"}}
    assert asyncio.run(routes.scan_status(item.id,"orphan"))["status"]=="interrupted"

def test_native_mysql_decimal_width_and_timezone_types_are_preserved():
    from types import SimpleNamespace
    from services.executor.app.result_types import column_types
    from app.result_engine import _type
    fields=[SimpleNamespace(length=32,flags=0,scale=8)]
    assert column_types([("balance",246,None,None,32,8,None)],True,mysql_fields=fields)==["DECIMAL(30,8)"]
    assert column_types([("at",1184,None,None,None,None,None)])==["TIMESTAMPTZ"]
    assert _type("TIMESTAMPTZ")=="TIMESTAMPTZ"

def test_vendor_date_timestamp_and_binary_semantics_are_explicit():
    from app.mdl_export import column_type
    assert column_type({"type":"DATE"},product="oracle")=="timestamp"
    assert column_type({"type":"TIMESTAMP(6) WITH TIME ZONE"},product="oracle")=="timestamptz"
    assert column_type({"type":"timestamp"},product="sqlserver")=="binary"
    assert column_type({"type":"datetimeoffset(7)"},product="sqlserver")=="timestamptz"

def test_oracle_legacy_paging_is_explicit_and_keeps_order_inside_limit(monkeypatch):
    from app import datasources
    from app.datasources import DataSourceRepository,DataSourceRecord
    from app.sql_dialect import compile_step
    from semora_contracts import SQLProgramStep
    from services.executor.app.main import assert_readonly
    repo=DataSourceRepository(dsn="")
    repo.save(DataSourceRecord("old","legacy","oracle","DATASOURCE_OLD_URL",scan_result={"capabilities":{
        "query":"verified","dialect":"oracle","pagination_strategy":"rownum","sql_baseline":"portable-read-v1"}}))
    monkeypatch.setattr(datasources.datasource_service,"repository",repo)
    step=SQLProgramStep(name="q",purpose="deterministic paging",data_source_id="old",sql="SELECT id FROM items ORDER BY id LIMIT 5",output_columns=["id"])
    artifact=compile_step(step,{"dataSourceId":"old","objects":[{"name":"item","table":"items","properties":[{"name":"id"}]}]})
    assert "ROWNUM <= 5" in artifact.physical_sql
    assert artifact.physical_sql.index("ORDER BY")<artifact.physical_sql.index("ROWNUM")
    assert_readonly(artifact.physical_sql,"oracle")

def test_cross_step_timezone_metadata_keeps_temporal_operations():
    from semora_contracts import SQLProgramStep,ExecutionResult
    from app.sql_dialect import compile_step
    from app.result_engine import local_query
    step=SQLProgramStep(name="utc",purpose="timezone arithmetic",engine="local",data_source_id="",
        sql="SELECT EXTRACT(HOUR FROM event_time AT TIME ZONE 'UTC') AS h FROM events",output_columns=["h"])
    inputs={"events":ExecutionResult(columns=["event_time"],column_types=["TIMESTAMP WITH TIME ZONE"],
        rows=[["2025-01-02T03:04:05+08:00"]],row_count=1,duration_ms=0)}
    artifact=compile_step(step,{},inputs)
    result=local_query(artifact,inputs,{"query_timeout_seconds":10,"max_result_rows":10})
    assert result.rows==[[19]]

def test_legacy_scan_requests_rescan_instead_of_losing_constraints():
    mdl,report=build_mdl({"tables":[{"schema":"public","name":"old","columns":[{"name":"id","type":"integer","primary_key":True}]}]},"postgresql")
    assert mdl is None
    assert report["issues"][0]["code"]=="legacy_snapshot_requires_rescan"
