"""Shared contracts for the public patch source (no installed-game writes)."""
from __future__ import annotations
import gzip
from enum import Enum
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import UnityPy
from UnityPy.streams import EndianBinaryReader
from toi_common import ToiError, content_revision, load_json, sha256_bytes, sha256_file

ROOT = Path(__file__).resolve().parents[1]
AA = 'Trials of Innocence_Data/StreamingAssets/aa/'
CATALOG = AA + 'catalog.json'
HEADER_FIELDS = ('signature', 'version', 'version_player', 'version_engine',
                 'dataflags', '_block_info_flags', '_uses_block_alignment')


def safe_path(root: Path, relative: str) -> Path:
    value = PurePosixPath(relative)
    if not relative or '\\' in relative or ':' in relative or value.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ToiError(f'Unsafe relative path: {relative!r}')
    base = root.resolve()
    candidate = base.joinpath(*value.parts)
    cursor = candidate
    while cursor != base:
        if cursor.is_symlink():
            raise ToiError(f'Symlink is not allowed: {relative}')
        cursor = cursor.parent
    if not candidate.resolve().is_relative_to(base):
        raise ToiError(f'Path leaves its root: {relative}')
    return candidate


def clean_output(output: Path, game: Path) -> Path:
    output, game = output.absolute(), game.resolve()
    resolved = output.resolve()
    if resolved.is_relative_to(game) or game.is_relative_to(resolved) or resolved == ROOT or ROOT.is_relative_to(resolved):
        raise ToiError('Output must be separate from the game and source roots')
    if output.exists() or output.is_symlink():
        raise ToiError(f'Output already exists: {output}')
    return output


def original_file(game: Path, relative: str, expected: str) -> Path:
    for root in (game, game / 'KoreanPatch/backup'):
        path = safe_path(root, relative)
        if path.is_file() and sha256_file(path) == expected:
            return path
    raise ToiError(f'Verified original not found: {relative}')


def xdelta(args: list[str], executable: str = 'xdelta3') -> None:
    env = dict(os.environ)
    env.pop('XDELTA', None)
    process = subprocess.run([executable, *args], env=env, capture_output=True, text=True)
    if process.returncode:
        raise ToiError(f'xdelta3 failed ({process.returncode}): {process.stderr.strip()}')


def encode_delta(before: bytes, after: bytes, executable: str = 'xdelta3') -> bytes:
    with tempfile.TemporaryDirectory(prefix='toi-delta-') as directory:
        root = Path(directory)
        (root / 'source').write_bytes(before)
        (root / 'target').write_bytes(after)
        xdelta(['-e', '-9', '-S', 'none', '-A', '-D', '-s', str(root / 'source'),
                str(root / 'target'), str(root / 'delta')], executable)
        return gzip.compress((root / 'delta').read_bytes(), mtime=0)


def decode_delta(before: bytes, delta: bytes, executable: str = 'xdelta3') -> bytes:
    with tempfile.TemporaryDirectory(prefix='toi-delta-') as directory:
        root = Path(directory)
        (root / 'source').write_bytes(before)
        (root / 'delta').write_bytes(gzip.decompress(delta))
        xdelta(['-d', '-D', '-R', '-s', str(root / 'source'), str(root / 'delta'), str(root / 'target')], executable)
        return (root / 'target').read_bytes()


def member_bytes(member) -> bytes:
    return member.reader.bytes if hasattr(member, 'reader') else member.bytes


def bundle_header(bundle) -> dict:
    return {key: int(value) if isinstance(value := getattr(bundle, key), int) else value for key in HEADER_FIELDS}


def pack_members(bundle, header: dict, members: list[tuple[str, int, bytes]]) -> bytes:
    if set(header) != set(HEADER_FIELDS):
        raise ToiError('Invalid Unity bundle header')
    for key, value in header.items():
        setattr(bundle, key, type(getattr(bundle, key))(value) if isinstance(getattr(bundle, key), Enum) else value)
    bundle.files = {}
    for name, flags, data in members:
        if name in bundle.files:
            raise ToiError('Duplicate Unity bundle member')
        reader = EndianBinaryReader(data)
        reader.flags = flags
        bundle.files[name] = reader
    return bundle.save(packer='original')


def reconstruct(row: dict, game: Path, root: Path = ROOT, executable: str = 'xdelta3') -> bytes:
    before = original_file(game, row['base_path'], row['base_sha256']).read_bytes()
    if row['kind'] == 'raw':
        delta = safe_path(root, row['delta']).read_bytes()
        if sha256_bytes(delta) != row['delta_sha256']:
            raise ToiError('Delta fingerprint mismatch')
        result = decode_delta(before, delta, executable)
    elif row['kind'] == 'unity':
        env = UnityPy.load(before)
        members = []
        for item in row['members']:
            if item['source_name'] not in env.file.files:
                raise ToiError('Unity source member not found')
            data = member_bytes(env.file.files[item['source_name']])
            if sha256_bytes(data) != item['source_sha256']:
                raise ToiError('Unity source member fingerprint mismatch')
            if item.get('delta'):
                delta = safe_path(root, item['delta']).read_bytes()
                if sha256_bytes(delta) != item['delta_sha256']:
                    raise ToiError('Delta fingerprint mismatch')
                data = decode_delta(data, delta, executable)
            if sha256_bytes(data) != item['output_sha256'] or len(data) != item['output_size']:
                raise ToiError('Unity member output mismatch')
            members.append((item['name'], item['flags'], data))
        result = pack_members(env.file, row['header'], members)
    else:
        raise ToiError('Unknown recipe kind')
    if sha256_bytes(result) != row['output_sha256'] or len(result) != row['output_size']:
        raise ToiError(f'Reconstructed payload mismatch: {row["path"]}')
    return result


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def baseline_manifest(root: Path = ROOT) -> dict:
    manifest = load_json(root / 'patch/manifest.json')
    if manifest['format'] != 'toi-l10n/game-patch-v2' or content_revision({k:v for k,v in manifest.items() if k != 'patch_id'}) != manifest['patch_id']:
        raise ToiError('Patch manifest fingerprint mismatch')
    return manifest
