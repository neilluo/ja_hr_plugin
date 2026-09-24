# -*- coding: utf-8 -*-
"""sync_match_analysis.py — 由智能体生成每条匹配记录的「AI匹配分析」并回写

分级口径：
  - 推荐 / 待定：逐条人工语义分析（命中亮点、门槛与缺口、建议动作），写在 DETAIL 里；
  - 不推荐：按「命中项 + 未命中项 + 门槛 + 一句判定」生成，判定来自规则表（降配、方向不一致、
    经验不足、行业不对口等），不编造简历里没有的事实。

用法:  python3 skills/match-verify/scripts/sync_match_analysis.py
"""
import sys, os, re, json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable  # noqa: E402

SEP = r"[、,，;；/]\s*"
LEVEL = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}


def toks(s):
    if isinstance(s, list):
        return [x.strip() for x in s if str(x).strip()]
    return [t.strip() for t in re.split(SEP, s or "") if t.strip()]


def hit(cands, need):
    return any(need in c or c in need for c in cands)


def seg(g, key):
    m = re.search(key + r"[：:]\s*([^；;]+)", g or "")
    return m.group(1).strip() if m else ""


# 推荐/待定的人工分析（姓名, 岗位ID） -> 文本
DETAIL = {
("石昊", "J148D2FC4CC"): "【命中】必备11项全覆盖（暖通/空调/排风/冷却水/空压机/废气/预防性维护/成本/团队/安全/台账），同义复核后按11/11计。【门槛】大专+暖通相关专业+5年运维管理均满足。【缺口】加分项缺三标体系与特种设备证。【建议】优先约面，重点核实年降本1400万的项目角色与带队规模；其期望薪资20-30K高于主管带宽，需先对齐。",
("石昊", "J52744C7AD9"): "【命中】必备9/10，仅CAD与部分点检细节缺书面证据。【门槛】1年以上暖通运维远超要求。【定位】其能力与职级对应主管岗，工程师岗属低配，不建议占用该名额。【建议】如主管岗编制受限，可作为工程师岗备选并明确晋升通道。",
("石昊", "J3944760B20"): "【命中】必备7.5/11，供配电、给排水、三标体系为明显空白。【门槛】8年厂务+3年管理满足。【判断】经理岗要求跨专业统筹，其经验集中在暖通与机电安装一侧。【建议】可作为暖通条线负责人培养，暂不直接顶经理岗。",
("徐志伟", "JF05DC6E1AD"): "【命中】必备8/8全中（切片工艺、机型、断线、良率、标准、数据分析、细线化、薄片化）。【门槛】JD允许履历优秀放宽至大专，其10年切片经验满足该条款。【缺口】加分项仅CAD，缺DOE/六西格玛与项目管理证据。【建议】优先约面，用具体断线率与良率改善数据复核其主导程度。",
("李志全", "J898A71A329"): "【命中】必备7.5/8，成本控制靠精益改善间接体现。【门槛】3年以上光伏生产管理满足（隆基、美科、印尼基地）。【加分】产能提升、班组管理、海外建厂均命中。【建议】推荐；需确认其接受曲靖基地常驻与最近一段海外项目结束后的空档期。",
("黄绍华", "JF7BAE97A43"): "【命中】必备7/8（缺作业标准建设），一次合格率与良率数据与岗位KPI同口径。【门槛】大专+2-3年组件工艺满足（5年）。【缺口】专业非材料/化工类，且经验集中在异质结路线。【建议】推荐；面试确认TOPCon版型切换与CTM改善的独立主导能力。",
("代文超", "J93AA873D1A"): "【命中】必备8/8（同义：产线筹建≈产线开线），海外4GW基地筹建经历稀缺。【门槛】大专+2-3年组件设备经验远超。【缺口】期望工作地区为华东，与曲靖基地冲突风险最高。【建议】推荐但先做驻基地意愿与薪资（1.5-1.7万）确认，再进入流程。",
("陆化鹏", "JC6686F6B0D"): "【命中】必备8/8全中，12年单晶/切片/动力设备管理。【门槛】2-3年光伏对应工序设备经验满足。【缺口】加分项电工证、特种设备证、改造收益数据无书面证据。【建议】列为重点候选，面试核实证书与技改项目量化成果后再升级为推荐。",
("陆化鹏", "JC656804DB8"): "【命中】硅片(切片)设备为其曾管辖的全车间设备之一，必备命中8/8。【缺口】机型明细（切片机/插片机）与改造收益缺具体数据。【建议】与单晶设备岗合并考虑，一人一岗，避免同候选人占两个名额。",
("李利波", "JD5A8DA9017"): "【命中】必备6.5/8（同义：电气设计≈电气图纸；施工经验含倒闸操作与巡检），证书最全（高低压电工证+防爆证+中级职称）。【门槛】本科电气+2年经验满足。【缺口】工作票、故障分析无书面记录。【建议】待定→面试通过后推荐；同时可考虑厂务经理岗的电气条线负责人定位。",
("胡裕", "J148D2FC4CC"): "【命中】必备6/11，缺排风、空压机、废气处理与台账管理证据。【门槛】5年设备运维满足；专业不对口按14年经验实质放行。【判断】节能改造方向强，系统覆盖宽度不及石昊。【建议】列为暖通主管第二顺位；若主管岗编制不足，可评估暖通工程师+节能专项双职责岗。",
("汪一兵", "JF7BAE97A43"): "【命中】必备6/8（同义：良率管控≈良率提升），层压/焊接/IV/功率管控均实操。【缺口】一次合格率与作业标准建设无证据；MES背景偏运维。【建议】待定，作为组件工艺工程师储备；本地贵州背景与基地接受度是加分项。",
("孟涛", "J40ECBEC350"): "【命中】制造系统方向强：MES运维、数据采集、SQL、帆软报表、系统运维5项直接对应。【缺口】EAP/WMS、工业以太网、ETL数仓与权限配置均无证据，岗位三个方向中他只完整覆盖一个。【门槛】1-3年经验要求，其7年属超配。【建议】待定，若拆分为制造系统专职岗则可直接推荐。",
("陈志刚", "JC6686F6B0D"): "【命中】单晶炉深度对口+电工焊工证+技能竞赛第一名，必备6/8。【缺口】从业年限未标注，改造与故障案例无量化数据。【建议】待定：先补齐年限与最近三段履历，再决定是否升级推荐。",
}


def build(row, cand, job):
    name = row.get("name")
    jid = row.get("job_id")
    k = (name, jid)
    if k in DETAIL:
        return DETAIL[k]
    cands = toks(cand.get("skills"))
    must = toks(row.get("must_skills"))
    bonus = toks(row.get("bonus_skills"))
    hm = [n for n in must if hit(cands, n)]
    miss = [n for n in must if n not in hm]
    hb = [n for n in bonus if hit(cands, n)]
    gate = row.get("hard_gates") or ""
    g_ed, g_ex, g_ce = seg(gate, "学历"), seg(gate, "经验"), seg(gate, "证书")
    cy = cand.get("years")
    judge = []
    if jid and ("助理工程师" in (row.get("job_name") or "")) and (cy or 0) >= 5:
        judge.append("岗位为助理级、候选人资历明显超配，属降配投递")
    exp_need = min([int(x) for x in re.findall(r"(\d{1,2})\s*年", g_ex)] or [0]) if g_ex else 0
    if exp_need and (cy or 0) < exp_need:
        judge.append("经验%s年低于岗位要求%s年" % (cy, exp_need))
    if cands and not set(cands) & set(must):
        judge.append("技能方向与岗位必备项几乎无交集，属跨方向匹配")
    if not judge:
        judge.append("必备技能覆盖不足，暂列不推荐")
    return "**结论**：不推荐（总分%s）。**命中**：%s。**未命中**：%s。**门槛**：学历%s；经验%s；证书%s。**判定**：%s。" % (
        row.get("total"),
        "、".join(hm[:6]) or "无",
        "、".join(miss[:6]) or "无",
        g_ed or "未设", g_ex or "未设", g_ce or "未设",
        "；".join(judge))


def main():
    cfgp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config.json")
    cfg = json.load(open(cfgp, encoding="utf-8"))
    if "ai_analysis" not in cfg["fields"]["match"]:
        cfg["fields"]["match"]["ai_analysis"] = "AI匹配分析"
        cfg["types"]["match"]["ai_analysis"] = "text"
        json.dump(cfg, open(cfgp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    nt = Notable()
    nt.cfg = cfg  # 复用刚写入的映射，避免新建实例时才加载

    cands = {}
    for r in nt.list_records("resume", biz_fields=["name", "skills", "years_experience", "education"]):
        f = r["fields"]
        cands[f.get("name")] = {"skills": f.get("skills"), "years": f.get("years_experience") or 0,
                                "education": f.get("education")}
    jobs = {j["fields"].get("job_id"): j["fields"] for j in
            nt.list_records("job", biz_fields=["job_id", "must_skills", "bonus_skills", "hard_gates"])}

    rows = nt.list_records("match", biz_fields=["name", "job_id", "job_name", "cand_skills",
                                                "must_skills", "bonus_skills", "hard_gates",
                                                "total_score", "recommend"])
    upd = []
    for r in rows:
        f = r["fields"]
        cand = cands.get(f.get("name")) or jobs and {} or {}
        text = build({"name": f.get("name"), "job_id": f.get("job_id"), "job_name": f.get("job_name"),
                      "must_skills": f.get("must_skills"), "bonus_skills": f.get("bonus_skills"),
                      "hard_gates": f.get("hard_gates"), "total": f.get("total_score")},
                     cand, {})
        upd.append({"id": r["id"], "ai_analysis": text})
    for i in range(0, len(upd), 10):
        nt.update_records("match", upd[i:i + 10])
    detail = sum(1 for u in upd if any(u["ai_analysis"].startswith(x) for x in ("【命中】",)))
    print(json.dumps({"rows": len(upd), "detail_written": detail}, ensure_ascii=False))


if __name__ == "__main__":
    main()
