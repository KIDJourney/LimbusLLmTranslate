#!/usr/bin/env python3
import collections
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.herdr_translation as herdr_tr
import scripts.translation_memory as tm
import scripts.localization as localization

class MockHerdrDriver:
    """Mock HerdrDriver that records calls and simulates agents."""
    def __init__(self):
        self.started_agents = []
        self.split_pane_called = False
        self.prompt_calls = []
        self.closed_panes = []
        self.agent_statuses = {}
        self.agent_outputs = {}
        self.agents_info = {}

    def split_pane(self, target_pane_id: str, cwd: Path, direction: str) -> str:
        self.split_pane_called = True
        return "mock_pane_split_1"

    def move_pane_to_new_tab(self, pane_id: str, label: str) -> str:
        return "mock_pane_tab_1"

    def start_claude_agent(self, agent_name: str, pane_id: str, model: str, settings_path: str):
        self.started_agents.append({
            "agent_name": agent_name,
            "pane_id": pane_id,
            "model": model,
            "settings_path": settings_path,
        })
        self.agent_statuses[agent_name] = "idle"
        self.agents_info[agent_name] = {
            "agent_name": agent_name,
            "pane_id": pane_id,
            "status": "idle"
        }

    def prompt_agent(self, agent_name: str, prompt: str, timeout_sec: int):
        self.prompt_calls.append({"agent_name": agent_name, "prompt": prompt})

    def get_agent_status(self, agent_name: str) -> str:
        return self.agent_statuses.get(agent_name, "idle")

    def get_agent_info(self, agent_name: str) -> dict[str, Any]:
        return self.agents_info.get(agent_name, {
            "agent_name": agent_name,
            "pane_id": "mock_pane_tab_1",
            "status": "idle"
        })

    def read_agent_output(self, agent_name: str, lines: int = 150) -> str:
        return self.agent_outputs.get(agent_name, "output ok")

    def close_pane(self, pane_id: str):
        self.closed_panes.append(pane_id)


class TestReceiptAndTMIndependent(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_receipt_"))
        self.addCleanup(lambda: shutil.rmtree(self.test_dir, ignore_errors=True))

    def _setup_shard_files(self, tamper_type=None):
        s_dir = self.test_dir / "shards" / "shard_001"
        s_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir = self.test_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        input_data = [
            {"file": "test.json", "path": ["data", 0, "content"], "source": "안녕"}
        ]
        input_file = s_dir / "input.json"
        herdr_tr.safe_write_json(input_file, input_data)
        input_hash = herdr_tr.file_sha256(input_file)

        prompt_file = s_dir / "prompt.txt"
        prompt_file.write_text("translate this", encoding="utf-8")

        glossary_file = s_dir / "glossary.json"
        herdr_tr.safe_write_json(glossary_file, {})

        context_file = s_dir / "context.json"
        herdr_tr.safe_write_json(context_file, {"ctx": "ok"})
        context_hash = herdr_tr.file_sha256(context_file)

        index_file = self.test_dir / "index.json"
        herdr_tr.safe_write_json(index_file, {"idx": "ok"})
        index_hash = herdr_tr.file_sha256(index_file)

        output_data = [
            {"file": "test.json", "path": ["data", 0, "content"], "source": "안녕", "translation": "你好"}
        ]
        output_file = s_dir / "translations.json"
        herdr_tr.safe_write_json(output_file, output_data)
        out_hash = herdr_tr.file_sha256(output_file)

        shard = {
            "shard_id": "shard_001",
            "directory": str(s_dir),
            "input_hash": input_hash,
            "context_hash": context_hash,
            "index_file": "index.json",
            "index_hash": index_hash,
        }

        reference_files = {
            str(context_file): context_hash,
            str(index_file): index_hash,
            str(prompt_file): herdr_tr.file_sha256(prompt_file),
            str(glossary_file): herdr_tr.file_sha256(glossary_file),
            str(input_file): input_hash,
        }

        receipt_file = s_dir / "translation_context_receipt.json"
        herdr_tr.safe_write_json(receipt_file, {
            "references": reference_files,
            "output_sha256": out_hash,
        })
        attempt_file = s_dir / "translation_context_attempt.json"
        herdr_tr.safe_write_json(attempt_file, reference_files)

        return shard, s_dir, evidence_dir, reference_files, output_file, context_file, input_file

    def test_1_valid_receipt_reused_no_agent_started(self):
        """有效receipt复用不启动agent"""
        shard, s_dir, evidence_dir, _, output_file, _, _ = self._setup_shard_files()
        driver = MockHerdrDriver()

        res = herdr_tr.execute_translation_shard(
            shard=shard,
            driver=driver,
            root_pane_id="root_1",
            model="gemini",
            settings_path="dummy_settings.json",
            timeout_sec=10,
            evidence_dir=evidence_dir,
        )

        self.assertEqual(res["status"], "success")
        self.assertTrue(res["reused"])
        self.assertEqual(len(driver.started_agents), 0)
        self.assertFalse(driver.split_pane_called)

    def test_2_modified_context_or_hash_rejected(self):
        """修改context/hash拒绝"""
        shard, s_dir, evidence_dir, _, output_file, context_file, _ = self._setup_shard_files()
        driver = MockHerdrDriver()

        # Tamper context file content
        herdr_tr.safe_write_json(context_file, {"ctx": "tampered"})

        with self.assertRaises(ValueError) as ctx:
            herdr_tr.execute_translation_shard(
                shard=shard,
                driver=driver,
                root_pane_id="root_1",
                model="gemini",
                settings_path="dummy_settings.json",
                timeout_sec=10,
                evidence_dir=evidence_dir,
            )
        self.assertIn("Translation reference modified", str(ctx.exception))
        self.assertEqual(len(driver.started_agents), 0)

    def test_3_reference_change_old_output_no_receipt_backed_up_not_reused(self):
        """reference变化旧output无receipt不复用而隔离备份"""
        shard, s_dir, evidence_dir, _, output_file, context_file, _ = self._setup_shard_files()
        driver = MockHerdrDriver()

        # Update context hash in shard definition to reflect reference change
        herdr_tr.safe_write_json(context_file, {"ctx": "v2_content"})
        new_ctx_hash = herdr_tr.file_sha256(context_file)
        shard["context_hash"] = new_ctx_hash

        # Remove or invalidate receipt so receipt does not match
        receipt_file = s_dir / "translation_context_receipt.json"
        if receipt_file.is_file():
            receipt_file.unlink()

        # Also provide agent output when agent runs
        def mock_prompt(agent_name, prompt, timeout_sec):
            # Write new output
            herdr_tr.safe_write_json(output_file, [
                {"file": "test.json", "path": ["data", 0, "content"], "source": "안녕", "translation": "你好v2"}
            ])
        driver.prompt_agent = mock_prompt

        res = herdr_tr.execute_translation_shard(
            shard=shard,
            driver=driver,
            root_pane_id="root_1",
            model="gemini",
            settings_path="dummy_settings.json",
            timeout_sec=10,
            evidence_dir=evidence_dir,
        )

        self.assertEqual(res["status"], "success")
        self.assertFalse(res["reused"])
        self.assertEqual(len(driver.started_agents), 1)

        # Check backup file exists
        backups = list(s_dir.glob("translations.pre-context-*.json"))
        self.assertGreaterEqual(len(backups), 1, "Expected isolated backup file for old output")
        old_data = herdr_tr.safe_read_json(backups[0])
        self.assertEqual(old_data[0]["translation"], "你好")

    def test_4_registry_and_reference_inconsistent_rejected(self):
        """registry与reference不一致拒绝"""
        shard, s_dir, evidence_dir, _, output_file, context_file, _ = self._setup_shard_files()
        driver = MockHerdrDriver()

        # Simulate agent registry having shard_001
        registry = {
            "shard_001": {
                "agent_name": "tr_shard_001_agent",
                "pane_id": "pane_existing_1",
                "input_sha": shard["input_hash"]
            }
        }

        # Modify shard context so reference_files changes
        herdr_tr.safe_write_json(context_file, {"ctx": "new_reference"})
        shard["context_hash"] = herdr_tr.file_sha256(context_file)

        with self.assertRaises(ValueError) as ctx:
            herdr_tr.execute_translation_shard(
                shard=shard,
                driver=driver,
                root_pane_id="root_1",
                model="gemini",
                settings_path="dummy_settings.json",
                timeout_sec=10,
                evidence_dir=evidence_dir,
                agent_registry=registry,
            )
        self.assertIn("Registered translation has different or missing reference identity", str(ctx.exception))

    def test_5_runtime_context_tampering_never_succeeds(self):
        """运行中context篡改不得success"""
        shard, s_dir, evidence_dir, _, output_file, context_file, _ = self._setup_shard_files()
        driver = MockHerdrDriver()

        # Delete receipt so it runs agent
        receipt_file = s_dir / "translation_context_receipt.json"
        if receipt_file.is_file():
            receipt_file.unlink()
        if output_file.is_file():
            output_file.unlink()

        def mock_prompt_tamper(agent_name, prompt, timeout_sec):
            # Tamper context during runtime
            herdr_tr.safe_write_json(context_file, {"ctx": "tampered_during_runtime"})
            # Also write output
            herdr_tr.safe_write_json(output_file, [
                {"file": "test.json", "path": ["data", 0, "content"], "source": "안녕", "translation": "你好"}
            ])
        driver.prompt_agent = mock_prompt_tamper

        with self.assertRaises(ValueError) as ctx:
            herdr_tr.execute_translation_shard(
                shard=shard,
                driver=driver,
                root_pane_id="root_1",
                model="gemini",
                settings_path="dummy_settings.json",
                timeout_sec=10,
                evidence_dir=evidence_dir,
            )
        self.assertIn("Translation reference modified", str(ctx.exception))
        # Context receipt must not be written on failure
        self.assertFalse(receipt_file.is_file())


if __name__ == "__main__":
    unittest.main()
