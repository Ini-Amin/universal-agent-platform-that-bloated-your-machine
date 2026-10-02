"""Tests for resource bounds and fail-closed sandbox defaults (Master spec §22, §25, §30).

Verifies:
1. MemorySink FIFO eviction at max_events cap with accurate dropped count & query filtering.
2. EventStream replay buffer bounded.
3. runs dict retention policy (keep newest N; evicted id -> 404, not stale data).
4. run_control registry cleanup on terminal status.
5. SSE replay cap with honest meta frame (`replayed: N of M`).
6. Sandbox: empty hosts + allow_network=True -> DENY (fail-closed default).
7. Sandbox: explicit allow_any_host=True -> allow (opt-in escape hatch).
8. Sandbox: listed host allowed, unlisted denied.
9. Normal healthy run lifecycle unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uap.observability import EventBus, EventKind, ObsEvent
from uap.observability.sinks import DEFAULT_MAX_EVENTS, MemorySink
from uap.sandbox import Sandbox, SandboxPolicy, SandboxViolation
from uap.server import EventStream, create_app
from uap.server.run_control import drop_control, get_control, run_controls


def _make_event(kind: EventKind = EventKind.TASK_STARTED, task_id: str = "t1", **data: Any) -> ObsEvent:
    return ObsEvent(kind=kind, task_id=task_id, data=data)


# --------------------------------------------------------------------------- #
# 1. MemorySink capping & query
# --------------------------------------------------------------------------- #


def test_memory_sink_evicts_oldest_at_cap_and_tracks_dropped() -> None:
    sink = MemorySink(max_events=3)
    events = [
        _make_event(EventKind.TASK_STARTED, task_id="t1", seq=0),
        _make_event(EventKind.NODE_STARTED, task_id="t1", seq=1),
        _make_event(EventKind.NODE_FINISHED, task_id="t1", seq=2),
        _make_event(EventKind.NODE_STARTED, task_id="t2", seq=3),
        _make_event(EventKind.TASK_FINISHED, task_id="t2", seq=4),
    ]
    for ev in events:
        sink(ev)

    assert len(sink.events) == 3
    assert sink.dropped == 2
    # The oldest two (seq 0 and 1) were evicted; seq 2, 3, 4 remain.
    retained_seqs = [ev.data["seq"] for ev in sink.events]
    assert retained_seqs == [2, 3, 4]


def test_memory_sink_query_filters_retained_events() -> None:
    sink = MemorySink(max_events=3)
    sink(_make_event(EventKind.NODE_STARTED, task_id="t1", seq=0))
    sink(_make_event(EventKind.NODE_FINISHED, task_id="t1", seq=1))
    sink(_make_event(EventKind.NODE_STARTED, task_id="t2", seq=2))
    sink(_make_event(EventKind.NODE_FINISHED, task_id="t2", seq=3))

    # seq 0 was evicted; remaining are seq 1 (t1), seq 2 (t2), seq 3 (t2)
    assert len(sink.events) == 3
    t1_events = sink.query(task_id="t1")
    assert len(t1_events) == 1
    assert t1_events[0].data["seq"] == 1

    t2_started = sink.query(kind=EventKind.NODE_STARTED, task_id="t2")
    assert len(t2_started) == 1
    assert t2_started[0].data["seq"] == 2


def test_memory_sink_zero_and_unbounded_limits() -> None:
    # max_events=0 drops everything immediately
    sink_zero = MemorySink(max_events=0)
    sink_zero(_make_event(EventKind.TASK_STARTED, task_id="t0"))
    assert len(sink_zero.events) == 0
    assert sink_zero.dropped == 1
    assert sink_zero.query() == []

    # max_events=None is unbounded
    sink_unbounded = MemorySink(max_events=None)
    for i in range(10):
        sink_unbounded(_make_event(EventKind.TOOL_CALL, task_id="t0", i=i))
    assert len(sink_unbounded.events) == 10
    assert sink_unbounded.dropped == 0


# --------------------------------------------------------------------------- #
# 2. EventStream buffer cap
# --------------------------------------------------------------------------- #


def test_event_stream_replay_buffer_capped() -> None:
    stream = EventStream(max_events=4, max_replay=10)
    for i in range(7):
        stream(_make_event(EventKind.NODE_STARTED, task_id="stream-test", seq=i))

    assert len(stream.events) == 4
    assert stream.dropped == 3
    assert [ev.data["seq"] for ev in stream.events] == [3, 4, 5, 6]


# --------------------------------------------------------------------------- #
# 3. runs dict retention policy (FIFO by creation time)
# --------------------------------------------------------------------------- #


def test_runs_dict_keeps_newest_n_and_evicted_returns_404(tmp_path: Path) -> None:
    app = create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        max_runs=2,
    )
    with TestClient(app) as client:
        # Submit 3 tasks; with max_runs=2, task 1 must be evicted.
        t1 = client.post("/tasks", json={"input": "research task 1"}).json()["task_id"]
        t2 = client.post("/tasks", json={"input": "research task 2"}).json()["task_id"]
        t3 = client.post("/tasks", json={"input": "research task 3"}).json()["task_id"]

        # Evicted run returns 404 (honest) rather than stale entry.
        res_t1 = client.get(f"/tasks/{t1}")
        assert res_t1.status_code == 404
        assert res_t1.json()["detail"] == "unknown task"

        # Tasks 2 and 3 are retained.
        res_t2 = client.get(f"/tasks/{t2}")
        assert res_t2.status_code == 200
        assert res_t2.json()["task_id"] == t2

        res_t3 = client.get(f"/tasks/{t3}")
        assert res_t3.status_code == 200
        assert res_t3.json()["task_id"] == t3

        # List endpoint only reports retained runs.
        task_list = client.get("/tasks").json()
        listed_ids = [run["task_id"] for run in task_list]
        assert t1 not in listed_ids
        assert t2 in listed_ids
        assert t3 in listed_ids


# --------------------------------------------------------------------------- #
# 4. run_control registry cleanup after terminal status
# --------------------------------------------------------------------------- #


def test_run_control_entry_removed_after_terminal_status(tmp_path: Path) -> None:
    app = create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
    )
    with TestClient(app) as client:
        task_res = client.post("/tasks", json={"input": "research best practices"}).json()
        task_id = task_res["task_id"]

        # Since run_inline=True, workflow completes synchronously before POST returns.
        # RunControl entry MUST be cleaned up from the registry on terminal status.
        assert task_id not in run_controls()

        # Probing pause on the completed task returns not_running and does NOT leak a control.
        pause_res = client.post(f"/api/executions/{task_id}/pause")
        assert pause_res.status_code == 200
        assert pause_res.json()["status"] == "not_running"
        assert task_id not in run_controls()


# --------------------------------------------------------------------------- #
# 5. SSE replay cap and honest meta frame
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_sse_replay_capped_with_honest_meta() -> None:
    stream = EventStream(max_events=20, max_replay=2)
    task_id = "replay-task"

    # Emit 5 events for this task
    for i in range(5):
        stream(_make_event(EventKind.NODE_STARTED, task_id=task_id, seq=i))

    frames: list[str] = []
    async for frame in stream.iterate(task_id=task_id, heartbeat=0.5):
        frames.append(frame)
        if len(frames) >= 3:  # 1 meta + 2 replayed
            break

    # First frame is meta describing the capped replay
    assert frames[0].startswith("data: ")
    meta = json.loads(frames[0][6:].strip())
    assert meta["kind"] == "meta"
    assert meta["task_id"] == task_id
    assert meta["replayed"] == 2
    assert meta["total_matching"] == 5
    assert meta["buffer_dropped"] == 0

    # The 2 replayed events are the newest 2 (seq 3 and 4)
    event1 = json.loads(frames[1][6:].strip())
    event2 = json.loads(frames[2][6:].strip())
    assert event1["data"]["seq"] == 3
    assert event2["data"]["seq"] == 4


def test_sse_endpoint_includes_meta_frame(tmp_path: Path) -> None:
    app = create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
    )
    with TestClient(app) as client:
        task_id = client.post("/tasks", json={"input": "research topic"}).json()["task_id"]

        with client.stream("GET", f"/events?task_id={task_id}") as stream:
            assert stream.status_code == 200
            data_frames = [
                json.loads(line[len("data:"):].strip())
                for line in stream.iter_lines()
                if line.startswith("data:")
            ]

        assert len(data_frames) >= 2
        meta = data_frames[0]
        assert meta["kind"] == "meta"
        assert meta["task_id"] == task_id
        assert "replayed" in meta
        assert "total_matching" in meta
        # Stream closed cleanly on the terminal task_finished event
        assert data_frames[-1]["kind"] == EventKind.TASK_FINISHED


# --------------------------------------------------------------------------- #
# 6. Sandbox: empty hosts + allow_network=True -> DENY (fail closed)
# --------------------------------------------------------------------------- #


def test_sandbox_empty_hosts_allow_network_denies_all() -> None:
    sb = Sandbox(SandboxPolicy(allow_network=True, allow_network_hosts=()))
    with pytest.raises(SandboxViolation) as exc_info:
        sb.check_network("example.com")
    assert "network host denied" in str(exc_info.value)
    with pytest.raises(SandboxViolation):
        sb.check_network("1.1.1.1")


# --------------------------------------------------------------------------- #
# 7. Sandbox: explicit allow_any_host=True -> allow (escape hatch)
# --------------------------------------------------------------------------- #


def test_sandbox_explicit_allow_any_host_escape_hatch() -> None:
    sb = Sandbox(SandboxPolicy(allow_network=True, allow_any_host=True))
    assert sb.check_network("example.com") == "example.com"
    assert sb.check_network("evil.internal.corp") == "evil.internal.corp"

    # Even with allow_any_host=True, global allow_network=False still fails closed
    sb_disabled = Sandbox(SandboxPolicy(allow_network=False, allow_any_host=True))
    with pytest.raises(SandboxViolation) as exc_info:
        sb_disabled.check_network("example.com")
    assert "network access disabled" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 8. Sandbox: listed host allowed, unlisted host denied
# --------------------------------------------------------------------------- #


def test_sandbox_listed_host_rules() -> None:
    sb = Sandbox(SandboxPolicy(allow_network=True, allow_network_hosts=("api.github.com", "hackerone.com")))
    assert sb.check_network("api.github.com") == "api.github.com"
    assert sb.check_network("hackerone.com") == "hackerone.com"

    with pytest.raises(SandboxViolation) as exc_info:
        sb.check_network("unauthorized.com")
    assert "network host not allowed" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 9. Healthy path unchanged for normal short run
# --------------------------------------------------------------------------- #


def test_healthy_path_unchanged_for_normal_run(tmp_path: Path) -> None:
    app = create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
    )
    with TestClient(app) as client:
        submit = client.post("/tasks", json={"input": "research best practices in rust"})
        assert submit.status_code == 200
        payload = submit.json()
        assert payload["status"] == "accepted"
        task_id = payload["task_id"]

        detail = client.get(f"/tasks/{task_id}").json()
        assert detail["status"] in ("completed", "failed")
        assert detail["task_id"] == task_id
        assert isinstance(detail["artifacts"], list)
