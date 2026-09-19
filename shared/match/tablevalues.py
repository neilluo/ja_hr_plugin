# -*- coding: utf-8 -*-
"""表值/文本归一小工具 —— **build_match_input（C2）一侧的语义**。

⚠️ 同名不同义，禁止合并（红线）：
  * `as_list`：build 侧 dict 元素取 name/text/value 且过 `clean_ws`；
    apply_decisions.as_list dict 元素走它自己的 as_text、只 strip。
  * `as_text`：build 侧 dict 兜底 `compact(v)`；apply 侧兜底 `json.dumps(v)`。
  两侧兜底/归一化强度不同 → 「抽公共实现统一两侧」本身就是行为变更。
  **不合并**——apply 侧独立实现在 match/applyvalues.py。本模块只服务 build 侧，
  apply/verify 不得 import 这里的 as_list/as_text。
"""

import json
import math
import re
from typing import Any, List, Optional, Sequence, Tuple

CHARS_PER_TOKEN = 1.5                   # 中文粗估：1.5 字符 / token（与前序实验同口径）

#: evidence 里**可截断**的两段的长度上限（字符）。education_text / cert_text 绝不截断。
WORK_TEXT_LIMIT = 900
SKILL_TEXT_LIMIT = 500


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def est_tokens(obj: Any) -> Tuple[int, int]:
    """返回 (字符数, 估算 token 数)。中文按 1.5 字符/token 粗估（与前序实验同口径）。"""
    n = len(compact(obj))
    return n, int(math.ceil(n / CHARS_PER_TOKEN))


def clip(s: Any, limit: int, mark: str = "…[截断]") -> str:
    """截断到 limit 字符；limit<=0 表示不截断。"""
    if s is None:
        return ""
    s = str(s)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + mark


def full(s: Any) -> str:
    """教育/证书段**完整保留，不截断**。"""
    return "" if s is None else str(s)


def clean_ws(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"[ \t\r\f\v]+", " ", str(s)).strip()


def as_list(v: Any) -> List[str]:
    """把表里读回的值归一成 list[str]（multipleSelect 读回是 list，text 是 str）。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        out = []
        for it in v:
            if isinstance(it, dict):
                it = it.get("name") or it.get("text") or it.get("value")
            if it is None:
                continue
            s = clean_ws(it)
            if s:
                out.append(s)
        return out
    if isinstance(v, dict):
        v = v.get("name") or v.get("text") or v.get("value")
    s = clean_ws(v)
    return [s] if s else []


def as_text(v: Any) -> str:
    """richText 读回可能是 {"markdown": ...}；singleSelect 可能是 {"name": ...}。"""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("markdown", "text", "name", "value"):
            if isinstance(v.get(k), str):
                return v[k]
        return compact(v)
    if isinstance(v, (list, tuple)):
        return "\n".join(as_text(x) for x in v)
    return str(v)


def as_number(v: Any, default: Optional[float] = None) -> Optional[float]:
    """number 字段读回是**字符串**（如 "0.7"），统一转 float。"""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        if not m:
            return default
        try:
            return float(m.group(0))
        except ValueError:
            return default


def dedupe_keep_order(items: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out
