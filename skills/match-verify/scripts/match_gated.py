# -*- coding: utf-8 -*-
"""match_gated.py — 智能匹配（门槛前置版）：先过硬性门槛，只为达标的人-岗建配对并打分

用法：
    python3 skills/match-verify/scripts/match_gated.py            # 阶段1 门槛判定，输出 gate_pairs.json / gate_pending.json
    python3 skills/match-verify/scripts/match_gated.py --commit   # 阶段2 只建达标配对 + 语义打分 + 连接「关联岗位」
    python3 skills/match-verify/scripts/match_gated.py --stats    # 阶段3 单独一次读取，刷新岗位四项统计

机械门槛（一票否决，缺证据即不通过）：组织一致、学历档次、经验年限、证书、年龄（简历有则判）。
「相关专业 / 工序是否对口」不机械否决，写进 gate_pending.json 由智能体批量判定后再决定去留。
技能同义与上下位关系统一复用 semantic_score 的语义词典，不做 A=A 字面相等。
"""
import sys, os, re, json, time, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "shared"))
sys.path.insert(0, HERE)
from notable import Notable  # noqa: E402
from semantic_score import hit, toks  # noqa: E402  语义词典与命中判定

LEVEL = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}
WS = os.path.dirname(os.path.abspath(__file__))


def seg(g, key):
    m = re.search(key + r"[：:]\s*([^；;]+)", g or "")
    return m.group(1).strip() if m else ""


def txt(v):
    return v.get("markdown") if isinstance(v, dict) else (v or "")


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


def score(cand, jf):
    must, bonus = toks(jf.get("must_skills")), toks(jf.get("bonus_skills"))
    mw, bw = float(jf.get("must_weight") or 0.7), float(jf.get("bonus_weight") or 0.3)
    cs = cand.get("skills") or []
    hm = [n for n in must if hit(cs, n)]
    hb = [n for n in bonus if hit(cs, n)]
    sk = int(round(100 * mw * (len(hm) / len(must)))) if must else 0
    bo = int(round(100 * bw * (len(hb) / len(bonus)))) if bonus else 0
    tot = sk + bo
    rec = "推荐" if tot >= 80 else ("待定" if tot >= 60 else "不推荐")
    miss = [n for n in must if n not in hm]
    ev = "语义匹配：必备%d/%d（%s）；加分%d/%d" % (len(hm), len(must), "、".join(hm[:8]) or "无",
                                              len(hb), len(bonus))
    if miss:
        ev += "；未命中：" + "、".join(miss[:6])
    return sk, bo, tot, rec, ev


def stage_gate(nt):
    jobs = [j["fields"] | {"_id": j["id"]} for j in
            nt.list_records("job", biz_fields=["job_id", "job_name", "department", "org", "status",
                                               "hard_gates", "must_skills", "bonus_skills",
                                               "must_weight", "bonus_weight"]) if
            (j["fields"].get("status") or "招聘中") == "招聘中"]
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "skills", "org", "major"]):
        f = r["fields"]
        cands[f.get("name")] = {"name": f.get("name"), "phone": f.get("phone"),
                                "education": f.get("education"), "years": f.get("years_experience") or 0,
                                "certificates": f.get("certificates"), "skills": toks(f.get("skills")),
                                "major": f.get("major"), "org": f.get("org")}
    pairs, pending, passed = [], [], set()
    min_score = int(os.environ.get("MIN_SCORE", "20"))
    for jf in jobs:
        for c in cands.values():
            cs = c["skills"]
            ok, why, need_major = gate(c, jf, cs)
            if not ok:
                continue
            sk, bo, tot, rec, ev = score({"skills": cs}, jf)
            if tot < min_score:          # 门槛过了但技能几乎无交集 -> 不建废配对
                continue
            passed.add(c["name"])
            if need_major:
                pending.append({"name": c["name"], "job_id": jf.get("job_id"),
                                "job_name": jf.get("job_name"), "major": c.get("major"),
                                "need": seg(txt(jf.get("hard_gates")), "专业")})
            pairs.append({"name": c["name"], "phone": c.get("phone"), "job_id": jf.get("job_id"),
                          "total": tot, "recommend": rec})
    json.dump(pairs, open(os.path.join(WS, "gate_pairs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(pending, open(os.path.join(WS, "gate_pending.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"岗位(招聘中)": len(jobs), "简历": len(cands), "达标配对": len(pairs),
                      "达标候选人": len(passed), "待人工判专业": len(pending),
                      "未进任何配对": len(cands) - len(passed)}, ensure_ascii=False))
    print("→ 智能体审 gate_pending.json（专业/工序是否实质对口），把不通过的项从 gate_pairs.json 删除后再 --commit")


def stage_commit(nt):
    pairs = json.load(open(os.path.join(WS, "gate_pairs.json"), encoding="utf-8"))
    jobs = {j["fields"].get("job_id"): j for j in
            nt.list_records("job", biz_fields=["job_id", "job_name", "department", "org", "status",
                                               "hard_gates", "must_skills", "bonus_skills",
                                               "must_weight", "bonus_weight"])}
    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "phone", "education", "years_experience",
                                                   "certificates", "skills", "org", "expected_position"]):
        f = r["fields"]
        cands[f.get("name")] = f
    # 幂等：先删掉这些岗位下的系统匹配旧记录
    key = {(p["name"], p["job_id"]) for p in pairs}
    olds = [r["id"] for r in nt.list_records("match", biz_fields=["name", "job_id", "recommend", "source"])
            if (r["fields"].get("name"), r["fields"].get("job_id")) in key]
    if olds:
        nt.delete_records("match", olds)
    rows = []
    for p in pairs:
        jf, cf = jobs.get(p["job_id"]), cands.get(p["name"])
        if not jf or not cf:
            continue
        sk, bo, tot, rec, ev = score({"skills": toks(cf.get("skills"))}, jf["fields"])
        rows.append({"job_id": p["job_id"], "name": p["name"], "phone": cf.get("phone"),
                     "job_name": jf["fields"].get("job_name"), "org": jf["fields"].get("org"),
                     "source": "系统匹配", "cand_skills": "、".join(toks(cf.get("skills"))),
                     "must_skills": jf["fields"].get("must_skills"),
                     "bonus_skills": jf["fields"].get("bonus_skills"),
                     "hard_gates": txt(jf["fields"].get("hard_gates")),
                     "expected_position": cf.get("expected_position"),
                     "years_experience": str(cf.get("years_experience") or "无"),
                     "skill_score": sk, "bonus_score": bo, "total_score": tot,
                     "recommend": rec, "evidence": ev, "update_time": int(time.time() * 1000)})
    ids = nt.create_records("match", rows)
    # 关联岗位：需要 recordId，单独 PUT
    back = nt.list_records("match", biz_fields=["name", "job_id", "phone"])
    want = {(r["fields"].get("name"), r["fields"].get("job_id")) for r in back}
    link = [{"id": r["id"], "fields": {"关联岗位": {"linkedRecordIds": [jobs[r["fields"]["job_id"]]["id"]]}}}
            for r in back
            if (r["fields"].get("name"), r["fields"].get("job_id")) in want
            and r["fields"].get("job_id") in jobs]
    for i in range(0, len(link), 10):
        nt.call("PUT", "/v1.0/notable/bases/%s/sheets/%s/records" % (nt.base, nt.sheet("match")),
                {"records": link[i:i + 10]})
    print(json.dumps({"created": len(rows), "linked": len(link), "deleted_old": len(olds)},
                     ensure_ascii=False))


def stage_stats(nt):
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
    print(json.dumps({"match_rows": len(rows), "jobs_updated": len(upd)}, ensure_ascii=False))


def main():
    nt = Notable()
    if "--commit" in sys.argv:
        stage_commit(nt)
    elif "--stats" in sys.argv:
        stage_stats(nt)
    else:
        stage_gate(nt)


if __name__ == "__main__":
    main()
