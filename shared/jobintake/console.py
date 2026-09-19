# -*- coding: utf-8 -*-
"""JobConsole：intake_job.py stdout/stderr 的唯一出口。

收拢原脚本全部 29 处 print；本类持有输出文本的格式契约，不含任何业务判定——
只接受编排层已算好的值并打印。

冻结面（run_job_oracle.sh 的 OUT_* / DRAFT / REPORT 面 + HOTPATH.md 协议行，
一个字符都不能变）：
  * "── 岗位入库结果 ──────────────" / 24 个 U+2500 的 "────────────────────────"
  * 行格式 "%d | %s | %s | %s"、"小计：…JD附件已传…JD附件失败…"、"墙钟 …dws_calls…"
  * "ARTIFACT:" 协议行（stdout 末行；config 失败/异常兜底路径也各有一行）
  * RESULT_ICONS 里 "❌ 失败" 的「失败」二字
  * flush=True 语义（stdout 全部 flush；main() 的三处 stderr 错误行**无 flush**，
    与原脚本逐处对应，不得「顺手补齐」）
  * B 侧 icon 表只有 4 项（A 侧 5 项含「未完成」）——两个 profile 不合并
"""

import sys
from typing import Any, Dict, List, Mapping

__all__ = ["JobConsole"]


class JobConsole:
    #: 人读清单 result → icon（job profile；resume profile 在 intake/console.py，勿合并）
    RESULT_ICONS: Dict[str, str] = {
        "新入库": "✅ 新入库", "已覆盖": "✅ 已覆盖", "跳过": "⏭️ 跳过",
        "失败": "❌ 失败"}
    #: warnings 预览条数与单条截断长度（清单尾部）
    WARN_PREVIEW_N = 8
    WARN_CLIP = 220

    # ---- 底层输出（流与 flush 语义逐处对应原脚本） ----
    def _out(self, text: str) -> None:
        print(text, flush=True)

    def _err(self, text: str) -> None:
        print(text, file=sys.stderr)

    # ---- 开场 ----
    def banner(self, mode: str) -> None:
        self._out("== 岗位入库 %s（脚本内一次做完，零 agent 回合）=="
                  % ("Turn 3：应用 LLM 归一化结果" if mode == "apply" else "Turn 1"))

    def batch_info(self, batch_id: str, out_dir: Any) -> None:
        self._out("batch_id=%s  out_dir=%s" % (batch_id, out_dir))

    def base_info(self, base_name: Any, base_id: Any,
                  table_name: Any, table_id: Any) -> None:
        self._out("base=%s(%s)  表=%s(%s)" % (base_name, base_id, table_name, table_id))

    # ---- Turn 1 各阶段计时行（模板逐字） ----
    def extract_done(self, n_entries: int, n_ok: int, extract_ms: int) -> None:
        self._out("提取完成：%d 份 JD，%d 份可用文本；本地耗时 %dms（0 次 dws 调用）"
                  % (n_entries, n_ok, extract_ms))

    def scan(self, n_recs: int, n_keys: int, max_seq: int,
             elapsed_s: float, calls: int) -> None:
        self._out("岗位表扫描：%d 条在库记录，复合键 %d 个，现有岗位ID 最大序号 %d"
                  "（%.2fs，%d 次调用）"
                  % (n_recs, n_keys, max_seq, elapsed_s, calls))

    def ensure_options(self, field_key: str, n_names: int, n_opts: int,
                       elapsed_s: float, calls: int) -> None:
        self._out("ensure_options(job.%s)：%d 个候选值 → 字段现有 %d 个选项（%.2fs，%d 次调用）"
                  % (field_key, n_names, n_opts, elapsed_s, calls))

    def submitter(self, name: Any) -> None:
        self._out("需求提交人：当前登录用户 %s（user 字段回填）" % (name or "?"))

    def upload(self, concurrency: int, n_ok: int, n_fail: int,
               ms: int, calls: int) -> None:
        self._out("附件并发上传（concurrency=%d）：%d 成功 / %d 失败，%dms，%d 次调用"
                  % (concurrency, n_ok, n_fail, ms, calls))

    def batch_create(self, n: int, created: int, n_failed: int,
                     elapsed_s: float, calls: int) -> None:
        self._out("batch_create：%d 条提交 / created=%d / failed=%d（%.2fs，%d 次调用）"
                  % (n, created, n_failed, elapsed_s, calls))

    def batch_update(self, n: int, updated: int, n_failed: int,
                     elapsed_s: float, calls: int) -> None:
        self._out("batch_update：%d 条提交 / updated=%d / failed=%d（%.2fs，%d 次调用）"
                  % (n, updated, n_failed, elapsed_s, calls))

    def readback(self, requested: int, found: int, n_mismatch: int,
                 n_attach_missing: int, polls: int, elapsed_s: float,
                 calls: int) -> None:
        self._out("回读校验：%d 条请求 / %d 条精确归属 / %d 处不一致 / %d 条附件缺失，轮询 %d 次，"
                  "%.2fs，%d 次调用"
                  % (requested, found, n_mismatch, n_attach_missing, polls,
                     elapsed_s, calls))

    # ---- Turn 3 计时行 ----
    def batch_update_verified(self, n: int, verified: int, recovered: int,
                              n_failed: int, elapsed_s: float, calls: int) -> None:
        self._out("batch_update_verified：%d 条提交 / verified=%d / recovered=%d / failed=%d"
                  "（%.2fs，%d 次调用）"
                  % (n, verified, recovered, n_failed, elapsed_s, calls))

    # ---- 尾部人读清单 + 协议行 ----
    def blank(self) -> None:
        self._out("")

    def result_header(self) -> None:
        self._out("── 岗位入库结果 ──────────────")
        self._out("序号 | 文件名 | 处理结果 | 说明")

    def result_row(self, row: Mapping[str, Any]) -> None:
        self._out("%d | %s | %s | %s"
                  % (row["seq"], row["file_name"],
                     self.RESULT_ICONS.get(row["result"], row["result"]), row["reason"]))

    def result_footer(self) -> None:
        self._out("────────────────────────")

    def subtotal(self, summary: Mapping[str, int]) -> None:
        self._out("小计：新入库 %d | 覆盖 %d | 跳过 %d | 失败 %d | JD附件已传 %d | JD附件失败 %d"
                  % (summary["new"], summary["overwrite"], summary["skip"], summary["fail"],
                     summary["attachment_uploaded"], summary["attachment_failed"]))

    def wall_line(self, elapsed_ms: int, calls: int, retries: int,
                  turns_saved: int) -> None:
        self._out("墙钟 %.2fs | dws_calls=%d（重试 %d）| 估算省下 %d 个 agent 回合"
                  % (elapsed_ms / 1000.0, calls, retries, turns_saved))

    def warnings_block(self, warnings: List[str]) -> None:
        if warnings:
            self._out("warnings %d 条（前 8 条）：" % len(warnings))
            for w in warnings[:self.WARN_PREVIEW_N]:
                self._out("  - %s" % str(w)[:self.WARN_CLIP])

    def draft_line(self, draft_path: Any) -> None:
        self._out("jobs_draft → %s" % draft_path)

    def next_step(self) -> None:
        self._out("下一步：把 jobs_draft.json 交 Turn 2 的 agent 归一化"
                  "（硬性门槛四项拆解 / 必备技能与加分项切分），产出 jobs_final.json 后用 "
                  "--apply 写回")

    def artifact(self, path: Any) -> None:
        self._out("ARTIFACT:%s" % path)

    # ---- main() 的 stderr（无 flush，与原脚本一致） ----
    def err_usage(self) -> None:
        self._err("错误：Turn 1 需要 --files；Turn 3 需要 --apply <jobs_final.json>")

    def err_config(self, config: Any) -> None:
        self._err("错误：--config 不存在：%s" % config)

    def interrupted(self) -> None:
        self._err("被用户中断")
