import subprocess
import tempfile
from pathlib import Path
import json
import sys
import unittest

class TestWorkflowRouting(unittest.TestCase):
    def run_workflow(self, workflow_json, expected_target, expect_error=False):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            wf_path = tmp_path / "wf.json"
            wf_path.write_text(json.dumps(workflow_json))
            
            # Test validation first
            val_res = subprocess.run([sys.executable, ".dev-workflow/run.py", "validate", "--workflow", str(wf_path)], capture_output=True, text=True)
            if expect_error:
                self.assertNotEqual(val_res.returncode, 0, f"Expected error for {workflow_json['name']}, but it validated.")
                if "workflow error" in val_res.stderr:
                    pass # We expect workflow error message
                return
                
            self.assertEqual(val_res.returncode, 0, f"Validation failed for {workflow_json['name']}: {val_res.stderr}")

            # Run workflow
            run_res = subprocess.run(
                [sys.executable, ".dev-workflow/run.py", "start", "--workflow", str(wf_path), "--workspace", str(tmp_path), "--request", "test"],
                capture_output=True, text=True
            )
            
            runs_dir = tmp_path / ".workflow-runs"
            latest_run = max([p for p in runs_dir.iterdir() if (p / "state.json").exists()], key=lambda p: p.name)
            state = json.loads((latest_run / "state.json").read_text())
            
            history = state.get("history", [])
            self.assertTrue(history, f"No history for {workflow_json['name']}. stdout: {run_res.stdout}, stderr: {run_res.stderr}")
                
            actual_target = history[0]["target"]
            self.assertEqual(actual_target, expected_target, f"{workflow_json['name']}: Expected target {expected_target}, got {actual_target}. Output: {run_res.stdout}")

    def test_normal_pass(self):
        wf = {
            "version": 2,
            "name": "Normal Pass",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "command",
                "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}],
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "$complete")
        
    def test_exit_route_10(self):
        wf = {
            "version": 2,
            "name": "Exit Route 10",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [
                {
                    "id": "n1",
                    "type": "command",
                    "exit_routes": {"10": "n2"},
                    "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(10)"]}],
                    "on": {"passed": "$complete", "failed": "$failed"}
                },
                {
                    "id": "n2",
                    "type": "command",
                    "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}],
                    "on": {"passed": "$complete", "failed": "$failed"}
                }
            ]
        }
        self.run_workflow(wf, "n2")

    def test_normal_fail(self):
        wf = {
            "version": 2,
            "name": "Normal Fail",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "command",
                "exit_routes": {"10": "$complete"},
                "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(2)"]}],
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "$failed")
        
    def test_gate_fails_exit_route(self):
        wf = {
            "version": 2,
            "name": "Gate Fails Exit Route",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [
                {
                    "id": "n1",
                    "type": "command",
                    "exit_routes": {"10": "n2"},
                    "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(10)"]}],
                    "gates": [{"type": "command", "command": [sys.executable, "-c", "import sys; sys.exit(1)"]}],
                    "on": {"passed": "$complete", "failed": "$failed"}
                },
                {
                    "id": "n2",
                    "type": "command",
                    "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}],
                    "on": {"passed": "$complete", "failed": "$failed"}
                }
            ]
        }
        self.run_workflow(wf, "$failed")

    def test_invalid_schema_multiple_actions(self):
        wf = {
            "version": 2,
            "name": "Invalid Exit Route - Multiple Actions",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "command",
                "exit_routes": {"10": "$complete"},
                "actions": [
                    {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]},
                    {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}
                ],
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "", expect_error=True)
        
    def test_invalid_schema_agent_node(self):
        wf = {
            "version": 2,
            "name": "Invalid Exit Route - Agent Node",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "agent",
                "exit_routes": {"10": "$complete"},
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "", expect_error=True)
        
    def test_invalid_schema_wrong_key_type(self):
        wf = {
            "version": 2,
            "name": "Invalid Exit Route - Wrong Key",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "command",
                "exit_routes": {"not_int": "$complete"},
                "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}],
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "", expect_error=True)
        
    def test_invalid_schema_wrong_target(self):
        wf = {
            "version": 2,
            "name": "Invalid Exit Route - Unknown Target",
            "entry": "n1",
            "agent": {"backend": "codex-sdk"},
            "nodes": [{
                "id": "n1",
                "type": "command",
                "exit_routes": {"10": "unknown_node"},
                "actions": [{"command": [sys.executable, "-c", "import sys; sys.exit(0)"]}],
                "on": {"passed": "$complete", "failed": "$failed"}
            }]
        }
        self.run_workflow(wf, "", expect_error=True)

if __name__ == "__main__":
    unittest.main()
