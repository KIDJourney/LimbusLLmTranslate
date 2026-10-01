#!/usr/bin/env python3
"""Tests for shard budgeting, context precomputation, and boundary enforcement."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "scripts"))

import translation_pipeline as tp


class TestTMShardBudget(unittest.TestCase):
    def test_900_short_items_sharded_at_most_50_items(self):
        """Verify 900 short items are partitioned into shards with <= 50 items each, preserving order and coverage."""
        items = [{"file": f"file_{i // 100}.json", "path": ["data", i], "source": f"src_{i}"} for i in range(900)]
        context_cache = {id(it): {"evidence": [f"match_{i}"]} for i, it in enumerate(items)}

        shards = tp.subshard_by_budget(items, context_cache, max_items=50, max_budget=40000)

        # 900 items with max 50 items per shard must produce exactly 18 shards
        self.assertEqual(len(shards), 18)
        for s in shards:
            self.assertLessEqual(len(s), 50)
            for item in s:
                # Ensure no internal cache keys added
                self.assertNotIn("_cached_context", item)
                self.assertNotIn("context", item)

        # Verify exact order and complete coverage
        flattened = [it for s in shards for it in s]
        self.assertEqual(flattened, items)

    def test_budget_character_limit_boundary(self):
        """Verify shard splits when combined item and context JSON exceeds max_budget."""
        # Create items where each item + context is ~15,000 characters
        large_context = {"evidence": ["x" * 14800]}
        items = [{"file": "test.json", "path": ["data", i], "source": f"source_{i}"} for i in range(5)]
        context_cache = {id(it): large_context for it in items}

        # 2 items ~ 30,000 chars (fits in 40,000), 3 items ~ 45,000 chars (exceeds)
        shards = tp.subshard_by_budget(items, context_cache, max_items=50, max_budget=40000)
        self.assertEqual(len(shards), 3)  # [2, 2, 1]
        self.assertEqual([len(s) for s in shards], [2, 2, 1])

        flattened = [it for s in shards for it in s]
        self.assertEqual(flattened, items)

    def test_single_item_exceeding_budget_raises_value_error(self):
        """Verify single item exceeding 40,000 characters explicitly raises ValueError without truncation."""
        giant_context = {"evidence": ["x" * 41000]}
        item = {"file": "overflow.json", "path": ["data", 0], "source": "hello"}
        context_cache = {id(item): giant_context}

        with self.assertRaises(ValueError) as ctx:
            tp.subshard_by_budget([item], context_cache, max_items=50, max_budget=40000)
        self.assertIn("exceeds character budget 40000", str(ctx.exception))
        self.assertIn("overflow.json", str(ctx.exception))

    def test_full_prepare_budget_integration(self):
        """Verify prepare() partitions shards with budget and preserves exact pending coverage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run_test"
            run_dir.mkdir(parents=True, exist_ok=True)

            source_dir = run_dir / "snapshot/source"
            llc_dir = run_dir / "snapshot/llc"
            kr_dir = source_dir / "KR"
            cn_dir = llc_dir / "LLC_zh-CN"
            kr_dir.mkdir(parents=True, exist_ok=True)
            cn_dir.mkdir(parents=True, exist_ok=True)
            (kr_dir / "story.json").write_text(json.dumps({"dialogue": ["dummy"]}), encoding="utf-8")
            (cn_dir / "story.json").write_text(json.dumps({"dialogue": ["dummy"]}), encoding="utf-8")

            (source_dir / "provenance.json").write_text(json.dumps({"source_kind": "windows_ssh", "raw_version": "v1", "source_hash": "h1"}))
            (llc_dir / "provenance.json").write_text(json.dumps({"release": "v1", "asset_sha256": "h2"}))

            # Create mock diff with 120 items
            diff_items = [{"file": "story.json", "path": ["dialogue", i], "source": f"line {i}"} for i in range(120)]
            diff_data = {
                "summary": {"pending_fields": len(diff_items), "parse_errors": 0},
                "pending": diff_items,
                "review": [],
            }
            (run_dir / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")

            with patch("translation_pipeline.run_snapshot_clis", return_value=({"raw_version": "v1"}, {"release": "v1"})),                  patch("translation_pipeline.get_live_published_manifest", return_value=None),                  patch.object(tp.localization, "diff", return_value=None),                  patch("translation_memory.build_translation_memory_index", return_value={"index": {}}),                  patch("translation_memory.query_item_evidence", side_effect=lambda idx, f, p, s: {"mock": f"evidence_for_{s}"}):

                code = tp.prepare(run_dir)
                self.assertEqual(code, 0)

                manifest = json.loads((run_dir / "shards_manifest.json").read_text(encoding="utf-8"))
                # 120 items split into at least 3 shards (max 50 each: 50, 50, 20)
                self.assertEqual(manifest["shards_count"], 3)
                self.assertEqual(manifest["total_pending"], 120)

                # Check prompt in shard 0
                prompt_text = (run_dir / "shards/shard_00/prompt.txt").read_text(encoding="utf-8")
                self.assertIn("context.json 为参考候选与证据提示", prompt_text)
                self.assertIn("证据不充分或缺少可靠匹配时应依据原文直接翻译", prompt_text)
                self.assertIn("term_notes 字段中说明理由与疑点", prompt_text)

                # Verify input.json has no extra cached fields
                sh0_input = json.loads((run_dir / "shards/shard_00/input.json").read_text(encoding="utf-8"))
                self.assertEqual(len(sh0_input), 50)
                for it in sh0_input:
                    self.assertEqual(set(it.keys()), {"file", "path", "source"})


if __name__ == "__main__":
    unittest.main()
