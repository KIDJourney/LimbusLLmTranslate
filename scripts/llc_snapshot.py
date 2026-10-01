"""Fetch and extract latest LocalizeLimbusCompany Chinese translation release (stdlib only).

Safely fetches release metadata, handles GitHub API rate limits via web expanded_assets,
verifies official asset SHA256 digests, fetches upstream LICENSE, unpacks LLC_zh-CN without symlinks,
and produces provenance.json with license, release, tag, asset SHA256 and font info.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

GITHUB_REPO = "LocalizeLimbusCompany/LocalizeLimbusCompany"
API_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
WEB_RELEASE_LATEST = f"https://github.com/{GITHUB_REPO}/releases/latest"
UPSTREAM_LICENSE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/LICENSE"
USER_AGENT = "LimbusLLmTranslate"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "llc"


def http_get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def safe_relative_path(rel_str: str) -> PurePosixPath:
    p = PurePosixPath(rel_str)
    if not rel_str or p.is_absolute() or ".." in p.parts or "\\" in rel_str or ":" in rel_str:
        raise ValueError(f"Unsafe path: {rel_str}")
    return p


def parse_expanded_assets(expanded_html: str, tag: str) -> tuple[str, str, str]:
    """Strictly parses release asset name, download url, and sha256 digest from expanded_assets HTML.

    Requires asset link and clipboard-copy button with digest to belong to the exact same <li> container.
    """
    li_blocks = re.findall(r"<li\b[^>]*>(.*?)</li>", expanded_html, flags=re.DOTALL)
    for block in li_blocks:
        link_match = re.search(
            rf'href="(/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/download/{re.escape(tag)}/(LimbusLocalize_[A-Za-z0-9._-]+\.zip))"',
            block,
        )
        if not link_match:
            continue
        rel_url, asset_name = link_match.group(1), link_match.group(2)
        digest_match = re.search(r'value="sha256:([a-fA-F0-9]{64})"', block)
        aria_match = re.search(r'aria-label="[^"]*' + re.escape(asset_name) + r'[^"]*"', block)
        if not digest_match or not aria_match:
            raise ValueError(f"Found asset {asset_name} but could not bind verified sha256 digest in same asset container")
        expected_sha256 = digest_match.group(1).lower()
        download_url = f"https://github.com{rel_url}"
        return asset_name, download_url, expected_sha256

    raise ValueError(f"No verified zip asset container found in expanded_assets for release {tag}")


def fetch_release_info() -> dict:
    """Fetches release info. Tries official GitHub API first; falls back to HTML parsing if rate limited."""
    now_ts = int(datetime.datetime.now().timestamp())
    api_url = f"{API_RELEASE_URL}?_={now_ts}"
    try:
        data = json.loads(http_get(api_url).decode("utf-8"))
        tag = data.get("tag_name")
        html_url = data.get("html_url")
        assets = data.get("assets", [])
        zip_asset = next(
            (a for a in assets if a.get("name", "").startswith("LimbusLocalize_") and a.get("name", "").endswith(".zip")),
            None,
        )
        if tag and zip_asset and zip_asset.get("browser_download_url") and zip_asset.get("digest"):
            return {
                "tag": tag,
                "html_url": html_url or f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
                "asset_name": zip_asset["name"],
                "download_url": zip_asset["browser_download_url"],
                "expected_sha256": zip_asset["digest"].lower().removeprefix("sha256:"),
                "source": "github_api",
            }
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, ValueError):
        pass

    # HTML fallback via latest redirect & expanded_assets
    req = urllib.request.Request(WEB_RELEASE_LATEST, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        final_url = resp.geturl()

    match = re.search(r"^https://github\.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/([^/?#]+)$", final_url)
    if not match:
        raise ValueError(f"Could not verify official latest release tag redirect: {final_url}")
    tag = match.group(1)

    expanded_url = f"https://github.com/{GITHUB_REPO}/releases/expanded_assets/{tag}"
    expanded_html = http_get(expanded_url).decode("utf-8")

    asset_name, download_url, expected_sha256 = parse_expanded_assets(expanded_html, tag)

    return {
        "tag": tag,
        "html_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
        "asset_name": asset_name,
        "download_url": download_url,
        "expected_sha256": expected_sha256,
        "source": "github_expanded_assets",
    }


def fetch_upstream_license() -> str:
    raw = http_get(UPSTREAM_LICENSE_URL)
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("Upstream license fetched is empty")
    return text


def unpack_and_verify_asset(blob: bytes, expected_sha256: str) -> dict[str, bytes]:
    computed_sha256 = hashlib.sha256(blob).hexdigest().lower()
    if computed_sha256 != expected_sha256.lower():
        raise ValueError(
            f"Asset SHA256 mismatch: expected {expected_sha256.lower()}, got {computed_sha256}"
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as exc:
        raise ValueError("Corrupted release zip file") from exc

    prefix = "LimbusCompany_Data/Lang/LLC_zh-CN/"
    results = {}
    seen = set()

    for info in archive.infolist():
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"Archive symlinks are unsupported: {info.filename}")
        if info.is_dir():
            continue
        safe_relative_path(info.filename)
        if not info.filename.startswith(prefix):
            raise ValueError(f"Unexpected release archive member: {info.filename}")

        rel = safe_relative_path(info.filename[len(prefix):])
        rel_str = str(rel)
        if rel_str.casefold() in seen:
            raise ValueError(f"Duplicate path in release: {rel_str}")
        seen.add(rel_str.casefold())

        data = archive.read(info)
        if rel.suffix == ".json":
            try:
                json.loads(data.decode("utf-8-sig"))
            except Exception as exc:
                raise ValueError(f"Invalid JSON in release file {rel_str}: {exc}") from exc

        results[rel_str] = data

    if not any(k.endswith(".json") for k in results.keys()):
        raise ValueError("Empty Chinese archive: no JSON files found")

    return results


def verify_and_copy_vendor_fonts(dest_font_dir: Path) -> dict:
    """Verifies vendor/llc font manifest and copies font files into destination LLC_zh-CN/Font directory.

    Skips .gitkeep so placeholder tracking files never leak into snapshots or installers.
    Ensures safe relative paths, strict size/sha256 matching, and non-empty Context TTF/OTF fonts.
    """
    manifest_path = VENDOR_DIR / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Vendor manifest missing: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dest_font_dir.mkdir(parents=True, exist_ok=True)
    copied_files = {}

    for rel_path, meta in manifest.items():
        if not rel_path.startswith("Font/"):
            continue
        p = safe_relative_path(rel_path)
        # Never leak .gitkeep placeholder files into snapshot or installer directories
        if p.name == ".gitkeep":
            continue

        src = VENDOR_DIR / rel_path
        if not src.exists():
            raise FileNotFoundError(f"Vendor font file missing: {src}")
        stat = src.stat()
        if "size" in meta and stat.st_size != meta["size"]:
            raise ValueError(f"Vendor font size mismatch for {rel_path}: expected {meta['size']}, got {stat.st_size}")

        data = src.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest().lower()
        if actual_sha != meta["sha256"].lower():
            raise ValueError(f"Vendor font SHA256 mismatch for {rel_path}: expected {meta['sha256']}, got {actual_sha}")
        sub_rel = rel_path[len("Font/"):]
        target = dest_font_dir / sub_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied_files[rel_path] = meta["sha256"]

    # Verify context fonts are not empty and have size > 0
    context_dir = dest_font_dir / "Context"
    if not context_dir.exists():
        raise ValueError("Font/Context directory is missing")
    valid_context_fonts = [
        f for f in context_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".ttf", ".otf"} and f.stat().st_size > 0
    ]
    if not valid_context_fonts:
        raise ValueError("Font/Context directory must contain at least one non-empty TTF/OTF font")
    title_dir = dest_font_dir / "Title"
    title_dir.mkdir(parents=True, exist_ok=True)

    return copied_files


def capture_llc_snapshot(
    output_dir: str | Path,
    release_info: dict | None = None,
    license_text: str | None = None,
    asset_blob: bytes | None = None,
    vendor_dir: Path | None = None,
) -> dict:
    out = Path(output_dir).resolve()
    if out.exists():
        raise ValueError(f"Output directory already exists: {out}")

    captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if release_info is None:
        release_info = fetch_release_info()
    if license_text is None:
        license_text = fetch_upstream_license()
    if asset_blob is None:
        asset_blob = http_get(release_info["download_url"])

    files = unpack_and_verify_asset(asset_blob, release_info["expected_sha256"])

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".llc-snap-", dir=out.parent) as temp:
        temp_dir = Path(temp) / "stage"
        temp_dir.mkdir()

        llc_dir = temp_dir / "LLC_zh-CN"
        llc_dir.mkdir(parents=True)
        for rel_str, data in files.items():
            dest = llc_dir / rel_str
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        # Check if release itself contained fonts; if not, populate from verified vendor fonts
        font_dir = llc_dir / "Font"
        release_has_font = font_dir.exists() and any(font_dir.rglob("*.ttf"))
        if not release_has_font:
            font_hashes = verify_and_copy_vendor_fonts(font_dir)
            font_source = "vendor_llc"
        else:
            font_hashes = {p.relative_to(llc_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in font_dir.rglob("*") if p.is_file()}
            font_source = "release_archive"

        provenance = {
            "release": release_info["tag"],
            "release_url": release_info["html_url"],
            "asset_name": release_info["asset_name"],
            "asset_sha256": release_info["expected_sha256"],
            "metadata_source": release_info["source"],
            "captured_at": captured_at,
            "total_files": len(files),
            "font_source": font_source,
            "fonts": font_hashes,
        }

        license_file = temp_dir / "LICENSE_UPSTREAM.txt"
        license_file.write_text(license_text, encoding="utf-8")

        prov_file = temp_dir / "provenance.json"
        prov_file.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        temp_dir.rename(out)

    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path to output directory (e.g. <snapshot>/llc)")
    args = parser.parse_args()

    prov = capture_llc_snapshot(output_dir=args.output)
    summary = {
        "release": prov["release"],
        "asset_name": prov["asset_name"],
        "asset_sha256": prov["asset_sha256"],
        "total_files": prov["total_files"],
        "font_source": prov["font_source"],
        "output": str(Path(args.output).resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
