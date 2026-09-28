# -*- coding: utf-8 -*-
"""弱依赖改造元测试（回归锁）：L2 软观察阈值禁止再硬化回丢弃路径。

历史事故（AGENTS.md 犯错记录）：审美类阈值（中文字数 2-6、总长 ≤12、技能数量区间、
缺门槛段、must∩bonus 重复）曾是 merge 的硬门槛，7 字中文术语"热镀铝锌硅钢板"触发
整行丢弃 → 队列卡死至次日 09:30 兜底（~11 小时）→ match_gated 门禁被非空队列阻塞。
现口径：格式问题由 normalize_row 确定性修复，质量问题进 observations 非阻断观察，
validate_row 只剩 L0（非 dict / 缺重 id / 三列全空，两链对称）。

本测试是**元测试**（同 test_single_source.py 风格）：扫源码文本 + 最小行为样例，
任何一处把软观察接回丢弃路径的改动都会在此炸响。行为断言与
test_soft_dependencies.py / test_jobs_soft_dependencies.py 刻意部分重复——
那两份锁"行为正确"，这份锁"结构不硬化"。纯本地、不触网、不构造 Notable()。
"""
import ast
import inspect
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "skills-analyze", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

import jobs_analyze as ja    # noqa: E402
import skills_analyze as sa  # noqa: E402
import soften                # noqa: E402

_PIPELINE_SRCS = (
    os.path.join(ROOT, "skills", "skills-analyze", "scripts", "skills_analyze.py"),
    os.path.join(ROOT, "skills", "job-intake", "scripts", "jobs_analyze.py"),
)
_SOFTEN_SRC = os.path.join(ROOT, "shared", "soften.py")
_SCANNED = _PIPELINE_SRCS + (_SOFTEN_SRC,)

# 已删除的旧硬阈值常量名（注释/docstring 里解释"为什么删"属允许，只禁 `NAME =` 定义复活）
_RETIRED_CONSTS = ("DONE_SKILL_ZH_RANGE", "DONE_SKILL_LEN_MAX",
                   "DONE_SKILLS_RANGE", "DONE_MUST_RANGE", "DONE_BONUS_RANGE")
# L2 软阈值：唯一定义处必须是 shared/soften.py（不变量 10）
_SOFT_CONSTS = ("TAG_LEN_MAX", "ZH_RANGE", "JD_MUST_RANGE", "JD_BONUS_RANGE")
# 软观察机制名：validate_row（丢弃路径）源码中禁止出现
_SOFT_HELPERS = ("over_len_words", "count_out_of_range", "soft_observations", "SOFT_SPECS")


def _src(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _func_code(fn):
    """取函数源码并剥掉 docstring（docstring 里解释"已移入 soft_observations"属允许；
    只有可执行代码里引用软观察机制才算丢弃路径可达）。"""
    src = inspect.getsource(fn)
    tree = ast.parse(src)
    body = tree.body[0].body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        lines = src.splitlines()
        src = "\n".join(lines[:body[0].lineno - 1] + lines[body[0].end_lineno:])
    return src


class TestRetiredThresholdsStayDead(unittest.TestCase):
    """旧硬阈值常量禁止在两条流水线脚本里以定义形态复活（soften.py 的
    JD_MUST_RANGE/JD_BONUS_RANGE 是合法唯一真源，故只扫流水线脚本）。"""

    def test_no_retired_const_definitions(self):
        for path in _PIPELINE_SRCS:
            src = _src(path)
            for name in _RETIRED_CONSTS:
                self.assertIsNone(
                    re.search(r"^\s*%s\s*=" % re.escape(name), src, re.M),
                    "%s 出现已删除阈值常量 %s 的定义（L2 阈值禁止再硬化，见 soften.py docstring）"
                    % (path, name))


class TestNoLocalCjkCounting(unittest.TestCase):
    """中文字数统计（引发原事故的机制）只许活在 shared/soften.py。"""

    def test_no_cjk_range_idiom_in_pipelines(self):
        for path in _PIPELINE_SRCS:
            src = _src(path)
            for idiom in ('"一" <=', '<= "鿿"', r"[\u4e00-\u9fff]"):
                self.assertNotIn(idiom, src,
                                 "%s 出现本地中文字数统计惯用法 %r（唯一真源 shared/soften.py）"
                                 % (path, idiom))


class TestDiscardPathUnreachableFromSoftHelpers(unittest.TestCase):
    """validate_row 是唯一可丢行判定：其可执行代码禁止引用软观察机制。"""

    def test_resume_validate_row_has_no_soft_helpers(self):
        code = _func_code(sa.validate_row)
        for name in _SOFT_HELPERS:
            self.assertNotIn(name, code,
                             "skills_analyze.validate_row 出现软观察机制 %r（观察禁止参与丢弃决策）"
                             % name)

    def test_job_validate_row_has_no_soft_helpers(self):
        code = _func_code(ja.validate_row)
        for name in _SOFT_HELPERS:
            self.assertNotIn(name, code,
                             "jobs_analyze.validate_row 出现软观察机制 %r（观察禁止参与丢弃决策）"
                             % name)


class TestMergeReportContract(unittest.TestCase):
    """两链 merge 必须产出新报告契约键，且旧 bad_rows 键禁止复活。"""

    def test_new_keys_present_old_key_gone(self):
        for path in _PIPELINE_SRCS:
            src = _src(path)
            for key in ("dropped_rows", "normalizations", "observations"):
                self.assertIn(key, src, "%s 缺新报告契约键 %s" % (path, key))
            self.assertNotIn("bad_rows", src,
                             "%s 出现已废键 bad_rows（现为 dropped_rows + observations）" % path)


class TestSoftThresholdSingleSource(unittest.TestCase):
    """不变量 10：L2 软阈值在三个被扫文件里有且仅有 shared/soften.py 一处定义。"""

    def test_defined_exactly_once_in_soften(self):
        for name in _SOFT_CONSTS:
            pat = re.compile(r"^\s*%s\s*=" % re.escape(name), re.M)
            hits = [p for p in _SCANNED if pat.search(_src(p))]
            self.assertEqual(hits, [_SOFTEN_SRC],
                             "%s 的定义处应为且仅为 shared/soften.py，实际：%s" % (name, hits))


class TestCanonicalSamplesStillAccepted(unittest.TestCase):
    """端到端最小回归样例：曾被旧硬门槛整行拒绝的形态，normalize_row → validate_row
    必须零错误（本类刻意与行为测试部分重复——有人再硬化门槛时元测试要第一个炸）。"""

    def test_resume_seven_char_zh_tag_accepted(self):
        row = {"id": "meta1",
               "skills": ["热镀铝锌硅钢板", "扫描电子显微镜", "质量管理体系认证",
                          "Continuous Plating Line", "PLC"],
               "ai_structured": "学历背景｜a\n工作经验｜b\n核心技能｜c\n求职意向｜d\n匹配度评估｜e",
               "ai_deep": "亮点：x"}
        nr, _ = sa.normalize_row(row)
        self.assertEqual(sa.validate_row(nr), [])

    def test_job_three_must_skills_accepted(self):
        row = {"job_id": "metaJ1",
               "hard_gates": "学历：本科及以上；专业：机械；经验：2年；证书：无；年龄：25-40",
               "must_skills": "设备运维、点检、故障维修",
               "bonus_skills": "光伏、TPM"}
        nr, _ = ja.normalize_row(row)
        self.assertEqual(ja.validate_row(nr), [])

    def test_job_missing_cert_segment_filled_not_rejected(self):
        row = {"job_id": "metaJ2",
               "hard_gates": "学历：本科及以上；专业：机械；经验：2年；年龄：25-40",
               "must_skills": "设备运维、点检、故障维修、备件管理、机械、电气",
               "bonus_skills": "光伏、TPM、项目管理、培训带教"}
        self.assertNotIn("证书：", row["hard_gates"])
        nr, kinds = ja.normalize_row(row)
        self.assertEqual(ja.validate_row(nr), [])
        self.assertIn("证书：", nr["hard_gates"])   # 归一化补段而非丢行
        self.assertIn("gate_filled", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
