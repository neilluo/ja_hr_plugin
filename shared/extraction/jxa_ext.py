# -*- coding: utf-8 -*-
"""PDF 梯队②：macOS 内置 JXA + Quartz/PDFKit（backend=pdfkit_jxa，仅 darwin）。

非 darwin / 无 osascript / 超时 / 非零退出码都在本梯队内转成 status="error"
的 note，与旧 _pdf_text 的异常出口逐条对应。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

from documents import ExtractionResult, FileKind, ResumeDocument, nws
from extraction.base import TextExtractor, normalize_pdf_text

__all__ = ["JxaExt"]

_JXA_TIMEOUT_S = 120
# 脚本返回 JSON {pages, text}——pages 是 PDFKit 实际解析到的 doc.pageCount，
# 让**非 ok 结果**（无文本层）也能带回真实页数，不再硬编码 npages=0；
# 文本内容本身与旧版逐字一致（JSON 往返无损）。
_JXA_SCRIPT = (
    'ObjC.import("Quartz");'
    "function run(argv){"
    "  var url = $.NSURL.fileURLWithPath(argv[0]);"
    "  var doc = $.PDFDocument.alloc.initWithURL(url);"
    "  if (doc.isNil()) { throw 'NIL_DOC'; }"
    "  var np = 0;"
    "  try { np = doc.pageCount; } catch (e) { np = 0; }"
    "  var s = doc.string;"
    "  return JSON.stringify({pages: np, text: (s.isNil() ? '' : s.js)});"
    "}"
)


def _pdf_jxa(path: Path) -> Tuple[str, int, int]:
    """macOS 内置 JXA + Quartz/PDFKit，零安装但仅 darwin 可用。

    返回 (text, npages_by_formfeed, page_count)：
      - npages_by_formfeed 与旧实现同式（\\f 计数），**成功路径沿用它**，
        保证既有行为逐字节不变；
      - page_count 是 PDFKit 解析到的真实页数，供失败路径（no_text_layer）
        回填 npages 用；拿不到（旧输出格式/JSON 解析失败）为 0。
    """
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
    raw = proc.stdout.decode("utf-8", "replace")
    page_count = 0
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            text = str(payload.get("text") or "")
            page_count = int(payload.get("pages") or 0)
        else:
            text = raw
    except ValueError:
        text = raw            # 防御：非 JSON 输出按旧纯文本语义处理
    return text, text.count("\f") + (1 if text.strip() else 0), page_count


class JxaExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        # 平台判定刻意留在 extract 内（旧实现在非 darwin 上也会记一条失败 note）
        return doc.kind == FileKind.PDF

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        try:
            text, npages, page_count = _pdf_jxa(doc.path)
            text = normalize_pdf_text(text)
        except subprocess.TimeoutExpired:
            # 超时/异常路径拿不到任何解析结果 → npages 只能是 0（主控裁决：
            # 「改为返回 JXA 实际解析到的页数（拿不到才 0）」）
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
            "", page_count or 0, "no_text_layer", "none",
            ["JXA/PDFKit 也提取到 0 个非空白字符（文档实际解析到 %d 页）"
             % (page_count or 0)])
