"""Canonical hashes and immutable input revisions for in-flight tasks."""

import hashlib
import json


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def binding(run, ontology, scope):
    return digest(
        {
            "domain": run.domain,
            "ontology": ontology,
            "sources": sorted(
                {
                    o.get("dataSourceId") or ontology.get("dataSourceId", "")
                    for o in ontology.get("objects", [])
                }
            ),
            "scope": scope,
            "authorization": run.authorization.model_dump(mode="json")
            if run.authorization
            else None,
        }
    )


def revision(run):
    # Operational events, new evidence and private drafts are deliberately not
    # authoritative user changes. They must not invalidate their own tool call.
    return digest(
        {
            "question": run.question,
            "domain": run.domain,
            "answers": [f.model_dump(mode="json") for f in run.user_answers],
            "authorization": run.authorization.model_dump(mode="json")
            if run.authorization
            else None,
            "ontology": run.ontology.model_dump(mode="json") if run.ontology else None,
            "requirement": run.requirement.model_dump(mode="json") if run.requirement else None,
            "plan": run.plan.model_dump(mode="json") if run.plan else None,
            "result_hash": digest(run.result.model_dump(mode="json")) if run.result else None,
        }
    )
