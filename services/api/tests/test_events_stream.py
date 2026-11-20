import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from semora_contracts import QueryRun, RunEvent, RunStatus
from app.events_stream import stream_events


def test_heartbeat_has_no_event_id_and_never_enters_persisted_run():
    run = QueryRun(session_id="s", question="List records", status=RunStatus.UNDERSTANDING)
    run.events = [RunEvent(event="analysis.started", status=run.status, data={})]
    store = SimpleNamespace(runs={run.id: run})

    async def scenario():
        stream = stream_events(store, run.id, 0, AsyncMock(return_value=False), with_heartbeat=True)
        first = await anext(stream)
        heartbeat = await anext(stream)
        await stream.aclose()
        return first, heartbeat

    first, heartbeat = asyncio.run(scenario())
    assert "id: 1\nevent: analysis.started" in first
    assert "event: run.heartbeat" in heartbeat
    assert '"cursor": 1' in heartbeat
    assert not heartbeat.startswith("id:")
    assert len(run.events) == 1


def test_resume_from_cursor_does_not_repeat_prior_events_and_closes():
    run = QueryRun(session_id="s", question="List records", status=RunStatus.COMPLETED)
    run.events = [
        RunEvent(event="analysis.started", status=RunStatus.UNDERSTANDING, data={}),
        RunEvent(event="answer.completed", status=RunStatus.COMPLETED, data={}),
    ]

    async def scenario():
        return [
            chunk
            async for chunk in stream_events(
                SimpleNamespace(runs={run.id: run}), run.id, 1, AsyncMock(return_value=False)
            )
        ]

    chunks = asyncio.run(scenario())
    assert len(chunks) == 2
    assert chunks[0].startswith("id: 2\nevent: answer.completed")
    assert "event: stream.closed" in chunks[1]
    assert "run.heartbeat" not in "".join(chunks)


def test_disconnected_client_stops_without_changing_task_status():
    run = QueryRun(session_id="s", question="List records", status=RunStatus.EXECUTING)

    async def scenario():
        return [
            chunk
            async for chunk in stream_events(
                SimpleNamespace(runs={run.id: run}), run.id, 0, AsyncMock(return_value=True)
            )
        ]

    assert asyncio.run(scenario()) == []
    assert run.status == RunStatus.EXECUTING


def test_legacy_clients_only_receive_comment_heartbeats():
    run = QueryRun(session_id="s", question="List records", status=RunStatus.EXECUTING)

    async def scenario():
        stream = stream_events(
            SimpleNamespace(runs={run.id: run}), run.id, 0, AsyncMock(return_value=False)
        )
        first = await anext(stream)
        await stream.aclose()
        return first

    assert asyncio.run(scenario()) == ": keep-alive\n\n"
    assert run.events == []
