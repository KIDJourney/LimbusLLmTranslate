#!/usr/bin/env python3
"""Multi-Agent Parallel Batch Review Orchestrator.

Enforces:
1. Pool of independent Claude Code (Gemini) reviewer agents (default max-workers: 6).
2. Shared work queue of batches: each batch handled by exactly one agent in its own tab/pane.
3. Explicit reuse of verified receipts; no re-reviewing already approved batches.
4. Prompt embed: source/KR and llc/LLC_zh-CN read guidance + verified LLC terms in heredoc.
5. Process-level safety: thread-safe agent registry & aggregation of final results.
6. Unresolved items strictly block completion.
"""

from __future__ import annotations

import argparse
import functools
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import localization
from scripts import translation_memory as tm
from herdr_translation import (
    DEFAULT_MODEL,
    DEFAULT_SETTINGS_PATH,
    HEX64_REGEX,
    HerdrDriver,
    VALID_REVIEW_ACTIONS,
    file_sha256,
    generate_agent_name,
    safe_read_json,
    safe_write_json,
    wait_agent_until_settled,
)

BATCH_SIZE = 50
MAX_BATCH_RETRIES = 3
DEFAULT_MAX_WORKERS = 6


class RootInputIntegrityError(ValueError):
    """Run-level inputs were changed or became unreadable."""


def assert_root_input(path: Path, expected: str) -> None:
    try:
        actual = file_sha256(path)
    except OSError as exc:
        raise RootInputIntegrityError(f"Root input unavailable: {path}") from exc
    if actual != expected:
        raise RootInputIntegrityError(f"Root input changed: {path}")


def track_review_run(fn):
    @functools.wraps(fn)
    def tracked(*args, **kwargs):
        run_dir = Path(kwargs.get("run_dir", args[0] if args else "."))
        progress = run_dir / "reviews/pool_progress.json"
        started = time.time()
        base = {"status": "running", "pid": os.getpid(), "started_at": started,
                "updated_at": started, "tasks": {},
                "note": "Historical status is not proof of liveness; verify the controller PID and agent state."}
        safe_write_json(progress, base)
        error = None
        result = None
        try:
            result = fn(*args, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            data = safe_read_json(progress)
            now = time.time()
            data.update(status="completed" if error is None and result == 0 else "failed",
                        pid=os.getpid(), started_at=started, finished_at=now,
                        updated_at=now, duration_sec=now-started)
            if error is not None:
                data.update(error_type=type(error).__name__, error_message=str(error))
            elif result != 0:
                data.update(error_type="ReviewRejected", error_message=f"Review returned {result}")
            safe_write_json(progress, data)
    return tracked


def safe_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(text, encoding="utf-8")
    os.replace(temp_file, path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_pending_batch_output(
    batch_input: list[dict[str, Any]],
    output_path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate pending batch output against input items."""
    if not output_path.is_file():
        return [], [f"Result file not found: {output_path}"]

    try:
        data = safe_read_json(output_path)
    except Exception as exc:
        return [], [f"Invalid JSON format: {exc}"]

    if not isinstance(data, list):
        return [], [f"Result must be a JSON array, got {type(data)}"]

    if len(data) != len(batch_input):
        return [], [f"Item count mismatch: expected {len(batch_input)}, got {len(data)}"]

    errors = []
    valid_items = []

    for idx, (exp, got) in enumerate(zip(batch_input, data)):
        if not isinstance(got, dict):
            errors.append(f"Item #{idx} is not a dictionary")
            continue

        if got.get("index") != exp.get("index"):
            errors.append(f"Item #{idx} index mismatch: expected {exp.get('index')}, got {got.get('index')}")
        if got.get("file") != exp.get("file") or got.get("path") != exp.get("path"):
            errors.append(f"Item #{idx} file/path mismatch: expected {exp.get('file')}:{exp.get('path')}")
        if got.get("source") != exp.get("source"):
            errors.append(f"Item #{idx} source tampering detected: {got.get('source')!r} != {exp.get('source')!r}")

        trans = got.get("translation")
        if not isinstance(trans, str) or not trans.strip():
            errors.append(f"Item #{idx} missing translation string")
        else:
            try:
                localization.validate_translation(exp["source"], trans)
            except Exception as e:
                errors.append(f"Item #{idx} translation validation error: {e}")

        verdict = got.get("verdict")
        if verdict not in {"approved", "corrected", "unresolved"}:
            errors.append(f"Item #{idx} invalid verdict '{verdict}', must be approved/corrected/unresolved")

        reason = got.get("reason")
        if not reason or not str(reason).strip():
            errors.append(f"Item #{idx} missing review reason note")

        valid_items.append(got)

    return valid_items, errors


def validate_review_batch_output(
    batch_input: list[dict[str, Any]],
    output_path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate review disposition batch output against input items."""
    if not output_path.is_file():
        return [], [f"Result file not found: {output_path}"]

    try:
        data = safe_read_json(output_path)
    except Exception as exc:
        return [], [f"Invalid JSON format: {exc}"]

    if not isinstance(data, list):
        return [], [f"Result must be a JSON array, got {type(data)}"]

    if len(data) != len(batch_input):
        return [], [f"Item count mismatch: expected {len(batch_input)}, got {len(data)}"]

    errors = []
    valid_items = []

    for idx, (exp, got) in enumerate(zip(batch_input, data)):
        if not isinstance(got, dict):
            errors.append(f"Item #{idx} is not a dictionary")
            continue

        if got.get("file") != exp.get("file") or got.get("path") != exp.get("path"):
            errors.append(f"Item #{idx} file/path mismatch: expected {exp.get('file')}:{exp.get('path')}")
        if got.get("reason") != exp.get("reason"):
            errors.append(f"Item #{idx} reason mismatch: expected {exp.get('reason')!r}")
        if got.get("source") != exp.get("source"):
            errors.append(f"Item #{idx} source mismatch: expected {exp.get('source')!r}")

        action = got.get("action")
        allowed_actions = VALID_REVIEW_ACTIONS | {"needs_translation"}
        if action not in allowed_actions:
            errors.append(f"Item #{idx} invalid action '{action}', must be one of {sorted(allowed_actions)}")

        note = got.get("resolution_note")
        if not note or not str(note).strip():
            errors.append(f"Item #{idx} missing resolution_note")

        resolved = got.get("resolved")
        if not isinstance(resolved, bool):
            errors.append(f"Item #{idx} 'resolved' must be boolean, got {type(resolved)}")
        elif action == "needs_translation" and resolved is not False:
            errors.append(f"Item #{idx} action is 'needs_translation' but resolved is True; must be false")

        valid_items.append(got)

    return valid_items, errors


def check_existing_verified_receipt(
    batch_dir: Path,
    batch_type: str,
    batch_idx: int,
    batch_items: list[dict[str, Any]],
    input_sha_pre: str,
    context_sha: str | None = None,
    index_sha: str | None = None,
) -> dict[str, Any] | None:
    """Check if verified batch receipt already exists and can be reused."""
    input_file = batch_dir / "input.json"
    result_file = batch_dir / "result.json"
    receipt_file = batch_dir / "receipt.json"

    if receipt_file.is_file() and result_file.is_file() and input_file.is_file():
        try:
            rcpt = safe_read_json(receipt_file)
            if rcpt.get("input_sha") == input_sha_pre and rcpt.get("status") == "success":
                if context_sha is not None and rcpt.get("context_sha") != context_sha:
                    return None
                if index_sha is not None and rcpt.get("index_sha") != index_sha:
                    return None
                res_sha = file_sha256(result_file)
                if rcpt.get("response_sha") == res_sha:
                    if batch_type == "pending":
                        valid_items, errs = validate_pending_batch_output(batch_items, result_file)
                    else:
                        valid_items, errs = validate_review_batch_output(batch_items, result_file)
                    if not errs:
                        return {
                            "batch_type": batch_type,
                            "batch_idx": batch_idx,
                            "items": valid_items,
                            "receipt": rcpt,
                        }
        except Exception:
            pass
    return None


def process_batch(
    driver: HerdrDriver,
    agent_name: str,
    pane_id: str,
    batch_dir: Path,
    batch_type: str,
    batch_idx: int,
    batch_items: list[dict[str, Any]],
    timeout_sec: int,
    input_sha_pre: str,
    diff_file: Path,
    diff_sha_pre: str,
    trans_file: Path,
    trans_sha_pre: str,
    draft_file: Path,
    draft_sha_pre: str,
    snapshot_kr_dir: Path | None = None,
    snapshot_llc_dir: Path | None = None,
    context_file: Path | None = None,
    context_sha: str | None = None,
    index_file: Path | None = None,
    index_sha: str | None = None,
) -> dict[str, Any]:
    """Execute a single review batch on the specified agent."""
    started_at = time.time()
    try:
        input_file = batch_dir / "input.json"
        result_file = batch_dir / "result.json"
        receipt_file = batch_dir / "receipt.json"

        # Pre-verification: verify run-level inputs and batch input sha
        assert_root_input(diff_file, diff_sha_pre)
        assert_root_input(trans_file, trans_sha_pre)
        assert_root_input(draft_file, draft_sha_pre)
        if index_file and index_sha:
            assert_root_input(index_file, index_sha)
        if context_file and context_sha:
            assert_root_input(context_file, context_sha)

        # 1. Check if valid receipt and result already exist for reuse
        cached = check_existing_verified_receipt(
            batch_dir, batch_type, batch_idx, batch_items, input_sha_pre,
            context_sha=context_sha, index_sha=index_sha
        )
        if cached is not None:
            print(f"[{batch_type} batch {batch_idx:03d}] Reusing verified batch receipt.")
            return cached

        # Invalidate stale pre-context result.json to prevent pseudo-reuse if worker writes nothing
        if context_sha is not None and result_file.is_file():
            backup_name = f"result.pre-context-{time.time_ns()}.json"
            result_file.rename(batch_dir / backup_name)

        # Write input.json
        safe_write_json(input_file, batch_items)
        input_sha = file_sha256(input_file)
        if input_sha != input_sha_pre:
            raise ValueError(f"Batch input sha mismatch: {input_sha} != {input_sha_pre}")

        # 2. Construct embedded prompt
        items_json_str = json.dumps(batch_items, ensure_ascii=False, indent=2)
        req_hash = sha256_text(items_json_str)

        kr_ref_str = str(snapshot_kr_dir.resolve()) if snapshot_kr_dir and snapshot_kr_dir.is_dir() else "snapshot/source/KR"
        llc_ref_str = str(snapshot_llc_dir.resolve()) if snapshot_llc_dir and snapshot_llc_dir.is_dir() else "snapshot/llc/LLC_zh-CN"

        if batch_type == "pending":
            prompt_text = (
                f"=== 待校对翻译批次 {batch_idx:03d} (共 {len(batch_items)} 项) ===\n"
                f"目标输出文件：{result_file.resolve()}\n\n"
                "【规则】\n"
                "1. 严禁使用外部翻译API或启动子Agent，必须由你逐项进行语义审查，不得使用统一套话假审查。\n"
                f"2. 参考对照目录（只读）：原版韩文 [{kr_ref_str}]，已核实LLC译名中文 [{llc_ref_str}]。不得对全库产生巨量输出，仅读必要文件上下文。\n"
                "3. 唯一可写路径是目标 result.json 文件；input.json、快照、其它批次及根目录译文一律只读，连格式化重写也禁止。\n"
                "4. 用 Python 加载本批 input.json，在其条目上逐项添加裁决/理由及必要译文修订，写入 result.json；不要重抄原始 file/path/source。每完成 10 项立即保存部分结果（未完成项不得填 approved）；最终必须覆盖全批。不要在对话中逐项展开长篇分析，理由直接写结果文件，每项简洁具体。无需运行验证脚本，控制器执行校验。\n"
                "5. 已核实LLC译名核心术语规范：\n"
                "   - '원레그' 规范译为 '单脚人'（严禁误改为独腿/单腿人）\n"
                "   - '간수' 角色规范译为 '看守'（严禁误改为狱卒）\n"
                "   - '르누아르' 规范译为 '黑派'\n"
                "   - '르루주' 规范译为 '红派'\n"
                "   - '보존' 规范译为 '保存'\n"
                "   - '죄책감' 规范译为 '负罪感'\n"
                "   - '박동' 规范译为 '搏动'\n"
                "6. 审查翻译是否自然流畅、术语统一、严格保留富文本标签与占位符（如 [WhenUse], <color=...> 等完全原样闭合）、无韩文遗漏。\n"
                "7. 格式必须为 JSON 数组，每项包含：\n"
                "   - index (与输入完全一致的整数)\n"
                "   - file, path, source (与输入完全一致，严禁篡改)\n"
                "   - translation (确认或修订后的中文翻译，非空)\n"
                "   - verdict ('approved': 译文无误通过; 'corrected': 修复缺陷后的译文; 'unresolved': 存在阻断疑问无法定稿)\n"
                "   - reason (具体中文校对理由或修改说明，若引用证据须包含证据ID)\n\n"
                f"输入待审条目如下：\n{items_json_str}\n"
            )
        else:
            prompt_text = (
                f"=== Diff复核项审查批次 {batch_idx:03d} (共 {len(batch_items)} 项) ===\n"
                f"目标输出文件：{result_file.resolve()}\n\n"
                "【规则】\n"
                "1. 逐项审查每一项未翻译/差异原因，给出明确定性，不得使用机械模板敷衍。\n"
                f"2. 参考对照目录（只读）：原版韩文 [{kr_ref_str}]，已核实LLC译名中文 [{llc_ref_str}]。\n"
                "3. 唯一可写路径是目标 result.json 文件；input.json、快照、其它批次及根目录译文一律只读，连格式化重写也禁止。\n"
                "4. 用 Python 加载本批 input.json，在其条目上逐项添加裁决/理由及必要译文修订，写入 result.json；不要重抄原始 file/path/source。每完成 10 项立即保存部分结果（未完成项不得填 approved）；最终必须覆盖全批。不要在对话中逐项展开长篇分析，理由直接写结果文件，每项简洁具体。无需运行验证脚本，控制器执行校验。\n"
                "5. 格式必须为 JSON 数组，每项包含：\n"
                "   - file, path, reason, source (与输入完全一致)\n"
                "   - action: 必须为 keep_current, approved_as_is, internal_dummy_ignored, empty_intentional, format_verified, 或 needs_translation\n"
                "   - resolution_note: 具体的定性中文说明（不可为空）\n"
                "   - resolved: 布尔值 (有把握合理解释的填 true；不确定或需补译的填写 needs_translation 并 resolved: false，严禁伪造)\n\n"
                f"输入复核条目如下：\n{items_json_str}\n"
            )

        if context_file:
            if batch_type == "pending":
                prompt_text += (
                    f"\n【参考记忆与证据文件】\n"
                    f"路径：{context_file.resolve()}\n"
                    "要求：必须结合 context.json 中提供的既有参考记忆与证据进行校对。若存在证据冲突，必须在 reason 中明确引用对应证据 ID 说明理由；若多方证据存在不可调和的矛盾或证据不足以决断，必须将 verdict 标为 'unresolved' 并详述冲突细节，严禁自行臆断。\n"
                )
            else:
                prompt_text += (
                    f"\n【参考记忆与证据文件】\n"
                    f"路径：{context_file.resolve()}\n"
                    "要求：必须结合 context.json 中提供的既有参考记忆与证据进行复核。若存在证据冲突或无法确定合理定性，必须在 resolution_note 中明确引用对应证据 ID，且将 action 设为 'needs_translation'，resolved 设为 false，严禁假确认。\n"
                )

        prompt_text += (
            "\n【检索范围与预算】\n"
            "只能使用本批 input/context 和上述当前 KR/LLC 快照作为证据。禁止读取其它运行、其它批次、历史AI译文或校对输出作为术语依据。\n"
            "禁止整份输出 context.json、translation_memory.json 或全库内容。用 Python 按当前 index/path 读取必要证据，仅输出证据 ID 和相关原文/译文片段。\n"
            "额外检索最多 6 次，每次最多输出 3000 字符；优先逐项完成校对并写 result.json。证据不足标 unresolved，不无限搜索或猜测。\n"
            "逐项给出具体理由，不能批量机械 approved。无需读项目代码、测试、旧运行日志或生成过程。\n"
        )

        # 3. Interactive prompt & wait loop with bounded retry
        attempt = 0
        val_errors: list[str] = []

        while attempt < MAX_BATCH_RETRIES:
            attempt += 1
            try:
                curr_st = driver.get_agent_status(agent_name)
            except Exception:
                curr_st = "unknown"
            if curr_st not in {"idle", "done"}:
                curr_st = wait_agent_until_settled(driver, agent_name, deadline_ts=time.time()+min(30, timeout_sec), poll_interval_sec=2)
                if curr_st not in {"idle", "done"}:
                    raise RuntimeError(f"Agent {agent_name} is not settled ({curr_st}); refusing prompt")

            if attempt == 1:
                # Clear accumulated conversation only when this worker is settled.
                clear_result = driver.run_cmd(["agent", "prompt", agent_name, "/clear"], timeout=30)
                safe_write_json(batch_dir / "session_reset.json", {"agent": agent_name, "at": time.time(), "result": clear_result})
                time.sleep(1)
                cleared_status = wait_agent_until_settled(driver, agent_name, deadline_ts=time.time()+30, poll_interval_sec=2)
                if cleared_status not in {"idle", "done"}:
                    raise RuntimeError(f"Reviewer session reset not settled: {cleared_status}")

            current_prompt = prompt_text
            if val_errors:
                current_prompt += f"\n【重要：上一轮输出校验未通过，请针对修复】\n" + "\n".join(val_errors)

            if index_file and index_sha:
                assert_root_input(index_file, index_sha)
            if context_file and context_sha:
                assert_root_input(context_file, context_sha)
            prompt_file = batch_dir / f"prompt_attempt_{attempt:02d}.txt"
            safe_write_text(prompt_file, current_prompt)
            prompt_sha = file_sha256(prompt_file)

            deadline_ts = time.time() + timeout_sec
            prompt_error = None
            try:
                driver.prompt_agent(agent_name, current_prompt, timeout_sec=min(timeout_sec, 60))
            except Exception as p_err:
                prompt_error = p_err
                print(f"[{batch_type} batch {batch_idx:03d}] prompt returned: {p_err}. Entering wait loop...", file=sys.stderr)

            agent_status = wait_agent_until_settled(driver, agent_name, deadline_ts=deadline_ts, poll_interval_sec=4)

            # Record driver real terminal output
            term_log = driver.read_agent_output(agent_name, lines=200)
            log_file = batch_dir / f"agent_output_attempt_{attempt:02d}.log"
            safe_write_text(log_file, str(term_log) if term_log is not None else "")

            if agent_status not in {"idle", "done"}:
                if agent_status == "blocked":
                    raise RuntimeError(f"Reviewer {agent_name} is blocked in pane {pane_id}")
                raise TimeoutError(f"Reviewer timed out (status={agent_status}) on batch {batch_idx}")

            if batch_type == "pending":
                valid_items, val_errors = validate_pending_batch_output(batch_items, result_file)
            else:
                valid_items, val_errors = validate_review_batch_output(batch_items, result_file)

            if prompt_error is not None and val_errors:
                raise RuntimeError(f"Prompt delivery/completion uncertain; refusing automatic resend: {prompt_error}")

            # Post-verification: ensure input.json and run-level files remained completely unmodified
            if file_sha256(input_file) != input_sha:
                raise ValueError(f"Batch input.json was modified during review of {batch_type}_{batch_idx}!")
            assert_root_input(diff_file, diff_sha_pre)
            assert_root_input(trans_file, trans_sha_pre)
            assert_root_input(draft_file, draft_sha_pre)
            if index_file and index_sha:
                assert_root_input(index_file, index_sha)
            if context_file and context_sha:
                assert_root_input(context_file, context_sha)

            if not val_errors:
                # Valid complete response
                resp_sha = file_sha256(result_file)
                receipt = {
                    "batch_type": batch_type,
                    "batch_idx": batch_idx,
                    "status": "success",
                    "agent_name": agent_name,
                    "pane_id": pane_id,
                    "agent_status": agent_status,
                    "attempts": attempt,
                    "items_count": len(valid_items),
                    "input_sha": input_sha,
                    "context_sha": context_sha,
                    "index_sha": index_sha,
                    "request_sha": req_hash,
                    "prompt_file": str(prompt_file.name),
                    "prompt_sha": prompt_sha,
                    "log_file": str(log_file.name),
                    "response_sha": resp_sha,
                    "started_at": started_at,
                    "finished_at": time.time(),
                    "duration_sec": time.time()-started_at,
                    "timestamp": time.time(),
                }
                safe_write_json(receipt_file, receipt)
                print(f"[{batch_type} batch {batch_idx:03d}] Batch verified and settled by {agent_name} ({len(valid_items)} items).")
                return {
                    "batch_type": batch_type,
                    "batch_idx": batch_idx,
                    "items": valid_items,
                    "receipt": receipt,
                }

            print(
                f"[{batch_type} batch {batch_idx:03d}] Attempt #{attempt} failed validation: {val_errors[:3]}",
                file=sys.stderr,
            )

        raise ValueError(
            f"Batch {batch_type}_{batch_idx} failed after {MAX_BATCH_RETRIES} attempts: {'; '.join(val_errors)}"
        )

    except Exception as exc:
        # Run-level corruption takes precedence over worker and output errors.
        try:
            assert_root_input(diff_file, diff_sha_pre)
            assert_root_input(trans_file, trans_sha_pre)
            assert_root_input(draft_file, draft_sha_pre)
            if index_file and index_sha:
                assert_root_input(index_file, index_sha)
            if context_file and context_sha:
                assert_root_input(context_file, context_sha)
        except RootInputIntegrityError as root_exc:
            exc = root_exc
        now = time.time()
        safe_write_json(batch_dir / "error_receipt.json", {
            "status": "failed", "batch_type": batch_type, "batch_idx": batch_idx,
            "agent_name": agent_name, "pane_id": pane_id,
            "attempts": locals().get("attempt", 0), "started_at": started_at,
            "finished_at": now, "duration_sec": now-started_at,
            "error_type": type(exc).__name__, "error_message": str(exc)})
        raise exc


def persist_reviewer_registry(
    registry_file: Path,
    run_dir: Path,
    workers: list[dict[str, Any]],
) -> None:
    """Save reviewer registry atomicaly and stringify all attributes."""
    if not workers:
        return
    reg_data = {
        "run_dir": str(run_dir.resolve()),
        "agent_name": str(workers[0]["agent_name"]),
        "pane_id": str(workers[0]["pane_id"]),
        "max_workers": len(workers),
        "workers": [
            {"agent_name": str(w["agent_name"]), "pane_id": str(w["pane_id"])}
            for w in workers
        ],
        "updated_at": time.time(),
    }
    safe_write_json(registry_file, reg_data)


def initialize_or_resume_workers(
    driver: HerdrDriver,
    run_dir: Path,
    max_workers: int,
    agent_name: str | None,
    pane_id: str | None,
    model: str,
    settings_path: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Initialize or resume worker agents pool and return (workers_list, newly_created_pane_ids)."""
    registry_file = run_dir / "reviewer_registry.json"
    workers: list[dict[str, Any]] = []
    new_pane_ids: list[str] = []

    # 1. Load existing registry if present
    existing_reg: dict[str, Any] = {}
    if registry_file.is_file():
        try:
            existing_reg = safe_read_json(registry_file)
        except Exception:
            existing_reg = {}

    raw_known: list[dict[str, Any]] = []
    if "workers" in existing_reg and isinstance(existing_reg["workers"], list):
        raw_known.extend(existing_reg["workers"])
    elif "agent_name" in existing_reg and "pane_id" in existing_reg:
        raw_known.append({"agent_name": existing_reg["agent_name"], "pane_id": existing_reg["pane_id"]})

    # If explicit CLI agent_name and pane_id provided, ensure it's first and live-verified
    if agent_name and pane_id:
        info = driver.get_agent_info(agent_name)
        live_pane = str(info.get("pane_id", ""))
        if live_pane != str(pane_id):
            raise ValueError(f"Agent pane mismatch for {agent_name}: specified {pane_id} != live {live_pane}")
        raw_known.insert(0, {"agent_name": str(agent_name), "pane_id": str(pane_id)})

    # Deduplicate known workers while preserving order
    seen_names = set()
    known_workers: list[dict[str, Any]] = []
    for w in raw_known:
        w_name = str(w.get("agent_name", "")).strip()
        w_pane = str(w.get("pane_id", "")).strip()
        if not w_name or not w_pane or w_name in seen_names:
            continue
        seen_names.add(w_name)
        known_workers.append({"agent_name": w_name, "pane_id": w_pane})

    # Validate live status for all known workers up to max_workers
    for w in known_workers:
        if len(workers) >= max_workers:
            break
        w_name = w["agent_name"]
        w_pane = w["pane_id"]
        try:
            info = driver.get_agent_info(w_name)
            if str(info.get("pane_id", "")) == w_pane:
                workers.append({"agent_name": w_name, "pane_id": w_pane, "created_now": False})
                print(f"[ReviewPool] Resumed verified worker {w_name} in {w_pane}")
        except Exception as e:
            print(f"[ReviewPool] Notice: could not resume worker {w_name} ({e})")

    # Persist current resumed state immediately
    if workers:
        persist_reviewer_registry(registry_file, run_dir, workers)

    # If we need more workers up to max_workers, spawn them
    needed = max_workers - len(workers)
    if needed > 0:
        root_pane = workers[0]["pane_id"] if workers else None
        ws_id = None
        if not root_pane:
            ws_label = f"limbus-rev-{run_dir.name[:12]}"
            ws_id, root_pane = driver.create_workspace(cwd=ROOT, label=ws_label)
            first_name = generate_agent_name("reviewer")
            driver.start_claude_agent(
                agent_name=first_name,
                pane_id=str(root_pane),
                model=model,
                settings_path=settings_path,
            )
            workers.append({"agent_name": str(first_name), "pane_id": str(root_pane), "created_now": True})
            new_pane_ids.append(str(root_pane))
            persist_reviewer_registry(registry_file, run_dir, workers)
            needed -= 1
            print(f"[ReviewPool] Created root reviewer {first_name} in pane {root_pane}")

        for i in range(needed):
            w_name = generate_agent_name("reviewer")
            split_p = str(driver.split_pane(target_pane_id=str(root_pane), cwd=ROOT, direction="down"))
            tab_pane = str(driver.move_pane_to_new_tab(split_p, label=f"rev_{len(workers)+1}"))
            driver.start_claude_agent(
                agent_name=w_name,
                pane_id=tab_pane,
                model=model,
                settings_path=settings_path,
            )
            workers.append({"agent_name": str(w_name), "pane_id": tab_pane, "created_now": True})
            new_pane_ids.append(tab_pane)
            persist_reviewer_registry(registry_file, run_dir, workers)
            print(f"[ReviewPool] Created worker {w_name} in tab {tab_pane}")

    return workers, new_pane_ids


@track_review_run
def orchestrate_review(
    run_dir: Path,
    agent_name: str | None = None,
    pane_id: str | None = None,
    model: str = DEFAULT_MODEL,
    settings_path: str = DEFAULT_SETTINGS_PATH,
    timeout_sec: int = 1800,
    batch_size: int = BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    driver: HerdrDriver | None = None,
) -> int:
    """Run parallel batch review across worker pool and aggregate artifacts."""
    if driver is None:
        driver = HerdrDriver()

    diff_file = run_dir / "diff.json"
    translations_file = run_dir / "translations.json"
    review_items_file = run_dir / "review_items.json"

    if not diff_file.is_file():
        status_file = run_dir / "status.json"
        if status_file.is_file() and safe_read_json(status_file).get("status") == "up_to_date":
            print("Review skipped: status is up_to_date.")
            return 0
        print(f"Error: {diff_file} not found", file=sys.stderr)
        return 2

    if not translations_file.is_file():
        print(f"Error: {translations_file} not found", file=sys.stderr)
        return 2

    root_diff_sha = file_sha256(diff_file)
    root_trans_sha = file_sha256(translations_file)

    # Check translation_memory index against shards_manifest
    shards_manifest_file = run_dir / "shards_manifest.json"
    tm_file = run_dir / "translation_memory.json"
    tm_index = None
    tm_sha = None
    if shards_manifest_file.is_file():
        manifest_data = safe_read_json(shards_manifest_file)
        expected_tm_sha = manifest_data.get("translation_memory_hash")
        if expected_tm_sha:
            if not tm_file.is_file():
                raise RootInputIntegrityError(f"shards_manifest.json declares translation_memory_hash={expected_tm_sha} but translation_memory.json is missing!")
            tm_sha = file_sha256(tm_file)
            if tm_sha != expected_tm_sha:
                raise RootInputIntegrityError(f"translation_memory.json sha mismatch: {tm_sha} != {expected_tm_sha}")
            tm_index = safe_read_json(tm_file)
    elif tm_file.is_file():
        tm_sha = file_sha256(tm_file)
        tm_index = safe_read_json(tm_file)

    diff_data = safe_read_json(diff_file)
    pending_items = diff_data.get("pending", [])
    raw_translations = safe_read_json(translations_file)

    review_items = []
    if review_items_file.is_file():
        review_items = safe_read_json(review_items_file)
    elif "review" in diff_data:
        review_items = diff_data["review"]

    # 2. Frozen draft persistence and validation
    draft_file = run_dir / "draft_translations.json"
    draft_meta_file = run_dir / "draft_translations.meta.json"

    if draft_file.is_file() and draft_meta_file.is_file():
        meta = safe_read_json(draft_meta_file)
        if meta.get("diff_sha") != root_diff_sha:
            raise ValueError(f"Frozen draft diff_sha mismatch: {meta.get('diff_sha')} != {root_diff_sha}")
        if meta.get("translations_sha") != root_trans_sha:
            raise ValueError(f"Frozen draft translations_sha mismatch: {meta.get('translations_sha')} != {root_trans_sha}")
        draft_trans_sha = file_sha256(draft_file)
        if meta.get("draft_sha") != draft_trans_sha:
            raise ValueError(f"Frozen draft content altered: {meta.get('draft_sha')} != {draft_trans_sha}")
        draft_translations = safe_read_json(draft_file)
        print(f"[Review] Resuming with verified frozen draft translations ({len(draft_translations)} items)")
    else:
        existing_reviewed = run_dir / "reviewed-translations.json"
        draft_translations = raw_translations
        if existing_reviewed.is_file():
            try:
                cand = safe_read_json(existing_reviewed)
                if isinstance(cand, list) and len(cand) == len(pending_items):
                    draft_translations = cand
                    print(f"[Review] Initializing frozen draft from reviewed-translations.json ({len(cand)} items)")
            except Exception:
                pass

        safe_write_json(draft_file, draft_translations)
        draft_trans_sha = file_sha256(draft_file)
        draft_meta = {
            "diff_sha": root_diff_sha,
            "translations_sha": root_trans_sha,
            "draft_sha": draft_trans_sha,
            "count": len(draft_translations),
            "created_at": time.time(),
        }
        safe_write_json(draft_meta_file, draft_meta)

    # 3. Check and single-arg identity validation
    if (agent_name and not pane_id) or (pane_id and not agent_name):
        raise ValueError(
            f"Incomplete reviewer identity provided: agent_name={agent_name!r}, pane_id={pane_id!r}. Both must be provided together."
        )

    # Build pending items payload for review (carrying term_notes from translations)
    trans_map = {}
    notes_map = {}
    for t in draft_translations:
        k = (t.get("file"), json.dumps(t.get("path"), sort_keys=True))
        trans_map[k] = t.get("translation", "")
        if "term_notes" in t:
            notes_map[k] = t["term_notes"]
    if notes_map and raw_translations:
        for t in raw_translations:
            k = (t.get("file"), json.dumps(t.get("path"), sort_keys=True))
            if "term_notes" in t and k not in notes_map:
                notes_map[k] = t["term_notes"]

    annotated_pending = []
    for idx, p in enumerate(pending_items):
        k = (p.get("file"), json.dumps(p.get("path"), sort_keys=True))
        item_entry = {
            "index": idx,
            "file": p.get("file"),
            "path": p.get("path"),
            "source": p.get("source"),
            "translation": trans_map.get(k, ""),
        }
        if k in notes_map:
            item_entry["term_notes"] = notes_map[k]
        annotated_pending.append(item_entry)

    # Prepare batch tasks
    batches_dir = run_dir / "reviews/batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    snapshot_kr_dir = run_dir / "snapshot/source/KR"
    snapshot_llc_dir = run_dir / "snapshot/llc/LLC_zh-CN"

    num_pending_batches = math.ceil(len(annotated_pending) / batch_size) if annotated_pending else 0
    num_review_batches = math.ceil(len(review_items) / batch_size) if review_items else 0

    batch_tasks: list[dict[str, Any]] = []

    for b_idx in range(num_pending_batches):
        chunk = annotated_pending[b_idx * batch_size : (b_idx + 1) * batch_size]
        b_dir = batches_dir / f"pending_batch_{b_idx:04d}"
        b_dir.mkdir(parents=True, exist_ok=True)
        ctx_file = None
        ctx_sha = None
        if tm_index is not None:
            ctx_file = b_dir / "context.json"
            chunk_context = [
                tm.query_item_evidence(tm_index, it.get("file", ""), it.get("path", []), it.get("source", ""))
                for it in chunk
            ]
            safe_write_json(ctx_file, chunk_context)
            ctx_sha = file_sha256(ctx_file)
        temp_chunk_file = b_dir / "temp_input.json"
        safe_write_json(temp_chunk_file, chunk)
        chunk_sha = file_sha256(temp_chunk_file)
        temp_chunk_file.unlink(missing_ok=True)
        batch_tasks.append({
            "batch_type": "pending",
            "batch_idx": b_idx,
            "batch_items": chunk,
            "batch_dir": b_dir,
            "chunk_sha": chunk_sha,
            "context_file": ctx_file,
            "context_sha": ctx_sha,
            "index_file": tm_file if tm_index is not None else None,
            "index_sha": tm_sha,
        })

    for b_idx in range(num_review_batches):
        chunk = review_items[b_idx * batch_size : (b_idx + 1) * batch_size]
        b_dir = batches_dir / f"review_batch_{b_idx:04d}"
        b_dir.mkdir(parents=True, exist_ok=True)
        ctx_file = None
        ctx_sha = None
        if tm_index is not None:
            ctx_file = b_dir / "context.json"
            chunk_context = [
                tm.query_item_evidence(tm_index, it.get("file", ""), it.get("path", []), it.get("source", ""))
                for it in chunk
            ]
            safe_write_json(ctx_file, chunk_context)
            ctx_sha = file_sha256(ctx_file)
        temp_chunk_file = b_dir / "temp_input.json"
        safe_write_json(temp_chunk_file, chunk)
        chunk_sha = file_sha256(temp_chunk_file)
        temp_chunk_file.unlink(missing_ok=True)
        batch_tasks.append({
            "batch_type": "review",
            "batch_idx": b_idx,
            "batch_items": chunk,
            "batch_dir": b_dir,
            "chunk_sha": chunk_sha,
            "context_file": ctx_file,
            "context_sha": ctx_sha,
            "index_file": tm_file if tm_index is not None else None,
            "index_sha": tm_sha,
        })

    # 4. Check reusable verified receipts before dispatching
    pending_results_map: dict[int, list[dict[str, Any]]] = {}
    review_results_map: dict[int, list[dict[str, Any]]] = {}
    batch_receipts_map: dict[tuple[str, int], dict[str, Any]] = {}
    unresolved_items: list[dict[str, Any]] = []

    unprocessed_tasks: list[dict[str, Any]] = []

    for task in batch_tasks:
        b_type = task["batch_type"]
        b_idx = task["batch_idx"]
        cached = check_existing_verified_receipt(
            batch_dir=task["batch_dir"],
            batch_type=b_type,
            batch_idx=b_idx,
            batch_items=task["batch_items"],
            input_sha_pre=task["chunk_sha"],
            context_sha=task.get("context_sha"),
            index_sha=task.get("index_sha"),
        )
        if cached is not None:
            batch_receipts_map[(b_type, b_idx)] = cached["receipt"]
            if b_type == "pending":
                pending_results_map[b_idx] = cached["items"]
                for it in cached["items"]:
                    if it.get("verdict") == "unresolved":
                        unresolved_items.append(it)
            else:
                review_results_map[b_idx] = cached["items"]
                for it in cached["items"]:
                    if it.get("resolved") is not True:
                        unresolved_items.append(it)
        else:
            unprocessed_tasks.append(task)

    print(
        f"[Review] Total batches: {len(batch_tasks)} "
        f"({num_pending_batches} pending, {num_review_batches} review). "
        f"Reused verified: {len(batch_receipts_map)}, Remaining to process: {len(unprocessed_tasks)}"
    )

    new_pane_ids: list[str] = []

    # Explicit identities are validated even for a no-work resume, without spawning.
    if not unprocessed_tasks and agent_name and pane_id:
        info = driver.get_agent_info(agent_name)
        if str(info.get("pane_id", "")) != str(pane_id):
            raise ValueError(f"Agent pane mismatch for {agent_name}: specified {pane_id} != live {info.get('pane_id')}")
        registry = run_dir / "reviewer_registry.json"
        saved = safe_read_json(registry).get("workers", []) if registry.is_file() else []
        retained = [w for w in saved if w.get("agent_name") != agent_name]
        persist_reviewer_registry(registry, run_dir, [
            {"agent_name": agent_name, "pane_id": pane_id}, *retained])

    # 5. Process remaining batches via worker pool
    if unprocessed_tasks:
        workers, new_pane_ids = initialize_or_resume_workers(
            driver=driver,
            run_dir=run_dir,
            max_workers=max_workers,
            agent_name=agent_name,
            pane_id=pane_id,
            model=model,
            settings_path=settings_path,
        )

        task_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        for t in unprocessed_tasks:
            task_queue.put(t)

        lock = threading.Lock()
        worker_error: list[Exception] = []

        progress_file = run_dir / "reviews/pool_progress.json"
        progress_lock = threading.Lock()
        root_tamper_event = threading.Event()
        root_tamper_errors: list[Exception] = []
        quarantined_workers: set[str] = set()
        active_workers: set[str] = {w["agent_name"] for w in workers}
        batch_errors_map: dict[tuple[str, int], Exception] = {}

        tasks_progress: dict[str, dict[str, Any]] = {}
        for t in batch_tasks:
            key = f"{t['batch_type']}_{t['batch_idx']:04d}"
            if (t["batch_type"], t["batch_idx"]) in batch_receipts_map:
                rc = batch_receipts_map[(t["batch_type"], t["batch_idx"])]
                tasks_progress[key] = {
                    "status": "completed",
                    "worker": rc.get("agent_name", "cached"),
                    "attempts": rc.get("attempts", 1),
                    "duration_sec": rc.get("duration_sec"),
                    "error_type": None,
                    "error_message": None,
                    "updated_at": time.time(),
                }
            else:
                tasks_progress[key] = {
                    "status": "pending",
                    "worker": None,
                    "attempts": 0,
                    "duration_sec": None,
                    "error_type": None,
                    "error_message": None,
                    "updated_at": time.time(),
                }

        def save_pool_progress() -> None:
            completed_c = sum(1 for v in tasks_progress.values() if v["status"] == "completed")
            failed_c = sum(1 for v in tasks_progress.values() if v["status"] == "failed")
            running_c = sum(1 for v in tasks_progress.values() if v["status"] == "running")
            prog_payload = {
                "status": "failed" if failed_c else "running",
                "pid": os.getpid(),
                "updated_at": time.time(),
                "note": "Process liveness must be verified via PID/OS; do not trust running state without checking process existence.",
                "total_tasks": len(tasks_progress),
                "completed_tasks": completed_c,
                "failed_tasks": failed_c,
                "running_tasks": running_c,
                "active_workers": sorted(list(active_workers)),
                "quarantined_workers": sorted(list(quarantined_workers)),
                "tasks": tasks_progress,
            }
            safe_write_json(progress_file, prog_payload)

        save_pool_progress()

        def is_root_tampering(exc: Exception) -> bool:
            return isinstance(exc, RootInputIntegrityError)

        def worker_loop(w_info: dict[str, Any]) -> None:
            w_name = w_info["agent_name"]
            w_pane = w_info["pane_id"]
            while not root_tamper_event.is_set():
                try:
                    task = task_queue.get_nowait()
                except queue.Empty:
                    break

                b_type = task["batch_type"]
                b_idx = task["batch_idx"]
                key = f"{b_type}_{b_idx:04d}"

                with progress_lock:
                    tasks_progress[key]["status"] = "running"
                    tasks_progress[key]["worker"] = w_name
                    tasks_progress[key]["updated_at"] = time.time()
                    save_pool_progress()

                try:
                    res = process_batch(
                        driver=driver,
                        agent_name=w_name,
                        pane_id=w_pane,
                        batch_dir=task["batch_dir"],
                        batch_type=b_type,
                        batch_idx=b_idx,
                        batch_items=task["batch_items"],
                        timeout_sec=timeout_sec,
                        input_sha_pre=task["chunk_sha"],
                        diff_file=diff_file,
                        diff_sha_pre=root_diff_sha,
                        trans_file=translations_file,
                        trans_sha_pre=root_trans_sha,
                        draft_file=draft_file,
                        draft_sha_pre=draft_trans_sha,
                        snapshot_kr_dir=snapshot_kr_dir,
                        snapshot_llc_dir=snapshot_llc_dir,
                        context_file=task.get("context_file"),
                        context_sha=task.get("context_sha"),
                        index_file=task.get("index_file"),
                        index_sha=task.get("index_sha"),
                    )
                    with lock:
                        batch_receipts_map[(b_type, b_idx)] = res["receipt"]
                        if b_type == "pending":
                            pending_results_map[b_idx] = res["items"]
                            for it in res["items"]:
                                if it.get("verdict") == "unresolved":
                                    unresolved_items.append(it)
                        else:
                            review_results_map[b_idx] = res["items"]
                            for it in res["items"]:
                                if it.get("resolved") is not True:
                                    unresolved_items.append(it)
                    with progress_lock:
                        tasks_progress[key]["status"] = "completed"
                        tasks_progress[key]["worker"] = w_name
                        tasks_progress[key]["attempts"] = res["receipt"].get("attempts", 1)
                        tasks_progress[key]["duration_sec"] = res["receipt"].get("duration_sec")
                        tasks_progress[key]["error_type"] = None
                        tasks_progress[key]["error_message"] = None
                        tasks_progress[key]["updated_at"] = time.time()
                        save_pool_progress()
                except Exception as exc:
                    with progress_lock:
                        tasks_progress[key]["status"] = "failed"
                        metrics_file = task["batch_dir"] / "error_receipt.json"
                        if metrics_file.is_file():
                            metrics = safe_read_json(metrics_file)
                            tasks_progress[key]["attempts"] = metrics.get("attempts", 0)
                            tasks_progress[key]["duration_sec"] = metrics.get("duration_sec")
                        tasks_progress[key]["worker"] = w_name
                        tasks_progress[key]["error_type"] = type(exc).__name__
                        tasks_progress[key]["error_message"] = str(exc)
                        tasks_progress[key]["updated_at"] = time.time()
                        if is_root_tampering(exc):
                            root_tamper_event.set()
                            root_tamper_errors.append(exc)
                            # Drain task_queue immediately
                            while not task_queue.empty():
                                try:
                                    task_queue.get_nowait()
                                    task_queue.task_done()
                                except queue.Empty:
                                    break
                            save_pool_progress()
                            print(f"[FATAL] Global root data tampering detected during batch {b_type}_{b_idx}: {exc}. Halting all workers immediately!", file=sys.stderr)
                            break
                        else:
                            # Worker-specific fault: quarantine worker, remaining workers continue draining queue
                            quarantined_workers.add(w_name)
                            active_workers.discard(w_name)
                            batch_errors_map[(b_type, b_idx)] = exc
                            save_pool_progress()
                            print(f"[QUARANTINE] Worker {w_name} faulted on batch {b_type}_{b_idx}: {exc}. Quarantining worker; remaining workers continue.", file=sys.stderr)
                            break
                finally:
                    task_queue.task_done()

        threads = []
        for w in workers:
            th = threading.Thread(target=worker_loop, args=(w,), daemon=True)
            threads.append(th)
            th.start()

        for th in threads:
            th.join()

        with progress_lock:
            save_pool_progress()

        if root_tamper_event.is_set():
            raise RuntimeError(f"Global fatal error: Root dataset tampered: {root_tamper_errors[0]}")
        if batch_errors_map:
            raise RuntimeError(f"Review failed: {len(batch_errors_map)} batch(es) failed: {batch_errors_map}")

    # Verify all batches were processed
    missing_pending = [i for i in range(num_pending_batches) if i not in pending_results_map]
    missing_review = [i for i in range(num_review_batches) if i not in review_results_map]
    if missing_pending or missing_review:
        raise RuntimeError(
            f"Missing completed batches: pending={missing_pending}, review={missing_review}"
        )

    # 6. Check for unresolved blocking items
    if unresolved_items:
        print(f"Error: {len(unresolved_items)} items are unresolved in review batches!", file=sys.stderr)
        safe_write_json(run_dir / "unresolved_review_items.json", unresolved_items)
        return 2

    assert_root_input(diff_file, root_diff_sha)
    assert_root_input(translations_file, root_trans_sha)
    assert_root_input(draft_file, draft_trans_sha)
    if tm_sha is not None:
        assert_root_input(tm_file, tm_sha)

    # 7. Final Aggregation
    all_pending_results: list[dict[str, Any]] = []
    for b_idx in range(num_pending_batches):
        all_pending_results.extend(pending_results_map[b_idx])

    all_review_results: list[dict[str, Any]] = []
    for b_idx in range(num_review_batches):
        all_review_results.extend(review_results_map[b_idx])

    batch_receipts: list[dict[str, Any]] = []
    for b_idx in range(num_pending_batches):
        batch_receipts.append(batch_receipts_map[("pending", b_idx)])
    for b_idx in range(num_review_batches):
        batch_receipts.append(batch_receipts_map[("review", b_idx)])

    # Output reviewed-translations.json
    final_reviewed_trans = []
    for it in all_pending_results:
        final_reviewed_trans.append({
            "file": it["file"],
            "path": it["path"],
            "source": it["source"],
            "translation": it["translation"],
        })

    reviewed_file = run_dir / "reviewed-translations.json"
    safe_write_json(reviewed_file, final_reviewed_trans)
    reviewed_sha = file_sha256(reviewed_file)

    # Output review.json
    review_report = {
        "hashes": {
            "diff.json": root_diff_sha,
            "translations.json": root_trans_sha,
            "reviewed-translations.json": reviewed_sha,
        },
        "dispositions": all_review_results,
    }
    safe_write_json(run_dir / "review.json", review_report)

    # Output semantic-review-receipt.json
    primary_agent = agent_name or (batch_receipts[0]["agent_name"] if batch_receipts else "pool")
    primary_pane = pane_id or (batch_receipts[0]["pane_id"] if batch_receipts else "")

    semantic_receipt = {
        "status": "success",
        "agent_name": primary_agent,
        "pane_id": primary_pane,
        "draft_translations_sha": draft_trans_sha,
        "root_diff_sha": root_diff_sha,
        "root_translations_sha": root_trans_sha,
        "reviewed_translations_sha": reviewed_sha,
        "total_pending_items": len(annotated_pending),
        "total_pending_batches": num_pending_batches,
        "total_review_items": len(review_items),
        "total_review_batches": num_review_batches,
        "batch_receipts": batch_receipts,
    }
    safe_write_json(run_dir / "semantic-review-receipt.json", semantic_receipt)

    print(
        f"Review successfully completed and aggregated: "
        f"{len(annotated_pending)} pending items across {num_pending_batches} batches, "
        f"{len(review_items)} review items across {num_review_batches} batches."
    )

    # Close newly created panes only if all succeeded and settled
    for p_id in new_pane_ids:
        try:
            driver.close_pane(p_id)
        except Exception:
            pass

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-agent parallel batch reviewer")
    parser.add_argument("--run-dir", type=Path, required=True, help="Workflow run directory")
    parser.add_argument("--agent-name", type=str, default=None, help="Existing reviewer agent name")
    parser.add_argument("--pane-id", type=str, default=None, help="Existing reviewer pane ID")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Model name")
    parser.add_argument("--settings", type=str, default=DEFAULT_SETTINGS_PATH, help="Path to settings")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds per batch")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Items per batch")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Parallel worker agents (default: 6)")

    args = parser.parse_args()

    run_dir = args.run_dir
    if not run_dir and "WORKFLOW_RUN_DIR" in os.environ:
        run_dir = Path(os.environ["WORKFLOW_RUN_DIR"])

    if not run_dir:
        print("Error: --run-dir is required", file=sys.stderr)
        return 2

    return orchestrate_review(
        run_dir=run_dir,
        agent_name=args.agent_name,
        pane_id=args.pane_id,
        model=args.model,
        settings_path=args.settings,
        timeout_sec=args.timeout,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    sys.exit(main())
