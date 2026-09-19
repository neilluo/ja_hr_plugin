# -*- coding: utf-8 -*-
"""stdout 冻结契约（MatchReporter）：digest 摘要 + ARTIFACT:/SHARD: 协议行。

`report_and_emit(res, emit_stdout=False, emit_always=False)` 是**冻结签名**，
薄壳留在入口脚本、委托到本类 `report()`。
"""

import json
from typing import Any, Dict

#: stdout 预算（字节）。qodercli 对 Bash tool_result 有硬上限（~30 KB 处静默切断），
#: 所以默认**不**把判定输入打进 stdout；本函数与 --emit-stdout 开关**均不移植进 CLI**
#: （agent 拿到半截 JSON 反而去 Read digest.json / Grep .py 源码，多烧回合与 output token）。
#: 保留本函数仅为 report 的冻结签名 `emit_stdout=False` 参数有定义可依。
EMIT_BUDGET_BYTES = 20000


class MatchReporter:
    """build 侧 agent 消费面（stdout 行协议）的唯一出口。无状态。"""

    def emit_shard_stdout(self, shard_doc: Dict[str, Any]) -> int:
        """**默认关闭、CLI 不可达**：把单片判定输入打到 stdout。

        返回打出的字符数；**超出 EMIT_BUDGET_BYTES 就返回 -1 并且什么都不打**。
        此路负收益，故不接任何 CLI 开关，仅保留函数体以满足 report 的冻结签名
        （emit_stdout 恒为 False，本函数永不被调用）。
        """
        payload = {
            "batch_id": shard_doc.get("batch_id"),
            "shard": shard_doc.get("shard"),
            "candidates": shard_doc.get("candidates"),
            "jobs": shard_doc.get("jobs"),
        }
        s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        nb = len(s.encode("utf-8"))
        if nb > EMIT_BUDGET_BYTES:
            print("判定输入 %d 字节 > stdout 预算 %d 字节（qodercli Bash 输出 ~30 KB 处会静默截断）"
                  "→ 未打 stdout，请按上面的 SHARD: 路径 Read 分片文件。"
                  % (nb, EMIT_BUDGET_BYTES), flush=True)
            return -1
        print("== 判定输入（本片全部信息，无需再 Read 分片文件；打分口径见 HOTPATH 卡片）==",
              flush=True)
        print(s, flush=True)
        print("== 判定输入结束 ==", flush=True)
        return len(s)

    def report(self, res: Dict[str, Any], emit_stdout: bool = False,
               emit_always: bool = False) -> int:
        """打印 digest 摘要 + ARTIFACT 行 + SHARD: 行（agent 下一步唯一该 Read 的东西）。

        独立成函数是为了让 intake_resume.py --auto-match 能在同一进程里复用完全相同的输出口径
        （两条入口的 stdout 一致，agent 学一次就够）。

        `emit_stdout` / `emit_always` 是冻结签名参数，但**恒为 False、无任何 CLI 开关能打开**：
        把判定输入打进 stdout 负收益——qodercli 在 ~30 KB 处静默切断 Bash 输出，
        半截 JSON 会让 agent 转而去 Read digest.json、甚至 Read/Grep .py 源码自证，多烧回合
        与 output token。所以判定输入**不打 stdout**，一律按 SHARD: 路径 Read 分片文件。
        返回 0 = digest.ok；1 = 不可判定（errors 里有业务话原因）。
        """
        d = res["digest"]
        m = d["meta"]
        print("digest: ok=%s 候选人 %d（原始 %d，已入职剔除 %d）× 在招岗位 %d = %d 个组合"
              % (d.get("ok"), m["candidate_count_active"], m["candidate_count_total"],
                 len(m["excluded_onboarded"]), m["job_count"], m["combo_count"]))
        if m.get("source_mode") == "from_table":
            ft = m.get("from_table") or {}
            print("来源: 简历库表存量导出（表内 %s 条，--org=%s，--exclude-onboarded=%s，"
                  "查询阶段排除已入职 %s；evidence 来源 %s）"
                  % (ft.get("from_table_resume_records"), ft.get("org_filter") or "不过滤",
                     bool(ft.get("exclude_onboarded")), ft.get("from_table_excluded_onboarded"),
                     json.dumps(ft.get("from_table_evidence_sources"), ensure_ascii=False)))
        print("分片: %d 片（--max-per-batch=%d）；输入 %d chars ≈ %d tokens"
              % (m["shard_count"], m["max_per_batch"], m["input_chars"], m["input_est_tokens"]))
        # 每片摘要 + 裁剪遥测从 shard_docs 读（其 shard meta 按预筛/裁剪后的 shard_jobs 计算，
        # 因此「输入 chars」反映 agent 真正要读进上下文的体量）；两开关都关时与全量一致、遥测=disabled。
        shard_docs = res.get("shard_docs") or []
        paths = res.get("shard_paths") or []
        digest_shards = m.get("shards") or []
        for idx, doc in enumerate(shard_docs):
            s = doc.get("shard") or (digest_shards[idx] if idx < len(digest_shards) else {})
            print("  片 %02d: %d 人 × %d 岗 = %d 组合 | 输入 %d chars ≈ %d tok | "
                  "稀疏输出估算 ≈ %d chars ≈ %d tok | 岗位预筛=%s 字段裁剪=%s（全量 %d 岗）"
                  % (s.get("shard_index", idx + 1), s.get("candidate_count", 0),
                     s.get("job_count", 0), s.get("combo_count", 0),
                     s.get("input_chars", 0), s.get("input_est_tokens", 0),
                     s.get("output_est_chars", 0), s.get("output_est_tokens", 0),
                     s.get("jobs_prefilter_note", "disabled"), s.get("jobs_slimmed", False),
                     s.get("job_count_all", m.get("job_count", 0))))
        if m["needs_review_years"]:
            print("D13 needs_review=[years]（工作年限是估算的，agent 必须复核）: %s"
                  % ",".join(m["needs_review_years"]))
        # 组织预筛疑似错杀的逐人清单（明细在分片候选人 prefilter_suspicious 里；
        # 这里只打人名+被删岗位名，控制 stdout 体量——Bash 输出 ~30KB 处会被静默截断）
        susp_meta = m.get("prefilter_suspicious") or {}
        if susp_meta.get("candidate_count"):
            print("P5 组织预筛疑似错杀 %d 人 / %d 个跨组织岗位组合（全部通过机械门槛却被删；"
                  "Turn 2 必须依 evidence 复核组织，判错 → candidate_overrides.org + 重跑）："
                  % (susp_meta["candidate_count"], susp_meta.get("job_pair_count", 0)))
            for doc in shard_docs:
                for c in doc.get("candidates") or []:
                    ps = c.get("prefilter_suspicious") or []
                    if ps:
                        shown = "；".join("%s[%s|%s]" % (e.get("job_name") or "?",
                                                        e.get("job_key") or "?",
                                                        e.get("dropped_org") or "?")
                                         for e in ps[:5])
                        if len(ps) > 5:
                            shown += "；等%d岗" % len(ps)
                        print("  PREFILTER_SUSPICIOUS: %s(%s) org=%s → 被删 %d 岗: %s"
                              % (c.get("name") or "?", c.get("key"), c.get("org_guess") or "?",
                                 len(ps), shown))
        if m["excluded_onboarded"]:
            print("已入职剔除（老插件铁律）: %s"
                  % ",".join(str(o["name"]) for o in m["excluded_onboarded"]))
        for e in d.get("errors") or []:
            print("ERROR: %s" % e)
        for w in m["warnings"]:
            print("WARN: %s" % w)
        print("dws_calls=%d elapsed_ms=%d python=%s"
              % (m["dws_calls"], m["elapsed_ms"], m["python"]))
        print("ARTIFACT:%s" % res["digest_path"])

        # SHARD: 行永远打印 —— 这是 agent 下一步**唯一**该 Read 的东西（判定输入不打 stdout）。
        if len(paths) == 1:
            print("下一步：Read 这个分片文件拿判定输入（**不要 Read digest.json** —— 它是给 "
                  "verify/apply 用的全量版，内容与分片重复且更大；也**不要 Read/Grep 任何 .py 源码**）：",
                  flush=True)
        elif len(paths) > 1:
            print("多片（%d 片）：按「一片一个回合」各自 Read 下面的分片文件"
                  "（**不要 Read digest.json**，也**不要 Read/Grep 任何 .py 源码**）：" % len(paths),
                  flush=True)
        for p in paths:
            print("SHARD:%s" % p, flush=True)
        # emit_stdout 恒 False（无 CLI 开关能打开；打 stdout 负收益，见 report docstring）
        if emit_stdout and shard_docs and (len(shard_docs) == 1 or emit_always):
            total = 0
            for doc in shard_docs:
                n = self.emit_shard_stdout(doc)
                if n > 0:
                    total += n
            if total:
                print("emit_stdout_chars=%d（stdout 已含本片全部判定输入，可省掉上面那次 Read）"
                      % total, flush=True)
        return 0 if d.get("ok") else 1
