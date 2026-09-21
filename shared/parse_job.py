# -*- coding: utf-8 -*-
"""岗位说明书文本/文件名 -> 业务字段。parse(text, filename) -> dict。零第三方依赖。"""
import os
import re

_DEPT_ENUM = (
    "技术部", "产品部", "市场部", "运营部", "厂务管理部", "EHS管理部", "财经管理部",
    "单晶制造部-生产部", "单晶制造部-设备部", "数据信息部-系统组",
    "硅片制造部-工艺部", "硅片制造部-设备部", "组件制造部-工艺部", "组件制造部-设备部",
    "电池制造部-工艺部", "电池制造部-设备部", "厂务管理部-暖通组", "厂务管理部-电力能源组",
)
_DEPT_ALIAS = {"电池设备部": "电池制造部-设备部", "电池工艺部": "电池制造部-工艺部",
               "电池制造部-电池设备部": "电池制造部-设备部"}
_ORG_RE = re.compile(r"(制造中心|职能中心|营销中心|研发中心)")
_SKIP_TOKEN_RE = re.compile(r"^(附件\d*[:：]?岗位说明书|岗位说明书|说明书|曲靖制造基地|曲靖基地|.*基地)$")
_CITY_RE = re.compile(r"(北京|上海|深圳|杭州|成都|广州|南京|武汉|曲靖)")
_JOB_NAME_RE = re.compile(r"(?:岗位名称|职位名称|职位)\s*(?:Position)?\s*[:：]\s*([^\n：:]{2,40})")
_SEC_PATTERNS = {
    "responsibilities": r"(岗位职责|工作职责|主要工作职责|工作内容|Major responsibilities)",
    "requirements": r"(任职要求|资格条件|任职资格|岗位要求|Qualifications)",
}
_SEC_END_RE = re.compile(
    r"(关键绩效指标|KPI|起草|Draft|审核|批准|工作地点|职位名称|职等|直接上司|下属)")
_SKILL_WORDS = (
    "暖通", "PLC", "CAD", "电气", "设备运维", "设备管理", "工艺", "切片", "镀膜",
    "组件", "电池", "硅片", "单晶", "拉晶", "EHS", "安全管理", "财务", "成本",
    "预算", "Python", "Java", "SQL", "Excel", "SAP", "MES", "ERP", "自动化",
    "机电", "机械", "焊接", "电工", "TPM", "精益", "SPC", "质量管理", "ISO9001",
    "光伏", "半导体", "扩散", "PECVD", "丝网印刷", "层压", "串焊", "项目管理",
    "会计", "税务", "审计", "报表", "账务", "高压", "特种设备", "消防",
)
_SKILL_SORTED = tuple(sorted(set(_SKILL_WORDS), key=lambda w: (-len(w), w)))
_GATE_RE = re.compile(
    r"[^\n。；;]*(?:学历|大专|本科|硕士|博士|年及以上|年以上|证书|电工证|注册)[^\n。；;]*")


def _split_sections(text):
    """按标题切段，返回 {key: 段文本}。"""
    out = {}
    for key, pat in _SEC_PATTERNS.items():
        m = re.search(pat, text)
        if not m:
            continue
        rest = text[m.end():]
        end = len(rest)
        for stop in list(_SEC_PATTERNS.values()) + [_SEC_END_RE.pattern]:
            em = re.search(stop, rest)
            if em and em.start() > 2:
                end = min(end, em.start())
        out[key] = rest[:end].strip()
    return out


def _parse_filename(filename):
    """返回 (org, department, job_name_tail)。"""
    base = os.path.splitext(filename or "")[0]
    org = _first(_ORG_RE, base) or "制造中心"
    tokens = [t.strip() for t in re.split(r"[-－—]", base) if t.strip()]
    tokens = [t for t in tokens if not _SKIP_TOKEN_RE.match(t) and not _ORG_RE.match(t)]
    dept, i = "", 0
    while i < len(tokens):
        pair = tokens[i] + "-" + tokens[i + 1] if i + 1 < len(tokens) else ""
        if pair in _DEPT_ENUM:
            dept, i = pair, i + 2
        elif pair in _DEPT_ALIAS:
            dept, i = _DEPT_ALIAS[pair], i + 2
        elif tokens[i] in _DEPT_ALIAS:
            dept, i = _DEPT_ALIAS[tokens[i]], i + 1
        elif tokens[i] in _DEPT_ENUM:
            dept, i = tokens[i], i + 1
        else:
            break
    dept_parts = set(dept.split("-"))
    tail = [t for t in tokens[i:] if t not in _DEPT_ENUM and t not in dept_parts]
    job = re.sub(r"\d+$", "", "、".join(tail)).strip()
    return org, dept, job


def _first(regex, text, group=1, default=""):
    m = regex.search(text or "")
    return m.group(group).strip() if m else default


def _skills(text):
    low = (text or "").lower()
    return [w for w in _SKILL_SORTED if w.lower() in low]


def parse(text, filename=""):
    text = text or ""
    org, dept, tail_job = _parse_filename(filename)
    job_name = _first(_JOB_NAME_RE, text) or tail_job
    job_name = re.sub(r"\s+", " ", job_name).strip()[:40]
    sections = _split_sections(text)
    resp = sections.get("responsibilities", "")
    reqs = sections.get("requirements", "")
    must = _skills(reqs) or _skills(text)[:10]
    bonus = []
    for line in reqs.splitlines():
        if "优先" in line:
            for w in _skills(line):
                if w not in must:
                    bonus.append(w)
    bonus = bonus[:8]
    gates = []
    for m in _GATE_RE.findall(reqs or text):
        g = m.strip(" 、，,：:")
        if g and g not in gates:
            gates.append(g)
    loc = _CITY_RE.findall(text)
    work_location = sorted(set(loc), key=loc.index) if loc else ["曲靖"]
    return {
        "job_name": job_name, "department": dept, "org": org, "status": "招聘中",
        "work_location": work_location, "responsibilities": resp[:3000],
        "requirements": reqs[:3000], "hard_gates": gates[:6],
        "must_skills": must[:12], "bonus_skills": bonus,
        "must_weight": 0.7, "bonus_weight": 0.3,
    }
