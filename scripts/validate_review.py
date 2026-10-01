import json
import os
import sys
import hashlib
from pathlib import Path

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def main():
    print(
        "ERROR: scripts/validate_review.py is obsolete and has been deprecated to fail closed.\n"
        "Do not use this legacy 25-item static validator.\n"
        "Use official validator instead: python3 scripts/translation_pipeline.py validate --run-dir <run_dir>",
        file=sys.stderr,
    )
    sys.exit(2)
    
if __name__ == "__main__":
    main()
