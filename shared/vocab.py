# -*- coding: utf-8 -*-
"""vocab.py — 跨 skill 公共词表与分词（唯一数据源，AGENTS.md 双源零容忍）。

- SKILL_WORDS：技能词表并集。parse_job（识别岗位必备技能）与 parse_resume（打简历技能标签）
  共用同一份——改这里两处解析同时生效，杜绝岗位/简历词表漂移导致 check_skill_coverage 命中率恒低。
- SEP / toks()：统一分词。match-verify / job-intake / skills-analyze 三方共用，禁止各自再抄一份。

边界：语义等价词典（SYNONYM 同义 / HYPERS 上下位）是 match-verify 私有的 semantic_score.py，
只服务匹配打分，不在此文件、禁止提升到 shared（见 AGENTS.md 代码归属条款）。
"""
import re

# 分词分隔符：顿号/中英文逗号/中英文分号/斜杠
SEP = r"[、,，;；/]\s*"

# 技能词表并集（parse_job._SKILL_WORDS ∪ parse_resume._SKILL_WORDS，去重）。
# 这是"原始技能词清单"的唯一源；语义同义/上下位归并见 semantic_score.py。
SKILL_WORDS = (
    "暖通 PLC CAD 电气 设备运维 设备管理 工艺 切片 镀膜 组件 电池 硅片 单晶 拉晶 "
    "EHS 安全管理 财务 成本 预算 Python Java SQL Excel SAP MES ERP 自动化 机电 机械 "
    "焊接 电工 点检 TPM 精益 六西格玛 SPC 质量管理 ISO9001 数据分析 SolidWorks 西门子 "
    "三菱 欧姆龙 变频器 伺服 光伏 半导体 扩散 PECVD 丝网印刷 层压 串焊 排程 项目管理 "
    "团队建设 Office Word PPT 账务 税务 审计 报表 会计 出纳 高压 特种设备 消防"
).split()

# 长词优先排序：避免「设备管理」被「设备」抢先命中导致长词丢失。
SKILL_WORDS_SORTED = tuple(sorted(set(SKILL_WORDS), key=lambda w: (-len(w), w)))


def toks(s):
    """把字段值（list 或分隔符字符串）分词为 strip 后的非空 token 列表。"""
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    return [t.strip() for t in re.split(SEP, s or "") if t.strip()]
