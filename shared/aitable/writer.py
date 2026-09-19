# -*- coding: utf-8 -*-
"""写路径：批量 create/update/upsert/delete + 整片失败二分定位 + 行数上限护栏。

批量写 ≤100 条/次（服务端硬限制），整片失败时二分定位坏行，幂等 upsert 先批量查再一次提交。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aitable.client import (MAX_QUERY_LIMIT, MAX_RECORDS_PER_CALL,
                            MAX_RECORD_IDS_PER_CALL, DwsClient, DwsError)
from aitable.query import RecordQuery
from aitable.schema import TableSchema
from aitable.values import val
from aitable.verifier import ReadBackVerifier

__all__ = ["RecordWriter", "SAFE_UPDATE_CHUNK", "MAX_FILTER_OPERANDS",
           "ROW_LIMIT_FREE_TIER", "ROW_LIMIT_WARN_RATIO"]

#: 一次 filter 查询里最多塞多少个 OR 条件（超出自动分片查询）
MAX_FILTER_OPERANDS = 100
#: `batch_update_verified` 的单片条数上限。
#: 对刚批量创建出来的记录，一次 update 12~19 条时写入可能要几分钟后才可读；
#: ≤10 条、或拆成多次、或逐条发，大多数轮次都能 1~2s 内读到（不是 100%，所以还要配回读校验）。
SAFE_UPDATE_CHUNK = 10
#: 免费版单表行数上限（记忆库口径）
ROW_LIMIT_FREE_TIER = 20000
#: 逼近上限的告警比例
ROW_LIMIT_WARN_RATIO = 0.9


class RecordWriter(object):

    def __init__(self, client: DwsClient, schema: TableSchema, query: RecordQuery,
                 verifier: ReadBackVerifier, warnings: List[str]):
        self.client = client
        self.schema = schema
        self.query = query
        self.verifier = verifier
        self.warnings = warnings
        self._row_counts: Dict[str, Optional[int]] = {}

    # -- 行数上限护栏 ---------------------------------------------
    def set_row_count(self, table_key: str, count: int) -> None:
        """把已知的表行数喂进来（调用方已经从别的查询里免费拿到时用，省一次 stats 调用）。"""
        self._row_counts[table_key] = int(count)

    def row_count(self, table_key: str) -> Optional[int]:
        """表行数（按表缓存）。拿不到就返回 None —— 护栏是尽力而为，绝不阻塞写入。"""
        if table_key in self._row_counts:
            return self._row_counts[table_key]
        n = self._fetch_row_count(table_key)
        self._row_counts[table_key] = n
        return n

    def _fetch_row_count(self, table_key: str) -> Optional[int]:
        keys = self.schema.field_keys(table_key)
        if not keys:
            return None
        try:
            fid = self.schema.field_id(table_key, keys[0])
            res = self.client.call(
                ["aitable", "record", "stats", "--base-id", self.schema.base_id,
                 "--table-id", self.schema.table_id(table_key),
                 "--stats", json.dumps([{"fieldId": fid, "statsType": "COUNT"}])],
                timeout=120)
        except Exception:                     # 统计失败绝不阻塞写入
            return None
        return _dig_count(res.get("data"))

    def guard_row_limit(self, table_key: str, incoming: int = 0) -> None:
        n = self.row_count(table_key)
        if n is None:
            return
        projected = n + max(0, int(incoming))
        floor = int(ROW_LIMIT_FREE_TIER * ROW_LIMIT_WARN_RATIO)
        if projected >= floor:
            self.warnings.append(
                "表 %s 行数逼近上限：现有 %d 行 + 本次待写 %d 行 = %d 行，"
                "已达免费版单表上限 %d 行的 %.0f%%（≥%d 行即告警）；"
                "再写可能被服务端拒收，请归档旧记录或升级套餐"
                % (table_key, n, max(0, int(incoming)), projected,
                   ROW_LIMIT_FREE_TIER, ROW_LIMIT_WARN_RATIO * 100, floor))

    # -- 单片提交 ---------------------------------------------------------
    def _write_chunk(self, cmd: str, tid: str, chunk: List[Dict[str, Any]],
                     origin: List[Dict[str, Any]], id_keys: Sequence[str],
                     isolate: bool) -> Tuple[List[Any], List[Dict[str, Any]], int]:
        """提交一片记录（一次 dws 调用）；整片失败时**二分递归**定位坏行。

        返回 (成功 recordId 列表, failed 列表, 二分额外调用次数)。
        """
        args = ["aitable", "record", cmd, "--base-id", self.schema.base_id,
                "--table-id", tid]
        try:
            res = self.client.call_with_payload(args, list(chunk), timeout=240)
        except DwsError as exc:
            if exc.category == "auth" or not isolate or len(chunk) == 1:
                return [], [{"row_index": o.get("index"), "row": o.get("row"),
                             "reason": "%s 失败：%s" % (cmd, exc.message[:200]),
                             "code": exc.code, "category": exc.category} for o in origin], 0
            mid = len(chunk) // 2
            li, lf, lc = self._write_chunk(cmd, tid, chunk[:mid], origin[:mid], id_keys, isolate)
            ri, rf, rc = self._write_chunk(cmd, tid, chunk[mid:], origin[mid:], id_keys, isolate)
            extra = lc + rc + 1
            self.warnings.append(
                "record %s 一片 %d 条整体失败（%s/%s），已二分定位坏行，额外 %d 次调用"
                % (cmd, len(chunk), exc.category, exc.code, extra))
            return li + ri, lf + rf, extra
        ids = self._extract_ids(res["data"], id_keys)
        failed: List[Dict[str, Any]] = []
        data = res["data"] if isinstance(res["data"], dict) else {}
        if isinstance(data, dict) and data.get("failedCount"):
            self.warnings.append("record %s 返回 failedCount=%s" % (cmd, data.get("failedCount")))
        if ids and len(ids) < len(chunk):
            self.warnings.append("record %s 返回 %d 个 id < 提交 %d 条，未返回 id 的行需回读确认"
                                 % (cmd, len(ids), len(chunk)))
            for o in origin[len(ids):]:
                failed.append({"row_index": o.get("index"), "row": o.get("row"),
                               "reason": "服务端未返回 recordId（可能未写入），需回读确认"})
        if not ids and cmd == "update":
            ids = [o.get("record_id") for o in origin if o.get("record_id")]
        return ids, failed, 0

    # -- 批量新增 / 更新 --------------------------------------------------
    def batch_create(self, table_key: str, rows: Sequence[Dict[str, Any]],
                     keep_empty: bool = False, isolate_failures: bool = True) -> Dict[str, Any]:
        """批量新增。内部按 100 条分片（服务端硬限制），每片**一次** dws 调用。

        `rows`：业务字段名 → 值 的 dict 列表（`_meta` / `record_id` 等下划线开头的键会被忽略）。
        整片被服务端拒（如某个 email 格式非法）时，`isolate_failures=True` 会二分定位坏行、
        只把坏行记进 failed，好行照常入库。

        返回::

            {"created": n, "failed": [{"row_index":i,"row":{...},"reason":"业务话","code":...}],
             "record_ids": [...], "submitted": n, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        tid = self.schema.table_id(table_key)
        payloads, failed = [], []
        origin: List[Dict[str, Any]] = []
        for i, row in enumerate(rows or []):
            cells, errors = self.schema.build_cells(table_key, row, keep_empty)
            if errors:
                failed.append({"row_index": i, "row": row, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row_index": i, "row": row, "reason": "该行没有任何可写字段"})
                continue
            payloads.append({"cells": cells})
            origin.append({"index": i, "row": row})

        self.guard_row_limit(table_key, len(payloads))

        record_ids: List[Any] = []
        extra_calls = 0
        for start in range(0, len(payloads), MAX_RECORDS_PER_CALL):
            chunk = payloads[start:start + MAX_RECORDS_PER_CALL]
            ori = origin[start:start + MAX_RECORDS_PER_CALL]
            ids, f, extra = self._write_chunk("create", tid, chunk, ori,
                                              ("newRecordIds", "recordIds",
                                               "createdRecordIds", "records"),
                                              isolate_failures)
            record_ids.extend([r for r in ids if r])
            failed.extend(f)
            extra_calls += extra
        return {"created": len(record_ids), "failed": failed, "record_ids": record_ids,
                "submitted": len(payloads), "isolate_extra_calls": extra_calls,
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_update(self, table_key: str, updates: Sequence[Dict[str, Any]],
                     keep_empty: bool = False, isolate_failures: bool = True) -> Dict[str, Any]:
        """批量更新。`updates` = [{"record_id": "recXXX", "cells": {业务字段名: 值}}, ...]。

        一次调用最多 100 条（超出自动分片）。只传需要改的字段，未传的保持原值。
        **attachment 字段是整体覆盖不是追加**：要保留原附件必须把原 fileToken 一起写回。
        """
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        tid = self.schema.table_id(table_key)
        payloads, failed = [], []
        origin: List[Dict[str, Any]] = []
        for i, u in enumerate(updates or []):
            rid = u.get("record_id") or u.get("recordId")
            row = u.get("cells") if isinstance(u.get("cells"), dict) else u
            if not rid:
                failed.append({"row_index": i, "row": u, "reason": "缺 record_id"})
                continue
            cells, errors = self.schema.build_cells(table_key, row or {}, keep_empty)
            if errors:
                failed.append({"row_index": i, "row": u, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row_index": i, "row": u, "reason": "没有可更新的字段"})
                continue
            payloads.append({"recordId": rid, "cells": cells})
            origin.append({"index": i, "row": u, "record_id": rid})

        updated_ids: List[Any] = []
        extra_calls = 0
        for start in range(0, len(payloads), MAX_RECORDS_PER_CALL):
            chunk = payloads[start:start + MAX_RECORDS_PER_CALL]
            ori = origin[start:start + MAX_RECORDS_PER_CALL]
            ids, f, extra = self._write_chunk("update", tid, chunk, ori,
                                              ("updatedRecordIds", "recordIds", "records"),
                                              isolate_failures)
            updated_ids.extend([r for r in ids if r])
            failed.extend(f)
            extra_calls += extra
        return {"updated": len(updated_ids), "failed": failed,
                "record_ids": [p["recordId"] for p in payloads],
                "submitted": len(payloads), "isolate_extra_calls": extra_calls,
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_update_verified(self, table_key: str, updates: Sequence[Dict[str, Any]],
                              chunk_size: int = SAFE_UPDATE_CHUNK,
                              settle_tries: int = 2,
                              fallback_single: bool = False) -> Dict[str, Any]:
        """`batch_update` + 写后回读 + **有界**重试。回填统计数字这类「必须写成功」的场景用它。

        对刚批量创建出来的记录做 update，写入可能要几分钟后才可读，期间当场重试无效。
        所以本方法只做**有界**重试（每片重发一次），然后把仍未生效的记录如实报进 `failed`
        （失败可见，后续回合重跑该步），**绝不空转烧调用**。

        流程（每片 ≤ chunk_size 条，默认 10）：update → readback_verify(expected=本次写入值)
        → 不一致就把该片重发一次 → 仍不一致 → 记 failed。
        `fallback_single=True` 时再逐条重发一次（默认关闭：「一开始就逐条发」有效，
        但「批量发失败后再逐条补」在延迟期内同样无效）。

        返回::

            {"updated": n, "verified": n, "recovered": n, "failed": [{"record_id","reason"}],
             "verify_ok": bool, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        normalized: List[Tuple[str, Dict[str, Any]]] = []
        failed: List[Dict[str, Any]] = []
        for u in updates or []:
            rid = u.get("record_id") or u.get("recordId")
            row = u.get("cells") if isinstance(u.get("cells"), dict) else u
            if not rid:
                failed.append({"record_id": None, "reason": "缺 record_id"})
                continue
            normalized.append((rid, dict(row or {})))

        verified = 0
        recovered = 0
        size = max(1, int(chunk_size))
        for start in range(0, len(normalized), size):
            piece = normalized[start:start + size]
            pending = piece
            for attempt in (1, 2):
                res = self.batch_update(table_key,
                                        [{"record_id": rid, "cells": cells}
                                         for rid, cells in pending])
                failed.extend(res["failed"])
                expected = {rid: cells for rid, cells in pending}
                rb = self.verifier.readback_verify(
                    table_key, [rid for rid, _ in pending],
                    sorted({k for _, c in pending for k in c}),
                    expected=expected, settle_tries=settle_tries)
                bad = {m["record_id"] for m in rb["mismatch"]} | set(rb["missing"])
                if not bad:
                    verified += len(pending)
                    if attempt == 2:
                        recovered += len(pending)
                    pending = []
                    break
                pending = [(rid, cells) for rid, cells in pending if rid in bad]
                self.warnings.append(
                    "batch_update_verified(%s) 第 %d 片第 %d 次写入后仍有 %d 条未生效"
                    % (table_key, start // size + 1, attempt, len(pending)))
            if pending and fallback_single:
                # 逐条重发（仅在「一开始就逐条发」的场景有效；丢写生效期间同样无效）
                for rid, cells in pending:
                    self.batch_update(table_key, [{"record_id": rid, "cells": cells}])
                rb = self.verifier.readback_verify(
                    table_key, [rid for rid, _ in pending],
                    sorted({k for _, c in pending for k in c}),
                    expected={rid: cells for rid, cells in pending},
                    settle_tries=settle_tries)
                bad = {m["record_id"] for m in rb["mismatch"]} | set(rb["missing"])
                good = [rid for rid, _ in pending if rid not in bad]
                verified += len(good)
                recovered += len(good)
                pending = [(rid, cells) for rid, cells in pending if rid in bad]
            for rid, _ in pending:
                failed.append({
                    "record_id": rid,
                    "reason": "写入返回成功但当场回读不到新值（新建记录的批量 update "
                              "可能要几分钟后才可读，期间重试无效）。请在后续回合重跑本步回填复核；"
                              "详见 aitable.writer.batch_update_verified 文档串",
                    "category": "server_write_lag"})
        return {"updated": len(normalized), "verified": verified, "recovered": recovered,
                "failed": failed, "verify_ok": not failed,
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_delete(self, table_key: str, record_ids: Sequence[str]) -> Dict[str, Any]:
        """批量删除（不可逆）。一次最多 100 个 id，超出自动分片。"""
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        tid = self.schema.table_id(table_key)
        ids = [r for r in (record_ids or []) if r]
        failed, deleted = [], 0
        for chunk in self.client.chunks(ids, MAX_RECORD_IDS_PER_CALL):
            try:
                self.client.call(["aitable", "record", "delete",
                                  "--base-id", self.schema.base_id,
                                  "--table-id", tid, "--record-ids", ",".join(chunk)],
                                 timeout=180)
                deleted += len(chunk)
            except DwsError as exc:
                failed.append({"record_ids": list(chunk),
                               "reason": "批量删除失败：%s" % exc.message[:200],
                               "code": exc.code, "category": exc.category})
        return {"deleted": deleted, "failed": failed,
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    # -- 幂等 upsert ------------------------------------------------------
    def batch_upsert_by_key(self, table_key: str, unique_field: str,
                            rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """按业务唯一键幂等写入。

        流程（**调用次数已压到最小**）：
          1. 一次 OR filter 查询把 N 个键全部查出来（N>100 才分片，每片仍是一次调用）；
          2. 命中 → 带 recordId 走 update；未命中 → 走 create；
          3. 用 `record upsert` 一次提交（≤100 条/片）；该命令不可用时自动回退成
             create + update 两次调用。

        返回::

            {"created": n, "updated": n, "failed": [{"row","reason","code"}],
             "record_ids": [...], "dws_calls": n, "elapsed_ms": n,
             "existing": {键: recordId}}

        纪律：库内同键命中**多条**、或本批内键重复 → 该行进 failed 并说明，
        **不自动挑一条覆盖**（沿用老插件「同号多条即停止并报告」的规则）。
        """
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        tid = self.schema.table_id(table_key)
        fid_key = self.schema.field_id(table_key, unique_field)
        failed: List[Dict[str, Any]] = []

        # --- 收集键 ---
        keyed: List[Tuple[Any, Dict[str, Any]]] = []
        for i, row in enumerate(rows or []):
            k = row.get(unique_field)
            k = val(k)
            if k is None or (isinstance(k, str) and not k.strip()):
                failed.append({"row_index": i, "row": row,
                               "reason": "缺唯一键 %s，无法幂等判定" % unique_field})
                continue
            keyed.append((k if not isinstance(k, str) else k.strip(), row))

        # --- 本批内重复键：后来的进 failed，不静默覆盖 ---
        seen_in_batch: Dict[Any, int] = {}
        deduped: List[Tuple[Any, Dict[str, Any]]] = []
        for k, row in keyed:
            if k in seen_in_batch:
                failed.append({"row": row, "reason": "本批内唯一键 %s=%s 重复（第 %d 行已用该键），"
                                                    "未写入" % (unique_field, k, seen_in_batch[k])})
                continue
            seen_in_batch[k] = len(deduped)
            deduped.append((k, row))

        # --- 一次性批量查已存在键（不逐条查！） ---
        existing: Dict[Any, str] = {}
        duplicated: Dict[Any, int] = {}
        keys = [k for k, _ in deduped]
        row_of = {k: row for k, row in deduped}
        for chunk in self.client.chunks(keys, MAX_FILTER_OPERANDS):
            try:
                found = self.query.query_records(
                    table_key, filter={unique_field: list(chunk)},
                    fields=[unique_field], limit=MAX_QUERY_LIMIT, all_pages=True)
            except DwsError as exc:
                for k in chunk:
                    failed.append({"row": row_of.get(k),
                                   "reason": "查重查询失败：%s" % exc.message[:200],
                                   "code": exc.code, "category": exc.category})
                continue
            for rec in found:
                v = val((rec.get("raw") or {}).get(fid_key))
                if isinstance(v, str):
                    v = v.strip()
                if v is None:
                    continue
                if v in existing:
                    duplicated[v] = duplicated.get(v, 1) + 1
                else:
                    existing[v] = rec["record_id"]

        # --- 拆 create / update ---
        payloads = []
        for k, row in deduped:
            if k in duplicated:
                failed.append({"row": row,
                               "reason": "库内 %s=%s 命中 %d 条记录，停止不自动选（需人工确认）"
                                         % (unique_field, k, duplicated[k])})
                continue
            cells, errors = self.schema.build_cells(table_key, row)
            if errors:
                failed.append({"row": row, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row": row, "reason": "该行没有任何可写字段"})
                continue
            rid = existing.get(k)
            payloads.append(({"recordId": rid, "cells": cells} if rid else {"cells": cells},
                             bool(rid), row))

        self.guard_row_limit(table_key, sum(1 for _, is_upd, _ in payloads if not is_upd))

        created = updated = 0
        record_ids: List[str] = []
        if payloads:
            created, updated, record_ids, fail2 = self._submit_upsert(tid, payloads)
            failed.extend(fail2)

        return {"created": created, "updated": updated, "failed": failed,
                "record_ids": record_ids, "existing": {str(k): v for k, v in existing.items()},
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def _submit_upsert(self, tid: str, payloads: Sequence[Tuple[Dict[str, Any], bool, Dict[str, Any]]]
                       ) -> Tuple[int, int, List[str], List[Dict[str, Any]]]:
        """提交 upsert：优先 `record upsert`（一次调用同时处理增改），失败回退 create+update。"""
        created = updated = 0
        record_ids: List[str] = []
        failed: List[Dict[str, Any]] = []
        for chunk in self.client.chunks(list(payloads), MAX_RECORDS_PER_CALL):
            body = [p for p, _, _ in chunk]
            try:
                res = self.client.call_with_payload(
                    ["aitable", "record", "upsert", "--base-id", self.schema.base_id,
                     "--table-id", tid], body, timeout=240)
            except DwsError as exc:
                if exc.category in ("invalid", "not_found", "unknown"):
                    self.warnings.append("record upsert 不可用（%s/%s），回退 create+update"
                                         % (exc.category, exc.code))
                    c, u, ids, f2 = self._submit_split(tid, chunk)
                    created += c
                    updated += u
                    record_ids.extend(ids)
                    failed.extend(f2)
                    continue
                for _, _, row in chunk:
                    failed.append({"row": row, "reason": "upsert 失败：%s" % exc.message[:200],
                                   "code": exc.code, "category": exc.category})
                continue
            data = res["data"] or {}
            c_ids = self._extract_ids(data, ("createdRecordIds", "newRecordIds"))
            u_ids = self._extract_ids(data, ("updatedRecordIds",))
            if not c_ids and not u_ids:
                # 服务端没回 id 明细：按提交内容推断
                c_ids = [None] * sum(1 for _, is_upd, _ in chunk if not is_upd)
                u_ids = [None] * sum(1 for _, is_upd, _ in chunk if is_upd)
                self.warnings.append("record upsert 未返回 id 明细，按提交内容计数")
            created += len(c_ids)
            updated += len(u_ids)
            record_ids.extend([r for r in (list(c_ids) + list(u_ids)) if r])
        return created, updated, record_ids, failed

    def _submit_split(self, tid: str, chunk: Sequence[Tuple[Dict[str, Any], bool, Dict[str, Any]]]
                      ) -> Tuple[int, int, List[str], List[Dict[str, Any]]]:
        """upsert 不可用时的回退路径：拆成 create + update 两次调用（各自也能二分定位坏行）。"""
        creates = [(p, row) for p, is_upd, row in chunk if not is_upd]
        updates = [(p, row) for p, is_upd, row in chunk if is_upd]
        created = updated = 0
        ids: List[str] = []
        failed: List[Dict[str, Any]] = []
        if creates:
            cids, cf, _ = self._write_chunk(
                "create", tid, [p for p, _ in creates],
                [{"index": None, "row": row} for _, row in creates],
                ("newRecordIds", "recordIds", "records"), True)
            created += len(cids)
            ids.extend([r for r in cids if r])
            failed.extend(cf)
        if updates:
            uids, uf, _ = self._write_chunk(
                "update", tid, [p for p, _ in updates],
                [{"index": None, "row": row, "record_id": p["recordId"]}
                 for p, row in updates],
                ("updatedRecordIds", "recordIds", "records"), True)
            updated += len(uids)
            ids.extend([r for r in uids if r])
            failed.extend(uf)
        return created, updated, ids, failed

    @staticmethod
    def _extract_ids(data: Any, keys: Sequence[str]) -> List[Any]:
        if not isinstance(data, dict):
            return []
        for k in keys:
            v = data.get(k)
            if isinstance(v, list) and v:
                out = []
                for it in v:
                    if isinstance(it, dict):
                        out.append(it.get("recordId") or it.get("record_id") or it.get("id"))
                    else:
                        out.append(it)
                return [x for x in out if x]
            if isinstance(v, list):
                return []
        return []


def _dig_count(data: Any) -> Optional[int]:
    """从 `record stats` 的响应里挖出总行数。

    响应形态::

        {"results": [{"calcVersion": 28, "dataVersion": 28, "deltaVersion": 28,
                      "results": [{"fieldId": "mUQYiBr", "statsType": "COUNT",
                                   "value": 28}]}]}

    `COUNT` 就是记录总数（与 `record query` 逐页点数一致）。形态不符时退回通用挖掘，
    再挖不到就返回 None —— 护栏静默跳过，**绝不因为统计失败而阻塞写入**。
    """
    if isinstance(data, dict):
        for group in data.get("results") or []:
            if not isinstance(group, dict):
                continue
            for item in group.get("results") or []:
                if (isinstance(item, dict) and item.get("statsType") == "COUNT"
                        and isinstance(item.get("value"), int)
                        and not isinstance(item.get("value"), bool)):
                    return int(item["value"])
    return _dig_count_generic(data)


def _dig_count_generic(data: Any) -> Optional[int]:
    nodes: List[Any] = [data]
    if isinstance(data, dict):
        for k in ("stats", "items", "result", "results", "data", "statList"):
            if k in data:
                nodes.append(data[k])
    for node in nodes:
        if isinstance(node, bool):
            continue
        if isinstance(node, int):
            return node
        if isinstance(node, str) and node.isdigit():
            return int(node)
        if isinstance(node, dict):
            for k in ("COUNT", "count", "value", "total", "rowCount", "statValue"):
                if k in node:
                    got = _dig_count_generic(node[k])
                    if got is not None:
                        return got
        if isinstance(node, list):
            for it in node:
                got = _dig_count_generic(it)
                if got is not None:
                    return got
    return None
