#!/usr/bin/env python3
"""Standalone SteamOS installer. Requires Python 3.9+ and its standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys

AA = "Trials of Innocence_Data/StreamingAssets/aa/"
RESOURCE = "Trials of Innocence_Data/resources.assets"
EXE = "Trials of Innocence.exe"
HEX = re.compile(r"[0-9a-f]{64}")
INVENTORY_COUNT = 3875
MAX_JSON_BYTES = 32 * 1024 * 1024


class PatchError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compact_name(relative):
    return digest(relative.encode("utf-8")) + ".bin"


def relative_parts(relative):
    if (not isinstance(relative, str) or not relative or
            any(c in relative for c in ("\\", ":", "\x00"))):
        raise PatchError(f"잘못된 상대경로: {relative!r}")
    parts = relative.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PatchError(f"잘못된 상대경로: {relative!r}")
    return parts


def require_hash(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise PatchError("올바르지 않은 SHA-256 값입니다.")


def require_size(value):
    if type(value) is not int or value < 0:
        raise PatchError("올바르지 않은 파일 크기입니다.")


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PatchError(f"중복 JSON 키: {key}")
            result[key] = value
        return result
    def reject_constant(value):
        raise PatchError(f"잘못된 JSON 숫자: {value}")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=reject_constant)
    except (UnicodeError, ValueError) as exc:
        raise PatchError(f"JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise PatchError("JSON 객체가 필요합니다.")
    return value


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def sync_directory(descriptor):
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise


def file_identity(item):
    if item is None:
        return None
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


class Tree:
    """Pin directory handles; never follow links inside the trusted root."""
    def __init__(self, path):
        self.path = Path(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def parent(self, relative, create=False):
        parts = relative_parts(relative)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def inspect(self, relative):
        try:
            with self.parent(relative) as (fd, name):
                item = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)):
                    raise PatchError(f"링크나 특수 파일은 사용할 수 없습니다: {relative}")
                return item
        except FileNotFoundError:
            return None

    @contextmanager
    def reader(self, relative):
        with self.parent(relative) as (fd, name):
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise PatchError(f"일반 파일이 아닙니다: {relative}")
            with os.fdopen(handle, "rb") as stream:
                handle = None
                yield stream
        finally:
            if handle is not None:
                os.close(handle)

    def read(self, relative, limit=MAX_JSON_BYTES):
        with self.reader(relative) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PatchError(f"파일 크기 제한을 넘었습니다: {relative}")
        return data

    def hash_and_stat(self, relative):
        sha = hashlib.sha256()
        with self.reader(relative) as stream:
            before = os.fstat(stream.fileno())
            while True:
                data = stream.read(1024 * 1024)
                if not data:
                    break
                sha.update(data)
            after = os.fstat(stream.fileno())
        if file_identity(before) != file_identity(after):
            raise PatchError(f"검사 중 파일이 변경되었습니다: {relative}")
        return sha.hexdigest(), after

    def file_hash(self, relative):
        return self.hash_and_stat(relative)[0]

    def assert_hash(self, relative, expected, label):
        actual, item = self.hash_and_stat(relative)
        if actual != expected:
            raise PatchError(f"{label} SHA-256 불일치: {relative}")
        return item

    def _write(self, stream, relative, expected, *, create=False, new=False, mode=0o644,
               expected_destination=None):
        with self.parent(relative, create=create) as (fd, name):
            temporary = ".ko-tmp-" + secrets.token_hex(16)
            try:
                try:
                    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    before = None
                if before is not None and not stat.S_ISREG(before.st_mode):
                    raise PatchError(f"일반 파일이 아닙니다: {relative}")
                if new and before is not None:
                    raise PatchError(f"파일이 이미 존재합니다: {relative}")
                if expected_destination is not None and file_identity(before) != file_identity(expected_destination):
                    raise PatchError(f"교체 전에 파일이 변경되었습니다: {relative}")
                if before is not None:
                    mode = stat.S_IMODE(before.st_mode) & 0o777
                handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 mode, dir_fd=fd)
                with os.fdopen(handle, "wb") as output:
                    sha = hashlib.sha256()
                    while True:
                        data = stream.read(1024 * 1024)
                        if not data:
                            break
                        output.write(data)
                        sha.update(data)
                    output.flush()
                    os.fsync(output.fileno())
                if sha.hexdigest() != expected:
                    raise PatchError(f"복사 중 SHA-256 불일치: {relative}")
                try:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if file_identity(current) != file_identity(before):
                    raise PatchError(f"복사 중 파일이 변경되었습니다: {relative}")
                if new:
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=fd)
                else:
                    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                sync_directory(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def copy_from(self, source, source_path, destination, expected, *, create=False, new=False,
                  expected_destination=None):
        with source.reader(source_path) as stream:
            self._write(stream, destination, expected, create=create, new=new,
                        expected_destination=expected_destination)

    def write_json(self, relative, value, *, new=False):
        data = json_bytes(value)
        self._write(io.BytesIO(data), relative, digest(data), new=new)

    def remove(self, relative, *, expected_destination=None):
        with self.parent(relative) as (fd, name):
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                raise PatchError(f"일반 파일이 아닙니다: {relative}")
            if expected_destination is not None and file_identity(item) != file_identity(expected_destination):
                raise PatchError(f"삭제 전에 파일이 변경되었습니다: {relative}")
            os.unlink(name, dir_fd=fd)
            sync_directory(fd)


def assert_game_closed(proc=Path("/proc")):
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if any(arg.replace(b"\\", b"/").rsplit(b"/", 1)[-1].lower() == EXE.lower().encode()
               for arg in arguments):
            raise PatchError("게임을 종료한 뒤 설치 또는 복구를 실행하세요.")


def validate_manifest(manifest):
    if (manifest.get("format") != "toi-l10n/game-patch-v2" or
            not isinstance(manifest.get("game_files"), list) or
            len(manifest["game_files"]) != INVENTORY_COUNT or
            not isinstance(manifest.get("changes"), list) or
            not 1 <= len(manifest["changes"]) <= 1100 or
            manifest.get("scope", {}).get("changed_files") != len(manifest["changes"])):
        raise PatchError("지원하지 않거나 불완전한 패치 manifest입니다.")
    require_hash(manifest.get("patch_id"))
    require_hash(manifest.get("game_revision"))
    identity = json.dumps({k: v for k, v in manifest.items() if k != "patch_id"},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if digest(identity) != manifest["patch_id"]:
        raise PatchError("패치 ID가 manifest 내용과 다릅니다.")
    inventory, seen = {}, set()
    for row in manifest["game_files"]:
        relative_parts(row["path"])
        if row["path"].casefold() in seen:
            raise PatchError(f"중복 게임 경로: {row['path']}")
        seen.add(row["path"].casefold())
        require_hash(row.get("sha256"))
        require_size(row.get("size"))
        inventory[row["path"]] = row
    if EXE not in inventory:
        raise PatchError("Windows판 게임 실행 파일이 manifest에 없습니다.")
    inventory_keys = seen.copy()
    seen = set()
    for row in manifest["changes"]:
        path = row["path"]
        relative_parts(path)
        if path.casefold() in seen:
            raise PatchError(f"중복 패치 경로: {path}")
        seen.add(path.casefold())
        if not path.startswith(AA) and path != RESOURCE:
            raise PatchError(f"허용한 게임 리소스 경로 밖입니다: {path}")
        require_hash(row.get("output_sha256"))
        require_size(row.get("output_size"))
        if row.get("payload_path") != compact_name(path):
            raise PatchError(f"패치 내부 파일명이 다릅니다: {path}")
        if row["operation"] == "replace":
            require_hash(row.get("source_sha256"))
            if path not in inventory or inventory[path]["sha256"] != row["source_sha256"]:
                raise PatchError(f"교체 파일과 원본 목록이 다릅니다: {path}")
        elif row["operation"] == "add":
            if path == RESOURCE or path.casefold() in inventory_keys or row.get("source_sha256") is not None:
                raise PatchError(f"추가 파일과 원본 목록이 겹칩니다: {path}")
        else:
            raise PatchError(f"잘못된 파일 작업: {path}")
    return manifest


class Installer:
    def __init__(self, patch_root, log=print):
        patch_root = Path(os.path.abspath(patch_root))
        if patch_root.is_symlink() or patch_root.parent.is_symlink():
            raise PatchError("게임 폴더와 KoreanPatch 폴더 자체에는 링크를 사용할 수 없습니다.")
        if patch_root.name != "KoreanPatch":
            raise PatchError("패치 폴더 이름은 KoreanPatch여야 합니다.")
        # Steam's .steam/steam ancestor alias is legitimate; resolve it once before pinning roots.
        self.root = patch_root.resolve(strict=True)
        self.patch = Tree(self.root)
        try:
            self.game = Tree(self.root.parent)
        except BaseException:
            self.patch.close()
            raise
        self.log = log
        self.manifest = None
        self.lock = None

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        self.game.close()
        self.patch.close()

    def acquire_lock(self):
        import fcntl
        self.lock = os.open("installer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                            0o600, dir_fd=self.patch.fd)
        if not stat.S_ISREG(os.fstat(self.lock).st_mode):
            raise PatchError("설치 잠금 파일이 일반 파일이 아닙니다.")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PatchError("다른 설치 또는 복구 작업이 실행 중입니다.") from exc

    def load(self):
        if not self.game.inspect(EXE):
            raise PatchError("KoreanPatch 폴더를 Trials of Innocence.exe와 같은 폴더에 넣으세요.")
        expected = self.patch.read("patch-manifest.sha256", 256).decode("ascii").strip()
        require_hash(expected)
        raw = self.patch.read("patch-manifest.json")
        if digest(raw) != expected:
            raise PatchError("패치 manifest SHA-256 불일치입니다.")
        self.manifest = validate_manifest(json_object(raw))
        # Validate every existing parent before backing up or changing game files.
        for row in self.manifest["game_files"] + self.manifest["changes"]:
            self.game.inspect(row["path"])
        for row in self.manifest["changes"]:
            self.patch.inspect(self.payload(row))
        return self.manifest

    @staticmethod
    def payload(row):
        return "payload/" + row["payload_path"]

    def assert_payload(self):
        for row in self.manifest["changes"]:
            name = self.payload(row)
            item = self.patch.inspect(name)
            if item is None or item.st_size != row["output_size"]:
                raise PatchError(f"패치 파일이 없거나 크기가 다릅니다: {name}")
            self.patch.assert_hash(name, row["output_sha256"], "패치 파일")

    def assert_original(self):
        for index, row in enumerate(self.manifest["game_files"], 1):
            self.game.assert_hash(row["path"], row["sha256"], "게임 원본")
            if index % 1000 == 0:
                self.log(f"  원본 확인: {index}/{len(self.manifest['game_files'])}")
        for row in self.manifest["changes"]:
            if row["operation"] == "add" and self.game.inspect(row["path"]) is not None:
                raise PatchError(f"추가할 파일이 이미 존재합니다: {row['path']}")

    def assert_backup(self):
        record = json_object(self.patch.read("backup/backup-manifest.json"))
        if (record.get("format") not in ("toi-l10n/game-patch-backup-v1", "toi-l10n/game-patch-backup-v2") or
                record.get("patch_id") != self.manifest["patch_id"] or
                not isinstance(record.get("files"), list) or
                len(record["files"]) != len(self.manifest["changes"])):
            raise PatchError("백업이 이 패치와 일치하지 않습니다.")
        rows = {r["path"]: r.get("sha256") for r in record["files"]}
        if len(rows) != len(record["files"]):
            raise PatchError("중복 백업 경로가 있습니다.")
        paths = {}
        for row in self.manifest["changes"]:
            path = row["path"]
            if path not in rows or rows[path] != row["source_sha256"]:
                raise PatchError(f"백업 항목이 다릅니다: {path}")
            name = compact_name(path) if record["format"].endswith("v2") else path
            paths[path] = "backup/" + name
            if row["operation"] == "replace":
                self.patch.assert_hash(paths[path], row["source_sha256"], "백업")
        return paths

    def state(self):
        if self.patch.inspect("state.json") is None:
            return None
        state = json_object(self.patch.read("state.json"))
        if state.get("format") != "toi-l10n/game-patch-state-v1" or state.get("patch_id") != self.manifest["patch_id"]:
            raise PatchError("다른 패치의 설치 상태가 기록되어 있습니다.")
        return state

    def new_backup(self):
        if self.patch.inspect("backup") is not None:
            self.log("[3/4] 기존 백업을 확인합니다.")
            return self.assert_backup()
        self.log("[3/4] 원본 파일을 백업합니다.")
        pending = "backup.pending-" + secrets.token_hex(16)
        os.mkdir(pending, 0o755, dir_fd=self.patch.fd)
        try:
            records = []
            for row in self.manifest["changes"]:
                if row["operation"] == "replace":
                    self.patch.copy_from(self.game, row["path"], pending + "/" + compact_name(row["path"]),
                                         row["source_sha256"], new=True)
                records.append({"path": row["path"], "sha256": row["source_sha256"]})
            self.patch.write_json(pending + "/backup-manifest.json",
                                  {"format": "toi-l10n/game-patch-backup-v2",
                                   "patch_id": self.manifest["patch_id"],
                                   "created_at": datetime.now(timezone.utc).isoformat(), "files": records}, new=True)
            # Publish without replacing an existing backup, including an empty directory.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(self.patch.fd, os.fsencode(pending), self.patch.fd, b"backup", 1) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
            sync_directory(self.patch.fd)
        finally:
            try:
                # /proc keeps the parent pinned on Python 3.9 too (rmtree's dir_fd is newer).
                shutil.rmtree(f"/proc/self/fd/{self.patch.fd}/{pending}")
            except FileNotFoundError:
                pass
        return {row["path"]: "backup/" + compact_name(row["path"]) for row in self.manifest["changes"]}

    def check_space(self):
        originals = {r["path"]: r["size"] for r in self.manifest["game_files"]}
        backup = 0 if self.patch.inspect("backup") is not None else sum(
            originals[r["path"]] for r in self.manifest["changes"] if r["operation"] == "replace")
        growth = sum(max(0, r["output_size"] - originals.get(r["path"], 0)) for r in self.manifest["changes"])
        required = backup + growth + max(r["output_size"] for r in self.manifest["changes"]) + 16 * 1024 * 1024
        if shutil.disk_usage(self.root.parent).free < required:
            raise PatchError(f"설치 공간이 부족합니다. 최소 {required / (1024 * 1024):.0f} MiB를 확보하세요.")

    def install(self):
        self.log("[1/4] 패치 파일을 확인합니다.")
        self.assert_payload()
        state = self.state()
        if state is not None:
            if state.get("status", "installed") != "installed":
                raise PatchError("중단된 작업이 있습니다. 먼저 restore.sh로 복구하세요.")
            self.assert_backup()
            for row in self.manifest["changes"]:
                self.game.assert_hash(row["path"], row["output_sha256"], "설치된 파일")
            self.log("이미 한국어 패치가 설치되어 있습니다.")
            return
        self.log("[2/4] 대응 게임 버전을 확인합니다.")
        self.assert_original()
        self.check_space()
        backup = self.new_backup()
        assert_game_closed()
        state = {"format": "toi-l10n/game-patch-state-v1", "patch_id": self.manifest["patch_id"],
                 "installed_at": datetime.now(timezone.utc).isoformat(),
                 "changed_files": len(self.manifest["changes"]), "platform": "steamdeck", "status": "installing"}
        self.patch.write_json("state.json", state, new=True)
        attempted = []
        try:
            self.log("[4/4] 한국어 파일을 설치합니다.")
            for row in sorted(self.manifest["changes"], key=lambda r: r["path"].endswith("/catalog.json")):
                destination_info = None
                if row["operation"] == "replace":
                    destination_info = self.game.assert_hash(row["path"], row["source_sha256"], "설치 전 원본")
                attempted.append(row)
                self.game.copy_from(self.patch, self.payload(row), row["path"], row["output_sha256"],
                                    create=row["operation"] == "add", new=row["operation"] == "add",
                                    expected_destination=destination_info)
                if len(attempted) % 150 == 0:
                    self.log(f"  설치: {len(attempted)}/{len(self.manifest['changes'])}")
            state["status"] = "installed"
            self.patch.write_json("state.json", state)
        except BaseException as error:
            failures = []
            for row in reversed(attempted):
                try:
                    current = self.game.inspect(row["path"])
                    if row["operation"] == "add":
                        if current is not None:
                            destination_info = self.game.assert_hash(row["path"], row["output_sha256"], "추가 파일 복구")
                            self.game.remove(row["path"], expected_destination=destination_info)
                    else:
                        current_hash, destination_info = self.game.hash_and_stat(row["path"])
                        if current_hash == row["source_sha256"]:
                            continue
                        if current_hash != row["output_sha256"]:
                            raise PatchError("설치 중 다른 프로그램이 파일을 변경했습니다.")
                        self.game.copy_from(self.patch, backup[row["path"]], row["path"], row["source_sha256"],
                                            expected_destination=destination_info)
                except BaseException:
                    failures.append(row["path"])
            if failures:
                raise PatchError("설치와 자동 복구가 실패했습니다. 백업을 보존하고 restore.sh를 실행하세요: " +
                                 ", ".join(failures)) from error
            self.patch.remove("state.json")
            raise
        self.log("한국어 패치 설치 완료. 게임 언어에서 한국어를 선택하세요.")

    def restore(self):
        self.assert_payload()
        prior_state = self.state()
        backup = self.assert_backup()
        installed = {}
        for row in self.manifest["changes"]:
            item = self.game.inspect(row["path"])
            if row["operation"] == "add" and item is None:
                installed[row["path"]] = None
                continue
            current = self.game.file_hash(row["path"])
            if current not in (row["source_sha256"], row["output_sha256"]):
                raise PatchError(f"다른 내용으로 변경된 파일이 있어 복구를 중단합니다: {row['path']}")
            installed[row["path"]] = current
        assert_game_closed()
        state = {"format": "toi-l10n/game-patch-state-v1", "patch_id": self.manifest["patch_id"],
                 "platform": "steamdeck", "status": "restoring"}
        attempted = []
        try:
            self.patch.write_json("state.json", state, new=prior_state is None)
            self.log("게임 원본을 복구합니다.")
            # Restore the catalog before removing any bundle it references.
            for row in sorted(self.manifest["changes"], key=lambda r: not r["path"].endswith("/catalog.json")):
                before = installed[row["path"]]
                if before is None or before == row["source_sha256"]:
                    continue
                destination_info = self.game.assert_hash(row["path"], before, "복구 전 파일")
                attempted.append(row)
                if row["operation"] == "add":
                    self.game.remove(row["path"], expected_destination=destination_info)
                else:
                    self.game.copy_from(self.patch, backup[row["path"]], row["path"], row["source_sha256"],
                                        expected_destination=destination_info)
            self.patch.remove("state.json")
        except BaseException as error:
            failures = []
            for row in reversed(attempted):
                try:
                    item = self.game.inspect(row["path"])
                    destination_info = None
                    if row["operation"] == "add":
                        if item is not None:
                            self.game.assert_hash(row["path"], row["output_sha256"], "복구 취소 파일")
                            continue
                    else:
                        current, destination_info = self.game.hash_and_stat(row["path"])
                        if current == row["output_sha256"]:
                            continue
                        if current != row["source_sha256"]:
                            raise PatchError("복구 중 다른 프로그램이 파일을 변경했습니다.")
                    self.game.copy_from(self.patch, self.payload(row), row["path"], row["output_sha256"],
                                        create=row["operation"] == "add", new=row["operation"] == "add",
                                        expected_destination=destination_info)
                except BaseException:
                    failures.append(row["path"])
            if failures:
                raise PatchError("복구와 복구 취소가 실패했습니다. 백업을 보존하고 restore.sh를 다시 실행하세요: " +
                                 ", ".join(failures)) from error
            if prior_state is None:
                if self.patch.inspect("state.json") is not None:
                    self.patch.remove("state.json")
            else:
                self.patch.write_json("state.json", prior_state)
            raise
        self.log("게임 원본 복구 완료. KoreanPatch/backup은 보관됩니다.")


def main(argv=None, patch_root=None):
    parser = argparse.ArgumentParser(description="Trials of Innocence SteamOS 한국어 패치 설치·복구")
    parser.add_argument("action", choices=("install", "restore"))
    parser.add_argument("--pause", action="store_true", help="터미널에서 실행한 경우 종료 전 Enter 대기")
    args = parser.parse_args(argv)
    installer = None
    result = 1
    try:
        if not sys.platform.startswith("linux") or sys.version_info < (3, 9):
            raise PatchError("Linux와 Python 3.9 이상이 필요합니다.")
        if os.geteuid() == 0:
            raise PatchError("sudo 없이 일반 사용자로 실행하세요.")
        assert_game_closed()
        installer = Installer(patch_root or Path(__file__).absolute().parent)
        installer.acquire_lock()
        installer.load()
        getattr(installer, args.action)()
        result = 0
    except (PatchError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"패치 {args.action} 실패: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print("작업이 취소되었습니다. 설치 상태가 남아 있다면 restore.sh로 복구하세요.", file=sys.stderr)
        result = 130
    finally:
        if installer is not None:
            installer.close()
        if args.pause and sys.stdin.isatty():
            try:
                input("Enter를 누르면 종료합니다. ")
            except (EOFError, KeyboardInterrupt):
                pass
    return result


if __name__ == "__main__":
    sys.exit(main())
