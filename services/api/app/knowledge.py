from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from semora_contracts import EvidenceRef, KnowledgeResearchResult, KnowledgeResearchStep


class KnowledgeCoreError(RuntimeError):
    pass


class WeKnoraClient:
    provider = "weknora"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("WEKNORA_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("WEKNORA_API_KEY", "")
        self._transport = transport

    def _ensure_configured(self) -> None:
        if not self.base_url or not self.api_key:
            raise KnowledgeCoreError("WeKnora 未配置：请在知识库的连接配置中填写服务地址和 API Key")

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise KnowledgeCoreError("WeKnora 返回了无效响应")
        if payload.get("success") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code")
            else:
                message = payload.get("message") or error
            raise KnowledgeCoreError(f"WeKnora 请求失败：{message or 'unknown error'}")
        return payload.get("data") if "data" in payload else payload

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        self._ensure_configured()
        headers = dict(kwargs.pop("headers", {}))
        headers.update({"X-API-Key": self.api_key, "Accept": "application/json"})
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}/{endpoint.lstrip('/')}",
                    headers=headers,
                    **kwargs,
                )
                response.raise_for_status()
                payload = response.json()
                return self._unwrap(payload), payload
        except KnowledgeCoreError:
            raise
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("code") or "")
                elif isinstance(body, dict):
                    detail = str(body.get("message") or "")
            except ValueError:
                pass
            raise KnowledgeCoreError(f"WeKnora HTTP {exc.response.status_code} {detail}".strip()) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeCoreError(f"WeKnora 请求失败：{type(exc).__name__}") from exc

    async def status(self) -> dict[str, Any]:
        rows = await self.list_knowledge_bases()
        return {
            "provider": self.provider,
            "status": "ready",
            "knowledge_bases": len(rows),
            "instance_url": self.base_url.removesuffix("/api/v1"),
            "agent_id": "semora-workbench-knowledge-retrieval",
            "api_key_configured": bool(self.api_key),
        }

    async def list_knowledge_bases(self) -> list[dict[str, Any]]:
        data, _ = await self._request("GET", "/knowledge-bases")
        rows = data if isinstance(data, list) else []
        return [
            {
                "id": str(row.get("id", "")),
                "name": row.get("name") or "未命名知识库",
                "description": row.get("description") or "",
                "documents": row.get("knowledge_count", 0),
                "chunks": row.get("chunk_count", 0),
                "status": "processing" if row.get("is_processing") else "ready",
                "provider": self.provider,
            }
            for row in rows
            if isinstance(row, dict)
        ]

    async def create_knowledge_base(self, name: str, description: str) -> dict[str, Any]:
        data, _ = await self._request(
            "POST", "/knowledge-bases", json={"name": name, "description": description, "type": "document"}
        )
        return data if isinstance(data, dict) else {}

    async def list_knowledge(self, kb_id: str, page: int, page_size: int) -> dict[str, Any]:
        data, envelope = await self._request(
            "GET",
            f"/knowledge-bases/{kb_id}/knowledge",
            params={"page": page, "page_size": page_size},
        )
        return {
            "documents": data if isinstance(data, list) else [],
            "total": envelope.get("total", len(data) if isinstance(data, list) else 0),
            "page": envelope.get("page", page),
            "page_size": envelope.get("page_size", page_size),
        }

    async def get_knowledge(self, document_id: str) -> dict[str, Any]:
        data, _ = await self._request("GET", f"/knowledge/{document_id}")
        return data if isinstance(data, dict) else {}

    async def get_document_chunks(self, document_id: str) -> dict[str, Any]:
        document = await self.get_knowledge(document_id)
        data, _ = await self._request(
            "GET", f"/chunks/{document_id}", params={"page": 1, "page_size": 1000}
        )
        return {"document": document, "chunks": data if isinstance(data, list) else []}

    async def retry_document(self, document_id: str) -> dict[str, Any]:
        data, _ = await self._request("POST", f"/knowledge/{document_id}/reparse")
        return data if isinstance(data, dict) else {"document_id": document_id, "state": "queued"}

    async def download_document(self, document_id: str) -> tuple[bytes, str, str, str]:
        raise KnowledgeCoreError("WeKnora Adapter 暂不代理原文下载，请在 WeKnora 中查看原文")

    async def upload_document(self, kb_id: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        data, _ = await self._request(
            "POST",
            f"/knowledge-bases/{kb_id}/knowledge/file",
            files={"file": (filename, content, content_type or "application/octet-stream")},
            data={"enable_multimodel": "true"},
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _map_evidence(rows: list[Any], selected_ids: list[str]) -> list[EvidenceRef]:
        evidence: list[EvidenceRef] = []
        seen: set[str] = set()
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            chunk_index = int(item.get("chunk_index") or 0)
            chunk_id = str(item.get("id") or item.get("chunk_id") or f"weknora-result-{index}")
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            evidence.append(
                EvidenceRef(
                    knowledge_base_id=str(
                        item.get("knowledge_base_id")
                        or item.get("kb_id")
                        or (selected_ids[0] if len(selected_ids) == 1 else "weknora")
                    ),
                    document_id=str(item.get("knowledge_id") or item.get("document_id") or ""),
                    chunk_id=chunk_id,
                    locator=str(item.get("locator") or f"分块 {chunk_index + 1}"),
                    revision=str(metadata.get("revision") or item.get("updated_at") or "current"),
                    title=str(item.get("knowledge_title") or item.get("knowledge_filename") or "知识文档"),
                    summary=str(item.get("content") or item.get("excerpt") or "")[:1200],
                )
            )
        return evidence

    async def _wiki_search(self, kb_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data, _ = await self._request(
            "GET",
            f"/knowledgebase/{kb_id}/wiki/search",
            params={"q": query, "limit": limit},
        )
        if isinstance(data, dict):
            rows = data.get("pages") or data.get("results") or []
        else:
            rows = data if isinstance(data, list) else []
        return [item for item in rows if isinstance(item, dict)]

    async def _wiki_page(self, kb_id: str, slug: str) -> dict[str, Any]:
        data, _ = await self._request(
            "GET",
            f"/knowledgebase/{kb_id}/wiki/pages/{quote(slug, safe='/')}",
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _page_evidence(kb_id: str, page: dict[str, Any]) -> EvidenceRef:
        source_refs = page.get("source_refs") if isinstance(page.get("source_refs"), list) else []
        chunk_refs = page.get("chunk_refs") if isinstance(page.get("chunk_refs"), list) else []
        slug = str(page.get("slug") or "unknown")
        content = str(page.get("content") or page.get("summary") or "")
        return EvidenceRef(
            knowledge_base_id=kb_id,
            document_id=str(source_refs[0] if source_refs else page.get("id") or slug),
            chunk_id=str(chunk_refs[0] if chunk_refs else page.get("id") or slug),
            locator=f"Wiki: {slug}",
            revision=f"v{page.get('version') or 'current'}",
            title=str(page.get("title") or slug),
            summary=content[:1200],
        )

    async def research(self, query: str, kb_ids: list[str] | None = None) -> KnowledgeResearchResult:
        from semora_agent.model import structured_model
        from .knowledge_retrieval import retrieve_knowledge

        selected_ids = list(dict.fromkeys(kb_ids or []))
        if not selected_ids:
            raise KnowledgeCoreError("请先选择需要检索的知识库")
        result = await retrieve_knowledge(structured_model, query, selected_ids, client=self)
        keywords = result.packet["knowledge_keywords"]
        pages = result.packet["knowledge"]
        summary = f"使用 {len(keywords)} 个检索词逐词搜索，读取 {len(pages)} 个相关页面。"
        if not pages:
            summary += "本次未取得可用页面，不据此判断知识库没有相关资料。"
        if result.packet.get("knowledge_warning"):
            summary += " 部分检索或读取未完成，已保留其他结果。"
        result.steps.append(KnowledgeResearchStep(kind="completed", summary=summary))
        excerpts = {(item["knowledge_base_id"], f"Wiki: {item['slug']}"): item["content"] for item in pages}
        evidence = [item.model_copy(update={"summary": excerpts.get((item.knowledge_base_id, item.locator), item.summary)[:1200]}) for item in result.evidence]
        return KnowledgeResearchResult(provider=self.provider, agent_id="semora-workbench-knowledge-retrieval",
                                       session_id="", answer=summary, steps=result.steps, evidence=evidence)

    async def search_evidence(self, query: str, kb_ids: list[str] | None = None) -> list[EvidenceRef]:
        selected_ids = kb_ids or []
        data, _ = await self._request(
            "POST",
            "/knowledge-search",
            json={"query": query, "knowledge_base_ids": selected_ids},
        )
        rows = data if isinstance(data, list) else []
        return self._map_evidence(rows, selected_ids)


knowledge_service = WeKnoraClient()
