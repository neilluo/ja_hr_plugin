#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / documents.py
================================================

文档模型层：FileKind / ResumeDocument / ExtractionResult + kind 嗅探 + vendor
目录注入。P1 责任链重构中从 extract_text.py 原样搬入（判定逻辑与常量零改动），
供 shared/extraction/** 各梯队与 extract_text.py 门面共用。

kind 判定按「魔数优先、扩展名兜底」；FileKind.value 即契约枚举串
pdf|docx|doc|image|unknown，对外返回值始终是 str，不外泄枚举对象。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "FileKind",
    "ResumeDocument",
    "ExtractionResult",
    "sniff_kind",
    "nws",
    "ensure_vendor_path",
    "VENDOR_DIR",
]

# --------------------------------------------------------------------------- #
# vendor 目录注入
# --------------------------------------------------------------------------- #
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def ensure_vendor_path() -> None:
    """把 shared/vendor 放到 sys.path 最前面，保证用到的是 vendor 版本而非环境里
    可能存在的旧版/残缺版。幂等。"""
    p = str(VENDOR_DIR)
    if VENDOR_DIR.is_dir():
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
_PDF_EXTS = {".pdf"}
_ZIP_WORD_EXTS = {".docx", ".docm", ".dotx", ".dotm"}
_OLE_WORD_EXTS = {".doc", ".dot", ".wps", ".wpt"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif",
               ".webp", ".heic", ".heif", ".ico"}

_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_SIG = b"PK\x03\x04"
_ZIP_SIG_EMPTY = b"PK\x05\x06"
_PDF_SIG = b"%PDF"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SIG = b"\xff\xd8\xff"
_GIF_SIG = b"GIF8"
_BMP_SIG = b"BM"
_TIFF_LE = b"II*\x00"
_TIFF_BE = b"MM\x00*"
_RTF_SIG = b"{\\rt"


class FileKind(Enum):
    PDF = "pdf"
    DOCX = "docx"
    DOC = "doc"
    IMAGE = "image"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def nws(text: str) -> str:
    """去掉所有空白后的文本（护栏与 detect_scanned 统一用它计数）。"""
    return re.sub(r"\s+", "", text or "")


def _read_head(path: Path, n: int = 8) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def sniff_kind(path: str, head: Optional[bytes] = None) -> FileKind:
    """按「魔数优先、扩展名兜底」判定 kind。

    魔数优先很重要：客户经常把 docx 改名成 doc（或反之），只认扩展名会走错后端。
    返回值限定在契约枚举内：pdf|docx|doc|image|unknown。
    """
    p = Path(path)
    ext = p.suffix.lower()
    if head is None:
        head = _read_head(p, 8)
    # 1) 魔数
    if head.startswith(_PDF_SIG):
        return FileKind.PDF
    if head.startswith(_ZIP_SIG) or head.startswith(_ZIP_SIG_EMPTY):
        return FileKind.DOCX     # OOXML 家族；具体是不是 word 由 DocxZipExt 再校
    if head.startswith(_OLE_SIG):
        return FileKind.DOC
    if (head.startswith(_PNG_SIG) or head.startswith(_JPEG_SIG)
            or head.startswith(_GIF_SIG) or head.startswith(_BMP_SIG)
            or head.startswith(_TIFF_LE) or head.startswith(_TIFF_BE)):
        return FileKind.IMAGE
    if head.startswith(_RTF_SIG):
        return FileKind.UNKNOWN  # rtf 无枚举位；按纯文本兜底处理
    # 2) 扩展名
    if ext in _PDF_EXTS:
        return FileKind.PDF
    if ext in _ZIP_WORD_EXTS:
        return FileKind.DOCX
    if ext in _OLE_WORD_EXTS:
        return FileKind.DOC
    if ext in _IMAGE_EXTS:
        return FileKind.IMAGE
    return FileKind.UNKNOWN


# --------------------------------------------------------------------------- #
# 文档模型
# --------------------------------------------------------------------------- #
@dataclass
class ResumeDocument:
    """一份待提取文件的元信息 + 惰性字节缓存。

    前置检查（存在性/目录/stat/0 字节/md5）仍由门面 extract_text 负责，
    inspect() 只复刻旧实现里 try 块之前的 head 嗅探顺序。
    """

    path: Path
    kind: FileKind
    ext: str
    head: bytes
    size: int
    md5: str
    _data: Optional[bytes] = field(default=None, repr=False, compare=False)
    # chain 在逐级询问 can_handle 前把「已走过的梯队结果」挂在这里（同一 list 对象，
    # 边跑边 append）；VisionOcrExt 等后置梯队靠它判断"前序全部没拿到可用文本"。
    prior: List[Any] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def inspect(cls, path: Path, size: int, md5: str) -> "ResumeDocument":
        head = _read_head(path, 8)
        kind = sniff_kind(str(path), head)
        return cls(path=path, kind=kind, ext=path.suffix.lower(),
                   head=head, size=size, md5=md5)

    def data(self) -> bytes:
        if self._data is None:
            self._data = self.path.read_bytes()
        return self._data


@dataclass
class ExtractionResult:
    """单个梯队（或整条 chain 耗尽后终态）的提取结果。

    status 语义（与旧 _pdf_text 梯队出口一一对应）：
      "ok"            -> 本梯队赢，chain 停止
      "no_text_layer" -> 梯队跑通但 0 个非空白字符，chain 落下一级
      "error"         -> 梯队自身异常（已被梯队内捕获转成 note），chain 落下一级
    notes 是 backend 链路的可追溯记录；meta 承载 .doc 专用的解析元信息。
    """

    text: str
    npages: Optional[int]
    status: str
    backend: str
    notes: List[str] = field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None
