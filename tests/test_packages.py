"""Tests for reproducible package export/import (Master sections 50, 51)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uap.gitx.compat import CompatibilityChecker
from uap.packages import PackageExporter, PackageImporter, PackageManifest
from uap.packages.manifest import content_hash_of


def _seed_root(root: Path) -> None:
    """Write one agent definition + one workflow graph in the pinned layout."""
    agent = root / "agent" / "recon" / "v1.json"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(json.dumps({"model": "x", "schema_version": 1}, sort_keys=True), encoding="utf-8")
    graph = root / "workflows" / "recon" / "v1.graph.json"
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text(json.dumps({"nodes": [], "schema_version": 1}, sort_keys=True), encoding="utf-8")


def _export(tmp_path: Path, name: str = "pkg") -> tuple[PackageExporter, Path]:
    root = tmp_path / "defs"
    _seed_root(root)
    exporter = PackageExporter(definitions_root=root)
    out = tmp_path / "out"
    package_dir = exporter.export(
        name,
        definition_refs=["agent:recon@v1"],
        graph_refs=["recon@v1"],
        out_dir=out,
        dependencies=["pydantic>=2"],
    )
    return exporter, package_dir


def test_export_writes_manifest_and_copies_files(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    assert (package_dir / "manifest.json").is_file()
    assert (package_dir / "agent" / "recon" / "v1.json").is_file()
    assert (package_dir / "workflows" / "recon" / "v1.graph.json").is_file()
    manifest = PackageManifest.model_validate_json(
        (package_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.name == "pkg"
    assert manifest.dependencies == ["pydantic>=2"]
    assert len(manifest.definitions) == 1
    assert len(manifest.graphs) == 1


def test_missing_ref_file_raises_filenotfound_naming_it(tmp_path: Path) -> None:
    root = tmp_path / "defs"
    _seed_root(root)
    exporter = PackageExporter(definitions_root=root)
    with pytest.raises(FileNotFoundError, match="agent:ghost@v9"):
        exporter.export(
            "p",
            definition_refs=["agent:ghost@v9"],
            graph_refs=[],
            out_dir=tmp_path / "out",
        )


def test_verify_ok_on_fresh_export(tmp_path: Path) -> None:
    exporter, package_dir = _export(tmp_path)
    ok, reason = exporter.verify(package_dir)
    assert ok is True
    assert reason == "ok"


def test_verify_detects_tampered_file(tmp_path: Path) -> None:
    exporter, package_dir = _export(tmp_path)
    tampered = package_dir / "agent" / "recon" / "v1.json"
    tampered.write_text("{\"model\": \"EVIL\"}", encoding="utf-8")
    ok, reason = exporter.verify(package_dir)
    assert ok is False
    assert "tampered" in reason
    assert "agent/recon/v1.json" in reason


def test_verify_detects_deleted_file(tmp_path: Path) -> None:
    exporter, package_dir = _export(tmp_path)
    (package_dir / "workflows" / "recon" / "v1.graph.json").unlink()
    ok, reason = exporter.verify(package_dir)
    assert ok is False
    assert "missing" in reason
    assert "workflows/recon/v1.graph.json" in reason


def test_content_hash_stable_across_identical_exports(tmp_path: Path) -> None:
    root = tmp_path / "defs"
    _seed_root(root)
    exporter = PackageExporter(definitions_root=root)
    dir_a = exporter.export("a", definition_refs=["agent:recon@v1"], graph_refs=["recon@v1"], out_dir=tmp_path / "oa")
    dir_b = exporter.export("a", definition_refs=["agent:recon@v1"], graph_refs=["recon@v1"], out_dir=tmp_path / "ob")
    man_a = PackageManifest.model_validate_json((dir_a / "manifest.json").read_text(encoding="utf-8"))
    man_b = PackageManifest.model_validate_json((dir_b / "manifest.json").read_text(encoding="utf-8"))
    # package_id/created_at differ, but the file content hashes are identical.
    assert [d["content_hash"] for d in man_a.definitions] == [d["content_hash"] for d in man_b.definitions]
    assert [g["content_hash"] for g in man_a.graphs] == [g["content_hash"] for g in man_b.graphs]


def test_plan_fresh_package_no_conflicts(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    target_root = tmp_path / "target"
    plan = PackageImporter(definitions_root=target_root).plan(package_dir)
    assert plan["conflicts"] == []
    assert plan["definitions"] == [{"kind": "agent", "name": "recon", "version": 1}]
    assert plan["graphs"] == [{"name": "recon", "version": 1}]


def test_plan_detects_conflict(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    target_root = tmp_path / "target"
    # Seed the target with a DIFFERENT copy of the same ref.
    existing = target_root / "agent" / "recon" / "v1.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("{\"model\": \"different\"}", encoding="utf-8")
    plan = PackageImporter(definitions_root=target_root).plan(package_dir)
    assert len(plan["conflicts"]) == 1
    assert plan["conflicts"][0]["path"] == "agent/recon/v1.json"


def test_import_fresh_files_present(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    target_root = tmp_path / "target"
    importer = PackageImporter(definitions_root=target_root)
    result = importer.import_package(package_dir)
    assert (target_root / "agent" / "recon" / "v1.json").is_file()
    assert (target_root / "workflows" / "recon" / "v1.graph.json").is_file()
    assert "agent/recon/v1.json" in result["imported"]


def test_import_conflict_requires_overwrite(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    target_root = tmp_path / "target"
    existing = target_root / "agent" / "recon" / "v1.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("{\"model\": \"different\"}", encoding="utf-8")
    importer = PackageImporter(definitions_root=target_root)
    with pytest.raises(ValueError, match="agent/recon/v1.json"):
        importer.import_package(package_dir)
    # With overwrite the conflicting file is replaced by the package copy.
    importer.import_package(package_dir, overwrite=True)
    replaced = json.loads(existing.read_text(encoding="utf-8"))
    assert replaced["model"] == "x"


def test_check_compatibility_schema_versions(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    importer = PackageImporter(definitions_root=tmp_path / "t")

    current = importer.check_compatibility(package_dir, checker=CompatibilityChecker(current_schema_version=1))
    assert current.ok is True
    assert current.issues == []

    older = importer.check_compatibility(package_dir, checker=CompatibilityChecker(current_schema_version=2))
    assert older.ok is True
    assert older.warnings and older.warnings[0].code == "outdated_schema_version"

    # Package schema newer than the platform -> error "migration required".
    newer_pkg = tmp_path / "newer"
    newer_pkg.mkdir()
    manifest = PackageManifest.model_validate_json((package_dir / "manifest.json").read_text(encoding="utf-8"))
    bumped = manifest.model_copy(update={"schema_version": 5})
    bumped = bumped.model_copy(update={"content_hash": content_hash_of(bumped)})
    (newer_pkg / "manifest.json").write_text(bumped.model_dump_json(), encoding="utf-8")
    newer = importer.check_compatibility(newer_pkg, checker=CompatibilityChecker(current_schema_version=1))
    assert newer.ok is False
    assert "migration" in newer.errors[0].message


def test_manifest_json_round_trip_and_content_hash_excludes_itself(tmp_path: Path) -> None:
    _, package_dir = _export(tmp_path)
    manifest = PackageManifest.model_validate_json(
        (package_dir / "manifest.json").read_text(encoding="utf-8")
    )
    restored = PackageManifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest
    # Recomputing the hash (which excludes content_hash) matches what was stored.
    assert content_hash_of(manifest) == manifest.content_hash
