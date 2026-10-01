"""Unit tests for scripts/llc_snapshot.py (stdlib only)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.llc_snapshot import (
    capture_llc_snapshot,
    parse_expanded_assets,
    safe_relative_path,
    unpack_and_verify_asset,
    verify_and_copy_vendor_fonts,
)


class LLCSnapshotTests(unittest.TestCase):
    def test_safe_relative_path(self):
        self.assertEqual(str(safe_relative_path("Font/Context/a.ttf")), "Font/Context/a.ttf")
        for bad in ["../file.txt", "/abs/file", "a/../../b", "C:\\foo", "a\\b"]:
            with self.assertRaises(ValueError):
                safe_relative_path(bad)

    def test_parse_expanded_assets_strict_container_binding(self):
        tag = "2.2.0"
        digest = "a" * 64
        valid_html = f"""
        <ul>
          <li>
            <a href="/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/download/{tag}/LimbusLocalize_B_v2.2.0.zip">LimbusLocalize_B_v2.2.0.zip</a>
            <button aria-label="Copy SHA-256 for LimbusLocalize_B_v2.2.0.zip" value="sha256:{digest}"></button>
          </li>
        </ul>
        """
        asset_name, url, expected_sha = parse_expanded_assets(valid_html, tag)
        self.assertEqual(asset_name, "LimbusLocalize_B_v2.2.0.zip")
        self.assertIn("https://github.com", url)
        self.assertEqual(expected_sha, digest)

    def test_parse_expanded_assets_rejects_unbound_digest(self):
        tag = "2.2.0"
        # Digest is in a different li or has mismatch aria-label
        bad_html = f"""
        <ul>
          <li>
            <a href="/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/download/{tag}/LimbusLocalize_B_v2.2.0.zip">LimbusLocalize_B_v2.2.0.zip</a>
          </li>
          <li>
            <button aria-label="Copy SHA-256 for something_else.zip" value="sha256:{'b' * 64}"></button>
          </li>
        </ul>
        """
        with self.assertRaises(ValueError):
            parse_expanded_assets(bad_html, tag)

    def _make_release_zip(self, files_dict: dict[str, bytes]) -> bytes:
        mem = io.BytesIO()
        prefix = "LimbusCompany_Data/Lang/LLC_zh-CN/"
        with zipfile.ZipFile(mem, "w") as z:
            for rel, data in files_dict.items():
                z.writestr(f"{prefix}{rel}", data)
        return mem.getvalue()

    def test_unpack_and_verify_asset_success(self):
        files = {
            "AbDlg.json": b'{"dataList": [{"id": 1, "desc": "test"}]}',
            "sub/Story.json": b'{"dataList": []}',
        }
        blob = self._make_release_zip(files)
        expected_sha = hashlib.sha256(blob).hexdigest()
        results = unpack_and_verify_asset(blob, expected_sha)
        self.assertEqual(set(results.keys()), {"AbDlg.json", "sub/Story.json"})

    def test_unpack_and_verify_asset_sha_mismatch(self):
        blob = self._make_release_zip({"AbDlg.json": b"{}"})
        with self.assertRaisesRegex(ValueError, "Asset SHA256 mismatch"):
            unpack_and_verify_asset(blob, "0" * 64)

    def test_unpack_and_verify_asset_invalid_json(self):
        blob = self._make_release_zip({"AbDlg.json": b"not json"})
        expected_sha = hashlib.sha256(blob).hexdigest()
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            unpack_and_verify_asset(blob, expected_sha)

    def test_unpack_and_verify_asset_rejects_symlink(self):
        mem = io.BytesIO()
        prefix = "LimbusCompany_Data/Lang/LLC_zh-CN/"
        with zipfile.ZipFile(mem, "w") as z:
            info = zipfile.ZipInfo(f"{prefix}link.json")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "target.json")
        blob = mem.getvalue()
        expected_sha = hashlib.sha256(blob).hexdigest()
        with self.assertRaisesRegex(ValueError, "symlinks are unsupported"):
            unpack_and_verify_asset(blob, expected_sha)

    def test_verify_and_copy_vendor_fonts_skips_gitkeep(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dest_font = Path(temp_dir) / "Font"
            copied = verify_and_copy_vendor_fonts(dest_font)
            self.assertIn("Font/Context/ChineseFont.ttf", copied)
            # Ensure .gitkeep is NOT in copied files
            self.assertNotIn("Font/Title/.gitkeep", copied)
            # Ensure .gitkeep was NOT copied to destination
            self.assertFalse((dest_font / "Title/.gitkeep").exists())
            self.assertTrue((dest_font / "Title").is_dir())
            self.assertTrue((dest_font / "Context/ChineseFont.ttf").is_file())
            self.assertGreater((dest_font / "Context/ChineseFont.ttf").stat().st_size, 0)

    def test_capture_llc_snapshot_end_to_end(self):
        files = {
            "AbDlg.json": b'{"dataList": [{"id": 1, "desc": "test"}]}',
        }
        blob = self._make_release_zip(files)
        expected_sha = hashlib.sha256(blob).hexdigest()
        release_info = {
            "tag": "v2.0.0",
            "html_url": "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/v2.0.0",
            "asset_name": "LimbusLocalize_B_v2.0.0.zip",
            "expected_sha256": expected_sha,
            "source": "test_mock",
        }
        license_text = "MIT License Mock"

        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "llc_out"
            prov = capture_llc_snapshot(
                output_dir=out,
                release_info=release_info,
                license_text=license_text,
                asset_blob=blob,
            )
            self.assertEqual(prov["release"], "v2.0.0")
            self.assertEqual(prov["font_source"], "vendor_llc")
            self.assertTrue((out / "LLC_zh-CN/AbDlg.json").exists())
            self.assertTrue((out / "LLC_zh-CN/Font/Context/ChineseFont.ttf").exists())
            self.assertTrue((out / "LLC_zh-CN/Font/Title").exists())
            # Ensure .gitkeep is never in output directory
            self.assertFalse((out / "LLC_zh-CN/Font/Title/.gitkeep").exists())
            self.assertTrue((out / "LICENSE_UPSTREAM.txt").exists())
            self.assertTrue((out / "provenance.json").exists())


if __name__ == "__main__":
    unittest.main()
