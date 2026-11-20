"""Version-independent connection contract. Secrets remain owned by Executor."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

PRODUCTS = {
    "postgresql": {"label": "PostgreSQL", "port": 5432, "transport": "native", "protocol": "postgres", "dialect": "postgres"},
    "mysql": {"label": "MySQL", "port": 3306, "transport": "native", "protocol": "mysql", "dialect": "mysql"},
    "oracle": {"label": "Oracle", "port": 1521, "transport": "jdbc", "driver_id": "oracle", "dialect": "oracle"},
    "sqlserver": {"label": "SQL Server", "port": 1433, "transport": "jdbc", "driver_id": "sqlserver", "dialect": "tsql"},
    "oceanbase": {"label": "OceanBase", "port": 2881, "transport": "jdbc", "driver_id": "oceanbase"},
    "goldendb": {"label": "GoldenDB", "port": 8880, "transport": "native", "protocol": "mysql", "dialect": "mysql"},
    "vastbase": {"label": "海量数据库 / Vastbase", "port": 5432, "transport": "jdbc", "driver_id": "vastbase", "dialect": "postgres"},
    "jdbc": {"label": "自定义 JDBC", "port": 5432, "transport": "jdbc"},
}


def native_protocol(kind: str) -> str:
    item = PRODUCTS.get(kind, {})
    return item.get("protocol", "") if item.get("transport") == "native" else ""

SQL_BASELINE_ID = "portable-read-v1"
SQL_OPTIONAL_FEATURES = {
    "cte": "common_table_expressions",
    "window_functions": "window_functions",
    "recursive_cte": "recursive_common_table_expressions",
    "offset_pagination": "offset_pagination",
}

def normalize_sql_probe(probe, dialect=""):
    """Classify isolated synthetic probes, never arbitrary business SQL failures."""
    if not isinstance(probe, dict):
        return {"status": "unverified"}
    value = dict(probe)
    state = value.get("status") if isinstance(value.get("status"), str) else None
    if state in {"verified", "unsupported"}:
        return value
    code = value.get("code") if isinstance(value.get("code"), (int, str)) else None
    sqlstate = value.get("sqlstate") if isinstance(value.get("sqlstate"), str) else None
    # Syntax/feature errors from fixed minimal probes only. Access, timeouts
    # and network errors must never become a global negative capability.
    dialect_codes = {"mysql": {1064, 1235}, "oracle": {900, 907, 933, 936}, "tsql": {102, 156}}
    unsupported = code in dialect_codes.get(dialect, set()) or code in {"42601", "0A000"} or sqlstate in {"42601", "0A000"}
    value["status"] = "unsupported" if state == "unavailable" and unsupported else "unverified"
    return value


def baseline_pagination(dialect: str, configured: str = "baseline") -> str:
    if configured and configured != "baseline":
        return configured
    return "rownum" if dialect == "oracle" else "sqlglot"

class DatabaseConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    config_version: Literal[2] = 2
    type: Literal["postgresql", "mysql", "oracle", "sqlserver", "oceanbase", "goldendb", "vastbase", "jdbc"]
    host: str = Field(default="", max_length=255, pattern=r"^[^\s/?#@]*$")
    port: int = Field(ge=1, le=65535)
    database: str = Field(default="", max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = SecretStr("")
    schema_name: str = Field(default="", max_length=255)
    catalog: str = Field(default="", max_length=255)
    compatibility_mode: Literal["", "mysql", "oracle", "postgres", "tsql"] = ""
    service_kind: Literal["service", "sid"] = "service"
    jdbc_url: str = Field(default="", max_length=4096)
    driver_id: str = Field(default="", max_length=64, pattern=r"^[a-zA-Z0-9_-]*$")
    properties: dict[str, str] = Field(default_factory=dict, max_length=32)
    product_override: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_connection(self):
        if not self.jdbc_url and (not self.host or not self.database):
            raise ValueError("请填写地址和数据库，或 JDBC 连接串")
        if self.jdbc_url:
            if native_protocol(self.type) or not self.jdbc_url.startswith("jdbc:"):
                raise ValueError("JDBC 连接串只适用于 JDBC 产品")
            # URLs/properties are public configuration: credentials must use secret fields.
            import re
            from urllib.parse import unquote
            decoded = unquote(unquote(self.jdbc_url))
            if re.search(r"(?i)(password|passwd|pwd|user|username|token|secret|credential)\s*[=:]|//[^/;]*@|jdbc:oracle:[^:]+:[^@]+@", decoded):
                raise ValueError("连接串不得包含账号或密码，使用专用字段")
        import re
        if any(re.search(r"(?i)password|passwd|pwd|secret|token|credential|^(user|username)$", k) for k in self.properties):
            raise ValueError("高级属性不得包含账号或密码")
        if native_protocol(self.type) and self.catalog and self.catalog!=self.database:
            raise ValueError("原生连接的 Catalog 必须与数据库名一致，其他数据库请新增数据源")
        if native_protocol(self.type)=="mysql" and self.schema_name and self.schema_name!=self.database:
            raise ValueError("MySQL 协议原生连接的 Schema 必须与数据库名一致")
        if native_protocol(self.type)=="mysql" and set(self.properties)-{"ssl_ca","ssl_cert","ssl_key","ssl_verify_cert","ssl_verify_identity"}:
            raise ValueError("MySQL 协议原生连接仅支持 ssl_ca / ssl_cert / ssl_key / ssl_verify_cert / ssl_verify_identity 属性")
        if self.type == "oceanbase" and self.compatibility_mode not in {"mysql", "oracle"}:
            raise ValueError("OceanBase 必须选择 MySQL 或 Oracle 模式")
        return self
