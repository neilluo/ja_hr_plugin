# -*- coding: utf-8 -*-
"""aitable 的值语义：写前净化 + 读回归一化 + 写读比对。

读写两侧都必须过这里，别直接当字符串用（每条都是实测换来的）：
  * `singleSelect` 读回是 dict（要取 `.name`）；`multipleSelect` 是 dict 数组且
    **读回不保序** → 所有比对按集合（`val_set` / `values_equal`），不能按列表；
  * `number` 读回是**字符串形式**的数字；`date` 读回是 RFC3339（写 `YYYY-MM-DD`）；
  * 写入前必须 `sanitize_text`：pdftotext/docx 的输出含 `\\x0c`/`\\t`/U+2028，
    原样写入会被 API 拒收（`contains dangerous Unicode characters`，整批失败）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

__all__ = ["val", "val_set", "values_equal", "sanitize_text"]


# ---------------------------------------------------------------------------
# 读回值归一化
# ---------------------------------------------------------------------------
def val(v: Any, default: Any = None) -> Any:
    """把 `record query` 读回的单元格值归一化成好用的 python 值。

    实测坑（必须走这个函数，别直接当字符串用）：
      * `singleSelect` → dict `{"id","name"}`，取 `.name`；
      * `multipleSelect` → dict 数组，且**读回不保序**；
      * `number` / `currency` / `progress` → **字符串形式**的数字（如 `"0.7"`）；
      * `richText` → `{"markdown": "..."}`；
      * `url` → `{"text","link"}`；
      * `attachment` → `[{"url","filename","size"}]`（url 是 2h 时效的 OSS 预签名链接，
        且 dws 输出里 `&` 会被转义成 `\\u0026`）。
    """
    if v is None:
        return default
    if isinstance(v, list):
        return [val(x, default) for x in v]
    if isinstance(v, dict):
        if "name" in v and ("id" in v or len(v) <= 3):        # select / user-ish
            return v.get("name")
        if "markdown" in v:                                    # richText
            return v.get("markdown")
        if "fileToken" in v or "filename" in v or ("url" in v and "size" in v):
            return v                                           # attachment：整体保留
        if "text" in v and "link" in v:                        # url
            return v
        if "userId" in v or "deptId" in v or "linkedRecordIds" in v:
            return v
        if "value" in v:
            return val(v.get("value"), default)
        return v
    return v


def val_set(v: Any) -> frozenset:
    """把单元格值变成可比较的**集合**（多选读回不保序，只能按集合比）。"""
    x = val(v)
    if x is None:
        return frozenset()
    if isinstance(x, (list, tuple, set, frozenset)):
        return frozenset(_hashable(i) for i in x)
    return frozenset({_hashable(x)})


def _hashable(x: Any) -> Any:
    if isinstance(x, dict):
        return json.dumps(x, ensure_ascii=False, sort_keys=True)
    if isinstance(x, (list, tuple)):
        return json.dumps(list(x), ensure_ascii=False)
    return x


def values_equal(a: Any, b: Any) -> bool:
    """比较写入值与读回值。列表/多选按集合比（读回不保序），数字按数值比（读回是字符串）。

    日期也算相等：写 `"2026-09-17"`，读回是 RFC3339 `"2026-09-17T00:00:00+08:00"`。
    """
    na, nb = val(a), val(b)
    if isinstance(na, (list, tuple, set)) or isinstance(nb, (list, tuple, set)):
        return val_set(na) == val_set(nb)
    if isinstance(na, bool) or isinstance(nb, bool):
        return bool(na) == bool(nb)
    if _is_numberish(na) and _is_numberish(nb):
        try:
            return abs(float(na) - float(nb)) < 1e-9
        except (TypeError, ValueError):
            pass
    if isinstance(na, dict) and isinstance(nb, dict):
        return _hashable(na) == _hashable(nb)
    if isinstance(na, str) and isinstance(nb, str) and _date_prefix_eq(na, nb):
        return True
    return na == nb


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_prefix_eq(a: str, b: str) -> bool:
    """date 字段写入 `YYYY-MM-DD`、读回 `YYYY-MM-DDThh:mm:ss+08:00` → 视为相等。"""
    if _DATE_ONLY_RE.match(a) and b.startswith(a):
        return True
    if _DATE_ONLY_RE.match(b) and a.startswith(b):
        return True
    return False


def _is_numberish(x: Any) -> bool:
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, (int, float)):
        return True
    if isinstance(x, str):
        try:
            float(x)
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# 写前净化
# ---------------------------------------------------------------------------
#: 必须丢掉的码位区间（含端点）。实测依据见 sanitize_text 文档串。
_DROP_RANGES = (
    (0x00, 0x09), (0x0B, 0x1F),          # C0 控制字符（含 \t），只保留 \n = 0x0A
    (0x7F, 0x9F),                        # DEL + C1 控制字符
    (0x00AD, 0x00AD),                    # 软连字符
    (0x061C, 0x061C),                    # 阿拉伯字母标记
    (0x200B, 0x200F),                    # 零宽空格/连接符/LRM/RLM
    (0x202A, 0x202E),                    # bidi 嵌入与覆盖
    (0x2060, 0x2064), (0x2066, 0x2069),  # 不可见运算符 + bidi 隔离
    (0xFEFF, 0xFEFF),                    # BOM / 零宽不换行空格
    (0xFFF9, 0xFFFB),                    # 行间注释
    (0xE0000, 0xE007F),                  # Tags 区块（隐写用）
)
#: 语义上是换行、但会被服务端判成「dangerous Unicode」的码位 → 换成 \n
_NEWLINE_MAP = ((0x2028, "\n"), (0x2029, "\n"))
_TRANSLATE_TABLE: Optional[Dict[int, Any]] = None


def _translate_table() -> Dict[int, Any]:
    global _TRANSLATE_TABLE
    if _TRANSLATE_TABLE is None:
        table: Dict[int, Any] = {}
        for lo, hi in _DROP_RANGES:
            for cp in range(lo, hi + 1):
                table[cp] = None
        for cp, rep in _NEWLINE_MAP:
            table[cp] = rep
        table[0x0D] = None               # \r（\r\n 先归一成 \n 再丢单独的 \r）
        _TRANSLATE_TABLE = table
    return _TRANSLATE_TABLE


def sanitize_text(s: Any) -> Any:
    """写入前净化文本；非字符串原样返回。

    规则（契约要求 + 实测）：
      1. `\\r\\n` → `\\n`，剩下的孤立 `\\r` 丢掉；
      2. **U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR → `\\n`**。
         实测：dws 会以 `[UNCLASSIFIED] <fieldId> contains dangerous Unicode characters`
         **拒绝整批写入**（错误码 3、无 retryable 提示）。31 份真实简历里 3 份的
         docx 提取文本含 U+2028 → 那 3 条记录写不进去。这类字符不是 C0/C1 控制符，
         `ord(c) < 32` 的老规则抓不到，必须显式处理。
      3. 丢掉 `ord(c) < 32`（**保留 `\\n`**）、DEL 与 C1（0x7F~0x9F）；
      4. 丢掉零宽/bidi 等不可见格式符（U+200B~U+200F、U+202A~U+202E、U+2060~U+2069、
         U+FEFF、U+00AD、U+061C、U+FFF9~U+FFFB、Tags 区块）。
         注：实测这些字符当前服务端**是收的**（有简历含 17~38 个 U+200F 也写成功了），
         但它们是 PDF/docx 提取噪声、不可见、且属于典型注入载体，统一清掉更稳。

    ⚠️ 净化会改变字节内容 → 任何「写入 vs 读回」的逐字节比对都必须拿
    `sanitize_text(原文)` 当基准，不能拿原文（否则会误判成截断）。
    """
    if not isinstance(s, str) or not s:
        return s
    if "\r\n" in s:
        s = s.replace("\r\n", "\n")
    return s.translate(_translate_table())
