# -*- coding: utf-8 -*-
"""身份字段安全阀的纯函数工具（intake_resume.py / build_match_input.py 共用）。

背景：name / email / expected_location 在 digest 里**既无 needs_review
标记也无原文保留面**，正则/OCR 错抽即静默入库。本模块提供三件确定性能力，
全部零第三方依赖、python 3.9/3.14 兼容：

1. `identity_evidence(text, ...)`：从简历全文里取「字段命中所在行原文」（≤60 字），
   进 digest 的 evidence.name_text / email_text / location_text——给 Turn 2 一个
   可见的原文复核面。
2. `email_ocr_noise(email)`：邮箱 OCR 噪声规则判据（域名无点 / TLD 含非字母 /
   域名主体字母+数字混排，如 q9.com ← qq.com、gmai1.com ← gmail.com、163.c0m ←
   163.com）。命中 → needs_review 追加 "email"。
3. `name_review_reason(...)`：姓名来源判据——name_source ∈ {filename, vision, ocr}
   或提取 backend 是 OCR 梯队（vision_ocr / agent_vision / 任何含 ocr/vision 的
   backend）或 field_source=agent_vision → needs_review 追加 "name"。

⚠️ 本模块**不参与字段抽取**（不进 extract_resume_fields 的返回值），因此不影响
ORACLE-LOCAL 的 EXTRACT/FIELDS 层指纹；只被编排层（C1 组装 candidates.json、
C2 组装 digest）调用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

__all__ = [
    "IDENTITY_EVIDENCE_KEYS", "IDENTITY_LINE_LIMIT", "OCR_BACKEND_HINTS",
    "NAME_FLAG_SOURCES", "find_hit_line", "identity_evidence",
    "email_ocr_noise", "name_review_reason",
]

#: digest evidence 里新增的三个身份原文键（只增，不动 D4 四段）
IDENTITY_EVIDENCE_KEYS = ("name_text", "email_text", "location_text")

#: 单条原文行的长度上限（字符）。姓名行设计值 ≤60；email/地点行同口径封顶，
#: 防止双栏串栏把整段垃圾带进 digest（可见即可，复核以行为单位）。
IDENTITY_LINE_LIMIT = 60

#: backend 名里出现这些子串即视为 OCR/视觉梯队（vision_ocr / agent_vision / jxa 视觉等）
OCR_BACKEND_HINTS = ("ocr", "vision")

#: 设计口径：name 来源为 filename/vision/ocr 时 needs_review 追加 "name"
NAME_FLAG_SOURCES = ("filename", "vision", "ocr")

_WS_RE = re.compile(r"\s+")


def _flat(s: Any) -> str:
    return _WS_RE.sub(" ", str(s or "")).strip()


def _clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + "…"


def find_hit_line(text: Any, needles: Iterable[str], limit: int = IDENTITY_LINE_LIMIT,
                  min_len: int = 2) -> str:
    """返回 text 中**第一个命中 needle 的行**原文（行内空白压成单空格，≤limit 字）。

    匹配按「压平空白后包含」判定（PDF/OCR 文本行内常有杂散空格）。
    needle 太短（<min_len）不参与匹配，防止单字误命中。全部未命中返回 ""。
    """
    t = str(text or "")
    if not t.strip():
        return ""
    lines = [ln for ln in (_flat(ln) for ln in t.splitlines()) if ln]
    for needle in needles:
        n = _flat(needle)
        if len(n) < min_len:
            continue
        for ln in lines:
            if n in ln:
                return _clip(ln, limit)
    return ""


def _head_line(text: Any, limit: int = IDENTITY_LINE_LIMIT) -> str:
    """姓名兜底：简历抬头行（姓名通常出现在首行区域；文件名覆盖正文姓名时，
    正文真名就在这一带——给 Turn 2 留比对面）。找不到返回 ""。"""
    t = str(text or "")
    for ln in t.splitlines():
        ln = _flat(ln)
        if ln:
            return _clip(ln, limit)
    return ""


def identity_evidence(text: Any, name: Any = None, email: Any = None,
                      expected_location: Any = None,
                      limit: int = IDENTITY_LINE_LIMIT) -> Dict[str, str]:
    """三个身份字段的「命中行原文」evidence（安全阀的原文保留面）。

    * name_text：姓名命中行；姓名不在正文（如 name_source=filename 且正文写的是
      另一个名字）→ 兜底取简历首行（真名通常就在抬头），仍无 → ""。
    * email_text：邮箱整串命中行；整串未命中（OCR 修复/补全过 @）→ 退而搜本地部分。
    * location_text：期望地点命中行；整串跨行未命中 → 退而搜首个片段（≤8 字）；
      无命中返回 ""（**不标记**，设计口径：地点阀只给原文面，不加 needs_review）。
    """
    t = str(text or "")
    name_s = _flat(name)
    email_s = _flat(email)
    loc_s = _flat(expected_location)

    name_text = find_hit_line(t, [name_s] if name_s else [], limit)
    if not name_text and name_s:
        name_text = _head_line(t, limit)

    email_needles = [email_s] if email_s else []
    if email_s and "@" in email_s:
        local = email_s.split("@", 1)[0]
        if len(local) >= 5:                     # 本地部分够长才敢单独搜（防短串误命中）
            email_needles.append(local)
    email_text = find_hit_line(t, email_needles, limit) if email_needles else ""

    loc_needles = [loc_s] if loc_s else []
    if loc_s and len(loc_s) > 8:
        first = re.split(r"[\s、,，;；:：/|]", loc_s)[0]
        if 2 <= len(first) <= 8:
            loc_needles.append(first)
    loc_text = find_hit_line(t, loc_needles, limit) if loc_needles else ""

    return {"name_text": name_text, "email_text": email_text, "location_text": loc_text}


#: 常见纯数字域名主体（163/126/139/189 等是合法服务商，不算字母数字混排噪声）
_ALL_DIGIT_OK = re.compile(r"^\d+$")
#: 域名主体 = 字母与数字混排（q9 / gmai1 / out1ook / qq123）→ 疑似 OCR 数字替字母
_MIXED_LABEL = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9\-]+$")
#: TLD 必须纯字母（c0m / co1n 之类直接命中噪声）
_TLD_OK = re.compile(r"^[A-Za-z]{2,10}$")


def email_ocr_noise(email: Any) -> Optional[str]:
    """邮箱 OCR 噪声判据。

    命中任一规则返回判据说明（→ needs_review 追加 "email"）；干净返回 None。
      R1 域名无点（`abc@163com`）；
      R2 TLD 含非字母字符（`xxx@163.c0m`）；
      R3 域名主体字母+数字混排（`930282610@q9.com` ← qq.com、`x@gmai1.com` ←
         gmail.com；纯数字主体 163/126/139 是正常服务商，不标）。
    本地部分（@ 前）不参与判定：QQ 号式纯数字本地部分完全合法。
    """
    s = _flat(email)
    if not s or "@" not in s:
        return None
    domain = s.rsplit("@", 1)[1].strip(".").lower()
    if not domain:
        return "R1:域名为空"
    if "." not in domain:
        return "R1:域名无点(%s)" % domain
    labels = domain.split(".")
    tld = labels[-1]
    if not _TLD_OK.match(tld):
        return "R2:TLD含非字母(%s)" % tld
    for lab in labels[:-1]:
        if not lab:
            return "R1:域名有空段(%s)" % domain
        if _ALL_DIGIT_OK.match(lab):
            continue
        if _MIXED_LABEL.match(lab):
            return "R3:域名主体字母数字混排(%s)，疑似OCR数字替字母(如qq.com→q9.com)" % lab
    return None


def name_review_reason(name_source: Any = None, parse_backend: Any = None,
                       field_sources: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """姓名 needs_review 判据（来源为 filename/vision/ocr 时标记）。

    返回判据说明（→ needs_review 追加 "name"）；无需标记返回 None。
      * field_sources["name"]=="agent_vision"（补丁草稿姓名）；
      * name_source ∈ NAME_FLAG_SOURCES（filename=文件名覆盖正文；vision/ocr 预留）；
      * parse_backend 属 OCR/视觉梯队（vision_ocr / agent_vision 等）——OCR 首行
        垃圾 token 会产出「本汉族」这类姓名，正文行原文必须人工比对。
    """
    fs = field_sources if isinstance(field_sources, dict) else {}
    if str(fs.get("name") or "") == "agent_vision":
        return "field_source=agent_vision(姓名取自图片识别草稿)"
    src = str(name_source or "").strip().lower()
    if src in NAME_FLAG_SOURCES:
        return "name_source=%s(文件名/视觉来源，正文未印证)" % src
    backend = str(parse_backend or "").strip().lower()
    if backend and any(h in backend for h in OCR_BACKEND_HINTS):
        return "parse_backend=%s(OCR/视觉文本，姓名可能误读)" % backend
    return None
