#!/usr/bin/env python3
"""Build the patch from public source plus a verified original game."""
from __future__ import annotations
import argparse
import json
import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
import UnityPy
from PIL import Image
from toi_adapter import parse_script_document, parse_managed_document
from toi_catalog import AddressablesCatalog
from toi_common import (ToiError, content_revision, load_json, sha256_bytes,
                        sha256_file, staged_output_directory)
from source_patch import (AA, CATALOG, ROOT, baseline_manifest, clean_output,
                          original_file, reconstruct, safe_path, write_json)
from patch_layout import audit_windows_paths, compact_manifest, compact_name, payload_relative
import hashlib
import zlib


def text_bindings(rows):
    return content_revision([{k:v for k,v in row.items() if k not in ('value','occurrence_values')}
                             for row in sorted(rows,key=lambda r:r['key'])])


def read_sources(root: Path = ROOT, edited: bool = False):
    if not (root/'patch/source-lock.json').is_file():
        raise ToiError('Translation inputs are not distributed in Git; supply a local --inputs directory')
    lock=load_json(root/'patch/source-lock.json')
    manifest=baseline_manifest(root)
    if lock['format']!='toi-l10n/public-source-v1' or lock['patch_id']!=manifest['patch_id']:
        raise ToiError('Source lock is for a different patch')
    if sha256_file(root/'patch/recipes.json')!=lock['recipes_sha256']:
        raise ToiError('Resource recipes differ from the source lock')
    for relative,expected in lock['source_sha256'].items():
        path=safe_path(root,relative)
        editable=relative in lock['text_files'] or relative=='localization/inventory/items.json'
        if (not edited or not editable) and sha256_file(path)!=expected:
            raise ToiError(f'Source changed; use --edited for translation updates: {relative}')
    rows=[]
    for relative in lock['text_files']:
        rows.extend(load_json(safe_path(root,relative))['entries'])
    if len(rows)!=lock['text_keys'] or len({r['key'] for r in rows})!=len(rows):
        raise ToiError('Missing or duplicate translation keys')
    if text_bindings(rows)!=lock['text_bindings_sha256']:
        raise ToiError('Translation target bindings changed')
    for row in rows:
        if not isinstance(row['value'],str) or not isinstance(row['object_id'],int):
            raise ToiError('Invalid translation value/object ID')
        if 'occurrence_values' in row and (not all(isinstance(v,str) for v in row['occurrence_values']) or row['value']!=row['occurrence_values'][0]):
            raise ToiError('Duplicate-ID value must match its first occurrence')
    inventory=load_json(root/'localization/inventory/items.json')
    if len(inventory)!=lock['inventory_items'] or content_revision([{k:v for k,v in r.items() if k not in ('title','info')} for r in inventory])!=lock['inventory_bindings_sha256']:
        raise ToiError('Inventory target bindings changed')
    if not all(isinstance(r['title'],str) and isinstance(r['info'],str) for r in inventory):
        raise ToiError('Inventory translation must contain strings')
    images=load_json(root/'assets/images/index.json')
    if len(images)!=lock['editable_images']:
        raise ToiError('Image source count changed')
    if not edited:
        for row in images:
            for field in ('pixels','mask'):
                if sha256_file(safe_path(root,row[field]))!=row[field+'_sha256']:
                    raise ToiError('Image source changed; use --edited')
    recipes=load_json(root/'patch/recipes.json')
    if recipes['format']!='toi-l10n/public-recipes-v1':raise ToiError('Invalid recipes format')
    changes={r['path']:r for r in manifest['changes']}
    if len(recipes['entries'])!=len(changes) or {r['path'] for r in recipes['entries']}!=set(changes):
        raise ToiError('Recipe/manifest resource sets differ')
    for row in recipes['entries']:
        for key,value in changes[row['path']].items():
            if row[key]!=value:raise ToiError('Recipe/manifest target differs')
        items=row.get('members',[row])
        for item in items:
            if item.get('delta') and sha256_file(safe_path(root,item['delta']))!=item['delta_sha256']:
                raise ToiError('Delta fingerprint mismatch')
    return manifest,lock,recipes['entries'],rows,inventory,images


def replace_translations(raw: bytes, rows: list[dict]) -> tuple[bytes, bool]:
    env=UnityPy.load(raw)
    by_id={o.path_id:o for o in env.objects}
    by_object=defaultdict(list)
    for row in rows:by_object[row['object_id']].append(row)
    changed=False
    for object_id,group in by_object.items():
        if object_id not in by_id or by_id[object_id].type.name!='TextAsset':raise ToiError('Translation TextAsset not found')
        obj=by_id[object_id]
        text=obj.read().m_Script
        kind=group[0]['kind']
        if {r['kind'] for r in group}!={kind}:raise ToiError('Mixed translation kinds')
        doc=parse_script_document(text) if kind=='script' else parse_managed_document(text)
        replacements={}
        special=[]
        for row in group:
            if row['record_id'] not in doc.records:raise ToiError('Translation record not found')
            occurrences=doc.occurrences[row['record_id']]
            expected=row.get('occurrence_values',[row['value']]*len(occurrences))
            if len(expected)!=len(occurrences):raise ToiError('Duplicate-ID occurrence count changed')
            current=[x.target if kind=='script' else x.value for x in occurrences]
            if current!=expected:
                if kind=='script' and len(set(expected))>1:
                    for record,value in zip(occurrences,expected):
                        if any(line.startswith(('# ',';')) for line in value.splitlines()):raise ToiError('Translation conflicts with script syntax')
                        special.append((record.target_start,record.target_end,value.split('\n')))
                else:
                    new=expected[0]
                    if row['record_id'] in replacements and replacements[row['record_id']]!=new:raise ToiError('Conflicting translations')
                    replacements[row['record_id']]=new
        if replacements or special:
            if special and replacements:
                # Apply ordinary edits as spans too, to retain original offsets.
                for key,value in replacements.items():
                    if any(line.startswith(('# ',';')) for line in value.splitlines()):raise ToiError('Translation conflicts with script syntax')
                    for record in doc.occurrences[key]:special.append((record.target_start,record.target_end,value.split('\n')))
                replacements={}
            if special:
                lines=list(doc.lines)
                for start,end,values in sorted(special,reverse=True):lines[start:end]=values
                updated=doc.newline.join(lines)+(doc.newline if doc.final_newline else '')
            else:updated=doc.replace(replacements)
            data=obj.read();data.m_Script=updated;data.save()
            changed=True
    return (env.file.save(packer='original') if changed else raw),changed


def edit_inventory(raw: bytes, rows: list[dict]) -> tuple[bytes,bool]:
    env=UnityPy.load(raw);by_id={o.path_id:o for o in env.objects};changed=False
    for row in rows:
        obj=by_id[row['object_id']];tree=obj.read_typetree()
        if tree['DisplayNameJa']!=row['title'] or tree['InfoJa']!=row['info']:
            tree['DisplayNameJa'],tree['InfoJa']=row['title'],row['info'];obj.save_typetree(tree);changed=True
    return (env.file.save(packer='original') if changed else raw),changed


def edit_images(raw: bytes, rows: list[dict], game: Path, base_hashes: dict, root: Path):
    env=None
    for row in rows:
        pixel_path,mask_path=(safe_path(root,row[k]) for k in ('pixels','mask'))
        if sha256_file(pixel_path)==row['pixels_sha256'] and sha256_file(mask_path)==row['mask_sha256']:continue
        if env is None:env=UnityPy.load(raw)
        source_env=UnityPy.load(original_file(game,row['base_path'],base_hashes[row['base_path']]).read_bytes())
        original=next(o for o in source_env.objects if o.path_id==row['object_id']).read().image.convert('RGBA')
        if list(original.size)!=row['size'] or sha256_bytes(original.tobytes())!=row['source_pixels_sha256']:raise ToiError('Original image mismatch')
        with Image.open(pixel_path) as image:pixels=image.convert('RGBA')
        with Image.open(mask_path) as image:mask=image.convert('L')
        x,y,right,bottom=row['box'];size=(right-x,bottom-y)
        if pixels.size!=size or mask.size!=size or set(mask.getdata())-{0,255}:raise ToiError('Image patch requires a binary mask and its original rectangle')
        original.paste(pixels,(x,y),mask)
        obj=next(o for o in env.objects if o.path_id==row['object_id']);texture=obj.read()
        type(texture).image.fset(texture,original,4);texture.save()
    return (env.file.save(packer='original') if env is not None else raw),env is not None


def bundle_metadata(raw):
    env=UnityPy.load(raw)
    data=b''.join(member.reader.bytes if hasattr(member,'reader') else member.bytes for member in env.file.files.values())
    return {'crc':zlib.crc32(data)&0xffffffff,'hash128':hashlib.md5(raw,usedforsecurity=False).hexdigest(),'size':len(raw)}


def build(game: Path, output: Path, root: Path=ROOT, edited: bool=False, executable: str='xdelta3', inputs: Path | None=None):
    game=game.resolve();output=clean_output(output,game)
    if inputs is None:raise ToiError('A local --inputs directory is required')
    inputs=inputs.resolve()
    if output.resolve().is_relative_to(inputs) or inputs.is_relative_to(output.resolve()):
        raise ToiError('Build output must be separate from private inputs')
    manifest,lock,recipes,text_rows,inventory,images=read_sources(inputs,edited)
    version=load_json(root/'patch/build-reference.json')['version']
    audit_windows_paths(compact_manifest(manifest,version))
    by_text,by_inventory,by_images=(defaultdict(list) for _ in range(3))
    for rows,group in [(text_rows,by_text),(inventory,by_inventory),(images,by_images)]:
        for row in rows:group[row['bundle']].append(row)
    hashes={r['path']:r['sha256'] for r in manifest['game_files']}
    baseline_changes={r['path']:r for r in manifest['changes']}
    # Confirm the entire supported game revision before doing any output work.
    for row in manifest['game_files']:
        original_file(game,row['path'],row['sha256'])
    changed_metadata={};actual_changes=[];mutations=0
    with staged_output_directory(output) as staging:
        patch=staging/'KoreanPatch'
        for number,row in enumerate(recipes,1):
            raw=reconstruct(row,game,inputs,executable)
            modified=False
            if row['path'] in by_text:
                raw,modified=replace_translations(raw,by_text[row['path']])
            if row['path'] in by_inventory:
                raw,did=edit_inventory(raw,by_inventory[row['path']]);modified|=did
            if row['path'] in by_images:
                raw,did=edit_images(raw,by_images[row['path']],game,hashes,inputs);modified|=did
            if modified:
                if not edited:raise ToiError('Frozen translation differs from the v7 payload')
                mutations+=1
                changed_metadata[row['path'].removeprefix(AA)]=bundle_metadata(raw)
            destination=safe_path(patch/'payload',compact_name(row['path']));destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(raw)
            # Preserve the frozen manifest's field order for byte-identical ZIPs.
            actual_changes.append({**baseline_changes[row['path']], 'output_sha256':sha256_bytes(raw),'output_size':len(raw)})
            if number%80==0:print(f'Built {number}/{len(recipes)} resources',flush=True)
        if changed_metadata:
            catalog_path=safe_path(patch/'payload',compact_name(CATALOG))
            catalog=AddressablesCatalog(load_json(catalog_path)).patched_bundle_metadata(changed_metadata)
            raw=json.dumps(catalog,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
            catalog_path.write_bytes(raw)
            for row in actual_changes:
                if row['path']==CATALOG:row['output_sha256'],row['output_size']=sha256_bytes(raw),len(raw)
        result=compact_manifest({**manifest,'changes':actual_changes},version)
        if mutations:
            result['name']='Trials of Innocence Korean patch (source edit)'
            result['public_source_revision']=content_revision({'text':text_rows,'inventory':inventory,
                'images':[{k:sha256_file(safe_path(inputs,r[k])) for k in ('pixels','mask')} for r in images]})
            result['patch_id']=content_revision({k:v for k,v in result.items() if k!='patch_id'})
        write_json(patch/'patch-manifest.json',result)
        (patch/'patch-manifest.sha256').write_text(sha256_file(patch/'patch-manifest.json')+'\n',encoding='ascii')
        for name in ['install.cmd','restore.cmd','patch.ps1','README.ko.txt']:
            shutil.copyfile(root/'installer'/name,patch/name)
        if mutations:
            with (patch/'README.ko.txt').open('a',encoding='utf-8') as stream:
                stream.write('\n공개 소스에서 수정한 개발 빌드입니다. v7 승인과 별개로 변경 대사를 검수하십시오.\n')
        (patch/'licenses').mkdir()
        for name in ['OFL-1.1.txt','FONT-NOTICE.txt']:shutil.copyfile(root/'licenses'/name,patch/'licenses'/name)
    return {'folder':str(output/'KoreanPatch'),'patch_id':result['patch_id'],'payload_files':len(actual_changes),'edited_bundles':mutations}


def package(folder: Path, archive: Path):
    if archive.exists() or archive.is_symlink():raise ToiError('Archive already exists')
    manifest=load_json(folder/'patch-manifest.json')
    if content_revision({k:v for k,v in manifest.items() if k!='patch_id'})!=manifest['patch_id']:raise ToiError('Invalid output manifest')
    for row in manifest['changes']:
        path=safe_path(folder/'payload',payload_relative(row))
        if sha256_file(path)!=row['output_sha256'] or path.stat().st_size!=row['output_size']:raise ToiError('Cannot package an invalid payload')
    files=[p for p in folder.rglob('*') if p.is_file()]
    if len(files)!=len(manifest['changes'])+8 or any(p.is_symlink() for p in folder.rglob('*')):raise ToiError('Unexpected package files')
    archive.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as target:
        for path in sorted(files):
            info=zipfile.ZipInfo('KoreanPatch/'+path.relative_to(folder).as_posix(),date_time=(2026,9,29,0,0,0))
            info.compress_type=zipfile.ZIP_STORED;info.external_attr=0o100644<<16
            target.writestr(info,path.read_bytes())
    digest=sha256_file(archive)
    archive.with_name(archive.name+'.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    return {'zip_sha256':digest,'entries':len(files)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True,help='Local private translation/image/recipe inputs')
    parser.add_argument('--edited',action='store_true');parser.add_argument('--xdelta',default='xdelta3')
    parser.add_argument('--zip',type=Path)
    args=parser.parse_args()
    try:
        if args.zip:
            clean_output(args.zip,args.game)
            if args.zip.resolve().is_relative_to(args.output.resolve()):
                raise ToiError('Archive must be outside the build output directory')
        result=build(args.game,args.output,edited=args.edited,executable=args.xdelta,inputs=args.inputs)
        if args.zip:result.update(package(args.output/'KoreanPatch',args.zip))
        print(json.dumps(result,ensure_ascii=False));return 0
    except (ToiError,OSError,ValueError,KeyError,TypeError) as exc:
        print(f'Build failed: {exc}',file=__import__('sys').stderr);return 1
if __name__=='__main__':raise SystemExit(main())
