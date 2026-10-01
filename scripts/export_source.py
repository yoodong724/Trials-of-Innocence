#!/usr/bin/env python3
"""Export verified local patch inputs into a new public source directory.

The export never copies original game files or complete patched bundles.
--mapping and --added-bases are metadata inputs from the local authoring workspace.
"""
from __future__ import annotations
import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path
from patch_layout import payload_relative
import UnityPy
from PIL import Image, ImageChops
from source_patch import (ROOT, AA, bundle_header, encode_delta, member_bytes, original_file,
                         pack_members, safe_path, write_json)
from toi_common import ToiError, content_revision, load_json, sha256_bytes, sha256_file
from toi_adapter import parse_managed_document, parse_script_document


def export(game: Path, patch: Path, mapping: Path, added_bases: Path, output: Path, executable: str) -> dict:
    if output.resolve().is_relative_to(ROOT):
        raise ToiError('Private authoring inputs must be exported outside the distributed checkout')
    if output.resolve().is_relative_to(game.resolve()):
        raise ToiError('Private inputs cannot be exported into the game directory')
    manifest = load_json(patch / 'patch-manifest.json')
    if content_revision({k:v for k,v in manifest.items() if k != 'patch_id'}) != manifest['patch_id']:
        raise ToiError('Input patch manifest is not verified')
    (output / 'patch/recipes').mkdir(parents=True, exist_ok=True)
    if (output / 'patch/manifest.json').exists():
        raise ToiError('Source export already exists')
    files = {r['path']:r for r in manifest['game_files']}
    added = load_json(added_bases)
    recipes, images, inventory = [], [], []
    text_groups = defaultdict(list)
    for item in load_json(mapping)['entries']:
        text_groups[AA + item['ja_bundle']].append(item)
    if len({e['stable_key'] for g in text_groups.values() for e in g}) != 49861:
        raise ToiError('Mapping must contain exactly 49,861 unique stable keys')
    text_rows = []
    changes = {row['path']: row for row in manifest['changes']}
    for number, change in enumerate(manifest['changes'], 1):
        relative = change['path']
        target_path = safe_path(patch / 'payload', payload_relative(change))
        target = target_path.read_bytes()
        if sha256_bytes(target) != change['output_sha256'] or len(target) != change['output_size']:
            raise ToiError(f'Patch payload mismatch: {relative}')
        base_path = relative if change['operation'] == 'replace' else added[relative]
        base_hash = files[base_path]['sha256']
        if change['operation'] == 'replace' and base_hash != change['source_sha256']:
            raise ToiError('Original fingerprint mismatch')
        before = original_file(game, base_path, base_hash).read_bytes()
        recipe = {**change, 'base_path': base_path, 'base_sha256': base_hash}
        if relative.endswith('.bundle'):
            source_env, target_env = UnityPy.load(before), UnityPy.load(target)
            source_names = list(source_env.file.files)
            target_names = list(target_env.file.files)
            if len(source_names) != len(target_names):
                raise ToiError(f'Unity member count changed: {relative}')
            recipe.update(kind='unity', header=bundle_header(target_env.file), members=[])
            packed = []
            for index, (source_name, target_name) in enumerate(zip(source_names, target_names)):
                source = member_bytes(source_env.file.files[source_name])
                value = member_bytes(target_env.file.files[target_name])
                member = {'name': target_name, 'source_name': source_name,
                          'flags': target_env.file.files[target_name].flags,
                          'source_sha256': sha256_bytes(source), 'output_sha256': sha256_bytes(value),
                          'output_size':len(value)}
                if source != value:
                    delta = encode_delta(source, value, executable)
                    delta_path = f'patch/recipes/{number:04d}-{index}.vcdiff.gz'
                    safe_path(output, delta_path).write_bytes(delta)
                    member.update(delta=delta_path, delta_sha256=sha256_bytes(delta))
                recipe['members'].append(member)
                packed.append((target_name, member['flags'], value))
            if pack_members(UnityPy.load(before).file, recipe['header'], packed) != target:
                raise ToiError(f'Unity compression cannot reproduce patch: {relative}')
            target_objects = {o.path_id:o for o in target_env.objects}
            source_objects = {o.path_id:o for o in source_env.objects}
            documents = {}
            for entry in text_groups.get(relative, []):
                obj = target_objects[entry['ja_object_path_id']]
                if obj.path_id not in documents:
                    text = obj.read().m_Script
                    documents[obj.path_id] = parse_script_document(text) if entry['record_kind'] == 'script' else parse_managed_document(text)
                doc = documents[obj.path_id]
                occurrences = doc.occurrences[entry['ja_record_id']]
                values = [x.target if entry['record_kind']=='script' else x.value for x in occurrences]
                # A few preserved duplicate IDs intentionally have distinct occurrence values.
                row = {'key':entry['stable_key'], 'record_id':entry['ja_record_id'],
                       'bundle':relative, 'object_id':entry['ja_object_path_id'], 'kind':entry['record_kind'],
                       'value':values[0]}
                if len(set(values)) > 1:
                    row['occurrence_values'] = values
                text_rows.append(row)
            for obj in target_objects.values():
                if obj.type.name == 'MonoBehaviour':
                    tree = obj.read_typetree()
                    if tree.get('DisplayNameJa') and tree.get('InfoJa') and 'DisplayNameZh' in tree:
                        old = source_objects[obj.path_id].read_typetree()
                        if tree['DisplayNameJa'] != old['DisplayNameJa'] or tree['InfoJa'] != old['InfoJa']:
                            inventory.append({'bundle':relative,'object_id':obj.path_id,
                                              'title':tree['DisplayNameJa'],'info':tree['InfoJa']})
                elif obj.type.name == 'Texture2D' and obj.path_id in source_objects:
                    old_obj = source_objects[obj.path_id]
                    if obj.get_raw_data() == old_obj.get_raw_data() and all(not m.get('delta') for m in recipe['members'] if m['name'].endswith('.resS')):
                        continue
                    source_image = old_obj.read().image.convert('RGBA')
                    target_image = obj.read().image.convert('RGBA')
                    if source_image.size != target_image.size:
                        # Resized atlases and font SDF textures are covered by resource recipes.
                        continue
                    difference = ImageChops.difference(source_image, target_image)
                    mask = Image.eval(difference.getchannel('R'),lambda x:255 if x else 0)
                    for channel in 'GBA':
                        mask = ImageChops.lighter(mask,Image.eval(difference.getchannel(channel),lambda x:255 if x else 0))
                    box = mask.getbbox()
                    if box is None:
                        continue
                    image_id = f'{number:04d}-{obj.path_id}'
                    pixels = Image.new('RGBA', target_image.size)
                    pixels.paste(target_image, mask=mask)
                    image_dir = output / 'assets/images';image_dir.mkdir(parents=True,exist_ok=True)
                    pixel_path, mask_path = f'assets/images/{image_id}.png',f'assets/images/{image_id}-mask.png'
                    pixels.crop(box).save(output / pixel_path)
                    mask.crop(box).save(output / mask_path)
                    images.append({'bundle':relative,'base_path':base_path,'object_id':obj.path_id,
                                   'size':list(target_image.size),'box':list(box),
                                   'source_pixels_sha256':sha256_bytes(source_image.tobytes()),
                                   'target_pixels_sha256':sha256_bytes(target_image.tobytes()),
                                   'pixels':pixel_path,'mask':mask_path,
                                   'pixels_sha256':sha256_file(output/pixel_path),
                                   'mask_sha256':sha256_file(output/mask_path)})
        else:
            delta = encode_delta(before,target,executable)
            name=f'patch/recipes/{number:04d}.vcdiff.gz'
            safe_path(output,name).write_bytes(delta)
            recipe.update(kind='raw',delta=name,delta_sha256=sha256_bytes(delta))
        recipes.append(recipe)
        if number % 40 == 0:
            print(f'Exported {number}/{len(manifest["changes"])} resources',flush=True)
    # Capture extra managed names not represented in the original 49,861-key mapping.
    for relative, group in text_groups.items():
        if group[0]['record_kind'] != 'managed_text':continue
        env=UnityPy.load(safe_path(patch/'payload',payload_relative(changes[relative])).read_bytes())
        path_id=group[0]['ja_object_path_id']
        obj=next(o for o in env.objects if o.path_id==path_id)
        doc=parse_managed_document(obj.read().m_Script)
        represented={e['ja_record_id'] for e in group}
        for record_id,record in doc.records.items():
            if record_id not in represented:
                text_rows.append({'key':f'added/{record_id}','record_id':record_id,'bundle':relative,
                                  'object_id':path_id,'kind':'managed_text','value':record.value})
    shards=defaultdict(list)
    for row in sorted(text_rows,key=lambda r:r['key']):
        shard=row['key'].split('/',1)[0]
        shards[shard].append(row)
    text_files=[]
    for shard,rows in sorted(shards.items()):
        # Stable file names without relying on scene-name filesystem characters.
        name=f'localization/text/{len(text_files)+1:04d}.json'
        write_json(output/name,{'scene':shard,'entries':rows})
        text_files.append(name)
    write_json(output/'localization/inventory/items.json',inventory)
    write_json(output/'assets/images/index.json',images)
    write_json(output/'patch/recipes.json',{'format':'toi-l10n/public-recipes-v1','entries':recipes})
    write_json(output/'patch/manifest.json',manifest)
    for src,dst in [('README.ko.txt','installer/README.ko.txt'),('licenses/FONT-NOTICE.txt','licenses/FONT-NOTICE.txt')]:
        shutil.copyfile(patch/src,output/dst)
    files_to_bind=[*text_files,'localization/inventory/items.json','assets/images/index.json']
    snapshot={'format':'toi-l10n/public-source-v1','patch_id':manifest['patch_id'],
              'text_files':text_files,'source_sha256':{n:sha256_file(output/n) for n in files_to_bind},
              'recipes_sha256':sha256_file(output/'patch/recipes.json'),
              'text_keys':len(text_rows),'inventory_items':len(inventory),'editable_images':len(images),
              'text_bindings_sha256':content_revision([{k:v for k,v in row.items() if k not in ('value','occurrence_values')} for row in sorted(text_rows,key=lambda r:r['key'])]),
              'inventory_bindings_sha256':content_revision([{k:v for k,v in row.items() if k not in ('title','info')} for row in inventory]),
              'reference_zip_sha256':sha256_file(patch.parent.with_suffix('.zip'))}
    for subdirectory in ('installer','licenses','canonical','assets/fonts'):
        for path in sorted((output/subdirectory).rglob('*')):
            if path.is_file():snapshot['source_sha256'][path.relative_to(output).as_posix()]=sha256_file(path)
    write_json(output/'patch/source-lock.json',snapshot)
    return {k:v for k,v in snapshot.items() if k not in ('source_sha256','text_files')}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['game','patch','mapping','added-bases','output']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--xdelta',default='xdelta3')
    args=parser.parse_args()
    print(json.dumps(export(args.game,args.patch,args.mapping,args.added_bases,args.output,args.xdelta),ensure_ascii=False))
if __name__=='__main__':main()
