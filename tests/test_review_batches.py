"""Unit tests for single-agent serial batch review orchestrator."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import scripts.localization as localization
import scripts.review_batches as rb

ROOT = Path(__file__).resolve().parents[1]


class TestReviewBatches(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp_dir.name) / "run_test"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.sleep_patcher = patch("scripts.review_batches.time.sleep", return_value=None)
        self.sleep_patcher.start()
        self.settle_patcher = patch("scripts.review_batches.wait_agent_until_settled", return_value="idle")
        self.settle_patcher.start()

    def tearDown(self):
        self.settle_patcher.stop()
        self.sleep_patcher.stop()
        self.temp_dir.cleanup()

    def _create_mock_driver(self):
        driver = MagicMock()
        driver.run_cmd.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        return driver

    def test_validate_pending_batch_output_passes_and_catches_tampering(self):
        batch_input = [
            {"index": 0, "file": "test.json", "path": ["a", 0], "source": "안녕", "translation": "你好"}
        ]
        res_file = self.run_dir / "result.json"

        # 1. Valid output
        valid_out = [
            {
                "index": 0,
                "file": "test.json",
                "path": ["a", 0],
                "source": "안녕",
                "translation": "你好！",
                "verdict": "approved",
                "reason": "准确流畅",
            }
        ]
        res_file.write_text(json.dumps(valid_out, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_pending_batch_output(batch_input, res_file)
        self.assertEqual(len(errs), 0)
        self.assertEqual(len(items), 1)

        # 2. Source tampering
        tampered_out = copy.deepcopy(valid_out)
        tampered_out[0]["source"] = "篡改原文"
        res_file.write_text(json.dumps(tampered_out, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_pending_batch_output(batch_input, res_file)
        self.assertTrue(any("source tampering" in e for e in errs))

        # 3. Invalid verdict
        bad_verdict = copy.deepcopy(valid_out)
        bad_verdict[0]["verdict"] = "something_else"
        res_file.write_text(json.dumps(bad_verdict, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_pending_batch_output(batch_input, res_file)
        self.assertTrue(any("invalid verdict" in e for e in errs))

        # 4. Missing reason
        missing_reason = copy.deepcopy(valid_out)
        missing_reason[0]["reason"] = ""
        res_file.write_text(json.dumps(missing_reason, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_pending_batch_output(batch_input, res_file)
        self.assertTrue(any("missing review reason" in e for e in errs))

    def test_validate_review_batch_output_passes_and_catches_invalid_action(self):
        batch_input = [
            {"file": "test.json", "path": ["a", 0], "reason": "diff_reason", "source": "안녕"}
        ]
        res_file = self.run_dir / "result.json"

        # 1. Valid disposition
        valid_out = [
            {
                "file": "test.json",
                "path": ["a", 0],
                "reason": "diff_reason",
                "source": "안녕",
                "action": "approved_as_is",
                "resolution_note": "符合预期无需修改",
                "resolved": True,
            }
        ]
        res_file.write_text(json.dumps(valid_out, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_review_batch_output(batch_input, res_file)
        self.assertEqual(len(errs), 0)
        self.assertEqual(len(items), 1)

        # 2. Invalid action
        bad_action = copy.deepcopy(valid_out)
        bad_action[0]["action"] = "unknown_action"
        res_file.write_text(json.dumps(bad_action, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_review_batch_output(batch_input, res_file)
        self.assertTrue(any("invalid action" in e for e in errs))

        # 3. Non-boolean resolved
        bad_res = copy.deepcopy(valid_out)
        bad_res[0]["resolved"] = "true"  # string instead of boolean
        res_file.write_text(json.dumps(bad_res, ensure_ascii=False), encoding="utf-8")
        items, errs = rb.validate_review_batch_output(batch_input, res_file)
        self.assertTrue(any("'resolved' must be boolean" in e for e in errs))

    def test_orchestrate_review_serial_execution_and_aggregation(self):
        # Prepare diff.json & translations.json
        diff_data = {
            "pending": [
                {"file": "p.json", "path": ["x", 0], "source": "안녕1"},
                {"file": "p.json", "path": ["x", 1], "source": "안녕2"},
            ],
            "review": [
                {"file": "r.json", "path": ["y", 0], "reason": "why", "source": "더미"}
            ],
        }
        (self.run_dir / "diff.json").write_text(json.dumps(diff_data, ensure_ascii=False), encoding="utf-8")

        trans_data = [
            {"file": "p.json", "path": ["x", 0], "source": "안녕1", "translation": "你好1"},
            {"file": "p.json", "path": ["x", 1], "source": "안녕2", "translation": "你好2"},
        ]
        (self.run_dir / "translations.json").write_text(json.dumps(trans_data, ensure_ascii=False), encoding="utf-8")

        # Mock HerdrDriver
        mock_driver = self._create_mock_driver()
        mock_driver.create_workspace.return_value = ("ws_123", "pane_root")
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "pane_root"}
        mock_driver.get_agent_status.return_value = "idle"

        prompts_received = []

        def fake_prompt(agent_name, prompt_text, timeout_sec=60):
            prompts_received.append(prompt_text)
            # Find target output file from prompt_text
            import re
            m = re.search(r"目标输出文件：([^\n]+)", prompt_text)
            if m:
                out_path = Path(m.group(1).strip())
                # If pending batch
                if "待校对翻译批次" in prompt_text:
                    out_items = [
                        {
                            "index": 0,
                            "file": "p.json",
                            "path": ["x", 0],
                            "source": "안녕1",
                            "translation": "你好1确认",
                            "verdict": "approved",
                            "reason": "OK",
                        },
                        {
                            "index": 1,
                            "file": "p.json",
                            "path": ["x", 1],
                            "source": "안녕2",
                            "translation": "你好2确认",
                            "verdict": "approved",
                            "reason": "OK",
                        }
                    ]
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(json.dumps(out_items, ensure_ascii=False), encoding="utf-8")
                elif "Diff复核项审查批次" in prompt_text:
                    out_items = [
                        {
                            "file": "r.json",
                            "path": ["y", 0],
                            "reason": "why",
                            "source": "더미",
                            "action": "internal_dummy_ignored",
                            "resolution_note": "内部废弃",
                            "resolved": True,
                        }
                    ]
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(json.dumps(out_items, ensure_ascii=False), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = fake_prompt

        ret = rb.orchestrate_review(
            run_dir=self.run_dir,
            driver=mock_driver,
            batch_size=50,
            max_workers=1,
        )
        self.assertEqual(ret, 0)

        # 1. Exactly one workspace & agent start
        mock_driver.create_workspace.assert_called_once()
        mock_driver.start_claude_agent.assert_called_once()

        # 2. Prompts directly embed items JSON
        self.assertEqual(len(prompts_received), 2)
        self.assertIn("안녕1", prompts_received[0])
        self.assertIn("안녕2", prompts_received[0])
        self.assertIn("p.json", prompts_received[0])
        self.assertIn("더미", prompts_received[1])

        # 3. Output artifacts generated and valid
        rev_file = self.run_dir / "reviewed-translations.json"
        rep_file = self.run_dir / "review.json"
        sem_file = self.run_dir / "semantic-review-receipt.json"

        self.assertTrue(rev_file.is_file())
        self.assertTrue(rep_file.is_file())
        self.assertTrue(sem_file.is_file())

        rev_data = json.loads(rev_file.read_text(encoding="utf-8"))
        self.assertEqual(len(rev_data), 2)
        self.assertEqual(rev_data[0]["translation"], "你好1确认")

        rep_data = json.loads(rep_file.read_text(encoding="utf-8"))
        self.assertEqual(len(rep_data["dispositions"]), 1)
        self.assertEqual(rep_data["hashes"]["diff.json"], rb.file_sha256(self.run_dir / "diff.json"))
        self.assertEqual(rep_data["hashes"]["translations.json"], rb.file_sha256(self.run_dir / "translations.json"))
        self.assertEqual(rep_data["hashes"]["reviewed-translations.json"], rb.file_sha256(rev_file))

        sem_data = json.loads(sem_file.read_text(encoding="utf-8"))
        self.assertEqual(sem_data["status"], "success")
        self.assertEqual(len(sem_data["batch_receipts"]), 2)

    def test_reconnect_live_agent_verification_and_unresolved_blocks(self):
        run_d1 = self.run_dir / "mismatch_test"
        run_d1.mkdir(parents=True, exist_ok=True)
        # 1. Reconnect pane mismatch raises ValueError
        diff_data = {"pending": [], "review": []}
        (run_d1 / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")
        (run_d1 / "translations.json").write_text(json.dumps([]), encoding="utf-8")

        mock_driver = self._create_mock_driver()
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "other_pane"}

        with self.assertRaises(ValueError):
            rb.orchestrate_review(
                run_dir=run_d1,
                agent_name="reviewer_xyz",
                pane_id="expected_pane",
                driver=mock_driver,
            )

        # 2. Unresolved item blocks with exit 2
        run_d2 = self.run_dir / "unresolved_test"
        run_d2.mkdir(parents=True, exist_ok=True)
        diff_data_pending = {
            "pending": [{"file": "p.json", "path": ["x", 0], "source": "안녕"}],
            "review": [],
        }
        (run_d2 / "diff.json").write_text(json.dumps(diff_data_pending), encoding="utf-8")
        (run_d2 / "translations.json").write_text(json.dumps([{"file": "p.json", "path": ["x", 0], "source": "안녕", "translation": "你好"}]), encoding="utf-8")

        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "expected_pane"}
        mock_driver.get_agent_status.return_value = "idle"

        def fake_unresolved(agent_name, prompt_text, timeout_sec=60):
            import re
            m = re.search(r"目标输出文件：([^\n]+)", prompt_text)
            if m:
                out_path = Path(m.group(1).strip())
                out_items = [
                    {
                        "index": 0,
                        "file": "p.json",
                        "path": ["x", 0],
                        "source": "안녕",
                        "translation": "你好",
                        "verdict": "unresolved",
                        "reason": "术语冲突待确认",
                    }
                ]
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(out_items, ensure_ascii=False), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = fake_unresolved
        ret = rb.orchestrate_review(
            run_dir=run_d2,
            agent_name="reviewer_xyz",
            pane_id="expected_pane",
            driver=mock_driver,
        )
        self.assertEqual(ret, 2)
        self.assertTrue((run_d2 / "unresolved_review_items.json").is_file())
        # Reconnected pane is never closed
        mock_driver.close_pane.assert_not_called()

    def test_single_arg_identity_rejected_and_registry_persistence(self):
        diff_data = {"pending": [], "review": []}
        (self.run_dir / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")
        (self.run_dir / "translations.json").write_text(json.dumps([]), encoding="utf-8")

        mock_driver = self._create_mock_driver()

        # One arg missing must fail
        with self.assertRaises(ValueError):
            rb.orchestrate_review(
                run_dir=self.run_dir,
                agent_name="only_name",
                pane_id=None,
                driver=mock_driver,
            )

        with self.assertRaises(ValueError):
            rb.orchestrate_review(
                run_dir=self.run_dir,
                agent_name=None,
                pane_id="only_pane",
                driver=mock_driver,
            )

        # Explicit live identity binds to reviewer_registry.json
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "wK:p1"}
        mock_driver.get_agent_status.return_value = "idle"
        ret = rb.orchestrate_review(
            run_dir=self.run_dir,
            agent_name="reviewer_2b081926",
            pane_id="wK:p1",
            driver=mock_driver,
        )
        self.assertEqual(ret, 0)
        reg_file = self.run_dir / "reviewer_registry.json"
        self.assertTrue(reg_file.is_file())
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        self.assertEqual(reg_data["agent_name"], "reviewer_2b081926")
        self.assertEqual(reg_data["pane_id"], "wK:p1")

        # Resume mode without args automatically loads registry
        ret_resume = rb.orchestrate_review(
            run_dir=self.run_dir,
            driver=mock_driver,
        )
        self.assertEqual(ret_resume, 0)

    def test_frozen_draft_resume_and_tamper_rejection(self):
        diff_data = {
            "pending": [{"file": "f.json", "path": ["a", 0], "source": "원문"}],
            "review": [],
        }
        (self.run_dir / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")
        trans_data = [{"file": "f.json", "path": ["a", 0], "source": "원문", "translation": "初始译文"}]
        (self.run_dir / "translations.json").write_text(json.dumps(trans_data), encoding="utf-8")

        mock_driver = self._create_mock_driver()
        mock_driver.create_workspace.return_value = ("ws_1", "p_1")
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "p_1"}
        mock_driver.get_agent_status.return_value = "idle"

        def fake_p(name, text, timeout_sec=60):
            import re
            m = re.search(r"目标输出文件：([^\n]+)", text)
            if m:
                p = Path(m.group(1).strip())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps([{
                    "index": 0, "file": "f.json", "path": ["a", 0], "source": "원문",
                    "translation": "校对后", "verdict": "approved", "reason": "ok"
                }], ensure_ascii=False), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = fake_p

        # Run 1: initializes frozen draft
        ret1 = rb.orchestrate_review(run_dir=self.run_dir, driver=mock_driver)
        self.assertEqual(ret1, 0)
        draft_file = self.run_dir / "draft_translations.json"
        draft_meta_file = self.run_dir / "draft_translations.meta.json"
        self.assertTrue(draft_file.is_file())
        self.assertTrue(draft_meta_file.is_file())

        # Tampering draft content raises ValueError on resume
        draft_file.write_text("tampered content", encoding="utf-8")
        with self.assertRaises(ValueError):
            rb.orchestrate_review(run_dir=self.run_dir, driver=mock_driver)

    def test_input_tamper_detected_during_batch_review(self):
        batch_input = [{"index": 0, "file": "t.json", "path": ["p", 0], "source": "원문", "translation": "译文"}]
        batch_dir = self.run_dir / "b_test"
        diff_file = self.run_dir / "diff.json"
        trans_file = self.run_dir / "translations.json"
        draft_file = self.run_dir / "draft.json"

        diff_file.write_text("{}", encoding="utf-8")
        trans_file.write_text("[]", encoding="utf-8")
        draft_file.write_text("[]", encoding="utf-8")

        mock_driver = self._create_mock_driver()
        mock_driver.get_agent_status.return_value = "idle"

        def tamper_diff(name, text, timeout_sec=60):
            # Tamper run diff during prompt execution
            diff_file.write_text('{"tampered": true}', encoding="utf-8")
            res_file = batch_dir / "result.json"
            res_file.write_text(json.dumps([{
                "index": 0, "file": "t.json", "path": ["p", 0], "source": "원문",
                "translation": "译文", "verdict": "approved", "reason": "ok"
            }]), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = tamper_diff

        with self.assertRaises(ValueError):
            rb.process_batch(
                driver=mock_driver,
                agent_name="ag",
                pane_id="p1",
                batch_dir=batch_dir,
                batch_type="pending",
                batch_idx=0,
                batch_items=batch_input,
                timeout_sec=60,
                input_sha_pre=rb.file_sha256(diff_file), # placeholder
                diff_file=diff_file,
                diff_sha_pre=rb.file_sha256(diff_file),
                trans_file=trans_file,
                trans_sha_pre=rb.file_sha256(trans_file),
                draft_file=draft_file,
                draft_sha_pre=rb.file_sha256(draft_file),
            )

    def test_parallel_worker_pool_dispatch_and_reuse(self):
        diff_data = {
            "pending": [
                {"file": "f1.json", "path": ["a", 0], "source": "안녕1"},
                {"file": "f2.json", "path": ["a", 1], "source": "안녕2"},
                {"file": "f3.json", "path": ["a", 2], "source": "안녕3"},
            ],
            "review": [],
        }
        (self.run_dir / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")
        trans_data = [
            {"file": "f1.json", "path": ["a", 0], "source": "안녕1", "translation": "你好1"},
            {"file": "f2.json", "path": ["a", 1], "source": "안녕2", "translation": "你好2"},
            {"file": "f3.json", "path": ["a", 2], "source": "안녕3", "translation": "你好3"},
        ]
        (self.run_dir / "translations.json").write_text(json.dumps(trans_data), encoding="utf-8")

        mock_driver = self._create_mock_driver()
        mock_driver.create_workspace.return_value = ("ws_test", "pane_root")
        mock_driver.split_pane.return_value = "pane_split"
        mock_driver.move_pane_to_new_tab.return_value = "pane_tab"
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "pane_root"}
        mock_driver.get_agent_status.return_value = "idle"

        def fake_worker_prompt(agent_name, prompt_text, timeout_sec=60):
            import re
            m = re.search(r"目标输出文件：([^\n]+)", prompt_text)
            if m:
                out_path = Path(m.group(1).strip())
                # Parse items from prompt
                m_items = re.search(r"输入待审条目如下：\n(\[[\s\S]+\])", prompt_text)
                if m_items:
                    items = json.loads(m_items.group(1))
                    res = []
                    for it in items:
                        res.append({
                            "index": it["index"],
                            "file": it["file"],
                            "path": it["path"],
                            "source": it["source"],
                            "translation": it["translation"] + "_reviewed",
                            "verdict": "approved",
                            "reason": "OK",
                        })
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = fake_worker_prompt

        # Run with batch_size=1 and max_workers=3
        ret = rb.orchestrate_review(
            run_dir=self.run_dir,
            driver=mock_driver,
            batch_size=1,
            max_workers=3,
        )
        self.assertEqual(ret, 0)

        reg_file = self.run_dir / "reviewer_registry.json"
        self.assertTrue(reg_file.is_file())
        reg_data = json.loads(reg_file.read_text())
        self.assertEqual(len(reg_data.get("workers", [])), 3)

        sem_file = self.run_dir / "semantic-review-receipt.json"
        self.assertTrue(sem_file.is_file())
        sem_data = json.loads(sem_file.read_text())
        self.assertEqual(len(sem_data["batch_receipts"]), 3)

    def test_verified_cache_not_cleared_and_prompt_forbids_historical_ai(self):
        # 1. Setup pending batch input
        diff_data = {
            "pending": [{"file": "f.json", "path": ["a", 0], "source": "원문"}],
            "review": [],
        }
        (self.run_dir / "diff.json").write_text(json.dumps(diff_data), encoding="utf-8")
        trans_data = [{"file": "f.json", "path": ["a", 0], "source": "원문", "translation": "初始译文"}]
        (self.run_dir / "translations.json").write_text(json.dumps(trans_data), encoding="utf-8")

        # 2. Pre-populate verified batch cache (receipt.json + result.json)
        batch_dir = self.run_dir / "reviews" / "batches" / "pending_batch_0000"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_input = [{"index": 0, "file": "f.json", "path": ["a", 0], "source": "원문", "translation": "初始译文"}]
        rb.safe_write_json(batch_dir / "input.json", batch_input)
        input_sha = rb.file_sha256(batch_dir / "input.json")
        res_items = [{
            "index": 0, "file": "f.json", "path": ["a", 0], "source": "원문",
            "translation": "已验证译文", "verdict": "approved", "reason": "通过"
        }]
        rb.safe_write_json(batch_dir / "result.json", res_items)
        receipt = {
            "batch_idx": 0,
            "batch_type": "pending",
            "input_sha": input_sha,
            "response_sha": rb.file_sha256(batch_dir / "result.json"),
            "agent_name": "cached_worker",
            "pane_id": "pane_cached",
            "status": "success",
            "item_count": 1,
            "completed_at": 1000.0,
        }
        rb.safe_write_json(batch_dir / "receipt.json", receipt)

        mock_driver = self._create_mock_driver()
        mock_driver.create_workspace.return_value = ("ws_test", "pane_root")
        mock_driver.get_agent_info.return_value = {"agent_status": "idle", "pane_id": "pane_root"}
        mock_driver.get_agent_status.return_value = "idle"

        ret = rb.orchestrate_review(
            run_dir=self.run_dir,
            driver=mock_driver,
            batch_size=50,
            max_workers=1,
        )
        self.assertEqual(ret, 0)
        # Verified cache reused: driver.run_cmd MUST NOT have been called with /clear
        clear_calls = [
            call for call in mock_driver.run_cmd.call_args_list
            if any("/clear" in str(arg) for arg in call[0])
        ]
        self.assertEqual(len(clear_calls), 0)
        mock_driver.prompt_agent.assert_not_called()

        # 3. Verify prompt contents for new unverified batches
        new_batch_dir = self.run_dir / "reviews" / "batches" / "pending_batch_0001"
        prompts_sent = []
        def capture_prompt(name, text, timeout_sec=60):
            prompts_sent.append(text)
            p = new_batch_dir / "result.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps([{
                "index": 0, "file": "f.json", "path": ["a", 0], "source": "원문",
                "translation": "新译文", "verdict": "approved", "reason": "ok"
            }], ensure_ascii=False), encoding="utf-8")

        mock_driver.prompt_agent.side_effect = capture_prompt
        diff_file = self.run_dir / "diff.json"
        trans_file = self.run_dir / "translations.json"
        draft_file = self.run_dir / "draft_translations.json"
        rb.process_batch(
            driver=mock_driver,
            agent_name="new_worker",
            pane_id="pane_root",
            batch_dir=new_batch_dir,
            batch_type="pending",
            batch_idx=1,
            batch_items=batch_input,
            timeout_sec=60,
            input_sha_pre=input_sha,
            diff_file=diff_file,
            diff_sha_pre=rb.file_sha256(diff_file),
            trans_file=trans_file,
            trans_sha_pre=rb.file_sha256(trans_file),
            draft_file=draft_file,
            draft_sha_pre=rb.file_sha256(draft_file),
        )
        self.assertEqual(len(prompts_sent), 1)
        sent_prompt = prompts_sent[0]
        self.assertIn("【检索范围与预算】", sent_prompt)
        self.assertIn("禁止读取其它运行、其它批次、历史AI译文或校对输出作为术语依据", sent_prompt)
        self.assertIn("禁止整份输出 context.json、translation_memory.json 或全库内容", sent_prompt)

    def test_new_batch_clears_only_when_settled_and_aborts_on_failure(self):
        case_dir = self.run_dir / "new_batch_test"
        case_dir.mkdir(parents=True, exist_ok=True)
        batch_dir = case_dir / "pending_batch_0000"
        batch_input = [{"index": 0, "file": "f.json", "path": ["a", 0], "source": "원문", "translation": "译文"}]
        rb.safe_write_json(batch_dir / "input.json", batch_input)
        input_sha = rb.file_sha256(batch_dir / "input.json")
        diff_file = case_dir / "diff.json"
        diff_file.write_text("{}", encoding="utf-8")
        trans_file = case_dir / "translations.json"
        trans_file.write_text("[]", encoding="utf-8")
        draft_file = case_dir / "draft.json"
        draft_file.write_text("[]", encoding="utf-8")

        mock_driver = self._create_mock_driver()

        # Case 1: Worker not settled initially -> refuses prompt without calling /clear
        mock_driver.get_agent_status.return_value = "running"
        with patch("scripts.review_batches.wait_agent_until_settled", return_value="running"):
            with self.assertRaises(RuntimeError) as cm:
                rb.process_batch(
                    driver=mock_driver,
                    agent_name="busy_worker",
                    pane_id="p1",
                    batch_dir=batch_dir,
                    batch_type="pending",
                    batch_idx=0,
                    batch_items=batch_input,
                    timeout_sec=60,
                    input_sha_pre=input_sha,
                    diff_file=diff_file,
                    diff_sha_pre=rb.file_sha256(diff_file),
                    trans_file=trans_file,
                    trans_sha_pre=rb.file_sha256(trans_file),
                    draft_file=draft_file,
                    draft_sha_pre=rb.file_sha256(draft_file),
                )
            self.assertIn("refusing prompt", str(cm.exception))
            mock_driver.run_cmd.assert_not_called()
            mock_driver.prompt_agent.assert_not_called()

        # Case 2: Worker idle, but after /clear reset it fails to settle -> aborts without dispatching prompt
        mock_driver.reset_mock()
        mock_driver.get_agent_status.return_value = "idle"
        with patch("scripts.review_batches.wait_agent_until_settled", return_value="error"):
            with self.assertRaises(RuntimeError) as cm2:
                rb.process_batch(
                    driver=mock_driver,
                    agent_name="reset_fail_worker",
                    pane_id="p1",
                    batch_dir=batch_dir,
                    batch_type="pending",
                    batch_idx=0,
                    batch_items=batch_input,
                    timeout_sec=60,
                    input_sha_pre=input_sha,
                    diff_file=diff_file,
                    diff_sha_pre=rb.file_sha256(diff_file),
                    trans_file=trans_file,
                    trans_sha_pre=rb.file_sha256(trans_file),
                    draft_file=draft_file,
                    draft_sha_pre=rb.file_sha256(draft_file),
                )
            self.assertIn("Reviewer session reset not settled", str(cm2.exception))
            # /clear was executed
            mock_driver.run_cmd.assert_called_with(["agent", "prompt", "reset_fail_worker", "/clear"], timeout=30)
            self.assertTrue((batch_dir / "session_reset.json").is_file())
            # Actual prompt was NEVER dispatched
            mock_driver.prompt_agent.assert_not_called()

        # Case 3: Worker idle and reset settles -> proceeds to dispatch prompt
        mock_driver.reset_mock()
        mock_driver.get_agent_status.return_value = "idle"
        with patch("scripts.review_batches.wait_agent_until_settled", return_value="idle"):
            def fake_prompt(name, text, timeout_sec=60):
                res_f = batch_dir / "result.json"
                res_f.write_text(json.dumps([{
                    "index": 0, "file": "f.json", "path": ["a", 0], "source": "원문",
                    "translation": "译文", "verdict": "approved", "reason": "ok"
                }], ensure_ascii=False), encoding="utf-8")
            mock_driver.prompt_agent.side_effect = fake_prompt
            res = rb.process_batch(
                driver=mock_driver,
                agent_name="settled_worker",
                pane_id="p1",
                batch_dir=batch_dir,
                batch_type="pending",
                batch_idx=0,
                batch_items=batch_input,
                timeout_sec=60,
                input_sha_pre=input_sha,
                diff_file=diff_file,
                diff_sha_pre=rb.file_sha256(diff_file),
                trans_file=trans_file,
                trans_sha_pre=rb.file_sha256(trans_file),
                draft_file=draft_file,
                draft_sha_pre=rb.file_sha256(draft_file),
            )
            self.assertEqual(len(res["items"]), 1)
            mock_driver.run_cmd.assert_called_with(["agent", "prompt", "settled_worker", "/clear"], timeout=30)
            mock_driver.prompt_agent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
