#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_decisions.py —— 匹配编排层第 2 步：对 LLM 产出的 decisions.json 做**纯本地**校验。

零 dws 调用、零网络、零第三方依赖。apply_decisions.py 内部会先调本模块的 `verify()`，
校验不通过就**不写库**（契约 D6：失败可见，禁止静默丢弃）。

校验项（任务书冻结）
------------------
1. **覆盖率**：每个 candidate × 每个**同组织**在招岗位，必须在 `passed` 或 `rejected` 里
   出现且**仅出现一次**；缺失或重复都报错并列出具体 key。
2. **引用合法性**：所有 `candidate_key` / `job_key` 必须在 digest 里存在。
3. **集合校验**：`skill_hits ⊆ must_skills`、`bonus_hits ⊆ bonus_skills`，
   越界即判该条无效（**防模型编造命中项**，契约 D16）。
4. **算术复核**：按口径重算 技能得分 / 加分项得分 / 总分 / 推荐状态。
   契约 D16：**分数一律脚本算**，模型输出的分数只作对照，不一致记 warnings。
5. **JSON 结构完整性** + `evidence` 非空且长度 ≤ 80 字。
6. **语义合理性护栏**（缺陷2 修复，2026-09-17；W-F run3 事故：agent 用自写规则脚本
   代替 Turn 2 语义判定 → 形式校验全 PASS 但 0 推荐、evidence 模板化复用）。
   见 `SEM_GUARD_DEFAULTS` 与 `semantic_guardrails()`：evidence 跨候选人复用（高占比
   → error 拒写库）、evidence 去重率过低、skill_hits 普遍过少 / 普遍 100% 全命中、
   0 推荐 / 推荐率异常高 / 推荐分布塌缩、门槛全拒或全过、needs_review:["years"]
   却零 override（D13 复核没做）。阈值可用 --sem-* CLI 覆盖。
   **护栏告警必须如实转述给用户并说明可能需要重做判定，禁止静默吞掉继续写库。**

用法
----
    python3 scripts/verify_decisions.py --digest <digest.json> --decisions <decisions.json>

stdout 打印结构化校验结论 JSON；退出码 0 = 通过，1 = 有问题。
"""

import argparse
import datetime as _dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# sys.path：用 __file__ 定位插件根（禁止硬编码绝对路径）
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT / "shared", _ROOT / "shared" / "vendor"):
    _s = str(_p)
    if _p.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

EVIDENCE_MAX_LEN = 80                   # 契约 §3.3：evidence 原文引用 ≤80 字
GATE_ITEMS = ("education", "major", "years", "certificates")
RECOMMEND_VALUES = ("推荐", "待定", "不推荐")
#: 编造命中项的 pass 条目占比超过这个值 → 升级为硬错误，整批不写库（防模型语义崩塌）
INVALID_PASSED_RATIO_LIMIT = 0.34
_PASS_LIKE = ("pass", "passed", "达标", "符合", "满足", "yes", "true", "y", "✓", "✅")
_FAIL_LIKE = ("fail", "failed", "不达标", "不符", "不满足", "no", "false", "n", "✗", "❌")

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


# ---------------------------------------------------------------------------
# 评分口径（与 build_match_input.SCORING_RULES / apply_decisions 三处必须一致）
# ---------------------------------------------------------------------------
def round_half_up(x: float) -> int:
    """四舍五入（**不是** python3 的 banker's rounding：round(66.5)=66 会算错）。"""
    if x is None:
        return 0
    return int(math.floor(float(x) + 0.5))


def compute_scores(skill_hits: Sequence[str], bonus_hits: Sequence[str],
                   must_skills: Sequence[str], bonus_skills: Sequence[str],
                   weights: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按老插件口径重算四个数字（契约 D16：分数一律脚本算）。

    技能得分   = 命中必备技能数 / 岗位必备技能总数 × 100，四舍五入取整
    加分项得分 = 命中加分项数 / 岗位加分项总数 × 100，四舍五入取整；**岗位加分项为空记 100**
    匹配总分   = 技能得分 × weights.must + 加分项得分 × weights.bonus，四舍五入取整
    推荐状态   = 总分 ≥80 推荐 / 60~79 待定 / <60 不推荐
    """
    w = weights or {}
    try:
        wm = float(w.get("must", 0.7))
    except (TypeError, ValueError):
        wm = 0.7
    try:
        wb = float(w.get("bonus", 0.3))
    except (TypeError, ValueError):
        wb = 0.3

    st = len(must_skills or [])
    bt = len(bonus_skills or [])
    sh = len(skill_hits or [])
    bh = len(bonus_hits or [])
    notes: List[str] = []
    if st:
        skill_score = round_half_up(sh * 100.0 / st)
    else:
        skill_score = 0
        notes.append("岗位必备技能为空 → 技能得分分母缺失，记 0（岗位数据有问题，需人工确认）")
    if bt:
        bonus_score = round_half_up(bh * 100.0 / bt)
    else:
        bonus_score = 100
        notes.append("岗位加分项为空 → 加分项得分记 100（沿用已验证口径；"
                     "老插件 system-config §6 要求这种情况先与用户确认）")
    total = round_half_up(skill_score * wm + bonus_score * wb)
    recommend = "推荐" if total >= 80 else ("待定" if total >= 60 else "不推荐")
    return {"skill_total": st, "bonus_total": bt, "skill_hits": sh, "bonus_hits": bh,
            "skill_score": skill_score, "bonus_score": bonus_score,
            "weights": {"must": wm, "bonus": wb},
            "total_score": total, "recommend": recommend, "notes": notes}


# ---------------------------------------------------------------------------
# 归一化小工具
# ---------------------------------------------------------------------------
def norm_item(s: Any) -> str:
    """技能条目比对用归一化：去空白/全角空格/末尾标点、转小写。"""
    if s is None:
        return ""
    if isinstance(s, dict):
        s = s.get("name") or s.get("text") or s.get("value") or ""
    s = re.sub(r"[\s\u3000]+", "", str(s))
    s = s.strip("，,、;；.。:：")
    return s.lower()


def gate_verdict(v: Any) -> Optional[bool]:
    """把 gate_detail 的值归一成 True(达标)/False(不达标)/None(读不懂)。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = re.sub(r"[\s\u3000]+", "", str(v)).lower()
    if not s:
        return None
    for t in _FAIL_LIKE:
        if s.startswith(t.lower()):
            return False
    for t in _PASS_LIKE:
        if s.startswith(t.lower()):
            return True
    return None


def load_json(path: Path, label: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """读 JSON，容错 markdown 围栏（agent 有时会用 ```json 包起来）。"""
    if not path.exists():
        return None, {"code": "file_not_found", "key": label,
                      "detail": "%s 不存在：%s" % (label, path)}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, {"code": "file_unreadable", "key": label,
                      "detail": "%s 读不出来：%s: %s" % (label, type(exc).__name__, exc)}
    body = raw.strip()
    stripped = False
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
        stripped = True
    try:
        return json.loads(body), ({"code": "markdown_fence_stripped", "key": label,
                                   "detail": "%s 带 markdown 代码围栏，已剥掉后解析成功" % label}
                                  if stripped else None)
    except Exception as exc:
        return None, {"code": "bad_json", "key": label,
                      "detail": "%s 不是合法 JSON：%s: %s" % (label, type(exc).__name__, exc)}


def as_str_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [v]


_SQUASH_DROP = re.compile(r"[\s\u3000、，,;；.。:：/／\-—_()（）\[\]【】\"'“”‘’]+")


def squash(s: Any) -> str:
    """比对用最强归一化：去掉所有空白与中英文标点、转小写。

    为什么需要它（实测坑）：W-A 的 `must_skills` 切分粒度比 JD 原文细——
    「对应工序生产设备的结构、原理及运维规范」被按顿号切成 2 条，模型在 skill_hits 里
    很自然地把它**合回一条**引用。严格逐字 ⊆ 会把这种「合并引用」误判成编造，
    实测 8人×19岗 一批里 3/10 条 pass 因此被判无效（白白丢掉 3 条匹配记录）。
    所以先做**可解释的归一化映射**，映射不上才算编造。
    """
    return _SQUASH_DROP.sub("", str(s or "")).lower()


def map_hits_to_items(hits: Sequence[Any], items: Sequence[Any]) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """把模型给的命中项映射回岗位 must_skills/bonus_skills 的**原文条目**。

    三条映射规则（任一命中即认为不是编造）：
      a) 归一化后与某一条目完全相等；
      b) 归一化后等于**连续若干条**目的拼接（模型把被切碎的条目合回一句）；
      c) 归一化后与某一条目互为子串且长度占比 ≥0.6（模型做了缩写/同义改写）。
    映射不上 → 该项算编造（D16：越界即判该条无效并进 warnings）。

    返回 (映射后的原文条目列表, 归一化明细)。
    """
    items = list(items or [])
    sq_items = [squash(x) for x in items]
    exact: Dict[str, int] = {}
    for i, s in enumerate(sq_items):
        if s and s not in exact:
            exact[s] = i
    out: List[Any] = []
    picked = set()
    detail: List[Dict[str, Any]] = []
    for h in hits:
        sq = squash(h)
        if not sq:
            continue
        idxs: List[int] = []
        rule = None
        if sq in exact:                                   # a) 精确
            idxs, rule = [exact[sq]], "exact"
        else:
            for st in range(len(sq_items)):                # b) 连续拼接
                acc = ""
                cov = []
                for k in range(st, len(sq_items)):
                    acc += sq_items[k]
                    cov.append(k)
                    if acc == sq:
                        idxs, rule = cov, "join_consecutive"
                        break
                    if len(acc) > len(sq):
                        break
                if idxs:
                    break
            if not idxs:                                   # c) 子串（占比 ≥0.6）
                best, best_ratio = None, 0.0
                for i, s in enumerate(sq_items):
                    if not s:
                        continue
                    if sq in s or s in sq:
                        ratio = min(len(sq), len(s)) / float(max(len(sq), len(s)))
                        if ratio > best_ratio:
                            best, best_ratio = i, ratio
                if best is not None and best_ratio >= 0.6:
                    idxs, rule = [best], "substring(%.2f)" % best_ratio
        if not idxs:
            detail.append({"original": h, "mapped_to": None, "rule": "fabricated"})
            continue
        for i in idxs:
            if i not in picked:
                picked.add(i)
                out.append(items[i])
        if rule != "exact":
            detail.append({"original": h, "rule": rule,
                           "mapped_to": [items[i] for i in idxs]})
    return out, detail


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------
def expected_pairs(digest: Dict[str, Any],
                   overrides: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[str, str]],
                                                                 Dict[str, List[str]],
                                                                 List[str]]:
    """按「候选人组织 == 岗位组织 且 岗位在招」算出**必须覆盖**的组合集合。

    candidate_overrides[].org 会覆盖 org_guess（组织归一是 agent 在判定回合补的）。
    """
    org_of: Dict[str, Optional[str]] = {}
    for c in digest.get("candidates") or []:
        org_of[c.get("key")] = c.get("org_guess") or c.get("org") or None
    for o in overrides or []:
        if isinstance(o, dict) and o.get("candidate_key") in org_of and o.get("org"):
            org_of[o["candidate_key"]] = str(o["org"]).strip()
    jobs_by_org: Dict[str, List[str]] = {}
    open_keys: List[str] = []
    for j in digest.get("jobs") or []:
        status = (j.get("status") or "招聘中").strip()
        if status and status != "招聘中":
            continue
        open_keys.append(j.get("key"))
        jobs_by_org.setdefault(j.get("org") or "", []).append(j.get("key"))
    pairs: List[Tuple[str, str]] = []
    per_cand: Dict[str, List[str]] = {}
    no_job: List[str] = []
    for ckey in sorted(org_of.keys(), key=lambda x: str(x)):
        corg = org_of[ckey]
        jks = jobs_by_org.get(corg or "", [])
        if not corg:
            # 组织缺失：无法判断同组织，退化成「全部在招岗位」并让调用方记 warning
            jks = list(open_keys)
        per_cand[ckey] = list(jks)
        if not jks:
            no_job.append(ckey)
        for jk in jks:
            pairs.append((ckey, jk))
    return pairs, per_cand, no_job


def semantic_guardrails(digest: Dict[str, Any], decisions: Dict[str, Any],
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
    th = dict(SEM_GUARD_DEFAULTS)
    for k, v in (thresholds or {}).items():
        if v is not None and k in th:
            th[k] = v
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {"thresholds": th, "evaluated": True}

    audits = [a for a in (passed_audit or []) if isinstance(a, dict)]
    n_passed = len(audits)
    min_n = int(th["min_passed_for_guards"])
    if n_passed == 0:
        # 全批 0 通过本身就是最强的退化信号（门槛通过率异常，见 SEM-G1）
        if rejected_pairs_n > 0:
            warnings.append({"code": "sem_all_rejected", "key": "batch",
                             "detail": "整批 0 条通过、%d 个组合全部被拒（门槛通过率=0%%）。"
                                       "可能确实无人达标，也可能是 Turn 2 没做语义判定"
                                       "（W-F run3 事故形态之一）→ 必须人工抽查 rejected "
                                       "的 reason 与 evidence 后复核" % rejected_pairs_n})
        metrics.update(n_passed=0, rejected_pairs=rejected_pairs_n)
        metrics["evaluated"] = False
        return errors, warnings, metrics

    # ---------------- SEM-E：evidence 复用检测 ----------------
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
    metrics.update(evidence_unique_ratio=round(unique_ratio, 3),
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
            errors.append({"code": "sem_evidence_cross_candidate", "key": "passed",
                           "detail": detail + " → 占比过高，整批不写库，请重做 Turn 2 判定"})
        else:
            warnings.append({"code": "sem_evidence_cross_candidate", "key": "passed",
                             "detail": detail})
    # E2：evidence 去重率过低（同人同模板跨岗位复用的批级形态）
    if n_passed >= min_n and unique_ratio < float(th["evidence_unique_ratio_min"]):
        worst = sorted(ev_map.items(), key=lambda kv: -len(kv[1]))[:3]
        ws = "; ".join("「%s…」被 %s 复用" % (e[:24], ",".join("%s×%s" % p for p in pairs[:4]))
                       for e, pairs in worst)
        warnings.append({"code": "sem_evidence_reuse", "key": "passed",
                         "detail": "evidence 去重率 %.2f < 阈值 %.2f（%d 条 pass 只有 %d 段"
                                   "不同 evidence）→ 模板化复用嫌疑（W-F run3 实测 0.33；"
                                   "正常批 run1/run2 = 0.55/0.67）。最重复样本：%s"
                                   % (unique_ratio, th["evidence_unique_ratio_min"],
                                      n_passed, len(ev_map), ws)})

    # ---------------- SEM-H：命中项数量异常（过少 / 放水两侧） ----------------
    hits_denoms = [(a.get("candidate_key"), a.get("job_key"),
                    int((a.get("recomputed") or {}).get("skill_hits") or 0),
                    int((a.get("recomputed") or {}).get("skill_total") or 0)) for a in audits]
    low_max = int(th["low_skill_hits_max"])
    low_n = sum(1 for _, _, h, t in hits_denoms if h <= low_max)
    denoms_sorted = sorted(t for _, _, _, t in hits_denoms if t > 0)
    median_denom = denoms_sorted[len(denoms_sorted) // 2] if denoms_sorted else 0
    metrics.update(skill_hits_low_n=low_n, skill_hits_low_ratio=round(low_n / float(n_passed), 3),
                   must_denominator_median=median_denom)
    if n_passed >= min_n and median_denom > int(th["low_skill_hits_min_denominator"]) \
            and low_n / float(n_passed) >= float(th["low_skill_hits_ratio"]):
        examples = ["%s×%s=%d/%d" % (c, j, h, t) for c, j, h, t in hits_denoms[:6]]
        warnings.append({"code": "sem_skill_hits_too_few", "key": "passed",
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
    metrics.update(full_hit_n=fh_n, full_hit_pool=len(fh_pool),
                   full_hit_ratio=round(fh_n / float(len(fh_pool)), 3) if fh_pool else None)
    if n_passed >= min_n and fh_pool and fh_n / float(len(fh_pool)) >= float(th["full_hit_ratio"]):
        examples = ["%s×%s=%d/%d" % (c, j, h, t) for c, j, h, t in fh_pool[:6]]
        warnings.append({"code": "sem_skill_hits_full_inflated", "key": "passed",
                         "detail": "分母 ≥%d 的 %d 条里 %d 条（%.0f%%，阈值 %.0f%%）100%% 全命中"
                                   " → 放水嫌疑（W-F 实测普晓刚 15/15 全命中但原文只支撑 11~13"
                                   " 项）。样本：%s"
                                   % (fh_min, len(fh_pool), fh_n, fh_n * 100.0 / len(fh_pool),
                                      th["full_hit_ratio"] * 100, "; ".join(examples))})

    # ---------------- SEM-R：推荐率 / 推荐分布异常 ----------------
    rec_dist: Dict[str, int] = {}
    for a in audits:
        r = (a.get("recomputed") or {}).get("recommend") or "?"
        rec_dist[r] = rec_dist.get(r, 0) + 1
    n_recommend = rec_dist.get("推荐", 0)
    recommend_ratio = n_recommend / float(n_passed)
    metrics.update(recommend_distribution=rec_dist,
                   recommend_ratio=round(recommend_ratio, 3))
    if n_passed >= min_n and n_recommend == 0:
        warnings.append({"code": "sem_zero_recommend", "key": "passed",
                         "detail": "整批 %d 条 pass 里「推荐」=0 条（分布=%s）→ 异常"
                                   "（W-F run3 事故：0 推荐，run1/run2=22 条）；请复核"
                                   "门槛与命中判定是否过严/偷懒，确需 0 推荐要向用户说明理由"
                                   % (n_passed, json.dumps(rec_dist, ensure_ascii=False))})
    elif n_passed >= min_n and recommend_ratio > float(th["recommend_ratio_high"]):
        warnings.append({"code": "sem_recommend_ratio_high", "key": "passed",
                         "detail": "推荐率 %.0f%%（%d/%d）> 阈值 %.0f%%（分布=%s）→ 异常偏高，"
                                   "疑似放水；请抽查高分条目的 evidence 是否支撑"
                                   % (recommend_ratio * 100, n_recommend, n_passed,
                                      th["recommend_ratio_high"] * 100,
                                      json.dumps(rec_dist, ensure_ascii=False))})
    if n_passed >= min_n and len([k for k in rec_dist if k != "?"]) == 1:
        only = [k for k in rec_dist if k != "?"][0]
        warnings.append({"code": "sem_recommend_collapsed", "key": "passed",
                         "detail": "整批 %d 条 pass 的推荐状态**全部**是「%s」（分布塌缩到单一"
                                   "取值）→ 语义判定退化嫌疑（正常批 run1/run2 三档齐有："
                                   "推荐22/待定26/不推荐19）；请人工复核后再写库"
                                   % (n_passed, only)})

    # ---------------- SEM-G：门槛通过率异常（全拒 / 全过） ----------------
    total_pairs = n_passed + rejected_pairs_n
    metrics.update(rejected_pairs=rejected_pairs_n,
                   gate_pass_ratio=round(n_passed / float(total_pairs), 3) if total_pairs else None)
    if total_pairs >= min_n:
        if rejected_pairs_n == 0:
            warnings.append({"code": "sem_all_passed", "key": "batch",
                             "detail": "整批 %d 个组合**全部通过**硬性门槛（0 拒绝）→ 异常"
                                       "（真实批次门槛通过率实测 ~12%%）；疑似门槛没判/"
                                       "全放行，请复核 gate_detail" % total_pairs})
        # 全拒（passed=0）已在 n_passed==0 分支处理

    # ---------------- SEM-O：candidate_overrides 覆盖率异常（D13 复核没做） ----------------
    need_years = [str(c.get("key")) for c in (digest.get("candidates") or [])
                  if isinstance(c, dict) and "years" in (c.get("needs_review") or [])]
    ovr = [o for o in as_str_list(decisions.get("candidate_overrides")) if isinstance(o, dict)]
    reviewed = {str(o.get("candidate_key")) for o in ovr if o.get("years_experience") is not None}
    covered = [k for k in need_years if k in reviewed]
    metrics.update(needs_review_years=len(need_years), years_reviewed=len(covered))
    if need_years and not covered:
        warnings.append({"code": "sem_years_review_missing", "key": "candidate_overrides",
                         "detail": "digest 标记 needs_review:[\"years\"] 的候选人有 %d 个（%s），"
                                   "但 candidate_overrides 里**一个** years_experience 复核值"
                                   "都没有 → D13 复核没做（估算年限直接喂一票否决门槛，"
                                   "W-F 实测 9/9 标记、agent 执行率不满：P0 8/9、run1 4/9）；"
                                   "必须补做复核或向用户说明"
                                   % (len(need_years), ",".join(need_years[:10]))})

    # ---------------- SEM-P（P5）：身份/组织安全阀的复核没有落点 ----------------
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
    for o in ovr:
        if o.get("candidate_key") is not None:
            ovr_by_ck.setdefault(str(o.get("candidate_key")), o)
    text_by_ck: Dict[str, str] = {}
    for p in decisions.get("passed") or []:
        if isinstance(p, dict) and p.get("candidate_key") is not None:
            k = str(p.get("candidate_key"))
            text_by_ck[k] = "%s %s" % (text_by_ck.get(k, ""), str(p.get("evidence") or ""))
    for rj in decisions.get("rejected") or []:
        if isinstance(rj, dict) and rj.get("candidate_key") is not None:
            k = str(rj.get("candidate_key"))
            text_by_ck[k] = "%s %s" % (text_by_ck.get(k, ""), str(rj.get("reason") or ""))
    for fld, kws in identity_review:
        need = [str(c.get("key")) for c in (digest.get("candidates") or [])
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
        metrics.update(**{"needs_review_%s" % fld: len(need),
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
            warnings.append({"code": "sem_%s_review_missing" % fld,
                             "key": "candidate_overrides",
                             "detail": "digest 标记 needs_review 含 \"%s\" 的候选人有 %d 个"
                                       "（%s%s），但 %s"
                                       % (fld, len(need), ",".join(missing[:10]),
                                          " 等" if len(missing) > 10 else "", act)})
    return errors, warnings, metrics


def verify(digest: Any, decisions: Any, check_coverage: bool = True,
           sem_thresholds: Optional[Dict[str, float]] = None,
           semantic_guards: bool = True) -> Dict[str, Any]:
    """核心校验函数（apply_decisions.py 直接 import 它）。

    `check_coverage=False` 用于「没传 --digest、岗位信息是从表里现查的」降级场景：
    那时无法知道**真实**的期望组合集合（组织归属可能不全），所以只跳过覆盖率判定，
    引用/集合/算术/evidence/结构 五项照查，并记一条 `coverage_not_checked` warning。

    `sem_thresholds` / `semantic_guards`（缺陷2 新增，缺省启用、阈值见
    SEM_GUARD_DEFAULTS）：语义合理性护栏开关与阈值覆盖；不传 = 默认阈值全开，
    既有调用方（apply_decisions）零改动兼容。

    返回结构化结论 dict；`ok` 为 False 时调用方**不得写库**。
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def err(code: str, key: Any, detail: str) -> None:
        errors.append({"code": code, "key": key, "detail": detail})

    def warn(code: str, key: Any, detail: str) -> None:
        warnings.append({"code": code, "key": key, "detail": detail})

    if not isinstance(digest, dict):
        err("digest_not_object", "digest", "digest 不是 JSON 对象，实得 %s" % type(digest).__name__)
        return _result(False, errors, warnings, {}, {}, {})
    if not isinstance(decisions, dict):
        err("decisions_not_object", "decisions",
            "decisions 不是 JSON 对象（契约 §3.3 要求 {batch_id,passed,rejected,...}），实得 %s"
            % type(decisions).__name__)
        return _result(False, errors, warnings, {}, {}, {})

    cands = digest.get("candidates")
    jobs = digest.get("jobs")
    if not isinstance(cands, list):
        err("digest_missing_candidates", "digest.candidates", "digest 缺 candidates 数组")
        cands = []
    if not isinstance(jobs, list):
        err("digest_missing_jobs", "digest.jobs", "digest 缺 jobs 数组")
        jobs = []

    cand_by_key: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        if isinstance(c, dict) and c.get("key") is not None:
            cand_by_key[str(c["key"])] = c
    job_by_key: Dict[str, Dict[str, Any]] = {}
    for j in jobs:
        if isinstance(j, dict) and j.get("key") is not None:
            job_by_key[str(j["key"])] = j

    overrides = [o for o in as_str_list(decisions.get("candidate_overrides")) if isinstance(o, dict)]
    passed = [p for p in as_str_list(decisions.get("passed")) ]
    rejected = [r for r in as_str_list(decisions.get("rejected"))]
    if decisions.get("passed") is not None and not isinstance(decisions.get("passed"), list):
        err("passed_not_list", "passed", "passed 必须是数组，实得 %s" % type(decisions.get("passed")).__name__)
    if decisions.get("rejected") is not None and not isinstance(decisions.get("rejected"), list):
        err("rejected_not_list", "rejected", "rejected 必须是数组，实得 %s" % type(decisions.get("rejected")).__name__)
    if "passed" not in decisions and "rejected" not in decisions:
        err("decisions_empty", "decisions", "decisions 里既没有 passed 也没有 rejected，等于没判定")

    dbid = decisions.get("batch_id")
    if dbid and digest.get("batch_id") and dbid != digest.get("batch_id"):
        warn("batch_id_mismatch", "batch_id",
             "decisions.batch_id=%r 与 digest.batch_id=%r 不一致（可能拿错了批次的判定结果）"
             % (dbid, digest.get("batch_id")))

    # ---------------- 2. 引用合法性 + 结构完整性 ----------------
    seen: Dict[Tuple[str, str], List[str]] = {}
    passed_audit: List[Dict[str, Any]] = []
    valid_passed: List[Dict[str, Any]] = []

    for i, p in enumerate(passed):
        pk = "passed[%d]" % i
        if not isinstance(p, dict):
            err("entry_not_object", pk, "passed[%d] 不是对象，实得 %s" % (i, type(p).__name__))
            continue
        ck = p.get("candidate_key")
        jk = p.get("job_key")
        for f in ("candidate_key", "job_key"):
            if not p.get(f):
                err("missing_field", "%s.%s" % (pk, f), "passed[%d] 缺 %s" % (i, f))
        if ck is None or jk is None:
            continue
        ck, jk = str(ck), str(jk)
        pair = (ck, jk)
        seen.setdefault(pair, []).append("passed[%d]" % i)
        if ck not in cand_by_key:
            err("unknown_candidate_key", "%s|%s" % pair,
                "passed[%d] 的 candidate_key=%r 不在 digest 里（digest 有 %d 个候选人：%s）"
                % (i, ck, len(cand_by_key), ",".join(sorted(cand_by_key)[:12])))
        if jk not in job_by_key:
            err("unknown_job_key", "%s|%s" % pair,
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
                    err("missing_field", "%s|%s" % pair,
                        "passed[%d] 缺 %s（稀疏格式也必须给，缺了就没法算分）" % (i, f))
                else:
                    warn("missing_field", "%s|%s" % pair, "passed[%d] 缺 %s" % (i, f))
        if "gate_detail" not in p:
            err("missing_field", "%s|%s" % pair, "passed[%d] 缺 gate_detail（四项门槛判定）" % i)

        # gate_detail：在 passed 里就必须四项全达标
        gd = p.get("gate_detail")
        if isinstance(gd, dict):
            for g in GATE_ITEMS:
                if g not in gd:
                    warn("gate_detail_incomplete", "%s|%s" % pair,
                         "gate_detail 缺 %r 项（应含 education/major/years/certificates 四项）" % g)
            bad_gates = []
            for g, v in gd.items():
                verdict = gate_verdict(v)
                if verdict is False:
                    bad_gates.append("%s=%s" % (g, v))
            if bad_gates:
                err("gate_detail_inconsistent", "%s|%s" % pair,
                    "该组合出现在 passed 里，但 gate_detail 有不达标项：%s"
                    "（硬门槛一票否决，不达标必须进 rejected）" % "; ".join(bad_gates))
        elif gd is not None:
            err("gate_detail_not_object", "%s|%s" % pair,
                "gate_detail 必须是对象，实得 %s" % type(gd).__name__)

        # 3. 集合校验（防模型编造命中项，契约 D16）
        #    先做**可解释的归一化映射**（模型常把 W-A 切碎的条目合回一句引用），
        #    映射不上的才算编造 → 该条无效 + warnings（D16 原文：越界即判该条无效并进 warnings）
        skill_hits_raw = as_str_list(p.get("skill_hits"))
        bonus_hits_raw = as_str_list(p.get("bonus_hits"))
        skill_eff, skill_map = map_hits_to_items(skill_hits_raw, must)
        bonus_eff, bonus_map = map_hits_to_items(bonus_hits_raw, bonus)
        bad_skill = [d for d in skill_map if d["rule"] == "fabricated"]
        bad_bonus = [d for d in bonus_map if d["rule"] == "fabricated"]
        fixed_skill = [d for d in skill_map if d["rule"] != "fabricated"]
        fixed_bonus = [d for d in bonus_map if d["rule"] != "fabricated"]
        if bad_skill:
            warn("fabricated_skill_hit", "%s|%s" % pair,
                 "skill_hits 有 %d 项无法映射到岗位 must_skills（判为编造，**该条 pass 无效**、"
                 "不会建匹配记录）：%s ｜岗位必备技能=%s"
                 % (len(bad_skill), json.dumps([d["original"] for d in bad_skill][:6],
                                               ensure_ascii=False),
                    json.dumps(list(must)[:8], ensure_ascii=False)))
        if bad_bonus:
            warn("fabricated_bonus_hit", "%s|%s" % pair,
                 "bonus_hits 有 %d 项无法映射到岗位 bonus_skills（判为编造，**该条 pass 无效**）：%s"
                 " ｜岗位加分项=%s"
                 % (len(bad_bonus), json.dumps([d["original"] for d in bad_bonus][:6],
                                               ensure_ascii=False),
                    json.dumps(list(bonus)[:8], ensure_ascii=False)))
        if fixed_skill or fixed_bonus:
            warn("skill_hit_normalized", "%s|%s" % pair,
                 "命中项已归一化映射回岗位原文条目（模型把被切碎的条目合回一句/做了缩写）："
                 "%s" % json.dumps((fixed_skill + fixed_bonus)[:4], ensure_ascii=False)[:400])
        # 同一集合内重复命中会虚增分子（map_hits_to_items 已按岗位条目去重）
        raw_skill = len([h for h in skill_hits_raw if squash(h)]) - len(bad_skill)
        raw_bonus = len([h for h in bonus_hits_raw if squash(h)]) - len(bad_bonus)
        dup_s = max(0, raw_skill - len(skill_eff))
        dup_b = max(0, raw_bonus - len(bonus_eff))
        if dup_s or dup_b:
            warn("duplicate_hits", "%s|%s" % pair,
                 "命中项有重复（技能 %d 项 / 加分 %d 项映射到同一条岗位条目），已按去重后计数"
                 % (dup_s, dup_b))
        skill_u = dedupe_norm(skill_eff)
        bonus_u = dedupe_norm(bonus_eff)

        # evidence
        ev = p.get("evidence")
        ev_s = "" if ev is None else str(ev)
        if not ev_s.strip():
            err("evidence_empty", "%s|%s" % pair, "passed[%d] 的 evidence 为空（必须给原文引用）" % i)
        elif len(ev_s) > EVIDENCE_MAX_LEN:
            err("evidence_too_long", "%s|%s" % pair,
                "evidence %d 字 > 上限 %d 字（契约 §3.3）：%s…"
                % (len(ev_s), EVIDENCE_MAX_LEN, ev_s[:40]))

        rec = p.get("recommend")
        if rec is not None and str(rec).strip() not in RECOMMEND_VALUES:
            err("recommend_invalid_value", "%s|%s" % pair,
                "recommend=%r 非法，只能是 %s" % (rec, "/".join(RECOMMEND_VALUES)))

        # 4. 算术复核（D16：脚本算，模型输出只作对照）
        got = compute_scores(skill_u, bonus_u, must, bonus, job.get("weights"))
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
            warn("score_mismatch", "%s|%s" % pair,
                 "%s × %s：%s ｜以脚本重算为准（契约 D16）"
                 % (audit["candidate_name"], audit["job_name"], "; ".join(audit["mismatch"])))
        for n in got["notes"]:
            warn("score_denominator", "%s|%s" % pair, "%s × %s：%s"
                 % (audit["candidate_name"], audit["job_name"], n))
        passed_audit.append(audit)
        if audit["valid"]:
            valid_passed.append(audit)

    rejected_pairs: List[Tuple[str, str]] = []
    rejected_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for i, r in enumerate(rejected):
        rk = "rejected[%d]" % i
        if not isinstance(r, dict):
            err("entry_not_object", rk, "rejected[%d] 不是对象，实得 %s" % (i, type(r).__name__))
            continue
        ck = r.get("candidate_key")
        if not ck:
            err("missing_field", "%s.candidate_key" % rk, "rejected[%d] 缺 candidate_key" % i)
            continue
        ck = str(ck)
        if ck not in cand_by_key:
            err("unknown_candidate_key", ck,
                "rejected[%d] 的 candidate_key=%r 不在 digest 里" % (i, ck))
        jks = r.get("job_keys")
        if jks is None:
            # 容错：有人写成 job_key 单数
            if r.get("job_key"):
                jks = [r.get("job_key")]
            else:
                err("missing_field", "%s.job_keys" % rk, "rejected[%d] 缺 job_keys" % i)
                continue
        if not isinstance(jks, list):
            err("job_keys_not_list", ck, "rejected[%d] 的 job_keys 必须是数组，实得 %s"
                % (i, type(jks).__name__))
            continue
        if not jks:
            warn("rejected_empty_job_keys", ck, "rejected[%d] 的 job_keys 是空数组（这条没有意义）" % i)
        reason = r.get("reason")
        if reason is None or not str(reason).strip():
            warn("rejected_reason_missing", ck,
                 "rejected[%d] 缺 reason（用户清单要标注「未过门槛原因」）" % i)
        for jk in jks:
            jk = str(jk)
            pair = (ck, jk)
            seen.setdefault(pair, []).append("rejected[%d]" % i)
            rejected_pairs.append(pair)
            rejected_index[pair] = {"candidate_key": ck, "job_key": jk,
                                    "candidate_name": cand_by_key.get(ck, {}).get("name"),
                                    "job_name": job_by_key.get(jk, {}).get("job_name"),
                                    "job_id": job_by_key.get(jk, {}).get("job_id"),
                                    "reason": (str(reason).strip() if reason else None)}
            if jk not in job_by_key:
                err("unknown_job_key", "%s|%s" % pair,
                    "rejected[%d] 的 job_key=%r 不在 digest 里" % (i, jk))

    # ---------------- 1. 覆盖率 ----------------
    exp_pairs, per_cand, no_job = expected_pairs(digest, overrides)
    exp_set = set(exp_pairs)
    missing = sorted(exp_set - set(seen.keys()))
    duplicates = sorted([p for p, srcs in seen.items() if len(srcs) > 1])
    unexpected = sorted(set(seen.keys()) - exp_set)
    if not check_coverage:
        warn("coverage_not_checked", "coverage",
             "本次没做覆盖率校验（调用方传了 check_coverage=False，通常是没给 --digest、"
             "岗位信息现查于表）；缺失/重复组合不会被发现，强烈建议传 --digest")
        missing, unexpected = [], []
    for p in missing:
        err("missing_pair", "%s|%s" % p,
            "组合 %s × %s 既不在 passed 也不在 rejected（覆盖率缺失）；岗位=%s 候选人=%s"
            % (p[0], p[1], job_by_key.get(p[1], {}).get("job_name"),
               cand_by_key.get(p[0], {}).get("name")))
    for p in duplicates:
        err("duplicate_pair", "%s|%s" % p,
            "组合 %s × %s 出现了 %d 次（%s），必须且只能出现一次"
            % (p[0], p[1], len(seen[p]), ", ".join(seen[p])))
    for p in unexpected:
        warn("unexpected_pair", "%s|%s" % p,
             "组合 %s × %s 不是「同组织在招岗位」组合（跨组织或岗位不在招），"
             "本插件不会为它建匹配记录" % (p[0], p[1]))
    for ck in no_job:
        warn("candidate_no_open_job", ck,
             "候选人 %s 在本 digest 里没有同组织的在招岗位 → 期望覆盖 0 个组合"
             % cand_by_key.get(ck, {}).get("name"))

    # 编造命中项本身按 D16 只作 warning + 该条无效；但**比例过高**说明模型整体在瞎写
    # （前序实验：20 人批量会语义崩塌、放弃推理改写关键词脚本），这时必须拦住不让写库。
    n_invalid = len([a for a in passed_audit if not a["valid"]])
    if passed_audit and n_invalid * 1.0 / len(passed_audit) > INVALID_PASSED_RATIO_LIMIT:
        err("too_many_invalid_passed", "passed",
            "有 %d/%d 条 pass 的命中项判为编造（>%.0f%%）→ 模型输出整体不可信，"
            "不写库；请缩小分片（契约 D3）或重跑本批判定"
            % (n_invalid, len(passed_audit), INVALID_PASSED_RATIO_LIMIT * 100))

    # ---------------- 6. 语义合理性护栏（缺陷2，2026-09-17） ----------------
    # 形式校验全过 ≠ 语义判定认真做了（W-F run3：规则脚本代跑 → 0 推荐但 verify PASS）。
    # 护栏告警**必须**如实转述给用户并说明可能需要重做 Turn 2，禁止静默吞掉继续写库。
    sem_metrics: Dict[str, Any] = {"evaluated": False}
    if semantic_guards:
        sem_errs, sem_warns, sem_metrics = semantic_guardrails(
            digest, decisions, passed_audit, len(rejected_pairs), sem_thresholds)
        for e in sem_errs:
            errors.append(e)
        for w in sem_warns:
            warnings.append(w)

    counts = {
        "candidates": len(cand_by_key),
        "jobs": len(job_by_key),
        "expected_pairs": len(exp_set),
        "passed_entries": len(passed),
        "rejected_entries": len(rejected),
        "rejected_pairs": len(rejected_pairs),
        "covered_pairs": len(set(seen.keys()) & exp_set),
        "missing_pairs": len(missing),
        "duplicate_pairs": len(duplicates),
        "unexpected_pairs": len(unexpected),
        "valid_passed_entries": len(valid_passed),
        "invalid_passed_entries": len(passed) - len(valid_passed),
    }
    coverage = {"missing": ["%s|%s" % p for p in missing],
                "duplicate": ["%s|%s" % p for p in duplicates],
                "unexpected": ["%s|%s" % p for p in unexpected],
                "expected_per_candidate": {k: len(v) for k, v in per_cand.items()}}
    recomputed = {"%s|%s" % (a["candidate_key"], a["job_key"]): a["recomputed"]
                  for a in passed_audit}
    summary = {
        "score_mismatch": len([a for a in passed_audit if a["mismatch"]]),
        "fabricated_skill_hits": len([w for w in warnings if w["code"] == "fabricated_skill_hit"]),
        "fabricated_bonus_hits": len([w for w in warnings if w["code"] == "fabricated_bonus_hit"]),
        "hits_normalized": len([w for w in warnings if w["code"] == "skill_hit_normalized"]),
        "invalid_passed_entries": len([a for a in passed_audit if not a["valid"]]),
        "evidence_problems": len([e for e in errors if e["code"] in ("evidence_empty",
                                                                     "evidence_too_long")]),
        "recommend_distribution": _dist(a["recomputed"]["recommend"] for a in valid_passed),
        "denominator_warnings": len([w for w in warnings if w["code"] == "score_denominator"]),
        # 语义护栏（缺陷2；只增不删）：触发的护栏代码 + 实测指标，供 agent/报告直接引用
        "semantic_guards": {
            "triggered_errors": sorted({e["code"] for e in errors if e["code"].startswith("sem_")}),
            "triggered_warnings": sorted({w["code"] for w in warnings
                                          if w["code"].startswith("sem_")}),
            "metrics": sem_metrics,
        },
    }
    ok = not errors
    return _result(ok, errors, warnings, counts, coverage, summary,
                   passed_audit=passed_audit, rejected_index=rejected_index,
                   recomputed=recomputed, digest=digest, decisions=decisions)


def dedupe_norm(items: Sequence[Any]) -> List[str]:
    """按归一化去重，保留原词（分子不能靠重复命中虚增）。"""
    seen, out = set(), []
    for it in items:
        k = norm_item(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it if isinstance(it, str) else str(it))
    return out


def _dist(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _result(ok: bool, errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]],
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
        "python": "%d.%d.%d" % sys.version_info[:3],
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _slim_for_stdout(res: Dict[str, Any]) -> Dict[str, Any]:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="纯本地校验 decisions.json（零 dws 调用）")
    ap.add_argument("--digest", required=True, help="digest.json（或某个 digest_batch_NN.json）绝对路径")
    ap.add_argument("--decisions", required=True, help="decisions.json 绝对路径（agent 产出）")
    ap.add_argument("--quiet", action="store_true", help="只打结论摘要，不打逐条 score_audit")
    # ---- 语义护栏阈值覆盖（缺陷2；缺省值与标定依据见 SEM_GUARD_DEFAULTS 注释）----
    ap.add_argument("--no-semantic-guards", action="store_true",
                    help="关闭语义合理性护栏（仅调试用；正常流程禁止关闭——W-F run3 事故"
                         "证明形式校验防不了语义偷懒）")
    ap.add_argument("--sem-evidence-unique-min", type=float, default=None,
                    help="evidence 去重率下限（缺省 %.2f）" % SEM_GUARD_DEFAULTS["evidence_unique_ratio_min"])
    ap.add_argument("--sem-low-hits-ratio", type=float, default=None,
                    help="skill_hits≤%d 条目占比告警阈值（缺省 %.2f）"
                         % (SEM_GUARD_DEFAULTS["low_skill_hits_max"],
                            SEM_GUARD_DEFAULTS["low_skill_hits_ratio"]))
    ap.add_argument("--sem-full-hit-ratio", type=float, default=None,
                    # 注意双写 %%：先经本行的 %% 格式化，再由 argparse 帮助展开一次
                    # （python 3.14 的 argparse 会在 add_argument 时就校验，单个 %% 会炸）
                    help="100%%%% 全命中（放水）占比告警阈值（缺省 %.2f）"
                         % SEM_GUARD_DEFAULTS["full_hit_ratio"])
    ap.add_argument("--sem-recommend-high", type=float, default=None,
                    help="推荐率异常高告警阈值（缺省 %.2f）" % SEM_GUARD_DEFAULTS["recommend_ratio_high"])
    args = ap.parse_args(list(argv) if argv is not None else None)

    digest, e1 = load_json(Path(args.digest).expanduser(), "digest")
    decisions, e2 = load_json(Path(args.decisions).expanduser(), "decisions")
    if e1 or e2:
        res = _result(False, [x for x in (e1, e2) if x and x["code"] != "markdown_fence_stripped"],
                      [x for x in (e1, e2) if x and x["code"] == "markdown_fence_stripped"],
                      {}, {}, {})
        print(json.dumps(_slim_for_stdout(res), ensure_ascii=False, indent=1))
        return 1
    sem_th = {
        "evidence_unique_ratio_min": args.sem_evidence_unique_min,
        "low_skill_hits_ratio": args.sem_low_hits_ratio,
        "full_hit_ratio": args.sem_full_hit_ratio,
        "recommend_ratio_high": args.sem_recommend_high,
    }
    res = verify(digest, decisions, sem_thresholds=sem_th,
                 semantic_guards=not args.no_semantic_guards)
    out = _slim_for_stdout(res)
    if args.quiet:
        out.pop("score_audit", None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sem = (res.get("summary") or {}).get("semantic_guards") or {}
    sys.stderr.write("VERIFY %s：errors=%d warnings=%d 覆盖 %s/%s 语义护栏=%s\n"
                     % ("PASS" if res["ok"] else "FAIL", len(res["errors"]),
                        len(res["warnings"]), res["counts"].get("covered_pairs"),
                        res["counts"].get("expected_pairs"),
                        (sem.get("triggered_warnings") or []) + (sem.get("triggered_errors") or [])
                        or "未触发"))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
