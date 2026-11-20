"""Evidence-driven batch research with finite tool use and explicit reconciliation."""
from __future__ import annotations

import asyncio
import os
import json
import logging

from .relationship_context import complete, relation_batches, relation_payload


async def audit_structural_acceptances(candidates, fetch, persist):
    """Check factual contradictions before publishing, including restored decisions."""
    rechecks = []
    for c in candidates:
        if (c.get('status') != 'accepted' or c.get('evidence_grade') in {'foreign_key', 'manual'}
                or c.get('reviewed_by') or any(e.get('kind') == 'value_profile' for e in c['evidence'])):
            continue
        try:
            evidence = await fetch(c, 'profile')
        except Exception as exc:
            # Preserve the prior publication on outages, without approving new proposals.
            evidence = {'kind': 'tool_unavailable',
                        'tool': 'profile', 'reason': type(exc).__name__, 'error_message': str(exc)[:500]}
        c['evidence'].append(evidence)
        if evidence.get('kind') == 'value_profile':
            from .relationship_review_policy import review_reasons
            first = next((e for e in reversed(c['evidence']) if e.get('kind') == 'model_semantic_review'), {})
            if review_reasons(c, first):
                c['evidence'] = [e for e in c['evidence'] if e.get('kind') != 'research_complete']
                rechecks.append(c)
        await persist([c])
    return rechecks


async def run(candidates, packet_for, fetch, model, persist, progress):
    from .relationship_research import VERSION, SYSTEM, REVIEW_SYSTEM, DATA_GUIDANCE, with_data_guidance, decode_decision, factual_evidence
    from .relationship_evidence import finalize_relationships
    todo = []
    for c in candidates:
        if c["evidence_grade"] in {"foreign_key", "manual"} or c.get("reviewed_by"):
            continue
        if any(e.get("kind") == "research_complete" and e.get("version") == VERSION for e in c["evidence"]):
            continue
        if any(e.get("kind") in {"verified_direct_lineage", "verified_non_relation_field"} for e in c["evidence"]):
            finalize_relationships([c])
            c["evidence"].append({"kind": "research_complete", "version": VERSION})
            await persist([c])
            continue
        guards = sorted(c.get('condition_template',{}).get('guards',[]),key=lambda g:json.dumps(g,sort_keys=True))
        c["evidence"] = [e for e in factual_evidence(c["evidence"])
                         if not (e.get("kind") == "value_profile" and (
                             sorted(e.get('statistics',{}).get('guards_applied',[]),key=lambda g:json.dumps(g,sort_keys=True)) != guards
                             or e.get('statistics', {}).get('method') in {'indexed_directional_membership_v1','indexed_directional_membership_v2','indexed_directional_membership_v3_context'}))]
        todo.append(c)
    todo.sort(key=lambda c: (c.get("source_datasource_id", ""), c.get("source_table_id", ""),
                             tuple(c.get("source_columns", [])), c.get("target_table_id", ""), c["id"]))
    lookup = {c["id"]: c for c in todo}
    workers = max(1, min(3, int(os.getenv("RELATIONSHIP_MODEL_CONCURRENCY", "3"))))
    done = 0

    async def batch(initial, fits):
        nonlocal done
        entries = [lookup[p["candidate_id"]] for p in initial]
        seen = set()
        async def obtain(c, kind):
            if kind not in {"definition", "profile"} or (c["id"], kind) in seen:
                return False
            seen.add((c["id"], kind))
            try:
                evidence = await fetch(c, kind)
            except Exception as exc:
                evidence = {"kind": "tool_unavailable", "tool": kind, "reason": type(exc).__name__}
            c["evidence"].append(evidence)
            return True

        async def assess(values, system, kind, *, repair=False, allow_tools=True):
            decisions = {}
            def review_packet(candidate):
                packet = packet_for(candidate)
                initial = (kind == 'model_semantic_review' and not repair
                           and not candidate.get('global_review_reasons')
                           and not any(e.get('kind') == 'value_profile' for e in candidate.get('evidence', [])))
                if initial:
                    # Structural first pass needs only this relation and its factual evidence.
                    packet.pop('other_proposals', None)
                    packet.pop('target_domain_observations', None)
                return packet
            for group, group_fits in relation_batches([packet_for(c) for c in values], system + DATA_GUIDANCE):
                current = {p["candidate_id"] for p in group}
                if not group_fits:
                    for identifier in current:
                        lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
                    continue
                # Evidence changes start a new request; format retries reuse the same candidate facts.
                item_diagnostics = {}
                for attempt in range(3 if allow_tools else 1):
                    has_data = any(e.get("kind") == "value_profile" for i in current for e in lookup[i]["evidence"])
                    prompt = with_data_guidance(system) if has_data else system
                    parsed_response = None
                    repair_feedback = None
                    for retry in range(3):
                        # Valid decisions are retained. Later retries target only candidates
                        # whose own decision is still missing or invalid.
                        expected = current - decisions.keys()
                        if not expected:
                            break
                        payload = relation_payload([review_packet(lookup[i]) for i in expected], sorted(expected),
                                                   tools_allowed=allow_tools and attempt < 2)
                        request_prompt = prompt
                        if repair_feedback:
                            request_prompt += ("\n【上次输出校验失败，请定向修复】只返回本次要求的candidate_id；"
                                               "逐条输出完整合法decision，补齐所有必填字段，不要复用非法枚举。"
                                               "kind表示关系语义，code只表示排除原因。校验错误如下：\n"
                                               + json.dumps(repair_feedback, ensure_ascii=False))
                        response = None
                        try:
                            response = await complete(model, request_prompt, payload)
                            from .relationship_research import normalize_research_response
                            response = normalize_research_response(response)
                            from .relationship_contracts import (ResearchOutput, CompactResearchOutput, DataResearchOutput,
                                                                 Decision, CompactDecision, DataCompactDecision)
                            compact = isinstance(response, dict) and any('outcome' in d for d in response.get('decisions',[]) if isinstance(d,dict))
                            response_contract = (DataResearchOutput if has_data else CompactResearchOutput) if compact else ResearchOutput
                            decision_contract = (DataCompactDecision if has_data else CompactDecision) if compact else Decision
                            # First retry keeps strict batch validation. From the second retry onward,
                            # malformed business decisions affect only their own candidate.
                            if retry == 0:
                                response_contract.model_validate(response)
                            elif not isinstance(response,dict) or not isinstance(response.get('decisions'),list):
                                raise ValueError('invalid_structured_decision')
                            values_out = response['decisions']
                            legacy_requests = response.get('requests', []) if isinstance(response, dict) else []
                            has_legacy_requests = isinstance(legacy_requests, list) and bool(legacy_requests)
                            values_by_id = {}
                            item_errors = {}
                            for item_index, value in enumerate(values_out):
                                identifier = value.get('candidate_id') if isinstance(value, dict) else None
                                try:
                                    if retry >= 1:
                                        decision_contract.model_validate(value)
                                    if not has_data and isinstance(value, dict) and 'coverage' in value:
                                        raise ValueError('coverage_outside_data_review')
                                    parsed = decode_decision(value, lookup[identifier].get('condition_template',{}).get('guards',[])) if identifier in expected else None
                                    if not parsed or identifier not in expected or identifier in values_by_id:
                                        raise ValueError('invalid_structured_decision')
                                    values_by_id[identifier] = parsed
                                except Exception as item_exc:
                                    if retry == 0:
                                        raise
                                    item_errors[str(identifier or f'index:{item_index}')] = {
                                        'error_type': type(item_exc).__name__, 'error_message': str(item_exc)[:500]}
                            if set(values_by_id) != expected and retry == 0 and not has_legacy_requests:
                                raise ValueError('missing_structured_decisions')
                            if not values_by_id and expected and not has_legacy_requests:
                                # Empty/all-invalid batches have no valid decision to salvage yet.
                                raise ValueError('missing_structured_decisions')
                            missing_now = expected - values_by_id.keys()
                            if retry >= 1 and missing_now:
                                item_diagnostics.update(item_errors)
                                repair_feedback = {'candidate_ids': sorted(missing_now), 'errors': {
                                    identifier: item_errors.get(identifier, {'error_message': '未返回合法decision'})
                                    for identifier in sorted(missing_now)}}
                                logging.getLogger('semora.model').info(json.dumps({'event':'relationship.research.decisions_discarded',
                                    'review':kind,'attempt':retry+1,'required_ids':sorted(expected),'retained_ids':sorted(values_by_id),
                                    'discarded_ids':sorted(missing_now),'errors':item_errors},ensure_ascii=False))
                            if missing_now and retry < 2 and not has_legacy_requests:
                                decisions.update(values_by_id)
                                continue
                            parsed_response = values_by_id
                            break
                        except Exception as exc:
                            from pydantic import ValidationError
                            received = response.get('decisions', []) if isinstance(response, dict) else []
                            received_ids = [v.get('candidate_id') for v in received if isinstance(v, dict)] if isinstance(received, list) else []
                            diagnostic = {'kind': 'research_response_invalid', 'review': kind,
                                'attempt': retry + 1, 'error_type': type(exc).__name__,
                                'error_message': str(exc)[:1000],
                                'error_code': str(exc) if str(exc) in {'invalid_structured_decision', 'missing_structured_decisions'} else None,
                                'required_ids': sorted(expected),
                                'missing_ids': sorted(expected - set(i for i in received_ids if isinstance(i, str))),
                                'extra_ids': sorted(set(i for i in received_ids if isinstance(i, str)) - expected),
                                'received_decisions': [{k: v.get(k) for k in ('candidate_id','outcome','kind','code','query_outcome','relationship_kind')}
                                    for v in received if isinstance(v, dict)] if isinstance(received, list) else [],
                                'validation': [{'path': list(e['loc']), 'type': e['type']} for e in exc.errors()] if isinstance(exc, ValidationError) else []}
                            repair_feedback = {'candidate_ids': sorted(expected), 'error_type': type(exc).__name__,
                                               'error_message': str(exc)[:1500]}
                            logging.getLogger('semora.model').info(json.dumps({'event': 'relationship.research.invalid', **diagnostic}, ensure_ascii=False))
                            if retry == 2:
                                for identifier in expected:
                                    lookup[identifier]['evidence'].append({'kind':'research_gap','reason':type(exc).__name__, 'diagnostic': diagnostic})
                    if parsed_response is None:
                        break
                    decisions.update(parsed_response)
                    # Give invalid IDs one final targeted repair. Only the last failed
                    # attempt becomes per-candidate pending; valid siblings are untouched.
                    if current - decisions.keys() and not has_legacy_requests:
                        break
                    added = False
                    requests = response.get("requests", []) if isinstance(response, dict) else []  # legacy wire compatibility
                    requests = list(requests) if isinstance(requests, list) else []
                    for value in values_out:
                        if not isinstance(value, dict) or value.get('candidate_id') not in current:
                            continue
                        for gap in value.get('gaps', []):
                            if isinstance(gap, dict) and gap.get('request') in {'definition','profile'}:
                                requests.append({'candidate_id':value['candidate_id'],'kind':gap['request']})
                    if allow_tools and attempt < 2 and isinstance(requests, list):
                        for request in requests:
                            if isinstance(request, dict) and request.get("candidate_id") in current:
                                added = await obtain(lookup[request["candidate_id"]], request.get("kind")) or added
                    if not added and current <= decisions.keys():
                        break
                    if added:
                        # A decision made before new evidence must be revisited.
                        for identifier in current:
                            decisions.pop(identifier, None)
                for identifier in current:
                    if identifier in decisions:
                        lookup[identifier]["evidence"].append({"kind": kind, **decisions[identifier]})
                    else:
                        gap = {"kind": "research_gap", "reason": "missing_valid_structured_decision"}
                        if identifier in item_diagnostics:
                            gap['diagnostic'] = item_diagnostics[identifier]
                        lookup[identifier]["evidence"].append(gap)
            return decisions

        if fits:
            first = await assess(entries, SYSTEM, "model_semantic_review")
            for c in entries:
                if first.get(c["id"], {}).get("query_outcome") == "usable" and not any(e.get("kind") == "value_profile" for e in c["evidence"]):
                    await obtain(c, "profile")
            await persist(entries)
            from .relationship_review_policy import review_reasons
            uncertain = []
            for c in entries:
                reasons = review_reasons(c, first.get(c['id'], {}))
                from .relationship_profile import support
                profile = next((e.get('statistics', {}) for e in reversed(c['evidence']) if e.get('kind') == 'value_profile'), {})
                data_blocked = bool(profile) and not support(profile, c.get('scope') == 'cross_source')['high_coverage']
                c['review_required'] = bool(reasons) and not data_blocked
                c['review_reasons'] = reasons
                c['review_policy_version'] = 'ambiguity-v2-coverage80'
                c['evidence'].append({'kind': 'review_policy', 'required': c['review_required'],
                                      'reasons': reasons, 'data_gate_not_met':data_blocked, 'version':c['review_policy_version']})
                if c['review_required']:
                    uncertain.append(c)
            second = await assess(uncertain, REVIEW_SYSTEM, 'independent_review') if uncertain else {}
            disagreement = [c for c in uncertain if c['id'] in first and c['id'] in second and
                            (first[c['id']]['query_outcome'] != second[c['id']]['query_outcome']
                             or first[c['id']]['additional_predicates'] != second[c['id']]['additional_predicates'])]
            if disagreement:
                await assess(disagreement, SYSTEM, 'model_semantic_review', repair=True, allow_tools=False)
            for c in entries:
                required = ['model_semantic_review'] + (['independent_review'] if c['review_required'] else [])
                from .relationship_profile import timeout_awaiting_review
                profile = next((e.get('statistics', {}) for e in reversed(c['evidence']) if e.get('kind') == 'value_profile'), {})
                if all(any(e.get('kind') == kind for e in c['evidence']) for kind in required) and not any(e.get('kind') in {'research_gap', 'tool_unavailable'} for e in c['evidence']) and not timeout_awaiting_review(profile,c.get('scope')=='cross_source'):
                    c['evidence'].append({'kind': 'research_complete', 'version': VERSION})
        else:
            for c in entries:
                c["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
        await persist(entries)
        done += len(entries)
        await progress(done, len(todo))

    queue = asyncio.Queue()
    for group in relation_batches([packet_for(c) for c in todo], SYSTEM):
        queue.put_nowait(group)
    async def worker():
        while not queue.empty():
            group, fits = queue.get_nowait()
            await batch(group, fits)
    await asyncio.gather(*(worker() for _ in range(workers)))
