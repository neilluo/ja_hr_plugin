# -*- coding: utf-8 -*-
"""IntakeConsole：intake 入口脚本 stdout/stderr 的唯一出口（P7 刀1）。

从 skills/resume-intake/scripts/intake_resume.py 收拢全部 61 处 print：
本类持有输出文本的格式契约（分隔线、icon 表、截断长度、协议行前缀），
不含任何业务判定——只接受编排层已算好的值并打印。

冻结面（net_oracle.parse_report 的 5 个字面锚点 + HOTPATH.md 协议行，
一个字符都不能变）：
  * "── 简历入库结果 ──────────────" / 24 个 U+2500 的 "────────────────────────"
  * 行格式 "%d | %s | %s | %s"、"小计：…"、"墙钟 …dws_calls…"
  * ARTIFACT: / RESUME: / VISION_NEEDED: / FATAL: / SHARD:（后者由 build_match_input 打）
  * icon 表里 "❌ 失败" 的「失败」二字（oracle 据此判失败名单）
  * flush=True 语义（killcheck_p3 硬杀后读日志，未 flush 的输出会丢）
"""

import sys
from typing import Any, Dict, Mapping, Optional, Sequence


class IntakeConsole:
    #: 人读清单 result → icon（resume profile；job profile 属 P8/P9，勿在此合并）
    RESULT_ICONS: Dict[str, str] = {
        "新入库": "✅ 新入库", "已覆盖": "✅ 已覆盖", "跳过": "⏭️ 跳过",
        "失败": "❌ 失败", "未完成": "⏸ 未完成"}
    #: warnings 预览条数与单条截断长度（清单尾部）
    WARN_PREVIEW_N = 8
    WARN_CLIP = 220

    # ---- 底层输出（流与 flush 语义逐处对应原脚本） ----
    def _out(self, text: str) -> None:
        print(text, flush=True)

    def _err(self, text: str, flush: bool = True) -> None:
        print(text, file=sys.stderr, flush=flush)

    # ---- 开场 ----
    def banner(self) -> None:
        self._out("== 简历入库 Turn 1（脚本内一次做完，零 agent 回合）==")

    def batch_info(self, batch_id: str, out_dir: Any) -> None:
        self._out("batch_id=%s  out_dir=%s" % (batch_id, out_dir))

    def base_info(self, base_name: Any, base_id: Any,
                  table_name: Any, table_id: Any) -> None:
        self._out("base=%s(%s)  表=%s(%s)" % (base_name, base_id, table_name, table_id))

    def reset_result(self, found: int, deleted: int, failed_n: int) -> None:
        self._out("--reset：表内发现 %d 条，已删除 %d 条（失败 %d）"
                  % (found, deleted, failed_n))

    def checkpoint_loaded(self, n_done: int) -> None:
        self._out("读到 checkpoint：%d 份已成功，重跑将跳过（D12 幂等）" % n_done)

    def vision_patch_loaded(self, n_entries: int, path: Any) -> None:
        self._out("读到 agent 兜底补丁：%d 个文件条目（%s）" % (n_entries, path))

    # ---- 阶段 1：提取 ----
    def extract_summary(self, n_entries: int, workers: int, n_ok: int,
                        n_pending: int, n_vision: int, extract_ms: int) -> None:
        self._out("提取完成：%d 份文件（并发 %d），%d 份可用文本，%d 份不可用%s%s；"
                  "本地耗时 %dms（0 次 dws 调用）"
                  % (n_entries, workers, n_ok, n_entries - n_ok - n_pending,
                     ("，%d 份因预算未提取" % n_pending) if n_pending else "",
                     ("，%d 份转 agent 多模态兜底" % n_vision) if n_vision else "",
                     extract_ms))

    def vision_needed(self, paths: Sequence[str]) -> None:
        # 单行、空格分隔的绝对路径清单——agent 兜底协议触发器（见 HOTPATH.md）
        self._out("VISION_NEEDED: %s" % " ".join(paths))

    def vision_needed_hint(self, n: int) -> None:
        self._out("%d 份文件本机读不出文字 → agent 多模态兜底：**一轮**读完上面全部文件，"
                  "按补丁 schema 写 json，重跑同一命令加 --apply-vision-patch <json>"
                  "（agent 只产出补丁，绝不写库）" % n)

    def vision_gate(self, n_needed: int, n_entries: int) -> None:
        self._out("⛔ 本批 %d/%d 份读不出文字，超过 20%% 阈值，疑似整批格式问题，"
                  "请确认后重试或提供文字版（本轮不写任何记录，报告 reason=vision_gate）"
                  % (n_needed, n_entries))

    def vision_gate_file(self, file_name: Any, path: Any) -> None:
        self._out("  读不出文字: %s（%s）" % (file_name, path))

    def budget_halt(self, budget: float) -> None:
        self._out("⏸ 墙钟预算 %.0fs 耗尽：未进入写库阶段，graceful 停止"
                  "（checkpoint 已落盘，退出码 0）" % budget)

    # ---- 阶段 2/3：去重 ----
    def old_lib_dedupe_fallback(self) -> None:
        self._out("⚠️ 简历库没有「附件内容MD5」字段 → 库内附件去重**未启用内容级比对**，"
                  "回退「文件名+字节大小」（启用方法与影响见 report.warnings）")

    def dedupe_scan(self, key_label: Any, records: int, indexed: int,
                    coverage_note: Optional[str], secs: float, calls: int) -> None:
        self._out("库内附件「%s」比对索引：%d 条记录 / %d 个附件%s（%.2fs，%d 次调用）"
                  % (key_label, records, indexed,
                     ("，" + coverage_note) if coverage_note else "", secs, calls))

    def dedupe_scan_truncated(self, max_pages: int, records: int) -> None:
        self._out("⚠️ 库内去重扫描不完整：max_pages=%d 已翻满，只取回 %d 条记录；"
                  "可加 --no-dedupe-scan 跳过库内比对" % (max_pages, records))

    def phone_scan(self, n_phones: int, hits: int, secs: float, calls: int) -> None:
        self._out("批量手机号查重：%d 个手机号 → 库内命中 %d 个（%.2fs，%d 次调用）"
                  % (n_phones, hits, secs, calls))

    # ---- 阶段 5/6/6b/7/8：写库链路 ----
    def ensure_options(self, field_key: Any, n_names: int, n_opts: int,
                       secs: float, calls: int) -> None:
        self._out("ensure_options(%s)：%d 个候选标签 → 字段现有 %d 个选项（%.2fs，%d 次调用）"
                  % (field_key, n_names, n_opts, secs, calls))

    def upload_summary(self, concurrency: int, uploaded: int, failed: int,
                       deferred: int, ms: int, calls: int) -> None:
        self._out("附件并发上传（concurrency=%d，逐片落 checkpoint）：%d 成功 / %d 失败 / "
                  "%d 预算内未传，%dms，%d 次调用"
                  % (concurrency, uploaded, failed, deferred, ms, calls))

    def fixup_deferred(self, n_all: int, cap: int, n_this: int, n_deferred: int) -> None:
        self._out("补传附件队列 %d 份 > 单轮上限 %d：本轮只处理前 %d 份，"
                  "其余 %d 份 defer 到下一轮（重跑同一命令续补）"
                  % (n_all, cap, n_this, n_deferred))

    def fixup_summary(self, n_fixups: int, uploaded: int, failed: int,
                      ms: int, calls: int) -> None:
        self._out("补传附件（v3 §9#6，记录不重建）：%d 份待补 → %d 成功 / %d 失败，%dms，%d 次调用"
                  % (n_fixups, uploaded, failed, ms, calls))

    def upsert_summary(self, created: Any, updated: Any, failed_n: int,
                       secs: float, calls: int) -> None:
        self._out("批量 upsert 简历库：created=%s updated=%s failed=%d，%.2fs，%d 次调用"
                  % (created, updated, failed_n, secs, calls))

    def readback_summary(self, requested: int, found: int, mismatch: int,
                         attach_missing: int, polls: int, secs: float,
                         calls: int) -> None:
        self._out("回读校验：%d 条请求 / %d 条读到 / %d 处不一致 / %d 条附件缺失，"
                  "轮询 %d 次，%.2fs，%d 次调用"
                  % (requested, found, mismatch, attach_missing, polls, secs, calls))

    # ---- 尾部人读清单 + 协议行 ----
    def result_banner(self) -> None:
        self._out("")
        self._out("── 简历入库结果 ──────────────")
        self._out("序号 | 文件名 | 处理结果 | 说明")

    def row(self, seq: Any, file_name: Any, result: Any, reason: Any) -> None:
        self._out("%d | %s | %s | %s"
                  % (seq, file_name, self.RESULT_ICONS.get(result, result), reason))

    def result_divider(self) -> None:
        self._out("────────────────────────")

    def subtotal(self, summary: Mapping[str, int]) -> None:
        self._out("小计：新入库 %d | 覆盖 %d | 跳过 %d | 失败 %d | 附件已传 %d | 附件失败 %d"
                  % (summary["new"], summary["overwrite"], summary["skip"],
                     summary["fail"], summary["attachment_uploaded"],
                     summary["attachment_failed"]))

    def pending_budget_note(self, n: int) -> None:
        self._out("其中墙钟预算内未完成：%d 份（重跑同一命令续跑，checkpoint 幂等）" % n)

    def fixup_note(self, uploaded: int, failed: int) -> None:
        self._out("其中补传附件（记录未重建，v3 §9#6）：成功 %d | 失败 %d"
                  % (uploaded, failed))

    def wall(self, elapsed_ms: int, dws_calls: int, retries: int, extract_ms: int,
             turns_saved: int, old_turns_per_file: int, n_done_files: int,
             new_turns: int, n_files: int) -> None:
        self._out("墙钟 %.2fs | dws_calls=%d（重试 %d）| 本地提取 %dms | 估算省下 %d 个 agent 回合"
                  "（老插件 %d 回合/份 × %d 份已完成 − 本脚本 %d 回合；未完成 %d 份不计入）"
                  % (elapsed_ms / 1000.0, dws_calls, retries, extract_ms, turns_saved,
                     old_turns_per_file, n_done_files, new_turns,
                     n_files - n_done_files))

    def warnings_block(self, warnings: Sequence[str]) -> None:
        self._out("warnings %d 条（前 8 条）：" % len(warnings))
        for w in warnings[:self.WARN_PREVIEW_N]:
            self._out("  - %s" % w[:self.WARN_CLIP])

    def partial_vision_gate(self, n_vision: int, n_entries: int,
                            ratio_pct: float) -> None:
        self._out("⏸ partial=true：20%% 闸门触发（%d/%d 份读不出文字 > %.0f%%），"
                  "本轮未写任何记录；请与用户确认整批格式问题后重跑同一命令，"
                  "或让用户提供文字版简历" % (n_vision, n_entries, ratio_pct))

    def partial_budget(self, budget: float, n_pending: int, n_deferred: int) -> None:
        self._out("⏸ partial=true：墙钟预算 %.0fs 内未完成 %d 份、附件欠传 %d 份；"
                  "checkpoint 已逐条落盘，重跑同一命令续跑（幂等，不产生重复记录）"
                  % (budget, n_pending, n_deferred))

    def pending_file(self, name: Any) -> None:
        self._out("  未完成: %s" % name)

    def deferred_file(self, name: Any) -> None:
        self._out("  附件欠传: %s" % name)

    def resume(self, cmd: str) -> None:
        self._out("RESUME: %s" % cmd)

    def fatal(self, msg: Any) -> None:
        self._out("FATAL: %s" % msg)

    def candidates_line(self, path: Any) -> None:
        self._out("candidates → %s" % path)

    def checkpoint_line(self, path: Any) -> None:
        self._out("checkpoint → %s" % path)

    def artifact(self, path: Any) -> None:
        self._out("ARTIFACT:%s" % path)

    # ---- CLI（main 参数校验；stderr 无 flush，与原脚本一致） ----
    def cli_error(self, msg: Any) -> None:
        self._err("错误：%s" % msg, flush=False)

    def interrupted(self) -> None:
        self._err("被用户中断", flush=False)

    # ---- auto-match（O2 合并入口） ----
    def auto_match_skip_banner(self) -> None:
        self._out("== auto-match 跳过 ==")

    def auto_match_skip_failed(self, intake_rc: int) -> None:
        self._err("入库未成功（退出码 %d）→ 不生成判定输入。请先按入库报告修正后重跑入库"
                  "（checkpoint 幂等，已成功项不会重放）。" % intake_rc)

    def auto_match_skip_partial(self) -> None:
        self._err("本批 partial=true（墙钟预算内有未完成/附件欠传项）→ 先按 RESUME 提示重跑"
                  "同一命令补齐，再单独跑 build_match_input.py 生成判定输入。")

    def auto_match_skip_no_out_dir(self) -> None:
        self._err("--auto-match 需要显式 --out-dir（否则定位不到 candidates.json）")

    def auto_match_skip_no_candidates(self, path: Any) -> None:
        self._err("找不到 %s → 不生成判定输入" % path)

    def auto_match_banner(self) -> None:
        self._out("== auto-match（同进程接着做，省 1 个 agent 回合）==")

    def auto_match_import_error(self, exc_type: str, exc: Any, directory: Any) -> None:
        self._err("auto-match 失败：导入 build_match_input 出错：%s: %s（目录 %s）"
                  % (exc_type, exc, directory))

    def auto_match_rerun_hint(self) -> None:
        self._err("入库产物已落地，可单独重跑 build_match_input.py，不会重复入库。")

    def auto_match_error(self, exc_type: str, exc: Any) -> None:
        self._err("auto-match 失败：%s: %s —— 入库产物已落地，可单独重跑 build_match_input.py，"
                  "不会重复入库。" % (exc_type, exc))

    def auto_match_wall(self, secs: float, match_out: Any) -> None:
        self._out("auto-match 墙钟 %.2fs | match_out=%s" % (secs, match_out))
