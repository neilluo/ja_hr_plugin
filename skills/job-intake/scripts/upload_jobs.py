#!/usr/bin/env python3
"""岗位 JD 入库：扫描目录 → 解析 → 去重 → 批量写「岗位JD表」→ 回读校验。

用法:
    python3 skills/job-intake/scripts/upload_jobs.py <目录> [--dry-run]
示例:
    python3 skills/job-intake/scripts/upload_jobs.py /path/to/岗位说明书
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared", "preflight"))
from extract import extract                      # noqa: E402
from notable import Notable, NotableError       # noqa: E402
from parse_job import parse                     # noqa: E402
from preflight import run_preflight             # noqa: E402
import refine_loop                              # noqa: E402  队列谓词唯一真源
from report import print_report                 # noqa: E402  报告输出唯一真源（VERDICT 行 + 结果字段前置）

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")

EXTS = (".doc", ".docx", ".pdf")


def job_id_of(dept, name):
    return "J" + hashlib.md5(("%s|%s" % (dept, name)).encode()).hexdigest()[:10].upper()


def main():
    ap = argparse.ArgumentParser(description="岗位 JD 批量入库")
    ap.add_argument("dir", help="岗位说明书目录")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写表")
    args = ap.parse_args()

    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG, files_dir=args.dir)

    nt = Notable()
    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(EXTS))
    existing = {r["fields"].get("job_id") for r in nt.list_records("job", biz_fields=["job_id"])} \
        if not args.dry_run else set()

    rows, report = [], {"kind": "岗位", "total": len(files), "parsed": 0, "skipped_dup": [],
                        "needs_ocr": [], "failed": []}
    for fn in files:
        try:
            ex = extract(os.path.join(args.dir, fn)) or {"text": "", "needs_ocr": False}
            # 岗位侧无 OCR 补录链：图片型/抽不出正文的 JD 若照样入库，会产出 responsibilities 等
            # 全空的记录——created 正常、回读通过、无告警，且空 responsibilities 永不进精析队列
            # （谓词真源 refine_loop），匹配时表现为"这岗没人合适"的坏数据。与简历侧同纪律：
            # 无正文不入库，进 needs_ocr 报出交人工（不变量 5）。
            if ex.get("needs_ocr") or not (ex.get("text") or "").strip():
                report["needs_ocr"].append(fn)
                continue
            jd = parse(ex["text"], fn)
            jid = job_id_of(jd["department"], jd["job_name"])
            if jid in existing:
                report["skipped_dup"].append(fn)
                continue
            existing.add(jid)  # 批内去重：同部门同岗位名只建一条
            rows.append({
                "job_id": jid,
                "job_name": jd["job_name"],
                "department": jd["department"],
                "org": jd["org"],
                "status": jd["status"],
                "work_location": jd["work_location"],
                "responsibilities": jd["responsibilities"],
                "requirements": jd["requirements"],
                "hard_gates": "；".join(jd["hard_gates"]),
                "must_skills": "、".join(jd["must_skills"]),
                "bonus_skills": "、".join(jd["bonus_skills"]),
                "must_weight": jd["must_weight"],
                "bonus_weight": jd["bonus_weight"],
                "submit_time": int(time.time() * 1000),
                "_file": os.path.join(args.dir, fn),
            })
            report["parsed"] += 1
        except Exception as e:  # noqa: BLE001 单文件失败不阻断批次
            report["failed"].append({"file": fn, "error": str(e)[:200]})

    if args.dry_run:
        report["rows"] = [{k: v for k, v in r.items() if k != "_file"} for r in rows]
        print_report(report)
        return

    # JD 附件先传后写（5 并发），与简历同纪律
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
        ids = nt.create_records("job", write_rows) if write_rows else []
    except NotableError as e:
        print_report({**report, "error": str(e)})
        sys.exit(1)

    try:
        # 写后查重自愈：重试双写或历史残留的重复 job_id，保留最早一条删其余
        back_all = nt.list_records("job", biz_fields=["job_id"])
        groups = {}
        for r in back_all:
            jid = r["fields"].get("job_id")
            if jid:
                groups.setdefault(jid, []).append(r["id"])
        dup_ids = [rid for ids in groups.values() if len(ids) > 1 for rid in ids[1:]]
        if dup_ids:
            nt.delete_records("job", dup_ids)
        report["duplicates_removed"] = len(dup_ids)
        back = set(groups)
    except NotableError as e:
        print_report({**report, "created": len(ids),
                      "error": "回读/查重失败: %s" % e})
        sys.exit(1)
    report["created"] = len(ids)
    report["readback_missing"] = [r["job_id"] for r in write_rows if r["job_id"] not in back]
    try:
        # 精析队列计数与注册规格（谓词与规格唯一真源 refine_loop.trigger，与简历链共用同一函数）
        refine_loop.trigger(nt, "job", report)
    except NotableError as e:
        print_report({**report, "error": "精析队列计数失败: %s" % e})
        sys.exit(1)
    print_report(report, created_rows=write_rows)
    sys.exit(1 if report["readback_missing"] else 0)


if __name__ == "__main__":
    main()
