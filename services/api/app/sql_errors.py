"""Typed SQL diagnostics shared by adapters and execution."""

from .executor_errors import ExecutorOperationError


class SQLIssue(ValueError):
    def __init__(self, message, step="", kind="sql", stage="baseline_validation"):
        self.step, self.kind, self.stage = step, kind, stage
        super().__init__(message)


def issue_data(exc, stage=None):
    if isinstance(exc, ExecutorOperationError):
        return {**exc.public(), "diagnostic": exc.diagnostic, "error_stage": stage or "database"}
    return {"error_type": getattr(exc, "kind", "sql"), "message": str(exc)[:1600],
            "error_stage": getattr(exc, "stage", stage or "baseline_validation")
            if stage in {None, "baseline_validation"} else stage}


def repairable(exc):
    return (
        isinstance(exc, SQLIssue)
        and exc.kind == "sql"
        or isinstance(exc, ExecutorOperationError)
        and exc.kind in {"syntax", "schema", "unsupported_sql"}
    )
