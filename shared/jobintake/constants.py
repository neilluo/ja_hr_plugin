# -*- coding: utf-8 -*-
"""jobintake 常量。

红线：
  * ORG_MFG_KEYWORDS / ORG_FUNC_KEYWORDS / ORG_EXPLICIT 是**岗位侧 B 变体**
    （13/10 关键词 + 显式声明表），与简历侧 intake/pipeline.py 的同名常量
    数据不同、策略相反（low → 仍写库），**禁止合并**。
  * SETTLE_WAITS 是 B 侧回读轮询口径，与 shared/intake/readback.py 的同名
    常量**不合并**（A 侧按业务键 filter 一对一，B 侧按 job_name 一对多精确归属）。
  * MUST_WEIGHT_DEFAULT / SKILL_ITEM_TOO_SHORT_MAXLEN 等阈值是裁判篡改灵敏度
    标定点，值即契约。
"""

from __future__ import annotations

UPLOAD_CONCURRENCY = 5
MUST_WEIGHT_DEFAULT = 0.7        # JD 未给比例时 70%/30%
BONUS_WEIGHT_DEFAULT = 0.3
STATUS_DEFAULT = "招聘中"
LOCATION_FALLBACK = "不限"
OLD_TURNS_PER_FILE = 25          # 老插件每份 20~40 个工具回合，取中位数
NEW_TURNS = 1
SETTLE_WAITS = (1.5, 3.0, 4.5)
RICHTEXT_MAX = 20000             # richText/text 写入上限
HARD_GATES_TEXT_MAX = 1500

# 组织分类关键词
ORG_MFG_KEYWORDS = ("制造基地", "厂务", "设备", "EHS", "暖通", "电气", "工艺",
                    "单晶", "硅片", "组件", "电池", "生产", "制造部")
ORG_FUNC_KEYWORDS = ("财务", "财经", "行政", "人力", "人事", "数据信息", "成本会计",
                     "会计", "审计", "法务")
#: 客户在文件名里**显式声明**的组织（最强证据：`岗位说明书-制造中心-曲靖制造基地-…`）
ORG_EXPLICIT = ("制造中心", "职能中心")

# 工作地点候选城市（与 config.json 的 work_location 选项对齐 + 常见补充）
CITY_HINTS = ("曲靖", "昆明", "云南", "北京", "上海", "深圳", "杭州", "成都", "广州",
              "南京", "武汉", "西安", "兰州", "银川", "西宁", "贵阳", "郑州", "合肥",
              "济南", "青岛", "苏州", "无锡", "常州", "盐城", "阜宁", "中卫", "大理",
              "牟定", "遵义", "徐州", "绵阳", "荆州", "焦作", "商丘", "阜阳", "毕节",
              "平凉", "全国", "不限")

PARSE_FAIL_REASON = {
    "no_text_layer": "扫描件/图片无文字层，且本机 OCR 未能救回（非 macOS 无 OCR 能力，"
                     "或 OCR 文本未通过可信护栏）；请提供 Word 或 PDF 文字版岗位说明书"
                     "（不硬造字段）",
    "garbled": "文本层疑似乱码/编码错位，无法可靠解析；请提供文字版 JD 或转人工核对原件",
    "encrypted": "文件加密或无读取权限，无法解析；请提供未加密的文字版 JD",
    "unsupported": "不支持的文件格式，无法解析；请提供 PDF/Word(docx/doc) 文字版 JD",
    "error": "文件解析失败；请确认文件完整后重新提供",
}

#: jobs[] 元素必备字段
JOB_FIELDS_CONTRACT = ("key", "record_id", "job_id", "job_name", "department", "org",
                       "status", "hard_gates", "must_skills", "bonus_skills", "weights")
HARD_GATE_KEYS = ("education", "major", "years", "certificates")
#: Turn 2 必须归一化、Turn 3 才写库的语义字段
LLM_NORMALIZE_FIELDS = ("hard_gates", "must_skills", "bonus_skills")

# --------------------------------------------------------------------------- #
# JD 技能条目**粒度护栏**配置（只增 warning 不拦写）
#
# 粒度标准：must_skills[] / bonus_skills[] 每一项必须是**原子的、可独立验证的
# 技能/能力名词短语**，不是句子碎片、不是单个泛化词。
#   ❌ 坏：「熟悉光伏行业生产」「质量」「安全相关标准」「能规范记录」「整理工艺数据」
#   ✅ 好：「光伏生产管理经验」「质量管理体系」「安全生产标准」「工艺数据记录规范」
# 下面三个阈值/黑名单**可配置**（改这里即可；SkillGranularityGuard.check 也接受覆盖参数）。
# --------------------------------------------------------------------------- #
#: 单条技能 ≤ 该字数 → 判「单个泛化词」告警（如「质量」=2 字）
SKILL_ITEM_TOO_SHORT_MAXLEN = 2
#: 单条技能 ≥ 该字数 → 判「整句碎片」告警（如「熟练使用Office办公软件及工艺分析工具」）
SKILL_ITEM_TOO_LONG_MINLEN = 30
#: 泛化软技能黑名单（子串匹配，大小写不敏感）——放水的重灾区；JD 明确列为必备时 agent 可保留，
#: 护栏只告警不拦写。**可配置**：增删这里的词即可。
SOFT_SKILL_BLACKLIST = (
    "沟通协调", "沟通能力", "沟通表达",
    "责任心", "责任感",
    "创新思维", "创新能力", "创新精神",
    "团队合作", "团队协作", "团队精神",
    "熟练使用office", "office办公软件", "办公软件", "熟练使用办公",
    "吃苦耐劳", "抗压能力", "抗压性",
    "学习能力", "执行力", "逻辑思维",
    "行业对标", "人才培养",
)
