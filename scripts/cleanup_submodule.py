import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent

def main():
    print("Verifying preconditions for submodule removal...")
    # 1. Fresh git status must be clean for submodule itself
    sub_res = subprocess.run(["git", "-C", "text_data/LocalizeLimbusCompany", "status", "--porcelain"], capture_output=True, text=True)
    if sub_res.returncode != 0 or sub_res.stdout.strip():
        raise RuntimeError(f"Submodule working tree is dirty: {sub_res.stdout.strip()}")

    # Get submodule commit
    commit_res = subprocess.run(["git", "-C", "text_data/LocalizeLimbusCompany", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    sub_commit = commit_res.stdout.strip()

    # 2. Verify vendor fonts and license hashes
    vendor_manifest_path = ROOT / "vendor/llc/manifest.json"
    if not vendor_manifest_path.is_file():
        raise RuntimeError("Missing vendor/llc/manifest.json")
    vendor_manifest = json.loads(vendor_manifest_path.read_text(encoding="utf-8"))
    for rel_path, meta in vendor_manifest.items():
        src = ROOT / "vendor/llc" / rel_path
        if not src.is_file():
            raise RuntimeError(f"Missing vendor file: {src}")
        data = src.read_bytes()
        actual_size = len(data)
        actual_hash = hashlib.sha256(data).hexdigest().lower()
        if actual_size != meta.get("size") or actual_hash != meta.get("sha256", "").lower():
            raise RuntimeError(f"Hash/size verification failed for {rel_path}")

    # 3. Verify real snapshot exists
    snap_dir = ROOT / "data/snapshots/windows-20261001-006787"
    if not (snap_dir / "source/provenance.json").is_file() or not (snap_dir / "llc/provenance.json").is_file():
        raise RuntimeError(f"Real snapshot not verified at {snap_dir}")

    # 4. Save cleanup receipt
    receipt = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": "decommission_submodule",
        "submodule_path": "text_data/LocalizeLimbusCompany",
        "submodule_commit": sub_commit,
        "vendor_manifest": vendor_manifest,
        "verified_snapshot": str(snap_dir.relative_to(ROOT)),
    }
    receipt_dir = ROOT / "data/receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / "submodule_cleanup_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Cleanup receipt saved: {receipt_path}")

    # 5. Precision removal:
    # Remove from git index using git rm --cached
    subprocess.run(["git", "rm", "--cached", "text_data/LocalizeLimbusCompany"], check=True)

    # Remove working directory
    sub_workdir = ROOT / "text_data/LocalizeLimbusCompany"
    if sub_workdir.exists():
        shutil.rmtree(sub_workdir)
        print(f"Removed working tree: {sub_workdir}")

    # Remove .git/modules/text_data/LocalizeLimbusCompany if present
    sub_git_dir = ROOT / ".git/modules/text_data/LocalizeLimbusCompany"
    if sub_git_dir.exists():
        shutil.rmtree(sub_git_dir)
        print(f"Removed .git/modules entry: {sub_git_dir}")

    # Remove .gitmodules entry
    gitmodules_file = ROOT / ".gitmodules"
    if gitmodules_file.is_file():
        gitmodules_file.unlink()
        print(f"Removed .gitmodules")

    # Clean up parent text_data directory if empty
    text_data_dir = ROOT / "text_data"
    if text_data_dir.exists() and not any(text_data_dir.iterdir()):
        text_data_dir.rmdir()
        print("Removed empty text_data directory")

    print("Submodule cleanup completed successfully.")

if __name__ == "__main__":
    main()
