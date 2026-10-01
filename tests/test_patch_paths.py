"""Windows path-limit regression, including real install/restore and rollback."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from patch_layout import audit_windows_paths, compact_name, payload_file, payload_relative
from toi_common import ToiError, content_revision, sha256_bytes

AA = "Trials of Innocence_Data/StreamingAssets/aa"
LONG = f"{AA}/StandaloneWindows64/wuzui_assets_naninovel/localization/ja/backgrounds/foreground/测试图(视角3)桌子_ko_6d7917a46122b991903c51443f134bc6.bundle"
USER_ROOT = r"C:\Program Files (x86)\Steam\steamapps\common\Trials of Innocence"


class PatchPathTest(unittest.TestCase):
    def test_reported_path_exceeds_limit_and_compact_name_does_not(self):
        legacy = USER_ROOT + "\\KoreanPatch\\payload\\" + LONG.replace("/", "\\")
        self.assertEqual(len(legacy), 265)
        row = {"path": LONG, "payload_path": compact_name(LONG)}
        compact = USER_ROOT + "\\KoreanPatch\\payload\\" + payload_relative(row)
        self.assertLess(len(compact), 260)
        self.assertTrue(payload_relative(row).isascii())
        self.assertNotIn("/", payload_relative(row))

    def test_legacy_package_paths_remain_readable(self):
        row = {"path": LONG}
        self.assertEqual(payload_file(Path("KoreanPatch"), row), Path("KoreanPatch/payload") / LONG)

    def test_compact_path_must_match_its_game_path(self):
        for invalid in ("../outside.bin", "C:\\outside.bin", None, compact_name("another.bundle")):
            with self.subTest(invalid=invalid), self.assertRaises(ToiError):
                payload_relative({"path": LONG, "payload_path": invalid})

    def test_all_io_paths_are_checked_including_pending_backup_and_temp(self):
        row = {"path": LONG, "payload_path": compact_name(LONG), "operation": "replace"}
        manifest = {"game_files": [{"path": LONG}], "changes": [row]}
        report = audit_windows_paths(manifest)
        self.assertEqual(set(report["paths"]), {"game", "payload", "backup", "pending_backup", "replacement_temp", "metadata"})
        self.assertTrue(all(item["max_length"] < 260 for item in report["paths"].values()))
        with self.assertRaises(ToiError):
            audit_windows_paths(manifest, USER_ROOT + "\\" + "x" * 100)

    def test_old_nested_payload_fails_the_full_path_audit(self):
        manifest = {"game_files": [], "changes": [{"path": LONG, "operation": "add"}]}
        with self.assertRaises(ToiError):
            audit_windows_paths(manifest)


def fixture_zip(path: Path, *, compact: bool) -> None:
    old = b"original\n"
    original = [f"{AA}/catalog.json"] + [f"{AA}/base/{i:04}.bundle" for i in range(3874)]
    added = [LONG] + [f"{AA}/StandaloneWindows64/wuzui_assets_naninovel/localization/ja/backgrounds/foreground/新增{i}.bundle" for i in range(3)]
    changes = []
    payloads = {}
    for relative in sorted(original[:455] + added, key=lambda p: (p.endswith("/catalog.json"), p)):
        raw = ("patched " + relative + "\n").encode("utf-8")
        row = {"path": relative, "operation": "add" if relative in added else "replace",
               "source_sha256": None if relative in added else sha256_bytes(old),
               "output_sha256": sha256_bytes(raw), "output_size": len(raw)}
        if compact:
            row["payload_path"] = compact_name(relative)
        changes.append(row)
        payloads["KoreanPatch/payload/" + payload_relative(row)] = raw
    manifest = {"format": "toi-l10n/game-patch-v2", "scope": {"changed_files": 459},
                "game_files": [{"path": p, "sha256": sha256_bytes(old), "size": len(old)} for p in original],
                "changes": changes}
    manifest["patch_id"] = content_revision(manifest)
    raw_manifest = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, raw in payloads.items():
            archive.writestr(name, raw)
        archive.writestr("KoreanPatch/patch-manifest.json", raw_manifest)
        archive.writestr("KoreanPatch/patch-manifest.sha256", sha256_bytes(raw_manifest) + "\n")
        archive.writestr("KoreanPatch/patch.ps1", (ROOT / "installer/patch.ps1").read_bytes())


@unittest.skipUnless(os.name == "nt" or os.environ.get("TOI_WINDOWS_PATCH_TESTS") == "1",
                     "Set TOI_WINDOWS_PATCH_TESTS=1 to run Windows PowerShell from WSL.")
class WindowsPatchPathTest(unittest.TestCase):
    def run_phase(self, phase: str, *, success: bool = True, root_length: int = len(USER_ROOT)) -> str:
        def windows(path: Path) -> str:
            if os.name == "nt":
                return str(path)
            return subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
        args = [shutil.which("powershell.exe") or "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", windows(ROOT / "tests/patch_windows_fixture.ps1"), "-Phase", phase]
        if phase == "setup":
            args += ["-ZipPath", windows(self.zip_path), "-RootLength", str(root_length)]
        else:
            args += ["-Root", self.game_root]
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="toi-test-")
        self.addCleanup(self.temporary.cleanup)
        self.zip_path = Path(self.temporary.name) / "fixture.zip"

    def setup_game(self, *, compact: bool = True, root_length: int = len(USER_ROOT)):
        fixture_zip(self.zip_path, compact=compact)
        self.game_root = self.run_phase("setup", root_length=root_length)
        self.assertEqual(len(self.game_root), root_length)
        self.addCleanup(lambda: self.run_phase("cleanup"))

    def test_install_restore_and_backup_reuse_with_long_paths_blocked(self):
        self.setup_game()
        self.assertIn("Korean patch installed", self.run_phase("install"))
        self.run_phase("installed")
        self.assertIn("already installed", self.run_phase("install"))
        self.run_phase("restore")
        self.run_phase("restored")
        self.assertIn("Checking existing backup", self.run_phase("install"))
        self.run_phase("restore")
        self.run_phase("restored")

    def test_tampered_payload_stops_before_game_changes(self):
        self.setup_game()
        self.run_phase("tamper")
        self.assertIn("SHA-256 mismatch for patch payload", self.run_phase("install", success=False))
        self.run_phase("restored")

    def test_overlong_original_path_is_reported_before_any_game_change(self):
        self.setup_game()
        self.run_phase("long-path")
        self.assertIn("Windows path limit exceeded", self.run_phase("install", success=False))
        self.run_phase("reset-path")
        self.run_phase("restored")

    def test_legacy_payload_and_backup_remain_compatible(self):
        self.setup_game(compact=False, root_length=48)
        self.run_phase("install")
        self.run_phase("legacy-backup")
        self.run_phase("restore")
        self.run_phase("restored")
        self.run_phase("install")
        self.run_phase("installed")
        self.run_phase("restore")
        self.run_phase("restored")

    def test_install_failure_rolls_back_all_applied_files(self):
        self.setup_game()
        self.assertIn("Atomic replace failed", self.run_phase("install-locked", success=False))
        self.run_phase("restored")
        self.run_phase("install")
        self.run_phase("restore")
        self.run_phase("restored")

    def test_restore_failure_rolls_back_to_installed_state(self):
        self.setup_game()
        self.run_phase("install")
        self.assertIn("Atomic replace failed", self.run_phase("restore-locked", success=False))
        self.run_phase("installed")
        self.run_phase("restore")
        self.run_phase("restored")


if __name__ == "__main__":
    unittest.main()
