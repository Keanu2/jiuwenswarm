# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve Designer catalog + skills + reports/trajectory package directory."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Current package name (catalog + skills + reports + trajectory).
_PACKAGE_DIRNAME = "designer_catalog_skills_reports_trajectory"
# Older checkouts may still use the previous folder name.
_LEGACY_PACKAGE_DIRNAMES = ("designer_catalog_and_skills",)
_CATALOG_JSON = "designer_node_catalog.json"
_CATALOG_TXT = "designer_node_catalog.txt"


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve()
    # .../jiuwenswarm/server/runtime/designer/paths.py
    repo_root = here.parents[4]
    package_root = here.parents[3]
    return [repo_root, package_root.parent, Path.cwd()]


@lru_cache(maxsize=1)
def designer_package_dir() -> Path:
    """Directory holding catalog JSON/TXT, skills/, and runs/ (reports + trajectory)."""
    names = (_PACKAGE_DIRNAME, *_LEGACY_PACKAGE_DIRNAMES)
    for root in _repo_roots():
        for name in names:
            candidate = root / name
            if (candidate / _CATALOG_JSON).is_file() or (candidate / "skills").is_dir():
                return candidate
    return _repo_roots()[0] / _PACKAGE_DIRNAME


def catalog_json_path() -> Path:
    return designer_package_dir() / _CATALOG_JSON


def catalog_txt_path() -> Path:
    return designer_package_dir() / _CATALOG_TXT


def skills_dir() -> Path:
    return designer_package_dir() / "skills"


def runs_dir() -> Path:
    """Per-run trajectory + supervisor/manager report bundles."""
    path = designer_package_dir() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_bundle_path(graph_id: str, run_id: str) -> Path:
    directory = runs_dir() / graph_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{run_id}.json"


def latest_run_bundle_path(graph_id: str) -> Path | None:
    directory = runs_dir() / graph_id
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Prefer real run bundles over the latest.json pointer.
    for path in files:
        if path.name != "latest.json":
            return path
    return None
