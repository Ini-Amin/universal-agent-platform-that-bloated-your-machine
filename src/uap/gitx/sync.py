"""Definition <-> Git synchronisation (Master sections 2.4, 49, 50).

The Global Library keeps definitions in the database; this module mirrors them
onto disk inside a git working tree so that every version is a real, reviewable
document and every AI-generated change is visible in history (section 49:
"AI-generated changes must appear in history").

Layout (one file per immutable version, section 50's reproducible package)::

    <definitions_root>/
    ├── agent/<name>/v<N>.json
    ├── tool/<name>/v<N>.json
    ├── skill/<name>/v<N>.json
    └── workflows/<name>/v<N>.graph.json

Rules:

* A version file is **canonical JSON**: keys sorted, UTF-8, trailing newline.
  Identical content therefore produces identical bytes, so a no-op export never
  creates a spurious commit.
* A commit message always names the kind, the definition and the version -
  ``workflow(recon): v2`` - and the author carries the acting identity, so an
  AI change is distinguishable from a human one at a glance (section 49).
* Every path component (``kind``, ``name``) is validated: a definition name can
  never escape the definitions root.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from uap.graph.model import WorkflowGraph

from .repo import GitError, GitRepo

__all__ = ["DefinitionGitSync"]

# Workflow *graphs* are exported under "workflows/" (section 50 layout).
# Definition specs use their own kind as the directory name.
_GRAPH_KIND = "workflows"


class DefinitionGitSync:
    """Mirror versioned definitions into a git repository, one file per version."""

    def __init__(self, repo: GitRepo, definitions_root: Path | str) -> None:
        self.repo = repo
        repo_root = Path(repo.path).resolve()
        root = Path(definitions_root)
        if not root.is_absolute():
            root = repo_root / root
        root = root.resolve()
        if root != repo_root and repo_root not in root.parents:
            raise ValueError(
                f"definitions_root {str(definitions_root)!r} must live inside "
                f"the repository {repo_root}"
            )
        self.definitions_root = root

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #

    @staticmethod
    def _safe_component(value: object, field: str) -> str:
        """Validate one path segment; reject anything that could escape."""
        text = str(value)
        if not text or not text.strip():
            raise ValueError(f"{field} must be a non-empty string")
        if text in {".", ".."}:
            raise ValueError(f"{field} {text!r} is not a valid path component")
        if "/" in text or "\\" in text or ".." in text:
            raise ValueError(
                f"{field} {text!r} must not contain path separators or '..'"
            )
        return text

    def _kind_dir(self, kind: str) -> str:
        """Directory for a kind's *spec* files (literal, per the pinned layout)."""
        return self._safe_component(kind, "kind")

    def _version_path(self, kind: str, name: str, version: int) -> Path:
        safe_name = self._safe_component(name, "name")
        safe_version = int(version)
        if safe_version < 1:
            raise ValueError(f"version must be >= 1, got {version}")
        return (
            self.definitions_root
            / self._kind_dir(kind)
            / safe_name
            / f"v{safe_version}.json"
        )

    def _graph_path(self, name: str, version: int) -> Path:
        safe_name = self._safe_component(name, "name")
        safe_version = int(version)
        if safe_version < 1:
            raise ValueError(f"version must be >= 1, got {version}")
        return (
            self.definitions_root
            / _GRAPH_KIND
            / safe_name
            / f"v{safe_version}.graph.json"
        )

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    @staticmethod
    def _write_canonical(path: Path, payload: dict[str, Any]) -> Path:
        """Write ``payload`` as canonical JSON (sorted keys + trailing newline)."""
        text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def export_version(
        self, kind: str, name: str, version: int, spec: dict
    ) -> Path:
        """Write ``<root>/<kind>/<name>/v<version>.json`` and return the path."""
        if not isinstance(spec, dict):
            raise ValueError("spec must be a JSON-serialisable dict")
        path = self._version_path(kind, name, version)
        return self._write_canonical(path, spec)

    def export_graph(self, name: str, version: int, graph: WorkflowGraph) -> Path:
        """Write ``<root>/workflows/<name>/v<version>.graph.json``.

        Serialised via :meth:`WorkflowGraph.to_dict`, so re-reading it and
        rebuilding with :meth:`WorkflowGraph.from_dict` yields the same content
        hash (the git pin, section 2.4).
        """
        if not isinstance(graph, WorkflowGraph):
            raise ValueError("graph must be a WorkflowGraph")
        path = self._graph_path(name, version)
        return self._write_canonical(path, graph.to_dict())

    def read_version(self, kind: str, name: str, version: int) -> dict | None:
        """Return the exported spec dict, or ``None`` when it does not exist."""
        path = self._version_path(kind, name, version)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ #
    # Commit / history
    # ------------------------------------------------------------------ #

    def commit_definition(
        self, kind: str, name: str, version: int, *, actor: str = "uap"
    ) -> str | None:
        """Commit the whole working tree under a deterministic message.

        Message: ``f"{kind}({name}): v{version}"`` (e.g. ``workflow(recon): v2``).
        Returns the commit SHA, or ``None`` when there was nothing to commit.
        The ``actor`` becomes the git author, so AI edits stay visible (section 49).
        """
        safe_kind = str(kind).strip()
        safe_name = self._safe_component(name, "name")
        message = f"{safe_kind}({safe_name}): v{int(version)}"
        return self.repo.commit_all(message, author=actor)

    def history(self, kind: str, name: str) -> list[dict]:
        """Commits touching the version directory of one definition."""
        safe_name = self._safe_component(name, "name")
        directory = self.definitions_root / self._kind_dir(kind) / safe_name
        relative = directory.relative_to(Path(self.repo.path).resolve()).as_posix()
        return self.repo.file_history(relative)

    # ------------------------------------------------------------------ #
    # Diff
    # ------------------------------------------------------------------ #

    def _commit_for(self, path: Path) -> str | None:
        relative = path.relative_to(Path(self.repo.path).resolve()).as_posix()
        entries = self.repo.file_history(relative, max_count=1)
        return entries[0]["sha"] if entries else None

    def diff_versions(self, kind: str, name: str, v_a: int, v_b: int) -> str:
        """Unified diff between two exported versions of the same definition."""
        path_a = self._version_path(kind, name, v_a)
        path_b = self._version_path(kind, name, v_b)
        if not path_a.is_file():
            raise GitError(f"no exported version {v_a} for {kind} {name!r}")
        if not path_b.is_file():
            raise GitError(f"no exported version {v_b} for {kind} {name!r}")

        sha_a = self._commit_for(path_a)
        sha_b = self._commit_for(path_b)
        if sha_a and sha_b:
            root = Path(self.repo.path).resolve()
            rel_a = path_a.relative_to(root).as_posix()
            rel_b = path_b.relative_to(root).as_posix()
            return self.repo.diff_blobs(f"{sha_a}:{rel_a}", f"{sha_b}:{rel_b}")

        # Uncommitted fallback: a plain textual diff, still deterministic.
        return "".join(
            difflib.unified_diff(
                path_a.read_text(encoding="utf-8").splitlines(keepends=True),
                path_b.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile=path_a.name,
                tofile=path_b.name,
            )
        )
