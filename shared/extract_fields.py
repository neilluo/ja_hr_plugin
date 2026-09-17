#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / extract_fields.py
=====================================================

纯正则 / 启发式字段抽取层。**零第三方依赖**，只用标准库。

对外契约（构建契约 §3.1，签名已冻结，不得改动）
------------------------------------------------
    extract_resume_fields(text: str, file_name: str) -> dict
    extract_job_fields(text: str, file_name: str) -> dict

设计要点（针对前序调研暴露的系统性缺陷逐条修）
--------------------------------------------
1. **姓名必须有文件名兜底**（任务书要求）。客户简历文件名高度规律
   （`姓名-岗位.pdf` / `个人简历-姓名-方向.docx` / `【岗位_地点 薪资】姓名 年限.pdf` /
   `岗位-姓名.docx`），比 PDF 文本层可靠得多：
     - `林云-电池设备.pdf` 正文里是 `林之府`（PDF 字体伪影）→ 文件名兜底纠正
     - `姓名：徐志伟性别：男`（无分隔符粘连）→ 正文模式会误抽 `徐志伟性`
     - `姓 名：许金 措施；`（双栏 PDF 串行）→ 正文模式会误抽 `许金措施`
   策略：文件名候选与正文候选**双向校验**，冲突时以文件名为准并在 warnings 里留痕。
2. **手机号优先用带数字边界断言的严格正则**。前序调研「去掉边界断言后 100%」
   的结论建立在 pdfplumber 被污染的文本上（strip-all-tags 会把 XML 属性里的
   长数字串漏进正文，例如 `5118106325870`、`-13081007195820`）。本层配合
   extract_text 的干净提取后，严格正则在 28/28 份有效简历上全中；仍保留
   「去边界断言」作为二级兜底，并把置信度降为 low。
3. **证书必须区分「持证者优先」与硬性门槛**（任务书要求，会造成误杀）。
   `cert_is_preferred_not_required=True` 表示检出的证书**全部**出现在
   「…者优先 /（优先）/ 加分 / 更佳」语境里，上层不得当硬性门槛用。
4. **技能列表必须多分隔符切分并去重去空**（任务书要求，会造成打分分母=1、
   命中即 100 分虚高）。`split_items()` 按 `、，,;；/ 空格 •·|` 等切分，
   JD 侧与简历侧共用同一个切分器。
5. **PDF 字体伪影归一化**：部分 PDF 文本层用「康熙部首 / CJK 部首补充」码位
   代替汉字（`⾦⽓⾃⼤⻘⻛⻓⻋⻔⻆⻄`），实测 50 份样本里 144 处。
   先做 NFKC（修掉 133 处），再用补充映射表修掉剩余 11 处。不归一化会直接
   漏掉 `最高学历：⼤专` 这类字段。
6. **邮箱缺 @ 的原文错字做保守修复**：`邮箱：1873259717qq.com`（原件 XML 里
   @ 确实不存在，count=0）→ 仅在「本地部分是 5 位以上数字 + 已知邮箱服务商
   域名」时补 @，并把该字段置信度标 low。不做其它臆测。

兼容性：D10 —— 全程 pathlib；语法兼容 python 3.8+（不用 match、不用运行时
`X | None`，只用 typing.Optional）。已在 3.9.6 与 3.14.0 实测。
"""

from __future__ import annotations  # noqa: F404

import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

__all__ = [
    "extract_resume_fields",
    "extract_job_fields",
    "split_items",
    "name_from_filename",
    "normalize_text",
]

# =========================================================================== #
# 0. 文本归一化
# =========================================================================== #
# NFKC 覆盖不到的 CJK 部首补充码位 -> 对应简化汉字（实测样本里出现过 7 种，
# 这里给出完整的「C-SIMPLIFIED」系列，客户 PDF 字体千奇百怪，宁可多映射）
_RADICAL_FIX = {
    "\u2EA6": "丬", "\u2EB0": "纟", "\u2EBE": "艹", "\u2EBF": "艹",
    "\u2EC0": "艹", "\u2EC4": "西", "\u2EC5": "见", "\u2EC6": "角",
    "\u2EC8": "讠", "\u2EC9": "贝", "\u2ECB": "车", "\u2ECC": "辶",
    "\u2ED0": "钅", "\u2ED3": "长", "\u2ED4": "门", "\u2ED7": "雨",
    "\u2ED8": "青", "\u2ED9": "韦", "\u2EDA": "页", "\u2EDB": "风",
    "\u2EDC": "飞", "\u2EE0": "饣", "\u2EE2": "马", "\u2EE3": "骨",
    "\u2EE4": "鬼", "\u2EE5": "鱼", "\u2EE6": "鸟", "\u2EE7": "卤",
    "\u2EE8": "麦", "\u2EE9": "黄", "\u2EEA": "黾", "\u2EEC": "齐",
    "\u2EEE": "齿", "\u2EF0": "龙", "\u2EF1": "龟",
}
_RADICAL_RE = re.compile("[" + "".join(_RADICAL_FIX) + "]")

# 全角空格 / 不换行空格 -> 普通空格
_SPACE_FIX = {
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2007": " ",
    "\u2009": " ", "\u200a": " ", "\u202f": " ", "\u205f": " ",
    "\u3000": " ", "\u200b": "", "\u200c": "", "\u200d": "",
    "\u200e": "", "\u200f": "", "\ufeff": "",
}
_SPACE_RE = re.compile("[" + "".join(_SPACE_FIX) + "]")


def normalize_text(text: str) -> str:
    """统一 PDF/Word 文本层的字体伪影与空白字符。

    NFKC 会把康熙部首（U+2F00-2FD5）还原成汉字，也会把全角数字/字母还原成
    半角；`_RADICAL_FIX` 补 NFKC 覆盖不到的 CJK 部首补充区。
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _RADICAL_RE.sub(lambda m: _RADICAL_FIX[m.group(0)], t)
    t = _SPACE_RE.sub(lambda m: _SPACE_FIX[m.group(0)], t)
    return t


# =========================================================================== #
# 1. 通用切分器（JD 侧与简历侧共用；修「逗号分隔被并成 1 条」缺陷）
# =========================================================================== #
_ITEM_SEP_RE = re.compile(r"[、，,;；/|·•‧∙●○◦▪（）()\t\r\n]+|\s{1,}")
_ITEM_SEP_HARD_RE = re.compile(r"[、，,;；/|·•‧∙●○◦▪（）()\t\r\n]+")
_SENTENCE_SEP_RE = re.compile(r"[。；;！!？?\n]+")


def split_items(raw: Optional[str],
                min_len: int = 2,
                max_len: int = 40,
                max_items: int = 40,
                split_space: bool = False,
                drop_stop: bool = True) -> List[str]:
    """把一段技能/证书描述切成条目列表：多分隔符切分 + 去重 + 去空 + 限长。

    split_space=False（默认）时不把单个空格当分隔符——中文里空格常只是排版，
    切开会把「熟练使用 Office 办公软件」拆成碎片。JD 的技能条目实测主要用
    `，、；` 分隔，不需要按空格切。
    """
    if not raw:
        return []
    sep = _ITEM_SEP_RE if split_space else _ITEM_SEP_HARD_RE
    out: List[str] = []
    seen: Set[str] = set()
    for chunk in sep.split(raw):
        item = _clean_item(chunk)
        if not item:
            continue
        if len(item) < min_len or len(item) > max_len:
            continue
        if drop_stop and _is_filler_item(item):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


_FILLER_ITEMS = {
    "等", "等等", "其他", "其它", "以及", "并且", "并且等", "如", "例如", "如下",
    "以上", "以下", "等能力", "等技能", "无", "暂无", "/", "-", "—", "、",
}
_FILLER_PREFIX = ("等", "及其他", "以及其他", "以及", "或", "比如")


# 自评/能力动词前缀：剥掉之后剩下的才是真正的技能名
# （`精通对应工序工艺原理` -> `对应工序工艺原理`，`熟练使用Office办公软件` -> `Office办公软件`）
_ITEM_VERB_PREFIX_RE = re.compile(
    r"^(?:能够|可以|熟练|精通|掌握|熟悉|了解|具备|擅长|善于|具有|拥有|负责|承担|参与|"
    r"主导|能|会)(?:使用|运用|应用|操作|掌握|进行)?\s*"
    r"(?:较强的|良好的|扎实的|丰富的|一定的|较强的|出色的|优秀的)?\s*")
# 明显不是技能条目的东西：句子（带冒号）、时间量词、自评尾巴
_ITEM_NOISE_RE = re.compile(
    r"(\d\s*年|\d\s*个月|\d+\s*岁|[:：]|经验$|意识$|精神$|态度$|责任心$|责任感$|"
    r"^\d+$|^\d{4}[-./年])")


def _clean_item(s: str) -> str:
    s = (s or "").strip()
    s = s.strip(" \t\r\n、，,;；.。:：·-—~()（）[]【】\"'“”‘’")
    s = re.sub(r"^\d{1,2}\s*[、.．)）]\s*", "", s)      # 去掉 "1、" "2." 编号
    s = re.sub(r"^[（(]\s*\d+\s*[)）]\s*", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) > 4 and s.endswith("等"):                 # 「…能力等」-> 「…能力」
        s = s[:-1].strip()
    stripped = _ITEM_VERB_PREFIX_RE.sub("", s).strip("，,、 ")
    if len(stripped) >= 4:                              # 剥完还剩下东西才剥
        s = stripped
    return s


def _is_filler_item(item: str) -> bool:
    if item in _FILLER_ITEMS:
        return True
    if _ITEM_NOISE_RE.search(item):
        return True
    for p in _FILLER_PREFIX:
        if item.startswith(p):
            return True
    # 纯标点 / 纯数字 / 单个字母
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", item):
        return True
    return False


# =========================================================================== #
# 2. 学历 / 学校 / 学校层次
# =========================================================================== #
# 归一到契约枚举：博士|硕士|本科|大专|None
_DEGREE_LEVELS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("博士", ("博士",)),
    ("硕士", ("硕士", "研究生", "MBA", "EMBA", "MPA")),
    ("本科", ("本科", "学士", "大学本科", "双一流本科")),
    ("大专", ("大专", "专科", "高职", "大学专科", "高专", "中专", "中职",
              "技工学校", "技校", "高中")),
)
_DEGREE_SCAN_RE = re.compile(
    r"(博士|硕士研究生|硕士|研究生|大学本科|本科|学士|大专|大学专科|专科|"
    r"高职|高专|中专|中职|技工学校|技校|高中)"
)
_DEGREE_LABEL_RE = re.compile(
    r"(?:最高学历|最终学历|学历层次|学历学位|文化程度|学\s*历|最高学位|学位)"
    r"\s*[:：|]?\s*(?:是|为)?\s*"
    r"(博士|硕士研究生|硕士|研究生|大学本科|统招本科|全日制本科|本科|学士|"
    r"大专|大学专科|专科|高职|高专|中专|中职|技工学校|技校|高中)"
)


def _degree_to_enum(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    w = word.strip()
    for enum, aliases in _DEGREE_LEVELS:
        for a in aliases:
            if a in w:
                return enum
    return None


_SCHOOL_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z]{2,18}?"
    r"(?:大学|学院|学校|职业技术学院|职业技术学校|高等专科学校|专科学校|中等专业学校|"
    r"技师学院|高级技工学校))"
)
_SCHOOL_LABEL_RE = re.compile(
    r"(?<![\u4e00-\u9fff])"
    r"(?:毕业院校|毕业学校|学校名称|就读学校|院校名称|学\s*校|院\s*校|母校)"
    r"\s*[:：|]?\s*(?:是|为)?\s*([^\n；;，,。|]{2,30})"
)

# 985（39 所）
_SCHOOLS_985 = (
    "北京大学 清华大学 中国人民大学 北京航空航天大学 北京理工大学 中国农业大学 "
    "北京师范大学 中央民族大学 南开大学 天津大学 大连理工大学 东北大学 吉林大学 "
    "哈尔滨工业大学 复旦大学 同济大学 上海交通大学 华东师范大学 南京大学 东南大学 "
    "浙江大学 中国科学技术大学 厦门大学 山东大学 中国海洋大学 武汉大学 华中科技大学 "
    "湖南大学 中南大学 中山大学 华南理工大学 四川大学 重庆大学 电子科技大学 "
    "西安交通大学 西北工业大学 兰州大学 国防科技大学 西北农林科技大学"
).split()
# 211（非 985 部分）
_SCHOOLS_211 = (
    "北京交通大学 北京工业大学 北京科技大学 北京化工大学 北京邮电大学 北京林业大学 "
    "北京中医药大学 北京外国语大学 中国传媒大学 中央财经大学 对外经济贸易大学 "
    "北京体育大学 中国政法大学 华北电力大学 中国矿业大学 中国石油大学 中国地质大学 "
    "河北工业大学 太原理工大学 内蒙古大学 辽宁大学 大连海事大学 延边大学 东北师范大学 "
    "哈尔滨工程大学 东北农业大学 东北林业大学 华东理工大学 东华大学 上海外国语大学 "
    "上海财经大学 上海大学 苏州大学 南京航空航天大学 南京理工大学 中国药科大学 "
    "河海大学 江南大学 南京农业大学 南京师范大学 安徽大学 合肥工业大学 福州大学 "
    "南昌大学 郑州大学 武汉理工大学 华中农业大学 华中师范大学 中南财经政法大学 "
    "湖南师范大学 暨南大学 华南师范大学 广西大学 海南大学 西南交通大学 西南大学 "
    "西南财经大学 贵州大学 云南大学 西藏大学 西北大学 西安电子科技大学 长安大学 "
    "陕西师范大学 青海大学 宁夏大学 新疆大学 石河子大学 空军军医大学 海军军医大学 "
    "第二军医大学 第四军医大学"
).split()
# 双一流（2017/2022 新增，非 985/211）
_SCHOOLS_DOUBLE_FIRST_CLASS = (
    "中国科学院大学 上海科技大学 南方科技大学 华南农业大学 广州医科大学 南京医科大学 "
    "湘潭大学 河南大学 山西大学 南京邮电大学 南京信息工程大学 上海海洋大学 "
    "上海中医药大学 首都师范大学 外交学院 中国人民公安大学 北京协和医学院 "
    "中央美术学院 中国美术学院 中央戏剧学院 上海体育学院 上海音乐学院 中国音乐学院 "
    "天津工业大学 宁波大学 西南石油大学 成都理工大学 成都中医药大学 广州中医药大学 "
    "首都医科大学 南京林业大学 中国美术学院 空军工程大学 陆军工程大学"
).split()
# 常见简称 -> 层次
_SCHOOL_ALIAS = {
    "清华": "985", "北大": "985", "浙大": "985", "复旦": "985", "交大": "985",
    "西交": "985", "哈工大": "985", "华中科大": "985", "华科": "985",
    "中山大": "985", "川大": "985", "重大": "985", "成电": "985",
    "电子科大": "985", "兰大": "985", "南开": "985", "天大": "985",
    "西电": "211", "北邮": "211", "北交": "211", "北科": "211",
    "华北电力": "211", "河海": "211", "江南大学": "211", "苏大": "211",
    "南航": "211", "南理工": "211", "合肥工大": "211", "郑大": "211",
    "武理工": "211", "华农": "211", "华师": "211", "中南财经": "211",
    "西南交大": "211", "云大": "211", "贵大": "211", "宁夏大": "211",
    "国科大": "双一流", "中科院": "双一流", "南邮": "双一流", "南信大": "双一流",
}


def school_rank(school: Optional[str], education: Optional[str],
                text: str = "") -> Optional[str]:
    """按 985 > 211 > 双一流 > 普通本科 > 大专 的优先级判层次。

    契约枚举：985|211|双一流|普通本科|大专|None。
    """
    hay = school or ""
    hit = None
    if hay:
        # **最长匹配优先**：`西安电子科技大学` 里含有 985 校名 `电子科技大学`，
        # 若按 985 先判会误升档；211 里的全名更长，必须让它赢。
        best_len = 0
        for rank, table in (("985", _SCHOOLS_985), ("211", _SCHOOLS_211),
                            ("双一流", _SCHOOLS_DOUBLE_FIRST_CLASS)):
            for s in table:
                if s and s in hay and len(s) > best_len:
                    best_len = len(s)
                    hit = rank
        if hit is None:
            best_len = 0
            for alias, rank in _SCHOOL_ALIAS.items():
                if alias in hay and len(alias) > best_len:
                    best_len = len(alias)
                    hit = rank
    if hit is None and text and re.search(r"985\s*(?:工程|院校|高校|重点)", text):
        hit = "985"
    if hit is None and text and re.search(r"211\s*(?:工程|院校|高校|重点)", text):
        hit = "211"
    if hit is None and text and re.search(r"双一流(?:建设|高校|大学|学科)?", text):
        hit = "双一流"
    if hit:
        return hit
    if education == "本科":
        return "普通本科"
    if education == "大专":
        return "大专"
    return None


# =========================================================================== #
# 3. 证书 / 技能词表
# =========================================================================== #
CERT_KEYWORDS: Tuple[str, ...] = (
    # 财会
    "注册会计师", "CPA", "高级会计师", "中级会计师", "初级会计师", "会计从业资格证",
    "中级会计职称", "初级会计职称", "高级会计职称", "会计职称",
    "注册税务师", "税务师", "中级经济师", "初级经济师", "高级经济师", "资产评估师",
    "审计师", "CMA", "ACCA",
    # 安全 / EHS
    "注册安全工程师", "注册消防工程师", "安全员证", "安全生产知识和管理能力考核合格证",
    "安全管理人员证", "特种设备安全管理和作业人员证", "体系内审员", "职业健康负责人",
    "CCAA温室气体核查员", "CSR企业社会责任内审员", "注册审核员",
    "危险化学品作业人员证", "特种作业证书",
    # 工程 / 建造
    "一级建造师", "二级建造师", "注册电气工程师", "注册公用设备工程师", "造价工程师",
    "监理工程师", "注册咨询工程师", "注册环保工程师", "注册结构工程师",
    "注册暖通工程师", "注册给排水工程师",
    # 特种作业
    "特种作业操作证", "低压电工证", "高压电工证", "电工证", "焊工证", "叉车证",
    "锅炉证", "制冷证", "高处作业证", "登高证", "起重证", "压力容器证",
    "变电值班员证", "调度证", "进网作业许可证", "特种设备作业人员证",
    # 职称
    "高级工程师职称", "中级职称", "高级职称", "初级职称", "助理工程师职称",
    "技师证", "高级技师", "工程师职称",
    # 体系 / 质量
    "ISO9001内审员", "ISO14001内审员", "ISO45001内审员", "内审员", "六西格玛黑带",
    "六西格玛绿带", "PMP", "项目管理专业人士",
    # 语言 / IT / 其他
    "英语四级", "CET-4", "CET4", "英语六级", "CET-6", "CET6", "计算机二级",
    "计算机三级", "软考", "系统集成项目管理工程师", "教师资格证", "人力资源管理师",
    "证券从业资格", "基金从业资格", "驾驶证", "C1驾驶证", "A2驾驶证", "B2驾驶证",
    "健康证",
)
def _det_sort_long_first(words) -> Tuple[str, ...]:
    """长度倒序 + 同长按字典序 —— **必须是全序**。

    只写 `sorted(set(X), key=len, reverse=True)` 会踩坑：同长度的元素之间保持
    set 的迭代顺序，而 str 的哈希默认按进程随机化（PYTHONHASHSEED），
    于是 certificates / skills 的**输出顺序在不同进程之间会变**，
    契约验收 3「连续 3 次运行结果一致」直接挂掉（实测已复现）。
    """
    return tuple(sorted(set(words), key=lambda w: (-len(w), w)))


# 长词优先，保证「注册安全工程师」先于「安全工程师」、「中级会计师」先于「会计师」
_CERT_KW_SORTED: Tuple[str, ...] = _det_sort_long_first(CERT_KEYWORDS)

SKILL_VOCAB: Tuple[str, ...] = (
    # ---- 光伏工序 / 工艺 ----
    "单晶", "拉晶", "铸锭", "开方", "切片", "磨面", "倒角", "清洗", "制绒", "绒面",
    "扩散", "刻蚀", "去磷硅", "镀膜", "PECVD", "ALD", "退火", "离子注入", "丝网印刷",
    "烧结", "测试", "分选", "EL检测", "IV测试", "组件", "层压", "焊接", "装框",
    "电池", "硅片", "硅棒", "石英坩埚", "良率提升", "工艺优化", "参数优化",
    "工艺管控", "工序", "少子寿命", "转换效率", "功率档位", "隐裂", "虚焊",
    # ---- 设备 ----
    "单晶炉", "扩散炉", "氧化炉", "PECVD设备", "丝网印刷机", "层压机", "串焊机",
    "空压机", "冷却塔", "水泵", "风机", "变频器", "伺服", "传感器", "减速机",
    "真空泵", "冷水机", "锅炉", "配电柜", "变压器", "断路器", "UPS", "制氮机",
    "纯水系统", "废气处理", "除尘", "设备管理", "设备维护", "设备维修", "设备点检",
    "设备保养", "故障排查", "故障诊断", "预防性维护", "技改", "设备选型", "安装调试",
    "稼动率", "备件管理", "台账", "设备全生命周期", "运维",
    # ---- 电气 / 厂务 ----
    "供配电", "倒闸操作", "继电保护", "电力电子", "电机拖动", "高低压", "0.4kV",
    "10kV", "35kV", "110kV", "电气安装", "电气调试", "电力系统", "节能降耗",
    "暖通", "空调系统", "制冷", "给排水", "消防系统", "压缩空气", "特气",
    "化学品供应", "厂务", "动力设备", "能源管理",
    # ---- 管理方法 / 体系 ----
    "TPM", "5S", "6S", "ISO9001", "ISO14001", "ISO45001", "ISO16949", "IATF16949",
    "SPC", "FMEA", "DOE", "8D", "MSA", "六西格玛", "精益生产", "PDCA", "KPI",
    "SOP", "作业指导书", "QC七大手法", "QCC", "全面质量管理", "质量体系",
    "EHS", "安全管理", "隐患排查", "危险源辨识", "应急管理", "事故管理",
    "职业健康", "安全生产", "环保", "职业病防治",
    "项目管理", "团队管理", "人员管理", "班组管理", "成本管控", "预算管理",
    "采购", "招标", "供应商管理", "产能提升", "生产计划", "生产调度",
    "现场管理", "标准化", "数据分析", "报表", "根因分析", "整改",
    # ---- 软件 / IT ----
    "Office", "Excel", "Word", "PowerPoint", "PPT", "AutoCAD", "CAD",
    "SolidWorks", "UG", "ProE", "CATIA", "SAP", "MES", "EAP", "WMS", "ERP",
    "OA", "PLC", "PLC编程", "西门子PLC", "三菱PLC", "组态", "WinCC", "触摸屏",
    "HMI", "SCADA", "DCS", "SQL", "Python", "Java", "C++", "JavaScript",
    "BI", "ETL", "数据库", "Linux", "网络配置", "服务器", "虚拟化",
    "numpy", "pandas", "sklearn", "selenium", "playwright", "爬虫", "机器学习",
    "数据清洗", "数据可视化", "Photoshop", "Project", "Visio", "金蝶", "用友",
    # ---- 财务 ----
    "成本核算", "财务核算", "会计凭证", "财务报表", "财务预算", "费用报销",
    "纳税申报", "税务筹划", "资金管理", "应收账款", "固定资产", "存货监盘",
    "成本分析", "内控", "审计", "总账", "合并报表", "财务分析", "预算管理",
    "降本增效", "结算",
)
_SKILL_VOCAB_SORTED: Tuple[str, ...] = _det_sort_long_first(SKILL_VOCAB)

# =========================================================================== #
# 4. 姓名
# =========================================================================== #
# 文件名里不可能是人名的词（岗位 / 行业 / 地名 / 模板词）
_NAME_STOPWORDS: Set[str] = set("""
个人简历 简历 求职简历 求职 意向 应聘 岗位 职位 基本信息 个人信息 基本资料
自我评价 自我推荐 个人评价 教育经历 教育背景 工作经历 工作经验 项目经历 项目经验
专业技能 技能特长 荣誉奖项 证书 联系方式 培训经历 核心优势 优势亮点 求职意向
工程师 高级工程师 助理工程师 主管 经理 总监 专员 助理 会计 出纳 班长 组长
技师 操作工 技术员 技术工 主任 厂长 部长 总裁 秘书 顾问 讲师 教师
工艺 设备 暖通 财务 财经 电力 电气 单晶 电池 组件 硅片 切片 镀膜 拉晶
生产 制造 质量 安全 环保 厂务 动力 数据 信息 系统 软件 硬件 网络 运维
行政 人事 采购 物流 仓储 销售 市场 客服 法务 审计 成本 预算 项目
曲靖 昆明 云南 江苏 贵州 湖北 四川 宁夏 甘肃 山东 河南 浙江 安徽 湖南 江西
福建 广东 广西 陕西 山西 河北 辽宁 吉林 新疆 西藏 青海 海南 重庆 天津 上海
北京 深圳 杭州 苏州 无锡 常州 镇江 绵阳 荆州 中卫 银川 西宁 大理 商丘 阜阳
毕节 平凉 牟定 贵阳 遵义 徐州 兰州 西安 焦作 盐城 阜宁 哈尔滨 长春 沈阳
济南 青岛 宁波 温州 福州 厦门 南昌 郑州 武汉 长沙 广州 东莞 佛山 中山 珠海
惠州 南宁 海口 成都 太原 全国 制造基地 基地 高新区 开发区 工业园 附件
毕业 年限 以上 以下 应届 社招 校招 内推 最新 修改 定稿 副本 模板 男 女
姓名 名字 性别 民族 年龄 电话 手机 邮箱 学历 学校 专业 籍贯 户籍 住址 地址
政治 面貌 婚姻 状况 身高 体重 出生 年月 工作 经验 技能 特长 证书 荣誉 奖项
求职 意向 期望 薪资 城市 地点 岗位 职位 自我 评价 推荐 基本 信息 资料 个人
简历 教育 经历 项目 培训 总结 教训 联系 方式 现居 目前 状态 离职 在职 沟通
""".split())

_SEP_SPLIT_RE = re.compile(r"[【】\[\]()（）\-－—–_＿·•、，,。.\s/\\|:：;；~～!！?？]+")
_CJK_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_JOB_WORD_RE = re.compile(
    r"(工程师|主管|经理|总监|专员|助理|会计|出纳|班长|组长|技师|操作工|技术员|"
    "主任|厂长|部长|秘书|顾问|工艺|设备|暖通|财务|财经|电力|电气|单晶|电池|组件|"
    "硅片|切片|镀膜|拉晶|生产|制造|质量|安全|环保|厂务|动力|数据|信息|系统|软件|"
    "运维|行政|人事|采购|物流|销售|市场|简历|意向|岗位|职位|毕业|应届|附件|说明书)")
_YEAR_IN_NAME_RE = re.compile(r"(19|20)\d{2}|\d{1,2}\s*(年|岁|月|K|k|W|w)")


def _plausible_person_name(tok: str) -> bool:
    """判断一个 token 像不像人名（2-4 个汉字，且不是岗位/地名/模板词）。"""
    if not tok or not _CJK_NAME_RE.match(tok):
        return False
    if tok in _NAME_STOPWORDS:
        return False
    if _JOB_WORD_RE.search(tok):
        return False
    if _YEAR_IN_NAME_RE.search(tok):
        return False
    # 单个 token 里出现「省/市/县/区/部/厂/院/校/司」等机构后缀 -> 不是人名
    if re.search(r"[省市县区部厂院校司局处科室组]$", tok):
        return False
    return True


def name_from_filename(file_name: str) -> Optional[str]:
    """从文件名抽姓名。客户简历文件名格式高度规律，实测 29/31 可解析。

    支持：`姓名-岗位.pdf` / `姓名_岗位.pdf` / `姓名 岗位.pdf` /
    `岗位-姓名.docx` / `个人简历-姓名-方向.docx` /
    `【岗位_地点 薪资】姓名 年限.pdf` / `姓名 23年毕业-设备助工.pdf`
    """
    if not file_name:
        return None
    stem = Path(file_name).name
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem)      # 去扩展名
    stem = unicodedata.normalize("NFKC", stem)
    # 1) 【...】之后的部分优先（客户 HR 转发的命名规范）
    bracket = re.search(r"[】\]]\s*(.+)$", stem)
    if bracket:
        after = bracket.group(1).strip()
        m = re.match(r"([\u4e00-\u9fff]{2,4})", after)
        if m and _plausible_person_name(m.group(1)):
            return m.group(1)
    # 2) 去掉前缀模板词
    stem = re.sub(r"^(附件\s*\d*\s*[：:]?\s*)", "", stem)
    stem = re.sub(r"^(个人简历|求职简历|简历|应聘简历)\s*[-－—_＿·]?\s*", "", stem)
    # 3) 按分隔符切 token，取第一个像人名的
    for tok in _SEP_SPLIT_RE.split(stem):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"([\u4e00-\u9fff]{2,4})", tok)
        if m and _plausible_person_name(m.group(1)):
            return m.group(1)
    # 4) 最后再从整串里找孤立的人名 token
    for m in re.finditer(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])", stem):
        if _plausible_person_name(m.group(1)):
            return m.group(1)
    return None


_NAME_LABEL_RE = re.compile(
    r"(?:姓\s*名|名\s*字|应聘者|候选人|应聘人)\s*[:：|]?\s*"
    r"([\u4e00-\u9fff](?:\s*[\u4e00-\u9fff]){1,3})"
)
_NAME_SELFINTRO_RE = re.compile(r"我\s*(?:叫|是)\s*([\u4e00-\u9fff]{2,4})")
_NAME_TITLE_LINE_RE = re.compile(
    r"^(?:个人简历|求职简历|简历|应聘简历|求职书|RESUME)\s*[-－—_＿·:：]?\s*"
    r"([\u4e00-\u9fff]{2,4})\s*$", re.M | re.I)


# 「姓名后面紧跟的标签词」——修剪掉它是**强信号**（说明粘连的确实是标签）
_NAME_TAIL_LABEL_2: Set[str] = set("""
性别 民族 出生 电话 联系 邮箱 电子 籍贯 学历 年龄 政治 身高 体重 住址 地址
求职 意向 婚姻 状况 健康 毕业 专业 户籍 现居 目前 应聘 手机 微信 个人 基本
任职 职级 岗位 邮编 家庭 最高 开始 照片 头像 编号
""".split())
# 实测碰到过、但不是标签的词（如双栏 PDF 串行导致的 `许金措施`）——修剪掉它是
# **弱信号**，需要文件名或别处佐证才敢给 high 置信度
_NAME_TAIL_WORD_2: Set[str] = set("""
措施 日期 学校 院校 籍贯
""".split())
# 单字修剪：只在候选长度=4 时用（`徐志伟性` -> `徐志伟`），弱信号
_NAME_TAIL_STOP_1: Set[str] = set(
    "性 民 出 电 联 邮 籍 学 年 政 身 住 求 意 婚 毕 专 户 现 目 工 期 应 手 "
    "微 基 个 简 职 编 日 措 照 头".split())


def _trim_name_candidate(cand: str) -> Tuple[str, str]:
    """修剪 `徐志伟性` -> `徐志伟`、`孟涛性别` -> `孟涛`、`许金措施` -> `许金`。

    返回 (修剪后的候选, 修剪强度 ""|"strong"|"weak")。
    """
    strength = ""
    if len(cand) >= 4 and cand[-2:] in _NAME_TAIL_LABEL_2:
        cand, strength = cand[:-2], "strong"
    elif len(cand) == 4 and cand[-1] in _NAME_TAIL_STOP_1:
        cand, strength = cand[:-1], "weak"
    elif len(cand) >= 4 and cand[-2:] in _NAME_TAIL_WORD_2:
        cand, strength = cand[:-2], "weak"
    if len(cand) >= 4 and cand[-2:] in _NAME_TAIL_LABEL_2:
        cand, strength = cand[:-2], strength or "strong"
    return cand, strength


def _dedupe_doubled(s: str) -> str:
    """修 pdfplumber 类文本层的叠字伪影：`代代文文超超` -> `代文超`。"""
    if len(s) >= 4 and len(s) % 2 == 0:
        half = s[:len(s) // 2]
        if "".join(c * 2 for c in half) == s:
            return half
    return s


def name_from_text(text: str) -> Tuple[Optional[str], str, str]:
    """返回 (姓名, 命中方式, 修剪强度 ""|strong|weak)。

    命中方式: label（有「姓名：」标签）/ selfintro / title / firstline / ""
    """
    if not text:
        return None, "", ""
    head = text[:1200]
    # 1) 「姓名：X」标签
    for m in _NAME_LABEL_RE.finditer(head):
        raw = re.sub(r"\s+", "", m.group(1))
        raw = _dedupe_doubled(raw)
        cand, strength = _trim_name_candidate(raw)
        if len(cand) >= 2 and cand not in _NAME_STOPWORDS:
            return cand, "label", strength
    # 2) 「我叫 X」
    m = _NAME_SELFINTRO_RE.search(head)
    if m:
        return m.group(1), "selfintro", ""
    # 3) 标题行 `个人简历-张三`
    m = _NAME_TITLE_LINE_RE.search(head)
    if m and _plausible_person_name(m.group(1)):
        return m.group(1), "title", ""
    # 4) 前 8 行里的孤立人名 token（`崔银亮` / `田海飞` / `W O 石昊 W O`）
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
    for ln in lines:
        if len(ln) > 60:
            continue
        for m in re.finditer(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])", ln):
            if ln[m.end():m.end() + 1] in (":", "："):
                continue                  # `电话：` `期望薪资：` 这种是标签不是人名
            cand = _dedupe_doubled(m.group(1))
            if _plausible_person_name(cand):
                return cand, "firstline", ""
    return None, "", ""


# =========================================================================== #
# 5. 电话 / 邮箱
# =========================================================================== #
_PHONE_STRICT_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PHONE_LOOSE_RE = re.compile(r"1[3-9]\d{9}")
_PHONE_KW_RE = re.compile(r"(电话|手机|联系方式|移动电话|Tel|TEL|Phone|微信|号码)")
_PHONE_DIGIT_GAP_RE = re.compile(r"(?<=\d)[ \-－—](?=\d)")

_EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9][A-Za-z0-9.\-]{1,63}\.[A-Za-z]{2,10}")
# 原件漏写 @ 的保守修复：本地部分为 5+ 位数字，域名是已知邮箱服务商
_EMAIL_MISSING_AT_RE = re.compile(
    r"(?<![A-Za-z0-9@._%+\-])(\d{5,12})\s*"
    r"(qq|QQ|foxmail|163|126|139|gmail|sina|sohu|yeah|hotmail|outlook|aliyun|189|wo)\s*"
    r"\.\s*(com|cn|net|com\.cn)",
)
_KNOWN_DOMAIN_TAIL = ("com", "cn", "net", "org", "com.cn", "edu.cn")
# 常见 PDF 字体伪影域名修复（实测 pdfplumber 出过 `46449614@qqV.ctom`）
_DOMAIN_ARTIFACT = (
    ("qqv.ctom", "qq.com"), ("qqv.com", "qq.com"), ("qq.ctom", "qq.com"),
    ("qq.corn", "qq.com"), ("qq.con", "qq.com"), ("qqv.cn", "qq.cn"),
    ("163.ctom", "163.com"), ("163.corn", "163.com"), ("126.ctom", "126.com"),
    ("foxmail.ctom", "foxmail.com"), ("gmail.ctom", "gmail.com"),
)


def extract_phone(text: str) -> Tuple[Optional[str], str]:
    """返回 (手机号, 置信度 high|low|"")。

    ① 严格正则（带数字边界断言）+ 分隔符归一：干净文本层下实测 28/28 全中。
    ② 关键词邻近优先：同一份文本里有多个候选时，选「前面 14 字内出现
       电话/手机/联系方式」的那个。
    ③ 去掉数字边界断言兜底：应对 PDF 把手机号与相邻数字粘连
       （`186017761571B`、`15555835562289430775`），此时置信度降为 low。
    """
    if not text:
        return None, ""
    norm = _PHONE_DIGIT_GAP_RE.sub("", text)

    def _pick(regex: re.Pattern) -> Optional[str]:
        cands = list(regex.finditer(norm))
        if not cands:
            return None
        for m in cands:
            ctx = norm[max(0, m.start() - 14):m.start()]
            if _PHONE_KW_RE.search(ctx):
                return m.group(0)
        return cands[0].group(0)

    hit = _pick(_PHONE_STRICT_RE)
    if hit:
        return hit, "high"
    hit = _pick(_PHONE_LOOSE_RE)
    if hit:
        return hit, "low"        # 可能是从长数字串里截出来的，需人工确认
    return None, ""


def extract_email(text: str) -> Tuple[Optional[str], str]:
    """返回 (邮箱, 置信度 high|low|"")。

    high = 原文直接匹配到合法邮箱（必要时修一下 pdfplumber 的域名伪影）；
    low  = 原件漏写 @（如 `1873259717qq.com`），按「数字本地部分 + 已知服务商域名」
           保守补全，需人工确认。
    """
    if not text:
        return None, ""
    m = _EMAIL_RE.search(text)
    if m:
        addr = m.group(0)
        low = addr.lower()
        for bad, good in _DOMAIN_ARTIFACT:
            if low.endswith(bad):
                addr = addr[:len(addr) - len(bad)] + good
                return addr, "high"
        if low.endswith(_KNOWN_DOMAIN_TAIL):
            return addr, "high"
        return addr, "low"
    m = _EMAIL_MISSING_AT_RE.search(text)
    if m:
        return "%s@%s.%s" % (m.group(1), m.group(2).lower(), m.group(3).lower()), "low"
    return None, ""


# =========================================================================== #
# 6. 分段（sections）
# =========================================================================== #
_RESUME_SECTION_HEADS: Tuple[Tuple[str, str], ...] = (
    ("cert_text", r"(资格证书|职业资格证书|职业证书|技能证书|证书情况|获得证书|证书|职称)"),
    ("education_text", r"(教育经历|教育背景|学历背景|教育情况|学习经历|受教育情况|学历信息|教育程度|教育)"),
    ("work_text", r"(工作经历|工作经验|职业经历|从业经历|任职经历|工作履历|项目经历|项目经验|实习经历|履历|主要工作)"),
    ("skill_text", r"(专业技能|个人技能|技能特长|技能技巧|技术能力|专业能力|核心能力|核心优势|优势亮点|技能|特长)"),
)
_JD_SECTION_HEADS: Tuple[Tuple[str, str], ...] = (
    ("education_text", r"(教育背景|学历要求|教育要求)"),
    ("cert_text", r"(培训经历|证书要求|职业证书要求|资格要求)"),
    ("work_text", r"(从业经验|工作经验|经验要求)"),
    ("skill_text", r"(技能技巧|技能要求|能力要求)"),
    ("kpi_text", r"(关键绩效指标|KPI)"),
    ("responsibility_text", r"(主要工作职责|工作职责|次要工作职责|岗位职责)"),
)
_SECTION_NOISE_RE = re.compile(r"^[\d一二三四五六七八九十]{1,3}\s*[、.．)）]?\s*")
# 标题允许带一点尾巴（`专业技能及专业知识` / `教育经历（2016-2020）`），
# 但仍要求整行 <= 14 字，避免把正文里出现的「专业技能」当标题。
_SECTION_TAIL = r"(?:[及与和、（(]?[\u4e00-\u9fffA-Za-z0-9（）()\-~～\s]{0,8})?"


def slice_sections(text: str, heads: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """把文本按标题行切成 {key: 正文}。找不到标题就返回空串（不硬造）。

    标题必须**独占一行**（或行首），否则正文里提到的「专业技能」「工作经历」
    会被误当标题，把段落切碎。
    """
    marks: List[Tuple[int, int, str]] = []
    pos = 0
    for line in text.splitlines():
        stripped = line.strip()
        core = _SECTION_NOISE_RE.sub("", stripped).strip().rstrip("：:")
        core = re.sub(r"\s*[/／|｜]\s*[A-Za-z][A-Za-z\s/&().]*$", "", core).strip()
        if core and len(core) <= 20:
            for key, pat in heads:
                if re.fullmatch(pat + _SECTION_TAIL, core):
                    marks.append((pos, pos + len(line), key))
                    break
        pos += len(line) + 1
    out: Dict[str, str] = {}
    for i, (start, line_end, key) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[line_end:end].strip()
        out[key] = (out[key] + "\n" + body).strip() if out.get(key) else body
    for key, _pat in heads:
        out.setdefault(key, "")
    return out


# =========================================================================== #
# 7. 工作年限
# =========================================================================== #
_CURRENT_YEAR = __import__("datetime").date.today().year
# `开始工作时间：2018.4` / `参加工作时间：2012` -> 用当前年份减，实测 4/4 与文件名/正文一致
_START_WORK_RE = re.compile(
    r"(?:开始工作时间|参加工作时间|首次工作时间|入职时间|工作时间)\s*[:：|]?\s*"
    r"(?:[A-Za-z]{0,4})?\s*((?:19|20)\d{2})")
_YEARS_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"工作\s*年限\s*[:：|]?\s*(\d{1,2})"),
    re.compile(r"(?:工作|从业|相关|行业)?\s*经验\s*[:：|]?\s*(\d{1,2})\s*年"),
    re.compile(r"(\d{1,2})\s*年\s*(?:以上|及以上)?\s*(?:工作|从业|相关|行业)?\s*经验"),
    re.compile(r"工作\s*(\d{1,2})\s*年"),
    re.compile(r"(\d{1,2})\s*年\s*(?:以上|及以上)?\s*(?:的)?\s*(?:工作|从业|管理|相关)"),
    re.compile(r"(\d{1,2})\s*\+\s*年"),
    re.compile(r"有\s*(\d{1,2})\s*年"),
)
# 招聘网站的「基本信息」行：`男 | 大专 | 10年以上 | 37(1989年03月)`
# 这种裸 `N年/N年以上` 只在「同时出现性别 + 学历」的行里才敢当年限用，置信度记 low。
_BASIC_INFO_TOKEN_RE = re.compile(r"(男|女)")
_BASIC_INFO_DEGREE_RE = re.compile(r"(博士|硕士|本科|大专|专科|中专|中职|高中|学士)")
_BARE_YEAR_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*年(?:以上|及以上)?(?![龄月日代毕])")
_DATE_RANGE_RE = re.compile(
    r"(?:19|20)\d{2}\s*[.\-/年。~]?\s*\d{0,2}\s*月?\s*[-~—－–至到]\s*"
    r"(?:(?:19|20)\d{2}\s*[.\-/年。~]?\s*\d{0,2}|至今|现在|今)")
_COMPANY_LINE_RE = re.compile(r"(有限公司|股份公司|股份|集团|公司|工厂|制造基地|基地|研究院|事务所)")
_JOB_TITLE_LINE_RE = re.compile(
    r"(班长|组长|主管|经理|总监|专员|助理|技师|技术员|操作工|主任|厂长|部长|秘书|"
    r"顾问|工程师|科员|职员|讲师|教师|会计|出纳|管理员|运维|设计|开发)")


def extract_years(text: str) -> Tuple[Optional[int], str]:
    """显式声明的工作年限。返回 (年数, 置信度 high|low|"")。找不到返回 (None, "")。"""
    if not text:
        return None, ""
    head = text[:1800]
    m = _START_WORK_RE.search(head) or _START_WORK_RE.search(text)
    if m:
        try:
            n = _CURRENT_YEAR - int(m.group(1))
        except ValueError:
            n = -1
        if 0 <= n <= 50:
            return n, "high"
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(head):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 0 < n <= 50:
                return n, "high"
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 0 < n <= 50:
                return n, "low"
    # 招聘网站「基本信息」块里的裸年限。字段可能全挤在一行
    # （`男 | 大专 | 10年以上 | 37(1989年03月)`），也可能一字段一行
    # （`男`\n`大专`\n`10年以上`\n`37(1989年03月)`），所以按 ±4 行窗口找
    # 「性别 + 学历」上下文，避免把项目年限/设备年限当成工龄。
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}\s*年(?:以上|及以上)?", line) \
                and not _BARE_YEAR_RE.fullmatch(line):
            continue
        win = "\n".join(lines[max(0, i - 4):i + 5])
        if not _BASIC_INFO_TOKEN_RE.search(win) or not _BASIC_INFO_DEGREE_RE.search(win):
            continue
        m2 = _BARE_YEAR_RE.search(line)
        if not m2:
            continue
        try:
            n = int(m2.group(1))
        except ValueError:
            continue
        if 0 < n <= 50:
            return n, "low"
    return None, ""


def estimate_years_from_dates(text: str) -> Optional[int]:
    """兜底估算：从**含公司名的行**里的日期区间算总跨度（最早起始 -> 最晚结束）。

    只用公司行，避免把教育经历的日期算进工龄。结果只作为 low 置信度兜底，
    调用方必须在 warnings 里说明来源。
    """
    if not text:
        return None
    starts: List[int] = []
    ends: List[int] = []
    for line in text.splitlines():
        if not _DATE_RANGE_RE.search(line):
            continue
        # 工作条目行 = 含公司名，或「日期区间 + 岗位名」（`2014.4-2018.3 银川隆基 生产班长`）
        if not _COMPANY_LINE_RE.search(line) and not _JOB_TITLE_LINE_RE.search(line):
            continue
        for m in _DATE_RANGE_RE.finditer(line):
            years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", m.group(0))]
            if not years:
                continue
            start = years[0]
            if re.search(r"(至今|现在)", m.group(0)):
                end = 2026
            elif len(years) > 1:
                end = years[-1]
            else:
                continue
            if 1970 <= start <= 2030 and start <= end <= 2030:
                starts.append(start)
                ends.append(end)
    if not starts:
        return None
    span = max(ends) - min(starts)
    return span if 0 < span <= 45 else None


def estimate_years_from_section(text: str) -> Optional[int]:
    """更弱的兜底：直接用**工作经历分段内**的所有日期区间算跨度。

    只在 `estimate_years_from_dates` 拿不到结果时用，且调用方必须传入已切好的
    work_text 分段（不能传全文，否则会把教育经历的日期算进工龄）。
    """
    if not text:
        return None
    starts: List[int] = []
    ends: List[int] = []
    for m in _DATE_RANGE_RE.finditer(text):
        years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", m.group(0))]
        if not years:
            continue
        start = years[0]
        end = _CURRENT_YEAR if re.search(r"(至今|现在)", m.group(0)) else (
            years[-1] if len(years) > 1 else None)
        if end is None:
            continue
        if 1970 <= start <= 2030 and start <= end <= 2030:
            starts.append(start)
            ends.append(end)
    if not starts:
        return None
    span = max(ends) - min(starts)
    return span if 0 < span <= 45 else None


# =========================================================================== #
# 8. 简历字段抽取
# =========================================================================== #
_EDU_LABEL_LINE_RE = re.compile(
    r"(?:最高学历|最终学历|学历层次|学历学位|文化程度|学\s*历|最高学位)\s*[:：|]?\s*"
    r"(?:是|为)?\s*([^\n；;，,。|]{0,24})")
_MAJOR_LABEL_RE = re.compile(
    r"(?:所学专业|主修专业|所修专业|专\s*业)\s*[:：|]?\s*(?:是|为)?\s*"
    r"([^\n；;。|]{2,40})")
# `专业` 后面跟的词若属于这些，说明这不是「所学专业」而是散文（专业技能/专业知识/
# 专业综合试验/专业课程设计…），必须跳过，否则误抽率极高。
_MAJOR_BAD_FOLLOW = re.compile(
    r"^(知识|技能|团队|课程|理论|能力|水平|素养|方向|技术|基础|背景|相关|领域|壁垒|"
    r"积淀|特长|成绩|综合|试验|实习|实践|设计|提升|发展|要求|素质|功底|对口|不符|符合|"
    r"一致|学习|工作|任职|岗位|职业|资历|经验|方面|上|中|内|领域|优势|知识|"
    r"培训|班长|组长|主任|职称|资格|荣誉|奖项)")
_EDU_ENTRY_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*[.\-/年。~]?\s*\d{0,2}\s*月?\s*[-~—－–至到]\s*"
    r"(?:(?:19|20)\d{2}|至今|现在|今)")
_SCHOOL_SUFFIX = (r"(?:大学|学院|学校|职业技术学院|职业技术学校|高等专科学校|专科学校|"
                  r"中等专业学校|技师学院|高级技工学校|商学院)")
# 贪婪前缀：`贵州财经大学商务学院` 必须整体命中，懒惰前缀会在 `贵州大学` 处截断
_SCHOOL_FULL_RE = re.compile(r"[\u4e00-\u9fffA-Za-z]{2,18}" + _SCHOOL_SUFFIX)
_SCHOOL_TOKEN_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z（）()]{2,22}?" + _SCHOOL_SUFFIX + r"$")
# 散文里的「就读于长江大学农业学院，在校学习期间…」会被 token 化成一个「像校名」的
# 长串，必须按动词/介词开头拒掉
_SCHOOL_BAD_PREFIX_RE = re.compile(
    r"^(就读|毕业于|毕业|曾|在|于|从|到|考入|进入|升学|升|至|经|由|自|我|本|"
    r"主要|参加|通过|负责|具备|熟悉|掌握|擅长|参与|期间|先后|历任|获得)")
_DEGREE_WORD_RE = re.compile(
    r"(博士|硕士研究生|硕士|研究生|大学本科|本科|学士|大学专科|大专|专科|高职|高专|"
    r"中专|中职|技工学校|技校|高中)")
_MAJOR_SUFFIX_RE = re.compile(
    r"(工程|技术|科学|管理|自动化|会计|金融|计算机|软件|物理|化学|冶金|材料|电子|信息|"
    r"数学|设计|制造|医学|护理|法学|文学|教育|物流|电子商务|数控|机电|电气|机械)$")
_MAJOR_BAD_TOKEN_RE = re.compile(
    r"(大学|学院|学校|中学|小学|职业技术|工程师|主管|助理|经理|总监|班长|组长|主任|"
    r"有限公司|科技|集团|公司|隆基|晶澳|协鑫|天合|润阳|基本信息|个人简历|简历|"
    r"荣誉|奖项|证书|主修|课程|毕业|在校|学生|至今|现在|赛事|宣传|安排|培训|实习|"
    r"住址|户籍|籍贯|电话|邮箱|性别|民族|政治|身高|体重|婚姻)")
_SKIP_LINE_RE = re.compile(r"^(主修课程|专业课程|主要课程|课程|荣誉|获奖|证书)")
_TOKEN_SPLIT_RE = re.compile(r"[\s|｜/、，,]+")
_STRAY_ASCII_RE = re.compile(r"(?<=[\u4e00-\u9fff])[A-Za-z](?=[\u4e00-\u9fff])")


def _strip_major_token(tok: str) -> str:
    tok = (tok or "").strip()
    tok = re.sub(r"[（(][^）)]{0,14}[)）]\s*$", "", tok)       # 去掉尾部（本科）
    tok = re.sub(r"(等相关专业|相关专业|专业|等)\s*$", "", tok).strip()
    tok = _STRAY_ASCII_RE.sub("", tok)                          # PDF 插进的孤立字母
    return tok.strip("、，,|｜/ 。.:：")


def _looks_like_major(tok: str) -> bool:
    if not tok or not (2 <= len(tok) <= 24):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,24}", tok):
        return False
    if _MAJOR_BAD_TOKEN_RE.search(tok):
        return False
    if _DEGREE_WORD_RE.fullmatch(tok):
        return False
    if tok in _NAME_STOPWORDS:
        return False
    return True


def _degree_rank(word: Optional[str]) -> int:
    """博士=0 硕士=1 本科=2 大专=3；未知=9。"""
    e = _degree_to_enum(word)
    if not e:
        return 9
    for i, (enum, _a) in enumerate(_DEGREE_LEVELS):
        if enum == e:
            return i
    return 9


def _find_school_in_token(tok: str) -> Optional[str]:
    """从一个 token 里挑出学校名。

    token 可能是 `2011.8-2014.7东营科技职业学院`（日期与校名之间没有空格），
    所以允许校名前面是一段纯日期/数字；但 `就读于长江大学农业学院` 这种散文
    开头必须拒掉。
    """
    if not tok:
        return None
    if _SCHOOL_TOKEN_RE.match(tok) and not _SCHOOL_BAD_PREFIX_RE.match(tok):
        return tok
    m = _SCHOOL_FULL_RE.search(tok)
    if not m:
        return None
    pre = tok[:m.start()]
    if pre and not re.fullmatch(r"[\d.\-/年~至到\s]+", pre):
        return None
    post = tok[m.end():]
    # 校名必须是整个 token（或只被日期前缀），否则是散文：
    # `主要参加学院的金工实训` 里能搜出 `主要参加学院`
    if post and re.match(r"[\u4e00-\u9fffA-Za-z]", post):
        return None
    cand = m.group(0)
    if _SCHOOL_BAD_PREFIX_RE.match(cand):
        return None
    return cand


def parse_education_block(text: str, edu_section: str,
                          exclude: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """把教育经历解析成 [{school, major, degree, degree_enum, rank, line}]。

    **条目行必须含学校名或学历词**——不能只凭日期区间。否则工作经历行
    （`2025.3-2025.8 印度尼西亚 现场工艺高级工程师`）会被当成教育条目，
    把「印度尼西亚」抽成专业（实测踩过）。
    专业兜底：条目行前后 3 行内、长度 <=20、不含公司/岗位词的短行，
    当作该条目的专业（应对 docx 表格里 日期/专业/学校 各占一行的排版，
    以及 `辽宁石油化工大学 本科 2010/09-2014/06` + 次行 `电气工程及其自动化专业`）。
    """
    entries: List[Dict[str, Any]] = []
    scopes = [s for s in (edu_section, text) if s and s.strip()]
    seen_lines: Set[str] = set()
    for scope in scopes:
        lines = [ln.strip() for ln in scope.splitlines() if ln.strip()]
        local: List[Dict[str, Any]] = []
        for idx, ln in enumerate(lines):
            if _SKIP_LINE_RE.match(ln) or ln in seen_lines:
                continue
            toks = [t for t in _TOKEN_SPLIT_RE.split(ln) if t]
            has_date = bool(_EDU_ENTRY_DATE_RE.search(ln))
            school = None
            degree = None
            for t in toks:
                if school is None:
                    school = _find_school_in_token(t)
                if degree is None:
                    dm = _DEGREE_WORD_RE.search(t)
                    if dm and len(t) <= 20:
                        degree = dm.group(1)
            if not (school or degree):
                continue                      # 只有日期 -> 是工作经历，不是教育条目
            if len(ln) > 90:
                continue                      # 太长的行是散文
            major = None
            for t in toks:
                if school and (t == school or school in t):
                    continue
                if degree and _DEGREE_WORD_RE.fullmatch(t):
                    continue
                if has_date and re.search(r"(?:19|20)\d{2}", t):
                    continue
                cand = _strip_major_token(t)
                if cand in exclude:
                    continue
                if _looks_like_major(cand):
                    major = cand
                    break
            seen_lines.add(ln)
            local.append({"school": school, "major": major, "degree": degree,
                          "degree_enum": _degree_to_enum(degree),
                          "rank": _degree_rank(degree), "line": ln, "_idx": idx})
        # 专业兜底：前后 3 行内找一个「像专业」的孤立短行
        for e in local:
            if e["major"] or not e["school"]:
                continue
            for j in range(max(0, e["_idx"] - 3), min(len(lines), e["_idx"] + 4)):
                if j == e["_idx"]:
                    continue
                cand_line = lines[j]
                if len(cand_line) > 20 or _COMPANY_LINE_RE.search(cand_line):
                    continue
                if _EDU_ENTRY_DATE_RE.fullmatch(cand_line):
                    continue
                cand = _strip_major_token(cand_line)
                if cand and cand not in exclude and _looks_like_major(cand):
                    e["major"] = cand
                    break
        for e in local:
            e.pop("_idx", None)
        entries.extend(local)
        if entries:
            break
    return entries


def _extract_education(text: str, edu_section: str,
                       entries: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], str]:
    """返回 (归一化学历, 学历原文, 置信度)。取**最高**学历。"""
    m = _EDU_LABEL_LINE_RE.search(edu_section or "") or _EDU_LABEL_LINE_RE.search(text)
    if m:
        raw = m.group(1).strip()
        dm = _DEGREE_SCAN_RE.search(raw)
        if dm:
            enum = _degree_to_enum(dm.group(1))
            if enum:
                return enum, (raw[:24] or dm.group(0)), "high"
    best_enum: Optional[str] = None
    best_rank = 99
    raws: List[str] = []
    for e in entries:
        if e["degree"]:
            raws.append(e["degree"])
        if e["rank"] < best_rank:
            best_rank = e["rank"]
            best_enum = e["degree_enum"]
    if best_enum:
        return best_enum, "、".join(dict.fromkeys(raws))[:40], "high"
    # 全文兜底：只给 low 置信度
    for scope in (edu_section, text[:2500], text):
        if not scope:
            continue
        found = [x.group(1) for x in _DEGREE_SCAN_RE.finditer(scope)]
        if found:
            for enum, _aliases in _DEGREE_LEVELS:
                if any(_degree_to_enum(w) == enum for w in found):
                    return enum, "、".join(dict.fromkeys(found))[:40], "low"
    return None, None, ""


def _extract_school(text: str, edu_section: str,
                    entries: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], str, List[str]]:
    """返回 (学校, 置信度, 全部检出学校)。

    取「最高学历对应的那所学校」（并列时取最后一次出现，通常是最近的学历）。
    显式 `毕业院校/学校名称` 标签优先级最高。
    """
    allschools: List[str] = []
    for e in entries:
        if e["school"] and e["school"] not in allschools:
            allschools.append(e["school"])
    label_school: Optional[str] = None
    for m in _SCHOOL_LABEL_RE.finditer(text):
        cand = m.group(1).strip().strip("：:|，,。 ")
        sm = _SCHOOL_FULL_RE.search(cand)
        if sm:
            label_school = sm.group(0)
            break
        # 没有学校后缀的候选（`毕业院校 ：宁夏` + 下一行 `大学` 这种双栏串行）
        # 只有在完全没有条目学校时才敢用，且只给 low
        if (label_school is None and not allschools and 4 <= len(cand) <= 24
                and not _MAJOR_BAD_TOKEN_RE.search(cand)
                and not _MAJOR_BAD_FOLLOW.match(cand)):
            label_school = cand
    if label_school and not allschools:
        return label_school, ("high" if _SCHOOL_FULL_RE.fullmatch(label_school) else "low"), [label_school]
    if allschools:
        best = None
        best_rank = 99
        for e in entries:
            if e["school"] and e["rank"] <= best_rank:
                best_rank = e["rank"]
                best = e["school"]
        return (best or allschools[0]), "high", allschools
    for ln in (edu_section or "").splitlines():
        ln = ln.strip()
        if not ln or len(ln) > 40:
            continue
        for t in [x for x in _TOKEN_SPLIT_RE.split(ln) if x]:
            if _SCHOOL_TOKEN_RE.match(t) and not _SCHOOL_BAD_PREFIX_RE.match(t):
                return t, "low", [t]
    return None, "", []


def _extract_major(text: str, edu_section: str,
                   entries: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """返回 (专业, 置信度)。显式标签优先，其次教育条目行；都不给就 None（不猜）。"""
    for src, conf in ((edu_section, "high"), (text[:2500], "high"), (text, "low")):
        if not src:
            continue
        for m in _MAJOR_LABEL_RE.finditer(src):
            cand = m.group(1).strip()
            cand = re.split(r"[；;。\n]", cand)[0].strip()
            cand = _strip_major_token(cand)
            if not cand or _MAJOR_BAD_FOLLOW.match(cand) or not _looks_like_major(cand):
                continue
            return cand, conf
    for e in entries:
        if e["major"]:
            return e["major"], "high"
    return None, ""


def _extract_certificates(text: str) -> List[str]:
    """全文扫证书关键词，返回去重列表（长词优先，避免「注册安全工程师」被切成「安全工程师」）。"""
    if not text:
        return []
    out: List[str] = []
    taken: List[Tuple[int, int]] = []
    for kw in _CERT_KW_SORTED:
        for m in re.finditer(re.escape(kw), text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue          # 已被更长的关键词覆盖
            taken.append((s, e))
            out.append(kw)
    # 去重保序
    seen: Set[str] = set()
    uniq: List[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _extract_skills(text: str, skill_section: str) -> List[str]:
    """技能标签 = 词表精确命中（高精度）+ 技能段短条目切分。"""
    if not text:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    # 1) 词表命中（大小写不敏感）
    lower = text.lower()
    for kw in _SKILL_VOCAB_SORTED:
        k = kw.lower()
        if k in seen:
            continue
        if k in lower:
            # 短词（<=2 字符）要求词边界，避免 "5S"/"BI"/"OA" 误命中
            if len(kw) <= 2 and not re.search(
                    r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", text):
                continue
            seen.add(k)
            out.append(kw)
    # 2) 技能段里的短条目
    for item in split_items(skill_section, min_len=3, max_len=22, max_items=25):
        if item in seen or item.lower() in seen:
            continue
        if re.search(r"[。！？!?]", item):
            continue
        seen.add(item.lower())
        out.append(item)
        if len(out) >= 40:
            break
    return out[:40]


# 标签一律「长的在前」——python 正则的 | 是最左最先匹配，不是最长匹配，
# 否则 `期望工作地区` 会被 `期望工作地` 截走，值变成 `区:`。
_EXPECT_LABELS = {
    "expected_position": r"(期望从事岗位|意向工作岗位|应聘职位名称|意向岗位|求职意向|应聘职位|"
                         r"应聘岗位|求职岗位|期望职位|意向职位|期望岗位|应聘职务|意向职务|"
                         r"目标岗位|申请职位|期望职能|意向职能)",
    "expected_location": r"(期望工作地区|意向工作城市|期望工作地点|期望工作地|意向工作地|"
                          r"户籍所在地|户口所在地|意向城市|期望地点|期望城市|意向地区|"
                          r"工作地点|现居住地|期望地区|工作地区)",
    "expected_salary": r"(当前期望薪资|期望薪资待遇|期望薪资|期望月薪|薪资要求|期望待遇|"
                        r"期望年薪|目标薪资|目前月薪|月薪要求|薪资待遇|期望薪酬|期望工资)",
}
_EXPECT_BAD_VALUE_PREFIX = re.compile(r"^(及|和|与|或|等|的|之|/|、)")
_MONEY_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*[kKwW万千]?\s*[-~—－至到]\s*\d+(?:\.\d+)?\s*[kKwW万千]?|"
    r"\d+(?:\.\d+)?\s*[kKwW万千]\s*(?:以上|以下|左右)?|"
    r"\d{4,7}\s*[-~—－至到]\s*\d{4,7}\s*(?:元)?(?:/月)?|"
    r"(?<![\d.])\d{4,7}(?![\d])\s*(?:元)?(?:/月|每月)?|"
    r"面议|协商)")


_ALL_EXPECT_LABELS = (
    "意向岗位|求职意向|应聘职位|应聘岗位|求职岗位|期望职位|意向职位|期望岗位|应聘职务|"
    "意向职务|目标岗位|申请职位|意向城市|期望地点|期望城市|意向地区|期望工作地|工作地点|"
    "意向工作地|现居住地|期望地区|工作地区|期望薪资|期望月薪|薪资要求|期望待遇|期望年薪|"
    "目标薪资|目前月薪|月薪要求|薪资待遇|求职类型|求职状况|联系方式|政治面貌|开始工作时间|"
    "出生年月|毕业时间|户籍|籍贯|工作年限|最高学历|教育经历|教育背景|工作经历|工作经验|"
    "自我评价|个人评价|专业技能|核心优势|优势亮点|当前状态|期望行业|期望职能|"
    "期望从事行业|期望从事岗位|期望工作地区|当前期望薪资|预计到岗时间|期望工资|"
    "婚姻状况|政治面貌|联系方式|开始工作时间|最高学历|毕业院校|学校名称|专业名称|"
    "到岗时间|工作经历|教育经历|项目经历|荣誉证书")
_NEXT_LABEL_RE = re.compile(r"(?:" + _ALL_EXPECT_LABELS + r")\s*[:：|]")


def _extract_expected(text: str, key: str) -> Tuple[Optional[str], str]:
    """抽求职意向三项之一。

    要点：① `求职意向` 常单独占一行、真值在下一行的 `意向岗位:` 里，所以要遍历所有
    命中并跳过「值本身又是一个标签」的情况；② 值里常粘着下一个字段
    （`厂务管理 期望薪资: 20k-30k`），要在下一个标签处截断。
    """
    if not text:
        return None, ""
    pat = _EXPECT_LABELS[key]
    for m in re.finditer(r"(?:" + pat + r")\s*[:：|]?\s*([^\n]{1,80})", text):
        raw = m.group(2).strip()      # group(1) 是标签本身，group(2) 才是值
        nm = _NEXT_LABEL_RE.search(raw)
        if nm:
            raw = raw[:nm.start()].strip()
        raw = re.split(r"[；;。]", raw)[0].strip()
        if not raw or _NEXT_LABEL_RE.match(raw):
            continue
        if re.search(r"[:：]", raw[:12]):      # 抓到的是下一个标签，不是值
            continue
        if _EXPECT_BAD_VALUE_PREFIX.match(raw):
            continue
        if len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", raw)) < 2:
            continue
        if key == "expected_salary":
            mm = _MONEY_RE.search(raw)
            if mm:
                return re.sub(r"\s+", "", mm.group(0)), "high"
            return (raw[:20] or None), ("low" if raw else "")
        # 岗位/地点常是多值列举（`安徽,河北,河南,江苏,浙江`），保留前几项而不是只取第一个
        limit = 3 if key == "expected_location" else 2
        parts = [x.strip() for x in re.split(r"[，,、|/]", raw) if x.strip()]
        parts = [x for x in parts if not _EXPECT_BAD_VALUE_PREFIX.match(x)][:limit]
        val = "、".join(parts)[:30]
        return (val or None), ("high" if val else "")
    return None, ""


# ---- 文件名里的岗位/地点/薪资（`【岗位_地点 薪资】姓名 年限.pdf`）----
# 贪婪前缀：`电气工程师` 必须整体命中，懒惰前缀会先吃掉 `电气` 再单独命中 `工程师`
_FN_JOB_WORD_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z]{0,12}"
    r"(?:工程师|主管|经理|总监|专员|助理|会计|出纳|班长|组长|技师|技术员|操作工|主任|"
    r"工艺|设备|暖通|电力|电气|财务|生产|质量|安全|运维|系统|软件|开发))")
_FN_SALARY_RE = re.compile(r"(\d+(?:\.\d+)?\s*[-~—－]\s*\d+(?:\.\d+)?\s*[kKwW万千])")
_CITY_LIST = ("曲靖 昆明 云南 贵阳 贵州 武汉 湖北 成都 四川 西安 陕西 兰州 甘肃 银川 宁夏 "
              "西宁 青海 镇江 江苏 苏州 无锡 南京 常州 盐城 杭州 浙江 合肥 安徽 郑州 河南 "
              "济南 山东 青岛 石家庄 河北 太原 山西 长沙 湖南 南昌 江西 福州 厦门 广州 深圳 "
              "东莞 佛山 南宁 海口 重庆 天津 上海 北京 大连 沈阳 长春 哈尔滨 绵阳 荆州 "
              "焦作 商丘 阜阳 毕节 平凉 牟定 大理 中卫 阜宁 徐州 全国").split()


def _job_tokens_from_filename(file_name: str) -> Optional[str]:
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(file_name or "").name)
    stem = unicodedata.normalize("NFKC", stem)
    bracket = re.search(r"[【\[]\s*([^】\]]+)[】\]]", stem)
    scope = bracket.group(1) if bracket else stem
    cands = [c.strip() for c in _FN_JOB_WORD_RE.findall(scope) if c.strip()]
    cands = [c for c in cands if len(c) >= 2]
    if not cands:
        return None
    return max(cands, key=len)


def _location_from_filename(file_name: str) -> Optional[str]:
    stem = unicodedata.normalize("NFKC", Path(file_name or "").name)
    for c in _CITY_LIST:
        if c in stem:
            return c
    return None


def _salary_from_filename(file_name: str) -> Optional[str]:
    stem = unicodedata.normalize("NFKC", Path(file_name or "").name)
    m = _FN_SALARY_RE.search(stem)
    return re.sub(r"\s+", "", m.group(0)) if m else None


def _years_from_filename(file_name: str) -> Optional[int]:
    """从文件名取年限（`石昊 10年` / `胡裕_14年` / `任旒10年以上`）。

    必须排除 `23年毕业`（那是毕业年份不是年限）和 `29岁`（那是年龄）。
    """
    stem = unicodedata.normalize("NFKC", Path(file_name or "").name)
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem)
    for m in re.finditer(r"(?<![\d.])(\d{1,2})\s*年(?!毕业|度|龄|月|日|代)", stem):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 1 <= n <= 50:
            return n
    # `田海飞 23年毕业-设备助工` -> 2023 届 -> 用当前年份减
    for m in re.finditer(r"(?<![\d.])(\d{2})\s*年毕业", stem):
        try:
            grad = 2000 + int(m.group(1))
        except ValueError:
            continue
        n = _CURRENT_YEAR - grad
        if 0 <= n <= 45:
            return n
    return None


_GRAD_YEAR_RE = re.compile(r"(?:毕业时间|毕业年月|毕业证书时间)\s*[:：|]?\s*((?:19|20)\d{2})")


def estimate_years_from_graduation(text: str) -> Optional[int]:
    """最弱兜底：`毕业时间：2015年6月30日` -> 当前年份 - 毕业年份。"""
    if not text:
        return None
    m = _GRAD_YEAR_RE.search(text[:2500])
    if not m:
        return None
    try:
        n = _CURRENT_YEAR - int(m.group(1))
    except ValueError:
        return None
    return n if 0 <= n <= 45 else None


# =========================================================================== #
# 9. 对外：简历
# =========================================================================== #
def _text_unusable(text: str) -> bool:
    """文本层等于没有（扫描件只剩水印、或正文过短且无数字）-> 不抽任何正文字段。

    与 extract_text.detect_scanned 判据同源；这里做一份本地实现，避免
    extract_fields 硬依赖 extract_text（W-C 可能单独 import 本模块）。
    """
    try:                                            # 优先复用 extract_text 的实现
        from extract_text import detect_scanned     # noqa: WPS433
        return bool(detect_scanned(text, "pdf"))
    except Exception:
        pass
    body = re.sub(r"\s+", "", text or "")
    if len(body) < 60:
        return True
    if len(body) >= 1500:
        return False
    digits = sum(1 for c in body if c.isdigit())
    if digits >= 12:
        return False
    uniq = len(set(body)) / len(body)
    return uniq < 0.15


_EMPTY_RESUME_KEYS = ("name", "phone", "email", "education", "school", "major",
                      "years_experience", "expected_position", "expected_location",
                      "expected_salary", "school_rank", "certificates", "skills")


def extract_resume_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从简历纯文本 + 文件名抽字段。契约 §3.1 冻结签名。

    **永不抛异常**：text 为空/水印/乱码时只回填文件名里确凿的信息，其余一律
    留空（契约 D11：不硬造字段）。
    """
    text = normalize_text(text or "")
    file_name = file_name or ""
    warnings: List[str] = []
    confidence: Dict[str, str] = {}

    sections = slice_sections(text, _RESUME_SECTION_HEADS)
    if _text_unusable(text):
        for k in _EMPTY_RESUME_KEYS:
            confidence[k] = "none"
        # 姓名/岗位仍可从文件名拿（这是事实数据，不是臆造）
        fn_name = name_from_filename(file_name)
        fn_pos = _job_tokens_from_filename(file_name)
        reason = "文本为空" if not text.strip() else "文本层无效（扫描件只剩水印/字数过少且无数字）"
        warnings.append("%s，%s；除文件名-derived 字段外一律留空，"
                        "建议候选人提供 word/pdf 文字版后重跑"
                        % (reason, "仅从文件名取到姓名 %s" % fn_name if fn_name else "文件名也没解析出姓名"))
        return {
            "name": fn_name, "name_source": "filename" if fn_name else None,
            "phone": None, "email": None,
            "education": None, "school": None, "school_rank": None, "major": None,
            "years_experience": None,
            "certificates": [], "skills": [],
            "expected_position": fn_pos,
            "expected_location": _location_from_filename(file_name),
            "expected_salary": _salary_from_filename(file_name),
            "sections": sections,
            "confidence": confidence,
            "warnings": warnings,
            "education_raw": None,
            "years_experience_est": None,
            "years_experience_source": None,
            "years_source": None,   # 契约 D13 键名别名（与 years_experience_source 同值，只增不删）
            "schools_all": [],
            "name_from_filename": fn_name,
            "name_from_text": None,
            "text_usable": False,
        }

    # ---------- 姓名 ----------
    fn_name = name_from_filename(file_name)
    txt_name, txt_how, txt_trim = name_from_text(text)
    name: Optional[str] = None
    name_source: Optional[str] = None
    if fn_name and txt_name:
        if fn_name == txt_name or fn_name in txt_name or txt_name in fn_name:
            name, name_source = fn_name, "text"
            confidence["name"] = "high"          # 文件名与正文互相印证
        else:
            name, name_source = fn_name, "filename"
            confidence["name"] = "high"
            warnings.append("正文姓名候选 %r（命中方式 %s）与文件名姓名 %r 不一致，"
                            "已按文件名取值（PDF 字体伪影/双栏串行常见）"
                            % (txt_name, txt_how or "-", fn_name))
    elif fn_name:
        name, name_source = fn_name, "filename"
        confidence["name"] = "high"
    elif txt_name:
        name, name_source = txt_name, "text"
        # 强修剪（后面粘的是「性别/民族」这类真标签）可信；弱修剪或首行孤立 token 存疑
        if txt_how == "label" and txt_trim in ("", "strong"):
            confidence["name"] = "high"
        else:
            confidence["name"] = "low"
            warnings.append("正文姓名 %r 来自%s%s，无文件名可互相印证，请人工确认"
                            % (name, {"firstline": "首行孤立词", "title": "标题行",
                                      "selfintro": "自我介绍", "label": "标签"}.get(txt_how, txt_how),
                               "（与后续内容粘连后修剪）" if txt_trim else ""))
    else:
        confidence["name"] = "none"
        warnings.append("正文与文件名都没能解析出姓名，请人工补录")

    # ---------- 电话 / 邮箱 ----------
    phone, conf_phone = extract_phone(text)
    confidence["phone"] = conf_phone or "none"
    if conf_phone == "low":
        warnings.append("手机号 %s 是从粘连数字串里截取的（去掉了数字边界断言），请人工确认" % phone)
    email, conf_email = extract_email(text)
    confidence["email"] = conf_email or "none"
    if conf_email == "low":
        warnings.append("邮箱 %s 是修复过的（原件缺 @ 或域名有 PDF 伪影），请人工确认" % email)

    # ---------- 学历 / 学校 / 专业 ----------
    edu_section = sections.get("education_text", "")
    # 把已识别出的姓名（以及首屏几行里的孤立词）排除掉，避免 `汪一兵` 这种
    # 抬头人名被 ±3 行的专业兜底当成专业
    _head_tokens = set()
    for _ln in [x.strip() for x in text.splitlines() if x.strip()][:4]:
        if len(_ln) <= 12:
            _head_tokens.add(_ln)
    entries = parse_education_block(text, edu_section,
                                    exclude=set([n for n in (name,) if n]) | _head_tokens)
    education, edu_raw, conf_edu = _extract_education(text, edu_section, entries)
    confidence["education"] = conf_edu or "none"
    if not education:
        warnings.append("正文未出现任何学历词（博士/硕士/本科/大专/中专），学历留空待人工确认")
    school, conf_school, schools_all = _extract_school(text, edu_section, entries)
    confidence["school"] = conf_school or "none"
    rank = school_rank(school, education, text[:1500])
    confidence["school_rank"] = "high" if rank in ("985", "211", "双一流") else (
        "low" if rank else "none")
    major, conf_major = _extract_major(text, edu_section, entries)
    confidence["major"] = conf_major or "none"

    # ---------- 工作年限 ----------
    years, conf_years = extract_years(text)
    years_src = "text" if years is not None else None
    if years is None:
        fy = _years_from_filename(file_name)
        if fy is not None:
            years, conf_years, years_src = fy, "low", "filename"
            warnings.append("工作年限 %d 来自文件名（正文未显式声明），请人工确认" % fy)
    grad_est = estimate_years_from_graduation(text)
    est = estimate_years_from_dates(sections.get("work_text", ""))
    if est is None:
        est = estimate_years_from_dates(text)      # 分段失败时退回全文（只认工作条目行）
    if est is None:
        # 更弱的兜底：公司名与日期分行排版时（`2019 年 7 月\n-\n2021 年 9 月\n公司名称 X`）
        # 只用 work_text 分段里的日期区间，绝不用全文（会把教育日期算进工龄）
        est = estimate_years_from_section(sections.get("work_text", ""))
    if years is None and est is None and grad_est is not None:
        est = grad_est                      # 连工作日期都没有时，用毕业年份推
    if years is None and est is not None and 0 < est <= 45:
        years, conf_years, years_src = est, "low", "estimated"
        warnings.append("正文未显式声明工作年限；按%s估算为 %d 年"
                        "（years_experience_source=estimated，置信度 low，"
                        "agent 判定时请复核，不要直接当硬性门槛用）"
                        % ("毕业年份" if est == grad_est and grad_est is not None
                           and estimate_years_from_dates(text) is None
                           else "工作经历日期区间", est))
    confidence["years_experience"] = conf_years or "none"

    # ---------- 证书 / 技能 ----------
    certs = _extract_certificates(sections.get("cert_text", "") or text)
    confidence["certificates"] = "high" if certs else "none"
    skills = _extract_skills(text, sections.get("skill_text", ""))
    confidence["skills"] = "high" if skills else "none"

    # ---------- 求职意向 ----------
    pos, conf_pos = _extract_expected(text, "expected_position")
    if not pos:
        pos = _job_tokens_from_filename(file_name)
        conf_pos = "low" if pos else ""
        if pos:
            warnings.append("期望岗位 %r 取自文件名" % pos)
    confidence["expected_position"] = conf_pos or "none"
    loc, conf_loc = _extract_expected(text, "expected_location")
    if not loc:
        loc = _location_from_filename(file_name)
        conf_loc = "low" if loc else ""
    confidence["expected_location"] = conf_loc or "none"
    sal, conf_sal = _extract_expected(text, "expected_salary")
    if not sal:
        sal = _salary_from_filename(file_name)
        conf_sal = "low" if sal else ""
    confidence["expected_salary"] = conf_sal or "none"

    return {
        "name": name,
        "name_source": name_source,
        "phone": phone,
        "email": email,
        "education": education,
        "school": school,
        "school_rank": rank,
        "major": major,
        "years_experience": years,
        "certificates": certs,
        "skills": skills,
        "expected_position": pos,
        "expected_location": loc,
        "expected_salary": sal,
        "sections": sections,
        "confidence": confidence,
        # ---- 契约之外的只增字段 ----
        "warnings": warnings,
        "education_raw": edu_raw,
        "years_experience_est": est,
        "years_experience_source": years_src,
        "years_source": years_src,   # 契约 D13 键名别名（与 years_experience_source 同值，只增不删）
        "schools_all": schools_all,
        "edu_entries": entries,
        "name_from_filename": fn_name,
        "name_from_text": txt_name,
        "text_usable": True,
    }


# =========================================================================== #
# 10. 对外：岗位说明书
# =========================================================================== #
_JD_JOB_NAME_RE = re.compile(
    r"职位名称\s*Position\s*[:：]\s*(.{0,60}?)\s*(?:职等|Grade|/职级)", re.S)
_JD_JOB_NAME_RE2 = re.compile(r"职位名称\s*(?:Position)?\s*[:：]\s*(.{0,60}?)\s*(?:职等|职级|Grade)", re.S)
_JD_DEPT_RE = re.compile(
    r"工作部门\s*Dept\.?\s*[:：]\s*(.{0,60}?)\s*(?:直接上司|Line\s*manager|上司)", re.S)
_JD_DEPT_RE2 = re.compile(r"工作部门\s*(?:Dept\.?)?\s*[:：]\s*(.{0,60}?)\s*(?:直接上司|Line)", re.S)
_JD_QUAL_RE = re.compile(r"任职要求\s*Qualifications(.*?)(?:起草\s*Draft|上级部门负责人|$)", re.S)
_JD_QUAL_RE2 = re.compile(r"任职要求(.*?)(?:起草|上级部门负责人|$)", re.S)
_JD_RESP_RE = re.compile(
    r"(?:主要工作职责|工作职责)\s*(?:Major\s*responsibilities)?(.*?)"
    r"(?:次要工作职责|Minor\s*responsibilities|关键绩效指标|任职要求|Key Performance|$)", re.S)
_JD_KPI_RE = re.compile(
    r"(?:关键绩效指标|Key Performance Indicators|KPI)(.*?)(?:任职要求|Qualifications|$)", re.S)

_JD_ITEM_LABELS: Tuple[Tuple[str, str], ...] = (
    ("education_text", r"教育背景|学历要求|教育要求|学历要求"),
    ("cert_text", r"培训经历|证书要求|职业证书要求|资格证书|证书"),
    ("work_text", r"从业经验|工作经验|经验要求|从业经历"),
    ("skill_text", r"技能技巧|技能要求|能力要求|专业技能"),
    ("age_text", r"年龄要求|年龄"),
    ("other_text", r"其他要求|其它要求|备注"),
)
_JD_ITEM_LABEL_RE = re.compile(
    r"(?:\d{1,2}\s*[、.．)）]\s*)?(教育背景|培训经历|从业经验|技能技巧|年龄要求|"
    r"证书要求|专业要求|工作经验|其他要求|其它要求|经验要求|学历要求|能力要求|技能要求)"
    r"(?:（[^）]{0,20}）|\([^)]{0,20}\))?\s*[:：]")

_DEGREE_REQ_RE = re.compile(
    r"(博士|硕士|研究生|大学本科|本科|学士|大学专科|大专|专科|高职|中专|中职|高中)"
    r"\s*(及以上|以上)?")
_YEARS_REQ_RANGE_RE = re.compile(r"(\d{1,2})\s*[-~—－]\s*(\d{1,2})\s*年(?:及以上|以上)?")
_YEARS_REQ_RE = re.compile(r"(\d{1,2})\s*年(?:及以上|以上|左右)?")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}
_CN_YEARS_RE = re.compile(r"([一二两三四五六七八九十])\s*年(?:及以上|以上)?")
_FRESH_GRAD_RE = re.compile(r"(应届生|应届毕业生|无相关从业经验|可接受无相关|无工作经验要求)")
# 「优先/加分」标记。注意不含「考虑」——`可优先考虑` 之外的「考虑」噪声太大。
_PREF_MARK_RE = re.compile(r"(?:者优先|优先|加分|更佳|尤佳)")
_CERT_PREFER_RE = _PREF_MARK_RE
# 证书是否「优先」的两级判定用分隔符
_ITEM_SEPS = "、，,；;。\n"        # 列举分隔符 -> 判单个证书
_CLAUSE_SEPS = "；;。\n"            # 强分隔符 -> 判整句
_PAREN_TAIL_PREF_RE = re.compile(
    r"[（(][^（()）]*(?:优先|加分|更佳|尤佳)[^（()）]*[)）]\s*$")
_BARE_TAIL_PREF_RE = re.compile(r"(?:者优先|优先|加分|更佳|尤佳)\s*$")


def _split_jd_items(qual: str) -> Dict[str, str]:
    """把 任职要求 段落按 `1、教育背景：… 2、培训经历：…` 切成 {label_key: 内容}。"""
    out: Dict[str, str] = {}
    marks: List[Tuple[int, int, str]] = []
    for m in _JD_ITEM_LABEL_RE.finditer(qual):
        label = m.group(1)
        key = None
        for k, pat in _JD_ITEM_LABELS:
            if re.fullmatch(pat, label):
                key = k
                break
        if key is None:
            continue
        marks.append((m.start(), m.end(), key))
    for i, (_s, e, key) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(qual)
        body = qual[e:end].strip()
        body = re.sub(r"\s{2,}", " ", body).strip()
        out[key] = (out[key] + " " + body).strip() if out.get(key) else body
    return out


def _extract_major_req(edu_line: str) -> Tuple[Optional[str], str]:
    """从教育背景行里切专业要求：`材料科学、化工工程、物理学等相关专业` -> `材料科学、化工工程、物理学`。"""
    if not edu_line:
        return None, ""
    segs = re.split(r"[；;。\n]", edu_line)
    picked: List[str] = []
    for seg in segs:
        if "专业" not in seg and "类" not in seg:
            continue
        # 去掉学历部分
        seg = _DEGREE_REQ_RE.sub("", seg, count=1)
        seg = re.sub(r"^(及以上学历|以上|学历|，|,|、|\s)+", "", seg).strip()
        for part in re.split(r"[、，,；;/或]", seg):
            p = part.strip()
            p = re.sub(r"^(其他|其它|等|以及|及|和|与|相关专业|相关)\s*", "", p)
            p = re.sub(r"(等相关专业|相关专业|专业|相关|类|等|方向)\s*$", "", p).strip()
            p = p.strip("，,、 （）()")
            if len(p) >= 2 and re.search(r"[\u4e00-\u9fffA-Za-z]", p):
                if p in ("放宽", "履历优秀者", "可放宽至大专"):
                    continue
                picked.append(p)
        if picked:
            break
    seen: Set[str] = set()
    uniq = [x for x in picked if not (x in seen or seen.add(x))]
    if not uniq:
        return None, ""
    return "、".join(uniq[:10]), "high"


def _extract_years_req(exp_line: str) -> Tuple[Optional[str], Optional[int], str]:
    """返回 (年限要求原文归一, 最低年限 int, 置信度)。"""
    if not exp_line:
        return None, None, ""
    if _FRESH_GRAD_RE.search(exp_line):
        m = _YEARS_REQ_RE.search(exp_line)
        if m:
            return "0年（可接受应届生，有%s年及以上经验者优先）" % m.group(1), 0, "high"
        return "0年（可接受应届生/无相关经验）", 0, "high"
    pieces: List[str] = []
    mins: List[int] = []
    for m in _YEARS_REQ_RANGE_RE.finditer(exp_line):
        a, b = int(m.group(1)), int(m.group(2))
        pieces.append("%d-%d年" % (a, b))
        mins.append(a)
    if not pieces:
        for m in _YEARS_REQ_RE.finditer(exp_line):
            n = int(m.group(1))
            if 0 < n <= 40:
                pieces.append("%d年及以上" % n)
                mins.append(n)
    if not pieces:                       # `五年以上设备设施运维…` 这类中文数字
        for m in _CN_YEARS_RE.finditer(exp_line):
            n = _CN_NUM.get(m.group(1))
            if n:
                pieces.append("%d年及以上" % n)
                mins.append(n)
    if not pieces:
        return None, None, ""
    seen: Set[str] = set()
    uniq = [x for x in pieces if not (x in seen or seen.add(x))]
    label = "（含分层要求）" if len(uniq) > 1 else ""
    # 取**第一条**（基础职级门槛），不是最小值：`8年及以上…其中3年及以上团队管理经验`
    # 的门槛是 8 年，取 min 会把门槛降到 3 年
    return "；".join(uniq[:3]) + label, mins[0], "high"


def _extract_cert_req(qual: str, cert_line: str,
                      exp_line: str) -> Tuple[Optional[str], bool, List[str], List[str], str]:
    """返回 (cert_req, cert_is_preferred_not_required, 硬性证书, 优先证书, 置信度)。

    **关键**：区分「持××证书者优先」（加分项，当门槛会误杀）与硬性证书门槛。
    两级判据：
      ① 条目级：证书关键词之后、到下一个列举分隔符（、，,；;。）之前的窗口里
         若出现「优先/加分/更佳」-> 该证书是优先项。
         这样 `…合格证、体系内审员、注册安全工程师（高级工程师优先）` 里只有
         最后一个判成优先，前两个仍是硬性 —— 旧写法按整句判会把三个全放掉。
      ② 整句级：整句以**裸**「…者优先」结尾（不在括号里）时，说明「优先」管的是
         整个列举，例如 `持有电工证、特种设备相关操作/管理证书者优先` -> 全句优先。
         若句尾的优先在括号里（`…（SA8000）（优先）`）则不套这条。
    """
    scope_parts = [p.strip() for p in (cert_line, exp_line) if p and p.strip()]
    # **必须用换行拼**：`…证书者优先` 后面若直接接上 从业经验 段，
    # 「整句以裸优先结尾」的判据就会被下一段的分号破坏（实测把 3 份 JD 的
    # 「持证者优先」误判成硬性证书门槛）
    scope = "\n".join(scope_parts) or (qual or "")
    if not scope.strip():
        return None, False, [], [], ""
    required: List[str] = []
    preferred: List[str] = []
    taken: List[Tuple[int, int]] = []
    for kw in _CERT_KW_SORTED:
        for m in re.finditer(re.escape(kw), scope):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue                       # 已被更长的关键词覆盖
            taken.append((s, e))
            # ① 条目级窗口
            wend = len(scope)
            for sep in _ITEM_SEPS:
                idx = scope.find(sep, e)
                if 0 <= idx < wend:
                    wend = idx
            item_pref = bool(_CERT_PREFER_RE.search(scope[e:wend + 1]))
            # ② 整句级窗口
            cstart = 0
            for sep in _CLAUSE_SEPS:
                idx = scope.rfind(sep, 0, s)
                if idx + 1 > cstart:
                    cstart = idx + 1
            cend = len(scope)
            for sep in _CLAUSE_SEPS:
                idx = scope.find(sep, e)
                if 0 <= idx < cend:
                    cend = idx
            clause = scope[cstart:cend].strip()
            clause_pref = bool(_BARE_TAIL_PREF_RE.search(clause)
                               and not _PAREN_TAIL_PREF_RE.search(clause))
            if item_pref or clause_pref:
                if kw not in preferred:
                    preferred.append(kw)
            else:
                if kw not in required:
                    required.append(kw)
    # 同一证书若在别处被写成硬性要求 -> 以硬性为准（宁严勿漏真门槛）
    for kw in list(preferred):
        if kw in required:
            preferred.remove(kw)
    if not required and not preferred:
        return None, False, [], [], ""
    if required:
        return "、".join(required), False, required, preferred, "high"
    return "、".join(preferred), True, [], preferred, "high"


def _split_skills_jd(skill_line: str) -> Tuple[List[str], List[str]]:
    """JD 技能切分：必备 vs 加分（带「优先/加分」的小句进加分项）。

    **必须多分隔符切分**：`EHS体系管理，危险源辨识与评价，隐患的排查和治理` 是 3 条，
    不是 1 条；否则打分分母=1，命中即 100 分虚高。
    """
    must: List[str] = []
    bonus: List[str] = []
    seen: Set[str] = set()
    if not skill_line:
        return must, bonus
    for clause in _SENTENCE_SEP_RE.split(skill_line):
        clause = clause.strip()
        if not clause:
            continue
        is_bonus = bool(_PREF_MARK_RE.search(clause))
        items = split_items(clause, min_len=3, max_len=40, max_items=20)
        if not items:
            items = [_clean_item(clause)] if len(_clean_item(clause)) >= 4 else []
        for it in items:
            k = it.lower()
            if k in seen:
                continue
            seen.add(k)
            (bonus if is_bonus else must).append(it)
    return must[:25], bonus[:15]


_DEPT_TAIL_RE = re.compile(r"(部|组|中心|处|科|室|线|区|课|站|队)$")
_JOB_TITLE_RE = re.compile(
    r"(工程师|主管|经理|总监|专员|助理|会计|出纳|班长|组长|技师|技术员|操作工|"
    r"主任|厂长|部长|秘书|顾问)$")


def _canon_job(name: str) -> str:
    """岗位名归一，用于「文件名 vs 正文」比较：去空白、顿号/逗号统一成 /。"""
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[、，,]", "/", s)
    s = re.sub(r"/{2,}", "/", s)
    return s.strip("/").lower()


def _dept_from_filename(file_name: str) -> Optional[str]:
    """`岗位说明书-制造中心-曲靖制造基地-硅片制造部-工艺部-工程师、高级工程师.doc`
    -> `硅片制造部-工艺部`。

    规则：定位最后一个含「基地/公司/厂区」的段，其后**连续**以
    部/组/中心/处/科/室 结尾的段拼成部门；遇到岗位名就停。
    """
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(file_name or "").name)
    stem = unicodedata.normalize("NFKC", stem)
    stem = re.sub(r"^附件\s*\d*\s*[：:]?\s*", "", stem)
    parts = [p.strip() for p in re.split(r"[-－—_＿]+", stem) if p.strip()]
    if not parts:
        return None
    anchor = -1
    for i, p in enumerate(parts):
        if re.search(r"(基地|公司|厂区|工业园)$", p):
            anchor = i
    if anchor < 0:
        for i, p in enumerate(parts):
            if p in ("制造中心", "岗位说明书") or "说明书" in p:
                anchor = i
    keep: List[str] = []
    for p in parts[anchor + 1:]:
        if _DEPT_TAIL_RE.search(p) and not _JOB_TITLE_RE.search(p):
            if p not in keep:
                keep.append(p)
        else:
            break
    return "-".join(keep) if keep else None


def _job_name_from_filename(file_name: str) -> Optional[str]:
    """`…-硅片制造部-工艺部-工程师、高级工程师.doc` -> `工程师/高级工程师`。"""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(file_name or "").name)
    stem = unicodedata.normalize("NFKC", stem)
    stem = re.sub(r"^附件\s*\d*\s*[：:]?\s*", "", stem)
    parts = [p.strip() for p in re.split(r"[-－—_＿]+", stem) if p.strip()]
    tail = [p for p in parts
            if _JOB_WORD_RE.search(p)
            or re.fullmatch(r"(经理|主管|工程师|专员|助理|会计|班长|组长|总监)", p)]
    if not tail:
        return None
    name = tail[-1]
    name = re.sub(r"\s*[、，,]\s*", "/", name)
    name = re.sub(r"\s+", "/", name).strip("/")
    return name or None


def extract_job_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从岗位说明书纯文本 + 文件名抽字段。契约 §3.1 冻结签名。**永不抛异常**。"""
    text = normalize_text(text or "")
    file_name = file_name or ""
    warnings: List[str] = []
    confidence: Dict[str, str] = {}

    sections = slice_sections(text, _JD_SECTION_HEADS)
    if _text_unusable(text):
        for k in ("job_name", "department", "education_req", "years_req",
                  "major_req", "cert_req", "must_skills_raw", "bonus_skills_raw",
                  "hard_gates_raw", "cert_is_preferred_not_required"):
            confidence[k] = "none"
        jn = _job_name_from_filename(file_name)
        dep = _dept_from_filename(file_name)
        if jn or dep:
            warnings.append("文本层无效，仅从文件名取到岗位名 %r / 部门 %r；"
                            "其余字段留空" % (jn, dep))
        return {
            "job_name": jn, "department": dep,
            "hard_gates_raw": None, "must_skills_raw": None, "bonus_skills_raw": None,
            "education_req": None, "years_req": None, "major_req": None, "cert_req": None,
            "cert_is_preferred_not_required": False,
            "sections": sections, "confidence": confidence, "warnings": warnings,
            "must_skills": [], "bonus_skills": [], "cert_required": [], "cert_preferred": [],
            "years_req_min": None, "age_req": None,
            "department_source": None, "text_usable": False,
        }

    flat = re.sub(r"\s{2,}", " ", text)

    # ---------- 岗位名称 ----------
    job_name: Optional[str] = None
    for pat in (_JD_JOB_NAME_RE, _JD_JOB_NAME_RE2):
        m = pat.search(flat)
        if m:
            cand = m.group(1).strip().strip("/").strip()
            cand = re.sub(r"\s+", "", cand)
            if cand and cand not in ("/", "-", ""):
                job_name = cand
                break
    fn_job = _job_name_from_filename(file_name)
    if job_name:
        confidence["job_name"] = "high"
        if fn_job and _canon_job(fn_job) != _canon_job(job_name):
            warnings.append("文件名岗位 %r 与正文职位名称 %r 不一致，已按正文取值"
                            % (fn_job, job_name))
    elif fn_job:
        job_name = fn_job
        confidence["job_name"] = "low"
        warnings.append("岗位名称取自文件名（正文未匹配到「职位名称 Position:」）")
    else:
        confidence["job_name"] = "none"
        warnings.append("岗位名称未能解析，请人工补录")

    # ---------- 所属部门 ----------
    department: Optional[str] = None
    for pat in (_JD_DEPT_RE, _JD_DEPT_RE2):
        m = pat.search(flat)
        if m:
            cand = re.sub(r"\s+", "", m.group(1).strip()).strip("/").strip()
            if cand:
                department = cand
                break
    fn_dept = _dept_from_filename(file_name)
    dept_source = "text" if department else None
    if department and fn_dept:
        # 正文的「工作部门 Dept.:」常常只写到部级（`厂务管理部`），
        # 文件名会写到组级（`厂务管理部-暖通组`）；两者兼容时取更细的那个
        if fn_dept.startswith(department) and fn_dept != department:
            department, dept_source = fn_dept, "text+filename"
            warnings.append("正文部门只到部级，已用文件名补全为 %r" % department)
        elif not department.startswith(fn_dept):
            warnings.append("正文部门 %r 与文件名部门 %r 不一致，已按正文取值"
                            % (department, fn_dept))
        confidence["department"] = "high"
    elif department:
        confidence["department"] = "high"
    else:
        department, dept_source = fn_dept, ("filename" if fn_dept else None)
        confidence["department"] = "low" if department else "none"
        if department:
            warnings.append("所属部门取自文件名")

    # ---------- 任职要求 分段 ----------
    mq = _JD_QUAL_RE.search(text) or _JD_QUAL_RE2.search(text)
    qual = mq.group(1) if mq else text
    qual = re.sub(r"[ \t]{2,}", " ", qual)
    items = _split_jd_items(qual)
    if not items:
        warnings.append("未能按「1、教育背景 / 2、培训经历 / 3、从业经验 / 4、技能技巧」"
                        "切分任职要求，字段可能不全")
    edu_line = items.get("education_text", "") or sections.get("education_text", "")
    cert_line = items.get("cert_text", "") or sections.get("cert_text", "")
    exp_line = items.get("work_text", "") or sections.get("work_text", "")
    skill_line = items.get("skill_text", "") or sections.get("skill_text", "")
    age_line = items.get("age_text", "")

    # ---------- 学历要求 ----------
    education_req: Optional[str] = None
    if edu_line:
        dm = _DEGREE_REQ_RE.search(edu_line)
        if dm:
            enum = _degree_to_enum(dm.group(1))
            # 「学历可放宽至大专」是放宽条款，不是门槛 -> 取句子里的最高学历
            best = None
            for m2 in _DEGREE_REQ_RE.finditer(edu_line):
                seg_start = edu_line.rfind("；", 0, m2.start())
                seg = edu_line[seg_start:m2.start()]
                if "放宽" in seg:
                    continue
                lv = _degree_to_enum(m2.group(1))
                if lv and (best is None or
                           [d[0] for d in _DEGREE_LEVELS].index(lv) <
                           [d[0] for d in _DEGREE_LEVELS].index(best)):
                    best = lv
            education_req = (best or enum) + (dm.group(2) or "及以上")
    if education_req:
        confidence["education_req"] = "high"
    else:
        confidence["education_req"] = "none"
        warnings.append("学历要求未解析到（教育背景段: %r）" % (edu_line[:40] or "-"))

    # ---------- 专业要求 ----------
    major_req, conf_major = _extract_major_req(edu_line)
    confidence["major_req"] = conf_major or "none"

    # ---------- 经验年限 ----------
    years_req, years_min, conf_years = _extract_years_req(exp_line)
    confidence["years_req"] = conf_years or "none"

    # ---------- 证书要求（区分优先/硬性）----------
    cert_req, cert_pref_only, cert_required, cert_preferred, conf_cert = _extract_cert_req(
        qual, cert_line, exp_line)
    confidence["cert_req"] = conf_cert or "none"
    confidence["cert_is_preferred_not_required"] = "high" if (cert_required or cert_preferred) else "none"
    if cert_pref_only and cert_preferred:
        warnings.append("证书 %r 全部出现在「…者优先/加分」语境，"
                        "cert_is_preferred_not_required=True，上层**不得**当硬性门槛用"
                        % "、".join(cert_preferred))
    elif cert_required and cert_preferred:
        warnings.append("证书要求混合：硬性=%r，优先=%r" %
                        ("、".join(cert_required), "、".join(cert_preferred)))

    # ---------- 技能：必备 vs 加分 ----------
    must_items, bonus_items = _split_skills_jd(skill_line)
    # 从业经验/教育背景里的「…者优先」句也算加分项。
    # **不扫 cert_line**：那里的「优先」是证书优先（已由 _extract_cert_req 处理），
    # 混进来会把整串证书名当成一条加分技能。
    extra_bonus: List[str] = []
    for line in (exp_line, edu_line):
        if not line:
            continue
        for m in re.finditer(r"([^；;。\n]{4,60}?(?:者优先|优先|加分|更佳|尤佳))", line):
            s = _clean_item(m.group(1))
            if not (4 <= len(s) <= 60):
                continue
            if s in bonus_items or s in extra_bonus:
                continue
            if any(kw in s for kw in _CERT_KW_SORTED):
                continue          # 是证书条款，不是技能
            extra_bonus.append(s)
    for s in extra_bonus:
        if len(bonus_items) < 20:
            bonus_items.append(s)
    # 「持××证书者优先」本质上就是加分项，并进 bonus，避免有证书加分的 JD
    # bonus_skills 反而为空（EHS经理这类 6 个（优先）证书的 JD 实测会漏）
    for c in cert_preferred:
        if c not in bonus_items and len(bonus_items) < 20:
            bonus_items.append(c + "（持证者优先）")
    must_skills_raw = re.sub(r"\s{2,}", " ", skill_line).strip() or None
    bonus_raw_parts = ([c.strip() for c in _SENTENCE_SEP_RE.split(skill_line)
                        if _PREF_MARK_RE.search(c)] + extra_bonus
                       + ["持%s者优先" % c for c in cert_preferred])
    bonus_skills_raw = "；".join(dict.fromkeys(bonus_raw_parts)).strip() or None
    confidence["must_skills_raw"] = "high" if must_items else "none"
    confidence["bonus_skills_raw"] = "high" if bonus_items else "none"
    if must_items and len(must_items) == 1 and len(skill_line) > 60:
        warnings.append("必备技能只切出 1 条但原文超过 60 字，分母可能偏小，"
                        "建议 agent 归一化时复核（原始: %r）" % skill_line[:60])
    if not must_items:
        warnings.append("必备技能未解析到（技能技巧段: %r）" % (skill_line[:40] or "-"))

    # ---------- 硬性门槛原文 ----------
    gate_bits = []
    if edu_line:
        gate_bits.append("教育背景：" + edu_line.strip())
    if exp_line:
        gate_bits.append("从业经验：" + exp_line.strip())
    if cert_line:
        gate_bits.append("培训/证书：" + cert_line.strip())
    if age_line:
        gate_bits.append("年龄：" + age_line.strip())
    hard_gates_raw = " | ".join(gate_bits)[:1200] or None
    confidence["hard_gates_raw"] = "high" if hard_gates_raw else "none"

    # 合并 sections：任职要求里的分段优先（更准），正文标题分段兜底
    merged_sections = dict(sections)
    for k, v in (("education_text", edu_line), ("cert_text", cert_line),
                 ("work_text", exp_line), ("skill_text", skill_line)):
        if v and not merged_sections.get(k):
            merged_sections[k] = v.strip()
    merged_sections["qualification_text"] = re.sub(r"\s{2,}", " ", qual).strip()[:2000]
    mr = _JD_RESP_RE.search(text)
    if mr and not merged_sections.get("responsibility_text"):
        merged_sections["responsibility_text"] = re.sub(r"\s{2,}", " ", mr.group(1)).strip()[:2000]
    mk = _JD_KPI_RE.search(text)
    if mk and not merged_sections.get("kpi_text"):
        merged_sections["kpi_text"] = re.sub(r"\s{2,}", " ", mk.group(1)).strip()[:800]
    merged_sections.setdefault("age_text", (age_line or "").strip())

    return {
        "job_name": job_name,
        "department": department,
        "hard_gates_raw": hard_gates_raw,
        "must_skills_raw": must_skills_raw,
        "bonus_skills_raw": bonus_skills_raw,
        "education_req": education_req,
        "years_req": years_req,
        "major_req": major_req,
        "cert_req": cert_req,
        "cert_is_preferred_not_required": bool(cert_pref_only),
        "sections": merged_sections,
        "confidence": confidence,
        # ---- 契约之外的只增字段（修「分母=1 虚高」缺陷，供 apply_decisions 直接用）----
        "must_skills": must_items,
        "bonus_skills": bonus_items,
        "cert_required": cert_required,
        "cert_preferred": cert_preferred,
        "years_req_min": years_min,
        "age_req": (age_line.strip()[:40] or None),
        "warnings": warnings,
        "job_name_from_filename": fn_job,
        "department_from_filename": fn_dept,
        "department_source": dept_source,
        "text_usable": True,
    }


# =========================================================================== #
# 11. CLI（自测用）
# =========================================================================== #
def _main(argv: List[str]) -> int:
    import json
    import sys as _sys
    if len(argv) < 2:
        print("usage: extract_fields.py <resume|job> <text_file> [file_name]",
              file=_sys.stderr)
        return 2
    mode, path = argv[0], argv[1]
    fname = argv[2] if len(argv) > 2 else Path(path).name
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fn = extract_resume_fields if mode == "resume" else extract_job_fields
    print(json.dumps(fn(text, fname), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:]))
