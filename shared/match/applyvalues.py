# -*- coding: utf-8 -*-
"""表值/时间小工具 —— **apply_decisions（Turn 3 写库）一侧的语义**（P9a 搬入）。

⚠️ as_list/as_text 终裁（P8 报告 §4a 逐案裁定表）：与 match/tablevalues.py 的
build 侧同名函数**不合并**——
  * `as_text`：本侧 dict 兜底 `json.dumps(v, ensure_ascii=False)`（默认 separators
    带空格），build 侧兜底 `compact(v)`（无空格）→ 兜底字节不同；
  * `as_list`：本侧 dict 元素走本侧 as_text、只 strip，build 侧取 name/text/value
    且过 clean_ws → 归一化强度不同。
统一即改行为（分析报告 §B.7#4 红线），两侧各自保留独立实现：本模块只服务
apply/写库侧，build 侧一律走 tablevalues.py，互不 import。

`_now`（与 build 侧 digest.py `_now` 逐字重复）P9a 裁定**不统一**：两侧各自模块
私有、各 4 行，跨包合并需先建 clock.py 并触碰 build 侧模块，字节收益为零。
"""

import datetime as _dt
import json
from typing import Any, List, Sequence

from aitable.client import now_iso


def _now() -> str:
    try:
        return now_iso()
    except Exception:
        return _dt.datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("markdown", "text", "name", "value"):
            if isinstance(v.get(k), str):
                return v[k]
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "\n".join(as_text(x) for x in v)
    return str(v)


def as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        out = []
        for it in v:
            s = (as_text(it) if isinstance(it, dict) else str(it)).strip()
            if s:
                out.append(s)
        return out
    s = as_text(v).strip()
    return [s] if s else []


def join_list(items: Sequence[Any], sep: str = "、") -> str:
    out = []
    for it in items or []:
        s = as_text(it).strip()
        if s and s not in out:
            out.append(s)
    return sep.join(out)


def chunks(items: Sequence[Any], size: int) -> List[List[Any]]:
    n = max(1, int(size))
    return [list(items[i:i + n]) for i in range(0, len(items), n)]
