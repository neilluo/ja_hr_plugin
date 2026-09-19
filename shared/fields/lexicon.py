# -*- coding: utf-8 -*-
"""字段抽取的词表与校名库（纯数据 + 由数据直接推出的判定）。

这里放的都是「查表」性质的东西：学历枚举与别名、985/211/双一流校名与简称、
证书关键词、技能词表。正则群在 `fields/regex_ext.py`。
原值自 extract_fields.py 第 2/3 节搬入，逐字保留（含确定性排序的依据注释）。
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional, Sequence, Tuple

__all__ = [
    "DEGREE_LEVELS", "degree_to_enum", "degree_rank", "school_rank",
    "SCHOOLS_985", "SCHOOLS_211", "SCHOOLS_DOUBLE_FIRST_CLASS", "SCHOOL_ALIAS",
    "CERT_KEYWORDS", "CERT_KW_SORTED", "SKILL_VOCAB", "SKILL_VOCAB_SORTED",
    "det_sort_long_first",
]

# ---------------------------------------------------------------------------
# External data-table loader (inline word tables are now JSON files in ./data/)
# ---------------------------------------------------------------------------
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load_data(name: str):
    """Load a JSON data file from the ``data/`` sub-directory (UTF-8, cached)."""
    path = os.path.join(_DATA_DIR, name + ".json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

# =========================================================================== #
# 2. 学历 / 学校 / 学校层次
# =========================================================================== #
# 归一到契约枚举：博士|硕士|本科|大专|None
DEGREE_LEVELS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("博士", ("博士",)),
    ("硕士", ("硕士", "研究生", "MBA", "EMBA", "MPA")),
    ("本科", ("本科", "学士", "大学本科", "双一流本科")),
    ("大专", ("大专", "专科", "高职", "大学专科", "高专", "中专", "中职",
              "技工学校", "技校", "高中")),
)
DEGREE_SCAN_RE = re.compile(
    r"(博士|硕士研究生|硕士|研究生|大学本科|本科|学士|大专|大学专科|专科|"
    r"高职|高专|中专|中职|技工学校|技校|高中)"
)
# 现状未被任何路径引用（学历标签行走 regex_ext._EDU_LABEL_LINE_RE）；原值搬迁保留
DEGREE_LABEL_RE = re.compile(
    r"(?:最高学历|最终学历|学历层次|学历学位|文化程度|学\s*历|最高学位|学位)"
    r"\s*[:：|]?\s*(?:是|为)?\s*"
    r"(博士|硕士研究生|硕士|研究生|大学本科|统招本科|全日制本科|本科|学士|"
    r"大专|大学专科|专科|高职|高专|中专|中职|技工学校|技校|高中)"
)


def degree_to_enum(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip()
    for enum, aliases in DEGREE_LEVELS:
        for a in aliases:
            if a in w:
                return enum
    return None


def degree_rank(word: Optional[str]) -> int:
    """博士=0 硕士=1 本科=2 大专=3；未知=9。"""
    e = degree_to_enum(word)
    if not e:
        return 9
    for i, (enum, _a) in enumerate(DEGREE_LEVELS):
        if enum == e:
            return i
    return 9


# 现状未被任何路径引用（校名匹配走 regex_ext._SCHOOL_FULL_RE/_SCHOOL_TOKEN_RE，
# 它们由那边的 _SCHOOL_SUFFIX 字符串拼出，后缀清单比这里多「商学院」）；原值搬迁保留
SCHOOL_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z]{2,18}?"
    r"(?:大学|学院|学校|职业技术学院|职业技术学校|高等专科学校|专科学校|中等专业学校|"
    r"技师学院|高级技工学校))"
)
SCHOOL_LABEL_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"(?:毕业院校|毕业学校|学校名称|就读学校|院校名称|学\s*校|院\s*校|母校)"
    r"\s*[:：|]?\s*(?:是|为)?\s*([^\n；;，,。|]{2,30})"
)

# 985（39 所）
SCHOOLS_985: Tuple[str, ...] = tuple(_load_data("schools_985"))
# 211（非 985 部分）
SCHOOLS_211: Tuple[str, ...] = tuple(_load_data("schools_211"))
# 双一流（2017/2022 新增，非 985/211）
SCHOOLS_DOUBLE_FIRST_CLASS: Tuple[str, ...] = tuple(_load_data("schools_double_first_class"))
# 常见简称 -> 层次
SCHOOL_ALIAS: Dict[str, str] = _load_data("school_alias")


def school_rank(school: Optional[str], education: Optional[str],
                text: str = "") -> Optional[str]:
    """按 985 > 211 > 双一流 > 普通本科 > 大专 的优先级判层次。

    契约枚举：985|211|双一流|普通本科|大专|None。
    """
    hay = school or ""
    hit = None
    if hay:
        # **最长匹配优先**：`西安电子科技大学` 里含有 985 校名 `电子科技大学`，
        # 若按 985 先判会误升档；211 里的全名更长，必须让它赢。
        best_len = 0
        for rank, table in (("985", SCHOOLS_985), ("211", SCHOOLS_211),
                            ("双一流", SCHOOLS_DOUBLE_FIRST_CLASS)):
            for s in table:
                if s and s in hay and len(s) > best_len:
                    best_len = len(s)
                    hit = rank
        if hit is None:
            best_len = 0
            for alias, rank in SCHOOL_ALIAS.items():
                if alias in hay and len(alias) > best_len:
                    best_len = len(alias)
                    hit = rank
    if hit is None and text and re.search(r"985\s*(?:工程|院校|高校|重点)", text):
        hit = "985"
    if hit is None and text and re.search(r"211\s*(?:工程|院校|高校|重点)", text):
        hit = "211"
    if hit is None and text and re.search(r"双一流(?:建设|高校|大学|学科)?", text):
        hit = "双一流"
    if hit:
        return hit
    if education == "本科":
        return "普通本科"
    if education == "大专":
        return "大专"
    return None


# =========================================================================== #
# 3. 证书 / 技能词表
# =========================================================================== #
CERT_KEYWORDS: Tuple[str, ...] = tuple(_load_data("cert_keywords"))


def det_sort_long_first(words) -> Tuple[str, ...]:
    """长度倒序 + 同长按字典序 —— **必须是全序**。

    只写 `sorted(set(X), key=len, reverse=True)` 会导致同长度元素的顺序
    随机化（PYTHONHASHSEED），输出顺序在不同进程之间会变。
    """
    return tuple(sorted(set(words), key=lambda w: (-len(w), w)))


# 长词优先，保证「注册安全工程师」先于「安全工程师」、「中级会计师」先于「会计师」
CERT_KW_SORTED: Tuple[str, ...] = det_sort_long_first(CERT_KEYWORDS)

SKILL_VOCAB: Tuple[str, ...] = tuple(_load_data("skill_vocab"))
SKILL_VOCAB_SORTED: Tuple[str, ...] = det_sort_long_first(SKILL_VOCAB)
