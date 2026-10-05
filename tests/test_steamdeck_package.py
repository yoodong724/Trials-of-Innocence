"""Small SteamDeck archive fixtures verify payload preservation and packaging boundaries."""
from __future__ import annotations

import json
import copy
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import game_patch_linux as linux
import package_steamdeck_patch as packaging
from toi_common import ToiError, sha256_bytes
from test_game_patch_linux import fixture


class SteamDeckPackageTest(unittest.TestCase):
    def test_installer_mirror_matches_source(self):
        self.assertEqual((ROOT / "scripts/game_patch_linux.py").read_bytes(),
                         (ROOT / "installer/linux/patch.py").read_bytes())

    def source(self, root, extra=None):
        _, folder, manifest, originals, _ = fixture(root)
        entries = {"KoreanPatch/" + name: name.encode() for name in packaging.WINDOWS_EXTRAS}
        for name in ("patch-manifest.json", "patch-manifest.sha256"):
            entries["KoreanPatch/" + name] = (folder / name).read_bytes()
        for row in manifest["changes"]:
            name = "payload/" + row["payload_path"]
            entries["KoreanPatch/" + name] = (folder / name).read_bytes()
        if extra:
            entries[extra] = b"unexpected"
        source = root / "windows.zip"
        with zipfile.ZipFile(source, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        digest = sha256_bytes(source.read_bytes())
        source.with_name(source.name + ".sha256").write_text(digest + "  " + source.name + "\n")
        config = {"format": "toi-l10n/steamdeck-package-v1", "version": "v1.0.0", "revision": "test1",
                  "archive_stem": "TrialsOfInnocence-KoreanPatch-v1.0.0-SteamDeck-test1",
                  "source_archive": str(source), "source_size": source.stat().st_size,
                  "source_zip_sha256": digest, "source_patch_id": manifest["patch_id"],
                  "steam_appid": 2983140, "steam_build": 18618782,
                  "steam_build_update_date_kst": "2025-05-27"}
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        return config_path, config, entries, originals

    def test_payload_manifest_and_licenses_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, config, entries, originals = self.source(root)
            with patch.object(linux, "INVENTORY_COUNT", len(originals)):
                with patch.object(packaging, "sha256_file", wraps=packaging.sha256_file) as sha:
                    result = packaging.package(config_path, root / "release")
                self.assertEqual(sha.call_count, 1)
                self.assertEqual(result["steamdeck_runtime_test"], "pending")
                with zipfile.ZipFile(result["zip"]) as archive:
                    for name, data in entries.items():
                        if name in {"KoreanPatch/" + n for n in packaging.INHERITED} or "/payload/" in name:
                            self.assertEqual(archive.read(name), data, name)
                    self.assertNotIn("KoreanPatch/patch.ps1", archive.namelist())
                    for name in ("install.sh", "restore.sh"):
                        self.assertEqual(stat.S_IMODE(archive.getinfo("KoreanPatch/" + name).external_attr >> 16), 0o755)
                        self.assertNotIn(b"\r\n", archive.read("KoreanPatch/" + name))
                    metadata = json.loads(archive.read("KoreanPatch/steamdeck-info.json"))
                    self.assertFalse(metadata["payload_changed"])
                    self.assertEqual(metadata["steamdeck_runtime_test"], "pending")
                    readme = archive.read("KoreanPatch/README.ko.txt").decode("utf-8-sig")
                    self.assertIn("sh ./install.sh", readme)
                    self.assertIn("실제 기기", readme)
                with self.assertRaisesRegex(ToiError, "이미 존재"):
                    packaging.package(config_path, root / "release")

    def test_source_identity_and_archive_extras_cannot_publish(self):
        for extra in (None, "../outside.txt", "KoreanPatch/state.json"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path, config, _, originals = self.source(root, extra=extra)
                if extra is None:
                    config["source_patch_id"] = "0" * 64
                    config_path.write_text(json.dumps(config))
                with patch.object(linux, "INVENTORY_COUNT", len(originals)):
                    with self.assertRaises(ToiError):
                        packaging.package(config_path, root / "release")
                self.assertFalse((root / "release").exists())
                self.assertFalse((root / "outside.txt").exists())

    def test_invalid_output_name_cannot_escape_release_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, config, _, _ = self.source(root)
            config["archive_stem"] = "../outside"
            config_path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ToiError, "이름"):
                packaging.package(config_path, root / "release")
            self.assertFalse((root / "release").exists())

    def test_source_sidecar_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, config, _, _ = self.source(root)
            Path(config["source_archive"] + ".sha256").write_text("untrusted")
            with self.assertRaisesRegex(ToiError, "체크섬 기록"):
                packaging.package(config_path, root / "release")

    def test_same_size_crc_valid_tampered_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, config, entries, originals = self.source(root)
            source = Path(config["source_archive"])
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
            first = next(name for name in entries if "/payload/" in name)
            entries[first] = b"X" * len(entries[first])
            with zipfile.ZipFile(source, "w") as archive:
                for info in infos:
                    archive.writestr(copy.copy(info), entries[info.filename])
            self.assertEqual(source.stat().st_size, config["source_size"])
            self.assertNotEqual(sha256_bytes(source.read_bytes()), config["source_zip_sha256"])
            with patch.object(linux, "INVENTORY_COUNT", len(originals)):
                with self.assertRaisesRegex(ToiError, "payload SHA-256"):
                    packaging.package(config_path, root / "release")
            self.assertFalse((root / "release" / (config["archive_stem"] + ".zip")).exists())


if __name__ == "__main__":
    unittest.main()
