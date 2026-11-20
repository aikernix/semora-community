from semora_contracts import LogicalQueryPlan, SQLProgram, SQLProgramStep

from app.query_alignment import inspect_alignment, validate_logical_refs
from app.query_alignment import validate_required_object_gaps
from semora_contracts import QueryUnderstanding
import pytest


def gap_report(required=("organization", "loan"), **gap_changes):
    gap = {
        "kind": "relation", "summary": "Required information cannot be connected",
        "required_for": "join", "objects": ["organization", "loan"],
        "field_refs": ["organization.name", "loan.customer_id"],
        "evidence_refs": ["ontology", "request"], "resolution": "Configure the required mapping",
        **gap_changes,
    }
    return QueryUnderstanding(
        summary="Summarize records by their owning organization",
        target_state="known",
        required_object_uses=[
            {"object": name, "requirement": "Requested grouping or measure",
             "reason": "Provides information unavailable from the other required object"}
            for name in required
        ],
        technical_gaps=[gap],
    )


def test_single_object_query_does_not_require_relations_between_candidates():
    report = gap_report(required=("loan",))
    report.technical_gaps = []
    assert validate_required_object_gaps(report, materials()) == []


def test_candidate_relation_cannot_block_single_object_query():
    errors = validate_required_object_gaps(gap_report(required=("loan",)), materials())
    assert any("未证明必要性" in error and "organization" in error for error in errors)


def test_genuine_required_relation_gap_is_preserved():
    packet = materials()
    packet["relations"] = []
    report = gap_report()
    assert validate_required_object_gaps(report, packet) == []
    assert len(report.technical_gaps) == 1


@pytest.mark.parametrize("changes", [
    {"objects": ["loan"]},
    {"objects": ["loan", "loan"]},
    {"field_refs": ["loan.unknown"]},
    {"field_refs": ["other.id"]},
])
def test_relation_gap_requires_distinct_endpoints_and_verified_fields(changes):
    assert validate_required_object_gaps(gap_report(**changes), materials())


def test_unknown_required_object_cannot_supply_necessity_evidence():
    assert validate_required_object_gaps(gap_report(required=("loan", "unknown")), materials())


def test_historical_understanding_without_object_uses_remains_readable():
    report = QueryUnderstanding.model_validate({"summary": "Historical query", "target_state": "known"})
    assert report.required_object_uses == []
    assert validate_required_object_gaps(report, materials()) == []


def materials():
    return {
        "objects": [
            {"object": {"name": "organization", "table": "org", "properties": [
                {"name": "name", "column": "org_name"},
            ]}},
            {"object": {"name": "loan", "table": "loan", "properties": [
                {"name": "customer_id", "column": "customer_id"},
                {"name": "balance", "column": "balance"},
                {"name": "status", "column": "status"},
            ]}},
        ],
        "relations": [{"name": "organization_has_loan", "from": "organization", "to": "loan"}],
    }


def plan():
    return LogicalQueryPlan(
        subject_refs=["loan"],
        output_refs=["organization.name"],
        dimension_refs=["organization.name"],
        metrics=[{"id": "balance_total", "label": "余额合计", "operation": "sum", "field_refs": ["loan.balance"]}],
        filters=[{"field_refs": ["loan.status"], "operator": "eq", "value": "active", "description": "只包含有效记录"}],
        relation_refs=["organization_has_loan"],
        sort=[{"target_ref": "balance_total", "direction": "desc"}],
        limit=10,
    )


def program(sql):
    return SQLProgram(summary="按机构统计余额", steps=[SQLProgramStep(
        name="q", purpose="按机构统计余额", data_source_id="source", sql=sql,
        output_columns=["org_name", "balance_total"],
    )])


def test_logical_plan_references_are_checked_against_published_materials():
    assert validate_logical_refs(plan(), materials()) == []
    broken = plan().model_copy(deep=True)
    broken.output_refs = ["loan.missing"]
    assert validate_logical_refs(broken, materials()) == ["未知属性引用：loan.missing"]


def test_ast_alignment_accepts_matching_query():
    sql = """SELECT o.org_name, SUM(l.balance) AS balance_total
             FROM org o JOIN loan l ON o.id=l.org_id
             WHERE l.status=:status GROUP BY o.org_name
             ORDER BY balance_total DESC LIMIT 10"""
    assert inspect_alignment(plan(), program(sql), materials()) == []


def test_ast_alignment_reports_only_concrete_missing_operations():
    sql = "SELECT o.org_name, l.balance FROM org o JOIN loan l ON o.id=l.org_id"
    kinds = {item["kind"] for item in inspect_alignment(plan(), program(sql), materials())}
    assert {"missing_group", "missing_metric", "missing_filter", "missing_sort", "missing_limit"} <= kinds
