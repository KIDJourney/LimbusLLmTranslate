import argparse
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io
import zipfile

spec = importlib.util.spec_from_file_location('localization', Path(__file__).parents[1] / 'scripts/localization.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class LocalizationTests(unittest.TestCase):
    def test_cdn_normalizes_prefix_without_flattening(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('LocalizeTemp_kr/a/KR_one.json', '{"dataList": []}')
            z.writestr('LocalizeTemp_kr/b/KR_one.json', '{"dataList": []}')
        self.assertEqual(set(m.raw_members(buffer.getvalue())), {'a/one.json', 'b/one.json'})

    def test_cdn_rejects_duplicates_and_invalid_json(self):
        for names in [['LocalizeTemp_kr/KR_a.json', 'LocalizeTemp_kr/KR_A.json'], ['LocalizeTemp_kr/../KR_a.json']]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as z:
                for name in names: z.writestr(name, '{}')
            with self.assertRaises(ValueError): m.raw_members(buffer.getvalue())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z: z.writestr('LocalizeTemp_kr/KR_a.json', 'broken')
        with self.assertRaises(ValueError): m.raw_members(buffer.getvalue())

    def test_discovery_fallback_is_explicit_and_validated(self):
        release = {'html_url': 'https://github.com/example/release', 'body': '<!-- lcta-auto-update:{"raw_token":"l20260924_example"} -->'}
        with patch.object(m, 'get', side_effect=[OSError('403'), json.dumps(release).encode()]):
            token, source, warning = m.discover_raw()
        self.assertEqual(token, 'l20260924_example')
        self.assertIn('unavailable', warning)
        with patch.object(m, 'get', return_value=b'{"latest_token":{"token":"../bad"}}'):
            with self.assertRaises(ValueError): m.discover_raw()

    def test_new_schema_fields_and_unknown_fields_are_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = {'dataList': [{'id': 1, 'flavor': '문장', 'speaker': '사람',
                'goalDescription8': '목표', 'displayName': '이름', 'result': ['결과'],
                'futureField': '미래', 'model': '모델'}]}
            m.write(root/'KR/a.json', source)
            m.write(root/'CN/a.json', {'dataList': [{'id': 1, 'result': ['结果']}]})
            m.diff(argparse.Namespace(source=root/'KR', chinese=root/'CN', previous_source=None, output=root/'d.json'))
            result = m.read(root/'d.json')
            self.assertEqual(len(result['pending']), 4)
            self.assertEqual([x['reason'] for x in result['review']], ['unclassified_field_review'])
            self.assertEqual(m.text_field(('dataList', 0, 'result', 0)), 'result')

    def test_reordered_ids_and_nested_lists(self):
        source = {'dataList': [{'id': 1, 'name': '하나'}, {'id': 2, 'levels': [{'desc': '둘'}]}]}
        target = {'dataList': [{'id': 2, 'levels': [{'desc': '二'}]}, {'id': 1, 'name': '一'}]}
        paths = dict(m.leaves(source))
        self.assertEqual(set(paths), set(dict(m.leaves(target))))
        p = next(p for p, v in paths.items() if v == '둘')
        m.set_translation(target, source, p, '两个')
        self.assertEqual(target['dataList'][0]['levels'][0]['desc'], '两个')
        self.assertEqual(target['dataList'][1]['name'], '一')

    def test_duplicate_and_typed_ids_are_distinct(self):
        data = [{'id': -1, 'content': '一'}, {'id': -1, 'content': '二'}, {'id': '1', 'content': '三'}, {'id': 1, 'content': '四'}]
        paths = [p for p, v in m.leaves(data) if p[-1] == 'content']
        self.assertEqual(len(set(paths)), 4)
        for p, expected in zip(paths, ['一', '二', '三', '四']):
            obj = data
            for step in p: obj = m.child(obj, step)
            self.assertEqual(obj, expected)

    def test_missing_record_preserves_structure_and_metadata(self):
        source = {'version': 2, 'dataList': [{'id': 1, 'model': '늑대', 'name': '이름'}, {'id': 2, 'name': '둘', 'power': 9}]}
        target = {'version': 3, 'dataList': [{'id': 1, 'model': '늑대', 'name': '已有翻译'}]}
        path = next(p for p, v in m.leaves(source) if v == '둘')
        m.set_translation(target, source, path, '二')
        self.assertEqual(target['version'], 3)
        self.assertEqual(target['dataList'][0]['name'], '已有翻译')
        self.assertEqual(target['dataList'][1], {'id': 2, 'name': '二', 'power': 9})

    def test_tokens_and_dialogue_brackets(self):
        m.validate_translation('<color=red>{0} [Burn]</color>\n<대사>', '<color=red>{0} [Burn]</color>\n<台词>')
        for bad in ['<color=red>{1} [Burn]</color>\n<台词>', '<color=red>{0} [Burn]</color><台词>', '한국어']:
            with self.assertRaises(ValueError): m.validate_translation('<color=red>{0} [Burn]</color>\n<대사>', bad)

    def test_unsafe_archive_paths(self):
        for name in ['../file', '/file', 'a/../../file', 'C:/file', 'a\\file']:
            with self.assertRaises(ValueError): m.safe_relative(name)

    def test_array_length_mismatch_requires_review(self):
        source = {'dataList': [{'id': 1, 'coins': [{'desc': '하나'}, {'desc': '둘'}]}]}
        target = {'dataList': [{'id': 1, 'coins': [{'desc': ''}]}]}
        path = next(p for p, v in m.leaves(source) if v == '둘')
        self.assertTrue(m.array_shape_changed(source, target, path))

    def test_build_checks_original_snapshot_for_multiple_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            m.write(root/'KR/a.json', {'dataList': [{'id': 1, 'name': '하나', 'desc': '둘'}]})
            m.write(root/'CN/a.json', {'dataList': []})
            m.diff(argparse.Namespace(source=root/'KR', chinese=root/'CN', previous_source=None, output=root/'diff.json'))
            rows = [dict(x, translation='译文') for x in m.read(root/'diff.json')['pending']]
            m.write(root/'t.json', rows)
            m.build(argparse.Namespace(manifest=root/'diff.json', translations=root/'t.json', output=root/'build'))
            self.assertEqual(m.read(root/'build/a.json')['dataList'], [{'id': 1, 'name': '译文', 'desc': '译文'}])
            m.write(root/'CN/a.json', {'dataList': [{'id': 1, 'name': '用户改动'}]})
            with self.assertRaisesRegex(ValueError, 'Chinese snapshot changed'):
                m.build(argparse.Namespace(manifest=root/'diff.json', translations=root/'t.json', output=root/'new'))

    def test_diff_and_build_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); src = root/'KR'; cn = root/'CN'
            m.write(src/'a/one.json', {'dataList': [{'id': 1, 'name': '이름', 'model': '늑대'}, {'id': 2, 'name': '둘'}]})
            m.write(cn/'a/one.json', {'dataList': [{'id': 1, 'name': '名字', 'model': '늑대'}]})
            m.write(src/'b/one.json', {'dataList': [{'id': 1, 'name': '하나'}]})
            m.write(cn/'b/one.json', {'dataList': [{'id': 1, 'name': ''}]})
            m.diff(argparse.Namespace(source=src, chinese=cn, previous_source=None, output=root/'diff.json'))
            report = m.read(root/'diff.json')
            self.assertEqual(report['summary']['pending_fields'], 1)
            self.assertEqual(report['summary']['review_fields'], 1)
            rows = [dict(report['pending'][0], translation='二')]
            m.write(root/'translations.json', rows)
            args = argparse.Namespace(manifest=root/'diff.json', translations=root/'translations.json', output=root/'build')
            m.build(args)
            self.assertEqual(m.read(root/'build/a/one.json')['dataList'][1]['name'], '二')
            self.assertEqual((cn/'a/one.json').read_bytes(), (root/'CN/a/one.json').read_bytes())
            self.assertEqual(m.read(cn/'a/one.json')['dataList'], [{'id': 1, 'name': '名字', 'model': '늑대'}])
            with self.assertRaises(ValueError): m.build(args)
            m.write(root/'translations.json', [])
            args.output = root/'incomplete'
            with self.assertRaises(ValueError): m.build(args)
            self.assertFalse(args.output.exists())

    def test_diff_does_not_overwrite_terminology_or_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            m.write(root/'KR/story.json', {'dataList': [{'id': 1, 'content': '하나 협회'}, {'id': -1, 'content': '소리'}, {'id': 2, 'content': '(더미)'}]})
            m.write(root/'CN/story.json', {'dataList': [{'id': 1, 'content': '하나协会'}, {'id': -1, 'content': '소리'}, {'id': 2, 'content': '(더미)'}]})
            m.diff(argparse.Namespace(source=root/'KR',chinese=root/'CN',previous_source=None,output=root/'d.json'))
            r=m.read(root/'d.json'); self.assertEqual(r['pending'], []); self.assertEqual(len(r['review']), 3)

if __name__ == '__main__': unittest.main()
