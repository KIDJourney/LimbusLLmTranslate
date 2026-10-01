#!/usr/bin/env python3
"""Tests for incremental review orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import scripts.incremental_review as inc_rv
import scripts.translation_pipeline as tp


class TestIncrementalReview(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

        self.baseline_dir = self.base_dir / "baseline"
        self.baseline_dir.mkdir(parents=True, exist_ok=True)

        self.current_dir = self.base_dir / "current"
        self.current_dir.mkdir(parents=True, exist_ok=True)

        self._setup_mock_directories()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_mock_directories(self, tamper_llc: bool = False):
        # 1. Setup LLC snapshots
        b_llc = self.baseline_dir / "snapshot/llc/LLC_zh-CN"
        b_llc.mkdir(parents=True, exist_ok=True)
        (b_llc / "test.json").write_text('{"hello": "world"}', encoding="utf-8")

        c_llc = self.current_dir / "snapshot/llc/LLC_zh-CN"
        c_llc.mkdir(parents=True, exist_ok=True)
        if tamper_llc:
            (c_llc / "test.json").write_text('{"hello": "tampered"}', encoding="utf-8")
        else:
            (c_llc / "test.json").write_text('{"hello": "world"}', encoding="utf-8")

        # 2. Setup baseline diff, translations, reviewed-translations, review.json, semantic-review-receipt
        base_pending = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과"},
            {"file": "skillA.json", "path": ["data", 0, "desc"], "source": "사과를 먹는다"},
            {"file": "skillB.json", "path": ["data", 1, "name"], "source": "이 낫으로 답하나이다!"},
            {"file": "skillB.json", "path": ["data", 1, "desc"], "source": "스킬'이 낫으로 답하나이다!'로 공격"},
        ]
        base_review = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과", "reason": "term_check"},
        ]
        inc_rv.safe_write_json(self.baseline_dir / "diff.json", {"pending": base_pending, "review": base_review})

        base_trans = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과", "translation": "苹果"},
            {"file": "skillA.json", "path": ["data", 0, "desc"], "source": "사과를 먹는다", "translation": "吃苹果"},
            {"file": "skillB.json", "path": ["data", 1, "name"], "source": "이 낫으로 답하나이다!", "translation": "以此镰作答！"},
            {"file": "skillB.json", "path": ["data", 1, "desc"], "source": "스킬'이 낫으로 답하나이다!'로 공격", "translation": "以技能“以此镰作答！”发动攻击"},
        ]
        inc_rv.safe_write_json(self.baseline_dir / "translations.json", base_trans)

        base_reviewed = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과", "translation": "苹果"},
            {"file": "skillA.json", "path": ["data", 0, "desc"], "source": "사과를 먹는다", "translation": "吃苹果"},
            {"file": "skillB.json", "path": ["data", 1, "name"], "source": "이 낫으로 답하나이다!", "translation": "以此镰作答！"},
            {"file": "skillB.json", "path": ["data", 1, "desc"], "source": "스킬'이 낫으로 답하나이다!'로 공격", "translation": "以技能“以此镰作答！”发动攻击"},
        ]
        inc_rv.safe_write_json(self.baseline_dir / "reviewed-translations.json", base_reviewed)
        base_reviewed_sha = inc_rv.file_sha256(self.baseline_dir / "reviewed-translations.json")

        base_review_json = {
            "hashes": {
                "diff.json": inc_rv.file_sha256(self.baseline_dir / "diff.json"),
                "translations.json": inc_rv.file_sha256(self.baseline_dir / "translations.json"),
                "reviewed-translations.json": base_reviewed_sha,
            },
            "dispositions": [
                {
                    "file": "skillA.json",
                    "path": ["data", 0, "name"],
                    "source": "사과",
                    "reason": "term_check",
                    "action": "accept",
                    "resolved": True,
                    "resolution_note": "符合LLC规范",
                }
            ],
        }
        inc_rv.safe_write_json(self.baseline_dir / "review.json", base_review_json)

        sr_receipt = {
            "status": "success",
            "reviewed_translations_sha": base_reviewed_sha,
        }
        inc_rv.safe_write_json(self.baseline_dir / "semantic-review-receipt.json", sr_receipt)

        # 3. Setup current run
        # Item 0: same (사과 -> 苹果) => reusable
        # Item 1: changed translation (사과를 먹는다 -> 吃红苹果) => delta!
        # Item 2: skill name definition "이 낫으로 답하나이다!"
        # Item 3: skill reference "스킬'이 낫으로 답하나이다!'로 공격"
        # Item 4: new item "바나나"
        curr_pending = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과"},
            {"file": "skillA.json", "path": ["data", 0, "desc"], "source": "사과를 먹는다"},
            {"file": "skillB.json", "path": ["data", 1, "name"], "source": "이 낫으로 답하나이다!"},
            {"file": "skillB.json", "path": ["data", 1, "desc"], "source": "스킬'이 낫으로 답하나이다!'로 공격"},
            {"file": "skillC.json", "path": ["data", 2, "name"], "source": "바나나"},
        ]
        curr_review = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과", "reason": "term_check"},
            {"file": "skillC.json", "path": ["data", 2, "name"], "source": "바나나", "reason": "new_term"},
        ]
        diff_file = self.current_dir / "diff.json"
        inc_rv.safe_write_json(diff_file, {"pending": curr_pending, "review": curr_review})
        curr_diff_sha = inc_rv.file_sha256(diff_file)

        curr_trans = [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과", "translation": "苹果"},
            {"file": "skillA.json", "path": ["data", 0, "desc"], "source": "사과를 먹는다", "translation": "吃红苹果"}, # changed
            {"file": "skillB.json", "path": ["data", 1, "name"], "source": "이 낫으로 답하나이다!", "translation": "以此镰答复！"}, # slightly different draft
            {"file": "skillB.json", "path": ["data", 1, "desc"], "source": "스킬'이 낫으로 답하나이다!'로 공격", "translation": "以此镰答复！攻击"},
            {"file": "skillC.json", "path": ["data", 2, "name"], "source": "바나나", "translation": "香蕉"},
        ]
        inc_rv.safe_write_json(self.current_dir / "translations.json", curr_trans)

        tm_file = self.current_dir / "translation_memory.json"
        inc_rv.safe_write_json(tm_file, {"version": 1})
        tm_sha = inc_rv.file_sha256(tm_file)

        manifest = {
            "translation_memory_hash": tm_sha,
            "diff_hash": curr_diff_sha,
        }
        inc_rv.safe_write_json(self.current_dir / "shards_manifest.json", manifest)

    def test_llc_tree_mismatch_fails_immediately(self):
        self._setup_mock_directories(tamper_llc=True)
        with patch.object(inc_rv.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_rv.run_incremental_review(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("LLC snapshot tree mismatch", str(ctx.exception))

    def test_baseline_review_hashes_mismatch_fails_immediately(self):
        # Tamper baseline reviewed-translations after writing receipt
        with open(self.baseline_dir / "reviewed-translations.json", "w", encoding="utf-8") as f:
            f.write("[]")

        with patch.object(inc_rv.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_rv.run_incremental_review(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Baseline reviewed_translations_sha mismatch", str(ctx.exception))

    def test_force_file_not_found_fails_immediately(self):
        non_existent = self.base_dir / "non_existent.json"
        with patch.object(inc_rv.tp, "validate", return_value=0):
            with self.assertRaises(FileNotFoundError) as ctx:
                inc_rv.run_incremental_review(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                    force_review_items_path=non_existent,
                )
            self.assertIn("Force review items file specified but not found", str(ctx.exception))

    def test_skill_closure_and_delta_isolation_and_merge(self):
        # Mock review_batches.orchestrate_review
        def mock_orchestrate_review(run_dir, **kwargs):
            sub_diff = inc_rv.safe_read_json(run_dir / "diff.json")
            sub_trans = inc_rv.safe_read_json(run_dir / "translations.json")

            # Check that "이 낫으로 답하나이다!" and its referencing desc are both in sub_diff pending
            pending_sources = [p["source"] for p in sub_diff["pending"]]
            self.assertIn("이 낫으로 답하나이다!", pending_sources)
            self.assertIn("스킬'이 낫으로 답하나이다!'로 공격", pending_sources)
            # Item 0 (사과) should NOT be in sub_diff pending
            self.assertNotIn("사과", pending_sources)

            # Produce reviewed-translations with identical skill term
            reviewed_out = []
            for it in sub_trans:
                src = it["source"]
                if src == "이 낫으로 답하나이다!":
                    t = "以此镰答复！"
                elif "이 낫으로 답하나이다!" in src:
                    t = "以技能“以此镰答复！”发动攻击"
                else:
                    t = it["translation"] + "_reviewed"
                reviewed_out.append({
                    "file": it["file"],
                    "path": it["path"],
                    "source": it["source"],
                    "translation": t,
                })
            inc_rv.safe_write_json(run_dir / "reviewed-translations.json", reviewed_out)
            rev_sha = inc_rv.file_sha256(run_dir / "reviewed-translations.json")

            # Produce review.json
            disps = []
            for r in sub_diff["review"]:
                disps.append({
                    "file": r["file"],
                    "path": r["path"],
                    "source": r["source"],
                    "reason": r.get("reason", ""),
                    "action": "accept",
                    "resolved": True,
                    "resolution_note": "校对通过",
                })
            inc_rv.safe_write_json(run_dir / "review.json", {
                "hashes": {
                    "diff.json": inc_rv.file_sha256(run_dir / "diff.json"),
                    "translations.json": inc_rv.file_sha256(run_dir / "translations.json"),
                    "reviewed-translations.json": rev_sha,
                },
                "dispositions": disps,
            })

            # Produce semantic-review-receipt.json
            inc_rv.safe_write_json(run_dir / "semantic-review-receipt.json", {
                "status": "success",
                "batch_receipts": [{"batch": 0, "agent": "mock_agent"}],
            })
            return 0

        with patch.object(inc_rv.rb, "orchestrate_review", side_effect=mock_orchestrate_review), \
             patch.object(inc_rv.tp, "validate", return_value=0):
            res = inc_rv.run_incremental_review(
                run_dir=self.current_dir,
                baseline_run_dir=self.baseline_dir,
            )
            self.assertEqual(res, 0)

        # Verify merged outputs
        merged_reviewed = inc_rv.safe_read_json(self.current_dir / "reviewed-translations.json")
        self.assertEqual(len(merged_reviewed), 5)
        # 사과 should be reused from baseline ("苹果")
        self.assertEqual(merged_reviewed[0]["translation"], "苹果")
        # delta items
        self.assertEqual(merged_reviewed[1]["translation"], "吃红苹果_reviewed")
        self.assertEqual(merged_reviewed[2]["translation"], "以此镰答复！")
        self.assertEqual(merged_reviewed[3]["translation"], "以技能“以此镰答复！”发动攻击")
        self.assertEqual(merged_reviewed[4]["translation"], "香蕉_reviewed")

        receipt = inc_rv.safe_read_json(self.current_dir / "semantic-review-receipt.json")
        self.assertEqual(receipt["strategy"], "verified_baseline_review_plus_delta")
        self.assertEqual(receipt["reused_reviewed_count"], 1)
        self.assertEqual(receipt["delta_reviewed_count"], 4)

    def test_inconsistent_skill_terminology_blocks_completion(self):
        def mock_orchestrate_review_inconsistent(run_dir, **kwargs):
            sub_diff = inc_rv.safe_read_json(run_dir / "diff.json")
            sub_trans = inc_rv.safe_read_json(run_dir / "translations.json")

            reviewed_out = []
            for it in sub_trans:
                src = it["source"]
                if src == "이 낫으로 답하나이다!":
                    t = "以此镰答复！"
                elif "이 낫으로 답하나이다!" in src:
                    # Inconsistent terminology in description!
                    t = "以技能“以镰回敬！”发动攻击"
                else:
                    t = it["translation"] + "_reviewed"
                reviewed_out.append({
                    "file": it["file"],
                    "path": it["path"],
                    "source": it["source"],
                    "translation": t,
                })
            inc_rv.safe_write_json(run_dir / "reviewed-translations.json", reviewed_out)
            rev_sha = inc_rv.file_sha256(run_dir / "reviewed-translations.json")

            disps = []
            for r in sub_diff["review"]:
                disps.append({
                    "file": r["file"],
                    "path": r["path"],
                    "source": r["source"],
                    "reason": r.get("reason", ""),
                    "action": "accept",
                    "resolved": True,
                    "resolution_note": "校对通过",
                })
            inc_rv.safe_write_json(run_dir / "review.json", {
                "hashes": {
                    "diff.json": inc_rv.file_sha256(run_dir / "diff.json"),
                    "translations.json": inc_rv.file_sha256(run_dir / "translations.json"),
                    "reviewed-translations.json": rev_sha,
                },
                "dispositions": disps,
            })
            inc_rv.safe_write_json(run_dir / "semantic-review-receipt.json", {"status": "success", "batch_receipts": []})
            return 0

        with patch.object(inc_rv.rb, "orchestrate_review", side_effect=mock_orchestrate_review_inconsistent), \
             patch.object(inc_rv.tp, "validate", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                inc_rv.run_incremental_review(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Terminology inconsistency detected", str(ctx.exception))

    def test_force_review_items_triggers_review(self):
        # Create force review items file for item 0 (사과)
        force_file = self.base_dir / "force.json"
        inc_rv.safe_write_json(force_file, [
            {"file": "skillA.json", "path": ["data", 0, "name"], "source": "사과"}
        ])

        def mock_orchestrate_review(run_dir, **kwargs):
            sub_diff = inc_rv.safe_read_json(run_dir / "diff.json")
            pending_sources = [p["source"] for p in sub_diff["pending"]]
            self.assertIn("사과", pending_sources)

            reviewed_out = [
                {"file": it["file"], "path": it["path"], "source": it["source"], "translation": it["translation"]}
                for it in inc_rv.safe_read_json(run_dir / "translations.json")
            ]
            inc_rv.safe_write_json(run_dir / "reviewed-translations.json", reviewed_out)
            rev_sha = inc_rv.file_sha256(run_dir / "reviewed-translations.json")
            inc_rv.safe_write_json(run_dir / "review.json", {
                "hashes": {
                    "diff.json": inc_rv.file_sha256(run_dir / "diff.json"),
                    "translations.json": inc_rv.file_sha256(run_dir / "translations.json"),
                    "reviewed-translations.json": rev_sha,
                },
                "dispositions": [
                    {"file": r["file"], "path": r["path"], "source": r["source"], "reason": r.get("reason", ""), "action": "accept", "resolved": True, "resolution_note": "ok"}
                    for r in sub_diff["review"]
                ]
            })
            inc_rv.safe_write_json(run_dir / "semantic-review-receipt.json", {"status": "success", "batch_receipts": []})
            return 0

        with patch.object(inc_rv.rb, "orchestrate_review", side_effect=mock_orchestrate_review), \
             patch.object(inc_rv.tp, "validate", return_value=0):
            res = inc_rv.run_incremental_review(
                run_dir=self.current_dir,
                baseline_run_dir=self.baseline_dir,
                force_review_items_path=force_file,
            )
            self.assertEqual(res, 0)

    def test_unresolved_review_item_blocks_completion(self):
        def mock_orchestrate_review_unresolved(run_dir, **kwargs):
            sub_diff = inc_rv.safe_read_json(run_dir / "diff.json")
            inc_rv.safe_write_json(run_dir / "reviewed-translations.json", [
                {"file": it["file"], "path": it["path"], "source": it["source"], "translation": it["translation"]}
                for it in inc_rv.safe_read_json(run_dir / "translations.json")
            ])
            rev_sha = inc_rv.file_sha256(run_dir / "reviewed-translations.json")
            inc_rv.safe_write_json(run_dir / "review.json", {
                "hashes": {
                    "diff.json": inc_rv.file_sha256(run_dir / "diff.json"),
                    "translations.json": inc_rv.file_sha256(run_dir / "translations.json"),
                    "reviewed-translations.json": rev_sha,
                },
                "dispositions": [
                    {"file": r["file"], "path": r["path"], "source": r["source"], "reason": r.get("reason", ""), "action": "accept", "resolved": False, "resolution_note": "unresolved"}
                    for r in sub_diff["review"]
                ]
            })
            inc_rv.safe_write_json(run_dir / "semantic-review-receipt.json", {"status": "success", "batch_receipts": []})
            return 0

        with patch.object(inc_rv.rb, "orchestrate_review", side_effect=mock_orchestrate_review_unresolved), \
             patch.object(inc_rv.tp, "validate", return_value=0):
            with self.assertRaises(RuntimeError) as ctx:
                inc_rv.run_incremental_review(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Unresolved item encountered", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
