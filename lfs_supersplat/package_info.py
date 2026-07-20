"""Canonical identity and version metadata for API attribution."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path

DISTRIBUTION_NAME = "lfs_supersplat"
UPLOAD_CLIENT_ID = "supersplat-lichtfeld-plugin"
SOFTWARE_TOOLS = ("lichtfeld-studio",)


def _source_manifest_version() -> str | None:
    """Read project.version when running directly from the plugin source tree."""
    manifest = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not manifest.is_file():
        return None
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None

    project = re.search(
        r"(?ms)^\[project\]\s*$\s*(.*?)(?=^\[|\Z)",
        text,
    )
    if project is None:
        return None
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project.group(1))
    return match.group(1) if match else None


def _resolve_version() -> str:
    source_version = _source_manifest_version()
    if source_version:
        return source_version
    try:
        return distribution_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return "0.0.0+unknown"


PLUGIN_VERSION = _resolve_version()
USER_AGENT = f"{UPLOAD_CLIENT_ID}/{PLUGIN_VERSION}"
