# -*- coding: utf-8 -*-
"""分片与裁剪（ShardPlanner）：D3 分片 + W-I 的 L3 组织预筛 / L4 字段裁剪。

原 build_match_input.py 的 `make_shards`(L908) / `shard_meta`(L918) /
`_estimate_output`(L945) / `slim_job`(L985) / `select_shard_jobs`(L990) 搬入；
L3/L4 两个开关（org_prefilter / slim_jobs）由构造注入，编排层读同名公开属性。

select_shard_jobs 的 note 五值枚举 {"disabled","prefiltered","low_confidence_org",
"no_org_guess","no_same_org_jobs"} 直接进分片字节（jobs_prefilter_note），保持
**字符串字面量**不做 Enum 化（分析报告 D.1 的 Enum 建议在本刀裁剪：任何序列化形态
变化都会破 RAW 指纹，收益为零）。
"""

import math
from typing import Any, Dict, List, Sequence, Tuple

from match.tablevalues import CHARS_PER_TOKEN, est_tokens

# --------------------------------------------------------------------------- #
# W-I 性能优化（W-J 移植）：分片岗位裁剪（L3 组织预筛 + L4 冗余字段裁剪）
#
# 背景：合并版 digest.json 与每个分片 digest_batch_NN.json 过去都携带**全部**在招岗位的
# **全部**字段。实测单份简历场景：19 岗 × 全字段 = 16315 字符，其中只有 16 个组合是同组织
# 可判定的；agent 还同时 Read 了 digest.json 与 digest_batch_01.json 两份近似重复的文件，
# 一次就把 ~30k token 灌进上下文。
#
# 设计约束（不可破坏）：
#   * **合并版 digest.json 保持全量不动** —— verify_decisions.py / apply_decisions.py
#     一律吃 digest.json，下游脚本的输入契约零变化，零回归风险。
#   * 裁剪只作用于**分片文件**（agent 唯一需要读进上下文的东西）。
#   * 保留字段 = verify_decisions.py 实读字段（key/org/status/must_skills/bonus_skills/
#     weights/job_id/job_name）+ Turn 2 判定必需（hard_gates/requirements_text/
#     years_req_min/cert_is_preferred_not_required）+ 清单输出用（job_name/department）。
# --------------------------------------------------------------------------- #

#: L4：分片里裁掉的岗位字段（Turn 2 语义判定读不到它们，apply/verify 走 digest.json）
SLIM_DROP_JOB_FIELDS = ("record_id", "responsibilities_text", "hard_gates_raw",
                        "gates_source", "skills_source")


class ShardPlanner:
    """按 --max-per-batch 分片（D3）+ 每片规模/token 估算 + L3/L4 裁剪。"""

    def __init__(self, org_prefilter: bool = True, slim_jobs: bool = True):
        self.org_prefilter = org_prefilter
        self.slim_jobs = slim_jobs

    def make_shards(self, candidates: Sequence[Dict[str, Any]], max_per_batch: int) -> List[List[Dict[str, Any]]]:
        """按 --max-per-batch 分片（D3）。**只在同分片内做候选人×岗位组合**。

        排序稳定：先按 key（C1 给的顺序本身就是入库顺序，key 是 c01..cNN），保证可复现。
        """
        size = max(1, int(max_per_batch))
        items = list(candidates)
        return [items[i:i + size] for i in range(0, len(items), size)] or []

    def shard_meta(self, shard: Sequence[Dict[str, Any]], jobs: Sequence[Dict[str, Any]],
                   idx: int, total_shards: int) -> Dict[str, Any]:
        """单片的规模与 token 估算（供 agent 判断是否超载）。"""
        orgs = {j.get("org") for j in jobs}
        combos = 0
        for c in shard:
            corg = c.get("org_guess")
            combos += len([j for j in jobs if (not corg or j.get("org") == corg)])
        payload = {"candidates": list(shard), "jobs": list(jobs)}
        chars, toks = est_tokens(payload)
        out_chars, out_toks = self.estimate_output(shard, jobs, combos)
        return {
            "shard_index": idx,
            "shard_total": total_shards,
            "candidate_count": len(shard),
            "job_count": len(jobs),
            "combo_count": combos,
            "candidate_orgs": sorted([c.get("org_guess") for c in shard if c.get("org_guess")]),
            "job_orgs": sorted([o for o in orgs if o]),
            "input_chars": chars,
            "input_est_tokens": toks,
            "output_est_chars": out_chars,
            "output_est_tokens": out_toks,
            "token_basis": "中文按 %.1f 字符/token 粗估（与前序实验同口径）" % CHARS_PER_TOKEN,
        }

    def estimate_output(self, shard: Sequence[Dict[str, Any]], jobs: Sequence[Dict[str, Any]],
                        combos: int) -> Tuple[int, int]:
        """稀疏 decisions 的输出量估算：pass 条目详写、reject 条目按 candidate 聚合。

        前序实验的实测通过率约 31/190 ≈ 16%，这里按 25% 保守估（宁可高估不要低估）。
        """
        n_pass = int(math.ceil(combos * 0.25))
        n_rej = max(0, combos - n_pass)
        avg_must = sum(len(j.get("must_skills") or []) for j in jobs) / max(1, len(jobs))
        avg_bonus = sum(len(j.get("bonus_skills") or []) for j in jobs) / max(1, len(jobs))
        # 一条 pass：键名 + gate_detail 四项 + 命中项（按 60% 命中率）+ evidence ≤80 字
        per_pass = 150 + int(avg_must * 0.6 * 12) + int(avg_bonus * 0.6 * 12) + 80
        # reject 是 {"candidate_key","job_keys":[...],"reason"}：每人一条聚合
        per_cand_rej = 60 + n_rej / max(1, len(shard)) * 6 + 20
        chars = int(n_pass * per_pass + len(shard) * per_cand_rej)
        return chars, int(math.ceil(chars / CHARS_PER_TOKEN))

    def slim_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """裁掉判定用不到的岗位字段，保持键序不变。"""
        return {k: v for k, v in job.items() if k not in SLIM_DROP_JOB_FIELDS}

    def select_shard_jobs(self, shard: Sequence[Dict[str, Any]],
                          jobs: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        """L3：分片只带**本片候选人同组织**的在招岗位（组合本来就只在同组织内发生）。

        安全阀（务必保留，否则会漏判）：
          * 本片任一候选人 `org_confidence == "low"` → **保留全部岗位**。因为 agent 可能在
            Turn 2 依 evidence 把组织改判到另一个中心（回填 candidate_overrides.org），
            改判后它仍需看到另一侧的岗位；预筛掉就等于把改判后的可判岗位删了。
          * 本片候选人全都没有 `org_guess` → 保留全部岗位（口径同 verify：组织缺失退化为全部在招岗位）。
          * 预筛后一个岗位都不剩 → 保留全部岗位，让 agent 看得见"为什么无可判组合"，
            而不是拿到空 jobs 数组无从交代。

        返回 (要落盘的岗位列表, note)；note ∈
          {"disabled","prefiltered","low_confidence_org","no_org_guess","no_same_org_jobs"}
        """
        if not self.org_prefilter:
            return list(jobs), "disabled"
        if any(str(c.get("org_confidence") or "").strip().lower() == "low" for c in shard):
            return list(jobs), "low_confidence_org"
        orgs = {c.get("org_guess") for c in shard if c.get("org_guess")}
        if not orgs:
            return list(jobs), "no_org_guess"
        kept = [j for j in jobs if j.get("org") in orgs]
        if not kept:
            return list(jobs), "no_same_org_jobs"
        return kept, "prefiltered"
