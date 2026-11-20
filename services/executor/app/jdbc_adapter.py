"""Bounded subprocess transport for JDBC. Never put credentials in argv or errors."""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from fastapi import HTTPException
from semora_contracts.database import PRODUCTS, SQL_BASELINE_ID, baseline_pagination, normalize_sql_probe
from . import drivers
from .connection_settings import connection_store
from .errors import database_error

ACTIVE = {}
PROGRESS = {}
LOCK = threading.RLock()

def settings(body):
    cfg = connection_store.get(body.datasource_id, body.secret_ref, body.type)
    if not cfg:
        raise HTTPException(422, "请先保存 JDBC 连接配置")
    if cfg["type"] != body.type:
        raise HTTPException(409, "数据源类型与 Executor 配置不一致")
    return cfg

def configuration(cfg):
    product = PRODUCTS[cfg["type"]]
    driver = drivers.resolve_driver(cfg.get("driver_id") or product.get("driver_id", ""))
    dialect = product.get("dialect", "")
    if cfg["type"] == "oceanbase":
        dialect = cfg.get("compatibility_mode", "")
    if cfg["type"] == "vastbase":
        dialect = cfg.get("compatibility_mode") or "postgres"
    if not driver.get("builtin"):
        dialect = driver.get("dialect", "")
    strategy = {"postgres": "postgres", "mysql": "mysql", "oracle": "oracle", "tsql": "sqlserver_permissions"}.get(dialect, "")
    if cfg["type"] == "vastbase":
        strategy = "postgres"  # Vastbase keeps the openGauss transaction boundary in every compatibility mode.
    if not driver.get("builtin"):
        strategy = driver.get("readonly_strategy", "")
    if driver.get("pagination") == "scan_only":
        strategy = ""
    driver={**driver, "effective_pagination":baseline_pagination(dialect,driver.get("pagination","baseline")), "sql_baseline":SQL_BASELINE_ID}
    return driver, dialect, strategy


def record_detected_compatibility(cfg, connection, datasource_id=""):
    detected = connection.get("detected_compatibility_mode", "")
    if cfg["type"] != "vastbase" or detected not in {"postgres", "oracle", "mysql", "tsql"}:
        return False
    changed = cfg.get("compatibility_mode") != detected
    expected = {"postgres": "postgresql", "oracle": "oracle", "mysql": "mysql", "tsql": "mssql"}[detected]
    properties = dict(cfg.get("properties", {}))
    if properties.get("db_compatibility") != expected:
        properties["db_compatibility"] = expected
        cfg["properties"] = properties
        changed = True
    if changed:
        cfg["_validation"] = ""
    cfg["compatibility_mode"] = detected
    if changed and datasource_id:
        connection_store.save(datasource_id, cfg)
    return changed

def jdbc_url(cfg):
    if cfg.get("jdbc_url"):
        return cfg["jdbc_url"]
    host, port, db = cfg["host"], cfg["port"], cfg["database"]
    # These fields must never be interpreted as JDBC connection properties.
    if any(c in db for c in ";?&#\\") or any(c in host for c in ";?&#\\"):
        raise HTTPException(422, "基础连接字段含 JDBC 分隔符，请使用高级连接串")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    kind = cfg["type"]
    if kind == "oracle":
        return f"jdbc:oracle:thin:@{host}:{port}:{db}" if cfg.get("service_kind") == "sid" else f"jdbc:oracle:thin:@//{host}:{port}/{db}"
    if kind == "sqlserver":
        return f"jdbc:sqlserver://{host}:{port};databaseName={db};encrypt=true;trustServerCertificate=false"
    if kind == "oceanbase":
        return f"jdbc:oceanbase://{host}:{port}/{db}"
    if kind == "vastbase":
        return f"jdbc:vastbase://{host}:{port}/{db}"
    raise HTTPException(422, "请填写该厂商驱动要求的 JDBC 连接串")

def bind_parameters(sql, parameters):
    """Read DBAPI pyformat outside literals/comments. Repeated names stay positional."""
    sql = sql.replace("%%", "%")
    out, values, i, state = [], [], 0, ""
    while i < len(sql):
        ch = sql[i]
        if state in {"'", '"', '`', ']'}:
            out.append(ch)
            if ch == state:
                if i + 1 < len(sql) and sql[i+1] == state:
                    out.append(sql[i+1]); i += 1
                else: state = ""
        elif state == "line":
            out.append(ch)
            if ch == "\n": state = ""
        elif state == "block":
            if sql[i:i+2] == "*/": out.append("*/"); i += 1; state = ""
            else: out.append(ch)
        elif sql[i:i+2] in {"--", "/*"}:
            state = "line" if sql[i:i+2] == "--" else "block";out.append(sql[i:i+2]);i += 1
        elif ch in "'\"`[":
            state = "]" if ch == "[" else ch;out.append(ch)
        elif match := re.match(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", sql[i:]):
            name = match[1]
            if name not in parameters or isinstance(parameters[name], (list, dict)):
                raise HTTPException(422, "JDBC 参数缺失或不是标量")
            values.append(parameters[name]);out.append("?");i += len(match[0])-1
        else: out.append(ch)
        i += 1
    return "".join(out), values

def validation_fingerprint(cfg, driver):
    clean={k:v for k,v in cfg.items() if k != "_validation"}
    return hashlib.sha256(json.dumps([clean,{k:v for k,v in driver.items() if k != "classpath"}],sort_keys=True).encode()).hexdigest()

def run(cfg, operation, *, execution_id=None, timeout_ms=None, validating=False, datasource_id="", **payload):
    from .timeouts import scan_timeout_ms, query_timeout_ms
    timeout_ms = timeout_ms if timeout_ms is not None else (scan_timeout_ms() if operation == "scan" else query_timeout_ms())
    driver, dialect, strategy = configuration(cfg)
    if operation == "query" and (not dialect or not strategy):
        raise HTTPException(409, "该来源仅开放结构扫描，请管理员配置并验证方言及只读策略")
    if operation == "query" and not validating and cfg.get("_validation") != validation_fingerprint(cfg,driver):
        raise HTTPException(409, "目标连接或驱动尚未通过查询验证，请先在数据源页面验证查询")
    home = Path(os.getenv("JDBC_WORKER_HOME", "/opt/semora-jdbc"))
    # Custom bundles must not be shadowed by a bundled vendor driver with the
    # same class name (e.g. an older Oracle driver needed by a legacy server).
    runtime_jars=[str(path) for path in sorted((home/"lib").glob("jackson-*.jar"))]
    classpath = os.pathsep.join([str(home / "jdbc-worker-1.0.0.jar"), *runtime_jars, driver["classpath"]])
    request = dict(operation=operation, connection=cfg, url=jdbc_url(cfg), driver_class=driver["driver_class"],
                   strategy=strategy, dialect=dialect, pagination=driver.get("effective_pagination",baseline_pagination(dialect)), timeout_ms=timeout_ms, metadata_queries=driver.get("metadata_queries",{}), **payload)
    # Disk-backed anonymous output prevents malicious/broken drivers exhausting RAM.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen([os.getenv("JAVA_BIN", "java"), "-Xmx256m", "-cp", classpath, "ai.semora.JdbcWorker"],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL)
        except OSError:
            raise HTTPException(503, "JDBC Worker / Java 未安装，请更新 Executor 镜像") from None
        with LOCK:
            if execution_id:
                ACTIVE[execution_id] = process
                PROGRESS[execution_id] = output
        try:
            from . import scan_control
            if scan_control.cancelled():
                process.kill()
                raise HTTPException(409,"结构扫描已取消")
            process.communicate(json.dumps(request, default=str).encode(), timeout=(timeout_ms / 1000 + 12))
            if process.returncode:
                raise HTTPException(502, detail={"error_type": "connect", "message": "JDBC Worker 异常退出或已取消"})
            size = output.tell()
            if operation != "query" and size > 32 * 1024**2:
                raise HTTPException(413, "JDBC 输出超过 32MB 预算，请缩小扫描范围")
            output.seek(0)
            try: result = json.load(output) if operation == "query" else json.loads(output.read().splitlines()[-1])
            except (ValueError, UnicodeError): raise HTTPException(502, "JDBC Worker 协议错误") from None
            if not result.get("ok"):
                if result.get("error_class") in {"ClassNotFoundException","NoClassDefFoundError","UnsupportedClassVersionError","AbstractMethodError","NoSuchMethodError"}:
                    raise HTTPException(409,detail={"error_type":"unknown","message":"JDBC 驱动类、配套依赖或 Java 版本不兼容","driver_error_class":result["error_class"]})
                exc = Exception(result.get("code"))
                exc.is_sql_timeout = result.get('error_class') == 'SQLTimeoutException'
                exc.sql_message = result.get("sql_message")
                exc.sqlstate = result.get("sqlstate")
                if result.get("code") in {1017,1031,18456,229,230,900,903,907,933,936,102,156,904,942,207,208,1045,1044,1142,1143,1227,1064,1054,1146}:
                    exc.sqlstate = None
                raise database_error(exc, operation)
            value = result["result"]
            if isinstance(value.get("probes"), dict):
                value["probes"] = {key: normalize_sql_probe(probe, dialect) for key, probe in value["probes"].items()}
            connection = value.get("connection", value)
            if record_detected_compatibility(cfg, connection, datasource_id):
                driver, dialect, strategy = configuration(cfg)
            connection["database"] = connection.get("database") or cfg.get("database", "")
            connection.update(dialect=dialect, compatibility_mode=cfg.get("compatibility_mode", ""), product_override=cfg.get("product_override", ""))
            if operation == "scan":
                value["type_mapping"] = driver.get("type_mapping",{})
            if operation in {"test", "scan"}:
                value["capabilities"] = {
                    "connection": "verified", "structure": "verified" if operation == "scan" else "pending",
                    "comments": "partial" if operation == "scan" else "pending",
                    "constraints": "partial" if value.get("permission_gaps") else "verified" if operation == "scan" else "pending",
                    "mdl": "pending", "query": "verified" if cfg.get("_validation") == validation_fingerprint(cfg,driver) else "partial" if connection.get("read_only") and dialect else "pending",
                    "preflight": "unavailable", "cancel": "worker_termination", "dialect": dialect,
                    "sql_baseline":SQL_BASELINE_ID,
                    "pagination_strategy":driver.get("effective_pagination",baseline_pagination(dialect)),
                    "cte": "unverified", "window_functions": "unverified", "recursive_cte":"unverified", "pagination": "unverified",
                    "live_validation": "connection_only" if operation == "test" else "metadata_only",
                }
            return value
        except subprocess.TimeoutExpired:
            process.kill();process.communicate()
            raise HTTPException(504, detail={"error_type": "timeout", "message": "JDBC 操作超时，Worker 已终止",
                                           "diagnostic": {"timeout_origin": "system"}}) from None
        finally:
            with LOCK:
                if execution_id and ACTIVE.get(execution_id) is process:
                    ACTIVE.pop(execution_id, None)
                    PROGRESS.pop(execution_id, None)
            if process.poll() is None: process.kill(); process.wait()

def cancel(execution_id):
    with LOCK:
        process = ACTIVE.get(execution_id)
        if process is None: return False
        process.kill()
        return True

def validate(body, probe_keys=None):
    cfg=settings(body);driver,dialect,strategy=configuration(cfg)
    if cfg["type"] == "vastbase" and not cfg.get("compatibility_mode"):
        run(cfg,"test",timeout_ms=10000,datasource_id=body.datasource_id)
        driver,dialect,strategy=configuration(cfg)
    if not dialect or not strategy:raise HTTPException(409,"请管理员配置方言和只读策略")
    from semora_contracts.sql_probes import probe_catalog
    catalog = probe_catalog(dialect, driver.get("effective_pagination", "baseline"))
    selected = set(catalog) if probe_keys is None else set(probe_keys)
    if not selected <= catalog.keys(): raise HTTPException(422, "未知探测项目")
    inspected=run(cfg,"test",timeout_ms=90000,probe_capabilities=True,datasource_id=body.datasource_id,
                  probe_catalog={k: v for k, v in catalog.items() if k in selected})
    inspected.setdefault("probes", {})
    for key in selected:
        if key not in inspected["probes"]:
            inspected["probes"][key] = {"status": "unverified", "reason": "probe_conversion_unavailable"}
    if not inspected.get("read_only"):
        raise HTTPException(409,detail={"error_type":"permission","message":"数据库只读边界或兼容模式验证未通过"})
    import sqlglot
    sql=sqlglot.parse_one("SELECT 1 AS semora_probe LIMIT 1",read="postgres").sql(dialect=dialect)
    if dialect=="oracle":sql="SELECT 1 AS semora_probe FROM DUAL"+(" WHERE ROWNUM <= 1" if driver.get("effective_pagination")=="rownum" else " FETCH FIRST 1 ROWS ONLY")
    result=run(cfg,"query",sql=sql,parameters=[],validating=True,timeout_ms=5000,max_rows=1,max_bytes=1024,datasource_id=body.datasource_id)
    if result.get("row_count")!=1:raise HTTPException(409,"查询验证未返回预期结果")
    cfg["_validation"]=validation_fingerprint(cfg,driver)
    connection_store.save(body.datasource_id,cfg)
    inspected["capabilities"].update(query="verified",dialect=dialect,live_validation="synthetic_query_only")
    inspected["capabilities"].update(inspected.get("probes",{}))
    return inspected


def progress(execution_id):
    with LOCK:
        stream=PROGRESS.get(execution_id)
        if stream is None:return {}
        try:
            if hasattr(os,"pread"):
                size=os.fstat(stream.fileno()).st_size
                data=os.pread(stream.fileno(),min(size,65536),max(0,size-65536))
            else:return {}
            for line in reversed(data.splitlines()):
                try:
                    value=json.loads(line)
                    if "progress" in value:return value["progress"]
                except ValueError:pass
        except OSError:pass
    return {}
