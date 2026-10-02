"""Import a reproducible package (Master sections 50, 51).

The importer plans and applies a package exported by
:class:`~uap.packages.exporter.PackageExporter`, copying its definition/graph
files into a target ``definitions_root`` using the same deterministic layout.

A *conflict* is a definition/graph of the same kind/name/version that already
exists in the target with a DIFFERENT content hash. Conflicts are never
silently overwritten (section 51: "do not silently replace it"): the caller must
pass ``overwrite=True`` explicitly.

Compatibility is checked against the running platform via a
:class:`~uap.gitx.compat.CompatibilityChecker`: a newer manifest schema is an
error ("migration required"), an older one a warning (section 51/63).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from uap.gitx.compat import CompatibilityIssue, CompatibilityReport

from .manifest import PackageManifest

__all__ = ["PackageImporter"]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PackageImporter:
    """Plan + apply a reproducible package into a target definitions root."""

    def __init__(self, *, definitions_root: Path) -> None:
        self.definitions_root = Path(definitions_root)

    # ------------------------------------------------------------------ #
    # Manifest + layout helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_manifest(package_dir: Path) -> PackageManifest:
        manifest_path = Path(package_dir) / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest.json missing at {manifest_path}")
        return PackageManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )

    @staticmethod
    def _def_rel(entry: dict) -> str:
        return f"{entry['kind']}/{entry['name']}/v{entry['version']}.json"

    @staticmethod
    def _graph_rel(entry: dict) -> str:
        return f"workflows/{entry['name']}/v{entry['version']}.graph.json"

    # ------------------------------------------------------------------ #
    # Plan
    # ------------------------------------------------------------------ #

    def plan(self, package_dir: Path) -> dict:
        """Describe what an import would add and which entries conflict."""
        package_dir = Path(package_dir)
        manifest = self._load_manifest(package_dir)
        conflicts: list[dict] = []

        definitions = [
            {"kind": e["kind"], "name": e["name"], "version": e["version"]}
            for e in manifest.definitions
        ]
        graphs = [
            {"name": e["name"], "version": e["version"]} for e in manifest.graphs
        ]

        for entry in manifest.definitions:
            rel = self._def_rel(entry)
            target = self.definitions_root / rel
            if target.is_file() and _sha256_file(target) != entry["content_hash"]:
                conflicts.append(
                    {
                        "kind": entry["kind"],
                        "name": entry["name"],
                        "version": entry["version"],
                        "path": rel,
                    }
                )
        for entry in manifest.graphs:
            rel = self._graph_rel(entry)
            target = self.definitions_root / rel
            if target.is_file() and _sha256_file(target) != entry["content_hash"]:
                conflicts.append(
                    {
                        "kind": "workflow",
                        "name": entry["name"],
                        "version": entry["version"],
                        "path": rel,
                    }
                )
        return {"definitions": definitions, "graphs": graphs, "conflicts": conflicts}

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_package(self, package_dir: Path, *, overwrite: bool = False) -> dict:
        """Copy package files into ``definitions_root``; refuse silent conflicts."""
        package_dir = Path(package_dir)
        plan = self.plan(package_dir)
        if plan["conflicts"] and not overwrite:
            listed = ", ".join(c["path"] for c in plan["conflicts"])
            raise ValueError(
                f"import would overwrite {len(plan['conflicts'])} conflicting "
                f"file(s) with different content: {listed}; pass overwrite=True "
                f"to replace them"
            )

        manifest = self._load_manifest(package_dir)
        imported: list[str] = []
        for entry in manifest.definitions:
            rel = self._def_rel(entry)
            self._copy(package_dir / rel, self.definitions_root / rel)
            imported.append(rel)
        for entry in manifest.graphs:
            rel = self._graph_rel(entry)
            self._copy(package_dir / rel, self.definitions_root / rel)
            imported.append(rel)
        return {"imported": imported, "overwrote": [c["path"] for c in plan["conflicts"]]}

    @staticmethod
    def _copy(src: Path, dest: Path) -> None:
        if not src.is_file():
            raise FileNotFoundError(f"package file missing: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    # ------------------------------------------------------------------ #
    # Compatibility
    # ------------------------------------------------------------------ #

    def check_compatibility(
        self, package_dir: Path, *, checker: object
    ) -> CompatibilityReport:
        """Check the manifest's schema version against the running platform."""
        manifest = self._load_manifest(Path(package_dir))
        current = getattr(checker, "current_schema_version", 1)
        issues: list[CompatibilityIssue] = []
        if manifest.schema_version > current:
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="migration_required",
                    message=(
                        f"package schema v{manifest.schema_version} is newer than "
                        f"the supported v{current}; a migration is required before "
                        f"it can be imported"
                    ),
                )
            )
        elif manifest.schema_version < current:
            issues.append(
                CompatibilityIssue(
                    severity="warning",
                    code="outdated_schema_version",
                    message=(
                        f"package schema v{manifest.schema_version} is older than "
                        f"v{current}; consider migrating"
                    ),
                )
            )
        return CompatibilityReport(
            ok=not any(i.severity == "error" for i in issues),
            issues=issues,
        )
