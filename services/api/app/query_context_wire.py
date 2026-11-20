"""Lossless model-input encoding; authoritative ontology and scans stay unchanged."""
import json
from copy import deepcopy

FORMAT = "query-material-tables-v2"
GUIDE = (
    "data是完整查询资料。$columns列出列定义；$rows每行首项是列定义索引，"
    "后续值依次对应列定义；$literal表示原始字典。"
    "字段的scan是该物理列的扫描信息；scan_shared列出的扫描属性与本体属性相同。"
    "schemas.columns的property_scan_ref引用本体字段，order保留物理列顺序。"
    "shared_labels列出[对象序号,字段序号]，这些字段的label与description相同。"
    "properties中的shared_properties_ref引用shared_properties中完全相同的字段定义，各表归属独立保留。"
    "这些是格式引用；字段名、类型、注释、枚举及约束仍具有原含义。"
)


def _pack(value):
    if isinstance(value, list):
        if len(value) >= 4 and all(isinstance(row, dict) for row in value):
            headers, rows = [], []
            for row in value:
                keys = list(row)
                if keys not in headers:
                    headers.append(keys)
                rows.append([headers.index(keys), *[_pack(row[k]) for k in keys]])
            return {"$columns": headers, "$rows": rows}
        return [_pack(v) for v in value]
    if isinstance(value, dict):
        packed = {k: _pack(v) for k, v in value.items()}
        if set(value) in ({"$columns", "$rows"}, {"$literal"}):
            return {"$literal": packed}
        return packed
    return value


def _unpack(value):
    if isinstance(value, dict):
        if set(value) == {"$literal"}:
            return {k: _unpack(v) for k, v in value["$literal"].items()}
        if set(value) == {"$columns", "$rows"}:
            return [
                {k: _unpack(v) for k, v in zip(value["$columns"][row[0]], row[1:], strict=True)}
                for row in value["$rows"]
            ]
        return {k: _unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    return value


def encode_material(packet):
    data = deepcopy(packet)
    objects = {o["object"].get("name", o["object"].get("table")): o["object"] for o in data.get("objects", [])}
    keys = [o["object"].get("name", o["object"].get("table")) for o in data.get("objects", [])]
    objects = {k: v for k, v in objects.items() if keys.count(k) == 1}
    merged_schemas, shared_labels = [], []
    for schema_index, schema in enumerate(data.get("schemas", [])):
        obj = objects.get(schema.get("object"), {})
        props = obj.get("properties", [])
        tables = schema.get("tables", [])
        # Ambiguous physical projections retain their original representation.
        if len(tables) != 1 or any("scan" in p or "scan_shared" in p for p in props):
            continue
        columns = tables[0].get("columns")
        if not isinstance(columns, list):
            continue
        by_name = {p.get("column", p.get("name")): p for p in props}
        if len(by_name) != len(props) or any(c.get("name") not in by_name for c in columns):
            continue
        if len({c.get("name") for c in columns}) != len(columns):
            continue
        for col in columns:
            prop = by_name[col["name"]]
            shared = [k for k in col if k != "name" and k in prop and prop[k] == col[k]]
            prop["scan"] = {k: v for k, v in col.items() if k != "name" and k not in shared}
            prop["scan_shared"] = shared
        tables[0]["columns"] = {
            "property_scan_ref": schema["object"], "order": [c["name"] for c in columns]
        }
        merged_schemas.append(schema_index)
    for object_index, item in enumerate(data.get("objects", [])):
        for prop_index, prop in enumerate(item["object"].get("properties", [])):
            if isinstance(prop.get("label"), str) and prop["label"] == prop.get("description"):
                prop.pop("label")
                shared_labels.append([object_index, prop_index])
    repeated={}
    for item in data.get('objects',[]):
        props=item['object'].get('properties')
        if not isinstance(props,list):continue
        signature=json.dumps(props,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
        if len(signature.encode())>512:repeated.setdefault(signature,[]).append(item['object'])
    shared_properties={}
    for signature,objects_with_props in repeated.items():
        if len(objects_with_props)<2:continue
        name='fields_'+str(len(shared_properties)+1)
        shared_properties[name]=_pack(objects_with_props[0]['properties'])
        for obj in objects_with_props:obj['properties']={'shared_properties_ref':name}
    return {
        "format": FORMAT, "guide": GUIDE, "shared_labels": shared_labels,
        "merged_schemas": merged_schemas, "data": _pack(data),
        **({'shared_properties':shared_properties} if shared_properties else {}),
    }


def decode_material(wire):
    if wire.get("format") not in {FORMAT,'query-material-tables-v1'}:
        raise ValueError("unknown_query_material_format")
    data = _unpack(wire["data"])
    for item in data.get('objects',[]):
        props=item['object'].get('properties')
        if isinstance(props,dict) and set(props)=={'shared_properties_ref'}:
            item['object']['properties']=deepcopy(_unpack(wire['shared_properties'][props['shared_properties_ref']]))
    objects = {o["object"].get("name", o["object"].get("table")): o["object"] for o in data.get("objects", [])}
    for schema_index in wire["merged_schemas"]:
        table = data["schemas"][schema_index]["tables"][0]
        ref = table["columns"]
        props = objects[ref["property_scan_ref"]]["properties"]
        lookup = {p.get("column", p.get("name")): p for p in props}
        table["columns"] = [
            {"name": name, **lookup[name]["scan"],
             **{k: lookup[name][k] for k in lookup[name]["scan_shared"]}}
            for name in ref["order"]
        ]
        for prop in props:
            prop.pop("scan", None)
            prop.pop("scan_shared", None)
    for object_index, prop_index in wire["shared_labels"]:
        prop = data["objects"][object_index]["object"]["properties"][prop_index]
        prop["label"] = prop["description"]
    return data


def model_material(packet):
    """Keep small packets in their familiar form when encoding would add overhead."""
    wire = encode_material(packet)
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode())
    return wire if size(wire) < size(packet) else packet


def encode_payload(payload):
    """Encode only at the model boundary; internal SQL guards keep raw materials."""
    if not isinstance(payload.get("materials"), dict):
        return payload
    return {**payload, "materials": model_material(payload["materials"])}
