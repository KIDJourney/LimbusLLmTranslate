#!/usr/bin/env python3
"""Thin Stage Dispatcher for Translation and Review.

Dispatches workflow translate/review stages:
- Scans sibling historical runs for a valid, verified baseline with identical LLC tree.
- Persists baseline selection into baseline-selection.json.
- Dispatches to incremental_translation when a valid baseline is found; falls back to full translation.
- Dispatches review stage using strictly the frozen baseline selection, rejecting tampered baselines.
- Generates terminology conflict candidates for pending name/displayName/nickName items as force-review-items.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import herdr_translation as ht
import incremental_review as inc_rev
import incremental_translation as inc_tr
import review_batches as rb

NAME_KEYS = {"name", "displayName", "nickName"}


def safe_read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, path)


def get_current_llc_tree_sha(run_dir: Path) -> str | None:
    llc_dir = run_dir / "snapshot/llc/LLC_zh-CN"
    if not llc_dir.is_dir():
        return None
    try:
        return inc_rev.compute_tree_sha256(llc_dir)
    except Exception:
        return None


def select_baseline(run_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    """Scan sibling historical runs (newest to oldest) for verified baseline with identical LLC."""
    run_dir = run_dir.resolve()
    parent = run_dir.parent
    if not parent.is_dir():
        return None, None

    curr_llc_sha = get_current_llc_tree_sha(run_dir)
    if not curr_llc_sha:
        return None, None

    candidates: list[Path] = []
    for entry in parent.iterdir():
        if entry.is_dir() and entry.resolve() != run_dir:
            # Fast check: skip runs without reviewed translations or semantic success receipt
            sr_file = entry / "semantic-review-receipt.json"
            rev_file = entry / "reviewed-translations.json"
            if sr_file.is_file() and rev_file.is_file():
                candidates.append(entry)

    # Sort descending by directory name (ISO timestamp / run naming: e.g. 20261001-...)
    # Avoid using st_mtime as validating old runs may touch their access/modification times.
    candidates.sort(key=lambda p: p.name, reverse=True)

    for cand in candidates:
        try:
            base_freeze = inc_rev.validate_and_freeze_baseline(cand)
            if base_freeze.get("llc_tree_sha") == curr_llc_sha:
                return cand, base_freeze
        except Exception:
            continue

    return None, None


def generate_terminology_force_review_items(
    run_dir: Path,
) -> list[dict[str, Any]]:
    """Generate force-review-items for pending name/displayName/nickName items with unique LLC TM translations."""
    diff_file = run_dir / "diff.json"
    tm_file = run_dir / "translation_memory.json"
    trans_file = run_dir / "translations.json"

    if not diff_file.is_file() or not tm_file.is_file() or not trans_file.is_file():
        return []

    diff_data = safe_read_json(diff_file)
    pending_items = diff_data.get("pending", [])
    translations = safe_read_json(trans_file)
    tm_data = safe_read_json(tm_file)

    records = tm_data.get("records", [])
    by_source = tm_data.get("by_source", {})

    # Build translation lookup by item key
    trans_map: dict[tuple[str, str], str] = {}
    for t in translations:
        p_val = t.get("path")
        if p_val is None:
            p_val = t.get("json_path", [])
        key = (t.get("file", ""), json.dumps(p_val, sort_keys=True))
        trans_map[key] = t.get("translation", "")

    force_items: list[dict[str, Any]] = []

    for item in pending_items:
        path_list = item.get("path", [])
        if not path_list:
            continue
        last_step = path_list[-1]
        if last_step not in NAME_KEYS:
            continue

        src = item.get("source", "").strip()
        if not src or src not in by_source:
            continue

        record_indices = by_source[src]
        unique_llc_trans = sorted(
            {records[i]["translation"].strip() for i in record_indices if records[i].get("translation")}
        )

        if len(unique_llc_trans) == 1:
            canonical_trans = unique_llc_trans[0]
            item_key = (item.get("file", ""), json.dumps(path_list, sort_keys=True))
            curr_trans = trans_map.get(item_key, "").strip()

            if curr_trans and curr_trans != canonical_trans:
                force_items.append({
                    "file": item.get("file"),
                    "path": path_list,
                    "source": item.get("source"),
                    "current_translation": curr_trans,
                    "target_terminology": canonical_trans,
                    "reason": "terminology_conflict",
                    "note": f"Candidate terminology mismatch with LLC canonical term '{canonical_trans}'",
                })

    return force_items


def dispatch_translate(run_dir: Path, args: argparse.Namespace) -> int:
    run_dir = run_dir.resolve()
    baseline_selection_file = run_dir / "baseline-selection.json"

    baseline_dir, baseline_freeze = select_baseline(run_dir)

    if baseline_dir is not None and baseline_freeze is not None:
        print(f"[CycleStage] Selected verified baseline: {baseline_dir}")
        selection_data = {
            "strategy": "incremental",
            "baseline_path": str(baseline_dir),
            "baseline_freeze": baseline_freeze,
            "llc_tree_sha": baseline_freeze.get("llc_tree_sha"),
        }
        safe_write_json(baseline_selection_file, selection_data)
        inc_tr.run_incremental_translation(
            run_dir=run_dir,
            baseline_run_dir=baseline_dir,
            concurrency=getattr(args, "concurrency", 6),
            timeout_sec=getattr(args, "timeout", 1200),
        )
        return 0
    else:
        print("[CycleStage] No verified baseline found matching current LLC tree. Falling back to full translation.")
        selection_data = {
            "strategy": "full",
            "baseline_path": None,
            "baseline_freeze": None,
            "llc_tree_sha": get_current_llc_tree_sha(run_dir),
        }
        safe_write_json(baseline_selection_file, selection_data)
        return ht.run_translation(
            run_dir=run_dir,
            concurrency=getattr(args, "concurrency", 6),
            timeout_sec=getattr(args, "timeout", 1200),
        )


def dispatch_review(run_dir: Path, args: argparse.Namespace) -> int:
    run_dir = run_dir.resolve()
    baseline_selection_file = run_dir / "baseline-selection.json"

    if not baseline_selection_file.is_file():
        print("[CycleStage] No baseline selection found; falling back to full review.")
        return rb.orchestrate_review(
            run_dir=run_dir,
            max_workers=getattr(args, "max_workers", 6),
            timeout_sec=getattr(args, "timeout", 1800),
        )

    selection_data = safe_read_json(baseline_selection_file)
    strategy = selection_data.get("strategy")

    if strategy != "incremental":
        print("[CycleStage] Baseline selection strategy is not incremental. Running full review.")
        return rb.orchestrate_review(
            run_dir=run_dir,
            max_workers=getattr(args, "max_workers", 6),
            timeout_sec=getattr(args, "timeout", 1800),
        )

    baseline_path_str = selection_data.get("baseline_path")
    if not baseline_path_str:
        raise ValueError("[CycleStage] Invalid baseline-selection.json: strategy is incremental but baseline_path is empty")

    baseline_dir = Path(baseline_path_str).resolve()
    # Re-validate baseline hash and LLC strictly. Do not silently change baseline!
    current_base_freeze = inc_rev.validate_and_freeze_baseline(baseline_dir)
    frozen_base = selection_data.get("baseline_freeze", {})

    required_freeze_keys = [
        "reviewed_translations_sha",
        "diff_sha",
        "review_json_sha",
        "semantic_receipt_sha",
        "llc_tree_sha",
    ]
    for check_key in required_freeze_keys:
        if check_key not in frozen_base:
            raise RuntimeError(
                f"[CycleStage] Frozen baseline metadata incomplete! Missing required key: {check_key}"
            )
        if check_key not in current_base_freeze:
            raise RuntimeError(
                f"[CycleStage] Baseline validation missing key: {check_key}"
            )
        if current_base_freeze[check_key] != frozen_base[check_key]:
            raise RuntimeError(
                f"[CycleStage] Baseline tamper detected! Key {check_key} mismatch: "
                f"expected {frozen_base[check_key]}, got {current_base_freeze[check_key]}"
            )

    curr_llc_sha = get_current_llc_tree_sha(run_dir)
    if curr_llc_sha != current_base_freeze["llc_tree_sha"]:
        raise RuntimeError(
            f"[CycleStage] Current LLC tree ({curr_llc_sha}) does not match baseline LLC tree ({current_base_freeze['llc_tree_sha']})"
        )

    # Generate terminology force review items
    force_items = generate_terminology_force_review_items(run_dir)
    force_path = None
    if force_items:
        force_path = run_dir / "force_review_items.json"
        safe_write_json(force_path, force_items)
        print(f"[CycleStage] Generated {len(force_items)} terminology force-review items.")

    return inc_rev.run_incremental_review(
        run_dir=run_dir,
        baseline_run_dir=baseline_dir,
        max_workers=getattr(args, "max_workers", 6),
        force_review_items_path=force_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Thin cycle stage dispatcher")
    subparsers = parser.add_subparsers(dest="stage", required=True)

    trans_p = subparsers.add_parser("translate", help="Dispatch translation stage")
    trans_p.add_argument("--run-dir", type=Path, required=True, help="Workflow run directory")
    trans_p.add_argument("--concurrency", type=int, default=6, help="Translation concurrency")
    trans_p.add_argument("--timeout", type=int, default=1200, help="Per-shard timeout in seconds")

    rev_p = subparsers.add_parser("review", help="Dispatch review stage")
    rev_p.add_argument("--run-dir", type=Path, required=True, help="Workflow run directory")
    rev_p.add_argument("--max-workers", type=int, default=6, help="Review worker concurrency")
    rev_p.add_argument("--timeout", type=int, default=1800, help="Review timeout in seconds")

    args = parser.parse_args()
    if args.stage == "translate":
        return dispatch_translate(args.run_dir, args)
    elif args.stage == "review":
        return dispatch_review(args.run_dir, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
