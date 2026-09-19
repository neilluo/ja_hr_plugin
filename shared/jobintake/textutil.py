# -*- coding: utf-8 -*-
"""jobintake 纯文本小工具。

本模块只服务岗位入库，与 shared/intake/ 的同源小工具刻意不合并。
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from jsonio import write_json    # noqa: E402

__all__ = ["new_batch_id", "today", "clean", "count_hits", "truncate", "write_json"]


def new_batch_id() -> str:
    return "%s-%04x" % (time.strftime("%Y%m%d-%H%M%S"), random.getrandbits(16))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def clean(s: Any) -> Optional[str]:
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        return s or None
    return s


def count_hits(hay: str, keywords: Sequence[str]) -> int:
    if not hay:
        return 0
    low = hay.lower()
    return sum(low.count(k.lower()) for k in keywords if k)


def truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    t = str(text)
    return t[:limit] + "…" if (limit and len(t) > limit) else t
