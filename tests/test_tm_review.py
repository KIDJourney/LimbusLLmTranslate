"""Unit test for translation memory and review batches integration.

Tests:
1. Validator rejects needs_translation with resolved: true, accepts resolved: false.
2. check_existing_verified_receipt rejects cache if context_sha or index_sha mismatches.
3. Prompt construction properly formats items, JSON schema, LLC rules, and context sections.
4. Legacy prompts (no context) retain complete schema and rules without context section.
5. Worker that writes nothing on context cache miss must FAIL (stale result.json renamed).
6. Worker writing new result in prompt callback succeeds and renames old result.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "scripts"))

from scripts.herdr_translation import file_sha256, safe_write_json
import scripts.review_batches as rb


class TranslationMemoryReviewBatchTest(unittest.TestCase):

    def test_needs_translation_must_have_resolved_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            batch_input = [{
                "file": "Passives.json",
                "path": ["name"],
                "source": "보존",
                "reason": "korean_remaining",
            }]
            result_file = tmp_dir / "result.json"

            # Case A: resolved is True with needs_translation -> Must FAIL validation with error
            safe_write_json(result_file, [{
                "file": "Passives.json",
                "path": ["name"],
                "source": "보존",
                "reason": "korean_remaining",
                "action": "needs_translation",
                "resolution_note": "ambiguous evidence",
                "resolved": True,
            }])
            valid_items, errs = rb.validate_review_batch_output(batch_input, result_file)
            self.assertGreater(len(errs), 0, "Expected validation errors when resolved is True for needs_translation")
            self.assertTrue(any("action is 'needs_translation' but resolved is True" in e for e in errs))

            # Case B: resolved is False with needs_translation -> Must PASS validation without error
            safe_write_json(result_file, [{
                "file": "Passives.json",
                "path": ["name"],
                "source": "보존",
                "reason": "korean_remaining",
                "action": "needs_translation",
                "resolution_note": "ambiguous evidence",
                "resolved": False,
            }])
            valid_items, errs = rb.validate_review_batch_output(batch_input, result_file)
            self.assertEqual(len(errs), 0, f"Expected no validation errors, got: {errs}")
            self.assertEqual(len(valid_items), 1)

    def test_cache_receipt_context_and_index_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            input_file = batch_dir / "input.json"
            result_file = batch_dir / "result.json"
            receipt_file = batch_dir / "receipt.json"

            items = [{
                "index": 0,
                "file": "Enemies.json",
                "path": ["name"],
                "source": "원레그",
                "translation": "单脚人",
            }]
            safe_write_json(input_file, items)
            input_sha = file_sha256(input_file)

            valid_result = [{
                "index": 0,
                "file": "Enemies.json",
                "path": ["name"],
                "source": "원레그",
                "translation": "单脚人",
                "verdict": "approved",
                "reason": "exact evidence match",
            }]
            safe_write_json(result_file, valid_result)
            resp_sha = file_sha256(result_file)

            base_rcpt = {
                "status": "success",
                "input_sha": input_sha,
                "response_sha": resp_sha,
                "context_sha": "ctx_sha_correct",
                "index_sha": "idx_sha_correct",
            }
            safe_write_json(receipt_file, base_rcpt)

            # 1. Matching hashes -> Can reuse
            cached = rb.check_existing_verified_receipt(
                batch_dir=batch_dir,
                batch_type="pending",
                batch_idx=0,
                batch_items=items,
                input_sha_pre=input_sha,
                context_sha="ctx_sha_correct",
                index_sha="idx_sha_correct",
            )
            self.assertIsNotNone(cached)

            # 2. Context sha mismatch -> Must NOT reuse
            cached_ctx_mismatch = rb.check_existing_verified_receipt(
                batch_dir=batch_dir,
                batch_type="pending",
                batch_idx=0,
                batch_items=items,
                input_sha_pre=input_sha,
                context_sha="ctx_sha_MODIFIED",
                index_sha="idx_sha_correct",
            )
            self.assertIsNone(cached_ctx_mismatch)

            # 3. Index sha mismatch -> Must NOT reuse
            cached_idx_mismatch = rb.check_existing_verified_receipt(
                batch_dir=batch_dir,
                batch_type="pending",
                batch_idx=0,
                batch_items=items,
                input_sha_pre=input_sha,
                context_sha="ctx_sha_correct",
                index_sha="idx_sha_MODIFIED",
            )
            self.assertIsNone(cached_idx_mismatch)

    def test_stale_result_invalidation_and_prompt_write_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            diff_file = tmp_dir / "diff.json"
            trans_file = tmp_dir / "translations.json"
            draft_file = tmp_dir / "draft_translations.json"
            safe_write_json(diff_file, {"pending": [], "review": []})
            safe_write_json(trans_file, [])
            safe_write_json(draft_file, [])
            diff_sha = file_sha256(diff_file)
            trans_sha = file_sha256(trans_file)
            draft_sha = file_sha256(draft_file)

            p_items = [{
                "index": 0, "file": "Enemies.json", "path": ["name"],
                "source": "원레그", "translation": "单脚人",
            }]

            # Sub-case 1: Stale result exists, driver writes NOTHING -> Must FAIL, old file renamed
            batch_dir_fail = tmp_dir / "stale_fail"
            batch_dir_fail.mkdir()
            c_file_fail = batch_dir_fail / "context.json"
            safe_write_json(c_file_fail, [{"evidence_id": "tm_fail"}])
            c_sha_fail = file_sha256(c_file_fail)

            safe_write_json(batch_dir_fail / "input.json", p_items)
            in_sha_fail = file_sha256(batch_dir_fail / "input.json")
            # Old stale result
            old_result_file = batch_dir_fail / "result.json"
            safe_write_json(old_result_file, [{
                "index": 0, "file": "Enemies.json", "path": ["name"],
                "source": "원레그", "translation": "独腿人",
                "verdict": "approved", "reason": "stale reason",
            }])

            noop_driver = MagicMock()
            noop_driver.get_agent_status.return_value = "idle"
            noop_driver.read_agent_output.return_value = ""

            with self.assertRaises(ValueError) as ctx:
                rb.process_batch(
                    driver=noop_driver, agent_name="ag_noop", pane_id="%1", batch_dir=batch_dir_fail,
                    batch_type="pending", batch_idx=0, batch_items=p_items, timeout_sec=5,
                    input_sha_pre=in_sha_fail, diff_file=diff_file, diff_sha_pre=diff_sha,
                    trans_file=trans_file, trans_sha_pre=trans_sha, draft_file=draft_file,
                    draft_sha_pre=draft_sha, context_file=c_file_fail, context_sha=c_sha_fail,
                )
            self.assertIn("failed after", str(ctx.exception))
            # Verify result.json was backed up to pre-context backup
            backups = list(batch_dir_fail.glob("result.pre-context-*.json"))
            self.assertEqual(len(backups), 1, "Expected stale result to be renamed to backup")

            # Sub-case 2: Stale result exists, driver writes NEW valid result in prompt callback -> Succeeds
            batch_dir_succ = tmp_dir / "stale_succ"
            batch_dir_succ.mkdir()
            c_file_succ = batch_dir_succ / "context.json"
            safe_write_json(c_file_succ, [{"evidence_id": "tm_succ"}])
            c_sha_succ = file_sha256(c_file_succ)

            safe_write_json(batch_dir_succ / "input.json", p_items)
            in_sha_succ = file_sha256(batch_dir_succ / "input.json")
            safe_write_json(batch_dir_succ / "result.json", [{
                "index": 0, "file": "Enemies.json", "path": ["name"],
                "source": "원레그", "translation": "独腿人",
                "verdict": "approved", "reason": "old",
            }])

            succ_driver = MagicMock()
            succ_driver.get_agent_status.return_value = "idle"
            succ_driver.read_agent_output.return_value = ""

            def write_new_result(agent: str, prompt: str, timeout_sec: int = 60) -> None:
                # Worker actually produces new result.json reflecting context
                safe_write_json(batch_dir_succ / "result.json", [{
                    "index": 0, "file": "Enemies.json", "path": ["name"],
                    "source": "원레그", "translation": "单脚人",
                    "verdict": "approved", "reason": "tm_succ evidence applied",
                }])

            succ_driver.prompt_agent.side_effect = write_new_result

            res = rb.process_batch(
                driver=succ_driver, agent_name="ag_succ", pane_id="%1", batch_dir=batch_dir_succ,
                batch_type="pending", batch_idx=0, batch_items=p_items, timeout_sec=5,
                input_sha_pre=in_sha_succ, diff_file=diff_file, diff_sha_pre=diff_sha,
                trans_file=trans_file, trans_sha_pre=trans_sha, draft_file=draft_file,
                draft_sha_pre=draft_sha, context_file=c_file_succ, context_sha=c_sha_succ,
            )
            self.assertEqual(res["receipt"]["status"], "success")
            self.assertEqual(res["items"][0]["translation"], "单脚人")
            succ_backups = list(batch_dir_succ.glob("result.pre-context-*.json"))
            self.assertEqual(len(succ_backups), 1, "Expected old stale result backed up")

    def test_prompt_construction_and_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            diff_file = tmp_dir / "diff.json"
            trans_file = tmp_dir / "translations.json"
            draft_file = tmp_dir / "draft_translations.json"
            safe_write_json(diff_file, {"pending": [], "review": []})
            safe_write_json(trans_file, [])
            safe_write_json(draft_file, [])
            diff_sha = file_sha256(diff_file)
            trans_sha = file_sha256(trans_file)
            draft_sha = file_sha256(draft_file)

            mock_driver = MagicMock()
            mock_driver.get_agent_status.return_value = "idle"
            prompts: list[str] = []
            mock_driver.read_agent_output.return_value = ""

            # Pending batch with context
            b_dir_ctx = tmp_dir / "p_ctx"
            b_dir_ctx.mkdir()
            c_file = b_dir_ctx / "context.json"
            safe_write_json(c_file, [{"evidence_id": "tm_01"}])
            c_sha = file_sha256(c_file)

            p_items = [{
                "index": 0, "file": "Enemies.json", "path": ["name"],
                "source": "원레그", "translation": "单脚人",
            }]
            safe_write_json(b_dir_ctx / "input.json", p_items)
            in_sha = file_sha256(b_dir_ctx / "input.json")

            def prompt_side_effect_ctx(agent: str, prompt: str, timeout_sec: int = 60) -> None:
                prompts.append(prompt)
                safe_write_json(b_dir_ctx / "result.json", [{
                    "index": 0, "file": "Enemies.json", "path": ["name"],
                    "source": "원레그", "translation": "单脚人",
                    "verdict": "approved", "reason": "ok",
                }])

            mock_driver.prompt_agent.side_effect = prompt_side_effect_ctx

            rb.process_batch(
                driver=mock_driver, agent_name="ag1", pane_id="%1", batch_dir=b_dir_ctx,
                batch_type="pending", batch_idx=0, batch_items=p_items, timeout_sec=5,
                input_sha_pre=in_sha, diff_file=diff_file, diff_sha_pre=diff_sha,
                trans_file=trans_file, trans_sha_pre=trans_sha, draft_file=draft_file,
                draft_sha_pre=draft_sha, context_file=c_file, context_sha=c_sha,
            )
            p_with_ctx = prompts[-1]
            self.assertIn("원레그", p_with_ctx)
            self.assertIn("JSON 数组", p_with_ctx)
            self.assertIn("已核实LLC译名核心术语规范", p_with_ctx)
            self.assertIn("参考记忆与证据文件", p_with_ctx)
            self.assertIn("unresolved", p_with_ctx)

            # Pending batch without context (legacy)
            b_dir_leg = tmp_dir / "p_leg"
            b_dir_leg.mkdir()
            safe_write_json(b_dir_leg / "input.json", p_items)
            in_sha_leg = file_sha256(b_dir_leg / "input.json")

            def prompt_side_effect_leg(agent: str, prompt: str, timeout_sec: int = 60) -> None:
                prompts.append(prompt)
                safe_write_json(b_dir_leg / "result.json", [{
                    "index": 0, "file": "Enemies.json", "path": ["name"],
                    "source": "원레그", "translation": "单脚人",
                    "verdict": "approved", "reason": "ok",
                }])

            mock_driver.prompt_agent.side_effect = prompt_side_effect_leg

            rb.process_batch(
                driver=mock_driver, agent_name="ag1", pane_id="%1", batch_dir=b_dir_leg,
                batch_type="pending", batch_idx=1, batch_items=p_items, timeout_sec=5,
                input_sha_pre=in_sha_leg, diff_file=diff_file, diff_sha_pre=diff_sha,
                trans_file=trans_file, trans_sha_pre=trans_sha, draft_file=draft_file,
                draft_sha_pre=draft_sha, context_file=None, context_sha=None,
            )
            p_legacy = prompts[-1]
            self.assertIn("원레그", p_legacy)
            self.assertIn("JSON 数组", p_legacy)
            self.assertIn("已核实LLC译名核心术语规范", p_legacy)
            self.assertNotIn("参考记忆与证据文件", p_legacy)


if __name__ == "__main__":
    unittest.main()
