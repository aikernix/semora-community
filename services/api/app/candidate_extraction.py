from __future__ import annotations
from semora_agent.prompts import ONTOLOGY_EXTRACTION, EVIDENCE
from .model_outputs import CandidateBatch, CandidateProposal

import json
import re
from typing import Any

from semora_agent.model import ModelError, StructuredModel, structured_model


ALLOWED_KINDS = {"definition", "metric", "object", "property", "relation"}


class CandidateExtractionError(RuntimeError):
    pass


class OntologyCandidateExtractor:
    """Extract reviewable ontology proposals without ever publishing them directly."""

    def __init__(self, model: StructuredModel | None = None) -> None:
        self.model = model or structured_model

    async def extract(
        self,
        bundle: dict[str, Any],
        ontology: dict[str, Any],
        domain: str,
    ) -> tuple[list[dict[str, Any]], str]:
        document = bundle.get("document") or {}
        chunks = [item for item in bundle.get("chunks", []) if isinstance(item, dict)]
        if not chunks:
            raise CandidateExtractionError("文档没有可抽取的分块")

        if self.model.configured:
            try:
                proposals = await self._extract_with_model(document, chunks, ontology, domain)
                if proposals:
                    return proposals, "llm"
                if self.model.required:
                    raise CandidateExtractionError("模型未返回有效的本体候选")
            except (ModelError, CandidateExtractionError):
                if self.model.required:
                    raise

        return self._extract_deterministically(document, chunks, ontology, domain), "deterministic"

    async def _extract_with_model(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        ontology: dict[str, Any],
        domain: str,
    ) -> list[dict[str, Any]]:
        source_chunks = [
            {
                "id": str(item.get("id", "")),
                "locator": item.get("locator", ""),
                "content": str(item.get("content", ""))[:5000],
            }
            for item in chunks[:30]
        ]
        system=ONTOLOGY_EXTRACTION+'\n'+EVIDENCE
        user = json.dumps(
            {
                "domain": domain,
                "output_contract": CandidateBatch.model_json_schema(),
                "document": {
                    "id": str(document.get("id", "")),
                    "title": document.get("title", ""),
                    "revision": str(document.get("content_hash", ""))[:12],
                },
                "published_shape": {
                    "objects": ontology.get("objects", []),
                    "relations": ontology.get("relations", []),
                    "metrics": ontology.get("metrics", []),
                },
                "chunks": source_chunks,
            },
            ensure_ascii=False,
        )
        value = await self.model.complete_json(system, user)
        raw_candidates = value.get("candidates")
        if not isinstance(raw_candidates, list):
            raise CandidateExtractionError("模型候选格式错误")
        chunk_map = {str(item.get("id", "")): item for item in chunks}
        proposals: list[dict[str, Any]] = []
        for raw in raw_candidates[:20]:
            if not isinstance(raw, dict):
                continue
            try:
                raw=CandidateProposal.model_validate(raw).model_dump()
            except ValueError:
                continue
            chunk = chunk_map.get(str(raw.get("evidence_chunk_id", "")))
            if chunk is None:
                continue
            proposal = self._validated_proposal(raw, document, chunk, ontology, domain)
            if proposal is not None:
                proposals.append(proposal)
        return self._deduplicate(proposals)

    def _extract_deterministically(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        ontology: dict[str, Any],
        domain: str,
    ) -> list[dict[str, Any]]:
        metrics_by_label = {
            str(item.get("label", "")).strip(): item
            for item in ontology.get("metrics", [])
            if isinstance(item, dict) and item.get("label")
        }
        proposals: list[dict[str, Any]] = []
        for chunk in chunks:
            content = str(chunk.get("content", ""))
            sections = self._sections(content)
            for heading, body in sections:
                term = heading.strip().strip("：:")
                body = body.strip()
                if not term or not body or len(body) < 8:
                    continue
                metric = metrics_by_label.get(term)
                if metric is not None:
                    raw = {
                        "term": term,
                        "proposal": body[:1200],
                        "kind": "metric",
                        "patch": {
                            key: metric.get(key)
                            for key in ("name", "label", "expression", "grain", "dimensions", "format")
                        },
                    }
                elif self._looks_governance_relevant(term, body):
                    raw = {"term": term, "proposal": body[:1200], "kind": "definition", "patch": {}}
                else:
                    continue
                proposal = self._validated_proposal(raw, document, chunk, ontology, domain)
                if proposal is not None:
                    proposals.append(proposal)
        return self._deduplicate(proposals)

    @staticmethod
    def _sections(content: str) -> list[tuple[str, str]]:
        matches = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", content))
        if matches:
            result = []
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
                result.append((match.group(1), content[match.end():end]))
            return result
        result = []
        for line in content.splitlines():
            if "=" in line or "定义为" in line or "是指" in line:
                term = re.split(r"=|定义为|是指", line, maxsplit=1)[0].strip(" ：:")
                if term:
                    result.append((term, line))
        return result

    @staticmethod
    def _looks_governance_relevant(term: str, body: str) -> bool:
        text = term + body
        return any(
            marker in text
            for marker in ("口径", "定义", "指标", "率", "余额", "金额", "客户数", "歧义", "敏感", "审批", "脱敏", "机构范围")
        )

    def _validated_proposal(
        self,
        raw: dict[str, Any],
        document: dict[str, Any],
        chunk: dict[str, Any],
        ontology: dict[str, Any],
        domain: str,
    ) -> dict[str, Any] | None:
        term = str(raw.get("term", "")).strip()[:200]
        proposal = str(raw.get("proposal", "")).strip()[:4000]
        kind = str(raw.get("kind", "definition")).strip()
        patch = raw.get("patch")
        if not term or not proposal or kind not in ALLOWED_KINDS or not isinstance(patch, dict):
            return None
        if not self._patch_is_safe(kind, patch, ontology):
            return None
        revision = str(document.get("content_hash", ""))[:12] or "current"
        title = str(document.get("title") or document.get("file_name") or "知识文档")
        locator = str(chunk.get("locator") or f"分块 {chunk.get('chunk_index', 0)}")
        return {
            "domain": domain,
            "term": term,
            "source": f"{title}@{revision} / {locator}",
            "proposal": proposal,
            "kind": kind,
            "patch": patch,
            "evidence": {
                "knowledge_base_id": str(document.get("knowledge_base_id", "")),
                "document_id": str(document.get("id", "")),
                "chunk_id": str(chunk.get("id", "")),
                "locator": locator,
                "revision": revision,
                "excerpt": str(chunk.get("content", ""))[:800],
            },
        }

    @staticmethod
    def _patch_is_safe(kind: str, patch: dict[str, Any], ontology: dict[str, Any]) -> bool:
        if kind == "definition":
            return patch == {}
        objects = {str(item.get("name")): item for item in ontology.get("objects", []) if isinstance(item, dict)}
        if kind == "metric":
            required = {"name", "label", "expression", "grain"}
            if not required.issubset(patch) or str(patch.get("grain")) not in objects:
                return False
            expression = str(patch.get("expression", ""))
            if not expression or any(token in expression for token in (";", "--", "/*", "*/")):
                return False
            references = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b", expression)
            for object_name, property_name in references:
                object_value = objects.get(object_name)
                if object_value is None:
                    return False
                properties = {str(item.get("name")) for item in object_value.get("properties", [])}
                if property_name not in properties:
                    return False
            return bool(references)
        if kind == "object":
            return all(str(patch.get(key, "")).strip() for key in ("name", "label", "table", "primaryKey"))
        if kind == "relation":
            return (
                all(str(patch.get(key, "")).strip() for key in ("name", "from", "to", "condition", "cardinality"))
                and patch.get("from") in objects
                and patch.get("to") in objects
            )
        if kind == "property":
            value = patch.get("property")
            return (
                patch.get("object") in objects
                and isinstance(value, dict)
                and all(str(value.get(key, "")).strip() for key in ("name", "column", "type"))
            )
        return False

    @staticmethod
    def _deduplicate(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for value in values:
            key = (value["term"], value["kind"], value["source"])
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result


candidate_extractor = OntologyCandidateExtractor()
