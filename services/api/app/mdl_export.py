"""Deterministic metadata-to-MDL builder. Never receives connection secrets."""
import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import os
import subprocess
import sys
import threading

VALIDATION_SLOTS = threading.BoundedSemaphore(2)
from copy import deepcopy

WREN_VERSION = "0.8.2"
SCHEMA_COMMIT = "548eee3e864b11e7f11eee15ae82be178a295f22"

def stable_name(value):
    stem = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    return "m_" + stem[:80] + "_" + hashlib.sha256(value.encode()).hexdigest()[:10]

def quoted(value):
    return '"' + value.replace('"', '""') + '"'

def column_type(column, mapping=None, product=None):
    raw = column.get("raw_type", column["type"]).lower().strip()
    if mapping and raw in mapping: return mapping[raw]
    original=raw
    raw = re.sub(r"\s+unsigned$", "", raw)
    if raw.endswith("[]"): return None
    if "time zone" in raw or raw.startswith("datetimeoffset"):return "timestamptz"
    base = raw.split("(")[0].strip()
    if product=="oracle" and base=="date":return "timestamp"
    if product=="oracle" and base=="float":return None
    if product=="sqlserver" and base in {"timestamp","rowversion"}:return "binary"
    aliases = {
        "int":"integer","int4":"integer","integer":"integer","smallint":"smallint","int2":"smallint",
        "tinyint":"smallint","bigint":"bigint","int8":"bigint","serial":"integer","bigserial":"bigint",
        "varchar":"varchar","character varying":"varchar","nvarchar":"varchar","nvarchar2":"varchar","varchar2":"varchar",
        "text":"varchar","ntext":"varchar","char":"char","nchar":"char","character":"char",
        "bool":"boolean","boolean":"boolean","bit":"boolean","float4":"real","real":"real",
        "float8":"double","double":"double","double precision":"double","float":"double",
        "date":"date","timestamp":"timestamp","timestamp without time zone":"timestamp",
        "timestamp with time zone":"timestamptz","timestamptz":"timestamptz","datetime":"timestamp","datetime2":"timestamp",
        "time":"time","time without time zone":"time","bytea":"binary","binary":"binary","varbinary":"binary",
        "binary_float":"real","binary_double":"double",
        "uuid":"uuid","uniqueidentifier":"uuid","json":"json","jsonb":"json",
    }
    if base in {"decimal","numeric","number"}:
        args = re.search(r"\((\d+)(?:\s*,\s*(-?\d+))?\)", raw)
        p = int(args[1]) if args else column.get("precision")
        s = int(args[2] or 0) if args else column.get("scale",0)
        if not p or not 0 < p <= 38 or s is None or not 0 <= s <= p: return None
        return f"decimal({p},{s})"
    if "unsigned" in original:
        return {"tinyint":"smallint","smallint":"integer","int":"bigint","integer":"bigint","bigint":"decimal(20,0)"}.get(base)
    return aliases.get(base)

def relationship_id(relation):
    fields = [relation.get(k) for k in ("source_table","columns","target_table","target_columns")]
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]

def candidates(snapshot):
    """Conservative metadata-only suggestions, never publishes inferred joins."""
    result=[];tables=snapshot.get("tables",[])
    declared={relationship_id(r) for r in snapshot.get("foreign_keys",[]) if "source_table" in r}
    for target in tables:
        for key in target.get("primary_keys",[]) + target.get("unique_constraints",[]):
            cols=key["columns"]
            if not cols: continue
            for source in tables:
                if source["id"]==target["id"]: continue
                src={c["name"]: c for c in source["columns"]};dst={c["name"]: c for c in target["columns"]}
                source_cols=[]
                for col in cols:
                    options=[col] if col.lower() not in {"id","pk"} else [target["name"].rstrip("s")+"_id",target["name"]+"_id"]
                    name=next((n for n in options if n in src and col in dst and column_type(src[n]) and column_type(src[n])==column_type(dst[col])),None)
                    if not name:break
                    source_cols.append(name)
                if len(source_cols)!=len(cols):continue
                r={"source_table":source["id"],"columns":source_cols,"target_table":target["id"],"target_columns":cols,"origin":"inferred","evidence":["同名字段","类型兼容","目标声明唯一键"],"status":"unresolved"}
                r["id"]=relationship_id(r)
                if r["id"] not in declared:result.append(r)
                if len(result)>=500:return sorted(result,key=lambda r:r["id"])
    return sorted({r["id"]:r for r in result}.values(),key=lambda r:r["id"])

def merge_snapshot(previous, current, *, infer_relationships=True):
    current=deepcopy(current)
    old={t.get("id") or ".".join(str(t.get(k)) for k in ("catalog","schema","name") if t.get(k)):t for t in previous.get("tables",[])}
    for t in current.get("tables",[]):
        t.setdefault("id", ".".join(str(t.get(k)) for k in ("catalog","schema","name") if t.get(k)))
        t.setdefault("columns", [])
        prior=old.get(t["id"],{})
        if "user_description" in prior:t["user_description"]=prior["user_description"]
        pc={c["name"]:c for c in prior.get("columns",[])}
        for c in t["columns"]:
            if "user_description" in pc.get(c["name"],{}):c["user_description"]=pc[c["name"]]["user_description"]
    current["scan_diff"]={"added":sorted(set(t["id"] for t in current.get("tables",[]))-set(old)),"removed":sorted(set(old)-set(t["id"] for t in current.get("tables",[])))}
    if not infer_relationships:
        return current
    decisions=previous.get("relationship_decisions",{})
    current["relationship_decisions"]=decisions
    current["relationship_candidates"]=candidates(current)
    prior_candidates={r["id"]:r for r in previous.get("relationship_candidates",[])}
    for r in current["relationship_candidates"]:
        r["status"]=decisions.get(r["id"],{}).get("status","pending")
        if "profile" in prior_candidates.get(r["id"],{}):r["profile"]=prior_candidates[r["id"]]["profile"]
    current["manual_relationships"]=previous.get("manual_relationships",[])
    current["scan_diff"]={"added":sorted(set(t["id"] for t in current.get("tables",[]))-set(old)),"removed":sorted(set(old)-set(t["id"] for t in current.get("tables",[])))}
    return current

def build_mdl(snapshot, product):
    if any("id" not in t or "columns" not in t for t in snapshot.get("tables",[])):
        return None,{"builder_version":1,"wren_core_version":WREN_VERSION,"schema_commit":SCHEMA_COMMIT,
            "status":"blocked","schema_validation":"pending","wren_build":"pending","wren_target_execution":"unverified",
            "issues":[{"code":"legacy_snapshot_requires_rescan","message":"旧扫描缺少完整约束和稳定标识，请重新扫描"}],"pending_relationships":[]}
    issues=[]; models=[]; relationships=[];tables={t["id"]:t for t in snapshot.get("tables",[])}
    names={k:stable_name(k) for k in tables}
    connection=snapshot.get("connection",{})
    semantic_product="oracle" if product=="oracle" or connection.get("compatibility_mode")=="oracle" else product
    report={"builder_version":1,"wren_core_version":WREN_VERSION,"schema_commit":SCHEMA_COMMIT,
            "issues":issues,"pending_relationships":[],
            "wren_target_execution":"unverified","schema_validation":"pending","wren_build":"pending"}
    if snapshot.get("scan_stale"):issues.append({"code":"stale_snapshot","message":"驱动配置已变化，请重新扫描并核对本体映射"})
    detected=connection.get("detected_compatibility_mode")
    if detected and detected!="unknown" and detected!=connection.get("compatibility_mode"):
        issues.append({"code":"compatibility_mode_mismatch"})
    if len(tables)!=len(snapshot.get("tables",[])):issues.append({"code":"duplicate_table_identity"})
    if not tables:issues.append({"code":"no_snapshot","message":"请先扫描结构"})
    if snapshot.get("permission_gaps"):issues.append({"code":"metadata_incomplete","gaps":snapshot["permission_gaps"]})
    for key,t in sorted(tables.items()):
        report.setdefault("source_constraints",[]).append({"table":key,**{k:t.get(k,[]) for k in ("primary_keys","unique_constraints","foreign_keys")}})
        for constraint in t.get("unique_constraints",[]):
            if any(constraint.get(k) for k in ("condition","predicate","expression","expressions")):
                issues.append({"code":"conditional_unique_constraint","table":key,"constraint":constraint})
        columns=[]
        for c in t["columns"]:
            dtype=column_type(c,snapshot.get("type_mapping"),semantic_product)
            raw=c.get("raw_type",c["type"]).lower().strip()
            if raw in snapshot.get("type_mapping",{}):
                report.setdefault("type_mappings_applied",[]).append({"table":key,"column":c["name"],"raw_type":raw,"mapped_type":dtype})
            if not dtype:
                issues.append({"code":"unsupported_type","table":key,"column":c["name"],"raw_type":c.get("raw_type",c["type"])})
                continue
            columns.append({"name":c["name"],"type":dtype,"notNull":c.get("nullable") is False,"properties":{"description":c.get("user_description",c.get("description","")) or ""}})
        model={"name":names[key],"tableReference":{k:t[k] for k in ("catalog","schema") if t.get(k)}|{"table":t["name"]},
               "columns":columns,"properties":{"description":t.get("user_description",t.get("description","")) or ""}}
        keys=t.get("primary_keys",[])
        if keys:
            pk=keys[0]["columns"]
            if pk: model["primaryKey"] = pk[0] if len(pk)==1 else pk
        unique_keys=sorted([u["columns"] for u in t.get("unique_constraints",[]) if u.get("columns")])
        if unique_keys: model["uniqueKeys"] = [k[0] if len(k)==1 else k for k in unique_keys]
        models.append(model)
    selected=list(snapshot.get("foreign_keys",[]))+[r for r in snapshot.get("relationship_candidates",[]) if r.get("status")=="accepted"]+snapshot.get("manual_relationships",[])
    seen=set();model_by_name={m["name"]:m for m in models}
    for r in sorted(selected,key=relationship_id):
        if "source_table" not in r:
            issues.append({"code":"ungrouped_foreign_key","relation":r});continue
        if r.get("condition"):
            issues.append({"code":"custom_relationship_condition_requires_mapping","condition":r["condition"]});continue
        rid=relationship_id(r)
        if rid in seen:continue
        seen.add(rid)
        src,dst=r["source_table"],r["target_table"];left,right=r["columns"],r["target_columns"]
        if src not in tables or dst not in tables:
            issues.append({"code":"relationship_table_missing","relation":rid});continue
        if not left or len(left)!=len(right) or any(c not in {x["name"] for x in tables[src]["columns"]} for c in left) or any(c not in {x["name"] for x in tables[dst]["columns"]} for c in right):
            issues.append({"code":"relationship_columns_invalid","relation":rid});continue
        target_keys=tables[dst].get("primary_keys",[])+tables[dst].get("unique_constraints",[])
        if not any(k["columns"]==right for k in target_keys):
            issues.append({"code":"relationship_target_not_unique","relation":rid});continue
        if src==dst:
            issues.append({"code":"self_relationship_needs_alias","relation":rid});continue
        condition=" AND ".join(f"{quoted(names[src])}.{quoted(a)} = {quoted(names[dst])}.{quoted(b)}" for a,b in zip(left,right))
        relationships.append({"name":"r_"+rid,"models":[names[src],names[dst]],"joinType":"MANY_TO_ONE","condition":condition})
        for origin,target_model,suffix in [(names[src],names[dst],"to"),(names[dst],names[src],"from")]:
            nav="rel_"+rid+"_"+suffix
            existing={c["name"] for c in model_by_name[origin]["columns"]}
            while nav in existing:nav+="_nav"
            model_by_name[origin]["columns"].append({"name":nav,"type":target_model,"relationship":"r_"+rid})
    mdl={"catalog":"semora","schema":"semantic","models":models,"relationships":sorted(relationships,key=lambda r:r["name"]),"views":[]}
    target={"postgresql":"POSTGRES","vastbase":"POSTGRES","mysql":"MYSQL","goldendb":"MYSQL","oracle":"ORACLE","sqlserver":"MSSQL"}.get(product)
    if target:mdl["dataSource"]=target
    else:report["connector_status"]="no_native_connector_asserted"
    report["source_product"]=product
    if issues:
        report["status"]="blocked"
        return None,report
    try:
        from jsonschema import Draft202012Validator
        schema=json.loads((Path(__file__).parent/"vendor"/"wren-mdl.schema.json").read_text(encoding="utf-8"))
        errors=sorted(Draft202012Validator(schema).iter_errors(mdl),key=lambda e:str(e.path))
        if errors:
            report["schema_validation"]="failed";issues.extend({"code":"mdl_schema","path":list(e.path),"message":e.message} for e in errors)
        else:report["schema_validation"]="passed"
    except ImportError:report["schema_validation"]="unavailable"
    if not VALIDATION_SLOTS.acquire(blocking=False):
        report["wren_build"]="unavailable"
        report["wren_validation_reason"]="validation_busy"
    else:
        try:
            env={**os.environ,"PYTHONPATH":os.pathsep.join(sys.path),"TOKIO_WORKER_THREADS":"2","RAYON_NUM_THREADS":"2"}
            result=subprocess.run([sys.executable,"-c","from app.wren_validation_worker import main; main()"],
                input=json.dumps({"mdl":mdl,"version":WREN_VERSION}).encode(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                timeout=25,env=env)
            if result.returncode or len(result.stdout)>65536:raise ValueError("worker_exited")
            validation=json.loads(result.stdout)
            report["wren_build"]=validation["status"]
            if validation.get("reason"):report["wren_validation_reason"]=validation["reason"]
            if validation["status"]=="failed":issues.append({"code":"wren_build","message":validation.get("message", "Wren build failed")})
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            report["wren_build"]="unavailable"
            report["wren_validation_reason"]=type(exc).__name__
        finally:VALIDATION_SLOTS.release()
    report["status"]="passed" if report["schema_validation"]==report["wren_build"]=="passed" else "partial" if not issues else "blocked"
    report["mdl_sha256"]=hashlib.sha256(json.dumps(mdl,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    return (None if issues else mdl),report
