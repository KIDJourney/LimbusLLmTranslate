import subprocess
from datetime import datetime
import json
import hashlib
from pathlib import Path
import sys

def run(cmd):
    print(f"[*] {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[!] Error: {res.stderr}", file=sys.stderr)
        sys.exit(1)
    return res.stdout.strip()

VPS = "root@152.42.184.69"
WWW_ROOT = "/var/www/deadfish"
LOCAL_DIR = "/Users/kidjourney/Workspaces/deadfish"

with open(f"{LOCAL_DIR}/index.html", "rb") as f:
    html_hash = hashlib.sha256(f.read()).hexdigest()[:12]

release_name = f"{html_hash}"
release_path = f"{WWW_ROOT}/releases/{release_name}"

# check if release exists
check_res = subprocess.run(f"ssh -o BatchMode=yes {VPS} 'ls -d {release_path}'", shell=True, capture_output=True)
if check_res.returncode == 0:
    print("[*] Release already exists.")
else:
    run(f"ssh -o BatchMode=yes {VPS} 'mkdir -p {release_path}'")
    # copy existing resources from current instead of re-uploading everything, just to be safe and fast since no other changes
    run(f"ssh -o BatchMode=yes {VPS} 'cp -R {WWW_ROOT}/current/* {release_path}/'")
    # sync local changes
    run(f"rsync -avz --delete {LOCAL_DIR}/ {VPS}:{release_path}/")

# swap symlink
tmp_link = f"{WWW_ROOT}/current_tmp"
run(f"ssh -o BatchMode=yes {VPS} 'ln -s releases/{release_name} {tmp_link} && mv -Tf {tmp_link} {WWW_ROOT}/current'")
print("[*] Deployed successfully.")
