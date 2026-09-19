# -*- coding: utf-8 -*-
"""digest 编排（DigestBuilder）：build_digest 的 OO 分解。

    _open_table()            config → AITable；失败走早退产物（ok:false digest）
    _load_raw_candidates()   模式 A（candidates.json）/ 模式 B（--from-table 表导出）
    _normalize()             逐人 normalize + key 去重改名
    _check_onboarded()       老插件铁律剔除（errors 时短路，不发查询）
    _enrich()                evidence 全文兜底开窗 + 聚合 warning
    _load_jobs()             在招岗位 + 「无可判定」两条 error 检查
    _resolve_batch_id()      --batch-id > candidates.json > 时间戳
    _count_combos()          同组织组合数 + unmatched warning
    _audit_prefilter()       机械复查 + 聚合 warning
    _assemble_meta()         meta 键插入序 = digest 字节（红线，逐字保留）
    _assemble_digest()       顶层键序 batch_id/generated_at/ok/errors/scoring_rules/
                             candidates/jobs/meta（逐字保留）
    _write_shards()          分片循环（含 L3/L4 裁剪遥测键）+ 落盘
    _write_digest()          digest.json 落盘

已知缺陷豁免（不许修）：config 失败早退路径的 meta 只有 3 个键，
report_and_emit 消费时抛 KeyError —— 本类**保持**该早退 meta 的键集合不变。
"""

import datetime as _dt
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aitable.client import DwsCallCounter, DwsClient, now_iso
from aitable.table import AITable

from match.candidates import CandidateNormalizer
from match.chunking import SLIM_DROP_JOB_FIELDS, ShardPlanner
from match.match_basics import DEFAULT_MAX_PER_BATCH
from match.gates import PrefilterAuditor
from match.jobparse import JobRecordParser
from match.match_basics import dump_json_doc
from match.scoring import SCORING_RULES
from match.source import MatchSourceGateway
from match.tablevalues import CHARS_PER_TOKEN, SKILL_TEXT_LIMIT, WORK_TEXT_LIMIT, \
    est_tokens, full
from match.match_basics import python_version


def _now() -> str:
    try:
        return now_iso()
    except Exception:
        return _dt.datetime.now().isoformat(timespec="seconds")


class DigestBuilder:
    """「简历×岗位」组合包（digest.json + digest_batch_NN.json）的构建编排。"""

    def __init__(self, config_path: str, candidates_path: Optional[str], out_dir: str,
                 max_per_batch: int = DEFAULT_MAX_PER_BATCH,
                 batch_id: Optional[str] = None,
                 from_table: bool = False, org: Optional[str] = None,
                 exclude_onboarded: bool = False,
                 org_prefilter: bool = True, slim_jobs: bool = True,
                 requirements_limit: int = 600,
                 job_field_extractor: Any = None,
                 resume_field_extractor: Any = None,
                 replay_path: Optional[str] = None):
        self.config_path = config_path
        self.candidates_path = candidates_path
        self.out_dir = out_dir
        self.max_per_batch = max_per_batch
        self.batch_id = batch_id
        self.from_table = from_table
        self.org = org
        self.exclude_onboarded = exclude_onboarded
        self.org_prefilter = org_prefilter
        self.slim_jobs = slim_jobs
        self.requirements_limit = requirements_limit
        self.job_field_extractor = job_field_extractor
        self.resume_field_extractor = resume_field_extractor
        self.replay_path = replay_path
        self.normalizer = CandidateNormalizer()
        self.auditor = PrefilterAuditor()
        self.planner = ShardPlanner(org_prefilter, slim_jobs)

    # ------------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        self._t0 = time.time()
        self._cfg = Path(self.config_path).expanduser()
        self._cand_path = Path(self.candidates_path).expanduser() if self.candidates_path else None
        outdir = Path(self.out_dir).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)

        self.warnings: List[str] = []
        self.errors: List[str] = []      # 不可判定的原因（digest.ok=false 时非空）

        failed = self._open_table(self._cfg, outdir)
        if failed is not None:
            return failed

        self._load_raw_candidates()
        self._normalize()
        self.active, self.onboarded = ([], []) if self.errors else \
            self.gateway.check_onboarded(self.normalized, self.warnings)
        self._enrich()
        self.normalizer.aggregate_identity_review(self.active, self.warnings)
        self._load_jobs()
        self.bid = self._resolve_batch_id()
        self._count_combos()
        self._audit_prefilter()

        self.shards = self.planner.make_shards(self.active, self.max_per_batch)
        in_chars, in_toks = est_tokens({"candidates": self.active, "jobs": self.jobs})
        self._assemble_meta(in_chars, in_toks)

        # 先把耗时/调用数补进 meta，再一次性落盘（避免写两遍）
        self.meta["elapsed_ms"] = int((time.time() - self._t0) * 1000)
        self.meta["dws_calls"] = self.table.dws_calls

        self._assemble_digest()
        self._write_shards(outdir)
        self._write_digest(outdir)

        # 注意：这里用 abspath 而不是 resolve()——macOS 上 /tmp 是 /private/tmp 的符号链接，
        # resolve() 会把 ARTIFACT 行打成 /private/tmp/... ，跟用户传进来的 --out-dir 长得不一样。
        return {"digest_path": os.path.abspath(str(outdir / "digest.json")),
                "digest": self.digest, "table": self.table,
                "shard_paths": self.shard_paths, "shard_docs": self.shard_docs}

    # --------------------------------------------------------------- stages
    def _open_table(self, cfg: Path, outdir: Path) -> Optional[Dict[str, Any]]:
        """config → AITable；打不开 → 失败也要落产物，返回早退 res（正常路径返回 None）。"""
        try:
            counter = DwsCallCounter()
            client = DwsClient(counter=counter,
                               replay_path=self.replay_path)
            self.table = AITable(str(cfg), client=client)
        except Exception as exc:                          # config 坏 → 失败也要落产物
            errors = self.errors
            errors.append("打不开 config.json（%s: %s）→ 无法查岗位/候选人，本批不可判定"
                          % (type(exc).__name__, exc))
            digest = {"batch_id": self.batch_id or ("match-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S")),
                      "generated_at": _now(), "ok": False, "errors": errors,
                      "scoring_rules": SCORING_RULES, "candidates": [], "jobs": [],
                      "meta": {"config_path": str(cfg), "python": python_version(),
                               "warnings": self.warnings}}
            dpath = outdir / "digest.json"
            dump_json_doc(digest, dpath)
            return {"digest_path": os.path.abspath(str(dpath)), "digest": digest, "table": None,
                    "shard_paths": [], "shard_docs": []}
        self.gateway = MatchSourceGateway(
            self.table,
            JobRecordParser(self.job_field_extractor, self.requirements_limit),
            self.resume_field_extractor)
        return None

    def _load_raw_candidates(self) -> None:
        self.ft_meta: Dict[str, Any] = {}
        if self.from_table:
            # 存量候选人反向匹配（指定岗位反向匹配 / 重建全部匹配的数据源）
            try:
                self.raw_cands, self.ft_meta = self.gateway.fetch_candidates_from_table(
                    org=self.org, exclude_onboarded=self.exclude_onboarded,
                    warnings=self.warnings)
            except SystemExit as exc:
                self.errors.append(str(exc))
                self.raw_cands, self.ft_meta = [], {}
            self.cand_doc = {}
            if not self.raw_cands and not self.errors:
                self.errors.append("简历库表里没有可导出的存量候选人%s → 本批不可判定；"
                                   "请先跑简历入库，或检查 --org 过滤值是否与表内「组织」选项一致"
                                   % ("（--org=%s 过滤后为空）" % self.org if self.org else ""))
        else:
            try:
                self.raw_cands, self.cand_doc = self.gateway.load_candidates_file(self._cand_path)
            except SystemExit as exc:
                self.errors.append(str(exc))
                self.raw_cands, self.cand_doc = [], {}
            if not self.raw_cands and not self.errors:
                self.errors.append("candidates.json 里 candidates 数组是空的，没东西可匹配")

    def _normalize(self) -> None:
        self.normalized: List[Dict[str, Any]] = []
        for i, c in enumerate(self.raw_cands):
            if not isinstance(c, dict):
                self.warnings.append("candidates[%d] 不是对象，已跳过" % i)
                continue
            self.normalized.append(self.normalizer.normalize(c, i, self.warnings))
        # key 去重（C1 没给 key 时按序号生成，理论上不会撞）
        seen: Dict[str, int] = {}
        for c in self.normalized:
            k = c["key"]
            if k in seen:
                seen[k] += 1
                newk = "%s_%d" % (k, seen[k])
                self.warnings.append("候选人 key %r 重复，已改名为 %r" % (k, newk))
                c["key"] = newk
            else:
                seen[k] = 0

    def _enrich(self) -> None:
        # 兜底：上游分段为空的 evidence，用简历表里的「简历全文」按关键词开窗补上
        self.n_enriched = 0
        for c in self.active:
            ft = c.pop("_full_text_from_table", "") or full(c.get("full_text"))
            notes = self.normalizer.enrich_evidence(c, ft)
            if notes:
                self.n_enriched += 1
                self.warnings.extend(notes)
        self.empty_edu = [c["key"] for c in self.active
                          if not (c.get("evidence", {}).get("education_text") or "").strip()]
        if self.empty_edu:
            self.warnings.append("有 %d 个候选人连兜底后 evidence.education_text 仍为空（%s）→ "
                                 "学历门槛只能靠 education 字段判，agent 判 fail 前请特别小心"
                                 % (len(self.empty_edu), ",".join(self.empty_edu[:8])))

    def _load_jobs(self) -> None:
        self.jobs: List[Dict[str, Any]] = []
        if not self.errors:
            try:
                self.jobs = self.gateway.fetch_open_jobs(self.warnings)
            except SystemExit as exc:
                self.errors.append(str(exc))
        if not self.jobs and not self.errors:
            self.errors.append("表里查不到任何「状态=招聘中」的岗位 → agent 无从判定；"
                               "请先跑 job-intake 把岗位入库，或检查 job 表「状态」字段取值")
        if self.active == [] and self.normalized and not self.errors:
            self.errors.append("候选人全部被剔除（如 沟通状态=已入职）→ 本批无可判定候选人")

    def _resolve_batch_id(self) -> str:
        return self.batch_id or self.cand_doc.get("batch_id") or (
            "match-table-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S") if self.from_table
            else "match-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

    def _count_combos(self) -> None:
        # 组合数（只在同组织内）
        self.combos = 0
        unmatched: List[str] = []
        for c in self.active:
            corg = c.get("org_guess")
            n = len([j for j in self.jobs if (not corg or j.get("org") == corg)])
            self.combos += n
            if n == 0:
                unmatched.append("%s(%s) 组织=%s" % (c.get("name"), c["key"], corg))
        if unmatched:
            self.warnings.append("有 %d 个候选人在表里找不到同组织的在招岗位，本轮不产生任何判定：%s"
                                 % (len(unmatched), "; ".join(unmatched[:8])))

    def _audit_prefilter(self) -> None:
        # 组织预筛错杀的机械复查（零 token；口径与判据见 find_prefilter_suspicious）。
        # 命中的候选人 needs_review 已就地追加 "org"；prefilter_suspicious 明细只进**分片**
        # 候选人（Turn 2 消费面），digest.json 候选人只带 needs_review 标记（apply 侧消费面）。
        self.suspicious = (self.auditor.find_prefilter_suspicious(
            self.active, self.jobs, org_prefilter=self.org_prefilter)
            if (self.jobs and not self.errors) else {})
        self.susp_pairs = sum(len(v) for v in self.suspicious.values())
        if self.suspicious:
            by_key = {c["key"]: c for c in self.active}
            sample = "、".join("%s(%s)×%d" % (by_key[k].get("name") or "?", k,
                                              len(self.suspicious[k]))
                               for k in sorted(self.suspicious)[:6])
            self.warnings.append("组织预筛疑似错杀：%d 个候选人共 %d 个跨组织岗位**全部通过机械"
                                 "门槛**（学历/年限/证书；专业是语义项不参与）却被组织预筛删除——%s%s。"
                                 "预筛可能错杀，请复核组织归属：明细见分片候选人 prefilter_suspicious"
                                 "（needs_review 已追加 org）；Turn 2 复核确认有误 → 回填 "
                                 "candidate_overrides.org 并**重跑同一命令**让预筛按新组织重切"
                                 % (len(self.suspicious), self.susp_pairs, sample,
                                    " 等%d人" % len(self.suspicious) if len(self.suspicious) > 6 else ""))

    def _assemble_meta(self, in_chars: int, in_toks: int) -> None:
        cfg, cand_path = self._cfg, self._cand_path
        self.meta = {
            "config_path": str(cfg.resolve()),
            "candidates_path": (str(cand_path.resolve()) if cand_path else None),
            "source_mode": "from_table" if self.from_table else "candidates_file",
            "base_name": self.table.base_name,
            "base_id": self.table.base_id,
            "max_per_batch": max(1, int(self.max_per_batch)),
            "shard_count": len(self.shards),
            "candidate_count_total": len(self.normalized),
            "candidate_count_active": len(self.active),
            "excluded_onboarded": self.onboarded,
            "job_count": len(self.jobs),
            "combo_count": self.combos,
            "input_chars": in_chars,
            "input_est_tokens": in_toks,
            "token_basis": "中文按 %.1f 字符/token 粗估（与前序实验同口径）" % CHARS_PER_TOKEN,
            "shards": [],
            "evidence_policy": "D4：education_text / cert_text 完整不截断；"
                               "work_text ≤%d 字、skill_text ≤%d 字（超出截断）"
                               % (WORK_TEXT_LIMIT, SKILL_TEXT_LIMIT),
            "requirements_text_limit": self.requirements_limit,
            "needs_review_years": sorted([c["key"] for c in self.active if "years" in c.get("needs_review", [])]),
            # 组织预筛机械复查的汇总（逐人明细在分片候选人 prefilter_suspicious 里；
            # digest 候选人只带 needs_review 追加的 "org" 标记，供 apply/verify 侧消费）
            "prefilter_suspicious": {"candidate_count": len(self.suspicious),
                                     "job_pair_count": self.susp_pairs,
                                     "candidate_keys": sorted(self.suspicious)},
            "evidence_enriched_candidates": self.n_enriched,
            "evidence_empty_education": self.empty_edu,
            "dws_calls": self.table.dws_calls,
            "elapsed_ms": int((time.time() - self._t0) * 1000),
            "python": python_version(),
            "warnings": self.warnings,
            "errors": self.errors,
        }
        if self.from_table:
            self.meta["from_table"] = dict(self.ft_meta, org_filter=self.org,
                                           exclude_onboarded=bool(self.exclude_onboarded))
            # evidence 来源标注（整段截断的计数在 from_table_evidence_sources 里）
            if (self.ft_meta.get("from_table_evidence_sources") or {}).get("full_text_fallback"):
                self.meta["evidence_source"] = "full_text_fallback"

    def _assemble_digest(self) -> None:
        ok = not self.errors
        self.digest = {
            "batch_id": self.bid,
            "generated_at": _now(),
            "ok": ok,                        # 产物凭证统一口径
            "errors": self.errors,
            "scoring_rules": SCORING_RULES,
            "candidates": self.active,
            "jobs": self.jobs,
            "meta": self.meta,
        }

    def _write_shards(self, outdir: Path) -> None:
        # 分片文件：candidates 只放本片；jobs 默认按 L3 组织预筛 + L4 字段裁剪，
        # 把 agent 要读进上下文的字符数压到最小。
        #
        # ⚠️ **合并版 digest.json 的构造完全不动**：它的 meta.shards 仍按**全量 jobs** 计算、
        # 不带任何裁剪遥测键，因此 digest.json 在开关开/关两种情况下**逐字节不变**。
        # 裁剪与遥测只作用于**分片文件**（agent 唯一读进上下文的东西）。
        # 两个开关都关时，分片文件也逐字节回到裁剪前形态。
        # （注：对 digest 的影响是**只增键**——候选人 evidence 身份原文键、
        # needs_review 的 org/name/email 标记、meta 汇总键，下游 verify/apply 按只增不减兼容。）
        ok = not self.errors
        optimize = bool(self.org_prefilter or self.slim_jobs)
        self.shard_paths: List[str] = []
        self.shard_docs: List[Dict[str, Any]] = []
        for i, sh in enumerate(self.shards, 1):
            # digest.json 的 meta.shards：保持移植前口径（全量 jobs、无遥测键），保证 digest 逐字节稳定
            sm_digest = self.planner.shard_meta(sh, self.jobs, i, len(self.shards))
            self.meta["shards"].append(sm_digest)
            # 分片文件的 shard meta：按预筛/裁剪后的 shard_jobs 计算；开优化时才带遥测键
            shard_jobs, pf_note = self.planner.select_shard_jobs(sh, self.jobs)
            if self.slim_jobs:
                shard_jobs = [self.planner.slim_job(j) for j in shard_jobs]
            # prefilter_suspicious 明细只挂**分片**候选人（dict 浅拷贝，digest.json 的
            # 候选人对象不被污染）；sm_shard 用挂载后的副本算，输入 chars 遥测才诚实。
            shard_cands = [(dict(c, prefilter_suspicious=self.suspicious[c["key"]])
                            if c["key"] in self.suspicious else c) for c in sh]
            sm_shard = self.planner.shard_meta(shard_cands, shard_jobs, i, len(self.shards))
            if optimize:
                sm_shard["job_count_all"] = len(self.jobs)
                sm_shard["jobs_org_prefiltered"] = (pf_note == "prefiltered")
                sm_shard["jobs_prefilter_note"] = pf_note
                sm_shard["jobs_slimmed"] = bool(self.slim_jobs)
                sm_shard["dropped_job_fields"] = list(SLIM_DROP_JOB_FIELDS) if self.slim_jobs else []
            n_susp_shard = sum(len(self.suspicious.get(c["key"]) or []) for c in sh)
            if n_susp_shard:
                sm_shard["prefilter_suspicious_count"] = n_susp_shard
            shard_doc = {
                "batch_id": self.bid,
                "generated_at": self.digest["generated_at"],
                "ok": ok,
                "errors": self.errors,
                "shard": sm_shard,
                "scoring_rules": SCORING_RULES,
                "candidates": shard_cands,
                "jobs": shard_jobs,
                "meta": {k: v for k, v in self.meta.items() if k != "shards"},
            }
            p = outdir / ("digest_batch_%02d.json" % i)
            dump_json_doc(shard_doc, p)
            # 同上：abspath 而非 resolve()，避开 macOS /tmp → /private/tmp 符号链接
            self.shard_paths.append(os.path.abspath(str(p)))
            self.shard_docs.append(shard_doc)

    def _write_digest(self, outdir: Path) -> None:
        dump_json_doc(self.digest, outdir / "digest.json")
