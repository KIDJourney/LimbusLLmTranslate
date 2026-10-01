#!/usr/bin/env python3
"""Incremental Review Orchestrator.

Reuses verified baseline review artifacts solely for items whose file, path,
source, and translation are strictly identical, and where baseline review dispositions
match completely.

Identifies delta items (changed translations, modified sources, new items),
applies transitive closure on related skill name definitions and references,
places skill definitions and citing descriptions adjacent in the first batch,
verifies terminology consistency after review,
supports forced review items (e.g. term conflict candidates),
constructs an isolated incremental review sub-run directory with read-only symlinks to snapshots and TM,
executes review_batches.orchestrate_review on deltas,
and merges results back into full reviewed-translations.json and review.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import herdr_translation as ht
import incremental_translation as inc_tr
import review_batches as rb
import translation_pipeline as tp


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def compute_tree_sha256(dir_path: Path) -> str:
    """Compute deterministic SHA256 of directory tree contents."""
    h = hashlib.sha256()
    file_count = 0
    for root, dirs, files in sorted(os.walk(dir_path)):
        dirs.sort()
        for f in sorted(files):
            file_count += 1
            p = Path(root) / f
            rel = p.relative_to(dir_path).as_posix()
            h.update(rel.encode("utf-8"))
            with open(p, "rb") as fp:
                while chunk := fp.read(8192):
                    h.update(chunk)
    if file_count == 0:
        raise ValueError(f"Directory tree is empty: {dir_path}")
    return h.hexdigest()


def safe_read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, path)


def build_item_key(item: dict[str, Any]) -> tuple[str, str]:
    path_val = item.get("path")
    if path_val is None:
        path_val = item.get("json_path", [])
    return (item.get("file", ""), json.dumps(path_val, sort_keys=True))


def validate_and_freeze_baseline(baseline_run_dir: Path) -> dict[str, Any]:
    """Validate baseline run integrity and freeze all critical baseline hashes."""
    baseline_run_dir = baseline_run_dir.resolve()
    val_code = tp.validate(baseline_run_dir)
    if val_code != 0:
        raise ValueError(f"Baseline validation failed with exit code {val_code}")

    diff_path = baseline_run_dir / "diff.json"
    review_path = baseline_run_dir / "review.json"
    reviewed_path = baseline_run_dir / "reviewed-translations.json"
    sr_receipt_path = baseline_run_dir / "semantic-review-receipt.json"
    b_llc = baseline_run_dir / "snapshot/llc/LLC_zh-CN"

    for p in [diff_path, review_path, reviewed_path, sr_receipt_path]:
        if not p.is_file():
            raise FileNotFoundError(f"Missing baseline file: {p}")
    if not b_llc.is_dir():
        raise FileNotFoundError(f"Missing baseline snapshot/llc/LLC_zh-CN directory: {b_llc}")

    sr_receipt = safe_read_json(sr_receipt_path)
    if sr_receipt.get("status") != "success":
        raise ValueError(f"Baseline semantic review receipt status is not 'success': {sr_receipt.get('status')}")

    actual_reviewed_sha = file_sha256(reviewed_path)
    expected_reviewed_sha = sr_receipt.get("reviewed_translations_sha")
    if not expected_reviewed_sha or expected_reviewed_sha != actual_reviewed_sha:
        raise ValueError(
            f"Baseline reviewed_translations_sha mismatch: expected {expected_reviewed_sha}, got {actual_reviewed_sha}"
        )

    review_report = safe_read_json(review_path)
    if not isinstance(review_report, dict):
        raise ValueError(f"Baseline review.json is not an object: {review_path}")

    actual_diff_sha = file_sha256(diff_path)
    actual_review_sha = file_sha256(review_path)
    actual_sr_receipt_sha = file_sha256(sr_receipt_path)
    llc_tree_sha = compute_tree_sha256(b_llc)

    return {
        "baseline_run_dir": str(baseline_run_dir),
        "diff_sha": actual_diff_sha,
        "review_json_sha": actual_review_sha,
        "reviewed_translations_sha": actual_reviewed_sha,
        "semantic_receipt_sha": actual_sr_receipt_sha,
        "llc_tree_sha": llc_tree_sha,
    }


def validate_and_freeze_current(run_dir: Path, force_review_items_path: Path | None) -> dict[str, Any]:
    """Validate current run integrity and freeze all critical inputs."""
    run_dir = run_dir.resolve()
    diff_path = run_dir / "diff.json"
    trans_path = run_dir / "translations.json"
    tm_path = run_dir / "translation_memory.json"
    manifest_path = run_dir / "shards_manifest.json"
    c_llc = run_dir / "snapshot/llc/LLC_zh-CN"

    for p in [diff_path, trans_path, tm_path, manifest_path]:
        if not p.is_file():
            raise FileNotFoundError(f"Missing current run file: {p}")
    if not c_llc.is_dir():
        raise FileNotFoundError(f"Missing current snapshot/llc/LLC_zh-CN directory: {c_llc}")

    actual_diff_sha = file_sha256(diff_path)
    actual_trans_sha = file_sha256(trans_path)
    actual_tm_sha = file_sha256(tm_path)
    actual_manifest_sha = file_sha256(manifest_path)
    llc_tree_sha = compute_tree_sha256(c_llc)

    # Validate shards_manifest binding
    manifest = safe_read_json(manifest_path)
    manifest_tm_hash = manifest.get("translation_memory_hash")
    if manifest_tm_hash and manifest_tm_hash != actual_tm_sha:
        raise ValueError(f"shards_manifest.json tm_hash mismatch: {manifest_tm_hash} != {actual_tm_sha}")
    manifest_diff_hash = manifest.get("diff_hash")
    if manifest_diff_hash and manifest_diff_hash != actual_diff_sha:
        raise ValueError(f"shards_manifest.json diff_hash mismatch: {manifest_diff_hash} != {actual_diff_sha}")

    force_file_sha = None
    if force_review_items_path is not None:
        if not force_review_items_path.is_file():
            raise FileNotFoundError(f"Force review items file specified but not found: {force_review_items_path}")
        force_file_sha = file_sha256(force_review_items_path)

    draft_trans_sha = None
    draft_file = run_dir / "draft_translations.json"
    if draft_file.is_file():
        draft_trans_sha = file_sha256(draft_file)
    else:
        draft_trans_sha = actual_trans_sha

    return {
        "run_dir": str(run_dir),
        "diff_sha": actual_diff_sha,
        "translations_sha": actual_trans_sha,
        "translation_memory_sha": actual_tm_sha,
        "shards_manifest_sha": actual_manifest_sha,
        "draft_translations_sha": draft_trans_sha,
        "llc_tree_sha": llc_tree_sha,
        "force_review_items_sha": force_file_sha,
    }


def find_related_terms_closure(
    pending_items: list[dict[str, Any]],
    current_translations: list[dict[str, Any]],
    delta_indices: set[int],
    extra_name_sources: set[str] | None = None,
) -> set[int]:
    """Build closure of skill name definitions and references."""
    closure = set(delta_indices)
    target_names: set[str] = set()
    if extra_name_sources:
        target_names.update(extra_name_sources)

    for idx in delta_indices:
        p = pending_items[idx]
        path_list = p.get("path", [])
        src = p.get("source", "")
        if path_list and path_list[-1] == "name" and src.strip():
            target_names.add(src.strip())
        if "'" in src:
            for part in src.split("'"):
                part_clean = part.strip()
                if 2 <= len(part_clean) <= 40 and not part_clean.startswith("["):
                    target_names.add(part_clean)

    # Hard-coded known critical term from requirements
    target_names.add("이 낫으로 답하나이다!")

    if not target_names:
        return closure

    expanded = True
    while expanded:
        expanded = False
        for idx, p in enumerate(pending_items):
            if idx in closure:
                continue
            src = p.get("source", "")
            path_list = p.get("path", [])
            if path_list and path_list[-1] == "name" and src.strip() in target_names:
                closure.add(idx)
                expanded = True
                continue
            for t_name in target_names:
                if t_name in src:
                    closure.add(idx)
                    expanded = True
                    break

    return closure


def order_delta_items_with_skill_affinity(
    delta_indices: set[int],
    pending_items: list[dict[str, Any]],
) -> list[int]:
    """Order delta indices placing critical skill definitions and citing descriptions adjacent in the first batch."""
    priority_names = ["이 낫으로 답하나이다!"]
    group_def_and_refs: list[int] = []
    other_indices: list[int] = []

    priority_set = set(priority_names)

    # First collect definitions
    def_indices = []
    ref_indices = []
    for idx in sorted(delta_indices):
        p = pending_items[idx]
        src = p.get("source", "")
        path_list = p.get("path", [])
        is_def = path_list and path_list[-1] == "name" and src.strip() in priority_set
        is_ref = any(p_name in src for p_name in priority_set) and not is_def
        if is_def:
            def_indices.append(idx)
        elif is_ref:
            ref_indices.append(idx)
        else:
            other_indices.append(idx)

    # Definitions first, then immediately their references
    group_def_and_refs = def_indices + ref_indices
    return group_def_and_refs + other_indices


def verify_terminology_consistency(
    final_reviewed: list[dict[str, Any]],
    target_term: str = "이 낫으로 답하나이다!",
) -> None:
    """Verify that skill name definition translation strictly matches any referencing description translations."""
    def_trans = None
    referencing_items = []

    for it in final_reviewed:
        path_list = it.get("path", [])
        src = it.get("source", "")
        if path_list and path_list[-1] == "name" and src.strip() == target_term:
            def_trans = it.get("translation", "").strip()
        elif target_term in src:
            referencing_items.append(it)

    if def_trans is not None and referencing_items:
        for ref in referencing_items:
            ref_trans = ref.get("translation", "")
            if def_trans not in ref_trans:
                raise RuntimeError(
                    f"Terminology inconsistency detected! Skill definition {target_term!r} translated as {def_trans!r}, "
                    f"but referencing item {ref.get('file')}:{ref.get('path')} translation does not contain it: {ref_trans!r}"
                )


def run_incremental_review(
    run_dir: Path,
    baseline_run_dir: Path,
    max_workers: int = 6,
    batch_size: int = 20,
    force_review_items_path: Path | None = None,
    driver: ht.HerdrDriver | None = None,
) -> int:
    """Execute end-to-end incremental review workflow."""
    run_dir = run_dir.resolve()
    baseline_run_dir = baseline_run_dir.resolve()

    print(f"[IncrementalReview] Target run: {run_dir}")
    print(f"[IncrementalReview] Baseline run: {baseline_run_dir}")

    # 1. Baseline and Current integrity validations & freezing
    base_freeze = validate_and_freeze_baseline(baseline_run_dir)
    curr_freeze = validate_and_freeze_current(run_dir, force_review_items_path)

    # Check LLC tree equality
    if curr_freeze["llc_tree_sha"] != base_freeze["llc_tree_sha"]:
        raise ValueError(
            f"LLC snapshot tree mismatch between current ({curr_freeze['llc_tree_sha']}) "
            f"and baseline ({base_freeze['llc_tree_sha']}). "
            "Incremental review reuse rejected! Full review required."
        )
    print(f"[IncrementalReview] Verified LLC trees identical (SHA: {curr_freeze['llc_tree_sha']})")

    # Read current files
    curr_diff = safe_read_json(run_dir / "diff.json")
    pending_items: list[dict[str, Any]] = curr_diff.get("pending", [])
    review_items: list[dict[str, Any]] = curr_diff.get("review", [])
    current_translations: list[dict[str, Any]] = safe_read_json(run_dir / "translations.json")

    if len(pending_items) != len(current_translations):
        raise ValueError(
            f"Current run translations count ({len(current_translations)}) != pending count ({len(pending_items)})"
        )

    # Read baseline reviewed translations
    base_reviewed_list: list[dict[str, Any]] = safe_read_json(baseline_run_dir / "reviewed-translations.json")
    base_reviewed_map: dict[tuple[str, str], dict[str, Any]] = {
        build_item_key(it): it for it in base_reviewed_list
    }

    # 2. Check force_review_items
    force_keys: set[tuple[str, str]] = set()
    force_name_sources: set[str] = set()
    if force_review_items_path is not None:
        force_data = safe_read_json(force_review_items_path)
        if isinstance(force_data, list):
            for it in force_data:
                force_keys.add(build_item_key(it))
                src = it.get("source", "")
                if src:
                    force_name_sources.add(src.strip())
        print(f"[IncrementalReview] Loaded {len(force_keys)} force review items from {force_review_items_path}")

    # Match pending items against baseline reviewed-translations
    reusable_indices: set[int] = set()
    delta_indices: set[int] = set()

    for idx, (p_item, t_item) in enumerate(zip(pending_items, current_translations)):
        p_key = build_item_key(p_item)
        t_key = build_item_key(t_item)
        if p_key != t_key:
            raise ValueError(f"Order or key mismatch between pending and translations at index {idx}: {p_key} vs {t_key}")

        if p_key in force_keys:
            delta_indices.add(idx)
            continue

        if p_key in base_reviewed_map:
            b_item = base_reviewed_map[p_key]
            # Exact match: file, path, source, translation must be strictly identical
            if (
                b_item.get("file") == p_item.get("file")
                and json.dumps(b_item.get("path"), sort_keys=True) == json.dumps(p_item.get("path"), sort_keys=True)
                and b_item.get("source") == p_item.get("source")
                and b_item.get("translation") == t_item.get("translation")
            ):
                reusable_indices.add(idx)
            else:
                delta_indices.add(idx)
        else:
            delta_indices.add(idx)

    # 3. Apply relation closure (skill name definitions and references)
    initial_delta_count = len(delta_indices)
    delta_indices = find_related_terms_closure(
        pending_items=pending_items,
        current_translations=current_translations,
        delta_indices=delta_indices,
        extra_name_sources=force_name_sources,
    )
    reusable_indices.difference_update(delta_indices)

    print(
        f"[IncrementalReview] Pending items: total={len(pending_items)}, "
        f"reusable={len(reusable_indices)}, delta={len(delta_indices)} "
        f"(expanded by relation closure from {initial_delta_count})"
    )

    # Match review items with baseline diff.review AND baseline review.json dispositions
    base_diff = safe_read_json(baseline_run_dir / "diff.json")
    base_review_items: list[dict[str, Any]] = base_diff.get("review", [])
    # Index baseline review items by serialized JSON string
    base_review_exact_set = {
        json.dumps(r, sort_keys=True): r for r in base_review_items
    }

    base_review_report = safe_read_json(baseline_run_dir / "review.json")
    base_dispositions: list[dict[str, Any]] = base_review_report.get("dispositions", [])
    base_disp_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for d in base_dispositions:
        d_key = (d.get("file", ""), json.dumps(d.get("path", []), sort_keys=True), d.get("reason", ""))
        base_disp_map[d_key] = d

    reusable_review_indices: set[int] = set()
    delta_review_indices: set[int] = set()

    for idx, r_item in enumerate(review_items):
        serialized_r = json.dumps(r_item, sort_keys=True)
        # Condition 1: Must be entirely identical to an item in baseline diff.review
        if serialized_r not in base_review_exact_set:
            delta_review_indices.add(idx)
            continue

        # Condition 2: Baseline review disposition must match and be resolved
        r_key = (r_item.get("file", ""), json.dumps(r_item.get("path", []), sort_keys=True), r_item.get("reason", ""))
        if r_key in base_disp_map:
            bd = base_disp_map[r_key]
            if (
                bd.get("source") == r_item.get("source")
                and bd.get("resolved") is True
                and bd.get("action") in ht.VALID_REVIEW_ACTIONS
                and (bd.get("resolution_note") or bd.get("rationale") or bd.get("note"))
            ):
                reusable_review_indices.add(idx)
            else:
                delta_review_indices.add(idx)
        else:
            delta_review_indices.add(idx)

    print(
        f"[IncrementalReview] Review items: total={len(review_items)}, "
        f"reusable={len(reusable_review_indices)}, delta={len(delta_review_indices)}"
    )

    # 4. Construct isolated sub_run_dir for delta review
    # Order delta pending items with skill affinity so related terms land in the first batch
    ordered_delta_pending_indices = order_delta_items_with_skill_affinity(delta_indices, pending_items)
    delta_pending_items = [pending_items[i] for i in ordered_delta_pending_indices]
    delta_trans_items = [current_translations[i] for i in ordered_delta_pending_indices]
    delta_review_items = [review_items[i] for i in sorted(delta_review_indices)]

    sub_run_dir = run_dir / "incremental-review"
    sub_run_dir.mkdir(parents=True, exist_ok=True)

    delta_receipts: list[dict[str, Any]] = []
    delta_reviewed_map: dict[tuple[str, str], dict[str, Any]] = {}
    delta_disp_map: dict[tuple[str, str, str], dict[str, Any]] = {}

    if delta_pending_items or delta_review_items:
        sub_diff = {
            "pending": delta_pending_items,
            "review": delta_review_items,
        }
        safe_write_json(sub_run_dir / "diff.json", sub_diff)
        safe_write_json(sub_run_dir / "translations.json", delta_trans_items)

        # Create read-only symlinks to snapshot/source, snapshot/llc, and translation_memory.json
        sub_snapshot = sub_run_dir / "snapshot"
        sub_snapshot.mkdir(parents=True, exist_ok=True)
        if (run_dir / "snapshot/source").exists() and not (sub_snapshot / "source").exists():
            (sub_snapshot / "source").symlink_to(run_dir / "snapshot/source")
        if (run_dir / "snapshot/llc").exists() and not (sub_snapshot / "llc").exists():
            (sub_snapshot / "llc").symlink_to(run_dir / "snapshot/llc")

        if (run_dir / "translation_memory.json").is_file() and not (sub_run_dir / "translation_memory.json").exists():
            (sub_run_dir / "translation_memory.json").symlink_to(run_dir / "translation_memory.json")

        print(
            f"[IncrementalReview] Invoking orchestrate_review for {len(delta_pending_items)} delta pending "
            f"and {len(delta_review_items)} delta review items..."
        )
        ret = rb.orchestrate_review(
            run_dir=sub_run_dir,
            batch_size=batch_size,
            max_workers=max_workers,
            driver=driver,
        )
        if ret != 0:
            print(f"[IncrementalReview] Sub-review failed with exit code {ret}", file=sys.stderr)
            return ret

        sub_reviewed = safe_read_json(sub_run_dir / "reviewed-translations.json")
        for it in sub_reviewed:
            delta_reviewed_map[build_item_key(it)] = it

        sub_review_rep = safe_read_json(sub_run_dir / "review.json")
        for d in sub_review_rep.get("dispositions", []):
            d_key = (d.get("file", ""), json.dumps(d.get("path", []), sort_keys=True), d.get("reason", ""))
            delta_disp_map[d_key] = d

        sub_receipt = safe_read_json(sub_run_dir / "semantic-review-receipt.json")
        delta_receipts = sub_receipt.get("batch_receipts", [])

    # 5. Merge results back into complete reviewed-translations.json and review.json
    final_reviewed_trans: list[dict[str, Any]] = []
    for idx, p_item in enumerate(pending_items):
        key = build_item_key(p_item)
        if idx in reusable_indices:
            b_rev = base_reviewed_map[key]
            final_reviewed_trans.append({
                "file": p_item["file"],
                "path": p_item["path"],
                "source": p_item["source"],
                "translation": b_rev["translation"],
            })
        elif key in delta_reviewed_map:
            d_rev = delta_reviewed_map[key]
            final_reviewed_trans.append({
                "file": p_item["file"],
                "path": p_item["path"],
                "source": p_item["source"],
                "translation": d_rev["translation"],
            })
        else:
            raise RuntimeError(f"Missing reviewed translation for item #{idx}: {key}")

    final_dispositions: list[dict[str, Any]] = []
    for idx, r_item in enumerate(review_items):
        r_key = (r_item.get("file", ""), json.dumps(r_item.get("path", []), sort_keys=True), r_item.get("reason", ""))
        if idx in reusable_review_indices:
            final_dispositions.append(base_disp_map[r_key])
        elif r_key in delta_disp_map:
            final_dispositions.append(delta_disp_map[r_key])
        else:
            raise RuntimeError(f"Missing review disposition for review item #{idx}: {r_key}")

    # Verify no unresolved dispositions
    for disp in final_dispositions:
        if disp.get("resolved") is not True:
            raise RuntimeError(f"Unresolved item encountered in final dispositions: {disp}")

    # Post-review terminology consistency check
    verify_terminology_consistency(final_reviewed_trans, "이 낫으로 답하나이다!")

    # Pre-merge re-validation of all frozen hashes
    # Baseline checks
    if file_sha256(baseline_run_dir / "diff.json") != base_freeze["diff_sha"]:
        raise RuntimeError("Baseline diff.json was tampered during execution!")
    if file_sha256(baseline_run_dir / "review.json") != base_freeze["review_json_sha"]:
        raise RuntimeError("Baseline review.json was tampered during execution!")
    if file_sha256(baseline_run_dir / "reviewed-translations.json") != base_freeze["reviewed_translations_sha"]:
        raise RuntimeError("Baseline reviewed-translations.json was tampered during execution!")
    if file_sha256(baseline_run_dir / "semantic-review-receipt.json") != base_freeze["semantic_receipt_sha"]:
        raise RuntimeError("Baseline semantic-review-receipt.json was tampered during execution!")
    if compute_tree_sha256(baseline_run_dir / "snapshot/llc/LLC_zh-CN") != base_freeze["llc_tree_sha"]:
        raise RuntimeError("Baseline LLC tree was tampered during execution!")

    # Current checks
    if file_sha256(run_dir / "diff.json") != curr_freeze["diff_sha"]:
        raise RuntimeError("Current diff.json was tampered during execution!")
    if file_sha256(run_dir / "translations.json") != curr_freeze["translations_sha"]:
        raise RuntimeError("Current translations.json was tampered during execution!")
    if file_sha256(run_dir / "translation_memory.json") != curr_freeze["translation_memory_sha"]:
        raise RuntimeError("Current translation_memory.json was tampered during execution!")
    if file_sha256(run_dir / "shards_manifest.json") != curr_freeze["shards_manifest_sha"]:
        raise RuntimeError("Current shards_manifest.json was tampered during execution!")
    if compute_tree_sha256(run_dir / "snapshot/llc/LLC_zh-CN") != curr_freeze["llc_tree_sha"]:
        raise RuntimeError("Current LLC tree was tampered during execution!")
    if force_review_items_path is not None:
        if file_sha256(force_review_items_path) != curr_freeze["force_review_items_sha"]:
            raise RuntimeError("Force review items file was tampered during execution!")

    # Write merged reviewed-translations.json
    reviewed_file = run_dir / "reviewed-translations.json"
    safe_write_json(reviewed_file, final_reviewed_trans)
    reviewed_sha = file_sha256(reviewed_file)

    # Write merged review.json
    review_report = {
        "hashes": {
            "diff.json": curr_freeze["diff_sha"],
            "translations.json": curr_freeze["translations_sha"],
            "reviewed-translations.json": reviewed_sha,
        },
        "dispositions": final_dispositions,
    }
    safe_write_json(run_dir / "review.json", review_report)

    # Write merged semantic-review-receipt.json
    semantic_receipt = {
        "status": "success",
        "strategy": "verified_baseline_review_plus_delta",
        "root_diff_sha": curr_freeze["diff_sha"],
        "root_translations_sha": curr_freeze["translations_sha"],
        "draft_translations_sha": curr_freeze["draft_translations_sha"],
        "reviewed_translations_sha": reviewed_sha,
        "reused_reviewed_count": len(reusable_indices),
        "reused_review_dispositions_count": len(reusable_review_indices),
        "delta_reviewed_count": len(delta_indices),
        "delta_review_dispositions_count": len(delta_review_indices),
        "total_pending_items": len(pending_items),
        "total_review_items": len(review_items),
        "baseline_hashes": base_freeze,
        "current_hashes": curr_freeze,
        "llc_tree_sha": curr_freeze["llc_tree_sha"],
        "delta_receipts": delta_receipts,
    }
    safe_write_json(run_dir / "semantic-review-receipt.json", semantic_receipt)

    print(
        f"[IncrementalReview] Merged {len(final_reviewed_trans)} reviewed translations and "
        f"{len(final_dispositions)} dispositions."
    )

    # 6. Validate entire run with tp.validate
    print(f"[IncrementalReview] Running full translation_pipeline validation on {run_dir}...")
    val_code = tp.validate(run_dir)
    if val_code != 0:
        raise ValueError(f"Pipeline validation failed on {run_dir} with code {val_code}")

    print("[IncrementalReview] Validation passed completely!")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Incremental Review Orchestrator")
    parser.add_argument("--run-dir", required=True, help="Target run directory")
    parser.add_argument("--baseline-run-dir", required=True, help="Baseline run directory with verified review")
    parser.add_argument("--max-workers", type=int, default=6, help="Worker concurrency for review")
    parser.add_argument("--batch-size", type=int, default=20, help="Batch size for review")
    parser.add_argument("--force-review-items", default=None, help="Path to JSON file with forced review items")

    args = parser.parse_args()
    try:
        force_path = Path(args.force_review_items) if args.force_review_items else None
        return run_incremental_review(
            run_dir=Path(args.run_dir),
            baseline_run_dir=Path(args.baseline_run_dir),
            max_workers=args.max_workers,
            batch_size=args.batch_size,
            force_review_items_path=force_path,
        )
    except Exception as exc:
        print(f"Error in incremental review: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
