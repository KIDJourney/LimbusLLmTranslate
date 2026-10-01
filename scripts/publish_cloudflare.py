#!/usr/bin/env python3
"""Publish translation releases to Cloudflare R2 and generate public manifest.

Features:
- Dynamically reads packaged build from --run-dir without hardcoded sha or counts.
- Verifies package zip hash and integrity before publishing.
- Eliminates 404 caching at final URL by using a distinct probe query before upload.
- Checks overwrite safety: refuses to overwrite if remote exists with different hash.
- Generates public latest.json adhering to schema_version 1 for compatibility.
- Outputs artifacts to build/public for downstream activation compatibility.
"""

from __future__ import annotations

import argparse
import binascii
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

BUCKET = "limbus-translation-releases"
CUSTOM_DOMAIN = "limbus-cdn.deadfish.win"
USER_AGENT = "LimbusTranslationUpdater/1.0"


def get_token() -> str:
    token_file = os.path.expanduser("~/.claude/secrets/meme_gen/r2_api_token")
    if not os.path.isfile(token_file):
        raise FileNotFoundError(f"Cloudflare token file not found: {token_file}")
    with open(token_file, "r") as f:
        return f.read().strip()


def hash_file(filepath: Path | str) -> str:
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


def crc32_file(filepath: Path | str) -> int:
    with open(filepath, "rb") as f:
        data = f.read()
    return binascii.crc32(data) & 0xFFFFFFFF


def verify_latest_zip(filepath: Path, expected_sha: str, expected_crc: int) -> None:
    print(f"Verifying {filepath}...")
    actual_sha = hash_file(filepath)
    actual_crc = crc32_file(filepath)

    if actual_sha != expected_sha:
        raise ValueError(f"SHA256 mismatch: {actual_sha} != {expected_sha}")
    if actual_crc != expected_crc:
        raise ValueError(f"CRC32 mismatch: {actual_crc} != {expected_crc}")

    with zipfile.ZipFile(filepath, "r") as zf:
        if zf.testzip() is not None:
            raise ValueError("ZipFile is corrupted")
        namelist = zf.namelist()

        has_font = any(n.startswith("build/LLC_zh-CN/Font/") for n in namelist)
        if not has_font:
            raise ValueError("Missing Font folder in zip")
        has_title = any(n.startswith("build/LLC_zh-CN/Font/Title/") for n in namelist)
        if not has_title:
            raise ValueError("Missing Title folder in zip")

        has_valid_font = False
        for info in zf.infolist():
            if info.filename.startswith("build/LLC_zh-CN/Font/") and (
                info.filename.endswith(".ttf") or info.filename.endswith(".otf")
            ):
                if info.file_size > 0:
                    has_valid_font = True
                    break
        if not has_valid_font:
            raise ValueError("Missing valid TTF/OTF font file in Font/Context")

        if "LICENSE_UPSTREAM.txt" not in namelist:
            raise ValueError("Missing LICENSE_UPSTREAM.txt in zip")


def check_remote_object(key: str, expected_sha: str, expected_size: int) -> bool:
    """Probe whether the remote object exists using a distinct probe query to avoid caching 404 at the final URL."""
    probe_url = f"https://{CUSTOM_DOMAIN}/{key}?probe=check&ts={int(time.time())}"
    req = urllib.request.Request(probe_url, method="HEAD")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                print(f"Object exists at remote key {key}. Verifying via GET with probe...")
                # Verify content without polluting final clean URL
                probe_get_req = urllib.request.Request(probe_url)
                probe_get_req.add_header("User-Agent", USER_AGENT)
                with urllib.request.urlopen(probe_get_req, timeout=30) as get_resp:
                    data = get_resp.read()
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha == expected_sha and len(data) == expected_size:
                        print(f"Object at {key} matches expected hash and size. Reusing.")
                        return True
                    else:
                        raise RuntimeError(
                            f"Object at {key} already exists with differing hash/size! Refusing to overwrite."
                        )
            return False
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise RuntimeError(f"HTTP Error probing remote object {key}: {e.code}") from e
    except Exception as e:
        if "Refusing to overwrite" in str(e):
            raise
        # On connection errors, proceed to try upload
        return False


def upload_to_r2(local_path: Path, key: str, token: str) -> bool:
    print(f"Uploading {local_path} to {key}...")
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = token
    env["CLOUDFLARE_ACCOUNT_ID"] = "986c17d0347c33645b6b4958fdb52b23"

    cmd = [
        "npx", "wrangler", "r2", "object", "put", f"{BUCKET}/{key}",
        "--remote",
        "--file", str(local_path),
        "--content-type", "application/zip",
        "--cache-control", "public, max-age=31536000, immutable",
    ]

    try:
        subprocess.run(cmd, env=env, check=True, capture_output=True, text=True, timeout=300)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Upload failed: {e.stderr}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("Upload timed out.", file=sys.stderr)
        return False


def verify_upload(final_url: str, expected_sha: str, expected_size: int) -> bool:
    """Verify final URL via GET only after put has completed."""
    print(f"Verifying final URL: {final_url}...")
    for attempt in range(5):
        try:
            req = urllib.request.Request(final_url)
            req.add_header("User-Agent", USER_AGENT)
            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()
                actual_size = len(data)
                actual_sha = hashlib.sha256(data).hexdigest()

                if actual_size == expected_size and actual_sha == expected_sha:
                    print(f"Verification successful for {final_url}")
                    return True
                else:
                    print(
                        f"Verification mismatch: Size {actual_size}/{expected_size}, SHA {actual_sha[:8]}/{expected_sha[:8]}",
                        file=sys.stderr,
                    )
                    return False
        except urllib.error.HTTPError as e:
            print(f"Attempt {attempt + 1}/5 failed with HTTP {e.code}: {e.reason}", file=sys.stderr)
            if attempt < 4:
                time.sleep(5)
        except Exception as e:
            print(f"Attempt {attempt + 1}/5 failed: {e}", file=sys.stderr)
            if attempt < 4:
                time.sleep(5)
    return False


def create_updater_zip(output_path: Path) -> tuple[str, int]:
    print(f"Creating updater zip at {output_path}...")
    files_to_include = [
        (ROOT / "scripts/Update-LimbusTranslation.bat", "Update-LimbusTranslation.bat"),
        (ROOT / "scripts/Update-LimbusTranslation.ps1", "Update-LimbusTranslation.ps1"),
        (ROOT / "scripts/Install-LimbusTranslation.ps1", "Install-LimbusTranslation.ps1"),
        (ROOT / "README_Updater.md", "README_Updater.md"),
    ]

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path, arcname in files_to_include:
            if not file_path.is_file():
                raise FileNotFoundError(f"Missing required file for updater: {file_path}")

            if file_path.name.endswith(".ps1"):
                content = file_path.read_text(encoding="utf-8")
                with zf.open(arcname, "w") as f_zip:
                    f_zip.write(b"\xef\xbb\xbf")
                    f_zip.write(content.encode("utf-8"))
            else:
                zf.write(file_path, arcname)

    return hash_file(output_path), os.path.getsize(output_path)


def recheck_validation_hashes(run_dir: Path) -> str:
    # 1. Full pipeline validation must pass
    val_proc = subprocess.run([
        sys.executable, str(ROOT / "scripts/translation_pipeline.py"), "validate",
        "--run-dir", str(run_dir)
    ], capture_output=True, text=True)
    if val_proc.returncode != 0:
        raise ValueError(f"Full pipeline validation failed with code {val_proc.returncode}:\n{val_proc.stderr}\n{val_proc.stdout}")

    # 2. All required files must exist strictly; fail if missing
    diff_file = run_dir / "diff.json"
    reviewed_file = run_dir / "reviewed-translations.json"
    review_file = run_dir / "review.json"
    translations_file = run_dir / "translations.json"
    val_file = run_dir / "validation_passed.json"
    source_prov_file = run_dir / "snapshot/source/provenance.json"
    llc_prov_file = run_dir / "snapshot/llc/provenance.json"

    for req_f in [diff_file, reviewed_file, review_file, translations_file, val_file, source_prov_file, llc_prov_file]:
        if not req_f.is_file():
            raise FileNotFoundError(f"Missing required validation artifact: {req_f}")

    actual_diff_hash = hash_file(diff_file)
    actual_reviewed_hash = hash_file(reviewed_file)
    actual_trans_hash = hash_file(translations_file)

    val_data = json.loads(val_file.read_text(encoding="utf-8"))
    if val_data.get("status") != "passed":
        raise ValueError("validation_passed.json status is not passed")
    if val_data.get("reviewed_translations_hash") != actual_reviewed_hash:
        raise ValueError("validation_passed.json hash mismatch with actual reviewed translations")
    if val_data.get("diff_hash") != actual_diff_hash:
        raise ValueError("validation_passed.json diff_hash mismatch with actual diff")
    if val_data.get("translations_hash") != actual_trans_hash:
        raise ValueError("validation_passed.json translations_hash mismatch with actual translations")

    review_report = json.loads(review_file.read_text(encoding="utf-8"))
    hashes = review_report.get("hashes", {})
    if hashes.get("reviewed-translations.json") != actual_reviewed_hash:
        raise ValueError("Review report reviewed-translations.json hash mismatch before publish")
    if hashes.get("translations.json") != actual_trans_hash:
        raise ValueError("Review report translations.json hash mismatch before publish")
    bound_diff_hash = hashes.get("diff.json") or hashes.get("input.json")
    if bound_diff_hash != actual_diff_hash:
        raise ValueError("Review report diff/input hash mismatch before publish")

    return actual_reviewed_hash


def publish(run_dir: Path) -> int:
    # 0. Re-verify validation-bound hashes before doing any network mutation or reading token
    try:
        actual_reviewed_hash = recheck_validation_hashes(run_dir)
    except Exception as exc:
        print(f"Error: Pre-publish validation check failed: {exc}", file=sys.stderr)
        return 2

    # Read latest.json strictly from run_dir/build/latest.json - do NOT fallback to ROOT/build
    latest_file = run_dir / "build/latest.json"
    if not latest_file.is_file():
        print(f"Error: {latest_file} not found. Must not fallback to ROOT/build.", file=sys.stderr)
        return 2

    latest = json.loads(latest_file.read_text(encoding="utf-8"))
    version = latest["version"]
    zip_sha256 = latest["zip_sha256"]
    zip_crc32 = latest["zip_crc32"]

    # Verify metadata consistency against actual provenance and reviewed hash before network
    source_prov = json.loads((run_dir / "snapshot/source/provenance.json").read_text(encoding="utf-8"))
    llc_prov = json.loads((run_dir / "snapshot/llc/provenance.json").read_text(encoding="utf-8"))

    if latest.get("translation_hash") != actual_reviewed_hash:
        print(
            f"Error: latest.json translation_hash ({latest.get('translation_hash')}) != actual reviewed hash ({actual_reviewed_hash})",
            file=sys.stderr,
        )
        return 2
    if latest.get("raw") != source_prov.get("raw_version"):
        print(
            f"Error: latest.json raw ({latest.get('raw')}) != source provenance raw_version ({source_prov.get('raw_version')})",
            file=sys.stderr,
        )
        return 2
    if latest.get("chinese") != llc_prov.get("release"):
        print(
            f"Error: latest.json chinese ({latest.get('chinese')}) != llc provenance release ({llc_prov.get('release')})",
            file=sys.stderr,
        )
        return 2

    # Locate latest.zip strictly from run_dir/build/latest.zip
    zip_path = run_dir / "build/latest.zip"
    if not zip_path.is_file():
        print(f"Error: {zip_path} not found. Must not fallback to ROOT/build.", file=sys.stderr)
        return 2

    # Verify latest.zip
    verify_latest_zip(zip_path, zip_sha256, zip_crc32)
    zip_size = os.path.getsize(zip_path)

    # All local gates passed; now retrieve token for network mutation
    token = get_token()

    # Output directories setup
    run_public_dir = run_dir / "build/public"
    root_public_dir = ROOT / "build/public"
    for p_dir in [run_public_dir, root_public_dir]:
        p_dir.mkdir(parents=True, exist_ok=True)

    # Create updater zip
    temp_updater_path = run_public_dir / "temp_updater.zip"
    updater_sha, updater_size = create_updater_zip(temp_updater_path)
    updater_filename = f"LimbusUpdater-{updater_sha[:12]}.zip"
    updater_path = run_public_dir / updater_filename
    if updater_path.exists():
        updater_path.unlink()
    temp_updater_path.rename(updater_path)
    shutil.copyfile(updater_path, root_public_dir / updater_filename)

    # R2 Object Keys
    lang_key = f"releases/{version}/{zip_sha256[:12]}/LimbusTranslation.zip"
    lang_url = f"https://{CUSTOM_DOMAIN}/{lang_key}?v={zip_sha256[:12]}"

    updater_key = f"updater/{updater_sha[:12]}/LimbusUpdater.zip"
    updater_url = f"https://{CUSTOM_DOMAIN}/{updater_key}?v={updater_sha[:12]}"

    # Upload lang zip
    if not check_remote_object(lang_key, zip_sha256, zip_size):
        if not upload_to_r2(zip_path, lang_key, token):
            return 1
    # Verify final URL via GET only after put
    if not verify_upload(lang_url, zip_sha256, zip_size):
        return 1

    # Upload updater zip
    if not check_remote_object(updater_key, updater_sha, updater_size):
        if not upload_to_r2(updater_path, updater_key, token):
            return 1
    if not verify_upload(updater_url, updater_sha, updater_size):
        return 1

    # Receipts and manifests
    receipt = {
        "lang_zip": {
            "url": lang_url,
            "sha256": zip_sha256,
            "bytes": zip_size,
        },
        "updater_zip": {
            "url": updater_url,
            "sha256": updater_sha,
            "bytes": updater_size,
        },
    }
    for p_dir in [run_public_dir, root_public_dir]:
        (p_dir / "upload-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    # Format raw_version for schema 1 compatibility
    raw_val = latest.get("raw", "")
    source_hash = latest.get("source_hash", "")
    if not raw_val.startswith("windows-sha256:") and source_hash:
        raw_version_formatted = f"windows-sha256:{source_hash}"
    else:
        raw_version_formatted = raw_val

    published_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    public_latest = {
        "schema_version": 1,
        "version": version,
        "published_at": published_at,
        "source": {
            "raw_version": raw_version_formatted,
            "chinese_release": latest["chinese"],
        },
        "package": {
            "url": lang_url,
            "sha256": zip_sha256,
            "size": zip_size,
        },
        "updater": {
            "url": updater_url,
            "sha256": updater_sha,
            "size": updater_size,
        },
        "notes": "社区补译测试版；尚未完成 Windows 游戏内验证",
    }

    for p_dir in [run_public_dir, root_public_dir]:
        (p_dir / "latest.json").write_text(json.dumps(public_latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Publish completed successfully for version: {version}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Publish translation release to Cloudflare")
    parser.add_argument("--run-dir", required=False, help="Run directory")
    args, _ = parser.parse_known_args()

    if not args.run_dir:
        print(
            "Error: Legacy CLI usage without --run-dir is deprecated. Pass --run-dir <run_dir>.",
            file=sys.stderr,
        )
        sys.exit(2)

    run_dir = Path(args.run_dir).resolve()
    code = publish(run_dir)
    sys.exit(code)


if __name__ == "__main__":
    main()
