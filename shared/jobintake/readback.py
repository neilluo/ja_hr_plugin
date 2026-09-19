# -*- coding: utf-8 -*-
"""JobReadBackVerifier：岗位侧写后回读（B 口径）。

  * 岗位名称不唯一，按 job_name filter 查回来的是「一名多条」，再用 job_id /
    （岗位名称,所属部门,组织分类）三元组做五级 fallback 精确归属。
  * SETTLE_WAITS = (1.5, 3.0, 4.5)：轮询次数/间隔逐字。
  * 本方法就地修改 entries（ent["record_id"] / ent["job_id"] 回填）。
  * 返回 dict 的键名键序被下游直接读（含 submitter_missing），不能动。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Sequence

from aitable.values import values_equal             # noqa: E402
from jobintake.constants import SETTLE_WAITS
from jobintake.textutil import clean

__all__ = ["JobReadBackVerifier"]


class JobReadBackVerifier:
    """岗位回读：一次 filter 查询同时完成 record_id 精确归属 + 字段值比对 + 附件非空检查。"""

    def __init__(self, tbl: Any, settle_waits: Sequence[float] = SETTLE_WAITS) -> None:
        self.tbl = tbl
        self.settle_waits = settle_waits

    def verify_jobs(self, entries: Sequence[Dict[str, Any]],
                    fields: Sequence[str], check_attach: bool) -> Dict[str, Any]:
        """岗位侧写后回读。

        与简历侧不同：**岗位名称不唯一**，所以按 job_name filter 查回来的是「一名多条」，
        必须再用 **job_id（全表唯一）** 或 **(岗位名称, 所属部门, 组织分类)** 三元组
        把 record_id 精确对回本批的每一行，否则会串档（把 A 部门的 record_id 写到
        B 部门那条上）。

        传播延迟用**有界**轮询（1.5/3.0/4.5s），不空转烧调用。
        """
        tbl = self.tbl
        settle_waits = self.settle_waits
        t0 = time.monotonic()
        calls0 = tbl.dws_calls
        names = sorted({clean(e["row"]["job_name"]) for e in entries if e.get("row")})
        polls = 0
        group: Dict[str, List[Dict[str, Any]]] = {}
        mismatch: List[Dict[str, Any]] = []
        unresolved: List[str] = []
        attach_missing: List[str] = []
        submitter_missing: List[str] = []
        while True:
            recs: List[Dict[str, Any]] = []
            for i in range(0, len(names), 100):
                recs.extend(tbl.query_records("job", filter={"job_name": names[i:i + 100]},
                                              fields=list(fields), limit=100, all_pages=True))
            group = {}
            for r in recs:
                jn = clean((r.get("cells") or {}).get("job_name"))
                if jn:
                    group.setdefault(jn, []).append(r)

            mismatch, unresolved, attach_missing, submitter_missing = [], [], [], []
            for ent in entries:
                jn = clean(ent["row"]["job_name"])
                cands = group.get(jn) or []
                pick = None
                jid = clean(ent.get("job_id"))
                if jid:
                    pick = next((r for r in cands
                                 if clean((r.get("cells") or {}).get("job_id")) == jid), None)
                if pick is None:
                    dep, org = clean(ent["row"].get("department")), clean(ent["row"].get("org"))
                    same = [r for r in cands
                            if clean((r.get("cells") or {}).get("department")) == dep
                            and clean((r.get("cells") or {}).get("org")) == org]
                    pick = same[0] if len(same) == 1 else (same[0] if same else
                                                           (cands[0] if len(cands) == 1 else None))
                if pick is None:
                    unresolved.append("%s|%s" % (jn, ent["file_name"]))
                    continue
                ent["record_id"] = pick["record_id"]
                if not ent.get("job_id"):
                    ent["job_id"] = clean((pick.get("cells") or {}).get("job_id"))
                cells = pick.get("cells") or {}
                for fk, ev in ent["row"].items():
                    # attachment / submitter(user) 读回结构与写入结构不同构，只查非空不比值
                    if fk in ("attachment", "submitter") or fk not in fields:
                        continue
                    if not values_equal(ev, cells.get(fk)):
                        mismatch.append({"key": jn, "record_id": pick["record_id"], "field": fk,
                                         "expected": ev, "actual": cells.get(fk)})
                if check_attach and not cells.get("attachment"):
                    attach_missing.append("%s(%s)" % (jn, ent.get("job_id") or "-"))
                if "submitter" in fields and ent["row"].get("submitter") \
                        and not cells.get("submitter"):
                    submitter_missing.append("%s(%s)" % (jn, ent.get("job_id") or "-"))
            if not unresolved and not mismatch:
                break
            if polls >= len(settle_waits):
                break
            time.sleep(settle_waits[polls])
            polls += 1

        return {"ok": not unresolved and not mismatch, "requested": len(entries),
                "found": len(entries) - len(unresolved), "missing": unresolved,
                "mismatch": mismatch, "attachment_missing": attach_missing,
                "submitter_missing": submitter_missing,
                "settle_polls": polls, "dws_calls": tbl.dws_calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}
