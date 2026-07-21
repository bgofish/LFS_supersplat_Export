from __future__ import annotations

import re
import unittest
from pathlib import Path

import supersplat
from supersplat.package_info import PLUGIN_VERSION, UPLOAD_CLIENT_ID, USER_AGENT


class PackageInfoTests(unittest.TestCase):
    def test_runtime_version_matches_project_manifest(self) -> None:
        manifest = Path(__file__).resolve().parent.parent / "pyproject.toml"
        match = re.search(
            r'(?m)^version\s*=\s*"([^"]+)"\s*$',
            manifest.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(match)
        self.assertEqual(PLUGIN_VERSION, match.group(1))  # type: ignore[union-attr]
        self.assertEqual(supersplat.__version__, PLUGIN_VERSION)

    def test_api_identity_uses_canonical_client_and_version(self) -> None:
        self.assertEqual(UPLOAD_CLIENT_ID, "supersplat-lichtfeld-plugin")
        self.assertEqual(USER_AGENT, f"{UPLOAD_CLIENT_ID}/{PLUGIN_VERSION}")


if __name__ == "__main__":
    unittest.main()
