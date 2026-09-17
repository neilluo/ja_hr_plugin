# -*- coding: utf-8 -*-
"""用户可读清单（ApplyReportBuilder）：rows / match_rows / summary 的组装（纯函数面）。

原 apply_decisions.py 的 `_build_rows`(L1029-1126) / `_printable_table`(L1188-1196)
搬入。rows/match_rows/summary 的键插入序与文案（「已匹配/门槛不符/跳过/失败」四态、
✅/⏸/❌ 图标行）直接进 apply_report.json 与 stdout 字节面，逐字保留。
"""

from typing import Any, Dict, List, Sequence

from match.applyvalues import as_text
from match.decisionctx import resolve_job
from match.recordfactory import MatchRecordFactory


class ApplyReportBuilder:
    """岗位｜匹配度｜技能｜加分｜结论（未过门槛标注原因）的清单产出。"""

    def __init__(self):
        self._factory = MatchRecordFactory()

    def build_rows(self, cand_index: Dict[str, Dict[str, Any]],
                   table_cells: Dict[str, Dict[str, Any]],
                   row_meta: List[Dict[str, Any]], rejected: List[Dict[str, Any]],
                   job_index: Dict[str, Dict[str, Any]], onboarded: Sequence[str],
                   create_res: Dict[str, Any], skipped_onboarded: int,
                   skipped_invalid: Sequence[Dict[str, Any]] = None) -> Dict[str, Any]:
        """产出用户可读清单：岗位｜匹配度｜技能｜加分｜结论（未过门槛标注原因）。"""
        by_cand: Dict[str, List[Dict[str, Any]]] = {}
        for m in row_meta:
            by_cand.setdefault(m["candidate_key"], []).append(m)
        invalid_by_cand: Dict[str, List[Dict[str, Any]]] = {}
        for si in (skipped_invalid or []):
            invalid_by_cand.setdefault(str(si.get("candidate_key")), []).append(si)
        rej_by_cand: Dict[str, List[Dict[str, Any]]] = {}
        for r in rejected:
            ck = str(r.get("candidate_key") or "")
            for jk in (r.get("job_keys") or []):
                job = resolve_job(job_index, r, str(jk)) or {}
                rej_by_cand.setdefault(ck, []).append({
                    "job_key": str(jk), "job_name": as_text(job.get("job_name")) or None,
                    "job_id": as_text(job.get("job_id")) or None,
                    "reason": as_text(r.get("reason")) or None})

        created_ids = set(create_res.get("record_ids") or [])
        rows: List[Dict[str, Any]] = []
        match_rows: List[Dict[str, Any]] = []
        seq = 0
        ordered = sorted(cand_index.keys(), key=lambda x: str(x))
        for ck in ordered:
            c = cand_index[ck]
            merged = self._factory.cells_for(c, table_cells.get(ck))
            seq += 1
            if ck in onboarded:
                rows.append({"seq": seq, "candidate_key": ck, "name": merged["name"],
                             "file_name": c.get("file_name"), "org": merged["org"],
                             "result": "跳过", "reason": "沟通状态=已入职（老插件铁律：删记录、不打分、不推荐）",
                             "matches": [], "rejected": []})
                continue
            ms = by_cand.get(ck, [])
            inv = invalid_by_cand.get(ck, [])
            for m in ms:
                rc = m["recomputed"]
                status = "%s %d" % ("✅" if rc["recommend"] == "推荐" else
                                    ("⏸" if rc["recommend"] == "待定" else "❌"), rc["total_score"])
                match_rows.append({
                    "候选人": m["candidate_name"], "岗位": m["job_name"], "岗位ID": m["job_id"],
                    "部门": m.get("department"),
                    "匹配度": rc["total_score"], "技能": rc["skill_score"], "加分": rc["bonus_score"],
                    "结论": rc["recommend"], "结论图标": status,
                    "技能命中": "%d/%d" % (rc["skill_hits"], rc["skill_total"]),
                    "加分命中": "%d/%d" % (rc["bonus_hits"], rc["bonus_total"]),
                    "匹配依据": m.get("evidence"),
                    "模型分数不一致": m.get("mismatch") or None,
                    "written": bool(created_ids),
                })
            if ms and inv:
                result, reason = "已匹配", ("另有 %d 条 pass 因命中项越界（D16 判为编造）未建记录：%s"
                                           % (len(inv), "; ".join("%s×%s" % (x.get("candidate_key"),
                                                                              x.get("job_key"))
                                                                  for x in inv)))
            elif ms:
                result, reason = "已匹配", None
            elif inv:
                result, reason = "失败", ("有 %d 条 pass 但命中项越界（D16 判为编造）→ 未建记录：%s"
                                         % (len(inv), "; ".join(str(x.get("fabricated_hits"))
                                                                for x in inv)))
            else:
                result, reason = "门槛不符", "全部同组织在招岗位均未通过硬性门槛，未产生记录"
            rows.append({"seq": seq, "candidate_key": ck, "name": merged["name"],
                         "file_name": c.get("file_name"), "org": merged["org"],
                         "result": result, "reason": reason,
                         "matches": [{"job_name": m["job_name"], "job_id": m["job_id"],
                                      "total_score": m["recomputed"]["total_score"],
                                      "skill_score": m["recomputed"]["skill_score"],
                                      "bonus_score": m["recomputed"]["bonus_score"],
                                      "recommend": m["recomputed"]["recommend"],
                                      "skill_hits": "%d/%d" % (m["recomputed"]["skill_hits"],
                                                               m["recomputed"]["skill_total"]),
                                      "bonus_hits": "%d/%d" % (m["recomputed"]["bonus_hits"],
                                                               m["recomputed"]["bonus_total"]),
                                      "evidence": m.get("evidence"),
                                      "model_mismatch": m.get("mismatch") or None}
                                     for m in ms],
                         "rejected": rej_by_cand.get(ck, [])})
        summary = {
            "candidates": len(ordered),
            "matched": len([r for r in rows if r["result"] == "已匹配"]),
            "gate_failed": len([r for r in rows if r["result"] == "门槛不符"]),
            "skip": len([r for r in rows if r["result"] == "跳过"]),
            "fail": len(create_res.get("failed") or []) + len([r for r in rows
                                                               if r["result"] == "失败"]),
            "match_records": len(match_rows),
            "recommend": len([m for m in match_rows if m["结论"] == "推荐"]),
            "pending": len([m for m in match_rows if m["结论"] == "待定"]),
            "reject": len([m for m in match_rows if m["结论"] == "不推荐"]),
            "skipped_onboarded_pairs": skipped_onboarded,
            "skipped_invalid_hits": len(skipped_invalid or []),
        }
        return {"rows": rows, "match_rows": match_rows, "summary": summary}

    def printable_table(self, match_rows: Sequence[Dict[str, Any]]) -> List[str]:
        if not match_rows:
            return []
        out = ["候选人 | 岗位 | 匹配度 | 技能 | 加分 | 结论"]
        for m in match_rows:
            out.append("%s | %s | %s | %s | %s | %s"
                       % (m.get("候选人"), m.get("岗位"), m.get("匹配度"),
                          m.get("技能"), m.get("加分"), m.get("结论图标") or m.get("结论")))
        return out
