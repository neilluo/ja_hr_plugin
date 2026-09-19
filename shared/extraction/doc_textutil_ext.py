# -*- coding: utf-8 -*-
""".doc 梯队：调用 macOS 自带 textutil 提取文本。

替代旧版 doc_piece_ext.py（368 行自写 OLE2/CFB + piece table 解析器）。
macOS 自带 `textutil -convert txt <file> -stdout` 原生支持 .doc，
无需自写容器解析；非 macOS 平台明确提示不支持。
"""

from __future__ import annotations

import platform
import subprocess
from typing import Any, Dict, List

from documents import ExtractionResult, FileKind, ResumeDocument
from extraction.base import TextExtractor

__all__ = ["DocTextutilExt"]

_IS_MAC = platform.system() == "Darwin"


class DocTextutilExt(TextExtractor):
    """macOS textutil .doc 文本提取器。"""

    BACKEND = "macos_textutil"

    def can_handle(self, doc: ResumeDocument) -> bool:
        return doc.kind == FileKind.DOC

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        if not _IS_MAC:
            return ExtractionResult(
                "", None, "error", "none",
                ["当前平台(%s)不支持 .doc 格式，请转换为 DOCX 后重试"
                 % platform.system()])

        notes: List[str] = []
        try:
            proc = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(doc.path)],
                capture_output=True, timeout=30)
        except FileNotFoundError:
            return ExtractionResult(
                "", None, "error", "none",
                ["textutil 命令不存在（非 macOS？），不支持 .doc 格式"])
        except subprocess.TimeoutExpired:
            return ExtractionResult(
                "", None, "error", "none",
                ["textutil 转换超时(30s)，文件可能损坏或过大"])

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            # 加密文档 textutil 会返回非零退出码
            if "encrypted" in stderr.lower() or "password" in stderr.lower():
                raise PermissionError("文档已加密(textutil 无法解密)")
            return ExtractionResult(
                "", None, "error", "none",
                ["textutil 退出码 %d: %s" % (proc.returncode, stderr or "(无 stderr)")])

        text = proc.stdout.decode("utf-8", "replace")
        if text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            text = "\n".join(
                ln for ln in (l.strip() for l in text.splitlines()) if ln)
            text = text.strip()

        meta: Dict[str, Any] = {"doc_path": "textutil", "platform": "macOS"}
        return ExtractionResult(text, None, "ok", self.BACKEND, notes, meta=meta)
