"""Regression tests: ``ArtifactStore.save()`` must stay inside the store root.

The hole this closes: ``get()`` / ``list()`` / ``delete_task()`` all resolve the
task directory and refuse one that escapes the root, but ``save()`` joined the
path lexically. A task directory that is a symlink pointing out of the store
therefore turned ``save()`` into an arbitrary-write primitive: the content file
and its sidecar landed wherever the symlink pointed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from uap.artifacts.store import ArtifactStore
from uap.contracts import Artifact


def _artifact(task_id: str, artifact_id: str = "a", **kw: object) -> Artifact:
    return Artifact(task_id=task_id, artifact_id=artifact_id, type="note", source="test", **kw)


def test_save_refuses_a_task_directory_that_symlinks_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = ArtifactStore(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "escape")

    with pytest.raises(ValueError, match="escapes the store root"):
        store.save(_artifact("escape"), "TOP SECRET")

    assert list(outside.iterdir()) == [], "save() wrote through the symlink"


def test_save_refuses_a_traversing_task_id(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    for task_id in ("..", "../evil", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            store.save(_artifact(task_id), "TOP SECRET")
    assert list((tmp_path / "store").iterdir()) == []


def test_save_keeps_writing_where_get_and_list_look(tmp_path: Path) -> None:
    """The guard must not move a legitimate artifact by one byte."""
    store = ArtifactStore(tmp_path / "store")
    stored = store.save(_artifact("task-1"), "hello")

    assert stored.uri == "task-1/a__v1__note"
    assert (tmp_path / "store" / "task-1" / "a__v1__note").read_bytes() == b"hello"
    assert (tmp_path / "store" / "task-1" / "a__v1__note.meta.json").is_file()

    found = store.get("task-1", "a")
    assert found is not None and found[1] == b"hello"
    assert [item.artifact_id for item in store.list("task-1")] == ["a"]


def test_new_version_stays_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = ArtifactStore(root)
    first = store.save(_artifact("task-1"), "v1")
    shutil.rmtree(root / "task-1")  # the task dir is swapped for a symlink
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "task-1")

    with pytest.raises(ValueError, match="escapes the store root"):
        store.new_version(first, "v2")

    assert list(outside.iterdir()) == []