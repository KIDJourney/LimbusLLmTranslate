import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error

USER_AGENT = "LimbusTranslationUpdater/1.0"
LIVE_LATEST_URL = "https://limbus-cn.deadfish.win/latest.json"

def fetch_json(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Cache-Control': 'no-cache'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        sys.exit(1)

def verify_file(url, expected_sha, expected_size):
    import hashlib
    print(f"Verifying {url}...")
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read()
            actual_size = len(data)
            actual_sha = hashlib.sha256(data).hexdigest()
            
            if actual_size != expected_size or actual_sha != expected_sha:
                print(f"Mismatch for {url}: size {actual_size}/{expected_size}, sha {actual_sha[:8]}/{expected_sha[:8]}")
                sys.exit(1)
            print(f"Verified successfully: {url}")
    except Exception as e:
        print(f"Failed to verify {url}: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Check published release against local run build/public")
    parser.add_argument("--run-dir", default=None, help="Workflow run directory")
    args = parser.parse_args()

    raw_run_dir = args.run_dir or os.environ.get("WORKFLOW_RUN_DIR")
    if not raw_run_dir:
        print("Error: Missing --run-dir argument and WORKFLOW_RUN_DIR is not set. Refusing to fallback to root.", file=sys.stderr)
        sys.exit(2)

    run_dir = Path(raw_run_dir).resolve()
    latest_path = run_dir / "build/public/latest.json"
    if not latest_path.is_file():
        print(f"Error: {latest_path} not found in run directory.", file=sys.stderr)
        sys.exit(2)

    # 1. Fetch live latest.json
    print(f"Fetching {LIVE_LATEST_URL}...")
    live_latest = fetch_json(LIVE_LATEST_URL)

    # 2. Read local latest.json strictly from run_dir
    try:
        with open(latest_path, 'r', encoding='utf-8') as f:
            local_latest = json.load(f)
    except Exception as e:
        print(f"Failed to read run latest.json ({latest_path}): {e}")
        sys.exit(1)
        
    # 3. Compare fields
    fields_to_check = ['version', 'source', 'package', 'updater']
    for field in fields_to_check:
        if live_latest.get(field) != local_latest.get(field):
            print(f"Field mismatch: '{field}' differs between live and local.")
            print(f"Live: {live_latest.get(field)}")
            print(f"Local: {local_latest.get(field)}")
            sys.exit(1)
            
    print("latest.json fields match successfully.")
    
    # 4. Verify URLs
    verify_file(
        live_latest['package']['url'],
        live_latest['package']['sha256'],
        live_latest['package']['size']
    )
    verify_file(
        live_latest['updater']['url'],
        live_latest['updater']['sha256'],
        live_latest['updater']['size']
    )
    
    print(f"Publish verified successfully. Online version: {live_latest['version']}")
    print(f"Package: {live_latest['package']['url']}")
    print(f"Updater: {live_latest['updater']['url']}")

if __name__ == "__main__":
    main()
