"""Tests for the Skill System (Master section 11).

A Skill is reusable methodology, not a tool. These tests cover the data model,
the registry's deterministic queries, and the dependency-free Markdown loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uap.skills import (
    FrontmatterError,
    Skill,
    SkillRegistry,
    load_skill_file,
    load_skills_from_dir,
    load_skills_lenient,
    parse_frontmatter,
)

LIBRARY = Path(__file__).resolve().parent.parent / "src" / "uap" / "skills" / "library"


def _skill(name: str, domain: str = "learning", capabilities: list[str] | None = None) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        domain=domain,
        methodology=f"# {name}\n\nDo the thing.",
        required_capabilities=capabilities or [],
    )


# --------------------------------------------------------------------------- #
# 1. Skill round-trips JSON

def test_skill_round_trips_json() -> None:
    original = _skill("alpha", domain="research", capabilities=["web.search"])
    restored = Skill.model_validate_json(original.model_dump_json())
    assert restored == original


# --------------------------------------------------------------------------- #
# 2. Register + get + names

def test_register_get_and_names() -> None:
    registry = SkillRegistry()
    registry.register(_skill("beta"))
    registry.register(_skill("alpha"))

    assert registry.get("beta") is not None
    assert registry.get("beta").name == "beta"
    assert registry.get("missing") is None
    assert registry.names() == ["alpha", "beta"]


# --------------------------------------------------------------------------- #
# 3. Duplicate raises

def test_duplicate_registration_raises() -> None:
    registry = SkillRegistry()
    registry.register(_skill("alpha"))
    with pytest.raises(ValueError, match="alpha"):
        registry.register(_skill("alpha"))


# --------------------------------------------------------------------------- #
# 4. find_by_domain filters and orders

def test_find_by_domain_filters_and_orders() -> None:
    registry = SkillRegistry()
    registry.register(_skill("zeta", domain="bbp"))
    registry.register(_skill("alpha", domain="bbp"))
    registry.register(_skill("mid", domain="learning"))

    bbp = registry.find_by_domain("bbp")
    assert [s.name for s in bbp] == ["alpha", "zeta"]
    assert registry.find_by_domain("unknown") == []


# --------------------------------------------------------------------------- #
# 5. find_by_capability matches

def test_find_by_capability_matches() -> None:
    registry = SkillRegistry()
    registry.register(_skill("one", capabilities=["http.request", "dns.lookup"]))
    registry.register(_skill("two", capabilities=["web.search"]))
    registry.register(_skill("three", capabilities=["http.request"]))

    assert [s.name for s in registry.find_by_capability("http.request")] == ["one", "three"]
    assert [s.name for s in registry.find_by_capability("web.search")] == ["two"]
    assert registry.find_by_capability("nope") == []


# --------------------------------------------------------------------------- #
# 6. load_skills_from_dir loads the 3 bundled library skills

def test_load_bundled_library() -> None:
    skills = load_skills_from_dir(LIBRARY)
    names = {s.name for s in skills}
    assert {"subnetting-teaching", "literature-review", "api-recon"} <= names
    assert len(skills) == 3


# --------------------------------------------------------------------------- #
# 7. Malformed file raises ValueError with the path in the message

def test_malformed_file_raises_with_path(tmp_path: Path) -> None:
    bad = tmp_path / "broken.md"
    bad.write_text("# no frontmatter at all\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_skills_from_dir(tmp_path)
    assert str(bad) in str(excinfo.value)


def test_missing_name_raises_with_path(tmp_path: Path) -> None:
    bad = tmp_path / "nameless.md"
    bad.write_text("---\ndomain: learning\n---\nbody\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        load_skills_from_dir(tmp_path)
    assert str(bad) in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 8. load_skills_lenient skips malformed files

def test_load_skills_lenient_skips_malformed(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text(
        "---\nname: good\ndomain: research\n---\nmethod\n", encoding="utf-8"
    )
    (tmp_path / "bad.md").write_text("not frontmatter\n", encoding="utf-8")

    with pytest.warns(UserWarning, match="bad.md"):
        skills = load_skills_lenient(tmp_path)

    assert [s.name for s in skills] == ["good"]


# --------------------------------------------------------------------------- #
# 9. Frontmatter parser handles missing optional fields (defaults)

def test_frontmatter_defaults_for_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "minimal.md"
    path.write_text("---\nname: minimal\n---\n# Body\n", encoding="utf-8")

    skill = load_skill_file(path)
    assert skill.name == "minimal"
    assert skill.description == ""
    assert skill.domain == ""
    assert skill.required_capabilities == []
    assert skill.version == 1
    assert skill.methodology == "# Body"


def test_required_capabilities_are_comma_separated(tmp_path: Path) -> None:
    path = tmp_path / "caps.md"
    path.write_text(
        "---\nname: caps\nrequired_capabilities: http.request, dns.lookup , web.search\n---\nbody\n",
        encoding="utf-8",
    )
    assert load_skill_file(path).required_capabilities == [
        "http.request",
        "dns.lookup",
        "web.search",
    ]


# --------------------------------------------------------------------------- #
# 10. Bundled subnetting-teaching has domain learning and non-empty methodology

def test_bundled_subnetting_teaching() -> None:
    skill = load_skill_file(LIBRARY / "subnetting-teaching.md")
    assert skill.domain == "learning"
    assert skill.methodology.strip() != ""
    assert skill.description.strip() != ""


# --------------------------------------------------------------------------- #
# 11. Registry is order-stable (insertion order independent of find order)

def test_registry_order_is_insertion_independent() -> None:
    forward = SkillRegistry()
    for name in ["alpha", "beta", "gamma"]:
        forward.register(_skill(name, domain="d"))

    backward = SkillRegistry()
    for name in ["gamma", "beta", "alpha"]:
        backward.register(_skill(name, domain="d"))

    assert forward.names() == backward.names() == ["alpha", "beta", "gamma"]
    assert [s.name for s in forward.find_by_domain("d")] == [
        s.name for s in backward.find_by_domain("d")
    ]


# --------------------------------------------------------------------------- #
# Extra: parser and error-type details

def test_parse_frontmatter_rejects_unterminated_block() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        parse_frontmatter("---\nname: x\n")


def test_frontmatter_error_carries_path() -> None:
    err = FrontmatterError(Path("/tmp/x.md"), "boom")
    assert err.path == Path("/tmp/x.md")
    assert "/tmp/x.md" in str(err)


def test_skill_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        Skill(name="x", description="", domain="", methodology="", bogus=1)  # type: ignore[call-arg]


def test_load_skills_from_dir_rejects_non_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        load_skills_from_dir(tmp_path / "nope")
