# -*- coding: utf-8 -*-
"""PDF 梯队②：macOS 内置 JXA + Quartz/PDFKit（backend=pdfkit_jxa，仅 darwin）。

非 darwin / 无 osascript / 超时 / 非零退出码都在本梯队内转成 status="error"
的 note，与旧 _pdf_text 的异常出口逐条对应。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

from documents import ExtractionResult, FileKind, ResumeDocument, nws
from extraction.base import TextExtractor, normalize_pdf_text

__all__ = ["JxaExt"]

_JXA_TIMEOUT_S = 120
_JXA_SCRIPT = (
    'ObjC.import("Quartz");'
    "function run(argv){"
    "  var url = $.NSURL.fileURLWithPath(argv[0]);"
    "  var doc = $.PDFDocument.alloc.initWithURL(url);"
    "  if (doc.isNil()) { throw 'NIL_DOC'; }"
    "  var s = doc.string;"
    "  return s.isNil() ? '' : s.js;"
    "}"
)


def _pdf_jxa(path: Path) -> Tuple[str, int]:
    """macOS 内置 JXA + Quartz/PDFKit，零安装但仅 darwin 可用。"""
    if sys.platform != "darwin":
        raise RuntimeError("非 macOS，无 JXA/PDFKit 可用")
    if shutil.which("osascript") is None:
        raise RuntimeError("找不到 osascript")
    proc = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", _JXA_SCRIPT, str(path)],
        capture_output=True, timeout=_JXA_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError("osascript 退出码 %d: %s" % (
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:200]))
    text = proc.stdout.decode("utf-8", "replace")
    return text, text.count("\f") + (1 if text.strip() else 0)


class JxaExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        # 平台判定刻意留在 extract 内（旧实现在非 darwin 上也会记一条失败 note）
        return doc.kind == FileKind.PDF

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        try:
            text, npages = _pdf_jxa(doc.path)
            text = normalize_pdf_text(text)
        except subprocess.TimeoutExpired:
            return ExtractionResult(
                "", 0, "error", "none",
                ["JXA/PDFKit 超时(%ds)" % _JXA_TIMEOUT_S])
        except Exception as e:
            return ExtractionResult(
                "", 0, "error", "none",
                ["JXA/PDFKit 失败(%s: %s)" % (type(e).__name__, e)])
        if nws(text):
            return ExtractionResult(text, npages, "ok", "pdfkit_jxa")
        return ExtractionResult(
            "", 0, "no_text_layer", "none",
            ["JXA/PDFKit 也提取到 0 个非空白字符"])
