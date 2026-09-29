#!/usr/bin/env python3
"""入库类脚本的报告输出：先打一行 VERDICT 结论、再打结果字段前置的 JSON。

存在理由（AGENTS.md「文档约束可靠性远低于代码保障」）：agent 曾扫 JSON 开头就误判
"报告缺 created 字段"（实为排在 timing_ms 之后被看漏），白白多绕两回合。凡脚本能直接
消除的歧义一律下沉到代码，不赌 agent 自觉读全。三个保障：
  1) stdout 第一行是 VERDICT，用 OK/WARN/BAD 直给结论，agent 不可能扫漏；
  2) JSON 里 user_line（一句话中文结论，agent 原样复述）与 next_action（下一步动作）排最前，
     created/readback_missing/table_total/refine_queued/cron_job 次之，timing_ms 垫底；
  3) 汇报措辞、精析延迟、兜底时刻均由代码从唯一真源派生，agent 不再自行组织长文（曾一次 6 段
     900 字汇报吃掉 15s 模型思考，用户只要一句"任务完成"）。
本模块是「结论」「字段展示顺序」「用户一句话」「下一步指令」的唯一真源（不变量 10）：消费方禁止再各自抄一份。
"""

import json
import time

import refine_loop   # 同目录 shared/：延迟与兜底时刻的人话表述由其唯一真源派生，禁止此处手抄


class Chrono:
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

# 结果字段展示顺序：给 agent 的行动指令与判成败字段在最前，性能诊断字段 timing_ms 垫底。
# 未列出的键按原插入顺序追加在 error 之后、timing_ms 之前，保证不丢字段。
_KEY_ORDER = [
    "user_line", "next_action",
    "created", "readback_missing", "table_total", "refine_queued", "cron_job",
    "created_summary",
    "total", "parsed", "skipped_dup", "needs_ocr", "backfill_stripped", "failed",
    "duplicates_removed",
]
_TAIL_KEYS = ["error", "timing_ms"]

# 入库成功记录的回带条数上限：够 agent 逐人复述给用户，又不让 31 条批量把报告撑成千字。
# 要看全表走 candidate-query，不靠回带（回带的目的是消灭"再跑一次 query.py 复核"那一个回合）。
SUMMARY_MAX = 5

# 入库成功记录回带的字段（唯一真源）：够 agent 逐人复述给用户，不含 full_text/attachment 等大字段。
SUMMARY_FIELDS = ("name", "phone", "expected_position", "school", "major",
                  "education", "years_experience", "job_name", "department")


def verdict(report):
    """从报告派生一行结论（不硬编码阈值，只看计数与 error 字段）。

    BAD  = 有 error 或 readback_missing 非空（脚本据此 exit 1）；
    WARN = 无上述但有 failed/needs_ocr（部分文件未入库，需人工看）；
    OK   = 干净。
    """
    missing = report.get("readback_missing") or []
    failed = report.get("failed") or []
    needs_ocr = report.get("needs_ocr") or []
    if report.get("error") or missing:
        level = "BAD"
    elif failed or needs_ocr:
        level = "WARN"
    else:
        level = "OK"
    parts = ["VERDICT:%s" % level,
             "created=%s" % report.get("created", 0),
             "readback_missing=%d" % len(missing),
             "failed=%d" % len(failed),
             "needs_ocr=%d" % len(needs_ocr),
             "skipped_dup=%d" % len(report.get("skipped_dup") or [])]
    if "table_total" in report:
        parts.append("table_total=%s" % report["table_total"])
    if "refine_queued" in report:
        parts.append("refine_queued=%s" % report["refine_queued"])
    if report.get("error"):
        # error 是自由文本：剥掉花括号，避免污染下游按 {..} 正则抠 JSON 的解析
        parts.append("error=%s" % str(report["error"])[:80].replace("{", "").replace("}", ""))
    return " ".join(parts)


def _level(report):
    return verdict(report).split(":", 1)[1].split(" ", 1)[0]


def user_line(report):
    """一句话中文结论：agent 交付用户时**原样复述此行**，不再自行组织措辞。
    分钟数与兜底时刻均从 refine_loop 唯一真源派生，禁止在此手抄数字。
    report["kind"] 由入库脚本设定（"简历"/"岗位"），缺省按"记录"。"""
    kind = report.get("kind") or "记录"
    created = report.get("created", 0)
    verb = {"OK": "入库完成", "WARN": "入库部分完成", "BAD": "入库失败"}[_level(report)]
    line = "%s%s：新增 %d 条" % (kind, verb, created)
    if report.get("table_total") is not None:
        line += "，表内共 %s 条" % report["table_total"]
    if report.get("needs_ocr"):
        line += "；%d 份扫描件待视觉补录" % len(report["needs_ocr"])
    if report.get("failed"):
        line += "；%d 份失败（详见 failed）" % len(report["failed"])
    if report.get("skipped_dup"):
        line += "；%d 份重复已跳过" % len(report["skipped_dup"])
    if report.get("readback_missing"):
        line += "；%d 条回读缺失，重跑同目录即可恢复" % len(report["readback_missing"])
    if report.get("refine_queued"):
        line += ("。精析已入队 %d 条，约 %s内后台自动精析，异常时最迟次日 %s 兜底"
                 % (report["refine_queued"], refine_loop.every_human(), refine_loop.fallback_hhmm()))
    return line + "。"


def next_action(report):
    """agent 的下一步指令（机器产出，非建议）：把"注册消费任务 + 一句话回复"压成
    一个回合的确定性动作，杜绝手写 payload、再跑 query.py 复核、再跑 date 算偏移。"""
    steps = []
    if report.get("cron_job"):
        steps.append("把报告里的 cron_job **原样**传给定时任务工具注册（every 型精析消费任务），"
                     "禁止改写其中的 name/schedule/message")
    if report.get("needs_ocr"):
        steps.append("按 SKILL.md 用视觉读 needs_ocr 里的原件、只抽基础字段，"
                     "用 `--backfill -` heredoc 一条命令补录（补录报告会带自己的 cron_job，同样原样注册）")
    if report.get("readback_missing"):
        steps.append("重跑同一目录（幂等）即可恢复，禁止自行调 API 修数据")
    steps.append("用 user_line 一句话回复用户即结束：不要再跑 query.py 复核、不要跑 date 算时刻、"
                 "不要手写 payload、不要展开 created/timing_ms 等字段")
    return "；".join("%d) %s" % (i + 1, s) for i, s in enumerate(steps))


def summarize(rows, limit=SUMMARY_MAX):
    """从写表行里摘出可复述给用户的少量字段，最多 limit 条 + 溢出计数。
    目的：用户问"传进去的是谁"时报告里就有答案，不必再跑一次 query.py（省一个回合）。"""
    out = []
    for r in rows[:limit]:
        item = {k: r[k] for k in SUMMARY_FIELDS if r.get(k) not in (None, "", [])}
        if item:
            out.append(item)
    if len(rows) > limit:
        out.append({"_more": len(rows) - limit, "_hint": "其余记录用 candidate-query 查看"})
    return out


def enrich(report, created_rows=None):
    """给报告补齐派生的展示字段：user_line / next_action（+ 可选 created_summary）。
    入库脚本在 print_report 前调用一次，把"该说什么、下一步做什么"全部下沉到代码。"""
    if created_rows is not None:
        report["created_summary"] = summarize(created_rows)
    report["user_line"] = user_line(report)
    report["next_action"] = next_action(report)
    return report


def _ordered(report):
    listed = set(_KEY_ORDER) | set(_TAIL_KEYS)
    out = {k: report[k] for k in _KEY_ORDER if k in report}
    for k, v in report.items():           # 未列出的键保持原相对顺序
        if k not in listed:
            out[k] = v
    for k in _TAIL_KEYS:                   # error / timing_ms 固定垫底
        if k in report:
            out[k] = report[k]
    return out


def print_report(report, created_rows=None):
    """打印 VERDICT 行 + 结果前置的 JSON。stdout 首行即结论，杜绝扫漏。
    自动补 user_line / next_action（created_rows 非空时一并回带 created_summary），
    保证任何调用点都带一句话结论与下一步动作。"""
    if "user_line" not in report or created_rows is not None:
        enrich(report, created_rows)
    print(verdict(report))
    print(json.dumps(_ordered(report), ensure_ascii=False, indent=2))
