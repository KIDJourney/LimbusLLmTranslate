#!/usr/bin/env python3
"""Check cycle entrypoint (migrated from legacy CDN).

Retains GitHub API & fallback release resolution helpers for test compatibility.
Explicitly refuses legacy official CDN queries to ensure active workflows never secretly hit CDN.
New workflows must use scripts/translation_pipeline.py prepare.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import localization


def get_github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return result.stdout.strip()
    except Exception:
        return None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def get_fallback_github_release_tag(url: str) -> tuple[str, str]:
    from urllib.parse import urlparse

    req = urllib.request.Request(url, headers={"User-Agent": "LimbusTranslationUpdater/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            final_url = response.geturl()
            parsed = urlparse(final_url)

            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise ValueError("Fallback URL must be https://github.com/...")

            if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
                raise ValueError("Fallback URL must not contain auth, port, query or fragment")

            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) == 5
                and parts[0] == "LocalizeLimbusCompany"
                and parts[1] == "LocalizeLimbusCompany"
                and parts[2] == "releases"
                and parts[3] == "tag"
                and parts[4]
            ):
                return parts[4], final_url
            raise ValueError(f"Invalid fallback tag path: {parsed.path}")
    except Exception as e:
        raise e


def get_latest_github_release(url: str) -> tuple[str, str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValueError("URL must be https://api.github.com/...")

    headers = {"User-Agent": "LimbusTranslationUpdater/1.0"}
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=30) as response:
            return json.loads(response.read().decode())["tag_name"], "api", url
    except urllib.error.HTTPError as e:
        if e.code == 403 and int(e.headers.get("X-RateLimit-Remaining", 1)) == 0:
            fallback_url = "https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/latest"
            tag, final_url = get_fallback_github_release_tag(fallback_url)
            return tag, "html", final_url
        raise e
    except Exception as e:
        raise e


def get_latest_published(url: str, user_agent: str = "LimbusTranslationUpdater/1.0") -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"HTTP Error fetching {url}: {e.code} {e.reason}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error fetching/parsing {url}: {e}", file=sys.stderr)
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description="Cycle check (migrated)")
    parser.add_argument("--run-dir", required=False, help="Run directory")
    args, _ = parser.parse_known_args()

    print(
        "Error: check_cycle.py CDN query mode is deactivated. Korean source must only be obtained "
        "from the Windows game directory via SSH using windows_source.py. Active workflows cannot "
        "secretly query official CDN.\n"
        "Please run: python3 scripts/translation_pipeline.py prepare --run-dir <run_dir>",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
