#!/usr/bin/env python3
"""Tests for Thin Stage Dispatcher (scripts/cycle_stage.py)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cycle_stage


class TestCycleStage(unittest.TestCase):
    def setUp(self):
        self.test_dir = ROOT / "tmp_test_cycle_stage"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)

        self.runs_dir = self.test_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        self.current_run = self.runs_dir / "run_current"
        self.current_run.mkdir(parents=True, exist_ok=True)
        self.curr_llc = self.current_run / "snapshot/llc/LLC_zh-CN"
        self.curr_llc.mkdir(parents=True, exist_ok=True)
        (self.curr_llc / "dummy.txt").write_text("llc content v1", encoding="utf-8")

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_no_baseline_fallback_translate_and_review(self):
        # Translate with no baseline -> fallback to ht.run_translation
        args = argparse.Namespace(concurrency=4, timeout=600)
        with patch("herdr_translation.run_translation", autospec=True, return_value=0) as mock_full_trans, \
             patch("incremental_translation.run_incremental_translation", autospec=True) as mock_inc_trans:
            ret = cycle_stage.dispatch_translate(self.current_run, args)
            self.assertEqual(ret, 0)
            mock_full_trans.assert_called_once()
            mock_inc_trans.assert_not_called()

            # Baseline selection should be recorded as full
            selection_file = self.current_run / "baseline-selection.json"
            self.assertTrue(selection_file.is_file())
            data = json.loads(selection_file.read_text(encoding="utf-8"))
            self.assertEqual(data["strategy"], "full")

        # Review with full strategy -> fallback to rb.orchestrate_review
        rev_args = argparse.Namespace(max_workers=4, timeout=900)
        with patch("review_batches.orchestrate_review", autospec=True, return_value=0) as mock_full_rev, \
             patch("incremental_review.run_incremental_review", autospec=True) as mock_inc_rev:
            ret_rev = cycle_stage.dispatch_review(self.current_run, rev_args)
            self.assertEqual(ret_rev, 0)
            mock_full_rev.assert_called_once()
            mock_inc_rev.assert_not_called()

    def test_valid_baseline_incremental_path(self):
        # Setup sibling historical run with timestamp directory name
        baseline_run = self.runs_dir / "20261001-120000-base01"
        baseline_run.mkdir(parents=True, exist_ok=True)
        (baseline_run / "semantic-review-receipt.json").write_text("{}", encoding="utf-8")
        (baseline_run / "reviewed-translations.json").write_text("[]", encoding="utf-8")

        fake_freeze = {
            "reviewed_translations_sha": "rev123",
            "diff_sha": "diff123",
            "review_json_sha": "revjson123",
            "semantic_receipt_sha": "receipt123",
            "llc_tree_sha": cycle_stage.get_current_llc_tree_sha(self.current_run),
        }

        # Mock baseline freeze validation
        with patch("incremental_review.validate_and_freeze_baseline", autospec=True, return_value=fake_freeze), \
             patch("incremental_translation.run_incremental_translation", autospec=True, return_value={}) as mock_inc_trans:
            args = argparse.Namespace(concurrency=6, timeout=1200)
            ret = cycle_stage.dispatch_translate(self.current_run, args)
            self.assertEqual(ret, 0)
            mock_inc_trans.assert_called_once()

            selection_file = self.current_run / "baseline-selection.json"
            self.assertTrue(selection_file.is_file())
            sel_data = json.loads(selection_file.read_text(encoding="utf-8"))
            self.assertEqual(sel_data["strategy"], "incremental")
            self.assertEqual(sel_data["baseline_path"], str(baseline_run.resolve()))

        # Now test review dispatch reusing the frozen baseline with autospec=True
        with patch("incremental_review.validate_and_freeze_baseline", autospec=True, return_value=fake_freeze), \
             patch("incremental_review.run_incremental_review", autospec=True, return_value=0) as mock_inc_rev:
            rev_args = argparse.Namespace(max_workers=6, timeout=1800)
            ret_rev = cycle_stage.dispatch_review(self.current_run, rev_args)
            self.assertEqual(ret_rev, 0)
            mock_inc_rev.assert_called_once()

    def test_baseline_tamper_rejected_on_review(self):
        baseline_run = self.runs_dir / "20261001-120000-base02"
        baseline_run.mkdir(parents=True, exist_ok=True)

        initial_freeze = {
            "reviewed_translations_sha": "hash_original",
            "diff_sha": "diff123",
            "review_json_sha": "revjson123",
            "semantic_receipt_sha": "receipt123",
            "llc_tree_sha": cycle_stage.get_current_llc_tree_sha(self.current_run),
        }

        selection_file = self.current_run / "baseline-selection.json"
        selection_file.write_text(json.dumps({
            "strategy": "incremental",
            "baseline_path": str(baseline_run.resolve()),
            "baseline_freeze": initial_freeze,
            "llc_tree_sha": initial_freeze["llc_tree_sha"],
        }), encoding="utf-8")

        # Baseline was tampered (different reviewed_translations_sha)
        tampered_freeze = dict(initial_freeze)
        tampered_freeze["reviewed_translations_sha"] = "hash_tampered"

        with patch("incremental_review.validate_and_freeze_baseline", autospec=True, return_value=tampered_freeze):
            rev_args = argparse.Namespace(max_workers=6, timeout=1800)
            with self.assertRaises(RuntimeError) as ctx:
                cycle_stage.dispatch_review(self.current_run, rev_args)
            self.assertIn("Baseline tamper detected", str(ctx.exception))

    def test_baseline_freeze_key_missing_rejected_on_review(self):
        baseline_run = self.runs_dir / "20261001-120000-base03"
        baseline_run.mkdir(parents=True, exist_ok=True)

        # Incomplete frozen metadata (missing semantic_receipt_sha)
        incomplete_freeze = {
            "reviewed_translations_sha": "hash_original",
            "diff_sha": "diff123",
            "review_json_sha": "revjson123",
            "llc_tree_sha": cycle_stage.get_current_llc_tree_sha(self.current_run),
        }

        selection_file = self.current_run / "baseline-selection.json"
        selection_file.write_text(json.dumps({
            "strategy": "incremental",
            "baseline_path": str(baseline_run.resolve()),
            "baseline_freeze": incomplete_freeze,
            "llc_tree_sha": incomplete_freeze["llc_tree_sha"],
        }), encoding="utf-8")

        current_freeze = dict(incomplete_freeze)
        current_freeze["semantic_receipt_sha"] = "receipt123"

        with patch("incremental_review.validate_and_freeze_baseline", autospec=True, return_value=current_freeze):
            rev_args = argparse.Namespace(max_workers=6, timeout=1800)
            with self.assertRaises(RuntimeError) as ctx:
                cycle_stage.dispatch_review(self.current_run, rev_args)
            self.assertIn("Missing required key: semantic_receipt_sha", str(ctx.exception))

    def test_terminology_force_review_candidate_generation(self):
        # Create diff.json with name field
        diff_data = {
            "pending": [
                {
                    "file": "Skills.json",
                    "path": ["dataList", ["id", "101", 0], "name"],
                    "source": "검은 줄기",
                    "current": None,
                },
                {
                    "file": "Passives.json",
                    "path": ["dataList", ["id", "201", 0], "desc"],
                    "source": "검은 줄기",
                    "current": None,
                }
            ]
        }
        (self.current_run / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")

        # Create translations.json where name translation differs from canonical LLC
        translations = [
            {
                "file": "Skills.json",
                "path": ["dataList", ["id", "101", 0], "name"],
                "source": "검은 줄기",
                "translation": "黑色树枝",  # differs from canonical 黑色荆棘
            },
            {
                "file": "Passives.json",
                "path": ["dataList", ["id", "201", 0], "desc"],
                "source": "검은 줄기",
                "translation": "黑色荆棘",
            }
        ]
        (self.current_run / "translations.json").write_text(json.dumps(translations), encoding="utf-8")

        # Create translation_memory.json with unique canonical LLC translation
        tm_data = {
            "records": [
                {"id": 0, "source": "검은 줄기", "translation": "黑色荆棘"}
            ],
            "by_source": {
                "검은 줄기": [0]
            }
        }
        (self.current_run / "translation_memory.json").write_text(json.dumps(tm_data), encoding="utf-8")

        force_items = cycle_stage.generate_terminology_force_review_items(self.current_run)
        self.assertEqual(len(force_items), 1)
        self.assertEqual(force_items[0]["source"], "검은 줄기")
        self.assertEqual(force_items[0]["current_translation"], "黑色树枝")
        self.assertEqual(force_items[0]["target_terminology"], "黑色荆棘")
        self.assertEqual(force_items[0]["reason"], "terminology_conflict")


if __name__ == "__main__":
    unittest.main()
