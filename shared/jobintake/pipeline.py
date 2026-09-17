# -*- coding: utf-8 -*-
"""JobPipeline：岗位入库 Turn 1 / Turn 3 的编排层（P9b，原 intake_job.py 的
run / run_turn1 / run_apply 三个巨型 def 逐字分解为阶段方法）。

职责三分（P6 分析 §4）：本类只做**编排 + 业务判定**——阶段顺序、条目状态机
（result / writable / dedupe / attachment_status / record_id）、跳过与 fatal 判定、
告警文案；不 print（→ JobConsole）、不 open（→ textutil.write_json / JobReport）、
不直接碰 `tbl.`（→ JobTableGateway / JobReadBackVerifier）。

红线（裁判 53 面 + P6 分析 §5）：
  * dws 调用门控逐字：`to_write and "submitter" in have` 才发那一次 contact +me；
    ensure_options 的 `if not names or field_key not in have: continue`；
    creates/updates 两次独立写不合并；`if updates:` 才发 batch_update_verified；
    `if lag_ids:` 才 sleep + 迟到复核。
  * 异常归类逐字：扫描/写库 DwsError → fatal；补查/update_verified DwsError →
    只告警；ensure_options Exception → 只告警；contact +me Exception → (None,None)+告警。
  * turn1 rc = `0 if (fatal is None and not infra_fail) else 1`（**不含** bool(rows)，
    rows 判定在外层 JobReport.finish 的 `ok`）；apply rc = `0 if (not updates or
    res.get("verify_ok")) else 1`。
  * 阶段 9 的 reason 拼接（extra 括注 + 「硬性门槛/…待 Turn 2」尾巴）与 summary
    累加顺序、jobs[] 键序（在 JobFieldAssembler）不动。
  * B 侧无 checkpoint / 无墙钟预算 / 无 vision 兜底——A 侧的 WallBudget /
    CheckpointStore / VisionPatchChannel **不接入**（P6 分析 §2.2①）。
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

__all__ = ["JobPipeline"]


class JobPipeline:
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

    # ------------------------------------------------------------------ #
    # 装配 + 调度（原 run() 1183–1275 的骨架部分）
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        args = self.args
        self.t_start = time.monotonic()
        self.batch_id = args.batch_id or new_batch_id()
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
            Path("/tmp/recruit-fast") / self.batch_id
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        report_path = out_dir / "intake_report.json"
        draft_path = out_dir / "jobs_draft.json"

        self.counter = DwsCallCounter()
        client = DwsClient(counter=self.counter, timeout=300, http_timeout=180)

        self.mode = "apply" if args.apply else "turn1"
        self.report = JobReport(self.console, args, self.counter, self.t_start,
                                self.mode, report_path, draft_path)
        self.console.banner(self.mode)
        self.console.batch_info(self.batch_id, out_dir)

        try:
            tbl = AITable(args.config, client=client)
        except Exception as exc:
            return self.report.write_config_failure(exc)
        self.console.base_info(tbl.base_name, tbl.base_id,
                               tbl.table_name("job"), tbl.table_id("job"))
        self.gateway = JobTableGateway(tbl, self.counter)
        self.verifier = JobReadBackVerifier(tbl)

        if self.mode == "apply":
            draft, rows, rc = self.run_apply()
        else:
            draft, rows, rc = self.run_turn1()
        return self.report.finish(draft, rows, rc)

    # ------------------------------------------------------------------ #
    # Turn 1（原 run_turn1 516–961，按源码 `# ---- 阶段 N` 注释切）
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

        self.turn1_extract()
        self.turn1_scan()
        self.turn1_dedupe()
        self.turn1_ensure_options()
        self.turn1_build_rows()
        self.turn1_upload()
        self.turn1_write()
        self.turn1_readback()
        self.turn1_assemble()
        return self.turn1_finalize()

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

        岗位ID 必须全表唯一（老插件 job-intake/SKILL.md:25），本来就要全表扫；
        查重键（岗位名称+所属部门+组织分类）也一并从这次扫描里取，省一次调用。
        """
        self.existing = {}
        self.max_seq = 0
        if self.fatal:
            return
        try:
            t0 = time.monotonic()
            calls0 = self.counter.calls
            scan_fields = [k for k in ("job_id", "job_name", "department", "org")
                           if k in self.have]
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
                              time.monotonic() - t0, self.counter.calls - calls0)
        except DwsError as exc:
            self.fatal = "岗位表查重扫描失败（%s/%s）：%s" % (exc.category, exc.code,
                                                             exc.message[:300])
            self.warnings.append(self.fatal)

    def turn1_dedupe(self) -> None:
        """阶段 3：组织/地点预判 + 复合键查重（老插件口径：三者全同才算重复）。"""
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
                t0 = time.monotonic()
                calls0 = self.counter.calls
                opts = self.gateway.ensure_options(field_key, names)
                self.console.ensure_options(field_key, len(names), len(opts),
                                            time.monotonic() - t0,
                                            self.counter.calls - calls0)
            except Exception as exc:
                self.warnings.append("ensure_options(job.%s) 失败：%s: %s；写入时服务端通常会自动补选项"
                                     % (field_key, type(exc).__name__, str(exc)[:200]))

    def turn1_build_rows(self) -> None:
        """阶段 5：整理写入行（Turn 1 只写脚本能确定的字段）。

        需求提交人（契约 v3 §9#5：可选。config.json 配了 fields.job.submitter 才回填，
        值 = 当前登录用户；未配置 → 留空并在 warnings 里向客户明示语义弱化）。
        """
        submitter_cell: Optional[List[Dict[str, Any]]] = None
        if self.to_write and "submitter" in self.have:
            submitter_cell, submitter_name = self.gateway.fetch_current_user_cell(self.warnings)
            if submitter_cell is not None:
                self.console.submitter(submitter_name)
        elif self.to_write:
            self.warnings.append("config.json 未配置 job.submitter（需求提交人）字段映射 → 该列留空。"
                                 "老插件口径「需求提交人」必填（=当前登录用户），极速版按契约 v3 §9#5 "
                                 "降级为可选（D1 不依赖 user 字段）；客户表里有该列时，在 config.json "
                                 "的 fields.job 里补 \"submitter\" 映射后重跑即可回填（是否补由客户决定）")
        for ent in self.to_write:
            ent["row"] = self.assembler.build_write_row(ent, self.have, submitter_cell,
                                                        self.warnings)

    def turn1_upload(self) -> None:
        """阶段 6：并发上传 JD 附件（一律原始文件名），fileToken 随 create 一次写入。"""
        if self.to_write and not self.args.no_attachment:
            t0 = time.monotonic()
            calls0 = self.counter.calls
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
                                int((time.monotonic() - t0) * 1000),
                                self.counter.calls - calls0)

    def turn1_write(self) -> None:
        """阶段 7：批量写（复合键无法用 batch_upsert_by_key，自己拆 create/update）。"""
        creates = [e for e in self.to_write if e["dedupe"] == "new"]
        updates = [e for e in self.to_write if e["dedupe"] == "overwrite"]
        if creates and not self.fatal:
            t0 = time.monotonic()
            calls0 = self.counter.calls
            try:
                r = self.gateway.batch_create([e["row"] for e in creates])
                self.console.batch_create(len(creates), r.get("created", 0),
                                          len(r.get("failed") or []),
                                          time.monotonic() - t0,
                                          self.counter.calls - calls0)
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
                self.fatal = "批量新建岗位失败（%s/%s）：%s" % (exc.category, exc.code,
                                                               exc.message[:300])
                self.warnings.append(self.fatal)
        if updates and not self.fatal:
            t0 = time.monotonic()
            calls0 = self.counter.calls
            try:
                r = self.gateway.batch_update([{"record_id": e["record_id"], "cells": e["row"]}
                                               for e in updates])
                self.console.batch_update(len(updates), r.get("updated", 0),
                                          len(r.get("failed") or []),
                                          time.monotonic() - t0,
                                          self.counter.calls - calls0)
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
                self.fatal = "批量覆盖岗位失败（%s/%s）：%s" % (exc.category, exc.code,
                                                               exc.message[:300])
                self.warnings.append(self.fatal)

    def turn1_readback(self) -> None:
        """阶段 8：回读校验（一次 filter 查询；岗位名不唯一，用 job_id/三元组精确归属）。"""
        written = [e for e in self.to_write if e["result"] is None]
        self.written = written
        verify: Dict[str, Any] = {}
        if written and not self.fatal:
            rb_fields = [k for k in ("job_name", "job_id", "department", "org", "status",
                                     "work_location", "must_weight", "bonus_weight",
                                     "submit_time", "attachment", "submitter") if k in self.have]
            t0 = time.monotonic()
            verify = self.verifier.verify_jobs(
                written, rb_fields,
                check_attach=("attachment" in rb_fields and not self.args.no_attachment))
            self.console.readback(verify.get("requested", 0), verify.get("found", 0),
                                  len(verify.get("mismatch") or []),
                                  len(verify.get("attachment_missing") or []),
                                  verify.get("settle_polls", 0), time.monotonic() - t0,
                                  verify.get("dws_calls", 0))
            if verify.get("missing"):
                self.warnings.append("回读未能精确归属 %d 个岗位（%s）；写入可能未生效或岗位名+部门+组织"
                                     "在库内有多条，请在后续回合重跑本步复核（契约 D6/D7）"
                                     % (len(verify["missing"]), list(verify["missing"])[:5]))
            for mm in (verify.get("mismatch") or [])[:20]:
                self.warnings.append("回读不一致：岗位 %s 字段 %s 期望 %r 实得 %r"
                                     % (mm.get("key"), mm.get("field"),
                                        str(mm.get("expected"))[:60],
                                        str(mm.get("actual"))[:60]))
            if verify.get("attachment_missing"):
                self.warnings.append("回读发现 %d 个岗位 JD 附件为空：%s；可后续「补传附件」重跑"
                                     % (len(verify["attachment_missing"]),
                                        list(verify["attachment_missing"])[:5]))
            if verify.get("submitter_missing"):
                self.warnings.append("回读发现 %d 个岗位「需求提交人」为空（已配置 job.submitter "
                                     "但写入未确认）：%s；请重跑本步或人工补填"
                                     % (len(verify["submitter_missing"]),
                                        list(verify["submitter_missing"])[:5]))
        self.verify = verify

    def turn1_assemble(self) -> None:
        """阶段 9：组装 rows / jobs。"""
        for ent in self.entries:
            f = ent.get("fields") or {}
            if ent["result"] is None:
                if not ent["writable"]:
                    ent["result"] = "失败"
                    ent["reason"] = ent["reason"] or "未能入库（原因见 warnings）"
                elif self.fatal or not ent.get("record_id"):
                    # 契约 D6：回读没精确归属到 record_id 就不许报成功
                    ent["result"] = "失败"
                    ent["reason"] = self.fatal or (
                        "已提交写入但回读未能精确归属到岗位记录，无法确认入库；"
                        "请在下一回合重跑本步复核（复合键查重幂等，不会重复建岗）")
                elif ent["dedupe"] == "new":
                    ent["result"] = "新入库"
                    ent["reason"] = "新岗位，已分配岗位ID %s 并置「招聘中」" % (ent.get("job_id") or "-")
                else:
                    ent["result"] = "已覆盖"
                    ent["reason"] = "岗位名称+所属部门+组织分类三者全同，已覆盖更新"
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
        """收尾（原 945–961）：tbl.warnings 合并 + infra_fail 判定 + draft 组装。"""
        JobReport.merge_table_warnings(self.warnings, self.gateway.warnings)

        # 基础设施级失败判定（同 intake_resume.py）：该写的岗位一份都没写进去 → ok=false，
        # 让 agent 重跑本步（契约 D7）；全是扫描件导致 rows 全失败则是正常业务结论。
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
    # Turn 3：--apply jobs_final.json（原 run_apply 967–1177）
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

        self.apply_lookup(items)
        self.apply_build(items)
        self.apply_guards()
        self.apply_write()
        return self.apply_finalize(path)

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
        """逐项定位 record_id + cells 组装（原 1012–1065）。"""
        self.updates = []
        self.meta = []
        # 任务二：(label, must_raw, bonus_raw)，供粒度护栏检查
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
            # 任务二：收集本岗位 Turn 2 归一化后的技能**逐条原文**（list 或串），供粒度护栏检查
            self.granularity_items.append((label, j.get("must_skills"), j.get("bonus_skills")))

    def apply_guards(self) -> None:
        """Turn 2 语义偷懒护栏 + JD 技能条目粒度护栏（只增 warning 不拦写）。"""
        # ---- Turn 2 语义偷懒护栏（缺陷2 增补，2026-09-17；与 verify_decisions 的语义护栏同因）----
        # W-F run3 事故：agent 自写 normalize_jobs.py 规则脚本代替 LLM 归一化 JD。
        # 批级形态学护栏（保守阈值，避免误报）：归一化后 must_skills **全部为空**
        # 或**全部雷同**（同一文本）→ 说明 Turn 2 大概率没做真归一化，告警并要求复核。
        # 注：部分岗位（同族岗位）must_skills 相似是正常现象（run1/run3 实测各有 2~3 组
        # 重复），所以只在 100% 空 / 100% 雷同时告警，不做比例阈值。
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

        # ---- 任务二（W-J，根因来自 W-I 实测）：JD 技能条目**粒度护栏**（只增 warning 不拦写）----
        # 紧接上面的批级护栏往下：逐条检查 must_skills/bonus_skills 的粒度（句子碎片/单个泛化词/
        # 泛化软技能/同岗位互为子串）。碎片几乎任何简历都能「语义等价」命中 → 匹配放水 + 判定回合
        # 反复斟酌。告警带具体岗位与条目，要求 agent 回 Turn 2 重切成原子技能名词短语后重跑 --apply。
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
        """batch_update_verified + 迟到二次复核 + rows/summary 落账（原 1102–1168）。"""
        res: Dict[str, Any] = {}
        if self.updates:
            t0 = time.monotonic()
            calls0 = self.counter.calls
            try:
                res = self.gateway.batch_update_verified(self.updates)
            except DwsError as exc:
                self.warnings.append("批量补写语义字段失败（%s/%s）：%s"
                                     % (exc.category, exc.code, exc.message[:300]))
            self.console.batch_update_verified(
                len(self.updates), res.get("verified", 0), res.get("recovered", 0),
                len(res.get("failed") or []), time.monotonic() - t0,
                self.counter.calls - calls0)
            # ---- 迟到的传播延迟二次复核（1 次调用，不空转）----
            # W-B 实测（aitable/table.py 模块文档第 8 条）：对「不久前刚批量写过的 job 表记录」
            # 再发 update，会返回 success 但读回要几分钟后才见到新值，当场怎么重试都没用。
            # batch_update_verified 只做**有界**重试然后如实报 failed（D6）。这里在全部片
            # 写完后补一次「迟到复核」：短延迟（十几秒内可见）的情况能就地救回来，
            # 长延迟的仍然报 failed 并让 agent 在下一回合重跑 --apply（幂等）。
            lag_ids = [f.get("record_id") for f in (res.get("failed") or [])
                       if f.get("category") == "server_write_lag" and f.get("record_id")]
            late_recovered: List[str] = []
            if lag_ids:
                exp_map = {u["record_id"]: u["cells"] for u in self.updates}
                late_expected = {rid: exp_map[rid] for rid in lag_ids if rid in exp_map}
                time.sleep(self.args.late_verify_wait)
                rb2 = self.gateway.readback_verify(
                    lag_ids,
                    sorted({k for c in late_expected.values() for k in c}),
                    late_expected)
                bad2 = {m["record_id"] for m in rb2["mismatch"]} | set(rb2["missing"])
                late_recovered = [rid for rid in lag_ids if rid not in bad2]
                if late_recovered:
                    res["failed"] = [f for f in res["failed"]
                                     if f.get("record_id") not in set(late_recovered)]
                    res["verified"] = res.get("verified", 0) + len(late_recovered)
                    res["late_recovered"] = len(late_recovered)
                    res["verify_ok"] = not res["failed"]
                    self.warnings.append("写入传播延迟二次复核（+%ds）救回 %d 条：%s"
                                         % (self.args.late_verify_wait, len(late_recovered),
                                            late_recovered[:6]))
                still = [f.get("record_id") for f in res.get("failed") or []]
                if still:
                    self.warnings.append("仍有 %d 条「record update 返回 success 但当场回读不到新值」：%s。"
                                         "这是钉钉 AI 表格 job 表的**服务端最终一致性异常**（W-B 实测："
                                         "同一批刚写过的记录，随后 34s/47s/156s 轮询全是旧值，约 4 分钟后"
                                         "才读到正确值；W-C1 复测：有时数分钟后值确实落库，有时整批丢弃）。"
                                         "**不要相信 success，也不要当场空转重试**——请在下一回合重跑同一条 "
                                         "--apply 命令复核（幂等：按 record_id 覆盖写，不会重复建岗）"
                                         % (len(still), still[:6]))
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
                    late = "（含传播延迟二次复核救回）" if m["rid"] in late_recovered else ""
                    self.rows.append({"seq": m["seq"], "file_name": m["label"], "result": "已覆盖",
                                      "reason": "Turn 2 归一化结果已批量写回（%s）并回读校验通过%s"
                                                % (got, late)})
                    self.summary["overwrite"] += 1
        self.res = res

    def apply_finalize(self, path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
        """收尾（原 1169–1177）：tbl.warnings 合并 + draft 组装 + rc。"""
        JobReport.merge_table_warnings(self.warnings, self.gateway.warnings)

        res = self.res
        draft = {"batch_id": self.batch_id, "generated_at": now_iso(),
                 "config_path": self.gateway.config_path,
                 "apply_source": str(path), "applied": len(self.updates),
                 "verify": {k: v for k, v in res.items() if k != "records"},
                 "jobs": [], "_warnings": self.warnings}
        return draft, self.rows, 0 if (not self.updates or res.get("verify_ok")) else 1
