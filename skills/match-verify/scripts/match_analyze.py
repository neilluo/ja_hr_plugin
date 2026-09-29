# -*- coding: utf-8 -*-
"""match_analyze.py — 智能匹配的逐岗精析（subagent 并发，一岗一批，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

    python3 skills/match-verify/scripts/match_gated.py                     # 0 机械门槛判定 → outputs/gate_pairs.json
    python3 skills/match-verify/scripts/match_analyze.py prepare           # 1 按岗位切批（每岗一批）+ 注入 baseline + 渲染 per-batch 提示词
    python3 skills/match-verify/scripts/match_analyze.py merge             # 2 归属校验 + 分数重算（串写/缺批 exit 2）
    python3 skills/match-verify/scripts/match_analyze.py apply             # 3 只建 keep=true 的配对
    python3 skills/match-verify/scripts/match_analyze.py stats             # 4 单独读取后刷新岗位四项统计

v2 分派契约（2026-09-29 事故重构，见 AGENTS.md 犯错记录）：agent 只负责方向判断（keep/keep_reason）、
语义增补（grants）与给人看的 ai_analysis；分数/evidence 一律代码算——
  - prepare 给每个候选人注入 baseline（机械命中 + 分数，经 match_gated.hits/score_counts），
    并渲染 per-batch 提示词 outputs/match_prompt_part<N>.md（<BATCH_PATH>/<DONE_PATH> 硬注入，
    路径与本批绑定，杜绝"agent 记错 part 号跨批误写"）；分派清单 match_dispatch.json 的 prompts 键。
  - merge 逐批归属校验（done_part<i> 里 (job_id,name) 不在 pending_part<i> 期望集合 = 串写，
    进报告 misattributed 不进 rows）+ 用 score_counts 重算 keep 行分数（机械命中 ∪ 有效 grants）、
    代码组装 evidence；misattributed 或 missing_batches 非空 → 打印报告后 exit 2。
提示词模板（唯一真源，render_prompts 渲染）：skills/match-verify/references/match-subagent-prompt.md

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
# 同 skill 私有常量/公共件唯一真源在 match_gated（不变量 10）：来源标记、行组装、四项统计刷新、
# 命中判定（hits）、打分公式与档位（score_counts）、evidence 格式串（evidence）
from match_gated import (SYS_SOURCE, _match_row, refresh_job_stats,  # noqa: E402
                         hits, score_counts, evidence, weights)
import analyze_parts as ap  # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）
import soften  # noqa: E402  L2 长度软阈值唯一真源（EV_LEN_MAX/AI_ANALYSIS_RANGE）

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "match"
PAIRS = os.path.join(OUTDIR, "gate_pairs.json")
FINAL = os.path.join(OUTDIR, "match_final.json")
# 提示词唯一源（模板）；per-batch 渲染产物见 render_prompts（与 skills/jobs 链对称）
PROMPT_TPL = os.path.join(ROOT, "skills", "match-verify", "references", "match-subagent-prompt.md")
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


def baseline_of(cand, jf):
    """一个 (候选人, 岗位) 的机械打分基线（prepare 注入 pending、merge 重算共用同一实现）。

    公式/档位/命中判定全部经 match_gated（hits + score_counts，唯一真源），本函数只做组装。
    miss = 该侧词表里未命中的项（subagent 的 grants 只允许增补这里的项）。"""
    hm, hb = hits({"skills": cand.get("skills") or []}, jf)
    must, bonus = toks(jf.get("must_skills")), toks(jf.get("bonus_skills"))
    mw, bw = weights(jf)
    sk, bo, tot, rec = score_counts(len(hm), len(hb), len(must), len(bonus), mw, bw)
    return {"skill_score": sk, "bonus_score": bo, "total_score": tot, "recommend": rec,
            "must_hits": hm, "must_miss": [n for n in must if n not in hm],
            "bonus_hits": hb, "bonus_miss": [n for n in bonus if n not in hb]}


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
            # baseline 按 (候选人, 岗位) 逐对注入：同一候选人可配多岗，dict(c) 拷贝防互相覆盖
            c2 = dict(c)
            c2["baseline"] = baseline_of(c, jobs[p["job_id"]]["fields"])
            byjob[p["job_id"]].append(c2)
    blocks = []
    for jid in sorted(byjob):
        jf = jobs[jid]["fields"]
        mw, bw = weights(jf)
        blocks.append({"job": {"job_id": jid, "job_name": jf.get("job_name"),
                               "department": jf.get("department"), "org": jf.get("org"),
                               "hard_gates": txt(jf.get("hard_gates")),
                               "must_skills": txt(jf.get("must_skills")),
                               "bonus_skills": txt(jf.get("bonus_skills")),
                               "must_weight": mw,
                               "bonus_weight": bw},
                       "candidates": byjob[jid]})
    batch = int(args[args.index("--batch") + 1]) if "--batch" in args else None
    # agent 数硬上限见 shared/waves.MAX_AGENTS(=20)，默认自动铺满
    capped = _cap_blocks(blocks)
    meta = ap.write_parts(OUTDIR, PREFIX, capped,
                          {"total_pairs": len(pairs), "jobs": len(blocks), "blocks": len(capped)}, batch)
    # 渲染 per-batch 提示词 + 分派清单（prompts 形态，与 skills/jobs 链同机制）：
    # 分派时每批只发一行 match_prompt_part<N>.md 路径指针，读写路径已在提示词里硬绑定本批，
    # agent 不再凭记忆手拼 part 号（stdout 仍只留 write_parts 打的单行 meta）
    if meta["batches"]:
        prompts = render_prompts(meta["batches"])
        meta["dispatch"] = ap.write_dispatch(OUTDIR, PREFIX, prompts)  # 清单形状唯一真源在公共骨架
        json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta


def render_prompts(n_batches):
    """把提示词模板渲染成 n_batches 个 per-batch 文件，返回渲染产物路径列表。

    match 分派提示词形态的唯一真源（AGENTS.md 不变量 10）：占位符 <N>/<BATCH_PATH>/<DONE_PATH>
    由代码替换（路径经 analyze_parts 派生，禁止本地抄 parts 命名），<AI_RANGE> 从
    shared/soften.AI_ANALYSIS_RANGE 注入（模板不手抄数字）。
    为什么渲染（2026-09-29 事故）：旧分派靠"共享模板 + agent 记住自己的 part 号"，路径无硬绑定，
    批9 agent 读了 part2 的输入、覆盖并删除了批2 的好产物；渲染后每批提示词自带本批读写路径，
    跨批误写被结构性消除，且分派载荷从内联原文降为一行路径指针（同 skills/jobs 链教训）。
    """
    with open(PROMPT_TPL, encoding="utf-8") as f:
        tpl = f.read()
    ap.prune_prompts(OUTDIR, PREFIX)   # 命名/清旧唯一真源在公共骨架（防小批次轮僵尸文件）
    paths = []
    for i in range(1, n_batches + 1):
        body = (tpl.replace("<BATCH_PATH>", ap.pending_path(OUTDIR, PREFIX, i))
                   .replace("<DONE_PATH>", ap.done_path(OUTDIR, PREFIX, i))
                   .replace("<AI_RANGE>", "%d-%d" % soften.AI_ANALYSIS_RANGE))
        body = body.replace("<N>", str(i))   # <N> 最后替换：防其他占位符名里含 "<N" 子串误伤
        out = ap.prompt_path(OUTDIR, PREFIX, i)
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        paths.append(out)
    return paths


def _valid_grants(row, base, must, bonus, invalid):
    """校验并收敛一行的 grants：返回 (must 侧有效增补, bonus 侧有效增补)，元素为 (item, basis)。

    有效 = side 合法 且 item 是该侧岗位词表原词 且 basis 非空字符串；无效 grant 丢弃并记入
    invalid（[[job_id, name, side, item, 原因]]，进 observations 供人工过目，不阻断）。
    机械已命中的 item 不重复计（union 去重：命中列表里已有同名字符串则跳过）；
    同侧重复 grant 同一 item 也只计一次（2026-09-29 review 抓出：重复 grant 曾把
    命中数虚增到满配、档位从「不推荐」抬到「推荐」）。"""
    jid, name = row.get("job_id"), row.get("name")
    out = {"must": [], "bonus": []}
    seen = {"must": set(), "bonus": set()}
    for g in row.get("grants") or []:
        if not isinstance(g, dict):
            invalid.append([jid, name, None, None, "grant 不是 dict"])
            continue
        side, item, basis = g.get("side"), g.get("item"), g.get("basis")
        if side not in ("must", "bonus"):
            invalid.append([jid, name, side, item, "side 非法（只准 must/bonus）"])
            continue
        words = must if side == "must" else bonus
        if not isinstance(item, str) or item not in words:
            invalid.append([jid, name, side, item, "item 不在该侧岗位词表"])
            continue
        if not (isinstance(basis, str) and basis.strip()):
            invalid.append([jid, name, side, item, "basis 空（无依据不得送分）"])
            continue
        hit_now = base.get("must_hits") if side == "must" else base.get("bonus_hits")
        if item in (hit_now or []):
            continue    # 机械已命中：union 去重，不重复计
        if item in seen[side]:
            continue    # 同侧重复 grant：只计一次，不双计
        seen[side].add(item)
        out[side].append((item, basis.strip()))
    return out["must"], out["bonus"]


def rescore(row, cand, job, invalid_grants):
    """keep=true 行的分数重算（merge 专用，公式唯一真源 match_gated.score_counts/evidence）。

    final 命中 = 机械命中 ∪ 有效 grants（grant 命中项在 evidence 里以
    「项※(依据)」形态出现）；行内 agent 可能残留的 skill_score/evidence 等同名字段一律被覆盖。
    机械命中经 baseline_of 从候选人 skills + 岗位词表**实时重算**，不采信 pending 里序列化的
    baseline 副本：旧产物/手搓 pending 缺 baseline 时照抄副本会把分数静默算成 0
    （2026-09-29 test_match_grants 夹具抓出的真缺陷）。
    事故背景：分数公式是确定性的却交给 agent 手算，同一输入 8 对里 7 对不一致。"""
    base = baseline_of(cand or {}, job)
    must, bonus = toks(job.get("must_skills")), toks(job.get("bonus_skills"))
    gm, gb = _valid_grants(row, base, must, bonus, invalid_grants)
    hm = list(base["must_hits"]) + gm
    hb = list(base["bonus_hits"]) + gb
    sk, bo, tot, rec = score_counts(len(hm), len(hb), len(must), len(bonus), *weights(job))
    row.update({"skill_score": sk, "bonus_score": bo, "total_score": tot,
                "recommend": rec, "evidence": evidence(hm, hb, must, bonus)})


def match_observations(rows, expected):
    """match 链 L2 软观察（阈值唯一真源 shared/soften.EV_LEN_MAX / AI_ANALYSIS_RANGE）。

    只报、绝不阻断：不截断、不丢行、不参与 keep/分数/recommend 任何决策，也不影响
    missing_batches 口径。2026-09-29 事故教训：evidence 80 字上限曾由提示词声明、
    subagent 自写 assert 执行，11 批里 9 批撞 AssertionError 返工改写；现长度一律降级为
    观察（prompt 同步改软偏好、删自检返工指令，见 match-subagent-prompt.md）。
    v2 契约：分数/evidence 由 merge 代码重算，agent 不供分 → skill_score 缺失检查已删；
    overlong_evidence 观察的是代码自产的 evidence（仍保留，供词表/截断口径人工过目）。
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
        if "keep" not in r:
            obs["missing_keep"].append(tag)
    for key in sorted(expected - got):
        obs["missing_pairs"].append(list(key))
    return {k: v for k, v in obs.items() if v}


def merge(args):
    n = ap.count_pending(OUTDIR, PREFIX)
    # pending 是本批归属与重算的唯一依据：期望集合（逐批）、(job_id,name)→rid/候选人基线、
    # job_id→岗位词表与权重（子任务产出只带 name/job_id，其余全部回联 pending 补齐）
    rid_of, pend_of, job_of = {}, {}, {}
    expected, expect_by_part = set(), {}
    rows, misattributed, missing, pending_corrupt, dup_rows = [], [], [], [], []
    for i in range(1, n + 1):
        es = set()
        expect_by_part[i] = es
        pp = ap.pending_path(OUTDIR, PREFIX, i)
        if not os.path.exists(pp):
            continue
        try:
            for blk in json.load(open(pp, encoding="utf-8")):
                job = blk.get("job") or {}
                job_of.setdefault(job.get("job_id"), job)
                for c in blk.get("candidates", []):
                    key = (job.get("job_id"), c.get("name"))
                    rid_of.setdefault(key, c.get("id"))
                    pend_of.setdefault(key, c)
                    es.add(key)
                    expected.add(key)
        except Exception:
            # pending 自身损坏 ≠ agent 串写：恢复动作是重跑 prepare 而非补发 subagent，
            # 单独成键报出（2026-09-29 review：混进 misattributed 会误导恢复路径）；
            # 该批期望集合为空 → done 行全部按串写报出（归属校验从严）
            pending_corrupt.append(i)
    # 逐批读 done + 归属校验：done_part<i> 里 (job_id,name) 不在 pending_part<i> 期望集合内
    # = 串写（2026-09-29 事故：批9 agent 跑错批次覆盖批2 好产物），只报不收；
    # 属于别的 pending_j 的行同样只报不收（正确批次自己的产物才是有效判定）。
    for i in range(1, n + 1):
        dp = ap.done_path(OUTDIR, PREFIX, i)
        if not os.path.exists(dp):
            missing.append(i)
            continue
        got, errors = ap.load_done(dp)   # 行级抢救口径与公共骨架单源（ap.load_done）
        if not got:
            missing.append(i)   # 整批未产出（文件坏，或交了空数组）= 缺批，exit 2 由下方判定
            if errors:
                print("批次%d解析失败: %s" % (i, errors[0]["reason"][:120]))
            continue
        if errors:
            print("批次%d部分损坏: 抢救 %d 行，坏 %d 行（%s）"
                  % (i, len(got), len(errors), "、".join(str(x["id"]) for x in errors)))
        seen_here = set()
        for r in got:
            key = (r.get("job_id"), r.get("name")) if isinstance(r, dict) else (None, None)
            if key not in expect_by_part[i]:
                misattributed.append([i, key[0], key[1]])
                continue
            if key in seen_here:
                dup_rows.append([i, key[0], key[1]])   # 批内重复行：首行生效，重复只报不收
                continue
            seen_here.add(key)
            r.setdefault("rid", rid_of.get(key))
            rows.append(r)
    # 分数重算（keep=true：机械命中 ∪ 有效 grants，公式经 match_gated 唯一真源）；
    # drop 行统一 None（消除旧契约 0/None 混用），agent 残留的分数字段一律覆盖。
    # job_of 与归属校验同源于 pending：key 过了归属校验则词表必在，直接取——
    # 若此处 KeyError 说明两键派生发散（编程错误），宁可炸响也不静默降档。
    invalid_grants = []
    for r in rows:
        key = (r.get("job_id"), r.get("name"))
        if r.get("keep"):
            rescore(r, pend_of.get(key), job_of[key[0]], invalid_grants)
        else:
            for k in ("skill_score", "bonus_score", "total_score", "recommend", "evidence"):
                r[k] = None
    obs = match_observations(rows, expected)
    if invalid_grants:
        obs["invalid_grants"] = invalid_grants
    json.dump(rows, open(FINAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    keep = [x for x in rows if x.get("keep")]
    report = {"batches": n, "missing_batches": missing, "misattributed": misattributed,
              "pending_corrupt": pending_corrupt, "duplicate_rows": dup_rows,
              "decided": len(rows), "keep": len(keep), "drop": len(rows) - len(keep),
              "observations": obs}
    print(json.dumps(report, ensure_ascii=False))
    # 硬失败（L0）：串写/缺批/pending 损坏说明产物不可信，报告先打（agent 仍能看到 JSON）
    # 再 exit 2，禁止带着不可信产物继续 apply；observations（含 invalid_grants）与批内重复行
    # （首行已生效、不丢数据）仍只报不阻断。
    if misattributed or missing or pending_corrupt:
        sys.exit(2)


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
