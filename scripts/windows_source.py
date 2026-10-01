"""Windows SSH live game localization source extractor (stdlib only).

Safely streams and verifies Korean game localization files from Windows Steam installation
via SSH and in-memory zip compression without leaving artifacts on Windows.
Detects in-flight modifications on Windows, verifies symlinks/reparse points, and performs
strict manifest & JSON validation.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import zipfile

DEFAULT_REMOTE_PATH = r"F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data\Assets\Resources_moved\Localize\kr"
DEFAULT_SSH_HOST = "windows"
DEFAULT_CONNECT_TIMEOUT = 15
DEFAULT_PROCESS_TIMEOUT = 600
MANIFEST_ENTRY_NAME = "__win_manifest__.json"


def safe_relative_path(rel_str: str) -> PurePosixPath:
    p = PurePosixPath(rel_str)
    if not rel_str or p.is_absolute() or ".." in p.parts or "\\" in rel_str or ":" in rel_str:
        raise ValueError(f"Unsafe path: {rel_str}")
    return p


def normalize_kr_rel_path(remote_rel_path: str) -> str:
    """Strip KR_ prefix from filename while keeping subdirectories intact.

    Reject empty paths, unsafe paths, and non-json files.
    """
    rel = safe_relative_path(remote_rel_path.replace("\\", "/"))
    if rel.suffix != ".json":
        raise ValueError(f"Non-JSON file in KR resource: {remote_rel_path}")
    name = rel.name
    if name.startswith("KR_"):
        normalized_name = name[3:]
    else:
        normalized_name = name
    if not normalized_name or normalized_name == ".json":
        raise ValueError(f"Invalid normalized filename: {remote_rel_path}")
    if len(rel.parts) > 1:
        return (PurePosixPath(*rel.parts[:-1]) / normalized_name).as_posix()
    return normalized_name


def build_powershell_script(remote_path: str) -> str:
    escaped_path = remote_path.replace("'", "''")
    return f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$target = '{escaped_path}'

if (-not (Test-Path -LiteralPath $target)) {{
    [Console]::Error.WriteLine("TARGET_NOT_FOUND: $target")
    exit 3
}}

$targetItem = Get-Item -LiteralPath $target
if ($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {{
    [Console]::Error.WriteLine("REPARSE_POINT_TARGET: $target")
    exit 5
}}

$allDirs = Get-ChildItem -LiteralPath $target -Recurse -Directory
foreach ($d in $allDirs) {{
    if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {{
        [Console]::Error.WriteLine("REPARSE_POINT_DIR: $($d.FullName)")
        exit 5
    }}
}}

$files1 = Get-ChildItem -LiteralPath $target -Recurse -File
if (-not $files1 -or $files1.Count -eq 0) {{
    [Console]::Error.WriteLine("NO_FILES_FOUND: $target")
    exit 4
}}

$sha = [System.Security.Cryptography.SHA256]::Create()
$beforeMap = [System.Collections.Generic.Dictionary[string, string]]::new()
foreach ($f in $files1) {{
    if ($f.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {{
        [Console]::Error.WriteLine("REPARSE_POINT_FILE: $($f.FullName)")
        exit 5
    }}
    $rel = $f.FullName.Substring($target.Length).TrimStart('\\').Replace('\\', '/')
    $fs = [System.IO.File]::OpenRead($f.FullName)
    $hashHex = [System.BitConverter]::ToString($sha.ComputeHash($fs)).Replace('-', '').ToLowerInvariant()
    $fs.Dispose()
    $beforeMap[$rel] = $hashHex
}}

Add-Type -AssemblyName System.IO.Compression
$mem = New-Object System.IO.MemoryStream
$zip = New-Object System.IO.Compression.ZipArchive($mem, [System.IO.Compression.ZipArchiveMode]::Create, $true)
foreach ($f in $files1) {{
    $rel = $f.FullName.Substring($target.Length).TrimStart('\\').Replace('\\', '/')
    $entry = $zip.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Fastest)
    $es = $entry.Open()
    $fs = [System.IO.File]::OpenRead($f.FullName)
    $fs.CopyTo($es)
    $fs.Dispose()
    $es.Dispose()
}}

$files2 = Get-ChildItem -LiteralPath $target -Recurse -File
if ($files2.Count -ne $beforeMap.Count) {{
    [Console]::Error.WriteLine("MUTATION_DETECTED_COUNT: before $($beforeMap.Count), after $($files2.Count)")
    exit 6
}}
foreach ($f in $files2) {{
    $rel = $f.FullName.Substring($target.Length).TrimStart('\\').Replace('\\', '/')
    if (-not $beforeMap.ContainsKey($rel)) {{
        [Console]::Error.WriteLine("MUTATION_DETECTED_NEW_FILE: $rel")
        exit 6
    }}
    $fs = [System.IO.File]::OpenRead($f.FullName)
    $hashHex = [System.BitConverter]::ToString($sha.ComputeHash($fs)).Replace('-', '').ToLowerInvariant()
    $fs.Dispose()
    if ($beforeMap[$rel] -ne $hashHex) {{
        [Console]::Error.WriteLine("MUTATION_DETECTED_HASH_CHANGED: $rel")
        exit 6
    }}
}}

$manifestEntry = $zip.CreateEntry('{MANIFEST_ENTRY_NAME}', [System.IO.Compression.CompressionLevel]::Fastest)
$ms = $manifestEntry.Open()
$mw = New-Object System.IO.StreamWriter($ms, [System.Text.UTF8Encoding]::new($false))
$mw.Write(($beforeMap | ConvertTo-Json -Depth 2 -Compress))
$mw.Flush()
$mw.Dispose()
$ms.Dispose()

$zip.Dispose()
$bytes = $mem.ToArray()
$mem.Dispose()

$out = [System.Console]::OpenStandardOutput()
$out.Write($bytes, 0, $bytes.Length)
$out.Flush()
"""


def capture_windows_kr_stream(
    ssh_host: str = DEFAULT_SSH_HOST,
    remote_path: str = DEFAULT_REMOTE_PATH,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT,
) -> bytes:
    ps_code = build_powershell_script(remote_path)
    encoded_cmd = base64.b64encode(ps_code.encode("utf-16le")).decode()
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        ssh_host,
        f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded_cmd}"
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=process_timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"SSH PowerShell process timed out after {process_timeout}s") from exc

    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"SSH PowerShell process failed (exit code {proc.returncode}): {err_msg.strip()}")
    if not proc.stdout:
        err_msg = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"No stream received from Windows: {err_msg.strip()}")
    return proc.stdout


def extract_and_validate_archive(raw_zip_bytes: bytes) -> tuple[dict[str, tuple[str, bytes, str]], str]:
    """Validates zip, unpacks and verifies all JSON contents against remote manifest.

    Returns:
        files: dict mapping normalized_path -> (original_rel_path, content_bytes, file_sha256)
        content_hash: SHA256 of sorted normalized file list and file hashes
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_zip_bytes))
    except Exception as exc:
        raise ValueError("Corrupted or invalid zip stream received") from exc

    names = set(archive.namelist())
    if MANIFEST_ENTRY_NAME not in names:
        raise ValueError(f"Missing mandatory remote manifest: {MANIFEST_ENTRY_NAME}")

    try:
        remote_manifest_raw = archive.read(MANIFEST_ENTRY_NAME)
        remote_manifest = json.loads(remote_manifest_raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Invalid remote manifest in zip: {exc}") from exc

    if not isinstance(remote_manifest, dict) or not remote_manifest:
        raise ValueError("Remote manifest is empty or not a valid dictionary")

    results = {}
    normalized_seen = {}
    seen_in_archive = set()

    for info in archive.infolist():
        if info.filename == MANIFEST_ENTRY_NAME:
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"Archive entry is a symlink: {info.filename}")
        if info.is_dir():
            continue

        original_rel = info.filename.replace("\\", "/")
        seen_in_archive.add(original_rel)

        if original_rel not in remote_manifest:
            raise ValueError(f"File in archive but not in remote manifest: {original_rel}")

        data = archive.read(info)
        f_hash = hashlib.sha256(data).hexdigest()

        if f_hash.lower() != remote_manifest[original_rel].lower():
            raise ValueError(
                f"Hash mismatch for {original_rel}: manifest={remote_manifest[original_rel]}, computed={f_hash}"
            )

        norm_path = normalize_kr_rel_path(original_rel)
        norm_cf = norm_path.casefold()
        if norm_cf in normalized_seen:
            raise ValueError(f"Duplicate normalized path: {norm_path} (from {original_rel} and {normalized_seen[norm_cf]})")
        normalized_seen[norm_cf] = original_rel

        try:
            json.loads(data.decode("utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"Invalid JSON in file {original_rel}: {exc}") from exc

        results[norm_path] = (original_rel, data, f_hash)

    manifest_diff = set(remote_manifest.keys()) - seen_in_archive
    if manifest_diff:
        raise ValueError(f"Files listed in remote manifest missing from archive: {sorted(list(manifest_diff))[:5]}")

    if not results:
        raise ValueError("Empty snapshot received: no files found")

    hasher = hashlib.sha256()
    for norm_path in sorted(results.keys()):
        _, _, f_hash = results[norm_path]
        hasher.update(f"{norm_path}:{f_hash}\n".encode("utf-8"))
    content_hash = hasher.hexdigest()

    return results, content_hash


def capture_windows_source(
    output_dir: str | Path,
    ssh_host: str = DEFAULT_SSH_HOST,
    remote_path: str = DEFAULT_REMOTE_PATH,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT,
    zip_bytes: bytes | None = None,
) -> dict:
    out = Path(output_dir).resolve()
    if out.exists():
        raise ValueError(f"Output directory already exists: {out}")

    captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if zip_bytes is None:
        raw_stream = capture_windows_kr_stream(
            ssh_host=ssh_host,
            remote_path=remote_path,
            connect_timeout=connect_timeout,
            process_timeout=process_timeout,
        )
    else:
        raw_stream = zip_bytes

    files, content_hash = extract_and_validate_archive(raw_stream)

    manifest_files = {}
    for norm_path, (orig_rel, _, f_sha) in files.items():
        manifest_files[norm_path] = {
            "original_path": orig_rel,
            "sha256": f_sha,
        }

    provenance = {
        "source_kind": "windows_ssh",
        "raw_version": f"windows-sha256:{content_hash}",
        "source_hash": content_hash,
        "remote_host": ssh_host,
        "remote_path": remote_path,
        "captured_at": captured_at,
        "total_files": len(files),
        "files": manifest_files,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".win-source-", dir=out.parent) as temp:
        temp_dir = Path(temp) / "stage"
        temp_dir.mkdir()
        kr_dir = temp_dir / "KR"
        kr_dir.mkdir(parents=True)
        for norm_path, (_, data, _) in files.items():
            dest = kr_dir / norm_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        prov_file = temp_dir / "provenance.json"
        prov_file.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        temp_dir.rename(out)

    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Path to output directory (e.g. <snapshot>/source)")
    parser.add_argument("--host", default=DEFAULT_SSH_HOST, help="SSH host alias or address")
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH, help="Path on Windows to KR folder")
    parser.add_argument("--timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT, help="SSH connect timeout")
    parser.add_argument("--process-timeout", type=int, default=DEFAULT_PROCESS_TIMEOUT, help="Process execution timeout")
    args = parser.parse_args()

    prov = capture_windows_source(
        output_dir=args.output,
        ssh_host=args.host,
        remote_path=args.remote_path,
        connect_timeout=args.timeout,
        process_timeout=args.process_timeout,
    )
    summary = {
        "source_kind": prov["source_kind"],
        "raw_version": prov["raw_version"],
        "source_hash": prov["source_hash"],
        "total_files": prov["total_files"],
        "captured_at": prov["captured_at"],
        "output": str(Path(args.output).resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
