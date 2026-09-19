# -*- coding: utf-8 -*-
"""匹配记录工厂（MatchRecordFactory）：写库 payload 的字段渲染（纯函数面）。

`make_record` 的键插入序与 None 剔除规则是 **PAYLOAD 层指纹的主要保护对象**
（写库 payload 字段顺序与 cells 键冻结，逐字保留）；截断上限（[:500]/[:1000]/
[:EVIDENCE_MAX_LEN]）是内联字面量口径，原样保留。
"""

from typing import Any, Dict, Optional

from match.applyvalues import as_list, as_text, join_list, _today
from match.match_basics import EVIDENCE_MAX_LEN, MATCH_SOURCE_SYSTEM


class MatchRecordFactory:
    """表值优先、digest 兜底的字段合并 + 「硬性门槛」渲染 + 匹配记录组装。"""

    def gate_text(self, job: Dict[str, Any], gate_detail: Optional[Dict[str, Any]]) -> str:
        """写进「硬性门槛」字段：岗位要求原文 + 本条判定结果（✓/✗），一眼能看懂为什么过。"""
        hg = job.get("hard_gates") or {}
        base = (job.get("hard_gates_raw") or "").strip()
        if not base:
            base = "|".join("%s:%s" % (lab, hg.get(k) or "无明确要求")
                            for k, lab in (("education", "学历"), ("major", "专业"),
                                           ("years", "年限"), ("certificates", "证书")))
        if isinstance(gate_detail, dict) and gate_detail:
            marks = []
            for k, lab in (("education", "学历"), ("major", "专业"),
                           ("years", "年限"), ("certificates", "证书")):
                if k in gate_detail:
                    v = str(gate_detail[k]).strip().lower()
                    ok = v.startswith(("pass", "达标", "符合", "满足", "true", "yes"))
                    marks.append("%s%s" % (lab, "✓" if ok else "✗"))
            if marks:
                base = "%s ｜判定:%s" % (base, " ".join(marks))
        return base[:500]

    def cells_for(self, cand: Dict[str, Any], table_cells: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """表里的值优先（唯一事实源），digest 的值兜底。"""
        tc = table_cells or {}
        def pick(field_key: str, *alt: str) -> Any:
            v = tc.get(field_key)
            if v not in (None, "", []):
                return v
            for a in alt:
                v = cand.get(a)
                if v not in (None, "", []):
                    return v
            return None
        return {
            "name": as_text(pick("name", "name")) or None,
            "phone": as_text(pick("phone", "phone")) or None,
            "org": as_text(pick("org", "org_guess", "org")) or None,
            "comm_status": as_text(tc.get("comm_status")) or cand.get("comm_status") or None,
            "expected_position": as_text(pick("expected_position", "expected_position")) or None,
            "years_experience": pick("years_experience", "years_experience"),
            "skills": as_list(pick("skills", "skills")),
            "record_id": cand.get("record_id"),
        }

    def years_text(self, v: Any) -> Optional[str]:
        if v in (None, ""):
            return None
        s = as_text(v).strip()
        if not s:
            return None
        try:
            f = float(s)
            return "%d年" % int(f) if f.is_integer() else "%s年" % s
        except ValueError:
            return s[:40]

    def make_record(self, p: Dict[str, Any], merged: Dict[str, Any], job: Dict[str, Any],
                    rc: Dict[str, Any], jid: Optional[str]) -> Dict[str, Any]:
        """一条「系统匹配」记录的 cells（键插入序 = 写库 payload 字节，None 值剔除）。"""
        rec = {
            "name": merged["name"],
            "phone": merged["phone"],
            "job_name": as_text(job.get("job_name")) or None,
            "job_id": jid,                                     # 普通 text，脚本自己 join
            "org": merged["org"] or as_text(job.get("org")) or None,
            "source": MATCH_SOURCE_SYSTEM,
            "cand_skills": join_list(merged["skills"], "、")[:500] or None,
            "must_skills": join_list(job.get("must_skills"), "\n")[:1000] or None,
            "bonus_skills": join_list(job.get("bonus_skills"), "\n")[:1000] or None,
            "hard_gates": self.gate_text(job, p.get("gate_detail")) or None,
            "expected_position": merged["expected_position"] or None,
            "years_experience": (self.years_text(merged["years_experience"]) or None),
            "skill_score": rc["skill_score"],
            "bonus_score": rc["bonus_score"],
            "total_score": rc["total_score"],
            "recommend": rc["recommend"],
            "update_time": _today(),
            "evidence": (as_text(p.get("evidence")).strip()[:EVIDENCE_MAX_LEN] or None),
        }
        return {k: v for k, v in rec.items() if v is not None}
