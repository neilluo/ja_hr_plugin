# -*- coding: utf-8 -*-
"""match_analyze.py — 智能匹配的逐岗精析（subagent 并发，一岗一批，上限7）

    python3 skills/match-verify/scripts/match_gated.py                     # 0 机械门槛判定 → gate_pairs.json
    python3 skills/match-verify/scripts/match_analyze.py prepare           # 1 按岗位切批（每岗一批）
    python3 skills/match-verify/scripts/match_analyze.py merge             # 2 合并子任务判定
    python3 skills/match-verify/scripts/match_analyze.py apply             # 3 只建 keep=true 的配对
    python3 skills/match-verify/scripts/match_analyze.py link              # 4 单独读取后连接「关联岗位」
    python3 skills/match-verify/scripts/match_analyze.py stats             # 5 单独读取后刷新岗位四项统计

子任务负责：判「专业/工序是否实质对口」+ 给部分覆盖计分 + 产出推荐状态/匹配依据/AI匹配分析。
提示词：skills/match-verify/references/match-subagent-prompt.md
"""
import sys, os, re, json, time, collections

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from waves import plan, MAX_AGENTS  # noqa: E402  agent数封顶8，超限自动加大每 agent 岗位数
from semantic_score import hit, toks  # noqa: E402

OUTDIR = os.path.join(ROOT, "outputs")
DEFAULT_BATCH = 2        # 每个 agent 负责几个岗位（13岗→7个agent，落在4-8最优区间）
PAIRS = os.path.join(OUTDIR, "gate_pairs.json")
LEGACY_PAIRS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_pairs.json")
FINAL = os.path.join(OUTDIR, "match_final.json")


def txt(v):
    return v.get("markdown") if isinstance(v, dict) else ("" if v is None else str(v))


def _pairs_file():
    return PAIRS if os.path.exists(PAIRS) else LEGACY_PAIRS


def prepare(nt, args=None):
    args = args or []
    os.makedirs(OUTDIR, exist_ok=True)
    pairs = json.load(open(_pairs_file(), encoding="utf-8"))
    jobs = {j["fields"].get("job_id"): j for j in nt.list_records(
        "job", biz_fields=["job_id", "job_name", "department", "org", "hard_gates",
                           "must_skills", "bonus_skills", "must_weight", "bonus_weight"])}
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "major", "skills",
                                                   "expected_position", "org"]):
        f = r["fields"]
        cands[f.get("name")] = {"id": r["id"], "name": f.get("name"), "phone": f.get("phone"),
                                "education": f.get("education"), "years": f.get("years_experience") or 0,
                                "certificates": f.get("certificates") or "", "major": f.get("major") or "",
                                "skills": toks(f.get("skills")), "expected_position": f.get("expected_position"),
                                "org": f.get("org")}
    byjob = collections.defaultdict(list)
    for p in pairs:
        if p["name"] in cands and p["job_id"] in jobs:
            byjob[p["job_id"]].append(cands[p["name"]])
    blocks = []
    for jid in sorted(byjob):
        jf = jobs[jid]["fields"]
        blocks.append({"job": {"job_id": jid, "job_name": jf.get("job_name"),
                               "department": jf.get("department"), "org": jf.get("org"),
                               "hard_gates": txt(jf.get("hard_gates")),
                               "must_skills": txt(jf.get("must_skills")),
                               "bonus_skills": txt(jf.get("bonus_skills")),
                               "must_weight": float(jf.get("must_weight") or 0.7),
                               "bonus_weight": float(jf.get("bonus_weight") or 0.3)},
                       "candidates": byjob[jid]})
    for old in os.listdir(OUTDIR):
        if old.startswith("match_pending_part") or old.startswith("match_done_part"):
            os.remove(os.path.join(OUTDIR, old))
    batch = int(args[args.index("--batch") + 1]) if "--batch" in args else None
    batch, groups = plan(len(blocks), batch)   # agent 数硬上限 20，默认自动铺满
    for i, g in enumerate(groups, 1):
        json.dump([blocks[k - 1] for k in g],
                  open(os.path.join(OUTDIR, "match_pending_part%d.json" % i), "w",
                       encoding="utf-8"), ensure_ascii=False, indent=1)
    meta = {"total_pairs": len(pairs), "jobs": len(blocks), "batches": len(groups),
            "batch_size": batch, "agents": len(groups),
            "agent_sizes": [len(g) for g in groups], "max_agents": MAX_AGENTS}
    json.dump(meta, open(os.path.join(OUTDIR, "match_analyze_meta.json"), "w", encoding="utf-8"))
    print(json.dumps(meta, ensure_ascii=False))


def merge(nt):
    n = 0
    while os.path.exists(os.path.join(OUTDIR, "match_pending_part%d.json" % (n + 1))):
        n += 1
    out, missing = [], []
    for i in range(1, n + 1):
        p = os.path.join(OUTDIR, "match_done_part%d.json" % i)
        if not os.path.exists(p):
            missing.append(i)
            continue
        try:
            out.extend(json.load(open(p, encoding="utf-8")))
        except Exception as e:
            missing.append(i)
            print("批次%d解析失败: %s" % (i, str(e)[:120]))
    json.dump(out, open(FINAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    keep = [x for x in out if x.get("keep")]
    print(json.dumps({"batches": n, "missing_batches": missing, "decided": len(out),
                      "keep": len(keep), "drop": len(out) - len(keep)}, ensure_ascii=False))


def apply_(nt):
    rows = json.load(open(FINAL, encoding="utf-8"))
    keep = [r for r in rows if r.get("keep")]
    jids = {r.get("job_id") for r in rows}
    jobs = {j["fields"].get("job_id"): j["id"] for j in nt.list_records("job", biz_fields=["job_id"])}
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "skills", "years_experience",
                                                   "expected_position", "org"]):
        f = r["fields"]
        cands[f.get("name")] = f
    olds = [r["id"] for r in nt.list_records("match", biz_fields=["job_id"])
            if r["fields"].get("job_id") in jids]
    if olds:
        nt.delete_records("match", olds)
    create, jmeta = [], {}
    for j in nt.list_records("job", biz_fields=["job_id", "job_name", "must_skills",
                                                "bonus_skills", "hard_gates", "department"]):
        jmeta[j["fields"].get("job_id")] = j["fields"]
    for r in keep:
        cf, jf = cands.get(r.get("name")), jmeta.get(r.get("job_id"))
        if not cf or not jf:
            continue
        create.append({"name": r["name"], "phone": cf.get("phone"), "job_id": r["job_id"],
                       "job_name": jf.get("job_name"), "org": jf.get("department") and cf.get("org"),
                       "source": "系统匹配", "cand_skills": "、".join(toks(cf.get("skills"))),
                       "must_skills": txt(jf.get("must_skills")), "bonus_skills": txt(jf.get("bonus_skills")),
                       "hard_gates": txt(jf.get("hard_gates")),
                       "expected_position": cf.get("expected_position"),
                       "years_experience": str(cf.get("years_experience") or "无"),
                       "skill_score": int(r.get("skill_score") or 0),
                       "bonus_score": int(r.get("bonus_score") or 0),
                       "total_score": int(r.get("total_score") or 0),
                       "recommend": r.get("recommend") or "不推荐",
                       "evidence": r.get("evidence") or "",
                       "ai_analysis": r.get("ai_analysis") or "",
                       "update_time": int(time.time() * 1000)})
    ids = nt.create_records("match", create)
    print(json.dumps({"created": len(create), "deleted_old": len(olds),
                      "next": "分别执行 link 与 stats（索引延迟，必须另起读取）"}, ensure_ascii=False))


def link(nt):
    jobs = {j["fields"].get("job_id"): j["id"] for j in nt.list_records("job", biz_fields=["job_id"])}
    rows = nt.list_records("match", biz_fields=["job_id", "name"])
    put = [{"id": r["id"], "fields": {"关联岗位": {"linkedRecordIds": [jobs[r["fields"]["job_id"]]]}}}
           for r in rows if r["fields"].get("job_id") in jobs]
    for i in range(0, len(put), 10):
        nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/records" % (nt.base, nt.sheet("match")),
                {"records": put[i:i + 10]})
    print(json.dumps({"match_rows": len(rows), "linked": len(put)}, ensure_ascii=False))


def stats(nt):
    rows = nt.list_records("match", biz_fields=["job_id", "recommend"])
    st = collections.defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        f = r["fields"]
        s = st[f.get("job_id")]
        s[0] += 1
        s[{"推荐": 1, "待定": 2, "不推荐": 3}.get(f.get("recommend"), 3)] += 1
    jobs = nt.list_records("job", biz_fields=["job_id"])
    upd = []
    for j in jobs:
        v = st.get(j["fields"].get("job_id"), [0, 0, 0, 0])
        upd.append({"id": j["id"], "stat_total": v[0], "stat_recommend": v[1],
                    "stat_pending": v[2], "stat_reject": v[3]})
    nt.update_records("job", upd)
    dist = collections.Counter(r["fields"].get("recommend") for r in rows)
    print(json.dumps({"match_rows": len(rows), "jobs_updated": len(upd), "分布": dict(dist)},
                     ensure_ascii=False))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("prepare", "merge", "apply", "link", "stats"):
        print(__doc__)
        sys.exit(1)
    nt = Notable()
    {"prepare": lambda n: prepare(n, sys.argv[2:]),
     "merge": merge, "apply": apply_, "link": link, "stats": stats}[cmd](nt)


if __name__ == "__main__":
    main()
