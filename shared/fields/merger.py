# -*- coding: utf-8 -*-
"""多源字段合并器。

设计意图（P4 才用满）：`extractors` 按优先级排列，前面的先抽；抽不到可用结果
（`text_usable=False`，即扫描件/水印/空文本）才落到后面的。P4 的 agent 多模态
兜底就是往这个列表尾部再挂一个 `FieldExtractor` 实现，然后用 `merge()` 做
「regex 优先、外部草稿补空、逐字段打 field_source」。

**P2 只实现单源路径**：`extractors` 里只有 `RegexFieldExtractor`，`merge()` 的
`drafts` 恒为空 —— 逐字段补空与冲突裁决**未实现**，只把 field_source 全量打标。
打标结果 `field_sources` 目前不进 `to_dict()`（进了就会改动 FIELDS 层指纹），
P4 若要透传给 candidates.json，那是 P4 的 EXPECTED DIFF。
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


class FieldMerger(object):

    SOURCE_REGEX = "regex"
    SOURCE_DRAFT = "draft"
    SOURCE_AGENT = "agent"

    def __init__(self, extractors: Optional[Sequence[FieldExtractor]] = None):
        self.extractors: List[FieldExtractor] = (list(extractors)
                                                 if extractors
                                                 else [RegexFieldExtractor()])

    # -- 抽取（按优先级问过去，第一个拿到可用文本结果的赢）------------------
    def resume_fields(self, text: str, file_name: str) -> CandidateFields:
        return self.merge_resume(self._run("extract_resume", text, file_name))

    def job_fields(self, text: str, file_name: str) -> JobFields:
        return self.merge_job(self._run("extract_job", text, file_name))

    def _run(self, method: str, text: str, file_name: str) -> Any:
        last = None
        for ext in self.extractors:
            last = getattr(ext, method)(text, file_name)
            if last.text_usable:
                return last
        return last

    # -- 合并（P2：单源，只打标）-------------------------------------------
    def merge_resume(self, primary: CandidateFields,
                     drafts: Sequence[Any] = (),
                     primary_source: str = SOURCE_REGEX) -> CandidateFields:
        for name in CANDIDATE_BUSINESS_FIELDS:
            primary.field_sources[name] = primary_source
        return primary

    def merge_job(self, primary: JobFields,
                  drafts: Sequence[Any] = (),
                  primary_source: str = SOURCE_REGEX) -> JobFields:
        for name in JOB_BUSINESS_FIELDS:
            primary.field_sources[name] = primary_source
        return primary
