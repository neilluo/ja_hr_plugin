# -*- coding: utf-8 -*-
"""semantic_score.py — 语义等价打分（严格版）
    python3 skills/match-verify/scripts/semantic_score.py            # 只算不写
    python3 skills/match-verify/scripts/semantic_score.py --write    # 回写

两类关系，避免过度归并：
  SYNONYM 双向等价：只收真正同义/同一事物的不同写法（成本管控=成本控制；良率管控=良率提升）
  HYPERS  单向上下位：候选人写的具体项可满足岗位的宽泛项（单晶炉 ⊨ 光伏设备），反向不算
                  （岗位要"切片机"，候选人只有"单晶炉"不算命中）
"""
import sys, os, re, json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "shared"))
from notable import Notable  # noqa: E402

SEP = r"[、,，;；/]\s*"

SYNONYM = [
    ["成本管控", "成本控制", "降本改善", "降本增效", "费用管控"],
    ["节能改造", "节能降耗", "降耗", "提产降耗"],
    ["团队管理", "班组管理", "带班", "人员管理", "团队建设", "团队带领"],
    ["人员培训", "技能培训", "培训带教", "人才建设", "人员培养"],
    ["预防性维护", "设备保养", "保养维修", "定期检测", "计划保养"],
    ["点检管理", "设备点检", "日常点检", "设备巡检", "巡检", "点巡检"],
    ["设备维修", "设备检修", "维修维护", "故障维修", "电气故障维修", "机械维修"],
    ["故障分析", "异常分析", "故障诊断", "故障排查", "原因分析"],
    ["设备改造", "技改项目", "升级改造", "设备改进", "优化改造", "自动化改造"],
    ["台账管理", "设备台账", "台账归档", "资料归档", "文档管理"],
    ["作业指导书", "SOP建设", "作业标准", "工艺文件", "操作规程", "规程修订", "标准建设"],
    ["生产计划", "排产", "计划排产", "生产调度", "任务分配"],
    ["良率管理", "良率管控", "良率改善", "良率提升", "一次合格率", "质量管控"],
    ["碎片率", "崩边", "不良分析", "缺陷分析"],
    ["设备安装调试", "安装调试", "装机调试", "设备调试", "开线调试"],
    ["产线开线", "开线", "首线开线"],
    ["产线筹建", "建厂", "海外基地建厂", "车间筹建", "前期筹备"],
    ["电气图纸", "电气设计", "图纸审核", "设计提资"],
    ["工作票", "操作票", "两票制度"],
    ["安全管理", "安全生产", "安全管控", "现场安全", "安全技术"],
    ["隐患排查", "隐患治理", "安全隐患排查", "安全检查"],
    ["危险源辨识", "风险辨识", "危险源评价"],
    ["应急管理", "应急预案", "应急演练", "事故演习"],
    ["特种设备管理", "特种设备档案"],
    ["计量校准", "计量体系", "校准计划"],
    ["备件管理", "备品备件", "备件管控", "库存管控"],
    ["5S管理", "6S管理", "现场6S", "7S管理"],
    ["MES运维", "MES", "mes系统", "制造系统运维"],
    ["SQL", "SQL查询", "数据查询"],
    ["帆软报表", "FineReport", "报表开发", "BI报表", "可视化报表"],
    ["数据看板", "看板开发", "驾驶舱", "BI看板"],
    ["CAD", "AutoCAD制图", "工程制图", "制图"],
    ["PLC", "PLC编程", "程序调试", "PLC控制"],
    ["项目管理", "项目主导", "项目统筹", "项目全周期管理", "项目规划"],
    ["跨部门协同", "沟通协调", "部门对接", "跨部门沟通"],
    ["EHS管理", "环境健康安全", "EHS体系"],
    ["数据分析", "工艺数据分析", "数据统计", "数据处理"],
    ["预算", "预算管理", "预算编制", "成本预算"],
    ["稼动率提升", "OEE提升", "开机率提升", "设备效率提升"],
    ["三标体系", "ISO体系", "管理体系建设", "制度体系建设"],
]

# 岗位宽泛项 -> 可满足它的候选人具体项（单向）
HYPERS = {
    "光伏设备": ["单晶炉", "切片机", "插片机", "清洗机", "丝网印刷机", "印刷设备", "镀膜设备", "PECVD",
             "湿法设备", "组件设备", "层压设备", "焊接设备", "IV测试", "扩散", "制绒", "刻蚀", "单晶炉集控"],
    "光伏工艺": ["拉晶工艺", "切片工艺", "组件工艺", "电池工艺", "镀膜工艺", "丝网印刷工艺", "湿法工艺",
             "制绒", "刻蚀", "扩散", "层压", "焊接", "多晶硅清洗", "酸洗", "正背膜"],
    "暖通": ["空调系统", "暖通空调", "转轮除湿机", "高效机房", "温湿度控制"],
    "空调系统": ["转轮除湿机", "高效机房", "暖通", "FFU"],
    "排风系统": ["除尘设备", "废气处理", "新风系统"],
    "废气处理": ["排风系统", "除尘设备", "RTO"],
    "冷却水系统": ["PCW", "空压机", "循环水", "高效机房"],
    "设备管理": ["设备点检", "设备保养", "设备维修", "设备改造", "台账管理", "备件管理", "预防性维护", "检修计划"],
    "设备全生命周期": ["设备选型", "设备维修", "验收", "报废处置", "安装调试"],
    "供配电运维": ["变电站运维", "倒闸操作", "送电", "电气施工管理", "配电室管理"],
    "电气图纸": ["电气设计", "图纸审核"],
    "安全管理": ["安全生产", "三级安全教育", "安全资料归档", "消防管理", "6S管理"],
    "团队管理": ["班组管理", "人员培训", "技能培训", "带班", "绩效考核"],
    "特种作业证": ["高压电工证", "低压电工证", "电工证", "焊工证", "防爆证", "高级电工职业技能证书"],
    "电工证": ["高压电工证", "低压电工证", "高级电工职业技能证书", "特种作业操作证"],
    "系统运维": ["MES运维", "权限配置", "系统监控", "工单处理"],
    "数据采集": ["ETL", "点表调试", "设备联网", "多源异构采集"],
    "财务核算": ["总账处理", "账务处理", "成本核算", "合并报表"],
    "成本核算": ["财务核算", "存货盘点", "成本分析", "账务处理"],
    "生产管理": ["生产计划", "排产", "现场管理", "车间管理", "班组管理"],
    "自动化": ["PLC", "工业机器人", "智能仓储", "伺服"],
}

EQ = {}
for gi, g in enumerate(SYNONYM):
    for w in g:
        EQ.setdefault(w, set()).add(gi)


def toks(s):
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    return [t.strip() for t in re.split(SEP, s or "") if t.strip()]


def hit(cands, need):
    for c in cands:
        if c == need:
            return True
        if need in c or c in need:            # 字面包含（保守：仅同词根）
            return True
        a, b = EQ.get(c, set()), EQ.get(need, set())
        if a and a == b:
            return True
        if c in HYPERS.get(need, []):
            return True
    return False


def main():
    write = "--write" in sys.argv
    nt = Notable()
    jobs = {j["fields"].get("job_id"): j for j in
            nt.list_records("job", biz_fields=["job_id", "must_skills", "bonus_skills",
                                               "must_weight", "bonus_weight"])}
    rows = nt.list_records("match", biz_fields=["name", "job_id", "job_name", "cand_skills",
                                                "total_score", "recommend"])
    upd = []
    for r in rows:
        f = r["fields"]
        j = jobs.get(f.get("job_id"))
        if not j:
            continue
        jf = j["fields"]
        cand = toks(f.get("cand_skills"))
        must, bonus = toks(jf.get("must_skills")), toks(jf.get("bonus_skills"))
        mw, bw = float(jf.get("must_weight") or 0.7), float(jf.get("bonus_weight") or 0.3)
        hm = [n for n in must if hit(cand, n)]
        hb = [n for n in bonus if hit(cand, n)]
        sk = int(round(100 * mw * (len(hm) / len(must)))) if must else 0
        bo = int(round(100 * bw * (len(hb) / len(bonus)))) if bonus else 0
        tot = sk + bo
        rec = "推荐" if tot >= 80 else ("待定" if tot >= 60 else "不推荐")
        if rec != f.get("recommend") or abs(tot - (f.get("total_score") or 0)) >= 1:
            miss = [n for n in must if n not in hm]
            ev = "语义匹配：必备%d/%d（%s）；加分%d/%d" % (len(hm), len(must),
                "、".join(hm[:8]) or "无", len(hb), len(bonus))
            if miss:
                ev += "；未命中：" + "、".join(miss[:6])
            upd.append({"id": r["id"], "skill_score": sk, "bonus_score": bo,
                        "total_score": tot, "recommend": rec, "evidence": ev})
            if rec != f.get("recommend"):
                print("  %-5s %-22s %s→%s  %s→%s" % (f.get("name"), f.get("job_name"),
                      f.get("recommend"), rec, f.get("total_score"), tot))
    print("重算=%d 更新=%d" % (len(rows), len(upd)))
    if not write:
        print("（未写入；--write 生效）")
        return
    for i in range(0, len(upd), 10):
        nt.update_records("match", upd[i:i + 10])
    print("已回写", len(upd))


if __name__ == "__main__":
    main()
