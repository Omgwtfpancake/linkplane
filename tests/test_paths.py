"""`linkplane.paths`: new locations first, pre-rename locations as a fallback (ADR 0010)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkplane import paths
from linkplane.backup import LEGACY_MANIFEST_NAME, MANIFEST_NAME, load_manifest, new_manifest, save_manifest


class PathsTests(unittest.TestCase):
    def test_new_location_wins_when_present(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"XDG_CONFIG_HOME": home, "LINKPLANE_CONFIG": "", "PHONEBRIDGE_CONFIG": ""}):
            (Path(home) / "linkplane").mkdir()
            (Path(home) / "phonebridge").mkdir()
            self.assertEqual(paths.config_dir(), Path(home) / "linkplane")

    def test_legacy_location_is_used_when_only_it_exists(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"XDG_CONFIG_HOME": home, "XDG_STATE_HOME": home, "LINKPLANE_CONFIG": "", "PHONEBRIDGE_CONFIG": ""}):
            (Path(home) / "phonebridge").mkdir()
            (Path(home) / "phonebridge" / "config.json").write_text("{}")
            self.assertEqual(paths.config_dir(), Path(home) / "phonebridge")
            self.assertEqual(paths.state_dir(), Path(home) / "phonebridge")
            with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", home)):
                self.assertEqual(paths.config_file("config.json"), Path(home) / "phonebridge" / "config.json")

    def test_fresh_machine_uses_new_names(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"XDG_CONFIG_HOME": home, "XDG_STATE_HOME": home, "XDG_RUNTIME_DIR": home}):
            self.assertEqual(paths.config_dir(), Path(home) / "linkplane")
            self.assertEqual(paths.state_dir(), Path(home) / "linkplane")
            self.assertEqual(paths.runtime_dir(), Path(home) / "linkplane")

    def test_environment_overrides_new_then_legacy(self):
        with patch.dict(os.environ, {"LINKPLANE_HISTORY": "/tmp/new.jsonl", "PHONEBRIDGE_HISTORY": "/tmp/old.jsonl"}):
            self.assertEqual(paths.env("HISTORY"), "/tmp/new.jsonl")
            self.assertEqual(paths.state_file("events.jsonl", env_name="HISTORY"), Path("/tmp/new.jsonl"))
        with patch.dict(os.environ, {"LINKPLANE_HISTORY": "", "PHONEBRIDGE_HISTORY": "/tmp/old.jsonl"}):
            self.assertEqual(paths.env("HISTORY"), "/tmp/old.jsonl")
        self.assertEqual(paths.state_file("x", "/explicit/path"), Path("/explicit/path"))


class LegacyManifestTests(unittest.TestCase):
    def test_pre_rename_backup_manifest_is_read_and_continued(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / LEGACY_MANIFEST_NAME
            manifest = new_manifest("S1", "/sdcard/DCIM/Camera")
            manifest["files"]["a.jpg"] = {"size": 1}
            save_manifest(legacy, manifest)
            loaded = load_manifest(Path(directory) / MANIFEST_NAME, "S1", "/sdcard/DCIM/Camera")
            self.assertIn("a.jpg", loaded["files"])

    def test_missing_both_starts_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_manifest(Path(directory) / MANIFEST_NAME, "S1", "/x")
            self.assertEqual(loaded["files"], {})


if __name__ == "__main__":
    unittest.main()
