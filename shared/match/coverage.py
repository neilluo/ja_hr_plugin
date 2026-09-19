# -*- coding: utf-8 -*-
"""覆盖率枚举（CoverageChecker）：「同组织 + 在招」的必须覆盖组合集。

candidate_overrides[].org 会覆盖 org_guess（组织归一是 agent 在判定回合补的），
组织缺失退化为全部在招岗位——两条口径都是覆盖率判定的契约面，逐字保留。
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple


class CoverageChecker:
    """verify 第 1 项校验（覆盖率）的期望组合枚举。"""

    def expected_pairs(self, digest: Dict[str, Any],
                       overrides: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[str, str]],
                                                                     Dict[str, List[str]],
                                                                     List[str]]:
        """按「候选人组织 == 岗位组织 且 岗位在招」算出**必须覆盖**的组合集合。

        candidate_overrides[].org 会覆盖 org_guess（组织归一是 agent 在判定回合补的）。
        """
        org_of: Dict[str, Optional[str]] = {}
        for c in digest.get("candidates") or []:
            org_of[c.get("key")] = c.get("org_guess") or c.get("org") or None
        for o in overrides or []:
            if isinstance(o, dict) and o.get("candidate_key") in org_of and o.get("org"):
                org_of[o["candidate_key"]] = str(o["org"]).strip()
        jobs_by_org: Dict[str, List[str]] = {}
        open_keys: List[str] = []
        for j in digest.get("jobs") or []:
            status = (j.get("status") or "招聘中").strip()
            if status and status != "招聘中":
                continue
            open_keys.append(j.get("key"))
            jobs_by_org.setdefault(j.get("org") or "", []).append(j.get("key"))
        pairs: List[Tuple[str, str]] = []
        per_cand: Dict[str, List[str]] = {}
        no_job: List[str] = []
        for ckey in sorted(org_of.keys(), key=lambda x: str(x)):
            corg = org_of[ckey]
            jks = jobs_by_org.get(corg or "", [])
            if not corg:
                # 组织缺失：无法判断同组织，退化成「全部在招岗位」并让调用方记 warning
                jks = list(open_keys)
            per_cand[ckey] = list(jks)
            if not jks:
                no_job.append(ckey)
            for jk in jks:
                pairs.append((ckey, jk))
        return pairs, per_cand, no_job
