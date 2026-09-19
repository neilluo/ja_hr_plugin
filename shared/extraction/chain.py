# -*- coding: utf-8 -*-
"""ExtractorChain：按注册顺序问 can_handle，第一个 extract 成功的赢。

语义：
  - status=="ok"          -> 赢；已走过的每一级的 notes 前插进赢家 notes，
                             backend 链路可追溯；
  - status!="ok"          -> 本级 notes 累积，落下一级；
  - extract 抛异常         -> chain 不捕获，穿透给调用方（门面）——复刻旧实现里
                             docx/doc 容器失败直达通用 except、pdf 加密直达
                             encrypted 的路径；pdf 梯队"异常转下一级"由
                             PypdfExt/JxaExt 内部自捕完成；
  - 全部走完无人赢（耗尽） -> 复刻旧终态
                             ("", "none", 0, notes, "no_text_layer" if ran_without_exc else "error")：
                             任一级"跑通但 0 字符"即 no_text_layer，否则 error。

增量语义（两条）：
  1. **gate（文本层护栏前置）**：调用方可传 gate(text, kind)->bool（门面传
     detect_scanned）。梯队返回 ok 但 gate 判定文本不可用（扫描件只剩水印/
     重复串/数字极少）时，把该结果**降级为 no_text_layer** 并落下一级——
     这是 Vision OCR 能接手「pypdf 提出 671 字符水印」这类假 ok 的前提；
     没有 gate 时行为不变。
  2. **npages 保留**：终态与赢家结果的 npages 不再被后梯队的
     硬编码 0 清掉——取所有已试梯队里首个非零 npages（如 pypdf 已解析出
     页数但文本层是水印时，最终 pages 仍是真实页数）。

doc.prior：每级 can_handle 之前，已走过的梯队结果（含被 gate 降级的）都在
doc.prior 里（同一 list，边跑边 append），后置梯队（VisionOcrExt）据此判断
"前序全部没拿到可用文本"。
"""

from __future__ import annotations

from typing import Callable, List, Optional

from documents import ExtractionResult, ResumeDocument, nws
from extraction.base import TextExtractor

__all__ = ["ExtractorChain"]


class ExtractorChain(object):

    def __init__(self, extractors: Optional[List[TextExtractor]] = None):
        self.extractors: List[TextExtractor] = list(extractors or [])

    def register(self, extractor: TextExtractor) -> None:
        self.extractors.append(extractor)

    def run(self, doc: ResumeDocument,
            gate: Optional[Callable[[str, str], bool]] = None) -> ExtractionResult:
        # 先整体读一次字节：复刻旧实现在 try 块内对**所有** kind（含不消费 bytes
        # 的 image/pdf）先行 read_bytes 的行为，读失败的异常映射保持一致
        doc.data()
        notes: List[str] = []
        tried: List[ExtractionResult] = []
        doc.prior = tried
        for ext in self.extractors:
            if not ext.can_handle(doc):
                continue
            result = ext.extract(doc)
            if result.status == "ok" and gate is not None \
                    and gate(result.text or "", doc.kind.value):
                body = nws(result.text or "")
                digits = sum(1 for c in body if c.isdigit())
                result.status = "no_text_layer"
                result.notes = list(result.notes) + [
                    "文本层护栏判定不可用（非空白 %d 字符、数字 %d 个，"
                    "疑似扫描件/水印），不作为可用结果，落下一级梯队"
                    % (len(body), digits)]
            if result.status == "ok":
                if not result.npages:
                    result.npages = _first_nonzero_npages(tried)
                result.notes = notes + result.notes
                return result
            notes = notes + result.notes
            tried.append(result)
        status = "error"
        for r in tried:
            if r.status == "no_text_layer":
                status = "no_text_layer"
                break
        npages = None
        if tried:
            npages = _first_nonzero_npages(tried, default=tried[-1].npages)
        return ExtractionResult("", npages, status, "none", notes)


def _first_nonzero_npages(tried: List[ExtractionResult],
                          default: Optional[int] = None) -> Optional[int]:
    for r in tried:
        if r.npages:
            return r.npages
    return default
