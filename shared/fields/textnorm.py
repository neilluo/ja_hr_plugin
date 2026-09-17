# -*- coding: utf-8 -*-
"""字段抽取的文本级前处理：字体伪影/空白归一、条目切分、按标题分段。

简历侧与 JD 侧共用（契约要求「JD 的技能条目与简历的技能条目用同一个切分器」，
否则打分分母口径不一致）。原值自 extract_fields.py 第 0/1/6 节搬入，逐字保留。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "normalize_text",
    "split_items",
    "slice_sections",
    "clean_item",
    "RESUME_SECTION_HEADS",
    "JD_SECTION_HEADS",
    "SENTENCE_SEP_RE",
]

# =========================================================================== #
# 0. 文本归一化
# =========================================================================== #
# NFKC 覆盖不到的 CJK 部首补充码位 -> 对应简化汉字（实测样本里出现过 7 种，
# 这里给出完整的「C-SIMPLIFIED」系列，客户 PDF 字体千奇百怪，宁可多映射）
_RADICAL_FIX = {
    "\u2EA6": "丬", "\u2EB0": "纟", "\u2EBE": "艹", "\u2EBF": "艹",
    "\u2EC0": "艹", "\u2EC4": "西", "\u2EC5": "见", "\u2EC6": "角",
    "\u2EC8": "讠", "\u2EC9": "贝", "\u2ECB": "车", "\u2ECC": "辶",
    "\u2ED0": "钅", "\u2ED3": "长", "\u2ED4": "门", "\u2ED7": "雨",
    "\u2ED8": "青", "\u2ED9": "韦", "\u2EDA": "页", "\u2EDB": "风",
    "\u2EDC": "飞", "\u2EE0": "饣", "\u2EE2": "马", "\u2EE3": "骨",
    "\u2EE4": "鬼", "\u2EE5": "鱼", "\u2EE6": "鸟", "\u2EE7": "卤",
    "\u2EE8": "麦", "\u2EE9": "黄", "\u2EEA": "黾", "\u2EEC": "齐",
    "\u2EEE": "齿", "\u2EF0": "龙", "\u2EF1": "龟",
}
_RADICAL_RE = re.compile("[" + "".join(_RADICAL_FIX) + "]")

# 全角空格 / 不换行空格 -> 普通空格
_SPACE_FIX = {
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2007": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ", "\u200b": "", "\u200c": "", "\u200d": "",
    "\u200e": "", "\u200f": "", "\ufeff": "",
}
_SPACE_RE = re.compile("[" + "".join(_SPACE_FIX) + "]")


def normalize_text(text: str) -> str:
    """统一 PDF/Word 文本层的字体伪影与空白字符。

    NFKC 会把康熙部首（U+2F00-2FD5）还原成汉字，也会把全角数字/字母还原成
    半角；`_RADICAL_FIX` 补 NFKC 覆盖不到的 CJK 部首补充区。
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _RADICAL_RE.sub(lambda m: _RADICAL_FIX[m.group(0)], t)
    t = _SPACE_RE.sub(lambda m: _SPACE_FIX[m.group(0)], t)
    return t


# =========================================================================== #
# 1. 通用切分器（JD 侧与简历侧共用；修「逗号分隔被并成 1 条」缺陷）
# =========================================================================== #
_ITEM_SEP_RE = re.compile(r"[、，,;；/|·•‧∙●○◦▪（）()\t\r\n]+|\s{1,}")
_ITEM_SEP_HARD_RE = re.compile(r"[、，,;；/|·•‧∙●○◦▪（）()\t\r\n]+")
SENTENCE_SEP_RE = re.compile(r"[。；;！!？?\n]+")


def split_items(raw: Optional[str],
                min_len: int = 2,
                max_len: int = 40,
                max_items: int = 40,
                split_space: bool = False,
                drop_stop: bool = True) -> List[str]:
    """把一段技能/证书描述切成条目列表：多分隔符切分 + 去重 + 去空 + 限长。

    split_space=False（默认）时不把单个空格当分隔符——中文里空格常只是排版，
    切开会把「熟练使用 Office 办公软件」拆成碎片。JD 的技能条目实测主要用
    `，、；` 分隔，不需要按空格切。
    """
    if not raw:
        return []
    sep = _ITEM_SEP_RE if split_space else _ITEM_SEP_HARD_RE
    out: List[str] = []
    seen: Set[str] = set()
    for chunk in sep.split(raw):
        item = clean_item(chunk)
        if not item:
            continue
        if len(item) < min_len or len(item) > max_len:
            continue
        if drop_stop and _is_filler_item(item):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


_FILLER_ITEMS = {
    "等", "等等", "其他", "其它", "以及", "并且", "并且等", "如", "例如", "如下",
    "以上", "以下", "等能力", "等技能", "无", "暂无", "/", "-", "—", "、",
}
_FILLER_PREFIX = ("等", "及其他", "以及其他", "以及", "或", "比如")


# 自评/能力动词前缀：剥掉之后剩下的才是真正的技能名
# （`精通对应工序工艺原理` -> `对应工序工艺原理`，`熟练使用Office办公软件` -> `Office办公软件`）
_ITEM_VERB_PREFIX_RE = re.compile(
    r"^(?:能够|可以|熟练|精通|掌握|熟悉|了解|具备|擅长|善于|具有|拥有|负责|承担|参与|"
    r"主导|能|会)(?:使用|运用|应用|操作|掌握|进行)?\s*"
    r"(?:较强的|良好的|扎实的|丰富的|一定的|较强的|出色的|优秀的)?\s*")
# 明显不是技能条目的东西：句子（带冒号）、时间量词、自评尾巴
_ITEM_NOISE_RE = re.compile(
    r"(\d\s*年|\d\s*个月|\d+\s*岁|[:：]|经验$|意识$|精神$|态度$|责任心$|责任感$|"
    r"^\d+$|^\d{4}[-./年])")


def clean_item(s: str) -> str:
    s = (s or "").strip()
    s = s.strip(" \t\r\n、，,;；.。:：·-—~()（）[]【】\"'“”‘’")
    s = re.sub(r"^\d{1,2}\s*[、.．)）]\s*", "", s)      # 去掉 "1、" "2." 编号
    s = re.sub(r"^[（(]\s*\d+\s*[)）]\s*", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) > 4 and s.endswith("等"):                 # 「…能力等」-> 「…能力」
        s = s[:-1].strip()
    stripped = _ITEM_VERB_PREFIX_RE.sub("", s).strip("，,、 ")
    if len(stripped) >= 4:                              # 剥完还剩下东西才剥
        s = stripped
    return s


def _is_filler_item(item: str) -> bool:
    if item in _FILLER_ITEMS:
        return True
    if _ITEM_NOISE_RE.search(item):
        return True
    for p in _FILLER_PREFIX:
        if item.startswith(p):
            return True
    # 纯标点 / 纯数字 / 单个字母
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", item):
        return True
    return False


# =========================================================================== #
# 6. 分段（sections）
# =========================================================================== #
RESUME_SECTION_HEADS: Tuple[Tuple[str, str], ...] = (
    ("cert_text", r"(资格证书|职业资格证书|职业证书|技能证书|证书情况|获得证书|证书|职称)"),
    ("education_text", r"(教育经历|教育背景|学历背景|教育情况|学习经历|受教育情况|学历信息|教育程度|教育)"),
    ("work_text", r"(工作经历|工作经验|职业经历|从业经历|任职经历|工作履历|项目经历|项目经验|实习经历|履历|主要工作)"),
    ("skill_text", r"(专业技能|个人技能|技能特长|技能技巧|技术能力|专业能力|核心能力|核心优势|优势亮点|技能|特长)"),
)
JD_SECTION_HEADS: Tuple[Tuple[str, str], ...] = (
    ("education_text", r"(教育背景|学历要求|教育要求)"),
    ("cert_text", r"(培训经历|证书要求|职业证书要求|资格要求)"),
    ("work_text", r"(从业经验|工作经验|经验要求)"),
    ("skill_text", r"(技能技巧|技能要求|能力要求)"),
    ("kpi_text", r"(关键绩效指标|KPI)"),
    ("responsibility_text", r"(主要工作职责|工作职责|次要工作职责|岗位职责)"),
)
_SECTION_NOISE_RE = re.compile(r"^[\d一二三四五六七八九十]{1,3}\s*[、.．)）]?\s*")
# 标题允许带一点尾巴（`专业技能及专业知识` / `教育经历（2016-2020）`），
# 但仍要求整行 <= 14 字，避免把正文里出现的「专业技能」当标题。
_SECTION_TAIL = r"(?:[及与和、（(]?[\u4e00-\u9fffA-Za-z0-9（）()\-~～\s]{0,8})?"


def slice_sections(text: str, heads: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """把文本按标题行切成 {key: 正文}。找不到标题就返回空串（不硬造）。

    标题必须**独占一行**（或行首），否则正文里提到的「专业技能」「工作经历」
    会被误当标题，把段落切碎。
    """
    marks: List[Tuple[int, int, str]] = []
    pos = 0
    for line in text.splitlines():
        stripped = line.strip()
        core = _SECTION_NOISE_RE.sub("", stripped).strip().rstrip("：:")
        core = re.sub(r"\s*[/／|｜]\s*[A-Za-z][A-Za-z\s/&().]*$", "", core).strip()
        if core and len(core) <= 20:
            for key, pat in heads:
                if re.fullmatch(pat + _SECTION_TAIL, core):
                    marks.append((pos, pos + len(line), key))
                    break
        pos += len(line) + 1
    out: Dict[str, str] = {}
    for i, (start, line_end, key) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[line_end:end].strip()
        out[key] = (out[key] + "\n" + body).strip() if out.get(key) else body
    for key, _pat in heads:
        out.setdefault(key, "")
    return out
