"""Resumable event transport. Heartbeats are ephemeral, never run history."""

import asyncio
import json
import time
from datetime import datetime, timezone
from semora_contracts import RunStatus


TERMINAL = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CLARIFYING,
    RunStatus.AWAITING_APPROVAL,
}


async def stream_events(store, run_id, after, disconnected, *, with_heartbeat=False):
    cursor = after
    next_heartbeat = 0.0
    while True:
        run = store.runs[run_id]
        while cursor < len(run.events):
            item = run.events[cursor]
            cursor += 1
            yield f"id: {cursor}\nevent: {item.event}\ndata: {item.model_dump_json()}\n\n"
        if run.status in TERMINAL:
            payload = json.dumps({"status": run.status.value, "cursor": cursor})
            yield f"event: stream.closed\ndata: {payload}\n\n"
            return
        if await disconnected():
            return
        if time.monotonic() >= next_heartbeat:
            payload = json.dumps(
                {
                    "status": run.status.value,
                    "cursor": cursor,
                    "server_time": datetime.now(timezone.utc).isoformat(),
                }
            )
            # Older clients treat every JSON event as persisted history.
            # Keep comment heartbeats until a client explicitly opts in.
            yield f"event: run.heartbeat\ndata: {payload}\n\n" if with_heartbeat else ": keep-alive\n\n"
            next_heartbeat = time.monotonic() + 5
        await asyncio.sleep(0.1)
