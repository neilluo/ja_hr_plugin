# -*- coding: utf-8 -*-
"""JobReadBackVerifier：岗位侧写后回读（B 口径，P9b 自 intake_job.verify_jobs 逐字搬移）。

红线（任务书 P9b #3 / P6 分析 §3.2 D2）：
  * 本类与 A 侧 shared/intake/readback.py 的 ReadBackVerifier **不合并**：
    A 按业务键（phone）filter 一对一、`found.setdefault` 一键多条保留第一条；
    B 按 job_name filter **一对多**，再用 job_id /（岗位名称,所属部门,组织分类）
    三元组做五级 fallback 精确归属（job_id 精确 → 三元组唯一 → 三元组多条取第一
    → 候选唯一取它 → None），**归属优先级顺序就是行为**。
  * 与 shared/aitable/verifier.ReadBackVerifier（按 record_ids 查）也不互换；
    run_apply 的迟到二次复核走 aitable 版（经 JobTableGateway.readback_verify），
    同一文件里两套回读是既有形态，保留。
  * SETTLE_WAITS = (1.5, 3.0, 4.5)：`time.sleep(settle_waits[polls])` 在
    `polls += 1` 之前，顺序不得交换；轮询次数/间隔逐字。
  * 本方法**就地修改** entries（ent["record_id"] / ent["job_id"] 回填），是原
    verify_jobs 的既有副作用，编排层阶段 9 依赖它。
  * 返回 dict 的键名键序被下游直接读（含 B 独有的 submitter_missing），不能动。
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
        """岗位侧写后回读（契约 D6）。

        与简历侧不同：**岗位名称不唯一**（19 份真实 JD 里「工程师/高级工程师」在
        硅片制造部-工艺部 / 硅片制造部-设备部 / 组件制造部-工艺部 … 各出现一次），
        所以按 job_name filter 查回来的是「一名多条」，必须再用 **job_id（全表唯一）**
        或 **(岗位名称, 所属部门, 组织分类)** 三元组把 record_id 精确对回本批的每一行，
        否则会串档（把 A 部门的 record_id 写到 B 部门那条上）。

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
