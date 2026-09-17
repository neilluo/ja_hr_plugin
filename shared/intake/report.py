# -*- coding: utf-8 -*-
"""IntakeReport：报告统计判定 + 组装 + 产物落盘 + 尾部人读清单（P7 刀1）。

收拢 skills/resume-intake/scripts/intake_resume.py 的 run() 尾段：
  assemble()  统计（elapsed/dws_calls/turns_saved/infra_fail/ok/partial）+
              14 键 report dict 组装（键序即产物字节，reason 仅 vision_gate 时
              追加在**最后**；args._partial 黑板写保持原语义）
  write()     candidates_doc（5 键）+ tbl.warnings 保序去重合并 +「放引用 →
              补 warnings → 重放」三步 + report/candidates 两份产物落盘
  emit()      尾部人读清单 + 协议行（全部经 IntakeConsole，输出字节冻结）

checkpoint 的终稿 update 与非原子落盘**不在本类**（P7 刀2 CheckpointStore 的
范围）：编排层在 assemble() 与 write() 之间、write() 之后经 CheckpointStore
（finalize() / write_final()）完成，保持原脚本的 now_iso() 调用顺序与
「report → candidates → checkpoint」文件写入顺序。
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aitable.client import now_iso


def _write_json(path: Path, payload: Any) -> None:
    # 与编排层 _write_json 同口径（非原子、indent=2、ensure_ascii=False）；
    # 产物写入口径的 shared 归属是后续 ArtifactWriter 刀，此处不提前统一。
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


class IntakeReport:
    def __init__(self, console: Any, report_path: Path, candidates_path: Path,
                 old_turns_per_file: int, new_turns: int,
                 vision_gate_ratio: float) -> None:
        self.console = console
        self.report_path = report_path
        self.candidates_path = candidates_path
        self.old_turns_per_file = old_turns_per_file
        self.new_turns = new_turns
        self.vision_gate_ratio = vision_gate_ratio
        self.report: Dict[str, Any] = {}
        self.ok = False
        self.partial = False
        self.rows: List[Dict[str, Any]] = []
        self.summary: Dict[str, int] = {}
        self.warnings: List[str] = []
        self.fatal: Optional[str] = None
        self.budget = 0.0
        self.vision_gated = False
        self.elapsed_ms = 0
        self.dws_calls = 0
        self.retries = 0
        self.turns_saved = 0
        self.n_done_files = 0
        self.n_entries = 0
        self.n_files = 0
        self.n_vision = 0
        self.pending_files: List[str] = []
        self.deferred_files: List[str] = []
        self.has_fixups = False

    def assemble(self, args: Any, budget: float, t_start: float, counter: Any,
                 entries: List[Dict[str, Any]], files: List[str],
                 to_write: List[Dict[str, Any]], rows: List[Dict[str, Any]],
                 summary: Dict[str, int], warnings: List[str],
                 fatal: Optional[str], deferred_files: List[str],
                 budget_stopped: bool, vision_needed_paths: List[str],
                 vision_gated: bool,
                 fixups: List[Dict[str, Any]]) -> bool:
        """统计 + ok/partial 判定 + report dict 组装。返回 ok。"""
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        dws_calls = counter.calls
        # 主控裁决回写（P4a）：turns_saved 不再把「未完成」文件计入省下回合——
        # 预算/闸门/补传上限顺延的文件本轮没做完，声称省下它们的回合是虚报。
        n_done_files = sum(1 for e in entries if e["result"] != "未完成")
        turns_saved = (max(0, n_done_files * self.old_turns_per_file - self.new_turns)
                       if files else 0)
        # 基础设施级失败判定：**该写的都没写进去** → ok=false，让 agent 重跑本步（契约 D7）。
        # 与「业务级失败」区分开：全是扫描件导致 rows 全失败是**正常业务结论**，ok 仍为 true。
        wrote_ok = [e for e in to_write if e["result"] in ("新入库", "已覆盖")]
        infra_fail = bool(to_write) and not wrote_ok
        if infra_fail:
            warnings.append("本批 %d 份可入库简历**一份都没写成功**（选项/附件/写库/回读链路"
                            "出现基础设施级错误，详见上面的 warnings）；已置 ok=false，"
                            "请修复后重跑本步（幂等：手机号查重 + checkpoint 都不会产生重复记录）"
                            % len(to_write))
        # --reset 单独使用（不带 --files）时 rows 为空是**预期**结果，不算失败
        ok = fatal is None and not infra_fail and (bool(rows) or (args.reset and not files))

        # ---- P3 墙钟预算：partial 语义（优雅停 + 续跑，不在脚本内循环子批）----
        # partial=true 时报告仍 ok=true、退出码 0：预算内完成的都是真完成，未完成项
        # 靠重跑同一命令续（checkpoint 幂等）。消费面纪律见 HOTPATH.md：见 RESUME:
        # 就重跑同命令，最多 3 次；仍 partial 才把已完成/未完成清单报给用户。
        pending_files = [e["file_name"] for e in entries if e["result"] == "未完成"]
        # P4a：20% 闸门触发也是 partial（本轮零写入，确认后重跑同一命令续处理）
        partial_flag = bool(pending_files or deferred_files or budget_stopped or vision_gated)
        args._partial = partial_flag

        report = {
            "ok": ok,
            "partial": partial_flag,
            "wall_budget_s": budget,
            "pending_files": pending_files,
            "deferred_attachment_files": deferred_files,
            # P4a（只增键）：agent 多模态兜底清单（绝对路径）与 20% 闸门状态。
            # 闸门触发时本轮零写入，报告 ok=true、partial=true、reason="vision_gate"。
            "vision_needed_files": vision_needed_paths,
            "vision_gate": vision_gated,
            "elapsed_ms": elapsed_ms,
            "dws_calls": dws_calls,
            "turns_saved_estimate": turns_saved,
            "rows": rows,
            "summary": summary,
            "warnings": warnings,
            "retry_count": counter.retries,
        }
        if vision_gated:
            report["reason"] = "vision_gate"

        self.report = report
        self.ok = ok
        self.partial = partial_flag
        self.rows = rows
        self.summary = summary
        self.warnings = warnings
        self.fatal = fatal
        self.budget = budget
        self.vision_gated = vision_gated
        self.elapsed_ms = elapsed_ms
        self.dws_calls = dws_calls
        self.retries = counter.retries
        self.turns_saved = turns_saved
        self.n_done_files = n_done_files
        self.n_entries = len(entries)
        self.n_files = len(files)
        self.n_vision = len(vision_needed_paths)
        self.pending_files = pending_files
        self.deferred_files = deferred_files
        self.has_fixups = bool(fixups)
        return ok

    def write(self, tbl: Any, batch_id: str, config_path: str,
              candidates: List[Dict[str, Any]]) -> None:
        """candidates_doc 组装 + tbl.warnings 保序去重合并 + report/candidates 落盘。

        「放引用 → 补 warnings → 重放」三步顺序原样保留（同键重赋不改键位置，
        report 在 candidates_doc 里的位置由第一次放入决定）。
        """
        candidates_doc = {
            "batch_id": batch_id,
            "generated_at": now_iso(),
            "config_path": config_path,
            "candidates": candidates,
            "report": self.report,
        }

        for w in (tbl.warnings if tbl is not None else []):
            if w not in self.report["warnings"]:
                self.report["warnings"].append(w)
        candidates_doc["report"] = self.report

        _write_json(self.report_path, self.report)
        _write_json(self.candidates_path, candidates_doc)

    def emit(self, extract_ms: int, checkpoint_path: Path,
             resume_cmd: Callable[[], str]) -> None:
        """尾部人读清单 + 协议行（沿用老插件「清单式留痕」铁律）。"""
        c = self.console
        c.result_banner()
        for r in self.rows:
            c.row(r["seq"], r["file_name"], r["result"], r["reason"])
        c.result_divider()
        c.subtotal(self.summary)
        if self.summary["pending_budget"]:
            c.pending_budget_note(self.summary["pending_budget"])
        if self.has_fixups:
            c.fixup_note(self.summary["attachment_fixup_uploaded"],
                         self.summary["attachment_fixup_failed"])
        c.wall(self.elapsed_ms, self.dws_calls, self.retries, extract_ms,
               self.turns_saved, self.old_turns_per_file, self.n_done_files,
               self.new_turns, self.n_files)
        if self.warnings:
            c.warnings_block(self.warnings)
        if self.partial:
            if self.vision_gated:
                c.partial_vision_gate(self.n_vision, self.n_entries,
                                      self.vision_gate_ratio * 100)
            else:
                c.partial_budget(self.budget, len(self.pending_files),
                                 len(self.deferred_files))
            for nm in self.pending_files:
                c.pending_file(nm)
            for nm in self.deferred_files:
                c.deferred_file(nm)
            c.resume(resume_cmd())
        if self.fatal:
            c.fatal(self.fatal)
        c.candidates_line(self.candidates_path)
        c.checkpoint_line(checkpoint_path)
        c.artifact(self.report_path)
