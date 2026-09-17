# -*- coding: utf-8 -*-
"""apply 编排（ApplyOrchestrator）：原 apply_decisions.apply（L575-994，420 行
上帝函数）的 8 步分解。原代码的编号注释（L594/625/683/739/753/881/908/943）就是
阶段切分线；跨阶段局部量提升为实例属性（沿 P7 IntakePipeline / P8 DigestBuilder
黑板手法），每个阶段方法与原代码块**逐字**对应。

    _load_inputs()        1. 读 decisions / digest + config→AITable（dry-run 时 table=None）
    _verify_decisions()   2. 先校验（不通过就不写库，契约 D6）；含无 digest 的两条降级路径
    _merge_context()      索引构建 + overrides 合入/写回 + report["overrides*"]
    _fetch_table_facts()  3. 表内事实：沟通状态（已入职铁律）+ 删旧名单
    _delete_stale()       4. 幂等：批量查旧「系统匹配」记录 → 一次批量删
    _create_records()     5. 批量建匹配记录（只建 passed 且候选人未入职的）+ 受影响岗位集合
    _refresh_job_stats()  6. D15：岗位统计脚本重算 + 一次 batch_update 回填
    _readback()           7. 写后必回读（D6）
    _assemble_report()    8. 用户可读清单 + summary/job_stats/readback 组装 + ok 判定

红线（原样保留）：
  * `warnings is report["warnings"]` 的**单一 sink 别名**（原 L589-592 注释：第一版
    漏了别名导致 report.warnings 恒空，违反 D6「失败可见」）；
  * report 顶层键插入序与条件插入点（overrides/overrides_writeback/verify_problems/
    turns_saved_estimate/skipped_invalid/dws_stats/readback/delete/create/stat_update
    → _finish 追加 exit_code/python/_report_path）= 字节面，逐字保留；
  * `--sem-*`/`--no-semantic-guards` 在 apply 路径**不可达**（原 L646 只传
    (digest, decisions, check_coverage=…)）——现有 API 面不对称，不显式授权不修；
  * dry-run 时 table=None、dws_calls=0（全部表操作分支短路）。

依赖注入（入口装配）：verify_fn = verify_decisions 入口薄壳（同进程复用同一个
verify()，且让入口 `_recommend_of` 锚点行为支配 apply 路径）；job_parser =
match.jobparse.JobRecordParser（入口按 _LateBoundExtractor 接线）；create_chunk =
入口 CREATE_CHUNK（裁判篡改自证 apply_chunk 的定位锚点在入口）。
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aitable.table import AITable

from match.applyreport import ApplyReportBuilder
from match.applyvalues import as_list, as_text, join_list, _now
from match.constants import COMM_STATUS_ONBOARDED
from match.decisionctx import DecisionContextBuilder, resolve_job
from match.jsonio import dump_json_doc, load_json
from match.matchgate import MatchTableGateway
from match.overrides import OVERRIDE_WRITEBACK_MAP, OverrideMerger
from match.recordfactory import MatchRecordFactory


class ApplyOrchestrator:
    """校验 → 幂等删旧 → 批量建匹配记录 → 统计重算回填 → 回读 → 用户清单。"""

    def __init__(self, config_path: str, decisions_path: str, out_dir: str,
                 digest_path: Optional[str] = None, dry_run: bool = False,
                 batch_id: Optional[str] = None, create_chunk: int = 100,
                 job_parser: Any = None, verify_fn: Any = None):
        self.config_path = config_path
        self.decisions_path = decisions_path
        self.out_dir = out_dir
        self.digest_path = digest_path
        self.dry_run = dry_run
        self.batch_id = batch_id
        self.create_chunk = create_chunk
        self.ctx = DecisionContextBuilder(job_parser)
        self.merger = OverrideMerger()
        self.factory = MatchRecordFactory()
        self.report_builder = ApplyReportBuilder()
        self._verify = verify_fn
        self.gw: Optional[MatchTableGateway] = None

    # ------------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        self.t0 = time.time()
        self.outdir = Path(self.out_dir).expanduser()
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.report: Dict[str, Any] = {
            "ok": False, "dry_run": bool(self.dry_run), "generated_at": _now(),
            "elapsed_ms": 0, "dws_calls": 0, "retry_count": 0,
            "decisions_path": os.path.abspath(str(Path(self.decisions_path).expanduser())),
            "digest_path": None, "config_path": os.path.abspath(str(Path(self.config_path).expanduser())),
            "rows": [], "match_rows": [], "summary": {}, "warnings": [],
            "verify": None, "job_stats": {}, "errors": [],
        }
        # ⚠️ 让 warnings 与 report["warnings"] 是**同一个 list 对象**：
        #    本流程有多条提前 return 的失败路径，逐个赋值容易漏（第一版就漏了，
        #    结果 apply_report.json 里 warnings 恒为空，违反契约 D6「失败可见」）。
        self.warnings: List[str] = self.report["warnings"]

        for stage in (self._load_inputs, self._verify_decisions, self._merge_context,
                      self._fetch_table_facts, self._delete_stale, self._create_records,
                      self._refresh_job_stats, self._readback):
            if not stage():
                return self.report
        return self._assemble_report()

    # --------------------------------------------------------------- stages
    def _load_inputs(self) -> bool:
        """1. 读 decisions / digest（含同目录自动猜与 config→AITable）。"""
        report, warnings = self.report, self.warnings
        decisions, derr = load_json(Path(self.decisions_path).expanduser(), "decisions")
        if derr and derr["code"] != "markdown_fence_stripped":
            report["errors"].append(derr["detail"])
            self._finish(1)
            return False
        if derr:
            warnings.append(derr["detail"])
        self.decisions = decisions
        digest = None
        if self.digest_path:
            digest, gerr = load_json(Path(self.digest_path).expanduser(), "digest")
            if gerr and gerr["code"] != "markdown_fence_stripped":
                report["errors"].append(gerr["detail"])
                self._finish(1)
                return False
            if gerr:
                warnings.append(gerr["detail"])
            report["digest_path"] = os.path.abspath(str(Path(self.digest_path).expanduser()))
        else:
            guess = Path(os.path.abspath(str(Path(self.decisions_path).expanduser()))).parent / "digest.json"
            if guess.exists():
                digest, _ = load_json(guess, "digest")
                report["digest_path"] = os.path.abspath(str(guess))
                warnings.append("没传 --digest，自动用了同目录的 %s" % guess)
        self.digest = digest

        self.table = None
        if not self.dry_run:
            try:
                self.table = AITable(self.config_path)
            except Exception as exc:                          # config 坏 → 直接失败，别猜
                report["errors"].append("打不开 config.json：%s" % exc)
                self._finish(1)
                return False
        if self.table is not None:
            self.gw = MatchTableGateway(self.table)
        return True

    def _verify_decisions(self) -> bool:
        """2. 先校验（不通过就不写库，契约 D6）。"""
        report, warnings = self.report, self.warnings
        check_coverage = True
        if self.digest is None and self.table is not None:
            # 降级路径：从表里现查岗位拼一个最小 digest（集合/算术/结构照查，覆盖率查不了）
            self.digest = self.ctx.synth_digest_from_table(self.table, self.decisions, warnings)
            check_coverage = False
            warnings.append("没有 --digest：岗位分母改从表里现查，**覆盖率无法校验**"
                            "（强烈建议传 --digest，否则漏判的组合发现不了）")
        if self.digest is None:
            # dry-run 且没 digest：连岗位分母都没有，只能做结构校验
            warnings.append("没有 digest（dry-run 不查表）：无法做覆盖率与分母校验，"
                            "只查 JSON 结构/evidence/推荐状态取值")
            vres = self._verify({"candidates": [], "jobs": []}, self.decisions, check_coverage=False)
            keep = ("bad_json", "decisions_not_object", "decisions_empty", "passed_not_list",
                    "rejected_not_list", "entry_not_object", "missing_field", "job_keys_not_list",
                    "evidence_too_long", "evidence_empty", "recommend_invalid_value",
                    "gate_detail_inconsistent", "gate_detail_not_object")
            vres["errors"] = [e for e in vres["errors"] if e["code"] in keep]
            vres["ok"] = not vres["errors"]
        else:
            vres = self._verify(self.digest, self.decisions, check_coverage=check_coverage)
        self.vres = vres
        report["verify"] = {k: vres.get(k) for k in ("ok", "counts", "coverage", "summary",
                                                     "errors", "warnings")}
        for w in vres.get("warnings") or []:
            warnings.append("verify[%s] %s: %s" % (w["code"], w["key"], w["detail"]))
        if not vres.get("ok"):
            report["errors"] = ["verify 不通过（%d 个问题），按契约 D6 **不写库**" % len(vres["errors"])]
            report["errors"] += ["%s | %s | %s" % (e["code"], e["key"], e["detail"])
                                 for e in vres["errors"]]
            report["verify_problems"] = vres["errors"]
            self._finish(1)
            return False
        return True

    def _merge_context(self) -> bool:
        """索引构建 + candidate_overrides 合入/写回（override 明细进报告）。"""
        report, warnings = self.report, self.warnings
        self.job_index = self.ctx.build_job_index(self.table, self.digest, warnings)
        self.cand_index = self.ctx.build_candidate_index(self.table, self.digest,
                                                         self.decisions, warnings)
        self.n_ovr = self.merger.apply_overrides(self.cand_index, self.decisions, warnings)
        if self.n_ovr:
            warnings.append("已合入 %d 处 candidate_overrides（组织/分类/期望地点/工作年限复核/"
                            "技能补充/证书补充）" % self.n_ovr)
        # override 明细进报告（含 org_reason，dry-run 也记录 → 契约 v3 §9#1 的凭证）
        cand_index = self.cand_index
        report["overrides"] = [
            {"candidate_key": ck,
             "name": as_text(cand_index[ck].get("name")) or None,
             "record_id": cand_index[ck].get("record_id"),
             "changed": dict(cand_index[ck].get("_override_changes") or {}),
             "org_reason": cand_index[ck].get("org_override_reason")}
            for ck in sorted(cand_index)
            if cand_index[ck].get("_override_changes") or cand_index[ck].get("org_override_reason")]
        if self.n_ovr and self.table is not None:
            # D13/D14：修正必须持久化到简历库（表是唯一事实源）；失败只进 warnings（D6 可见）
            report["overrides_writeback"] = self.gw.writeback_overrides(cand_index, warnings)
        elif self.n_ovr and self.dry_run:
            report["overrides_writeback"] = {"skipped": "dry-run 不写库；实跑时会把上述修正写回简历库"}

        self.audit = {(a["candidate_key"], a["job_key"]): a
                      for a in (self.vres.get("passed_audit") or [])}
        self.passed = [p for p in (self.decisions.get("passed") or []) if isinstance(p, dict)]
        self.rejected = [r for r in (self.decisions.get("rejected") or []) if isinstance(r, dict)]
        return True

    def _fetch_table_facts(self) -> bool:
        """3. 表内事实：沟通状态（已入职铁律）+ 删旧名单。"""
        report, warnings = self.report, self.warnings
        cand_index = self.cand_index
        self.table_cells: Dict[str, Dict[str, Any]] = {}
        self.onboarded: List[str] = []
        if self.table is not None:
            raw = self.gw.fetch_comm_status(cand_index, warnings)
            for ck, c in cand_index.items():
                rid = c.get("record_id")
                cells = raw.get(str(rid)) if rid else None
                if cells is None:
                    cells = raw.get("name:%s" % as_text(c.get("name")).strip())
                if cells:
                    self.table_cells[ck] = cells
                    c.setdefault("name", as_text(cells.get("name")) or c.get("name"))
            # override 修正值优先于刚读回的表值：写回简历库与本次查询之间可能有传播延迟
            # （契约 §10 R2），读到的可能是写回前的旧值 → 以 agent 修正值为准。
            for ck, c in cand_index.items():
                ch = c.get("_override_changes") or {}
                if ch and ck in self.table_cells:
                    tc = self.table_cells[ck]
                    for src, dst in OVERRIDE_WRITEBACK_MAP:
                        if src not in ch:
                            continue
                        tc[dst] = (join_list(ch[src], "、")[:500] if dst == "certificates"
                                   else as_list(ch[src]) if dst == "skills" else ch[src])
            for ck, c in cand_index.items():
                cs = as_text((self.table_cells.get(ck) or {}).get("comm_status")).strip() \
                    or as_text(c.get("comm_status")).strip()
                if cs == COMM_STATUS_ONBOARDED:
                    self.onboarded.append(ck)
            if self.onboarded:
                warnings.append("老插件铁律：%d 个候选人「沟通状态=已入职」→ 删记录、不打分、不推荐：%s"
                                % (len(self.onboarded),
                                   ",".join(as_text(cand_index[k].get("name")) for k in self.onboarded)))

        names = sorted({as_text(self.factory.cells_for(cand_index[k],
                                                       self.table_cells.get(k)).get("name")).strip()
                        for k in cand_index} - {""})
        # build_match_input 已把「沟通状态=已入职」的人从 digest 剔除（他们不会出现在 decisions 里），
        # 但老插件铁律要求他们的「系统匹配」记录**照样要删** → 从 digest.meta.excluded_onboarded 补进来
        excluded_onboarded_names: List[str] = []
        if isinstance(self.digest, dict) and isinstance(self.digest.get("meta"), dict):
            for o in self.digest["meta"].get("excluded_onboarded") or []:
                nm = as_text((o or {}).get("name")).strip()
                if nm and nm not in names:
                    excluded_onboarded_names.append(nm)
        if excluded_onboarded_names:
            warnings.append("老插件铁律：另需清理 %d 个已入职候选人的历史「系统匹配」记录（他们已被 "
                            "build_match_input 排除在本批判定之外）：%s"
                            % (len(excluded_onboarded_names), ",".join(excluded_onboarded_names)))
        self.names = names
        self.excluded_onboarded_names = excluded_onboarded_names
        self.names_for_delete = sorted(set(names) | set(excluded_onboarded_names))
        if self.table is not None and not names and self.ctx.rows_needed(self.decisions):
            # 没姓名就没法定位本批候选人做「删旧建新」→ 宁可不写也不写出重复记录（D6 失败可见）
            report["errors"].append(
                "无法定位本批候选人（decisions 里没有姓名，且没传 --digest 拿不到候选人档案）→ "
                "幂等「删旧建新」无法保证，拒绝写库。请加 --digest <digest.json> 重跑")
            self._finish(1)
            return False
        return True

    def _delete_stale(self) -> bool:
        """4. 幂等：批量查旧「系统匹配」记录 → 一次批量删。"""
        warnings = self.warnings
        self.stale: List[Dict[str, Any]] = []
        self.del_res: Dict[str, Any] = {"deleted": 0, "failed": [], "dws_calls": 0}
        if self.table is not None:
            self.stale = self.gw.find_stale_match_records(self.names_for_delete, warnings)
            stale_ids = [s.get("record_id") for s in self.stale if s.get("record_id")]
            if stale_ids:
                self.del_res = self.gw.delete_records(stale_ids)
                if self.del_res.get("failed"):
                    for f in self.del_res["failed"]:
                        warnings.append("删旧记录失败：%s" % json.dumps(f, ensure_ascii=False)[:200])
            else:
                warnings.append("本批候选人在 match 表没有旧的「系统匹配」记录（首次匹配或已清理干净）")
        return True

    def _create_records(self) -> bool:
        """5. 批量建匹配记录（只建 passed 且候选人未入职的）+ 受影响岗位集合。"""
        report, warnings = self.report, self.warnings
        cand_index, job_index = self.cand_index, self.job_index
        self.rows_to_create: List[Dict[str, Any]] = []
        self.row_meta: List[Dict[str, Any]] = []
        self.affected_job_ids: List[str] = []
        for s in self.stale:
            jid = as_text((s.get("cells") or {}).get("job_id")).strip()
            if jid and jid not in self.affected_job_ids:
                self.affected_job_ids.append(jid)

        self.skipped_onboarded = 0
        self.skipped_invalid: List[Dict[str, Any]] = []
        for p in self.passed:
            ck = str(p.get("candidate_key"))
            jk = str(p.get("job_key"))
            a = self.audit.get((ck, jk))
            if not a or not a.get("valid"):
                # D16：命中项越界（编造）→ 该条无效，不建记录；D6：必须可见，进 warnings 与清单
                self.skipped_invalid.append({
                    "candidate_key": ck, "job_key": jk,
                    "candidate_name": (cand_index.get(ck) or {}).get("name"),
                    "job_name": ((resolve_job(job_index, p, jk) or {}).get("job_name")),
                    "reason": (a or {}).get("invalid_reason") or "verify 未给出该条的算术复核结果",
                    "fabricated_hits": (a or {}).get("fabricated_hits")})
                continue
            if ck in self.onboarded:
                self.skipped_onboarded += 1
                continue
            job = resolve_job(job_index, p, jk)
            cand = cand_index.get(ck, {"key": ck})
            if not job:
                warnings.append("passed %s×%s：在岗位索引里找不到岗位，未建记录" % (ck, jk))
                continue
            tc = self.table_cells.get(ck) or {}
            merged = self.factory.cells_for(cand, tc)
            rc = a["recomputed"]
            jid = as_text(job.get("job_id")).strip() or None
            rec = self.factory.make_record(p, merged, job, rc, jid)
            self.rows_to_create.append(rec)
            self.row_meta.append({"candidate_key": ck, "job_key": jk, "candidate_name": merged["name"],
                                  "job_name": rec.get("job_name"), "job_id": jid,
                                  "department": as_text(job.get("department")) or None,
                                  "record": rec, "recomputed": rc,
                                  "model_scores": a.get("model") or {},
                                  "mismatch": a.get("mismatch") or [],
                                  "evidence": rec.get("evidence")})
            if jid and jid not in self.affected_job_ids:
                self.affected_job_ids.append(jid)

        # 缺陷3 修复（W-F S9 实测，2026-09-17）：**受本批影响的岗位**不止「建了记录/删了旧记录」
        # 的岗位，还包括「本批对它做过判定但一个达标者都没有」的零匹配岗位。这类岗位此前
        # 不进 affected_job_ids → 四个统计字段（候选人总数/推荐数/待定数/不推荐数）留空，
        # recruit-dashboard 的「待补/标红」逻辑分不清"还没算"和"算出来是 0"。
        # 现在把 decisions 里 passed/rejected 引用到的岗位全部算作受影响：
        # 即使重算结果四个数字都是 0，也**显式写入 0**（recompute_job_stats 对查不到
        # 匹配记录的岗位返回全 0，stat_updates 会原样回填并回读校验）。
        # 边界：只动 decisions 引用到的岗位（= 与本批判定相关），digest 里没有被本批
        # 判定触及的岗位（如其它组织的岗位）不写，避免无谓写调用与权限风险。
        for p in self.passed:
            job_r = resolve_job(job_index, p, str(p.get("job_key")))
            jid_r = as_text((job_r or {}).get("job_id")).strip()
            if jid_r and jid_r not in self.affected_job_ids:
                self.affected_job_ids.append(jid_r)
        for r in self.rejected:
            for jk in (r.get("job_keys") or []):
                job_r = resolve_job(job_index, r, str(jk))
                jid_r = as_text((job_r or {}).get("job_id")).strip()
                if jid_r and jid_r not in self.affected_job_ids:
                    self.affected_job_ids.append(jid_r)

        self.create_res: Dict[str, Any] = {"created": 0, "failed": [], "record_ids": [],
                                           "dws_calls": 0, "elapsed_ms": 0, "submitted": 0,
                                           "isolate_extra_calls": 0}
        if self.table is not None and self.rows_to_create:
            report["retry_count"] += self.gw.create_records(
                self.create_res, self.rows_to_create, self.create_chunk, warnings)
        return True

    def _refresh_job_stats(self) -> bool:
        """6. D15：岗位统计脚本重算 + 一次 batch_update 回填。"""
        warnings = self.warnings
        self.stats: Dict[str, Dict[str, int]] = {}
        self.stat_update_res: Dict[str, Any] = {"updated": 0, "failed": [], "dws_calls": 0}
        self.stat_updates: List[Dict[str, Any]] = []
        self.job_records: Dict[str, str] = {}
        for j in self.job_index.values():
            jid = as_text(j.get("job_id")).strip()
            if jid and j.get("record_id"):
                self.job_records[jid] = j["record_id"]
        stat_job_ids = [j for j in self.affected_job_ids if j in self.job_records]
        missing_rec = [j for j in self.affected_job_ids if j not in self.job_records]
        if missing_rec:
            warnings.append("有 %d 个受影响岗位拿不到 record_id（%s）→ 无法回填统计，"
                            "请传 --digest 或检查 job 表 岗位ID"
                            % (len(missing_rec), ",".join(missing_rec[:5])))
        if self.table is not None and stat_job_ids:
            self.stats = self.gw.recompute_job_stats(stat_job_ids, warnings)
            self.stat_updates = [{"record_id": self.job_records[jid],
                                  "cells": {"stat_total": s["total"], "stat_recommend": s["recommend"],
                                            "stat_pending": s["pending"], "stat_reject": s["reject"]}}
                                 for jid, s in sorted(self.stats.items())]
            if self.stat_updates:
                self.stat_update_res = self.gw.update_job_stats(self.stat_updates)
                if self.stat_update_res.get("failed"):
                    for f in self.stat_update_res["failed"]:
                        warnings.append("回填岗位统计失败：%s" % json.dumps(f, ensure_ascii=False)[:200])
        return True

    def _readback(self) -> bool:
        """7. 写后必回读（D6）。"""
        warnings = self.warnings
        self.rb_match: Dict[str, Any] = {}
        self.rb_job: Dict[str, Any] = {}
        if self.table is not None and self.create_res.get("record_ids"):
            self.rb_match = self.gw.readback_match(self.create_res["record_ids"],
                                                   self.row_meta, warnings)
        if self.table is not None and self.stat_update_res.get("record_ids"):
            self.rb_job = self.gw.readback_job(self.stat_update_res, self.stat_updates,
                                               warnings)
        return True

    def _assemble_report(self) -> Dict[str, Any]:
        """8. 用户可读清单 + summary/job_stats/readback 组装 + ok 判定 + 落盘。"""
        report, warnings = self.report, self.warnings
        report.update(self.report_builder.build_rows(
            self.cand_index, self.table_cells, self.row_meta, self.rejected, self.job_index,
            self.onboarded, self.create_res, self.skipped_onboarded, self.skipped_invalid))
        combos = (self.vres.get("counts") or {}).get("expected_pairs") or 0
        # 老插件按「每人每岗一个判定回合」的下界估；新插件 = 1 判定回合 + 2 脚本回合
        report["turns_saved_estimate"] = max(0, combos - 3)
        create_res, rb_match, rb_job = self.create_res, self.rb_match, self.rb_job
        report["summary"].update({
            "batch_id": self.batch_id or (self.digest or {}).get("batch_id") or self.decisions.get("batch_id"),
            "stale_deleted": self.del_res.get("deleted", 0),
            "created": create_res.get("created", 0),
            "create_failed": len(create_res.get("failed") or []),
            "skipped_onboarded": self.skipped_onboarded,
            "skipped_invalid_hits": len(self.skipped_invalid),
            "onboarded_history_cleaned": len(self.excluded_onboarded_names),
            "passed_entries": len(self.passed),
            "rejected_entries": len(self.rejected),
            "combo_count": combos,
            "jobs_stat_refreshed": len(self.stats),
            "overrides_applied": self.n_ovr,
            "readback_match_ok": (rb_match.get("ok") if rb_match else None),
            "readback_job_ok": (rb_job.get("ok") if rb_job else None),
            "turns_saved_estimate": report["turns_saved_estimate"],
            "turns_saved_basis": "老插件下界 = 组合数 %d 个判定回合；新插件 = 3 个回合"
                                 "（build / 批量判定 / apply）" % combos,
        })
        report["job_stats"] = {jid: dict(s, job_record_id=self.job_records.get(jid),
                                         record_id=self.job_records.get(jid))
                               for jid, s in sorted(self.stats.items())}
        report["skipped_invalid"] = self.skipped_invalid
        for si in self.skipped_invalid:
            warnings.append("未建记录（D16 命中项越界）：%s × %s ｜%s ｜越界项=%s"
                            % (si.get("candidate_name"), si.get("job_name"), si.get("reason"),
                               json.dumps(si.get("fabricated_hits"), ensure_ascii=False)[:200]))
        report["dws_calls"] = self.table.dws_calls if self.table is not None else 0
        report["dws_stats"] = self.table.stats() if self.table is not None else {}
        report["readback"] = {
            "match": {k: rb_match.get(k) for k in ("ok", "requested", "found", "settle_polls",
                                                   "dws_calls", "elapsed_ms")} if rb_match else None,
            "job": {k: rb_job.get(k) for k in ("ok", "requested", "found", "settle_polls",
                                               "dws_calls", "elapsed_ms")} if rb_job else None,
        }
        report["delete"] = {k: self.del_res.get(k) for k in ("deleted", "dws_calls", "elapsed_ms")}
        report["create"] = {k: create_res.get(k) for k in ("created", "submitted", "dws_calls",
                                                           "elapsed_ms", "isolate_extra_calls")}
        report["stat_update"] = {k: self.stat_update_res.get(k) for k in ("updated", "submitted",
                                                                          "dws_calls", "elapsed_ms")}
        ok = (not create_res.get("failed")) and not report["errors"]
        if self.table is not None and create_res.get("record_ids") and rb_match and not rb_match.get("ok"):
            ok = False
            report["errors"].append("回读校验未通过（见 warnings），请重跑本步复核")
        report["ok"] = bool(ok) or bool(self.dry_run and not report["errors"])
        return self._finish(0 if report["ok"] else 1)

    # ---------------------------------------------------------------- finish
    def _finish(self, exit_code: int) -> Dict[str, Any]:
        report = self.report
        report["elapsed_ms"] = int((time.time() - self.t0) * 1000)
        report["warnings"] = report.get("warnings") or []
        report["generated_at"] = _now()
        report.setdefault("summary", {})
        report["exit_code"] = exit_code
        report["python"] = "%d.%d.%d" % sys.version_info[:3]
        path = self.outdir / "apply_report.json"
        tmp = self.outdir / ("apply_report.json.tmp-%d" % int(time.time() * 1000))
        dump_json_doc(report, tmp)
        tmp.replace(path)                                 # 原子落盘（D7：产物必须存在且完整）
        # abspath 而非 resolve()：macOS 上 /tmp 是 /private/tmp 的符号链接，
        # resolve() 会让 ARTIFACT 行跟用户传进来的 --out-dir 长得不一样。
        report["_report_path"] = os.path.abspath(str(path))
        return report
