# -*- coding: utf-8 -*-
"""PDF 梯队①：vendor pypdf（backend=pypdf）。

**禁止** pdfplumber（依赖链含 cryptography 等二进制 wheel）。
PermissionError（加密且空口令解不开）必须原样上抛——旧实现即如此，
门面把它映射成 status=encrypted，且不落 JXA 梯队。
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from documents import ExtractionResult, FileKind, ResumeDocument
from documents import ensure_vendor_path, nws
from extraction.base import TextExtractor, normalize_pdf_text

__all__ = ["PypdfExt"]


def _pdf_pypdf(path: Path) -> Tuple[str, int]:
    ensure_vendor_path()
    from pypdf import PdfReader                      # vendor 目录优先

    with path.open("rb") as fh:
        reader = PdfReader(fh)
        if reader.is_encrypted:
            try:
                rc = reader.decrypt("")
            except Exception as e:
                raise PermissionError("PDF 已加密且空口令解不开: %s" % e)
            if not rc:
                raise PermissionError("PDF 已加密（空口令解密失败）")
        npages = len(reader.pages)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                parts.append("")
    return "\n".join(parts), npages


class PypdfExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        return doc.kind == FileKind.PDF

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        try:
            text, npages = _pdf_pypdf(doc.path)
            text = normalize_pdf_text(text)
        except PermissionError:
            raise
        except ImportError as e:
            return ExtractionResult(
                "", 0, "error", "none",
                ["vendor pypdf 不可用(%s: %s)" % (type(e).__name__, e)])
        except Exception as e:
            return ExtractionResult(
                "", 0, "error", "none",
                ["pypdf 失败(%s: %s)" % (type(e).__name__, e)])
        if nws(text):
            return ExtractionResult(text, npages, "ok", "pypdf")
        # npages 如实上报（P1 评审裁决①）：文本层是空的但页数已解析出来，
        # chain 会在终态/赢家里保留首个非零 npages，扫描件不再丢页数
        return ExtractionResult(
            "", npages, "no_text_layer", "none",
            ["pypdf 提取到 0 个非空白字符（可能无文本层）"])
