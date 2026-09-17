# -*- coding: utf-8 -*-
"""多源字段合并器。

设计意图：`extractors` 按优先级排列，前面的先抽；抽不到可用结果
（`text_usable=False`，即扫描件/水印/空文本）才落到后面的。

**P2 只实现单源路径**：`extractors` 里只有 `RegexFieldExtractor`，`merge()` 的
`drafts` 恒为空 —— 只把 field_source 全量打标。打标结果 `field_sources` 不进
`to_dict()`（进了就会改动 FIELDS 层指纹）。

**P4 起 merge_resume 实现真正的草稿合并**（agent 多模态兜底通道）：
`drafts` 是外部草稿 dict 列表（intake `--apply-vision-patch` 传入补丁的
`fields_draft`），合并规则（主控裁决，逐字执行）：
  1. 先对文本跑 RegexFieldExtractor（`_run` 已做）；
  2. **regex 有值的字段用 regex**；
  3. regex 为空的字段才取草稿值，且只接受白名单 `_DRAFT_FIELDS` 里的业务字段；
  4. 凡取自草稿的字段打 `field_source="agent_vision"`，其余 `field_source="regex"`；
  5. 取自草稿的字段名追加进 `needs_review`（候选人级复核清单，intake 透传给
     candidates.json，由回合 2 用 evidence 原文复核）。
草稿值本身不校验语义（agent 只产出结构化补丁、绝不写库；写库仍由脚本按
正常候选流程做查重/护栏/回读）。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from fields.base import FieldExtractor
from fields.candidate import BUSINESS_FIELDS as CANDIDATE_BUSINESS_FIELDS
from fields.candidate import CandidateFields
from fields.job import BUSINESS_FIELDS as JOB_BUSINESS_FIELDS
from fields.job import JobFields
from fields.regex_ext import RegexFieldExtractor

__all__ = ["FieldMerger"]

#: 允许从外部草稿补空的业务字段白名单（sections/edu_entries 等派生结构不补，
#: 防止草稿注入非业务键；派生结构一律以 regex 在补丁文本上重跑的结果为准）。
_DRAFT_FIELDS = ("name", "phone", "email", "education", "school", "school_rank",
                 "major", "years_experience", "certificates", "skills",
                 "expected_position", "expected_location", "expected_salary")


class FieldMerger(object):

    SOURCE_REGEX = "regex"
    SOURCE_DRAFT = "draft"
    SOURCE_AGENT = "agent"
    #: P4 agent 多模态兜底补丁的字段来源标记（主控裁决口径）
    SOURCE_AGENT_VISION = "agent_vision"

    def __init__(self, extractors: Optional[Sequence[FieldExtractor]] = None):
        self.extractors: List[FieldExtractor] = (list(extractors)
                                                 if extractors
                                                 else [RegexFieldExtractor()])

    # -- 抽取（按优先级问过去，第一个拿到可用文本结果的赢）------------------
    def resume_fields(self, text: str, file_name: str) -> CandidateFields:
        return self.merge_resume(self._run("extract_resume", text, file_name))

    def job_fields(self, text: str, file_name: str) -> JobFields:
        return self.merge_job(self._run("extract_job", text, file_name))

    def resume_fields_with_drafts(self, text: str, file_name: str,
                                  drafts: Sequence[Any] = (),
                                  draft_source: str = SOURCE_AGENT_VISION
                                  ) -> CandidateFields:
        """P4 agent 兜底入口：对 text 跑 regex，再用外部草稿补空 + 打标。"""
        primary = self._run("extract_resume", text, file_name)
        return self.merge_resume(primary, drafts,
                                 primary_source=self.SOURCE_REGEX,
                                 draft_source=draft_source)

    def _run(self, method: str, text: str, file_name: str) -> Any:
        last = None
        for ext in self.extractors:
            last = getattr(ext, method)(text, file_name)
            if last.text_usable:
                return last
        return last

    # -- 合并 ---------------------------------------------------------------
    @staticmethod
    def _is_empty(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return not v.strip()
        if isinstance(v, (list, tuple, dict)):
            return len(v) == 0
        return False

    def merge_resume(self, primary: CandidateFields,
                     drafts: Sequence[Any] = (),
                     primary_source: str = SOURCE_REGEX,
                     draft_source: str = SOURCE_AGENT_VISION) -> CandidateFields:
        for name in CANDIDATE_BUSINESS_FIELDS:
            primary.field_sources[name] = primary_source
        for draft in (drafts or ()):
            if not isinstance(draft, dict):
                continue
            for name in _DRAFT_FIELDS:
                if name not in draft:
                    continue
                val = draft[name]
                if (name == "years_experience" and isinstance(val, str)
                        and val.strip().isdigit()):
                    val = int(val.strip())
                if self._is_empty(val):
                    continue
                if not self._is_empty(getattr(primary, name, None)):
                    continue        # regex 有值 → 用 regex（裁决规则②）
                setattr(primary, name, val)
                primary.field_sources[name] = draft_source
                if name not in primary.needs_review:
                    primary.needs_review.append(name)
        return primary

    def merge_job(self, primary: JobFields,
                  drafts: Sequence[Any] = (),
                  primary_source: str = SOURCE_REGEX) -> JobFields:
        for name in JOB_BUSINESS_FIELDS:
            primary.field_sources[name] = primary_source
        return primary
