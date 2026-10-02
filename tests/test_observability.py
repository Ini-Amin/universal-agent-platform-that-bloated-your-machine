"""Tests for build Step 15 - Observability (Master section 22).

Self-contained and deterministic: no sleeps, no network, no threads. The
JsonlSink tests use pytest's ``tmp_path`` so nothing escapes the sandbox.
"""

import json

import pytest

from uap.observability import (
    EventBus,
    EventKind,
    JsonlSink,
    MemorySink,
    ObsEvent,
    Tracer,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_event(**overrides) -> ObsEvent:
    fields = {"kind": EventKind.TASK_STARTED, "task_id": "t1"}
    fields.update(overrides)
    return ObsEvent(**fields)


# --------------------------------------------------------------------------- #
# EventBus fan-out and isolation
# --------------------------------------------------------------------------- #

def test_emit_reaches_memory_sink():
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    event = make_event()
    bus.emit(event)

    assert sink.events == [event]


def test_multiple_sinks_all_receive():
    bus = EventBus()
    first = MemorySink()
    second = MemorySink()
    bus.subscribe(first)
    bus.subscribe(second)

    event = make_event()
    bus.emit(event)

    assert first.events == [event]
    assert second.events == [event]


def test_failing_sink_is_isolated_and_others_still_called():
    bus = EventBus()
    seen: list[ObsEvent] = []

    def boom(_event: ObsEvent) -> None:
        raise RuntimeError("sink exploded")

    bus.subscribe(boom)
    bus.subscribe(seen.append)

    event = make_event()
    bus.emit(event)  # must not raise

    assert seen == [event]
    assert len(bus.errors) == 1
    assert "sink exploded" in bus.errors[0]
    assert "RuntimeError" in bus.errors[0]


# --------------------------------------------------------------------------- #
# JsonlSink
# --------------------------------------------------------------------------- #

def test_jsonl_sink_writes_parseable_json_line(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlSink(path)

    event = make_event(kind=EventKind.TOOL_CALL, agent="researcher", model="m1")
    sink(event)

    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    record = json.loads(raw.strip())
    assert record["event_id"] == event.event_id
    assert record["kind"] == "tool_call"
    assert record["agent"] == "researcher"
    assert record["model"] == "m1"


def test_jsonl_sink_appends_across_emits(tmp_path):
    path = tmp_path / "events.jsonl"
    bus = EventBus()
    bus.subscribe(JsonlSink(path))

    bus.emit(make_event(kind=EventKind.NODE_STARTED, node="a"))
    bus.emit(make_event(kind=EventKind.NODE_FINISHED, node="b"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["node"] for line in lines] == ["a", "b"]


def test_jsonl_sink_creates_nested_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "tree" / "events.jsonl"
    assert not path.parent.exists()

    JsonlSink(path)(make_event())

    assert path.exists()
    assert path.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# MemorySink queries
# --------------------------------------------------------------------------- #

def test_memory_sink_query_filters_by_kind():
    sink = MemorySink()
    sink(make_event(kind=EventKind.NODE_STARTED, node="a"))
    sink(make_event(kind=EventKind.ERROR, node="b"))
    sink(make_event(kind=EventKind.NODE_STARTED, node="c"))

    started = sink.query(kind=EventKind.NODE_STARTED)
    assert [event.node for event in started] == ["a", "c"]

    errors = sink.query(kind=EventKind.ERROR)
    assert [event.node for event in errors] == ["b"]


def test_memory_sink_query_filters_by_task_id():
    sink = MemorySink()
    sink(make_event(task_id="t1", node="a"))
    sink(make_event(task_id="t2", node="b"))
    sink(make_event(task_id="t1", node="c"))

    only_t1 = sink.query(task_id="t1")
    assert [event.node for event in only_t1] == ["a", "c"]

    both = sink.query(kind=EventKind.TASK_STARTED, task_id="t1")
    assert [event.node for event in both] == ["a", "c"]


# --------------------------------------------------------------------------- #
# Tracer
# --------------------------------------------------------------------------- #

def test_tracer_emits_finish_event_with_duration():
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    with Tracer(bus, EventKind.NODE_STARTED, task_id="t1", node="plan"):
        pass

    kinds = [event.kind for event in sink.events]
    assert kinds == [EventKind.NODE_STARTED, EventKind.NODE_FINISHED]

    finished = sink.events[-1]
    assert finished.node == "plan"
    assert finished.duration_ms is not None
    assert finished.duration_ms >= 0.0


def test_tracer_on_exception_emits_error_and_reraises():
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    original = ValueError("node blew up")
    with pytest.raises(ValueError) as excinfo:
        with Tracer(bus, EventKind.NODE_STARTED, task_id="t1", node="boom"):
            raise original

    assert excinfo.value is original

    error_events = sink.query(kind=EventKind.ERROR)
    assert len(error_events) == 1
    assert error_events[0].error is not None
    assert "node blew up" in error_events[0].error
    assert error_events[0].duration_ms is not None
    assert error_events[0].duration_ms >= 0.0


# --------------------------------------------------------------------------- #
# ObsEvent / EventBus conveniences
# --------------------------------------------------------------------------- #

def test_obs_event_json_round_trip():
    event = make_event(
        kind=EventKind.AGENT_RUN,
        workflow="research",
        node="gather",
        agent="researcher",
        model="cbai/deepseek-v4.1-flash",
        tokens_in=120,
        tokens_out=42,
        data={"evidence": ["src1", "src2"]},
    )

    encoded = event.model_dump_json()
    restored = ObsEvent.model_validate_json(encoded)

    assert restored == event
    assert restored.ts == event.ts
    assert restored.data == {"evidence": ["src1", "src2"]}


def test_emit_kind_builds_and_emits_event():
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    returned = bus.emit_kind(
        EventKind.RETRY,
        task_id="t1",
        node="fetch",
        error="timeout",
    )

    assert returned.kind is EventKind.RETRY
    assert returned.task_id == "t1"
    assert returned.node == "fetch"
    assert returned.error == "timeout"
    assert sink.events == [returned]


def test_event_ids_unique_across_100_emits():
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    for index in range(100):
        bus.emit_kind(EventKind.AGENT_RUN, task_id=f"t{index}")

    ids = [event.event_id for event in sink.events]
    assert len(ids) == 100
    assert len(set(ids)) == 100
