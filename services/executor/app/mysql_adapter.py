"""MySQL connections stay inside Executor; all queries use read-only transactions."""
from __future__ import annotations

import json
import importlib.metadata
from urllib.parse import unquote, urlsplit, parse_qs

import pymysql
from fastapi import HTTPException
from .errors import database_error
from . import scan_control
from .timeouts import scan_timeout_ms, query_timeout_ms


def connect(dsn: str, timeout_ms: int | None = None):
    timeout_ms = timeout_ms if timeout_ms is not None else query_timeout_ms()
    url = urlsplit(dsn)
    if url.scheme != 'mysql' or not url.hostname or not url.path.strip('/'):
        raise HTTPException(422, 'MySQL 连接配置无效')
    properties={k:v[-1] for k,v in parse_qs(url.query).items()}
    tls={k:v for k,v in properties.items() if k in {"ssl_ca","ssl_cert","ssl_key"}}
    tls.update({k:v.lower()=="true" for k,v in properties.items() if k in {"ssl_verify_cert","ssl_verify_identity"}})
    return pymysql.connect(
        host=url.hostname, port=url.port or 3306,
        user=unquote(url.username or ''), password=unquote(url.password or ''),
        database=unquote(url.path.lstrip('/')), charset='utf8mb4',
        connect_timeout=5, read_timeout=max(1, timeout_ms / 1000 + 1),
        write_timeout=5, autocommit=False, **tls,
    )


def begin_readonly(connection, timeout_ms: int):
    with connection.cursor() as cursor:
        cursor.execute('SET SESSION MAX_EXECUTION_TIME = %s', (timeout_ms,))
        cursor.execute('START TRANSACTION READ ONLY')


def inspect(dsn: str) -> dict:
    try:
        with scan_control.track(connect(dsn)) as connection:
            begin_readonly(connection, 5000)
            with connection.cursor() as cursor:
                cursor.execute('SELECT DATABASE(), CURRENT_USER(), VERSION()')
                database, user, version = cursor.fetchone()
                cursor.execute('SHOW GRANTS')
                grants = [row[0] for row in cursor.fetchall()]
            connection.rollback()
        import re
        writable = any(re.search(r'\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|EXECUTE|SUPER|GRANT OPTION)\b', grant, re.I) for grant in grants)
        return {
            'product':'MariaDB' if 'mariadb' in version.lower() else 'OceanBase' if 'oceanbase' in version.lower() else 'MySQL',
            'driver':'PyMySQL','driver_version':importlib.metadata.version('PyMySQL'),
            'database': database, 'user': user, 'version': version,
            'read_only': True, 'account_read_only': not writable,
            'default_transaction_read_only': 'transaction_enforced',
            'dangerous_role_capabilities': writable,
            'warning': '账号具有写权限；Semora 每次查询强制只读事务' if writable else '',
        }
    except HTTPException:
        raise
    except Exception as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 'unknown'
        message = '登录被拒绝，请核对密码及账号来源地址授权' if code == 1045 else '请核对网络、数据库名和服务配置'
        raise HTTPException(502, f'MySQL 连接失败（{code}）：{message}') from exc


def scan(dsn: str, sensitivity_level, table_names: list[str] | None = None) -> dict:
    info = inspect(dsn)
    selected = ' AND c.TABLE_NAME IN (' + ','.join(['%s'] * len(table_names)) + ')' if table_names else ''
    tables = {}
    sensitive = []
    try:
        with scan_control.track(connect(dsn, scan_timeout_ms())) as connection:
            begin_readonly(connection, scan_timeout_ms())
            with connection.cursor() as cursor:
                cursor.execute('''SELECT c.TABLE_SCHEMA,c.TABLE_NAME,c.COLUMN_NAME,c.COLUMN_TYPE,
                    c.IS_NULLABLE,c.COLUMN_KEY,c.COLUMN_COMMENT,t.TABLE_COMMENT,t.TABLE_TYPE
                    FROM information_schema.COLUMNS c JOIN information_schema.TABLES t
                    ON c.TABLE_SCHEMA=t.TABLE_SCHEMA AND c.TABLE_NAME=t.TABLE_NAME
                    WHERE c.TABLE_SCHEMA=DATABASE()''' + selected + ' ORDER BY c.TABLE_NAME,c.ORDINAL_POSITION', table_names or ())
                for schema, table, name, dtype, nullable, key, comment, table_comment, table_kind in cursor.fetchall():
                    item = tables.setdefault((schema, table), {'schema':schema, 'name':table, 'kind':table_kind, 'comment':table_comment, 'columns':[]})
                    level = sensitivity_level(table, name)
                    item['columns'].append({'name':name,'type':dtype,'nullable':nullable=='YES','primary_key':key=='PRI','comment':comment,'sensitivity':level})
                    if level != 'internal':
                        sensitive.append({'field':f'{schema}.{table}.{name}','level':level})
                cursor.execute('''SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,
                    REFERENCED_TABLE_SCHEMA,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME
                    FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE()
                    AND REFERENCED_TABLE_NAME IS NOT NULL''' + ((' AND TABLE_NAME IN (' + ','.join(['%s'] * len(table_names)) + ') AND REFERENCED_TABLE_NAME IN (' + ','.join(['%s'] * len(table_names)) + ')') if table_names else '') + ' ORDER BY TABLE_NAME,ORDINAL_POSITION', (table_names + table_names) if table_names else ())
                foreign_keys = [{'from':'.'.join(row[:3]),'to':'.'.join(row[3:])} for row in cursor.fetchall()]
            connection.rollback()
    except Exception as exc:
        raise HTTPException(502, f'MySQL 表结构扫描失败：{type(exc).__name__}') from exc
    return {'status':'completed','connection':info,'tables':list(tables.values()),'foreign_keys':foreign_keys,'sensitive_columns':sensitive}


def estimate(dsn: str, sql: str, parameters: dict, timeout_ms: int) -> dict:
    try:
        with connect(dsn, timeout_ms) as connection:
            begin_readonly(connection, timeout_ms)
            with connection.cursor() as cursor:
                cursor.execute('EXPLAIN FORMAT=JSON ' + sql, parameters)
                plan = json.loads(cursor.fetchone()[0])
            connection.rollback()
        rows, costs = [], []
        def visit(node):
            if isinstance(node, dict):
                if 'rows_produced_per_join' in node:
                    rows.append(float(node['rows_produced_per_join']))
                if 'query_cost' in node:
                    costs.append(float(node['query_cost']))
                for value in node.values(): visit(value)
            elif isinstance(node, list):
                for value in node: visit(value)
        visit(plan)
        return {'estimated_rows':int(max(rows, default=0)), 'total_cost':max(costs, default=0), 'source':'mysql-explain'}
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error(exc,'estimate') from exc
