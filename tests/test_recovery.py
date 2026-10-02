"""Error recovery tests (Master section 35).

Pure in-process tests of the classifier and the decision-only recovery policy.
"""

from __future__ import annotations

import asyncio

from uap.recovery import (
    ErrorClass,
    ErrorClassifier,
    RecoveryAction,
    RecoveryPolicy,
)


# --------------------------------------------------------------------------- #
# Classification (1-5)
# --------------------------------------------------------------------------- #

def test_classify_timeout_is_transient():
    classifier = ErrorClassifier()
    assert classifier.classify(TimeoutError("boom")) is ErrorClass.TRANSIENT
    assert classifier.classify(asyncio.TimeoutError()) is ErrorClass.TRANSIENT
    assert classifier.classify(ConnectionError("reset")) is ErrorClass.TRANSIENT
    assert classifier.classify("request timed out") is ErrorClass.TRANSIENT


def test_classify_policy_denial_is_policy():
    classifier = ErrorClassifier()
    assert classifier.classify("SandboxViolation: access denied") is ErrorClass.POLICY
    assert classifier.classify("operation forbidden") is ErrorClass.POLICY
    assert classifier.classify("that is not allowed") is ErrorClass.POLICY
    assert classifier.classify("request blocked by guardrail") is ErrorClass.POLICY


def test_classify_memory_is_resource():
    classifier = ErrorClassifier()
    assert classifier.classify(MemoryError()) is ErrorClass.RESOURCE
    assert classifier.classify("token budget exhausted") is ErrorClass.RESOURCE
    assert classifier.classify("quota reached") is ErrorClass.RESOURCE


def test_classify_valueerror_is_permanent():
    classifier = ErrorClassifier()
    assert classifier.classify(ValueError("bad")) is ErrorClass.PERMANENT
    assert classifier.classify(TypeError("bad")) is ErrorClass.PERMANENT
    assert classifier.classify(KeyError("missing")) is ErrorClass.PERMANENT
    assert classifier.classify("unknown tool: httpx") is ErrorClass.PERMANENT
    assert classifier.classify("resource not found") is ErrorClass.PERMANENT


def test_classify_unknown_is_unknown():
    classifier = ErrorClassifier()
    assert classifier.classify(RuntimeError("weird")) is ErrorClass.UNKNOWN
    assert classifier.classify("something unexpected happened") is ErrorClass.UNKNOWN


# --------------------------------------------------------------------------- #
# Policy decisions (6-13)
# --------------------------------------------------------------------------- #

def test_transient_retries_with_growing_delay():
    policy = RecoveryPolicy(max_retries=3, base_delay_s=0.1, max_delay_s=100.0)
    delays = []
    for attempt in (0, 1, 2):
        decision = policy.decide(ErrorClass.TRANSIENT, attempt=attempt)
        assert decision.action is RecoveryAction.RETRY
        delays.append(decision.delay_s)
    # base * 2**attempt: 0.1, 0.2, 0.4 - strictly growing.
    assert delays == [0.1, 0.2, 0.4]
    assert delays[0] < delays[1] < delays[2]


def test_transient_exhausted_with_fallback():
    policy = RecoveryPolicy(max_retries=2)
    decision = policy.decide(ErrorClass.TRANSIENT, attempt=2, has_fallback=True)
    assert decision.action is RecoveryAction.FALLBACK


def test_transient_exhausted_without_fallback_fails():
    policy = RecoveryPolicy(max_retries=2)
    decision = policy.decide(ErrorClass.TRANSIENT, attempt=2, has_fallback=False)
    assert decision.action is RecoveryAction.FAIL


def test_resource_asks():
    policy = RecoveryPolicy()
    decision = policy.decide(ErrorClass.RESOURCE, attempt=0)
    assert decision.action is RecoveryAction.ASK


def test_policy_always_fails_regardless_of_attempt():
    policy = RecoveryPolicy(max_retries=5)
    # attempt is ignored for POLICY: a denial is never retried.
    for attempt in (0, 1, 99):
        decision = policy.decide(ErrorClass.POLICY, attempt=attempt)
        assert decision.action is RecoveryAction.FAIL
        assert decision.delay_s == 0.0


def test_permanent_reroutes_or_fails():
    policy = RecoveryPolicy()
    with_reroute = policy.decide(ErrorClass.PERMANENT, attempt=0, has_reroute=True)
    assert with_reroute.action is RecoveryAction.REROUTE
    without = policy.decide(ErrorClass.PERMANENT, attempt=0, has_reroute=False)
    assert without.action is RecoveryAction.FAIL


def test_unknown_asks():
    policy = RecoveryPolicy()
    decision = policy.decide(ErrorClass.UNKNOWN, attempt=0)
    assert decision.action is RecoveryAction.ASK


def test_backoff_capped_at_max_delay():
    policy = RecoveryPolicy(max_retries=10, base_delay_s=1.0, max_delay_s=2.0)
    # 1 * 2**5 = 32, capped at 2.0.
    decision = policy.decide(ErrorClass.TRANSIENT, attempt=5)
    assert decision.action is RecoveryAction.RETRY
    assert decision.delay_s == 2.0


# --------------------------------------------------------------------------- #
# End-to-end: classifier feeds the policy
# --------------------------------------------------------------------------- #

def test_classifier_feeds_policy():
    classifier = ErrorClassifier()
    policy = RecoveryPolicy(max_retries=1)
    error_class = classifier.classify(ConnectionError("connection reset"))
    first = policy.decide(error_class, attempt=0)
    assert first.action is RecoveryAction.RETRY
    exhausted = policy.decide(error_class, attempt=1, has_fallback=True)
    assert exhausted.action is RecoveryAction.FALLBACK
