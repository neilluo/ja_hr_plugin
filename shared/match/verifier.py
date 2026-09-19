# -*- coding: utf-8 -*-
"""主校验（DecisionVerifier）：原 verify 的 OO 分解 + 结果组装原语。

`verify(digest, decisions, check_coverage=True, sem_thresholds=None,
semantic_guards=True) -> dict` 是**冻结签名**（sem_thresholds 的 5 个仅 API
键必须继续可传）。薄壳留在 verify_decisions.py 入口，apply 侧经入口薄壳同进程复用
同一个 verify()。

返回 dict 的键插入序（ok/verified_at/errors/warnings/counts/coverage/summary/
python + extras）与全部告警文案是 stdout/report 字节面，**逐字保留**。

推荐档位判定（total ≥80 推荐 / 60~79 待定 / <60 不推荐）经 ScoreCalculator 构造
注入：verify_decisions 入口的 `_recommend_of` 必须留在入口且行为支配。
"""

import datetime as _dt
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from match.match_basics import EVIDENCE_MAX_LEN, GATE_ITEMS, \
    INVALID_PASSED_RATIO_LIMIT, RECOMMEND_VALUES
from match.coverage import CoverageChecker
from match.guardrails import SemanticGuardrails
from match.hitmap import GateVerdictReader, HitMapper, as_str_list
from match.scoring import ScoreCalculator
from match.match_basics import json_dumps_zh, make_err, make_warn, python_version


def dist(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def build_result(ok: bool, errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]],
                 counts: Dict[str, Any], coverage: Dict[str, Any], summary: Dict[str, Any],
                 **extra: Any) -> Dict[str, Any]:
    out = {
        "ok": ok,
        "verified_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
        "coverage": coverage,
        "summary": summary,
        "python": python_version(),
    }
    out.update(extra)
    return out


def slim_for_stdout(res: Dict[str, Any]) -> Dict[str, Any]:
    """stdout 只打结论，不把 passed_audit / digest 全量倒出来（否则刷屏）。"""
    keep = ("ok", "verified_at", "counts", "coverage", "summary", "errors", "warnings", "python")
    out = {k: res.get(k) for k in keep if k in res}
    out["score_audit"] = [{"candidate_key": a["candidate_key"], "job_key": a["job_key"],
                           "candidate_name": a.get("candidate_name"),
                           "job_name": a.get("job_name"),
                           "recomputed": a["recomputed"], "model": a["model"],
                           "mismatch": a["mismatch"], "valid": a["valid"]}
                          for a in (res.get("passed_audit") or [])]
    return out


class DecisionVerifier:
    """decisions.json 的六族校验：覆盖率/引用/集合/算术复核/结构/语义护栏。"""

    def __init__(self, calc: Optional[ScoreCalculator] = None):
        self._calc = calc or ScoreCalculator()
        self._hits = HitMapper()
        self._gates = GateVerdictReader()
        self._coverage = CoverageChecker()
        self._guards = SemanticGuardrails()

    # err/warn → 实例方法（append 顺序 = 字节序，不变）
    def err(self, code: str, key: Any, detail: str) -> None:
        self.errors.append(make_err(code, key, detail))

    def warn(self, code: str, key: Any, detail: str) -> None:
        self.warnings.append(make_warn(code, key, detail))

    def verify(self, digest: Any, decisions: Any, check_coverage: bool = True,
               sem_thresholds: Optional[Dict[str, float]] = None,
               semantic_guards: bool = True) -> Dict[str, Any]:
        """核心校验函数（apply_decisions.py 经入口薄壳同进程复用）。

        `check_coverage=False` 用于「没传 --digest、岗位信息是从表里现查的」降级场景：
        那时无法知道**真实**的期望组合集合（组织归属可能不全），所以只跳过覆盖率判定，
        引用/集合/算术/evidence/结构 五项照查，并记一条 `coverage_not_checked` warning。

        `sem_thresholds` / `semantic_guards`（缺省启用、阈值见
        SEM_GUARD_DEFAULTS）：语义合理性护栏开关与阈值覆盖；不传 = 默认阈值全开，
        既有调用方（apply_decisions）零改动兼容。

        返回结构化结论 dict；`ok` 为 False 时调用方**不得写库**。
        """
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []

        if not isinstance(digest, dict):
            self.err("digest_not_object", "digest", "digest 不是 JSON 对象，实得 %s" % type(digest).__name__)
            return build_result(False, self.errors, self.warnings, {}, {}, {})
        if not isinstance(decisions, dict):
            self.err("decisions_not_object", "decisions",
                     "decisions 不是 JSON 对象（要求 {batch_id,passed,rejected,...}），实得 %s"
                     % type(decisions).__name__)
            return build_result(False, self.errors, self.warnings, {}, {}, {})
        self.digest = digest
        self.decisions = decisions

        self._index_inputs()
        self._audit_passed()
        self._audit_rejected()
        return self._conclude(check_coverage, sem_thresholds, semantic_guards)

    # ---------------- 输入索引 + 结构完整性 ----------------
    def _index_inputs(self) -> None:
        digest, decisions = self.digest, self.decisions
        cands = digest.get("candidates")
        jobs = digest.get("jobs")
        if not isinstance(cands, list):
            self.err("digest_missing_candidates", "digest.candidates", "digest 缺 candidates 数组")
            cands = []
        if not isinstance(jobs, list):
            self.err("digest_missing_jobs", "digest.jobs", "digest 缺 jobs 数组")
            jobs = []

        self.cand_by_key: Dict[str, Dict[str, Any]] = {}
        for c in cands:
            if isinstance(c, dict) and c.get("key") is not None:
                self.cand_by_key[str(c["key"])] = c
        self.job_by_key: Dict[str, Dict[str, Any]] = {}
        for j in jobs:
            if isinstance(j, dict) and j.get("key") is not None:
                self.job_by_key[str(j["key"])] = j

        self.overrides = [o for o in as_str_list(decisions.get("candidate_overrides")) if isinstance(o, dict)]
        self.passed = [p for p in as_str_list(decisions.get("passed"))]
        self.rejected = [r for r in as_str_list(decisions.get("rejected"))]
        if decisions.get("passed") is not None and not isinstance(decisions.get("passed"), list):
            self.err("passed_not_list", "passed", "passed 必须是数组，实得 %s" % type(decisions.get("passed")).__name__)
        if decisions.get("rejected") is not None and not isinstance(decisions.get("rejected"), list):
            self.err("rejected_not_list", "rejected", "rejected 必须是数组，实得 %s" % type(decisions.get("rejected")).__name__)
        if "passed" not in decisions and "rejected" not in decisions:
            self.err("decisions_empty", "decisions", "decisions 里既没有 passed 也没有 rejected，等于没判定")

        dbid = decisions.get("batch_id")
        if dbid and digest.get("batch_id") and dbid != digest.get("batch_id"):
            self.warn("batch_id_mismatch", "batch_id",
                      "decisions.batch_id=%r 与 digest.batch_id=%r 不一致（可能拿错了批次的判定结果）"
                      % (dbid, digest.get("batch_id")))

    # ---------------- 2/3/4/5. passed 逐条：引用、结构、集合、算术复核 ----------------
    def _audit_passed(self) -> None:
        self.seen: Dict[Tuple[str, str], List[str]] = {}
        self.passed_audit: List[Dict[str, Any]] = []
        self.valid_passed: List[Dict[str, Any]] = []
        cand_by_key, job_by_key = self.cand_by_key, self.job_by_key

        for i, p in enumerate(self.passed):
            pk = "passed[%d]" % i
            if not isinstance(p, dict):
                self.err("entry_not_object", pk, "passed[%d] 不是对象，实得 %s" % (i, type(p).__name__))
                continue
            ck = p.get("candidate_key")
            jk = p.get("job_key")
            for f in ("candidate_key", "job_key"):
                if not p.get(f):
                    self.err("missing_field", "%s.%s" % (pk, f), "passed[%d] 缺 %s" % (i, f))
            if ck is None or jk is None:
                continue
            ck, jk = str(ck), str(jk)
            pair = (ck, jk)
            self.seen.setdefault(pair, []).append("passed[%d]" % i)
            if ck not in cand_by_key:
                self.err("unknown_candidate_key", "%s|%s" % pair,
                         "passed[%d] 的 candidate_key=%r 不在 digest 里（digest 有 %d 个候选人：%s）"
                         % (i, ck, len(cand_by_key), ",".join(sorted(cand_by_key)[:12])))
            if jk not in job_by_key:
                self.err("unknown_job_key", "%s|%s" % pair,
                         "passed[%d] 的 job_key=%r 不在 digest 里（digest 有 %d 个岗位：%s）"
                         % (i, jk, len(job_by_key), ",".join(sorted(job_by_key)[:24])))
            if ck not in cand_by_key or jk not in job_by_key:
                continue

            job = job_by_key[jk]
            must = job.get("must_skills") or []
            bonus = job.get("bonus_skills") or []

            # 结构完整性
            for f in ("skill_hits", "bonus_hits", "recommend", "evidence"):
                if f not in p:
                    if f in ("skill_hits", "bonus_hits"):
                        self.err("missing_field", "%s|%s" % pair,
                                 "passed[%d] 缺 %s（稀疏格式也必须给，缺了就没法算分）" % (i, f))
                    else:
                        self.warn("missing_field", "%s|%s" % pair, "passed[%d] 缺 %s" % (i, f))
            if "gate_detail" not in p:
                self.err("missing_field", "%s|%s" % pair, "passed[%d] 缺 gate_detail（四项门槛判定）" % i)

            # gate_detail：在 passed 里就必须四项全达标
            gd = p.get("gate_detail")
            if isinstance(gd, dict):
                for g in GATE_ITEMS:
                    if g not in gd:
                        self.warn("gate_detail_incomplete", "%s|%s" % pair,
                                  "gate_detail 缺 %r 项（应含 education/major/years/certificates 四项）" % g)
                bad_gates = []
                for g, v in gd.items():
                    verdict = self._gates.verdict(v)
                    if verdict is False:
                        bad_gates.append("%s=%s" % (g, v))
                if bad_gates:
                    self.err("gate_detail_inconsistent", "%s|%s" % pair,
                             "该组合出现在 passed 里，但 gate_detail 有不达标项：%s"
                             "（硬门槛一票否决，不达标必须进 rejected）" % "; ".join(bad_gates))
            elif gd is not None:
                self.err("gate_detail_not_object", "%s|%s" % pair,
                         "gate_detail 必须是对象，实得 %s" % type(gd).__name__)

            # 3. 集合校验（防模型编造命中项）
            #    先做**可解释的归一化映射**（模型常把切碎的条目合回一句引用），
            #    映射不上的才算编造 → 该条无效 + warnings
            skill_hits_raw = as_str_list(p.get("skill_hits"))
            bonus_hits_raw = as_str_list(p.get("bonus_hits"))
            skill_eff, skill_map = self._hits.map_hits_to_items(skill_hits_raw, must)
            bonus_eff, bonus_map = self._hits.map_hits_to_items(bonus_hits_raw, bonus)
            bad_skill = [d for d in skill_map if d["rule"] == "fabricated"]
            bad_bonus = [d for d in bonus_map if d["rule"] == "fabricated"]
            fixed_skill = [d for d in skill_map if d["rule"] != "fabricated"]
            fixed_bonus = [d for d in bonus_map if d["rule"] != "fabricated"]
            if bad_skill:
                self.warn("fabricated_skill_hit", "%s|%s" % pair,
                          "skill_hits 有 %d 项无法映射到岗位 must_skills（判为编造，**该条 pass 无效**、"
                          "不会建匹配记录）：%s ｜岗位必备技能=%s"
                          % (len(bad_skill), json_dumps_zh([d["original"] for d in bad_skill][:6]),
                             json_dumps_zh(list(must)[:8])))
            if bad_bonus:
                self.warn("fabricated_bonus_hit", "%s|%s" % pair,
                          "bonus_hits 有 %d 项无法映射到岗位 bonus_skills（判为编造，**该条 pass 无效**）：%s"
                          " ｜岗位加分项=%s"
                          % (len(bad_bonus), json_dumps_zh([d["original"] for d in bad_bonus][:6]),
                             json_dumps_zh(list(bonus)[:8])))
            if fixed_skill or fixed_bonus:
                self.warn("skill_hit_normalized", "%s|%s" % pair,
                          "命中项已归一化映射回岗位原文条目（模型把被切碎的条目合回一句/做了缩写）："
                          "%s" % json_dumps_zh((fixed_skill + fixed_bonus)[:4])[:400])
            # 同一集合内重复命中会虚增分子（map_hits_to_items 已按岗位条目去重）
            raw_skill = len([h for h in skill_hits_raw if self._hits.squash(h)]) - len(bad_skill)
            raw_bonus = len([h for h in bonus_hits_raw if self._hits.squash(h)]) - len(bad_bonus)
            dup_s = max(0, raw_skill - len(skill_eff))
            dup_b = max(0, raw_bonus - len(bonus_eff))
            if dup_s or dup_b:
                self.warn("duplicate_hits", "%s|%s" % pair,
                          "命中项有重复（技能 %d 项 / 加分 %d 项映射到同一条岗位条目），已按去重后计数"
                          % (dup_s, dup_b))
            skill_u = self._hits.dedupe_norm(skill_eff)
            bonus_u = self._hits.dedupe_norm(bonus_eff)

            # evidence
            ev = p.get("evidence")
            ev_s = "" if ev is None else str(ev)
            if not ev_s.strip():
                self.err("evidence_empty", "%s|%s" % pair, "passed[%d] 的 evidence 为空（必须给原文引用）" % i)
            elif len(ev_s) > EVIDENCE_MAX_LEN:
                self.err("evidence_too_long", "%s|%s" % pair,
                         "evidence %d 字 > 上限 %d 字：%s…"
                         % (len(ev_s), EVIDENCE_MAX_LEN, ev_s[:40]))

            rec = p.get("recommend")
            if rec is not None and str(rec).strip() not in RECOMMEND_VALUES:
                self.err("recommend_invalid_value", "%s|%s" % pair,
                         "recommend=%r 非法，只能是 %s" % (rec, "/".join(RECOMMEND_VALUES)))

            # 4. 算术复核（脚本算，模型输出只作对照）
            got = self._calc.compute(skill_u, bonus_u, must, bonus, job.get("weights"))
            audit: Dict[str, Any] = {
                "candidate_key": ck, "job_key": jk,
                "candidate_name": cand_by_key[ck].get("name"),
                "job_name": job.get("job_name"), "job_id": job.get("job_id"),
                "recomputed": {k: got[k] for k in ("skill_score", "bonus_score", "total_score",
                                                   "recommend", "skill_total", "bonus_total",
                                                   "skill_hits", "bonus_hits", "weights")},
                "model": {k: p.get(k) for k in ("skill_score", "bonus_score", "total_score",
                                                "total", "recommend", "skill_total", "bonus_total")
                          if k in p},
                "mismatch": [],
                "notes": got["notes"],
                "valid": not (bad_skill or bad_bonus),
                "invalid_reason": ("skill_hits/bonus_hits 有无法映射到岗位列表的项（判为编造）"
                                   if (bad_skill or bad_bonus) else None),
                # 归一化后的**有效**命中项：apply_decisions 用这个写库和算分
                "skill_hits_effective": skill_u,
                "bonus_hits_effective": bonus_u,
                "hits_normalized": (fixed_skill + fixed_bonus) or None,
                "fabricated_hits": ([d["original"] for d in bad_skill]
                                    + [d["original"] for d in bad_bonus]) or None,
                "gate_detail": p.get("gate_detail"),
                "evidence": ev_s.strip() or None,
            }
            m = audit["model"]
            for mk, rk in (("skill_score", "skill_score"), ("bonus_score", "bonus_score"),
                           ("total_score", "total_score"), ("total", "total_score")):
                if mk in m and m[mk] is not None:
                    try:
                        mv = float(m[mk])
                    except (TypeError, ValueError):
                        audit["mismatch"].append("%s=%r 不是数字" % (mk, m[mk]))
                        continue
                    if abs(mv - got[rk]) > 0.5:
                        audit["mismatch"].append("%s: 模型=%s 脚本重算=%s" % (rk, m[mk], got[rk]))
            if "recommend" in m and str(m["recommend"]).strip() and \
                    str(m["recommend"]).strip() != got["recommend"]:
                audit["mismatch"].append("recommend: 模型=%s 脚本重算=%s（总分=%d）"
                                         % (m["recommend"], got["recommend"], got["total_score"]))
            if audit["mismatch"]:
                self.warn("score_mismatch", "%s|%s" % pair,
                          "%s × %s：%s ｜以脚本重算为准"
                          % (audit["candidate_name"], audit["job_name"], "; ".join(audit["mismatch"])))
            for n in got["notes"]:
                self.warn("score_denominator", "%s|%s" % pair, "%s × %s：%s"
                          % (audit["candidate_name"], audit["job_name"], n))
            self.passed_audit.append(audit)
            if audit["valid"]:
                self.valid_passed.append(audit)

    # ---------------- rejected 逐条：引用合法性 ----------------
    def _audit_rejected(self) -> None:
        self.rejected_pairs: List[Tuple[str, str]] = []
        self.rejected_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        cand_by_key, job_by_key = self.cand_by_key, self.job_by_key
        for i, r in enumerate(self.rejected):
            rk = "rejected[%d]" % i
            if not isinstance(r, dict):
                self.err("entry_not_object", rk, "rejected[%d] 不是对象，实得 %s" % (i, type(r).__name__))
                continue
            ck = r.get("candidate_key")
            if not ck:
                self.err("missing_field", "%s.candidate_key" % rk, "rejected[%d] 缺 candidate_key" % i)
                continue
            ck = str(ck)
            if ck not in cand_by_key:
                self.err("unknown_candidate_key", ck,
                         "rejected[%d] 的 candidate_key=%r 不在 digest 里" % (i, ck))
            jks = r.get("job_keys")
            if jks is None:
                # 容错：有人写成 job_key 单数
                if r.get("job_key"):
                    jks = [r.get("job_key")]
                else:
                    self.err("missing_field", "%s.job_keys" % rk, "rejected[%d] 缺 job_keys" % i)
                    continue
            if not isinstance(jks, list):
                self.err("job_keys_not_list", ck, "rejected[%d] 的 job_keys 必须是数组，实得 %s"
                         % (i, type(jks).__name__))
                continue
            if not jks:
                self.warn("rejected_empty_job_keys", ck, "rejected[%d] 的 job_keys 是空数组（这条没有意义）" % i)
            reason = r.get("reason")
            if reason is None or not str(reason).strip():
                self.warn("rejected_reason_missing", ck,
                          "rejected[%d] 缺 reason（用户清单要标注「未过门槛原因」）" % i)
            for jk in jks:
                jk = str(jk)
                pair = (ck, jk)
                self.seen.setdefault(pair, []).append("rejected[%d]" % i)
                self.rejected_pairs.append(pair)
                self.rejected_index[pair] = {"candidate_key": ck, "job_key": jk,
                                             "candidate_name": cand_by_key.get(ck, {}).get("name"),
                                             "job_name": job_by_key.get(jk, {}).get("job_name"),
                                             "job_id": job_by_key.get(jk, {}).get("job_id"),
                                             "reason": (str(reason).strip() if reason else None)}
                if jk not in job_by_key:
                    self.err("unknown_job_key", "%s|%s" % pair,
                             "rejected[%d] 的 job_key=%r 不在 digest 里" % (i, jk))

    # ---------------- 1. 覆盖率 + 编造占比 + 6. 语义护栏 + 结果组装 ----------------
    def _conclude(self, check_coverage: bool,
                  sem_thresholds: Optional[Dict[str, float]],
                  semantic_guards: bool) -> Dict[str, Any]:
        cand_by_key, job_by_key = self.cand_by_key, self.job_by_key
        exp_pairs, per_cand, no_job = self._coverage.expected_pairs(self.digest, self.overrides)
        exp_set = set(exp_pairs)
        missing = sorted(exp_set - set(self.seen.keys()))
        duplicates = sorted([p for p, srcs in self.seen.items() if len(srcs) > 1])
        unexpected = sorted(set(self.seen.keys()) - exp_set)
        if not check_coverage:
            self.warn("coverage_not_checked", "coverage",
                      "本次没做覆盖率校验（调用方传了 check_coverage=False，通常是没给 --digest、"
                      "岗位信息现查于表）；缺失/重复组合不会被发现，强烈建议传 --digest")
            missing, unexpected = [], []
        for p in missing:
            self.err("missing_pair", "%s|%s" % p,
                     "组合 %s × %s 既不在 passed 也不在 rejected（覆盖率缺失）；岗位=%s 候选人=%s"
                     % (p[0], p[1], job_by_key.get(p[1], {}).get("job_name"),
                        cand_by_key.get(p[0], {}).get("name")))
        for p in duplicates:
            self.err("duplicate_pair", "%s|%s" % p,
                     "组合 %s × %s 出现了 %d 次（%s），必须且只能出现一次"
                     % (p[0], p[1], len(self.seen[p]), ", ".join(self.seen[p])))
        for p in unexpected:
            self.warn("unexpected_pair", "%s|%s" % p,
                      "组合 %s × %s 不是「同组织在招岗位」组合（跨组织或岗位不在招），"
                      "本插件不会为它建匹配记录" % (p[0], p[1]))
        for ck in no_job:
            self.warn("candidate_no_open_job", ck,
                      "候选人 %s 在本 digest 里没有同组织的在招岗位 → 期望覆盖 0 个组合"
                      % cand_by_key.get(ck, {}).get("name"))

        # 编造命中项本身只作 warning + 该条无效；但**比例过高**说明模型整体在瞎写，
        # 这时必须拦住不让写库。
        n_invalid = len([a for a in self.passed_audit if not a["valid"]])
        if self.passed_audit and n_invalid * 1.0 / len(self.passed_audit) > INVALID_PASSED_RATIO_LIMIT:
            self.err("too_many_invalid_passed", "passed",
                     "有 %d/%d 条 pass 的命中项判为编造（>%.0f%%）→ 模型输出整体不可信，"
                     "不写库；请缩小分片或重跑本批判定"
                     % (n_invalid, len(self.passed_audit), INVALID_PASSED_RATIO_LIMIT * 100))

        # ---------------- 6. 语义合理性护栏 ----------------
        # 形式校验全过 ≠ 语义判定认真做了（规则脚本代跑 → 0 推荐但 verify PASS）。
        # 护栏告警**必须**如实转述给用户并说明可能需要重做 Turn 2，禁止静默吞掉继续写库。
        sem_metrics: Dict[str, Any] = {"evaluated": False}
        if semantic_guards:
            sem_errs, sem_warns, sem_metrics = self._guards.evaluate(
                self.digest, self.decisions, self.passed_audit, len(self.rejected_pairs),
                sem_thresholds)
            for e in sem_errs:
                self.errors.append(e)
            for w in sem_warns:
                self.warnings.append(w)

        counts = {
            "candidates": len(cand_by_key),
            "jobs": len(job_by_key),
            "expected_pairs": len(exp_set),
            "passed_entries": len(self.passed),
            "rejected_entries": len(self.rejected),
            "rejected_pairs": len(self.rejected_pairs),
            "covered_pairs": len(set(self.seen.keys()) & exp_set),
            "missing_pairs": len(missing),
            "duplicate_pairs": len(duplicates),
            "unexpected_pairs": len(unexpected),
            "valid_passed_entries": len(self.valid_passed),
            "invalid_passed_entries": len(self.passed) - len(self.valid_passed),
        }
        coverage = {"missing": ["%s|%s" % p for p in missing],
                    "duplicate": ["%s|%s" % p for p in duplicates],
                    "unexpected": ["%s|%s" % p for p in unexpected],
                    "expected_per_candidate": {k: len(v) for k, v in per_cand.items()}}
        recomputed = {"%s|%s" % (a["candidate_key"], a["job_key"]): a["recomputed"]
                      for a in self.passed_audit}
        summary = {
            "score_mismatch": len([a for a in self.passed_audit if a["mismatch"]]),
            "fabricated_skill_hits": len([w for w in self.warnings if w["code"] == "fabricated_skill_hit"]),
            "fabricated_bonus_hits": len([w for w in self.warnings if w["code"] == "fabricated_bonus_hit"]),
            "hits_normalized": len([w for w in self.warnings if w["code"] == "skill_hit_normalized"]),
            "invalid_passed_entries": len([a for a in self.passed_audit if not a["valid"]]),
            "evidence_problems": len([e for e in self.errors if e["code"] in ("evidence_empty",
                                                                              "evidence_too_long")]),
            "recommend_distribution": dist(a["recomputed"]["recommend"] for a in self.valid_passed),
            "denominator_warnings": len([w for w in self.warnings if w["code"] == "score_denominator"]),
            # 语义护栏（只增不删）：触发的护栏代码 + 指标，供 agent/报告直接引用
            "semantic_guards": {
                "triggered_errors": sorted({e["code"] for e in self.errors if e["code"].startswith("sem_")}),
                "triggered_warnings": sorted({w["code"] for w in self.warnings
                                              if w["code"].startswith("sem_")}),
                "metrics": sem_metrics,
            },
        }
        ok = not self.errors
        return build_result(ok, self.errors, self.warnings, counts, coverage, summary,
                            passed_audit=self.passed_audit, rejected_index=self.rejected_index,
                            recomputed=recomputed, digest=self.digest, decisions=self.decisions)
