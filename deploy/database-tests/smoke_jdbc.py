"""Run inside the Executor image on the isolated fixture network.

Mount the unmodified PostgreSQL vendor JDBC JAR at /fixture/postgresql-42.7.7.jar.
This verifies the custom-JDBC path, not Oracle/SQL Server/OceanBase support.
"""
import json
from pathlib import Path
import tempfile

from app import main,drivers,jdbc_adapter,connection_settings
from app.connection_settings import ConnectionSettings,ConnectionStore
from fastapi import HTTPException

def run():
    with tempfile.TemporaryDirectory() as folder:
        store=ConnectionStore(folder)
        for module in (main,drivers,jdbc_adapter,connection_settings):module.connection_store=store
        bundle=drivers.install(drivers.DriverConfig(name="PostgreSQL fixture",driver_class="org.postgresql.Driver",
            version="42.7.7",enabled=True,dialect="postgres",readonly_strategy="postgres"),
            [("postgresql.jar",Path("/fixture/postgresql-42.7.7.jar").read_bytes())])
        for host in ("pg15","pg16"):
            settings=ConnectionSettings(type="jdbc",port=5432,host=host,database="fixture",username="fixture_reader",password="fixture_reader_only",
                jdbc_url=f"jdbc:postgresql://{host}:5432/fixture",driver_id=bundle["id"],schema_name="b")
            store.save(host,store.candidate(host,"DATASOURCE_FIXTURE_URL",settings))
            source=main.DataSourceInput(datasource_id=host,type="jdbc",secret_ref="DATASOURCE_FIXTURE_URL")
            info=main.test_datasource(source,None)
            assert info["product"]=="PostgreSQL" and info["read_only"],info
            snapshot=main.scan_datasource(main.DataSourceScanInput(**source.model_dump()),None)
            assert not snapshot["permission_gaps"],snapshot["permission_gaps"]
            assert any(len(r["columns"])==2 for r in snapshot["foreign_keys"])
            verified=main.validate_datasource_query(source,None)
            assert verified["capabilities"]["query"]=="verified",verified
            for feature in ("cte", "window_functions", "recursive_cte", "offset_pagination"):
                assert verified["capabilities"][feature]["status"]=="verified",verified
            result=main.execute_query(main.QueryInput(datasource=source,
                sql="SELECT balance,ts FROM b.loan WHERE id=%(id)s LIMIT 2",parameters={"id":101}),None)
            assert result["rows"][0][0]=="12345678901234567890.12345678",result
            assert result["column_types"][0]=="DECIMAL(30,8)"
            assert result["column_types"][1]=="TIMESTAMPTZ",result
            from datetime import datetime
            assert datetime.fromisoformat(result["rows"][0][1]).timestamp()==datetime.fromisoformat("2025-01-02T03:04:05+08:00").timestamp()
            nullable=main.execute_query(main.QueryInput(datasource=source,sql="SELECT CAST(%(v)s AS NUMERIC(30,8)) AS n LIMIT 1",parameters={"v":None}),None)
            assert nullable["rows"]==[[None]]
            encoded=main.execute_query(main.QueryInput(datasource=source,sql="SELECT CAST(%(v)s AS NUMERIC(30,8)) AS n LIMIT 1",parameters={"v":"12345678901234567890.12345678"}),None)
            assert encoded["rows"][0][0]=="12345678901234567890.12345678"
            # New store object proves persisted connections survive process restart.
            assert ConnectionStore(folder).get(host,"DATASOURCE_FIXTURE_URL","jdbc")["driver_id"]==bundle["id"]
            try:
                main.execute_query(main.QueryInput(datasource=source,sql="SELECT pg_sleep(5) LIMIT 1",timeout_ms=100),None)
                raise AssertionError("timeout unexpectedly succeeded")
            except HTTPException as exc:assert exc.status_code in {502,504},exc
            from concurrent.futures import ThreadPoolExecutor
            import time
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(main.execute_query,main.QueryInput(datasource=source,sql="SELECT pg_sleep(20) LIMIT 1",execution_id="fixture-cancel"),None)
                for attempt in range(100):
                    if "fixture-cancel" in jdbc_adapter.ACTIVE:break
                    time.sleep(0.05)
                assert main.cancel_query("fixture-cancel",None)["accepted"]
                try:future.result(timeout=5);raise AssertionError("cancel did not interrupt worker")
                except HTTPException:pass
            # A fresh worker must still execute after an earlier worker was killed.
            main.execute_query(main.QueryInput(datasource=source,sql="SELECT 1 LIMIT 1"),None)
            print(json.dumps({"host":host,"product":info["product"],"version":info["version"],"driver_version":info["driver_version"],
                "scan_tables":len(snapshot["tables"]),"query":"passed","precision":"passed","timeout":"passed","restart":"passed"}))
        config=drivers.DriverConfig.model_validate({k:v for k,v in bundle.items() if k in drivers.DriverConfig.model_fields})
        templates={
            "tables":"SELECT current_database() AS catalog,table_schema AS schema_name,table_name,table_type AS kind,'from_template' AS description FROM information_schema.tables WHERE table_schema='a' AND table_name='customer'",
            "columns":"SELECT current_database()||'.'||table_schema||'.'||table_name AS table_id,column_name,data_type AS raw_type,(is_nullable='YES') AS nullable,numeric_precision::integer AS precision,numeric_scale::integer AS scale,'from_template' AS description FROM information_schema.columns WHERE table_schema='a' AND table_name='customer' ORDER BY ordinal_position",
            "constraints":"""SELECT current_database()||'.a.customer' AS table_id,
                json_build_object('primary_keys',(SELECT json_agg(json_build_object('name',con.conname,'columns',
                ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,ord)
                JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.num ORDER BY k.ord)))
                FROM pg_constraint con WHERE con.conrelid='a.customer'::regclass AND con.contype='p'),
                'unique_constraints',json_build_array(),'foreign_keys',json_build_array())::text AS constraints_json""",
        }
        drivers.update(bundle["id"],config.model_copy(update={"metadata_queries":templates}))
        cfg=store.get("pg15","DATASOURCE_FIXTURE_URL","jdbc");cfg["schema_name"]="a";store.save("pg15",cfg)
        source=main.DataSourceInput(datasource_id="pg15",type="jdbc",secret_ref="DATASOURCE_FIXTURE_URL")
        snapshot=main.scan_datasource(main.DataSourceScanInput(**source.model_dump()),None)
        assert not snapshot["permission_gaps"],snapshot
        assert snapshot["tables"][0]["description"]=="from_template"
        assert snapshot["tables"][0]["primary_keys"][0]["columns"]==["tenant_id","customer_id"]
        assert len(snapshot["tables"][0]["columns"])==3
        try:
            main.execute_query(main.QueryInput(datasource=source,sql="SELECT 1 LIMIT 1"),None)
            raise AssertionError("driver change failed to invalidate query validation")
        except HTTPException as exc:assert exc.status_code==409,exc
        print(json.dumps({"advanced_metadata_templates":"passed","driver_change_invalidation":"passed"}))

if __name__=="__main__":run()
