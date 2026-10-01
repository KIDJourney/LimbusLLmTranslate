#!/usr/bin/env python3
"""Code-controlled workflow runner with a local live visualization UI."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RESULT_SCHEMA = ROOT / "agent-result.schema.json"
TERMINALS = {"$complete", "$failed"}
NODE_TYPES = {"agent", "command", "verifier", "approval"}
ACTIVE_STATUSES = {"running", "paused", "waiting_approval"}


class WorkflowError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_event(run_dir: Path, event: dict[str, Any]) -> None:
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": utc_now(), **event}, ensure_ascii=False) + "\n")


def canonical_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_command(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"{label} must be a non-empty string array")


def validate_workflow(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # Extended to validate exit_routes
    if workflow.get("version") != 2:
        raise WorkflowError("workflow.version must be 2")
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowError("workflow.nodes must be a non-empty array")
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise WorkflowError("every node needs a string id")
        node_id = node["id"]
        if node_id in node_map:
            raise WorkflowError(f"duplicate node id: {node_id}")
        if node.get("type") not in NODE_TYPES:
            raise WorkflowError(f"node {node_id} has unsupported type {node.get('type')}")
        if node["type"] == "agent" and node.get("session_policy", "ephemeral") not in {"ephemeral", "reuse"}:
            raise WorkflowError(f"node {node_id} has invalid session_policy")
        transitions = node.get("on")
        if not isinstance(transitions, dict) or set(transitions) != {"passed", "failed"}:
            raise WorkflowError(f"node {node_id} needs on.passed and on.failed")
        for index, action in enumerate(node.get("actions", []), 1):
            if not isinstance(action, dict):
                raise WorkflowError(f"node {node_id} action {index} must be an object")
            validate_command(action.get("command"), f"node {node_id} action {index} command")

        exit_routes = node.get("exit_routes")
        if exit_routes is not None:
            if node["type"] != "command":
                raise WorkflowError(f"node {node_id} cannot have exit_routes because it is not a command node")
            if len(node.get("actions", [])) != 1:
                raise WorkflowError(f"node {node_id} must have exactly 1 action when using exit_routes")
            if not isinstance(exit_routes, dict):
                raise WorkflowError(f"node {node_id} exit_routes must be an object")
            for key, val in exit_routes.items():
                try:
                    int(key)
                except ValueError:
                    raise WorkflowError(f"node {node_id} exit_routes keys must be string representations of integers")
                if not isinstance(val, str):
                    raise WorkflowError(f"node {node_id} exit_routes targets must be strings")

        for index, gate in enumerate(node.get("gates", []), 1):
            if not isinstance(gate, dict) or gate.get("type") not in {"command", "artifact"}:
                raise WorkflowError(f"node {node_id} gate {index} is unsupported")
            if gate["type"] == "command":
                validate_command(gate.get("command"), f"node {node_id} gate {index} command")
            elif not isinstance(gate.get("path"), str):
                raise WorkflowError(f"node {node_id} gate {index} needs an artifact path")
        node_map[node_id] = node
    if workflow.get("entry") not in node_map:
        raise WorkflowError("workflow.entry must reference a node")
    for node in nodes:
        for target in node["on"].values():
            if target not in node_map and target not in TERMINALS:
                raise WorkflowError(f"node {node['id']} references unknown target {target}")
        for target in node.get("exit_routes", {}).values():
            if target not in node_map and target not in TERMINALS:
                raise WorkflowError(f"node {node['id']} exit_routes references unknown target {target}")
    reachable: set[str] = set()
    pending = [workflow["entry"]]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(target for target in node_map[current]["on"].values() if target in node_map)
        pending.extend(target for target in node_map[current].get("exit_routes", {}).values() if target in node_map)
    unreachable = set(node_map) - reachable
    if unreachable:
        raise WorkflowError(f"unreachable nodes: {', '.join(sorted(unreachable))}")
    agent = workflow.get("agent", {})
    if not isinstance(agent, dict) or agent.get("backend") != "codex-sdk":
        raise WorkflowError("workflow.agent.backend must be codex-sdk")
    if agent.get("sandbox", "workspace-write") not in {"read-only", "workspace-write", "full-access"}:
        raise WorkflowError("workflow.agent.sandbox is invalid")
    if agent.get("approval_mode", "deny_all") != "deny_all":
        raise WorkflowError("workflow.agent.approval_mode must be deny_all")
    if agent.get("model") is not None and not isinstance(agent.get("model"), str):
        raise WorkflowError("workflow.agent.model must be a string")
    agent_timeout = workflow.get("limits", {}).get("agent_timeout_seconds", 1800)
    if not isinstance(agent_timeout, int) or agent_timeout <= 0:
        raise WorkflowError("workflow.limits.agent_timeout_seconds must be a positive integer")
    return node_map


def resolve_inside(workspace: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise WorkflowError(f"path escapes workspace: {value}") from exc
    return resolved


def render(items: list[str], variables: dict[str, str]) -> list[str]:
    output = []
    for original in items:
        item = original
        for key, value in variables.items():
            item = item.replace("{" + key + "}", value)
        output.append(item)
    return output


def tail(value: str, limit: int = 2400) -> str:
    return value if len(value) <= limit else value[-limit:]


def acquire_lock(run_dir: Path) -> Path:
    lock = run_dir / ".lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkflowError(f"run is already active or has a stale lock: {lock}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    return lock


def read_events(run_dir: Path, limit: int = 300) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def build_context(state: dict[str, Any]) -> str:
    lines = []
    for item in state["history"][-4:]:
        lines.append(
            f"- {item['node_id']} attempt {item['attempt']}: {item['outcome']}; "
            f"summary={tail(item.get('summary', ''), 500)}; receipts={item['attempt_dir']}"
        )
    return "\n".join(lines) if lines else "- none"


def command_receipt(
    item: dict[str, Any], workflow: dict[str, Any], state: dict[str, Any], attempt_dir: Path, prefix: str
) -> tuple[bool, dict[str, Any]]:
    workspace = Path(state["workspace"])
    variables = {
        "workspace": str(workspace),
        "run_dir": state["run_dir"],
        "attempt_dir": str(attempt_dir),
        "node_id": str(state["current_node"]),
    }
    command = render(item["command"], variables)
    cwd = resolve_inside(workspace, item.get("cwd", "."))
    timeout = int(item.get("timeout_seconds", workflow.get("limits", {}).get("command_timeout_seconds", 600)))
    env = os.environ.copy()
    env.update({
        "WORKFLOW_RUN_DIR": str(state["run_dir"]),
        "WORKFLOW_ATTEMPT_DIR": str(attempt_dir),
        "WORKFLOW_NODE_ID": str(state["current_node"]),
    })
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
        stdout = attempt_dir / f"{prefix}.stdout.log"
        stderr = attempt_dir / f"{prefix}.stderr.log"
        stdout.write_text(completed.stdout, encoding="utf-8")
        stderr.write_text(completed.stderr, encoding="utf-8")
        accepted = item.get("accept_exit_codes", [0])
        return completed.returncode in accepted, {
            "name": item.get("name", prefix),
            "type": "command",
            "command": command,
            "exit_code": completed.returncode,
            "accepted_exit_codes": accepted,
            "stdout": str(stdout),
            "stderr": str(stderr),
            "stdout_tail": tail(completed.stdout),
            "stderr_tail": tail(completed.stderr),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, {"name": item.get("name", prefix), "type": "command", "command": command, "error": str(exc)}


def artifact_receipt(gate: dict[str, Any], state: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    path = resolve_inside(Path(state["workspace"]), gate["path"])
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    minimum = int(gate.get("min_bytes", 1))
    passed = exists and size >= minimum
    receipt: dict[str, Any] = {
        "name": gate.get("name", path.name),
        "type": "artifact",
        "path": str(path),
        "exists": exists,
        "size": size,
        "minimum": minimum,
    }
    if passed:
        receipt["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return passed, receipt


async def run_agent_with_sdk(
    workflow: dict[str, Any], node: dict[str, Any], state: dict[str, Any], prompt: str,
    env: dict[str, str], existing_session: str | None,
) -> tuple[Any, str, str, str, str, str | None]:
    try:
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
    except ImportError as exc:
        raise WorkflowError(
            "Codex SDK is not installed; install the workflow requirements with the Python interpreter used to run run.py"
        ) from exc

    agent = workflow["agent"]
    sandbox = {
        "read-only": Sandbox.read_only,
        "workspace-write": Sandbox.workspace_write,
        "full-access": Sandbox.full_access,
    }[agent.get("sandbox", "workspace-write")]
    approval_mode = ApprovalMode.deny_all
    workspace = str(Path(state["workspace"]))
    session_policy = node.get("session_policy", "ephemeral")
    invocation = "resume" if session_policy == "reuse" and existing_session else "start"
    config = CodexConfig(cwd=workspace, env=env)
    async with AsyncCodex(config) as codex:
        thread_options = {
            "approval_mode": approval_mode,
            "cwd": workspace,
            "sandbox": sandbox,
        }
        if agent.get("model"):
            thread_options["model"] = agent["model"]
        if invocation == "resume":
            thread = await codex.thread_resume(existing_session, **thread_options)
        else:
            thread = await codex.thread_start(
                ephemeral=session_policy == "ephemeral",
                **thread_options,
            )
        thread_name = f"{workflow['name']} / {node.get('title', node['id'])}"
        thread_name_source = "prompt"
        thread_name_error = None
        if session_policy != "ephemeral":
            try:
                await thread.set_name(thread_name)
                thread_name_source = "metadata"
            except Exception as exc:
                thread_name_error = str(exc)
        turn = await thread.turn(
            prompt,
            cwd=workspace,
            output_schema=load_json(RESULT_SCHEMA),
            sandbox=sandbox,
        )
        timeout = int(workflow.get("limits", {}).get("agent_timeout_seconds", 1800))
        try:
            result = await asyncio.wait_for(turn.run(), timeout=timeout)
        except asyncio.TimeoutError:
            await turn.interrupt()
            raise WorkflowError(f"Codex SDK turn timed out after {timeout} seconds")
        return result, thread.id, invocation, thread_name, thread_name_source, thread_name_error


def model_json(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "value"):
        return value.value
    return str(value)


def run_agent(
    workflow: dict[str, Any], node: dict[str, Any], state: dict[str, Any], attempt_dir: Path
) -> tuple[bool, dict[str, Any]]:
    workspace = Path(state["workspace"])
    session_policy = node.get("session_policy", "ephemeral")
    existing_session = state["sessions"].get(node["id"])
    attempt = str(state["visits"][node["id"]])
    request = Path(state["request_file"]).read_text(encoding="utf-8")
    prompt = f"""Workflow node: {workflow['name']} / {node.get('title', node['id'])}

You are executing one node in a code-controlled workflow.

Workflow: {workflow['name']}
Node: {node['id']} — {node.get('title', node['id'])}
Attempt: {attempt}
Session policy: {session_policy}

User request:
{request}

Node responsibility:
{node.get('prompt', '')}

Recent workflow outcomes:
{build_context(state)}

Hard rules:
- Work only on this node; never perform a later release.
- Read receipts instead of trusting prior prose.
- The runner independently executes gates and chooses transitions.
- Finish with JSON matching the supplied output schema.
"""
    env = os.environ.copy()
    env.update({
        "WORKFLOW_RUN_DIR": state["run_dir"],
        "WORKFLOW_ATTEMPT_DIR": str(attempt_dir),
        "WORKFLOW_NODE_ID": node["id"],
        "WORKFLOW_NODE_ATTEMPT": attempt,
    })
    try:
        turn_result, session_id, invocation, thread_name, thread_name_source, thread_name_error = asyncio.run(
            run_agent_with_sdk(workflow, node, state, prompt, env, existing_session)
        )
    except Exception as exc:
        return False, {"status": "runner_error", "summary": str(exc), "backend": "codex-sdk"}
    if session_policy == "reuse" and session_id:
        state["sessions"][node["id"]] = session_id
    final_response = turn_result.final_response
    (attempt_dir / "agent-final-response.json").write_text((final_response or "") + "\n", encoding="utf-8")
    receipt: dict[str, Any] = {
        "backend": "codex-sdk",
        "invocation": invocation,
        "session_policy": session_policy,
        "session_id": session_id,
        "thread_name": thread_name,
        "thread_name_source": thread_name_source,
        "thread_name_error": thread_name_error,
        "turn_id": turn_result.id,
        "turn_status": model_json(turn_result.status),
        "turn_error": model_json(getattr(turn_result, "error", None)),
        "duration_ms": turn_result.duration_ms,
        "usage": model_json(turn_result.usage),
    }
    if receipt["turn_status"] != "completed":
        receipt.update({
            "status": "turn_failed",
            "summary": f"Codex SDK turn ended with status {receipt['turn_status']}",
        })
        return False, receipt
    if final_response is None:
        receipt.update({"status": "invalid_result", "summary": "Codex SDK turn returned no final response"})
        return False, receipt
    try:
        result = json.loads(final_response)
    except json.JSONDecodeError as exc:
        receipt.update({"status": "invalid_result", "summary": f"cannot parse SDK final response: {exc}"})
        return False, receipt
    required = {"status", "summary", "artifacts", "risks"}
    if not required.issubset(result) or result["status"] not in {"completed", "blocked"}:
        receipt.update({"status": "invalid_result", "summary": "agent result violates the contract"})
        return False, receipt
    receipt.update(result)
    return result["status"] == "completed", receipt


def control_requested(run_dir: Path) -> str | None:
    path = run_dir / "control.json"
    if not path.is_file():
        return None
    return load_json(path).get("requested")


def execute(workflow: dict[str, Any], state: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    node_map = validate_workflow(workflow)
    limits = workflow.get("limits", {})
    lock = acquire_lock(run_dir)
    try:
        while state["status"] == "running":
            if control_requested(run_dir) == "pause":
                state["status"] = "paused"
                append_event(run_dir, {"event": "run_paused", "node_id": state["current_node"]})
                break
            if state["step_count"] >= int(limits.get("max_total_steps", 20)):
                state.update({"status": "failed", "failure": "maximum total steps exceeded"})
                break
            node = node_map[state["current_node"]]
            node_id = node["id"]
            if node["type"] == "approval" and node_id not in state["approvals"]:
                state["status"] = "waiting_approval"
                append_event(run_dir, {"event": "approval_required", "node_id": node_id})
                break
            state["visits"][node_id] = state["visits"].get(node_id, 0) + 1
            attempt = state["visits"][node_id]
            if attempt > int(node.get("max_visits", limits.get("max_visits_per_node", 3))):
                state.update({"status": "failed", "failure": f"max visits exceeded for {node_id}"})
                break
            state["step_count"] += 1
            attempt_dir = run_dir / "nodes" / node_id / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            atomic_json(run_dir / "state.json", state)
            append_event(run_dir, {"event": "node_started", "node_id": node_id, "node_type": node["type"], "attempt": attempt})
            print(f"[{state['step_count']}] {node.get('title', node_id)} ({node['type']}, attempt {attempt})", flush=True)

            summary = ""
            action_exit_route = None
            if node["type"] == "agent":
                work_ok, work_receipt = run_agent(workflow, node, state, attempt_dir)
                summary = work_receipt.get("summary", "")
                atomic_json(attempt_dir / "agent-receipt.json", work_receipt)
            else:
                action_receipts = []
                work_ok = True
                for index, action in enumerate(node.get("actions", []), 1):
                    passed, receipt = command_receipt(action, workflow, state, attempt_dir, f"action-{index:02d}")

                    if "exit_routes" in node:
                        exit_code_str = str(receipt["exit_code"])
                        exit_routes = node["exit_routes"]
                        if exit_code_str in exit_routes:
                            action_exit_route = exit_routes[exit_code_str]
                            passed = True
                            receipt["passed"] = True

                    receipt["passed"] = passed
                    action_receipts.append(receipt)
                    if not passed:
                        work_ok = False
                        break
                atomic_json(attempt_dir / "action-receipts.json", {"actions": action_receipts})
                summary = f"{len(action_receipts)} deterministic action(s) executed"

            gate_receipts = []
            gates_ok = work_ok
            if work_ok:
                for index, gate in enumerate(node.get("gates", []), 1):
                    if gate["type"] == "artifact":
                        passed, receipt = artifact_receipt(gate, state)
                    else:
                        passed, receipt = command_receipt(gate, workflow, state, attempt_dir, f"gate-{index:02d}")
                    receipt["passed"] = passed
                    gate_receipts.append(receipt)
                    if not passed:
                        gates_ok = False
                        break
            atomic_json(attempt_dir / "gate-receipts.json", {"gates": gate_receipts})
            outcome = "passed" if gates_ok else "failed"
            target = node["on"][outcome]
            if outcome == "passed" and action_exit_route is not None:
                target = action_exit_route
            history_item = {
                "node_id": node_id,
                "node_type": node["type"],
                "attempt": attempt,
                "outcome": outcome,
                "summary": summary,
                "attempt_dir": str(attempt_dir),
                "target": target,
            }
            state["history"].append(history_item)
            append_event(run_dir, {"event": "node_finished", **history_item})
            print(f"    {outcome} -> {target}", flush=True)
            if target == "$complete":
                state.update({"status": "completed", "current_node": None, "finished_at": utc_now()})
            elif target == "$failed":
                state.update({"status": "failed", "current_node": None, "failure": f"node {node_id} failed", "finished_at": utc_now()})
            else:
                state["current_node"] = target
            atomic_json(run_dir / "state.json", state)
    finally:
        atomic_json(run_dir / "state.json", state)
        lock.unlink(missing_ok=True)
    append_event(run_dir, {"event": "run_yielded", "status": state["status"]})
    return state


def create_state(workflow: dict[str, Any], workflow_path: Path, workspace: Path, run_dir: Path, request: str) -> dict[str, Any]:
    request_file = run_dir / "request.md"
    request_file.write_text(request.strip() + "\n", encoding="utf-8")
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "workflow_path": str(workflow_path),
        "workflow_hash": canonical_hash(workflow),
        "workspace": str(workspace),
        "request_file": str(request_file),
        "status": "running",
        "current_node": workflow["entry"],
        "step_count": 0,
        "visits": {},
        "sessions": {},
        "approvals": {},
        "history": [],
        "started_at": utc_now(),
    }


def exit_code(state: dict[str, Any]) -> int:
    return {"completed": 0, "waiting_approval": 3, "paused": 4}.get(state["status"], 1)


def start_run(args: argparse.Namespace) -> int:
    workflow_path = Path(args.workflow).resolve()
    workflow = load_json(workflow_path)
    validate_workflow(workflow)
    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise WorkflowError(f"workspace is not a directory: {workspace}")
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else workspace / ".workflow-runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    state = create_state(workflow, workflow_path, workspace, run_dir, args.request)
    atomic_json(run_dir / "workflow.snapshot.json", workflow)
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, {"event": "run_started", "workflow": workflow["name"]})
    print(f"run_dir: {run_dir}")
    final = execute(workflow, state, run_dir)
    print(f"status: {final['status']}")
    return exit_code(final)


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_json(run_dir / "state.json")
    workflow = load_json(run_dir / "workflow.snapshot.json")
    if canonical_hash(workflow) != state["workflow_hash"]:
        raise WorkflowError("workflow snapshot does not match the run state; continuation refused")
    return workflow, state


def resume_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    workflow, state = load_run(run_dir)
    if state["status"] == "completed":
        return 0
    if state["status"] == "waiting_approval":
        raise WorkflowError("run is waiting for approval; use the approve command")
    if state["status"] not in {"running", "paused"}:
        return 1
    (run_dir / "control.json").unlink(missing_ok=True)
    state["status"] = "running"
    append_event(run_dir, {"event": "run_resumed", "node_id": state["current_node"]})
    return exit_code(execute(workflow, state, run_dir))


def approve_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    workflow, state = load_run(run_dir)
    node_id = args.node or state.get("current_node")
    node = validate_workflow(workflow).get(node_id)
    if not node or node["type"] != "approval":
        raise WorkflowError(f"node is not an approval node: {node_id}")
    if state.get("current_node") != node_id:
        raise WorkflowError(f"run is not waiting at node {node_id}")
    state["approvals"][node_id] = {"by": args.by, "at": utc_now()}
    state["status"] = "running"
    atomic_json(run_dir / "state.json", state)
    append_event(run_dir, {"event": "approval_granted", "node_id": node_id, "by": args.by})
    return exit_code(execute(workflow, state, run_dir))


def pause_run(run_dir: Path) -> None:
    atomic_json(run_dir / "control.json", {"requested": "pause", "at": utc_now()})
    append_event(run_dir, {"event": "pause_requested"})


def latest_run(runs_dir: Path) -> Path | None:
    if not runs_dir.is_dir():
        return None
    candidates = [path for path in runs_dir.iterdir() if (path / "state.json").is_file()]
    return max(candidates, key=lambda path: path.name) if candidates else None


def collect_receipts(run_dir: Path) -> dict[str, Any]:
    receipts: dict[str, Any] = {}
    nodes_dir = run_dir / "nodes"
    if not nodes_dir.is_dir():
        return receipts
    for node_dir in nodes_dir.iterdir():
        attempts = sorted(path for path in node_dir.iterdir() if path.is_dir())
        if not attempts:
            continue
        latest = attempts[-1]
        value: dict[str, Any] = {"attempt_dir": str(latest)}
        for name in ("agent-receipt.json", "action-receipts.json", "gate-receipts.json"):
            path = latest / name
            if path.is_file():
                value[name.removesuffix(".json")] = load_json(path)
        receipts[node_dir.name] = value
    return receipts


def snapshot(workflow_path: Path, runs_dir: Path, selected_run: Path | None = None) -> dict[str, Any]:
    workflow = load_json(workflow_path)
    validate_workflow(workflow)
    run_dir = selected_run or latest_run(runs_dir)
    if not run_dir:
        return {"workflow": workflow, "state": None, "events": [], "receipts": {}, "server_time": utc_now()}
    try:
        state = load_json(run_dir / "state.json")
        request = Path(state["request_file"]).read_text(encoding="utf-8").strip()
    except WorkflowError:
        raise
    return {
        "workflow": workflow,
        "state": state,
        "request": request,
        "events": read_events(run_dir),
        "receipts": collect_receipts(run_dir),
        "server_time": utc_now(),
    }


class VisualizerServer(ThreadingHTTPServer):
    workflow_path: Path
    workspace: Path
    runs_dir: Path
    static_dir: Path
    selected_run: Path | None


class VisualizerHandler(BaseHTTPRequestHandler):
    server: VisualizerServer

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def send_json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise WorkflowError("request body must be a JSON object")
        return value

    def active_run(self) -> Path | None:
        return self.server.selected_run or latest_run(self.server.runs_dir)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path == "/api/snapshot":
            try:
                self.send_json(snapshot(self.server.workflow_path, self.server.runs_dir, self.server.selected_run))
            except WorkflowError as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        relative = path.lstrip("/") or "index.html"
        target = (self.server.static_dir / relative).resolve()
        try:
            target.relative_to(self.server.static_dir)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            target = self.server.static_dir / "index.html"
        if not target.is_file():
            self.send_json({"error": "UI build is missing; run npm run build"}, 503)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        try:
            body = self.read_body()
            if self.path == "/api/validate":
                nodes = validate_workflow(load_json(self.server.workflow_path))
                self.send_json({"ok": True, "nodes": len(nodes)})
                return
            if self.path == "/api/start":
                current = self.active_run()
                if current:
                    state = load_json(current / "state.json")
                    if state.get("status") in ACTIVE_STATUSES:
                        raise WorkflowError(f"run {state['run_id']} is already {state['status']}")
                request = str(body.get("request", "")).strip()
                if not request:
                    raise WorkflowError("request is required")
                command = [
                    sys.executable, str(Path(__file__).resolve()), "start",
                    "--workflow", str(self.server.workflow_path),
                    "--workspace", str(self.server.workspace),
                    "--runs-dir", str(self.server.runs_dir),
                    "--request", request,
                ]
                subprocess.Popen(command, cwd=self.server.workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.server.selected_run = None
                self.send_json({"ok": True}, 202)
                return
            run_dir = self.active_run()
            if not run_dir:
                raise WorkflowError("no run is available")
            if self.path == "/api/pause":
                pause_run(run_dir)
                self.send_json({"ok": True}, 202)
                return
            if self.path == "/api/approve":
                state = load_json(run_dir / "state.json")
                command = [
                    sys.executable, str(Path(__file__).resolve()), "approve",
                    "--run-dir", str(run_dir), "--node", str(state.get("current_node")),
                    "--by", str(body.get("by", "visualizer")),
                ]
                subprocess.Popen(command, cwd=self.server.workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_json({"ok": True}, 202)
                return
            self.send_json({"error": "unknown endpoint"}, 404)
        except (WorkflowError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)


def create_visualizer_server(
    host: str, port: int, workflow_path: Path, workspace: Path, runs_dir: Path,
    selected_run: Path | None = None, static_dir: Path | None = None,
) -> VisualizerServer:
    server = VisualizerServer((host, port), VisualizerHandler)
    server.workflow_path = workflow_path.resolve()
    server.workspace = workspace.resolve()
    server.runs_dir = runs_dir.resolve()
    server.static_dir = (static_dir or ROOT / "dist" / "client").resolve()
    server.selected_run = selected_run.resolve() if selected_run else None
    server.verbose = False
    return server


def visualize(args: argparse.Namespace) -> int:
    workflow_path = Path(args.workflow).resolve()
    validate_workflow(load_json(workflow_path))
    workspace = Path(args.workspace).resolve()
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else workspace / ".workflow-runs"
    selected_run = Path(args.run_dir).resolve() if args.run_dir else None
    static_dir = Path(args.static_dir).resolve() if args.static_dir else ROOT / "dist" / "client"
    if not (static_dir / "index.html").is_file():
        raise WorkflowError(f"visualizer build not found at {static_dir}; run npm run build")
    server = create_visualizer_server(args.host, args.port, workflow_path, workspace, runs_dir, selected_run, static_dir)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"visualizer: {url}")
    if args.open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--workflow", default=str(ROOT / "workflow.json"))
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--workflow", default=str(ROOT / "workflow.json"))
    start_parser.add_argument("--workspace", required=True)
    start_parser.add_argument("--request", required=True)
    start_parser.add_argument("--runs-dir")
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("--run-dir", required=True)
    approve_parser = commands.add_parser("approve")
    approve_parser.add_argument("--run-dir", required=True)
    approve_parser.add_argument("--node")
    approve_parser.add_argument("--by", default=os.environ.get("USER", "operator"))
    pause_parser = commands.add_parser("pause")
    pause_parser.add_argument("--run-dir", required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--run-dir", required=True)
    visualize_parser = commands.add_parser("visualize")
    visualize_parser.add_argument("--workflow", default=str(ROOT / "workflow.json"))
    visualize_parser.add_argument("--workspace", default=".")
    visualize_parser.add_argument("--runs-dir")
    visualize_parser.add_argument("--run-dir")
    visualize_parser.add_argument("--host", default="127.0.0.1")
    visualize_parser.add_argument("--port", type=int, default=8765)
    visualize_parser.add_argument("--static-dir")
    visualize_parser.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.command == "validate":
        workflow = load_json(Path(args.workflow).resolve())
        print(f"valid workflow: {workflow['name']} ({len(validate_workflow(workflow))} nodes)")
        return 0
    if args.command == "start":
        return start_run(args)
    if args.command == "resume":
        return resume_run(args)
    if args.command == "approve":
        return approve_run(args)
    if args.command == "pause":
        pause_run(Path(args.run_dir).resolve())
        return 0
    if args.command == "status":
        print(json.dumps(load_json(Path(args.run_dir).resolve() / "state.json"), ensure_ascii=False, indent=2))
        return 0
    return visualize(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkflowError as exc:
        print(f"workflow error: {exc}", file=sys.stderr)
        raise SystemExit(2)
