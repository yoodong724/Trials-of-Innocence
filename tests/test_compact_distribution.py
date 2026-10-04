"""Contracts shared by the tools-only builder, verifier and installer."""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build
import verify
from patch_layout import compact_manifest, compact_name, payload_relative
from source_patch import CATALOG, original_file
from toi_common import content_revision, sha256_bytes


class CompactDistributionTests(unittest.TestCase):
    def test_packager_reproduces_reference_zip_metadata_and_order(self):
        manifest, _, data = self.manifest()
        manifest = compact_manifest(manifest, 'v1.0.0')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root / 'KoreanPatch'
            payload = folder / 'payload' / payload_relative(manifest['changes'][0])
            payload.parent.mkdir(parents=True); payload.write_bytes(data)
            (folder / 'patch-manifest.json').write_text(json.dumps(manifest))
            extras = ['README.ko.txt', 'install.cmd', 'licenses/FONT-NOTICE.txt',
                      'licenses/OFL-1.1.txt', 'patch-manifest.json', 'patch-manifest.sha256',
                      'patch.ps1', 'restore.cmd']
            for name in extras:
                path = folder / name; path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists(): path.write_bytes(name.encode())
            order = [payload] + [folder / name for name in extras]
            expected = root / 'expected.zip'
            with zipfile.ZipFile(expected, 'w', compression=zipfile.ZIP_STORED) as zipped:
                for path in order:
                    info = zipfile.ZipInfo('KoreanPatch/' + path.relative_to(folder).as_posix(),
                                           date_time=(2026, 10, 4, 0, 0, 0))
                    info.external_attr = 0o100644 << 16
                    zipped.writestr(info, path.read_bytes())
            reference = {'zip_format': {'timestamp': [2026, 10, 4, 0, 0, 0],
                                       'order': 'manifest-payloads-then-sorted-metadata'}}
            with patch.object(build, 'load_json', side_effect=lambda path:
                              reference if path.name == 'build-reference.json' else json.loads(path.read_text())):
                actual = root / 'actual.zip'; build.package(folder, actual)
            self.assertEqual(actual.read_bytes(), expected.read_bytes())

    def test_release_names_keep_legacy_patch_id_and_support_revision_suffix(self):
        manifest, _, _ = self.manifest()
        for version in ('v7', 'v7.8', 'v1.0.0', 'v1.0.0-rc2'):
            converted = compact_manifest(manifest, version)
            label = 'test patch' if version in ('v7', 'v7.8') else 'patch'
            self.assertEqual(converted['name'], f'Trials of Innocence Korean {label} {version}')
            self.assertEqual(compact_manifest(converted, version), converted)
            self.assertEqual(converted['game_files'], manifest['game_files'])

    def manifest(self):
        old, new = b'original', b'{"catalog":"test"}'
        row = {'path': CATALOG, 'operation': 'replace', 'source_sha256': sha256_bytes(old),
               'output_sha256': sha256_bytes(new), 'output_size': len(new)}
        manifest = {'format': 'toi-l10n/game-patch-v2', 'name': 'test v7',
                    'scope': {'changed_files': 1}, 'changes': [row],
                    'game_files': [{'path': CATALOG, 'sha256': sha256_bytes(old), 'size': len(old)}]}
        manifest['patch_id'] = content_revision(manifest)
        return manifest, old, new

    def test_frozen_inputs_convert_without_changing_game_file_hashes(self):
        manifest, _, _ = self.manifest()
        converted = compact_manifest(manifest, 'v7.1')
        self.assertEqual(converted['game_files'], manifest['game_files'])
        self.assertEqual(converted['changes'][0]['output_sha256'], manifest['changes'][0]['output_sha256'])
        self.assertNotIn('payload_path', manifest['changes'][0])
        self.assertEqual(compact_manifest(converted, 'v7.1'), converted)
        self.assertEqual(content_revision({k: v for k, v in converted.items() if k != 'patch_id'}), converted['patch_id'])

    def test_original_lookup_supports_compact_backup_after_install(self):
        manifest, old, new = self.manifest()
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory)
            file = game / CATALOG
            file.parent.mkdir(parents=True)
            file.write_bytes(new)
            backup = game / 'KoreanPatch/backup'
            backup.mkdir(parents=True)
            (backup / 'backup-manifest.json').write_text(json.dumps({'format': 'toi-l10n/game-patch-backup-v2'}))
            saved = backup / compact_name(CATALOG)
            saved.write_bytes(old)
            self.assertEqual(original_file(game, CATALOG, sha256_bytes(old)), saved)

    def test_verified_original_does_not_read_unused_backup_metadata(self):
        _, old, _ = self.manifest()
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory)
            file = game / CATALOG
            file.parent.mkdir(parents=True)
            file.write_bytes(old)
            backup = game / 'KoreanPatch/backup'
            backup.mkdir(parents=True)
            (backup / 'backup-manifest.json').write_text('invalid json')
            self.assertEqual(original_file(game, CATALOG, sha256_bytes(old)), file)

    def test_builder_and_verifier_share_compact_frozen_manifest(self):
        manifest, old, new = self.manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game, inputs, output = root / 'game', root / 'inputs', root / 'output'
            game.mkdir(); inputs.mkdir()
            original = game / 'original'
            original.write_bytes(old)
            sources = (manifest, {}, manifest['changes'], [], [], [])
            with patch.object(build, 'read_sources', return_value=sources), \
                 patch.object(build, 'original_file', return_value=original), \
                 patch.object(build, 'reconstruct', return_value=new):
                result = build.build(game, output, inputs=inputs)
            folder = output / 'KoreanPatch'
            actual = json.loads((folder / 'patch-manifest.json').read_text())
            version = build.load_json(build.ROOT / 'patch/build-reference.json')['version']
            self.assertEqual(actual, compact_manifest(manifest, version))
            self.assertEqual(result['patch_id'], actual['patch_id'])
            self.assertEqual((folder / 'payload' / payload_relative(actual['changes'][0])).read_bytes(), new)
            with patch.object(verify, 'read_sources', return_value=sources):
                self.assertEqual(verify.verify(folder=folder, inputs=inputs)['build'], 'pass')
            archive = root / 'fixture.zip'
            build.package(folder, archive)
            with zipfile.ZipFile(archive) as zipped:
                self.assertIsNone(zipped.testzip())
                self.assertTrue(all(name.isascii() for name in zipped.namelist()))
                for name in zipped.namelist():
                    self.assertEqual(zipped.read(name), (output / name).read_bytes())

    def test_compact_manifest_serialization_preserves_the_frozen_field_order(self):
        manifest, old, new = self.manifest()
        version = build.load_json(build.ROOT / 'patch/build-reference.json')['version']
        manifest = compact_manifest(manifest, version)
        row = manifest['changes'][0]
        manifest['changes'][0] = {k: row[k] for k in ('path', 'payload_path', 'operation',
                                                      'source_sha256', 'output_sha256', 'output_size')}
        expected = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); game, inputs, output = root/'game', root/'inputs', root/'output'
            game.mkdir(); inputs.mkdir(); original = game/'original'; original.write_bytes(old)
            sources = (manifest, {}, manifest['changes'], [], [], [])
            with patch.object(build, 'read_sources', return_value=sources), \
                 patch.object(build, 'original_file', return_value=original), \
                 patch.object(build, 'reconstruct', return_value=new):
                build.build(game, output, inputs=inputs)
            self.assertEqual((output/'KoreanPatch/patch-manifest.json').read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
