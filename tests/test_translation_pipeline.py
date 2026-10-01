#!/usr/bin/env python3
"""Comprehensive tests for the translation pipeline, Herdr driver, packaging, and workflow."""

from __future__ import annotations

import binascii
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import herdr_translation as ht
import package_translation as pt
import publish_cloudflare as pc
import translation_pipeline as tp


class TestTranslationPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.run_dir = self.workspace / "test_run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_mock_snapshots(self, run_dir: Path, raw_ver: str = "l20261001_abc", source_hash: str = "1122334455667788", llc_ver: str = "v20261001", include_license: bool = True, include_fonts: bool = True) -> tuple[Path, Path]:
        source_dir = run_dir / "snapshot/source"
        llc_dir = run_dir / "snapshot/llc"
        source_dir.mkdir(parents=True, exist_ok=True)
        llc_dir.mkdir(parents=True, exist_ok=True)

        source_prov = {
            "source_kind": "windows_ssh",
            "raw_version": raw_ver,
            "source_hash": source_hash,
        }
        (source_dir / "provenance.json").write_text(json.dumps(source_prov), encoding="utf-8")

        llc_prov = {
            "release": llc_ver,
            "asset_sha256": "aabbccdd",
        }
        (llc_dir / "provenance.json").write_text(json.dumps(llc_prov), encoding="utf-8")

        if include_license:
            (llc_dir / "LICENSE_UPSTREAM.txt").write_text("CC-BY-NC-SA-4.0 upstream license text\n", encoding="utf-8")

        # Mock source KR and LLC_zh-CN directories
        kr_dir = source_dir / "KR"
        cn_dir = llc_dir / "LLC_zh-CN"
        kr_dir.mkdir(parents=True, exist_ok=True)
        cn_dir.mkdir(parents=True, exist_ok=True)

        if include_fonts:
            font_dir = cn_dir / "Font"
            (font_dir / "Context").mkdir(parents=True, exist_ok=True)
            (font_dir / "Title").mkdir(parents=True, exist_ok=True)
            (font_dir / "Context" / "Pretendard-Regular.otf").write_bytes(b"mock font binary data")

        # Add a dummy JSON file
        dummy_kr = {"data": [{"id": 1, "name": "테스트", "desc": "안녕"}]}
        dummy_cn = {"data": [{"id": 1, "name": "测试"}]}  # desc is missing in CN
        (kr_dir / "test.json").write_text(json.dumps(dummy_kr), encoding="utf-8")
        (cn_dir / "test.json").write_text(json.dumps(dummy_cn), encoding="utf-8")

        return source_dir, llc_dir

    def test_herdr_driver_json_fixtures(self):
        """Verify HerdrDriver parses real JSON structures and avoids guessing or regex fallbacks."""
        driver = ht.HerdrDriver(herdr_bin="herdr", session="default")

        # 1. Test workspace create parsing
        ws_mock_output = json.dumps({
            "ok": True,
            "result": {
                "workspace": {"workspace_id": "w1", "label": "limbus-test"},
                "tab": {"tab_id": "w1:t1"},
                "root_pane": {"pane_id": "w1:p1"}
            }
        })
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=ws_mock_output, stderr="")
            ws_id, root_pane = driver.create_workspace(cwd=ROOT, label="limbus-test")
            self.assertEqual(ws_id, "w1")
            self.assertEqual(root_pane, "w1:p1")
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("--session", called_cmd)
            self.assertIn("default", called_cmd)
            self.assertIn("workspace", called_cmd)
            self.assertIn("create", called_cmd)

        # 2. Test pane split parsing
        split_mock_output = json.dumps({
            "ok": True,
            "result": {
                "pane": {"pane_id": "w1:p2"}
            }
        })
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=split_mock_output, stderr="")
            new_pane = driver.split_pane(target_pane_id="w1:p1", cwd=ROOT, direction="down")
            self.assertEqual(new_pane, "w1:p2")
            called_cmd = mock_run.call_args[0][0]
            self.assertIn("--pane", called_cmd)
            self.assertIn("w1:p1", called_cmd)
            self.assertIn("--direction", called_cmd)
            self.assertIn("down", called_cmd)

        # 3. Test agent status parsing
        agent_get_output = json.dumps({
            "ok": True,
            "result": {
                "agent": {
                    "agent_id": "ag1",
                    "agent_status": "done"
                }
            }
        })
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=agent_get_output, stderr="")
            status = driver.get_agent_status("my_agent")
            self.assertEqual(status, "done")

    def test_prepare_up_to_date_exit_10(self):
        """When live published manifest matches both source and LLC versions, exit 10 immediately."""
        self._setup_mock_snapshots(self.run_dir, raw_ver="windows-sha256:11223344", source_hash="11223344", llc_ver="v20261001")

        mock_online_manifest = {
            "schema_version": 1,
            "version": "v20261001.11223344.aabbccdd",
            "source": {
                "raw_version": "windows-sha256:11223344",
                "chinese_release": "v20261001"
            }
        }

        with patch.object(tp, "run_snapshot_clis") as mock_clis, \
             patch.object(tp, "get_live_published_manifest", return_value=mock_online_manifest):
            mock_clis.return_value = (
                {"source_kind": "windows_ssh", "raw_version": "windows-sha256:11223344", "source_hash": "11223344"},
                {"release": "v20261001", "asset_sha256": "aabbccdd"}
            )
            code = tp.prepare(self.run_dir)
            self.assertEqual(code, 10)
            status = json.loads((self.run_dir / "status.json").read_text())
            self.assertEqual(status.get("status"), "up_to_date")

    def test_prepare_new_version_produces_shards_and_diff(self):
        """When version differs from online, prepare runs diff and creates shards."""
        self._setup_mock_snapshots(self.run_dir, raw_ver="windows-sha256:999999", source_hash="999999", llc_ver="v20261002")

        mock_online_manifest = {
            "schema_version": 1,
            "version": "v20261001.11223344.aabbccdd",
            "source": {
                "raw_version": "windows-sha256:11223344",
                "chinese_release": "v20261001"
            }
        }

        with patch.object(tp, "run_snapshot_clis") as mock_clis, \
             patch.object(tp, "get_live_published_manifest", return_value=mock_online_manifest):
            mock_clis.return_value = (
                {"source_kind": "windows_ssh", "raw_version": "windows-sha256:999999", "source_hash": "999999"},
                {"release": "v20261002", "asset_sha256": "aabbccdd"}
            )
            code = tp.prepare(self.run_dir)
            self.assertEqual(code, 0)
            self.assertTrue((self.run_dir / "diff.json").is_file())
            self.assertTrue((self.run_dir / "shards_manifest.json").is_file())
            self.assertTrue((self.run_dir / "shards/shard_00/input.json").is_file())
            self.assertTrue((self.run_dir / "shards/shard_00/prompt.txt").is_file())

    def test_validate_passes_and_blocks_tampering_and_unresolved(self):
        """Comprehensive verification of validation: valid flow, source tampering, missing items, and review dispositions."""
        diff_data = {
            "source": "/mock/kr",
            "chinese": "/mock/cn",
            "summary": {"pending_fields": 2, "review_fields": 1, "parse_errors": 0},
            "pending": [
                {"file": "test.json", "path": ["data", 0, "name"], "source": "테스트1", "current": None},
                {"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "current": None}
            ],
            "review": [
                {"file": "test.json", "path": ["data", 0, "rawDesc"], "source": "더미", "current": "测试", "reason": "internal_or_dummy_review"}
            ],
            "errors": []
        }
        diff_file = self.run_dir / "diff.json"
        diff_file.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")
        diff_hash = tp.file_sha256(diff_file)

        # Set up shards manifest
        shard_dir = self.run_dir / "shards/shard_00"
        shard_dir.mkdir(parents=True, exist_ok=True)
        shard_input = shard_dir / "input.json"
        shard_input.write_text(json.dumps(diff_data["pending"], ensure_ascii=False, indent=2), encoding="utf-8")
        shards_manifest = {
            "diff_hash": diff_hash,
            "total_pending": 2,
            "total_review": 1,
            "shards_count": 1,
            "shards": [
                {
                    "shard_id": "shard_00",
                    "directory": str(shard_dir),
                    "items_count": 2,
                    "input_hash": tp.file_sha256(shard_input)
                }
            ]
        }
        (self.run_dir / "shards_manifest.json").write_text(json.dumps(shards_manifest), encoding="utf-8")

        # Good translations
        translations = [
            {"file": "test.json", "path": ["data", 0, "name"], "source": "테스트1", "translation": "测试1"},
            {"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "translation": "你好"}
        ]
        trans_file = self.run_dir / "translations.json"
        trans_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
        trans_hash = tp.file_sha256(trans_file)

        reviewed_file = self.run_dir / "reviewed-translations.json"
        reviewed_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
        reviewed_hash = tp.file_sha256(reviewed_file)

        review_data = {
            "hashes": {
                "diff.json": diff_hash,
                "translations.json": trans_hash,
                "reviewed-translations.json": reviewed_hash
            },
            "dispositions": [
                {
                    "file": "test.json",
                    "path": ["data", 0, "rawDesc"],
                    "reason": "internal_or_dummy_review",
                    "source": "더미",
                    "action": "internal_dummy_ignored",
                    "resolution_note": "Internal dummy text ignored intentionally.",
                    "resolved": True
                }
            ]
        }
        review_file = self.run_dir / "review.json"
        review_file.write_text(json.dumps(review_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 1. Validation passes
        self.assertEqual(tp.validate(self.run_dir), 0)
        self.assertTrue((self.run_dir / "validation_passed.json").is_file())

        # 2. Block on source tampering
        tampered_translations = copy.deepcopy(translations)
        tampered_translations[0]["source"] = "篡改的原文"
        reviewed_file.write_text(json.dumps(tampered_translations, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(tp.validate(self.run_dir), 2)

        # Restore reviewed_file
        reviewed_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3. Block on unresolved review disposition
        unresolved_review = copy.deepcopy(review_data)
        unresolved_review["dispositions"][0]["resolved"] = False
        unresolved_review["dispositions"][0]["action"] = "needs_translation"
        review_file.write_text(json.dumps(unresolved_review, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(tp.validate(self.run_dir), 2)

        # 4. Block on missing review disposition
        empty_dispositions = copy.deepcopy(review_data)
        empty_dispositions["dispositions"] = []
        review_file.write_text(json.dumps(empty_dispositions, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(tp.validate(self.run_dir), 2)

    def test_package_dynamic_version_and_zip(self):
        """Test package_translation generates dynamic version and includes Font/Title directory."""
        self._setup_mock_snapshots(self.run_dir, raw_ver="l20261001", source_hash="11223344", llc_ver="v20261001")

        # Prepare diff, reviewed translations, review report
        diff_data = {
            "source": str(self.run_dir / "snapshot/source/KR"),
            "chinese": str(self.run_dir / "snapshot/llc/LLC_zh-CN"),
            "summary": {"pending_fields": 1, "review_fields": 0, "parse_errors": 0},
            "pending": [
                {"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "current": None}
            ],
            "review": [],
            "errors": []
        }
        diff_file = self.run_dir / "diff.json"
        diff_file.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2), encoding="utf-8")

        translations = [
            {"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "translation": "你好"}
        ]
        translations_file = self.run_dir / "translations.json"
        translations_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")

        reviewed_file = self.run_dir / "reviewed-translations.json"
        reviewed_file.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")

        s_dir = self.run_dir / "shards/shard_00"
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "input.json").write_text(json.dumps(diff_data["pending"], ensure_ascii=False, indent=2), encoding="utf-8")

        shards_manifest = {
            "diff_hash": tp.file_sha256(diff_file),
            "shards": [
                {
                    "shard_id": "shard_00",
                    "directory": str(s_dir),
                    "input_hash": tp.file_sha256(s_dir / "input.json"),
                    "items_count": 1
                }
            ]
        }
        (self.run_dir / "shards_manifest.json").write_text(json.dumps(shards_manifest), encoding="utf-8")

        review_file = self.run_dir / "review.json"
        review_file.write_text(json.dumps({
            "hashes": {
                "diff.json": tp.file_sha256(diff_file),
                "translations.json": tp.file_sha256(translations_file),
                "reviewed-translations.json": tp.file_sha256(reviewed_file)
            },
            "dispositions": []
        }), encoding="utf-8")

        # Run validate first to produce validation_passed.json
        val_code = tp.validate(self.run_dir)
        self.assertEqual(val_code, 0)

        code = pt.package_release(self.run_dir)
        self.assertEqual(code, 0)

        # Check latest.json
        run_latest_json = self.run_dir / "build/latest.json"
        self.assertTrue(run_latest_json.is_file())
        meta = json.loads(run_latest_json.read_text())
        expected_version = f"v20261001.11223344.{tp.file_sha256(reviewed_file)[:8]}"
        self.assertEqual(meta["version"], expected_version)

        # Check latest.zip content: must include empty Title folder entry
        zip_path = self.run_dir / "build/latest.zip"
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path, "r") as zf:
            nl = zf.namelist()
            self.assertIn("LICENSE_UPSTREAM.txt", nl)
            self.assertIn("NOTICE.txt", nl)
            self.assertTrue(any("Font/Title" in name for name in nl), f"Font/Title missing from {nl}")
            self.assertTrue(any("Font/Context" in name for name in nl), f"Font/Context missing from {nl}")

    def test_package_fails_if_license_missing(self):
        """package_translation fails if upstream LICENSE_UPSTREAM.txt is missing in snapshot."""
        self._setup_mock_snapshots(self.run_dir, include_license=False)

        diff_file = self.run_dir / "diff.json"
        diff_file.write_text(json.dumps({"pending": [], "review": []}), encoding="utf-8")
        reviewed_file = self.run_dir / "reviewed-translations.json"
        reviewed_file.write_text(json.dumps([]), encoding="utf-8")
        review_file = self.run_dir / "review.json"
        review_file.write_text(json.dumps({
            "hashes": {
                "diff.json": tp.file_sha256(diff_file),
                "reviewed-translations.json": tp.file_sha256(reviewed_file)
            },
            "dispositions": []
        }), encoding="utf-8")

        code = pt.package_release(self.run_dir)
        self.assertEqual(code, 2)

    def test_publish_strict_pre_publish_validation_failure(self):
        """publish fails before get_token/network if validation artifacts missing or modified."""
        self._setup_mock_snapshots(self.run_dir, raw_ver="l20261001", source_hash="11223344", llc_ver="v20261001")

        # Set up a fake get_token that raises if reached
        orig_get_token = pc.get_token
        def fail_if_token_called():
            raise AssertionError("get_token() must NOT be called if validation fails!")
        pc.get_token = fail_if_token_called

        try:
            diff_data = {
                "source": str(self.run_dir / "snapshot/source/KR"),
                "chinese": str(self.run_dir / "snapshot/llc/LLC_zh-CN"),
                "summary": {"pending_fields": 1, "review_fields": 0, "parse_errors": 0},
                "pending": [{"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "current": None}],
                "review": [],
                "errors": []
            }
            diff_file = self.run_dir / "diff.json"
            diff_file.write_text(json.dumps(diff_data), encoding="utf-8")

            translations = [{"file": "test.json", "path": ["data", 0, "desc"], "source": "안녕", "translation": "你好"}]
            (self.run_dir / "translations.json").write_text(json.dumps(translations), encoding="utf-8")
            (self.run_dir / "reviewed-translations.json").write_text(json.dumps(translations), encoding="utf-8")

            s_dir = self.run_dir / "shards/shard_00"
            s_dir.mkdir(parents=True, exist_ok=True)
            (s_dir / "input.json").write_text(json.dumps(diff_data["pending"]), encoding="utf-8")
            (self.run_dir / "shards_manifest.json").write_text(json.dumps({
                "diff_hash": tp.file_sha256(diff_file),
                "shards": [{"shard_id": "shard_00", "directory": str(s_dir), "input_hash": tp.file_sha256(s_dir / "input.json"), "items_count": 1}]
            }), encoding="utf-8")

            (self.run_dir / "review.json").write_text(json.dumps({
                "hashes": {
                    "diff.json": tp.file_sha256(diff_file),
                    "translations.json": tp.file_sha256(self.run_dir / "translations.json"),
                    "reviewed-translations.json": tp.file_sha256(self.run_dir / "reviewed-translations.json"),
                },
                "dispositions": []
            }), encoding="utf-8")

            # 1. Missing validation_passed.json fails publish
            code = pc.publish(self.run_dir)
            self.assertEqual(code, 2)

            # Generate validation_passed.json
            self.assertEqual(tp.validate(self.run_dir), 0)
            self.assertEqual(pt.package_release(self.run_dir), 0)

            # 2. Missing review.json fails publish
            review_file = self.run_dir / "review.json"
            review_content = review_file.read_text(encoding="utf-8")
            review_file.unlink()
            self.assertEqual(pc.publish(self.run_dir), 2)
            review_file.write_text(review_content, encoding="utf-8")

            # 3. Tampered translations.json fails publish
            orig_trans = (self.run_dir / "translations.json").read_text(encoding="utf-8")
            (self.run_dir / "translations.json").write_text(json.dumps([]), encoding="utf-8")
            self.assertEqual(pc.publish(self.run_dir), 2)
            (self.run_dir / "translations.json").write_text(orig_trans, encoding="utf-8")

            # 4. Tampered provenance fails publish (meta mismatch)
            prov_file = self.run_dir / "snapshot/source/provenance.json"
            orig_prov = prov_file.read_text(encoding="utf-8")
            prov_file.write_text(json.dumps({"source_kind": "windows_ssh", "raw_version": "tampered", "source_hash": "tampered"}), encoding="utf-8")
            self.assertEqual(pc.publish(self.run_dir), 2)
            prov_file.write_text(orig_prov, encoding="utf-8")

        finally:
            pc.get_token = orig_get_token

    def test_legacy_cli_migration_error(self):
        """Invoking package or publish without --run-dir must report migration error and exit 2."""
        res_pkg = subprocess.run([sys.executable, str(ROOT / "scripts/package_translation.py")], capture_output=True, text=True)
        self.assertEqual(res_pkg.returncode, 2)
        self.assertIn("Legacy CLI usage is deprecated", res_pkg.stderr)

        res_pub = subprocess.run([sys.executable, str(ROOT / "scripts/publish_cloudflare.py")], capture_output=True, text=True)
        self.assertEqual(res_pub.returncode, 2)
        self.assertIn("Legacy CLI usage without --run-dir is deprecated", res_pub.stderr)

    def test_workflow_execution_exit_10(self):
        """Run through .dev-workflow runner where prepare returns 10; workflow routes to $complete directly."""
        with tempfile.TemporaryDirectory() as wf_tmp:
            wf_path = Path(wf_tmp)
            wf_json_file = wf_path / "wf.json"

            # Workflow with prepare returning 10 -> $complete
            wf_def = {
                "version": 2,
                "name": "Exit10 Test",
                "entry": "prepare",
                "agent": {"backend": "codex-sdk"},
                "nodes": [
                    {
                        "id": "prepare",
                        "type": "command",
                        "exit_routes": {"10": "$complete"},
                        "actions": [
                            {"command": [sys.executable, "-c", "import sys; sys.exit(10)"]}
                        ],
                        "on": {"passed": "translate", "failed": "$failed"}
                    },
                    {
                        "id": "translate",
                        "type": "command",
                        "actions": [
                            {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}
                        ],
                        "on": {"passed": "$complete", "failed": "$failed"}
                    }
                ]
            }
            wf_json_file.write_text(json.dumps(wf_def))

            run_res = subprocess.run(
                [sys.executable, str(ROOT / ".dev-workflow/run.py"), "start", "--workflow", str(wf_json_file), "--workspace", str(wf_path), "--request", "test"],
                capture_output=True, text=True
            )
            self.assertEqual(run_res.returncode, 0)
            runs_dir = wf_path / ".workflow-runs"
            latest_run = max([p for p in runs_dir.iterdir() if (p / "state.json").exists()], key=lambda p: p.name)
            state = json.loads((latest_run / "state.json").read_text())
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["history"]), 1)
            self.assertEqual(state["history"][0]["target"], "$complete")


if __name__ == "__main__":
    unittest.main()
