"""Shared workbench knowledge preparation, also used by the retrieval test page."""

import asyncio
from dataclasses import dataclass, field
import json
import re

from pydantic import BaseModel, Field
from semora_agent.model import TaskSupersededError
from semora_contracts import KnowledgeResearchStep


KEYWORD_PROMPT = "【任务】从需求与确认口径提取需要查询定义的核心词。\n【要求】按需要决定数量，最多5个不同词，不凑数量，每项一个独立检索词，不拼接查询表达式。\n【输出】按给定JSON契约返回knowledge_keywords。"


class KnowledgeSelection(BaseModel):
    knowledge_keywords: list[str] = Field(
        default_factory=list, max_length=5,
        description="按当前需求决定所需数量，最多5个不同的核心检索词，每项单独检索。",
    )


def knowledge_excerpt(text, keywords, limit=8000):
    """Select verbatim paragraphs, without creating new business definitions."""
    text = str(text or "")
    paragraphs = list(dict.fromkeys(p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()))
    terms = [str(term).casefold() for term in keywords if term]
    matches = {i for i, p in enumerate(paragraphs) if any(t in p.casefold() for t in terms)}
    priority = {0} if paragraphs else set()
    for i in matches:
        priority.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(paragraphs))
    selected = {}
    remaining = limit
    for i in [*sorted(priority), *[j for j in range(len(paragraphs)) if j not in priority]]:
        separator = 2 if selected else 0
        if len(paragraphs[i]) + separator <= remaining:
            selected[i] = paragraphs[i]
            remaining -= len(paragraphs[i]) + separator
    if not selected and paragraphs:
        i = min(matches) if matches else 0
        selected[i] = paragraphs[i][:limit]
    content = "\n\n".join(selected[i] for i in sorted(selected))
    return {
        "content": content,
        "excerpted": len(selected) < len(paragraphs) or any(selected[i] != paragraphs[i] for i in selected),
        "original_chars": len(text), "selected_paragraphs": sorted(selected),
    }


@dataclass
class KnowledgeRetrieval:
    packet: dict
    evidence: list
    discoveries: list
    steps: list = field(default_factory=list)


async def retrieve_knowledge(model, query, kb_ids, *, client, confirmed_answers=None,
                             evidence=None, discoveries=None):
    packet = {"knowledge": []}
    result = KnowledgeRetrieval(packet, evidence if evidence is not None else [],
                               discoveries if discoveries is not None else [])
    keywords = []
    kb_ids = list(dict.fromkeys(kb_ids or []))
    if kb_ids:
        try:
            raw = await model.complete_json(KEYWORD_PROMPT, json.dumps({
                "request": query, "confirmed_answers": confirmed_answers or [],
                "output_contract": KnowledgeSelection.model_json_schema(),
            }, ensure_ascii=False))
            if isinstance(raw.get("knowledge_keywords"), list):
                raw = {**raw, "knowledge_keywords": list(dict.fromkeys(
                    t.strip() for t in raw["knowledge_keywords"]
                    if isinstance(t, str) and t.strip() and len(t.strip()) <= 64))[:5]}
            keywords = KnowledgeSelection.model_validate(raw).knowledge_keywords
        except TaskSupersededError:
            raise
        except Exception:
            packet["knowledge_warning"] = "检索词提取暂不可用，按需求原文检索；辅助知识不阻断规划"
            result.steps.append(KnowledgeResearchStep(kind="fallback", tool="knowledge_keywords",
                summary="检索词提取暂不可用，使用需求原文中的词继续检索"))
        if not keywords:
            keywords = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fffA-Za-z0-9_+-]{2,64}", query)))[:5]
    keywords = list(dict.fromkeys(t.strip() for t in keywords if t.strip() and len(t.strip()) <= 64))
    packet["knowledge_keywords"] = keywords
    packet["knowledge_retrieval"] = []
    result.steps.append(KnowledgeResearchStep(kind="keywords", tool="knowledge_keywords",
        summary="检索词：" + "、".join(keywords) if keywords else "本次没有需要检索的关键词"))

    def remember(kb, kind, ref):
        value = {"kb_id": kb, "kind": kind, "ref": ref}
        if ref and value not in result.discoveries:
            result.discoveries.append(value)

    async def collect():
        pages = set()
        for term in keywords:
            for kb in kb_ids:
                try:
                    async with asyncio.timeout(10):
                        rows = await client._wiki_search(kb, re.escape(term))
                except TaskSupersededError:
                    raise
                except Exception:
                    packet["knowledge_retrieval"].append({"keyword": term, "knowledge_base_id": kb, "state": "unavailable"})
                    packet["knowledge_warning"] = "部分关键词检索暂不可用；已保留其他检索结果，未将检索失败视为业务缺口"
                    result.steps.append(KnowledgeResearchStep(kind="search", tool="wiki_search", status="unavailable",
                        summary=f"检索“{term}”暂不可用，继续其他关键词"))
                    continue
                packet["knowledge_retrieval"].append({"keyword": term, "knowledge_base_id": kb,
                                                       "state": "completed", "count": len(rows)})
                result.steps.append(KnowledgeResearchStep(kind="search", tool="wiki_search",
                    summary=f"检索“{term}”，找到 {len(rows)} 个页面"))
                for row in rows:
                    remember(kb, "page", row.get("slug"))
                for row in rows[:2]:
                    slug = row.get("slug")
                    if not slug or (kb, slug) in pages or len(pages) >= 3:
                        continue
                    try:
                        async with asyncio.timeout(10):
                            page = await client._wiki_page(kb, slug)
                    except TaskSupersededError:
                        raise
                    except Exception:
                        packet["knowledge_warning"] = "部分页面暂不可用；已保留入口与其他证据，可按具体缺口补读"
                        result.steps.append(KnowledgeResearchStep(kind="page", tool="wiki_read_page", status="unavailable",
                            summary=f"页面“{row.get('title') or slug}”暂不可用，保留其他检索结果"))
                        continue
                    pages.add((kb, slug))
                    for link in (page.get("out_links") or []) + (page.get("in_links") or []):
                        remember(kb, "page", link.get("slug") if isinstance(link, dict) else link)
                    for source in page.get("source_refs") or []:
                        if isinstance(source, str):
                            remember(kb, "source", source)
                    reference = client._page_evidence(kb, page)
                    if not any(e.knowledge_base_id == kb and e.locator == reference.locator for e in result.evidence):
                        result.evidence.append(reference)
                    packet["knowledge"].append({
                        "knowledge_base_id": kb, "slug": slug, "title": page.get("title"),
                        **knowledge_excerpt(page.get("content") or page.get("summary"), keywords),
                        "read_more": {"knowledge_base_id": kb, "slug": slug},
                        "links": page.get("out_links") or [], "source_refs": page.get("source_refs") or [],
                    })
                    result.steps.append(KnowledgeResearchStep(kind="page", tool="wiki_read_page",
                        summary=f"读取“{page.get('title') or slug}”，保留相关原文和来源引用"))
    try:
        await collect()
    except TaskSupersededError:
        raise
    except Exception:
        packet["knowledge_warning"] = "辅助知识准备未全部完成，保留已取得证据；不阻断基于本体与用户口径的规划"
    return result
