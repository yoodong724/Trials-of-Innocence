#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any

import UnityPy
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.ttLib.reorderGlyphs import reorderGlyphs
from fontTools.ttLib.tables._g_l_y_f import Glyph

from toi_catalog import AddressablesCatalog
from toi_common import (
    ToiError,
    common_game_root,
    content_revision,
    dump_json_new,
    load_json,
    path_from_record,
    require_hex,
    resolved,
    sha256_bytes,
    sha256_file,
    staged_output_directory,
    validate_output_path,
    write_bytes_new,
)

DATA_DIR_NAME = "Trials of Innocence_Data"
AA_RELATIVE = f"{DATA_DIR_NAME}/StreamingAssets/aa"
CATALOG_RELATIVE = f"{AA_RELATIVE}/catalog.json"
SETTINGS_RELATIVE = f"{AA_RELATIVE}/settings.json"
FONT_BUNDLE_RELATIVE = (
    "StandaloneWindows64/wuzui_assets_naninovel/fonts/"
    "sourcehanserifcn-bold-2_a5b9c9a97c0fbee1a62992f4ec5d156c.bundle"
)
FONT_OBJECT_NAME = "SourceHanSerifCN-Bold-2"
SDF_FONT_BUNDLE_RELATIVE = (
    "StandaloneWindows64/wuzui_assets_naninovel/fonts/"
    "sourcehanserifcn-bold-2_sdf_fe742cf67d176e5409b30f2ec005a34c.bundle"
)
SDF_FONT_ASSET_NAME = "SourceHanSerifCN-Bold-2_SDF"
GENERATED_FAMILY = "TOI Korean Serif"
HANGUL_START = 0xAC00
HANGUL_END = 0xD7A3
REQUIRED_CODEPOINTS = (0xD55C, 0xAE00, 0xD45C, 0xC2DC, 0xD655, 0xC778)
MAX_FONT_BYTES = 64 * 1024 * 1024
OFL_SHA256 = "6cf3dbe8a3afffe25bd51d1b16f2c2da2ead89ff4e77f21589a2b589b45b1969"
MAX_LICENSE_BYTES = 64 * 1024


def _font_names(font: TTFont, name_id: int) -> list[str]:
    names: set[str] = set()
    for record in font["name"].names:
        if record.nameID != name_id:
            continue
        try:
            names.add(record.toUnicode())
        except UnicodeDecodeError:
            continue
    return sorted(names)


def _rename_generated_font(font: TTFont, generated_family: str = GENERATED_FAMILY, generated_style: str = "Bold") -> None:
    name_table = font["name"]
    values = {
        1: generated_family,
        2: generated_style,
        3: f"{generated_family} {generated_style}; 2026-09-26",
        4: f"{generated_family} {generated_style}",
        5: "Version 1.0",
        6: f"TOIKoreanSans-{generated_style}" if generated_family == "TOI Korean Sans" else f"TOIKoreanSerif-{generated_style}",
        16: generated_family,
        17: generated_style,
        21: generated_family,
        22: generated_style,
    }
    existing = [
        (
            record.nameID,
            record.platformID,
            record.platEncID,
            record.langID,
        )
        for record in name_table.names
        if record.nameID in values
    ]
    for name_id, platform_id, encoding_id, language_id in existing:
        name_table.setName(
            values[name_id],
            name_id,
            platform_id,
            encoding_id,
            language_id,
        )
    for name_id, value in values.items():
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 3, 10, 0x409)


def _static_glyph_indices(bundle_bytes: bytes, asset_name: str = SDF_FONT_ASSET_NAME) -> set[int]:
    environment = UnityPy.load(bundle_bytes)
    candidates: list[dict[str, Any]] = []
    for obj in environment.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        if tree.get("m_Name") == asset_name:
            candidates.append(tree)
    if len(candidates) != 1:
        raise ToiError(
            f"SDF font asset을 하나로 결정할 수 없음: {len(candidates)}"
        )
    glyphs = candidates[0].get("m_GlyphTable")
    if not isinstance(glyphs, list) or not glyphs:
        raise ToiError("SDF font asset glyph table이 비어 있음")
    indices: set[int] = set()
    for row in glyphs:
        index = row.get("m_Index") if isinstance(row, dict) else None
        if not isinstance(index, int) or index <= 0 or index > 0xFFFF:
            raise ToiError("SDF font asset glyph index가 잘못됨")
        if index in indices:
            raise ToiError(f"SDF font asset glyph index 중복: {index}")
        indices.add(index)
    return indices


def _avoid_static_glyph_collisions(
    font: TTFont,
    reserved_indices: set[int],
) -> int:
    if 0 in reserved_indices:
        raise ToiError("glyph index 0은 .notdef를 위해 예약할 수 없음")
    original_order = font.getGlyphOrder()
    if not original_order or original_order[0] != ".notdef":
        raise ToiError("font glyph order가 .notdef로 시작하지 않음")
    new_order: list[str] = []
    dummy_names: list[str] = []
    original_index = 0
    output_index = 0
    while original_index < len(original_order):
        if output_index in reserved_indices:
            name = f".toi_reserved_{output_index:05d}"
            if name in font["glyf"].glyphs:
                raise ToiError(f"font padding glyph 이름 충돌: {name}")
            font["glyf"].glyphs[name] = Glyph()
            font["hmtx"].metrics[name] = (0, 0)
            if "vmtx" in font:
                font["vmtx"].metrics[name] = (0, 0)
            dummy_names.append(name)
            new_order.append(name)
        else:
            new_order.append(original_order[original_index])
            original_index += 1
        output_index += 1
        if output_index > 0xFFFF:
            raise ToiError("충돌 회피 후 glyph index가 16비트 범위를 넘음")
    font.setGlyphOrder(original_order + dummy_names)
    reorderGlyphs(font, new_order)
    font["maxp"].numGlyphs = len(new_order)
    return len(dummy_names)


def _prepare_font(
    path: Path,
    weight: int,
    reserved_glyph_indices: set[int],
    generated_family: str = GENERATED_FAMILY,
) -> tuple[bytes, dict[str, Any]]:
    if weight not in (500, 700):
        raise ToiError("현재 TMP asset과 일치하도록 --weight 500 또는 700만 지원함")
    try:
        size = path.stat().st_size
        if size > MAX_FONT_BYTES:
            raise ToiError(f"font 입력이 {MAX_FONT_BYTES}바이트 한도를 넘음")
        source_bytes = path.read_bytes()
        if len(source_bytes) > MAX_FONT_BYTES:
            raise ToiError(f"font 입력이 {MAX_FONT_BYTES}바이트 한도를 넘음")
        font = TTFont(BytesIO(source_bytes), lazy=False)
    except OSError as exc:
        raise ToiError(f"font 입력을 읽을 수 없음: {path}: {exc}") from exc
    except Exception as exc:
        raise ToiError(f"지원하지 않는 OpenType font: {path}: {exc}") from exc

    original_family = _font_names(font, 16) or _font_names(font, 1)
    source_copyright = _font_names(font, 0)
    if not source_copyright:
        raise ToiError("font name table에서 저작권 고지를 확인하지 못함")
    license_texts = _font_names(font, 13)
    if not any("SIL Open Font License" in text for text in license_texts):
        raise ToiError("font name table에서 SIL Open Font License를 확인하지 못함")
    if "fvar" in font:
        axes = {axis.axisTag: axis for axis in font["fvar"].axes}
        axis = axes.get("wght")
        if axis is None or not axis.minValue <= weight <= axis.maxValue:
            raise ToiError(f"font의 wght axis가 {weight}를 지원하지 않음")
        instantiateVariableFont(font, {"wght": weight}, inplace=True, optimize=True)
    generated_style = "Medium" if weight == 500 else "Bold"
    _rename_generated_font(font, generated_family, generated_style)
    padding_glyphs = _avoid_static_glyph_collisions(
        font, reserved_glyph_indices
    )
    font.recalcTimestamp = False
    output = BytesIO()
    font.save(output)
    generated = output.getvalue()
    if len(generated) > MAX_FONT_BYTES:
        raise ToiError("생성 font가 크기 한도를 넘음")

    verified = TTFont(BytesIO(generated), lazy=False)
    if "fvar" in verified:
        raise ToiError("생성 font에 variable axis가 남음")
    best_cmap = verified.getBestCmap() or {}
    cmap = set(best_cmap)
    missing = [codepoint for codepoint in REQUIRED_CODEPOINTS if codepoint not in cmap]
    hangul_count = sum(HANGUL_START <= codepoint <= HANGUL_END for codepoint in cmap)
    collisions = [
        (codepoint, verified.getGlyphID(glyph_name))
        for codepoint, glyph_name in best_cmap.items()
        if verified.getGlyphID(glyph_name) in reserved_glyph_indices
    ]
    if (
        missing
        or hangul_count != HANGUL_END - HANGUL_START + 1
        or collisions
    ):
        raise ToiError(
            "생성 font의 한글 범위 또는 glyph index 충돌 오류: "
            f"missing={missing}, syllables={hangul_count}, collisions={collisions[:3]}"
        )
    generated_family_names = _font_names(verified, 16) or _font_names(verified, 1)
    reserved_names = {
        value
        for name_id in (1, 4, 6, 16, 21)
        for value in _font_names(verified, name_id)
    }
    if (
        generated_family not in generated_family_names
        or any(
            "Noto" in value or "Source Han" in value
            for value in reserved_names
        )
    ):
        raise ToiError("생성 font의 Reserved Font Name 제거가 실패함")
    generated_copyright = _font_names(verified, 0)
    if generated_copyright != source_copyright:
        raise ToiError("생성 font가 원본 저작권 고지를 보존하지 않음")
    return generated, {
        "source_path": str(path),
        "source_size": size,
        "source_sha256": sha256_bytes(source_bytes),
        "source_family": original_family,
        "source_copyright": source_copyright,
        "source_license": "SIL Open Font License 1.1",
        "generated_family": generated_family,
        "generated_style": generated_style,
        "generated_size": len(generated),
        "generated_sha256": sha256_bytes(generated),
        "weight": weight,
        "hangul_syllables": hangul_count,
        "reserved_glyph_indices": len(reserved_glyph_indices),
        "reserved_glyph_revision": content_revision(
            sorted(reserved_glyph_indices)
        ),
        "padding_glyphs": padding_glyphs,
        "required_codepoints": [f"U+{codepoint:04X}" for codepoint in REQUIRED_CODEPOINTS],
    }


def _replace_embedded_font(
    original_bundle: bytes,
    generated_font: bytes,
    object_name: str = FONT_OBJECT_NAME,
) -> tuple[bytes, int]:
    environment = UnityPy.load(original_bundle)
    objects = [obj for obj in environment.objects if obj.type.name == "Font"]
    if len(objects) != 1:
        raise ToiError(f"source font bundle의 Font 객체 수가 1이 아님: {len(objects)}")
    obj = objects[0]
    data = obj.read()
    if data.m_Name != object_name:
        raise ToiError(f"예상하지 않은 source Font 이름: {data.m_Name!r}")
    data.m_FontData = generated_font
    data.save()
    output = environment.file.save(packer="original")

    verify = UnityPy.load(output)
    verify_objects = [obj for obj in verify.objects if obj.type.name == "Font"]
    if len(verify_objects) != 1 or verify_objects[0].path_id != obj.path_id:
        raise ToiError("저장 후 source Font path ID 검증 실패")
    verify_data = verify_objects[0].read()
    if verify_data.m_Name != object_name or bytes(verify_data.m_FontData) != generated_font:
        raise ToiError("저장 후 source Font 이름 또는 바이트 검증 실패")
    return output, obj.path_id
