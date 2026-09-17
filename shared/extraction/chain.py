# -*- coding: utf-8 -*-
"""ExtractorChain：按注册顺序问 can_handle，第一个 extract 成功的赢。

语义与旧 _pdf_text 梯队 fallback 逐条对应（见 documents.ExtractionResult
的 status 注释）：
  - status=="ok"          -> 赢；已走过的每一级的 notes 前插进赢家 notes，
                             backend 链路可追溯；
  - status!="ok"          -> 本级 notes 累积，落下一级；
  - extract 抛异常         -> chain 不捕获，穿透给调用方（门面）——复刻旧实现里
                             docx/doc 容器失败直达通用 except、pdf 加密直达
                             encrypted 的路径；pdf 梯队"异常转下一级"由
                             PypdfExt/JxaExt 内部自捕完成；
  - 全部走完无人赢（耗尽） -> 复刻旧 _pdf_text 终态
                             ("", "none", 0, notes, "no_text_layer" if ran_without_exc else "error")：
                             任一级"跑通但 0 字符"即 no_text_layer，否则 error。
"""

from __future__ import annotations

from typing import List, Optional

from documents import ExtractionResult, ResumeDocument
from extraction.base import TextExtractor

__all__ = ["ExtractorChain"]


class ExtractorChain(object):

    def __init__(self, extractors: Optional[List[TextExtractor]] = None):
        self.extractors: List[TextExtractor] = list(extractors or [])

    def register(self, extractor: TextExtractor) -> None:
        self.extractors.append(extractor)

    def run(self, doc: ResumeDocument) -> ExtractionResult:
        # 先整体读一次字节：复刻旧实现在 try 块内对**所有** kind（含不消费 bytes
        # 的 image/pdf）先行 read_bytes 的行为，读失败的异常映射保持一致
        doc.data()
        notes: List[str] = []
        tried: List[ExtractionResult] = []
        for ext in self.extractors:
            if not ext.can_handle(doc):
                continue
            result = ext.extract(doc)
            if result.status == "ok":
                result.notes = notes + result.notes
                return result
            notes = notes + result.notes
            tried.append(result)
        status = "error"
        for r in tried:
            if r.status == "no_text_layer":
                status = "no_text_layer"
                break
        npages = tried[-1].npages if tried else None
        return ExtractionResult("", npages, status, "none", notes)
