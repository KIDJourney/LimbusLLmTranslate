#!/usr/bin/env python3
"""Unit tests for final targeted terminology pass."""

import hashlib
import json
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("final_terminology_pass", str(ROOT / ".dev-workflow/final-terminology-pass.py"))
ftp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ftp)


class TestFinalTerminologyPass(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.run_dir = Path(self.temp_dir) / "run_dir"
        self.run_dir.mkdir(parents=True)

        self.evidence_data = {
            "원레그": {
                "top_name_matches": [
                    {"source": "원레그", "current": "单脚人", "file": "Enemies-a1c10p1.json"}
                ]
            },
            "간수": {
                "top_name_matches": [
                    {"source": "간수", "current": "看守", "file": "RPGSystem/rpg-loc-npc-route-a-warden.json"}
                ]
            }
        }
        self.rev_agent_dir = self.run_dir / "review_agent"
        self.rev_agent_dir.mkdir()
        (self.rev_agent_dir / "llc-terminology-evidence.json").write_text(
            json.dumps(self.evidence_data, ensure_ascii=False)
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_extract_targeted_candidates(self):
        reviewed = [
            {"file": "f1.json", "path": ["a"], "source": "원레그 강화", "translation": "独腿强化"},
            {"file": "f2.json", "path": ["b"], "source": "원레그 돌진", "translation": "单腿人冲撞"},
            {"file": "f3.json", "path": ["c"], "source": "원레그 점프", "translation": "单脚人跳跃"}, # standard, no match
            {"file": "f4.json", "path": ["d"], "source": "간수 처치", "translation": "击败狱卒"},
            {"file": "f5.json", "path": ["e"], "source": "간수 등장", "translation": "看守登场"}, # standard, no match
            {"file": "f6.json", "path": ["f"], "source": "다른 텍스트", "translation": "其他内容"},
        ]
        candidates = ftp.extract_targeted_candidates(reviewed, self.evidence_data)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["index"], 0)
        self.assertEqual(candidates[0]["llc_reference"]["standard_term"], "单脚人")
        self.assertEqual(candidates[1]["index"], 1)
        self.assertEqual(candidates[2]["index"], 3)
        self.assertEqual(candidates[2]["llc_reference"]["standard_term"], "看守")

    def test_run_targeted_pass_empty_candidates(self):
        reviewed = [
            {"file": "f1.json", "path": ["a"], "source": "원레그", "translation": "单脚人"},
            {"file": "f2.json", "path": ["b"], "source": "간수", "translation": "看守"},
        ]
        diff_file = self.run_dir / "diff.json"
        trans_file = self.run_dir / "translations.json"
        draft_file = self.run_dir / "draft_translations.json"
        reviewed_file = self.run_dir / "reviewed-translations.json"
        registry_file = self.run_dir / "reviewer_registry.json"

        diff_file.write_text("[]")
        trans_file.write_text("[]")
        draft_file.write_text("[]")
        reviewed_file.write_text(json.dumps(reviewed))
        registry_file.write_text(json.dumps({
            "run_dir": str(self.run_dir.resolve()),
            "agent_name": "rev_agent",
            "pane_id": "pane_1"
        }))

        mock_driver = MagicMock()
        mock_driver.get_agent_info.return_value = {"pane_id": "pane_1"}
        mock_driver.get_agent_status.return_value = "idle"

        ret = ftp.run_targeted_terminology_pass(
            run_dir=self.run_dir,
            driver=mock_driver,
        )
        self.assertEqual(ret, 0)
        receipt_file = self.run_dir / "term_check_receipt.json"
        self.assertTrue(receipt_file.is_file())
        receipt = json.loads(receipt_file.read_text())
        self.assertEqual(receipt["candidates_found"], 0)


if __name__ == "__main__":
    unittest.main()
