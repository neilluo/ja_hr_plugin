#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / extract_text.py
===================================================

多格式文本提取层。**零第三方 pip 依赖**：只用 python 标准库 + `shared/vendor/`
里 vendor 进来的纯 python 库（olefile / pypdf / typing_extensions，见
`shared/vendor/VENDOR_MANIFEST.txt`）。

对外契约（构建契约 §3.1，签名已冻结，不得改动）
------------------------------------------------
    extract_text(path: str) -> dict
        {"path": str, "kind": "pdf|docx|doc|image|unknown",
         "text": str, "chars": int, "elapsed_ms": int,
         "status": "ok|no_text_layer|garbled|encrypted|unsupported|error",
         "backend": "pypdf|pdfkit_jxa|stdlib_zip|ole_stdlib|ole_vendored|none",
         "md5": str, "size": int, "error": str | None}
    detect_scanned(text: str, kind: str) -> bool

本实现额外附带的**只增不改**字段（W-C 可忽略，不影响签名兼容）
--------------------------------------------------------------
    warning : str | None   —— 人类可读的风险提示（契约 §0/D11 要求「失败可见」）
    checks  : dict         —— 护栏量化指标（字符数下限 / CJK 占比 / 重复度 / 数字数），
                              上层要转人工时可直接引用
    pages   : int | None   —— pdf 页数
    doc     : dict | None  —— .doc 专用：fComplex、piece 数、Table 流名、走的是
                              piece table 还是 fcMin/fcMac 直读

后端梯队（契约 D9）
------------------
    .docx  -> stdlib zipfile + 正则解析 word/document.xml      backend=stdlib_zip
              （**不要**用 macOS textutil 处理 docx：实测丢表格内容，
                10 份 docx 只提出 21,141 字符，zipfile 路径 142,366 字符）
    .doc   -> OLE2/CFB 容器：vendor olefile（backend=ole_vendored）
              容器读取失败时退回自带 stdlib CFB 读取器（backend=ole_stdlib）
              正文按 MS-DOC 规范的 piece table（FIB.fcClx -> Clx -> Pcdt ->
              PlcPcd -> PCD）解析；解析不出来才退回 fcMin/fcMac 直读并打 warning。
    .pdf   -> ① vendor pypdf（backend=pypdf）
              ② macOS 内置 JXA/PDFKit（backend=pdfkit_jxa，仅 darwin）
              ③ 都失败 -> status=error
              **禁止** pdfplumber（依赖链含 cryptography 等二进制 wheel）。
    图片    -> 无文本层可提，status=no_text_layer, backend=none（契约 D11：
              本期不做 OCR，不硬造字段，进 ❌ 清单建议提供文字版）

.doc 的规范外风险与护栏
----------------------
实测这批 20 份 WPS 产出的 .doc，FIB flags（偏移 0x0A）bit 0x0004 = fComplex
**全部为 True**，即按 MS-DOC 规范它们是 piece table 分段存储，「fcMin/fcMac
直读 + UTF-16-LE 切片」属规范外路径（这批文件恰好只有 1 个 piece、文本连续，
所以侥幸可用）。本模块因此：
  1. 优先走**规范内**的 piece table 路径（实测 20/20 与 macOS textutil 输出
     逐字符一致，非空白字符相似度 1.0000，0 处差异）；
  2. 直读只作为兜底，一旦启用就写 warning；
  3. 无论走哪条路，返回前都过一遍护栏：字符数下限、CJK 占比、乱码字符占比。
     护栏不过 -> status=garbled，交给上层转人工。**不假装 100% 覆盖。**

兼容性
------
D10：全程 pathlib，不硬编码路径分隔符；语法兼容 python 3.8+（不用 match、
不用 `X | None` 运行时标注，只用 typing.Optional）。已在 3.9.6 与 3.14.0 实测。
"""

from __future__ import annotations  # noqa: F404  (仅影响注解求值，3.7+ 可用)

import hashlib
import html
import io
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "extract_text",
    "detect_scanned",
    "text_quality",
    "sniff_kind",
    "VENDOR_DIR",
]

# --------------------------------------------------------------------------- #
# vendor 目录注入
# --------------------------------------------------------------------------- #
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def _ensure_vendor_path() -> None:
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
_PLAIN_EXTS = {".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".log",
               ".json", ".html", ".htm", ".xml", ".yaml", ".yml"}
_MARKUP_EXTS = {".html", ".htm", ".xml"}

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

# 护栏阈值（实测标定，见模块 docstring）
DOC_MIN_CHARS = 100        # .doc 非空白字符下限；低于此判 garbled
DOC_WARN_CHARS = 300       # 低于此但高于下限时只打 warning
MIN_CJK_RATIO = 0.15       # CJK 占比下限（配合拉丁占比一起判，避免误杀英文简历）
MIN_LATIN_RATIO = 0.50     # 非 CJK 文档要求的拉丁字母占比
MAX_GARBAGE_RATIO = 0.10   # U+FFFD / 私用区 / 控制字符 占比上限

# detect_scanned 阈值（实测标定：3 份扫描件 80~1175 字符、0 个数字、
# 唯一字符占比 <=0.13；28 份有效简历最少 966 字符但数字 >=29 个）
SCAN_MAX_CHARS = 1500      # 非空白字符数超过此值一律不判扫描件
SCAN_MIN_DIGITS = 12       # 数字字符少于此值才算「数字极少」
# 主判据是 unique_ratio：水印是「同一串字重复 N 遍」，唯一字符占比会掉到 0.01~0.13。
# top_line / top_char 只作宽松备份——阈值必须放得很松，否则正常短文本
# （50 字纯文本简历里手机号含 5 个 0）会被误判成水印，实测踩过。
SCAN_MIN_UNIQUE_RATIO = 0.15    # 唯一字符占比低于此值 = 重复串（水印）
SCAN_MAX_TOP_LINE_RATIO = 0.30  # 单行重复占比高于此值 = 重复串（需 >= 8 行才判）
SCAN_MIN_LINES_FOR_TOP_LINE = 8
SCAN_MAX_TOP_CHAR_RATIO = 0.30  # 单字符占比高于此值 = 重复串（需 >= 60 个文字字符才判）
SCAN_MIN_LETTERS_FOR_TOP_CHAR = 60
SCAN_WATERMARK_HINTS = ("招聘专用", "水印", "机密", "内部资料", "仅供", "样本",
                        "SAMPLE", "CONFIDENTIAL", "WATERMARK", "禁止复制")

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


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _nws(text: str) -> str:
    """去掉所有空白后的文本（护栏与 detect_scanned 统一用它计数）。"""
    return re.sub(r"\s+", "", text or "")


def _file_md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            blk = fh.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


def _read_head(path: Path, n: int = 8) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def sniff_kind(path: str, head: Optional[bytes] = None) -> str:
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
        return "pdf"
    if head.startswith(_ZIP_SIG) or head.startswith(_ZIP_SIG_EMPTY):
        return "docx"          # OOXML 家族；具体是不是 word 由 _docx_text 再校
    if head.startswith(_OLE_SIG):
        return "doc"
    if (head.startswith(_PNG_SIG) or head.startswith(_JPEG_SIG)
            or head.startswith(_GIF_SIG) or head.startswith(_BMP_SIG)
            or head.startswith(_TIFF_LE) or head.startswith(_TIFF_BE)):
        return "image"
    if head.startswith(_RTF_SIG):
        return "unknown"       # rtf 无枚举位；按纯文本兜底处理
    # 2) 扩展名
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _ZIP_WORD_EXTS:
        return "docx"
    if ext in _OLE_WORD_EXTS:
        return "doc"
    if ext in _IMAGE_EXTS:
        return "image"
    return "unknown"


def _cjk_count(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff"
               or "\u3400" <= c <= "\u4dbf")


def _garbage_count(text: str) -> int:
    """U+FFFD 替换符 + 私用区 + 残留控制字符。"""
    n = 0
    for c in text:
        o = ord(c)
        if o == 0xFFFD or 0xE000 <= o <= 0xF8FF or (o < 32 and c not in "\n\t\r"):
            n += 1
    return n


def text_quality(text: str, kind: str) -> Dict[str, Any]:
    """护栏量化指标 + 判定。返回 dict（extract_text 会原样放进 result["checks"]）。

    判据（契约要求的三项 + 一项乱码字符占比）：
      chars_floor_ok : 非空白字符数 >= 字符数下限
      cjk_ok         : CJK 占比达标，或（非中文文档）拉丁字母占比达标
      garbage_ok     : 乱码字符占比 <= MAX_GARBAGE_RATIO

    字符数下限只对 doc/docx 生效（floor_enforced）：.doc 结果过短意味着 piece
    table / 直读漏了内容，是真乱码风险；而 pdf 结果过短要么是扫描件（由
    detect_scanned 判成 no_text_layer），要么原文本来就短，不该算乱码。
    """
    body = _nws(text)
    n = len(body)
    cjk = _cjk_count(body)
    latin = sum(1 for c in body if ("a" <= c <= "z") or ("A" <= c <= "Z"))
    garbage = _garbage_count(body)
    digits = sum(1 for c in body if c.isdigit())
    uniq = len(set(body))
    cjk_ratio = (cjk / n) if n else 0.0
    latin_ratio = (latin / n) if n else 0.0
    garbage_ratio = (garbage / n) if n else 0.0
    unique_ratio = (uniq / n) if n else 0.0
    floor_enforced = kind in ("doc", "docx")
    floor = DOC_MIN_CHARS if floor_enforced else 0
    checks = {
        "chars": n,
        "chars_floor": floor,
        "chars_floor_ok": n >= floor,
        "chars_low_warn": n < DOC_WARN_CHARS,
        "cjk": cjk,
        "cjk_ratio": round(cjk_ratio, 4),
        "latin_ratio": round(latin_ratio, 4),
        "cjk_ok": (cjk_ratio >= MIN_CJK_RATIO) or (latin_ratio >= MIN_LATIN_RATIO),
        "garbage": garbage,
        "garbage_ratio": round(garbage_ratio, 4),
        "garbage_ok": garbage_ratio <= MAX_GARBAGE_RATIO,
        "digits": digits,
        "unique_ratio": round(unique_ratio, 4),
        "kind": kind,
    }
    checks["passed"] = bool(
        checks["chars_floor_ok"] and checks["cjk_ok"] and checks["garbage_ok"]
    )
    return checks


def detect_scanned(text: str, kind: str) -> bool:
    """判断「拿到了文本但其实等于没拿到」——扫描件 / 纯图片 / 只剩水印。

    判据是**三条同时成立**（契约要求，单条都容易误杀）：
      ① 字符数低于阈值：非空白字符 < SCAN_MAX_CHARS(1500)
      ② 水印 / 重复串占比高：唯一字符占比极低，或单行/单字符高度重复，
         或命中已知水印词
      ③ 数字字符极少：< SCAN_MIN_DIGITS(12)
         —— 这条是防误杀的关键：真实简历/JD 必含手机号、年份、日期，
            实测最短的有效简历（周彦淇 966 字符）数字也有 29 个，
            而 3 份扫描件数字都是 0 个。

    kind == "image" 时直接返回 True：图片没有文本层可提，按契约 D11 归为
    no_text_layer（本期不做 OCR）。
    """
    if kind == "image":
        return True
    body = _nws(text)
    n = len(body)
    if n == 0:
        return True
    # ① 字符数阈值
    if n >= SCAN_MAX_CHARS:
        return False
    digits = sum(1 for c in body if c.isdigit())
    # ③ 数字极少
    if digits >= SCAN_MIN_DIGITS:
        return False
    # ② 重复度 / 水印
    unique_ratio = len(set(body)) / n
    # 重复度只统计「文字类」字符：`：` `|` 这类标点在正常短文本里也会出现 4~5 次，
    # 若把它们算进去，50 字的纯文本简历会被误判成水印（实测踩过）
    letters = [c for c in body if c.isalnum()]
    ln_n = len(letters)
    top_char_ratio = 0.0
    if ln_n >= SCAN_MIN_LETTERS_FOR_TOP_CHAR:
        counts: Dict[str, int] = {}
        for c in letters:
            counts[c] = counts.get(c, 0) + 1
        top_char_ratio = max(counts.values()) / ln_n
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    top_line_ratio = 0.0
    if len(lines) >= SCAN_MIN_LINES_FOR_TOP_LINE:
        top_line_ratio = max(lines.count(ln) for ln in set(lines)) / len(lines)
    repetitive = (unique_ratio < SCAN_MIN_UNIQUE_RATIO
                  or top_line_ratio > SCAN_MAX_TOP_LINE_RATIO
                  or top_char_ratio > SCAN_MAX_TOP_CHAR_RATIO)
    watermark = any(h in (text or "") for h in SCAN_WATERMARK_HINTS)
    return bool(repetitive or watermark)


# --------------------------------------------------------------------------- #
# .docx —— stdlib zipfile + 单趟正则
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# .doc —— OLE2/CFB 容器 + MS-DOC piece table
# --------------------------------------------------------------------------- #
class MiniOLE(object):
    """~120 行的 stdlib OLE2(CFB) 读取器，仅用于 vendor olefile 不可用时兜底。

    实测与前序调研结论一致：对本批 20 份 .doc，读出的 WordDocument 流与
    olefile 逐字节相同（20/20）。
    """

    ENDOFCHAIN = 0xFFFFFFFE
    FREESECT = 0xFFFFFFFF

    def __init__(self, data: bytes):
        if not data.startswith(_OLE_SIG):
            raise ValueError("不是 OLE2 复合文档（魔数不符）")
        if len(data) < 0x200:
            raise ValueError("文件过短，OLE 头不完整（%d 字节）" % len(data))
        self.d = data
        self.ss = 1 << struct.unpack("<H", data[0x1E:0x20])[0]      # sector size
        self.mss = 1 << struct.unpack("<H", data[0x20:0x22])[0]     # mini sector
        self.cutoff = struct.unpack("<I", data[0x38:0x3C])[0]       # mini cutoff
        num_fat = struct.unpack("<I", data[0x2C:0x30])[0]
        dir_start = struct.unpack("<I", data[0x30:0x34])[0]
        minifat_start = struct.unpack("<I", data[0x3C:0x40])[0]
        difat_start = struct.unpack("<I", data[0x44:0x48])[0]
        difat_count = struct.unpack("<I", data[0x48:0x4C])[0]
        if self.ss < 128 or self.ss > (1 << 16):
            raise ValueError("非法 sector size: %d" % self.ss)

        difat = list(struct.unpack("<109I", data[0x4C:0x200]))
        sec = difat_start
        for _ in range(min(difat_count, 10000)):
            if sec in (self.ENDOFCHAIN, self.FREESECT) or sec >= 1 << 24:
                break
            blk = self._sector(sec)
            entries = struct.unpack("<%dI" % (self.ss // 4), blk)
            difat.extend(entries[:-1])
            sec = entries[-1]

        self.fat: List[int] = []
        for s in difat[:num_fat]:
            if s in (self.ENDOFCHAIN, self.FREESECT):
                continue
            self.fat.extend(struct.unpack("<%dI" % (self.ss // 4), self._sector(s)))

        self.dirents: List[Tuple[str, int, int, int]] = []
        for sec in self._chain(dir_start):
            blk = self._sector(sec)
            for i in range(0, self.ss, 128):
                e = blk[i:i + 128]
                if len(e) < 128:
                    break
                nlen = struct.unpack("<H", e[0x40:0x42])[0]
                name = e[:max(0, nlen - 2)].decode("utf-16-le", "ignore")
                etype = e[0x42]
                start = struct.unpack("<I", e[0x74:0x78])[0]
                size = struct.unpack("<Q", e[0x78:0x80])[0]
                self.dirents.append((name, etype, start, size))
        if not self.dirents:
            raise ValueError("OLE 目录为空")

        root = self.dirents[0]
        self.ministream = self._read_regular(root[2], root[3]) if root[3] else b""
        self.minifat: List[int] = []
        for sec in self._chain(minifat_start):
            self.minifat.extend(
                struct.unpack("<%dI" % (self.ss // 4), self._sector(sec)))

    def _sector(self, sec: int) -> bytes:
        off = (sec + 1) * self.ss
        blk = self.d[off:off + self.ss]
        if len(blk) < self.ss:
            blk = blk + b"\x00" * (self.ss - len(blk))
        return blk

    def _chain(self, start: int):
        sec = start
        seen = set()
        limit = len(self.fat) + 16
        while (sec not in (self.ENDOFCHAIN, self.FREESECT)
               and sec < len(self.fat) and sec not in seen and len(seen) < limit):
            seen.add(sec)
            yield sec
            sec = self.fat[sec]

    def _read_regular(self, start: int, size: int) -> bytes:
        out = b"".join(self._sector(s) for s in self._chain(start))
        return out[:size]

    def stream_names(self) -> List[str]:
        return [nm for nm, et, _s, _z in self.dirents if et == 2]

    def open_stream(self, name: str) -> bytes:
        for nm, etype, start, size in self.dirents:
            if nm == name and etype == 2:
                if size < self.cutoff:
                    out = b""
                    sec = start
                    seen = set()
                    while (sec not in (self.ENDOFCHAIN, self.FREESECT)
                           and sec < len(self.minifat) and sec not in seen):
                        seen.add(sec)
                        out += self.ministream[sec * self.mss:(sec + 1) * self.mss]
                        sec = self.minifat[sec]
                    return out[:size]
                return self._read_regular(start, size)
        raise KeyError("流 %r 不存在（现有流: %s）" % (name, self.stream_names()))


def _read_ole_streams(data: bytes) -> Tuple[Dict[str, bytes], str]:
    """读出 OLE 容器里的全部顶层流。返回 (streams, backend)。"""
    _ensure_vendor_path()
    try:
        import olefile  # vendor 目录优先
    except Exception as e:                                  # pragma: no cover
        olefile = None
        vendor_err = "%s: %s" % (type(e).__name__, e)
    else:
        vendor_err = ""
    if olefile is not None:
        try:
            o = olefile.OleFileIO(io.BytesIO(data))
            try:
                streams = {}
                for entry in o.listdir():
                    if len(entry) != 1:
                        continue
                    try:
                        streams[entry[0]] = o.openstream(entry).read()
                    except Exception:
                        continue
                if "WordDocument" not in streams:
                    raise KeyError("olefile 未找到 WordDocument 流")
                return streams, "ole_vendored"
            finally:
                o.close()
        except Exception as e:
            vendor_err = "olefile 失败(%s: %s)，改用 stdlib CFB 读取器" % (
                type(e).__name__, e)
    # 兜底：自带 stdlib CFB 读取器
    try:
        mini = MiniOLE(data)
        streams = {}
        for nm in mini.stream_names():
            try:
                streams[nm] = mini.open_stream(nm)
            except Exception:
                continue
        if "WordDocument" not in streams:
            raise KeyError("MiniOLE 未找到 WordDocument 流")
        if vendor_err:
            streams["__vendor_note__"] = vendor_err.encode("utf-8")
        return streams, "ole_stdlib"
    except Exception as e:
        raise ValueError("OLE 容器解析失败: %s | %s" % (
            e, vendor_err or "vendor olefile 不可用"))


# MS-DOC: 8-bit 压缩 piece 用的 CP1252 特殊映射（0x80-0x9F 中非 ISO-8859-1 的部分）
_CP1252_MAP = {
    0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020,
    0x87: 0x2021, 0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039,
    0x8C: 0x0152, 0x91: 0x2018, 0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D,
    0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC, 0x99: 0x2122,
    0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153, 0x9F: 0x0178,
}


def _decode_piece(raw: bytes, compressed: bool) -> str:
    if compressed:
        out = []
        for b in bytearray(raw):
            out.append(chr(_CP1252_MAP.get(b, b)))
        return "".join(out)
    return raw.decode("utf-16-le", errors="replace")


def _clean_word_text(text: str) -> str:
    """.doc 正文清洗。

    \x07（表格单元格/行结束符）-> \n，\r 与 \x0b（软换行）-> \n，
    去掉其余控制字符（**保留** \n 和 \t —— 前序实验里
    `re.sub(r"[\\x00-\\x06\\x08-\\x1f]", "", t)` 把刚生成的 \\n 也删掉了，
    导致字符数被低估 3.6%，进而误判成「textutil 比我们多提 3.6%」）。
    """
    text = text.replace("\x07", "\n").replace("\r", "\n").replace("\x0b", "\n")
    text = re.sub(r"[\x00-\x06\x08\x0c-\x1f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _doc_piece_table_text(wd: bytes, streams: Dict[str, bytes]) -> Tuple[str, Dict[str, Any]]:
    """按 MS-DOC 规范走 piece table：FIB.fcClx -> Clx -> Pcdt -> PlcPcd -> PCD。

    这是规范内路径；fComplex=True 的文档必须走这里才保证不漏内容。
    """
    if len(wd) < 0x1AA + 8:
        raise ValueError("WordDocument 流过短（%d 字节），无法读 FIB" % len(wd))
    flags = struct.unpack("<H", wd[0x0A:0x0C])[0]
    f_complex = bool(flags & 0x0004)
    f_which_tbl = bool(flags & 0x0200)
    fc_clx, lcb_clx = struct.unpack("<II", wd[0x01A2:0x01AA])

    preferred = "1Table" if f_which_tbl else "0Table"
    order = [preferred] + [n for n in ("0Table", "1Table") if n != preferred]
    last_err: Optional[str] = None
    for tname in order:
        tbl = streams.get(tname)
        if not tbl:
            last_err = "缺少 %s 流" % tname
            continue
        if lcb_clx <= 0 or fc_clx + lcb_clx > len(tbl):
            last_err = "fcClx/lcbClx 越界(%d/%d, 流长 %d)" % (fc_clx, lcb_clx, len(tbl))
            continue
        clx = tbl[fc_clx:fc_clx + lcb_clx]
        # 跳过前导 Prc（clxt == 0x01）数组，定位 Pcdt（clxt == 0x02）
        i = 0
        ok = True
        while i < len(clx) and clx[i] == 0x01:
            if i + 3 > len(clx):
                ok = False
                break
            cb = struct.unpack("<H", clx[i + 1:i + 3])[0]
            i += 3 + cb
        if not ok or i >= len(clx) or clx[i] != 0x02:
            last_err = "Clx 中未找到 Pcdt（偏移 %d / 共 %d 字节）" % (i, len(clx))
            continue
        lcb = struct.unpack("<I", clx[i + 1:i + 5])[0]
        plc = clx[i + 5:i + 5 + lcb]
        if lcb < 16 or len(plc) < lcb:
            last_err = "PlcPcd 长度非法(lcb=%d, 实得 %d)" % (lcb, len(plc))
            continue
        n = (lcb - 4) // 12
        if n <= 0 or 4 * (n + 1) + 8 * n > len(plc):
            last_err = "piece 数量非法(n=%d, plc=%d)" % (n, len(plc))
            continue
        cps = struct.unpack("<%dI" % (n + 1), plc[:4 * (n + 1)])
        pieces: List[str] = []
        pcd_base = 4 * (n + 1)
        bad = False
        for k in range(n):
            pcd = plc[pcd_base + 8 * k: pcd_base + 8 * k + 8]
            fc_raw = struct.unpack("<I", pcd[2:6])[0]
            compressed = bool(fc_raw & 0x40000000)
            fc = fc_raw & 0x3FFFFFFF
            if compressed:
                fc //= 2
            cch = cps[k + 1] - cps[k]
            if cch < 0 or cch > len(wd):
                bad = True
                break
            nbytes = cch if compressed else cch * 2
            if fc + nbytes > len(wd):
                bad = True
                break
            pieces.append(_decode_piece(wd[fc:fc + nbytes], compressed))
        if bad:
            last_err = "PCD 的 fc/cch 越界（piece %d）" % k
            continue
        meta = {
            "fComplex": f_complex,
            "fWhichTblStm": f_which_tbl,
            "table_stream": tname,
            "pieces": n,
            "fcClx": fc_clx,
            "lcbClx": lcb_clx,
            "cp_max": cps[-1],
            "doc_path": "piece_table",
        }
        return "".join(pieces), meta
    raise ValueError("piece table 解析失败: %s" % (last_err or "未知原因"))


def _doc_direct_text(wd: bytes) -> Tuple[str, Dict[str, Any]]:
    """规范外兜底：fcMin(0x18)/fcMac(0x1C) 直读 + UTF-16-LE 解码。

    仅当文本恰好连续（单 piece）时才正确，属于侥幸路径，必须打 warning。
    """
    fc_min, fc_mac = struct.unpack("<II", wd[0x18:0x20])
    if fc_mac <= fc_min or fc_mac > len(wd):
        raise ValueError("fcMin/fcMac 非法(%d/%d, 流长 %d)" % (fc_min, fc_mac, len(wd)))
    flags = struct.unpack("<H", wd[0x0A:0x0C])[0]
    meta = {
        "fComplex": bool(flags & 0x0004),
        "fWhichTblStm": bool(flags & 0x0200),
        "table_stream": None,
        "pieces": None,
        "fcMin": fc_min,
        "fcMac": fc_mac,
        "doc_path": "fcMin_fcMac_direct",
    }
    return wd[fc_min:fc_mac].decode("utf-16-le", errors="replace"), meta


def _doc_text(data: bytes) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
    """返回 (text, backend, doc_meta, warning)。"""
    streams, backend = _read_ole_streams(data)
    warn_bits: List[str] = []
    note = streams.pop("__vendor_note__", None)
    if note:
        warn_bits.append(note.decode("utf-8", "replace"))
    if any(k in streams for k in ("EncryptionInfo", "EncryptedPackage")):
        raise PermissionError("文档已加密（存在 EncryptionInfo/EncryptedPackage 流）")
    wd = streams["WordDocument"]
    try:
        raw, meta = _doc_piece_table_text(wd, streams)
    except Exception as e:
        raw, meta = _doc_direct_text(wd)
        warn_bits.append(
            "piece table 解析失败(%s: %s)，退回 fcMin/fcMac 直读——这是 MS-DOC "
            "规范外路径，仅当正文连续时正确，可能漏内容" % (type(e).__name__, e))
    else:
        if meta.get("fComplex"):
            warn_bits.append(
                "FIB fComplex=True（piece table 分段存储），已按规范解析 piece "
                "table（pieces=%s）；该标记只说明存储方式，不代表提取失败"
                % meta.get("pieces"))
    return _clean_word_text(raw), backend, meta, ("; ".join(warn_bits) or None)


# --------------------------------------------------------------------------- #
# .pdf —— pypdf（vendor） -> macOS JXA/PDFKit -> error
# --------------------------------------------------------------------------- #
def _pdf_pypdf(path: Path) -> Tuple[str, int]:
    _ensure_vendor_path()
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


def _normalize_pdf_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = text.replace("\f", "\n").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_text(path: Path) -> Tuple[str, str, int, List[str], str]:
    """返回 (text, backend, pages, tried_notes, pdf_status)。按 D9 梯队 fallback。

    pdf_status: "ok"（拿到文本）| "no_text_layer"（梯队跑通但 0 字符，
    典型是纯扫描/纯图片 PDF）| "error"（两条梯队都抛异常）。
    """
    notes: List[str] = []
    _ensure_vendor_path()
    ran_without_exc = 0
    try:
        text, npages = _pdf_pypdf(path)
        text = _normalize_pdf_text(text)
        ran_without_exc += 1
        if _nws(text):
            return text, "pypdf", npages, notes, "ok"
        notes.append("pypdf 提取到 0 个非空白字符（可能无文本层）")
    except PermissionError:
        raise
    except ImportError as e:
        notes.append("vendor pypdf 不可用(%s: %s)" % (type(e).__name__, e))
    except Exception as e:
        notes.append("pypdf 失败(%s: %s)" % (type(e).__name__, e))
    # 第二梯队：macOS JXA / PDFKit
    try:
        text, npages = _pdf_jxa(path)
        text = _normalize_pdf_text(text)
        ran_without_exc += 1
        if _nws(text):
            return text, "pdfkit_jxa", npages, notes, "ok"
        notes.append("JXA/PDFKit 也提取到 0 个非空白字符")
    except subprocess.TimeoutExpired:
        notes.append("JXA/PDFKit 超时(%ds)" % _JXA_TIMEOUT_S)
    except Exception as e:
        notes.append("JXA/PDFKit 失败(%s: %s)" % (type(e).__name__, e))
    return "", "none", 0, notes, ("no_text_layer" if ran_without_exc else "error")


# --------------------------------------------------------------------------- #
# 纯文本兜底（扩展名不在枚举里，但内容其实是文本）
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]{0,4000}>")


def _plain_text(data: bytes, ext: str) -> Tuple[str, str]:
    """返回 (text, note)。编码依次试 utf-8-sig / utf-8 / gbk / latin-1。"""
    text = None
    used = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "big5", "latin-1"):
        try:
            text = data.decode(enc)
            used = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法按 utf-8/gbk/big5/latin-1 解码，判定为二进制文件")
    if ext in _MARKUP_EXTS:
        text = _TAG_RE.sub("\n", text)
        text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    note = ("扩展名 %r 不在契约 kind 枚举(pdf|docx|doc|image|unknown)内，"
            "已按纯文本(%s)读取：kind 仍报 unknown、backend 报 none。"
            "建议主控为纯文本扩充 kind='text' / backend='stdlib_text'。"
            % (ext or "(无)", used))
    return text.strip(), note


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _blank(path: str, kind: str, status: str, backend: str, size: int, md5: str,
           error: Optional[str], elapsed_ms: int,
           warning: Optional[str] = None,
           checks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "path": path,
        "kind": kind,
        "text": "",
        "chars": 0,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "backend": backend,
        "md5": md5,
        "size": size,
        "error": error,
        "warning": warning,
        "checks": checks or {},
        "pages": None,
        "doc": None,
    }


def extract_text(path: str) -> Dict[str, Any]:
    """把一份文件（pdf / docx / doc / 图片 / 纯文本）提取成纯文本 + 质量元信息。

    契约 §3.1 冻结签名：extract_text(path: str) -> dict。
    永不抛异常——所有失败都落到 status/error/warning 里（契约 D6：失败可见）。
    """
    t0 = time.perf_counter()
    p = Path(path)
    spath = str(p)

    def ms() -> int:
        return int(round((time.perf_counter() - t0) * 1000))

    if not p.exists():
        return _blank(spath, sniff_kind(spath), "error", "none", 0, "",
                      "文件不存在", ms())
    if p.is_dir():
        return _blank(spath, sniff_kind(spath), "error", "none", 0, "",
                      "路径是目录不是文件", ms())
    try:
        size = p.stat().st_size
    except OSError as e:
        return _blank(spath, "unknown", "error", "none", 0, "",
                      "stat 失败: %s" % e, ms())
    if size == 0:
        return _blank(spath, sniff_kind(spath), "error", "none", 0, "",
                      "文件 0 字节", ms())
    try:
        md5 = _file_md5(p)
    except OSError as e:
        md5 = ""
        _ = e

    head = _read_head(p, 8)
    kind = sniff_kind(spath, head)
    ext = p.suffix.lower()

    text = ""
    backend = "none"
    hard_status: Optional[str] = None   # 提取阶段就已确定的终态；None = 待质量判定
    error: Optional[str] = None
    warning: Optional[str] = None
    checks: Dict[str, Any] = {}
    pages: Optional[int] = None
    docmeta: Optional[Dict[str, Any]] = None

    try:
        data = p.read_bytes()

        if kind == "image":
            # 契约 D11：本期不做 OCR，不硬造字段
            backend = "none"
            hard_status = "no_text_layer"
            warning = ("图片文件无文本层可提取（本期不做 OCR）。"
                       "建议候选人提供 word/pdf 文字版简历。")

        elif kind == "docx":
            text = _docx_text(data)
            backend = "stdlib_zip"

        elif kind == "doc":
            text, backend, docmeta, warning = _doc_text(data)

        elif kind == "pdf":
            text, backend, pages, notes, pdf_status = _pdf_text(p)
            if notes:
                warning = "; ".join(notes)
            if pdf_status != "ok":
                hard_status = pdf_status
                if pdf_status == "error":
                    error = "PDF 两条梯队都失败: %s" % ("; ".join(notes) or "无文本")

        else:  # unknown
            if ext in _PLAIN_EXTS or ext == ".rtf" or ext == "":
                try:
                    text, note = _plain_text(data, ext)
                    backend = "none"
                    warning = note
                except Exception as e:
                    hard_status = "unsupported"
                    error = "无法当纯文本读取: %s: %s" % (type(e).__name__, e)
            else:
                hard_status = "unsupported"
                error = ("不支持的扩展名 %r（魔数前 8 字节 %s）；契约 kind 枚举只覆盖 "
                         "pdf/docx/doc/image" % (ext or "(无)", head[:8].hex()))
    except PermissionError as e:
        hard_status = "encrypted"
        error = str(e)
        text = ""
    except MemoryError as e:                                  # pragma: no cover
        hard_status = "error"
        error = "内存不足: %s" % e
    except Exception as e:
        hard_status = "error"
        error = "%s: %s" % (type(e).__name__, e)
        text = ""

    # ---------------- 质量判定（阶段 2）----------------
    checks = text_quality(text, kind)
    if hard_status is not None:
        status = hard_status
    elif not _nws(text):
        status = "no_text_layer"
        warning = ((warning + "; ") if warning else "") + "提取结果为空（无文本层）"
    elif not checks["passed"]:
        # **护栏必须排在 detect_scanned 前面**：.doc 字节错位（fcMin 奇偶错一位）
        # 产生的乱码里会有大量 \u2000 重复，先跑 detect_scanned 会把它误报成
        # 「扫描件 no_text_layer」，而真相是「编码错位 garbled」，两者给用户的
        # 处置建议完全不同（前者要文字版，后者要转人工核对原件）。
        status = "garbled"
        reasons = []
        if not checks["chars_floor_ok"]:
            reasons.append("非空白字符仅 %d < 下限 %d"
                           % (checks["chars"], checks["chars_floor"]))
        if not checks["cjk_ok"]:
            reasons.append("CJK 占比 %.3f < %.2f 且拉丁占比 %.3f < %.2f"
                           % (checks["cjk_ratio"], MIN_CJK_RATIO,
                              checks["latin_ratio"], MIN_LATIN_RATIO))
        if not checks["garbage_ok"]:
            reasons.append("乱码字符占比 %.3f > %.2f"
                           % (checks["garbage_ratio"], MAX_GARBAGE_RATIO))
        error = "文本质量护栏未通过: " + "；".join(reasons)
        warning = ((warning + "; ") if warning else "") + \
            "疑似乱码/编码错位，建议转人工核对原件"
    elif detect_scanned(text, kind):
        status = "no_text_layer"
        warning = ((warning + "; ") if warning else "") + (
            "疑似扫描件：非空白字符 %d、数字字符仅 %d 个、唯一字符占比 %.3f"
            "（水印/重复串占比高）。建议提供文字版，本期不做 OCR。"
            % (checks["chars"], checks["digits"], checks["unique_ratio"]))
    else:
        status = "ok"
        if checks["chars_low_warn"]:
            warning = ((warning + "; ") if warning else "") + (
                "文本偏短（非空白字符 %d < %d），字段抽取可能不全"
                % (checks["chars"], DOC_WARN_CHARS))

    result = {
        "path": spath,
        "kind": kind,
        "text": text,
        "chars": len(text),
        "elapsed_ms": ms(),
        "status": status,
        "backend": backend,
        "md5": md5,
        "size": size,
        "error": error,
        # ---- 以下为契约之外的只增字段，W-C 可忽略 ----
        "warning": warning,
        "checks": checks,
        "pages": pages,
        "doc": docmeta,
        "ext": ext,
    }
    return result


def extract_many(paths: List[str]) -> List[Dict[str, Any]]:
    """便捷批量封装（契约未要求，W-C 可自行决定用不用）。"""
    return [extract_text(p) for p in paths]


# --------------------------------------------------------------------------- #
# CLI（自测用；SKILL.md 不直接调本文件，由 W-C 的 intake 脚本调用）
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    import json
    if not argv:
        print("usage: extract_text.py <file...>   # 每行输出一个 JSON", file=sys.stderr)
        return 2
    rc = 0
    for a in argv:
        r = extract_text(a)
        slim = dict(r)
        slim["text"] = slim["text"][:80].replace("\n", " ")
        print(json.dumps(slim, ensure_ascii=False))
        if r["status"] not in ("ok",):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
