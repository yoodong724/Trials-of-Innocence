from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_SHARDS = 100_000


class ToiError(Exception):
    """Expected input, contract, or safety error."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_revision(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToiError(f"중복 JSON 키: {key}")
        result[key] = value
    return result


def load_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> Any:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ToiError(f"JSON 입력이 {max_bytes}바이트 한도를 넘음: {path}")
        raw = path.read_bytes()
    except OSError as exc:
        raise ToiError(f"파일을 읽을 수 없음: {path}: {exc}") from exc
    if len(raw) > max_bytes:
        raise ToiError(f"JSON 입력이 {max_bytes}바이트 한도를 넘음: {path}")
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ToiError(f"비유한 JSON 숫자: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToiError(f"올바른 UTF-8 JSON이 아님: {path}: {exc}") from exc


def dump_json_new(path: Path, value: Any) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    write_bytes_new(path, payload)


def ensure_new_path(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ToiError(f"출력 경로가 이미 존재함: {path}")

def require_shard_id(value: Any, name: str = "shard_id") -> str:
    if (
        not isinstance(value, str)
        or len(value) < 5
        or value[0] != "b"
        or not value[1:].isdigit()
    ):
        raise ToiError(f"{name}은 b와 네 자리 이상 숫자로 구성해야 함")
    return value

def _secure_directory(path: Path, *, create: bool) -> int:
    if not sys.platform.startswith("linux"):
        raise ToiError(
            f"directory-handle 기반 안전한 출력은 Linux에서만 지원함: {sys.platform}"
        )
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    parts = absolute.parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    if (
        len(parts) >= 5
        and parts[:4] == ("/", "proc", "self", "fd")
        and parts[4].isdigit()
    ):
        descriptor = os.dup(int(parts[4]))
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ToiError(f"directory fd가 디렉터리가 아님: {absolute}")
        start = 5
    else:
        descriptor = os.open(absolute.anchor, flags)
        start = 1
    try:
        for part in parts[start:]:
            if part in ("", "."):
                continue
            if part == "..":
                raise ToiError(f"상위 경로 구성요소는 허용하지 않음: {absolute}")
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException as exc:
        os.close(descriptor)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, ToiError)):
            raise
        raise ToiError(f"출력 디렉터리를 안전하게 열 수 없음: {absolute}: {exc}") from exc


def _entry_exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _temporary_name(destination_name: str) -> str:
    return f".{destination_name}.tmp-{secrets.token_hex(16)}"


def _publish_name_noreplace(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ToiError("renameat2(RENAME_NOREPLACE)를 사용할 수 없음")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_directory,
        os.fsencode(source_name),
        destination_directory,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOTEMPTY):
        raise ToiError(f"출력 경로가 이미 존재함: {destination_name}")
    raise ToiError(
        f"출력 경로를 게시할 수 없음: {destination_name}: {os.strerror(error)}"
    )


def write_bytes_new(path: Path, payload: bytes) -> None:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    parent = _secure_directory(path.parent, create=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        if _entry_exists(parent, path.name):
            raise ToiError(f"출력 경로가 이미 존재함: {path}")
        for _ in range(128):
            candidate = _temporary_name(path.name)
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise ToiError(f"임시 출력 이름을 할당할 수 없음: {path}")
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_name_noreplace(parent, temporary_name, parent, path.name)
        temporary_name = None
        os.fsync(parent)
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except OSError:
                pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit, ToiError)):
            raise
        raise ToiError(f"출력 파일을 안전하게 쓸 수 없음: {path}: {exc}") from exc
    finally:
        os.close(parent)


@contextmanager
def staged_output_directory(output: Path):
    output = Path(os.path.abspath(os.fspath(output.expanduser())))
    parent = _secure_directory(output.parent, create=True)
    staging_name: str | None = None
    staging_descriptor: int | None = None
    try:
        if _entry_exists(parent, output.name):
            raise ToiError(f"출력 경로가 이미 존재함: {output}")
        for _ in range(128):
            candidate = _temporary_name(output.name)
            try:
                os.mkdir(candidate, 0o755, dir_fd=parent)
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if staging_name is None:
            raise ToiError(f"임시 출력 이름을 할당할 수 없음: {output}")
        staging_descriptor = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        yield Path(f"/proc/self/fd/{staging_descriptor}")
        _publish_name_noreplace(parent, staging_name, parent, output.name)
        staging_name = None
        os.fsync(parent)
    finally:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if staging_name is not None:
            try:
                shutil.rmtree(staging_name, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def ensure_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part in (path.anchor, ""):
            continue
        current /= part
        if current.is_symlink():
            raise ToiError(f"심볼릭 링크 경로는 허용하지 않음: {current}")


def resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ToiError(f"경로를 확인할 수 없음: {path}: {exc}") from exc


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_output_path(output: Path, protected_roots: Iterable[Path]) -> Path:
    output = output.expanduser().absolute()
    ensure_new_path(output)
    ensure_no_symlink_components(output.parent)
    candidate = output.resolve(strict=False)
    for root in protected_roots:
        protected = resolved(root)
        if candidate == protected or is_relative_to(candidate, protected):
            raise ToiError(f"원본 내부에는 출력할 수 없음: {candidate}")
    ensure_new_path(candidate)
    return candidate


def validate_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ToiError("비어 있지 않은 상대경로가 필요함")
    if "\\" in value or "\x00" in value:
        raise ToiError(f"허용하지 않는 상대경로: {value!r}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ToiError(f"경로 탈출은 허용하지 않음: {value!r}")
    return path


def path_from_record(root: Path, value: str) -> Path:
    relative = validate_relative_path(value)
    path = root / relative
    ensure_no_symlink_components(path)
    real = path.resolve(strict=True)
    if not is_relative_to(real, root.resolve(strict=True)):
        raise ToiError(f"기준 경로를 벗어남: {value!r}")
    return real


def require_hex(value: Any, name: str, length: int = 64) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ToiError(f"{name}은 {length}자리 소문자 16진수여야 함")
    return value


def relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def common_game_root(inventory: dict[str, Any]) -> Path:
    if inventory.get("format") != "toi-l10n/game-inventory-v1":
        raise ToiError("지원하지 않는 inventory 형식")
    game_root = inventory.get("game_root")
    if not isinstance(game_root, str):
        raise ToiError("inventory.game_root가 없음")
    return resolved(Path(game_root))


def json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def read_pointer_object(document: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ToiError(f"지원하지 않는 JSON Pointer: {pointer!r}")
    current = document
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise ToiError(f"JSON Pointer가 없음: {pointer!r}")
        current = current[token]
    return current


def canonical_revision(canonical_path: Path) -> str:
    canonical_path = resolved(canonical_path)
    if canonical_path.is_file():
        value = load_json(canonical_path)
    else:
        names = ["glossary.json", "characters.json", "scenes.json", "decisions.json"]
        records = {name[:-5]: load_json(canonical_path / name) for name in names}
        style = (canonical_path / "style.md").read_text(encoding="utf-8")
        value = {"schema_version": "0.1", **records, "style": style}
    return content_revision(value)


def fail(message: str) -> "None":
    raise ToiError(message)
