# -*- coding: utf-8 -*-
"""读路径：`record query` 的翻页、record-ids 分片、结果归一化，以及**截断状态**。

设计纪律：
  * **一次调用干完一批**：查重一次 OR filter 查完 N 个键（`build_filter` 在 schema.py）。
  * **`record query --all` 是坏的**：当前 dws 版本带不带 filter 都返回 0 条
    （`hasMore`/`nextCursor` 也都是 null），所以本层一律自己用 `--cursor` 翻页。
  * 每页一次 dws 调用，翻到空页或 `max_pages` 为止。

截断可见：翻到 `max_pages` 仍有下一页时，除了照旧往 warnings 记一条，
还把状态暴露在 `last_pages` / `last_truncated` / `last_returned` 上，让调用方能
把「这次扫描不完整、据此做的去重不可信」如实告诉用户。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from aitable.client import MAX_QUERY_LIMIT, MAX_RECORD_IDS_PER_CALL, DwsClient
from aitable.schema import TableSchema
from aitable.values import val

__all__ = ["RecordQuery"]


class RecordQuery(object):

    def __init__(self, client: DwsClient, schema: TableSchema,
                 warnings: List[str]):
        self.client = client
        self.schema = schema
        self.warnings = warnings
        #: 最近一次 query_records 的翻页数 / 是否因 max_pages 截断 / 实际返回条数
        self.last_pages = 0
        self.last_truncated = False
        self.last_returned = 0

    def query_records(self, table_key: str, filter: Any = None,
                      fields: Optional[Sequence[str]] = None, limit: int = 100,
                      all_pages: bool = False,
                      sort: Optional[Sequence[Dict[str, str]]] = None,
                      record_ids: Optional[Sequence[str]] = None,
                      cursor: Optional[str] = None,
                      max_pages: int = 50) -> List[Dict[str, Any]]:
        """查询记录，返回归一化后的 list[dict]。

        每条形如::

            {"record_id": "recXXX",
             "cells": {"phone": "138...", "education": "本科", "skills": ["PLC", "CAD"]},
             "raw":   {"bYoewPy": "138...", "cWX4BSj": {"id": "opt..", "name": "本科"}}}

        `cells` 的 key 是**业务字段名**、值已过 `val()`（singleSelect 取到 name、
        multipleSelect 是 name 列表但**不保序**）；`raw` 保留 fieldId → 原始值供排查。

        分页：`limit ≤ 100`（服务端硬限制）。`all_pages=True` 时本层**自己用 `--cursor` 翻页**，
        每页一次 dws 调用，翻到空页或 `max_pages`（默认 50 页 ≈5000 条）为止。

        ⚠️ **`record query --all` 在当前 dws 版本返回 0 条记录**（带 filter、不带
        filter 都一样，`hasMore`/`nextCursor` 也都是 null），所以本层绝不用 `--all`。
        """
        self.last_pages = 0
        self.last_truncated = False
        self.last_returned = 0
        schema = self.schema
        tid = schema.table_id(table_key)
        args = ["aitable", "record", "query", "--base-id", schema.base_id,
                "--table-id", tid]
        if record_ids:
            ids = [r for r in record_ids if r]
            if len(ids) > MAX_RECORD_IDS_PER_CALL:
                # 超过 100 个 id 只能分片（服务端硬限制）
                out: List[Dict[str, Any]] = []
                for chunk in self.client.chunks(ids, MAX_RECORD_IDS_PER_CALL):
                    out.extend(self.query_records(table_key, fields=fields,
                                                  record_ids=list(chunk)))
                return out
            args += ["--record-ids", ",".join(ids)]
        else:
            f = schema.build_filter(table_key, filter)
            if f is not None:
                args += ["--filters", json.dumps(f, ensure_ascii=False)]
            if sort:
                args += ["--sort", json.dumps(
                    [{"fieldId": schema.field_id(table_key, s["field_key"])
                      if "field_key" in s else s.get("fieldId"),
                      "direction": s.get("direction", "asc")} for s in sort],
                    ensure_ascii=False)]
        if fields:
            fids = [schema.field_id(table_key, k) for k in fields]
            if len(fids) > MAX_QUERY_LIMIT:
                self.warnings.append("query_records 字段数 %d 超过单次上限 %d，已截断"
                                     % (len(fids), MAX_QUERY_LIMIT))
            args += ["--field-ids", ",".join(fids[:MAX_QUERY_LIMIT])]
        lim = max(1, min(int(limit or MAX_QUERY_LIMIT), MAX_QUERY_LIMIT))
        args += ["--limit", str(lim)]

        page_args = list(args) + (["--cursor", cursor] if cursor else [])
        out = []
        pages = 0
        while True:
            res = self.client.call(page_args, timeout=180)
            data = res["data"] if isinstance(res["data"], dict) else {}
            page = self._normalize_records(table_key, data)
            out.extend(page)
            pages += 1
            if record_ids or not all_pages:
                nc = data.get("nextCursor") or data.get("cursor")
                if nc and not record_ids and not all_pages and len(page) >= lim:
                    self.warnings.append(
                        "query_records 结果可能被截断（还有下一页 nextCursor=%s）；"
                        "需要全量请传 all_pages=True" % nc)
                break
            nc = data.get("nextCursor") or data.get("cursor")
            if not nc or not page or pages >= max_pages:
                if pages >= max_pages and nc:
                    self.warnings.append("query_records 翻到 max_pages=%d 仍有下一页，已停止"
                                         % max_pages)
                    self.last_truncated = True
                break
            page_args = list(args) + ["--cursor", str(nc)]
        self.last_pages = pages
        self.last_returned = len(out)
        return out

    def _normalize_records(self, table_key: str, data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        raw_records = data.get("records") or data.get("items") or []
        rev = self.schema.fid_map(table_key)
        out = []
        for r in raw_records:
            if not isinstance(r, dict):
                continue
            rid = r.get("recordId") or r.get("record_id") or r.get("id")
            raw_cells = r.get("cells") or r.get("fields") or r.get("values") or {}
            cells, unknown = {}, {}
            for fid, v in raw_cells.items():
                bkey = rev.get(fid)
                if bkey:
                    cells[bkey] = val(v)
                else:
                    unknown[fid] = val(v)
            item = {"record_id": rid, "cells": cells, "raw": raw_cells}
            if unknown:
                item["unknown_fields"] = unknown
            out.append(item)
        return out
