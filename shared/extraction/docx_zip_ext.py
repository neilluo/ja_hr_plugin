# -*- coding: utf-8 -*-
""".docx 梯队：stdlib zipfile + 单趟正则解析 word/document.xml（backend=stdlib_zip）。

**不要**用 macOS textutil 处理 docx：会丢表格内容。
容器非法/缺 document.xml 抛 ValueError，由门面通用 except 映射成 status=error。
"""

from __future__ import annotations

import html
import io
import re
from typing import List

from documents import ExtractionResult, FileKind, ResumeDocument
from extraction.base import TextExtractor

__all__ = ["DocxZipExt"]

_DOCX_TOKEN_RE = re.compile(
    r"<w:t(?:\s[^>]*)?>(.*?)</w:t>"        # 1: 文本内容
    r"|</w:p>"                             # 段落结束 -> \n
    r"|<w:p(?:\s[^>]*)?/>"                 # 自闭合空段落 -> \n
    r"|<w:tab\s*/>"                        # 制表位 -> \t
    r"|<w:br(?:\s[^>]*)?/>"                # 换行/分页 -> \n
    r"|<w:cr\s*/>",                        # 回车 -> \n
    re.S,
)


def _docx_text(data: bytes) -> str:
    import zipfile

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("不是合法的 zip/OOXML 容器: %s" % e)
    with zf:
        names = zf.namelist()
        if "word/document.xml" not in names:
            raise ValueError(
                "zip 容器里没有 word/document.xml（可能是 xlsx/pptx 改名而来）；"
                "实际条目 %d 个，前 8 个: %s" % (len(names), names[:8])
            )
        xml = zf.read("word/document.xml").decode("utf-8", "replace")

    out: List[str] = []
    for m in _DOCX_TOKEN_RE.finditer(xml):
        if m.group(1) is not None:
            out.append(m.group(1))
        else:
            tok = m.group(0)
            out.append("\t" if tok.startswith("<w:tab") else "\n")
    text = html.unescape("".join(out))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


class DocxZipExt(TextExtractor):
    def can_handle(self, doc: ResumeDocument) -> bool:
        return doc.kind == FileKind.DOCX

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        text = _docx_text(doc.data())
        return ExtractionResult(text, None, "ok", "stdlib_zip")
