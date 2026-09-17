#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / extract_text.py
===================================================

多格式文本提取层的**薄门面**（P1 责任链重构后）。**零第三方 pip 依赖**：只用
python 标准库 + `shared/vendor/` 里 vendor 进来的纯 python 库（olefile /
pypdf / typing_extensions，见 `shared/vendor/VENDOR_MANIFEST.txt`）。

对外契约（构建契约 §3.1，签名已冻结，不得改动）
------------------------------------------------
    extract_text(path: str) -> dict
        {"path": str, "kind": "pdf|docx|doc|image|unknown",
         "text": str, "chars": int, "elapsed_ms": int,
         "status": "ok|no_text_layer|garbled|encrypted|unsupported|error",
         "backend": "pypdf|pdfkit_jxa|vision_ocr|agent_vision|stdlib_zip|ole_stdlib|"
                    "ole_vendored|none",
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

架构（P1 责任链，契约 D9 后端梯队）
----------------------------------
本文件只保留：前置检查、责任链委托、阶段 2 质量判定（text_quality /
detect_scanned 护栏）、结果组装与 CLI。文档模型（FileKind / ResumeDocument /
ExtractionResult / kind 嗅探）在 `shared/documents.py`；具体提取实现在
`shared/extraction/**`，每个梯队一个 TextExtractor 类：

    .pdf   -> PypdfExt    vendor pypdf                     backend=pypdf
           -> JxaExt      macOS JXA/PDFKit（仅 darwin）     backend=pdfkit_jxa
              都失败 -> status=error；**禁止** pdfplumber（依赖链含二进制 wheel）
    .docx  -> DocxZipExt  stdlib zipfile + 正则             backend=stdlib_zip
    .doc   -> DocPieceExt OLE2/CFB + MS-DOC piece table     backend=ole_vendored/ole_stdlib
    扫描件/图片 -> VisionOcrExt（Tier 1.5，P3，仅 darwin）   backend=vision_ocr
              macOS 自带 Vision framework 走 osascript/JXA，零 pip 依赖。
              图片直接受理；PDF 仅当 pypdf/JXA 都没拿到可用文本（含「提出文本但
              被 detect_scanned 判成水印/扫描件」的 gate 降级）才受理。
              OCR 文本必须再过 detect_scanned + 「数字字符数>0」护栏，不可信判
              no_text_layer，绝不当假成功（B1：markitdown 的水印噪声曾骗过护栏）。
              非 darwin 无此梯队 -> ImageExt/no_text_layer 如实告知。
    跨平台兜底 -> AgentPatchExt（Tier 2，P4a 通道 / P7 刀6 入链） backend=agent_vision
              agent 多模态读出的补丁（`--apply-vision-patch`）作为链上最后一环：
              只接「补丁表里有本文件」且「链终态本来是 no_text_layer」的文档，
              文本原样返回并合并 fields_draft（取自草稿的字段 field_source=
              agent_vision + needs_review）。补丁表为空时恒不受理（链行为与 P4a
              之前逐字一致）；本门面只经 `agent_patch_tier()` 注册唯一实例，
              装补丁是编排层（intake Pipeline）的事，本文件不读任何补丁文件。
    图片（OCR 不可用/不可信时的终态）-> ImageExt  status=no_text_layer, backend=none
              （契约 D11：不硬造字段，进 ❌ 清单建议提供文字版）
    unknown（纯文本兜底）-> 仍由本文件 _plain_text 处理，不进链
              （因此 Tier 2 对它不适用；编排层用 AgentPatchExt.merge_entry 走同一份
              合并实现兜底，见 intake/pipeline.py）

ExtractorChain 按注册顺序问 can_handle，第一个 extract 返回 status=="ok" 的
梯队赢；走过的每一级记录进 notes（backend 链路可追溯）。P3 起 run() 额外接受
gate（本门面传 detect_scanned）：ok 结果若被 gate 判为水印/扫描件文本会降级
落下一级；npages 在所有梯队里保留首个非零值（P1 评审裁决①：扫描件不再丢页数）。
加梯队 = 加一个类 + _CHAIN 注册一行。各梯队的标定结论与护栏阈值依据在对应模块
docstring。

兼容性
------
D10：全程 pathlib，不硬编码路径分隔符；语法兼容 python 3.8+（不用 match、
不用 `X | None` 运行时标注，只用 typing.Optional）。已在 3.9.6 与 3.14.0 实测。
"""

from __future__ import annotations  # noqa: F404  (仅影响注解求值，3.7+ 可用)

import hashlib
import html
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from documents import VENDOR_DIR, ResumeDocument
from documents import nws as _nws
from documents import sniff_kind as _sniff_file_kind
from extraction.agent_patch_ext import AgentPatchExt, agent_patch_tier
from extraction.chain import ExtractorChain
from extraction.doc_piece_ext import DocPieceExt
from extraction.docx_zip_ext import DocxZipExt
from extraction.image_ext import ImageExt
from extraction.jxa_ext import JxaExt
from extraction.pypdf_ext import PypdfExt
from extraction.vision_ext import VisionOcrExt

__all__ = [
    "extract_text",
    "detect_scanned",
    "text_quality",
    "sniff_kind",
    "VENDOR_DIR",
]

# --------------------------------------------------------------------------- #
# 提取责任链（注册顺序 = 梯队顺序；加梯队 = 加一个类 + 这里加一行）
# VisionOcrExt = Tier 1.5（P3）：仅 darwin，只接「图片」与「前序文本层梯队全部
# 没拿到可用文本的 PDF」；OCR 文本仍要过 detect_scanned + 数字字符数护栏。
# AgentPatchExt = Tier 2（P4a 通道，P7 刀6 入链）：agent 多模态补丁，只接「补丁表
# 里有本文件」且「链终态本来是 no_text_layer」的文档；**必须排链尾**——can_handle
# 靠 doc.prior 判断本机梯队全失败，插在中间会漏掉 docx/doc/image 的失败
# （见 extraction/agent_patch_ext.py 模块 docstring）。补丁表为空时恒不受理，
# 链行为与 P4a 之前逐字一致。
# --------------------------------------------------------------------------- #
_CHAIN = ExtractorChain([PypdfExt(), JxaExt(), VisionOcrExt(),
                         DocxZipExt(), DocPieceExt(), ImageExt(),
                         agent_patch_tier()])


# --------------------------------------------------------------------------- #
# 常量（kind 嗅探的魔数/扩展名常量已随 sniff 本体迁入 documents.py）
# --------------------------------------------------------------------------- #
_PLAIN_EXTS = {".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".log",
               ".json", ".html", ".htm", ".xml", ".yaml", ".yml"}
_MARKUP_EXTS = {".html", ".htm", ".xml"}

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


# --------------------------------------------------------------------------- #
# 通用小工具（_nws/_read_head/sniff 本体已迁入 documents.py）
# --------------------------------------------------------------------------- #
def _file_md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            blk = fh.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


def sniff_kind(path: str, head: Optional[bytes] = None) -> str:
    """契约包装：判定本体在 documents.py（魔数优先、扩展名兜底——客户经常把
    docx 改名成 doc，只认扩展名会走错后端）；对外始终回枚举串
    pdf|docx|doc|image|unknown。"""
    return _sniff_file_kind(path, head).value


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

    kind == "image" 且文本为空时直接返回 True：图片没有文本层可提。
    P3 起图片可能被 Vision OCR 救回（backend=vision_ocr）——**有文本的图片不再
    一票判死**，改走与 pdf 相同的内容判据（字符数/数字字符数/重复度/水印词）：
    OCR 文本若仍是水印/重复串/数字极少，照样判 True（防静默假成功，B1 教训）。
    """
    if kind == "image" and not _nws(text):
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
# 纯文本兜底（扩展名不在枚举里，但内容其实是文本；不进责任链）
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

    doc = ResumeDocument.inspect(p, size, md5)
    kind = doc.kind.value
    ext = doc.ext

    text = ""
    backend = "none"
    hard_status: Optional[str] = None   # 提取阶段就已确定的终态；None = 待质量判定
    error: Optional[str] = None
    warning: Optional[str] = None
    checks: Dict[str, Any] = {}
    pages: Optional[int] = None
    docmeta: Optional[Dict[str, Any]] = None

    try:
        if kind == "unknown":
            data = doc.data()
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
                         "pdf/docx/doc/image" % (ext or "(无)", doc.head[:8].hex()))
        else:
            # gate=detect_scanned（P3）：梯队返回 ok 但文本层被判「扫描件只剩水印/
            # 重复串」时降级落下一级——没有这一层，pypdf 提出 671 字符水印就赢了，
            # Vision OCR 永远接不到手。护栏本身与门面阶段 2 用同一个函数，口径一致。
            _res = _CHAIN.run(doc, gate=detect_scanned)
            text, backend, pages, docmeta = (
                _res.text, _res.backend, _res.npages, _res.meta)
            if _res.notes:
                warning = "; ".join(_res.notes)
            if _res.status != "ok":
                hard_status = _res.status
                # chain 耗尽且全梯队 error 才会走到这里（P3 起 pdf 含 Vision OCR 梯队）
                if _res.status == "error" and kind == "pdf":
                    error = "PDF 全部梯队都失败: %s" % ("; ".join(_res.notes) or "无文本")
            elif _res.backend == AgentPatchExt.BACKEND:
                # Tier 2（agent 多模态补丁）赢时**不过下面的本机质量护栏**：护栏
                # （字符数/CJK 占比/乱码占比/detect_scanned）恰恰是因为本机读不出
                # 文字才走到这一环，拿它去判 agent 读出来的补丁文本会把兜底通道自己
                # 判死（补丁文本短/无数字很常见）。与 P4a 编排层原语义一致：补丁覆盖
                # 本文件即 parse_status="ok"，文本原样入库，复核责任交回合 2。
                hard_status = "ok"
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
            "（水印/重复串占比高）。建议提供文字版；macOS 上扫描件/图片会先经"
            " Vision OCR 梯队救回，走到本判定说明 OCR 不可用或 OCR 文本不可信。"
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
