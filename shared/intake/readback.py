# -*- coding: utf-8 -*-
"""ReadBackVerifier：写后回读与核查读路径。

收拢留在 intake_resume.py 的读路径：
  verify_by_filter        阶段8 写后回读：按业务键 filter 查 + 有界轮询 +
                          values_equal 比对 + 附件非空检查（原模块级 def 逐字搬移）
  poll_fixup_attachments  阶段6b 补传附件 batch_update 后的回读轮询（附件非空才算
                          补传成功；DwsError → 告警 + recs=[] 按未确认处理）
  query_records /         去重两处 scan（lib_deduper / phone_deduper）的 query 薄转发
  last_query_pages /      ——deduper.scan 的 `table` 门面：query 参数与结果消费顺序
  last_query_truncated    逐字不变，判定（build_index/decide）仍在 dedupe 包
  known_option_names      known_locs 的 config 选项池读（不发 dws；含原
                          except Exception → [] 防御兜底）

红线（逐字保持）：
  * SETTLE_WAITS = (1.5, 3.0, 4.5) 有界轮询语义：`time.sleep(settle_waits[polls])`
    在 `polls += 1` 之前，顺序不得交换；`polls >= len(settle_waits)` 即停，不空转。
  * verify_by_filter 的 filter ≤100 个 OR/片；`found.setdefault` 一键多条保留第一条；
    cells 按 fieldId 键的映射是 query 返回原形状，本层不做任何转换。
  * 返回 dict 的键名键序被下游直接读（requested/found/mismatch/attachment_missing/
    settle_polls/dws_calls/missing/records），不能动；`dws_calls` 是
    `tbl.dws_calls - calls0` 差值口径，别改成 counter 口径。
  * 6b 轮询的告警文案「补传附件回读查询失败（%s）：按未确认处理，请重跑复核」逐字。
  * 本层只搬 IO 与轮询骨架：重复判定、fixup 名单、FIXUP_ROUND_MAX 上限与 cap 文案、
    其余 DwsError 异常归类全部留在 run()。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from aitable.client import DwsError                 # noqa: E402
from aitable.values import values_equal             # noqa: E402

__all__ = ["ReadBackVerifier", "SETTLE_WAITS"]

#: 写后读回传播延迟（create 后按条件查 ≈2.7s 才可命中）
SETTLE_WAITS = (1.5, 3.0, 4.5)


class ReadBackVerifier:
    """写后回读与核查读路径。`tbl` 可为 None（config 失败不致命）——本类全部方法
    都由 run() 在既有 `if tbl is not None` 门控之下调用，门控本身不搬。"""

    def __init__(self, tbl: Any, warnings: List[str]) -> None:
        self.tbl = tbl
        #: 与 run() 共享同一个 warnings list（6b 轮询告警的 append 落点不变）
        self.warnings = warnings

    # -- 去重 scan 的读 IO 门面（deduper.scan 的 `table` 参数；薄转发） --------
    def query_records(self, table_key: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.tbl.query_records(table_key, **kwargs)

    @property
    def last_query_truncated(self) -> bool:
        return self.tbl.last_query_truncated

    @property
    def last_query_pages(self) -> int:
        return self.tbl.last_query_pages

    # -- config 选项池读（不发 dws） -----------------------------------------
    def known_option_names(self, table_key: str, field_key: str) -> List[str]:
        try:
            return [o.get("name") for o in
                    ((self.tbl.config.get("options") or {}).get(table_key) or {})
                    .get(field_key, []) if o.get("name")]
        except Exception:
            return []

    # -- 阶段6b：补传附件写后回读轮询 -----------------------------------------
    def poll_fixup_attachments(self, ids: Sequence[str]) -> set:
        """写后必回读：附件字段非空才算补传成功（有界轮询，不空转烧调用）。"""
        ok_rids: set = set()
        polls = 0
        while ids:
            try:
                recs = self.tbl.query_records("resume", record_ids=ids,
                                              fields=["attachment"])
            except DwsError as exc:
                self.warnings.append("补传附件回读查询失败（%s）：按未确认处理，请重跑复核"
                                     % str(exc)[:160])
                recs = []
            ok_rids = {r2.get("record_id") for r2 in recs
                       if (r2.get("cells") or {}).get("attachment")}
            if len(ok_rids) >= len(ids) or polls >= len(SETTLE_WAITS):
                break
            time.sleep(SETTLE_WAITS[polls])
            polls += 1
        return ok_rids

    # -- 阶段8：写后回读校验（按 filter 查，一次调用同时拿 record_id 映射与读回值） --
    def verify_by_filter(self, table_key: str, key_field: str,
                         keys: Sequence[str], fields: Sequence[str],
                         expected: Dict[Any, Dict[str, Any]],
                         attach_field: Optional[str] = None,
                         settle_waits: Sequence[float] = SETTLE_WAITS) -> Dict[str, Any]:
        """写后必回读（按业务键 filter 查而不是按 record_id 查。

        为什么不用 `AITable.readback_verify`：`batch_upsert_by_key` 返回的 record_ids 是
        「本片 created ids + updated ids」拼接，**无法可靠对回具体行**；而本脚本必须知道
        每份简历落到哪个 record_id（要写进 candidates.json 给 C2 用）。按手机号 filter 查
        一次就同时拿到 record_id 映射 + 读回值，省一次调用。

        传播延迟：create 后按条件查 ≈2.7s 才可命中，所以这里自带**有界**轮询
        （1.5/3.0/4.5s），绝不空转烧调用；轮询完仍不一致就如实报进 mismatch。
        """
        t0 = time.monotonic()
        calls0 = self.tbl.dws_calls
        keys = [k for k in (keys or []) if k]
        found: Dict[Any, Dict[str, Any]] = {}
        polls = 0
        mismatch: List[Dict[str, Any]] = []
        while True:
            recs: List[Dict[str, Any]] = []
            # filter 的 OR 条件也有 100 个上限，超出必须自己分片（每片仍是一次调用）
            for i in range(0, len(keys), 100):
                recs.extend(self.tbl.query_records(table_key, filter={key_field: keys[i:i + 100]},
                                                   fields=list(fields), limit=100, all_pages=True))
            found = {}
            for r in recs:
                k = (r.get("cells") or {}).get(key_field)
                if isinstance(k, str):
                    k = k.strip()
                if k is None:
                    continue
                found.setdefault(k, r)
            mismatch = []
            for k, exp in (expected or {}).items():
                rec = found.get(k)
                if rec is None:
                    continue
                cells = rec.get("cells") or {}
                for fk, ev in exp.items():
                    if not values_equal(ev, cells.get(fk)):
                        mismatch.append({"key": k, "record_id": rec.get("record_id"),
                                         "field": fk, "expected": ev, "actual": cells.get(fk)})
            missing = [k for k in keys if k not in found]
            if not missing and not mismatch:
                break
            if polls >= len(settle_waits):
                break
            time.sleep(settle_waits[polls])
            polls += 1

        missing = [k for k in keys if k not in found]
        attach_missing: List[Any] = []
        if attach_field:
            for k, rec in found.items():
                if not (rec.get("cells") or {}).get(attach_field):
                    attach_missing.append(k)
        return {
            "ok": not missing and not mismatch,
            "requested": len(keys), "found": len(found), "missing": missing,
            "mismatch": mismatch, "attachment_missing": attach_missing,
            "records": found, "settle_polls": polls,
            "dws_calls": self.tbl.dws_calls - calls0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
