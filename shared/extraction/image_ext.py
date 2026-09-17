# -*- coding: utf-8 -*-
"""图片梯队：无文本层可提，直接终态（契约 D11：本期不做 OCR，不硬造字段，
进 ❌ 清单建议提供文字版）。status=no_text_layer, backend=none。

P3 的 VisionOcrExt 落地前，本梯队是图片的唯一去处；它返回非 ok 结果，
chain 耗尽后的终态与旧实现逐字段一致（text="" / pages=None / warning=固定文案）。
"""

from __future__ import annotations

from documents import ExtractionResult, FileKind, ResumeDocument
from extraction.base import TextExtractor

__all__ = ["ImageExt"]


class ImageExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        return doc.kind == FileKind.IMAGE

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        return ExtractionResult("", None, "no_text_layer", "none", [
            "图片文件无文本层可提取（本期不做 OCR）。"
            "建议候选人提供 word/pdf 文字版简历。"])
