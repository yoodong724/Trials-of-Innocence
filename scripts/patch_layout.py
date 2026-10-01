"""Keep distributed patch files independent of the game's deep resource paths."""
from __future__ import annotations

import ntpath
from pathlib import Path

from toi_common import ToiError, content_revision, sha256_bytes

DEFAULT_GAME_ROOT = r"C:\Program Files (x86)\Steam\steamapps\common\Trials of Innocence"


def compact_name(relative: str) -> str:
    return sha256_bytes(relative.encode("utf-8")) + ".bin"


def compact_manifest(manifest: dict, version: str) -> dict:
    result = {**manifest, "name": f"Trials of Innocence Korean test patch {version}",
              "changes": [{**row, "payload_path": compact_name(row["path"])}
                          for row in manifest["changes"]]}
    result["patch_id"] = content_revision({k: v for k, v in result.items() if k != "patch_id"})
    return result


def payload_relative(row: dict) -> str:
    if "payload_path" not in row:
        return row["path"]  # Previously released packages.
    expected = compact_name(row["path"])
    if row["payload_path"] != expected:
        raise ToiError(f"잘못된 패치 내부 경로: {row['path']}")
    return expected


def payload_file(folder: Path, row: dict) -> Path:
    return folder / "payload" / payload_relative(row)


def audit_windows_paths(manifest: dict, game_root: str = DEFAULT_GAME_ROOT) -> dict:
    """Budget every file operation for restrictive Windows/.NET installations."""
    patch = ntpath.join(game_root, "KoreanPatch")
    pending = ntpath.join(patch, "backup.pending-" + "0" * 32)
    paths: dict[str, list[str]] = {key: [] for key in
        ("game", "payload", "backup", "pending_backup", "replacement_temp", "metadata")}
    paths["game"] = [ntpath.join(game_root, row["path"].replace("/", "\\"))
                     for row in manifest["game_files"]]
    for row in manifest["changes"]:
        destination = ntpath.join(game_root, row["path"].replace("/", "\\"))
        paths["game"].append(destination)
        paths["payload"].append(ntpath.join(patch, "payload", payload_relative(row).replace("/", "\\")))
        paths["replacement_temp"].append(ntpath.join(ntpath.dirname(destination), ".ko-tmp-" + "0" * 32))
        if row["operation"] == "replace":
            name = compact_name(row["path"])
            paths["backup"].append(ntpath.join(patch, "backup", name))
            paths["pending_backup"].append(ntpath.join(pending, name))
    for path in (ntpath.join(patch, "state.json"), ntpath.join(pending, "backup-manifest.json")):
        paths["metadata"].extend((path, path + ".tmp-" + "0" * 32))
    paths["metadata"].extend(ntpath.join(patch, name) for name in
        ("patch.ps1", "patch-manifest.json", "patch-manifest.sha256", "backup/backup-manifest.json"))
    units = lambda path: len(path.encode("utf-16-le")) // 2
    report = {}
    for category, entries in paths.items():
        for path in entries:
            if units(path) > 259 or units(ntpath.dirname(path)) > 247:
                raise ToiError(f"Windows 경로 제한 초과 ({units(path)}자): {path}")
        report[category] = {"checked": len(entries), "max_length": max(map(units, entries), default=0)}
    return {"game_root": game_root, "file_limit": 259, "directory_limit": 247, "paths": report}
