from app.query_relationships import entity_references, validated_guards
from app.relationship_evidence import finalize_relationships
from app.datasource_modeling import ModelingRepository, package_objects, package_relations


def verdict(outcome, code=""):
    return {"supported": outcome == "usable", "confidence": .98, "target_role": "unknown",
            "alternative_unresolved": False, "query_outcome": outcome, "exclusion_code": code,
            "missing_evidence": ["actual identifier meaning"] if outcome == "needs_evidence" else [],
            "reason": "field descriptions prove mismatch"}


def test_clear_mismatch_is_excluded_but_missing_evidence_is_pending():
    for outcome, expected in (("exclude", "rejected"), ("needs_evidence", "unresolved")):
        c = {"evidence_grade": "medium", "evidence": [
            {"kind": "model_semantic_review", **verdict(outcome, "meaning_mismatch")},
            {"kind": "independent_review", **verdict(outcome, "meaning_mismatch")}], "status": "pending"}
        finalize_relationships([c])
        assert c["status"] == expected


def test_one_model_rejection_does_not_automatically_exclude():
    c = {"evidence_grade": "medium", "evidence": [
        {"kind": "model_semantic_review", **verdict("exclude", "wrong_target")},
        {"kind": "independent_review", **verdict("needs_evidence")}], "status": "pending"}
    finalize_relationships([c])
    assert c["status"] == "unresolved"


def test_reference_routing_selects_entity_anchor_not_sibling_extension():
    def table(name, description):
        return {"id": name, "name": name, "description": description,
                "primary_keys": [{"columns": ["k"]}], "columns": [{"name": "k", "description": "归属实体编号"}]}
    tables = [table("alpha", "实体资料"), table("beta", "扩展资料"), table("gamma", "发生记录")]
    profiles = {"alpha": {"role": "entity_master", "entity": "归属实体"},
                "beta": {"role": "extension"},
                "gamma": {"role": "detail", "field_roles": {"k": "reference"}}}
    refs = list(entity_references(tables, profiles))
    assert [(s["id"], t["id"]) for s, _, t, _ in refs] == [("gamma", "alpha")]


def test_guard_contract_rejects_hallucinated_values_and_compiles_known_literal():
    tables = {"ds": {"a": {"id": "a", "name": "alpha", "columns": [
        {"name": "key", "type": "varchar"}, {"name": "kind", "type": "varchar", "description": "对象类型 'entity.alpha'"}]},
        "b": {"id": "b", "name": "beta", "columns": [{"name": "key", "type": "varchar"}]}}}
    c = {"id": "c", "source_datasource_id": "ds", "target_datasource_id": "ds",
         "source_table_id": "a", "target_table_id": "b", "source_columns": ["key"], "target_columns": ["key"],
         "status": "accepted", "cardinality": "N:1", "evidence_grade": "strong", "evidence": [],
         "key_pairs": [{"source": "key", "target": "key"}]}
    guards = [{"side": "source", "field": "kind", "operator": "eq", "value": "entity.alpha"}]
    assert validated_guards(c, guards, tables) == guards
    assert validated_guards(c, [{**guards[0], "value": "invented"}], tables) is None
    c["condition_template"] = {"guards": guards}
    _, mapping = package_objects("ds", list(tables["ds"].values()))
    relation = package_relations([c], mapping)[0]
    assert "AND" in relation["condition"] and "'entity.alpha'" in relation["condition"]


def test_contextual_reference_does_not_depend_on_field_or_industry_names():
    from app.query_relationships import contextual_references
    source = {"id": "events", "name": "events", "columns": [
        {"name": "opaque_ref", "description": "对象标识"},
        {"name": "category", "description": "类型 namespace.alpha, value='namespace.alpha'"}]}
    target = {"id": "alpha", "name": "alpha", "primary_keys": [{"columns": ["pk"]}],
              "columns": [{"name": "pk", "description": "实体主键"}]}
    profiles = {"events": {"field_roles": {"opaque_ref": "reference", "category": "attribute"}}}
    matches = list(contextual_references([source, target], profiles))
    assert len(matches) == 1
    assert matches[0][1]["name"] == "opaque_ref" and matches[0][3]["name"] == "pk"


def test_reverse_duplicates_are_excluded_but_distinct_roles_are_kept():
    from app.query_relationships import deduplicate_query_edges
    base = {"status": "accepted", "evidence_grade": "strong", "evidence": [], "cardinality": "N:1",
            "source_datasource_id": "ds", "target_datasource_id": "ds", "condition_template": {}}
    forward = {**base, "id": "a", "source_table_id": "events", "target_table_id": "entities",
               "key_pairs": [{"source": "owner", "target": "pk"}]}
    reverse = {**base, "id": "b", "source_table_id": "entities", "target_table_id": "events",
               "evidence": [], "key_pairs": [{"source": "pk", "target": "owner"}]}
    other_role = {**base, "id": "c", "source_table_id": "events", "target_table_id": "entities",
                  "evidence": [], "key_pairs": [{"source": "creator", "target": "pk"}]}
    deduplicate_query_edges([forward, reverse, other_role])
    assert forward["status"] == other_role["status"] == "accepted"
    assert reverse["status"] == "rejected"


def test_model_label_does_not_duplicate_a_declared_foreign_key():
    from app.query_relationships import deduplicate_query_edges
    base = {'status': 'accepted', 'source_datasource_id': 'ds', 'target_datasource_id': 'ds',
            'source_table_id': 'records', 'target_table_id': 'entities',
            'condition_template': {}, 'key_pairs': [{'source': 'entity_id', 'target': 'id'}]}
    declared = {**base, 'id': 'fk', 'evidence_grade': 'foreign_key', 'evidence': []}
    proposed = {**base, 'id': 'model', 'evidence_grade': 'strong', 'evidence': [], 'query_role': 'entity_reference'}
    deduplicate_query_edges([proposed, declared])
    assert declared['status'] == 'accepted'
    assert proposed['status'] == 'rejected'
    assert proposed['evidence'][-1]['canonical_candidate_id'] == 'fk'
