# -*- coding: utf-8 -*-
"""config.json → 真实 ID / 字段类型的唯一映射层（脚本内零硬编码 ID）。

**不发任何 dws 调用**：本模块只做三件事
  ① 装载 config.json 并暴露 base/table/field 的 ID 与类型查询；
  ② 把业务语义的 filter 翻成 dws 的 filters JSON（字段名 → fieldId）；
  ③ 把业务值按字段类型格式化成 cells 里的写入值（含 attachment 的 fileToken 纪律）。

`types` / `formatters` / `options` 是 config.json 的可选段（缺失时按 text 兜底），
由 replicate/bootstrap 生成。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aitable.values import sanitize_text

__all__ = ["TableSchema", "AITableConfigError", "TEXT_TYPES", "READONLY_TYPES",
           "SELECT_TYPES"]

#: TEXT_TYPES / SELECT_TYPES 现状未被任何路径引用（format_cell 直接按 ftype 字面量分支）；
#: 原值搬迁保留，别处要判类型时用这里的集合，别再抄一份字面量。
TEXT_TYPES = {"text", "telephone", "email", "barcode", "idCard", "primaryDoc"}
READONLY_TYPES = {"formula", "lookup", "filterUp", "creator", "lastModifier",
                  "createdTime", "lastModifiedTime"}
SELECT_TYPES = {"singleSelect", "multipleSelect"}


class AITableConfigError(Exception):
    """config.json 缺 base_id / table / field 映射时抛出（唯一 ID 源）。"""


class TableSchema(object):
    """一个 base 的表/字段映射。所有 table_key / field_key 一律用**业务名**。"""

    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).expanduser().resolve())
        if not Path(self.config_path).exists():
            raise AITableConfigError("config.json 不存在：%s" % self.config_path)
        with open(self.config_path, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.base_id = self.config.get("base_id")
        if not self.base_id:
            raise AITableConfigError("config.json 缺 base_id：%s" % self.config_path)
        self.base_name = self.config.get("base_name", "")
        self._tables = self.config.get("tables") or {}
        self._fields = self.config.get("fields") or {}
        self._types = self.config.get("types") or {}
        self._formatters = self.config.get("formatters") or {}
        self._options_cache = self.config.get("options") or {}

    # -- 元信息 -----------------------------------------------------------
    @property
    def options_cache(self) -> Dict[str, Any]:
        return self._options_cache

    def table_keys(self) -> List[str]:
        return sorted(self._tables.keys())

    def table_id(self, table_key: str) -> str:
        t = self._tables.get(table_key)
        if not t:
            raise AITableConfigError("config.json 里没有表 %r（现有：%s）"
                                     % (table_key, self.table_keys()))
        return t.get("table_id") or t.get("tableId")

    def table_name(self, table_key: str) -> str:
        return (self._tables.get(table_key) or {}).get("name", table_key)

    def field_keys(self, table_key: str) -> List[str]:
        return list((self._fields.get(table_key) or {}).keys())

    def field_id(self, table_key: str, field_key: str) -> str:
        fmap = self._fields.get(table_key) or {}
        fid = fmap.get(field_key)
        if not fid:
            # 容错：允许直接传中文字段名
            for k, v in (self.config.get("field_names") or {}).get(table_key, {}).items():
                if v == field_key:
                    fid = fmap.get(k)
                    break
        if not fid:
            raise AITableConfigError("表 %r 里没有字段 %r（现有：%s）"
                                     % (table_key, field_key, sorted(fmap.keys())))
        return fid

    def field_type(self, table_key: str, field_key: str) -> str:
        return (self._types.get(table_key) or {}).get(field_key, "text")

    def field_formatter(self, table_key: str, field_key: str) -> Optional[str]:
        return (self._formatters.get(table_key) or {}).get(field_key)

    def fid_map(self, table_key: str) -> Dict[str, str]:
        """fieldId → 业务 key 的反向映射。"""
        return {v: k for k, v in (self._fields.get(table_key) or {}).items()}

    # -- filter 构造 ------------------------------------------------------
    def build_filter(self, table_key: str, filter: Any = None) -> Optional[Dict[str, Any]]:
        """把业务语义的 filter 翻成 dws 的 filters JSON（字段名 → fieldId）。

        支持四种写法：
          * `None` → 不过滤（全表，受 limit 限制）
          * `{"phone": ["138...", "139..."]}` → 同一字段多值 OR（**查重就靠它，一次查完 N 个键**）
          * `{"org": "制造中心", "status": "招聘中"}` → 多字段 AND（eq）
          * `{"phone": {"op": "contain", "value": "138"}}` → 显式操作符
            （op ∈ eq/ne/contain/exclusive/exist/un_exist/lt/gt/lte/gte/all_of/any_of/none_of/
              date_eq/before/after/not_before/not_after）
          * 已经是 dws 原生结构（含 "operator" 键）→ 原样透传，但里面的业务字段名会被替换成 fieldId

        注意：singleSelect/multipleSelect 的过滤值传**选项名字面量**，不要传 option id；
        date 字段只能用 date_eq/before/after/not_before/not_after，通用 eq/gte 会静默返回 0 条。
        """
        if filter is None:
            return None
        if isinstance(filter, dict) and "operator" in filter:
            return self._map_raw_filter(table_key, filter)
        if isinstance(filter, dict):
            items = list(filter.items())
        elif isinstance(filter, (list, tuple)):
            items = []
            for part in filter:
                if isinstance(part, dict):
                    items.extend(part.items())
        else:
            raise ValueError("filter 只能是 dict / list[dict] / dws 原生结构，实得 %s"
                             % type(filter).__name__)
        operands = []
        for key, value in items:
            fid = self.field_id(table_key, key)
            if isinstance(value, dict) and "op" in value:
                op = value["op"]
                val_ = value.get("value")
                if op in ("exist", "un_exist"):
                    operands.append({"operator": op, "operands": [fid]})
                else:
                    operands.append({"operator": op, "operands": [fid, val_]})
            elif isinstance(value, (list, tuple, set, frozenset)):
                vals = list(value)
                if len(vals) == 1:
                    operands.append({"operator": "eq", "operands": [fid, vals[0]]})
                else:
                    operands.append({"operator": "or",
                                     "operands": [{"operator": "eq", "operands": [fid, v]}
                                                  for v in vals]})
            elif value is None:
                operands.append({"operator": "un_exist", "operands": [fid]})
            else:
                operands.append({"operator": "eq", "operands": [fid, value]})
        if not operands:
            return None
        if len(operands) == 1:
            only = operands[0]
            # 只有一个条件且它本身就是并列关系（如同一字段多值 OR）→ 直接当最外层，
            # dws 要求 filters 最外层必须是 and / or
            if only.get("operator") in ("and", "or"):
                return only
            return {"operator": "and", "operands": operands}
        return {"operator": "and", "operands": operands}

    def _map_raw_filter(self, table_key: str, node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "operands" and isinstance(v, list) and v and isinstance(v[0], str):
                    head = v[0]
                    try:
                        head = self.field_id(table_key, head)
                    except AITableConfigError:
                        pass
                    out[k] = [head] + [self._map_raw_filter(table_key, x) for x in v[1:]]
                else:
                    out[k] = self._map_raw_filter(table_key, v)
            return out
        if isinstance(node, list):
            return [self._map_raw_filter(table_key, x) for x in node]
        return node

    # -- 写值格式化 -------------------------------------------------------
    def format_cell(self, table_key: str, field_key: str, value: Any,
                    keep_empty: bool = False) -> Tuple[bool, Any, Optional[str]]:
        """把业务值格式化成 cells 里该 fieldId 的写入值。

        返回 (是否要写这个 cell, 值, 错误原因)。错误原因非空时上层应把该行记进 failed。
        """
        ftype = self.field_type(table_key, field_key)
        if ftype in READONLY_TYPES:
            return False, None, "字段 %s 是只读类型 %s，不可写入" % (field_key, ftype)
        if value is None:
            return False, None, None
        if isinstance(value, str) and not value.strip() and not keep_empty:
            return False, None, None

        if ftype == "attachment":
            return self._format_attachment(field_key, value)
        if ftype == "multipleSelect":
            if isinstance(value, str):
                value = [value]
            if isinstance(value, (set, frozenset, tuple)):
                value = list(value)
            if not isinstance(value, list):
                return False, None, "多选字段 %s 需要 list，实得 %s" % (field_key, type(value).__name__)
            seen, out = set(), []
            for it in value:
                if isinstance(it, dict):
                    key = it.get("id") or it.get("name")
                    out.append(it)
                else:
                    key = sanitize_text(str(it))
                    out.append(key)
                if key in seen:
                    continue
                seen.add(key)
            if not out and not keep_empty:
                return False, None, None
            return True, out, None
        if ftype == "singleSelect":
            if isinstance(value, dict):
                return True, value, None
            return True, sanitize_text(str(value)), None
        if ftype == "number":
            if isinstance(value, bool):
                return True, int(value), None
            if isinstance(value, (int, float)):
                return True, value, None
            if isinstance(value, str):
                t = value.strip()
                try:
                    f = float(t)
                except ValueError:
                    return False, None, ("数字字段 %s 收到非数字文本 %r" % (field_key, value[:40]))
                return True, (int(f) if f.is_integer() and "." not in t and "e" not in t.lower()
                              else f), None
            return False, None, "数字字段 %s 收到 %s" % (field_key, type(value).__name__)
        if ftype == "richText":
            if isinstance(value, dict):
                md = value.get("markdown")
                if isinstance(md, str):
                    value = dict(value, markdown=sanitize_text(md))
                return True, value, None
            return True, {"markdown": sanitize_text(str(value))}, None
        if ftype == "checkbox":
            return True, bool(value), None
        if ftype == "date":
            if isinstance(value, (_dt.datetime, _dt.date)):
                return True, value.strftime("%Y-%m-%d"), None
            if isinstance(value, (int, float)):
                return True, int(value), None          # 毫秒时间戳
            return True, sanitize_text(str(value)), None
        if ftype == "url":
            if isinstance(value, dict):
                return True, value, None
            return True, {"text": str(value), "link": str(value)}, None
        if ftype in ("user", "department", "group",
                     "unidirectionalLink", "bidirectionalLink", "geolocation"):
            return True, value, None                   # 结构由调用方负责
        # text / telephone / email / barcode / idCard / 未知类型
        return True, sanitize_text(value if isinstance(value, str) else str(value)), None

    @staticmethod
    def _format_attachment(field_key: str, value: Any) -> Tuple[bool, Any, Optional[str]]:
        """attachment 字段：只接受 fileToken。

        **严禁**在 cells 里直传 `{"url":"https://..."}` 或本地路径 —— 服务端会同步下载，
        10 条记录即触发 TIMEOUT_ERROR。必须先 `upload_attachments` 拿 fileToken。
        写入是**整体覆盖不是追加**。
        """
        items = value if isinstance(value, list) else [value]
        out = []
        for it in items:
            if isinstance(it, dict):
                tok = it.get("fileToken") or it.get("file_token")
                if tok:
                    out.append({"fileToken": tok})
                    continue
                if it.get("url"):
                    return False, None, ("附件字段 %s 收到 {\"url\":...}：严禁直传 URL，"
                                         "服务端会同步下载并 TIMEOUT_ERROR；"
                                         "请先 upload_attachments 拿 fileToken" % field_key)
                return False, None, "附件字段 %s 收到无法识别的对象：%s" % (
                    field_key, json.dumps(it, ensure_ascii=False)[:120])
            if isinstance(it, str):
                s = it.strip()
                if s.startswith("ft_") or re.match(r"^[A-Za-z0-9_-]{16,}$", s):
                    out.append({"fileToken": s})
                    continue
                return False, None, ("附件字段 %s 收到字符串 %r：既不是 fileToken 也不允许"
                                     "（本地路径/URL 都要先 upload_attachments）" % (field_key, s[:80]))
            return False, None, "附件字段 %s 收到 %s" % (field_key, type(it).__name__)
        if not out:
            return False, None, None
        return True, out, None

    def build_cells(self, table_key: str, row: Dict[str, Any],
                    keep_empty: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        """业务 key 的 row → {fieldId: 写入值}；同时返回该行的错误原因列表。"""
        cells: Dict[str, Any] = {}
        errors: List[str] = []
        for key, value in row.items():
            if key in ("record_id", "recordId", "_key", "_meta"):
                continue
            try:
                fid = self.field_id(table_key, key)
            except AITableConfigError as exc:
                errors.append(str(exc))
                continue
            ok, formatted, reason = self.format_cell(table_key, key, value, keep_empty)
            if reason:
                errors.append(reason)
            if ok:
                cells[fid] = formatted
        return cells, errors
