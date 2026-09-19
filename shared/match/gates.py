# -*- coding: utf-8 -*-
"""门槛语义（build 侧）：EMPTY_GATE 词表 + 组织预筛「错杀」的机械复查。

EMPTY_GATE 同时被 match.jobparse 消费（「无明确要求」归一）——这是 build 侧内部的单一词表源。
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from match.tablevalues import as_list, as_number, clean_ws, clip, \
    dedupe_keep_order

EMPTY_GATE = ("无", "无明确要求", "不限", "无要求", "/", "-", "None", "null", "")

# --------------------------------------------------------------------------- #
# 组织预筛「错杀」的机械复查（零 token、零语义——只做 ordinal/数值/非空比较）
#
# 背景：org_confidence=high 时 L3 预筛把跨组织岗位删出分片，
# 且判定组合本来就只在同组织内发生——组织一旦判错，正确组织的全部组合**静默错杀**，
# Turn 2 不可补救。
#
# 对策：对每个候选人**跨组织**的在招岗位，用 digest 里现成的数据做
# 机械硬门槛筛查：学历 ordinal 比较（博士>硕士>本科>大专>中专）/ 年限数值比较 /
# 证书「无要求或持证者优先视为过，否则候选人证书非空视为过（粗筛）」；**专业跳过**
# （语义项，机械判不了）。全部通过 → 该岗位「本来很有可能是该候选人的正确组织」→
# 分片候选人加 prefilter_suspicious + needs_review 追加 "org" + 聚合 warning。
#
# 实现口径：
#   * 跨组织岗位按**全量在招岗位**算，不只按「本片分片缺了什么」：分片预筛是并集口径
#     （片里只要有一个人属于该组织，岗位就保留），但组合层只在同组织内配对——跨组织
#     岗位对高置信候选人**永远不进组合**，错杀面与分片删光完全一致。
#   * org_confidence=low 的候选人不标：其分片必然保留全部岗位，且 HOTPATH 规则 5
#     已强制 agent 复核组织，再标属重复噪声。
# **不把被删岗位的 JD 塞进分片**；可见即可，复核走
# candidate_overrides.org + 重跑同一命令。
# --------------------------------------------------------------------------- #

#: 学历 ordinal（设计口径：博士>硕士>本科>大专>中专；常见同义词归到同档）。
#: 顺序无关：取命中档的最小值当「岗位下限」、最大值当「候选人学历」。
EDU_ORDINAL = (("博士", 5), ("硕士", 4), ("研究生", 4), ("本科", 3), ("学士", 3),
               ("大学", 3), ("大专", 2), ("专科", 2), ("高职", 2), ("中专", 1),
               ("中职", 1), ("高中", 0))

_YEARS_REQ_RE = re.compile(r"(\d{1,2})\s*年")


class PrefilterAuditor:
    """机械硬门槛复查（纯计算，零 IO）。

    find_prefilter_suspicious **就地**给命中候选人追加 needs_review+="org"
    （入参 candidates 是编排层自有的 normalized 对象，就地语义被编排层封闭）。
    """

    def edu_rank(self, s: Any, mode: str) -> Optional[int]:
        """学历 → ordinal。mode="req" 取命中档最小值（岗位下限），"cand" 取最大值。"""
        t = str(s or "")
        if not t.strip():
            return None
        hits = [o for w, o in EDU_ORDINAL if w in t]
        if not hits:
            return None
        return min(hits) if mode == "req" else max(hits)

    def years_req_of(self, job: Dict[str, Any]) -> Optional[float]:
        """岗位经验年限下限：优先 years_req_min，缺失时从 hard_gates.years
        文本里抓「N年」。抓不到/≤0 → None（= 无年限要求）。"""
        n = as_number(job.get("years_req_min"), None)
        if n is None:
            raw = clean_ws((job.get("hard_gates") or {}).get("years"))
            if raw and raw not in EMPTY_GATE:
                m = _YEARS_REQ_RE.search(raw)
                if m:
                    n = float(m.group(1))
        if n is None or n <= 0:
            return None
        return n

    def mechanical_hard_gates(self, cand: Dict[str, Any], job: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """对一个 候选人×被预筛删掉的岗位 做机械硬门槛筛查。返回 (是否全过, 过闸明细)。

        从严口径（与 Turn 2「证据不足按不达标」一致）：任一项**无法确认通过**即 False——
        岗位要求存在但候选人数据缺失/解析不出 → 不过；专业是语义项，不参与（设计口径）。
        """
        hg = job.get("hard_gates") or {}
        gates: List[str] = []

        # ① 学历：ordinal 比较；要求为空/不限 → 过
        req_s = clean_ws(hg.get("education"))
        if not req_s or req_s in EMPTY_GATE or "不限" in req_s or "无" in req_s:
            gates.append("学历(无要求)")
        else:
            r_req = self.edu_rank(req_s, "req")
            r_cand = self.edu_rank(cand.get("education"), "cand")
            if r_req is None or r_cand is None or r_cand < r_req:
                return False, []
            gates.append("学历(%s≥%s)" % (clip(clean_ws(cand.get("education")), 6, ""),
                                          clip(req_s, 6, "")))

        # ② 年限：数值比较（候选人 years vs 岗位下限）
        req_y = self.years_req_of(job)
        cand_y = as_number(cand.get("years_experience"), None)
        if req_y is None:
            gates.append("年限(无要求)")
        elif cand_y is None or cand_y < req_y:
            return False, []
        else:
            gates.append("年限(%g≥%g)" % (cand_y, req_y))

        # ③ 证书：要求为「无/空/持证者优先」→ 过；否则候选人证书非空视为过（粗筛即可）
        c_req = clean_ws(hg.get("certificates"))
        if (not c_req or c_req in EMPTY_GATE or "无" in c_req or "优先" in c_req
                or job.get("cert_is_preferred_not_required")):
            gates.append("证书(无要求或持证者优先)")
        elif as_list(cand.get("certificates")):
            gates.append("证书(候选人持证,粗筛过)")
        else:
            return False, []

        return True, gates

    def find_prefilter_suspicious(self, candidates: Sequence[Dict[str, Any]],
                                  jobs: Sequence[Dict[str, Any]],
                                  org_prefilter: bool = True) -> Dict[str, List[Dict[str, Any]]]:
        """找出「组织预筛可能错杀」的 候选人×跨组织岗位 组合。

        返回 {candidate_key: [entry,…]}，entry = {"job_key","job_name","dropped_org",
        "passed_mechanical_gates"}；命中的候选人**就地**把 needs_review 追加 "org"。
        只查 org_confidence != low 且有 org_guess 的候选人（口径见模块头注释块）。
        """
        out: Dict[str, List[Dict[str, Any]]] = {}
        if not org_prefilter:
            return out
        for c in candidates:
            corg = clean_ws(c.get("org_guess"))
            if not corg:
                continue
            if str(c.get("org_confidence") or "").strip().lower() == "low":
                continue
            entries: List[Dict[str, Any]] = []
            for j in jobs:
                jorg = clean_ws(j.get("org"))
                if not jorg or jorg == corg:
                    continue
                ok, gates = self.mechanical_hard_gates(c, j)
                if ok:
                    entries.append({"job_key": j.get("key"), "job_name": j.get("job_name"),
                                    "dropped_org": jorg, "passed_mechanical_gates": gates})
            if entries:
                out[c["key"]] = entries
                nr = dedupe_keep_order(as_list(c.get("needs_review")))
                if "org" not in nr:
                    nr.append("org")
                c["needs_review"] = nr
        return out
