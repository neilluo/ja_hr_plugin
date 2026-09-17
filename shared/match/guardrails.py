# -*- coding: utf-8 -*-
"""语义合理性护栏（SemanticGuardrails）：缺陷2 修复（2026-09-17）的统计形态退化检测。

原 verify_decisions.semantic_guardrails（L335-570，236 行）搬入，按 SEM-E/H/R/G/O/P
六族拆成六个私有方法（分析报告 D.3 类 5）；阈值 SEM_GUARD_DEFAULTS 共 **9** 键，
只有 4 个有 CLI 开关（--sem-*），另 5 个只能经 `evaluate(thresholds=…)` 编程覆盖
——这是必须冻结的 API 面。

护栏告警文案（每条带具体证据：实测值 vs 阈值 + 样本 key）是 verify stdout / apply
report 的字节面，**逐字保留**；metrics 的键插入序同样是字节（无 sort_keys）。
"""

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from match.hitmap import as_str_list

# ---------------------------------------------------------------------------
# 语义合理性护栏（缺陷2 修复，2026-09-17）—— 阈值全部可配置（模块常量 + CLI 覆盖）
# ---------------------------------------------------------------------------
# 背景（W-F run3 实测事故）：agent 在 Turn 2 没做语义判定，而是自写规则脚本
# （normalize_jobs.py + generate_decisions.py）代替大模型 → verify 全部形式校验 PASS、
# apply ok=true，但业务结果崩塌：0 条推荐（run1/run2 = 22 条）、skill_hits 普遍 ≤3、
# evidence 同人同模板跨岗位复用、3 个岗位被错归组织。形式校验防不了语义偷懒，
# 下面这组护栏专门盯「统计形态退化」，每条告警必须带具体证据（实测值 vs 阈值 + 样本 key）。
SEM_GUARD_DEFAULTS: Dict[str, float] = {
    # evidence 去重率下限：unique(evidence)/passed 低于它 → 模板化复用告警。
    # 标定：W-F run1=0.55、run2=0.67（正常），run3=0.33（退化）→ 取 0.40 分界。
    "evidence_unique_ratio_min": 0.40,
    # 同一段 evidence 被**不同候选人**引用 → 疑似编造（evidence 必须出自本人简历）。
    # 占比超过该值升级为硬错误拒写库；低于该值只告警。run1/run2/run3 实测均为 0。
    "evidence_cross_candidate_error_ratio": 0.20,
    # 「命中项过少」：skill_hits ≤ low_skill_hits_max 的条目占比 ≥ low_skill_hits_ratio，
    # 且岗位 must_skills 分母中位数 > low_skill_hits_min_denominator → 告警（规则脚本
    # 只做浅层关键词匹配的典型形态）。run3=33/33=1.00，run1=25/67=0.37。
    "low_skill_hits_max": 3,
    "low_skill_hits_ratio": 0.90,
    "low_skill_hits_min_denominator": 4,
    # 「命中项放水」：分母 ≥ full_hit_min_denominator 的条目里 100% 全命中占比
    # ≥ full_hit_ratio → 告警（W-F 观察到普晓刚 15/15 全命中但原文只支撑 11~13 项）。
    # run1 全命中 18/67=0.27 → 不误报；阈值取 0.60。
    "full_hit_min_denominator": 5,
    "full_hit_ratio": 0.60,
    # 推荐率异常高：推荐/通过条目 > recommend_ratio_high → 告警复核。run1=22/67=0.33。
    "recommend_ratio_high": 0.60,
    # 护栏最小样本量：passed 条目少于它时分布类护栏不评估（小批次统计无意义、易误报）。
    "min_passed_for_guards": 5,
}


class SemanticGuardrails:
    """「判定像不像认真做的」的批级统计护栏（形式校验之外的第二道闸）。"""

    def evaluate(self, digest: Dict[str, Any], decisions: Dict[str, Any],
                 passed_audit: Sequence[Dict[str, Any]],
                 rejected_pairs_n: int,
                 thresholds: Optional[Dict[str, float]] = None,
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        """语义合理性护栏（缺陷2）。返回 (errors, warnings, metrics)。

        与形式校验的分工：形式校验查「JSON 对不对」，本护栏查「判定像不像认真做的」。
        每条告警都带**具体证据**（哪个 candidate/job、实测值 vs 阈值），不许只说"异常"。
        阈值来自 SEM_GUARD_DEFAULTS，可被 `thresholds` 覆盖（CLI --sem-* 参数）。

        升级为 error（拒写库）的只有一类：**同一段 evidence 被不同候选人引用**且占比高
        ——evidence 必须是本人简历原文，跨人复用等于编造数据，写库会污染匹配记录的
        「匹配依据」列；其余分布类异常（0 推荐、命中全 ≤3、模板化 evidence…）在极端
        批次里**可能合法**（例如整批确实无人达标），一律 warning + 强制人工复核，
        不拦写库（拦了就是误报，W-F run1 类正常批次会被天天卡住）。
        """
        self.digest = digest
        self.decisions = decisions
        self.rejected_pairs_n = rejected_pairs_n
        th = dict(SEM_GUARD_DEFAULTS)
        for k, v in (thresholds or {}).items():
            if v is not None and k in th:
                th[k] = v
        self.th = th
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {"thresholds": th, "evaluated": True}

        self.audits = [a for a in (passed_audit or []) if isinstance(a, dict)]
        self.n_passed = len(self.audits)
        self.min_n = int(th["min_passed_for_guards"])
        if self.n_passed == 0:
            self._all_rejected()
            return self.errors, self.warnings, self.metrics
        self._sem_e()
        self._sem_h()
        self._sem_r()
        self._sem_g()
        self._sem_o()
        self._sem_p()
        return self.errors, self.warnings, self.metrics

    # ---------------- 全批 0 通过（最强的退化信号，其余族不评估） ----------------
    def _all_rejected(self) -> None:
        # 全批 0 通过本身就是最强的退化信号（门槛通过率异常，见 SEM-G1）
        if self.rejected_pairs_n > 0:
            self.warnings.append({"code": "sem_all_rejected", "key": "batch",
                                  "detail": "整批 0 条通过、%d 个组合全部被拒（门槛通过率=0%%）。"
                                            "可能确实无人达标，也可能是 Turn 2 没做语义判定"
                                            "（W-F run3 事故形态之一）→ 必须人工抽查 rejected "
                                            "的 reason 与 evidence 后复核" % self.rejected_pairs_n})
        self.metrics.update(n_passed=0, rejected_pairs=self.rejected_pairs_n)
        self.metrics["evaluated"] = False

    # ---------------- SEM-E：evidence 复用检测 ----------------
    def _sem_e(self) -> None:
        th, audits, n_passed, min_n = self.th, self.audits, self.n_passed, self.min_n
        ev_entries = [(a.get("candidate_key"), a.get("job_key"),
                       str(a.get("evidence") or "").strip()) for a in audits]
        ev_nonempty = [(c, j, e) for c, j, e in ev_entries if e]
        ev_map: Dict[str, List[Tuple[Any, Any]]] = {}
        for c, j, e in ev_nonempty:
            ev_map.setdefault(e, []).append((c, j))
        unique_ratio = len(ev_map) / float(n_passed)
        # E1：同一段 evidence 被**不同候选人**引用（疑似编造/模板生成）
        cross = {e: pairs for e, pairs in ev_map.items()
                 if len({c for c, _ in pairs}) > 1}
        cross_entries = sum(len(p) for p in cross.values())
        cross_ratio = cross_entries / float(n_passed)
        self.metrics.update(evidence_unique_ratio=round(unique_ratio, 3),
                            evidence_unique=len(ev_map),
                            evidence_cross_candidate_entries=cross_entries,
                            evidence_cross_candidate_ratio=round(cross_ratio, 3))
        if cross:
            sample = sorted(("%s(%s)" % ("/".join(str(c) for c in sorted({c for c, _ in pairs})),
                                          e[:30]) for e, pairs in list(cross.items())[:3]))
            detail = ("有 %d 条 pass（占比 %.0f%%，阈值 %.0f%%）的 evidence 被**不同候选人**"
                      "复用（evidence 必须是本人简历原文，跨人复用=编造嫌疑）；样本：%s"
                      % (cross_entries, cross_ratio * 100,
                         th["evidence_cross_candidate_error_ratio"] * 100, "; ".join(sample)))
            if cross_ratio > float(th["evidence_cross_candidate_error_ratio"]):
                self.errors.append({"code": "sem_evidence_cross_candidate", "key": "passed",
                                    "detail": detail + " → 占比过高，整批不写库，请重做 Turn 2 判定"})
            else:
                self.warnings.append({"code": "sem_evidence_cross_candidate", "key": "passed",
                                      "detail": detail})
        # E2：evidence 去重率过低（同人同模板跨岗位复用的批级形态）
        if n_passed >= min_n and unique_ratio < float(th["evidence_unique_ratio_min"]):
            worst = sorted(ev_map.items(), key=lambda kv: -len(kv[1]))[:3]
            ws = "; ".join("「%s…」被 %s 复用" % (e[:24], ",".join("%s×%s" % p for p in pairs[:4]))
                           for e, pairs in worst)
            self.warnings.append({"code": "sem_evidence_reuse", "key": "passed",
                                  "detail": "evidence 去重率 %.2f < 阈值 %.2f（%d 条 pass 只有 %d 段"
                                            "不同 evidence）→ 模板化复用嫌疑（W-F run3 实测 0.33；"
                                            "正常批 run1/run2 = 0.55/0.67）。最重复样本：%s"
                                            % (unique_ratio, th["evidence_unique_ratio_min"],
                                               n_passed, len(ev_map), ws)})

    # ---------------- SEM-H：命中项数量异常（过少 / 放水两侧） ----------------
    def _sem_h(self) -> None:
        th, audits, n_passed, min_n = self.th, self.audits, self.n_passed, self.min_n
        hits_denoms = [(a.get("candidate_key"), a.get("job_key"),
                        int((a.get("recomputed") or {}).get("skill_hits") or 0),
                        int((a.get("recomputed") or {}).get("skill_total") or 0)) for a in audits]
        low_max = int(th["low_skill_hits_max"])
        low_n = sum(1 for _, _, h, t in hits_denoms if h <= low_max)
        denoms_sorted = sorted(t for _, _, _, t in hits_denoms if t > 0)
        median_denom = denoms_sorted[len(denoms_sorted) // 2] if denoms_sorted else 0
        self.metrics.update(skill_hits_low_n=low_n, skill_hits_low_ratio=round(low_n / float(n_passed), 3),
                            must_denominator_median=median_denom)
        if n_passed >= min_n and median_denom > int(th["low_skill_hits_min_denominator"]) \
                and low_n / float(n_passed) >= float(th["low_skill_hits_ratio"]):
            examples = ["%s×%s=%d/%d" % (c, j, h, t) for c, j, h, t in hits_denoms[:6]]
            self.warnings.append({"code": "sem_skill_hits_too_few", "key": "passed",
                                  "detail": "skill_hits ≤%d 的条目占 %d/%d=%.0f%%（阈值 %.0f%%），而岗位"
                                            " must_skills 分母中位数=%d（>%d）→ 命中数与分母规模严重"
                                            "不匹配，疑似关键词浅匹配代替语义判定（W-F run3：33/33 全"
                                            "≤3、分母 5~15）。样本：%s"
                                            % (low_max, low_n, n_passed, low_n * 100.0 / n_passed,
                                               th["low_skill_hits_ratio"] * 100, median_denom,
                                               th["low_skill_hits_min_denominator"], "; ".join(examples))})
        fh_min = int(th["full_hit_min_denominator"])
        fh_pool = [(c, j, h, t) for c, j, h, t in hits_denoms if t >= fh_min]
        fh_n = sum(1 for _, _, h, t in fh_pool if h == t)
        self.metrics.update(full_hit_n=fh_n, full_hit_pool=len(fh_pool),
                            full_hit_ratio=round(fh_n / float(len(fh_pool)), 3) if fh_pool else None)
        if n_passed >= min_n and fh_pool and fh_n / float(len(fh_pool)) >= float(th["full_hit_ratio"]):
            examples = ["%s×%s=%d/%d" % (c, j, h, t) for c, j, h, t in fh_pool[:6]]
            self.warnings.append({"code": "sem_skill_hits_full_inflated", "key": "passed",
                                  "detail": "分母 ≥%d 的 %d 条里 %d 条（%.0f%%，阈值 %.0f%%）100%% 全命中"
                                            " → 放水嫌疑（W-F 实测普晓刚 15/15 全命中但原文只支撑 11~13"
                                            " 项）。样本：%s"
                                            % (fh_min, len(fh_pool), fh_n, fh_n * 100.0 / len(fh_pool),
                                               th["full_hit_ratio"] * 100, "; ".join(examples))})

    # ---------------- SEM-R：推荐率 / 推荐分布异常 ----------------
    def _sem_r(self) -> None:
        th, audits, n_passed, min_n = self.th, self.audits, self.n_passed, self.min_n
        rec_dist: Dict[str, int] = {}
        for a in audits:
            r = (a.get("recomputed") or {}).get("recommend") or "?"
            rec_dist[r] = rec_dist.get(r, 0) + 1
        n_recommend = rec_dist.get("推荐", 0)
        recommend_ratio = n_recommend / float(n_passed)
        self.metrics.update(recommend_distribution=rec_dist,
                            recommend_ratio=round(recommend_ratio, 3))
        if n_passed >= min_n and n_recommend == 0:
            self.warnings.append({"code": "sem_zero_recommend", "key": "passed",
                                  "detail": "整批 %d 条 pass 里「推荐」=0 条（分布=%s）→ 异常"
                                            "（W-F run3 事故：0 推荐，run1/run2=22 条）；请复核"
                                            "门槛与命中判定是否过严/偷懒，确需 0 推荐要向用户说明理由"
                                            % (n_passed, json.dumps(rec_dist, ensure_ascii=False))})
        elif n_passed >= min_n and recommend_ratio > float(th["recommend_ratio_high"]):
            self.warnings.append({"code": "sem_recommend_ratio_high", "key": "passed",
                                  "detail": "推荐率 %.0f%%（%d/%d）> 阈值 %.0f%%（分布=%s）→ 异常偏高，"
                                            "疑似放水；请抽查高分条目的 evidence 是否支撑"
                                            % (recommend_ratio * 100, n_recommend, n_passed,
                                               th["recommend_ratio_high"] * 100,
                                               json.dumps(rec_dist, ensure_ascii=False))})
        if n_passed >= min_n and len([k for k in rec_dist if k != "?"]) == 1:
            only = [k for k in rec_dist if k != "?"][0]
            self.warnings.append({"code": "sem_recommend_collapsed", "key": "passed",
                                  "detail": "整批 %d 条 pass 的推荐状态**全部**是「%s」（分布塌缩到单一"
                                            "取值）→ 语义判定退化嫌疑（正常批 run1/run2 三档齐有："
                                            "推荐22/待定26/不推荐19）；请人工复核后再写库"
                                            % (n_passed, only)})

    # ---------------- SEM-G：门槛通过率异常（全拒 / 全过） ----------------
    def _sem_g(self) -> None:
        min_n = self.min_n
        total_pairs = self.n_passed + self.rejected_pairs_n
        self.metrics.update(rejected_pairs=self.rejected_pairs_n,
                            gate_pass_ratio=round(self.n_passed / float(total_pairs), 3) if total_pairs else None)
        if total_pairs >= min_n:
            if self.rejected_pairs_n == 0:
                self.warnings.append({"code": "sem_all_passed", "key": "batch",
                                      "detail": "整批 %d 个组合**全部通过**硬性门槛（0 拒绝）→ 异常"
                                                "（真实批次门槛通过率实测 ~12%%）；疑似门槛没判/"
                                                "全放行，请复核 gate_detail" % total_pairs})
            # 全拒（passed=0）已在 n_passed==0 分支处理

    # ---------------- SEM-O：candidate_overrides 覆盖率异常（D13 复核没做） ----------------
    def _sem_o(self) -> None:
        need_years = [str(c.get("key")) for c in (self.digest.get("candidates") or [])
                      if isinstance(c, dict) and "years" in (c.get("needs_review") or [])]
        ovr = [o for o in as_str_list(self.decisions.get("candidate_overrides")) if isinstance(o, dict)]
        reviewed = {str(o.get("candidate_key")) for o in ovr if o.get("years_experience") is not None}
        covered = [k for k in need_years if k in reviewed]
        self.metrics.update(needs_review_years=len(need_years), years_reviewed=len(covered))
        if need_years and not covered:
            self.warnings.append({"code": "sem_years_review_missing", "key": "candidate_overrides",
                                  "detail": "digest 标记 needs_review:[\"years\"] 的候选人有 %d 个（%s），"
                                            "但 candidate_overrides 里**一个** years_experience 复核值"
                                            "都没有 → D13 复核没做（估算年限直接喂一票否决门槛，"
                                            "W-F 实测 9/9 标记、agent 执行率不满：P0 8/9、run1 4/9）；"
                                            "必须补做复核或向用户说明"
                                            % (len(need_years), ",".join(need_years[:10]))})
        self._ovr = ovr

    # ---------------- SEM-P（P5）：身份/组织安全阀的复核没有落点 ----------------
    def _sem_p(self) -> None:
        # digest 里 needs_review 含 org/name/email 的候选人（P5：组织预筛机械复查命中 /
        # 姓名来源文件名/OCR/agent 草稿 / 邮箱疑似 OCR 噪声），decisions 里必须有复核动作：
        #   org   = candidate_overrides 给了 org 或 org_reason（或条目 evidence 里有组织说明）；
        #   name/email = 该人任一条目的 evidence/reason 文本里出现复核关键词——契约 v3 §9#1
        #     的 candidate_overrides 没有 name/email 键，复核结论只能落在 evidence 里
        #     （HOTPATH 回合 2 已写明该落点）。
        # 两者皆无 → warning 强制人工复核（尺度同 SEM-O / sem_years_review_missing，不拒写库）。
        identity_review = (("org", ("组织", "org")),
                           ("name", ("姓名", "name")),
                           ("email", ("邮箱", "email", "邮件")))
        ovr_by_ck: Dict[str, Dict[str, Any]] = {}
        for o in self._ovr:
            if o.get("candidate_key") is not None:
                ovr_by_ck.setdefault(str(o.get("candidate_key")), o)
        text_by_ck: Dict[str, str] = {}
        for p in self.decisions.get("passed") or []:
            if isinstance(p, dict) and p.get("candidate_key") is not None:
                k = str(p.get("candidate_key"))
                text_by_ck[k] = "%s %s" % (text_by_ck.get(k, ""), str(p.get("evidence") or ""))
        for rj in self.decisions.get("rejected") or []:
            if isinstance(rj, dict) and rj.get("candidate_key") is not None:
                k = str(rj.get("candidate_key"))
                text_by_ck[k] = "%s %s" % (text_by_ck.get(k, ""), str(rj.get("reason") or ""))
        for fld, kws in identity_review:
            need = [str(c.get("key")) for c in (self.digest.get("candidates") or [])
                    if isinstance(c, dict) and fld in (c.get("needs_review") or [])]
            if not need:
                continue
            missing = []
            for k in need:
                o = ovr_by_ck.get(k) or {}
                if fld == "org" and (o.get("org") or o.get("org_reason")):
                    continue
                txt = text_by_ck.get(k, "").lower()
                if any(w.lower() in txt for w in kws):
                    continue
                missing.append(k)
            self.metrics.update(**{"needs_review_%s" % fld: len(need),
                                   "%s_review_missing" % fld: len(missing)})
            if missing:
                if fld == "org":
                    act = ("candidate_overrides 里没有任何 org / org_reason 复核结论，条目 "
                           "evidence 也无组织说明 → 组织归属复核没做（P5 预筛错杀阀形同虚设）；"
                           "必须依 evidence 复核：确认无误写 org_reason，确认有误写 "
                           "candidate_overrides.org 并重跑 build_match_input")
                elif fld == "name":
                    act = ("decisions 里没有一处「姓名」复核说明 → evidence.name_text 原文"
                           "比对没做；必须复核并把结论写进该人任一条目 evidence（误读要业务话"
                           "照转请用户人工修正库内记录；candidate_overrides 无 name 键）")
                else:
                    act = ("decisions 里没有一处「邮箱」复核说明 → evidence.email_text 原文"
                           "比对没做；必须复核并把结论写进该人任一条目 evidence（疑似 OCR "
                           "误读如 qq.com→q9.com 要照转请用户人工修正）")
                self.warnings.append({"code": "sem_%s_review_missing" % fld,
                                      "key": "candidate_overrides",
                                      "detail": "digest 标记 needs_review 含 \"%s\" 的候选人有 %d 个"
                                                "（%s%s），但 %s"
                                                % (fld, len(need), ",".join(missing[:10]),
                                                   " 等" if len(missing) > 10 else "", act)})
