"""Learning Workflow — interactive learning with code exercises, lessons, and sources.

Linear pipeline with deterministic stages and real source collectors:

    goal_analysis -> curriculum_planning -> source_gathering -> exercise
        -> lesson -> sources -> synthesis -> review

Views produced (canvas-as-stage), following Astra's do -> understand -> reference
hierarchy and the explicit ruling against four redundant lesson representations:

1. ``exercise`` (code): FIRST — a ready-to-start runnable exercise with a clear
   finish line, so a beginner knows exactly what to type and when they are done.
2. ``lesson`` (markdown): the explanation — the task, the success condition, and
   just enough guidance (it ends with the takeaways; no separate summary card).
3. ``sources`` (markdown): references with real URLs, labeled honestly if stubs.

No ``video`` view is ever emitted without a real, verified, embeddable URL, and
no link is ever fabricated. When no usable video exists the explanation says so.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from uap.contracts.models import (
    Artifact,
    ArtifactStatus,
    NodeView,
    TaskSpec,
    VerificationCriterion,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
)
from uap.workflows.research import (
    CollectorResults,
    Evidence,
    docs_collector,
    stub_docs_collector,
    stub_web_collector,
    web_collector,
)
from uap.workflows.runner import NodeResult, StateStore, WorkflowRunner

log = logging.getLogger(__name__)

WORKFLOW_NAME = "learning"

NODES: tuple[str, ...] = (
    "goal_analysis",
    "curriculum_planning",
    "source_gathering",
    "exercise",
    "lesson",
    "sources",
    "synthesis",
    "review",
)

_ARTIFACT_SOURCE = "learning_workflow"

Collector = Callable[[str], Awaitable[list[Evidence]]]

HONESTY_BANNER = (
    "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**\n"
    "> This run used built-in deterministic stub collectors. The findings and\n"
    "> URLs below are FIXED SAMPLE DATA, not real web research. Configure real\n"
    "> collectors before treating any claim as verified."
)


# --------------------------------------------------------------------------- #
# Goal Analysis Helpers
# --------------------------------------------------------------------------- #


def _extract_goal_text(state: WorkflowState) -> str:
    """Extract raw user goal text from workflow state."""
    data = state.data
    task_dict = data.get("task") or {}
    if isinstance(task_dict, dict):
        goal = (
            task_dict.get("goal")
            or task_dict.get("input", {}).get("raw")
            or task_dict.get("input", {}).get("question")
        )
        if goal:
            return str(goal).strip()
    return str(data.get("goal") or data.get("question") or "").strip()


def _analyze_goal(goal_text: str) -> dict[str, Any]:
    """Parse topic, target level, current level, and coding orientation."""
    lower = goal_text.casefold()

    # Determine topic
    topic = goal_text
    prefixes = (
        "learn", "study", "belajar", "ajari saya", "pelajari", "teach me",
        "teach", "explain", "tutorial", "how to build", "how to develop", "how to",
    )
    for prefix in prefixes:
        if lower.startswith(prefix):
            topic = goal_text[len(prefix):].strip()
            lower_topic = topic.casefold()
            for phrase in (
                "from scratch to intermediate",
                "from scratch to advanced",
                "from scratch",
                "to intermediate",
                "to advanced",
                "from zero",
                "dari dasar ke menengah",
                "dari dasar",
            ):
                if phrase in lower_topic:
                    topic = lower_topic.replace(phrase, "").strip()
                    lower_topic = topic

    topic = topic.strip(" :-\t\r\n") or "backend development"

    # Determine levels
    if any(k in lower for k in ("scratch", "zero", "basics", "dasar", "pemula", "beginner")):
        current_level = "beginner"
        prior_knowledge = "Assumes no prior experience; start from foundational concepts."
    elif any(k in lower for k in ("intermediate", "menengah")):
        current_level = "intermediate"
        prior_knowledge = "Assumes basic programming knowledge."
    else:
        current_level = "beginner"
        prior_knowledge = "Assumes foundational curiosity; gentle ramp."

    if any(k in lower for k in ("intermediate", "menengah")):
        target_level = "intermediate"
    elif any(k in lower for k in ("advanced", "expert", "lanjutan", "mahir")):
        target_level = "advanced"
    else:
        target_level = "intermediate"

    code_keywords = (
        "backend", "frontend", "api", "code", "coding", "python", "javascript",
        "sql", "database", "fastapi", "django", "node", "programming", "server",
        "rest", "microservice", "docker", "develop",
    )
    is_code_topic = any(k in lower for k in code_keywords)

    return {
        "topic": topic,
        "current_level": current_level,
        "target_level": target_level,
        "prior_knowledge": prior_knowledge,
        "is_code_topic": is_code_topic,
    }


# --------------------------------------------------------------------------- #
# Curriculum Planning Helpers
# --------------------------------------------------------------------------- #


def _build_backend_curriculum(topic: str) -> list[dict[str, Any]]:
    """Tailored, actionable curriculum for backend development."""
    return [
        {
            "step_number": 1,
            "title": "HTTP Fundamentals & Request/Response Lifecycle",
            "time_estimate": "2-3 hours",
            "goal": "Understand how HTTP clients and servers exchange request and response messages.",
            "finish_line": 'Make your first API say hello — done when GET /hello returns {"message": "hello"}.',
            "exercise_code": (
                '"""Exercise 1: Your First HTTP Endpoint\n\n'
                'Finish line:\n'
                '  Run the verification check below — done when GET /hello\n'
                '  returns {"message": "hello"} with status 200.\n'
                '"""\n\n'
                'from fastapi import FastAPI\n'
                'from fastapi.testclient import TestClient\n\n'
                'app = FastAPI(title="My First Backend API")\n\n\n'
                '@app.get("/hello")\n'
                'def get_hello():\n'
                '    return {"message": "hello"}\n\n\n'
                '# --- Automated Verification Check ---\n'
                'def test_hello_endpoint():\n'
                '    client = TestClient(app)\n'
                '    response = client.get("/hello")\n'
                '    assert response.status_code == 200, f"Expected 200, got {response.status_code}"\n'
                '    assert response.json() == {"message": "hello"}, f"Unexpected body: {response.json()}"\n'
                '    print("PASS: /hello returned 200 and {\'message\': \'hello\'}")\n\n\n'
                'if __name__ == "__main__":\n'
                '    test_hello_endpoint()\n'
            ),
            "explanation": (
                "### Step 1: HTTP Fundamentals & Request/Response Lifecycle\n\n"
                "Every backend interaction begins with an **HTTP request** and ends with an "
                "**HTTP response**.\n\n"
                "#### 1. The Request Structure\n"
                "- **Method**: What action to take (`GET` to read, `POST` to create, `PUT`/`PATCH` to update, `DELETE` to remove).\n"
                "- **Path / URL**: The resource identifier (e.g. `/hello` or `/users/42`).\n"
                "- **Headers**: Metadata (e.g. `Accept: application/json`, `Authorization: Bearer ...`).\n"
                "- **Body**: Optional payload sent with `POST` or `PUT`.\n\n"
                "#### 2. The Response Structure\n"
                "- **Status Code**: Indicates outcome (`200 OK`, `201 Created`, `400 Bad Request`, `404 Not Found`, `500 Server Error`).\n"
                "- **Headers**: Metadata describing the content (e.g. `Content-Type: application/json`).\n"
                "- **Body**: The returned data payload.\n\n"
                "#### 3. Your Task\n"
                "Open the **Exercise** code view in your editor or IDE. The starter server defines a FastAPI app. "
                "Ensure the `/hello` route responds with the exact JSON object `{\"message\": \"hello\"}`. "
                "Run `test_hello_endpoint()` to verify completion.\n\n"
                "> **Note on video**: *Video view omitted because no verified, embeddable video lesson "
                "with accurate transcript is available for this topic. Following interactive code + explanation format.*"
            ),
            "summary": (
                "### Summary: Step 1 Takeaways\n\n"
                "1. **Stateless Protocol**: HTTP requests are independent. Each request contains all information needed to process it.\n"
                "2. **Methods Signal Intent**: `GET` must never mutate server state; mutations use `POST`, `PUT`, `PATCH`, or `DELETE`.\n"
                "3. **JSON is Standard**: Modern web APIs exchange structured JSON payloads with `Content-Type: application/json`.\n"
                "4. **Finish Line**: A verified 200 OK response returning `{\"message\": \"hello\"}`."
            ),
        },
        {
            "step_number": 2,
            "title": "RESTful Routing & Parameter Handling",
            "time_estimate": "3-4 hours",
            "goal": "Handle path parameters, query strings, and request bodies with structured validation.",
            "finish_line": 'Implement GET /items/{item_id}?format=short and POST /items with JSON schema validation.',
            "exercise_code": (
                '"""Exercise 2: RESTful Parameters & Validation\n\n'
                'Finish line: GET /items/{item_id} and POST /items work with Pydantic validation.\n'
                '"""\n\n'
                'from fastapi import FastAPI, HTTPException\n'
                'from pydantic import BaseModel\n\n'
                'app = FastAPI()\n\n'
                'class Item(BaseModel):\n'
                '    name: str\n'
                '    price: float\n\n'
                'db: dict[int, Item] = {}\n\n'
                '@app.post("/items", status_code=201)\n'
                'def create_item(item: Item):\n'
                '    item_id = len(db) + 1\n'
                '    db[item_id] = item\n'
                '    return {"id": item_id, **item.model_dump()}\n\n'
                '@app.get("/items/{item_id}")\n'
                'def get_item(item_id: int):\n'
                '    if item_id not in db:\n'
                '        raise HTTPException(status_code=404, detail="Item not found")\n'
                '    return {"id": item_id, **db[item_id].model_dump()}\n'
            ),
            "explanation": "Learn path parameters, query filtering, and request body serialization.",
            "summary": "Path params identify resources; query params filter/paginate; bodies carry entity data.",
        },
        {
            "step_number": 3,
            "title": "Relational Modeling & Database Persistence",
            "time_estimate": "4-5 hours",
            "goal": "Connect your backend to PostgreSQL/SQLite via an ORM and run migrations.",
            "finish_line": "Persist and query database rows with transactional consistency.",
            "exercise_code": "",
            "explanation": "Relational schemas, foreign keys, SQL queries, and ORM abstractions.",
            "summary": "ACID guarantees keep backend data consistent; indexes keep reads fast.",
        },
        {
            "step_number": 4,
            "title": "Authentication, Sessions & Password Hashing",
            "time_estimate": "4-6 hours",
            "goal": "Secure endpoints with salted hashes, JWT access tokens, and role-based permissions.",
            "finish_line": "Protected route rejects unauthenticated requests and permits authorized users.",
            "exercise_code": "",
            "explanation": "Cryptographic password hashing (Argon2/bcrypt), token issuance, and header auth.",
            "summary": "Never store plain passwords; verify signatures on every protected request.",
        },
        {
            "step_number": 5,
            "title": "Middleware, Error Handling & API Hardening",
            "time_estimate": "3-4 hours",
            "goal": "Global exception handlers, CORS headers, rate limiting, and structured logging.",
            "finish_line": "Server handles unexpected exceptions gracefully without leaking stack traces.",
            "exercise_code": "",
            "explanation": "Middleware intercepts the request pipeline for cross-cutting concerns.",
            "summary": "A hardened backend fails predictably with standardized JSON errors.",
        },
        {
            "step_number": 6,
            "title": "Automated Testing, Caching & Deployment",
            "time_estimate": "4-5 hours",
            "goal": "Unit/integration test suite, Redis caching, Docker containerization.",
            "finish_line": "Full test suite passes and service runs inside a deterministic container.",
            "exercise_code": "",
            "explanation": "Pytest fixtures, mock dependencies, cache invalidation, and container packaging.",
            "summary": "Automated testing and isolated deployment make backend systems production-ready.",
        },
    ]


def _build_generic_curriculum(topic: str) -> list[dict[str, Any]]:
    """Curriculum for non-backend topics."""
    return [
        {
            "step_number": 1,
            "title": f"Foundational Concepts of {topic.title()}",
            "time_estimate": "2-3 hours",
            "goal": f"Master the core terminology, principles, and architecture of {topic}.",
            "finish_line": f"Complete the starter exercise demonstrating core {topic} functionality.",
            "exercise_code": f"# Starter Exercise for {topic}\n# Implement the initial demonstration\n",
            "explanation": f"Core principles and introductory concepts for {topic}.",
            "summary": f"Key concepts and foundational knowledge in {topic}.",
        },
        {
            "step_number": 2,
            "title": f"Core Mechanics & Practical Application of {topic.title()}",
            "time_estimate": "4-5 hours",
            "goal": f"Apply {topic} in practical scenarios with realistic constraints.",
            "finish_line": "Construct an end-to-end working example.",
            "exercise_code": "",
            "explanation": f"Deeper dive into mechanisms and patterns for {topic}.",
            "summary": f"Practical patterns and best practices for {topic}.",
        },
    ]


# --------------------------------------------------------------------------- #
# LearningWorkflow Implementation
# --------------------------------------------------------------------------- #


class LearningWorkflow:
    """Deterministic learning workflow driven by `WorkflowRunner`."""

    def __init__(
        self,
        collectors: tuple[Collector, ...] | None = None,
        synthesizer: Callable[[str, list[Any]], Awaitable[str]] | None = None,
        state_store: StateStore | None = None,
        max_retries: int = 2,
        tools: Any | None = None,
    ) -> None:
        self.collectors: tuple[Collector, ...] = (
            collectors
            if collectors is not None
            else (web_collector, docs_collector)
        )
        self.synthesizer = synthesizer
        self.tools = tools
        self.simulated = True

        self.runner = WorkflowRunner(
            name=WORKFLOW_NAME,
            nodes={
                "goal_analysis": self._goal_analysis,
                "curriculum_planning": self._curriculum_planning,
                "source_gathering": self._source_gathering,
                "exercise": self._exercise,
                "lesson": self._lesson,
                "sources": self._sources,
                "synthesis": self._synthesis,
                "review": self._review,
            },
            entry="goal_analysis",
            state_store=state_store,
            max_retries=max_retries,
        )

    # ------------------------------------------------------------------ #
    # Canonical Graph Specification
    # ------------------------------------------------------------------ #

    def graph_spec(self) -> dict[str, Any]:
        """Return the canonical graph shape for this workflow (no execution)."""
        nodes = [
            {
                "id": "input",
                "kind": "input",
                "title": "Learner Goal",
                "config": {},
                "position": [100, 300],
                "inputs": [],
                "outputs": [{"name": "goal", "type": "text", "required": True}],
            },
            {
                "id": "goal_analysis",
                "kind": "agent",
                "title": "Goal Analysis",
                "config": {"pipeline_node": "goal_analysis"},
                "position": [260, 300],
                "inputs": [{"name": "goal", "type": "text", "required": True}],
                "outputs": [{"name": "analysis", "type": "json", "required": True}],
            },
            {
                "id": "curriculum_planning",
                "kind": "agent",
                "title": "Curriculum Planning",
                "config": {"pipeline_node": "curriculum_planning"},
                "position": [420, 300],
                "inputs": [{"name": "analysis", "type": "json", "required": True}],
                "outputs": [{"name": "curriculum", "type": "json", "required": True}],
            },
            {
                "id": "source_gathering",
                "kind": "tool",
                "title": "Source Gathering",
                "config": {"pipeline_node": "source_gathering"},
                "position": [580, 300],
                "inputs": [{"name": "curriculum", "type": "json", "required": True}],
                "outputs": [{"name": "evidence", "type": "json", "required": True}],
            },
            {
                "id": "exercise",
                "kind": "agent",
                "title": "Exercise",
                "config": {
                    "pipeline_node": "exercise",
                    "view": {"kind": "code", "title": "Starter Exercise"},
                },
                "position": [740, 300],
                "inputs": [{"name": "evidence", "type": "json", "required": True}],
                "outputs": [{"name": "exercise_view", "type": "json", "required": True}],
            },
            {
                "id": "lesson",
                "kind": "agent",
                "title": "Lesson",
                "config": {
                    "pipeline_node": "lesson",
                    "view": {"kind": "markdown", "title": "Lesson Explanation"},
                },
                "position": [900, 300],
                "inputs": [{"name": "exercise_view", "type": "json", "required": True}],
                "outputs": [{"name": "lesson_view", "type": "json", "required": True}],
            },
            {
                "id": "sources",
                "kind": "knowledge",
                "title": "Sources",
                "config": {
                    "pipeline_node": "sources",
                    "view": {"kind": "markdown", "title": "Sources & References"},
                },
                "position": [1060, 300],
                "inputs": [{"name": "lesson_view", "type": "json", "required": True}],
                "outputs": [{"name": "sources_view", "type": "json", "required": True}],
            },
            {
                "id": "synthesis",
                "kind": "synthesis",
                "title": "Synthesis",
                "config": {"pipeline_node": "synthesis"},
                "position": [1220, 300],
                "inputs": [{"name": "sources_view", "type": "json", "required": True}],
                "outputs": [{"name": "lesson_material", "type": "text", "required": True}],
            },
            {
                "id": "review",
                "kind": "evaluation",
                "title": "Review",
                "config": {"pipeline_node": "review", "verifier": "learning_review"},
                "position": [1380, 300],
                "inputs": [{"name": "lesson_material", "type": "text", "required": True}],
                "outputs": [{"name": "verification", "type": "json", "required": True}],
            },
            {
                "id": "output",
                "kind": "output",
                "title": "Result",
                "config": {},
                "position": [1540, 300],
                "inputs": [{"name": "lesson_material", "type": "text", "required": True}],
                "outputs": [],
            },
        ]

        node_ids = [n["id"] for n in nodes]
        edges = []
        for i in range(len(node_ids) - 1):
            src = nodes[i]
            tgt = nodes[i + 1]
            src_port = src["outputs"][0]["name"] if src["outputs"] else "out"
            tgt_port = tgt["inputs"][0]["name"] if tgt["inputs"] else "in"
            edges.append({
                "id": f"e{i+1}",
                "source": node_ids[i],
                "source_port": src_port,
                "target": node_ids[i + 1],
                "target_port": tgt_port,
                "kind": "data",
            })

        return {
            "id": f"wf-{WORKFLOW_NAME}",
            "name": WORKFLOW_NAME,
            "description": "Learning pipeline: goal analysis through interactive exercises and sourced lessons",
            "nodes": nodes,
            "edges": edges,
        }

    # ------------------------------------------------------------------ #
    # Node Functions
    # ------------------------------------------------------------------ #

    async def _goal_analysis(self, state: WorkflowState) -> NodeResult:
        goal_text = _extract_goal_text(state)
        analysis = _analyze_goal(goal_text)
        return NodeResult(
            {
                "goal": goal_text,
                "topic": analysis["topic"],
                "current_level": analysis["current_level"],
                "target_level": analysis["target_level"],
                "prior_knowledge": analysis["prior_knowledge"],
                "is_code_topic": analysis["is_code_topic"],
            },
            next_node="curriculum_planning",
        )

    async def _curriculum_planning(self, state: WorkflowState) -> NodeResult:
        topic = state.data.get("topic", "backend development")
        is_code = state.data.get("is_code_topic", True)
        if is_code or "backend" in topic.casefold():
            curriculum = _build_backend_curriculum(topic)
        else:
            curriculum = _build_generic_curriculum(topic)

        current_step = curriculum[0]
        return NodeResult(
            {
                "curriculum": curriculum,
                "current_step": current_step,
            },
            next_node="source_gathering",
        )

    async def _source_gathering(self, state: WorkflowState) -> NodeResult:
        topic = state.data.get("topic", "backend development")
        query = f"{topic} documentation tutorial"

        all_results: list[Evidence] = []
        collectors_used: list[str] = []

        for collector in self.collectors:
            name = getattr(collector, "__name__", "collector")
            collectors_used.append(name)
            try:
                # web_collector and docs_collector support tools kwarg
                try:
                    res = await collector(query, tools=self.tools)
                except TypeError:
                    res = await collector(query)
                all_results.extend(res)
            except Exception as exc:
                log.warning("Collector %s failed: %s", name, exc)

        # Fallback to stub collectors if no evidence collected
        if not all_results:
            stub_items = await stub_web_collector(query)
            all_results.extend(stub_items)
            collectors_used.append("stub_web_collector")

        # Determine if simulation / stubs
        simulated = True
        for item in all_results:
            prov = str(item.get("provider", ""))
            if prov and not (prov.startswith("stub:") or prov.startswith("stub_")):
                simulated = False
                break
        self.simulated = simulated

        # Build formatted sources summary text
        sources_lines = []
        seen_urls = set()
        for item in all_results:
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                claim = item.get("claim") or item.get("title") or "Documentation resource"
                provider = item.get("provider", "reference")
                sources_lines.append(f"- [{claim}]({url}) — `{provider}`")

        sources_summary = "\n".join(sources_lines) if sources_lines else "- No external sources recorded."

        return NodeResult(
            {
                "evidence": all_results,
                "simulated": simulated,
                "collectors_used": collectors_used,
                "sources_summary": sources_summary,
            },
            next_node="exercise",
        )

    async def _exercise(self, state: WorkflowState) -> NodeResult:
        current_step = state.data.get("current_step", {})
        code_text = current_step.get("exercise_code", "")
        title = f"Exercise: {current_step.get('title', 'Ready-to-Start Exercise')}"
        finish_line = current_step.get("finish_line", "Complete verification test.")

        view = NodeView(
            kind="code",
            title=title,
            text=code_text,
            language="python",
            caption=f"Finish line: {finish_line}",
        )

        return NodeResult(
            {
                "exercise_view": view.model_dump(mode="json"),
            },
            next_node="lesson",
        )

    async def _lesson(self, state: WorkflowState) -> NodeResult:
        current_step = state.data.get("current_step", {})
        curriculum = state.data.get("curriculum", [])
        title = f"Lesson: {current_step.get('title', 'Explanation')}"
        explanation = current_step.get("explanation", "")

        view = NodeView(
            kind="markdown",
            title=title,
            text=f"{explanation}\n\n{current_step.get('summary', '')}".strip(),
            caption=f"Step 1 of {len(curriculum)}: {current_step.get('title', '')}",
        )

        return NodeResult(
            {
                "lesson_view": view.model_dump(mode="json"),
            },
            next_node="sources",
        )

    async def _sources(self, state: WorkflowState) -> NodeResult:
        sources_summary = state.data.get("sources_summary", "")
        simulated = state.data.get("simulated", self.simulated)

        parts = ["### Recommended Documentation & Sources\n"]
        if simulated:
            parts.append(HONESTY_BANNER + "\n")
        parts.append(sources_summary)
        sources_content = "\n".join(parts)

        view = NodeView(
            kind="markdown",
            title="Sources & Reference Material",
            text=sources_content,
            caption="Curated references with real URLs (labeled if simulation)",
        )

        return NodeResult(
            {
                "sources_view": view.model_dump(mode="json"),
            },
            next_node="synthesis",
        )

    async def _synthesis(self, state: WorkflowState) -> NodeResult:
        topic = state.data.get("topic", "Backend Development")
        current_level = state.data.get("current_level", "beginner")
        target_level = state.data.get("target_level", "intermediate")
        curriculum = state.data.get("curriculum", [])
        current_step = state.data.get("current_step", {})
        simulated = state.data.get("simulated", self.simulated)
        sources_summary = state.data.get("sources_summary", "")

        # Assemble full lesson artifact text
        lines = [f"# Learning Path: {topic.title()}", ""]
        if simulated:
            lines.extend([HONESTY_BANNER, ""])

        lines.extend([
            "## Goal & Overview",
            "",
            f"- **Topic**: {topic}",
            f"- **Progression**: {current_level} → {target_level}",
            f"- **Prerequisites**: {state.data.get('prior_knowledge', 'None')}",
            "",
            "## Curriculum Modules",
            "",
        ])

        for step in curriculum:
            num = step.get("step_number", 1)
            title = step.get("title", "")
            time_est = step.get("time_estimate", "")
            goal = step.get("goal", "")
            finish = step.get("finish_line", "")
            lines.extend([
                f"### Module {num}: {title} ({time_est})",
                f"- **Objective**: {goal}",
                f"- **Finish Line**: {finish}",
                "",
            ])

        lines.extend([
            "---",
            "",
            "## Current Step: Step 1 Walkthrough",
            "",
            current_step.get("explanation", ""),
            "",
            "## Practical Exercise",
            "",
            "```python",
            current_step.get("exercise_code", "").strip(),
            "```",
            "",
            "## Summary & Mental Model",
            "",
            current_step.get("summary", ""),
            "",
            "## Video Lesson Status",
            "",
            "*Video view omitted because no verified, embeddable video lesson is available for this topic. Following text-and-code interactive format.*",
            "",
            "## Sources & Documentation",
            "",
            sources_summary,
            "",
        ])

        full_lesson_md = "\n".join(lines)

        task_id = state.task_id
        artifacts = [
            Artifact(
                task_id=task_id,
                type="lesson.md",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=full_lesson_md,
            ),
            Artifact(
                task_id=task_id,
                type="curriculum.json",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=json.dumps(curriculum, indent=2),
            ),
        ]

        # Gather the 3 non-redundant canvas views (Astra UX hierarchy)
        exercise_view = state.data.get("exercise_view")
        lesson_view = state.data.get("lesson_view")
        sources_view = state.data.get("sources_view")

        node_views: list[dict[str, Any]] = []
        if exercise_view:
            node_views.append({"node_id": "exercise", "view": exercise_view})
        if lesson_view:
            node_views.append({"node_id": "lesson", "view": lesson_view})
        if sources_view:
            node_views.append({"node_id": "sources", "view": sources_view})
        return NodeResult(
            {
                "output": full_lesson_md,
                "synthesis": full_lesson_md,
                "artifacts": artifacts,
                "node_views": node_views,
            },
            next_node="review",
        )

    async def _review(self, state: WorkflowState) -> NodeResult:
        curriculum = state.data.get("curriculum", [])
        evidence = state.data.get("evidence", [])
        simulated = state.data.get("simulated", False)
        output = state.data.get("output", "")
        artifacts = state.artifacts or state.data.get("artifacts", [])

        has_lesson_artifact = any(
            getattr(a, "type", "") == "lesson.md"
            or (isinstance(a, dict) and a.get("type") == "lesson.md")
            for a in artifacts
        )
        honesty_valid = not simulated or ("SIMULATION — DETERMINISTIC STUB EVIDENCE" in output)

        criteria = [
            VerificationCriterion(
                criterion="topic_and_level_analyzed",
                passed=bool(state.data.get("topic")),
                evidence=f"topic={state.data.get('topic')!r}, level={state.data.get('target_level')!r}",
            ),
            VerificationCriterion(
                criterion="curriculum_planned",
                passed=len(curriculum) >= 1,
                evidence=f"curriculum contains {len(curriculum)} modules",
            ),
            VerificationCriterion(
                criterion="exercise_with_finish_line_provided",
                passed=bool(curriculum and curriculum[0].get("finish_line")),
                evidence=f"finish line: {curriculum[0].get('finish_line') if curriculum else 'none'}",
            ),
            VerificationCriterion(
                criterion="sources_collected",
                passed=len(evidence) >= 1,
                evidence=f"{len(evidence)} evidence/source items collected",
            ),
            VerificationCriterion(
                criterion="honesty_banner_when_stubs",
                passed=honesty_valid,
                evidence="honesty banner verified" if honesty_valid else "missing honesty banner on stub run",
            ),
            VerificationCriterion(
                criterion="lesson_artifact_created",
                passed=has_lesson_artifact,
                evidence="lesson.md artifact emitted",
            ),
        ]

        failed = [c.criterion for c in criteria if not c.passed]
        verification = VerificationResult(
            verifier="learning_review",
            passed=not failed,
            criteria=criteria,
            score=sum(1 for c in criteria if c.passed) / len(criteria) if criteria else 1.0,
            notes="all learning review criteria passed" if not failed else f"failed criteria: {', '.join(failed)}",
        )

        return NodeResult({"verification": verification}, next_node=None)

    # ------------------------------------------------------------------ #
    # Entry Point
    # ------------------------------------------------------------------ #

    async def run(self, task: TaskSpec) -> WorkflowResult:
        """Run the full learning pipeline for `task`."""
        return await self.runner.run(task)
