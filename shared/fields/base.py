# -*- coding: utf-8 -*-
"""字段抽取器抽象基类（与 `extraction/base.py` 同构）。

契约：
    extract_resume(text, file_name) -> CandidateFields
    extract_job(text, file_name)    -> JobFields
两个方法都**永不抛异常**：文本为空/水印/乱码时只回填文件名里
确凿的信息，其余一律留空，并在 `warnings` 里说明。

只有一个实现：`regex_ext.RegexFieldExtractor`。agent 多模态兜底按此
接入，再由 `merger.FieldMerger` 做「regex 优先、外部草稿补空」的合并。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fields.candidate import CandidateFields
from fields.job import JobFields

__all__ = ["FieldExtractor"]


class FieldExtractor(ABC):

    @abstractmethod
    def extract_resume(self, text: str, file_name: str) -> CandidateFields:
        raise NotImplementedError

    @abstractmethod
    def extract_job(self, text: str, file_name: str) -> JobFields:
        raise NotImplementedError
