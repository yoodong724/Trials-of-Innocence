"""Exercise installer transactions and refusal paths on owned synthetic game files."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import game_patch_linux as linux

CATALOG = linux.AA + "catalog.json"
BUNDLE = linux.AA + "StandaloneWindows64/中文 폴더/기존.bundle"
ADDED = linux.AA + "StandaloneWindows64/새 폴더/新增 한국어.bundle"
OTHER = linux.AA + "unmodified.bundle"


def fixture(root, *, count=5):
    game = root / "SD card library" / "Trials of Innocence"
    folder = game / "KoreanPatch"
    folder.mkdir(parents=True)
    originals = {linux.EXE: b"synthetic executable", linux.RESOURCE: b"original resources",
                 CATALOG: b"original catalog", BUNDLE: b"original bundle", OTHER: b"untouched"}
    for index in range(count - len(originals)):
        originals[linux.AA + f"inventory/{index:04}.bundle"] = b"untouched"
    outputs = {linux.RESOURCE: b"Korean resources", BUNDLE: b"Korean bundle",
               ADDED: "추가 번들".encode(), CATALOG: b"Korean catalog"}
    rows = []
    for relative, data in outputs.items():
        row = {"path": relative, "payload_path": linux.compact_name(relative),
               "operation": "replace" if relative in originals else "add",
               "source_sha256": linux.digest(originals[relative]) if relative in originals else None,
               "output_sha256": linux.digest(data), "output_size": len(data)}
        rows.append(row)
        path = folder / "payload" / row["payload_path"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    for relative, data in originals.items():
        path = game / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"format": "toi-l10n/game-patch-v2", "release_version": "v1.0.0",
                "game_revision": "1" * 64, "scope": {"changed_files": len(rows)},
                "game_files": [{"path": relative, "sha256": linux.digest(data), "size": len(data)}
                               for relative, data in originals.items()], "changes": rows}
    save_manifest(folder, manifest)
    return game, folder, manifest, originals, outputs


def save_manifest(folder, manifest):
    identity = json.dumps({k: v for k, v in manifest.items() if k != "patch_id"},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest["patch_id"] = linux.digest(identity)
    data = linux.json_bytes(manifest)
    (folder / "patch-manifest.json").write_bytes(data)
    (folder / "patch-manifest.sha256").write_text(linux.digest(data) + "\n")


@unittest.skipUnless(sys.platform.startswith("linux"), "SteamOS installer requires Linux")
class LinuxInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="toi-linux-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game, self.folder, self.manifest, self.originals, self.outputs = fixture(self.root)
        self.inventory_count = patch.object(linux, "INVENTORY_COUNT", len(self.originals))
        self.inventory_count.start()
        self.addCleanup(self.inventory_count.stop)
        self.messages = []
        self.installer = linux.Installer(self.folder, log=self.messages.append)
        self.addCleanup(self.installer.close)
        self.installer.acquire_lock()
        self.installer.load()

    def assert_originals(self):
        for relative, expected in self.originals.items():
            self.assertEqual((self.game / relative).read_bytes(), expected, relative)
        for relative in set(self.outputs) - set(self.originals):
            self.assertFalse((self.game / relative).exists(), relative)

    def assert_installed(self):
        for relative, expected in self.originals.items():
            self.assertEqual((self.game / relative).read_bytes(), self.outputs.get(relative, expected), relative)
        for relative, expected in self.outputs.items():
            self.assertEqual((self.game / relative).read_bytes(), expected, relative)

    def test_install_restore_repeat_and_backup_reuse(self):
        self.installer.install()
        self.assert_installed()
        backup = (self.folder / "backup/backup-manifest.json").read_bytes()
        self.installer.install()
        self.assertIn("이미 한국어 패치가 설치되어 있습니다.", self.messages)
        self.installer.restore()
        self.assert_originals()
        self.assertFalse((self.folder / "state.json").exists())
        self.installer.restore()
        self.assert_originals()
        self.installer.install()
        self.assertEqual((self.folder / "backup/backup-manifest.json").read_bytes(), backup)
        self.installer.restore()
        self.assert_originals()

    def test_windows_v1_backup_and_state_can_be_restored(self):
        self.installer.install()
        record_path = self.folder / "backup/backup-manifest.json"
        record = json.loads(record_path.read_bytes())
        for row in self.manifest["changes"]:
            if row["operation"] == "replace":
                old = self.folder / "backup" / linux.compact_name(row["path"])
                target = self.folder / "backup" / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                old.rename(target)
        record["format"] = "toi-l10n/game-patch-backup-v1"
        record_path.write_bytes(linux.json_bytes(record))
        state = self.installer.state()
        state.pop("status")
        state.pop("platform")
        (self.folder / "state.json").write_bytes(linux.json_bytes(state))
        self.installer.install()
        self.installer.restore()
        self.assert_originals()

    def test_corrupt_payload_stops_before_backup_or_game_changes(self):
        row = self.manifest["changes"][0]
        (self.folder / "payload" / row["payload_path"]).write_bytes(b"tampered")
        with self.assertRaises(linux.PatchError):
            self.installer.install()
        self.assert_originals()
        self.assertFalse((self.folder / "backup").exists())

    def test_wrong_game_version_stops_before_backup(self):
        (self.game / OTHER).write_bytes(b"updated game")
        with self.assertRaisesRegex(linux.PatchError, "게임 원본"):
            self.installer.install()
        self.assertEqual((self.game / BUNDLE).read_bytes(), self.originals[BUNDLE])
        self.assertFalse((self.folder / "backup").exists())

    def test_existing_addition_is_not_overwritten(self):
        target = self.game / ADDED
        target.parent.mkdir(parents=True)
        target.write_bytes(b"owned by someone else")
        with self.assertRaisesRegex(linux.PatchError, "이미 존재"):
            self.installer.install()
        self.assertEqual(target.read_bytes(), b"owned by someone else")
        self.assertFalse((self.folder / "backup").exists())

    def test_corrupt_backup_blocks_restore_without_game_changes(self):
        self.installer.install()
        (self.folder / "backup" / linux.compact_name(BUNDLE)).write_bytes(b"corrupt")
        with self.assertRaisesRegex(linux.PatchError, "백업"):
            self.installer.restore()
        self.assert_installed()

    def test_modified_installed_file_blocks_restore(self):
        self.installer.install()
        (self.game / BUNDLE).write_bytes(b"modified externally")
        with self.assertRaisesRegex(linux.PatchError, "변경된 파일"):
            self.installer.restore()
        self.assertEqual((self.game / CATALOG).read_bytes(), self.outputs[CATALOG])
        self.assertEqual((self.game / BUNDLE).read_bytes(), b"modified externally")
        self.assertTrue((self.folder / "state.json").exists())

    def test_install_failure_after_publish_rolls_back(self):
        original_copy = self.installer.game.copy_from
        def fail_after_publish(source, source_path, destination, expected, **kwargs):
            original_copy(source, source_path, destination, expected, **kwargs)
            if destination == BUNDLE and expected == linux.digest(self.outputs[BUNDLE]):
                raise OSError("simulated disk failure after rename")
        with patch.object(self.installer.game, "copy_from", side_effect=fail_after_publish):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.installer.install()
        self.assert_originals()
        self.assertFalse((self.folder / "state.json").exists())
        self.assertTrue((self.folder / "backup").is_dir())
        self.installer.install()
        self.assert_installed()

    def test_restore_failure_rolls_back_exact_mixed_state(self):
        self.installer.install()
        # A previous interruption already restored resources; other rows remain patched.
        (self.game / linux.RESOURCE).write_bytes(self.originals[linux.RESOURCE])
        original_copy = self.installer.game.copy_from
        def fail_once(source, source_path, destination, expected, **kwargs):
            if destination == BUNDLE and expected == linux.digest(self.originals[BUNDLE]):
                raise OSError("simulated restore failure")
            return original_copy(source, source_path, destination, expected, **kwargs)
        with patch.object(self.installer.game, "copy_from", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.installer.restore()
        self.assertEqual((self.game / linux.RESOURCE).read_bytes(), self.originals[linux.RESOURCE])
        for relative in (CATALOG, BUNDLE, ADDED):
            self.assertEqual((self.game / relative).read_bytes(), self.outputs[relative])
        self.assertEqual(self.installer.state()["status"], "installed")
        self.installer.restore()
        self.assert_originals()

    def test_forced_exit_leaves_recoverable_install_state(self):
        self.installer.assert_payload()
        self.installer.assert_original()
        self.installer.new_backup()
        self.installer.patch.write_json("state.json", {"format": "toi-l10n/game-patch-state-v1",
                                        "patch_id": self.manifest["patch_id"], "status": "installing"}, new=True)
        row = next(r for r in self.manifest["changes"] if r["path"] == BUNDLE)
        self.installer.game.copy_from(self.installer.patch, self.installer.payload(row), BUNDLE, row["output_sha256"])
        with self.assertRaisesRegex(linux.PatchError, "중단된 작업"):
            self.installer.install()
        self.installer.restore()
        self.assert_originals()
        self.installer.install()
        self.assert_installed()

    def test_ctrl_c_rolls_back(self):
        original_copy = self.installer.game.copy_from
        def interrupt(source, source_path, destination, expected, **kwargs):
            if destination == BUNDLE and expected == linux.digest(self.outputs[BUNDLE]):
                raise KeyboardInterrupt()
            return original_copy(source, source_path, destination, expected, **kwargs)
        with patch.object(self.installer.game, "copy_from", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.installer.install()
        self.assert_originals()
        self.assertFalse((self.folder / "state.json").exists())

    def test_second_installer_cannot_acquire_lock(self):
        second = linux.Installer(self.folder)
        self.addCleanup(second.close)
        with self.assertRaisesRegex(linux.PatchError, "다른 설치"):
            second.acquire_lock()

    def test_symlinked_game_file_and_parent_are_rejected(self):
        path = self.game / BUNDLE
        path.unlink()
        outside = self.root / "outside.bundle"
        outside.write_bytes(self.originals[BUNDLE])
        path.symlink_to(outside)
        with self.assertRaises(linux.PatchError):
            self.installer.load()
        path.unlink()
        parent = path.parent
        parent.rmdir()
        target = self.root / "outside-directory"
        target.mkdir()
        (target / path.name).write_bytes(self.originals[BUNDLE])
        parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            self.installer.load()
        self.assertEqual((target / path.name).read_bytes(), self.originals[BUNDLE])

    def test_symlinked_payload_backup_and_state_are_rejected(self):
        self.installer.install()
        state_path = self.folder / "state.json"
        state = state_path.read_bytes()
        outside = self.root / "outside-state.json"
        outside.write_bytes(state)
        state_path.unlink()
        state_path.symlink_to(outside)
        with self.assertRaises(linux.PatchError):
            self.installer.restore()
        self.assertEqual(outside.read_bytes(), state)
        state_path.unlink()
        state_path.write_bytes(state)
        backup = self.folder / "backup" / linux.compact_name(BUNDLE)
        outside_backup = self.root / "outside-backup.bin"
        backup.rename(outside_backup)
        backup.symlink_to(outside_backup)
        with self.assertRaises(OSError):
            self.installer.restore()
        self.assert_installed()

    def test_manifest_traversal_and_outside_resource_edits_are_rejected(self):
        for path in ("../outside.bundle", "/absolute.bundle", "foo//bar", "foo/./bar", "Trials of Innocence.exe"):
            with self.subTest(path=path):
                manifest = json.loads(linux.json_bytes(self.manifest))
                manifest["changes"][0]["path"] = path
                save_manifest(self.folder, manifest)
                with self.assertRaises(linux.PatchError):
                    self.installer.load()
                self.assert_originals()

    def test_manifest_digest_and_patch_id_must_both_match(self):
        data = (self.folder / "patch-manifest.json").read_bytes()
        (self.folder / "patch-manifest.json").write_bytes(data + b" ")
        with self.assertRaisesRegex(linux.PatchError, "SHA-256"):
            self.installer.load()
        (self.folder / "patch-manifest.sha256").write_text(linux.digest(data + b" ") + "\n")
        broken = json.loads(data)
        broken["patch_id"] = "0" * 64
        raw = linux.json_bytes(broken)
        (self.folder / "patch-manifest.json").write_bytes(raw)
        (self.folder / "patch-manifest.sha256").write_text(linux.digest(raw) + "\n")
        with self.assertRaisesRegex(linux.PatchError, "패치 ID"):
            self.installer.load()

    def test_low_disk_space_stops_before_backup(self):
        usage = type(shutil.disk_usage(self.root))(100, 99, 1)
        with patch.object(linux.shutil, "disk_usage", return_value=usage):
            with self.assertRaisesRegex(linux.PatchError, "공간"):
                self.installer.install()
        self.assert_originals()
        self.assertFalse((self.folder / "backup").exists())

    def test_steam_ancestor_alias_is_supported_but_root_links_are_rejected(self):
        alias = self.root / "steam-alias"
        alias.symlink_to(self.game.parent, target_is_directory=True)
        installer = linux.Installer(alias / self.game.name / "KoreanPatch")
        installer.close()
        linked_root = self.root / "KoreanPatch-link"
        linked_root.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(linux.PatchError):
            linux.Installer(linked_root)

    def test_running_windows_game_is_detected_without_launching_game(self):
        proc = self.root / "proc"
        (proc / "123").mkdir(parents=True)
        (proc / "123/cmdline").write_bytes(b"wine\0Z:\\games\\Trials of Innocence.exe\0")
        with self.assertRaisesRegex(linux.PatchError, "게임을 종료"):
            linux.assert_game_closed(proc)

    def test_atomic_copy_does_not_publish_tampered_stream(self):
        with self.assertRaisesRegex(linux.PatchError, "복사 중 SHA-256"):
            self.installer.game._write(io.BytesIO(b"corrupt"), BUNDLE, linux.digest(self.outputs[BUNDLE]))
        self.assert_originals()

    def test_restore_rollback_recreates_already_deleted_addition(self):
        self.installer.install()
        original_remove = self.installer.game.remove
        def fail_after_unlink(relative, **kwargs):
            original_remove(relative, **kwargs)
            if relative == ADDED:
                raise OSError("failure after unlink")
        with patch.object(self.installer.game, "remove", side_effect=fail_after_unlink):
            with self.assertRaisesRegex(OSError, "after unlink"):
                self.installer.restore()
        self.assert_installed()
        self.installer.restore()
        self.assert_originals()

    def test_incomplete_inventory_is_rejected(self):
        manifest = json.loads(linux.json_bytes(self.manifest))
        manifest["game_files"].pop()
        save_manifest(self.folder, manifest)
        with self.assertRaisesRegex(linux.PatchError, "불완전"):
            self.installer.load()

    def test_external_replacement_after_precheck_is_preserved(self):
        original_copy = self.installer.game.copy_from
        external = b"updated externally"
        def concurrent_update(source, source_path, destination, expected, **kwargs):
            if destination == BUNDLE and expected == linux.digest(self.outputs[BUNDLE]):
                temporary = self.game / (BUNDLE + ".external")
                temporary.write_bytes(external)
                temporary.replace(self.game / BUNDLE)
            return original_copy(source, source_path, destination, expected, **kwargs)
        with patch.object(self.installer.game, "copy_from", side_effect=concurrent_update):
            with self.assertRaises(linux.PatchError):
                self.installer.install()
        self.assertEqual((self.game / BUNDLE).read_bytes(), external)
        self.assertEqual((self.game / linux.RESOURCE).read_bytes(), self.originals[linux.RESOURCE])
        self.assertEqual(self.installer.state()["status"], "installing")

    def test_external_update_during_payload_copy_is_preserved(self):
        class ChangingStream(io.BytesIO):
            def read(stream, size=-1):
                (self.game / BUNDLE).write_bytes(b"updated during copy")
                return super().read(size)
        with self.assertRaisesRegex(linux.PatchError, "変更|변경"):
            self.installer.game._write(ChangingStream(self.outputs[BUNDLE]), BUNDLE,
                                      linux.digest(self.outputs[BUNDLE]))
        self.assertEqual((self.game / BUNDLE).read_bytes(), b"updated during copy")

    def test_external_update_during_restore_is_preserved(self):
        self.installer.install()
        original_copy = self.installer.game.copy_from
        external = b"updated while restoring"
        def concurrent_update(source, source_path, destination, expected, **kwargs):
            if destination == BUNDLE and expected == linux.digest(self.originals[BUNDLE]):
                (self.game / BUNDLE).write_bytes(external)
            return original_copy(source, source_path, destination, expected, **kwargs)
        with patch.object(self.installer.game, "copy_from", side_effect=concurrent_update):
            with self.assertRaises(linux.PatchError):
                self.installer.restore()
        self.assertEqual((self.game / BUNDLE).read_bytes(), external)
        self.assertEqual((self.game / CATALOG).read_bytes(), self.outputs[CATALOG])

    def test_addition_cannot_shadow_original_windows_path_by_case(self):
        manifest = json.loads(linux.json_bytes(self.manifest))
        row = next(r for r in manifest["changes"] if r["operation"] == "add")
        row["path"] = OTHER.replace("unmodified", "UNMODIFIED")
        row["payload_path"] = linux.compact_name(row["path"])
        save_manifest(self.folder, manifest)
        with self.assertRaisesRegex(linux.PatchError, "겹칩니다"):
            self.installer.load()

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(linux.PatchError, "중복 JSON"):
            linux.json_object(b'{"patch_id": "one", "patch_id": "two"}')


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() != 0, "Linux user CLI")
class LinuxCommandLineTest(unittest.TestCase):
    def test_sh_launchers_and_standalone_cli_from_another_directory(self):
        with tempfile.TemporaryDirectory(prefix="toi-cli-test-") as directory:
            root = Path(directory)
            game, folder, manifest, originals, outputs = fixture(root, count=linux.INVENTORY_COUNT)
            for name, source in (("patch.py", "game_patch_linux.py"), ("install.sh", "patch_install_linux.sh"),
                                 ("restore.sh", "patch_restore_linux.sh")):
                (folder / name).write_bytes((ROOT / "installer" / "linux" / name).read_bytes())
            for action in ("install", "restore"):
                result = subprocess.run(["sh", str(folder / (action + ".sh"))], cwd=root,
                                        input="", text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                expected = outputs if action == "install" else originals
                for path, data in expected.items():
                    self.assertEqual((game / path).read_bytes(), data)
            self.assertFalse((game / ADDED).exists())
            self.assertEqual((game / OTHER).read_bytes(), originals[OTHER])


if __name__ == "__main__":
    unittest.main()
