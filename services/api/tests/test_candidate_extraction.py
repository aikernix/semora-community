import asyncio

import pytest

from app.candidate_extraction import CandidateExtractionError, OntologyCandidateExtractor


ONTOLOGY = {
    "objects": [
        {
            "name": "loan",
            "properties": [
                {"name": "outstanding_balance", "column": "outstanding_balance", "type": "decimal"}
            ],
        },
        {"name": "branch", "properties": [{"name": "name", "column": "name", "type": "string"}]},
    ],
    "relations": [],
    "metrics": [
        {
            "name": "loan_balance",
            "label": "贷款余额",
            "expression": "SUM(loan.outstanding_balance)",
            "grain": "branch",
            "dimensions": ["branch"],
            "format": "currency",
        }
    ],
}

BUNDLE = {
    "document": {
        "id": "doc-1",
        "knowledge_base_id": "kb-1",
        "title": "指标口径手册",
        "content_hash": "abcdef1234567890",
    },
    "chunks": [
        {
            "id": "chunk-1",
            "locator": "全文 / 分块 1",
            "content": "# 指标口径手册\n## 贷款余额\n贷款余额取有效贷款 outstanding_balance 之和。\n## 客户数歧义\n客户数可能指授信客户数或贷款客户数，必须澄清。",
        }
    ],
}


class BadOptionalModel:
    configured = True
    required = False

    async def complete_json(self, _system, _user):
        return {
            "candidates": [
                {
                    "term": "虚构指标",
                    "proposal": "虚构定义",
                    "kind": "metric",
                    "patch": {
                        "name": "fake",
                        "label": "虚构指标",
                        "expression": "SUM(missing.value)",
                        "grain": "branch",
                    },
                    "evidence_chunk_id": "chunk-1",
                }
            ]
        }


class BadRequiredModel(BadOptionalModel):
    required = True


def test_deterministic_extraction_binds_metric_and_evidence():
    class DisabledModel:
        configured = False
        required = False

    values, mode = asyncio.run(OntologyCandidateExtractor(DisabledModel()).extract(BUNDLE, ONTOLOGY, "retail_credit"))
    assert mode == "deterministic"
    assert [item["term"] for item in values] == ["贷款余额", "客户数歧义"]
    assert values[0]["kind"] == "metric"
    assert values[0]["patch"]["expression"] == "SUM(loan.outstanding_balance)"
    assert values[0]["evidence"]["chunk_id"] == "chunk-1"
    assert values[1]["kind"] == "definition"


def test_optional_model_cannot_introduce_unknown_ontology_reference():
    values, mode = asyncio.run(OntologyCandidateExtractor(BadOptionalModel()).extract(BUNDLE, ONTOLOGY, "retail_credit"))
    assert mode == "deterministic"
    assert all(item["term"] != "虚构指标" for item in values)


def test_required_model_fails_when_all_candidates_are_unsafe():
    with pytest.raises(CandidateExtractionError):
        asyncio.run(OntologyCandidateExtractor(BadRequiredModel()).extract(BUNDLE, ONTOLOGY, "retail_credit"))
