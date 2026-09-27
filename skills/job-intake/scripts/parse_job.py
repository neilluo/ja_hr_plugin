# -*- coding: utf-8 -*-
"""岗位说明书文本/文件名 -> 业务字段。parse(text, filename) -> dict。零第三方依赖。

枚举唯一真源：部门/组织/城市取自 config.json options.job，技能词表取自 shared/vocab.py，
本文件不维护任何清单副本（AGENTS.md 双源零容忍）。
"""
import json
import os
import re
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "shared"))
from vocab import SKILL_WORDS_SORTED  # noqa: E402

CONFIG = json.load(open(os.path.join(_ROOT, "config.json"), encoding="utf-8"))

_DEPT_ENUM = tuple(CONFIG["options"]["job"]["department"])
_DEPT_ALIAS = {"电池设备部": "电池制造部-设备部", "电池工艺部": "电池制造部-工艺部",
               "电池制造部-电池设备部": "电池制造部-设备部"}
_ORG_RE = re.compile("(" + "|".join(map(re.escape, CONFIG["options"]["job"]["org"])) + ")")
_SKIP_TOKEN_RE = re.compile(r"^(附件\d*[:：]?岗位说明书|岗位说明书|说明书|曲靖制造基地|曲靖基地|.*基地)$")
_CITY_RE = re.compile("(" + "|".join(map(re.escape, CONFIG["options"]["job"]["work_location"])) + ")")
_JOB_NAME_RE = re.compile(r"(?:岗位名称|职位名称|职位)\s*(?:Position)?\s*[:：]\s*([^\n：:]{2,40})")
_SEC_PATTERNS = {
    "responsibilities": r"(岗位职责|工作职责|主要工作职责|工作内容|Major responsibilities)",
    "requirements": r"(任职要求|资格条件|任职资格|岗位要求|Qualifications)",
}
_SEC_END_RE = re.compile(
    r"(关键绩效指标|KPI|起草|Draft|审核|批准|工作地点|职位名称|职等|直接上司|下属)")
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
    return [w for w in SKILL_WORDS_SORTED if w.lower() in low]


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
