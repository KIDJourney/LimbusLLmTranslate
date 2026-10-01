"""Tests for release run isolation and command_receipt WORKFLOW_RUN_DIR injection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class ReleaseRunIsolationTests(unittest.TestCase):
    def test_activate_release_missing_run_dir_fails(self):
        """Must fail if neither --run-dir nor WORKFLOW_RUN_DIR is supplied (no fallback to root)."""
        env = {k: v for k, v in os.environ.items() if k != "WORKFLOW_RUN_DIR"}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/activate_release.py")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Refusing to fallback to root", proc.stderr)

    def test_check_publish_missing_run_dir_fails(self):
        """Must fail if neither --run-dir nor WORKFLOW_RUN_DIR is supplied (no fallback to root)."""
        env = {k: v for k, v in os.environ.items() if k != "WORKFLOW_RUN_DIR"}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_publish.py")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Refusing to fallback to root", proc.stderr)

    def test_cross_run_isolation_reads_correct_run(self):
        """Verify that when run_a and run_b exist, activate_release strictly reads run_b."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            run_a = base / "run_a"
            run_b = base / "run_b"

            (run_a / "build/public").mkdir(parents=True)
            (run_b / "build/public").mkdir(parents=True)

            latest_a = {
                "schema_version": 1,
                "version": "v1.0.0.a",
                "package": {"url": "https://limbus-cdn.deadfish.win/a.zip", "sha256": "sha_a", "size": 10},
                "updater": {"url": "https://limbus-cdn.deadfish.win/up_a.zip", "sha256": "0123456789012345", "size": 10},
            }
            latest_b = {
                "schema_version": 1,
                "version": "v1.0.0.b",
                "package": {"url": "https://limbus-cdn.deadfish.win/b.zip", "sha256": "sha_b", "size": 20},
                "updater": {"url": "https://limbus-cdn.deadfish.win/up_b.zip", "sha256": "9876543210987654", "size": 20},
            }

            (run_a / "build/public/latest.json").write_text(json.dumps(latest_a), encoding="utf-8")
            (run_b / "build/public/latest.json").write_text(json.dumps(latest_b), encoding="utf-8")

            receipt_b = {
                "lang_zip": {"url": "https://limbus-cdn.deadfish.win/b.zip", "sha256": "sha_b", "bytes": 20},
                "updater_zip": {"url": "https://limbus-cdn.deadfish.win/up_b.zip", "sha256": "9876543210987654", "bytes": 20},
            }
            (run_b / "build/public/upload-receipt.json").write_text(json.dumps(receipt_b), encoding="utf-8")

            # Missing updater zip in run_b must fail without reading run_a
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts/activate_release.py"), "--run-dir", str(run_b)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("LimbusUpdater-987654321098.zip", proc.stderr + proc.stdout)

    def test_run_py_command_receipt_injects_env(self):
        """Verify command_receipt in run.py injects WORKFLOW_RUN_DIR into environment."""
        sys.path.insert(0, str(ROOT / ".dev-workflow"))
        import run

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run_test"
            attempt_dir = run_dir / "attempt-01"
            run_dir.mkdir(parents=True)
            attempt_dir.mkdir(parents=True)

            state = {
                "workspace": str(ROOT),
                "run_dir": str(run_dir),
                "current_node": "test_node",
            }
            item = {
                "command": [sys.executable, "-c", "import os; print(os.environ.get('WORKFLOW_RUN_DIR', 'NONE'))"],
            }
            passed, receipt = run.command_receipt(item, {}, state, attempt_dir, "action-01")
            self.assertTrue(passed)
            stdout_content = Path(receipt["stdout"]).read_text(encoding="utf-8").strip()
            self.assertEqual(stdout_content, str(run_dir))

    def test_herdr_move_pane_to_new_tab(self):
        """Verify HerdrDriver.move_pane_to_new_tab executes expected CLI schema."""
        from scripts.herdr_translation import HerdrDriver
        driver = HerdrDriver(herdr_bin="herdr_mock", session="default")

        mock_response = {
            "status": "ok",
            "result": {
                "move_result": {
                    "pane": {
                        "pane_id": "pane_123"
                    }
                }
            }
        }
        with patch.object(driver, "run_cmd", return_value=mock_response) as mock_run:
            new_pid = driver.move_pane_to_new_tab("pane_orig", label="shard_01")
            self.assertEqual(new_pid, "pane_123")
            mock_run.assert_called_once_with(
                ["pane", "move", "pane_orig", "--new-tab", "--label", "shard_01", "--no-focus"],
                timeout=30,
            )

    def test_herdr_wait_agent_and_continuation_and_registry(self):
        """Verify wait_agent CLI call, load_agent_registry validation, and validate_shard_output."""
        from scripts.herdr_translation import (
            HerdrDriver,
            load_agent_registry,
            validate_shard_output,
        )

        # 1. wait_agent schema
        driver = HerdrDriver(herdr_bin="herdr_mock", session="default")
        with patch.object(driver, "run_cmd", return_value={"ok": True}) as mock_run:
            driver.wait_agent("agent_xyz", until=["idle", "done"], timeout_ms=10000)
            mock_run.assert_called_once_with(
                ["agent", "wait", "agent_xyz", "--timeout", "10000", "--until", "idle", "--until", "done"],
                timeout=25,
            )

        # 2. load_agent_registry validation
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            run_dir = base / "run1"
            run_dir.mkdir()
            reg_file = base / "registry.json"

            valid_sha = "a" * 64
            reg_data = {
                "run_dir": str(run_dir),
                "agents": {
                    "shard_00": {
                        "agent_name": "tr_shard_00_9fd7a465",
                        "pane_id": "wH:p3",
                        "input_sha": valid_sha,
                    }
                }
            }
            reg_file.write_text(json.dumps(reg_data), encoding="utf-8")

            # Mismatched run_dir must fail
            other_run = base / "other_run"
            other_run.mkdir()
            with self.assertRaises(ValueError):
                load_agent_registry(reg_file, other_run)

            # Matched run_dir succeeds
            loaded = load_agent_registry(reg_file, run_dir)
            self.assertIn("shard_00", loaded)
            self.assertEqual(loaded["shard_00"]["agent_name"], "tr_shard_00_9fd7a465")
            self.assertEqual(loaded["shard_00"]["pane_id"], "wH:p3")
            self.assertEqual(loaded["shard_00"]["input_sha"], valid_sha)

            # Invalid input_sha (non-64-hex) must fail
            reg_data_bad_sha = {
                "run_dir": str(run_dir),
                "agents": {
                    "shard_00": {
                        "agent_name": "tr_shard_00_9fd7a465",
                        "pane_id": "wH:p3",
                        "input_sha": "not_64_hex",
                    }
                }
            }
            bad_reg_file = base / "bad_registry.json"
            bad_reg_file.write_text(json.dumps(reg_data_bad_sha), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_agent_registry(bad_reg_file, run_dir)

            # 3. validate_shard_output
            inp_file = run_dir / "input.json"
            out_file = run_dir / "translations.json"
            inp_items = [{"file": "test.json", "path": ["a", 0], "source": "안녕"}]
            out_items = [{"file": "test.json", "path": ["a", 0], "source": "안녕", "translation": "你好"}]
            inp_file.write_text(json.dumps(inp_items), encoding="utf-8")
            out_file.write_text(json.dumps(out_items), encoding="utf-8")

            valid = validate_shard_output(inp_file, out_file, "shard_00")
            self.assertEqual(len(valid), 1)

            # Tampered source must fail
            out_items_tampered = [{"file": "test.json", "path": ["a", 0], "source": "篡改", "translation": "你好"}]
            out_file.write_text(json.dumps(out_items_tampered), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_shard_output(inp_file, out_file, "shard_00")

            # 4. update_run_registry atomic persistence
            from scripts.herdr_translation import update_run_registry
            lock = threading.Lock()
            reg_dyn = base / "dyn_reg.json"
            update_run_registry(reg_dyn, "shard_01", "tr_01", "wH:p5", valid_sha, lock)
            self.assertTrue(reg_dyn.is_file())
            dyn_data = json.loads(reg_dyn.read_text(encoding="utf-8"))
            self.assertEqual(dyn_data["agents"]["shard_01"]["agent_name"], "tr_01")
            self.assertEqual(dyn_data["agents"]["shard_01"]["pane_id"], "wH:p5")
            self.assertEqual(dyn_data["agents"]["shard_01"]["input_sha"], valid_sha)


if __name__ == "__main__":
    unittest.main()
