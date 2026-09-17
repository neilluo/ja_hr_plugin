# -*- coding: utf-8 -*-
"""WallBudget：--wall-budget 墙钟预算的全部语义（P7 刀3）。

收拢 skills/resume-intake/scripts/intake_resume.py 里散落的预算状态：
  t_start / budget / deadline   计时三元组（构造时定格，t_start 供 Report 算 elapsed_ms）
  budget_stopped                预算触顶标志（阶段1 截断 / 写库前停 / 附件截断）
  deferred_files                记录已入库、附件因预算欠传的文件名清单

「不进任何 dws 写阶段」的 `halted` 标志**不在本类**（P7 刀6 起归 IntakePipeline）：
它是预算与 20% vision gate 共用的编排级闸门，两个写点分处两个阶段，收在 Pipeline
才有唯一所有权；本类的 halt_before_write() 只翻 budget_stopped + 打印。

partial 语义（→ RESUME: 行触发条件）由 IntakeReport.assemble 读取
budget_stopped / deferred_files 后判定；args._partial 黑板写与 --auto-match
在 partial 时跳过 SHARD: 行的语义均保持在 Report / auto_match 侧（本类不碰）。

与 CheckpointStore 的协作点：阶段1 提取后 store.persist() 在预算检查点 A 之前
（顺序是红线）；本类不新增任何落盘时机（5 原子 + 1 非原子是全集）。
与 Console 的协作点：halt_before_write() 调 console.budget_halt(budget)。

预算检查点 A 里 `ent["md5"] not in done_md5` 的判定属 Pipeline（用
store.done_md5 快照），本类只提供 halt_reason() 文本与 budget_stopped 翻转。
"""

import time
from typing import Any, List


class WallBudget:
    def __init__(self, args: Any, default_budget: float) -> None:
        self.t_start = time.monotonic()
        self.budget = float(getattr(args, "wall_budget", None) or default_budget)
        self.deadline = self.t_start + self.budget
        self.budget_stopped = False
        self.deferred_files: List[str] = []

    def over_budget(self) -> bool:
        return time.monotonic() >= self.deadline

    def stop_extraction(self) -> None:
        """阶段1 提取池触顶：标记 budget_stopped（cancel 循环在 ExtractionRunner）。"""
        self.budget_stopped = True

    def halt_before_write(self, console: Any) -> None:
        """预算检查点 A：写库前 graceful 停止。entries 的「未完成」标记与 `halted`
        闸门都留在 Pipeline（判定用 store.done_md5 快照，属 Pipeline 职责）。"""
        self.budget_stopped = True
        console.budget_halt(self.budget)

    def mark_deferred(self, file_name: str) -> None:
        self.deferred_files.append(file_name)

    # ---- 产物字节：reason / msg 文本（进 rows[].reason 与 warnings，逐字冻结） ----
    def pending_reason(self) -> str:
        """阶段1 未提取（pending_budget）的 reason 文本。"""
        return ("墙钟预算 %.0fs 耗尽，本次未处理；checkpoint 已落盘，"
                "重跑同一命令续跑（幂等，不产生重复记录）" % self.budget)

    def halt_reason(self) -> str:
        """预算检查点 A 的「未完成」reason 文本。"""
        return ("墙钟预算 %.0fs 耗尽（未进入写库阶段），本次未处理；"
                "重跑同一命令续跑" % self.budget)

    def deferred_attachment_msg(self) -> str:
        """阶段6 附件欠传 msg（进 ent.warnings 与全局 warnings）。"""
        return ("墙钟预算（%.0fs）耗尽，附件本轮未上传；记录将先入库，"
                "重跑同一命令自动只补附件（不重建记录）" % self.budget)
