#!/usr/bin/env python3
"""入库类脚本的报告输出：先打一行 VERDICT 结论、再打结果字段前置的 JSON。

存在理由（AGENTS.md「文档约束可靠性远低于代码保障」）：agent 曾扫 JSON 开头就误判
"报告缺 created 字段"（实为排在 timing_ms 之后被看漏），白白多绕两回合。凡脚本能直接
消除的歧义一律下沉到代码，不赌 agent 自觉读全。两个保障：
  1) stdout 第一行是 VERDICT，用 OK/WARN/BAD 直给结论，agent 不可能扫漏；
  2) JSON 里 created/readback_missing/table_total/refine_queued 排在最前，timing_ms 垫底。
本模块是「结论」与「字段展示顺序」的唯一真源（不变量 10）：消费方禁止再各自抄一份。
"""

import json

# 结果字段展示顺序：判成功/失败的字段在最前，性能诊断字段 timing_ms 垫底。
# 未列出的键按原插入顺序追加在 error 之后、timing_ms 之前，保证不丢字段。
_KEY_ORDER = [
    "created", "readback_missing", "table_total", "refine_queued", "refine_fire_at",
    "total", "parsed", "skipped_dup", "needs_ocr", "backfill_stripped", "failed",
    "duplicates_removed",
]
_TAIL_KEYS = ["error", "timing_ms"]


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


def print_report(report):
    """打印 VERDICT 行 + 结果前置的 JSON。stdout 首行即结论，杜绝扫漏。"""
    print(verdict(report))
    print(json.dumps(_ordered(report), ensure_ascii=False, indent=2))
