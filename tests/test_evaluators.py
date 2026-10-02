"""Tests for versioned evaluator entities + runner (Master section 47)."""

from __future__ import annotations

import pytest

from uap.evaluators import (
    EvaluationRecord,
    EvaluatorRegistry,
    EvaluatorRunner,
    EvaluatorSpec,
)


def _registry() -> EvaluatorRegistry:
    return EvaluatorRegistry()


def test_add_versions_and_resolve_exact() -> None:
    reg = _registry()
    v1 = reg.add("quality", [{"name": "c", "kind": "has_field", "params": {"field": "x"}}])
    assert v1.version == 1
    v2 = reg.add("quality", [{"name": "c", "kind": "non_empty", "params": {"field": "x"}}])
    assert v2.version == 2
    assert reg.resolve_ref("quality@v1").version == 1
    assert reg.resolve_ref("quality@v2").version == 2
    assert reg.resolve_ref("quality@v2").criteria[0]["kind"] == "non_empty"


def test_resolve_ref_unknown_raises_keyerror_listing_refs() -> None:
    reg = _registry()
    reg.add("quality", [{"name": "c", "kind": "has_field", "params": {"field": "x"}}])
    with pytest.raises(KeyError) as exc:
        reg.resolve_ref("missing@v9")
    assert "quality@v1" in str(exc.value)


def test_latest_returns_newest() -> None:
    reg = _registry()
    reg.add("q", [{"name": "a", "kind": "has_field", "params": {"field": "x"}}])
    reg.add("q", [{"name": "b", "kind": "has_field", "params": {"field": "y"}}])
    latest = reg.latest("q")
    assert latest.version == 2
    assert latest.criteria[0]["params"]["field"] == "y"
    # Bare-name resolve also yields latest.
    assert reg.resolve_ref("q").version == 2


def test_runner_min_count_pass_and_fail() -> None:
    reg = _registry()
    reg.add("mc", [{"name": "min", "kind": "min_count", "params": {"field": "items", "n": 2}}])
    runner = EvaluatorRunner(reg)
    ok = runner.run("mc@v1", {"items": [1, 2, 3]})
    assert ok.result.passed is True
    bad = runner.run("mc@v1", {"items": [1]})
    assert bad.result.passed is False


def test_runner_non_empty_fails_on_empty() -> None:
    reg = _registry()
    reg.add("ne", [{"name": "ne", "kind": "non_empty", "params": {"field": "s"}}])
    runner = EvaluatorRunner(reg)
    assert runner.run("ne@v1", {"s": "hello"}).result.passed is True
    assert runner.run("ne@v1", {"s": "   "}).result.passed is False
    assert runner.run("ne@v1", {"s": []}).result.passed is False


def test_runner_has_field() -> None:
    reg = _registry()
    reg.add("hf", [{"name": "hf", "kind": "has_field", "params": {"field": "k"}}])
    runner = EvaluatorRunner(reg)
    # Present even when the value is None.
    assert runner.run("hf@v1", {"k": None}).result.passed is True
    rec = runner.run("hf@v1", {"other": 1})
    assert rec.result.passed is False
    assert "missing field k" in rec.result.criteria[0].evidence


def test_runner_regex_match() -> None:
    reg = _registry()
    reg.add("rx", [{"name": "rx", "kind": "regex_match", "params": {"field": "email", "pattern": r"@"}}])
    runner = EvaluatorRunner(reg)
    assert runner.run("rx@v1", {"email": "a@b.com"}).result.passed is True
    assert runner.run("rx@v1", {"email": "nope"}).result.passed is False


def test_runner_threshold_numeric() -> None:
    reg = _registry()
    reg.add("th", [{"name": "th", "kind": "threshold", "params": {"field": "score", "op": "ge", "value": 0.8}}])
    runner = EvaluatorRunner(reg)
    assert runner.run("th@v1", {"score": 0.9}).result.passed is True
    assert runner.run("th@v1", {"score": 0.5}).result.passed is False
    # Non-numeric comparison fails rather than crashing.
    assert runner.run("th@v1", {"score": "high"}).result.passed is False


def test_missing_field_fails_with_evidence_no_crash() -> None:
    reg = _registry()
    reg.add("mc", [{"name": "min", "kind": "min_count", "params": {"field": "items", "n": 1}}])
    runner = EvaluatorRunner(reg)
    rec = runner.run("mc@v1", {})
    assert rec.result.passed is False
    assert rec.result.criteria[0].evidence == "missing field items"


def test_run_all_filters_by_target_kind() -> None:
    reg = _registry()
    reg.add("a", [{"name": "a", "kind": "has_field", "params": {"field": "x"}}], target_kind="artifact")
    reg.add("e", [{"name": "e", "kind": "has_field", "params": {"field": "x"}}], target_kind="execution")
    runner = EvaluatorRunner(reg)
    artifact_only = runner.run_all({"x": 1}, target_kind="artifact")
    assert [r.evaluator_ref for r in artifact_only] == ["a@v1"]
    everything = runner.run_all({"x": 1})
    assert {r.evaluator_ref for r in everything} == {"a@v1", "e@v1"}


def test_evaluation_record_round_trips_json() -> None:
    reg = _registry()
    reg.add("hf", [{"name": "hf", "kind": "has_field", "params": {"field": "k"}}])
    runner = EvaluatorRunner(reg)
    rec = runner.run("hf@v1", {"k": 1, "artifact_id": "art-1"})
    dumped = rec.model_dump_json()
    restored = EvaluationRecord.model_validate_json(dumped)
    assert restored == rec
    assert restored.subject_ref == "art-1"


def test_verifier_name_is_exact_ref() -> None:
    reg = _registry()
    reg.add("v", [{"name": "c", "kind": "has_field", "params": {"field": "x"}}])
    reg.add("v", [{"name": "c", "kind": "has_field", "params": {"field": "x"}}])
    runner = EvaluatorRunner(reg)
    # Bare-name run still records the resolved exact ref.
    rec = runner.run("v", {"x": 1})
    assert rec.result.verifier == "v@v2"
    assert rec.evaluator_ref == "v@v2"


def test_all_criteria_pass_sets_passed_true() -> None:
    reg = _registry()
    reg.add(
        "multi",
        [
            {"name": "has", "kind": "has_field", "params": {"field": "x"}},
            {"name": "ne", "kind": "non_empty", "params": {"field": "x"}},
            {"name": "th", "kind": "threshold", "params": {"field": "n", "op": "gt", "value": 0}},
        ],
    )
    runner = EvaluatorRunner(reg)
    rec = runner.run("multi@v1", {"x": "ok", "n": 5})
    assert rec.result.passed is True
    assert rec.result.score == 1.0
    assert all(c.passed for c in rec.result.criteria)


def test_evaluator_spec_forbids_extra_fields() -> None:
    with pytest.raises(Exception):
        EvaluatorSpec(name="x", criteria=[], bogus=1)
