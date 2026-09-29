# -*- coding: utf-8 -*-
"""match_gated.py — 智能匹配（门槛前置版）：先过硬性门槛，只为达标的人-岗建配对并打分

用法：
    python3 skills/match-verify/scripts/match_gated.py            # 阶段1 门槛判定，输出 outputs/gate_pairs.json / gate_pending.json
    python3 skills/match-verify/scripts/match_gated.py --force    # 阶段1 绕过精析队列门禁（粗值打分，风险自担）
    python3 skills/match-verify/scripts/match_gated.py --commit   # 阶段2 只建达标配对 + 语义打分
    python3 skills/match-verify/scripts/match_gated.py --stats    # 阶段3 单独一次读取，刷新岗位四项统计

前置门禁（stage_gate）：job 表 0 条、或在岗 must_skills 全空 → 拒绝（强跑必出 0 配对，--force 也不放行）；
精析队列非空（真源 shared/refine_loop.queue_counts）→ 拒绝粗值打分，--force 可绕过。
机械门槛（一票否决，缺证据即不通过）：组织一致、学历档次、经验年限、证书、年龄（简历有则判）。
「相关专业 / 工序是否对口」不机械否决，写进 gate_pending.json 由智能体批量判定后再决定去留。
技能同义与上下位关系统一复用 semantic_score 的语义词典，不做 A=A 字面相等。
"""
import sys, os, re, json, time, collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "shared", "preflight"))
sys.path.insert(0, HERE)
from notable import Notable  # noqa: E402
from preflight import run_preflight  # noqa: E402
from semantic_score import hit, toks, txt  # noqa: E402  语义词典与命中判定
import refine_loop  # noqa: E402  精析队列谓词唯一真源，禁止本地抄副本

_CONFIG = os.path.join(ROOT, "config.json")
CONFIG = json.load(open(_CONFIG, encoding="utf-8"))

# 系统写入的来源标记（写入值字面量，选项真源 config.options.match.source）；删旧只删它，不碰人工记录
SYS_SOURCE = "系统匹配"

# 学历档次映射（打分逻辑，非 config 枚举副本；键须与 config.options.resume.education 一致）
LEVEL = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}

# 推荐阈值：total>=REC_MIN 推荐 / >=PEND_MIN 待定 / <PEND_MIN 不推荐
REC_MIN = 80
PEND_MIN = 60
# 推荐状态三值唯一派生自 config.options.match.recommend（顺序：推荐/待定/不推荐）
_RECOMMEND = CONFIG["options"]["match"]["recommend"]
REC_LABEL, PEND_LABEL, REJ_LABEL = _RECOMMEND[0], _RECOMMEND[1], _RECOMMEND[2]

# gate 阶段产物统一写仓库根 outputs/（与 match_analyze 同源，杜绝双份路径）
OUTDIR = os.path.join(ROOT, "outputs")
GATE_PAIRS = os.path.join(OUTDIR, "gate_pairs.json")
GATE_PENDING = os.path.join(OUTDIR, "gate_pending.json")


def seg(g, key):
    m = re.search(key + r"[：:]\s*([^；;]+)", g or "")
    return m.group(1).strip() if m else ""


def gate(cand, jf, cs):
    """返回 (是否通过机械门槛, 未过原因, 专业是否需人工判定)"""
    g = txt(jf.get("hard_gates"))
    why = []
    if jf.get("org") and cand.get("org") and jf["org"] != cand["org"]:
        why.append("组织不一致")
    s = seg(g, "学历")
    if s:
        need = (1 if "放宽至大专" in s else
                2 if "本科及以上" in s else
                1 if "大专及以上" in s else
                3 if ("硕士" in s or "研究生" in s) else
                4 if "博士" in s else 0)
        have = LEVEL.get((cand.get("education") or "").strip(), 0)
        if need and have < need:
            why.append("学历%s＜要求%s" % (cand.get("education") or "未知", s))
    s = seg(g, "经验")
    if s and "应届" not in s and "不作" not in s:
        nums = [int(x) for x in re.findall(r"(\d{1,2})\s*年", s)]
        req = min(nums) if nums else 0
        cy = cand.get("years") or 0
        if req and float(cy) < req:
            why.append("经验%s年＜要求%s年" % (cy, req))
    s = seg(g, "证书")
    if s and "不作硬性要求" not in s:
        hard = re.sub(r"（[^）]*优先[^）]*）", "", s)
        hard = re.sub(r"[^、，,\s]*优先", "", hard)
        items = [re.sub(r"等.*$", "", x).strip() for x in re.split(r"[、,，]", hard) if x.strip()]
        pool = (cand.get("certificates") or "") + "|" + "|".join(cand.get("skills") or [])
        miss = [c for c in items if c and c not in pool]
        if miss:
            why.append("证书缺证据:" + "、".join(miss[:2]))
    s = seg(g, "年龄")
    if s and cand.get("age"):
        nums = [int(x) for x in re.findall(r"\d{2}", s)]
        if len(nums) >= 2 and not (min(nums) <= cand["age"] <= max(nums)):
            why.append("年龄%s不在%s区间" % (cand["age"], s))
    need_major = seg(g, "专业")
    if need_major:
        # 专业先按语义自动放行：候选人专业/技能任一命中岗位专业要求即通过；判不动的才交人工
        auto = any(hit(cs, t) for req in re.split(r"[、,，；;（）()]|\s{2,}", need_major)
                   for t in [tok.strip() for tok in [req] if len(req.strip()) >= 2])
        auto = auto or any(hit(cs, w) for w in re.findall(r"[\u4e00-\u9fff]{2,6}", need_major)) \
            or any((cand.get("major") or "") and cand["major"] in need_major for _ in [0])
    else:
        auto = True
    return (not why), why, (bool(need_major) and not auto)


def weights(jf):
    """权重取值 (mw, bw) 唯一实现：0.0 是合法业务值（该侧不计分），
    仅 None/空串回落默认 0.7/0.3——`or` 回落会把 0.0 误当缺失（2026-09-29
    test_match_grants 抓出：bonus_weight=0.0 被算成 0.3）。score/baseline_of/
    prepare/rescore 共用，禁止各自抄回落逻辑。"""
    mw, bw = jf.get("must_weight"), jf.get("bonus_weight")
    return (float(mw) if mw not in (None, "") else 0.7,
            float(bw) if bw not in (None, "") else 0.3)


def hits(cand, jf):
    """必备/加分命中列表 (hm, hb)：语义命中判定（semantic_score.hit）的唯一实现，
    score() 与 match_analyze（prepare 注入 baseline / merge 重算 grants）共用，禁止另抄。"""
    must, bonus = toks(jf.get("must_skills")), toks(jf.get("bonus_skills"))
    cs = cand.get("skills") or []
    hm = [n for n in must if hit(cs, n)]
    hb = [n for n in bonus if hit(cs, n)]
    return hm, hb


def score_counts(n_hm, n_hb, must_n, bonus_n, mw, bw):
    """打分公式 + 推荐档位的唯一实现 (sk, bo, tot, rec)。

    sk = round(100*mw*必备命中率)、bo = round(100*bw*加分命中率)、tot = sk+bo，
    rec 按 REC_MIN/PEND_MIN 分档（三值标签派生自 config.options.match.recommend）。
    score() 与 match_analyze.merge 共用；公式禁止第二份副本——
    2026-09-29 事故：公式是确定性的却交给 subagent 手算，同一输入 8 对里 7 对分数不一致。"""
    sk = int(round(100 * mw * (n_hm / must_n))) if must_n else 0
    bo = int(round(100 * bw * (n_hb / bonus_n))) if bonus_n else 0
    tot = sk + bo
    rec = REC_LABEL if tot >= REC_MIN else (PEND_LABEL if tot >= PEND_MIN else REJ_LABEL)
    return sk, bo, tot, rec


def evidence(hm, hb, must, bonus):
    """evidence 格式串的唯一组装点（score() 与 match_analyze.merge 共用）。

    hm 元素可为 (项, 依据) 元组 → 渲染为「项※(依据)」（merge 侧语义 grant 命中的展示形态）；
    未命中段按"must 里不在命中集合（元组按项计）"计算。截断口径不变：命中列 ≤8、未命中 ≤6。"""
    disp = [("%s※(%s)" % (x[0], x[1])) if isinstance(x, tuple) else x for x in hm]
    plain = {(x[0] if isinstance(x, tuple) else x) for x in hm}
    ev = "语义匹配：必备%d/%d（%s）；加分%d/%d" % (len(hm), len(must), "、".join(disp[:8]) or "无",
                                              len(hb), len(bonus))
    miss = [n for n in must if n not in plain]
    if miss:
        ev += "；未命中：" + "、".join(miss[:6])
    return ev


def score(cand, jf):
    """机械语义打分（hits + score_counts + evidence 的组合，行为与拆分前一致）。"""
    must, bonus = toks(jf.get("must_skills")), toks(jf.get("bonus_skills"))
    mw, bw = weights(jf)
    hm, hb = hits(cand, jf)
    sk, bo, tot, rec = score_counts(len(hm), len(hb), len(must), len(bonus), mw, bw)
    return sk, bo, tot, rec, evidence(hm, hb, must, bonus)


def _match_row(cf, jf, scores, source=SYS_SOURCE):
    """match 表一行的唯一组装点（不变量 10）。

    两条写表链路共用：stage_commit（机械打分直连兜底）与 match_analyze.apply_
    （subagent 判定）。cf = 简历表 fields，jf = 岗位表 fields，
    scores = 打分产物 dict（name/skill_score/bonus_score/total_score/recommend/evidence，
    可带 ai_analysis）；缺推荐值时兜底 REJ_LABEL（唯一派生自 config.options.match.recommend，
    禁止在此写"不推荐"字面量）。source 是写入值，选项真源 config.options.match.source。"""
    return {"job_id": jf.get("job_id"), "name": scores.get("name"), "phone": cf.get("phone"),
            "job_name": jf.get("job_name"), "org": jf.get("org"),
            "source": source, "cand_skills": "、".join(toks(cf.get("skills"))),
            "must_skills": txt(jf.get("must_skills")), "bonus_skills": txt(jf.get("bonus_skills")),
            "hard_gates": txt(jf.get("hard_gates")),
            "expected_position": cf.get("expected_position"),
            "years_experience": str(cf.get("years_experience") or "无"),
            "skill_score": int(scores.get("skill_score") or 0),
            "bonus_score": int(scores.get("bonus_score") or 0),
            "total_score": int(scores.get("total_score") or 0),
            "recommend": scores.get("recommend") or REJ_LABEL,
            "evidence": scores.get("evidence") or "",
            "ai_analysis": scores.get("ai_analysis") or "",
            "update_time": int(time.time() * 1000)}


def refresh_job_stats(nt):
    """岗位四项统计刷新的唯一实现（stage_stats 与 match_analyze.stats 共用）。

    位次派生自 config.options.match.recommend 顺序（推荐→1 待定→2 不推荐→3）。
    返回 (match 行数, 刷新的岗位数, recommend 分布 Counter)。"""
    rows = nt.list_records("match", biz_fields=["job_id", "recommend"])
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
    return len(rows), len(upd), dist


def _refuse(reason, **extra):
    """门禁拒绝：打印 JSON 原因并 exit 2。"""
    print(json.dumps({"refused": True, "reason": reason, **extra}, ensure_ascii=False))
    sys.exit(2)


def stage_gate(nt, force=False):
    os.makedirs(OUTDIR, exist_ok=True)
    jobs = [j["fields"] | {"_id": j["id"]} for j in
            nt.list_records("job", biz_fields=["job_id", "job_name", "department", "org", "status",
                                               "hard_gates", "must_skills", "bonus_skills",
                                               "must_weight", "bonus_weight"]) if
            (j["fields"].get("status") or "招聘中") == "招聘中"]
    # 前置门禁：job 表 0 条 / 在岗 must_skills 全空 → 强跑必出 0 配对，--force 也不放行；
    # 精析队列非空 → 粗值打分无意义，--force 可绕过（逃生舱）。队列谓词真源 refine_loop。
    if not jobs:
        _refuse("job 表 0 条在岗岗位：先入库岗位（skills/job-intake）再匹配，--force 也不放行")
    counts = refine_loop.queue_counts(nt)
    if any(counts.values()):
        if not force:
            _refuse("精析未完成/岗位链未跑，粗值打分无意义；--force 可强跑",
                    queue=counts, hint="先跑 skills-analyze / job-intake 精析流水线清空队列")
        print(json.dumps({"forced": True, "queue": counts,
                          "warn": "精析队列非空仍 --force 强跑：三列可能是入库正则粗值，分数仅参考"},
                         ensure_ascii=False))
    if not any(txt(j.get("must_skills")) for j in jobs):
        _refuse("在岗岗位 must_skills 全空：技能打分无从谈起，--force 也不放行；先跑 JD 精析回写三列")
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "skills", "org", "major"]):
        f = r["fields"]
        # key 用 record id（重名简历会互相覆盖，name 只作展示字段）
        cands[r["id"]] = {"name": f.get("name"), "phone": f.get("phone"),
                          "education": f.get("education"), "years": f.get("years_experience") or 0,
                          "certificates": f.get("certificates"), "skills": toks(f.get("skills")),
                          "major": f.get("major"), "org": f.get("org")}
    pairs, pending, passed = [], [], set()
    min_score = int(os.environ.get("MIN_SCORE", "20"))
    for jf in jobs:
        for rid, c in cands.items():
            cs = c["skills"]
            ok, why, need_major = gate(c, jf, cs)
            if not ok:
                continue
            sk, bo, tot, rec, ev = score({"skills": cs}, jf)
            if tot < min_score:          # 门槛过了但技能几乎无交集 -> 不建废配对
                continue
            passed.add(rid)
            if need_major:
                pending.append({"name": c["name"], "job_id": jf.get("job_id"),
                                "job_name": jf.get("job_name"), "major": c.get("major"),
                                "need": seg(txt(jf.get("hard_gates")), "专业")})
            pairs.append({"rid": rid, "name": c["name"], "phone": c.get("phone"),
                          "job_id": jf.get("job_id"), "total": tot, "recommend": rec})
    json.dump(pairs, open(GATE_PAIRS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(pending, open(GATE_PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"岗位(招聘中)": len(jobs), "简历": len(cands), "达标配对": len(pairs),
                      "达标候选人": len(passed), "待人工判专业": len(pending),
                      "未进任何配对": len(cands) - len(passed)}, ensure_ascii=False))
    print("→ 智能体审 gate_pending.json（专业/工序是否实质对口），把不通过的项从 gate_pairs.json 删除后再 --commit")


def stage_commit(nt):
    pairs = json.load(open(GATE_PAIRS, encoding="utf-8"))
    jobs = {j["fields"].get("job_id"): j for j in
            nt.list_records("job", biz_fields=["job_id", "job_name", "department", "org", "status",
                                               "hard_gates", "must_skills", "bonus_skills",
                                               "must_weight", "bonus_weight"])}
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "skills", "org", "expected_position"]):
        cands[r["id"]] = r["fields"]   # key 用 record id（重名不覆盖），name 列仍取姓名字段
    # 幂等：先删掉这些岗位下的系统匹配旧记录（source 过滤：人工匹配记录不删）
    key = {(p["name"], p["job_id"]) for p in pairs}
    olds = [r["id"] for r in nt.list_records("match", biz_fields=["name", "job_id", "recommend", "source"])
            if r["fields"].get("source") == SYS_SOURCE
            and (r["fields"].get("name"), r["fields"].get("job_id")) in key]
    if olds:
        nt.delete_records("match", olds)
    rows = []
    for p in pairs:
        jf, cf = jobs.get(p["job_id"]), cands.get(p.get("rid"))
        if not jf or not cf:
            continue
        sk, bo, tot, rec, ev = score({"skills": toks(cf.get("skills"))}, jf["fields"])
        # 行组装唯一真源 _match_row（与 match_analyze.apply_ 共用）
        rows.append(_match_row(cf, jf["fields"],
                               {"name": p["name"], "skill_score": sk, "bonus_score": bo,
                                "total_score": tot, "recommend": rec, "evidence": ev}))
    nt.create_records("match", rows)
    print(json.dumps({"created": len(rows), "deleted_old": len(olds)},
                     ensure_ascii=False))


def stage_stats(nt):
    # 统计唯一实现 refresh_job_stats（与 match_analyze.stats 共用）
    n_rows, n_jobs, _dist = refresh_job_stats(nt)
    print(json.dumps({"match_rows": n_rows, "jobs_updated": n_jobs}, ensure_ascii=False))


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不进 preflight、不构造 Notable
        print(__doc__)
        sys.exit(0)
    # stage 0: 环境预检
    run_preflight(config_path=_CONFIG)
    nt = Notable()
    if "--commit" in sys.argv:
        stage_commit(nt)
    elif "--stats" in sys.argv:
        stage_stats(nt)
    else:
        stage_gate(nt, force="--force" in sys.argv)


if __name__ == "__main__":
    main()
