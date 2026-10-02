"""Git integration tests (Master sections 2.4, 49, 50, 51, 63).

Every test builds its own throwaway repository under ``tmp_path``; there is no
network access and nothing touches the surrounding UAP checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uap.gitx import (
    CompatibilityChecker,
    CompatibilityIssue,
    CompatibilityReport,
    DefinitionGitSync,
    GitError,
    GitRepo,
)
from uap.graph.model import GraphEdge, GraphNode, NodeKind, WorkflowGraph

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def repo(tmp_path: Path) -> GitRepo:
    """A fresh repository with a local git identity (no global config needed)."""
    return GitRepo.init(tmp_path / "repo")


@pytest.fixture()
def sync(repo: GitRepo) -> DefinitionGitSync:
    return DefinitionGitSync(repo, "definitions")


def _graph(name: str = "recon", *, pin: str | None = "recon-agent@v1") -> WorkflowGraph:
    return WorkflowGraph(
        id=f"{name}-graph",
        name=name,
        nodes=[
            GraphNode(id="in", kind=NodeKind.INPUT, title="Input"),
            GraphNode(
                id="agent",
                kind=NodeKind.AGENT,
                title="Recon Agent",
                version_ref=pin,
            ),
            GraphNode(id="out", kind=NodeKind.OUTPUT, title="Output"),
        ],
        edges=[
            GraphEdge(
                id="e1",
                source="in",
                source_port="out",
                target="agent",
                target_port="in",
            ),
            GraphEdge(
                id="e2",
                source="agent",
                source_port="out",
                target="out",
                target_port="in",
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# 1-8: GitRepo
# --------------------------------------------------------------------------- #


def test_init_creates_repo_on_main(repo: GitRepo) -> None:
    assert repo.is_repo() is True
    assert repo.current_branch() == "main"
    assert (Path(repo.path) / ".git").is_dir()


def test_commit_all_returns_sha_and_appears_in_log(repo: GitRepo) -> None:
    (Path(repo.path) / "notes.txt").write_text("hello\n", encoding="utf-8")
    sha = repo.commit_all("add notes")

    assert sha is not None
    assert len(sha) == 40
    entries = repo.log()
    assert entries and entries[0]["sha"] == sha
    assert entries[0]["message"] == "add notes"


def test_commit_all_with_nothing_to_commit_returns_none(repo: GitRepo) -> None:
    (Path(repo.path) / "a.txt").write_text("1\n", encoding="utf-8")
    assert repo.commit_all("first") is not None
    # Working tree already matches HEAD -> no-op, not an error.
    assert repo.commit_all("second") is None


def test_diff_between_two_commits_shows_the_change(repo: GitRepo) -> None:
    target = Path(repo.path) / "config.json"
    target.write_text('{"v": 1}\n', encoding="utf-8")
    sha_a = repo.commit_all("v1")
    target.write_text('{"v": 2}\n', encoding="utf-8")
    sha_b = repo.commit_all("v2")

    patch = repo.diff(sha_a, sha_b)
    assert "-{\"v\": 1}" in patch
    assert "+{\"v\": 2}" in patch

    # Path-limited diff narrows to the same single file.
    assert "config.json" in repo.diff(sha_a, sha_b, path="config.json")


def test_show_reads_file_content_at_sha(repo: GitRepo) -> None:
    target = Path(repo.path) / "readme.txt"
    target.write_text("version one\n", encoding="utf-8")
    sha = repo.commit_all("v1")
    target.write_text("version two\n", encoding="utf-8")
    repo.commit_all("v2")

    assert repo.show(sha, "readme.txt") == "version one\n"


def test_file_history_returns_commits_touching_path(repo: GitRepo) -> None:
    a = Path(repo.path) / "a.txt"
    b = Path(repo.path) / "b.txt"
    a.write_text("1\n", encoding="utf-8")
    repo.commit_all("touch a")
    b.write_text("1\n", encoding="utf-8")
    repo.commit_all("touch b")
    a.write_text("2\n", encoding="utf-8")
    repo.commit_all("touch a again")

    history = repo.file_history("a.txt")
    messages = [entry["message"] for entry in history]
    assert messages == ["touch a again", "touch a"]


def test_current_branch_on_plain_dir_raises_giterror(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "file.txt").write_text("x\n", encoding="utf-8")

    repo = GitRepo(plain)
    assert repo.is_repo() is False
    with pytest.raises(GitError) as excinfo:
        repo.current_branch()
    assert excinfo.value.stderr or "not a git repository" in str(excinfo.value)


def test_export_path_escape_is_rejected(sync: DefinitionGitSync) -> None:
    for bad_name in ("../evil", "sub/../../evil", "..", "a/b"):
        with pytest.raises((GitError, ValueError)):
            sync.export_version("workflow", bad_name, 1, {"a": 1})
    # Nothing escaped the definitions root.
    assert not (sync.definitions_root.parent / "evil").exists()


# --------------------------------------------------------------------------- #
# 9-13: DefinitionGitSync
# --------------------------------------------------------------------------- #


def test_export_version_writes_canonical_json(sync: DefinitionGitSync) -> None:
    path = sync.export_version("workflow", "recon", 1, {"b": 2, "a": 1})

    assert path == sync.definitions_root / "workflow" / "recon" / "v1.json"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    # Sorted keys -> "a" before "b", regardless of insertion order.
    assert json.loads(text) == {"a": 1, "b": 2}
    assert text == '{\n  "a": 1,\n  "b": 2\n}\n'


def test_commit_definition_message_format_and_author(
    repo: GitRepo, sync: DefinitionGitSync
) -> None:
    sync.export_version("workflow", "recon", 1, {"a": 1})
    sync.commit_definition("workflow", "recon", 1)
    sync.export_version("workflow", "recon", 2, {"a": 2})
    sha = sync.commit_definition("workflow", "recon", 2)

    assert sha is not None
    entries = repo.log()
    assert entries[0]["message"] == "workflow(recon): v2"
    # The acting identity is visible, so AI changes are attributable (§49).
    assert entries[0]["author"] == "uap"

    other = sync.commit_definition("agent", "planner", 1)
    assert other is None  # nothing exported for that definition


def test_read_version_round_trip_and_unknown_is_none(
    sync: DefinitionGitSync,
) -> None:
    payload = {"model": "jdw/claude-opus-4-8", "instructions": "recon"}
    sync.export_version("agent", "recon-agent", 3, payload)

    assert sync.read_version("agent", "recon-agent", 3) == payload
    assert sync.read_version("agent", "recon-agent", 99) is None
    assert sync.read_version("agent", "missing", 1) is None


def test_diff_versions_shows_v1_vs_v2(sync: DefinitionGitSync) -> None:
    sync.export_version("workflow", "recon", 1, {"steps": 1, "mode": "fast"})
    sync.commit_definition("workflow", "recon", 1)
    sync.export_version("workflow", "recon", 2, {"steps": 2, "mode": "fast"})
    sync.commit_definition("workflow", "recon", 2)

    patch = sync.diff_versions("workflow", "recon", 1, 2)
    assert '"steps": 1' in patch
    assert '"steps": 2' in patch
    assert '"mode": "fast"' in patch


def test_export_graph_round_trips_and_hash_matches(
    sync: DefinitionGitSync,
) -> None:
    graph = _graph()
    path = sync.export_graph("recon", 1, graph)

    assert path == sync.definitions_root / "workflows" / "recon" / "v1.graph.json"
    restored = WorkflowGraph.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored.content_hash() == graph.content_hash()


# --------------------------------------------------------------------------- #
# 14-17: CompatibilityChecker
# --------------------------------------------------------------------------- #


def test_check_definition_unknown_kind_errors_good_spec_ok() -> None:
    checker = CompatibilityChecker(current_schema_version=1)

    bad = checker.check_definition("banana", {"a": 1})
    assert bad.ok is False
    assert any(issue.code == "unknown_kind" for issue in bad.errors)

    good = checker.check_definition("agent", {"model": "jdw/claude-opus-4-8"})
    assert good.ok is True
    assert good.issues == []


def test_schema_version_newer_than_current_requires_migration() -> None:
    checker = CompatibilityChecker(current_schema_version=1)
    report = checker.check_definition(
        "workflow", {"schema_version": 2, "graph_ref": "g.json"}
    )

    assert report.ok is False
    assert any(issue.code == "migration_required" for issue in report.errors)
    assert "migration" in report.errors[0].message.lower()


def test_check_graph_flags_non_exact_version_ref() -> None:
    checker = CompatibilityChecker(current_schema_version=1)

    floating = checker.check_graph(_graph(pin="recon-agent"))
    assert floating.ok is False
    assert any(i.code == "non_exact_version_ref" for i in floating.errors)

    exact = checker.check_graph(_graph(pin="recon-agent@v3"))
    assert exact.ok is True

    # Missing schema_version is only a warning when explicitly required.
    warned = checker.check_definition("agent", {"model": "m"}, required_schema_version=1)
    assert warned.ok is True
    assert any(i.code == "missing_schema_version" for i in warned.warnings)


def test_propose_migration_lists_issue_codes() -> None:
    checker = CompatibilityChecker(current_schema_version=1)
    report = checker.check_definition(
        "workflow", {"schema_version": 5, "graph_ref": "g.json"}
    )
    proposal = checker.propose_migration(report)

    assert proposal.strip()
    for issue in report.issues:
        assert issue.code in proposal
    assert "approval" in proposal.lower()


# --------------------------------------------------------------------------- #
# 18: End-to-end LibraryService-like flow
# --------------------------------------------------------------------------- #


def test_end_to_end_library_flow(repo: GitRepo, sync: DefinitionGitSync) -> None:
    # v1: register + publish + export + commit
    sync.export_version(
        "workflow", "recon", 1, {"graph_ref": "graphs/recon.v1.json", "schema_version": 1}
    )
    first = sync.commit_definition("workflow", "recon", 1)
    assert first is not None

    # v2: new_version + export + commit
    sync.export_version(
        "workflow", "recon", 2, {"graph_ref": "graphs/recon.v2.json", "schema_version": 1}
    )
    second = sync.commit_definition("workflow", "recon", 2)
    assert second is not None and second != first

    history = sync.history("workflow", "recon")
    assert len(history) == 2
    assert [h["message"] for h in history] == [
        "workflow(recon): v2",
        "workflow(recon): v1",
    ]

    patch = sync.diff_versions("workflow", "recon", 1, 2)
    assert "recon.v1.json" in patch
    assert "recon.v2.json" in patch

    # v1 is untouched by the v2 change (section 2.4).
    assert sync.read_version("workflow", "recon", 1) == {
        "graph_ref": "graphs/recon.v1.json",
        "schema_version": 1,
    }
