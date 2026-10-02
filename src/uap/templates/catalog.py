"""The UAP template catalog (data).

ComfyUI-shaped, but with one non-negotiable rule: **a template is listed only
when the workflow it maps to can actually run today.** The platform currently
executes exactly two workflows -- ``research`` (:class:`uap.workflows.research.ResearchWorkflow`)
and ``bbp`` (:class:`uap.workflows.bbp.BBPWorkflow`) -- so every template below
maps to one of them. Categories with no runnable template are omitted rather
than padded with fiction.

The catalog is plain data (no imports of the workflows, so this module stays
import-cheap and cycle-free). ``workflow.domain`` is the string the Router and
``POST /tasks`` use; ``workflow.workflow`` is the workflow class name the server
reports; ``workflow.workflow_ref`` is the registered name.

Catalog shape (one group)::

    {
      "moduleName": "default",
      "category": "Research",
      "title": "Research",
      "icon": "icon-[lucide--microscope]",
      "type": "research",
      "templates": [ { "name": ..., "title": ..., "description": ...,
                       "tags": [...], "mediaType": ..., "tutorialUrl": ... } ]
    }

``mediaType`` is ``None`` on every template: UAP ships no preview media yet, so
the UI falls back to the category icon. Claiming ``"image"`` would render a
broken thumbnail. ``tutorialUrl`` is ``None`` for the same reason -- no
tutorials exist. The detail endpoint adds the workflow mapping and the input
shape a caller must supply.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CATALOG",
    "PUBLIC_TEMPLATE_FIELDS",
    "TEMPLATE_NAMES",
    "catalog",
    "get_template",
    "template_detail",
]

#: Fields the *list* endpoint returns per template (the ComfyUI index shape).
PUBLIC_TEMPLATE_FIELDS: tuple[str, ...] = (
    "name",
    "title",
    "description",
    "tags",
    "mediaType",
    "tutorialUrl",
)

#: The workflow each domain maps to, as the server reports it. Kept as literals
#: (not imported) so the catalog has no dependency on the workflow packages.
_RESEARCH_WORKFLOW = {
    "domain": "research",
    "workflow": "ResearchWorkflow",
    "workflow_ref": "research",
}
_BBP_WORKFLOW = {
    "domain": "bbp",
    "workflow": "BBPWorkflow",
    "workflow_ref": "bbp",
}

def _research_input(example: str, notes: str) -> dict[str, Any]:
    return {
        # The shape a caller must provide (what the UI pre-fills the box with).
        "shape": {"input": example},
        "example": example,
        "required": ["input"],
        "notes": notes,
    }

def _bbp_input(example: str, notes: str) -> dict[str, Any]:
    return {
        "shape": {"input": example},
        "example": example,
        "required": ["input"],
        "notes": notes,
        # BBP fails closed: the scope gate refuses to probe anything without an
        # in-scope declaration. The UI must surface this before starting.
        "requires_scope": True,
    }

# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #
#
# Order is the order the UI shows. Categories: Learning, Research, Bug Bounty.
# Each template's ``description`` states what the run actually produces; none
# claims a capability the workflow does not have.

CATALOG: list[dict[str, Any]] = [
    {
        "moduleName": "default",
        "category": "Learning",
        "title": "Learning",
        "icon": "icon-[lucide--graduation-cap]",
        "type": "learning",
        "templates": [
            {
                "name": "learning_research_roadmap",
                "title": "Research-driven learning roadmap",
                "description": (
                    "Turns a topic into a sourced, ordered learning path: the "
                    "research workflow gathers and cross-verifies sources, then "
                    "synthesises a roadmap with references. This is research "
                    "over a topic, not a course engine -- it produces a "
                    "reading/study plan with citations, not lessons or quizzes."
                ),
                "tags": ["Learning", "Roadmap", "Research-driven"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _RESEARCH_WORKFLOW,
                "input": _research_input(
                    "research a learning roadmap for backend engineering from "
                    "scratch to intermediate",
                    "Lead with 'research' so the request routes to the research "
                    "workflow; name the topic and the target level.",
                ),
            }
        ],
    },
    {
        "moduleName": "default",
        "category": "Research",
        "title": "Research",
        "icon": "icon-[lucide--microscope]",
        "type": "research",
        "templates": [
            {
                "name": "research_literature_review",
                "title": "Literature review",
                "description": (
                    "Collects and cross-verifies sources on a question, then "
                    "writes a synthesised review that keeps its citations. "
                    "Produces report.md and sources.json; every claim is "
                    "traceable to a collected source."
                ),
                "tags": ["Research", "Literature", "Synthesis"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _RESEARCH_WORKFLOW,
                "input": _research_input(
                    "research the literature on retrieval augmented generation",
                    "State the question or topic to review. Sources come from "
                    "the configured collectors (MCP search, HTTP, or the "
                    "offline stub when no provider is configured).",
                ),
            },
            {
                "name": "research_technology_comparison",
                "title": "Technology comparison",
                "description": (
                    "Researches two or more options against each other and "
                    "writes a side-by-side synthesis with the trade-offs and "
                    "caveats the evidence supports. Produces report.md and "
                    "sources.json."
                ),
                "tags": ["Research", "Comparison", "Decision"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _RESEARCH_WORKFLOW,
                "input": _research_input(
                    "research and compare PostgreSQL vs SQLite for durable "
                    "agent state",
                    "Name the options and the decision context; the synthesis "
                    "reports trade-offs rather than a single winner.",
                ),
            },
            {
                "name": "research_state_of_the_art_survey",
                "title": "State-of-the-art survey",
                "description": (
                    "Surveys the current state of the art on a topic: gathers "
                    "evidence, cross-checks it, and synthesises what is settled "
                    "versus still open. Produces report.md and sources.json."
                ),
                "tags": ["Research", "Survey", "State-of-the-art"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _RESEARCH_WORKFLOW,
                "input": _research_input(
                    "research a survey of the state of the art in graph neural "
                    "networks",
                    "Name the field; the report separates consensus from open "
                    "questions and cites its sources.",
                ),
            },
        ],
    },
    {
        "moduleName": "default",
        "category": "Bug Bounty",
        "title": "Bug Bounty",
        "icon": "icon-[lucide--bug]",
        "type": "bbp",
        "templates": [
            {
                "name": "bbp_recon_sweep",
                "title": "Recon sweep (assets + endpoints)",
                "description": (
                    "Starts from an authorised target and its scope, discovers "
                    "assets and endpoints, then runs the same bug-bounty "
                    "pipeline through to a preliminary findings report. Recon "
                    "is the emphasis; the report is included, so this is not a "
                    "recon-only mode."
                ),
                "tags": ["Bug Bounty", "Recon", "Scope-gated"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _BBP_WORKFLOW,
                "input": _bbp_input(
                    "bug bounty recon on example.com. In scope: *.example.com",
                    "The scope declaration is mandatory: the scope gate fails "
                    "closed and probes nothing without it. Only test targets "
                    "you are authorised to test.",
                ),
            },
            {
                "name": "bbp_assisted_assessment",
                "title": "Assisted assessment",
                "description": (
                    "A full assisted assessment: scope validation, recon, "
                    "finding generation, deterministic severity classification, "
                    "validation against evidence, and a written report. "
                    "Produces findings.json and report.md."
                ),
                "tags": ["Bug Bounty", "Assessment", "Scope-gated"],
                "mediaType": None,
                "tutorialUrl": None,
                "workflow": _BBP_WORKFLOW,
                "input": _bbp_input(
                    "bug bounty on example.com. In scope: *.example.com",
                    "Declare scope with 'In scope:'. Add 'Out of scope:' to "
                    "exclude hosts. Only test targets you are authorised to test.",
                ),
            },
        ],
    },
]

#: Every template name, in catalog order (the group order above).
TEMPLATE_NAMES: tuple[str, ...] = tuple(
    tpl["name"] for group in CATALOG for tpl in group["templates"]
)

#: Flat name -> template lookup (built once).
_BY_NAME: dict[str, dict[str, Any]] = {
    tpl["name"]: tpl for group in CATALOG for tpl in group["templates"]
}

def _public(template: dict[str, Any]) -> dict[str, Any]:
    """Return only the ComfyUI index fields for ``template``."""
    return {field: template.get(field) for field in PUBLIC_TEMPLATE_FIELDS}

def catalog() -> list[dict[str, Any]]:
    """Return the grouped catalog with only the public index fields.

    A fresh structure every call, so a caller can never mutate the module data
    through the returned value.
    """
    return [
        {
            "moduleName": group["moduleName"],
            "category": group["category"],
            "title": group["title"],
            "icon": group["icon"],
            "type": group["type"],
            "templates": [_public(tpl) for tpl in group["templates"]],
        }
        for group in CATALOG
    ]

def get_template(name: str) -> dict[str, Any] | None:
    """Return the internal template dict for ``name``, or ``None``."""
    return _BY_NAME.get(name)

def template_detail(name: str) -> dict[str, Any] | None:
    """Return one template with its workflow mapping and input shape.

    ``None`` when ``name`` is not a catalog template.
    """
    template = _BY_NAME.get(name)
    if template is None:
        return None
    return {
        **_public(template),
        "workflow": dict(template["workflow"]),
        "input": dict(template["input"]),
    }
