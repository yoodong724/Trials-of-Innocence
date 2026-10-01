#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import UnityPy

from toi_catalog import AddressablesCatalog
from toi_common import (
    ToiError,
    canonical_json_bytes,
    common_game_root,
    content_revision,
    dump_json_new,
    json_pointer_token,
    load_json,
    path_from_record,
    read_pointer_object,
    relative_posix,
    require_hex,
    require_shard_id,
    staged_output_directory,
    resolved,
    sha256_bytes,
    sha256_file,
    validate_output_path,
    write_bytes_new,
)


DATA_DIR_NAME = "Trials of Innocence_Data"
AA_RELATIVE = f"{DATA_DIR_NAME}/StreamingAssets/aa"
CATALOG_RELATIVE = f"{AA_RELATIVE}/catalog.json"
SETTINGS_RELATIVE = f"{AA_RELATIVE}/settings.json"
SOURCE_SCRIPT_PREFIX = "StandaloneWindows64/wuzui_assets_naninovel/scripts/"
JA_SCRIPT_PREFIX = "StandaloneWindows64/wuzui_assets_naninovel/localization/ja/text/scripts/"
SOURCE_MANAGED_PREFIX = "StandaloneWindows64/wuzui_assets_naninovel/text/"
JA_MANAGED_PREFIX = "StandaloneWindows64/wuzui_assets_naninovel/localization/ja/text/"
BUNDLE_RE = re.compile(r"^(?P<name>.+)_[0-9a-f]{32}\.bundle$", re.IGNORECASE)
HEX_32_RE = re.compile(r"[0-9a-f]{32}")


@dataclass
class ScriptRecord:
    record_id: str
    source_comment: str
    target: str
    target_start: int
    target_end: int


@dataclass
class ScriptDocument:
    text: str
    lines: list[str]
    newline: str
    final_newline: bool
    records: dict[str, ScriptRecord]
    occurrences: dict[str, list[ScriptRecord]]
    duplicate_ids: set[str]

    def replace(self, targets: dict[str, str], preserve_duplicate_ids: set[str] | None = None) -> str:
        preserve_duplicate_ids = preserve_duplicate_ids or set()
        unknown = sorted(set(targets) - set(self.records))
        if unknown:
            raise ToiError(f"일본어 script에 없는 ID: {unknown[0]}")
        lines = list(self.lines)
        edits: list[tuple[int, int, list[str]]] = []
        for record_id, target in targets.items():
            if any(line.startswith("# ") or line.startswith(";") for line in target.splitlines()):
                raise ToiError(f"script 문법과 충돌하는 번역 행: {record_id}")
            records = self.occurrences[record_id]
            if len(records) > 1 and record_id in preserve_duplicate_ids:
                continue
            for record in records:
                edits.append((record.target_start, record.target_end, target.split("\n")))
        for start, end, replacement in sorted(edits, reverse=True):
            lines[start:end] = replacement
        result = self.newline.join(lines)
        if self.final_newline:
            result += self.newline
        return result


@dataclass
class ManagedRecord:
    key: str
    value: str
    line_index: int
    prefix: str


@dataclass
class ManagedDocument:
    text: str
    lines: list[str]
    newline: str
    final_newline: bool
    records: dict[str, ManagedRecord]
    occurrences: dict[str, list[ManagedRecord]]

    def replace(self, targets: dict[str, str]) -> str:
        unknown = sorted(set(targets) - set(self.records))
        if unknown:
            raise ToiError(f"일본어 managed text에 없는 키: {unknown[0]}")
        lines = list(self.lines)
        for key, target in targets.items():
            if "\n" in target or "\r" in target:
                raise ToiError(f"managed text 값에는 개행을 넣을 수 없음: {key}")
            for record in self.occurrences[key]:
                lines[record.line_index] = record.prefix + target
        result = self.newline.join(lines)
        if self.final_newline:
            result += self.newline
        return result


def _newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r"
    return "\n"


def parse_script_document(text: str) -> ScriptDocument:
    newline = _newline_style(text)
    final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("# ")]
    if not starts:
        raise ToiError("Naninovel script 레코드가 없음")
    records: dict[str, ScriptRecord] = {}
    occurrences: dict[str, list[ScriptRecord]] = {}
    duplicate_ids: set[str] = set()
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        record_id = lines[start][2:].strip()
        if not record_id:
            raise ToiError("비어 있는 Naninovel script ID")
        cursor = start + 1
        comments: list[str] = []
        while cursor < end and lines[cursor].startswith(";"):
            comments.append(lines[cursor][1:])
            cursor += 1
        target_end = end
        while target_end > cursor and lines[target_end - 1] == "":
            target_end -= 1
        record = ScriptRecord(
            record_id=record_id,
            source_comment="\n".join(comments),
            target="\n".join(lines[cursor:target_end]),
            target_start=cursor,
            target_end=target_end,
        )
        occurrences.setdefault(record_id, []).append(record)
        if record_id in records:
            duplicate_ids.add(record_id)
        else:
            records[record_id] = record
    return ScriptDocument(text, lines, newline, final_newline, records, occurrences, duplicate_ids)


def parse_managed_document(text: str) -> ManagedDocument:
    newline = _newline_style(text)
    final_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    records: dict[str, ManagedRecord] = {}
    occurrences: dict[str, list[ManagedRecord]] = {}
    for index, line in enumerate(lines):
        if not line or line.lstrip().startswith(("#", ";")):
            continue
        colon = line.find(":")
        if colon < 0:
            raise ToiError(f"managed text 구분자(:)가 없는 행: {index + 1}")
        key = line[:colon].strip()
        value_start = colon + 1
        while value_start < len(line) and line[value_start] in " \t":
            value_start += 1
        if not key:
            raise ToiError("비어 있는 managed text 키")
        record = ManagedRecord(key, line[value_start:], index, line[:value_start])
        occurrences.setdefault(key, []).append(record)
        records.setdefault(key, record)
    if not records:
        raise ToiError("managed text 레코드가 없음")
    return ManagedDocument(text, lines, newline, final_newline, records, occurrences)


def _logical_name(path: Path) -> str:
    match = BUNDLE_RE.match(path.name)
    if not match:
        raise ToiError(f"Addressables bundle 이름을 해석할 수 없음: {path.name}")
    return match.group("name")


def _detect_addressables_version(game_root: Path) -> str | None:
    dll = game_root / DATA_DIR_NAME / "Managed" / "Unity.Addressables.dll"
    if not dll.is_file():
        return None
    match = re.search(rb"com\.unity\.addressables@(\d+\.\d+\.\d+)", dll.read_bytes())
    return match.group(1).decode("ascii") if match else None


def _file_kind(relative: str) -> str:
    if relative == CATALOG_RELATIVE:
        return "addressables_catalog"
    if relative == SETTINGS_RELATIVE:
        return "addressables_settings"
    if relative.endswith(".bundle"):
        return "asset_bundle"
    if relative.endswith(".dll"):
        return "managed_assembly"
    if relative.endswith(".exe"):
        return "executable"
    return "game_file"


def _assert_plain_game_tree(game_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in game_root.rglob("*"):
        if path.is_symlink():
            raise ToiError(f"게임 원본의 심볼릭 링크는 허용하지 않음: {path}")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda path: relative_posix(path, game_root).casefold())


def _catalog_document(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise ToiError("catalog JSON 루트는 객체여야 함")
    return value


def command_inventory(args: argparse.Namespace) -> dict[str, Any]:
    game_root = resolved(Path(args.game_root))
    data_root = game_root / DATA_DIR_NAME
    aa_root = data_root / "StreamingAssets" / "aa"
    if not aa_root.is_dir():
        raise ToiError(f"Addressables 루트를 찾을 수 없음: {aa_root}")
    output = validate_output_path(Path(args.out), [game_root])
    catalog_path = game_root / CATALOG_RELATIVE
    settings_path = game_root / SETTINGS_RELATIVE
    catalog_document = _catalog_document(catalog_path)
    catalog = AddressablesCatalog(catalog_document)

    file_records: list[dict[str, Any]] = []
    for path in _assert_plain_game_tree(game_root):
        relative = relative_posix(path, game_root)
        file_records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "kind": _file_kind(relative),
            }
        )
    revision_input = [{"path": row["path"], "sha256": row["sha256"]} for row in file_records]
    game_revision = content_revision(revision_input)
    locations = catalog.describe_locations()
    document = {
        "format": "toi-l10n/game-inventory-v1",
        "game_root": str(game_root),
        "game_revision": game_revision,
        "addressables_root": AA_RELATIVE,
        "unity_version": "2022.3.22f1",
        "addressables_version": _detect_addressables_version(game_root),
        "files": file_records,
        "catalog": {
            "path": CATALOG_RELATIVE,
            "sha256": sha256_file(catalog_path),
            "locator_id": catalog_document.get("m_LocatorId"),
            "build_result_hash": catalog_document.get("m_BuildResultHash"),
            "provider_ids": catalog_document.get("m_ProviderIds"),
            "resource_types": catalog_document.get("m_resourceTypes"),
            "locations": locations,
        },
        "settings": {
            "path": SETTINGS_RELATIVE,
            "sha256": sha256_file(settings_path),
            "value": load_json(settings_path),
        },
    }
    dump_json_new(output, document)
    return {
        "game_revision": game_revision,
        "files": len(file_records),
        "catalog_locations": len(locations),
        "out": str(output),
    }


def _inventory_files(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = inventory.get("files")
    if not isinstance(rows, list):
        raise ToiError("inventory.files가 배열이 아님")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ToiError("inventory 파일 레코드가 잘못됨")
        path = row["path"]
        if path in result:
            raise ToiError(f"inventory 중복 파일: {path}")
        require_hex(row.get("sha256"), f"files[{path}].sha256")
        result[path] = row
    expected_revision = content_revision(
        [{"path": row["path"], "sha256": row["sha256"]} for row in rows]
    )
    if expected_revision != inventory.get("game_revision"):
        raise ToiError("inventory game_revision이 파일 목록과 맞지 않음")
    return result


def _aa_bundle_rows(files: dict[str, dict[str, Any]], prefix: str) -> list[tuple[str, dict[str, Any]]]:
    full_prefix = f"{AA_RELATIVE}/{prefix}"
    rows = [(path[len(AA_RELATIVE) + 1 :], row) for path, row in files.items() if path.startswith(full_prefix)]
    return sorted(rows, key=lambda item: item[0].casefold())


def _by_logical_name(rows: list[tuple[str, dict[str, Any]]]) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for relative, row in rows:
        logical = _logical_name(Path(relative)).casefold()
        if logical in result:
            raise ToiError(f"중복 논리 bundle 이름: {logical}")
        result[logical] = (relative, row)
    return result


def _verified_file_bytes(
    game_root: Path, relative: str, record: dict[str, Any]
) -> tuple[Path, bytes]:
    path = path_from_record(game_root, relative)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ToiError(f"원본 파일을 읽을 수 없음: {relative}: {exc}") from exc
    if len(payload) != record.get("size") or sha256_bytes(payload) != record.get("sha256"):
        raise ToiError(f"inventory 이후 원본 파일이 변경됨: {relative}")
    return path, payload


def _verify_file(game_root: Path, relative: str, record: dict[str, Any]) -> Path:
    path, _ = _verified_file_bytes(game_root, relative, record)
    return path


def _unity_object(
    source: Path | bytes, label: Path, type_name: str
) -> tuple[Any, Any, Any]:
    environment = UnityPy.load(source if isinstance(source, bytes) else str(source))
    objects = [obj for obj in environment.objects if obj.type.name == type_name]
    if len(objects) != 1:
        raise ToiError(f"{label.name}의 {type_name} 객체 수가 1이 아님: {len(objects)}")
    obj = objects[0]
    return environment, obj, obj.read() if type_name == "TextAsset" else obj.read_typetree()


def _script_source(
    path: Path, payload: bytes | None = None
) -> tuple[str, int, list[tuple[str, str, str | None, list[str]]]]:
    _, obj, tree = _unity_object(payload if payload is not None else path, path, "MonoBehaviour")
    try:
        name = tree["m_Name"]
        keys = tree["textMap"]["idToText"]["keys"]
        values = tree["textMap"]["idToText"]["values"]
        references = tree["references"]["RefIds"]
    except (KeyError, TypeError) as exc:
        raise ToiError(f"Naninovel Script 타입트리가 예상과 다름: {path}") from exc
    if not isinstance(name, str) or len(keys) != len(values):
        raise ToiError(f"Naninovel Script key/value 개수가 다름: {path}")
    if len(set(keys)) != len(keys) or not all(isinstance(x, str) for x in keys + values):
        raise ToiError(f"Naninovel Script ID가 중복되었거나 문자열이 아님: {path}")
    if not isinstance(references, list):
        raise ToiError(f"Naninovel Script reference 목록이 아님: {path}")
    authors_by_text_id: dict[str, set[str]] = {}
    for index, reference in enumerate(references):
        if not isinstance(reference, dict):
            raise ToiError(f"Naninovel Script reference가 객체가 아님: {path}: {index}")
        reference_type = reference.get("type")
        if not isinstance(reference_type, dict) or reference_type.get("class") != "MyCustomPrintCommand":
            continue
        data = reference.get("data")
        try:
            text_parameter = data["Text"]
            parts = text_parameter["value"]["parts"]
            author_parameter = data["AuthorId"]
            author_value = author_parameter["value"]
            author_has_value = author_parameter["hasValue"]
        except (KeyError, TypeError) as exc:
            raise ToiError(f"Naninovel 출력 명령 구조가 예상과 다름: {path}: {index}") from exc
        if (
            not isinstance(text_parameter, dict)
            or not isinstance(author_parameter, dict)
            or text_parameter.get("hasValue") != 1
            or not isinstance(parts, list)
            or not isinstance(author_value, str)
            or author_has_value not in (0, 1)
        ):
            raise ToiError(f"Naninovel 출력 명령 text/author 형식이 잘못됨: {path}: {index}")
        author = author_value if author_has_value else ""
        if "|" in author or author == "<narration>":
            raise ToiError(f"Naninovel 화자 ID가 예약 구분자와 충돌함: {path}: {index}")
        for part in parts:
            text_id = part.get("id") if isinstance(part, dict) else None
            text_script = part.get("script") if isinstance(part, dict) else None
            if (
                not isinstance(text_id, str)
                or not text_id
                or text_script != name
            ):
                raise ToiError(f"Naninovel 출력 명령 text ID/script가 잘못됨: {path}: {index}")
            authors_by_text_id.setdefault(text_id, set()).add(author)
    unknown_print_ids = set(authors_by_text_id) - set(keys)
    if unknown_print_ids:
        raise ToiError(
            f"Naninovel 출력 명령 ID가 textMap에 없음: {path}: {sorted(unknown_print_ids)[0]}"
        )
    rows: list[tuple[str, str, str | None, list[str]]] = []
    for text_id, value in zip(keys, values, strict=True):
        authors = authors_by_text_id.get(text_id, set())
        if not authors:
            speaker_id = None
        elif len(authors) == 1:
            speaker_id = next(iter(authors)) or None
        else:
            speaker_id = "|".join(
                "<narration>" if author == "" else author
                for author in sorted(authors)
            )
        rows.append((text_id, value, speaker_id, []))
    return name, obj.path_id, rows


def _text_asset(
    path: Path, payload: bytes | None = None
) -> tuple[str, int, str]:
    _, obj, data = _unity_object(payload if payload is not None else path, path, "TextAsset")
    if not isinstance(data.m_Name, str) or not isinstance(data.m_Script, str):
        raise ToiError(f"TextAsset 이름/본문이 문자열이 아님: {path}")
    return data.m_Name, obj.path_id, data.m_Script


def _context(
    scene_id: str | None,
    speaker_id: str | None,
    questions: list[str],
) -> dict[str, Any]:
    return {
        "scene_id": scene_id,
        "speaker_id": speaker_id,
        "status": "unknown" if questions else "confirmed",
        "note": (
            "Naninovel 출력 명령을 검사하고 원문·ja 대상 위치를 대조함"
            if scene_id and scene_id.startswith(("C", "c"))
            else "게임별 어댑터가 원문과 ja 대상 위치를 대조함"
        ),
        "blocking_questions": questions,
        "max_chars": None,
    }


def command_export_text(args: argparse.Namespace) -> dict[str, Any]:
    inventory_path = resolved(Path(args.inventory))
    inventory = load_json(inventory_path)
    if not isinstance(inventory, dict):
        raise ToiError("inventory JSON 루트는 객체여야 함")
    game_root = common_game_root(inventory)
    output = validate_output_path(Path(args.out_dir), [game_root])
    files = _inventory_files(inventory)

    source_scripts = _by_logical_name(_aa_bundle_rows(files, SOURCE_SCRIPT_PREFIX))
    ja_scripts = _by_logical_name(_aa_bundle_rows(files, JA_SCRIPT_PREFIX))
    source_managed = _by_logical_name(
        [row for row in _aa_bundle_rows(files, SOURCE_MANAGED_PREFIX) if "/scripts/" not in row[0]]
    )
    ja_managed = _by_logical_name(
        [row for row in _aa_bundle_rows(files, JA_MANAGED_PREFIX) if "/scripts/" not in row[0]]
    )
    if not source_scripts or not source_managed:
        raise ToiError("원문 Script 또는 Managed Text bundle을 찾지 못함")

    prepared: list[dict[str, Any]] = []
    mapping_entries: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    shard_number = 1

    def add_shard(kind: str, logical_key: str, source_item: tuple[str, dict[str, Any]], ja_item: tuple[str, dict[str, Any]] | None) -> None:
        nonlocal shard_number
        source_relative, source_record = source_item
        source_game_relative = f"{AA_RELATIVE}/{source_relative}"
        source_path, source_bytes = _verified_file_bytes(
            game_root, source_game_relative, source_record
        )
        ja_relative: str | None = None
        ja_record: dict[str, Any] | None = None
        ja_path: Path | None = None
        ja_bytes: bytes | None = None
        if ja_item:
            ja_relative, ja_record = ja_item
            ja_path, ja_bytes = _verified_file_bytes(
                game_root, f"{AA_RELATIVE}/{ja_relative}", ja_record
            )
        shard_id = f"b{shard_number:04d}"
        shard_number += 1
        source_object: dict[str, str] = {}
        target_object: dict[str, str] = {}
        contexts: dict[str, Any] = {}
        questions: list[str] = []

        if kind == "script":
            object_name, source_path_id, source_rows = _script_source(
                source_path, source_bytes
            )
            target_path_id: int | None = None
            target_document: ScriptDocument | None = None
            target_document_hash: str | None = None
            if ja_path:
                target_name, target_path_id, target_text = _text_asset(ja_path, ja_bytes)
                if target_name.casefold() != object_name.casefold():
                    questions.append(f"source/ja 객체 이름 불일치: {object_name} / {target_name}")
                target_document = parse_script_document(target_text)
                target_document_hash = sha256_bytes(target_text.encode("utf-8"))
            for source_id, source_value, speaker_id, speaker_questions in source_rows:
                stable_key = f"{object_name}/{source_id}"
                source_object[stable_key] = source_value
                row_questions = [*questions, *speaker_questions]
                target_value: str | None = None
                source_comment: str | None = None
                ja_record_occurrences = 0
                if target_document is None:
                    row_questions.append("ja 대상 script가 없음; 도달성과 ja 레코드 추가 방식 확인 필요")
                elif source_id not in target_document.records:
                    row_questions.append("ja 대상 script에 동일 원문 ID가 없음")
                else:
                    occurrences = target_document.occurrences[source_id]
                    ja_record_occurrences = len(occurrences)
                    if len(occurrences) == 1:
                        target_value = occurrences[0].target
                        source_comment = occurrences[0].source_comment
                        if source_comment != source_value:
                            row_questions.append("ja 원문 주석이 기본 Script 원문과 다름")
                            findings.append({"severity": "blocking", "code": "SOURCE_COMMENT_MISMATCH", "shard_id": shard_id, "record_id": source_id})
                    else:
                        matching = [row for row in occurrences if row.source_comment == source_value]
                        matching_targets = {row.target for row in matching}
                        if len(matching_targets) == 1:
                            target_value = matching[0].target
                            source_comment = matching[0].source_comment
                            findings.append({"severity": "warning", "code": "DUPLICATE_JA_RECORD_ID_NORMALIZABLE", "shard_id": shard_id, "record_id": source_id, "occurrences": len(occurrences)})
                        elif not matching:
                            row_questions.append("중복 ja ID 중 기본 Script 원문 주석과 일치하는 레코드가 없음")
                            findings.append({"severity": "blocking", "code": "DUPLICATE_JA_RECORD_NO_SOURCE_MATCH", "shard_id": shard_id, "record_id": source_id})
                        else:
                            row_questions.append("중복 ja ID의 원문 일치 레코드 번역 값이 서로 다름")
                            findings.append({"severity": "blocking", "code": "DUPLICATE_JA_RECORD_TARGET_CONFLICT", "shard_id": shard_id, "record_id": source_id})
                if target_value is not None:
                    target_object[stable_key] = target_value
                pointer = f"/{json_pointer_token(stable_key)}"
                contexts[f"{shard_id}.json#{pointer}"] = _context(
                    object_name, speaker_id, row_questions
                )
                mapping_entries.append(
                    {
                        "shard_id": shard_id,
                        "stable_key": stable_key,
                        "source_bundle": source_relative,
                        "source_object_path_id": source_path_id,
                        "source_text_id": source_id,
                        "source_value": source_value,
                        "ja_bundle": ja_relative,
                        "ja_object_path_id": target_path_id,
                        "ja_record_id": source_id if target_value is not None else None,
                        "ja_original_value": target_value,
                        "ja_source_comment": source_comment,
                        "ja_record_occurrences": ja_record_occurrences,
                        "record_kind": "script",
                        "json_file": f"source/{shard_id}.json",
                        "json_pointer": pointer,
                        "scene_id": object_name,
                        "speaker_id": speaker_id,
                        "source_document_sha256": source_record["sha256"],
                        "ja_bundle_sha256": ja_record["sha256"] if ja_record else None,
                        "ja_document_sha256": target_document_hash,
                        "blocking_questions": row_questions,
                    }
                )
        else:
            object_name, source_path_id, source_text = _text_asset(
                source_path, source_bytes
            )
            source_document = parse_managed_document(source_text)
            target_path_id = None
            target_document: ManagedDocument | None = None
            target_document_hash: str | None = None
            if ja_path:
                target_name, target_path_id, target_text = _text_asset(ja_path, ja_bytes)
                if target_name.casefold() != object_name.casefold():
                    questions.append(f"source/ja 객체 이름 불일치: {object_name} / {target_name}")
                target_document = parse_managed_document(target_text)
                target_document_hash = sha256_bytes(target_text.encode("utf-8"))
            for source_id, source_row in source_document.records.items():
                stable_key = f"{object_name}/{source_id}"
                source_object[stable_key] = source_row.value
                row_questions = list(questions)
                source_occurrences = source_document.occurrences[source_id]
                if any(row.value != source_row.value for row in source_occurrences):
                    row_questions.append("같은 managed text 키의 원문 값이 서로 다름")
                    findings.append({"severity": "blocking", "code": "DUPLICATE_SOURCE_MANAGED_KEY_DIFFERENT", "shard_id": shard_id, "record_id": source_id})
                elif len(source_occurrences) > 1:
                    findings.append({"severity": "warning", "code": "DUPLICATE_SOURCE_MANAGED_KEY_IDENTICAL", "shard_id": shard_id, "record_id": source_id, "occurrences": len(source_occurrences)})
                target_value: str | None = None
                if target_document is None:
                    row_questions.append("ja 대상 managed text가 없음")
                elif source_id not in target_document.records:
                    row_questions.append("ja 대상 managed text에 동일 키가 없음")
                else:
                    target_row = target_document.records[source_id]
                    target_occurrences = target_document.occurrences[source_id]
                    if any(row.value != target_row.value for row in target_occurrences):
                        row_questions.append("같은 ja managed text 키의 값이 서로 다름")
                        findings.append({"severity": "blocking", "code": "DUPLICATE_JA_MANAGED_KEY_DIFFERENT", "shard_id": shard_id, "record_id": source_id})
                    else:
                        target_value = target_row.value
                        if len(target_occurrences) > 1:
                            findings.append({"severity": "warning", "code": "DUPLICATE_JA_MANAGED_KEY_IDENTICAL", "shard_id": shard_id, "record_id": source_id, "occurrences": len(target_occurrences)})
                if target_value is not None:
                    target_object[stable_key] = target_value
                pointer = f"/{json_pointer_token(stable_key)}"
                contexts[f"{shard_id}.json#{pointer}"] = _context(
                    object_name, None, row_questions
                )
                mapping_entries.append(
                    {
                        "shard_id": shard_id,
                        "stable_key": stable_key,
                        "source_bundle": source_relative,
                        "source_object_path_id": source_path_id,
                        "source_text_id": source_id,
                        "source_value": source_row.value,
                        "ja_bundle": ja_relative,
                        "ja_object_path_id": target_path_id,
                        "ja_record_id": source_id if target_value is not None else None,
                        "ja_original_value": target_value,
                        "record_kind": "managed_text",
                        "json_file": f"source/{shard_id}.json",
                        "json_pointer": pointer,
                        "scene_id": object_name,
                        "speaker_id": None,
                        "source_document_sha256": sha256_bytes(source_text.encode("utf-8")),
                        "ja_bundle_sha256": ja_record["sha256"] if ja_record else None,
                        "ja_document_sha256": target_document_hash,
                        "blocking_questions": row_questions,
                    }
                )
        config = {
            "files": [f"{shard_id}.json"],
            "include_prefixes": [""],
            "exclude_prefixes": [],
            "context": contexts,
            "constraints": {
                "placeholders": "simple-v1",
                "tag_policy": "strict-sequence",
                "preserve_newline_count": True,
                "max_chars": None,
            },
        }
        prepared.append(
            {
                "shard_id": shard_id,
                "record_kind": kind,
                "logical_name": logical_key,
                "source_bundle": source_relative,
                "ja_bundle": ja_relative,
                "source": source_object,
                "target_current": target_object,
                "config": config,
                "blocking_questions": sorted({q for entry in mapping_entries if entry["shard_id"] == shard_id for q in entry["blocking_questions"]}),
            }
        )

    for logical_key, source_item in source_scripts.items():
        add_shard("script", logical_key, source_item, ja_scripts.get(logical_key))
    for logical_key, source_item in source_managed.items():
        add_shard("managed_text", logical_key, source_item, ja_managed.get(logical_key))

    with staged_output_directory(output) as staging:
        for directory in ("source", "target-current", "config"):
            (staging / directory).mkdir()
        for shard in prepared:
            dump_json_new(
                staging / "source" / f"{shard['shard_id']}.json", shard["source"]
            )
            dump_json_new(
                staging / "target-current" / f"{shard['shard_id']}.json",
                shard["target_current"],
            )
            dump_json_new(
                staging / "config" / f"{shard['shard_id']}.json", shard["config"]
            )
        mapping_base = {
            "format": "toi-l10n/text-mapping-v1",
            "game_revision": inventory["game_revision"],
            "inventory_path": str(inventory_path),
            "entries": mapping_entries,
            "findings": findings,
        }
        mapping_base["mapping_revision"] = content_revision(mapping_base)
        dump_json_new(staging / "mapping.json", mapping_base)
        export_manifest = {
            "format": "toi-l10n/text-export-v1",
            "game_revision": inventory["game_revision"],
            "mapping_revision": mapping_base["mapping_revision"],
            "shards": [
                {
                    "shard_id": shard["shard_id"],
                    "record_kind": shard["record_kind"],
                    "logical_name": shard["logical_name"],
                    "source_path": f"source/{shard['shard_id']}.json",
                    "source_sha256": sha256_file(
                        staging / "source" / f"{shard['shard_id']}.json"
                    ),
                    "target_current_path": (
                        f"target-current/{shard['shard_id']}.json"
                    ),
                    "target_current_sha256": sha256_file(
                        staging / "target-current" / f"{shard['shard_id']}.json"
                    ),
                    "config_path": f"config/{shard['shard_id']}.json",
                    "config_sha256": sha256_file(
                        staging / "config" / f"{shard['shard_id']}.json"
                    ),
                    "units": len(shard["source"]),
                    "blocking_questions": shard["blocking_questions"],
                }
                for shard in prepared
            ],
        }
        export_manifest["export_revision"] = content_revision(export_manifest)
        dump_json_new(staging / "export-manifest.json", export_manifest)
        roundtrip_authorization = {
            "format": "toi-l10n/build-input-v1",
            "mode": "current-ja-roundtrip",
            "game_revision": inventory["game_revision"],
            "mapping_revision": mapping_base["mapping_revision"],
            "canonical_revision": None,
            "shards": [
                {
                    "shard_id": shard["shard_id"],
                    "translated_path": f"{shard['shard_id']}.json",
                    "translated_sha256": sha256_file(
                        staging
                        / "target-current"
                        / f"{shard['shard_id']}.json"
                    ),
                    "source_revision": None,
                    "translations_revision": None,
                }
                for shard in prepared
                if shard["target_current"]
            ],
        }
        roundtrip_authorization["authorization_revision"] = content_revision(
            roundtrip_authorization
        )
        dump_json_new(
            staging / "target-current" / "build-input.json",
            roundtrip_authorization,
        )
    blocking = sum(1 for entry in mapping_entries if entry["blocking_questions"])
    return {
        "game_revision": inventory["game_revision"],
        "shards": len(prepared),
        "units": len(mapping_entries),
        "blocking_units": blocking,
        "out_dir": str(output),
    }



def command_prepare_font_smoke(args: argparse.Namespace) -> dict[str, Any]:
    inventory = load_json(resolved(Path(args.inventory)))
    mapping = load_json(resolved(Path(args.mapping)))
    current_root = resolved(Path(args.current_root))
    if not isinstance(inventory, dict) or not isinstance(mapping, dict):
        raise ToiError("inventory와 mapping은 JSON 객체여야 함")
    if mapping.get("format") != "toi-l10n/text-mapping-v1":
        raise ToiError("지원하지 않는 mapping 형식")
    game_root = common_game_root(inventory)
    output = validate_output_path(Path(args.out_dir), [game_root])
    if mapping.get("game_revision") != inventory.get("game_revision"):
        raise ToiError("mapping과 inventory의 game_revision이 다름")
    mapping_body = dict(mapping)
    mapping_revision = mapping_body.pop("mapping_revision", None)
    require_hex(mapping_revision, "mapping_revision")
    if content_revision(mapping_body) != mapping_revision:
        raise ToiError("mapping_revision이 mapping 내용과 다름")
    entries = mapping.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > 1_000_000
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        raise ToiError("mapping.entries가 비어 있거나 형식/개수 한도를 벗어남")
    if any(entry.get("blocking_questions") for entry in entries):
        raise ToiError("font smoke 입력 mapping에 blocking question이 남음")
    probe_path = resolved(Path(args.probes))
    probe_document = load_json(probe_path)
    if (
        not isinstance(probe_document, dict)
        or set(probe_document) != {"format", "probes"}
        or probe_document.get("format") != "toi-l10n/font-smoke-probes-v1"
    ):
        raise ToiError("지원하지 않는 font smoke probes 형식")
    probe_rows = probe_document.get("probes")
    if (
        not isinstance(probe_rows, list)
        or not 1 <= len(probe_rows) <= 32
        or any(not isinstance(row, dict) for row in probe_rows)
    ):
        raise ToiError("font smoke probes가 비어 있거나 형식/개수 한도를 벗어남")
    entry_by_key = {
        (entry.get("shard_id"), entry.get("stable_key")): entry for entry in entries
    }
    if len(entry_by_key) != len(entries):
        raise ToiError("mapping에 중복 shard/stable_key가 있음")
    targets_by_shard: dict[str, dict[str, str]] = {}
    authorization_probes: list[dict[str, Any]] = []
    seen_probes: set[tuple[str, str]] = set()
    for index, row in enumerate(probe_rows):
        unknown = set(row) - {"shard_id", "stable_key", "target"}
        shard_id = require_shard_id(
            row.get("shard_id"), f"probes[{index}].shard_id"
        )
        stable_key = row.get("stable_key")
        target = row.get("target")
        if (
            unknown
            or not isinstance(stable_key, str)
            or not stable_key
            or not isinstance(target, str)
            or not target
            or len(target) > 65_536
        ):
            raise ToiError(f"font smoke probe가 잘못됨: {index}")
        probe_key = (shard_id, stable_key)
        if probe_key in seen_probes or probe_key not in entry_by_key:
            raise ToiError(f"font smoke probe가 중복되거나 mapping에 없음: {probe_key}")
        seen_probes.add(probe_key)
        original = entry_by_key[probe_key].get("ja_original_value")
        if not isinstance(original, str) or original == target:
            raise ToiError(f"font smoke probe의 기존 ja 값이 없거나 target과 같음: {probe_key}")
        targets_by_shard.setdefault(shard_id, {})[stable_key] = target
        authorization_probes.append(
            {
                "shard_id": shard_id,
                "stable_key": stable_key,
                "original_sha256": sha256_bytes(original.encode("utf-8")),
                "target": target,
                "target_sha256": sha256_bytes(target.encode("utf-8")),
            }
        )

    by_shard: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        shard_id = require_shard_id(
            entry.get("shard_id"), f"mapping.entries[{index}].shard_id"
        )
        by_shard.setdefault(shard_id, []).append(entry)
    with staged_output_directory(output) as staging:
        authorization_shards: list[dict[str, Any]] = []
        for shard_id, rows in sorted(by_shard.items()):
            current_path = path_from_record(current_root, f"{shard_id}.json")
            current = load_json(current_path)
            expected = {
                row.get("stable_key"): row.get("ja_original_value") for row in rows
            }
            if (
                not isinstance(current, dict)
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in expected.items()
                )
                or current != expected
            ):
                raise ToiError(f"export 당시 ja 값과 current shard가 다름: {shard_id}")
            prepared = dict(current)
            prepared.update(targets_by_shard.get(shard_id, {}))
            translated_path = staging / f"{shard_id}.json"
            dump_json_new(translated_path, prepared)
            authorization_shards.append(
                {
                    "shard_id": shard_id,
                    "translated_path": f"{shard_id}.json",
                    "translated_sha256": sha256_file(translated_path),
                    "source_revision": None,
                    "translations_revision": None,
                }
            )
        authorization = {
            "format": "toi-l10n/build-input-v1",
            "mode": "font-smoke",
            "game_revision": inventory["game_revision"],
            "mapping_revision": mapping_revision,
            "canonical_revision": None,
            "font_smoke": {
                "probes": authorization_probes,
                "probes_sha256": sha256_bytes(
                    canonical_json_bytes(authorization_probes)
                ),
            },
            "shards": authorization_shards,
        }
        authorization["authorization_revision"] = content_revision(authorization)
        dump_json_new(staging / "build-input.json", authorization)
    return {
        "out_dir": str(output),
        "shards": len(by_shard),
        "probes": len(authorization_probes),
        "probes_sha256": authorization["font_smoke"]["probes_sha256"],
    }


def _validated_build_authorization(
    path: Path,
    translated_root: Path,
    game_revision: str,
    mapping_revision: str,
    expected_shards: set[str],
) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], Path]]]:
    authorization = load_json(path)
    if not isinstance(authorization, dict) or authorization.get("format") != "toi-l10n/build-input-v1":
        raise ToiError("지원하지 않는 build authorization 형식")
    body = dict(authorization)
    revision = body.pop("authorization_revision", None)
    require_hex(revision, "authorization_revision")
    if content_revision(body) != revision:
        raise ToiError("authorization_revision이 build authorization 내용과 다름")
    if authorization.get("game_revision") != game_revision:
        raise ToiError("build authorization과 inventory의 game_revision이 다름")
    if authorization.get("mapping_revision") != mapping_revision:
        raise ToiError("build authorization과 mapping_revision이 다름")
    mode = authorization.get("mode")
    if mode not in {
        "current-ja-roundtrip",
        "font-smoke",
        "reviewed-translations",
    }:
        raise ToiError("지원하지 않는 build authorization mode")
    if mode == "reviewed-translations":
        require_hex(authorization.get("canonical_revision"), "canonical_revision")
        pipeline = authorization.get("pipeline")
        if (
            not isinstance(pipeline, dict)
            or set(pipeline)
            != {"manifest_path", "manifest_sha256", "canonical_path"}
            or not isinstance(pipeline.get("manifest_path"), str)
            or not pipeline["manifest_path"]
            or not isinstance(pipeline.get("canonical_path"), str)
            or not pipeline["canonical_path"]
        ):
            raise ToiError("reviewed-translations pipeline 바인딩이 잘못됨")
        require_hex(pipeline.get("manifest_sha256"), "pipeline.manifest_sha256")
    elif (
        authorization.get("canonical_revision") is not None
        or authorization.get("pipeline") is not None
    ):
        raise ToiError(
            f"{mode} authorization에는 canonical_revision/pipeline을 둘 수 없음"
        )
    if mode == "font-smoke":
        smoke = authorization.get("font_smoke")
        probes = smoke.get("probes") if isinstance(smoke, dict) else None
        if (
            not isinstance(smoke, dict)
            or set(smoke) != {"probes", "probes_sha256"}
            or not isinstance(probes, list)
            or not 1 <= len(probes) <= 32
        ):
            raise ToiError("font-smoke authorization probes가 잘못됨")
        require_hex(smoke.get("probes_sha256"), "font_smoke.probes_sha256")
        if sha256_bytes(canonical_json_bytes(probes)) != smoke["probes_sha256"]:
            raise ToiError("font_smoke probes_sha256 불일치")
        seen_probes: set[tuple[str, str]] = set()
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict):
                raise ToiError(f"font_smoke probe가 객체가 아님: {index}")
            unknown = set(probe) - {
                "shard_id",
                "stable_key",
                "original_sha256",
                "target",
                "target_sha256",
            }
            shard_id = require_shard_id(
                probe.get("shard_id"), f"font_smoke.probes[{index}].shard_id"
            )
            stable_key = probe.get("stable_key")
            target = probe.get("target")
            if (
                unknown
                or not isinstance(stable_key, str)
                or not stable_key
                or not isinstance(target, str)
                or not target
                or len(target) > 65_536
                or (shard_id, stable_key) in seen_probes
            ):
                raise ToiError(f"font_smoke probe가 잘못되거나 중복됨: {index}")
            seen_probes.add((shard_id, stable_key))
            require_hex(
                probe.get("original_sha256"),
                f"font_smoke.probes[{index}].original_sha256",
            )
            require_hex(
                probe.get("target_sha256"),
                f"font_smoke.probes[{index}].target_sha256",
            )
            if sha256_bytes(target.encode("utf-8")) != probe["target_sha256"]:
                raise ToiError(f"font_smoke probe target_sha256 불일치: {index}")
    elif authorization.get("font_smoke") is not None:
        raise ToiError("font-smoke 이외 mode에는 font_smoke 레코드를 둘 수 없음")
    rows = authorization.get("shards")
    if not isinstance(rows, list):
        raise ToiError("build authorization shards가 배열이 아님")
    by_shard: dict[str, tuple[dict[str, Any], Path]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ToiError("build authorization shard가 잘못됨")
        shard_id = require_shard_id(
            row.get("shard_id"), f"authorization.shards[{index}].shard_id"
        )
        if shard_id in by_shard:
            raise ToiError(f"build authorization 중복 shard: {shard_id}")
        require_hex(row.get("translated_sha256"), f"{shard_id}.translated_sha256")
        if mode == "reviewed-translations":
            require_hex(row.get("source_revision"), f"{shard_id}.source_revision")
            require_hex(row.get("translations_revision"), f"{shard_id}.translations_revision")
        elif (
            row.get("source_revision") is not None
            or row.get("translations_revision") is not None
        ):
            raise ToiError(f"{mode} shard에는 번역 revision을 둘 수 없음")
        translated_path = path_from_record(translated_root, row.get("translated_path"))
        if sha256_file(translated_path) != row["translated_sha256"]:
            raise ToiError(f"authorization 이후 번역 JSON이 변경됨: {shard_id}")
        by_shard[shard_id] = (row, translated_path)
    if set(by_shard) != expected_shards:
        missing = sorted(expected_shards - set(by_shard))
        extra = sorted(set(by_shard) - expected_shards)
        raise ToiError(f"build authorization shard 집합 불일치: missing={missing[:1]}, extra={extra[:1]}")
    return authorization, by_shard


def _bundle_crc(bundle_bytes: bytes) -> int:
    environment = UnityPy.load(bundle_bytes)
    uncompressed = b"".join(file.reader.bytes for file in environment.file.files.values())
    return zlib.crc32(uncompressed) & 0xFFFFFFFF


def _save_text_asset_bundle(
    original_bytes: bytes, label: Path, expected_path_id: int, new_text: str
) -> bytes:
    environment = UnityPy.load(original_bytes)
    objects = [obj for obj in environment.objects if obj.type.name == "TextAsset"]
    if len(objects) != 1 or objects[0].path_id != expected_path_id:
        raise ToiError(f"TextAsset path ID가 mapping과 다름: {label}")
    data = objects[0].read()
    data.m_Script = new_text
    data.save()
    output = environment.file.save(packer="original")
    verify = UnityPy.load(output)
    verify_objects = [obj for obj in verify.objects if obj.type.name == "TextAsset"]
    if len(verify_objects) != 1 or verify_objects[0].path_id != expected_path_id:
        raise ToiError(f"저장 후 TextAsset path ID 검증 실패: {label}")
    if verify_objects[0].read().m_Script != new_text:
        raise ToiError(f"저장 후 TextAsset 본문 검증 실패: {label}")
    return output


def command_build_text(args: argparse.Namespace) -> dict[str, Any]:
    inventory_path = resolved(Path(args.inventory))
    mapping_path = resolved(Path(args.mapping))
    inventory = load_json(inventory_path)
    mapping = load_json(mapping_path)
    translated_root = resolved(Path(args.translated_root))
    authorization_path = resolved(Path(args.authorization))
    if not isinstance(inventory, dict) or not isinstance(mapping, dict):
        raise ToiError("inventory와 mapping은 JSON 객체여야 함")
    if mapping.get("format") != "toi-l10n/text-mapping-v1":
        raise ToiError("지원하지 않는 mapping 형식")
    game_root = common_game_root(inventory)
    output = validate_output_path(Path(args.out_dir), [game_root])
    files = _inventory_files(inventory)
    if mapping.get("game_revision") != inventory.get("game_revision"):
        raise ToiError("mapping과 inventory의 game_revision이 다름")
    mapping_copy = dict(mapping)
    revision = mapping_copy.pop("mapping_revision", None)
    require_hex(revision, "mapping_revision")
    if content_revision(mapping_copy) != revision:
        raise ToiError("mapping_revision이 mapping 내용과 다름")
    entries = mapping.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > 1_000_000
        or any(not isinstance(entry, dict) for entry in entries)
    ):
        raise ToiError("mapping.entries가 비어 있거나 형식/개수 한도를 벗어남")
    blocked = [entry for entry in entries if entry.get("blocking_questions")]
    if blocked:
        raise ToiError(f"미해결 blocking question이 있는 unit: {blocked[0].get('stable_key')}")

    mapping_identities: set[tuple[str, str]] = set()
    destination_identities: set[tuple[str, int, str]] = set()
    for index, entry in enumerate(entries):
        shard_id = require_shard_id(
            entry.get("shard_id"), f"mapping.entries[{index}].shard_id"
        )
        stable_key = entry.get("stable_key")
        ja_bundle = entry.get("ja_bundle")
        ja_path_id = entry.get("ja_object_path_id")
        ja_record_id = entry.get("ja_record_id")
        if (
            not isinstance(stable_key, str)
            or not stable_key
            or not isinstance(ja_bundle, str)
            or not isinstance(ja_path_id, int)
            or not isinstance(ja_record_id, str)
            or not ja_record_id
        ):
            raise ToiError(f"mapping identity 또는 ja destination이 잘못됨: {index}")
        identity = (shard_id, stable_key)
        destination = (ja_bundle, ja_path_id, ja_record_id)
        if identity in mapping_identities:
            raise ToiError(f"mapping shard/stable_key 중복: {identity}")
        if destination in destination_identities:
            raise ToiError(f"mapping ja destination 중복: {destination}")
        mapping_identities.add(identity)
        destination_identities.add(destination)

    verified_source_bundles: set[str] = set()
    for entry in entries:
        source_bundle = entry.get("source_bundle")
        if not isinstance(source_bundle, str):
            raise ToiError(f"source bundle이 없는 unit: {entry.get('stable_key')}")
        if source_bundle in verified_source_bundles:
            continue
        game_relative = f"{AA_RELATIVE}/{source_bundle}"
        if game_relative not in files:
            raise ToiError(f"inventory에 source bundle이 없음: {game_relative}")
        _verify_file(game_root, game_relative, files[game_relative])
        verified_source_bundles.add(source_bundle)

    shard_entries: dict[str, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        shard_id = require_shard_id(
            entry.get("shard_id"), f"mapping.entries[{index}].shard_id"
        )
        shard_entries.setdefault(shard_id, []).append(entry)
    authorization, authorized_shards = _validated_build_authorization(
        authorization_path,
        translated_root,
        inventory["game_revision"],
        revision,
        set(shard_entries),
    )
    if authorization["mode"] == "reviewed-translations":
        if args.batch_manifest is None or args.canonical is None:
            raise ToiError(
                "reviewed-translations build에는 --batch-manifest와 --canonical이 필요함"
            )
        from text_pipeline import reviewed_build_authorization

        expected_authorization = reviewed_build_authorization(
            Path(args.batch_manifest),
            inventory_path,
            Path(args.canonical),
            translated_root,
        )
        if authorization != expected_authorization:
            raise ToiError(
                "build authorization이 현재 전체 manifest/canonical/승인 shard 증거와 다름"
            )
    authorization_mode = authorization["mode"]
    smoke = authorization.get("font_smoke")
    smoke_probes = (
        smoke["probes"]
        if authorization_mode == "font-smoke"
        else []
    )
    probes_by_shard: dict[str, list[dict[str, Any]]] = {}
    for probe in smoke_probes:
        probes_by_shard.setdefault(probe["shard_id"], []).append(probe)
    applied_probes: set[tuple[str, str]] = set()
    translations: dict[str, dict[str, str]] = {}
    for shard_id, rows in shard_entries.items():
        _, path = authorized_shards[shard_id]
        value = load_json(path)
        if (
            not isinstance(value, dict)
            or not all(
                isinstance(key, str)
                and isinstance(text, str)
                and len(text) <= 65_536
                for key, text in value.items()
            )
        ):
            raise ToiError(f"번역 shard는 65,536자 이하 문자열 객체여야 함: {path}")
        expected = {row["stable_key"] for row in rows}
        if set(value) != expected:
            missing = sorted(expected - set(value))
            extra = sorted(set(value) - expected)
            raise ToiError(f"번역 shard ID 집합 불일치: {shard_id}; missing={missing[:1]}, extra={extra[:1]}")
        if authorization_mode in {"current-ja-roundtrip", "font-smoke"}:
            current_values = {
                row["stable_key"]: row.get("ja_original_value") for row in rows
            }
            if authorization_mode == "font-smoke":
                for probe in probes_by_shard.get(shard_id, []):
                    stable_key = probe["stable_key"]
                    original_value = current_values.get(stable_key)
                    if (
                        not isinstance(original_value, str)
                        or sha256_bytes(original_value.encode("utf-8"))
                        != probe["original_sha256"]
                    ):
                        raise ToiError(
                            f"font smoke 대상의 기존 ja 값 또는 해시 불일치: "
                            f"{shard_id}/{stable_key}"
                        )
                    current_values[stable_key] = probe["target"]
                    applied_probes.add((shard_id, stable_key))
            if value != current_values:
                raise ToiError(
                    f"{authorization_mode} 입력이 승인된 ja 기대값과 다름: {shard_id}"
                )
        translations[shard_id] = value
    if authorization_mode == "font-smoke" and len(applied_probes) != len(smoke_probes):
        raise ToiError("font smoke probes 일부를 mapping shard에서 찾지 못함")

    bundle_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        ja_bundle = entry.get("ja_bundle")
        if not isinstance(ja_bundle, str):
            raise ToiError(f"ja 대상 bundle이 없는 unit: {entry.get('stable_key')}")
        bundle_entries.setdefault(ja_bundle, []).append(entry)

    built_bundles: dict[str, bytes] = {}
    bundle_report: list[dict[str, Any]] = []
    catalog_updates: dict[str, dict[str, Any]] = {}
    for bundle_relative, rows in sorted(bundle_entries.items()):
        game_relative = f"{AA_RELATIVE}/{bundle_relative}"
        if game_relative not in files:
            raise ToiError(f"inventory에 ja bundle이 없음: {game_relative}")
        bundle_path, original_bytes = _verified_file_bytes(
            game_root, game_relative, files[game_relative]
        )
        _, path_id, text = _text_asset(bundle_path, original_bytes)
        expected_path_ids = {row.get("ja_object_path_id") for row in rows}
        if expected_path_ids != {path_id}:
            raise ToiError(f"mapping의 ja object path ID가 일치하지 않음: {bundle_relative}")
        document_hashes = {row.get("ja_document_sha256") for row in rows}
        if document_hashes != {sha256_bytes(text.encode("utf-8"))}:
            raise ToiError(f"mapping 이후 ja TextAsset 본문이 변경됨: {bundle_relative}")
        targets = {row["ja_record_id"]: translations[row["shard_id"]][row["stable_key"]] for row in rows}
        kinds = {row["record_kind"] for row in rows}
        if kinds == {"script"}:
            document = parse_script_document(text)
            preserve_duplicates = {
                row["ja_record_id"]
                for row in rows
                if row.get("ja_record_occurrences", 1) > 1
                and translations[row["shard_id"]][row["stable_key"]] == row.get("ja_original_value")
            }
            new_text = document.replace(targets, preserve_duplicates)
        elif kinds == {"managed_text"}:
            new_text = parse_managed_document(text).replace(targets)
        else:
            raise ToiError(f"한 bundle에 여러 record_kind가 섞임: {bundle_relative}")
        changed = new_text != text
        if changed:
            output_bytes = _save_text_asset_bundle(
                original_bytes, bundle_path, path_id, new_text
            )
            crc = _bundle_crc(output_bytes)
            hash128 = hashlib.md5(output_bytes, usedforsecurity=False).hexdigest()
            catalog_updates[bundle_relative] = {
                "crc": crc,
                "hash128": hash128,
                "size": len(output_bytes),
            }
        else:
            output_bytes = original_bytes
            crc = _bundle_crc(output_bytes)
            hash128 = None
            if output_bytes != original_bytes:
                raise ToiError(f"무변경 bundle이 byte-identical하지 않음: {bundle_relative}")
        built_bundles[bundle_relative] = output_bytes
        bundle_report.append(
            {
                "path": bundle_relative,
                "changed": changed,
                "source_sha256": files[game_relative]["sha256"],
                "output_sha256": sha256_bytes(output_bytes),
                "size": len(output_bytes),
                "crc": crc,
                "catalog_hash128": hash128,
            }
        )

    catalog_record = files.get(CATALOG_RELATIVE)
    settings_record = files.get(SETTINGS_RELATIVE)
    if not catalog_record or not settings_record:
        raise ToiError("inventory에 catalog/settings가 없음")
    catalog_path, original_catalog_bytes = _verified_file_bytes(
        game_root, CATALOG_RELATIVE, catalog_record
    )
    _, settings_bytes = _verified_file_bytes(
        game_root, SETTINGS_RELATIVE, settings_record
    )
    if catalog_updates:
        try:
            catalog_document = json.loads(original_catalog_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToiError("원본 catalog가 올바른 UTF-8 JSON이 아님") from exc
        if not isinstance(catalog_document, dict):
            raise ToiError("원본 catalog JSON 루트는 객체여야 함")
        patched = AddressablesCatalog(catalog_document).patched_bundle_metadata(
            catalog_updates
        )
        catalog_bytes = json.dumps(
            patched, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        verified_catalog = AddressablesCatalog(json.loads(catalog_bytes.decode("utf-8")))
        verified_indices = verified_catalog.bundle_entry_indices()
        for relative in catalog_updates:
            if relative not in verified_indices:
                raise ToiError(f"패치 catalog 검증에서 bundle을 찾지 못함: {relative}")
    else:
        catalog_bytes = original_catalog_bytes

    with staged_output_directory(output) as staging:
        overlay = staging / "overlay"
        for relative, data in built_bundles.items():
            destination = overlay / AA_RELATIVE / relative
            write_bytes_new(destination, data)
        catalog_destination = overlay / CATALOG_RELATIVE
        write_bytes_new(catalog_destination, catalog_bytes)
        write_bytes_new(overlay / SETTINGS_RELATIVE, settings_bytes)
        manifest = {
            "format": "toi-l10n/ja-text-build-v1",
            "game_revision": inventory["game_revision"],
            "mapping_revision": mapping["mapping_revision"],
            "translated_root": str(translated_root),
            "authorization": {
                "path": str(authorization_path),
                "sha256": sha256_file(authorization_path),
                "mode": authorization_mode,
            },
            "hash128_algorithm": "md5-of-rebuilt-bundle-bytes",
            "catalog_build_result_hash_preserved": True,
            "catalog": {
                "source_sha256": catalog_record["sha256"],
                "output_sha256": sha256_bytes(catalog_bytes),
                "changed": bool(catalog_updates),
            },
            "settings": {
                "source_sha256": settings_record["sha256"],
                "output_sha256": sha256_bytes(settings_bytes),
                "changed": False,
            },
            "bundles": bundle_report,
        }
        manifest["build_revision"] = content_revision(manifest)
        dump_json_new(staging / "build-manifest.json", manifest)
    return {
        "bundles": len(bundle_report),
        "changed_bundles": sum(row["changed"] for row in bundle_report),
        "build_revision": manifest["build_revision"],
        "out_dir": str(output),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trials of Innocence Unity/Naninovel localization adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="hash the read-only game and decode Addressables catalog metadata")
    inventory.add_argument("--game-root", required=True)
    inventory.add_argument("--out", required=True)
    inventory.set_defaults(handler=command_inventory)

    export = subparsers.add_parser("export-text", help="export Simplified Chinese source and ja target mapping")
    export.add_argument("--inventory", required=True)
    export.add_argument("--out-dir", required=True)
    export.set_defaults(handler=command_export_text)

    font_smoke = subparsers.add_parser(
        "prepare-font-smoke",
        help="copy current ja shards and authorize Korean render probes",
    )
    font_smoke.add_argument("--inventory", required=True)
    font_smoke.add_argument("--mapping", required=True)
    font_smoke.add_argument("--current-root", required=True)
    font_smoke.add_argument("--probes", required=True)
    font_smoke.add_argument("--out-dir", required=True)
    font_smoke.set_defaults(handler=command_prepare_font_smoke)

    build = subparsers.add_parser("build-text", help="rebuild ja TextAssets and patch Addressables bundle metadata")
    build.add_argument("--inventory", required=True)
    build.add_argument("--mapping", required=True)
    build.add_argument("--translated-root", required=True)
    build.add_argument("--authorization", required=True)
    build.add_argument("--batch-manifest")
    build.add_argument("--canonical")
    build.add_argument("--out-dir", required=True)
    build.set_defaults(handler=command_build_text)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except ToiError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"예상하지 못한 오류: {type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
