#!/usr/bin/env python3
"""Targeted unit tests for orchestration hardening.

Covers:
1. Train: Worker fault isolation - one worker failure quarantines that worker, while remaining healthy workers continue processing tasks from the queue; failed batch blocks final success.
2. Preserve: Full cache - fully cached/verified batches do not initialize or spawn any worker panes; normal parallel execution succeeds cleanly with 6 workers.
3. Holdout: Anti-tamper and anti-stall - root file (diff/trans/draft) tampering halts the entire pool immediately; working/unknown/blocked agent states reject blind re-prompting; legacy validator fails closed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import herdr_translation
import review_batches


class FakeHerdrDriver:
    """Mock HerdrDriver that can simulate normal, failing, busy, and tampered agents."""

    def __init__(self, agent_behaviors: dict[str, str] | None = None, agent_panes: dict[str, str] | None = None) -> None:
        self.agent_behaviors = agent_behaviors or {}
        self.agent_panes = agent_panes or {}
        self.prompts: list[tuple[str, str]] = []
        self.closed_panes: list[str] = []
        self.panes_split: int = 0
        self.agent_statuses: dict[str, str] = {}
        self.lock = threading.Lock()

    def get_agent_status(self, agent_name: str) -> str:
        with self.lock:
            return self.agent_statuses.get(agent_name, "idle")

    def get_agent_info(self, agent_name: str) -> dict[str, Any]:
        with self.lock:
            pane_id = self.agent_panes.get(agent_name, agent_name)
            return {
                "name": agent_name,
                "pane_id": pane_id,
                "status": self.agent_statuses.get(agent_name, "idle"),
            }

    def prompt_agent(self, agent_name: str, prompt: str, timeout_sec: int = 60) -> None:
        with self.lock:
            self.prompts.append((agent_name, prompt))
            behavior = self.agent_behaviors.get(agent_name, "normal")
            if behavior == "blocked":
                self.agent_statuses[agent_name] = "blocked"
            elif behavior == "working_stall":
                self.agent_statuses[agent_name] = "working"
            else:
                self.agent_statuses[agent_name] = "idle"

    def wait_agent(self, agent_name: str, timeout_ms: int = 5000) -> bool:
        return True

    def read_agent_output(self, agent_name: str, lines: int = 200) -> str:
        return f"[FakeDriver output for {agent_name}]"

    def create_workspace(self, cwd: Path | None = None, label: str = "") -> tuple[str, str]:
        with self.lock:
            self.panes_split += 1
            return ("ws_fake", f"pane_{self.panes_split}")

    def split_pane(self, target_pane: str = "", *args: Any, **kwargs: Any) -> str:
        with self.lock:
            self.panes_split += 1
            return f"pane_{self.panes_split}"

    def move_pane_to_new_tab(self, pane_id: str, label: str = "", *args: Any, **kwargs: Any) -> str:
        return pane_id

    def start_claude_agent(self, pane_id: str, agent_name: str, model: str, settings_path: str, *args: Any, **kwargs: Any) -> None:
        with self.lock:
            self.agent_statuses[agent_name] = "idle"

    def close_pane(self, pane_id: str) -> None:
        with self.lock:
            self.closed_panes.append(pane_id)


class OrchestrationHardeningTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp_dir.name)

        # Create basic run files
        self.diff_file = self.run_dir / "diff.json"
        self.trans_file = self.run_dir / "translations.json"

        # 3 pending items across 3 small batches
        self.pending_items = [
            {
                "file": "test1.json",
                "path": ["key1"],
                "source": "사과",
                "reason": "new",
            },
            {
                "file": "test2.json",
                "path": ["key2"],
                "source": "바나나",
                "reason": "new",
            },
            {
                "file": "test3.json",
                "path": ["key3"],
                "source": "원레그",
                "reason": "new",
            },
        ]
        self.diff_data = {"pending": self.pending_items, "review": []}
        self.raw_translations = [
            {"file": "test1.json", "path": ["key1"], "source": "사과", "translation": "苹果"},
            {"file": "test2.json", "path": ["key2"], "source": "바나나", "translation": "香蕉"},
            {"file": "test3.json", "path": ["key3"], "source": "원레그", "translation": "单脚人"},
        ]
        review_batches.safe_write_json(self.diff_file, self.diff_data)
        review_batches.safe_write_json(self.trans_file, self.raw_translations)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # --- 1. TRAIN: Worker fault isolation ---
    def test_train_worker_fault_isolation(self) -> None:
        """When worker 1 fails, it is quarantined while worker 2 continues draining tasks."""
        driver = FakeHerdrDriver(
            agent_behaviors={"worker_fail": "fail", "worker_good": "normal"},
            agent_panes={"worker_fail": "p_fail", "worker_good": "p_good"},
        )

        batches_dir = self.run_dir / "reviews/batches"
        # Custom mock process_batch to simulate worker_fail throwing, worker_good succeeding
        orig_process_batch = review_batches.process_batch

        def mock_process_batch(driver, agent_name, pane_id, batch_dir, batch_type, batch_idx, batch_items, **kwargs):
            if agent_name == "worker_fail":
                raise RuntimeError("Simulated worker crash")
            # Produce valid result for good worker
            res_items = []
            for it in batch_items:
                res_items.append({
                    "index": it["index"],
                    "file": it["file"],
                    "path": it["path"],
                    "source": it["source"],
                    "translation": it["translation"],
                    "verdict": "approved",
                    "reason": "测试通过",
                })
            res_file = batch_dir / "result.json"
            review_batches.safe_write_json(res_file, res_items)
            res_sha = review_batches.file_sha256(res_file)
            input_file = batch_dir / "input.json"
            review_batches.safe_write_json(input_file, batch_items)
            inp_sha = review_batches.file_sha256(input_file)
            receipt = {
                "batch_type": batch_type,
                "batch_idx": batch_idx,
                "status": "success",
                "agent_name": agent_name,
                "pane_id": pane_id,
                "attempts": 1,
                "duration_sec": 0.5,
                "input_sha": inp_sha,
                "response_sha": res_sha,
            }
            rcpt_file = batch_dir / "receipt.json"
            review_batches.safe_write_json(rcpt_file, receipt)
            return {"batch_type": batch_type, "batch_idx": batch_idx, "items": res_items, "receipt": receipt}

        review_batches.process_batch = mock_process_batch
        try:
            # Seed registry with 2 workers
            registry_file = self.run_dir / "reviewer_registry.json"
            review_batches.safe_write_json(registry_file, {
                "run_dir": str(self.run_dir.resolve()),
                "workers": [
                    {"agent_name": "worker_fail", "pane_id": "p_fail"},
                    {"agent_name": "worker_good", "pane_id": "p_good"},
                ],
            })

            # Running orchestrate_review should isolate worker_fail and raise error at the end because of failed batch
            with self.assertRaises(RuntimeError) as ctx:
                review_batches.orchestrate_review(
                    run_dir=self.run_dir,
                    driver=driver,
                    batch_size=1,
                    max_workers=2,
                    timeout_sec=5,
                )
            self.assertIn("Review failed", str(ctx.exception))

            # Verify pool_progress was written with quarantined worker
            prog_file = self.run_dir / "reviews/pool_progress.json"
            self.assertTrue(prog_file.is_file())
            prog = review_batches.safe_read_json(prog_file)
            self.assertIn("worker_fail", prog["quarantined_workers"])
            self.assertIn("worker_good", prog["active_workers"])
            self.assertGreaterEqual(prog["completed_tasks"], 1)
            self.assertGreaterEqual(prog["failed_tasks"], 1)
            self.assertIn("note", prog)
            self.assertIn("pid", prog)
        finally:
            review_batches.process_batch = orig_process_batch

    # --- 2. PRESERVE: Fully cached batches do not spawn workers ---
    def test_preserve_cached_batches_skip_worker_init(self) -> None:
        """When all batches have valid verified receipts, worker init is completely skipped."""
        driver = FakeHerdrDriver()

        # Pre-seed frozen draft and verified receipts for 1 batch
        batches_dir = self.run_dir / "reviews/batches/pending_batch_0000"
        batches_dir.mkdir(parents=True, exist_ok=True)

        batch_items = [
            {"index": 0, "file": "test1.json", "path": ["key1"], "source": "사과", "translation": "苹果", "verdict": "approved", "reason": "已校验"},
            {"index": 1, "file": "test2.json", "path": ["key2"], "source": "바나나", "translation": "香蕉", "verdict": "approved", "reason": "已校验"},
            {"index": 2, "file": "test3.json", "path": ["key3"], "source": "원레그", "translation": "单脚人", "verdict": "approved", "reason": "已校验"},
        ]
        input_items = [
            {"index": 0, "file": "test1.json", "path": ["key1"], "source": "사과", "translation": "苹果"},
            {"index": 1, "file": "test2.json", "path": ["key2"], "source": "바나나", "translation": "香蕉"},
            {"index": 2, "file": "test3.json", "path": ["key3"], "source": "원레그", "translation": "单脚人"},
        ]
        input_file = batches_dir / "input.json"
        review_batches.safe_write_json(input_file, input_items)
        inp_sha = review_batches.file_sha256(input_file)

        result_file = batches_dir / "result.json"
        review_batches.safe_write_json(result_file, batch_items)
        res_sha = review_batches.file_sha256(result_file)

        rcpt_file = batches_dir / "receipt.json"
        receipt = {
            "batch_type": "pending",
            "batch_idx": 0,
            "status": "success",
            "agent_name": "cached_agent",
            "pane_id": "cached_pane",
            "attempts": 1,
            "duration_sec": 0.1,
            "input_sha": inp_sha,
            "response_sha": res_sha,
            "timestamp": time.time(),
        }
        review_batches.safe_write_json(rcpt_file, receipt)

        # Run orchestrate_review with batch_size=10 (1 batch total)
        ret = review_batches.orchestrate_review(
            run_dir=self.run_dir,
            driver=driver,
            batch_size=10,
            max_workers=6,
        )
        self.assertEqual(ret, 0)
        # Verify no panes were split and no worker initialized
        self.assertEqual(driver.panes_split, 0)
        self.assertEqual(len(driver.prompts), 0)

        # Verify output files generated correctly
        self.assertTrue((self.run_dir / "reviewed-translations.json").is_file())
        self.assertTrue((self.run_dir / "review.json").is_file())
        self.assertTrue((self.run_dir / "semantic-review-receipt.json").is_file())

    # --- 3. HOLDOUT: Global poison halt on root data tampering ---
    def test_holdout_root_tampering_halts_all_workers(self) -> None:
        """Tampering with diff.json/translations.json/draft triggers global poison halt immediately."""
        driver = FakeHerdrDriver()

        orig_process_batch = review_batches.process_batch

        def mock_tampering_batch(driver, agent_name, pane_id, batch_dir, batch_type, batch_idx, batch_items, **kwargs):
            raise review_batches.RootInputIntegrityError(f"Run diff.json tampered before batch {batch_type}_{batch_idx}")

        review_batches.process_batch = mock_tampering_batch
        try:
            registry_file = self.run_dir / "reviewer_registry.json"
            review_batches.safe_write_json(registry_file, {
                "run_dir": str(self.run_dir.resolve()),
                "workers": [
                    {"agent_name": "w1", "pane_id": "p1"},
                    {"agent_name": "w2", "pane_id": "p2"},
                ],
            })

            with self.assertRaises(RuntimeError) as ctx:
                review_batches.orchestrate_review(
                    run_dir=self.run_dir,
                    driver=driver,
                    batch_size=1,
                    max_workers=2,
                )
            self.assertIn("Global fatal error: Root dataset tampered", str(ctx.exception))
        finally:
            review_batches.process_batch = orig_process_batch

    def test_holdout_root_file_deletion_halts_all_workers(self) -> None:
        """Deleting diff.json raises RootInputIntegrityError and halts the pool."""
        driver = FakeHerdrDriver()

        orig_process_batch = review_batches.process_batch

        def mock_deleting_batch(driver, agent_name, pane_id, batch_dir, batch_type, batch_idx, batch_items, **kwargs):
            # Physically delete diff_file
            diff_file = kwargs.get("diff_file")
            if diff_file and diff_file.is_file():
                diff_file.unlink()
            raise review_batches.RootInputIntegrityError("Root input unavailable: diff.json")

        review_batches.process_batch = mock_deleting_batch
        try:
            registry_file = self.run_dir / "reviewer_registry.json"
            review_batches.safe_write_json(registry_file, {
                "run_dir": str(self.run_dir.resolve()),
                "workers": [
                    {"agent_name": "w1", "pane_id": "p1"},
                    {"agent_name": "w2", "pane_id": "p2"},
                ],
            })

            with self.assertRaises(RuntimeError) as ctx:
                review_batches.orchestrate_review(
                    run_dir=self.run_dir,
                    driver=driver,
                    batch_size=1,
                    max_workers=2,
                )
            self.assertIn("Global fatal error: Root dataset tampered", str(ctx.exception))
        finally:
            review_batches.process_batch = orig_process_batch

    # --- 4. HOLDOUT: Anti-stall rejects blind re-prompting on non-settled states ---
    def test_holdout_anti_blind_prompt_guard(self) -> None:
        """When agent remains working/unknown/blocked, process_batch does not blind re-prompt."""
        for non_settled_state in ["working_stall", "blocked", "busy_custom"]:
            driver = FakeHerdrDriver(agent_behaviors={"stall_agent": non_settled_state})
            if non_settled_state == "busy_custom":
                driver.agent_statuses["stall_agent"] = "waiting_on_tool"

            batch_dir = self.run_dir / f"batch_stall_{non_settled_state}"
            batch_dir.mkdir(parents=True, exist_ok=True)
            draft_file = self.run_dir / "draft.json"
            review_batches.safe_write_json(draft_file, [])
            d_sha = review_batches.file_sha256(draft_file)
            diff_sha = review_batches.file_sha256(self.diff_file)
            trans_sha = review_batches.file_sha256(self.trans_file)

            with self.assertRaises(Exception):
                review_batches.process_batch(
                    driver=driver,
                    agent_name="stall_agent",
                    pane_id="p_stall",
                    batch_dir=batch_dir,
                    batch_type="pending",
                    batch_idx=0,
                    batch_items=[self.pending_items[0]],
                    timeout_sec=1,
                    input_sha_pre="invalid_dummy",
                    diff_file=self.diff_file,
                    diff_sha_pre=diff_sha,
                    trans_file=self.trans_file,
                    trans_sha_pre=trans_sha,
                    draft_file=draft_file,
                    draft_sha_pre=d_sha,
                )
            # Check error receipt is recorded
            err_rcpt_file = batch_dir / "error_receipt.json"
            self.assertTrue(err_rcpt_file.is_file())
            err_data = review_batches.safe_read_json(err_rcpt_file)
            self.assertEqual(err_data["status"], "failed")
            self.assertIn("error_type", err_data)

    def test_train_batch_input_modification_allows_healthy_workers_to_continue(self) -> None:
        """Single batch input.json modification quarantines worker without halting the healthy workers."""
        driver = FakeHerdrDriver()

        processed_batches: list[tuple[int, str]] = []
        batch_lock = threading.Lock()

        orig_process_batch = review_batches.process_batch

        def mock_batch_tamper(driver, agent_name, pane_id, batch_dir, batch_type, batch_idx, batch_items, **kwargs):
            with batch_lock:
                processed_batches.append((batch_idx, agent_name))
            if batch_idx == 0:
                raise ValueError("Batch input.json was modified during review of pending_0000!")
            res_items = [
                {
                    "index": it["index"],
                    "file": it["file"],
                    "path": it["path"],
                    "source": it["source"],
                    "translation": it["translation"],
                    "verdict": "approved",
                    "reason": "OK",
                }
                for it in batch_items
            ]
            receipt = {
                "batch_type": batch_type,
                "batch_idx": batch_idx,
                "status": "success",
                "agent_name": agent_name,
                "pane_id": pane_id,
                "attempts": 1,
                "duration_sec": 0.1,
            }
            return {"batch_type": batch_type, "batch_idx": batch_idx, "items": res_items, "receipt": receipt}

        review_batches.process_batch = mock_batch_tamper
        try:
            registry_file = self.run_dir / "reviewer_registry.json"
            review_batches.safe_write_json(registry_file, {
                "run_dir": str(self.run_dir.resolve()),
                "workers": [
                    {"agent_name": "w1", "pane_id": "p1"},
                    {"agent_name": "w2", "pane_id": "p2"},
                ],
            })

            with self.assertRaises(RuntimeError):
                review_batches.orchestrate_review(
                    run_dir=self.run_dir,
                    driver=driver,
                    batch_size=1,
                    max_workers=2,
                )

            # Healthy worker w2 continued claiming tasks from queue
            self.assertGreater(len(processed_batches), 1)
        finally:
            review_batches.process_batch = orig_process_batch

    def test_pool_progress_status_progression(self) -> None:
        """Verify pool_progress status is recorded as completed on cache, and failed on error/unresolved."""
        driver = FakeHerdrDriver()

        # 1. Verification of failed status when root diff is missing
        missing_run_dir = self.run_dir / "missing_run"
        missing_run_dir.mkdir(parents=True, exist_ok=True)
        ret = review_batches.orchestrate_review(run_dir=missing_run_dir, driver=driver)
        self.assertEqual(ret, 2)
        prog_file = missing_run_dir / "reviews/pool_progress.json"
        self.assertTrue(prog_file.is_file())
        prog = review_batches.safe_read_json(prog_file)
        self.assertEqual(prog["status"], "failed")

    # --- 5. HOLDOUT: Legacy validator fails closed with code 2 ---
    def test_holdout_validate_review_fails_closed(self) -> None:
        import subprocess
        res = subprocess.run([sys.executable, str(ROOT / "scripts/validate_review.py")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("obsolete", res.stderr.lower())

    # --- 6. PRESERVE: Default concurrency is 6 in herdr_translation ---
    def test_preserve_default_concurrency_is_six(self) -> None:
        self.assertEqual(herdr_translation.DEFAULT_CONCURRENCY, 6)


if __name__ == "__main__":
    unittest.main()
