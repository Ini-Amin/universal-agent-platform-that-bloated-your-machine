"""Tests for ASD-STE100 controlled-language style instruction in Learning workflow prompts.

Based on Andrej Karpathy's writing advice (October 2026):
"Ask your LLM to explain something in ASD-STE100, it's a controlled language
specification originally developed for aerospace maintenance documentation.
LLMs well-versed in this language and it comes with heavy constraints on clean
writing style that I often find a lot more readable. Sometimes I've tried to
soften it a bit e.g. ask for '80% of the way to ASD-STE100' because the spec
is quite stringent."

ASD-STE100 properties enforced in prompt text:
1. One idea per sentence
2. Short sentences, active voice
3. One term for one thing, used consistently
4. No ambiguous pronouns ('it', 'this') when the referent could be unclear
5. Simple common words, no idioms or metaphors
6. Instructions as direct commands
"""

from __future__ import annotations

import pytest

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.workflows.learning import (
    ASD_STE100_STYLE_INSTRUCTION,
    HONESTY_BANNER,
    LESSON_PROMPT_TEMPLATE,
    SUMMARY_PROMPT_TEMPLATE,
    LearningWorkflow,
    compose_lesson_prompt,
    compose_summary_prompt,
)


def test_lesson_prompt_carries_asd_ste100_style_instruction() -> None:
    # PROXY check: This test asserts that the composed prompt text carries
    # the ASD-STE100 style instruction and constraints. It proves the instruction
    # is sent in the prompt, not that the model obeyed it.
    prompt = compose_lesson_prompt(
        topic="backend development",
        step={"title": "HTTP Fundamentals", "goal": "Understand HTTP messages", "finish_line": "GET /hello returns 200"},
        current_level="beginner",
        target_level="intermediate",
    )

    assert "ASD-STE100" in prompt
    assert "80%" in prompt
    # Verify all explicit ASD-STE100 style properties are present in prompt text
    assert "one idea per sentence" in prompt.lower()
    assert "short" in prompt.lower() and "active voice" in prompt.lower()
    assert "one term for one thing" in prompt.lower()
    assert "ambiguous pronouns" in prompt.lower()
    assert "simple, common words" in prompt.lower()
    assert "idioms" in prompt.lower()
    assert "direct commands" in prompt.lower()


def test_summary_prompt_carries_asd_ste100_style_instruction() -> None:
    # PROXY check: This test asserts that the composed summary prompt carries
    # the ASD-STE100 style instruction and constraints. It proves the instruction
    # is sent in the prompt, not that the model obeyed it.
    prompt = compose_summary_prompt(
        topic="backend development",
        step={"title": "HTTP Fundamentals", "goal": "Understand HTTP messages"},
        current_level="beginner",
        target_level="intermediate",
    )

    assert "ASD-STE100" in prompt
    assert "80%" in prompt
    assert "one idea per sentence" in prompt.lower()
    assert "short" in prompt.lower() and "active voice" in prompt.lower()
    assert "one term for one thing" in prompt.lower()
    assert "ambiguous pronouns" in prompt.lower()
    assert "simple, common words" in prompt.lower()
    assert "direct commands" in prompt.lower()


@pytest.mark.asyncio
async def test_workflow_execution_carries_ste100_prompts_in_state() -> None:
    # PROXY check: Verifies that running the workflow generates and stores
    # composed prompts carrying the ASD-STE100 instruction in state.
    wf = LearningWorkflow()
    task = TaskSpec(
        domain=Domain.LEARNING,
        goal="learn backend development from scratch to intermediate",
    )
    result = await wf.run(task)
    assert result.status == WorkflowStatus.COMPLETED

    state = wf.runner.state_store.load(task.task_id)
    assert state is not None

    lesson_prompt = state.data.get("lesson_prompt", "")
    summary_prompt = state.data.get("summary_prompt", "")

    assert lesson_prompt, "state.data should contain composed lesson_prompt"
    assert summary_prompt, "state.data should contain composed summary_prompt"

    assert "ASD-STE100" in lesson_prompt
    assert "80%" in lesson_prompt
    assert "ASD-STE100" in summary_prompt
    assert "80%" in summary_prompt


def test_honesty_requirement_in_prompt_does_not_weaken_banner() -> None:
    # PROXY check: Verifies simulation honesty requirement is preserved in prompts
    # when collectors are stubs, and omitted when sources are real.
    simulated_prompt = compose_lesson_prompt(
        topic="backend development",
        simulated=True,
    )
    assert HONESTY_BANNER in simulated_prompt

    real_prompt = compose_lesson_prompt(
        topic="backend development",
        simulated=False,
    )
    assert HONESTY_BANNER not in real_prompt


@pytest.mark.asyncio
async def test_synthesizer_callable_receives_ste100_prompt() -> None:
    # PROXY check: Verifies that an attached synthesizer receives the composed
    # prompt containing the ASD-STE100 style instruction.
    received_prompts: list[str] = []

    async def spy_synthesizer(prompt: str, evidence: list) -> str:
        received_prompts.append(prompt)
        return "Synthesized explanation in clear ASD-STE100 style."

    wf = LearningWorkflow(synthesizer=spy_synthesizer)
    task = TaskSpec(
        domain=Domain.LEARNING,
        goal="learn backend development from scratch to intermediate",
    )
    result = await wf.run(task)
    assert result.status == WorkflowStatus.COMPLETED

    assert len(received_prompts) >= 1
    assert "ASD-STE100" in received_prompts[0]
    assert "80%" in received_prompts[0]
    assert "one idea per sentence" in received_prompts[0].lower()
