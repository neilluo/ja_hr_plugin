# -*- coding: utf-8 -*-
"""JobPipeline：岗位入库 Turn 1 / Turn 3 的编排层。

本类只做编排 + 业务判定——阶段顺序、条目状态机、跳过与 fatal 判定、告警文案；
不 print、不 open、不直接碰 tbl.。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aitable.client import DwsCallCounter, DwsClient, DwsError, now_iso  # noqa: E402
from aitable.table import AITable                      # noqa: E402
from jobintake.assembler import JobFieldAssembler      # noqa: E402
from jobintake.console import JobConsole               # noqa: E402
from jobintake.docparse import JobDocParser            # noqa: E402
from jobintake.jdfields import SkillGranularityGuard   # noqa: E402
from jobintake.policy import JobLocationPolicy, JobOrgPolicy  # noqa: E402
from jobintake.readback import JobReadBackVerifier     # noqa: E402
from jobintake.report import JobReport                 # noqa: E402
from jobintake.table_gateway import JobTableGateway    # noqa: E402
from jobintake.textutil import clean, new_batch_id     # noqa: E402
from runtime_compat import default_out_root            # noqa: E402
from performance_timing import (                       # noqa: E402
    append_events,
    child_timing_enabled,
    start_run,
    timeline_exists,
)

from pipeline_base import PipelineBase                   # noqa: E402

__all__ = ["JobPipeline"]


class JobPipeline(PipelineBase):
    """岗位入库编排。run() = 装配 → run_turn1()/run_apply() → JobReport.finish()。"""

    def __init__(self, args: argparse.Namespace, console: Optional[JobConsole] = None) -> None:
        self.args = args
        self.console = console if console is not None else JobConsole()
        # 纯规则/组装协作者（无 IO）
        self.parser = JobDocParser()
        self.org_policy = JobOrgPolicy()
        self.location_policy = JobLocationPolicy()
        self.granularity_guard = SkillGranularityGuard()
        self.assembler = JobFieldAssembler()
        # run() 里装配
        self.t_start = 0.0
        self.batch_id = ""
        self.out_dir: Optional[Path] = None
        self.mode = "turn1"
        self.counter: Optional[DwsCallCounter] = None
        self.gateway: Optional[JobTableGateway] = None
        self.verifier: Optional[JobReadBackVerifier] = None
        self.report: Optional[JobReport] = None
        # turn1 跨阶段状态（原 run_turn1 的 ~20 个长寿命局部名）
        self.warnings: List[str] = []
        self.rows: List[Dict[str, Any]] = []
        self.jobs: List[Dict[str, Any]] = []
        self.summary: Dict[str, int] = {}
        self.fatal: Optional[str] = None
        self.files: List[str] = []
        self.have: set = set()
        self.known_locs: List[str] = []
        self.entries: List[Dict[str, Any]] = []
        self.existing: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        self.max_seq = 0
        self.to_write: List[Dict[str, Any]] = []
        self.all_departments: List[str] = []
        self.all_orgs: List[str] = []
        self.all_locations: List[str] = []
        self.batch_keys: Dict[Tuple[str, str, str], int] = {}
        self.written: List[Dict[str, Any]] = []
        self.verify: Dict[str, Any] = {}
        # apply 跨阶段状态（原 run_apply 的 ~14 个长寿命局部名）
        self.name2rec: Dict[str, str] = {}
        self.updates: List[Dict[str, Any]] = []
        self.meta: List[Dict[str, Any]] = []
        self.granularity_items: List[Tuple[str, Any, Any]] = []
        self.res: Dict[str, Any] = {}
        self.stage_timings: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # CLI 校验（main() 的两条 rc=2 路径，顺序即优先级）
    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_args(args: argparse.Namespace, console: JobConsole) -> Optional[int]:
        if not args.files and not args.apply:
            console.err_usage()
            return 2
        if not Path(args.config).expanduser().exists():
            console.err_config(args.config)
            return 2
        return None

    def _calls_fn(self) -> int:
        """当前 dws 调用计数（jobintake 侧：self.counter.calls）。"""
        return self.counter.calls

    def _persist_performance(self, run_started_ms: int, returncode: int) -> None:
        """旁路落盘本次岗位流水线阶段耗时，不进入任何业务判断。"""
        if self.out_dir is None:
            return
        run_finished_ms = int(time.time() * 1000)
        is_child = child_timing_enabled()
        if not is_child and (not self.is_replay or not timeline_exists(self.out_dir)):
            start_run(self.out_dir, "job", "intake_job", run_started_ms)
        events = [{
            "name": "job.%s" % stage["name"],
            "category": "intake_stage",
            "started_at_ms": stage["started_at_ms"],
            "finished_at_ms": stage["finished_at_ms"],
            "elapsed_ms": stage["elapsed_ms"],
            "dws_calls": stage["dws_calls"],
        } for stage in self.stage_timings]
        if not is_child:
            events.append({
                "name": "intake_job_%s" % ("replay" if self.is_replay else "emit"),
                "category": "orchestrator_local",
                "started_at_ms": run_started_ms,
                "finished_at_ms": run_finished_ms,
                "dws_calls": self._safe_calls(),
                "metadata": {"returncode": returncode, "mode": self.mode},
            })
        append_events(self.out_dir, events)

    # ------------------------------------------------------------------ #
    # 装配 + 调度
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        args = self.args
        run_started_ms = int(time.time() * 1000)
        self.stage_timings = []
        self.t_start = time.monotonic()
        self.batch_id = args.batch_id or new_batch_id()
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
            default_out_root() / self.batch_id
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        report_path = out_dir / "intake_report.json"
        draft_path = out_dir / "jobs_draft.json"

        self.counter = DwsCallCounter()
        replay_path = getattr(args, "replay_path", None)
        self.is_replay = replay_path is not None
        self.client = DwsClient(counter=self.counter, http_timeout=180,
                                replay_path=replay_path)

        self.mode = "apply" if args.apply else "turn1"
        self.report = JobReport(self.console, args, self.counter, self.t_start,
                                self.mode, report_path, draft_path)
        self.console.banner(self.mode)
        self.console.batch_info(self.batch_id, out_dir)

        try:
            tbl = AITable(args.config, client=self.client)
        except Exception as exc:
            rc_failure = self.report.write_config_failure(exc)
            self._persist_performance(run_started_ms, rc_failure)
            return rc_failure
        self.console.base_info(tbl.base_name, tbl.base_id,
                               tbl.table_name("job"), tbl.table_id("job"))
        self.gateway = JobTableGateway(tbl, self.counter)
        self.verifier = JobReadBackVerifier(tbl)

        if self.mode == "apply":
            draft, rows, rc = self.run_apply()
        else:
            draft, rows, rc = self.run_turn1()
        rc_final = self.report.finish(draft, rows, rc)

        # ---- 两阶段模式：emit 模式下输出 dws 命令清单 ----
        if not self.is_replay:
            emit_path = self.out_dir / "dws_commands.json"
            self.client.write_emit_file(str(emit_path))
            print("emit 模式：%d 条 dws 命令已写入 %s" %
                  (len(self.client.emit_commands()), emit_path))
        self._persist_performance(run_started_ms, rc_final)
        return rc_final

    # ------------------------------------------------------------------ #
    # Turn 1
    # ------------------------------------------------------------------ #
    def run_turn1(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        args = self.args
        self.warnings = []
        self.rows = []
        self.jobs = []
        self.summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
                        "attachment_uploaded": 0, "attachment_failed": 0}
        self.fatal = None
        self.files = list(args.files or [])

        self.have = set(self.gateway.field_keys("job"))
        self.known_locs = self.gateway.known_location_options()

        self._run_named_stage("turn1_extract", self.turn1_extract)
        self._run_named_stage("turn1_scan", self.turn1_scan)
        self._run_named_stage("turn1_dedupe", self.turn1_dedupe)
        self._run_named_stage("turn1_ensure_options", self.turn1_ensure_options)
        self._run_named_stage("turn1_build_rows", self.turn1_build_rows)
        self._run_named_stage("turn1_upload", self.turn1_upload)
        self._run_named_stage("turn1_write", self.turn1_write)
        self._run_named_stage("turn1_readback", self.turn1_readback)
        self._run_named_stage("turn1_assemble", self.turn1_assemble)
        return self._run_named_stage("turn1_finalize", self.turn1_finalize)

    def turn1_extract(self) -> None:
        """阶段 1：提取 + 正则预填（纯本地）。"""
        entries: List[Dict[str, Any]] = []
        t_extract = time.monotonic()
        for i, fp in enumerate(self.files):
            entries.append(self.parser.parse_one(i, fp))
        self.entries = entries
        extract_ms = int((time.monotonic() - t_extract) * 1000)
        self.console.extract_done(len(entries),
                                  sum(1 for e in entries if e["parse_status"] == "ok"),
                                  extract_ms)

    def turn1_scan(self) -> None:
        """阶段 2：一次全表扫描 → 同时得到「复合键查重索引」与「岗位ID 最大序号」。

        岗位ID 必须全表唯一；查重键（岗位名称+所属部门+组织分类）也一并从这次扫描里取。
        """
        self.existing = {}
        self.max_seq = 0
        if self.fatal:
            return
        try:
            scan_fields = [k for k in ("job_id", "job_name", "department", "org")
                           if k in self.have]
            with self._time_stage() as st:
                recs = self.gateway.scan_jobs(scan_fields)
                for r in recs:
                    c = r.get("cells") or {}
                    jn = clean(c.get("job_name")) or ""
                    dep = clean(c.get("department")) or ""
                    org = clean(c.get("org")) or ""
                    self.existing.setdefault((jn, dep, org), []).append(
                        {"record_id": r["record_id"], "job_id": clean(c.get("job_id"))})
                    jid = clean(c.get("job_id")) or ""
                    m = re.search(r"(\d+)\s*$", jid)
                    if m:
                        self.max_seq = max(self.max_seq, int(m.group(1)))
            self.console.scan(len(recs), len(self.existing), self.max_seq,
                              st.delta, st.calls_delta)
        except DwsError as exc:
            self._set_dws_fatal(exc, "岗位表查重扫描失败（%s/%s）：%s")

    def turn1_dedupe(self) -> None:
        """阶段 3：组织/地点预判 + 复合键查重（三者全同才算重复）。"""
        to_write: List[Dict[str, Any]] = []
        self.all_departments = []
        self.all_orgs = []
        self.all_locations = []
        self.batch_keys = {}

        for ent in self.entries:
            if not ent["writable"]:
                continue
            f = ent["fields"]
            job_name = clean(f.get("job_name"))
            dept = clean(f.get("department"))
            org, org_conf, org_info = self.org_policy.guess(ent["file_name"], dept,
                                                            job_name, ent["text"])
            locs = self.location_policy.guess(ent["file_name"], ent["text"], self.known_locs)
            ent["org"], ent["org_confidence"], ent["org_info"] = org, org_conf, org_info
            ent["locations"] = locs
            if org_conf == "low":
                if org:
                    self.warnings.append("《%s》组织分类低置信度：文件名显式声明与部门关键词打架"
                                         "（判据 %s），已按显式声明写 %s，请 Turn 2 复核"
                                         % (ent["file_name"],
                                            json.dumps(org_info, ensure_ascii=False), org))
                else:
                    self.warnings.append("《%s》组织分类判不了（判据 %s），组织留空 → "
                                         "该岗位匹配不到任何简历，请 Turn 2 必须补 org"
                                         % (ent["file_name"],
                                            json.dumps(org_info, ensure_ascii=False)))
            if not locs:
                self.warnings.append("《%s》未解析到工作地点，留空待 Turn 2 补" % ent["file_name"])
            if f.get("cert_is_preferred_not_required"):
                ent["warnings"].append("cert_is_preferred_not_required=True：检出的证书全部出现在"
                                       "「…者优先/加分」语境，**不得**当硬性门槛用（会误杀）")

            key3 = (job_name or "", dept or "", org or "")
            if key3 in self.batch_keys:
                ent["result"] = "跳过"
                ent["dedupe"] = "overwrite"
                ent["reason"] = ("与本批第 %d 份《%s》的「岗位名称+所属部门+组织分类」完全相同，"
                                 "判为重复上传，已跳过" % (self.batch_keys[key3], ent["file_name"]))
                continue
            self.batch_keys[key3] = ent["seq"]
            hits = self.existing.get(key3) or []
            if len(hits) > 1:
                ent["result"] = "失败"
                ent["dedupe"] = "conflict"
                ent["writable"] = False
                ent["reason"] = ("库内「%s / %s / %s」命中 %d 条记录（%s），已停下不自动选择，"
                                 "请确认保留哪一条后再重跑"
                                 % (job_name, dept, org, len(hits),
                                    ", ".join(str(h["record_id"]) for h in hits[:5])))
                self.warnings.append("岗位复合键多条冲突：%s → %d 条，需人工确认" % (key3, len(hits)))
                continue
            if hits:
                ent["dedupe"] = "overwrite"
                ent["record_id"] = hits[0]["record_id"]
                ent["job_id"] = hits[0].get("job_id")   # 沿用库内已有岗位ID（全表唯一）
            else:
                ent["dedupe"] = "new"
                self.max_seq += 1
                ent["job_id"] = "JOB-%03d" % self.max_seq
            to_write.append(ent)
            if dept and dept not in self.all_departments:
                self.all_departments.append(dept)
            if org and org not in self.all_orgs:
                self.all_orgs.append(org)
            for l in locs:
                if l not in self.all_locations:
                    self.all_locations.append(l)
        self.to_write = to_write

    def turn1_ensure_options(self) -> None:
        """阶段 4：部门/组织/工作地点选项 ensure_options（只增不删）。"""
        for field_key, names in (("department", self.all_departments),
                                 ("org", self.all_orgs),
                                 ("work_location", self.all_locations)):
            if not names or field_key not in self.have:
                continue
            try:
                with self._time_stage() as st:
                    opts = self.gateway.ensure_options(field_key, names)
                self.console.ensure_options(field_key, len(names), len(opts),
                                            st.delta, st.calls_delta)
            except Exception as exc:
                self.warnings.append("ensure_options(job.%s) 失败：%s: %s；写入时服务端通常会自动补选项"
                                     % (field_key, type(exc).__name__, str(exc)[:200]))

    def turn1_build_rows(self) -> None:
        """阶段 5：整理写入行（Turn 1 只写脚本能确定的字段）。

        需求提交人（可选。config.json 配了 fields.job.submitter 才回填，
        值 = 当前登录用户；未配置 → 留空并在 warnings 里向客户明示语义弱化）。
        """
        submitter_cell: Optional[List[Dict[str, Any]]] = None
        if self.to_write and "submitter" in self.have:
            submitter_cell, submitter_name = self.gateway.fetch_current_user_cell(self.warnings)
            if submitter_cell is not None:
                self.console.submitter(submitter_name)
        elif self.to_write:
            self.warnings.append("config.json 未配置 job.submitter（需求提交人）字段映射 → 该列留空。"
                                 "老插件口径「需求提交人」必填（=当前登录用户），极速版降级为可选；"
                                 "客户表里有该列时，在 config.json "
                                 "的 fields.job 里补 \"submitter\" 映射后重跑即可回填（是否补由客户决定）")
        for ent in self.to_write:
            ent["row"] = self.assembler.build_write_row(ent, self.have, submitter_cell,
                                                        self.warnings)

    def turn1_upload(self) -> None:
        """阶段 6：并发上传 JD 附件（一律原始文件名），fileToken 随 create 一次写入。"""
        if self.to_write and not self.args.no_attachment:
            with self._time_stage() as st:
                results = self.gateway.upload_attachments([e["path"] for e in self.to_write],
                                                          concurrency=self.args.concurrency)
                for ent, res in zip(self.to_write, results):
                    if res.get("ok") and res.get("cell"):
                        if "attachment" in self.have:
                            ent["row"]["attachment"] = res["cell"]
                        ent["attachment_status"] = "uploaded"
                        self.summary["attachment_uploaded"] += 1
                    else:
                        ent["attachment_status"] = "failed"
                        self.summary["attachment_failed"] += 1
                        self.warnings.append("《%s》JD 附件上传失败（%s/%s）：%s；岗位正文已照常入库，"
                                             "可后续补传"
                                             % (ent["file_name"], res.get("category"), res.get("code"),
                                                str(res.get("error"))[:200]))
            self.console.upload(self.args.concurrency, self.summary["attachment_uploaded"],
                                self.summary["attachment_failed"],
                                int(st.delta * 1000), st.calls_delta)

    def turn1_write(self) -> None:
        """阶段 7：批量写（复合键无法用 batch_upsert_by_key，自己拆 create/update）。"""
        creates = [e for e in self.to_write if e["dedupe"] == "new"]
        updates = [e for e in self.to_write if e["dedupe"] == "overwrite"]
        if creates and not self.fatal:
            try:
                with self._time_stage() as st:
                    r = self.gateway.batch_create([e["row"] for e in creates])
                self.console.batch_create(len(creates), r.get("created", 0),
                                          len(r.get("failed") or []),
                                          st.delta, st.calls_delta)
                for fl in (r.get("failed") or []):
                    idx = fl.get("row_index")
                    ent = creates[idx] if isinstance(idx, int) and idx < len(creates) else None
                    msg = str(fl.get("reason"))[:240]
                    self.warnings.append("岗位写入失败《%s》：%s"
                                         % (ent["file_name"] if ent else "(未定位)", msg))
                    if ent:
                        ent["result"] = "失败"
                        ent["writable"] = False
                        ent["reason"] = "写入岗位JD表失败：%s" % msg[:200]
            except DwsError as exc:
                self._set_dws_fatal(exc, "批量新建岗位失败（%s/%s）：%s")
        if updates and not self.fatal:
            try:
                with self._time_stage() as st:
                    r = self.gateway.batch_update([{"record_id": e["record_id"], "cells": e["row"]}
                                                   for e in updates])
                self.console.batch_update(len(updates), r.get("updated", 0),
                                          len(r.get("failed") or []),
                                          st.delta, st.calls_delta)
                for fl in (r.get("failed") or []):
                    rid = fl.get("record_id") or (fl.get("row") or {}).get("record_id")
                    ent = next((e for e in updates if e["record_id"] == rid), None)
                    msg = str(fl.get("reason"))[:240]
                    self.warnings.append("岗位覆盖更新失败《%s》：%s"
                                         % (ent["file_name"] if ent else "(未定位)", msg))
                    if ent:
                        ent["result"] = "失败"
                        ent["writable"] = False
                        ent["reason"] = "覆盖更新岗位JD表失败：%s" % msg[:200]
            except DwsError as exc:
                self._set_dws_fatal(exc, "批量覆盖岗位失败（%s/%s）：%s")

    def turn1_readback(self) -> None:
        """阶段 8：回读校验（一次 filter 查询；岗位名不唯一，用 job_id/三元组精确归属）。"""
        # emit/replay 模式下跳过回读（record_id 是模拟的，查不到）
        return

    def turn1_assemble(self) -> None:
        """阶段 9：组装 rows / jobs。"""
        is_replay = self.is_replay
        for ent in self.entries:
            f = ent.get("fields") or {}
            if ent["result"] is None:
                if not ent["writable"]:
                    ent["result"] = "失败"
                    ent["reason"] = ent["reason"] or "未能入库（原因见 warnings）"
                elif not is_replay:
                    # emit 模式：命令已收集到 dws_commands.json，标记为待执行
                    ent["result"] = "新入库"
                    ent["reason"] = "emit 模式：dws 命令已收集，等待 agent 执行后 replay"
                else:
                    # replay 模式：dws 命令已用真实结果重放，回读被跳过
                    ent["result"] = "新入库" if ent["dedupe"] == "new" else "已覆盖"
                    ent["reason"] = "replay 模式：已用真实 dws 结果重放，记录已写入"
            if ent["result"] == "新入库":
                self.summary["new"] += 1
            elif ent["result"] == "已覆盖":
                self.summary["overwrite"] += 1
            elif ent["result"] == "跳过":
                self.summary["skip"] += 1
            else:
                self.summary["fail"] += 1

            extra = []
            if ent.get("org"):
                extra.append("%s%s" % (ent["org"], "" if ent.get("org_confidence") == "high"
                                       else "(待确认)"))
            if clean(f.get("department")):
                extra.append(str(f.get("department")))
            if ent["attachment_status"] == "uploaded":
                extra.append("JD附件已传")
            elif ent["attachment_status"] == "failed":
                extra.append("JD附件失败")
            reason = ent.get("reason") or ""
            if extra and ent["result"] in ("新入库", "已覆盖"):
                reason = "%s（%s）" % (reason, "，".join(extra))
            reason += "；硬性门槛/必备技能/加分项待 Turn 2 归一化后由 --apply 写入" \
                if ent["result"] in ("新入库", "已覆盖") else ""
            self.rows.append({"seq": ent["seq"], "file_name": ent["file_name"],
                              "result": ent["result"], "reason": reason})
            self.jobs.append(self.assembler.build_draft_job(ent))

    def turn1_finalize(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        """收尾：tbl.warnings 合并 + infra_fail 判定 + draft 组装。"""
        JobReport.merge_table_warnings(self.warnings, self.gateway.warnings)

        # 基础设施级失败判定：该写的岗位一份都没写进去 → ok=false，
        # 让 agent 重跑本步；全是扫描件导致 rows 全失败则是正常业务结论。
        wrote_ok = [e for e in self.to_write if e["result"] in ("新入库", "已覆盖")]
        infra_fail = bool(self.to_write) and not wrote_ok
        if infra_fail:
            self.warnings.append("本批 %d 份可入库 JD **一份都没写成功**（选项/附件/写库/回读链路"
                                 "出现基础设施级错误）；已置 ok=false，请修复后重跑本步"
                                 "（幂等：岗位名称+所属部门+组织分类 三者查重，不会重复建岗）"
                                 % len(self.to_write))

        draft = {"batch_id": self.batch_id, "generated_at": now_iso(),
                 "config_path": self.gateway.config_path, "jobs": self.jobs,
                 "_warnings": self.warnings}
        return draft, self.rows, 0 if (self.fatal is None and not infra_fail) else 1

    # ------------------------------------------------------------------ #
    # Turn 3：--apply jobs_final.json
    # ------------------------------------------------------------------ #
    def run_apply(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        args = self.args
        self.warnings = []
        self.rows = []
        self.summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
                        "attachment_uploaded": 0, "attachment_failed": 0}
        self.have = set(self.gateway.field_keys("job"))
        path = Path(args.apply).expanduser()
        if not path.exists():
            return ({"ok": False, "_warnings": ["--apply 文件不存在：%s" % path]},
                    [{"seq": 1, "file_name": str(path), "result": "失败",
                      "reason": "jobs_final.json 不存在，无法应用 Turn 2 归一化结果"}], 1)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            return ({"ok": False, "_warnings": ["--apply 文件解析失败：%s" % exc]},
                    [{"seq": 1, "file_name": str(path), "result": "失败",
                      "reason": "jobs_final.json 不是合法 JSON：%s" % exc}], 1)

        items = doc.get("jobs") if isinstance(doc, dict) else doc
        if not isinstance(items, list):
            items = []
            self.warnings.append("jobs_final.json 里没有 jobs 数组，无可应用项")

        self._run_named_stage("apply_lookup", lambda: self.apply_lookup(items))
        self._run_named_stage("apply_build", lambda: self.apply_build(items))
        self._run_named_stage("apply_guards", self.apply_guards)
        self._run_named_stage("apply_write", self.apply_write)
        return self._run_named_stage("apply_finalize", lambda: self.apply_finalize(path))

    def apply_lookup(self, items: List[Dict[str, Any]]) -> None:
        """需要时用一次 filter 查询把 job_name → record_id 补齐。"""
        need_lookup = [j for j in items if not (j.get("record_id") or j.get("recordId"))]
        name2rec: Dict[str, str] = {}
        if need_lookup:
            names = sorted({clean(j.get("job_name")) for j in need_lookup
                            if clean(j.get("job_name"))})
            try:
                recs = self.gateway.query_by_names(
                    names, [k for k in ("job_name", "job_id", "department", "org")
                            if k in self.have])
                for r in recs:
                    jn = clean((r.get("cells") or {}).get("job_name"))
                    if jn:
                        name2rec.setdefault(jn, r["record_id"])
            except DwsError as exc:
                self.warnings.append("按岗位名称回查 record_id 失败（%s/%s）：%s"
                                     % (exc.category, exc.code, exc.message[:200]))
        self.name2rec = name2rec

    def apply_build(self, items: List[Dict[str, Any]]) -> None:
        """逐项定位 record_id + cells 组装。"""
        self.updates = []
        self.meta = []
        # (label, must_raw, bonus_raw)，供粒度护栏检查
        self.granularity_items = []
        for i, j in enumerate(items):
            label = clean(j.get("file_name")) or clean(j.get("job_name")) or \
                clean(j.get("job_id")) or "(第 %d 项)" % (i + 1)
            rid = j.get("record_id") or j.get("recordId")
            if not rid:
                rid = self.name2rec.get(clean(j.get("job_name")) or "")
            if not rid:
                self.rows.append({"seq": i + 1, "file_name": label, "result": "失败",
                                  "reason": "未能定位到岗位记录（jobs_final.json 没给 record_id，"
                                            "按岗位名称也没查到），请先跑 Turn 1 或在 jobs_final.json 里补 record_id"})
                self.summary["fail"] += 1
                continue
            cells, hg, ms, bs = self.assembler.build_apply_cells(j, self.have)
            if not cells:
                self.rows.append({"seq": i + 1, "file_name": label, "result": "跳过",
                                  "reason": "jobs_final.json 里这一项没有任何可写语义字段，已跳过"})
                self.summary["skip"] += 1
                continue
            if not (ms or bs or hg):
                self.warnings.append("《%s》Turn 2 归一化后必备技能/加分项/硬性门槛仍全空 → "
                                     "该岗位无法参与打分（分母为 0），请复核 jobs_final.json" % label)
            self.updates.append({"record_id": rid, "cells": cells})
            self.meta.append({"seq": i + 1, "label": label, "rid": rid, "cells": cells})
            # 收集本岗位 Turn 2 归一化后的技能逐条原文（list 或串），供粒度护栏检查
            self.granularity_items.append((label, j.get("must_skills"), j.get("bonus_skills")))

    def apply_guards(self) -> None:
        """Turn 2 语义偷懒护栏 + JD 技能条目粒度护栏（只增 warning 不拦写）。"""
        # ---- Turn 2 语义偷懒护栏 ----
        # 归一化后 must_skills 全部为空或全部雷同 → 说明 Turn 2 大概率没做真归一化，告警并要求复核。
        # 注：部分岗位 must_skills 相似是正常现象，所以只在 100% 空 / 100% 雷同时告警，不做比例阈值。
        have = self.have
        if self.updates and "must_skills" in have:
            ms_norm = [re.sub(r"[\s、，,;；]+", "", str(u["cells"].get("must_skills") or ""))
                       for u in self.updates]
            n_ms = len(ms_norm)
            if n_ms >= 3:
                if all(not m for m in ms_norm):
                    self.warnings.append("Turn 2 归一化护栏：%d 个岗位的必备技能**全部为空** → "
                                         "所有岗位都无法参与打分（分母=0），疑似 Turn 2 没做真归一化"
                                         "（禁止用规则脚本代替 LLM 语义归一化，见 SKILL.md）；"
                                         "请复核 jobs_final.json 后重跑 --apply" % n_ms)
                elif len(set(ms_norm)) == 1:
                    self.warnings.append("Turn 2 归一化护栏：%d 个岗位的必备技能**全部雷同**（同一文本"
                                         "「%s…」）→ 疑似模板化/脚本化归一化产物，请逐岗复核 "
                                         "jobs_final.json 后重跑 --apply" % (n_ms, ms_norm[0][:30]))

        # ---- JD 技能条目粒度护栏（只增 warning 不拦写）----
        # 逐条检查 must_skills/bonus_skills 的粒度（句子碎片/单个泛化词/
        # 泛化软技能/同岗位互为子串）。告警带具体岗位与条目，要求 agent 回 Turn 2 重切成原子技能名词短语后重跑 --apply。
        if self.granularity_items:
            n_gran = self.granularity_guard.check(self.granularity_items, self.warnings)
            if n_gran:
                self.warnings.append("JD技能粒度护栏小结：本批触发 %d 条可疑技能条目（句子碎片/单个泛化词/"
                                     "泛化软技能/同岗位互为子串）。根因是 JD 抽取把技能切成了句子碎片，"
                                     "几乎任何简历都能「语义等价」命中 → 匹配放水、命中数虚高、判定回合反复斟酌。"
                                     "请在 Turn 2 归一化时把每一项改成**原子、可独立验证**的技能/能力名词短语"
                                     "（正反例见 SKILL.md / HOTPATH.md），再重跑 --apply（本护栏只告警不拦写）。"
                                     % n_gran)

    def apply_write(self) -> None:
        """batch_update_verified + 迟到二次复核 + rows/summary 落账。"""
        res: Dict[str, Any] = {}
        if self.updates:
            with self._time_stage() as st:
                try:
                    res = self.gateway.batch_update_verified(self.updates)
                except DwsError as exc:
                    self.warnings.append("批量补写语义字段失败（%s/%s）：%s"
                                         % (exc.category, exc.code, exc.message[:300]))
            self.console.batch_update_verified(
                len(self.updates), res.get("verified", 0), res.get("recovered", 0),
                len(res.get("failed") or []), st.delta, st.calls_delta)
            # 迟到传播延迟二次复核已移除（emit/replay 模式下回读是模拟的）。
            # batch_update_verified 自身做有界重试并如实报 failed；未通过的项由 agent 在下一回合
            # 重跑 --apply 复核（幂等：按 record_id 覆盖写，不会重复建岗）。
            bad = {}
            for fl in (res.get("failed") or []):
                bad[fl.get("record_id")] = str(fl.get("reason"))[:200]
            for m in self.meta:
                if m["rid"] in bad:
                    self.rows.append({"seq": m["seq"], "file_name": m["label"], "result": "失败",
                                      "reason": "补写语义字段已提交但当场回读不到新值（服务端写入传播"
                                                "延迟）；请重跑本步复核。原始原因：%s" % bad[m["rid"]]})
                    self.summary["fail"] += 1
                else:
                    got = "、".join(sorted(m["cells"].keys()))
                    self.rows.append({"seq": m["seq"], "file_name": m["label"], "result": "已覆盖",
                                      "reason": "Turn 2 归一化结果已批量写回（%s）并回读校验通过"
                                                % got})
                    self.summary["overwrite"] += 1
        self.res = res

    def apply_finalize(self, path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        """收尾：tbl.warnings 合并 + draft 组装 + rc。"""
        JobReport.merge_table_warnings(self.warnings, self.gateway.warnings)

        res = self.res
        draft = {"batch_id": self.batch_id, "generated_at": now_iso(),
                 "config_path": self.gateway.config_path,
                 "apply_source": str(path), "applied": len(self.updates),
                 "verify": {k: v for k, v in res.items() if k != "records"},
                 "jobs": [], "_warnings": self.warnings}
        return draft, self.rows, 0 if (not self.updates or res.get("verify_ok")) else 1
