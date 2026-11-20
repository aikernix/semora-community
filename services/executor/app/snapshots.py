"""Lossless metadata supplements and a common snapshot envelope."""
from collections import defaultdict
import psycopg
from . import mysql_adapter, scan_control, adapter_registry
from .timeouts import scan_timeout_ms

def supplement_native(result, dsn, kind):
    tables = {(t["schema"],t["name"]): t for t in result.get("tables", [])}
    gaps = result.setdefault("permission_gaps", [])
    protocol = adapter_registry.protocol(kind)
    factory = (lambda: mysql_adapter.connect(dsn,scan_timeout_ms())) if protocol=="mysql" else (lambda: psycopg.connect(dsn,connect_timeout=5))
    try:
        with scan_control.track(factory()) as connection:
            with connection.cursor() as cur:
                if protocol=="mysql": mysql_adapter.begin_readonly(connection,scan_timeout_ms())
                else: cur.execute("SET TRANSACTION READ ONLY");cur.execute("SELECT set_config('statement_timeout', %s, true)",(str(scan_timeout_ms()),))
                if protocol=="postgres":
                    cur.execute("""SELECT n.nspname,c.relname,c.relkind,obj_description(c.oid),a.attname,
                        format_type(a.atttypid,a.atttypmod),col_description(c.oid,a.attnum)
                        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
                        WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','m','f')
                        AND has_table_privilege(c.oid,'SELECT')""")
                    for schema,name,kind_code,comment,col,dtype,description in cur.fetchall():
                        t=tables.get((schema,name))
                        if not t: continue
                        t.update(kind="VIEW" if kind_code in {"v","m"} else "TABLE",description=comment or "")
                        for column in t["columns"]:
                            if column["name"]==col: column.update(raw_type=dtype,type=dtype,description=description or "")
                    cur.execute("""SELECT n.nspname,c.relname,con.conname,con.contype,
                        ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.num ORDER BY k.ord),
                        tn.nspname,tc.relname,
                        ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=k.num ORDER BY k.ord),
                        pg_get_constraintdef(con.oid)
                        FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                        LEFT JOIN pg_class tc ON tc.oid=con.confrelid LEFT JOIN pg_namespace tn ON tn.oid=tc.relnamespace
                        WHERE con.contype IN ('p','u','f') AND n.nspname NOT IN ('pg_catalog','information_schema') AND has_table_privilege(c.oid,'SELECT')""")
                    constraints=cur.fetchall()
                else:
                    cur.execute("""SELECT k.TABLE_SCHEMA,k.TABLE_NAME,k.CONSTRAINT_NAME,t.CONSTRAINT_TYPE,
                        k.COLUMN_NAME,k.REFERENCED_TABLE_SCHEMA,k.REFERENCED_TABLE_NAME,k.REFERENCED_COLUMN_NAME,k.ORDINAL_POSITION
                        FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.TABLE_CONSTRAINTS t
                        ON t.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND t.TABLE_NAME=k.TABLE_NAME AND t.CONSTRAINT_NAME=k.CONSTRAINT_NAME
                        WHERE k.TABLE_SCHEMA=DATABASE() ORDER BY k.TABLE_SCHEMA,k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION""")
                    grouped={}
                    for schema,name,key,ctype,col,ts,tn,tc,pos in cur.fetchall():
                        item=grouped.setdefault((schema,name,key),[schema,name,key,{"PRIMARY KEY":"p","UNIQUE":"u","FOREIGN KEY":"f"}.get(ctype),[],ts,tn,[],""])
                        item[4].append(col)
                        if tc: item[7].append(tc)
                    constraints=list(grouped.values())
                for t in tables.values():
                    t.update(primary_keys=[], unique_constraints=[], foreign_keys=[], indexes=[])
                fks=[]
                for schema,name,key,ctype,cols,ts,tn,target,definition in constraints:
                    t=tables.get((schema,name))
                    if not t:continue
                    entry={"name":key,"columns":cols,"definition":definition}
                    if ctype=="f":
                        entry.update(target_table=f"{ts}.{tn}",target_columns=target)
                        t["foreign_keys"].append(entry)
                        fks.append({**entry,"source_table":f"{schema}.{name}","origin":"declared"})
                    elif ctype in {"p","u"}: t["primary_keys" if ctype=="p" else "unique_constraints"].append(entry)
                result["foreign_keys"]=fks
                if protocol=="postgres":
                    cur.execute("""SELECT n.nspname,c.relname,ci.relname,i.indisunique,
                        array_agg(a.attname ORDER BY k.ord)
                        FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
                        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ci ON ci.oid=i.indexrelid
                        JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) ON true
                        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
                        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                        AND i.indpred IS NULL AND i.indexprs IS NULL AND has_table_privilege(c.oid,'SELECT')
                        GROUP BY n.nspname,c.relname,ci.relname,i.indisunique""")
                    index_rows = cur.fetchall()
                else:
                    cur.execute("""SELECT TABLE_SCHEMA,TABLE_NAME,INDEX_NAME,NON_UNIQUE,
                        SEQ_IN_INDEX,COLUMN_NAME FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_SCHEMA,TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX""")
                    grouped_indexes = {}
                    for schema,name,index_name,non_unique,position,column in cur.fetchall():
                        if not index_name or not column:
                            continue
                        entry = grouped_indexes.setdefault(
                            (schema,name,index_name), [schema,name,index_name,not bool(non_unique),[]]
                        )
                        entry[4].append(column)
                    index_rows = list(grouped_indexes.values())
                for schema,name,index_name,unique,columns in index_rows:
                    table = tables.get((schema,name))
                    if table and columns:
                        table["indexes"].append(
                            {"name":index_name,"columns":list(columns),"unique":bool(unique)}
                        )
            connection.rollback()
    except Exception as exc:
        # Never represent inaccessible constraints as absent, nor erase the original scan.
        gaps.append({"area":"metadata_supplement","status":"unavailable","error_class":type(exc).__name__})
    return result

def normalize_snapshot(result):
    result["snapshot_version"]=2
    gaps=result.setdefault("permission_gaps",[])
    for table in result.get("tables",[]):
        table.setdefault("id",".".join(str(table.get(k) or "") for k in ("catalog","schema","name") if table.get(k)))
        table.setdefault("description",table.get("comment", ""))
        table.setdefault("kind","TABLE")
        for column in table.get("columns",[]):
            column.setdefault("raw_type",column["type"])
            column.setdefault("description",column.get("comment", ""))
        if "primary_keys" not in table:
            gaps.append({"table":table["id"],"area":"constraints","status":"not_read"})
    connection=result.setdefault("connection",{})
    connection.setdefault("schemas",sorted({t.get("schema", "") for t in result.get("tables",[])}))
    connection.setdefault("catalogs",sorted({t.get("catalog") or connection.get("database", "") for t in result.get("tables",[]) if t.get("catalog") or connection.get("database")}))
    result.setdefault("capabilities",{
        "connection":"verified","structure":"verified","comments":"partial" if gaps else "verified",
        "constraints":"partial" if gaps else "verified","mdl":"pending","query":"partial",
        "live_validation":"metadata_only", "cte":"unverified", "window_functions":"unverified"})
    return result
