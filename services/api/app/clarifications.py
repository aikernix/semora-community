"""Ticket-level business questions, branching and confirmed answers."""

from copy import deepcopy
import json
from hashlib import sha256


def clarification_history(run):
    """Ticket-level submissions survive retries; graph replay is not a new round."""
    previous = (run.continuation or {}).get("clarification_history", [])
    return {item["key"]: item for item in [*previous, *run.clarification_history]}


def clarification_policy(run):
    history = list(clarification_history(run).values())
    business_answered = any(item["stage"] == "business" for item in history)
    return {
        "answered_rounds": len(history),
        "max_rounds": 2,
        "direction_allowed": not history,
        "business_allowed": not business_answered and len(history) < 2,
        "next_action": "interpret_answers_and_compile"
        if business_answered
        else "preview_all_relevant_branches",
    }


def canonical_answers(questions, answers):
    result = {}
    for q in questions:
        raw = str(answers.get(q.id, "")).strip()
        option = next((o for o in q.options if raw in {o.id, o.value}), None)
        if raw:
            result[q.id] = option.value if option else raw
    return result


def active_questions(questions, answers):
    answers = canonical_answers(questions, answers)
    by_id = {q.id: q for q in questions}

    def active(q, visiting):
        if q.id in visiting:
            return False
        return all(
            c.question_id in by_id
            and active(by_id[c.question_id], visiting | {q.id})
            and answers.get(c.question_id) in c.values
            for c in q.depends_on
        )

    return [q for q in questions if active(q, set())]


def relevant_answers(questions, answers):
    normalized = canonical_answers(questions, answers)
    return {
        q.id: normalized[q.id]
        for q in active_questions(questions, normalized)
        if q.id in normalized
    }


def active_gaps(report, answers):
    answers = relevant_answers(report.questions, answers)
    return [
        g
        for g in report.technical_gaps
        if all(answers.get(c.question_id) in c.values for c in g.depends_on)
    ]


def route_business_definitions(report):
    """Legacy/model definition gaps are business questions, never tech blockers.

    Prefer model-authored options. For a misclassified definition, preserve its
    actual request as a free-text card rather than inventing business choices.
    """
    from semora_contracts import ClarificationQuestion

    technical = []
    for gap in report.technical_gaps:
        if gap.kind != "definition":
            technical.append(gap)
            continue
        text = gap.resolution.strip()
        key = (
            "business_"
            + sha256(
                json.dumps(
                    [gap.summary, text, [c.model_dump() for c in gap.depends_on]],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]
        )
        if any(
            q.id == key or (q.question == text and q.depends_on == gap.depends_on)
            for q in report.questions
        ):
            continue
        report.questions.append(
            ClarificationQuestion(
                id=key,
                field=gap.field_refs[0] if gap.field_refs else gap.required_for,
                question=text,
                reason=gap.summary,
                options=[],
                allow_free_text=True,
                blocking=True,
                depends_on=deepcopy(gap.depends_on),
            )
        )
    report.technical_gaps = technical


def prune_answered_questions(report, facts):
    """Resolve form dependencies; never execute a model-authored patch."""
    known = {f.question_id: f.value for f in facts}
    inactive = set()
    report.questions = [q for q in report.questions if q.id not in known]
    changed = True
    while changed:
        changed = False
        for question in list(report.questions):
            if any(
                c.question_id in inactive
                or (c.question_id in known and known[c.question_id] not in c.values)
                for c in question.depends_on
            ):
                inactive.add(question.id)
                report.questions.remove(question)
                changed = True
            else:
                question.depends_on = [c for c in question.depends_on if c.question_id not in known]
    report.technical_gaps = [
        g
        for g in report.technical_gaps
        if not any(
            c.question_id in inactive
            or (c.question_id in known and known[c.question_id] not in c.values)
            for c in g.depends_on
        )
    ]
    for gap in report.technical_gaps:
        gap.depends_on = [c for c in gap.depends_on if c.question_id not in known]
    by_id = {q.id: q for q in report.questions}

    def simplify(conditions):
        result = []
        for c in conditions:
            parent = by_id.get(c.question_id)
            if parent and parent.options and {o.value for o in parent.options} <= set(c.values):
                result.extend(simplify(parent.depends_on))
            else:
                result.append(c)
        return result

    for gap in report.technical_gaps:
        gap.depends_on = simplify(gap.depends_on)


def validate_questions(questions):
    by_id = {q.id: q for q in questions}
    if len(by_id) != len(questions):
        raise ValueError("澄清问题编号重复")

    def visit(q, path):
        if q.id in path:
            raise ValueError("澄清分支形成循环")
        for c in q.depends_on:
            parent = by_id.get(c.question_id)
            if parent is None:
                raise ValueError("澄清分支引用不存在的问题")
            if not set(c.values) <= {o.value for o in parent.options}:
                raise ValueError("澄清分支引用不存在的选项值")
            visit(parent, path | {q.id})

    for q in questions:
        q.allow_free_text = True
        visit(q, set())
