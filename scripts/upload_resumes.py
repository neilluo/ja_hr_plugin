#!/usr/bin/env python3
"""简历入库：扫描目录 → 解析 → 查重(手机号/附件MD5) → 附件上传 → 写「简历库管理」→ 按手机号回读。

两种模式:
    # A. 批量入库（可解析的 pdf/docx/doc；扫描件/图片进 needs_ocr 队列）
    python3 scripts/upload_resumes.py <目录> [--dry-run]

    # B. 扫描件补录（agent 用视觉读取 needs_ocr 文件后，把字段+原文件路径写成 JSON 交给本命令）
    python3 scripts/upload_resumes.py --backfill records.json
    # records.json = [{"name":"张三","phone":"138...","_file":"/abs/扫描件.pdf", ...}, ...]

两种模式共用同一套尾部流程：字段校验 → 附件先传（失败则该条不写表）→ 写表 → 按手机号回读。
补录与批量走完全一致的不变量，扫描件的原件也会进表、MD5 也写入，重跑幂等。

输出 JSON 报告：created / skipped_dup / needs_ocr / failed / readback_missing。
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
from preflight import run_preflight             # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")

EXTS = (".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg")
FULL_TEXT_MAX = 20000  # 全文参考字段截断上限（打分用 skills，不依赖此字段）


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _validate(nt, table, row):
    """返回 (非法业务键列表, 可用业务键提示)。内部键（下划线开头）不计入非法。"""
    valid = set(nt.cfg["fields"][table])
    bad = [k for k in row if not k.startswith("_") and k not in valid]
    return bad, ", ".join(sorted(valid))


def _dedupe_selfheal(nt, table, key_of):
    """写后查重自愈：按业务键聚合，>1 副本保留最早一条、删其余。
    治愈重试双写与历史残留；返回删除条数。key_of(fields) -> 键或 None。"""
    back = nt.list_records(table, biz_fields=["phone", "attach_md5"])
    groups = {}
    for r in back:
        k = key_of(r["fields"])
        if k:
            groups.setdefault(k, []).append(r["id"])
    dup_ids = [rid for ids in groups.values() if len(ids) > 1 for rid in ids[1:]]
    if dup_ids:
        nt.delete_records(table, dup_ids)
    return len(dup_ids)


def _finalize(nt, table, rows, report):
    """批量与补录共用尾部：附件先传 → 写表 → 写后查重自愈 → 按手机号回读。rows 含 _file/attach_md5。"""
    if not rows:
        try:
            report["duplicates_removed"] = _dedupe_selfheal(
                nt, table, lambda f: f.get("phone") or f.get("attach_md5"))
        except NotableError as e:
            print(json.dumps({**report, "error": "查重失败: %s" % e}, ensure_ascii=False))
            sys.exit(1)
        report["created"] = 0
        report["readback_missing"] = []
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 附件先行（5 并发）：任一附件上传失败则该条不写表（不留无附件记录）
    paths = [row.pop("_file") for row in rows]
    cells, errs = nt.map_parallel(nt.upload_attachment, paths)
    write_rows = []
    for row, cell in zip(rows, cells):
        if cell is not None:
            row["attachment"] = cell
            write_rows.append(row)
    for i, e in errs:
        report["failed"].append({"file": os.path.basename(paths[i]), "error": str(e)[:200]})

    try:
        ids = nt.create_records(table, write_rows) if write_rows else []
    except NotableError as e:
        print(json.dumps({**report, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

    try:
        report["duplicates_removed"] = _dedupe_selfheal(
            nt, table, lambda f: f.get("phone") or f.get("attach_md5"))
        back = {r["fields"].get("phone") for r in nt.list_records(table, biz_fields=["phone"])}
    except NotableError as e:
        print(json.dumps({**report, "created": len(ids),
                          "error": "回读/查重失败: %s" % e}, ensure_ascii=False))
        sys.exit(1)
    report["created"] = len(ids)
    report["readback_missing"] = [r["phone"] for r in write_rows
                                  if r.get("phone") and r["phone"] not in back]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1 if report["readback_missing"] else 0)


def run_batch(nt, args):
    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(EXTS))
    if args.dry_run:
        phones, md5s = set(), set()
    else:
        old = nt.list_records("resume", biz_fields=["phone", "attach_md5"])
        phones = {r["fields"].get("phone") for r in old}
        md5s = {r["fields"].get("attach_md5") for r in old}

    # 阶段1：并行提取文本（extract 走 subprocess pdftotext，I/O 密集，线程可提速）
    paths = [os.path.join(args.dir, fn) for fn in files]
    extracts, _errs = nt.map_parallel(extract, paths)

    rows = []
    report = {"total": len(files), "parsed": 0, "skipped_dup": [],
              "needs_ocr": [], "failed": []}
    for fn, path, ex in zip(files, paths, extracts):
        try:
            if ex is None or ex.get("error"):
                # 提取失败但可能有部分文本：仍按下方逻辑判扫描件
                pass
            ex = ex or {"text": "", "needs_ocr": False}
            digest = _md5(path)
            if digest in md5s:
                report["skipped_dup"].append({"file": fn, "reason": "md5"})
                continue
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
            row["full_text"] = ex["text"][:FULL_TEXT_MAX]
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

    _finalize(nt, "resume", rows, report)


def run_backfill(nt, args):
    """扫描件补录：读 records.json（字段 + _file 原文件路径），走与批量一致的附件/写表/回读。"""
    with open(args.backfill, encoding="utf-8") as f:
        records = json.load(f)
    if isinstance(records, dict):
        records = [records]

    old = nt.list_records("resume", biz_fields=["phone", "attach_md5"])
    phones = {r["fields"].get("phone") for r in old}
    md5s = {r["fields"].get("attach_md5") for r in old}

    report = {"total": len(records), "parsed": 0, "skipped_dup": [],
              "needs_ocr": [], "failed": []}
    rows = []
    for rec in records:
        path = rec.get("_file") or rec.get("file")
        name = os.path.basename(path) if path else (rec.get("name") or "?")
        try:
            bad, valid = _validate(nt, "resume", rec)
            if bad:
                report["failed"].append({"file": name,
                                         "error": "非法字段: %s；可用: %s" % (",".join(bad), valid)})
                continue
            if not path or not os.path.exists(path):
                report["failed"].append({"file": name, "error": "缺少有效的 _file 原文件路径"})
                continue
            if not rec.get("phone") and not rec.get("email"):
                report["failed"].append({"file": name, "error": "无手机号且无邮箱，不入库"})
                continue
            digest = _md5(path)
            if digest in md5s:
                report["skipped_dup"].append({"file": name, "reason": "md5"})
                continue
            if rec.get("phone") and rec["phone"] in phones:
                report["skipped_dup"].append({"file": name, "reason": "phone", "phone": rec["phone"]})
                continue
            row = {k: v for k, v in rec.items() if k not in ("_file", "file", "full_text")}
            if isinstance(row.get("certificates"), list):
                row["certificates"] = "、".join(row["certificates"])
            row["upload_time"] = int(time.time() * 1000)
            row.setdefault("comm_status", "待筛选")
            row["attach_md5"] = digest
            row["_file"] = path
            rows.append(row)
            if rec.get("phone"):
                phones.add(rec["phone"])
            md5s.add(digest)
            report["parsed"] += 1
        except Exception as e:  # noqa: BLE001
            report["failed"].append({"file": name, "error": str(e)[:200]})

    _finalize(nt, "resume", rows, report)


def main():
    ap = argparse.ArgumentParser(description="简历批量入库 / 扫描件补录")
    ap.add_argument("dir", nargs="?", help="简历目录（批量模式）")
    ap.add_argument("--dry-run", action="store_true", help="只解析不上传不写表（批量模式）")
    ap.add_argument("--backfill", metavar="JSON", help="扫描件补录：字段+原文件路径的 JSON 文件")
    args = ap.parse_args()

    if bool(args.dir) == bool(args.backfill):
        ap.error("二选一：提供 <目录> 走批量，或用 --backfill JSON 走补录")

    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG, files_dir=args.dir)

    nt = Notable()
    if args.backfill:
        run_backfill(nt, args)
    else:
        run_batch(nt, args)


if __name__ == "__main__":
    main()
