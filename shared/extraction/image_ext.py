# -*- coding: utf-8 -*-
"""图片梯队：无文本层可提，返回非 ok 结果让 chain 落下一级。

P3 起 macOS 上图片会继续落到 VisionOcrExt（注册序在 JxaExt 之后、本梯队之前，
见 extract_text._CHAIN）自动 OCR 救回；本梯队是 OCR 不可用（非 darwin）或
OCR 文本不可信时的最后一级，其 no_text_layer 结果决定 chain 耗尽终态
（text="" / backend=none），门面据此如实报「无法解析」（D11 不硬造字段）。
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
            "图片文件无文本层可提取，且本机 OCR 未能救回（非 macOS 无 Vision OCR "
            "梯队，或 OCR 文本未通过可信护栏）。建议候选人提供 word/pdf 文字版简历。"])
