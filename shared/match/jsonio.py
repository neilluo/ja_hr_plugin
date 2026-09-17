# -*- coding: utf-8 -*-
"""JSON 读写原语（P8 第一刀：build 侧的两个落盘点 + markdown 围栏剥离）。

红线：digest/分片落盘是 `json.dump(..., ensure_ascii=False, indent=1)` 且**无
sort_keys** —— 键插入序即字节。所有 digest 文档一律经 `dump_json_doc` 落盘，
禁止在别处另起 dump 参数。

围栏剥离正则在原 verify_decisions.load_json（L190-191）有一份**逐字相同**的实现；
P9a 裁定：统一——load_json 整体搬入本模块（围栏剥离走 strip_md_fence，报错文案面
原样保留），verify/apply 两入口共用；verify_decisions 入口留同名薄壳（冻结出口，
旧 apply L77 曾从那里 import）。
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def strip_md_fence(body: str) -> str:
    """容错：agent 有时会给 JSON 带上 markdown 围栏。"""
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
    return body


def dump_json_doc(obj: Any, path: Any) -> None:
    """digest / digest_batch_NN / apply_report 的唯一落盘原语（indent=1 无 sort_keys）。"""
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def load_json(path: Path, label: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """读 JSON，容错 markdown 围栏（agent 有时会用 ```json 包起来）。"""
    if not path.exists():
        return None, {"code": "file_not_found", "key": label,
                      "detail": "%s 不存在：%s" % (label, path)}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, {"code": "file_unreadable", "key": label,
                      "detail": "%s 读不出来：%s: %s" % (label, type(exc).__name__, exc)}
    body = raw.strip()
    stripped = False
    if body.startswith("```"):
        body = strip_md_fence(body)
        stripped = True
    try:
        return json.loads(body), ({"code": "markdown_fence_stripped", "key": label,
                                   "detail": "%s 带 markdown 代码围栏，已剥掉后解析成功" % label}
                                  if stripped else None)
    except Exception as exc:
        return None, {"code": "bad_json", "key": label,
                      "detail": "%s 不是合法 JSON：%s: %s" % (label, type(exc).__name__, exc)}
