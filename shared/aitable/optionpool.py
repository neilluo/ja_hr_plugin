# -*- coding: utf-8 -*-
"""选项池（singleSelect / multipleSelect）：只增不删 + 上限护栏。

本层**绝不调用 `dws aitable field update`**，只做三件事：field get 只读拉回现有选项、
把池里没有的名字记进 pending_options、原样返回现有选项列表。补建延迟到写记录时由服务端自动完成。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from aitable.client import DwsClient
from aitable.schema import TableSchema
from aitable.values import sanitize_text

__all__ = ["OptionPool", "OPTION_LIMIT", "OPTION_WARN_RATIO"]

#: 单个 select 字段的选项上限（记忆库口径）
OPTION_LIMIT = 3000
#: 逼近上限的告警比例
OPTION_WARN_RATIO = 0.9


class OptionPool(object):

    def __init__(self, client: DwsClient, schema: TableSchema, warnings: List[str]):
        self.client = client
        self.schema = schema
        self.warnings = warnings
        #: 记下的待补建选项名：{(table_key, field_key): [name,...]}
        #: 实际补建由服务端在 record create/update/upsert 写选项名时自动完成（见模块文档串）
        self.pending_options: Dict[Tuple[str, str], List[str]] = {}

    def get_field_options(self, table_key: str, field_key: str,
                          use_cache: bool = False) -> List[Dict[str, Any]]:
        """读回单个字段的选项 [{"id","name"}]（一次 `field get`，≤10 字段/次的限制这里只用 1 个）。"""
        if use_cache:
            cached = (self.schema.options_cache.get(table_key) or {}).get(field_key)
            if cached:
                return [{"id": o.get("id"), "name": o.get("name")} for o in cached]
        fid = self.schema.field_id(table_key, field_key)
        res = self.client.call(["aitable", "field", "get",
                                "--base-id", self.schema.base_id,
                                "--table-id", self.schema.table_id(table_key),
                                "--field-ids", fid], timeout=120)
        return self._parse_options(res["data"], fid)

    @staticmethod
    def _parse_options(data: Any, fid: Optional[str] = None) -> List[Dict[str, Any]]:
        fields = []
        if isinstance(data, dict):
            fields = data.get("fields") or data.get("items") or []
        elif isinstance(data, list):
            fields = data
        for f in fields:
            if not isinstance(f, dict):
                continue
            if fid and (f.get("fieldId") or f.get("id")) != fid:
                continue
            cfg = f.get("config") or f.get("property") or {}
            opts = cfg.get("options") or []
            return [{"id": o.get("id") or o.get("optionId"), "name": o.get("name")}
                    for o in opts if isinstance(o, dict)]
        return []

    def ensure_options(self, table_key: str, field_key: str, names: Sequence[str],
                       settle_tries: int = 5) -> List[Dict[str, Any]]:
        """确保 singleSelect/multipleSelect 的选项池最终包含 `names`，返回现有 [{"id","name"}]。

        实现口径见模块文档串（**只读 + 延迟补建**，签名与返回结构不变）。
        `settle_tries` 参数仅为兼容旧签名保留（只读实现没有写传播延迟可轮询），传任意值等效。
        """
        # field_id 先行校验：config 缺映射时照旧抛 AITableConfigError（调用方已有兜底）
        self.schema.field_id(table_key, field_key)
        existing = self.get_field_options(table_key, field_key)
        by_name = {o.get("name") for o in existing if o.get("name") is not None}
        pending = self.pending_options.setdefault((table_key, field_key), [])
        missing: List[str] = []
        for n in names or []:
            n = sanitize_text(str(n)).strip()
            if n and n not in by_name and n not in pending and n not in missing:
                missing.append(n)
        self._guard_option_limit(table_key, field_key, len(existing), missing)
        pending.extend(missing)
        if missing:
            self.warnings.append(
                "ensure_options(%s.%s)：%d 个新选项不在当前选项池（%s%s）；"
                "本层已**不再执行 field update**（会触发 option id churn、静默清空存量"
                "单元格，见方法文档串），这些名字已记入 pending_options，写记录时由服务端"
                "自动补建（不动已有选项 id）"
                % (table_key, field_key, len(missing), "、".join(missing[:5]),
                   "…" if len(missing) > 5 else ""))
        self.schema.options_cache.setdefault(table_key, {})[field_key] = existing
        return existing

    def _guard_option_limit(self, table_key: str, field_key: str, pool_size: int,
                            missing: Sequence[str]) -> None:
        incoming = len(missing or [])
        projected = pool_size + incoming
        floor = int(OPTION_LIMIT * OPTION_WARN_RATIO)
        if projected < floor:
            return
        self.warnings.append(
            "选项池 %s.%s 逼近上限：现有 %d 个 + 本批新增 %d 个 = %d 个，"
            "已达单字段选项上限 %d 的 %.0f%%（≥%d 即告警）；超限后服务端会拒写整批记录，"
            "请合并同义标签或改用文本字段"
            % (table_key, field_key, pool_size, incoming, projected,
               OPTION_LIMIT, OPTION_WARN_RATIO * 100, floor))

    def pending_option_names(self, table_key: str, field_key: str) -> List[str]:
        """`ensure_options` 记下的「待服务端在写记录时自动补建」的选项名（只读视图）。"""
        return list(self.pending_options.get((table_key, field_key), []))
