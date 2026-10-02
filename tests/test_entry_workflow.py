"""Tests for the Entry Workflow (Master sections 3 and 4)."""

import pytest

from uap.contracts.models import (
    Domain,
    TaskMode,
    TaskSpec,
    UserRequest,
)
from uap.entry import (
    DeterministicIntentAnalyzer,
    EntryOutcome,
    EntryWorkflow,
    IntentAnalyzer,
    IntentResult,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run(text: str, **kwargs) -> EntryOutcome:
    return EntryWorkflow(**kwargs).run(UserRequest(raw_input=text))


# --------------------------------------------------------------------------- #
# 1-4: Domain classification (Indonesian + English)
# --------------------------------------------------------------------------- #


def test_indonesian_learning_request():
    outcome = run("Ajari saya subnetting dari dasar")

    assert outcome.needs_clarification is False
    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.LEARNING
    assert "subnetting" in outcome.spec.goal
    assert outcome.spec.constraints["difficulty"] == "beginner"
    assert outcome.spec.constraints["language"] == "id"


def test_english_research_request():
    outcome = run("Research the performance trade-offs between Python web frameworks")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.RESEARCH
    assert outcome.spec.constraints["language"] == "en"


def test_bbp_request():
    outcome = run("Bug bounty recon and vulnerability scanning on example.com")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.BBP


def test_coding_request():
    outcome = run("Refactor the authentication module and fix the login bug")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.CODING


def test_data_request_indonesian():
    outcome = run("Analisis dataset penjualan untuk dasbor")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.DATA


# --------------------------------------------------------------------------- #
# 5-7: Clarification and the round cap
# --------------------------------------------------------------------------- #


def test_empty_input_requests_clarification():
    outcome = run("")

    assert outcome.needs_clarification is True
    assert outcome.spec is None
    assert outcome.question
    assert outcome.stage_log[-1] == "clarification"


def test_garbage_input_requests_clarification():
    # No domain keyword matches, so the workflow asks rather than guessing.
    outcome = run("asdfgh")

    assert outcome.needs_clarification is True
    assert outcome.spec is None
    assert outcome.question


def test_clarification_round_cap_falls_back_to_unknown():
    workflow = EntryWorkflow(max_clarification_rounds=1)
    request = UserRequest(raw_input="asdfgh")

    first = workflow.run(request)
    assert first.needs_clarification is True

    second = workflow.run(request, clarification="qwerty zxcv")

    # Cap reached: a best-effort spec is compiled instead of looping.
    assert second.needs_clarification is False
    assert second.spec is not None
    assert second.spec.domain is Domain.UNKNOWN
    assert second.spec.goal  # never empty
    assert second.stage_log[-1] == "task_compiler"


def test_clarification_answer_resolves_into_spec():
    workflow = EntryWorkflow()
    request = UserRequest(raw_input="")

    first = workflow.run(request)
    assert first.needs_clarification is True

    second = workflow.run(request, clarification="Ajari saya belajar python dari dasar")

    assert second.needs_clarification is False
    assert second.spec is not None
    assert second.spec.domain is Domain.LEARNING


def test_default_max_clarification_rounds_is_three():
    assert EntryWorkflow().max_clarification_rounds == 3


# --------------------------------------------------------------------------- #
# 8: Analyzer injection
# --------------------------------------------------------------------------- #


class RecordingAnalyzer:
    """Minimal duck-typed IntentAnalyzer."""

    def __init__(self):
        self.calls = 0
        self.seen: list[UserRequest] = []

    def analyze(self, request: UserRequest) -> IntentResult:
        self.calls += 1
        self.seen.append(request)
        return IntentResult(
            domain=Domain.DATA,
            goal="ingested telemetry",
            constraints={"language": "en", "difficulty": "advanced"},
            confidence=0.9,
        )


def test_injected_analyzer_is_used():
    analyzer = RecordingAnalyzer()
    workflow = EntryWorkflow(intent_analyzer=analyzer)

    outcome = workflow.run(UserRequest(raw_input="anything at all"))

    assert analyzer.calls == 1
    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.DATA
    assert outcome.spec.goal == "ingested telemetry"
    # The injected analyzer decides the domain even without any keyword match.
    assert outcome.needs_clarification is False


def test_intent_analyzer_protocol_is_runtime_checkable():
    assert isinstance(RecordingAnalyzer(), IntentAnalyzer)
    assert not isinstance(object(), IntentAnalyzer)


def test_default_analyzer_is_deterministic():
    analyzer = DeterministicIntentAnalyzer()
    request = UserRequest(raw_input="Ajari saya subnetting dari dasar")
    assert analyzer.analyze(request) == analyzer.analyze(request)


# --------------------------------------------------------------------------- #
# 9: Stage log
# --------------------------------------------------------------------------- #


def test_stage_log_records_pipeline_in_order():
    outcome = run("Ajari saya subnetting dari dasar")

    assert outcome.stage_log == [
        "intake",
        "intent_analysis",
        "context_sufficiency",
        "constraint_check",
        "task_compiler",
    ]


def test_stage_log_marks_clarification_branch():
    outcome = run("")

    assert outcome.stage_log[:3] == ["intake", "intent_analysis", "context_sufficiency"]
    assert "clarification" in outcome.stage_log
    assert "task_compiler" not in outcome.stage_log


# --------------------------------------------------------------------------- #
# 10: TaskSpec serialisation round-trip
# --------------------------------------------------------------------------- #


def test_task_spec_round_trips_through_json():
    outcome = run("Ajari saya subnetting dari dasar")
    assert outcome.spec is not None

    restored = TaskSpec.model_validate_json(outcome.spec.model_dump_json())

    assert restored == outcome.spec
    assert restored.domain is Domain.LEARNING
    assert restored.task_id == outcome.spec.task_id


# --------------------------------------------------------------------------- #
# 11: User supplied constraints
# --------------------------------------------------------------------------- #


def test_user_constraints_are_preserved():
    request = UserRequest(
        raw_input="Teach me python basics",
        metadata={"constraints": {"audience": "tim dev", "max_examples": 5}},
    )
    outcome = EntryWorkflow().run(request)
    assert outcome.spec is not None

    constraints = outcome.spec.constraints
    assert constraints["audience"] == "tim dev"
    assert constraints["max_examples"] == 5
    # Detected constraints are still present alongside the user supplied ones.
    assert constraints["language"] == "en"
    assert constraints["difficulty"] == "beginner"


def test_user_constraints_win_over_detected_ones():
    request = UserRequest(
        raw_input="Ajari saya subnetting dari dasar",
        metadata={"constraints": {"difficulty": "advanced", "language": "en"}},
    )
    outcome = EntryWorkflow().run(request)
    assert outcome.spec is not None

    assert outcome.spec.constraints["difficulty"] == "advanced"
    assert outcome.spec.constraints["language"] == "en"


def test_user_supplied_mode_and_verification_move_to_spec_fields():
    request = UserRequest(
        raw_input="Ajari saya subnetting dari dasar",
        metadata={"constraints": {"mode": "autonomous", "verification": False}},
    )
    outcome = EntryWorkflow().run(request)
    assert outcome.spec is not None

    assert outcome.spec.mode is TaskMode.AUTONOMOUS
    assert outcome.spec.verification is False
    # Mode/verification are TaskSpec fields, not duplicated in constraints.
    assert "mode" not in outcome.spec.constraints
    assert "verification" not in outcome.spec.constraints


# --------------------------------------------------------------------------- #
# Extra: compiled TaskSpec shape
# --------------------------------------------------------------------------- #


def test_compiled_task_spec_carries_raw_input():
    request = UserRequest(raw_input="Ajari saya subnetting dari dasar")
    outcome = EntryWorkflow().run(request)
    assert outcome.spec is not None

    assert outcome.spec.input["raw"] == "Ajari saya subnetting dari dasar"
    assert outcome.spec.verification is True
    assert outcome.spec.mode is TaskMode.INTERACTIVE
    assert outcome.spec.task_id  # uuid assigned


def test_autonomous_mode_detected_from_keywords():
    outcome = run("Implementasikan fungsi login secara otomatis tanpa interaksi")
    assert outcome.spec is not None
    assert outcome.spec.mode is TaskMode.AUTONOMOUS


def test_entry_outcome_defaults():
    outcome = EntryOutcome()
    assert outcome.spec is None
    assert outcome.needs_clarification is False
    assert outcome.question is None
    assert outcome.stage_log == []


# --------------------------------------------------------------------------- #
# 12-23: Target extraction, scope parsing and the BBP sufficiency rule
# --------------------------------------------------------------------------- #

def test_bug_bounty_extracts_single_target():
    outcome = run("Bug bounty recon and vulnerability scanning on example.com")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.BBP
    assert outcome.spec.input["targets"] == ["example.com"]


def test_url_with_scheme_port_and_path_normalizes():
    outcome = run("Pentest https://api.example.com:8443/v1 for issues")

    assert outcome.spec is not None
    assert outcome.spec.input["targets"] == ["api.example.com"]


def test_multiple_targets_deduplicate_and_preserve_order():
    outcome = run("Bug bounty on example.com and www.example.com and example.com")

    assert outcome.spec is not None
    assert outcome.spec.input["targets"] == ["example.com", "www.example.com"]


def test_no_targets_means_no_targets_key():
    outcome = run("Refactor the authentication module and fix the login bug")

    assert outcome.spec is not None
    assert "targets" not in outcome.spec.input
    assert outcome.spec.input == {
        "raw": "Refactor the authentication module and fix the login bug"
    }


def test_email_domain_is_not_extracted_as_target():
    # The domain of an email address must never be lifted out as a target.
    analyzer = DeterministicIntentAnalyzer()
    intent = analyzer.analyze(UserRequest(raw_input="contact security@example.com"))
    assert intent.targets == []

    # ... and a BBP request whose only "host" is an email has no target.
    outcome = run("Bug bounty: contact security@example.com")
    assert outcome.needs_clarification is True
    assert outcome.spec is None


def test_version_number_is_not_extracted_as_target():
    outcome = run("Refactor the code and upgrade to v3.8 next")

    assert outcome.spec is not None
    assert "targets" not in outcome.spec.input


def test_scope_declarations_are_parsed_for_bbp():
    outcome = run(
        "Bug bounty on example.com. In scope: *.example.com, api.example.com. "
        "Out of scope: legacy.example.com"
    )

    assert outcome.spec is not None
    assert outcome.spec.constraints["in_scope"] == ["*.example.com", "api.example.com"]
    assert outcome.spec.constraints["out_of_scope"] == ["legacy.example.com"]


def test_reproduced_misclassification_now_classifies_as_bbp():
    outcome = run(
        "Always prioritize authorization, scope compliance, reproducibility, "
        "safety, and evidence. Target: api.example.com"
    )

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.BBP
    assert outcome.spec.input["targets"] == ["api.example.com"]


def test_bbp_without_target_or_scope_asks_for_a_target():
    outcome = run("I need a bug bounty assessment")

    assert outcome.needs_clarification is True
    assert outcome.spec is None
    assert outcome.question is not None
    assert "target" in outcome.question.lower()


def test_bbp_with_target_does_not_require_scope_at_entry():
    outcome = run("Bug bounty recon on example.com")

    assert outcome.needs_clarification is False
    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.BBP
    assert "in_scope" not in outcome.spec.constraints


def test_scope_phrases_are_ignored_outside_bbp():
    outcome = run("Teach me subnetting from scratch. Scope: basics")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.LEARNING
    assert "in_scope" not in outcome.spec.constraints
    assert "out_of_scope" not in outcome.spec.constraints


def test_master_section_three_example_is_preserved():
    outcome = run("Ajari saya subnetting dari dasar")

    assert outcome.spec is not None
    assert outcome.spec.domain is Domain.LEARNING
    assert "subnetting" in outcome.spec.goal
    assert outcome.spec.constraints["difficulty"] == "beginner"
    assert outcome.spec.constraints["language"] == "id"
    assert outcome.spec.task_id  # uuid assigned


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
