from services.executor.app import main as executor


class FakeConnection:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_executor_cancel_targets_only_registered_query(monkeypatch):
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")
    connection = FakeConnection()
    executor.ACTIVE_QUERIES["run-1"] = connection
    try:
        result = executor.cancel_query("run-1", "test-token")
    finally:
        executor.ACTIVE_QUERIES.pop("run-1", None)
    assert result["accepted"] is True
    assert connection.cancelled is True


def test_executor_cancel_reports_inactive_query(monkeypatch):
    monkeypatch.setenv("REQUIRE_INTERNAL_TOKEN", "true")
    monkeypatch.setenv("INTERNAL_TOKEN", "test-token")
    result = executor.cancel_query("missing", "test-token")
    assert result == {"execution_id": "missing", "accepted": False, "reason": "not_active"}
