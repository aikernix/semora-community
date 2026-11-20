"""Persist query state and migrate historic record shapes; never execute old plans."""

from semora_contracts import QueryRun, ExecutionReceipt
from .run_state import digest


def record_receipt(run, payload, *, skipped=False):
    fields = {k: v for k, v in payload.items() if k in ExecutionReceipt.model_fields}
    fields["status"] = "skipped" if skipped else "completed"
    receipt = ExecutionReceipt.model_validate(fields)
    run.execution_receipts[receipt.step] = receipt
    return receipt


def restore_receipt(run, step):
    if step not in run.execution_receipts:
        event = next(
            (
                e
                for e in reversed(run.events)
                if e.event in {"source.step.completed", "source.step.skipped"}
                and e.data.get("step") == step
            ),
            None,
        )
        if event:
            fields = {
                **event.data,
                "finished_at": event.data.get("finished_at") or event.created_at,
            }
            # First SQL-native preview emitted display-only step events. Build
            # their receipt from the actual persisted output, not model prose.
            if (
                not fields.get("output_hash")
                and run.plan
                and run.plan.sql_program
                and step in run.source_results
            ):
                output = run.source_results[step]
                fields.update(
                    output_hash=digest(output.model_dump(mode="json")),
                    row_count=output.row_count,
                    duration_ms=output.duration_ms,
                    truncated=output.truncated,
                )
            record_receipt(run, fields, skipped=event.event.endswith("skipped"))
    return run.execution_receipts.get(step)


def run_payload(run):
    """Private fields have one explicit persistence boundary, never API output."""
    return {
        **run.model_dump(mode="json"),
        "agent_memory": run.agent_memory,
        "source_results": {k: v.model_dump(mode="json") for k, v in run.source_results.items()},
        "execution_binding": run.execution_binding,
        "execution_receipts": {
            k: v.model_dump(mode="json") for k, v in run.execution_receipts.items()
        },
    }


def restore_run(payload):
    run = QueryRun.model_validate(payload)
    for step in run.source_results:
        restore_receipt(run, step)
    return run
