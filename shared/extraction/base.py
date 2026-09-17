# -*- coding: utf-8 -*-
"""梯队抽象基类 + 跨梯队共享的文本规整工具。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from documents import ExtractionResult, ResumeDocument

__all__ = ["TextExtractor", "normalize_pdf_text"]


class TextExtractor(ABC):
    """一个提取梯队。

    契约（与 ExtractorChain 的语义配套，见 chain.py）：
      can_handle(doc) -> bool     是否受理该文档
      extract(doc) -> ExtractionResult
          status="ok" 即赢；"no_text_layer"/"error" 表示本梯队没拿到可用文本，
          chain 会记录 notes 后落下一级。梯队**内部**可捕获的异常应转成
          status="error" 的结果；需要直达门面的异常（如 PermissionError=加密、
          容器解析失败）照常 raise。
    """

    @abstractmethod
    def can_handle(self, doc: ResumeDocument) -> bool:
        raise NotImplementedError

    @abstractmethod
    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        raise NotImplementedError


def normalize_pdf_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = text.replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
