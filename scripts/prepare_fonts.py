#!/usr/bin/env python3
"""Regenerate the three Korean font byte streams using checked-in OFL sources."""
import argparse
from pathlib import Path
from io import BytesIO
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.DefaultTable import DefaultTable
from font_source import _prepare_font, _static_glyph_indices
from source_patch import AA, ROOT, baseline_manifest, original_file, write_json
from toi_common import load_json, sha256_bytes, sha256_file, ToiError, staged_output_directory

JOBS=[
 ('serif-bold',0,'sourcehanserifcn-bold-2_sdf_fe742cf67d176e5409b30f2ec005a34c.bundle','SourceHanSerifCN-Bold-2_SDF'),
 ('sans-bold',1,'sourcehansanssc-bold-2_sdf_9e93f9f518f8572f2eaf5738110b8b27.bundle','SourceHanSansSC-Bold-2_SDF'),
 ('sans-medium',2,'sourcehansanssc-medium-2_sdf_8e649b49f7827e8c180e40496cb686f8.bundle','SourceHanSansSC-Medium-2_SDF')]

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--inputs',required=True,type=Path);parser.add_argument('--game',required=True,type=Path);parser.add_argument('--output',required=True,type=Path);a=parser.parse_args()
 from source_patch import clean_output
 clean_output(a.output,a.game)
 config=load_json(ROOT/'assets/fonts/config.json')['fonts'];hashes={r['path']:r['sha256'] for r in baseline_manifest(a.inputs)['game_files']};records=[]
 with staged_output_directory(a.output) as output:
  for name,index,bundle,asset in JOBS:
   spec=config[index];font=ROOT/spec['source_path']
   if sha256_file(font)!=spec['source_sha256']:raise ToiError('Font source mismatch')
   relative=AA+'StandaloneWindows64/wuzui_assets_naninovel/fonts/'+bundle
   raw=original_file(a.game,relative,hashes[relative]).read_bytes();reserved=_static_glyph_indices(raw,asset)
   generated,record=_prepare_font(font,spec['weight'],reserved,spec['generated_family'])
   if spec.get('table_overrides'):
    font_object=TTFont(BytesIO(generated));font_object.recalcTimestamp=False
    for tag,relative in spec['table_overrides'].items():
     table=DefaultTable(tag);table.data=(ROOT/relative).read_bytes();font_object[tag]=table
    stream=BytesIO();font_object.save(stream);generated=stream.getvalue()
   print(name, sha256_bytes(generated), flush=True)
   if sha256_bytes(generated)!=spec['generated_sha256']:raise ToiError(f'Generated {name} font differs from v7 source')
   (output/(name+'.ttf')).write_bytes(generated)
   record['source_path']=spec['source_path'];record['generated_sha256']=sha256_bytes(generated);record['generated_size']=len(generated);record['table_overrides']=spec.get('table_overrides',{});records.append(record)
  write_json(output/'manifest.json',records)
 print('Verified three generated fonts against v7 hashes')
if __name__=='__main__':main()
