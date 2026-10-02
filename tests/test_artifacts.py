"""Tests for the filesystem artifact store (Master section 19)."""

from pathlib import Path

import pytest

from uap.contracts import Artifact, ArtifactStatus
from uap.artifacts import ArtifactStore

PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
INDONESIAN = "Hasil riset: belajar arsitektur agen dengan cepat."


def make_artifact(task_id="task-1", type_="report.md", source="research", **kwargs) -> Artifact:
    return Artifact(task_id=task_id, type=type_, source=source, **kwargs)


def test_save_writes_content_and_sets_uri(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    artifact = make_artifact()

    stored = store.save(artifact, "hello world")

    assert stored.uri is not None
    assert stored.status == ArtifactStatus.DRAFT
    content_path = tmp_path / "store" / stored.uri
    assert content_path.read_bytes() == b"hello world"


def test_sidecar_validates_as_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    artifact = make_artifact()

    stored = store.save(artifact, "body")

    content_path = tmp_path / "store" / stored.uri
    meta_path = content_path.parent / (content_path.name + ".meta.json")
    assert meta_path.is_file()
    reloaded = Artifact.model_validate_json(meta_path.read_text())
    assert reloaded.artifact_id == stored.artifact_id
    assert reloaded.task_id == artifact.task_id
    assert reloaded.type == artifact.type
    assert reloaded.version == stored.version
    assert reloaded.source == artifact.source
    assert reloaded.uri == stored.uri


def test_get_round_trip_is_identical(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    stored = store.save(make_artifact(), "round trip")

    got = store.get(stored.task_id, stored.artifact_id)

    assert got is not None
    got_artifact, got_content = got
    assert got_artifact.model_dump() == stored.model_dump()
    assert got_content == b"round trip"


def test_list_returns_task_artifacts_and_empty_for_unknown(tmp_path):
    store = ArtifactStore(tmp_path / "store")

    assert store.list("task-1") == []
    a1 = store.save(make_artifact(task_id="task-1", type_="report.md"), "one")
    a2 = store.save(make_artifact(task_id="task-1", type_="sources.json"), "two")

    listed = store.list("task-1")
    assert {item.artifact_id for item in listed} == {a1.artifact_id, a2.artifact_id}
    assert store.list("nope") == []


def test_new_version_bumps_version_keeps_id(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    v1 = store.save(make_artifact(), "first")

    v2 = store.new_version(v1, "second")

    assert v2.version == 2
    assert v2.artifact_id == v1.artifact_id
    assert (tmp_path / "store" / v1.uri).is_file()
    assert (tmp_path / "store" / v2.uri).is_file()

    got_v1 = store.get(v1.task_id, v1.artifact_id, version=1)
    assert got_v1 is not None and got_v1[1] == b"first"
    assert got_v1[0].version == 1


def test_get_without_version_returns_latest(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    v1 = store.save(make_artifact(), "first")
    store.new_version(v1, "second")

    got = store.get(v1.task_id, v1.artifact_id)

    assert got is not None
    assert got[0].version == 2
    assert got[1] == b"second"


def test_supersede_persists_status(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    v1 = store.save(make_artifact(), "first")
    v2 = store.new_version(v1, "second")

    superseded = store.supersede(v1)

    assert superseded.status == ArtifactStatus.SUPERSEDED
    fresh = store.get(v1.task_id, v1.artifact_id, version=1)
    assert fresh is not None
    assert fresh[0].status == ArtifactStatus.SUPERSEDED
    assert fresh[0].artifact_id == v1.artifact_id
    # The other version is untouched.
    assert store.get(v1.task_id, v1.artifact_id, version=2)[0].status == v2.status


def test_delete_task_returns_count_and_is_idempotent(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    v1 = store.save(make_artifact(), "first")
    store.new_version(v1, "second")  # 2 versions -> 4 files

    assert store.delete_task(v1.task_id) == 4
    assert store.list(v1.task_id) == []
    assert store.delete_task(v1.task_id) == 0
    assert not (tmp_path / "store" / v1.task_id).exists()


def test_task_id_path_traversal_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "store")

    with pytest.raises(ValueError):
        store.save(make_artifact(task_id="../evil"), "payload")
    with pytest.raises(ValueError):
        store.list("../evil")
    with pytest.raises(ValueError):
        store.delete_task("../evil")

    assert not (tmp_path / "evil").exists()
    assert list((tmp_path / "store").iterdir()) == []


def test_artifact_id_with_separator_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "store")

    with pytest.raises(ValueError):
        store.save(
            Artifact(task_id="task-1", artifact_id="a/b", type="report.md", source="s"), "payload"
        )
    with pytest.raises(ValueError):
        store.get("task-1", "a/b")

    assert store.list("task-1") == []


def test_two_tasks_are_isolated(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    t1 = store.save(make_artifact(task_id="task-1"), "one")
    t2 = store.save(make_artifact(task_id="task-2"), "two")

    listed = store.list(t1.task_id)
    assert [item.artifact_id for item in listed] == [t1.artifact_id]
    assert t2.artifact_id not in {item.artifact_id for item in listed}

    got = store.get(t1.task_id, t2.artifact_id)
    assert got is None


def test_binary_content_round_trip(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    payload = PNG_MAGIC + b"\x00\x01\x02\xff\xfe" * 10
    stored = store.save(make_artifact(type_="image.png"), payload)

    got = store.get(stored.task_id, stored.artifact_id)
    assert got is not None
    assert got[1] == payload


def test_utf8_indonesian_content_round_trip(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    stored = store.save(make_artifact(type_="laporan.md"), INDONESIAN)

    got = store.get(stored.task_id, stored.artifact_id)
    assert got is not None
    assert got[1].decode("utf-8") == INDONESIAN


def test_type_sanitized_and_status_preserved(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    stored = store.save(
        make_artifact(type_="my report/final v2", status=ArtifactStatus.FINAL), "body"
    )

    assert "/" not in Path(stored.uri).name and " " not in Path(stored.uri).name
    assert stored.status == ArtifactStatus.FINAL
    assert store.get(stored.task_id, stored.artifact_id)[0].status == ArtifactStatus.FINAL


def test_get_missing_returns_none(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    store.save(make_artifact(), "body")

    assert store.get("task-1", "does-not-exist") is None
    assert store.get("task-1", "known-id", version=99) is None


def test_str_and_bytes_content_paths(tmp_path):
    store = ArtifactStore(tmp_path / "store")
    a = store.save(make_artifact(type_="a.txt"), "text")
    b = store.save(make_artifact(type_="b.bin"), b"bytes")
    assert store.get(a.task_id, a.artifact_id)[1] == b"text"
    assert store.get(b.task_id, b.artifact_id)[1] == b"bytes"
