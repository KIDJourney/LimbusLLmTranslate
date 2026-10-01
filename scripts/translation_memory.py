#!/usr/bin/env python3
"""LLC Reference Translation Memory and Retrieval.

Features:
1. Frozen LLC reference index built strictly with Python standard library.
2. Uses localization.leaves/decode/encode stable ID paths to align KR and LLC_zh-CN.
3. Filters for valid Chinese translations: non-empty, contains no Hangul characters, contains Chinese characters.
4. Excludes diff pending and diff review targets as well as unreviewed AI translations.
5. Path alignment only marks 'aligned_reference' (never treated as definitive fact if upstream Korean could have changed).
6. Conservative against index-only lists (integer steps) and duplicate id occurrences (purged completely across all occurrences).
7. Query per item returns evidence:
   - exact_match (identical source text in reference corpus, fragile excluded)
   - short_name (names matching substrings in pending source, prioritized)
   - document_preceding (preceding translations in the same file strictly before target ordinal)
   - similar_terms (n-gram / inverted index candidates with capped unique postings)
8. Caps: max evidence per item, max character limit strictly enforced without silent truncation (skip if single record exceeds limit), stable evidence IDs.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
import scripts.localization as localization

HANGUL = localization.HANGUL
CHINESE_CHAR = re.compile(r"[一-鿿]")
NAME_KEYS = {
    "name",
    "title",
    "nickName",
    "teller",
    "speaker",
    "shortName",
    "specialName",
    "abName",
    "abnormalityName",
    "displayName",
}
MAX_EVIDENCE_PER_ITEM = 5
MAX_CHARS_PER_ITEM = 2400


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def is_valid_chinese_translation(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if HANGUL.search(stripped):
        return False
    if not CHINESE_CHAR.search(stripped):
        return False
    return True


def build_translation_memory_index(
    kr_dir: Path,
    llc_cn_dir: Path,
    pending_items: list[dict[str, Any]] | None = None,
    review_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    kr_dir = Path(kr_dir).resolve()
    llc_cn_dir = Path(llc_cn_dir).resolve()

    excluded_keys: set[tuple[str, str]] = set()
    if pending_items:
        for p in pending_items:
            excluded_keys.add((p.get("file", ""), json.dumps(localization.encode(localization.decode(p.get("path", []))), sort_keys=True)))
    if review_items:
        for r in review_items:
            excluded_keys.add((r.get("file", ""), json.dumps(localization.encode(localization.decode(r.get("path", []))), sort_keys=True)))

    records: list[dict[str, Any]] = []
    by_source: dict[str, list[int]] = collections.defaultdict(list)
    by_file_order: dict[str, list[int]] = collections.defaultdict(list)
    file_path_ordinals: dict[str, dict[str, int]] = collections.defaultdict(dict)
    name_records: dict[str, list[int]] = collections.defaultdict(list)
    ngram_index: dict[str, list[int]] = collections.defaultdict(list)

    kr_files = sorted(kr_dir.rglob("*.json"))

    for file_path in kr_files:
        rel = file_path.relative_to(kr_dir).as_posix()
        if "donttranslate" in rel.lower():
            continue
        cn_file = llc_cn_dir / rel

        try:
            kr_data = localization.read(file_path)
            cn_data = localization.read(cn_file) if cn_file.is_file() else {}
        except Exception:
            continue

        kr_leaves_list = list(localization.leaves(kr_data))
        cn_leaves_dict = dict(localization.leaves(cn_data))

        # Check for duplicate ids across kr_data and cn_data
        duplicate_id_steps: set[tuple[str, str]] = set()
        for leaves_items in (kr_leaves_list, cn_leaves_dict.items()):
            for rpath, _ in leaves_items:
                for step in rpath:
                    if isinstance(step, tuple) and len(step) >= 3 and isinstance(step[2], int):
                        if step[2] > 0:
                            duplicate_id_steps.add((step[0], step[1]))

        for ordinal, (raw_path, kr_text) in enumerate(kr_leaves_list):
            enc_path = localization.encode(raw_path)
            path_key = json.dumps(enc_path, sort_keys=True)
            file_path_ordinals[rel][path_key] = ordinal

            if not isinstance(kr_text, str) or not kr_text.strip() or not HANGUL.search(kr_text):
                continue

            t_field = localization.text_field(raw_path)
            if t_field not in localization.TEXT_KEYS:
                continue

            if (rel, path_key) in excluded_keys:
                continue

            if raw_path not in cn_leaves_dict:
                continue

            cn_text = cn_leaves_dict[raw_path]
            if not is_valid_chinese_translation(cn_text):
                continue

            # Check fragile: pure integer index step or duplicate id in KR or CN
            has_pure_int = any(isinstance(step, int) for step in raw_path)
            if has_pure_int:
                continue

            has_duplicate_id = any(
                isinstance(step, tuple) and len(step) >= 3 and (step[0], step[1]) in duplicate_id_steps
                for step in raw_path
            )
            has_fragile_structure = has_duplicate_id

            rec_id = len(records)
            stable_id = f"tm_{rec_id:06d}"
            record = {
                "id": rec_id,
                "stable_id": stable_id,
                "file": rel,
                "ordinal": ordinal,
                "path": enc_path,
                "raw_path": [list(x) if isinstance(x, tuple) else x for x in raw_path],
                "source": kr_text,
                "translation": cn_text,
                "fragile": has_fragile_structure,
                "is_name": t_field in NAME_KEYS,
            }
            records.append(record)

            if not has_fragile_structure:
                by_source[kr_text].append(rec_id)
                by_file_order[rel].append(rec_id)

                if record["is_name"] and len(kr_text.strip()) <= 30:
                    name_records[kr_text.strip()].append(rec_id)

                clean_src = re.sub(r"\s+", " ", kr_text)
                seen_bigrams = set()
                for i in range(len(clean_src) - 1):
                    bi = clean_src[i : i + 2]
                    if all(HANGUL.fullmatch(ch) for ch in bi):
                        seen_bigrams.add(bi)
                for bi in sorted(seen_bigrams):
                    if len(ngram_index[bi]) < 200:
                        ngram_index[bi].append(rec_id)

    source_conflicts: dict[str, list[str]] = {}
    for src, r_ids in by_source.items():
        unique_translations = sorted({records[idx]["translation"] for idx in r_ids})
        if len(unique_translations) > 1:
            source_conflicts[src] = unique_translations

    summary = {
        "total_records": len(records),
        "total_files": len(by_file_order),
        "total_sources": len(by_source),
        "conflicting_sources": len(source_conflicts),
    }

    return {
        "version": 1,
        "summary": summary,
        "records": records,
        "by_source": by_source,
        "by_file_order": by_file_order,
        "file_path_ordinals": file_path_ordinals,
        "name_records": name_records,
        "ngram_index": ngram_index,
        "source_conflicts": source_conflicts,
    }


def query_item_evidence(
    index: dict[str, Any],
    file: str,
    path: list[Any],
    source: str,
    max_evidence: int = MAX_EVIDENCE_PER_ITEM,
    max_chars: int = MAX_CHARS_PER_ITEM,
) -> dict[str, Any]:
    records = index["records"]
    by_source = index["by_source"]
    by_file_order = index["by_file_order"]
    file_path_ordinals = index.get("file_path_ordinals", {})
    name_records = index.get("name_records", {})
    ngram_index = index["ngram_index"]
    source_conflicts = index["source_conflicts"]

    selected_evidence: list[dict[str, Any]] = []
    seen_evidence_keys: set[tuple[str, str, str, str]] = set()
    total_chars = 0

    conflicts: list[str] = source_conflicts.get(source, [])

    # Determine target ordinal for document_preceding. Default to None; unknown target provides no document_preceding.
    target_ordinal: int | None = None
    path_key = json.dumps(localization.encode(localization.decode(path)), sort_keys=True)
    if file in file_path_ordinals and path_key in file_path_ordinals[file]:
        target_ordinal = file_path_ordinals[file][path_key]

    def add_evidence(rec: dict[str, Any], ev_type: str, note: str = "") -> bool:
        nonlocal total_chars
        if len(selected_evidence) >= max_evidence:
            return False
        if rec.get("fragile", False) and ev_type == "exact_match":
            return True
        key = (rec["file"], json.dumps(rec["path"], sort_keys=True), rec["source"], rec["translation"])
        if key in seen_evidence_keys:
            return True

        ev_item = {
            "evidence_id": rec.get("stable_id", f"tm_{rec['id']:06d}"),
            "type": ev_type,
            "file": rec["file"],
            "path": rec["path"],
            "source": rec["source"],
            "translation": rec["translation"],
        }
        if note:
            ev_item["note"] = note

        ev_len = len(json.dumps(ev_item, ensure_ascii=False))
        if ev_len > max_chars:
            return True
        if total_chars + ev_len > max_chars:
            return True

        seen_evidence_keys.add(key)
        selected_evidence.append(ev_item)
        total_chars += ev_len
        return len(selected_evidence) < max_evidence

    # 1. Exact match across the corpus (fragile strictly excluded)
    if source in by_source:
        exact_ids = by_source[source]
        same_file_exact = [i for i in exact_ids if records[i]["file"] == file]
        other_file_exact = [i for i in exact_ids if records[i]["file"] != file]
        for idx in same_file_exact + other_file_exact:
            rec = records[idx]
            note = "aligned_reference"
            if len(conflicts) > 1:
                note += "; multiple_translations_found"
            if not add_evidence(rec, "exact_match", note):
                break

    # 2. Short name matches where Korean name is a substring of source (prioritized over general context)
    if len(selected_evidence) < max_evidence:
        matching_names = []
        for name_src, n_ids in name_records.items():
            if name_src != source and len(name_src) >= 2 and name_src in source:
                matching_names.append((len(name_src), name_src, n_ids))
        matching_names.sort(key=lambda x: -x[0])
        for _, _, n_ids in matching_names:
            for nid in n_ids:
                if not add_evidence(records[nid], "short_name", "name_substring_match"):
                    break
            if len(selected_evidence) >= max_evidence:
                break

    # 3. Document preceding context in same file strictly before target ordinal
    if len(selected_evidence) < max_evidence and file in by_file_order and target_ordinal is not None:
        preceding_ids = [
            i for i in by_file_order[file]
            if not records[i].get("fragile", False) and records[i]["ordinal"] < target_ordinal
        ]
        preceding_candidates = [
            records[i] for i in preceding_ids
            if records[i]["source"] != source
        ]
        for rec in reversed(preceding_candidates[-3:]):
            if not add_evidence(rec, "document_preceding", "preceding_in_same_file"):
                break

    # 4. Similar terms / n-gram match
    if len(selected_evidence) < max_evidence:
        clean_src = re.sub(r"\s+", " ", source)
        bigrams = [clean_src[i : i + 2] for i in range(len(clean_src) - 1) if all(HANGUL.fullmatch(ch) for ch in clean_src[i : i + 2])]
        if bigrams:
            candidate_counts: collections.Counter[int] = collections.Counter()
            for bg in sorted(set(bigrams)):
                for rid in ngram_index.get(bg, []):
                    if records[rid]["source"] != source and not records[rid].get("fragile", False):
                        candidate_counts[rid] += 1
            most_similar = [rid for rid, _ in candidate_counts.most_common(10)]
            for rid in most_similar:
                rec = records[rid]
                if not add_evidence(rec, "similar_terms", "ngram_overlap"):
                    break

    return {
        "file": file,
        "path": path,
        "source": source,
        "evidence_count": len(selected_evidence),
        "evidence": selected_evidence,
        "conflicts": conflicts if len(conflicts) > 1 else [],
        "term_conflicts": {term: source_conflicts[term] for term in sorted(name_records) if term in source and term in source_conflicts},
    }


def build_shards_translation_context(
    index: dict[str, Any],
    shards: list[list[dict[str, Any]]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_contexts = []

    for idx, shard_items in enumerate(shards):
        shard_id = f"shard_{idx:02d}"
        s_dir = output_dir / shard_id
        s_dir.mkdir(parents=True, exist_ok=True)

        shard_contexts = []
        for item in shard_items:
            ctx = query_item_evidence(
                index=index,
                file=item.get("file", ""),
                path=item.get("path", []),
                source=item.get("source", ""),
            )
            shard_contexts.append(ctx)

        context_file = s_dir / "context.json"
        localization.write(context_file, shard_contexts)

        manifest_contexts.append({
            "shard_id": shard_id,
            "context_file": str(context_file.resolve()),
            "context_hash": file_sha256(context_file),
            "items_count": len(shard_contexts),
        })

    return manifest_contexts


def build_batches_review_context(
    index: dict[str, Any],
    batches: list[list[dict[str, Any]]],
    batches_dir: Path,
    batch_prefix: str = "pending",
) -> list[dict[str, Any]]:
    batches_dir = Path(batches_dir).resolve()
    batches_dir.mkdir(parents=True, exist_ok=True)
    batch_contexts = []

    for idx, batch_items in enumerate(batches):
        batch_name = f"{batch_prefix}_{idx:03d}"
        b_dir = batches_dir / batch_name
        b_dir.mkdir(parents=True, exist_ok=True)

        b_contexts = []
        for item in batch_items:
            ctx = query_item_evidence(
                index=index,
                file=item.get("file", ""),
                path=item.get("path", []),
                source=item.get("source", ""),
            )
            b_contexts.append(ctx)

        context_file = b_dir / "context.json"
        localization.write(context_file, b_contexts)

        batch_contexts.append({
            "batch_name": batch_name,
            "context_file": str(context_file.resolve()),
            "context_hash": file_sha256(context_file),
            "items_count": len(b_contexts),
        })

    return batch_contexts
