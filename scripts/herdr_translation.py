#!/usr/bin/env python3
"""Herdr Translation & Review Orchestrator.

Orchestrates interactive Claude Code with Gemini in Herdr default session:
- Explicit --session default on every herdr CLI invocation.
- Dedicated workspace created at trusted project root: herdr --session default workspace create --cwd <ROOT> --label limbus-<run> --no-focus
- Root pane ID obtained strictly from json.result.root_pane.pane_id.
- Dynamic pane pool: strictly maintains at most concurrency active panes (never pre-splits all shards).
  Panes are split with direction down from root_pane_id inside a lock, verified, and closed before the next shard.
- Unified worker function: no duplicate process_translation_shard / worker implementations.
- Reviewer runs in a single dedicated review workspace.
- Reviewer prompt explicitly prohibits faking resolved=true: any uncertain or needs_translation item must have resolved=false/needs_translation so that validation gate blocks publication.
- Strictly records and asserts that all inputs (review_agent copies and root diff.json/translations.json) are completely unmodified before and after execution.
- Retains pane and logs on error; closes only upon successful verification.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import localization

DEFAULT_MODEL = "gemini-account/gemini-3.8-flash-high"
DEFAULT_CONCURRENCY = 6
DEFAULT_TIMEOUT_SEC = 600
DEFAULT_SETTINGS_PATH = "/Users/kidjourney/.config/limbus-workflow/gemini-settings.json"


def file_sha256(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
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


def generate_agent_name(prefix: str) -> str:
    """Generate a unique agent name strictly matching [a-z][a-z0-9_-]{0,31}."""
    uid = uuid.uuid4().hex[:8]
    clean_prefix = re.sub(r"[^a-z0-9_-]", "", prefix.lower())[:20]
    name = f"{clean_prefix}_{uid}"
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        name = f"ag_{uid}"
    return name


class HerdrDriver:
    """Strict wrapper around Herdr CLI operating on the default session."""

    def __init__(self, herdr_bin: str = "herdr", session: str = "default"):
        self.herdr_bin = herdr_bin
        self.session = session
        self._lock = threading.Lock()

    def run_cmd(self, args: list[str], timeout: int = 60) -> dict[str, Any]:
        full_cmd = [self.herdr_bin, "--session", self.session, *args]
        res = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if res.returncode != 0:
            raise RuntimeError(
                f"Herdr command failed (exit code {res.returncode}): {' '.join(full_cmd)}\n"
                f"stderr: {res.stderr}\nstdout: {res.stdout}"
            )
        try:
            payload = json.loads(res.stdout)
        except Exception as exc:
            raise RuntimeError(
                f"Herdr returned non-JSON stdout: {res.stdout}\ncmd: {' '.join(full_cmd)}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(f"Herdr output must be a JSON object, got: {type(payload)}")
        return payload

    def create_workspace(self, cwd: Path, label: str) -> tuple[str, str]:
        """Create a dedicated workspace and return (workspace_id, root_pane_id)."""
        args = [
            "workspace", "create",
            "--cwd", str(cwd.resolve()),
            "--label", label,
            "--no-focus",
        ]
        data = self.run_cmd(args, timeout=30)
        try:
            ws_id = data["result"]["workspace"]["workspace_id"]
            root_pane_id = data["result"]["root_pane"]["pane_id"]
            return ws_id, root_pane_id
        except KeyError as exc:
            raise RuntimeError(f"Unexpected schema from workspace create: {data}") from exc

    def split_pane(self, target_pane_id: str, cwd: Path, direction: str = "down") -> str:
        """Split pane explicitly targeting target_pane_id and return new pane_id (thread-safe)."""
        args = [
            "pane", "split",
            "--pane", target_pane_id,
            "--direction", direction,
            "--cwd", str(cwd.resolve()),
            "--no-focus",
        ]
        with self._lock:
            data = self.run_cmd(args, timeout=30)
        try:
            return data["result"]["pane"]["pane_id"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected schema from pane split: {data}") from exc

    def move_pane_to_new_tab(self, pane_id: str, label: str) -> str:
        """Move pane to a new tab within the same workspace to guarantee full viewport."""
        args = [
            "pane", "move", pane_id,
            "--new-tab",
            "--label", label,
            "--no-focus",
        ]
        with self._lock:
            data = self.run_cmd(args, timeout=30)
        try:
            return data["result"]["move_result"]["pane"]["pane_id"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected schema from pane move: {data}") from exc

    def start_claude_agent(
        self,
        agent_name: str,
        pane_id: str,
        model: str = DEFAULT_MODEL,
        settings_path: str = DEFAULT_SETTINGS_PATH,
        timeout_ms: int = 45000,
        max_busy_retries: int = 5,
        busy_retry_delay_sec: float = 1.0,
    ) -> None:
        """Start interactive Claude Code inside specified pane with bounded retry on agent_pane_busy."""
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", agent_name):
            raise ValueError(f"Invalid agent name format: {agent_name}")

        # Check if an agent with this name already exists in target pane and is ready
        try:
            info = self.get_agent_info(agent_name)
            if str(info.get("pane_id", "")) == str(pane_id):
                st = str(info.get("agent_status", ""))
                if st in {"idle", "done", "working"}:
                    print(f"[Herdr] Agent {agent_name} already running in {pane_id} (status: {st}).")
                    return
        except Exception:
            pass

        agent_args = [
            "--model", model,
            "--dangerously-skip-permissions",
            "--settings", settings_path,
        ]

        args = [
            "agent", "start", agent_name,
            "--kind", "claude",
            "--pane", pane_id,
            "--timeout", str(timeout_ms),
            "--",
            *agent_args,
        ]

        attempt = 0
        while True:
            try:
                self.run_cmd(args, timeout=int(timeout_ms / 1000) + 15)
                return
            except RuntimeError as exc:
                err_str = str(exc)
                if "agent_pane_busy" in err_str:
                    attempt += 1
                    # Check if the agent actually started despite error
                    try:
                        info = self.get_agent_info(agent_name)
                        if str(info.get("pane_id", "")) == str(pane_id):
                            print(f"[Herdr] Agent {agent_name} verified started in {pane_id} after busy notice.")
                            return
                    except Exception:
                        pass

                    if attempt <= max_busy_retries:
                        print(
                            f"[Herdr] Pane {pane_id} busy starting {agent_name} (attempt {attempt}/{max_busy_retries}), retrying in {busy_retry_delay_sec}s...",
                            file=sys.stderr,
                        )
                        time.sleep(busy_retry_delay_sec)
                        continue
                raise

    def prompt_agent(self, agent_name: str, prompt_text: str, timeout_sec: int = 600) -> None:
        """Submit prompt. Note: does not rely exclusively on --wait for overall task completion."""
        timeout_ms = timeout_sec * 1000
        args = [
            "agent", "prompt", agent_name, prompt_text,
            "--wait",
            "--timeout", str(timeout_ms),
        ]
        self.run_cmd(args, timeout=timeout_sec + 30)

    def wait_agent(self, agent_name: str, until: list[str] | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
        """Wait until an agent reaches one of the requested states using herdr agent wait."""
        args = ["agent", "wait", agent_name, "--timeout", str(timeout_ms)]
        if until:
            for st in until:
                args.extend(["--until", st])
        return self.run_cmd(args, timeout=int(timeout_ms / 1000) + 15)

    def get_agent_info(self, agent_name: str) -> dict[str, Any]:
        """Query agent details strictly from json.result.agent."""
        args = ["agent", "get", agent_name]
        data = self.run_cmd(args, timeout=20)
        try:
            return data["result"]["agent"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected schema from agent get: {data}") from exc

    def get_agent_status(self, agent_name: str) -> str:
        """Query agent status strictly from json.result.agent.agent_status."""
        info = self.get_agent_info(agent_name)
        return str(info.get("agent_status", ""))

    def read_agent_output(self, agent_name: str, lines: int = 150) -> str:
        """Read recent terminal output from agent for evidence."""
        cmd = [
            self.herdr_bin, "--session", self.session,
            "agent", "read", agent_name,
            "--source", "recent-unwrapped",
            "--lines", str(lines),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        return res.stdout if res.returncode == 0 else res.stderr

    def close_pane(self, pane_id: str) -> None:
        """Close pane upon successful verification."""
        cmd = [self.herdr_bin, "--session", self.session, "pane", "close", pane_id]
        subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)


MAX_CONTINUATION_ATTEMPTS = 3


def validate_shard_output(
    input_file: Path,
    output_file: Path,
    shard_id: str,
) -> list[dict[str, Any]]:
    """Strictly validate shard translation output against input items."""
    if not output_file.is_file():
        raise FileNotFoundError(f"Shard {shard_id} did not produce translations.json")

    translations = safe_read_json(output_file)
    if not isinstance(translations, list):
        raise TypeError(f"Shard {shard_id} output must be a JSON array")

    inputs = safe_read_json(input_file)
    if len(translations) != len(inputs):
        raise ValueError(
            f"Shard {shard_id} length mismatch: got {len(translations)}, expected {len(inputs)}"
        )

    for idx, (inp, trans) in enumerate(zip(inputs, translations)):
        if not isinstance(trans, dict):
            raise TypeError(f"Shard {shard_id} item #{idx} is not a dict")
        if trans.get("file") != inp.get("file") or trans.get("path") != inp.get("path"):
            raise ValueError(f"Shard {shard_id} item #{idx} key mismatch")
        if trans.get("source") != inp.get("source"):
            raise ValueError(f"Shard {shard_id} item #{idx} source tampered")
        trans_text = trans.get("translation")
        if not isinstance(trans_text, str) or not trans_text.strip():
            raise ValueError(f"Shard {shard_id} item #{idx} translation is empty or not string")
        localization.validate_translation(inp["source"], trans_text)

    return translations


def wait_agent_until_settled(
    driver: HerdrDriver,
    agent_name: str,
    deadline_ts: float,
    poll_interval_sec: int = 5,
) -> str:
    """Wait until agent transitions from working/prompting to settled state (idle, done, or blocked)."""
    while time.time() < deadline_ts:
        remain_sec = max(1, int(deadline_ts - time.time()))
        wait_ms = min(remain_sec * 1000, poll_interval_sec * 1000)
        try:
            driver.wait_agent(agent_name, timeout_ms=wait_ms)
        except Exception:
            pass

        try:
            status = driver.get_agent_status(agent_name)
        except Exception as exc:
            print(f"Warning: failed to query status for {agent_name}: {exc}", file=sys.stderr)
            time.sleep(2)
            continue

        if status in {"idle", "done", "blocked"}:
            return status

    # Final check at deadline
    try:
        status = driver.get_agent_status(agent_name)
        return status
    except Exception:
        return "timeout"


def execute_translation_shard(
    shard: dict[str, Any],
    driver: HerdrDriver,
    root_pane_id: str,
    model: str,
    settings_path: str,
    timeout_sec: int,
    evidence_dir: Path,
    abort_event: threading.Event | None = None,
    agent_registry: dict[str, Any] | None = None,
    registry_file: Path | None = None,
    registry_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """Execute a single translation shard or reconnect via registry; close pane only on verified success."""
    shard_id = shard["shard_id"]
    s_dir = Path(shard["directory"])
    input_file = s_dir / "input.json"
    output_file = s_dir / "translations.json"
    prompt_file = s_dir / "prompt.txt"

    if abort_event is not None and abort_event.is_set():
        raise RuntimeError(f"Shard {shard_id} skipped because workflow circuit breaker is tripped")

    if not input_file.is_file() or not prompt_file.is_file():
        raise FileNotFoundError(f"Missing input or prompt file in {s_dir}")

    expected_input_hash = shard["input_hash"]
    initial_input_hash = file_sha256(input_file)
    if initial_input_hash != expected_input_hash:
        raise ValueError(f"Shard {shard_id} input hash tampered before execution: {initial_input_hash} != {expected_input_hash}")

    reference_files = {}
    context_receipt = s_dir / "translation_context_receipt.json"
    attempt_receipt = s_dir / "translation_context_attempt.json"
    if "context_hash" in shard:
        reference_files = {
            str(s_dir / "context.json"): shard["context_hash"],
            str(s_dir.parent.parent / shard["index_file"]): shard["index_hash"],
            str(prompt_file): file_sha256(prompt_file),
            str(s_dir / "glossary.json"): file_sha256(s_dir / "glossary.json"),
            str(input_file): initial_input_hash,
        }

    def verify_references():
        for name, expected in reference_files.items():
            if file_sha256(Path(name)) != expected:
                raise ValueError(f"Translation reference modified: {name}")

    verify_references()
    if reference_files:
        old_attempt = safe_read_json(attempt_receipt) if attempt_receipt.is_file() else None
        registered = (agent_registry or {}).get(shard_id)
        if registered and old_attempt != reference_files:
            raise ValueError("Registered translation has different or missing reference identity; use a new run")
        cached = safe_read_json(context_receipt) if context_receipt.is_file() else {}
        can_reuse = (cached.get("references") == reference_files and output_file.is_file()
                     and cached.get("output_sha256") == file_sha256(output_file))
        if output_file.is_file() and not can_reuse and not registered:
            output_file.rename(s_dir / f"translations.pre-context-{time.time_ns()}.json")
        safe_write_json(attempt_receipt, reference_files)

    inputs = safe_read_json(input_file)
    input_count = len(inputs)

    # Check if this shard has already produced valid output (can be reused)
    if output_file.is_file():
        try:
            if reference_files and not can_reuse:
                raise ValueError("No matching translation context receipt")
            valid_translations = validate_shard_output(input_file, output_file, shard_id)
            verify_references()
            out_hash = file_sha256(output_file)
            print(f"[{shard_id}] Reusing already verified translations.json ({len(valid_translations)} items)")
            return {
                "shard_id": shard_id,
                "status": "success",
                "reused": True,
                "agent_name": agent_registry.get(shard_id, {}).get("agent_name", "pre_existing") if agent_registry else "pre_existing",
                "pane_id": agent_registry.get(shard_id, {}).get("pane_id", "") if agent_registry else "",
                "model": model,
                "input_sha256": initial_input_hash,
                "output_sha256": out_hash,
                "references": reference_files,
                "items_count": len(valid_translations),
            }
        except Exception as reuse_err:
            print(f"[{shard_id}] Pre-existing translations.json is invalid ({reuse_err}), re-executing...", file=sys.stderr)

    # Check registry for reconnecting to an already-started agent
    reconnected = False
    registered_info = agent_registry.get(shard_id) if agent_registry else None
    if registered_info:
        reg_input_sha = registered_info.get("input_sha")
        if reg_input_sha and reg_input_sha != initial_input_hash:
            raise ValueError(
                f"Registry input_sha mismatch for {shard_id}: {reg_input_sha} != {initial_input_hash}"
            )
        agent_name = registered_info["agent_name"]
        pane_id = registered_info.get("pane_id")

        # Verify live agent name and pane_id match registry before interacting
        live_info = driver.get_agent_info(agent_name)
        live_pane = live_info.get("pane_id")
        live_name = live_info.get("agent_name") or live_info.get("name")
        if live_name != agent_name:
            raise RuntimeError(f"Live agent name mismatch for {shard_id}: expected {agent_name}, got {live_name}")
        if live_pane != pane_id:
            raise RuntimeError(f"Live agent pane mismatch for {agent_name}: expected {pane_id}, got {live_pane}")

        reconnected = True
        print(f"[{shard_id}] Reconnecting to verified registered agent {agent_name} in pane {pane_id}")
    else:
        agent_name = generate_agent_name(f"tr_{shard_id}")
        pane_id = None

    step_succeeded = False
    deadline_ts = time.time() + timeout_sec

    try:
        if not reconnected:
            # Split pane down from root_pane_id inside lock
            initial_pane_id = driver.split_pane(target_pane_id=root_pane_id, cwd=ROOT, direction="down")
            print(f"[{shard_id}] Split active pane {initial_pane_id}")

            # Move to dedicated tab within the same workspace to ensure full viewport
            pane_id = driver.move_pane_to_new_tab(initial_pane_id, label=shard_id)
            print(f"[{shard_id}] Moved pane to new tab {pane_id} for agent {agent_name}")

            driver.start_claude_agent(
                agent_name=agent_name,
                pane_id=pane_id,
                model=model,
                settings_path=settings_path,
            )
            print(f"[{shard_id}] Started agent {agent_name}")

            # Atomically record newly started agent in run-specific registry
            if registry_file and registry_lock:
                update_run_registry(
                    registry_file=registry_file,
                    shard_id=shard_id,
                    agent_name=agent_name,
                    pane_id=pane_id,
                    input_sha=initial_input_hash,
                    lock=registry_lock,
                )

            prompt_content = prompt_file.read_text(encoding="utf-8")
            try:
                # Send prompt with initial wait
                verify_references()
                driver.prompt_agent(agent_name, prompt_content, timeout_sec=min(timeout_sec, 60))
            except Exception as p_err:
                print(f"[{shard_id}] prompt --wait returned: {p_err}. Entering reliable status tracking loop...", file=sys.stderr)

        # Loop: reliable wait + bounded continuation prompts based on progress
        max_no_progress = 3
        max_total_continuations = math.ceil(input_count / 50) + 3
        no_progress_count = 0
        total_continuations = 0
        last_valid_prefix = validate_shard_prefix(input_file, output_file, shard_id)
        translations = None

        while True:
            if abort_event is not None and abort_event.is_set():
                raise RuntimeError(f"Shard {shard_id} aborted by tripped circuit breaker")

            agent_status = wait_agent_until_settled(driver, agent_name, deadline_ts=deadline_ts, poll_interval_sec=5)
            terminal_log = driver.read_agent_output(agent_name, lines=150)
            (evidence_dir / f"{shard_id}.terminal.log").write_text(terminal_log, encoding="utf-8")

            # Check if output is complete and valid
            val_err_msg = ""
            if output_file.is_file():
                try:
                    translations = validate_shard_output(input_file, output_file, shard_id)
                    # Must be settled before accepting and closing pane
                    if agent_status in {"idle", "done"}:
                        print(f"[{shard_id}] translations.json produced and verified ({len(translations)} items). Agent settled.")
                        break
                    else:
                        print(f"[{shard_id}] translations.json valid but agent still in '{agent_status}', waiting for settled state...")
                except Exception as val_err:
                    val_err_msg = str(val_err)
                    print(f"[{shard_id}] translations.json validation failed: {val_err}", file=sys.stderr)

            if time.time() >= deadline_ts:
                raise TimeoutError(f"Shard {shard_id} timed out after {timeout_sec}s. Last status: '{agent_status}'")

            if agent_status == "blocked":
                raise RuntimeError(f"Agent {agent_name} is blocked and requires user intervention")

            if agent_status in {"idle", "done"}:
                # Agent stopped without producing valid complete output
                current_valid_prefix = validate_shard_prefix(input_file, output_file, shard_id)
                if current_valid_prefix > last_valid_prefix:
                    no_progress_count = 0
                    last_valid_prefix = current_valid_prefix
                    print(f"[{shard_id}] Progress detected: valid prefix grew to {last_valid_prefix}/{input_count}")
                else:
                    no_progress_count += 1
                    print(f"[{shard_id}] No progress count: {no_progress_count}/{max_no_progress} (prefix at {last_valid_prefix}/{input_count})")

                if no_progress_count >= max_no_progress or total_continuations >= max_total_continuations:
                    raise RuntimeError(
                        f"Agent {agent_name} reached '{agent_status}' without valid translations.json. "
                        f"Exceeded continuation limit (no_progress={no_progress_count}/{max_no_progress}, "
                        f"total={total_continuations}/{max_total_continuations}, prefix={last_valid_prefix}/{input_count})"
                    )

                total_continuations += 1
                remind_parts = [
                    f"提醒：任务尚未完成！当前目录 {s_dir.resolve()} 的 translations.json 尚未完整合法通过校验（当前有效进度：{last_valid_prefix}/{input_count}）。",
                    f"请从第 {last_valid_prefix + 1} 条开始继续翻译，建议每 50 条增量保存一次 translations.json，避免一次性过长输出丢失进展。",
                    "切勿过度循环查询关键字或子Agent，专注于翻译并确保输出完整 JSON 数组。",
                ]
                if val_err_msg:
                    remind_parts.append(f"上一轮校验错误信息（请根据此具体修复控制标签、占位符或结构）：{val_err_msg}")

                remind_msg = "\n".join(remind_parts)
                print(f"[{shard_id}] Sending continuation prompt #{total_continuations} (no_progress={no_progress_count})...", file=sys.stderr)
                try:
                    verify_references()
                    driver.prompt_agent(agent_name, remind_msg, timeout_sec=min(int(deadline_ts - time.time()), 60))
                except Exception as cont_err:
                    print(f"[{shard_id}] continuation prompt submission returned: {cont_err}. Continuing wait...", file=sys.stderr)
                continue

            # If still working, loop continues wait_agent_until_settled

        verify_references()
        # Verify input was not modified
        final_input_hash = file_sha256(input_file)
        if final_input_hash != initial_input_hash:
            raise ValueError(f"Shard {shard_id} input was modified by agent! {final_input_hash} != {initial_input_hash}")

        if translations is None:
            translations = validate_shard_output(input_file, output_file, shard_id)

        # Enforce settled status
        final_status = driver.get_agent_status(agent_name)
        if final_status not in {"idle", "done"}:
            raise RuntimeError(f"Agent {agent_name} ended in non-settled state '{final_status}'")

        out_hash = file_sha256(output_file)
        if reference_files:
            safe_write_json(context_receipt, {"references": reference_files, "output_sha256": out_hash})
        step_succeeded = True
        return {
            "shard_id": shard_id,
            "status": "success",
            "reused": False,
            "agent_name": agent_name,
            "pane_id": pane_id or "",
            "model": model,
            "input_sha256": initial_input_hash,
            "output_sha256": out_hash,
                "references": reference_files,
            "items_count": len(translations),
        }

    except Exception as exc:
        err_msg = f"Shard {shard_id} failed: {exc}"
        print(err_msg, file=sys.stderr)
        (evidence_dir / f"{shard_id}.error.txt").write_text(err_msg, encoding="utf-8")
        if abort_event is not None:
            abort_event.set()
        # Do NOT close pane on error so human/controller can inspect
        raise
    finally:
        # On success, close only this pane without disturbing others, and strictly only when settled
        if step_succeeded and pane_id:
            try:
                driver.close_pane(pane_id)
            except Exception:
                pass
        if step_succeeded and pane_id:
            try:
                driver.close_pane(pane_id)
            except Exception:
                pass


HEX64_REGEX = re.compile(r"^[0-9a-f]{64}$")


def validate_shard_prefix(
    input_file: Path,
    output_file: Path,
    shard_id: str,
) -> int:
    """Validate valid items prefix of translations.json against input.json. Returns valid count."""
    if not output_file.is_file():
        return 0
    try:
        translations = safe_read_json(output_file)
        if not isinstance(translations, list):
            return 0
        inputs = safe_read_json(input_file)
    except Exception:
        return 0

    valid_count = 0
    for idx, (inp, trans) in enumerate(zip(inputs, translations)):
        if not isinstance(trans, dict):
            break
        if trans.get("file") != inp.get("file") or trans.get("path") != inp.get("path"):
            break
        if trans.get("source") != inp.get("source"):
            break
        trans_text = trans.get("translation")
        if not isinstance(trans_text, str) or not trans_text.strip():
            break
        try:
            localization.validate_translation(inp["source"], trans_text)
        except Exception:
            break
        valid_count += 1

    return valid_count


def load_agent_registry(
    registry_path: Path,
    expected_run_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly load and validate an agent registry for --resume/--recovery.

    Enforces mandatory 64-hex input_sha and cross-checks with manifest and input.json.
    """
    if not registry_path.is_file():
        raise FileNotFoundError(f"Registry file not found: {registry_path}")

    data = safe_read_json(registry_path)
    if not isinstance(data, dict):
        raise TypeError(f"Registry must be a JSON object, got {type(data)}")

    reg_run_dir = data.get("run_dir")
    if not reg_run_dir:
        raise ValueError("Registry missing 'run_dir' field")
    if Path(reg_run_dir).resolve() != expected_run_dir.resolve():
        raise ValueError(
            f"Registry run_dir mismatch: {Path(reg_run_dir).resolve()} != {expected_run_dir.resolve()}"
        )

    agents = data.get("agents")
    if not isinstance(agents, dict):
        raise TypeError("Registry missing 'agents' mapping")

    manifest_map = {}
    if manifest and "shards" in manifest:
        for s in manifest["shards"]:
            manifest_map[s["shard_id"]] = s

    validated: dict[str, Any] = {}
    for sid, entry in agents.items():
        if not isinstance(entry, dict):
            raise TypeError(f"Registry entry for shard {sid} must be a dict")
        agent_name = entry.get("agent_name")
        pane_id = entry.get("pane_id")
        input_sha = entry.get("input_sha")
        if not agent_name or not pane_id:
            raise ValueError(f"Registry entry for shard {sid} missing agent_name or pane_id")
        if not input_sha or not isinstance(input_sha, str) or not HEX64_REGEX.fullmatch(input_sha.lower()):
            raise ValueError(f"Registry entry for shard {sid} input_sha must be 64-hex string, got: {input_sha!r}")
        input_sha = input_sha.lower()

        # Check against manifest if provided
        if manifest_map and sid in manifest_map:
            expected_hash = manifest_map[sid].get("input_hash", "").lower()
            if input_sha != expected_hash:
                raise ValueError(
                    f"Registry shard {sid} input_sha mismatch with manifest: {input_sha} != {expected_hash}"
                )

        # Check physical input.json if exists
        shard_input_file = expected_run_dir / "shards" / sid / "input.json"
        if shard_input_file.is_file():
            actual_input_sha = file_sha256(shard_input_file).lower()
            if input_sha != actual_input_sha:
                raise ValueError(
                    f"Registry shard {sid} input_sha mismatch with input.json: {input_sha} != {actual_input_sha}"
                )

        validated[sid] = {
            "shard_id": sid,
            "agent_name": agent_name,
            "pane_id": pane_id,
            "input_sha": input_sha,
        }
    return validated


def update_run_registry(
    registry_file: Path,
    shard_id: str,
    agent_name: str,
    pane_id: str,
    input_sha: str,
    lock: threading.Lock,
) -> None:
    """Atomically persist agent start to run-specific registry under lock."""
    with lock:
        current_data = {"run_dir": str(registry_file.parent.resolve()), "agents": {}}
        if registry_file.is_file():
            try:
                current_data = safe_read_json(registry_file)
            except Exception:
                pass
        if "agents" not in current_data or not isinstance(current_data["agents"], dict):
            current_data["agents"] = {}
        current_data["run_dir"] = str(registry_file.parent.resolve())
        current_data["agents"][shard_id] = {
            "agent_name": agent_name,
            "pane_id": pane_id,
            "input_sha": input_sha,
        }
        safe_write_json(registry_file, current_data)


def run_translation(
    run_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    model: str = DEFAULT_MODEL,
    settings_path: str = DEFAULT_SETTINGS_PATH,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    driver: HerdrDriver | None = None,
    registry_file: Path | None = None,
) -> int:
    manifest_file = run_dir / "shards_manifest.json"
    if not manifest_file.is_file():
        status_file = run_dir / "status.json"
        if status_file.is_file() and safe_read_json(status_file).get("status") == "up_to_date":
            print("Translation skipped: status is up_to_date.")
            return 0
        print(f"Error: {manifest_file} not found.", file=sys.stderr)
        return 2

    manifest = safe_read_json(manifest_file)
    shards = manifest.get("shards", [])
    if not shards:
        print("No shards to translate.")
        safe_write_json(run_dir / "translations.json", [])
        return 0

    agent_registry = None
    if registry_file:
        try:
            agent_registry = load_agent_registry(registry_file, run_dir, manifest=manifest)
            print(f"Loaded agent registry from {registry_file} with {len(agent_registry)} entries.")
        except Exception as reg_err:
            print(f"Error loading agent registry: {reg_err}", file=sys.stderr)
            return 2

    # Run-specific registry path for atomic updates of newly started agents
    run_registry_file = run_dir / "agents_registry.json"
    registry_lock = threading.Lock()

    if driver is None:
        driver = HerdrDriver()

    evidence_dir = run_dir / "evidence/translation"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create a dedicated workspace at trusted ROOT
    run_label = f"limbus-{run_dir.name[:16]}"
    ws_id, root_pane_id = driver.create_workspace(cwd=ROOT, label=run_label)
    print(f"Created dedicated workspace {ws_id} with root pane {root_pane_id}")

    # Process translation shards via pool: at most `concurrency` active panes
    results = []
    abort_event = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_map = {
            executor.submit(
                execute_translation_shard,
                s,
                driver,
                root_pane_id,
                model,
                settings_path,
                timeout_sec,
                evidence_dir,
                abort_event,
                agent_registry,
                run_registry_file,
                registry_lock,
            ): s["shard_id"]
            for s in shards
        }
        for future in concurrent.futures.as_completed(future_map):
            sid = future_map[future]
            try:
                res = future.result()
                results.append(res)
            except Exception as e:
                print(f"Translation shard {sid} aborted: {e}", file=sys.stderr)
                abort_event.set()
                return 2

    # Close root pane after all shards finish
    try:
        driver.close_pane(root_pane_id)
    except Exception:
        pass

    # Merge all translations into <run_dir>/translations.json
    all_translations = []
    for shard in shards:
        s_dir = Path(shard["directory"])
        shard_out = safe_read_json(s_dir / "translations.json")
        all_translations.extend(shard_out)

    merged_file = run_dir / "translations.json"
    safe_write_json(merged_file, all_translations)

    receipt = {
        "status": "success",
        "workspace_id": ws_id,
        "shards": sorted(results, key=lambda x: x["shard_id"]),
        "total_items": len(all_translations),
        "translations_hash": file_sha256(merged_file),
    }
    safe_write_json(run_dir / "translations_receipt.json", receipt)
    print(f"All {len(shards)} shards translated and merged successfully.")
    return 0


VALID_REVIEW_ACTIONS = {
    "keep_current",
    "approved_as_is",
    "internal_dummy_ignored",
    "empty_intentional",
    "format_verified",
}


def validate_review_artifacts(
    run_dir: Path,
    expected_pending_count: int,
    expected_review_items: list[dict[str, Any]],
    root_diff_sha: str,
    root_trans_sha: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strictly validate review.json and reviewed-translations.json."""
    reviewed_file = run_dir / "reviewed-translations.json"
    review_json_file = run_dir / "review.json"

    if not reviewed_file.is_file():
        raise FileNotFoundError(f"Missing {reviewed_file}")
    if not review_json_file.is_file():
        raise FileNotFoundError(f"Missing {review_json_file}")

    reviewed_translations = safe_read_json(reviewed_file)
    if not isinstance(reviewed_translations, list):
        raise TypeError("reviewed-translations.json must be a JSON array")
    if len(reviewed_translations) != expected_pending_count:
        raise ValueError(
            f"reviewed-translations.json count mismatch: got {len(reviewed_translations)}, expected {expected_pending_count}"
        )

    for item in reviewed_translations:
        if not isinstance(item, dict):
            raise TypeError("Item in reviewed-translations is not a dict")
        source = item.get("source")
        trans = item.get("translation")
        if not isinstance(trans, str) or not trans.strip():
            raise ValueError(f"Empty translation in reviewed-translations for item {item.get('file')}:{item.get('path')}")
        localization.validate_translation(source, trans)

    actual_reviewed_hash = file_sha256(reviewed_file)

    review_report = safe_read_json(review_json_file)
    if not isinstance(review_report, dict):
        raise TypeError("review.json must be a JSON object")

    hashes = review_report.get("hashes", {})
    if hashes.get("reviewed-translations.json") != actual_reviewed_hash:
        raise ValueError("review.json hashes['reviewed-translations.json'] mismatch")
    if hashes.get("translations.json") != root_trans_sha:
        raise ValueError("review.json hashes['translations.json'] mismatch")
    diff_or_inp = hashes.get("diff.json") or hashes.get("input.json")
    if diff_or_inp != root_diff_sha:
        raise ValueError("review.json hashes['diff.json'] mismatch")

    dispositions = review_report.get("dispositions")
    if dispositions is None:
        dispositions = review_report.get("details")
    if not isinstance(dispositions, list):
        raise TypeError("review.json missing 'dispositions' list")
    if len(dispositions) != len(expected_review_items):
        raise ValueError(
            f"review.json dispositions count mismatch: got {len(dispositions)}, expected {len(expected_review_items)}"
        )

    exp_rev_map = {}
    for r in expected_review_items:
        rk = (r["file"], json.dumps(r["path"], sort_keys=True), r.get("reason", ""))
        exp_rev_map[rk] = r

    seen_d_keys = set()
    for disp in dispositions:
        if not isinstance(disp, dict):
            raise TypeError("Disposition entry in review.json is not a dict")
        dk = (disp.get("file"), json.dumps(disp.get("path"), sort_keys=True), disp.get("reason", ""))
        if dk not in exp_rev_map:
            raise ValueError(f"Unknown review disposition key: {dk}")
        if dk in seen_d_keys:
            raise ValueError(f"Duplicate disposition key in review.json: {dk}")
        seen_d_keys.add(dk)

        action = disp.get("action")
        if action not in VALID_REVIEW_ACTIONS:
            raise ValueError(f"Invalid review action '{action}'. Must be in {VALID_REVIEW_ACTIONS}")
        if disp.get("resolved") is not True:
            raise ValueError(f"Review item not resolved (resolved != True): {dk}")
        note = disp.get("resolution_note") or disp.get("rationale") or disp.get("note")
        if not note or not str(note).strip():
            raise ValueError(f"Missing resolution note for {dk}")

    return review_report, reviewed_translations


def run_review(
    run_dir: Path,
    model: str = DEFAULT_MODEL,
    settings_path: str = DEFAULT_SETTINGS_PATH,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    driver: HerdrDriver | None = None,
) -> int:
    translations_file = run_dir / "translations.json"
    diff_file = run_dir / "diff.json"
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

    translations = safe_read_json(translations_file)
    diff_data = safe_read_json(diff_file)
    pending_items = diff_data.get("pending", [])
    review_items = safe_read_json(review_items_file) if review_items_file.is_file() else diff_data.get("review", [])

    review_work_dir = run_dir / "review_agent"
    review_work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Save inputs
    review_input_file = review_work_dir / "input.json"
    review_trans_file = review_work_dir / "translations.json"
    review_diff_file = review_work_dir / "diff_review.json"

    safe_write_json(review_input_file, pending_items)
    safe_write_json(review_trans_file, translations)
    safe_write_json(review_diff_file, review_items)

    # Record hashes before reviewer execution
    root_diff_sha_pre = file_sha256(diff_file)
    root_trans_sha_pre = file_sha256(translations_file)
    rev_input_sha_pre = file_sha256(review_input_file)
    rev_trans_sha_pre = file_sha256(review_trans_file)
    rev_diff_sha_pre = file_sha256(review_diff_file)

    prompt_text = (
        f"工作目录：{review_work_dir.resolve()}。\n"
        f"待校对翻译：{review_trans_file.resolve()}。\n"
        f"原文待翻译输入：{review_input_file.resolve()}。\n"
        f"Diff待复核项：{review_diff_file.resolve()}。\n"
        "任务要求：\n"
        "1. 使用全新独立 Claude Code（Gemini 模型），同一 Agent 负责按文件分批仔细审查全部 4484 条（或实际 pending 数量）待校对翻译及 632 条（或实际 review 数量）复核项，严禁修改任何输入文件或启动子 Agent。\n"
        "2. 严禁改动任何凭据，严禁生成 controller approval（主控审批由 release 节点授权）。\n"
        "3. 仔细审查每一条 translations.json 的翻译，确保自然流畅、保留格式与占位符、无韩文残留、术语正确。\n"
        f"4. 将最终校对确认的翻译完整落盘到：{(run_dir / 'reviewed-translations.json').resolve()}。\n"
        f"5. 将校对报告完整落盘到：{(run_dir / 'review.json').resolve()}，必须包含两部分：\n"
        "   a) hashes 绑定：\n"
        f'      "diff.json": "{root_diff_sha_pre}",\n'
        f'      "translations.json": "{root_trans_sha_pre}",\n'
        '      "reviewed-translations.json": "<reviewed-translations.json的实际sha256>"\n'
        "   b) dispositions 列表：对 diff_review.json 中的每一项进行逐项审查定性，包含 file, path, reason, source, action, resolution_note, resolved。\n"
        "      合法 action 包含: keep_current, approved_as_is, internal_dummy_ignored, empty_intentional, format_verified。\n"
        "      重要警示：严禁为了过门禁伪造 resolved=true！有不确定或需补译项必须填写 action=\"needs_translation\" 且 resolved=false，并说明原因，由验证 gate 阻断！\n"
        "      只有能够充分证明保留现状合理（如已有译文无误、内部无用注释、UI故意留空）的项，才可填写对应 action 并 resolved=true。"
    )
    (review_work_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")

    if driver is None:
        driver = HerdrDriver()

    evidence_dir = run_dir / "evidence/review"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Simplified: create only ONE dedicated review workspace
    run_label = f"limbus-rev-{run_dir.name[:12]}"
    rev_ws_id, rev_root_pane_id = driver.create_workspace(cwd=ROOT, label=run_label)

    agent_name = generate_agent_name("reviewer")
    step_succeeded = False
    deadline_ts = time.time() + timeout_sec
    try:
        driver.start_claude_agent(
            agent_name=agent_name,
            pane_id=rev_root_pane_id,
            model=model,
            settings_path=settings_path,
        )
        print(f"[Review] Started reviewer agent {agent_name} in pane {rev_root_pane_id}")

        try:
            driver.prompt_agent(agent_name, prompt_text, timeout_sec=min(timeout_sec, 60))
        except Exception as p_err:
            print(f"[Review] prompt --wait returned: {p_err}. Entering reliable status tracking loop...", file=sys.stderr)

        max_continuation_attempts = 3
        continuation_attempts = 0
        review_report = None
        reviewed_translations = None

        while True:
            agent_status = wait_agent_until_settled(driver, agent_name, deadline_ts=deadline_ts, poll_interval_sec=5)
            terminal_log = driver.read_agent_output(agent_name, lines=150)
            (evidence_dir / "reviewer.terminal.log").write_text(terminal_log, encoding="utf-8")

            # Check artifacts validity
            val_err_msg = ""
            try:
                review_report, reviewed_translations = validate_review_artifacts(
                    run_dir=run_dir,
                    expected_pending_count=len(pending_items),
                    expected_review_items=review_items,
                    root_diff_sha=root_diff_sha_pre,
                    root_trans_sha=root_trans_sha_pre,
                )
                if agent_status in {"idle", "done"}:
                    print("[Review] review artifacts verified successfully. Agent settled.")
                    break
                else:
                    print(f"[Review] Artifacts valid but agent still in '{agent_status}', waiting for settled state...")
            except Exception as r_err:
                val_err_msg = str(r_err)

            if time.time() >= deadline_ts:
                raise TimeoutError(f"Reviewer timed out after {timeout_sec}s. Last status: '{agent_status}'")

            if agent_status == "blocked":
                raise RuntimeError(f"Reviewer {agent_name} is blocked and requires user intervention")

            if agent_status in {"idle", "done"}:
                if continuation_attempts >= max_continuation_attempts:
                    raise RuntimeError(
                        f"Reviewer {agent_name} reached '{agent_status}' without producing valid complete review artifacts "
                        f"after {continuation_attempts} continuation attempts. Last validation error: {val_err_msg}"
                    )

                continuation_attempts += 1
                remind_parts = [
                    f"提醒：校对任务尚未完成！当前 run 目录下仍缺少完整合法的 review.json 或 reviewed-translations.json。",
                    f"请务必完成全部 {len(pending_items)} 项待校对翻译及 {len(review_items)} 项 review dispositions，落盘相应 JSON 文件。",
                ]
                if val_err_msg:
                    remind_parts.append(f"上一轮工件校验失败原因（请根据此具体修复）：{val_err_msg}")

                remind_msg = "\n".join(remind_parts)
                print(f"[Review] Agent stopped in '{agent_status}' without valid artifacts. Sending continuation prompt #{continuation_attempts}...", file=sys.stderr)
                try:
                    driver.prompt_agent(agent_name, remind_msg, timeout_sec=min(int(deadline_ts - time.time()), 60))
                except Exception as cont_err:
                    print(f"[Review] continuation prompt returned: {cont_err}. Continuing wait...", file=sys.stderr)
                continue

        # Verify all input copies and root files were NOT modified
        if file_sha256(diff_file) != root_diff_sha_pre:
            raise ValueError("Root diff.json was modified during review!")
        if file_sha256(translations_file) != root_trans_sha_pre:
            raise ValueError("Root translations.json was modified during review!")
        if file_sha256(review_input_file) != rev_input_sha_pre:
            raise ValueError("review_agent/input.json was modified during review!")
        if file_sha256(review_trans_file) != rev_trans_sha_pre:
            raise ValueError("review_agent/translations.json was modified during review!")
        if file_sha256(review_diff_file) != rev_diff_sha_pre:
            raise ValueError("review_agent/diff_review.json was modified during review!")

        reviewed_file = run_dir / "reviewed-translations.json"
        review_json_file = run_dir / "review.json"

        # Final check: status must strictly be settled
        final_status = driver.get_agent_status(agent_name)
        if final_status not in {"idle", "done"}:
            raise RuntimeError(f"Reviewer {agent_name} ended in non-settled state '{final_status}'")

        receipt = {
            "status": "success",
            "workspace_id": rev_ws_id,
            "agent_name": agent_name,
            "pane_id": rev_root_pane_id,
            "model": model,
            "reviewed_translations_sha256": file_sha256(reviewed_file),
            "review_json_sha256": file_sha256(review_json_file),
            "input_hashes_verified": {
                "diff_file": root_diff_sha_pre,
                "translations_file": root_trans_sha_pre,
                "review_input": rev_input_sha_pre,
                "review_trans": rev_trans_sha_pre,
                "review_diff": rev_diff_sha_pre,
            },
        }
        safe_write_json(run_dir / "review_receipt.json", receipt)
        step_succeeded = True
        print("Review step completed successfully.")
        return 0

    except Exception as exc:
        err_msg = f"Review step failed: {exc}"
        print(err_msg, file=sys.stderr)
        (evidence_dir / "reviewer.error.txt").write_text(err_msg, encoding="utf-8")
        return 2
    finally:
        if step_succeeded:
            try:
                driver.close_pane(rev_root_pane_id)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Herdr Translation & Review Orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    trans_p = subparsers.add_parser("translate")
    trans_p.add_argument("--run-dir", required=True, help="Run directory")
    trans_p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Shard concurrency")
    trans_p.add_argument("--model", default=DEFAULT_MODEL, help="Model ID")
    trans_p.add_argument("--settings", default=DEFAULT_SETTINGS_PATH, help="Path to Claude settings")
    trans_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Timeout in seconds")
    trans_p.add_argument("--registry", "--resume", "--recovery", dest="registry", default=None, help="Path to run-specific agent registry JSON for recovery/resume")

    rev_p = subparsers.add_parser("review")
    rev_p.add_argument("--run-dir", required=True, help="Run directory")
    rev_p.add_argument("--model", default=DEFAULT_MODEL, help="Model ID")
    rev_p.add_argument("--settings", default=DEFAULT_SETTINGS_PATH, help="Path to Claude settings")
    rev_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Timeout in seconds")

    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()

    if args.command == "translate":
        registry_path = Path(args.registry).resolve() if args.registry else None
        code = run_translation(
            run_dir=run_dir,
            concurrency=args.concurrency,
            model=args.model,
            settings_path=args.settings,
            timeout_sec=args.timeout,
            registry_file=registry_path,
        )
        sys.exit(code)
    elif args.command == "review":
        code = run_review(
            run_dir=run_dir,
            model=args.model,
            settings_path=args.settings,
            timeout_sec=args.timeout,
        )
        sys.exit(code)


if __name__ == "__main__":
    main()
