# -*- coding: utf-8 -*-
"""jobintake 纯文本小工具（自 intake_job.py 逐字搬移，P9b）。

与 shared/intake/（A 侧）的同源小工具**刻意不合并**：本模块只服务 B 侧岗位入库，
A/B 两侧脚本历史上各自自包含；合并进同一个 textutil 会把两侧未来的口径演化
绑在一起（P6 分析 §3.2 D7 记录了字节相同这一事实，但归并属行为无关的收敛，
不在 P9b「行为逐字节不变」范围内做）。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any, Optional, Sequence

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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
