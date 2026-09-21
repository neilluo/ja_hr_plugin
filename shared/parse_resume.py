# -*- coding: utf-8 -*-
"""简历文本 -> 业务字段。parse(text, filename) -> dict。零第三方依赖。"""
import os
import re

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_PHONE_DASH_RE = re.compile(r"(?<!\d)(1[3-9]\d)[\s\-](\d{4})[\s\-](\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_LABEL_RE = re.compile(
    r"(?:姓\s*名|Name)\s*[:：]\s*([\u4e00-\u9fff]{2,4}|[A-Za-z\u4e00-\u9fff·]{2,20})")
_YEARS_RE = re.compile(r"(?<!\d)(\d{1,2})\s*年(?:以上)?[^\n。，,；;]{0,12}?经[历验]|工作\s*(\d{1,2})\s*年(?![份级])|(?<!\d)(\d{1,2})\s*年\s*(?:以上|\+|工作)|经[历验]\s*[:：]?\s*(\d{1,2})\s*年(?!\s*(?:毕|届))")
_YEARS_LOOSE_RE = re.compile(r"工作年限\s*[:：]?\s*(\d{1,2})\s*年|(\d{1,2})\s*年\s*\+")
_YEARS_FN_RE = re.compile(r"(?<!\d)(\d{1,2})\s*年(?!\s*(?:毕业|届|底|初|中|份|度|级))")
_SALARY_RE = re.compile(r"(?:期望薪[资酬]|薪[资酬]|月薪|待遇)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[Kk千万]?\s*[-~～至]\s*\d+(?:\.\d+)?\s*[Kk千万]?\s*[元/月]*|面议|[\d.]+\s*[Kk](?!\s*[-~～至]))|(\d+(?:\.\d+)?\s*[-~～]\s*\d+(?:\.\d+)?\s*[Kk])")
_CITY_RE = re.compile(r"(?:期望|意向|工作|现居|居住|所在地|城市|地点)[^\n]{0,12}?(北京|上海|深圳|杭州|成都|广州|南京|武汉|曲靖|不限)")
_DEGREE_RE = re.compile(r"(?:学\s*历|学位|文化程度)\s*[:：|]?\s*(?:是|为)?\s*(博士|硕士研究生|硕士|研究生|大学本科|统招本科|全日制本科|本科|学士|大专|大学专科|专科|高职)")
_DEGREE_SCAN_RE = re.compile(r"(博士|硕士|研究生|本科|学士|大专|专科|高职|中专)")
_SCHOOL_RE = re.compile(r"([\u4e00-\u9fff]{2,14}?(?:大学|学院|职业技术学院|高等专科学校|技师学院))")
_SCHOOL_LABEL_RE = re.compile(r"(?:毕业院校|毕业学校|学校名称|就读学校|院校)\s*[:：|]?\s*([^\n；;，,。|：:]{2,30})")
_SCHOOL_NOISE_RE = re.compile(r"毕业于|就读于|毕业自|结业于|就读|入读|考取|参加|升学|本科|大专|硕士|博士|研究生|中专|高职|全日制|统招|成人")
_SCHOOL_BAD = ("主要参加学院", "主要学院", "宁夏", "学院", "大学", "学校")
_MAJOR_RE = re.compile(r"(?:专\s*业(?:名称)?|所学专业|主修专业|主修)(?:\s*[:：|]\s*|\s*\n\s*)([\u4e00-\u9fffA-Za-z]{2,20})")
_MAJOR_BAD = ("课程", "基础扎实", "培训", "大专", "本科", "知识", "技能", "技术", "能力", "方向", "相关", "学习", "理论")
_NAME_BAD = frozenset((
    "自我评价 专业技能 技能证书 核心优势 优势亮点 姓名 名字 本科 大专 硕士 博士 研究生 专科 "
    "中专 学历 教育背景 工作经历 工作经验 项目经历 项目经验 求职意向 期望岗位 基本信息 个人信息 "
    "联系方式 荣誉证书 获奖情况 培训经历 证书 简介 云南 四川 贵州 广西 湖南 湖北 河南 河北 山东 "
    "山西 陕西 甘肃 宁夏 青海 新疆 西藏 内蒙 辽宁 吉林 广东 江苏 浙江 安徽 福建 江西 海南 北京 "
    "上海 天津 重庆 暖通 电工 焊工 会计 出纳 司机 普工 工程师").split())
_POS_RE = re.compile(r"(?:期望|意向|应聘|求职|目标)\s*(?:岗位|职位|职务|意向)?\s*[:：]\s*([\u4e00-\u9fffA-Za-z/、（）() ]{2,25})")
_CERT_RE = re.compile(r"(注册安全工程师|注册电气工程师|建造师|电工证|特种作业[证操]?[作证]?|高压电工证|注册会计师|中级会计师|初级会计[职称]?|会计从业|教师资格证|法律职业资格|PMP|CFA|CPA|软考|系统分析师|六级|四级|CET-?\d?)")
_SKILL_WORDS = ("暖通 PLC CAD 电气 设备运维 设备管理 工艺 切片 镀膜 组件 电池 硅片 单晶 拉晶 "
                "EHS 安全管理 财务 成本 预算 Python Java SQL Excel SAP MES ERP 自动化 机电 机械 "
                "焊接 电工 点检 TPM 精益 六西格玛 SPC 质量管理 ISO9001 数据分析 SolidWorks 西门子 "
                "三菱 欧姆龙 变频器 伺服 光伏 半导体 扩散 PECVD 丝网印刷 层压 串焊 排程 项目管理 "
                "团队建设 Office Word PPT 账务 税务 审计 报表 会计 出纳").split()
_SKILL_SORTED = tuple(sorted(set(_SKILL_WORDS), key=lambda w: (-len(w), w)))
_985 = ("清华大学 北京大学 复旦大学 上海交通大学 浙江大学 南京大学 中国科学技术大学 "
        "哈尔滨工业大学 西安交通大学 武汉大学 华中科技大学 中山大学 同济大学 天津大学 "
        "东南大学 北京航空航天大学 北京理工大学 大连理工大学 吉林大学 山东大学").split()
_211 = ("北京邮电大学 华北电力大学 河海大学 江南大学 苏州大学 南京理工大学 南京航空航天大学 "
        "西安电子科技大学 合肥工业大学 郑州大学 南昌大学 太原理工大学 云南大学 贵州大学 "
        "广西大学 西南交通大学 电子科技大学 中南大学 湖南师范大学 长安大学").split()
_CATS = (("技术类", "工程师 技术 设备 工艺 电气 暖通 研发 维修 PLC 切片 镀膜 电池 组件 硅片 单晶 EHS 安全".split()),
         ("产品类", ("产品", "产品经理", "产品运营")),
         ("市场类", ("市场", "销售", "商务", "营销", "客户")),
         ("运营类", ("运营", "财务", "会计", "成本", "人力", "行政", "供应链", "采购")))


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
    for s in _211:  # 长名优先，防「西安电子科技大学」被 985 短名误判
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
    return "其他"


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
    location = _first(_CITY_RE, text) or "不限"
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
    major = _first(_MAJOR_RE, text)
    if major and (major in _MAJOR_BAD or major.startswith(_MAJOR_BAD)):
        major = ""
    certs = sorted(set(_CERT_RE.findall(text)))
    position = _first(_POS_RE, text)
    position = re.sub(r"[：:，,。].*$", "", position)[:25]
    skills = [w for w in _SKILL_SORTED if w.lower() in text.lower()][:15]
    return {
        "name": name, "phone": phone, "email": email, "education": education,
        "school": school, "school_rank": _school_rank(school, education),
        "years_experience": years, "major": major, "certificates": certs,
        "expected_position": position, "expected_location": location,
        "expected_salary": salary, "skills": skills,
        "category": _category(position, skills), "full_text": text[:5000],
    }
