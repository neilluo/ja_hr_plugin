# -*- coding: utf-8 -*-
"""JSON 读写原语（P8 第一刀：build 侧的两个落盘点 + markdown 围栏剥离）。

红线：digest/分片落盘是 `json.dump(..., ensure_ascii=False, indent=1)` 且**无
sort_keys** —— 键插入序即字节。所有 digest 文档一律经 `dump_json_doc` 落盘，
禁止在别处另起 dump 参数。

围栏剥离正则在 verify_decisions.load_json（L190-191）还有一份**逐字相同**的实现；
是否统一到本模块由 P9 逐案裁定（两侧当前行为一致，统一本身安全，但本刀不动 verify）。
"""

import json
import re
from typing import Any


def strip_md_fence(body: str) -> str:
    """容错：agent 有时会给 JSON 带上 markdown 围栏。"""
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
    return body


def dump_json_doc(obj: Any, path: Any) -> None:
    """digest / digest_batch_NN 的唯一落盘原语（参数与原 build 两处写点逐字一致）。"""
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
