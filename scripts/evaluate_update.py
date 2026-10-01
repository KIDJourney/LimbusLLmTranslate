import json
import sys
import os
import subprocess
import shutil
from pathlib import Path

def main():
    root_dir = Path(__file__).resolve().parent.parent
    try:
        with open(root_dir / ".workflow/evidence/update_status.json", "r") as f:
            status = json.load(f)
    except Exception as e:
        print(f"Failed to load update_status.json: {e}", file=sys.stderr)
        sys.exit(2) # Network/load error -> failed workflow
        
    kr_cdn = status["kr_cdn_version"]
    llc_release = status["llc_release_version"]
    
    snapshot_dir = ".workflow/evidence/snapshot_3238f924.json"
    snapshot_path = root_dir / snapshot_dir
    
    try:
        with open(snapshot_path / "provenance.json", "r") as f:
            prov = json.load(f)
            target_kr = prov.get("raw_version")
            target_llc = prov.get("release")
    except Exception as e:
        print(f"Failed to read provenance: {e}", file=sys.stderr)
        sys.exit(2)

    # assert status两版本与prov对应
    if kr_cdn != target_kr:
        print(f"kr_cdn {kr_cdn} != target_kr {target_kr}")
        sys.exit(2)
    if llc_release != target_llc:
        print(f"llc_release {llc_release} != target_llc {target_llc}")
        sys.exit(2)

    diff_path = root_dir / ".workflow/evidence/current_diff.json"
    if diff_path.exists():
        with open(diff_path, "r") as f:
            diff_data = json.load(f)

        # assert Path(diff.source).resolve==snapshot/KR与chinese==snapshot/LLC_zh-CN
        diff_source = Path(diff_data.get("source")).resolve()
        diff_chinese = Path(diff_data.get("chinese")).resolve()
        expected_source = (snapshot_path / "KR").resolve()
        expected_chinese = (snapshot_path / "LLC_zh-CN").resolve()

        if diff_source != expected_source:
            print(f"diff_source {diff_source} != expected_source {expected_source}")
            sys.exit(2)
        if diff_chinese != expected_chinese:
            print(f"diff_chinese {diff_chinese} != expected_chinese {expected_chinese}")
            sys.exit(2)

        # input.json与diff.pending以(file,json.dumps(path),source)去重集合相等且无重复
        try:
            with open(root_dir / "data/agent-work/release-2026092802/input.json", "r") as f:
                input_data = json.load(f)
        except Exception as e:
            print(f"Failed to read input.json: {e}")
            sys.exit(2)

        diff_pending = diff_data.get("pending", [])
        
        def extract_set(items):
            result = []
            for item in items:
                file_val = item.get("file")
                path_val = json.dumps(item.get("path"))
                source_val = item.get("source")
                result.append((file_val, path_val, source_val))
            return result
            
        input_set = extract_set(input_data)
        diff_set = extract_set(diff_pending)

        # check for duplicates
        if len(input_set) != len(set(input_set)):
            print("Duplicates found in input.json")
            sys.exit(2)
        if len(diff_set) != len(set(diff_set)):
            print("Duplicates found in diff.pending")
            sys.exit(2)

        if set(input_set) != set(diff_set):
            print("input.json and diff.pending do not match")
            sys.exit(2)

        # summary.pending_fields等于实际len且errors为空
        summary = diff_data.get("summary", {})
        pending_fields = summary.get("pending_fields", 0)
        
        if pending_fields != len(diff_pending):
            print(f"summary pending_fields {pending_fields} != len(diff_pending) {len(diff_pending)}")
            sys.exit(2)
            
        if summary.get("parse_errors", 0) != 0:
            print(f"summary parse_errors != 0")
            sys.exit(2)

        # 若pending0本次验收应exit2不是模拟no-update
        if pending_fields == 0:
            print("No pending fields for fixed evaluation.")
            sys.exit(2)
            
        with open(root_dir / ".workflow/evidence/translation_task.json", "w") as f:
            json.dump({
                "kr_cdn_version": kr_cdn,
                "llc_release_version": llc_release,
                "items_to_translate": pending_fields,
                "diff_summary": summary,
                "snapshot_dir": snapshot_dir
            }, f, indent=2)

        print(f"Update needed and verified ({pending_fields} items).")
        sys.exit(0) # passed -> validate_review
    else:
        print("Error: expected diff missing.")
        sys.exit(2)

if __name__ == "__main__":
    main()
