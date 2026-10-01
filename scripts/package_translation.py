#!/usr/bin/env python3
"""Package translation artifacts for release.

Dynamic release versioning: <LLCtag>.<source_hash_short>.<reviewed_hash_short>.
Preserves fonts, requires snapshot LICENSE_UPSTREAM.txt (fails if missing, does not rely on text_data).
Font source is strictly read from current llc snapshot (no fallback to root/build).
Diff file is strictly required (fails if missing).
Always invokes localization.py build even for 0 pending items.
Re-verifies all input/output hashes bound during validation before packaging.
Explicitly writes directory entries (including empty Font/Title) into ZIP.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import localization


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def file_crc32(path: Path | str) -> int:
    with open(path, "rb") as f:
        buf = f.read()
    return binascii.crc32(buf) & 0xFFFFFFFF


def dir_sha256(dir_path: Path | str) -> str:
    h = hashlib.sha256()
    for root, _, files in sorted(os.walk(dir_path)):
        for file in sorted(files):
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, dir_path)
            h.update(rel_path.encode("utf-8"))
            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
    return h.hexdigest()


def safe_read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, path)


def verify_fonts(font_dir: Path) -> None:
    context_dir = font_dir / "Context"
    title_dir = font_dir / "Title"
    if not context_dir.is_dir() or not title_dir.is_dir():
        raise RuntimeError(f"Font/Context or Font/Title directory is missing in {font_dir}")

    has_font = False
    for f in context_dir.iterdir():
        if f.is_file() and f.suffix.lower() in [".ttf", ".otf"] and f.stat().st_size > 0:
            has_font = True
            break
    if not has_font:
        raise RuntimeError("Font/Context does not contain any non-empty TTF/OTF files.")


def find_upstream_license(llc_snapshot_dir: Path) -> Path:
    candidates = [
        llc_snapshot_dir / "LICENSE_UPSTREAM.txt",
        llc_snapshot_dir / "LICENSE",
        llc_snapshot_dir / "LLC_zh-CN" / "LICENSE",
    ]
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            return c
    raise RuntimeError(
        f"Upstream license missing in snapshot ({llc_snapshot_dir})! "
        "Failing because package cannot rely on deleted text_data repository."
    )


def recheck_validation_hashes(run_dir: Path) -> tuple[str, str, str]:
    # Call full translation_pipeline.validate(run_dir); must return 0
    ret = localization.validate_run_dir if hasattr(localization, "validate_run_dir") else None
    val_code = subprocess.run([
        sys.executable, str(ROOT / "scripts/translation_pipeline.py"), "validate",
        "--run-dir", str(run_dir)
    ], capture_output=True, text=True)
    if val_code.returncode != 0:
        raise ValueError(f"Full pipeline validation failed with code {val_code.returncode}:\n{val_code.stderr}\n{val_code.stdout}")

    # All required files must exist strictly; fail if missing
    diff_file = run_dir / "diff.json"
    reviewed_file = run_dir / "reviewed-translations.json"
    review_file = run_dir / "review.json"
    translations_file = run_dir / "translations.json"
    val_file = run_dir / "validation_passed.json"

    for req_f in [diff_file, reviewed_file, review_file, translations_file, val_file]:
        if not req_f.is_file():
            raise FileNotFoundError(f"Missing required validation artifact: {req_f}")

    actual_diff_hash = file_sha256(diff_file)
    actual_reviewed_hash = file_sha256(reviewed_file)
    actual_trans_hash = file_sha256(translations_file)

    val_data = safe_read_json(val_file)
    if val_data.get("status") != "passed":
        raise ValueError("validation_passed.json status is not passed")
    if val_data.get("reviewed_translations_hash") != actual_reviewed_hash:
        raise ValueError("validation_passed.json hash mismatch with actual reviewed translations")
    if val_data.get("diff_hash") != actual_diff_hash:
        raise ValueError("validation_passed.json diff_hash mismatch with actual diff")
    if val_data.get("translations_hash") != actual_trans_hash:
        raise ValueError("validation_passed.json translations_hash mismatch with actual translations")

    review_report = safe_read_json(review_file)
    hashes = review_report.get("hashes", {})
    if hashes.get("reviewed-translations.json") != actual_reviewed_hash:
        raise ValueError(
            f"Review report reviewed-translations hash mismatch: "
            f"{hashes.get('reviewed-translations.json')} != {actual_reviewed_hash}"
        )
    if hashes.get("translations.json") != actual_trans_hash:
        raise ValueError("Review report translations hash mismatch")

    bound_diff_hash = hashes.get("diff.json") or hashes.get("input.json")
    if bound_diff_hash != actual_diff_hash:
        raise ValueError(
            f"Review report diff/input hash mismatch: {bound_diff_hash} != {actual_diff_hash}"
        )

    return actual_diff_hash, actual_trans_hash, actual_reviewed_hash


def package_release(run_dir: Path) -> int:
    source_prov_file = run_dir / "snapshot/source/provenance.json"
    llc_prov_file = run_dir / "snapshot/llc/provenance.json"
    diff_path = run_dir / "diff.json"
    reviewed_path = run_dir / "reviewed-translations.json"

    if not source_prov_file.is_file() or not llc_prov_file.is_file():
        print(f"Error: Missing snapshot provenance files in {run_dir}", file=sys.stderr)
        return 2

    source_prov = safe_read_json(source_prov_file)
    llc_prov = safe_read_json(llc_prov_file)

    raw_version = source_prov.get("raw_version")
    source_hash = source_prov.get("source_hash")
    llc_release = llc_prov.get("release")

    if not raw_version or not source_hash or not llc_release:
        print("Error: Incomplete provenance metadata for dynamic versioning", file=sys.stderr)
        return 2

    # Re-verify all validation-bound hashes before doing any packaging
    try:
        _, _, review_sha = recheck_validation_hashes(run_dir)
    except Exception as exc:
        print(f"Error: Validation re-verification failed before packaging: {exc}", file=sys.stderr)
        return 2

    source_hash_short = source_hash[:8]
    review_hash_short = review_sha[:8]
    dynamic_version = f"{llc_release}.{source_hash_short}.{review_hash_short}"

    print(f"Packaging dynamic release: {dynamic_version}")

    llc_snapshot_dir = run_dir / "snapshot/llc"
    try:
        license_src = find_upstream_license(llc_snapshot_dir)
    except Exception as exc:
        print(f"Error: License check failed: {exc}", file=sys.stderr)
        return 2

    # Locate Font source strictly from current llc snapshot
    font_src = llc_snapshot_dir / "LLC_zh-CN/Font"
    if not font_src.is_dir():
        print(f"Error: Font source missing in llc snapshot: {font_src}. Do not fallback to ROOT/build.", file=sys.stderr)
        return 2
    verify_fonts(font_src)

    # Build directory setup
    run_build_dir = run_dir / "build"
    root_build_dir = ROOT / "build"
    final_release_dir = run_build_dir / "releases" / dynamic_version / "LLC_zh-CN"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_release_dir = Path(temp_dir) / "LLC_zh-CN"

        # Always run localization.py build even for 0 pending to perform full tree verification
        try:
            subprocess.run([
                sys.executable, str(ROOT / "scripts/localization.py"), "build",
                "--manifest", str(diff_path),
                "--translations", str(reviewed_path),
                "--output", str(temp_release_dir)
            ], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            print(f"localization.py build failed: {e.stderr}", file=sys.stderr)
            return 2

        # Copy fonts into temp_release_dir
        font_dst = temp_release_dir / "Font"
        shutil.copytree(font_src, font_dst, dirs_exist_ok=True)
        verify_fonts(font_dst)

        # Conflict check if final_release_dir already exists
        if final_release_dir.exists():
            new_hash = dir_sha256(temp_release_dir)
            old_hash = dir_sha256(final_release_dir)
            if new_hash == old_hash:
                print(f"Directory {final_release_dir} already exists with identical contents. Reusing.")
            else:
                print(f"Conflict: Directory {final_release_dir} exists with different contents!", file=sys.stderr)
                return 2
        else:
            final_release_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(temp_release_dir, final_release_dir)

    # Write LICENSE_UPSTREAM.txt and NOTICE.txt
    for b_dir in [run_build_dir, root_build_dir]:
        b_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_src, b_dir / "LICENSE_UPSTREAM.txt")
        (b_dir / "NOTICE.txt").write_text(
            "Limbus Company Chinese Translation. Upstream font and text materials belong to LLC under CC BY-NC-SA 4.0.\n",
            encoding="utf-8",
        )

    # Create latest.zip explicitly including directories (especially empty Font/Title)
    temp_zip = run_build_dir / f"temp_{dynamic_version}.zip"
    with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(run_build_dir / "LICENSE_UPSTREAM.txt", "LICENSE_UPSTREAM.txt")
        zipf.write(run_build_dir / "NOTICE.txt", "NOTICE.txt")

        for root, dirs, files in os.walk(final_release_dir):
            for dir_name in dirs:
                dir_path = os.path.join(root, dir_name)
                arcname = os.path.join("build", "LLC_zh-CN", os.path.relpath(dir_path, final_release_dir))
                # Explicitly write directory entry to preserve empty dirs like Font/Title
                zipf.write(dir_path, arcname)

            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.join("build", "LLC_zh-CN", os.path.relpath(file_path, final_release_dir))
                zipf.write(file_path, arcname)

    zip_sha = file_sha256(temp_zip)
    zip_crc = file_crc32(temp_zip)

    # Verify zip integrity immediately
    with zipfile.ZipFile(temp_zip, "r") as zf:
        if zf.testzip() is not None:
            raise RuntimeError("Corrupted zip generated")
        nl = zf.namelist()
        if not any(n.startswith("build/LLC_zh-CN/Font/Title") for n in nl):
            raise RuntimeError("Missing Font/Title directory in packaged zip!")
        if not any(n.startswith("build/LLC_zh-CN/Font/Context") for n in nl):
            raise RuntimeError("Missing Font/Context directory in packaged zip!")
        if "LICENSE_UPSTREAM.txt" not in nl:
            raise RuntimeError("Missing LICENSE_UPSTREAM.txt in packaged zip!")

    # Write to run_build_dir and root_build_dir
    final_zip_name = "latest.zip"
    for b_dir in [run_build_dir, root_build_dir]:
        target_zip = b_dir / final_zip_name
        shutil.copyfile(temp_zip, target_zip)

    if temp_zip.exists():
        temp_zip.unlink()

    meta = {
        "version": dynamic_version,
        "raw": raw_version,
        "chinese": llc_release,
        "source_hash": source_hash,
        "translation_hash": review_sha,
        "zip_sha256": zip_sha,
        "zip_crc32": zip_crc,
    }
    for b_dir in [run_build_dir, root_build_dir]:
        safe_write_json(b_dir / "latest.json", meta)

    safe_write_json(run_dir / "package_receipt.json", {
        "status": "success",
        "version": dynamic_version,
        "zip_sha256": zip_sha,
        "zip_crc32": zip_crc,
        "final_release_dir": str(final_release_dir),
    })

    print(f"Package created successfully: version={dynamic_version}, sha256={zip_sha}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Package Limbus translation into release zip")
    parser.add_argument("--run-dir", required=False, help="Run directory containing snapshots and diff")

    args, unknown = parser.parse_known_args()

    if not args.run_dir:
        print(
            "Error: Legacy CLI usage is deprecated. Migration required: you must pass --run-dir <run_dir>.\n"
            "Hardcoded 2026092802 / sha58f pipeline has been decommissioned.",
            file=sys.stderr,
        )
        sys.exit(2)

    run_dir = Path(args.run_dir).resolve()
    code = package_release(run_dir)
    sys.exit(code)


if __name__ == "__main__":
    main()
