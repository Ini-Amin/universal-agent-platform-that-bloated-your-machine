"""Reproducible packages (Master sections 50, 51).

Public API::

    from uap.packages import (
        PackageManifest,
        PackageExporter,
        PackageImporter,
    )

    exporter = PackageExporter(definitions_root=root)
    package_dir = exporter.export(
        "my-pkg",
        definition_refs=["agent:recon@v1"],
        graph_refs=["recon@v1"],
        out_dir=out,
    )
    ok, reason = exporter.verify(package_dir)

    importer = PackageImporter(definitions_root=target_root)
    plan = importer.plan(package_dir)
    importer.import_package(package_dir, overwrite=False)
"""

from .exporter import PackageExporter
from .importer import PackageImporter
from .manifest import PackageManifest

__all__ = ["PackageExporter", "PackageImporter", "PackageManifest"]
