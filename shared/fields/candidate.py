# -*- coding: utf-8 -*-
"""简历侧字段载体：19 个业务字段 + 每字段来源 + warnings。

`to_dict()` 是**对外契约面**（`extract_fields.extract_resume_fields` 的返回值），
键集与键序必须与重构前逐字一致——两个分支的键集本来就不同：
`text_usable=False`（扫描件/水印/空文本）时**不含** `edu_entries`，因为那条路径
根本没跑教育条目解析（契约 D11：不硬造字段）。

来源有两层，别混：
  * `field_sources[field]` = 值由哪个抽取器给出（regex / draft / agent）。P2 只有
    regex 一路，由 `merger.FieldMerger` 统一打标；**不进 to_dict()**——进了就会
    改动 FIELDS 层指纹，P4 若要透传给 candidates.json 属于 P4 的 EXPECTED DIFF。
  * `name_source` / `years_experience_source` = 现状契约键，细粒度取值来源
    （text / filename / estimated / None）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["CandidateFields", "BUSINESS_FIELDS", "EMPTY_KEYS"]

#: 19 个业务字段（= to_dict() 的键集去掉 name_source/confidence/warnings/
#: years_source 别名/name_from_filename/name_from_text/text_usable 这 7 个元信息键）
BUSINESS_FIELDS = (
    "name", "phone", "email", "education", "school", "school_rank", "major",
    "years_experience", "certificates", "skills", "expected_position",
    "expected_location", "expected_salary", "sections", "education_raw",
    "years_experience_est", "years_experience_source", "schools_all",
    "edu_entries",
)

#: 文本层无效时要置 "none" 置信度的键（原 extract_fields._EMPTY_RESUME_KEYS）
EMPTY_KEYS = ("name", "phone", "email", "education", "school", "major",
              "years_experience", "expected_position", "expected_location",
              "expected_salary", "school_rank", "certificates", "skills")


@dataclass
class CandidateFields:
    # ---- 19 业务字段 ----
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    education: Optional[str] = None
    school: Optional[str] = None
    school_rank: Optional[str] = None
    major: Optional[str] = None
    years_experience: Optional[int] = None
    certificates: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    expected_position: Optional[str] = None
    expected_location: Optional[str] = None
    expected_salary: Optional[str] = None
    sections: Dict[str, str] = field(default_factory=dict)
    education_raw: Optional[str] = None
    years_experience_est: Optional[int] = None
    years_experience_source: Optional[str] = None
    schools_all: List[str] = field(default_factory=list)
    edu_entries: Optional[List[Dict[str, Any]]] = None
    # ---- 每字段来源 ----
    name_source: Optional[str] = None
    field_sources: Dict[str, str] = field(default_factory=dict)
    # ---- 置信度 / 告警 / 元信息 ----
    confidence: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    text_usable: bool = True
    name_from_filename: Optional[str] = None
    name_from_text: Optional[str] = None

    @property
    def sources(self) -> Dict[str, Optional[str]]:
        """细粒度取值来源视图（现状只有姓名与工作年限两个字段有）。"""
        return {"name": self.name_source,
                "years_experience": self.years_experience_source}

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "name_source": self.name_source,
            "phone": self.phone,
            "email": self.email,
            "education": self.education,
            "school": self.school,
            "school_rank": self.school_rank,
            "major": self.major,
            "years_experience": self.years_experience,
            "certificates": self.certificates,
            "skills": self.skills,
            "expected_position": self.expected_position,
            "expected_location": self.expected_location,
            "expected_salary": self.expected_salary,
            "sections": self.sections,
            "confidence": self.confidence,
            "warnings": self.warnings,
            "education_raw": self.education_raw,
            "years_experience_est": self.years_experience_est,
            "years_experience_source": self.years_experience_source,
            # 契约 D13 键名别名（与 years_experience_source 同值，只增不删）
            "years_source": self.years_experience_source,
            "schools_all": self.schools_all,
        }
        if self.text_usable:
            out["edu_entries"] = self.edu_entries
        out["name_from_filename"] = self.name_from_filename
        out["name_from_text"] = self.name_from_text
        out["text_usable"] = self.text_usable
        return out
