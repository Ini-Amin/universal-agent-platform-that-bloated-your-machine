"""Tests for the template catalog (pure data, no server).

The catalog's contract is small but strict:

* it has the ComfyUI group shape (``moduleName`` / ``category`` / ``title`` /
  ``icon`` / ``type`` / ``templates``);
* every listed template exposes exactly the public index fields;
* **every template maps to a workflow that can actually run today** -- this is
  the honesty rule the whole feature rests on;
* a category with no real template is omitted, never faked with an empty
  ``templates`` list.
"""

from __future__ import annotations

import pytest

from uap.contracts import Domain, UserRequest
from uap.entry import EntryWorkflow
from uap.templates import (
    CATALOG,
    PUBLIC_TEMPLATE_FIELDS,
    TEMPLATE_NAMES,
    catalog,
    get_template,
    template_detail,
)
from uap.workflows.bbp import WORKFLOW_NAME as BBP_WORKFLOW_NAME
from uap.workflows.learning import WORKFLOW_NAME as LEARNING_WORKFLOW_NAME
from uap.workflows.research import WORKFLOW_NAME as RESEARCH_WORKFLOW_NAME

#: Workflows the platform can actually execute today. A template may only map
#: to one of these; anything else would be a template for a capability that
#: does not exist.
EXECUTABLE_WORKFLOW_REFS = frozenset({RESEARCH_WORKFLOW_NAME, BBP_WORKFLOW_NAME, LEARNING_WORKFLOW_NAME})

#: Domains the server treats as executable (mirrors ``_EXECUTABLE`` in app.py).
EXECUTABLE_DOMAINS = frozenset({Domain.RESEARCH.value, Domain.BBP.value, Domain.LEARNING.value})

GROUP_FIELDS = {"moduleName", "category", "title", "icon", "type", "templates"}

# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_catalog_is_a_list_of_groups_with_the_comfyui_shape() -> None:
    groups = catalog()
    assert isinstance(groups, list)
    assert groups, "the catalog must not be empty"
    for group in groups:
        assert set(group) == GROUP_FIELDS, group
        assert isinstance(group["moduleName"], str) and group["moduleName"]
        assert isinstance(group["category"], str) and group["category"]
        assert isinstance(group["title"], str) and group["title"]
        assert isinstance(group["icon"], str) and group["icon"].startswith("icon-[")
        assert isinstance(group["type"], str) and group["type"]
        assert isinstance(group["templates"], list) and group["templates"]

def test_each_template_has_exactly_the_public_index_fields() -> None:
    for group in catalog():
        for tpl in group["templates"]:
            assert set(tpl) == set(PUBLIC_TEMPLATE_FIELDS), tpl
            assert isinstance(tpl["name"], str) and tpl["name"]
            assert isinstance(tpl["title"], str) and tpl["title"]
            assert isinstance(tpl["description"], str) and tpl["description"]
            assert isinstance(tpl["tags"], list) and tpl["tags"]
            # No preview media or tutorials ship yet: claiming them would
            # render a broken thumbnail / dead link in the UI.
            assert tpl["mediaType"] is None
            assert tpl["tutorialUrl"] is None

def test_no_empty_categories() -> None:
    # A category with nothing runnable must be omitted, not shipped empty.
    for group in catalog():
        assert group["templates"], f"empty category {group['category']!r}"

def test_template_names_are_unique_and_match_the_flat_index() -> None:
    names = [tpl["name"] for group in catalog() for tpl in group["templates"]]
    assert len(names) == len(set(names)), "duplicate template name"
    assert tuple(names) == TEMPLATE_NAMES

def test_expected_categories_are_present() -> None:
    categories = {group["category"] for group in catalog()}
    assert categories == {"Learning", "Research", "Bug Bounty"}

def test_catalog_returns_fresh_structures() -> None:
    first = catalog()
    first[0]["category"] = "mutated"
    first[0]["templates"][0]["title"] = "mutated"
    assert catalog()[0]["category"] != "mutated"
    assert catalog()[0]["templates"][0]["title"] != "mutated"

# --------------------------------------------------------------------------- #
# Honesty: every template maps to a workflow that runs today
# --------------------------------------------------------------------------- #

def test_every_template_maps_to_an_executable_workflow() -> None:
    for group in CATALOG:
        for tpl in group["templates"]:
            workflow = tpl["workflow"]
            assert workflow["domain"] in EXECUTABLE_DOMAINS, tpl["name"]
            assert workflow["workflow_ref"] in EXECUTABLE_WORKFLOW_REFS, tpl["name"]

def test_every_template_input_example_routes_to_its_declared_domain() -> None:
    """The strongest honesty check: the example the user is handed really routes.

    If a template's example did not classify into the workflow it claims, the
    template would be a lie the UI tells before anything runs.
    """
    entry = EntryWorkflow()
    for group in CATALOG:
        for tpl in group["templates"]:
            example = tpl["input"]["example"]
            outcome = entry.run(UserRequest(raw_input=example))
            assert not outcome.needs_clarification, (tpl["name"], example)
            assert outcome.spec is not None
            assert outcome.spec.domain.value == tpl["workflow"]["domain"], (
                tpl["name"],
                example,
                outcome.spec.domain,
            )

def test_bbp_templates_require_scope() -> None:
    for group in CATALOG:
        for tpl in group["templates"]:
            if tpl["workflow"]["domain"] == Domain.BBP.value:
                assert tpl["input"].get("requires_scope") is True, tpl["name"]
                assert "In scope:" in tpl["input"]["example"], tpl["name"]

# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

def test_get_template_returns_the_internal_record() -> None:
    tpl = get_template("research_literature_review")
    assert tpl is not None
    assert tpl["name"] == "research_literature_review"
    assert get_template("does_not_exist") is None

def test_template_detail_adds_workflow_and_input_shape() -> None:
    detail = template_detail("bbp_assisted_assessment")
    assert detail is not None
    assert set(detail) == set(PUBLIC_TEMPLATE_FIELDS) | {"workflow", "input"}
    assert detail["workflow"]["domain"] == "bbp"
    assert detail["workflow"]["workflow"] == "BBPWorkflow"
    assert detail["workflow"]["workflow_ref"] == "bbp"
    assert detail["input"]["shape"] == {
        "input": "bug bounty on example.com. In scope: *.example.com"
    }
    assert detail["input"]["required"] == ["input"]

def test_template_detail_unknown_returns_none() -> None:
    assert template_detail("nope") is None

@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_every_named_template_has_a_detail(name: str) -> None:
    detail = template_detail(name)
    assert detail is not None
    assert detail["name"] == name
