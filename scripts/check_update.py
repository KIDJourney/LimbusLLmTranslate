#!/usr/bin/env python3
"""Check update verification (migrated).

Explicitly rejects legacy CDN queries and static snapshot hashes.
Active workflows must use scripts/translation_pipeline.py prepare with SSH Windows source.
"""

from __future__ import annotations

import sys


def main():
    print(
        "Error: check_update.py legacy CDN discovery is deprecated and rejected. "
        "Active workflows must obtain Korean source exclusively from the Windows game directory via SSH "
        "using windows_source.py. Run scripts/translation_pipeline.py prepare instead.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
