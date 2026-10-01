"""Unit tests for key identification in scripts/localization.py (stdlib only).

Verifies stable key-based matching for RPG dataList structures:
- Key reordering
- Addition and deletion of records
- Duplicate keys with occurrence tracking
- Typed keys (int vs str)
- Preservation of existing id prioritization
"""
from __future__ import annotations

import collections
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("localization", Path(__file__).parents[1] / "scripts/localization.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class WindowsKeysTests(unittest.TestCase):
    def test_id_priority_over_key(self):
        # When both id and key are present, id takes precedence
        data = [
            {"id": 1, "key": "k1", "desc": "일"},
            {"id": 2, "key": "k2", "desc": "이"},
        ]
        paths = [p for p, text in m.leaves(data) if p[-1] == "desc"]
        self.assertEqual(len(paths), 2)
        p0 = paths[0]
        self.assertEqual(p0[0][0], "id")
        self.assertEqual(p0[0][1], "1")

    def test_key_reordering(self):
        source = {
            "dataList": [
                {"key": "D-100", "texts": [{"index": 0, "text": "하나"}]},
                {"key": "D-200", "texts": [{"index": 0, "text": "둘"}]},
            ]
        }
        target = {
            "dataList": [
                {"key": "D-200", "texts": [{"index": 0, "text": "二"}]},
                {"key": "D-100", "texts": [{"index": 0, "text": "一"}]},
            ]
        }
        src_leaves = dict(m.leaves(source))
        tgt_leaves = dict(m.leaves(target))
        self.assertEqual(set(src_leaves.keys()), set(tgt_leaves.keys()))

        # Update translation in target
        target_copy = {
            "dataList": [
                {"key": "D-200", "texts": [{"index": 0, "text": "二"}]},
                {"key": "D-100", "texts": [{"index": 0, "text": "旧一"}]},
            ]
        }
        p_d100 = next(p for p, v in src_leaves.items() if v == "하나")
        m.set_translation(target_copy, source, p_d100, "新一")
        self.assertEqual(target_copy["dataList"][1]["texts"][0]["text"], "新一")
        self.assertEqual(target_copy["dataList"][0]["texts"][0]["text"], "二")

    def test_key_addition_and_deletion(self):
        source = {
            "dataList": [
                {"key": "k1", "text": "하나"},
                {"key": "k2", "text": "둘"},
            ]
        }
        target = {
            "dataList": [
                {"key": "k1", "text": "一"},
            ]
        }
        src_leaves = dict(m.leaves(source))
        p_k2 = next(p for p, v in src_leaves.items() if v == "둘")

        # set_translation should append missing k2 record preserving structure
        m.set_translation(target, source, p_k2, "二")
        self.assertEqual(len(target["dataList"]), 2)
        self.assertEqual(target["dataList"][1]["key"], "k2")
        self.assertEqual(target["dataList"][1]["text"], "二")

    def test_duplicate_and_typed_keys(self):
        # Distinct int vs str keys, plus duplicate keys with occurrence
        data = [
            {"key": 100, "content": "num_first"},
            {"key": "100", "content": "str_100"},
            {"key": 100, "content": "num_second"},
        ]
        paths = [p for p, v in m.leaves(data) if p[-1] == "content"]
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(set(paths)), 3)

        self.assertEqual(paths[0][0], ("key", "100", 0))
        self.assertEqual(paths[1][0], ("key", '"100"', 0))
        self.assertEqual(paths[2][0], ("key", "100", 1))

        # Test child lookup resolves accurately
        for p, expected in zip(paths, ["num_first", "str_100", "num_second"]):
            node = data
            for step in p:
                node = m.child(node, step)
            self.assertEqual(node, expected)


if __name__ == "__main__":
    unittest.main()
