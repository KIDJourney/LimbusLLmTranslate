#!/usr/bin/env python3
"""Final Targeted Terminology Verification Pass.

Verifies targeted terminology anomalies against verified LLC references without
touching unrelated translations.
Reuses process_batch from scripts.review_batches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from herdr_translation import (
    HerdrDriver,
    file_sha256,
    safe_read_json,
    safe_write_json,
    wait_agent_until_settled,
)
from review_batches import process_batch

BATCH_SIZE = 50


def safe_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(text, encoding="utf-8")
    os.replace(temp_file, path)


def load_llc_evidence(evidence_file: Path) -> dict[str, Any]:
    if not evidence_file.is_file():
        raise FileNotFoundError(f"LLC evidence file not found: {evidence_file}")
    return safe_read_json(evidence_file)


def extract_targeted_candidates(
    reviewed_items: list[dict[str, Any]],
    llc_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Filter candidates strictly by specified terminology anomalies.

    Rule:
    1. source contains '원레그' AND translation contains ('独腿' or '单腿人')
    2. source contains '간수' AND translation contains '狱卒'
    Attach original global index and exact llc_reference evidence.
    """
    candidates = []

    oneleg_ev = llc_evidence.get("원레그", {})
    warden_ev = llc_evidence.get("간수", {})

    oneleg_ref = {
        "standard_term": "单脚人",
        "evidence_summary": "LLC Chinese standard: '원레그' -> '单脚人' (Enemies id1483, RPGSystem N104011)",
        "samples": oneleg_ev.get("top_name_matches", []),
    }
    warden_ref = {
        "standard_term": "看守",
        "evidence_summary": "LLC Chinese standard: '간수' -> '看守' (Warden NPC N999997/N999998/N999999)",
        "samples": warden_ev.get("top_name_matches", []),
    }

    for idx, item in enumerate(reviewed_items):
        source = item.get("source", "")
        translation = item.get("translation", "")
        matched_ref = None

        if "원레그" in source and ("独腿" in translation or "单腿人" in translation):
            matched_ref = oneleg_ref
        elif "간수" in source and "狱卒" in translation:
            matched_ref = warden_ref

        if matched_ref is not None:
            candidates.append({
                "index": idx,
                "file": item.get("file"),
                "path": item.get("path"),
                "source": source,
                "translation": translation,
                "llc_reference": matched_ref,
            })

    return candidates


def backup_existing_artifacts(run_dir: Path, backup_dir: Path) -> dict[str, str]:
    """Backup original reviewed-translations.json, review.json, and semantic-review-receipt.json."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed_up = {}

    files_to_backup = [
        "reviewed-translations.json",
        "review.json",
        "semantic-review-receipt.json",
    ]
    for fname in files_to_backup:
        src = run_dir / fname
        if src.is_file():
            dst = backup_dir / fname
            shutil.copy2(src, dst)
            backed_up[fname] = file_sha256(dst)
            print(f"[Backup] {fname} -> {dst} (sha256: {backed_up[fname][:12]}...)")
        else:
            print(f"[Backup] {fname} not found in {run_dir}, skipping.")

    return backed_up


def run_targeted_terminology_pass(
    run_dir: Path,
    agent_name: str | None = None,
    pane_id: str | None = None,
    timeout_sec: int = 600,
    batch_size: int = BATCH_SIZE,
    driver: HerdrDriver | None = None,
) -> int:
    if driver is None:
        driver = HerdrDriver()

    # 1. Path setup and verification
    diff_file = run_dir / "diff.json"
    trans_file = run_dir / "translations.json"
    draft_file = run_dir / "draft_translations.json"
    reviewed_file = run_dir / "reviewed-translations.json"
    review_json_file = run_dir / "review.json"
    semantic_receipt_file = run_dir / "semantic-review-receipt.json"
    evidence_file = run_dir / "review_agent/llc-terminology-evidence.json"
    registry_file = run_dir / "reviewer_registry.json"

    if not reviewed_file.is_file():
        raise FileNotFoundError(f"reviewed-translations.json not found in {run_dir}")
    if not diff_file.is_file() or not trans_file.is_file() or not draft_file.is_file():
        raise FileNotFoundError("Missing baseline diff/translations/draft files in run directory.")

    root_diff_sha = file_sha256(diff_file)
    root_trans_sha = file_sha256(trans_file)
    draft_trans_sha = file_sha256(draft_file)
    reviewed_pre_sha = file_sha256(reviewed_file)

    # 2. Check and verify reviewer registry and agent identity
    if not registry_file.is_file() and not (agent_name and pane_id):
        raise ValueError("Missing reviewer_registry.json and no explicit --agent-name and --pane-id provided.")

    if agent_name and pane_id:
        info = driver.get_agent_info(agent_name)
        live_pane = info.get("pane_id")
        if live_pane != pane_id:
            raise ValueError(f"Agent pane mismatch for {agent_name}: specified {pane_id} != live {live_pane}")
    else:
        reg_data = safe_read_json(registry_file)
        agent_name = reg_data.get("agent_name")
        pane_id = reg_data.get("pane_id")
        if not agent_name or not pane_id:
            raise ValueError("Corrupt reviewer registry: missing agent_name or pane_id")
        info = driver.get_agent_info(agent_name)
        live_pane = info.get("pane_id")
        if live_pane != pane_id:
            raise ValueError(f"Agent pane mismatch from registry: registered {pane_id} != live {live_pane}")

    # Ensure agent is settled before starting
    agent_status = driver.get_agent_status(agent_name)
    if agent_status not in {"idle", "done"}:
        print(f"[TermPass] Waiting for reviewer {agent_name} to settle (current: {agent_status})...")
        agent_status = wait_agent_until_settled(driver, agent_name, deadline_ts=time.time() + 120, poll_interval_sec=2)
        if agent_status not in {"idle", "done"}:
            raise RuntimeError(f"Reviewer {agent_name} not settled before term pass (status: {agent_status})")

    # 3. Read LLC evidence and filter candidates
    llc_evidence = load_llc_evidence(evidence_file)
    reviewed_items: list[dict[str, Any]] = safe_read_json(reviewed_file)
    candidates = extract_targeted_candidates(reviewed_items, llc_evidence)

    evidence_backup_dir = run_dir / "evidence/term_check_pre_pass"
    backup_existing_artifacts(run_dir, evidence_backup_dir)

    batches_dir = run_dir / "reviews/batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    # If no candidates, write honest empty receipt and exit cleanly
    if not candidates:
        print("[TermPass] No targeted terminology drift found in reviewed translations.")
        empty_term_report = {
            "status": "success",
            "candidates_found": 0,
            "batches_executed": 0,
            "corrections_applied": 0,
            "pre_sha256": reviewed_pre_sha,
            "post_sha256": reviewed_pre_sha,
            "timestamp": time.time(),
        }
        safe_write_json(run_dir / "term_check_receipt.json", empty_term_report)
        return 0

    print(f"[TermPass] Found {len(candidates)} candidate items requiring targeted terminology review.")

    # 4. Partition candidates into batches (<= 50) and review using process_batch
    num_batches = math.ceil(len(candidates) / batch_size)
    term_check_receipts = []
    corrected_items: list[dict[str, Any]] = []
    unresolved_items: list[dict[str, Any]] = []

    for b_idx in range(num_batches):
        chunk = candidates[b_idx * batch_size : (b_idx + 1) * batch_size]
        b_dir = batches_dir / f"termcheck_{b_idx:04d}"

        temp_chunk_file = b_dir / "temp_input.json"
        safe_write_json(temp_chunk_file, chunk)
        chunk_sha = file_sha256(temp_chunk_file)
        temp_chunk_file.unlink(missing_ok=True)

        res = process_batch(
            driver=driver,
            agent_name=agent_name,
            pane_id=pane_id,
            batch_dir=b_dir,
            batch_type="pending",
            batch_idx=b_idx,
            batch_items=chunk,
            timeout_sec=timeout_sec,
            input_sha_pre=chunk_sha,
            diff_file=diff_file,
            diff_sha_pre=root_diff_sha,
            trans_file=trans_file,
            trans_sha_pre=root_trans_sha,
            draft_file=draft_file,
            draft_sha_pre=draft_trans_sha,
        )

        term_check_receipts.append(res["receipt"])
        for it in res["items"]:
            verdict = it.get("verdict")
            if verdict == "unresolved":
                unresolved_items.append(it)
            elif verdict == "corrected":
                corrected_items.append(it)

    # 5. Check unresolved items (strictly blocking)
    if unresolved_items:
        print(f"[TermPass] Error: {len(unresolved_items)} items unresolved during terminology pass!", file=sys.stderr)
        safe_write_json(run_dir / "unresolved_termcheck_items.json", unresolved_items)
        return 2

    # 6. Apply corrections by file and path into reviewed-translations
    corrections_map: dict[tuple[str, str], str] = {}
    for it in corrected_items:
        key = (it.get("file", ""), json.dumps(it.get("path"), sort_keys=True))
        corrections_map[key] = it.get("translation", "")

    new_reviewed_items = []
    applied_count = 0
    for it in reviewed_items:
        key = (it.get("file", ""), json.dumps(it.get("path"), sort_keys=True))
        if key in corrections_map:
            it_copy = dict(it)
            it_copy["translation"] = corrections_map[key]
            new_reviewed_items.append(it_copy)
            applied_count += 1
        else:
            new_reviewed_items.append(it)

    safe_write_json(reviewed_file, new_reviewed_items)
    reviewed_post_sha = file_sha256(reviewed_file)
    print(f"[TermPass] Applied {applied_count} terminology corrections to reviewed-translations.json.")

    # 7. Update review.json hashes
    if review_json_file.is_file():
        review_data = safe_read_json(review_json_file)
        if "hashes" in review_data:
            review_data["hashes"]["reviewed-translations.json"] = reviewed_post_sha
        safe_write_json(review_json_file, review_data)
        print(f"[TermPass] Updated review.json with reviewed-translations.json SHA: {reviewed_post_sha}")

    # 8. Update semantic-review-receipt.json preserving original receipts
    if semantic_receipt_file.is_file():
        sem_data = safe_read_json(semantic_receipt_file)
        sem_data["reviewed_translations_pre_term_sha"] = reviewed_pre_sha
        sem_data["reviewed_translations_sha"] = reviewed_post_sha
        sem_data["term_check_applied"] = applied_count
        sem_data["term_check_receipts"] = term_check_receipts
        safe_write_json(semantic_receipt_file, sem_data)
        print("[TermPass] Updated semantic-review-receipt.json with term check receipts.")

    # Write dedicated pass summary
    term_pass_summary = {
        "status": "success",
        "candidates_found": len(candidates),
        "batches_executed": num_batches,
        "corrections_applied": applied_count,
        "pre_sha256": reviewed_pre_sha,
        "post_sha256": reviewed_post_sha,
        "term_check_receipts": term_check_receipts,
        "timestamp": time.time(),
    }
    safe_write_json(run_dir / "term_check_receipt.json", term_pass_summary)

    print(
        f"[TermPass] Completed successfully: {applied_count}/{len(candidates)} items corrected, "
        f"new SHA: {reviewed_post_sha[:12]}..."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Targeted Terminology Verification Pass")
    parser.add_argument("--run-dir", type=Path, required=True, help="Workflow run directory")
    parser.add_argument("--agent-name", type=str, default=None, help="Verified reviewer agent name")
    parser.add_argument("--pane-id", type=str, default=None, help="Verified reviewer pane ID")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch size (default: 50)")
    parser.add_argument("--timeout", type=int, default=600, help="Batch review timeout in seconds")

    args = parser.parse_args()

    try:
        return run_targeted_terminology_pass(
            run_dir=args.run_dir,
            agent_name=args.agent_name,
            pane_id=args.pane_id,
            batch_size=args.batch_size,
            timeout_sec=args.timeout,
        )
    except Exception as exc:
        print(f"[TermPass] Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
