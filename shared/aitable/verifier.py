# -*- coding: utf-8 -*-
"""写后回读校验（契约 D6：写后必回读，失败可见）。

⚠️ **实测：写入到可读有传播延迟**（update 后 ≈1.2s、create 后按条件查 ≈2.7s 才可命中），
所以给了 `expected` 时本层会**轮询**：不一致就等 `settle_wait` 秒再读，最多
`settle_tries` 次（默认 3 次 → 1.2s/2.4s/3.6s），避免把「还没同步」误报成「写错了」。
不给 expected 时只读一次（无从判断对错，也就不轮询）。轮询是**有界**的，绝不空转烧调用。

比对规则用 `values_equal`（多选按集合、数字按数值、日期按前缀），避免「多选读回不保序」
造成假失败。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from aitable.client import DwsClient
from aitable.query import RecordQuery
from aitable.values import val, values_equal

__all__ = ["ReadBackVerifier"]


class ReadBackVerifier(object):

    def __init__(self, client: DwsClient, query: RecordQuery, warnings: List[str]):
        self.client = client
        self.query = query
        self.warnings = warnings

    def readback_verify(self, table_key: str, record_ids: Sequence[str],
                        field_keys: Sequence[str],
                        expected: Optional[Dict[str, Dict[str, Any]]] = None,
                        settle_tries: int = 3,
                        settle_wait: float = 1.2) -> Dict[str, Any]:
        """一次 `record query --record-ids`（≤100 个/次，超出由 RecordQuery 自动分片）。

        `expected` 可选：{record_id: {业务字段名: 期望值}}。给了就顺手比对。

        返回::

            {"ok": bool, "requested": n, "found": n, "missing": [record_id...],
             "records": [{"record_id":..,"cells":{..}}],
             "values": {record_id: {field_key: value}},
             "mismatch": [{"record_id":..,"field":..,"expected":..,"actual":..}],
             "settle_polls": n, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.client.counter.calls
        ids = [r for r in (record_ids or []) if r]
        polls = 0
        recs, values, missing, mismatch = [], {}, [], []
        while True:
            recs = self.query.query_records(
                table_key, fields=list(field_keys or []) or None,
                record_ids=ids) if ids else []
            values = {r["record_id"]: r["cells"] for r in recs}
            missing = [r for r in ids if r not in values]
            mismatch = []
            if expected:
                for rid, exp in expected.items():
                    got = values.get(rid)
                    if got is None:
                        continue
                    for fk, ev in exp.items():
                        if not values_equal(ev, got.get(fk)):
                            mismatch.append({"record_id": rid, "field": fk,
                                             "expected": val(ev), "actual": got.get(fk)})
            if expected is None or (not mismatch and not missing) or polls >= settle_tries:
                break
            polls += 1
            time.sleep(settle_wait * polls)
        if polls and (mismatch or missing):
            self.warnings.append(
                "readback_verify(%s) 轮询 %d 次后仍有 %d 处不一致 / %d 条读不到"
                % (table_key, polls, len(mismatch), len(missing)))
        return {"ok": not missing and not mismatch, "requested": len(ids),
                "found": len(recs), "missing": missing, "records": recs,
                "values": values, "mismatch": mismatch, "settle_polls": polls,
                "dws_calls": self.client.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
