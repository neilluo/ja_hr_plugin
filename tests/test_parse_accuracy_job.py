# -*- coding: utf-8 -*-
"""岗位解析精度回归（真值审计案例内联夹具，不依赖 outputs/ 文件存在）：
① responsibilities 不被正文动词"审核/起草/批准"截断，仅行首表单字段形态截断；
② work_location 正文无城市时回退扫文件名，两处皆无信号则留空（不盲目兜底曲靖）。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

from parse_job import parse as parse_job  # noqa: E402

# 财务主管 JD 最小片段（源自 outputs/audit_src 真实审计案例）：
# 第 3 条正文含动词"审核"，旧版 _SEC_END_RE 在此处误截断。
_FINANCE_JD = """职位名称 Position:  财务主管
工作部门 Dept.: 财经管理部
主要工作职责 Major responsibilities
1、对财务数据进行日常监控，保障财务工作合规性与规范性；
2、组织编制月度、季度、年度财务报表，为管理层提供详细财务分析报告；
3、负责审核公司税务申报与缴纳工作，确保税务处理合法合规，研究税收政策变化，进行税务筹划以降低税务成本；
4、负责本公司财务核算审核，账务及各报表的审核；
关键绩效指标（KPI）Key Performance Indicators
1、结账计划安排及审核上报金蝶报表
任职要求 Qualifications
1、教育背景：本科及以上、会计类等相关专业"""

# 表单字段形态：行首"审核：张三"仍应作为停止词截断。
_FORM_LINE_JD = """岗位职责
1、负责部门日常事务。
2、配合完成月度报表。
审核：张三
批准：李四"""


class TestResponsibilitiesTruncation(unittest.TestCase):
    def test_body_verb_not_stop_word(self):
        """正文动词"审核"不截断职责段（含 KPI 前整段职责）。"""
        j = parse_job(_FINANCE_JD, "岗位说明书--制造中心-曲靖制造基地-财经管理部-财务主管.doc")
        resp = j["responsibilities"]
        self.assertIn("负责审核公司税务申报与缴纳工作", resp)
        self.assertIn("进行税务筹划以降低税务成本", resp)
        # 第 4 条（"审核"出现两次）也完整保留
        self.assertIn("负责本公司财务核算审核", resp)
        # 仍应在 KPI 段前截断
        self.assertNotIn("关键绩效指标", resp)
        self.assertNotIn("金蝶报表", resp)

    def test_form_field_line_still_stops(self):
        """行首表单字段形态（审核：）仍截断。"""
        j = parse_job(_FORM_LINE_JD, "岗位说明书-制造中心-技术部-主管.doc")
        resp = j["responsibilities"]
        self.assertIn("配合完成月度报表", resp)
        self.assertNotIn("张三", resp)
        self.assertNotIn("李四", resp)

    def test_requirements_section_intact(self):
        """任职要求段不受改动影响。"""
        j = parse_job(_FINANCE_JD, "岗位说明书--制造中心-曲靖制造基地-财经管理部-财务主管.doc")
        self.assertIn("本科及以上", j["requirements"])


class TestWorkLocation(unittest.TestCase):
    def test_fallback_to_filename(self):
        """正文无城市、文件名含"曲靖制造基地" → 从文件名扫出曲靖。"""
        j = parse_job(_FINANCE_JD,
                      "岗位说明书--制造中心-曲靖制造基地-财经管理部-财务主管.doc")
        self.assertEqual(j["work_location"], ["曲靖"])

    def test_empty_when_no_signal(self):
        """正文与文件名都无城市 → 留空，不再盲目兜底。"""
        j = parse_job(_FINANCE_JD, "岗位说明书-制造中心-财经管理部-财务主管.doc")
        self.assertEqual(j["work_location"], [])

    def test_body_city_wins_and_dedup(self):
        """正文有城市时优先正文，去重保序。"""
        text = "工作地点：曲靖\n岗位职责\n1、负责曲靖基地设备运维。"
        j = parse_job(text, "岗位说明书-制造中心-技术部-工程师.doc")
        self.assertEqual(j["work_location"], ["曲靖"])


if __name__ == "__main__":
    unittest.main()
