# -*- coding: utf-8 -*-
"""match 侧共享基础模块（constants + utils + jsonio 合并）。

  * 常量（原 constants.py）
  * ``make_warn`` / ``make_err`` / ``json_dumps_zh`` / ``python_version``（原 utils.py）
  * ``dump_json_doc`` / ``load_json`` / ``strip_md_fence``（原 jsonio.py）

apply_decisions 里与 build 侧**同值**的字面量（JOB_STATUS_OPEN /
COMM_STATUS_ONBOARDED）及 apply/verify 专属常量（MATCH_SOURCE_* /
FILTER_VALUE_CHUNK / EVIDENCE_MAX_LEN / GATE_ITEMS / RECOMMEND_VALUES /
INVALID_PASSED_RATIO_LIMIT）并在本模块。
as_list/as_text 是**同名不同义**，仍禁止合并（见 match/tablevalues.py 头注）。

红线：digest/分片落盘是 `json.dump(..., ensure_ascii=False, indent=1)` 且**无
sort_keys** —— 键插入序即字节。所有 digest 文档一律经 `dump_json_doc` 落盘，
禁止在别处另起 dump 参数。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    # constants
    "DEFAULT_MAX_PER_BATCH", "JOB_STATUS_OPEN", "COMM_STATUS_ONBOARDED",
    "DEFAULT_LOCATION", "MATCH_SOURCE_SYSTEM", "MATCH_SOURCE_MANUAL",
    "FILTER_VALUE_CHUNK", "EVIDENCE_MAX_LEN", "GATE_ITEMS", "RECOMMEND_VALUES",
    "INVALID_PASSED_RATIO_LIMIT", "FIELD_CANDIDATES", "FIELD_JOBS",
    "FIELD_PASSED", "FIELD_REJECTED", "FIELD_MUST_SKILLS", "FIELD_BONUS_SKILLS",
    "FIELD_CANDIDATE_OVERRIDES", "FIELD_BATCH_ID", "FIELD_EVIDENCE",
    "FIELD_RECOMMEND", "FIELD_GATE_DETAIL",
    # utils
    "make_warn", "make_err", "json_dumps_zh", "python_version",
    # jsonio
    "strip_md_fence", "dump_json_doc", "load_json",
]

# =========================================================================== #
# 常量（原 constants.py）
# =========================================================================== #
DEFAULT_MAX_PER_BATCH = 8
JOB_STATUS_OPEN = "招聘中"
COMM_STATUS_ONBOARDED = "已入职"        # 老插件铁律：已入职不参与匹配
DEFAULT_LOCATION = "不限"               # 兜底值

MATCH_SOURCE_SYSTEM = "系统匹配"        # 老插件口径：只有「系统匹配」参与删旧建新
MATCH_SOURCE_MANUAL = "人工匹配"        # 人工匹配一律不动，但要算进岗位统计
FILTER_VALUE_CHUNK = 60                 # 单次 filter 的值个数（dws operands 上限 100，留余量）

EVIDENCE_MAX_LEN = 80                   # evidence 原文引用 ≤80 字
GATE_ITEMS = ("education", "major", "years", "certificates")
RECOMMEND_VALUES = ("推荐", "待定", "不推荐")
#: 编造命中项的 pass 条目占比超过这个值 → 升级为硬错误，整批不写库（防模型语义崩塌）
INVALID_PASSED_RATIO_LIMIT = 0.34

# --------------------------------------------------------------------------- #
# 字段名常量
# --------------------------------------------------------------------------- #
#: digest/decisions 里的数组字段名
FIELD_CANDIDATES = "candidates"
FIELD_JOBS = "jobs"
FIELD_PASSED = "passed"
FIELD_REJECTED = "rejected"
FIELD_MUST_SKILLS = "must_skills"
FIELD_BONUS_SKILLS = "bonus_skills"
FIELD_CANDIDATE_OVERRIDES = "candidate_overrides"
FIELD_BATCH_ID = "batch_id"
FIELD_EVIDENCE = "evidence"
FIELD_RECOMMEND = "recommend"
FIELD_GATE_DETAIL = "gate_detail"


# =========================================================================== #
# 公共小工具（原 utils.py）
# =========================================================================== #
def make_warn(code: str, key: Any, detail: str) -> Dict[str, Any]:
    """构造一条 warning 字典（键序 = 字节序，不变）。"""
    return {"code": code, "key": key, "detail": detail}


def make_err(code: str, key: Any, detail: str) -> Dict[str, Any]:
    """构造一条 error 字典（键序 = 字节序，不变）。"""
    return {"code": code, "key": key, "detail": detail}


def json_dumps_zh(obj: Any) -> str:
    """``json.dumps(obj, ensure_ascii=False)`` 的短名（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)


def python_version() -> str:
    """当前 Python 版本串，如 ``"3.12.1"``。"""
    return "%d.%d.%d" % sys.version_info[:3]


# =========================================================================== #
# JSON 读写原语（原 jsonio.py）
# =========================================================================== #
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
