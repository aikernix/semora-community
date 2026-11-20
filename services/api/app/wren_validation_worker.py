"""A bounded, disposable Wren validation process; never receives connection config."""
import base64
import importlib.metadata
import json
import sys

def main():
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(2*1024**3,2*1024**3))
        except ImportError:pass
        request=json.load(sys.stdin)
        if importlib.metadata.version("wren-core-py")!=request["version"]:
            raise ImportError("pinned Wren version unavailable")
        import wren_core
        mdl=request["mdl"]
        def q(s):return '"'+s.replace('"','""')+'"'
        models={m["name"]:m for m in mdl["models"]}
        probes=[]
        # Wren exposes navigation in calculated MDL expressions. Ordinary SQL
        # uses flat projected fields; it need not accept raw three-part paths.
        for index,relation in enumerate(mdl["relationships"]):
            left,right=relation["models"]
            nav=next(c for c in models[left]["columns"] if c.get("relationship")==relation["name"])
            physical=next(c for c in models[right]["columns"] if not c.get("relationship"))
            name=f"__semora_validation_{index}"
            while any(c["name"]==name for c in models[left]["columns"]):name+="_"
            models[left]["columns"].append({"name":name,"type":physical["type"],"isCalculated":True,
                "expression":f"{q(nav['name'])}.{q(physical['name'])}"})
            probes.append((left,name))
        context=wren_core.SessionContext(base64.b64encode(json.dumps(mdl).encode()).decode())
        for model in models:
            context.transform_sql(f"SELECT * FROM {q(model)} LIMIT 0")
        for relation in mdl["relationships"]:
            left,right=relation["models"]
            context.transform_sql(f"SELECT * FROM {q(left)} JOIN {q(right)} ON {relation['condition']} LIMIT 0")
        for model,column in probes:
            context.transform_sql(f"SELECT {q(column)} FROM {q(model)} LIMIT 0")
        result={"status":"passed"}
    except (ImportError,importlib.metadata.PackageNotFoundError):result={"status":"unavailable","reason":"pinned_runtime_unavailable"}
    except BaseException as exc:result={"status":"failed","message":str(exc)[:2000]}
    print(json.dumps(result))
