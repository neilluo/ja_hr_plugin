#!/usr/bin/env python3
"""简历入库：扫描目录 → 解析 → 查重(手机号/附件MD5) → 附件上传 → 写「简历库管理」→ 按手机号回读。

用法:
    python3 scripts/upload_resumes.py <目录> [--dry-run]
示例:
    python3 scripts/upload_resumes.py /path/to/AI简历

输出 JSON 报告：created / skipped_dup / needs_ocr / failed / readback_missing。
needs_ocr 的文件（扫描件/图片）不入库，由 agent 用视觉读取后补录（见 skills/resume-intake）。
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from extract import extract                      # noqa: E402
from notable import Notable, NotableError       # noqa: E402
from parse_resume import parse                  # noqa: E402

EXTS = (".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg")


def main():
    ap = argparse.ArgumentParser(description="简历批量入库")
    ap.add_argument("dir", help="简历目录")
    ap.add_argument("--dry-run", action="store_true", help="只解析不上传不写表")
    args = ap.parse_args()

    nt = Notable()
    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(EXTS))
    if args.dry_run:
        phones, md5s = set(), set()
    else:
        old = nt.list_records("resume", biz_fields=["phone", "attach_md5"])
        phones = {r["fields"].get("phone") for r in old}
        md5s = {r["fields"].get("attach_md5") for r in old}

    rows, report = [], {"total": len(files), "parsed": 0, "skipped_dup": [],
                        "needs_ocr": [], "failed": []}
    for fn in files:
        path = os.path.join(args.dir, fn)
        try:
            with open(path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
            if digest in md5s:
                report["skipped_dup"].append({"file": fn, "reason": "md5"})
                continue
            ex = extract(path)
            if ex["needs_ocr"] or not ex["text"].strip():
                report["needs_ocr"].append(fn)
                continue
            c = parse(ex["text"], fn)
            if not c["phone"] and not c["email"]:
                report["needs_ocr"].append(fn)  # 扫描件特征：抽不出任何联系方式
                continue
            if c["phone"] and c["phone"] in phones:
                report["skipped_dup"].append({"file": fn, "reason": "phone", "phone": c["phone"]})
                continue
            row = {k: v for k, v in c.items() if k not in ("full_text", "certificates")}
            row["certificates"] = "、".join(c["certificates"])
            row["full_text"] = ex["text"][:5000]
            row["upload_time"] = int(time.time() * 1000)
            row["comm_status"] = "待筛选"
            row["attach_md5"] = digest
            row["_file"] = path
            rows.append(row)
            if c["phone"]:
                phones.add(c["phone"])
            md5s.add(digest)
            report["parsed"] += 1
        except Exception as e:  # noqa: BLE001 单文件失败不阻断批次
            report["failed"].append({"file": fn, "error": str(e)[:200]})

    if args.dry_run:
        report["rows"] = [{k: v for k, v in r.items() if k != "_file"} for r in rows]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # 附件先行（5 并发）：任一附件上传失败则该条不写表（不留无附件记录）
    paths = [row.pop("_file") for row in rows]
    cells, errs = nt.map_parallel(nt.upload_attachment, paths)
    write_rows, attach_fail = [], []
    for row, cell in zip(rows, cells):
        if cell is not None:
            row["attachment"] = cell
            write_rows.append(row)
    for i, e in errs:
        attach_fail.append({"file": os.path.basename(paths[i]), "error": str(e)[:200]})
    report["failed"] += attach_fail

    try:
        ids = nt.create_records("resume", write_rows) if write_rows else []
    except NotableError as e:
        print(json.dumps({**report, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    back = {r["fields"].get("phone") for r in
            nt.list_records("resume", biz_fields=["phone"])}
    report["created"] = len(ids)
    report["readback_missing"] = [r["phone"] for r in write_rows
                                  if r.get("phone") and r["phone"] not in back]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1 if report["readback_missing"] else 0)


if __name__ == "__main__":
    main()
