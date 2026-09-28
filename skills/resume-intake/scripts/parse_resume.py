# -*- coding: utf-8 -*-
"""简历文本 -> 业务字段。parse(text, filename) -> dict。零第三方依赖。

枚举双源零容忍（AGENTS.md）：城市备选来自 config.options.resume.expected_location，
技能词表来自 shared/vocab.SKILL_WORDS_SORTED，分类名以 config.options.resume.category 校验，
院校 985/211 名单来自 config.refs.resume（派生参考数据真源，本地不留副本）。
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "shared"))
from vocab import SKILL_WORDS_SORTED            # noqa: E402

with open(os.path.join(_ROOT, "config.json"), encoding="utf-8") as _f:
    CONFIG = json.load(_f)
_RESUME_OPTS = CONFIG["options"]["resume"]

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_PHONE_DASH_RE = re.compile(r"(?<!\d)(1[3-9]\d)[\s\-](\d{4})[\s\-](\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_LABEL_RE = re.compile(
    r"(?:姓\s*名|Name)\s*[:：]\s*([\u4e00-\u9fff]{2,4}|[A-Za-z\u4e00-\u9fff·]{2,20})")
_YEARS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*年(?:以上)?[^\n。，,；;]{0,12}?经[历验]|工作\s*(\d{1,2})\s*年(?![份级])|(?<!\d)(\d{1,2})\s*年\s*(?:以上|\+|工作)|经[历验]\s*[:：]?\s*(\d{1,2})\s*年(?!\s*(?:毕|届))")
_YEARS_LOOSE_RE = re.compile(r"工作年限\s*[:：]?\s*(\d{1,2})\s*年|(\d{1,2})\s*年\s*\+")
_YEARS_FN_RE = re.compile(r"(?<!\d)(\d{1,2})\s*年(?!\s*(?:毕业|届|底|初|中|份|度|级))")
_SALARY_RE = re.compile(r"(?:期望薪[资酬]|薪[资酬]|月薪|待遇)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[Kk千万]?\s*[-~～至]\s*\d+(?:\.\d+)?\s*[Kk千万]?\s*[元/月]*|面议|[\d.]+\s*[Kk](?!\s*[-~～至]))|(\d+(?:\.\d+)?\s*[-~～]\s*\d+(?:\.\d+)?\s*[Kk])")
# 城市备选唯一源：config.options.resume.expected_location（长词优先防子串抢先命中）
_CITIES = sorted(_RESUME_OPTS["expected_location"], key=len, reverse=True)
_CITY_RE = re.compile(
    r"(?:期望|意向|工作|现居|居住|所在地|城市|地点)[^\n]{0,12}?(" +
    "|".join(re.escape(c) for c in _CITIES) + ")")
# 未命中城市时的兜底枚举值，必须在 config 选项清单内
_LOCATION_FALLBACK = "不限"
if _LOCATION_FALLBACK not in _RESUME_OPTS["expected_location"]:
    raise RuntimeError("兜底城市 %r 不在 config.options.resume.expected_location 中" % _LOCATION_FALLBACK)
_DEGREE_RE = re.compile(r"(?:学\s*历|学位|文化程度)\s*[:：|]?\s*(?:是|为)?\s*(博士|硕士研究生|硕士|研究生|大学本科|统招本科|全日制本科|本科|学士|大专|大学专科|专科|高职)")
_DEGREE_SCAN_RE = re.compile(r"(博士|硕士|研究生|本科|学士|大专|专科|高职|中专)")
# 贪婪 {2,14} + 后缀长词优先：防"贵州财经大学商务学院"在第一个后缀（大学）处被非贪婪截断；
# 贪婪可能带入的动词前缀由 _pick_school 的 _SCHOOL_NOISE_RE 清洗，裸词/黑名单仍走 _SCHOOL_BAD。
_SCHOOL_RE = re.compile(r"([\u4e00-\u9fff]{2,14}(?:职业技术学院|高等专科学校|技师学院|大学|学院))")
_SCHOOL_LABEL_RE = re.compile(r"(?:毕业院校|毕业学校|学校名称|就读学校|院校)\s*[:：|]?\s*([^\n；;，,。|：:]{2,30})")
_SCHOOL_NOISE_RE = re.compile(r"毕业于|就读于|毕业自|结业于|就读|入读|考取|参加|升学|本科|大专|硕士|博士|研究生|中专|高职|全日制|统招|成人")
_SCHOOL_BAD = ("主要参加学院", "主要学院", "宁夏", "学院", "大学", "学校")
_MAJOR_RE = re.compile(r"(?:专\s*业(?:名称)?|所学专业|主修专业|主修)(?:\s*[:：|/｜]\s*|\s*\n\s*|\s+)([\u4e00-\u9fffA-Za-z]{2,20})")
# 表格行兜底：「院校|专业|学历」竖线格式（_MAJOR_RE 无"专业"标签可抓时），
# 命中"哈尔滨远东理工学院|机械设计制造及其自动化|本科"这类行，取中间专业段。
_MAJOR_TABLE_RE = re.compile(r"(?:大学|学院|学校)\s*[|｜/]\s*([\u4e00-\u9fffA-Za-z（）()]{2,20})\s*[|｜/]\s*(?:本科|大专|硕士|博士|研究生|专科)")
_MAJOR_BAD = ("课程", "基础扎实", "培训", "大专", "本科", "知识", "技能", "技术", "能力", "方向", "相关", "学习", "理论")
_NAME_BAD = frozenset((
    "自我评价 专业技能 技能证书 核心优势 优势亮点 姓名 名字 本科 大专 硕士 博士 研究生 专科 "
    "中专 学历 教育背景 工作经历 工作经验 项目经历 项目经验 求职意向 期望岗位 基本信息 个人信息 "
    "联系方式 荣誉证书 获奖情况 培训经历 证书 简介 云南 四川 贵州 广西 湖南 湖北 河南 河北 山东 "
    "山西 陕西 甘肃 宁夏 青海 新疆 西藏 内蒙 辽宁 吉林 广东 江苏 浙江 安徽 福建 江西 海南 北京 "
    "上海 天津 重庆 暖通 电工 焊工 会计 出纳 司机 普工 工程师").split())
_POS_RE = re.compile(r"(?:期望|意向|应聘|求职|目标)\s*(?:岗位|职位|职务|意向)?\s*[:：]\s*([\u4e00-\u9fffA-Za-z/、（）() ]{2,25})")
# 证书词表长词优先（显式交替，不用字符类缩写——`[职称]` 只匹配单字会截断"初级会计职称"；
# `特种作业[证操]?[作证]?` 匹配"特种作业操作证"会丢"证"字）
_CERT_RE = re.compile(r"(注册安全工程师|注册电气工程师|注册消防工程师|建造师|高压电工证|低压电工证|电工证|焊工证|特种作业操作证|特种作业证|特种作业|注册会计师|中级会计师|初级会计(?:职称|资格)?|会计从业|教师资格证|法律职业资格|PMP|CFA|CPA|软考|系统分析师|六级|四级|CET-?\d?)")
# 院校名单唯一真源：config.refs.resume.school_985 / school_211（派生参考数据，非 select 选项），本地不留副本
_985 = tuple(CONFIG["refs"]["resume"]["school_985"])
_211 = tuple(CONFIG["refs"]["resume"]["school_211"])
# 分类→关键词映射是解析逻辑（保留本地）；分类名本身以 config.options.resume.category 为唯一源校验。
_CATS = (("技术类", "工程师 技术 设备 工艺 电气 暖通 研发 维修 PLC 切片 镀膜 电池 组件 硅片 单晶 EHS 安全".split()),
         ("产品类", ("产品", "产品经理", "产品运营")),
         ("市场类", ("市场", "销售", "商务", "营销", "客户")),
         ("运营类", ("运营", "财务", "会计", "成本", "人力", "行政", "供应链", "采购")))
_CATEGORY_FALLBACK = "其他"
_bad = ({c for c, _ in _CATS} | {_CATEGORY_FALLBACK}) - set(_RESUME_OPTS["category"])
if _bad:  # config 是分类枚举唯一真源：映射里的分类名必须都在 config 里，缺则报错人工修
    raise RuntimeError("parse_resume._CATS 分类名不在 config.options.resume.category 中: %s" % sorted(_bad))


def _first(regex, text, group=1):
    m = regex.search(text)
    if not m:
        return ""
    g = m.group(group) if m.groups() and group <= m.re.groups else m.group(0)
    return (g or "").strip()


def _norm_degree(word):
    if not word:
        return ""
    for enum, aliases in (("博士", ("博士",)), ("硕士", ("硕士", "研究生")),
                          ("本科", ("本科", "学士")), ("大专", ("大专", "专科", "高职", "中专"))):
        if any(a in word for a in aliases):
            return enum
    return ""


def _school_rank(school, education):
    for s in _211:  # 长名优先：先扫 211 长校名，防长名被 985 短名子串抢先误判
        if s in (school or ""):
            return "985" if s in _985 else "211"
    if any(s in (school or "") for s in _985):
        return "985"
    if education == "大专":
        return "大专"
    return "普通本科" if education else ""


def _category(position, skills):
    hay = (position or "") + " " + " ".join(skills or [])
    for cat, kws in _CATS:
        if any(k in hay for k in kws):
            return cat
    return _CATEGORY_FALLBACK


def _pick_school(cand):
    """去动词前缀与学历词；裸词/黑名单返回空，否则收敛到含后缀的最短校名。"""
    cand = _SCHOOL_NOISE_RE.sub("", cand or "").strip()
    if not cand or cand in _SCHOOL_BAD:
        return ""
    m = _SCHOOL_RE.search(cand)
    return m.group(1) if m else cand


def _name_from_filename(filename):
    base = os.path.splitext(filename or "")[0]
    base = re.sub(r"[【\[][^】\]]*[】\]]", " ", base)  # 整块剔除【岗位_城市_薪资】前缀
    base = re.sub(r"^(个人简历|简历|附件\d*[:：]?)", "", base).strip("-_－ ")
    for seg in re.split(r"[－\-_—\s]+", base):  # 段序优先，黑名单段跳过
        seg = re.sub(r"^[\d【】\[\]()（）]+|[\d【】\[\]()（）.].*$", "", seg).strip()
        if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", seg or "") and seg not in _NAME_BAD:
            return seg
    return ""


def parse(text, filename=""):
    text = text or ""
    phone = _first(_PHONE_RE, text)
    if not phone:
        m = _PHONE_DASH_RE.search(text)
        phone = "".join(m.groups()) if m else ""
    email = _first(_EMAIL_RE, text)
    name = _first(_NAME_LABEL_RE, text)  # ①"姓名：X" 标签
    if name in _NAME_BAD:
        name = ""
    if not name:  # ②文件名拆段（跳过黑名单/岗位词段）
        name = _name_from_filename(filename)
    if not name:  # ③简历抬头：前几行独立 2-4 个汉字（带黑名单）
        for line in text.splitlines()[:6]:
            cand = re.sub(r"^\s*[-\d个人简历]+\s*", "", line.strip())
            if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cand) and cand not in _NAME_BAD:
                name = cand
                break
    m = _YEARS_RE.search(text) or _YEARS_LOOSE_RE.search(text)
    years = int(next(g for g in m.groups() if g)) if m else None
    if years is None:  # 正文未命中时用文件名「N年」兜底
        m = _YEARS_FN_RE.search(os.path.splitext(filename or "")[0])
        years = int(m.group(1)) if m else None
    salary = _first(_SALARY_RE, text) or _first(_SALARY_RE, text, 2)
    location = _first(_CITY_RE, text) or _LOCATION_FALLBACK
    education = _norm_degree(_first(_DEGREE_RE, text))
    if not education:
        m = _DEGREE_SCAN_RE.search(text)
        education = _norm_degree(m.group(1)) if m else ""
    school = _pick_school(_first(_SCHOOL_LABEL_RE, text) or _first(_SCHOOL_RE, text))
    if not school:  # 候选被黑名单/动词污染剔除后，在净化全文里重试
        school = _pick_school(_first(_SCHOOL_RE, _SCHOOL_NOISE_RE.sub(" ", text)))
    if not education and school:  # 无学历词时按校名后缀兜底推断
        if re.search(r"职业技术学院|技师学院|高等专科|专科学校", school):
            education = "大专"
        elif re.search(r"大学|学院", school):
            education = "本科"
    major = _first(_MAJOR_RE, text) or _first(_MAJOR_TABLE_RE, text)
    if major and (major in _MAJOR_BAD or major.startswith(_MAJOR_BAD)):
        major = ""
    certs = sorted(set(_CERT_RE.findall(text)))
    position = _first(_POS_RE, text)
    # 捕获组字符类含空格，会吞入后续"应聘企业/期望工资/求职类型"标签：
    # 先按空白与标签词切头，再走原有标点切尾；[:40] 容纳多职位顿号列表。
    position = re.split(r"\s+|(?=应聘|期望薪|期望工资|求职类型)", position)[0]
    position = re.sub(r"[：:，,。].*$", "", position)[:40]
    skills = [w for w in SKILL_WORDS_SORTED if w.lower() in text.lower()][:15]
    return {
        "name": name, "phone": phone, "email": email, "education": education,
        "school": school, "school_rank": _school_rank(school, education),
        "years_experience": years, "major": major, "certificates": certs,
        "expected_position": position, "expected_location": location,
        "expected_salary": salary, "skills": skills,
        "category": _category(position, skills),
    }
