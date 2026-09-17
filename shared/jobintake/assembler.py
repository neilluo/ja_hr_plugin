# -*- coding: utf-8 -*-
"""JobFieldAssembler：岗位表 payload / draft 文档的字段组装（纯函数，无 IO）。

自 intake_job.py 逐字搬移（P9b）：
  build_write_row   阶段 5 写入行（原 702–734）——岗位表 payload 的**字段顺序与
                    cells 键**是红线：job_name/status/must_weight/bonus_weight/
                    submit_time 五键先行，其后条件键按 job_id → department → org →
                    work_location → responsibilities → requirements → submitter 的
                    插入序；缺字段 pop 循环的顺序与告警文案逐字。
  build_draft_job   阶段 9 jobs[] 元素（原 887–942）——24+ 键插入序即
                    jobs_draft.json 字节（裁判 DRAFT 面双指纹含 ORDER）；
                    parse_status != "ok" 时的「不硬造字段」置空分支（契约 D11）逐字。
  build_apply_cells Turn 3 cells 组装（原 1027–1053）——hard_gates/must_skills/
                    bonus_skills → 权重 → 直传五键 → work_location 的顺序即键序；
                    返回 (cells, hg, ms, bs)，后三者供编排层的「全空」告警判定。

刻意保留的既有行为（不许「顺手修好」）：
  * resp/req 的写前处理是**裸切片** `[:RICHTEXT_MAX]`，不走 A 侧 sanitize_text
    （P6 分析 §3.1：是否遗漏待业务确认，统一会改产物字节）。
  * weights 恒为默认 0.7/0.3（float 字面量 json.dump 写 `0.7`，不得改 Decimal/round）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from jobintake.constants import (BONUS_WEIGHT_DEFAULT, HARD_GATE_KEYS,
                                 LLM_NORMALIZE_FIELDS, MUST_WEIGHT_DEFAULT,
                                 RICHTEXT_MAX, STATUS_DEFAULT)
from jobintake.jdfields import as_text_list, compose_hard_gates
from jobintake.textutil import clean, today, truncate

__all__ = ["JobFieldAssembler"]


class JobFieldAssembler:
    """岗位表写入行 / draft 文档 / apply cells 的组装。判定与告警落点留在编排层，
    本类只按既有规则拼值（build_write_row 的缺字段告警是唯一例外——它逐字搬移，
    告警仍 append 进传入的 warnings list，落点与顺序不变）。"""

    def build_write_row(self, ent: Dict[str, Any], have: Sequence[str],
                        submitter_cell: Optional[List[Dict[str, Any]]],
                        warnings: List[str]) -> Dict[str, Any]:
        """Turn 1 只写脚本能确定的字段（刻意**不写** hard_gates / must_skills /
        bonus_skills，见 intake_job.py 模块 docstring）。"""
        f = ent["fields"]
        secs = f.get("sections") or {}
        resp = clean(secs.get("responsibility_text")) or clean(secs.get("work_text"))
        req = clean(secs.get("qualification_text")) or clean(secs.get("skill_text"))
        row: Dict[str, Any] = {
            "job_name": clean(f.get("job_name")),
            "status": STATUS_DEFAULT,
            "must_weight": MUST_WEIGHT_DEFAULT,
            "bonus_weight": BONUS_WEIGHT_DEFAULT,
            "submit_time": today(),
        }
        if ent.get("job_id"):
            row["job_id"] = ent["job_id"]
        if clean(f.get("department")):
            row["department"] = clean(f.get("department"))
        if ent.get("org"):
            row["org"] = ent["org"]
        if ent.get("locations"):
            row["work_location"] = ent["locations"]
        if resp:
            row["responsibilities"] = resp[:RICHTEXT_MAX]
        if req:
            row["requirements"] = req[:RICHTEXT_MAX]
        if submitter_cell is not None:
            row["submitter"] = submitter_cell          # v3 §9#5：配置了才回填
        for k in ("job_id", "department", "org", "work_location",
                  "responsibilities", "requirements"):
            if k not in have and k in row:
                row.pop(k)
                warnings.append("config.json 的 job 表没有字段 %s，已跳过写入" % k)
        return row

    def build_draft_job(self, ent: Dict[str, Any]) -> Dict[str, Any]:
        """jobs_draft.json 的 jobs[] 元素（调用前 ent["result"] 必须已定）。"""
        f = ent.get("fields") or {}
        secs = f.get("sections") or {}
        job: Dict[str, Any] = {
            "key": "j%02d" % ent["seq"],
            "record_id": ent.get("record_id"),
            "job_id": ent.get("job_id"),
            "job_name": clean(f.get("job_name")),
            "department": clean(f.get("department")),
            "org": ent.get("org"),
            "status": STATUS_DEFAULT if ent["result"] in ("新入库", "已覆盖") else None,
            "hard_gates": {
                "education": clean(f.get("education_req")),
                "major": clean(f.get("major_req")),
                "years": clean(f.get("years_req")),
                "certificates": clean(f.get("cert_req")),
            },
            "must_skills": [str(x) for x in (f.get("must_skills") or [])],
            "bonus_skills": [str(x) for x in (f.get("bonus_skills") or [])],
            "weights": {"must": MUST_WEIGHT_DEFAULT, "bonus": BONUS_WEIGHT_DEFAULT},
            # ---- 交给 Turn 2 的 LLM 归一化（本脚本刻意不写库）----
            "file_name": ent["file_name"],
            "parse_status": ent["parse_status"],
            "dedupe": ent["dedupe"],
            "attachment_status": ent["attachment_status"],
            "work_location": ent.get("locations") or [],
            "org_confidence": ent.get("org_confidence") or "low",
            "needs_llm_normalization": list(LLM_NORMALIZE_FIELDS),
            "draft_source": "regex(Turn1) —— 命中率实测 26%~79%，必须 Turn 2 复核",
            "hard_gates_raw": clean(f.get("hard_gates_raw")),
            "must_skills_raw": clean(f.get("must_skills_raw")),
            "bonus_skills_raw": clean(f.get("bonus_skills_raw")),
            "cert_is_preferred_not_required": bool(f.get("cert_is_preferred_not_required")),
            "cert_required": [str(x) for x in (f.get("cert_required") or [])],
            "cert_preferred": [str(x) for x in (f.get("cert_preferred") or [])],
            "years_req_min": f.get("years_req_min"),
            "age_req": clean(f.get("age_req")),
            "regex_confidence": dict(f.get("confidence") or {}),
            "evidence": {k: truncate(secs.get(k), 4000) for k in
                         ("education_text", "cert_text", "work_text", "skill_text",
                          "qualification_text", "responsibility_text", "kpi_text",
                          "age_text")},
            "warnings": list(ent.get("warnings") or []),
        }
        if ent["parse_status"] != "ok":
            # 契约 D11：不硬造字段
            for k in ("job_id", "job_name", "department", "org", "status",
                      "hard_gates_raw", "must_skills_raw", "bonus_skills_raw",
                      "years_req_min", "age_req"):
                job[k] = None
            job["hard_gates"] = {k: None for k in HARD_GATE_KEYS}
            job["must_skills"] = []
            job["bonus_skills"] = []
            job["cert_is_preferred_not_required"] = False
            job["cert_required"] = []
            job["cert_preferred"] = []
            job["work_location"] = []
            job["org_confidence"] = "low"
            job["evidence"] = {k: "" for k in job["evidence"]}
        return job

    def build_apply_cells(self, j: Dict[str, Any], have: Sequence[str]
                          ) -> Tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str]]:
        """Turn 3 单项 cells 组装。返回 (cells, hg, ms, bs)。"""
        cells: Dict[str, Any] = {}
        hg = compose_hard_gates(j.get("hard_gates"))
        if hg and "hard_gates" in have:
            cells["hard_gates"] = hg
        ms = as_text_list(j.get("must_skills"))
        if ms and "must_skills" in have:
            cells["must_skills"] = ms
        bs = as_text_list(j.get("bonus_skills"))
        if bs and "bonus_skills" in have:
            cells["bonus_skills"] = bs
        w = j.get("weights") or {}
        if isinstance(w, dict):
            for src, dst in (("must", "must_weight"), ("bonus", "bonus_weight")):
                v = w.get(src)
                if isinstance(v, (int, float)) and dst in have:
                    cells[dst] = float(v)
        for src, dst in (("org", "org"), ("department", "department"), ("status", "status"),
                         ("job_name", "job_name"), ("job_id", "job_id")):
            v = clean(j.get(src))
            if v and dst in have:
                cells[dst] = v
        if "work_location" in have:
            wl = j.get("work_location")
            if isinstance(wl, str) and clean(wl):
                cells["work_location"] = [clean(wl)]
            elif isinstance(wl, (list, tuple)) and wl:
                cells["work_location"] = [str(x).strip() for x in wl if str(x).strip()]
        return cells, hg, ms, bs
