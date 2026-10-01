"""Unit tests for LLC Reference Translation Memory."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import scripts.localization as localization
import scripts.translation_memory as tm


class TestTranslationMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.kr_dir = self.root / "KR"
        self.cn_dir = self.root / "LLC_zh-CN"
        self.kr_dir.mkdir(parents=True)
        self.cn_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_integer_steps_and_duplicate_ids_purged(self):
        # KR data with pure integer list and duplicate id steps
        kr_file = self.kr_dir / "test_fragile.json"
        cn_file = self.cn_dir / "test_fragile.json"

        kr_content = {
            "dataList": [
                {"id": 101, "name": "단테", "desc": "첫번째 설명"},
                # duplicate id 101
                {"id": 101, "name": "단테 분신", "desc": "두번째 설명"},
                # unique id 102
                {"id": 102, "name": "파우스트", "desc": "파우스트의 설명"},
            ],
            "pureList": [
                "순수 정수 배열 항목 1",
                "순수 정수 배열 항목 2",
            ],
        }

        cn_content = {
            "dataList": [
                {"id": 101, "name": "但丁", "desc": "第一个描述"},
                {"id": 101, "name": "但丁分身", "desc": "第二个描述"},
                {"id": 102, "name": "浮士德", "desc": "浮士德的描述"},
            ],
            "pureList": [
                "纯整数列表项 1",
                "纯整数列表项 2",
            ],
        }

        localization.write(kr_file, kr_content)
        localization.write(cn_file, cn_content)

        index = tm.build_translation_memory_index(self.kr_dir, self.cn_dir)

        # 1. pure integer steps must be completely skipped
        pure_list_records = [r for r in index["records"] if "pureList" in str(r["raw_path"])]
        self.assertEqual(len(pure_list_records), 0)

        # 2. duplicate id 101 (occurrence 0 and 1) must be marked fragile and excluded from exact/by_source
        id_101_records = [r for r in index["records"] if "101" in str(r["raw_path"])]
        for r in id_101_records:
            self.assertTrue(r["fragile"], f"Record {r} with duplicate id should be fragile")

        self.assertNotIn("단테", index["by_source"])
        self.assertNotIn("첫번째 설명", index["by_source"])

        # 3. unique id 102 should be non-fragile and present
        id_102_records = [r for r in index["records"] if "102" in str(r["raw_path"])]
        self.assertTrue(len(id_102_records) >= 2)
        for r in id_102_records:
            self.assertFalse(r["fragile"])
        self.assertIn("파우스트", index["by_source"])

    def test_document_preceding_order_and_unknown_path(self):
        kr_file = self.kr_dir / "story.json"
        cn_file = self.cn_dir / "story.json"

        kr_content = {
            "dataList": [
                {"id": 1, "teller": "단테", "title": "단테의 일기", "desc": "이야기 시작"},
                {"id": 2, "teller": "파우스트", "title": "보고서", "desc": "중간 보고"},
                {"id": 3, "teller": "그레고르", "title": "회상", "desc": "과거 회상"},
                {"id": 4, "teller": "로쟈", "title": "대화", "desc": "결말 도달"},
            ]
        }
        cn_content = {
            "dataList": [
                {"id": 1, "teller": "但丁", "title": "但丁的日记", "desc": "故事开始"},
                {"id": 2, "teller": "浮士德", "title": "报告书", "desc": "阶段报告"},
                {"id": 3, "teller": "格里高尔", "title": "回想", "desc": "过往回想"},
                {"id": 4, "teller": "罗佳", "title": "对话", "desc": "抵达结局"},
            ]
        }

        localization.write(kr_file, kr_content)
        localization.write(cn_file, cn_content)

        index = tm.build_translation_memory_index(self.kr_dir, self.cn_dir)

        # Query for item at id 3 (ordinal of id 3 desc)
        # raw path is: (('id', '3', 0), 'desc') -> encoded: [['id', '3', 0], 'desc']
        target_path = [["id", 3, 0], "desc"]
        res = tm.query_item_evidence(
            index=index,
            file="story.json",
            path=target_path,
            source="과거 회상",
        )

        preceding = [ev for ev in res["evidence"] if ev["type"] == "document_preceding"]
        # Preceding entries must strictly have ordinal < id 3 desc ordinal
        for ev in preceding:
            rec = next(r for r in index["records"] if r["file"] == ev["file"] and r["path"] == ev["path"])
            target_ord = index["file_path_ordinals"]["story.json"][json.dumps(localization.encode(localization.decode(target_path)), sort_keys=True)]
            self.assertLess(rec["ordinal"], target_ord)
            self.assertNotEqual(ev["source"], "과거 회상")

        # Unknown path test: target path not in file_path_ordinals
        unknown_path = [["id", 999, 0], "desc"]
        res_unknown = tm.query_item_evidence(
            index=index,
            file="story.json",
            path=unknown_path,
            source="완전 새로운 텍스트",
        )
        preceding_unknown = [ev for ev in res_unknown["evidence"] if ev["type"] == "document_preceding"]
        # Must be empty because target_ordinal is None
        self.assertEqual(len(preceding_unknown), 0)

    def test_short_name_substring_prioritization(self):
        kr_file = self.kr_dir / "names.json"
        cn_file = self.cn_dir / "names.json"

        kr_content = {
            "dataList": [
                {"id": 1, "displayName": "이상", "desc": "캐릭터 이상"},
                {"id": 2, "shortName": "파우스트", "desc": "캐릭터 파우스트"},
                {"id": 3, "desc": "이상과 파우스트가 골목길을 걷고 있었다."},
            ]
        }
        cn_content = {
            "dataList": [
                {"id": 1, "displayName": "李箱", "desc": "角色李箱"},
                {"id": 2, "shortName": "浮士德", "desc": "角色浮士德"},
                {"id": 3, "desc": "李箱和浮士德走在小巷里。"},
            ]
        }

        localization.write(kr_file, kr_content)
        localization.write(cn_file, cn_content)

        index = tm.build_translation_memory_index(self.kr_dir, self.cn_dir)

        # Query with source sentence that contains substring '이상' and '파우스트'
        sentence = "이 문장은 이상과 파우스트를 포함합니다."
        res = tm.query_item_evidence(
            index=index,
            file="other.json",
            path=[["id", 1, 0], "desc"],
            source=sentence,
        )

        name_evidences = [ev for ev in res["evidence"] if ev["type"] == "short_name"]
        self.assertTrue(len(name_evidences) >= 2)
        name_sources = {ev["source"] for ev in name_evidences}
        self.assertIn("이상", name_sources)
        self.assertIn("파우스트", name_sources)

    def test_conflicts_and_limits(self):
        # Two files with identical source but different translations
        kr_file1 = self.kr_dir / "file1.json"
        cn_file1 = self.cn_dir / "file1.json"
        kr_file2 = self.kr_dir / "file2.json"
        cn_file2 = self.cn_dir / "file2.json"

        localization.write(kr_file1, {"dataList": [{"id": 1, "desc": "동일 원문 문장"}]})
        cn_file1_content = {"dataList": [{"id": 1, "desc": "第一种译文版本"}]}
        localization.write(cn_file1, cn_file1_content)

        localization.write(kr_file2, {"dataList": [{"id": 2, "desc": "동일 원문 문장"}]})
        cn_file2_content = {"dataList": [{"id": 2, "desc": "第二种不同译文版本"}]}
        localization.write(cn_file2, cn_file2_content)

        index = tm.build_translation_memory_index(self.kr_dir, self.cn_dir)
        self.assertIn("동일 원문 문장", index["source_conflicts"])
        self.assertEqual(len(index["source_conflicts"]["동일 원문 문장"]), 2)

        res = tm.query_item_evidence(
            index=index,
            file="file1.json",
            path=[["id", 1, 0], "desc"],
            source="동일 원문 문장",
        )
        self.assertEqual(len(res["conflicts"]), 2)

        # Character limit without slicing: create a huge record
        kr_file_huge = self.kr_dir / "huge.json"
        cn_file_huge = self.cn_dir / "huge.json"
        huge_kr = "매우 " * 300 + "긴 텍스트"
        huge_cn = "非常 " * 300 + "长文本"
        localization.write(kr_file_huge, {"dataList": [{"id": 10, "desc": huge_kr}]})
        localization.write(cn_file_huge, {"dataList": [{"id": 10, "desc": huge_cn}]})

        index_huge = tm.build_translation_memory_index(self.kr_dir, self.cn_dir)
        res_huge = tm.query_item_evidence(
            index=index_huge,
            file="huge.json",
            path=[["id", 10, 0], "desc"],
            source=huge_kr,
            max_chars=200,
        )
        # Because huge item json length > 200, it must be skipped, not silently sliced
        self.assertEqual(len(res_huge["evidence"]), 0)


if __name__ == "__main__":
    unittest.main()
