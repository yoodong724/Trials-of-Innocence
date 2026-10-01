from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toi_common import ToiError


_INT32 = struct.Struct("<i")
_ENTRY = struct.Struct("<7i")
_BUNDLE_OPTIONS = "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"
_BUNDLE_RESOURCE = "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource"


def _i32(data: bytes | bytearray, offset: int) -> int:
    try:
        return _INT32.unpack_from(data, offset)[0]
    except struct.error as exc:
        raise ToiError(f"Addressables int32 범위 오류: {offset}") from exc


def _decode_object(data: bytes, offset: int) -> tuple[Any, int, dict[str, Any] | None]:
    start = offset
    try:
        kind = data[offset]
    except IndexError as exc:
        raise ToiError(f"Addressables 객체 범위 오류: {offset}") from exc
    offset += 1
    if kind in (0, 1):
        length = _i32(data, offset)
        offset += 4
        if length < 0 or offset + length > len(data):
            raise ToiError("Addressables 문자열 길이가 잘못됨")
        encoding = "ascii" if kind == 0 else "utf-16le"
        try:
            value = data[offset : offset + length].decode(encoding)
        except UnicodeDecodeError as exc:
            raise ToiError("Addressables 문자열 인코딩이 잘못됨") from exc
        return value, offset + length, None
    if kind == 2:
        return struct.unpack_from("<H", data, offset)[0], offset + 2, None
    if kind == 3:
        return struct.unpack_from("<I", data, offset)[0], offset + 4, None
    if kind == 4:
        return _i32(data, offset), offset + 4, None
    if kind in (5, 6):
        if offset >= len(data):
            raise ToiError("Addressables 짧은 객체 길이가 없음")
        length = data[offset]
        offset += 1
        end = offset + length
        if end > len(data):
            raise ToiError("Addressables 짧은 객체 길이가 잘못됨")
        raw = data[offset:end]
        if kind == 5:
            try:
                return raw.decode("ascii"), end, None
            except UnicodeDecodeError as exc:
                raise ToiError("Addressables Hash128 문자열이 잘못됨") from exc
        return raw.hex(), end, None
    if kind == 7:
        if offset >= len(data):
            raise ToiError("Addressables JSON assembly 길이가 없음")
        assembly_length = data[offset]
        offset += 1
        if offset + assembly_length > len(data):
            raise ToiError("Addressables JSON assembly 길이가 잘못됨")
        assembly = data[offset : offset + assembly_length].decode("ascii")
        offset += assembly_length
        if offset >= len(data):
            raise ToiError("Addressables JSON class 길이가 없음")
        class_length = data[offset]
        offset += 1
        if offset + class_length > len(data):
            raise ToiError("Addressables JSON class 길이가 잘못됨")
        class_name = data[offset : offset + class_length].decode("ascii")
        offset += class_length
        json_length = _i32(data, offset)
        offset += 4
        if json_length < 0 or offset + json_length > len(data):
            raise ToiError("Addressables JSON 객체 길이가 잘못됨")
        raw_json = data[offset : offset + json_length].decode("utf-16le")
        try:
            value = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ToiError("Addressables JSON 객체가 잘못됨") from exc
        end = offset + json_length
        metadata = {
            "kind": kind,
            "assembly": assembly,
            "class": class_name,
            "raw_json": raw_json,
            "start": start,
            "end": end,
        }
        return value, end, metadata
    raise ToiError(f"지원하지 않는 Addressables 객체 형식: {kind}")


def _encode_json_object(assembly: str, class_name: str, value: dict[str, Any]) -> bytes:
    assembly_bytes = assembly.encode("ascii")
    class_bytes = class_name.encode("ascii")
    if len(assembly_bytes) > 255 or len(class_bytes) > 255:
        raise ToiError("Addressables 타입 이름이 너무 김")
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = text.encode("utf-16le")
    return b"".join(
        (
            b"\x07",
            bytes((len(assembly_bytes),)),
            assembly_bytes,
            bytes((len(class_bytes),)),
            class_bytes,
            _INT32.pack(len(payload)),
            payload,
        )
    )


def normalize_internal_id(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    marker = "StandaloneWindows64/"
    position = normalized.find(marker)
    if position < 0:
        return None
    return normalized[position:]


@dataclass(frozen=True)
class CatalogEntry:
    index: int
    internal_id_index: int
    provider_index: int
    dependency_key_index: int
    dependency_hash: int
    data_index: int
    primary_key_index: int
    resource_type_index: int


class AddressablesCatalog:
    def __init__(self, document: dict[str, Any]):
        self.document = document
        try:
            self.internal_ids = document["m_InternalIds"]
            self.provider_ids = document["m_ProviderIds"]
            self.resource_types = document["m_resourceTypes"]
            encoded_fields = {
                name: document[name]
                for name in (
                    "m_BucketDataString",
                    "m_KeyDataString",
                    "m_EntryDataString",
                    "m_ExtraDataString",
                )
            }
            if any(
                not isinstance(value, str) or len(value) > 64 * 1024 * 1024
                for value in encoded_fields.values()
            ):
                raise ToiError("Addressables catalog 이진 필드가 형식 또는 크기 한도를 벗어남")
            bucket_data = base64.b64decode(encoded_fields["m_BucketDataString"], validate=True)
            key_data = base64.b64decode(encoded_fields["m_KeyDataString"], validate=True)
            entry_data = base64.b64decode(encoded_fields["m_EntryDataString"], validate=True)
            self.extra_data = base64.b64decode(
                encoded_fields["m_ExtraDataString"], validate=True
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ToiError("지원하지 않는 Addressables catalog JSON") from exc
        self.buckets = self._parse_buckets(bucket_data)
        key_count = _i32(key_data, 0)
        if key_count != len(self.buckets):
            raise ToiError("Addressables key/bucket 개수가 다름")
        decoded_keys: list[tuple[Any, int, int]] = []
        seen_offsets: set[int] = set()
        for data_offset, _ in self.buckets:
            if data_offset < 4 or data_offset >= len(key_data) or data_offset in seen_offsets:
                raise ToiError("Addressables key offset 범위 또는 중복 오류")
            seen_offsets.add(data_offset)
            value, end, _ = _decode_object(key_data, data_offset)
            decoded_keys.append((value, data_offset, end))
        ordered_ranges = sorted((start, end) for _, start, end in decoded_keys)
        if any(end > next_start for (_, end), (next_start, _) in zip(ordered_ranges, ordered_ranges[1:])):
            raise ToiError("Addressables key 레코드가 겹침")
        self.keys = [value for value, _, _ in decoded_keys]
        self.entries = self._parse_entries(entry_data)
        self._validate_entry_indices()
        self.entry_keys: list[list[Any]] = [[] for _ in self.entries]
        for key_index, (_, entry_indices) in enumerate(self.buckets):
            if len(set(entry_indices)) != len(entry_indices):
                raise ToiError(f"Addressables bucket에 중복 entry 참조가 있음: {key_index}")
            for entry_index in entry_indices:
                if entry_index < 0 or entry_index >= len(self.entries):
                    raise ToiError("Addressables bucket entry 범위 오류")
                self.entry_keys[entry_index].append(self.keys[key_index])
        self._entry_data = entry_data

    def _validate_entry_indices(self) -> None:
        for entry in self.entries:
            required = (
                (entry.internal_id_index, len(self.internal_ids), "internal ID"),
                (entry.provider_index, len(self.provider_ids), "provider"),
                (entry.primary_key_index, len(self.keys), "primary key"),
                (entry.resource_type_index, len(self.resource_types), "resource type"),
            )
            for index, length, name in required:
                if index < 0 or index >= length:
                    raise ToiError(f"Addressables {name} index 범위 오류: entry {entry.index}")
            for index, length, name in (
                (entry.dependency_key_index, len(self.keys), "dependency key"),
                (entry.data_index, len(self.extra_data), "extra data"),
            ):
                if index != -1 and (index < 0 or index >= length):
                    raise ToiError(f"Addressables {name} index 범위 오류: entry {entry.index}")

    @staticmethod
    def _parse_buckets(data: bytes) -> list[tuple[int, list[int]]]:
        count = _i32(data, 0)
        if count < 0:
            raise ToiError("Addressables bucket 개수가 잘못됨")
        offset = 4
        result: list[tuple[int, list[int]]] = []
        for _ in range(count):
            data_offset = _i32(data, offset)
            entry_count = _i32(data, offset + 4)
            offset += 8
            if entry_count < 0 or offset + 4 * entry_count > len(data):
                raise ToiError("Addressables bucket 길이가 잘못됨")
            entries = list(struct.unpack_from(f"<{entry_count}i", data, offset))
            offset += 4 * entry_count
            result.append((data_offset, entries))
        if offset != len(data):
            raise ToiError("Addressables bucket 후행 데이터가 있음")
        return result

    @staticmethod
    def _parse_entries(data: bytes) -> list[CatalogEntry]:
        count = _i32(data, 0)
        expected = 4 + count * _ENTRY.size
        if count < 0 or expected != len(data):
            raise ToiError("Addressables entry 길이가 잘못됨")
        return [CatalogEntry(index, *_ENTRY.unpack_from(data, 4 + index * _ENTRY.size)) for index in range(count)]

    def _resource_type(self, index: int) -> dict[str, Any]:
        try:
            return self.resource_types[index]
        except (IndexError, TypeError) as exc:
            raise ToiError("Addressables resource type 범위 오류") from exc

    def _expanded_internal_id(self, index: int) -> str:
        try:
            value = self.internal_ids[index]
        except (IndexError, TypeError) as exc:
            raise ToiError("Addressables internal ID 범위 오류") from exc
        prefixes = self.document.get("m_InternalIdPrefixes") or []
        if prefixes and "#" in value:
            prefix_index, suffix = value.rsplit("#", 1)
            if prefix_index.isdigit() and int(prefix_index) < len(prefixes):
                value = prefixes[int(prefix_index)] + suffix
        return value

    def describe_locations(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in self.entries:
            try:
                primary_key = self.keys[entry.primary_key_index]
                dependency_key = (
                    None if entry.dependency_key_index < 0 else self.keys[entry.dependency_key_index]
                )
                provider = self.provider_ids[entry.provider_index]
            except (IndexError, TypeError) as exc:
                raise ToiError("Addressables entry 참조 범위 오류") from exc
            resource_type = self._resource_type(entry.resource_type_index)
            data: Any = None
            data_type: str | None = None
            if entry.data_index >= 0:
                data, _, metadata = _decode_object(self.extra_data, entry.data_index)
                if metadata:
                    data_type = metadata["class"]
            result.append(
                {
                    "index": entry.index,
                    "internal_id": self._expanded_internal_id(entry.internal_id_index),
                    "provider_id": provider,
                    "dependency_key": dependency_key,
                    "dependency_hash": entry.dependency_hash,
                    "primary_key": primary_key,
                    "keys": self.entry_keys[entry.index],
                    "resource_type": {
                        "assembly": resource_type.get("m_AssemblyName"),
                        "class": resource_type.get("m_ClassName"),
                    },
                    "data_type": data_type,
                    "data": data,
                }
            )
        return result

    def bundle_entry_indices(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in self.entries:
            resource_type = self._resource_type(entry.resource_type_index).get("m_ClassName")
            if resource_type != _BUNDLE_RESOURCE:
                continue
            internal_id = self._expanded_internal_id(entry.internal_id_index)
            relative = normalize_internal_id(internal_id)
            if relative is None:
                continue
            if relative in result:
                raise ToiError(f"중복 bundle catalog 위치: {relative}")
            result[relative] = entry.index
        return result

    def patched_bundle_metadata(self, updates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not updates:
            return self.document
        bundle_entries = self.bundle_entry_indices()
        unknown = sorted(set(updates) - set(bundle_entries))
        if unknown:
            raise ToiError(f"catalog에서 bundle을 찾을 수 없음: {unknown[0]}")

        replacements: dict[int, bytes] = {}
        for relative, update in updates.items():
            entry = self.entries[bundle_entries[relative]]
            if entry.data_index < 0:
                raise ToiError(f"bundle 요청 옵션이 없음: {relative}")
            value, _, metadata = _decode_object(self.extra_data, entry.data_index)
            if not metadata or metadata["class"] != _BUNDLE_OPTIONS or not isinstance(value, dict):
                raise ToiError(f"지원하지 않는 bundle 요청 옵션: {relative}")
            required = {"m_Crc", "m_Hash", "m_BundleSize"}
            if not required.issubset(value):
                raise ToiError(f"bundle 요청 옵션 필드가 부족함: {relative}")
            value = dict(value)
            value["m_Crc"] = update["crc"]
            value["m_Hash"] = update["hash128"]
            value["m_BundleSize"] = update["size"]
            replacements[entry.index] = _encode_json_object(
                metadata["assembly"], metadata["class"], value
            )

        new_extra = bytearray()
        unchanged_offsets: dict[int, int] = {}
        new_entry_data = bytearray(self._entry_data)
        for entry in self.entries:
            old_offset = entry.data_index
            if old_offset < 0:
                continue
            if entry.index in replacements:
                new_offset = len(new_extra)
                new_extra.extend(replacements[entry.index])
            else:
                if old_offset not in unchanged_offsets:
                    unchanged_offsets[old_offset] = len(new_extra)
                    _, end, _ = _decode_object(self.extra_data, old_offset)
                    new_extra.extend(self.extra_data[old_offset:end])
                new_offset = unchanged_offsets[old_offset]
            struct.pack_into("<i", new_entry_data, 4 + entry.index * _ENTRY.size + 16, new_offset)

        patched = dict(self.document)
        patched["m_EntryDataString"] = base64.b64encode(new_entry_data).decode("ascii")
        patched["m_ExtraDataString"] = base64.b64encode(new_extra).decode("ascii")
        return patched
