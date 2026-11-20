"""Opt-in isolated database acceptance. Never uses configured business sources.

docker compose -f deploy/compose.database-tests.yml --profile mysql up -d
SEMORA_TEST_DATABASES=postgresql,mysql pytest services/api/tests/test_database_live.py
"""
import os
import pytest
from services.executor.app import main as executor
from services.executor.app.connection_settings import ConnectionSettings,ConnectionStore
from app.mdl_export import build_mdl

CASES=[("postgresql",25432),("postgresql",25433),("mysql",23306),("mysql",23307),("mysql",23305)]

@pytest.mark.parametrize("kind,port",CASES)
def test_isolated_native_database(kind,port,tmp_path,monkeypatch):
    if kind not in os.getenv("SEMORA_TEST_DATABASES","").split(","):pytest.skip("isolated instance not requested")
    if port==23305 and os.getenv("SEMORA_TEST_LEGACY")!="true":pytest.skip("legacy fixture not requested")
    store=ConnectionStore(str(tmp_path));monkeypatch.setattr(executor,"connection_store",store)
    cfg=ConnectionSettings(type=kind,host="127.0.0.1",port=port,database="fixture",username="fixture_reader",password="fixture_reader_only")
    store.save("fixture",store.candidate("fixture","DATASOURCE_FIXTURE_URL",cfg))
    source=executor.DataSourceInput(datasource_id="fixture",secret_ref="DATASOURCE_FIXTURE_URL",type=kind)
    assert executor.test_datasource(source,None)["read_only"]
    capabilities=executor.validate_datasource_query(source,None)["capabilities"]
    assert capabilities["sql_baseline"]=="portable-read-v1"
    assert capabilities["cte"]["status"]==("unsupported" if port==23305 else "verified")
    assert capabilities["window_functions"]["status"]==("unsupported" if port==23305 else "verified")
    assert capabilities["offset_pagination"]["status"]=="verified"
    scan=executor.scan_datasource(executor.DataSourceScanInput(**source.model_dump()),None)
    assert not scan["permission_gaps"],scan["permission_gaps"]
    customer=next(t for t in scan["tables"] if t["name"]=="customer")
    assert customer["primary_keys"][0]["columns"]==["tenant_id","customer_id"]
    assert any(len(r["columns"])==2 for r in scan["foreign_keys"])
    assert customer["description"]
    schema="b" if kind=="postgresql" else "fixture"
    query=executor.QueryInput(sql=f"SELECT balance,ts FROM {schema}.loan WHERE id=%(id)s LIMIT 2",parameters={"id":101},datasource=source)
    result=executor.execute_query(query,None)
    assert result["rows"][0][0]=="12345678901234567890.12345678"
    assert "DECIMAL(30,8)" in result["column_types"]
    assert result["row_count"]==1
    source_tables=[t for t in scan["tables"] if t["name"] in {"loan","customer"} and (kind=="mysql" or t["id"] in {"a.customer","b.loan"})]
    mdl,report=build_mdl({**scan,"tables":source_tables},kind)
    assert mdl is not None,report
    assert report["wren_build"] in {"passed","unavailable"}
    if kind=="postgresql":
        with pytest.raises(Exception):executor.execute_query(query.model_copy(update={"sql":"SELECT pg_sleep(2) LIMIT 1","parameters":{},"timeout_ms":100}),None)
    with pytest.raises(Exception):executor.execute_query(query.model_copy(update={"sql":f"DELETE FROM {schema}.loan","parameters":{}}),None)
