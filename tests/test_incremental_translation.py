#!/usr/bin/env python3
"""Tests for incremental translation orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import scripts.incremental_translation as inc_tr


class TestIncrementalTranslation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

        self.baseline_dir = self.base_dir / "baseline"
        self.baseline_dir.mkdir(parents=True, exist_ok=True)

        self.current_dir = self.base_dir / "current"
        self.current_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_mock_runs(self, tamper_reviewed: bool = False, tamper_receipt_sha: bool = False):
        # Baseline reviewed translations
        reviewed_data = [
            {"file": "fileA.json", "path": ["data", 0, "name"], "source": "사과", "translation": "苹果"},
            {"file": "fileB.json", "path": ["data", 1, "desc"], "source": "바나나", "translation": "香蕉"},
            {"file": "fileC.json", "path": ["data", 2, "skill"], "source": "공격1", "translation": "攻击1"},
        ]
        reviewed_file = self.baseline_dir / "reviewed-translations.json"
        inc_tr.safe_write_json(reviewed_file, reviewed_data)
        actual_reviewed_sha = inc_tr.file_sha256(reviewed_file)

        if tamper_receipt_sha:
            receipt_sha = "0000000000000000000000000000000000000000000000000000000000000000"
        else:
            receipt_sha = actual_reviewed_sha

        # Baseline semantic review receipt
        sr_receipt = {
            "status": "success",
            "reviewed_translations_sha": receipt_sha,
        }
        inc_tr.safe_write_json(self.baseline_dir / "semantic-review-receipt.json", sr_receipt)
        inc_tr.safe_write_json(self.baseline_dir / "diff.json", {"pending": []})

        if tamper_reviewed:
            reviewed_data[0]["translation"] = "篡改苹果"
            inc_tr.safe_write_json(reviewed_file, reviewed_data)

        # Current run:
        # Item 0: unchanged source -> reused as draft
        # Item 1: changed source ("바나나2") -> delta
        # Item 2: new item ("오렌지") -> delta
        curr_pending = [
            {"file": "fileA.json", "path": ["data", 0, "name"], "source": "사과"},
            {"file": "fileB.json", "path": ["data", 1, "desc"], "source": "바나나2"},
            {"file": "fileD.json", "path": ["data", 3, "extra"], "source": "오렌지"},
        ]
        curr_diff_file = self.current_dir / "diff.json"
        inc_tr.safe_write_json(curr_diff_file, {"pending": curr_pending})
        curr_diff_sha = inc_tr.file_sha256(curr_diff_file)

        # Current translation memory
        tm_index = {
            "version": 1,
            "records": [],
            "by_source": {},
            "by_file_order": {},
            "file_path_ordinals": {},
            "name_records": {},
            "ngram_index": {},
            "source_conflicts": {},
        }
        tm_file = self.current_dir / "translation_memory.json"
        inc_tr.safe_write_json(tm_file, tm_index)
        tm_sha = inc_tr.file_sha256(tm_file)

        manifest = {
            "translation_memory_hash": tm_sha,
            "diff_hash": curr_diff_sha,
            "shards": [
                {
                    "shard_id": "shard_00",
                    "index_file": "translation_memory.json",
                    "index_hash": tm_sha,
                }
            ]
        }
        inc_tr.safe_write_json(self.current_dir / "shards_manifest.json", manifest)

    def test_pairing_and_source_change_separation(self):
        self._setup_mock_runs()

        # Mock driver
        mock_driver = MagicMock()
        mock_driver.create_workspace.return_value = ("ws_test_id", "pane_mock_0")

        def fake_execute_shard(shard, **kwargs):
            s_dir = Path(shard["directory"])
            inp = inc_tr.safe_read_json(s_dir / "input.json")
            out_items = []
            for it in inp:
                out_items.append({
                    "file": it["file"],
                    "path": it["path"],
                    "source": it["source"],
                    "translation": f"Translated_{it['source']}",
                })
            inc_tr.safe_write_json(s_dir / "translations.json", out_items)
            return {
                "shard_id": shard["shard_id"],
                "status": "success",
                "items_count": len(out_items),
            }

        with patch.object(inc_tr.tp, "validate", return_value=0), \
             patch.object(inc_tr.ht, "execute_translation_shard", side_effect=fake_execute_shard):
            receipt = inc_tr.run_incremental_translation(
                run_dir=self.current_dir,
                baseline_run_dir=self.baseline_dir,
                concurrency=2,
                timeout_sec=60,
                driver=mock_driver,
            )

        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["strategy"], "verified_baseline_drafts_plus_delta")
        self.assertEqual(receipt["reused_draft_count"], 1)
        self.assertEqual(receipt["new_translation_count"], 2)
        self.assertEqual(receipt["total_translations_count"], 3)

        # Check merged translations.json
        merged = inc_tr.safe_read_json(self.current_dir / "translations.json")
        self.assertEqual(len(merged), 3)

        # Item 0 was reused from baseline reviewed translation
        self.assertEqual(merged[0]["source"], "사과")
        self.assertEqual(merged[0]["translation"], "苹果")

        # Item 1 had changed source ("바나나2") -> translated fresh
        self.assertEqual(merged[1]["source"], "바나나2")
        self.assertEqual(merged[1]["translation"], "Translated_바나나2")

        # Item 2 was newly added ("오렌지") -> translated fresh
        self.assertEqual(merged[2]["source"], "오렌지")
        self.assertEqual(merged[2]["translation"], "Translated_오렌지")

    def test_baseline_tampering_rejected(self):
        # Case 1: semantic review receipt hash mismatch
        self._setup_mock_runs(tamper_receipt_sha=True)
        with patch.object(inc_tr.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_tr.run_incremental_translation(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Baseline reviewed_translations_sha mismatch", str(ctx.exception))

    def test_baseline_pipeline_validate_failure_rejected(self):
        self._setup_mock_runs()
        with patch.object(inc_tr.tp, "validate", return_value=2):
            with self.assertRaises(ValueError) as ctx:
                inc_tr.run_incremental_translation(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Baseline validation failed", str(ctx.exception))

    def test_baseline_receipt_status_failure_rejected(self):
        self._setup_mock_runs()
        sr = inc_tr.safe_read_json(self.baseline_dir / "semantic-review-receipt.json")
        sr["status"] = "failed"
        inc_tr.safe_write_json(self.baseline_dir / "semantic-review-receipt.json", sr)

        with patch.object(inc_tr.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_tr.run_incremental_translation(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Baseline semantic review receipt status is not 'success'", str(ctx.exception))

    def test_current_index_mismatch_rejected(self):
        self._setup_mock_runs()
        # Tamper shards_manifest.json index_hash
        manifest = inc_tr.safe_read_json(self.current_dir / "shards_manifest.json")
        manifest["shards"][0]["index_hash"] = "tampered_hash_value"
        inc_tr.safe_write_json(self.current_dir / "shards_manifest.json", manifest)

        with patch.object(inc_tr.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_tr.run_incremental_translation(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Index hash mismatch", str(ctx.exception))

    def test_manifest_top_level_hash_mismatch_rejected(self):
        self._setup_mock_runs()
        manifest = inc_tr.safe_read_json(self.current_dir / "shards_manifest.json")
        manifest["translation_memory_hash"] = "tampered_tm_hash"
        inc_tr.safe_write_json(self.current_dir / "shards_manifest.json", manifest)

        with patch.object(inc_tr.tp, "validate", return_value=0):
            with self.assertRaises(ValueError) as ctx:
                inc_tr.run_incremental_translation(
                    run_dir=self.current_dir,
                    baseline_run_dir=self.baseline_dir,
                )
            self.assertIn("Top-level translation_memory_hash mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
