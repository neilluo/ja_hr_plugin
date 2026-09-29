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
import sys, os, json, collections

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from semantic_score import toks, txt  # noqa: E402
# 同 skill 私有常量/公共件唯一真源在 match_gated（不变量 10）：来源标记、行组装、四项统计刷新
from match_gated import SYS_SOURCE, _match_row, refresh_job_stats  # noqa: E402
import analyze_parts as ap  # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）
import soften  # noqa: E402  L2 长度软阈值唯一真源（EV_LEN_MAX/AI_ANALYSIS_RANGE）

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
    meta = ap.write_parts(OUTDIR, PREFIX, capped,
                          {"total_pairs": len(pairs), "jobs": len(blocks), "blocks": len(capped)}, batch)
    # 分派清单落盘：agent 复制真实 pending 路径指针分派，不再凭记忆手拼 *_pending_part<N>.json
    # （清单形态唯一真源 = shared/analyze_parts.write_dispatch，与 jobs_analyze 同机制；
    # stdout 仍只留 write_parts 打的单行 meta）
    if meta["batches"]:
        meta["dispatch"] = ap.write_dispatch(OUTDIR, PREFIX, meta["batches"])
        json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta


def match_observations(rows, expected):
    """match 链 L2 软观察（阈值唯一真源 shared/soften.EV_LEN_MAX / AI_ANALYSIS_RANGE）。

    只报、绝不阻断：不截断、不丢行、不参与 keep/分数/recommend 任何决策，也不影响
    missing_batches 口径。2026-09-29 事故教训：evidence 80 字上限曾由提示词声明、
    subagent 自写 assert 执行，11 批里 9 批撞 AssertionError 返工改写；现长度一律降级为
    观察（prompt 同步改软偏好、删自检返工指令，见 match-subagent-prompt.md）。
    expected = pending 应覆盖的 (job_id, name) 集合，missing_pairs 只报漏行不补不丢。
    返回 {观察名: 非空问题列表}（空观察不出现）。"""
    obs = {"overlong_evidence": [], "overlong_analysis": [], "thin_analysis": [],
           "missing_fields": [], "missing_keep": [], "missing_pairs": []}
    got = set()
    for r in rows:
        tag = "%s/%s" % (r.get("job_id"), r.get("name"))
        got.add((r.get("job_id"), r.get("name")))
        ev = r.get("evidence")
        if isinstance(ev, str) and len(ev) > soften.EV_LEN_MAX:
            obs["overlong_evidence"].append([tag, len(ev)])
        ai = r.get("ai_analysis")
        if isinstance(ai, str) and ai.strip():
            if len(ai) > soften.AI_ANALYSIS_RANGE[1]:
                obs["overlong_analysis"].append([tag, len(ai)])
            elif len(ai) < soften.AI_ANALYSIS_RANGE[0]:
                obs["thin_analysis"].append([tag, len(ai)])
        elif r.get("keep"):
            obs["missing_fields"].append([tag, "ai_analysis"])
        if r.get("keep") and not isinstance(r.get("skill_score"), (int, float)):
            obs["missing_fields"].append([tag, "skill_score"])
        if "keep" not in r:
            obs["missing_keep"].append(tag)
    for key in sorted(expected - got):
        obs["missing_pairs"].append(list(key))
    return {k: v for k, v in obs.items() if v}


def merge(args):
    n = ap.count_pending(OUTDIR, PREFIX)
    # 子任务产出只带 name/job_id；用 pending 输入回联 (job_id, name)→record id，给每条判定补 rid，
    # 供 apply 按 record id 取简历（重名不覆盖）
    rid_of = {}
    expected = set()
    for i in range(1, n + 1):
        pp = ap.pending_path(OUTDIR, PREFIX, i)
        if not os.path.exists(pp):
            continue
        try:
            for blk in json.load(open(pp, encoding="utf-8")):
                for c in blk.get("candidates", []):
                    rid_of.setdefault((blk["job"]["job_id"], c.get("name")), c.get("id"))
                    expected.add((blk["job"]["job_id"], c.get("name")))
        except Exception:
            pass    # pending 缺坏只影响 rid 补全，判定照常合并（apply 有 name 回退）
    rows, missing = ap.read_done(OUTDIR, PREFIX)
    for r in rows:
        r.setdefault("rid", rid_of.get((r.get("job_id"), r.get("name"))))
    obs = match_observations(rows, expected)
    json.dump(rows, open(FINAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    keep = [x for x in rows if x.get("keep")]
    print(json.dumps({"batches": n, "missing_batches": missing, "decided": len(rows),
                      "keep": len(keep), "drop": len(rows) - len(keep),
                      "observations": obs}, ensure_ascii=False))


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
        # 行组装唯一真源 match_gated._match_row（与 stage_commit 共用；缺推荐兜底 REJ_LABEL）
        create.append(_match_row(cf, {"job_id": r["job_id"], "job_name": jf.get("job_name"),
                                      "org": jf.get("org"), "must_skills": jf.get("must_skills"),
                                      "bonus_skills": jf.get("bonus_skills"),
                                      "hard_gates": jf.get("hard_gates")},
                                 {"name": r["name"], "skill_score": r.get("skill_score"),
                                  "bonus_score": r.get("bonus_score"), "total_score": r.get("total_score"),
                                  "recommend": r.get("recommend"), "evidence": r.get("evidence"),
                                  "ai_analysis": r.get("ai_analysis")}))
    nt.create_records("match", create)
    print(json.dumps({"created": len(create), "deleted_old": len(olds),
                      "next": "执行 stats 刷新岗位统计（索引延迟，必须另起读取）"}, ensure_ascii=False))


def stats(nt):
    # 统计唯一实现 match_gated.refresh_job_stats（与 stage_stats 共用）
    n_rows, n_jobs, dist = refresh_job_stats(nt)
    print(json.dumps({"match_rows": n_rows, "jobs_updated": n_jobs, "分布": dict(dist)},
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
