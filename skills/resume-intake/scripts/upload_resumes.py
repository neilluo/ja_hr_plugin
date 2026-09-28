#!/usr/bin/env python3
"""简历入库：扫描目录 → 解析 → 查重(手机号/附件MD5) → 附件上传 → 写「简历库管理」→ 按手机号回读。

两种模式:
    # A. 批量入库（可解析的 pdf/docx/doc；扫描件/图片进 needs_ocr 队列）
    python3 skills/resume-intake/scripts/upload_resumes.py <目录> [--dry-run]
    # <目录> 也可直接传单个简历文件路径：只处理这一份，无需建临时目录/软链

    # B. 扫描件补录（agent 用视觉读取 needs_ocr 文件后，把字段+原文件路径写成 JSON 交给本命令）
    python3 skills/resume-intake/scripts/upload_resumes.py --backfill records.json
    # records.json = [{"name":"张三","phone":"138...","_file":"/abs/扫描件.pdf", ...}, ...]
    # --backfill - 从 stdin 读 payload：agent 可一条命令 heredoc 写完即跑，省一次回合往返。

两种模式共用同一套尾部流程：字段校验 → 附件先传（失败则该条不写表）→ 写表 → 按手机号回读。
补录与批量走完全一致的不变量，扫描件的原件也会进表、MD5 也写入，重跑幂等。

输出报告（格式唯一真源 shared/report.py）：stdout 首行 VERDICT:OK|WARN|BAD 一行结论，
其后 JSON 报告（成品指令与结果字段前置，timing_ms 垫底）。报告是**给 agent 的成品**，照做即可：
  user_line       = 交付用户的一句话原话（措辞与"约 X 分钟/次日 HH:MM 兜底"由 report.py 从
                    refine_loop 真源派生），agent 原样复述即结束，禁止自行组织长汇报；
  next_action     = 下一步动作清单（机器产出），照它做；
  cron_job        = 精析消费任务的完整注册规格（name/schedule.at/payload/contextDirs 全备好，
                    refine_loop.consume_task_spec 产出）：仅 refine_queued>0 时输出，agent **原样透传**
                    给定时任务工具，禁止手写字段、禁止再跑 date 算时刻（延迟真源 = REFINE_DELAY_S）；
  created_summary = 本次入库记录关键字段回带（最多 SUMMARY_MAX 条）：用户问"传进去的是谁"直接引用，
                    禁止再跑 query.py 复核（readback_missing 为空即已逐手机号回读，不变量 4）；
  created / skipped_dup / needs_ocr / failed / readback_missing / table_total / refine_queued / timing_ms。
timing_ms 为各阶段机器耗时（毫秒）：list_existing/extract(批量)/build_rows/attach/create/readback/total，
用于定位脚本侧瓶颈（附件与建记录是主要网络段）。精析是异步队列：上传写完表即结束，refine_queued = 当前待精析队列长度
（谓词唯一真源 shared/refine_loop.py），由后台周期消费，上传环节不衔接精析。
table_total = 表内记录总数（回读那趟全表扫描顺带得出，零额外请求）。
补录（--backfill）只写基础字段与 source_file（原件本地路径），不写 AI 三列、不打 ai_refined_at：
扫描件与批量记录一样进精析队列，三列由后台 subagent 读原件产出（谓词与读图机制见 shared/refine_loop.py）。
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared", "preflight"))
from extract import extract, SUPPORTED_EXTS     # noqa: E402  扩展名清单唯一源在 shared/extract.py
from notable import Notable, NotableError       # noqa: E402
from parse_resume import parse, derive      # noqa: E402  parse=批量、derive=补录派生，同一批函数
from preflight import run_preflight             # noqa: E402
import refine_loop                              # noqa: E402  队列谓词唯一真源，禁止本地抄副本
from report import print_report                 # noqa: E402  报告输出唯一真源（VERDICT 行 + 结果字段前置）

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")

FULL_TEXT_MAX = 20000  # 全文参考字段截断上限（打分用 skills，不依赖此字段），唯一源
# 新入库默认沟通状态；合法枚举清单见 config.options.resume.comm_status
COMM_STATUS_DEFAULT = "待筛选"
# 补录通道强制剥离的"精析产出列"：AI 三列 + 出队标记。这些列只由后台精析流水线写
# （谓词真源 shared/refine_loop.py：ai_refined_at 非空即出队）。payload 由 agent 手填，
# 若允许带入 ai_refined_at，扫描件会静默永不出队、三列手析烂也没人再管；带入三列则当场
# 手析、与后台重复劳动（曾实测单份 51s）。docstring 口头禁过，但代码没拦——现由代码兜底。
BACKFILL_STRIP = frozenset(("skills", "ai_extract", "ai_deep", "ai_refined_at"))
UPLOAD_WORKERS = 10  # 附件上传/文本解析并发；notable.map_parallel 默认 5 对 30+ 文件偏保守


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


class _Chrono:
    """阶段计时器：mark(name) 记录自上次 mark 起的毫秒增量并按 name 累加。

    报告 timing_ms 字段唯一生产者。分段名在批量/补录两模式间保持一致以便横向比对：
    list_existing / extract(仅批量) / build_rows / attach / create / readback / total。
    纯本地 monotonic 计时，不触网、不改变任何业务行为。"""

    def __init__(self):
        self._start = self._last = time.monotonic()
        self.segs = {}

    def mark(self, name):
        now = time.monotonic()
        self.segs[name] = self.segs.get(name, 0) + round((now - self._last) * 1000)
        self._last = now

    def finish(self):
        # total = 自构造起墙钟耗时（非末段残差）；各分段之和 ≈ total（含未打标间隙）
        self.segs["total"] = round((time.monotonic() - self._start) * 1000)
        return self.segs


def _validate(nt, table, row):
    """返回 (非法业务键列表, 可用业务键提示)。内部键（下划线开头）不计入非法。"""
    valid = set(nt.cfg["fields"][table])
    bad = [k for k in row if not k.startswith("_") and k not in valid]
    return bad, ", ".join(sorted(valid))


def _dedupe_selfheal(nt, table, key_of):
    """写后查重自愈：按业务键聚合，>1 副本保留最早一条、删其余。
    治愈重试双写与历史残留；返回 (删除条数, 自愈后表内总条数)。
    总数直接取自本次全表扫描，零额外请求，供报告 table_total 使用。key_of(fields) -> 键或 None。"""
    back = nt.list_records(table, biz_fields=["phone", "attach_md5"])
    groups = {}
    for r in back:
        k = key_of(r["fields"])
        if k:
            groups.setdefault(k, []).append(r["id"])
    dup_ids = [rid for ids in groups.values() if len(ids) > 1 for rid in ids[1:]]
    if dup_ids:
        nt.delete_records(table, dup_ids)
    return len(dup_ids), len(back) - len(dup_ids)


def _finalize(nt, table, rows, report, chrono=None):
    """批量与补录共用尾部：附件先传 → 写表 → 写后查重自愈 → 按手机号回读 → 队列计数。
    rows 含 _file/attach_md5。chrono 非空时按 attach/create/readback 分段计时并写入 report。
    批量与补录一视同仁：两者新记录都入精析队列（扫描件靠 source_file 入队、由 subagent 读图），
    故队列非空时都输出 refine_fire_at / cron_job 供 agent 注册消费任务（trigger 唯一真源）。"""
    if not rows:
        try:
            report["duplicates_removed"], report["table_total"] = _dedupe_selfheal(
                nt, table, lambda f: f.get("phone") or f.get("attach_md5"))
            refine_loop.trigger(nt, "resume", report)
        except NotableError as e:

            if chrono:
                report["timing_ms"] = chrono.finish()
            print_report({**report, "error": "查重失败: %s" % e})
            sys.exit(1)
        if chrono:
            chrono.mark("readback")
            report["timing_ms"] = chrono.finish()
        report["created"] = 0
        report["readback_missing"] = []
        print_report(report)
        sys.exit(0)

    # 附件先行（UPLOAD_WORKERS 并发）：任一附件上传失败则该条不写表（不留无附件记录）
    paths = [row.pop("_file") for row in rows]
    cells, errs = nt.map_parallel(nt.upload_attachment, paths, workers=UPLOAD_WORKERS)
    if chrono:
        chrono.mark("attach")
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
        if chrono:
            report["timing_ms"] = chrono.finish()
        print_report({**report, "error": str(e)})
        sys.exit(1)
    if chrono:
        chrono.mark("create")

    try:
        report["duplicates_removed"], report["table_total"] = _dedupe_selfheal(
            nt, table, lambda f: f.get("phone") or f.get("attach_md5"))
        back = {r["fields"].get("phone") for r in nt.list_records(table, biz_fields=["phone"])}
        # 写表回读完成后计队列（谓词与注册规格唯一真源 refine_loop.trigger；dry-run 不走 _finalize 天然不含）
        refine_loop.trigger(nt, "resume", report)
    except NotableError as e:
        if chrono:
            report["timing_ms"] = chrono.finish()
        print_report({**report, "created": len(ids),
                      "error": "回读/查重失败: %s" % e})
        sys.exit(1)
    if chrono:
        chrono.mark("readback")
        report["timing_ms"] = chrono.finish()
    report["created"] = len(ids)
    report["readback_missing"] = [r["phone"] for r in write_rows
                                  if r.get("phone") and r["phone"] not in back]
    # created_summary：把入库记录的关键字段回带进报告，agent 无需再跑 query.py 复核（省一个回合）
    print_report(report, created_rows=write_rows)
    sys.exit(1 if report["readback_missing"] else 0)


def _scan(dir_or_file):
    """返回 (base_dir, files)：files 是 base_dir 下的文件名列表。
    dir_or_file 既可是目录（扫全部支持格式），也可是单个文件（只处理它）——
    单份上传是常见诉求，此前只能整目录跑或让 agent 在 /tmp 建软链绕路（多花 2 个回合）。"""
    if os.path.isfile(dir_or_file):
        return os.path.dirname(os.path.abspath(dir_or_file)), [os.path.basename(dir_or_file)]
    files = sorted(f for f in os.listdir(dir_or_file)
                   if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS)
    return dir_or_file, files


def run_batch(nt, args):
    chrono = _Chrono()
    base_dir, files = _scan(args.dir)
    if args.dry_run:
        phones, md5s = set(), set()
    else:
        old = nt.list_records("resume", biz_fields=["phone", "attach_md5"])
        phones = {r["fields"].get("phone") for r in old}
        md5s = {r["fields"].get("attach_md5") for r in old}
        chrono.mark("list_existing")

    # 阶段1：并行提取文本（extract 走 subprocess pdftotext，I/O 密集，线程可提速）
    paths = [os.path.join(base_dir, fn) for fn in files]
    extracts, _errs = nt.map_parallel(extract, paths, workers=UPLOAD_WORKERS)
    chrono.mark("extract")

    rows = []
    report = {"kind": "简历", "total": len(files), "parsed": 0, "skipped_dup": [],
              "needs_ocr": [], "failed": []}
    for fn, path, ex in zip(files, paths, extracts):
        try:
            # 提取失败(ex=None/带error)不特殊处理：text 为空自然落入下方 needs_ocr 判定
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
            row = {k: v for k, v in c.items() if k != "certificates"}
            row["certificates"] = "、".join(c["certificates"])
            row["full_text"] = ex["text"][:FULL_TEXT_MAX]
            row["upload_time"] = int(time.time() * 1000)
            row["comm_status"] = COMM_STATUS_DEFAULT
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
        chrono.mark("build_rows")
        report["timing_ms"] = chrono.finish()  # dry-run 仅含 extract/build_rows/total（不触网写表）
        print_report(report)
        return

    chrono.mark("build_rows")
    _finalize(nt, "resume", rows, report, chrono)


def run_backfill(nt, args):
    """扫描件补录：读 records.json（字段 + _file 原文件路径），走与批量一致的附件/写表/回读。
    payload 路径为 "-" 时从 stdin 读——agent 可 heredoc 一条命令写完即跑，省一次回合往返。"""
    chrono = _Chrono()
    if args.backfill == "-":
        records = json.load(sys.stdin)
    else:
        with open(args.backfill, encoding="utf-8") as f:
            records = json.load(f)
    if isinstance(records, dict):
        records = [records]

    old = nt.list_records("resume", biz_fields=["phone", "attach_md5"])
    phones = {r["fields"].get("phone") for r in old}
    md5s = {r["fields"].get("attach_md5") for r in old}
    chrono.mark("list_existing")

    report = {"kind": "简历", "total": len(records), "parsed": 0, "skipped_dup": [],
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
            row = {k: v for k, v in rec.items()
                   if k not in ("_file", "file", "full_text") and k not in BACKFILL_STRIP}
            # 精析产出列若被 payload 带入，剥离并在报告里点名（不静默丢，否则 agent 以为写进去了）
            dropped = sorted(k for k in rec if k in BACKFILL_STRIP)
            if dropped:
                report.setdefault("backfill_stripped", []).append(
                    {"file": name, "stripped": dropped,
                     "reason": "AI 三列与出队标记只由后台精析写，补录带入已剥离"})
            if isinstance(row.get("certificates"), list):
                row["certificates"] = "、".join(row["certificates"])
            # 派生列补齐（与批量 parse() 同一批函数，见 parse_resume.derive）：agent 只填事实，
            # category/school_rank/expected_location 由脚本算——曾漏这一步导致 3 份扫描件两列静默留空。
            row = derive(row)
            row["upload_time"] = int(time.time() * 1000)
            row.setdefault("comm_status", COMM_STATUS_DEFAULT)
            row["attach_md5"] = digest
            # 扫描件无 full_text，靠原件本地路径入精析队列（谓词真源 refine_loop）：
            # subagent 直接读本地原件出图精析三列，agent 补录时不再手析、不打 ai_refined_at。
            # 表内附件 url 是 OSS 签名链（约 2 小时过期、无换签接口），故必须存本地路径而非靠 url。
            row["source_file"] = os.path.abspath(path)
            row["_file"] = path
            rows.append(row)
            if rec.get("phone"):
                phones.add(rec["phone"])
            md5s.add(digest)
            report["parsed"] += 1
        except Exception as e:  # noqa: BLE001
            report["failed"].append({"file": name, "error": str(e)[:200]})

    chrono.mark("build_rows")
    _finalize(nt, "resume", rows, report, chrono)


def main():
    ap = argparse.ArgumentParser(description="简历批量入库 / 扫描件补录")
    ap.add_argument("dir", nargs="?", help="简历目录（批量模式）；也可直接传单个简历文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只解析不上传不写表（批量模式）")
    ap.add_argument("--backfill", metavar="JSON",
                    help="扫描件补录：字段+原文件路径的 JSON 文件；传 - 从 stdin 读（heredoc 一条命令跑完）")
    args = ap.parse_args()

    if bool(args.dir) == bool(args.backfill):
        ap.error("二选一：提供 <目录或单个文件> 走批量，或用 --backfill JSON 走补录")
    # --dry-run 只在批量路径生效；补录曾静默忽略它、照传附件建记录（人以为在预览实则已写表）。
    # 补录无 dry-run 语义，直接拒收该组合，不赌调用方知道"这个 flag 会被吞"。
    if args.backfill and args.dry_run:
        ap.error("--dry-run 仅用于批量模式（<目录>）；补录（--backfill）无预演语义，会真实写表")

    # stage 0: 环境预检。单文件走 files= 精确校验，目录走 files_dir= 扫描（两者互斥，别把单文件
    # 当目录传——_check_files_dir 会因目录里只有 1 个文件而误判/漏判）。
    if args.dir and os.path.isfile(args.dir):
        run_preflight(config_path=_CONFIG, files=[args.dir])
    else:
        run_preflight(config_path=_CONFIG, files_dir=args.dir)

    nt = Notable()
    if args.backfill:
        run_backfill(nt, args)
    else:
        run_batch(nt, args)


if __name__ == "__main__":
    main()
