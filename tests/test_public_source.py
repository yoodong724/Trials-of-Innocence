import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from source_patch import safe_path,clean_output,encode_delta,decode_delta,ROOT
from toi_common import ToiError
from build import read_sources,text_bindings
from verify import verify,verify_distribution_tree

class PublicSourceTests(unittest.TestCase):
 def test_distribution_excludes_private_story_inputs(self):
  result=verify_distribution_tree()
  self.assertEqual(result['mode'],'tools_only')
  self.assertFalse(result['private_inputs_in_git'])
  with tempfile.TemporaryDirectory() as directory:
   with self.assertRaisesRegex(ToiError,'Translation inputs'):
    read_sources(root=Path(directory))
 def test_distribution_rejects_an_accidental_translation_directory(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'localization').mkdir()
   with self.assertRaisesRegex(ToiError,'Private content'):
    verify_distribution_tree(root)
 def test_built_patch_verification_requires_private_inputs(self):
  with self.assertRaisesRegex(ToiError,'--inputs is required'):
   verify(folder=Path('/nonexistent-built-patch'))
 def test_reject_traversal_and_cross_platform_absolute_paths(self):
  with tempfile.TemporaryDirectory() as directory:
   for name in ['../escape','a/../b','/absolute','C:/Windows','a\\b','a//b','.','']:
    with self.subTest(name=name),self.assertRaises(ToiError):safe_path(Path(directory),name)
 def test_reject_symlink_in_source_path(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'link').symlink_to('/tmp',target_is_directory=True)
   with self.assertRaises(ToiError):safe_path(root,'link/file')
 def test_output_cannot_overlap_game(self):
  with tempfile.TemporaryDirectory() as directory:
   game=Path(directory)/'game';game.mkdir()
   for output in [game,game/'patch',game.parent]:
    with self.subTest(output=output),self.assertRaises(ToiError):clean_output(output,game)
 def test_output_cannot_replace_source(self):
  with self.assertRaises(ToiError):clean_output(ROOT,Path('/tmp/nonexistent-game'))
 def test_delta_roundtrip_handles_insert_remove_and_binary_pixels(self):
  before=(b'unchanged source\x00'*300)+bytes(range(256))*40
  after=b'header'+before[100:900]+b'\x00\xff\x80 translated payload'+before[1100:]+b'end'
  delta=encode_delta(before,after)
  self.assertEqual(decode_delta(before,delta),after)
  self.assertLess(len(delta),len(after)//3)
 def test_delta_has_no_local_filename_header(self):
  import gzip
  raw=gzip.decompress(encode_delta(b'old'*300,b'new'+b'old'*299))
  self.assertEqual(raw[:3],b'\xd6\xc3\xc4')
  self.assertEqual(raw[4]&4,0)
 def test_binding_hash_accepts_translation_edit_but_rejects_target_edit(self):
  row={'key':'scene/id','bundle':'data.bundle','object_id':1,'record_id':'id','kind':'script','value':'원문'}
  self.assertEqual(text_bindings([row]),text_bindings([{**row,'value':'수정'}]))
  self.assertNotEqual(text_bindings([row]),text_bindings([{**row,'object_id':2}]))
 def test_existing_output_not_overwritten(self):
  with tempfile.TemporaryDirectory() as directory:
   output=Path(directory)/'existing';output.mkdir()
   with self.assertRaises(ToiError):clean_output(output,Path(directory)/'game')
if __name__=='__main__':unittest.main()
