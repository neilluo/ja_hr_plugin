# -*- coding: utf-8 -*-
"""candidate_overrides 合入（OverrideMerger）：契约 v3 §9#1 的七键修正。

原 apply_decisions.py 的 `apply_overrides`(L290-347) / `OVERRIDE_KEYS`(L286) /
`OVERRIDE_WRITEBACK_MAP`(L351-354) 搬入。就地改 cand_index 的语义**原样保留**
（`_override_changes` 是写回简历库与报告留痕的依据）。norm_item 经 match.hitmap
取用（原实现从 verify_decisions import，同一函数）。写回简历库的 IO 部分
（writeback_overrides）在 match.matchgate.MatchTableGateway。
"""

from typing import Any, Dict, List

from match.applyvalues import as_list
from match.hitmap import HitMapper

#: 契约 v3 §9#1 裁定：candidate_overrides 支持且仅支持这七个键（+定位用 candidate_key）。
#: 文档（match-verify/SKILL.md、ai-analysis-spec.md）与本清单必须保持一致。
OVERRIDE_KEYS = ("org", "category", "expected_location", "skills_extra",
                 "certificates_extra", "years_experience", "org_reason")

#: override 内部字段名 → 简历库业务字段名（MatchTableGateway.writeback_overrides 用）
OVERRIDE_WRITEBACK_MAP = (("org_guess", "org"), ("category_guess", "category"),
                          ("expected_location", "expected_location"),
                          ("years_experience", "years_experience"),
                          ("skills", "skills"), ("certificates", "certificates"))


class OverrideMerger:
    """把 agent 在批量判定回合补齐的稀疏字段合回候选人索引。"""

    def __init__(self):
        self._hits = HitMapper()

    def apply_overrides(self, cand_index: Dict[str, Dict[str, Any]],
                        decisions: Dict[str, Any], warnings: List[str]) -> int:
        """把 agent 在批量判定回合补齐的稀疏字段（D13 years 复核 / D14 地点与证书 / 组织归一）
        合回候选人。

        支持的键（契约 v3 §9#1，七个）：`org`、`category`、`expected_location`、
        `skills_extra`(数组，与已有技能合并去重)、`certificates_extra`(数组，与已有证书合并去重)、
        `years_experience`(int，D13 复核后的修正值)、`org_reason`(组织判定理由，只进报告)。

        每处**实际改变值**的修正会记进 `cand["_override_changes"]`（业务字段名→新值），
        供 `writeback_overrides` 写回简历库与报告留痕使用。
        """
        norm_item = self._hits.norm_item
        n = 0
        for o in decisions.get("candidate_overrides") or []:
            if not isinstance(o, dict):
                continue
            ck = o.get("candidate_key")
            if ck is None or str(ck) not in cand_index:
                warnings.append("candidate_overrides 里的 candidate_key=%r 不在候选人索引里，已忽略" % ck)
                continue
            unknown = [k for k in o if k not in OVERRIDE_KEYS and k != "candidate_key"]
            if unknown:
                warnings.append("candidate_overrides[%s] 含不支持的键 %s（支持的键：%s），已忽略这些键"
                                % (ck, ",".join(sorted(unknown)), "/".join(OVERRIDE_KEYS)))
            c = cand_index[str(ck)]
            changed: Dict[str, Any] = c.get("_override_changes") or {}
            for src, dst in (("org", "org_guess"), ("category", "category_guess"),
                             ("expected_location", "expected_location"),
                             ("years_experience", "years_experience")):
                if o.get(src) is not None and o.get(src) != "":
                    if c.get(dst) != o[src]:
                        c[dst] = o[src]
                        changed[dst] = o[src]
                        n += 1
            extra = as_list(o.get("skills_extra"))
            if extra:
                have = {norm_item(x) for x in as_list(c.get("skills"))}
                add = [x for x in extra if norm_item(x) not in have]
                if add:
                    c["skills"] = as_list(c.get("skills")) + add
                    changed["skills"] = list(c["skills"])
                    n += 1
            certs_extra = as_list(o.get("certificates_extra"))
            if certs_extra:
                have = {norm_item(x) for x in as_list(c.get("certificates"))}
                add = [x for x in certs_extra if norm_item(x) not in have]
                if add:
                    c["certificates"] = as_list(c.get("certificates")) + add
                    changed["certificates"] = list(c["certificates"])
                    n += 1
            if o.get("org_reason"):
                c["org_override_reason"] = o["org_reason"]
            if "years" in as_list(c.get("needs_review")) and o.get("years_experience") is not None:
                c["needs_review"] = [x for x in as_list(c.get("needs_review")) if x != "years"]
                c["years_reviewed"] = True
            if changed:
                c["_override_changes"] = changed
        return n
