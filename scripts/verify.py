#!/usr/bin/env python3
"""Verify source integrity and optional locally built patch/ZIP bytes."""
from __future__ import annotations
import argparse
import json
import zipfile
from pathlib import Path
from build import read_sources
from source_patch import ROOT, baseline_manifest, safe_path
from toi_common import ToiError, load_json, sha256_file, content_revision


def verify(folder: Path | None = None, archive: Path | None = None, edited: bool = False, inputs: Path | None=None):
    distribution=verify_distribution_tree()
    if inputs is None:
        if folder or archive or edited:raise ToiError('--inputs is required to verify a built patch')
        return distribution
    baseline,lock,recipes,text,inventory,images=read_sources(root=inputs,edited=edited)
    result={'source':'pass','text_keys':len(text),'inventory_items':len(inventory),
            'editable_images':len(images),'payload_files':len(recipes)}
    if folder:
        actual=load_json(folder/'patch-manifest.json')
        if content_revision({k:v for k,v in actual.items() if k!='patch_id'})!=actual['patch_id']:
            raise ToiError('Output patch ID mismatch')
        if not edited and actual!=baseline:raise ToiError('Output differs from the frozen v7 manifest')
        if actual['game_files']!=baseline['game_files']:raise ToiError('Output supports a different game revision')
        if (folder/'patch-manifest.sha256').read_text().strip()!=sha256_file(folder/'patch-manifest.json'):
            raise ToiError('Output manifest sidecar mismatch')
        paths={r['path'] for r in actual['changes']}
        if len(paths)!=len(recipes) or paths!={r['path'] for r in recipes}:raise ToiError('Output resource set differs')
        for row in actual['changes']:
            path=safe_path(folder/'payload',row['path'])
            if path.stat().st_size!=row['output_size'] or sha256_file(path)!=row['output_sha256']:
                raise ToiError('Output resource fingerprint mismatch')
        expected={f'payload/{p}' for p in paths}|{'patch-manifest.json','patch-manifest.sha256','install.cmd','restore.cmd','patch.ps1','README.ko.txt','licenses/OFL-1.1.txt','licenses/FONT-NOTICE.txt'}
        actual_paths={p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        if expected!=actual_paths or any(p.is_symlink() for p in folder.rglob('*')):raise ToiError('Unexpected output package files')
        for name in ['install.cmd','restore.cmd','patch.ps1']:
            if sha256_file(folder/name)!=sha256_file(ROOT/'installer'/name):raise ToiError('Installer differs from source')
        result['build']='pass'
        if archive:
            with zipfile.ZipFile(archive) as zipped:
                if zipped.testzip():raise ToiError('Archive CRC failure')
                names=zipped.namelist()
                if len(names)!=len(set(names)) or set(names)!={'KoreanPatch/'+p for p in expected}:
                    raise ToiError('Archive entry set differs')
                for name in names:
                    if zipped.read(name)!=safe_path(folder,name.removeprefix('KoreanPatch/')).read_bytes():
                        raise ToiError('Archive contents differ from the build')
            digest=sha256_file(archive)
            if not edited and digest!=lock['reference_zip_sha256']:raise ToiError('Archive differs from the reference v7 ZIP')
            result.update(archive='pass',zip_sha256=digest)
    elif archive:raise ToiError('--zip requires --folder')
    return result



def verify_distribution_tree(root: Path = ROOT):
    """Reject story content and resource deltas from the distributed tree."""
    forbidden = ('localization', 'canonical', 'assets/images', 'patch/recipes',
                 'patch/recipes.json', 'patch/manifest.json', 'patch/source-lock.json')
    for name in forbidden:
        path = root / name
        if path.exists() or path.is_symlink():
            raise ToiError(f'Private content found in distribution: {name}')
    required = ('README.md', 'CHANGELOG.md', 'scripts/build.py', 'scripts/verify.py',
                'installer/install.cmd', 'installer/restore.cmd', 'installer/patch.ps1')
    if not all((root/name).is_file() for name in required):
        raise ToiError('Required distribution tool or installer is missing')
    reference = load_json(root/'patch/build-reference.json')
    if reference['format'] != 'toi-l10n/distribution-reference-v1':
        raise ToiError('Invalid distribution reference')
    return {'distribution': 'pass', 'mode': 'tools_only',
            'version': reference['version'], 'private_inputs_in_git': False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path);parser.add_argument('--folder',type=Path);parser.add_argument('--zip',type=Path);parser.add_argument('--edited',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(verify(args.folder,args.zip,args.edited,args.inputs),ensure_ascii=False));return 0
    except (ToiError,OSError,ValueError,KeyError,zipfile.BadZipFile) as exc:
        print(f'Verification failed: {exc}',file=__import__('sys').stderr);return 1
if __name__=='__main__':raise SystemExit(main())
