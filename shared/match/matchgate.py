# -*- coding: utf-8 -*-
"""apply 侧表读写边界（MatchTableGateway）：**唯一持 AITable** 的 IO 类。

原 apply_decisions.py 的 `fetch_comm_status`(L440-469) / `find_stale_match_records`
(L496-531) / `writeback_overrides`(L357-434) / `_ensure_select_options`(L1010-1026)
/ `recompute_job_stats`(L534-569) 与 apply() 内联的 batch_delete / batch_create
分片+重试 / 双回读调用点搬入；dws 调用次数/顺序/分片粒度/filter 构造是 ARGV 层
指纹面，逐字保留。

红线（原样保留，不在本刀修）：
  * `readback_match` 里 `zip(record_ids, row_meta)` 假设服务端按提交顺序返回
    record id（分析报告 §B.7#2 的已知风险）；
  * settle_polls 轮询次数（match 回读 settle_tries=3 / job 与 override 回读
    settle_tries=2）与 shared/aitable/verifier 的等待间隔不动；
  * CREATE_CHUNK 批切的**值**由 apply 入口注入（裁判篡改自证 apply_chunk 的
    定位锚点在入口），本类只消费 create_chunk 参数。

D15 的取数与累加（recompute_job_stats）未按分析草案 D.2 类 5 拆成独立
JobStatsRecomputer：取数/累加交错在分页循环与失败降级（stats.pop）里，拆开需引入
中间数据形状，字节风险大于收益（裁剪理由同 P8 报告 DigestAssembler/Emitter 合并）。
"""

import json
from typing import Any, Dict, List, Sequence

from aitable.client import DwsError
from aitable.schema import AITableConfigError
from aitable.table import AITable
from aitable.values import sanitize_text

from match.applyvalues import as_list, as_text, chunks, join_list
from match.constants import FILTER_VALUE_CHUNK, MATCH_SOURCE_SYSTEM, RECOMMEND_VALUES
from match.overrides import OVERRIDE_WRITEBACK_MAP


class MatchTableGateway:
    """resume/match/job 三表的全部读写点（一次调用干完一批的性能纪律在此）。"""

    def __init__(self, table: AITable):
        self.table = table

    # ---------------------------------------------------------------- resume
    def writeback_overrides(self, cand_index: Dict[str, Dict[str, Any]],
                            warnings: List[str]) -> Dict[str, Any]:
        """把 candidate_overrides 的实际修正**写回简历库**（表是唯一事实源，修正必须持久化）。

        只写有变化且 config 里有对应字段的项；没有 record_id 的候选人只进 warnings
        （修正在本批匹配记录里仍然生效）。选项类字段先 ensure_options（只增不删），
        写后回读（D6）；失败如实进 warnings，不静默。
        """
        out: Dict[str, Any] = {"submitted": 0, "updated": 0, "failed": [],
                               "readback_ok": None, "dws_calls": 0, "elapsed_ms": 0,
                               "skipped_no_record_id": 0}
        have = set(self.table.field_keys("resume") or [])
        updates: List[Dict[str, Any]] = []
        for ck in sorted(cand_index):
            c = cand_index[ck]
            ch = c.get("_override_changes") or {}
            if not ch:
                continue
            rid = c.get("record_id")
            if not rid:
                out["skipped_no_record_id"] += 1
                warnings.append("override 写回：候选人 %s(%s) 没有 record_id，修正（%s）无法写回"
                                "简历库，仅在本批匹配记录里生效"
                                % (as_text(c.get("name")) or "?", ck, ",".join(sorted(ch))))
                continue
            cells: Dict[str, Any] = {}
            for src, dst in OVERRIDE_WRITEBACK_MAP:
                if src not in ch or dst not in have:
                    continue
                v = ch[src]
                if dst == "skills":
                    v = as_list(v)
                elif dst == "certificates":
                    v = join_list(v, "、")[:500]
                if v in (None, "", []):
                    continue
                cells[dst] = v
            if cells:
                updates.append({"record_id": rid, "cells": cells})
        if not updates:
            return out
        # ⚠️ 刻意**不依赖 ensure_options 建选项**（W-G 实测 2026-09-18，G base；
        #    W-H 已根治，2026-09-17）：旧版 ensure_options 走 `field update` 整体覆盖写，
        #    而 `field get` 的选项快照有最终一致性（W-H 实测能读到 7/94 的陈旧快照），
        #    陈旧/中间态快照进 payload 就会触发服务端给选项**重新分配 id**（churn）→
        #    存量记录里按旧 id 引用的多选/单选单元格悬空、值被**静默清空**（W-G 实测把
        #    27 条记录的「技能标签」清掉大半）。现 shared 层的 ensure_options 已改为
        #    「只读 + 延迟补建」（彻底移除 field update）：record create/update 写**选项名**
        #    时服务端自动补建缺失选项且不动已有 id（W-B/W-G/W-H 均实测，W-H 受控实验
        #    585 个存量多选值零丢失）。所以写回直接按名字写，靠 readback 校验兜底；
        #    读回缺值 → warnings 提示重跑（幂等）。
        out["submitted"] = len(updates)
        try:
            res = self.table.batch_update("resume", updates)
        except (DwsError, AITableConfigError) as exc:
            warnings.append("override 写回简历库失败（%s）：修正只在本批匹配记录里生效，"
                            "请重跑本步复核" % str(exc)[:200])
            out["failed"] = [{"record_id": u["record_id"], "reason": str(exc)[:200]} for u in updates]
            return out
        out["updated"] = res.get("updated", 0)
        out["failed"] = res.get("failed") or []
        out["dws_calls"] = res.get("dws_calls", 0)
        out["elapsed_ms"] = res.get("elapsed_ms", 0)
        for f in out["failed"]:
            warnings.append("override 写回失败：%s" % json.dumps(f, ensure_ascii=False)[:200])
        rb = self.table.readback_verify("resume", [u["record_id"] for u in updates],
                                        sorted({k for u in updates for k in u["cells"]}),
                                        expected={u["record_id"]: u["cells"] for u in updates},
                                        settle_tries=2)
        out["readback_ok"] = bool(rb.get("ok"))
        out["dws_calls"] += rb.get("dws_calls", 0)
        if not rb.get("ok"):
            for m in (rb.get("mismatch") or [])[:10]:
                warnings.append("override 写回回读不一致：%s.%s 期望=%r 实得=%r"
                                % (m["record_id"], m["field"], m["expected"], m["actual"]))
            for mid in (rb.get("missing") or [])[:10]:
                warnings.append("override 写回回读不到记录 %s（写入传播延迟，请下一回合重跑复核）" % mid)
        return out

    def fetch_comm_status(self, cand_index: Dict[str, Dict[str, Any]],
                          warnings: List[str]) -> Dict[str, Dict[str, Any]]:
        """1 次查询拿回本批候选人的「沟通状态」等表内事实（表是唯一事实源）。"""
        ids = [c.get("record_id") for c in cand_index.values() if c.get("record_id")]
        out: Dict[str, Dict[str, Any]] = {}
        fields = ["name", "phone", "comm_status", "org", "category", "expected_position",
                  "years_experience", "skills", "education"]
        if ids:
            try:
                recs = self.table.query_records("resume", record_ids=ids, fields=fields)
                for r in recs:
                    out[str(r.get("record_id"))] = r.get("cells") or {}
            except (DwsError, AITableConfigError) as exc:
                warnings.append("按 record_id 查简历表失败（%s），改用姓名查" % str(exc)[:160])
        names = sorted({as_text(c.get("name")).strip() for c in cand_index.values()
                        if as_text(c.get("name")).strip()})
        if len(out) < len(ids) and names:
            for part in chunks(names, FILTER_VALUE_CHUNK):
                try:
                    recs = self.table.query_records("resume", filter={"name": part}, fields=fields,
                                                    all_pages=True)
                except (DwsError, AITableConfigError) as exc:
                    warnings.append("按姓名查简历表失败（%s）" % str(exc)[:160])
                    continue
                for r in recs:
                    cells = r.get("cells") or {}
                    nm = as_text(cells.get("name")).strip()
                    if nm:
                        out.setdefault("name:%s" % nm, cells)
        return out

    # ----------------------------------------------------------------- match
    def find_stale_match_records(self, names: Sequence[str],
                                 warnings: List[str]) -> List[Dict[str, Any]]:
        """查本批候选人在 match 表已有的「系统匹配」记录（幂等删旧用）。

        **一次批量查**（filter 里 name 传多值），不逐条查。人工匹配不动。

        ⚠️ 实测坑（本 worker 发现，W-B 层无法改，只能在这里绕）：
        dws 的 filters **不支持嵌套 or**。`aitable.schema.TableSchema.build_filter` 对
        `{"source":"系统匹配","name":[n1,n2,...]}` 会生成
        `and[ eq(source), or[eq(name,n1), eq(name,n2)...] ]`，服务端直接报
        `INVALID_FILTER_OPERATOR: Invalid filter operator: 'or'. Supported operators:
        [all_of, exist, not_after, any_of, contain, after, none_of, lt, gt, ne, before,
        from_now, date_between, date_eq, exclusive, gte, un_exist, not_before, eq, lte]`
        —— **or 根本不在支持列表里**，只有当它是 filters 的**最外层**时才被接受
        （单字段多值那种情况 build_filter 会把 or 提到最外层，所以能跑通）。
        → 这里只用**单字段多值**（name）当最外层 or 查，`source` 拿回本地再过滤。
        """
        out: List[Dict[str, Any]] = []
        uniq = sorted({n for n in names if n})
        if not uniq:
            return out
        for part in chunks(uniq, FILTER_VALUE_CHUNK):
            try:
                recs = self.table.query_records("match", filter={"name": list(part)},
                                                fields=["name", "job_id", "job_name", "recommend",
                                                        "source"],
                                                all_pages=True)
            except (DwsError, AITableConfigError) as exc:
                warnings.append("查旧「系统匹配」记录失败（%s）→ 本次无法保证幂等，"
                                "可能产生重复匹配记录，请重跑" % str(exc)[:200])
                continue
            for r in recs:
                cells = r.get("cells") or {}
                if as_text(cells.get("source")).strip() == MATCH_SOURCE_SYSTEM:
                    out.append(r)
        return out

    def delete_records(self, stale_ids: Sequence[str]) -> Dict[str, Any]:
        return self.table.batch_delete("match", stale_ids)

    def create_records(self, create_res: Dict[str, Any], rows_to_create: Sequence[Dict[str, Any]],
                       create_chunk: int, warnings: List[str]) -> int:
        """分片 batch_create（契约 ≤100/片）+ 选项失败 ensure_options 后重试一次。

        就地累加 create_res（键序/聚合口径与原实现逐字一致）；返回重试次数
        （原实现现场 `report["retry_count"] += 1`，改由调用方加回，终值不变）。
        """
        retries = 0
        # 契约要求 ≤100 条/片：这里显式分片（aitable.writer 内部也会兜底再切一次）
        for piece in chunks(rows_to_create, create_chunk):
            r = self.table.batch_create("match", piece)
            create_res["created"] += r.get("created", 0)
            create_res["submitted"] += r.get("submitted", 0)
            create_res["record_ids"] += r.get("record_ids", [])
            create_res["failed"] += r.get("failed", [])
            create_res["dws_calls"] += r.get("dws_calls", 0)
            create_res["elapsed_ms"] += r.get("elapsed_ms", 0)
            create_res["isolate_extra_calls"] += r.get("isolate_extra_calls", 0)
        if create_res.get("failed"):
            # 选项不存在导致的失败 → ensure_options 后重试一次（retry_count 记进报告）
            opt_failed = [f for f in create_res["failed"]
                          if "option" in str(f.get("reason", "")).lower()
                          or "选项" in str(f.get("reason", ""))]
            if opt_failed:
                retries += 1
                warnings.append("有 %d 行因选项问题写失败，ensure_options 后重试一次"
                                % len(opt_failed))
                self.ensure_select_options(rows_to_create, warnings)
                retry_rows = [f["row"] for f in opt_failed if isinstance(f.get("row"), dict)]
                res2 = self.table.batch_create("match", retry_rows)
                create_res["created"] += res2.get("created", 0)
                create_res["record_ids"] += res2.get("record_ids", [])
                create_res["dws_calls"] += res2.get("dws_calls", 0)
                still = res2.get("failed", [])
                retried = {(f.get("row_index")) for f in opt_failed}
                create_res["failed"] = [f for f in create_res["failed"]
                                        if f.get("row_index") not in retried] + still
                if res2.get("created"):
                    warnings.append("ensure_options 后重试成功 %d 行（剩余失败 %d 行）"
                                    % (res2.get("created", 0), len(still)))
            for f in create_res["failed"]:
                warnings.append("建匹配记录失败：%s" % json.dumps(f, ensure_ascii=False)[:300])
        return retries

    def ensure_select_options(self, rows: Sequence[Dict[str, Any]],
                              warnings: List[str]) -> None:
        """只在「因选项写失败」时才付这个成本（正常路径省 3 次 dws 调用）。

        注意：shared 层 ensure_options 已是**只读**实现（缺陷1 根治后不再 field update），
        这里调它只是刷新选项池现状 + 把缺失名记入 pending_options；真正把选项补进池子
        的是紧接着的 batch_create 重试——写选项名时服务端自动补建（不动已有 id）。
        """
        try:
            self.table.ensure_options("match", "source", [MATCH_SOURCE_SYSTEM])
            self.table.ensure_options("match", "recommend", list(RECOMMEND_VALUES))
            orgs = sorted({as_text(r.get("org")).strip() for r in rows if r.get("org")})
            if orgs:
                self.table.ensure_options("match", "org", orgs)
        except (DwsError, AITableConfigError) as exc:
            warnings.append("ensure_options 失败（%s）；服务端通常会自动补选项，继续重试写入"
                            % str(exc)[:160])

    def recompute_job_stats(self, job_ids: Sequence[str],
                            warnings: List[str]) -> Dict[str, Dict[str, int]]:
        """D15：从表里查每个受影响岗位的**全部**匹配记录（含人工匹配、含历史批次），重算四个数字。

        **禁止**用本批 decisions 直接累加（会漏历史与人工记录导致统计漂移）。
        """
        stats: Dict[str, Dict[str, int]] = {j: {"total": 0, "recommend": 0, "pending": 0, "reject": 0}
                                            for j in job_ids if j}
        uniq = sorted({j for j in job_ids if j})
        for part in chunks(uniq, FILTER_VALUE_CHUNK):
            try:
                recs = self.table.query_records("match", filter={"job_id": part},
                                                fields=["job_id", "recommend", "source"],
                                                all_pages=True)
            except (DwsError, AITableConfigError) as exc:
                warnings.append("D15 统计重算：查岗位 %s 的全部匹配记录失败（%s）→ "
                                "该批岗位统计**不回填**（宁可不写也不写错）"
                                % (",".join(part[:3]), str(exc)[:160]))
                for j in part:
                    stats.pop(j, None)
                continue
            for r in recs:
                cells = r.get("cells") or {}
                jid = as_text(cells.get("job_id")).strip()
                if not jid or jid not in stats:
                    # 表里可能有本批之外的岗位记录（历史），不影响；只统计受影响的
                    continue
                stats[jid]["total"] += 1
                rec = as_text(cells.get("recommend")).strip()
                if rec == "推荐":
                    stats[jid]["recommend"] += 1
                elif rec == "待定":
                    stats[jid]["pending"] += 1
                elif rec == "不推荐":
                    stats[jid]["reject"] += 1
        return stats

    # ------------------------------------------------------------------- job
    def update_job_stats(self, stat_updates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.table.batch_update("job", stat_updates)   # **一次**回填（D15）

    # --------------------------------------------------------------- 回读 D6
    def readback_match(self, record_ids: Sequence[str], row_meta: Sequence[Dict[str, Any]],
                       warnings: List[str]) -> Dict[str, Any]:
        want = ["name", "job_id", "job_name", "source", "skill_score", "bonus_score",
                "total_score", "recommend", "evidence"]
        want = [w for w in want if w in (self.table.field_keys("match") or [])]
        expected = {}
        for rid, meta in zip(record_ids, row_meta):
            # ⚠️ 实测坑（aitable.values.sanitize_text 文档串）：写入前会净化文本，
            # 所以「写入 vs 读回」的比对基准必须拿 sanitize_text(原值)，否则会误判成不一致。
            expected[rid] = {k: (sanitize_text(v) if isinstance(v, str) else v)
                             for k, v in meta["record"].items() if k in want and v is not None}
        rb_match = self.table.readback_verify("match", record_ids, want,
                                              expected=expected, settle_tries=3)
        if not rb_match.get("ok"):
            for m in (rb_match.get("mismatch") or [])[:20]:
                warnings.append("回读不一致：%s.%s 期望=%r 实得=%r"
                                % (m["record_id"], m["field"], m["expected"], m["actual"]))
            for mid in (rb_match.get("missing") or [])[:20]:
                warnings.append("回读不到刚建的匹配记录 %s（服务端写入传播延迟，"
                                "请在下一回合重跑回读复核）" % mid)
        return rb_match

    def readback_job(self, stat_update_res: Dict[str, Any],
                     stat_updates: Sequence[Dict[str, Any]],
                     warnings: List[str]) -> Dict[str, Any]:
        rb_job = self.table.readback_verify(
            "job", stat_update_res["record_ids"],
            ["stat_total", "stat_recommend", "stat_pending", "stat_reject"],
            expected={u["record_id"]: u["cells"] for u in stat_updates},
            settle_tries=2)
        if not rb_job.get("ok"):
            for m in (rb_job.get("mismatch") or [])[:20]:
                warnings.append("岗位统计回读不一致：%s.%s 期望=%r 实得=%r"
                                % (m["record_id"], m["field"], m["expected"], m["actual"]))
            for jid in (rb_job.get("missing") or [])[:10]:
                warnings.append("岗位统计回读不到记录 %s" % jid)
        return rb_job
