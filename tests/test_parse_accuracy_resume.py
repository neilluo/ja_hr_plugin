#!/usr/bin/env python3
"""A 层正则修复回归：真值审计（outputs/audit_report_resume_*.json）暴露的系统性解析错误。

夹具文本 = 审计原件（outputs/audit_src/resume__*.txt）的最小内联片段，不依赖 outputs 文件存在。
覆盖：证书长词完整提取（初级会计职称/特种作业操作证/低压电工证）、期望职位不吃后续标签、
院校独立学院名不截断、专业表格行兜底与标签式并存。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

from parse_resume import parse as parse_resume  # noqa: E402


class TestCertificates(unittest.TestCase):
    """审计案例：许金（初级会计职称）、杨自雄（特种作业操作证）、田海飞（低压电工证被截成电工证）。"""

    def test_primary_accountant_title_full(self):
        # 片段源：resume__【财务主管_曲靖 8-13K】许金 10年.pdf.txt
        text = "证\n\n书\n\n2019.05 初级会计职称\n2020.09 中级会计职称\n"
        certs = parse_resume(text, "许金.pdf")["certificates"]
        self.assertIn("初级会计职称", certs)
        self.assertNotIn("初级会计", certs)   # 旧 `[职称]` 字符类只吃单字会截断

    def test_special_operation_cert_full(self):
        # 片段源：resume__杨自雄_29岁_电气工程师.pdf.txt（原件该行被版面切断）
        text = "证书：高级电工职业技能证书\n\n特种作业操作证（低压电\n"
        certs = parse_resume(text, "杨自雄.pdf")["certificates"]
        self.assertIn("特种作业操作证", certs)
        self.assertNotIn("特种作业", certs)   # 长词优先：不再被短交替抢先截断

    def test_low_voltage_electrician_cert(self):
        # 片段源：resume__田海飞 23年毕业-设备助工.pdf.txt（表内旧值被截为"电工证"）
        text = "奖项证书\n低压电工证\n\n自我评价\n"
        certs = parse_resume(text, "田海飞.pdf")["certificates"]
        self.assertIn("低压电工证", certs)
        self.assertNotIn("电工证", certs)


class TestExpectedPosition(unittest.TestCase):
    """审计案例：黄绍华（吃掉"应聘企业"尾巴）、马雷震（吃掉"期望工资"尾巴）。"""

    def test_position_stops_before_employer_label(self):
        # 片段源：resume__黄绍华_组件工艺工程师.pdf.txt
        text = ("黄绍华\n应聘职位：组件工艺工程师 应聘企业：曲靖晶澳太阳能科技有限公司\n"
                "电子邮箱：386248819@qq.com 手机号码：13273707026\n")
        self.assertEqual(parse_resume(text, "黄绍华.pdf")["expected_position"],
                         "组件工艺工程师")

    def test_position_stops_before_salary_label(self):
        # 片段源：resume__个人简历-马雷震－镀膜制程新.docx.txt（多空格分隔）
        text = "应聘岗位：设备镀膜制程主管     期望工资：20K/月\n"
        self.assertEqual(parse_resume(text, "马雷震.docx")["expected_position"],
                         "设备镀膜制程主管")


class TestSchool(unittest.TestCase):
    def test_independent_college_not_truncated(self):
        # 片段源：resume__【财务主管_曲靖 8-13K】许金 10年.pdf.txt
        # 旧非贪婪 {2,14}?(?:大学|学院) 在第一个后缀"大学"处截断成"贵州财经大学"
        text = "教育背景\n学校名称：贵州财经大学商务学院\n就读时间：2012.09-2016.07\n"
        self.assertEqual(parse_resume(text, "许金.pdf")["school"],
                         "贵州财经大学商务学院")


class TestMajor(unittest.TestCase):
    def test_table_row_fallback(self):
        # 片段源：resume__个人简历-周彦淇-单晶设备.pdf.txt（「院校|专业|学历」表格行，无"专业"标签）
        text = "教育经历\n哈尔滨远东理工学院 | 机械设计制造及其自动化 | 本科 2014/09-2018/06\n"
        self.assertEqual(parse_resume(text, "周彦淇.pdf")["major"],
                         "机械设计制造及其自动化")

    def test_label_form_still_works(self):
        # 标签式「专业：X」为原有主路径，表格兜底不得挤掉它
        text = "教育背景\n专业：工程管理\n学历：本科\n"
        self.assertEqual(parse_resume(text, "x.pdf")["major"], "工程管理")


if __name__ == "__main__":
    unittest.main()
