"""Filesystem-first artifact store (Master section 19).

Content is written to ``<root>/<task_id>/<artifact_id>__v<version>__<type>``
next to a JSON sidecar ``<file>.meta.json`` holding the full ``Artifact``
model. The sidecar is the metadata source of truth for ``get()``/``list()``.
A later SQLite-backed store replaces this module behind the same interface.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from pydantic import ValidationError

from uap.contracts import Artifact, ArtifactStatus

_META_SUFFIX = ".meta.json"
_TYPE_UNSAFE = re.compile(r"[/\\\s]+")


def _sanitize_id(value: object, field: str) -> str:
    """Reject any id that could escape its task directory."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field} must not be empty")
    if "\x00" in candidate or os.sep in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError(f"{field} contains a path separator: {value!r}")
    if candidate in {".", ".."}:
        raise ValueError(f"{field} escapes the store root: {value!r}")
    return candidate


def _sanitize_type(type_str: object) -> str:
    """Make an artifact type safe to embed in a filename."""
    if not isinstance(type_str, str):
        raise TypeError("artifact type must be a string")
    cleaned = _TYPE_UNSAFE.sub("_", type_str).strip("._")
    return cleaned or "artifact"


class ArtifactStore:
    """A deterministic, dependency-free artifact store rooted at ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- internals --------------------------------------------------------- #

    def _task_dir(self, task_id: str) -> Path:
        directory = (self.root / _sanitize_id(task_id, "task_id")).resolve()
        if not directory.is_relative_to(self.root) or directory == self.root:
            raise ValueError(f"task_id escapes the store root: {task_id!r}")
        return directory

    @staticmethod
    def _content_rel(artifact: Artifact) -> Path:
        task_id = _sanitize_id(artifact.task_id, "task_id")
        artifact_id = _sanitize_id(artifact.artifact_id, "artifact_id")
        name = f"{artifact_id}__v{artifact.version}__{_sanitize_type(artifact.type)}"
        return Path(task_id) / name

    @staticmethod
    def _write(target: Path, payload: bytes) -> None:
        """Write atomically so a crash never leaves a torn half-file."""
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, target)

    @classmethod
    def _write_meta(cls, artifact: Artifact, content_path: Path) -> None:
        cls._write(
            content_path.with_name(content_path.name + _META_SUFFIX),
            artifact.model_dump_json(indent=2).encode("utf-8"),
        )

    def _read_meta(self, meta_path: Path) -> Artifact | None:
        try:
            return Artifact.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, ValueError):
            return None

    # -- public API -------------------------------------------------------- #

    def save(self, artifact: Artifact, content: str | bytes) -> Artifact:
        """Persist ``content`` plus a metadata sidecar; returns the stored artifact."""
        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray, memoryview)):
            payload = bytes(content)
        else:
            raise TypeError("content must be str or bytes")

        rel = self._content_rel(artifact)
        # Same root-containment guard as get()/list()/delete_task(): a task
        # directory that is a symlink out of the store must not be written
        # through either (regression test in tests/test_store_containment.py).
        directory = self._task_dir(artifact.task_id)
        directory.mkdir(parents=True, exist_ok=True)
        content_path = directory / rel.name
        self._write(content_path, payload)

        stored = artifact.model_copy(update={"uri": rel.as_posix()})
        self._write_meta(stored, content_path)
        return stored

    def get(
        self, task_id: str, artifact_id: str, version: int | None = None
    ) -> tuple[Artifact, bytes] | None:
        """Return ``(artifact, content)``; ``version=None`` resolves the latest."""
        wanted = _sanitize_id(artifact_id, "artifact_id")
        directory = self._task_dir(task_id)
        if not directory.is_dir():
            return None

        found: list[Artifact] = []
        for meta_path in directory.glob("*" + _META_SUFFIX):
            artifact = self._read_meta(meta_path)
            if artifact is not None and artifact.artifact_id == wanted:
                found.append(artifact)
        if not found:
            return None

        if version is None:
            artifact = max(found, key=lambda item: item.version)
        else:
            matches = [item for item in found if item.version == version]
            if not matches:
                return None
            artifact = matches[0]

        content_path = self.root / self._content_rel(artifact)
        try:
            return artifact, content_path.read_bytes()
        except OSError:
            return None

    def list(self, task_id: str) -> list[Artifact]:
        """All artifacts for a task, oldest first; empty for an unknown task."""
        directory = self._task_dir(task_id)
        if not directory.is_dir():
            return []
        artifacts = []
        for meta_path in directory.glob("*" + _META_SUFFIX):
            artifact = self._read_meta(meta_path)
            if artifact is not None:
                artifacts.append(artifact)
        artifacts.sort(key=lambda item: (item.created_at, item.version))
        return artifacts

    def new_version(self, artifact: Artifact, content: str | bytes) -> Artifact:
        """Save ``content`` as ``version + 1`` under the same ``artifact_id``."""
        bumped = artifact.model_copy(update={"version": artifact.version + 1})
        return self.save(bumped, content)

    def supersede(self, artifact: Artifact) -> Artifact:
        """Mark the artifact SUPERSEDED and persist that in its sidecar."""
        superseded = artifact.model_copy(update={"status": ArtifactStatus.SUPERSEDED})
        content_path = self.root / self._content_rel(superseded)
        if not content_path.is_file():
            raise FileNotFoundError(f"artifact content not found: {content_path}")
        self._write_meta(superseded, content_path)
        return superseded

    def delete_task(self, task_id: str) -> int:
        """Remove every file for a task; returns the number of files removed."""
        directory = self._task_dir(task_id)
        if not directory.is_dir():
            return 0
        removed = 0
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                removed += sum(1 for child in entry.rglob("*") if child.is_file())
                shutil.rmtree(entry)
            else:
                entry.unlink()
                removed += 1
        directory.rmdir()
        return removed


__all__ = ["ArtifactStore"]
