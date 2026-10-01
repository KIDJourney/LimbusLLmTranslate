import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import zipfile
import tempfile
import time
import shlex
import hashlib

VPS_HOST = "152.42.184.69"
SSH_TARGET = f"root@{VPS_HOST}"
VPS_CURRENT_LINK = "/var/www/limbus-cn/current"
RECEIPT_FILE = "build/public/activate-receipt.json"

def run_ssh(cmd, check=True, timeout=180):
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_TARGET, cmd]
    res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise Exception(f"SSH command failed with code {res.returncode}:\n{res.stderr}")
    return res

def run_scp_upload(local_path, remote_path, timeout=60):
    scp_cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", local_path, f"{SSH_TARGET}:{remote_path}"]
    res = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise Exception(f"SCP upload failed with code {res.returncode}:\n{res.stderr}")

def write_receipt(run_dir, success, error=None):
    public_dir = Path(run_dir) / "build/public"
    public_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = public_dir / "activate-receipt.json"
    receipt = {
        "status": "success" if success else "failed",
        "timestamp": time.time()
    }
    if error:
        receipt["error"] = str(error)

    with open(receipt_file, 'w', encoding='utf-8') as f:
        json.dump(receipt, f, indent=2)

def hash_file(filepath):
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def verify_updater_zip(updater_zip_path, expected_sha, expected_size):
    print(f"Verifying {updater_zip_path}...")
    actual_size = os.path.getsize(updater_zip_path)
    actual_sha = hash_file(updater_zip_path)
    
    assert actual_size == expected_size, f"Updater size mismatch: {actual_size} != {expected_size}"
    assert actual_sha == expected_sha, f"Updater SHA mismatch: {actual_sha} != {expected_sha}"
    
    with zipfile.ZipFile(updater_zip_path, 'r') as zf:
        namelist = zf.namelist()
        required_files = [
            'Update-LimbusTranslation.bat',
            'Update-LimbusTranslation.ps1',
            'Install-LimbusTranslation.ps1',
            'README_Updater.md'
        ]
        for req in required_files:
            if req not in namelist:
                raise Exception(f"Missing {req} in updater zip")
                
        # Verify UA inside scripts
        with zf.open('Update-LimbusTranslation.ps1') as f:
            content = f.read().decode('utf-8')
            if "Invoke-RestMethod -UserAgent 'LimbusTranslationUpdater/1.0'" not in content:
                raise Exception("Missing UserAgent in Invoke-RestMethod")
            if "Invoke-WebRequest -UserAgent 'LimbusTranslationUpdater/1.0'" not in content:
                raise Exception("Missing UserAgent in Invoke-WebRequest")

def main():
    parser = argparse.ArgumentParser(description="Activate translation release on VPS")
    parser.add_argument("--run-dir", default=None, help="Workflow run directory containing build/public")
    args = parser.parse_args()

    raw_run_dir = args.run_dir or os.environ.get("WORKFLOW_RUN_DIR")
    if not raw_run_dir:
        print("Error: Missing --run-dir argument and WORKFLOW_RUN_DIR is not set. Refusing to fallback to root.", file=sys.stderr)
        sys.exit(2)

    run_dir = Path(raw_run_dir).resolve()
    public_dir = run_dir / "build/public"
    if not public_dir.is_dir():
        print(f"Error: Run public directory not found: {public_dir}", file=sys.stderr)
        sys.exit(2)

    latest_path = public_dir / "latest.json"
    receipt_path = public_dir / "upload-receipt.json"

    if not latest_path.is_file() or not receipt_path.is_file():
        print(f"Error: Missing required release metadata in {public_dir}", file=sys.stderr)
        sys.exit(2)

    try:
        # 1. Verification
        with open(latest_path, 'r', encoding='utf-8') as f:
            latest = json.load(f)

        with open(receipt_path, 'r', encoding='utf-8') as f:
            receipt = json.load(f)

        assert latest.get("schema_version") == 1, "Invalid schema version"
        assert latest["package"]["url"].startswith("https://limbus-cdn.deadfish.win/"), "Invalid package URL domain"
        assert latest["updater"]["url"].startswith("https://limbus-cdn.deadfish.win/"), "Invalid updater URL domain"

        # Verify sizes, hashes, and URLs match receipt exactly
        assert latest["package"]["sha256"] == receipt["lang_zip"]["sha256"]
        assert latest["package"]["size"] == receipt["lang_zip"]["bytes"]
        assert latest["package"]["url"] == receipt["lang_zip"]["url"]

        assert latest["updater"]["sha256"] == receipt["updater_zip"]["sha256"]
        assert latest["updater"]["size"] == receipt["updater_zip"]["bytes"]
        assert latest["updater"]["url"] == receipt["updater_zip"]["url"]

        # Verify updater.zip comprehensively
        updater_sha = latest["updater"]["sha256"]
        updater_sha_prefix = updater_sha[:12]
        updater_zip_path = public_dir / f"LimbusUpdater-{updater_sha_prefix}.zip"
        if not updater_zip_path.is_file():
            raise FileNotFoundError(f"Updater zip not found in run public dir: {updater_zip_path}")
        verify_updater_zip(str(updater_zip_path), latest["updater"]["sha256"], latest["updater"]["size"])

        print("Local verification passed. Uploading to server...")

        # 2. Upload latest.json via SCP
        # Determine actual current target directory with readlink -f
        res = run_ssh(f"readlink -f {VPS_CURRENT_LINK}")
        target_dir = res.stdout.strip()
        if not target_dir:
            raise Exception("Could not resolve current release symlink")

        # Security validation for target_dir
        if not target_dir.startswith("/var/www/limbus-cn/releases/"):
            raise Exception(f"Invalid target directory: {target_dir}")
        if " " in target_dir or ".." in target_dir:
            raise Exception(f"Invalid characters in target directory: {target_dir}")

        remote_latest_path = shlex.quote(f"{target_dir}/latest.json")
        tmp_latest_path_unquoted = f"{target_dir}/latest.json.tmp_{int(time.time())}"
        tmp_latest_path = shlex.quote(tmp_latest_path_unquoted)
        bak_latest_path = shlex.quote(f"{target_dir}/latest.json.bak_{int(time.time())}")

        run_scp_upload(str(latest_path), tmp_latest_path_unquoted)
        run_ssh(f"chmod 644 {tmp_latest_path}")

        # Backup old and swap
        run_ssh(f"if [ -f {remote_latest_path} ]; then cp {remote_latest_path} {bak_latest_path}; fi")
        run_ssh(f"mv {tmp_latest_path} {remote_latest_path}")

        print("Server latest.json activated successfully.")

        # 3. Synchronize local site/latest.json
        os.makedirs('site', exist_ok=True)
        with open('site/latest.json', 'w', encoding='utf-8') as f:
            json.dump(latest, f, indent=2)

        write_receipt(run_dir, True)
        print("Activation complete.")

    except Exception as e:
        write_receipt(run_dir, False, e)
        print(f"Activation failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
