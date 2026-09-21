#!/usr/bin/env python3
"""智能匹配：简历 × 岗位 确定性打分 → 写「智能匹配」表 → 刷新岗位统计。

用法:
    python3 scripts/match.py [--job-id Jxxx ...] [--min-score N] [--dry-run]

规则（与 skills/recruit-model/references/ai-analysis-spec.md 一致）：
    skill_score = round(100 * must_weight * 必备命中率)
    bonus_score = round(100 * bonus_weight * 加分命中率)   # 无加分项时为 0
    total = skill_score + bonus_score
    recommend: total>=70 推荐 / >=40 待定 / 其余不推荐；total>0 的配对才落库
幂等：按 job_id 先删旧匹配再写新匹配；重跑结果稳定。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from notable import Notable, NotableError  # noqa: E402
from preflight import run_preflight  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")

THRESHOLDS = ((70, "推荐"), (40, "待定"))


def tokens(text):
    if isinstance(text, list):
        parts = text
    else:
        parts = (text or "").replace("、", ",").replace("，", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def _hit(cand, need):
    a, b = cand.lower(), need.lower()
    return a == b or a in b or b in a


def score(cand_skills, must, bonus, mw, bw):
    """返回 (skill_score, bonus_score, total, 命中必备list, 命中加分list)。"""
    hit_m = [n for n in must if any(_hit(c, n) for c in cand_skills)]
    hit_b = [n for n in bonus if any(_hit(c, n) for c in cand_skills)]
    s = int(round(100 * mw * (len(hit_m) / len(must)))) if must else 0
    b = int(round(100 * bw * (len(hit_b) / len(bonus)))) if bonus else 0
    return s, b, s + b, hit_m, hit_b


def recommend_of(total):
    for bar, label in THRESHOLDS:
        if total >= bar:
            return label
    return "不推荐"


def main():
    ap = argparse.ArgumentParser(description="简历×岗位 确定性匹配打分")
    ap.add_argument("--job-id", action="append", default=[], help="只跑指定岗位（可重复）")
    ap.add_argument("--min-score", type=int, default=1, help="落库最低总分（默认 1）")
    ap.add_argument("--dry-run", action="store_true", help="只打分不写表")
    args = ap.parse_args()

    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG)

    nt = Notable()
    jobs = [r for r in nt.list_records("job")
            if not args.job_id or r["fields"].get("job_id") in args.job_id]
    resumes = nt.list_records("resume", biz_fields=[
        "name", "phone", "skills", "expected_position", "years_experience"])

    rows_by_job, report = {}, {"jobs": len(jobs), "written": 0, "recommend": {}, "failed": []}
    for job in jobs:
        jf = job["fields"]
        must, bonus = tokens(jf.get("must_skills")), tokens(jf.get("bonus_skills"))
        mw = float(jf.get("must_weight") or 0.7)
        bw = float(jf.get("bonus_weight") or 0.3)
        rows = []
        for res in resumes:
            cf = res["fields"]
            cand = tokens(cf.get("skills"))
            if not cand:
                continue
            s, b, total, hm, hb = score(cand, must, bonus, mw, bw)
            if total < args.min_score:
                continue
            ev = "必备命中%d/%d%s；加分命中%d/%d%s" % (
                len(hm), len(must), ("(" + "、".join(hm) + ")") if hm else "",
                len(hb), len(bonus), ("(" + "、".join(hb) + ")") if hb else "")
            rows.append({
                "name": cf.get("name"), "phone": cf.get("phone"),
                "job_name": jf.get("job_name"), "job_id": jf.get("job_id"),
                "org": jf.get("org"), "source": "系统匹配",
                "cand_skills": "、".join(cand),
                "must_skills": jf.get("must_skills"), "bonus_skills": jf.get("bonus_skills"),
                "hard_gates": jf.get("hard_gates"),
                "expected_position": cf.get("expected_position"),
                "years_experience": str(cf.get("years_experience") or ""),
                "skill_score": s, "bonus_score": b, "total_score": total,
                "recommend": recommend_of(total),
                "update_time": int(time.time() * 1000), "evidence": ev,
            })
        rows_by_job[job["id"]] = rows
        for r in rows:
            report["recommend"][r["recommend"]] = report["recommend"].get(r["recommend"], 0) + 1
        report["written"] += len(rows)

    if args.dry_run:
        report["rows"] = [r for rows in rows_by_job.values() for r in rows]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    try:
        for job in jobs:  # 按岗位先删旧匹配再写，保证幂等
            jid = job["fields"].get("job_id")
            old = [r["id"] for r in nt.list_records("match", flt={"job_id": jid})]
            if old:
                nt.delete_records("match", old)
            rows = rows_by_job[job["id"]]
            if rows:
                nt.create_records("match", rows)
            counts = {"推荐": 0, "待定": 0, "不推荐": 0}
            for r in rows:
                counts[r["recommend"]] += 1
            nt.update_records("job", [{"id": job["id"], "stat_total": len(rows),
                                       "stat_recommend": counts["推荐"],
                                       "stat_pending": counts["待定"],
                                       "stat_reject": counts["不推荐"]}])
    except NotableError as e:
        print(json.dumps({**report, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

    for job in jobs:  # 回读：每个岗位的匹配数与写入数一致
        jid = job["fields"].get("job_id")
        back = len(nt.list_records("match", flt={"job_id": jid}))
        if back != len(rows_by_job[job["id"]]):
            report["failed"].append({"job_id": jid, "expect": len(rows_by_job[job["id"]]), "got": back})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(1 if report["failed"] else 0)


if __name__ == "__main__":
    main()
