"""Administrator-managed JDBC bundles. No driver code is loaded in Python."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from uuid import uuid4
import zipfile
import io

from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict
from .connection_settings import connection_store

BUILTINS = {
    "oracle": ("oracle.jdbc.OracleDriver", "ojdbc11-23.6.0.24.10.jar"),
    "sqlserver": ("com.microsoft.sqlserver.jdbc.SQLServerDriver", "mssql-jdbc-12.8.1.jre11.jar"),
    "oceanbase": ("com.oceanbase.jdbc.Driver", "oceanbase-client-2.4.13.jar"),
    "vastbase": ("cn.com.vastbase.Driver", "vastbase-jdbc-2.13.2v.jar"),
}
LOCK = threading.RLock()

class DriverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    driver_class: str = Field(default="", max_length=255, pattern=r"^[\w.$]*$")
    version: str = Field(default="", max_length=100)
    enabled: bool = False
    dialect: str = Field(default="", pattern=r"^(|postgres|mysql|oracle|tsql)$")
    # Only known database-enforced boundaries. Generic JDBC defaults to scan-only.
    readonly_strategy: str = Field(default="", pattern=r"^(|postgres|mysql|oracle|sqlserver_permissions)$")
    pagination: str = Field(default="baseline", pattern=r"^(baseline|sqlglot|rownum|scan_only)$")
    type_mapping: dict[str, str] = Field(default_factory=dict, max_length=200)
    metadata_queries: dict[str, str] = Field(default_factory=dict, max_length=4)

def root():
    return connection_store.directory / "drivers"

def _read():
    path = root() / "registry.json"
    return json.loads(path.read_text()) if path.exists() else {}

def _save(values):
    root().mkdir(parents=True, exist_ok=True)
    path = root() / f"registry-{uuid4().hex}.tmp"
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    os.replace(path, root() / "registry.json")

def list_drivers():
    with LOCK:
        values = _read()
    builtins = []
    for key, (cls, filename) in BUILTINS.items():
        jar = Path(os.getenv("JDBC_LIB_DIR", "/opt/semora-jdbc/lib")) / filename
        builtins.append({"id": key, "name": key, "driver_class": cls, "builtin": True,
                         "enabled": jar.exists(), "version": filename.removesuffix(".jar"),
                         "files": [{"name": filename, "sha256": hashlib.sha256(jar.read_bytes()).hexdigest()}] if jar.exists() else []})
    return builtins + list(values.values())

def resolve_driver(driver_id):
    if driver_id in BUILTINS:
        item = next(d for d in list_drivers() if d["id"] == driver_id)
        if not item["enabled"]:
            raise HTTPException(409, "内置 JDBC 驱动未安装，请更新 Executor 镜像")
        return {**item, "classpath": str(Path(os.getenv("JDBC_LIB_DIR", "/opt/semora-jdbc/lib")) / "*")}
    with LOCK:
        item = _read().get(driver_id)
    if not item or not item["enabled"]:
        raise HTTPException(409, "请管理员安装并启用厂商 JDBC 驱动")
    for file in item["files"]:
        path = root() / driver_id / file["name"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != file["sha256"]:
            raise HTTPException(409, "JDBC 驱动包哈希检查失败")
    return {**item, "classpath": str(root() / driver_id / "*")}

def install(config: DriverConfig, files: list[tuple[str, bytes]]):
    if not files or len(files) > 20 or sum(len(data) for _, data in files) > 100 * 1024**2:
        raise HTTPException(413, "驱动包限制 20 个 JAR、合计 100MB")
    key = uuid4().hex
    records, classes, available_classes = [], set(), set()
    for index, (name, data) in enumerate(files):
        if not name.lower().endswith(".jar"):
            raise HTTPException(422, "仅允许上传 JAR 文件")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as jar:
                info = jar.getinfo("META-INF/services/java.sql.Driver") if "META-INF/services/java.sql.Driver" in jar.namelist() else None
                if info and info.file_size < 16384:
                    classes.update(line.split("#")[0].strip() for line in jar.read(info).decode().splitlines() if line.split("#")[0].strip())
                available_classes.update(name[:-6].replace("/", ".") for name in jar.namelist() if name.endswith(".class"))
                # A supplied class must exist in at least one dependency.
                if config.driver_class.replace(".", "/") + ".class" in jar.namelist():
                    classes.add(config.driver_class)
        except (ValueError, zipfile.BadZipFile, UnicodeError):
            raise HTTPException(422, "无效 JAR 文件") from None
        records.append({"name": f"{index:02d}.jar", "original_name": Path(name).name,
                        "sha256": hashlib.sha256(data).hexdigest()})
    if not config.driver_class and len(classes)==1:
        config=config.model_copy(update={"driver_class":next(iter(classes))})
    if config.driver_class not in available_classes:
        raise HTTPException(422, {"message":"请指定上传包中的 JDBC 驱动类", "driver_classes":sorted(classes)[:20]})
    validate_templates(config)
    with LOCK:
        folder = root() / key
        folder.mkdir(parents=True)
        for record, (_, data) in zip(records, files):
            (folder / record["name"]).write_bytes(data)
        item = {"id": key, **config.model_dump(), "builtin": False, "revision": uuid4().hex, "files": records, "discovered_classes": sorted(classes)}
        values = _read(); values[key] = item; _save(values)
    return item

def validate_templates(config):
    if config.pagination=="rownum" and config.dialect!="oracle":
        raise HTTPException(422,"ROWNUM 分页只适用于显式配置的 Oracle 方言")
    import sqlglot
    from sqlglot import exp
    for key, sql in config.metadata_queries.items():
        if key not in {"columns", "constraints", "comments", "tables"}:
            raise HTTPException(422, "元数据模板名称无效")
        try:
            trees = sqlglot.parse(sql, read=config.dialect or "postgres")
            if len(trees) != 1 or not isinstance(trees[0], exp.Select) or any(trees[0].find(t) for t in (exp.Into, exp.Lock, exp.Insert, exp.Delete, exp.Update, exp.Command)):
                raise ValueError()
        except (ValueError, sqlglot.errors.SqlglotError):
            raise HTTPException(422, "元数据模板必须是单条只读 SELECT") from None

def update(driver_id, config):
    validate_templates(config)
    with LOCK:
        values = _read()
        if driver_id not in values:
            raise HTTPException(404, "可更新的自定义驱动不存在")
        if config.driver_class != values[driver_id]["driver_class"]:
            raise HTTPException(422, "更换驱动类请重新上传包")
        values[driver_id].update(config.model_dump()); values[driver_id]["revision"]=uuid4().hex; _save(values)
        return values[driver_id]
