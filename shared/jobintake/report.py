# -*- coding: utf-8 -*-
"""JobReport：intake_job 报告统计判定 + 组装 + 产物落盘 + 兜底产物。

  * report 是 8 键（ok/elapsed_ms/dws_calls/turns_saved_estimate/rows/summary/
    warnings/retry_count）。
  * summary 从 rows 重算；apply 模式 attachment_uploaded/failed 恒为 0。
  * draft 收尾三步顺序：draft["report"] = report → draft["ok"] = … →
    pop("_warnings") 只在 turn1；apply 模式 draft 不落盘。
  * ok = rc == 0 and bool(rows)。
  * tbl.warnings 合并是保序去重（if w not in warnings），不是 set()。
  * 两条兜底报告的键序/文案/ARTIFACT: 行逐字；异常兜底自身失败 → 静默吞掉
    （except Exception: pass）后仍 traceback.print_exc() + return 1。
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from aitable.client import now_iso                  # noqa: E402
from jobintake.constants import NEW_TURNS, OLD_TURNS_PER_FILE
from jobintake.textutil import new_batch_id, write_json
from runtime_compat import default_out_root

__all__ = ["JobReport"]


class JobReport:
    def __init__(self, console: Any, args: Any, counter: Any, t_start: float,
                 mode: str, report_path: Path, draft_path: Path) -> None:
        self.console = console
        self.args = args
        self.counter = counter
        self.t_start = t_start
        self.mode = mode
        self.report_path = report_path
        self.draft_path = draft_path

    @staticmethod
    def merge_table_warnings(warnings: List[str], tbl_warnings: List[str]) -> None:
        """tbl.warnings 保序去重合并（位置在 report 组装之后、落盘之前）。"""
        for w in tbl_warnings:
            if w not in warnings:
                warnings.append(w)

    def finish(self, draft: Dict[str, Any], rows: List[Dict[str, Any]], rc: int) -> int:
        """run() 尾段：summary 重算 → report 组装 → 落盘 → 人读清单 → 退出码。"""
        args = self.args
        mode = self.mode
        summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
                   "attachment_uploaded": 0, "attachment_failed": 0}
        for r in rows:
            k = {"新入库": "new", "已覆盖": "overwrite", "跳过": "skip", "失败": "fail"}.get(r["result"])
            if k:
                summary[k] += 1
        if mode == "turn1":
            # 附件计数在 run_turn1 内部已算过，这里从 draft 的 jobs 里重算，避免丢失
            summary["attachment_uploaded"] = sum(1 for j in draft.get("jobs", [])
                                                 if j.get("attachment_status") == "uploaded")
            summary["attachment_failed"] = sum(1 for j in draft.get("jobs", [])
                                               if j.get("attachment_status") == "failed")

        elapsed_ms = int((time.monotonic() - self.t_start) * 1000)
        n_files = len(args.files or []) if mode == "turn1" else len(rows)
        turns_saved = max(0, n_files * OLD_TURNS_PER_FILE - NEW_TURNS) if n_files else 0
        warnings = list(draft.get("_warnings") or [])
        report = {"ok": rc == 0 and bool(rows), "elapsed_ms": elapsed_ms,
                  "dws_calls": self.counter.calls, "turns_saved_estimate": turns_saved,
                  "rows": rows, "summary": summary, "warnings": warnings,
                  "retry_count": self.counter.retries}
        draft["report"] = report
        # 所有产物统一凭证口径「文件存在 且 ok==true」→ jobs_draft 顶层也带 ok
        draft["ok"] = bool(report.get("ok"))

        write_json(self.report_path, report)
        if mode == "turn1":
            draft.pop("_warnings", None)
            write_json(self.draft_path, draft)

        console = self.console
        console.blank()
        console.result_header()
        for r in rows:
            console.result_row(r)
        console.result_footer()
        console.subtotal(summary)
        console.wall_line(elapsed_ms, self.counter.calls, self.counter.retries, turns_saved)
        console.warnings_block(warnings)
        if mode == "turn1":
            console.draft_line(self.draft_path)
            console.next_step()
        console.artifact(self.report_path)
        return 0 if report["ok"] else 1

    def write_config_failure(self, exc: Exception) -> int:
        """AITable 装配失败：直接写报告 + ARTIFACT: + return 1。"""
        write_json(self.report_path, {
            "ok": False, "elapsed_ms": int((time.monotonic() - self.t_start) * 1000),
            "dws_calls": self.counter.calls, "turns_saved_estimate": 0,
            "rows": [{"seq": 1, "file_name": str(self.args.config), "result": "失败",
                      "reason": "读取 config.json 失败：%s: %s" % (type(exc).__name__, exc)}],
            "summary": {"new": 0, "overwrite": 0, "skip": 0, "fail": 1,
                        "attachment_uploaded": 0, "attachment_failed": 0},
            "warnings": ["config.json 不可用：%s" % exc], "retry_count": 0})
        self.console.artifact(self.report_path)
        return 1

    @staticmethod
    def crash_artifact(args: Any, exc: Exception, console: Any) -> int:
        """main() 异常兜底产物（绝不静默早退）。只依赖 args——Pipeline
        构造失败时也要能走通，故为 staticmethod。"""
        out_dir: Optional[Path] = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
            default_out_root() / (args.batch_id or new_batch_id())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            names = [Path(f).name for f in (args.files or [])] or [str(args.apply or "")]
            write_json(out_dir / "intake_report.json", {
                "ok": False, "elapsed_ms": 0, "dws_calls": 0, "turns_saved_estimate": 0,
                "rows": [{"seq": i + 1, "file_name": n, "result": "失败",
                          "reason": "脚本异常中止：%s: %s" % (type(exc).__name__, exc)}
                         for i, n in enumerate(names)],
                "summary": {"new": 0, "overwrite": 0, "skip": 0, "fail": len(names),
                            "attachment_uploaded": 0, "attachment_failed": 0},
                "warnings": [traceback.format_exc()[-1500:]], "retry_count": 0})
            console.artifact(out_dir / "intake_report.json")
        except Exception:
            pass
        traceback.print_exc()
        return 1
