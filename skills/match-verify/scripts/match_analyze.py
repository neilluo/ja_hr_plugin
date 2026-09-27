# -*- coding: utf-8 -*-
"""match_analyze.py — 智能匹配的逐岗精析（subagent 并发，一岗一批，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

    python3 skills/match-verify/scripts/match_gated.py                     # 0 机械门槛判定 → outputs/gate_pairs.json
    python3 skills/match-verify/scripts/match_analyze.py prepare           # 1 按岗位切批（每岗一批）
    python3 skills/match-verify/scripts/match_analyze.py merge             # 2 合并子任务判定
    python3 skills/match-verify/scripts/match_analyze.py apply             # 3 只建 keep=true 的配对
    python3 skills/match-verify/scripts/match_analyze.py stats             # 4 单独读取后刷新岗位四项统计

子任务负责：判「专业/工序是否实质对口」+ 给部分覆盖计分 + 产出推荐状态/匹配依据/AI匹配分析。
提示词：skills/match-verify/references/match-subagent-prompt.md

切批/清旧/写 meta 与 done 合并的公共骨架 = shared/analyze_parts.py（parts 命名唯一真源）。
分派纪律（历史教训：merge(nt) vs prepare(nt,args) 签名不一致必崩 TypeError）：
所有子命令 handler 签名一致 = handler(args)，需要 nt 的自己构造；merge 不触网就不构造 nt。
"""
import sys, os, json, time, collections

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from semantic_score import toks, txt  # noqa: E402
from match_gated import SYS_SOURCE  # noqa: E402  系统来源标记唯一具名常量（同 skill 私有库）
import analyze_parts as ap  # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）

_CONFIG = os.path.join(ROOT, "config.json")
CONFIG = json.load(open(_CONFIG, encoding="utf-8"))
# 推荐状态清单唯一派生自 config.options.match.recommend
_RECOMMEND = CONFIG["options"]["match"]["recommend"]

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "match"
PAIRS = os.path.join(OUTDIR, "gate_pairs.json")
FINAL = os.path.join(OUTDIR, "match_final.json")
# 单 agent 配对上限：实测 12 对/agent 的产出量是中位数 2 倍、落地耗时 7 倍，
# 拉长 stall 窗口暴露时间；超限的岗位拆成多个 block（merge/apply 行级处理，同岗多块安全）
MAX_PAIRS_PER_AGENT = 8


def _cap_blocks(blocks):
    out = []
    for b in blocks:
        cs = b["candidates"]
        if not cs:
            continue
        for i in range(0, len(cs), MAX_PAIRS_PER_AGENT):
            nb = dict(b)
            nb["candidates"] = cs[i:i + MAX_PAIRS_PER_AGENT]
            out.append(nb)
    return out


def prepare(args):
    nt = Notable()
    args = args or []
    pairs = json.load(open(PAIRS, encoding="utf-8"))
    jobs = {j["fields"].get("job_id"): j for j in nt.list_records(
        "job", biz_fields=["job_id", "job_name", "department", "org", "hard_gates",
                           "must_skills", "bonus_skills", "must_weight", "bonus_weight"])}
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "major", "skills",
                                                   "expected_position", "org"]):
        f = r["fields"]
        # key 用 record id（重名简历会互相覆盖），name 只作展示字段
        cands[r["id"]] = {"id": r["id"], "name": f.get("name"), "phone": f.get("phone"),
                          "education": f.get("education"), "years": f.get("years_experience") or 0,
                          "certificates": f.get("certificates") or "", "major": f.get("major") or "",
                          "skills": toks(f.get("skills")), "expected_position": f.get("expected_position"),
                          "org": f.get("org")}
    byjob = collections.defaultdict(list)
    for p in pairs:
        c = cands.get(p.get("rid"))
        if c and p["job_id"] in jobs:
            byjob[p["job_id"]].append(c)
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
    batch = int(args[args.index("--batch") + 1]) if "--batch" in args else None
    # agent 数硬上限见 shared/waves.MAX_AGENTS(=20)，默认自动铺满
    capped = _cap_blocks(blocks)
    ap.write_parts(OUTDIR, PREFIX, capped,
                   {"total_pairs": len(pairs), "jobs": len(blocks), "blocks": len(capped)}, batch)


def merge(args):
    n = ap.count_pending(OUTDIR, PREFIX)
    # 子任务产出只带 name/job_id；用 pending 输入回联 (job_id, name)→record id，给每条判定补 rid，
    # 供 apply 按 record id 取简历（重名不覆盖）
    rid_of = {}
    for i in range(1, n + 1):
        pp = ap.pending_path(OUTDIR, PREFIX, i)
        if not os.path.exists(pp):
            continue
        try:
            for blk in json.load(open(pp, encoding="utf-8")):
                for c in blk.get("candidates", []):
                    rid_of.setdefault((blk["job"]["job_id"], c.get("name")), c.get("id"))
        except Exception:
            pass    # pending 缺坏只影响 rid 补全，判定照常合并（apply 有 name 回退）
    rows, missing = ap.read_done(OUTDIR, PREFIX)
    for r in rows:
        r.setdefault("rid", rid_of.get((r.get("job_id"), r.get("name"))))
    json.dump(rows, open(FINAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    keep = [x for x in rows if x.get("keep")]
    print(json.dumps({"batches": n, "missing_batches": missing, "decided": len(rows),
                      "keep": len(keep), "drop": len(rows) - len(keep)}, ensure_ascii=False))


def apply_(nt):
    rows = json.load(open(FINAL, encoding="utf-8"))
    keep = [r for r in rows if r.get("keep")]
    jids = {r.get("job_id") for r in rows}
    cands, by_name = {}, {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "skills", "years_experience",
                                                   "expected_position", "org"]):
        cands[r["id"]] = r["fields"]   # key 用 record id（重名不覆盖），与 match_gated 同因
        by_name.setdefault(r["fields"].get("name"), r["id"])   # rid 缺失（旧产物）时的回退
    # 幂等：按 job_id 整岗删旧，但只删系统匹配记录（source 过滤：人工匹配记录不删）
    olds = [r["id"] for r in nt.list_records("match", biz_fields=["job_id", "source"])
            if r["fields"].get("source") == SYS_SOURCE
            and r["fields"].get("job_id") in jids]
    if olds:
        nt.delete_records("match", olds)
    create, jmeta = [], {}
    for j in nt.list_records("job", biz_fields=["job_id", "job_name", "org", "must_skills",
                                                "bonus_skills", "hard_gates", "department"]):
        jmeta[j["fields"].get("job_id")] = j["fields"]
    for r in keep:
        rid = r.get("rid") or by_name.get(r.get("name"))
        cf, jf = cands.get(rid), jmeta.get(r.get("job_id"))
        if not cf or not jf:
            continue
        create.append({"name": r["name"], "phone": cf.get("phone"), "job_id": r["job_id"],
                       "job_name": jf.get("job_name"), "org": jf.get("org"),
                       "source": SYS_SOURCE, "cand_skills": "、".join(toks(cf.get("skills"))),
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
    nt.create_records("match", create)
    print(json.dumps({"created": len(create), "deleted_old": len(olds),
                      "next": "执行 stats 刷新岗位统计（索引延迟，必须另起读取）"}, ensure_ascii=False))


def stats(nt):
    rows = nt.list_records("match", biz_fields=["job_id", "recommend"])
    # 统计位次派生自 config.options.match.recommend 顺序（推荐→1 待定→2 不推荐→3）
    rec_idx = {r: i + 1 for i, r in enumerate(_RECOMMEND)}
    st = collections.defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        f = r["fields"]
        s = st[f.get("job_id")]
        s[0] += 1
        s[rec_idx.get(f.get("recommend"), len(_RECOMMEND))] += 1
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


def _apply(args):
    apply_(Notable())


def _stats(args):
    stats(Notable())


# 统一签名 handler(args)；apply_/stats 保留 (nt) 形参供回归测试直接注入 mock
HANDLERS = {"prepare": prepare, "merge": merge, "apply": _apply, "stats": _stats}


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不构造 Notable、不触网
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("prepare", "merge", "apply", "stats"):   # 子命令白名单
        print(__doc__)
        sys.exit(1)
    HANDLERS[cmd](sys.argv[2:])


if __name__ == "__main__":
    main()
