"""Export a reproducible package (Master section 50).

The exporter gathers already-exported definition and graph files (written to
``definitions_root`` by :class:`~uap.gitx.sync.DefinitionGitSync`) into a
self-contained package directory with a canonical :class:`PackageManifest`.

Reference formats:

* definition ref -- ``"<kind>:<name>@v<N>"`` -> ``<root>/<kind>/<name>/v<N>.json``
* graph ref      -- ``"<name>@v<N>"``         -> ``<root>/workflows/<name>/v<N>.graph.json``

Each copied file is hashed (sha256 of its bytes) and recorded in the manifest,
so the package can be verified byte-for-byte later. Nothing calls git here --
``git_sync`` is accepted only to reuse its deterministic path layout.
"""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
from pathlib import Path

from .manifest import PackageManifest, canonical_json, content_hash_of

__all__ = ["PackageExporter"]

_DEF_REF = re.compile(r"^(?P<kind>[^:@\s]+):(?P<name>[^:@\s]+)@v(?P<version>\d+)$")
_GRAPH_REF = re.compile(r"^(?P<name>[^:@\s]+)@v(?P<version>\d+)$")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PackageExporter:
    """Collect pinned definition/graph files into a reproducible package."""

    def __init__(
        self,
        *,
        definitions_root: Path,
        git_sync: object | None = None,
    ) -> None:
        self.definitions_root = Path(definitions_root)
        self.git_sync = git_sync

    # ------------------------------------------------------------------ #
    # Ref -> source path
    # ------------------------------------------------------------------ #

    def _definition_source(self, ref: str) -> tuple[dict, Path, str]:
        match = _DEF_REF.match(ref)
        if match is None:
            raise ValueError(
                f"definition ref {ref!r} must be '<kind>:<name>@v<N>'"
            )
        kind, name, version = (
            match["kind"],
            match["name"],
            int(match["version"]),
        )
        rel = f"{kind}/{name}/v{version}.json"
        return (
            {"kind": kind, "name": name, "version": version},
            self.definitions_root / kind / name / f"v{version}.json",
            rel,
        )

    def _graph_source(self, ref: str) -> tuple[dict, Path, str]:
        match = _GRAPH_REF.match(ref)
        if match is None:
            raise ValueError(f"graph ref {ref!r} must be '<name>@v<N>'")
        name, version = match["name"], int(match["version"])
        rel = f"workflows/{name}/v{version}.graph.json"
        return (
            {"name": name, "version": version},
            self.definitions_root / "workflows" / name / f"v{version}.graph.json",
            rel,
        )

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    def export(
        self,
        name: str,
        *,
        definition_refs: list[str],
        graph_refs: list[str],
        out_dir: Path,
        dependencies: list[str] | None = None,
    ) -> Path:
        """Write ``<out_dir>/<name>/`` with a manifest and the referenced files."""
        package_dir = Path(out_dir) / name
        package_dir.mkdir(parents=True, exist_ok=True)

        def_entries: list[dict] = []
        for ref in definition_refs:
            entry, src, rel = self._definition_source(ref)
            if not src.is_file():
                raise FileNotFoundError(
                    f"definition file for {ref!r} not found at {src}"
                )
            self._copy(src, package_dir / rel)
            entry["content_hash"] = _sha256_file(src)
            def_entries.append(entry)

        graph_entries: list[dict] = []
        for ref in graph_refs:
            entry, src, rel = self._graph_source(ref)
            if not src.is_file():
                raise FileNotFoundError(
                    f"graph file for {ref!r} not found at {src}"
                )
            self._copy(src, package_dir / rel)
            entry["content_hash"] = _sha256_file(src)
            graph_entries.append(entry)

        manifest = PackageManifest(
            name=name,
            definitions=def_entries,
            graphs=graph_entries,
            python_version=platform.python_version(),
            dependencies=list(dependencies or []),
        )
        manifest = manifest.model_copy(
            update={"content_hash": content_hash_of(manifest)}
        )
        self._write_manifest(package_dir / "manifest.json", manifest)
        return package_dir

    @staticmethod
    def _copy(src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    @staticmethod
    def _write_manifest(path: Path, manifest: PackageManifest) -> None:
        payload = manifest.model_dump(mode="json")
        path.write_text(canonical_json(payload) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Verify
    # ------------------------------------------------------------------ #

    def verify(self, package_dir: Path) -> tuple[bool, str]:
        """Re-hash the manifest + its files; report mismatches/missing files."""
        package_dir = Path(package_dir)
        manifest_path = package_dir / "manifest.json"
        if not manifest_path.is_file():
            return (False, f"manifest.json missing at {manifest_path}")
        manifest = PackageManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )

        recomputed = content_hash_of(manifest)
        if recomputed != manifest.content_hash:
            return (
                False,
                f"manifest content_hash mismatch: stored {manifest.content_hash}, "
                f"recomputed {recomputed}",
            )

        missing: list[str] = []
        tampered: list[str] = []
        for entry in manifest.definitions:
            rel = f"{entry['kind']}/{entry['name']}/v{entry['version']}.json"
            self._check_file(package_dir / rel, entry["content_hash"], rel, missing, tampered)
        for entry in manifest.graphs:
            rel = f"workflows/{entry['name']}/v{entry['version']}.graph.json"
            self._check_file(package_dir / rel, entry["content_hash"], rel, missing, tampered)

        if missing:
            return (False, f"missing files: {', '.join(sorted(missing))}")
        if tampered:
            return (False, f"tampered files: {', '.join(sorted(tampered))}")
        return (True, "ok")

    @staticmethod
    def _check_file(
        path: Path,
        expected_hash: str,
        rel: str,
        missing: list[str],
        tampered: list[str],
    ) -> None:
        if not path.is_file():
            missing.append(rel)
            return
        if _sha256_file(path) != expected_hash:
            tampered.append(rel)
