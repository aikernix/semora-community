from __future__ import annotations

import os
from typing import Any

import httpx
from semora_contracts import OntologySnapshotRef


class OntologyCoreError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class OntologyCoreClient:
    def __init__(self, base_url: str | None = None, internal_token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("ONTOLOGY_URL", "http://localhost:8081")).rstrip("/")
        self.internal_token = internal_token or os.getenv("INTERNAL_TOKEN", "semora-dev-internal")

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        # Legacy records can contain database Decimal scores. Strip retired
        # relation fields before HTTP encoding, for every publication entry point.
        if 'json' in kwargs:
            from .relation_output import clean_relations
            kwargs['json'] = clean_relations(kwargs['json'])
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}/{endpoint.lstrip('/')}",
                    headers={"X-Semora-Internal-Token": self.internal_token},
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if 400 <= exc.response.status_code < 500 else 503
            detail = exc.response.text[:500]
            try:
                detail = str(exc.response.json().get("error", detail))
            except ValueError:
                pass
            raise OntologyCoreError(detail, status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OntologyCoreError(f"Ontology Core 请求失败：{type(exc).__name__}") from exc

    async def list_drafts(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/ontology/drafts")

    async def get_draft(self, domain: str) -> dict[str, Any]:
        return self._normalize_collections(await self._request("GET", f"/api/v1/ontology/drafts/{domain}"))

    @staticmethod
    def _normalize_collections(value: dict[str, Any]) -> dict[str, Any]:
        # Go encodes nil slices as null. Consumers use collection semantics even
        # when a domain has no formal metrics, relations or knowledge bindings.
        ontology = dict(value)
        for key in ('objects', 'relations', 'metrics', 'knowledgeBaseIds', 'defaultFilters', 'sourcePackageRefs'):
            items = ontology.get(key)
            if items is not None and not isinstance(items, list):
                raise OntologyCoreError(f'本体字段 {key} 必须是数组')
            ontology[key] = items if items is not None else []
        return ontology

    async def save_draft(self, domain: str, draft: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/ontology/drafts/{domain}", json=draft)

    async def create_domain(self, domain: str, name: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/ontology/domains", json={"domain": domain, "name": name})

    async def delete_domain(self, domain: str) -> dict[str, bool]:
        return await self._request("DELETE", f"/api/v1/ontology/domains/{domain}")

    async def delete_datasource(self, source_id: str) -> dict[str, bool]:
        return await self._request("DELETE", f"/internal/v1/datasources/{source_id}")

    async def get_configuration(self, domain: str) -> dict[str, Any]:
        from .relation_output import clean_relations
        return clean_relations(self._normalize_collections(await self._request("GET", f"/api/v1/ontology/configurations/{domain}")))

    async def save_configuration(self, domain: str, value: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/ontology/configurations/{domain}", json=value)

    async def unbind_source(self, domain: str, source_id: str) -> dict[str, Any]:
        return self._normalize_collections(await self._request('DELETE',f'/api/v1/ontology/configurations/{domain}/sources/{source_id}'))

    async def update_source_package(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self._request('PUT','/api/v1/ontology/source-packages',json=value)

    async def append_cross_relations(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self._request('POST', '/api/v1/ontology/cross-relations/append', json=value)

    async def bind_knowledge_bases(self, domain: str, ids: list[str]) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/v1/ontology/drafts/{domain}/knowledge-bases",
                                   json={"knowledgeBaseIds": ids})

    async def publish(self, domain: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/ontology/publish", json={"domain": domain})

    async def latest_snapshot(self, domain: str) -> OntologySnapshotRef:
        reference, _ = await self.latest_snapshot_bundle(domain)
        return reference

    async def latest_snapshot_bundle(self, domain: str) -> tuple[OntologySnapshotRef, dict[str, Any]]:
        if not domain:
            raise OntologyCoreError('请先选择业务域，不能读取未指定业务域的本体')
        try:
            value = await self._request("GET", "/api/v1/ontology/snapshots/latest", params={"domain": domain})
        except OntologyCoreError as exc:
            if exc.status_code == 404:
                raise OntologyCoreError('该业务域尚未配置可用本体，请先添加对象和关系并保存', 404) from exc
            raise
        ontology = self._normalize_collections(value["ontology"])
        reference = OntologySnapshotRef(
            domain=value["domain"],
            version=value["version"],
            content_hash=value["contentHash"],
            artifact_version=value["artifactVersion"],
            data_source_id=str(ontology.get("dataSourceId") or ""),
            knowledge_base_ids=[str(item) for item in ontology.get("knowledgeBaseIds", [])],
        )
        return reference, ontology

    async def find_path(self, domain: str, source: str, target: str) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/v1/ontology/paths", params={"domain": domain, "from": source, "to": target}
        )

    async def list_candidates(self, domain: str = "") -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/ontology/candidates", params={"domain": domain})

    async def create_candidate(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/ontology/candidates", json=value)

    async def decide_candidate(
        self, candidate_id: str, decision: str, reviewer: str = "", note: str = ""
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/ontology/candidates/{candidate_id}/decision",
            json={"decision": decision, "reviewer": reviewer, "note": note},
        )


ontology_service = OntologyCoreClient()
