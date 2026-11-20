"""Explicit transport/dialect registry. Unknown products never fall through to PostgreSQL."""
from fastapi import HTTPException
from semora_contracts.database import PRODUCTS, native_protocol
from . import jdbc_adapter

def product(kind):
    if kind not in PRODUCTS:
        raise HTTPException(422, f"未注册数据源类型：{kind}")
    return PRODUCTS[kind]

def is_jdbc(kind):
    return product(kind)["transport"] == "jdbc"

def protocol(kind):
    product(kind)
    return native_protocol(kind)

def dialect(body):
    item = product(body.type)
    if item["transport"] == "native": return item["dialect"]
    _, value, strategy = jdbc_adapter.configuration(jdbc_adapter.settings(body))
    if not value or not strategy:
        raise HTTPException(409, "该数据源尚未配置可安全执行的方言和只读策略")
    return value
