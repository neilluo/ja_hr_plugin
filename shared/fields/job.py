# -*- coding: utf-8 -*-
"""岗位说明书（JD）侧字段载体：17 个业务字段 + 来源 + warnings。

与 `candidate.CandidateFields` 对称。`to_dict()` 是**对外契约面**
（`extract_fields.extract_job_fields` 的返回值），键集必须与重构前逐字一致：
`text_usable=False` 那条路径**不含** `job_name_from_filename` /
`department_from_filename`（旧实现就没放），且 `warnings` 紧跟 `confidence`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["JobFields", "BUSINESS_FIELDS", "EMPTY_KEYS"]

#: JD 侧业务字段
BUSINESS_FIELDS = (
    "job_name", "department", "hard_gates_raw", "must_skills_raw",
    "bonus_skills_raw", "education_req", "years_req", "major_req", "cert_req",
    "cert_is_preferred_not_required", "sections", "must_skills", "bonus_skills",
    "cert_required", "cert_preferred", "years_req_min", "age_req",
)

#: 文本层无效时要置 "none" 置信度的键（原 extract_job_fields 里的内联元组）
EMPTY_KEYS = ("job_name", "department", "education_req", "years_req",
              "major_req", "cert_req", "must_skills_raw", "bonus_skills_raw",
              "hard_gates_raw", "cert_is_preferred_not_required")


@dataclass
class JobFields:
    # ---- 业务字段 ----
    job_name: Optional[str] = None
    department: Optional[str] = None
    hard_gates_raw: Optional[str] = None
    must_skills_raw: Optional[str] = None
    bonus_skills_raw: Optional[str] = None
    education_req: Optional[str] = None
    years_req: Optional[str] = None
    major_req: Optional[str] = None
    cert_req: Optional[str] = None
    cert_is_preferred_not_required: bool = False
    sections: Dict[str, str] = field(default_factory=dict)
    must_skills: List[str] = field(default_factory=list)
    bonus_skills: List[str] = field(default_factory=list)
    cert_required: List[str] = field(default_factory=list)
    cert_preferred: List[str] = field(default_factory=list)
    years_req_min: Optional[int] = None
    age_req: Optional[str] = None
    # ---- 来源 ----
    department_source: Optional[str] = None
    job_name_from_filename: Optional[str] = None
    department_from_filename: Optional[str] = None
    field_sources: Dict[str, str] = field(default_factory=dict)
    # ---- 置信度 / 告警 / 元信息 ----
    confidence: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    text_usable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "job_name": self.job_name,
            "department": self.department,
            "hard_gates_raw": self.hard_gates_raw,
            "must_skills_raw": self.must_skills_raw,
            "bonus_skills_raw": self.bonus_skills_raw,
            "education_req": self.education_req,
            "years_req": self.years_req,
            "major_req": self.major_req,
            "cert_req": self.cert_req,
            "cert_is_preferred_not_required": self.cert_is_preferred_not_required,
            "sections": self.sections,
            "confidence": self.confidence,
        }
        lists: Dict[str, Any] = {
            "must_skills": self.must_skills,
            "bonus_skills": self.bonus_skills,
            "cert_required": self.cert_required,
            "cert_preferred": self.cert_preferred,
            "years_req_min": self.years_req_min,
            "age_req": self.age_req,
        }
        if not self.text_usable:
            out["warnings"] = self.warnings
        out.update(lists)
        out["warnings"] = self.warnings
        if self.text_usable:
            out["job_name_from_filename"] = self.job_name_from_filename
            out["department_from_filename"] = self.department_from_filename
        out["department_source"] = self.department_source
        out["text_usable"] = self.text_usable
        return out
