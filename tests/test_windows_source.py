"""Unit tests for scripts/windows_source.py (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from scripts.windows_source import (
    DEFAULT_REMOTE_PATH,
    DEFAULT_SSH_HOST,
    MANIFEST_ENTRY_NAME,
    build_powershell_script,
    capture_windows_kr_stream,
    capture_windows_source,
    extract_and_validate_archive,
    normalize_kr_rel_path,
    safe_relative_path,
)


class WindowsSourceTests(unittest.TestCase):
    def test_safe_relative_path(self):
        self.assertEqual(str(safe_relative_path("foo/bar.json")), "foo/bar.json")
        for bad in ["../file.json", "/abs/file.json", "a/../../b.json", "C:\\foo", "a\\b"]:
            with self.assertRaises(ValueError):
                safe_relative_path(bad)

    def test_normalize_kr_rel_path_strip_prefix(self):
        self.assertEqual(normalize_kr_rel_path("KR_AbDlg.json"), "AbDlg.json")
        self.assertEqual(normalize_kr_rel_path("AbDlg.json"), "AbDlg.json")
        self.assertEqual(normalize_kr_rel_path("sub/folder/KR_Story.json"), "sub/folder/Story.json")
        self.assertEqual(
            normalize_kr_rel_path(r"RPGSystem\rpg-dialogue-text-data-floor-7-c2.json"),
            "RPGSystem/rpg-dialogue-text-data-floor-7-c2.json",
        )

    def test_normalize_kr_rel_path_reject_non_json_or_empty(self):
        for bad in ["KR_something.txt", "KR_.json", "KR_test.png", ""]:
            with self.assertRaises(ValueError):
                normalize_kr_rel_path(bad)

    def test_build_powershell_script_structure(self):
        script = build_powershell_script(r"F:\SteamLibrary\Test Path")
        self.assertIn("F:\\SteamLibrary\\Test Path", script)
        self.assertIn("ReparsePoint", script)
        self.assertIn("MUTATION_DETECTED", script)
        self.assertIn(MANIFEST_ENTRY_NAME, script)

    def _make_zip(self, files_dict: dict[str, bytes], include_manifest: bool = True) -> bytes:
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w") as z:
            manifest = {}
            for rel_name, data in files_dict.items():
                z.writestr(rel_name, data)
                manifest[rel_name] = hashlib.sha256(data).hexdigest()
            if include_manifest:
                z.writestr(MANIFEST_ENTRY_NAME, json.dumps(manifest))
        return mem.getvalue()

    def test_extract_and_validate_archive_success(self):
        files_dict = {
            "KR_One.json": b'{"dataList": [{"id": 1}]}',
            "sub/KR_Two.json": b'{"dataList": [{"id": 2}]}',
            "sub/Three.json": b'{"dataList": [{"id": 3}]}',
        }
        zip_bytes = self._make_zip(files_dict)
        results, content_hash = extract_and_validate_archive(zip_bytes)
        self.assertEqual(set(results.keys()), {"One.json", "sub/Two.json", "sub/Three.json"})
        self.assertEqual(len(content_hash), 64)

    def test_extract_and_validate_archive_missing_manifest(self):
        zip_bytes = self._make_zip({"KR_One.json": b"{}"}, include_manifest=False)
        with self.assertRaisesRegex(ValueError, "Missing mandatory remote manifest"):
            extract_and_validate_archive(zip_bytes)

    def test_extract_and_validate_archive_hash_mismatch(self):
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w") as z:
            z.writestr("KR_One.json", b'{"id": 1}')
            z.writestr(MANIFEST_ENTRY_NAME, json.dumps({"KR_One.json": "00000000000000000000000000000000"}))
        with self.assertRaisesRegex(ValueError, "Hash mismatch"):
            extract_and_validate_archive(mem.getvalue())

    def test_extract_and_validate_archive_duplicate_normalized_name(self):
        files_dict = {
            "KR_One.json": b'{"id": 1}',
            "One.json": b'{"id": 2}',
        }
        zip_bytes = self._make_zip(files_dict)
        with self.assertRaisesRegex(ValueError, "Duplicate normalized path"):
            extract_and_validate_archive(zip_bytes)

    def test_extract_and_validate_archive_invalid_json(self):
        files_dict = {"KR_One.json": b"not valid json"}
        zip_bytes = self._make_zip(files_dict)
        with self.assertRaisesRegex(ValueError, "Invalid JSON in file"):
            extract_and_validate_archive(zip_bytes)

    def test_capture_windows_kr_stream_timeouts_and_errors(self):
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 5
            mock_proc.stderr = b"REPARSE_POINT_TARGET: F:\\SteamLibrary"
            mock_run.return_value = mock_proc
            with self.assertRaisesRegex(RuntimeError, "REPARSE_POINT_TARGET"):
                capture_windows_kr_stream()

        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 6
            mock_proc.stderr = b"MUTATION_DETECTED_HASH_CHANGED"
            mock_run.return_value = mock_proc
            with self.assertRaisesRegex(RuntimeError, "MUTATION_DETECTED"):
                capture_windows_kr_stream()

    def test_capture_windows_source_end_to_end_in_memory(self):
        files_dict = {
            "KR_Test.json": b'{"dataList": [{"id": 100, "name": "\xed\x95\x98\xeb\x82\x98"}]}'
        }
        zip_bytes = self._make_zip(files_dict)
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "source"
            prov = capture_windows_source(output_dir=out, zip_bytes=zip_bytes)
            self.assertEqual(prov["source_kind"], "windows_ssh")
            self.assertEqual(prov["total_files"], 1)
            self.assertTrue((out / "KR/Test.json").exists())
            self.assertTrue((out / "provenance.json").exists())
            # Ensure out exists and duplicate call fails
            with self.assertRaisesRegex(ValueError, "Output directory already exists"):
                capture_windows_source(output_dir=out, zip_bytes=zip_bytes)


if __name__ == "__main__":
    unittest.main()
