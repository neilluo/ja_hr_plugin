#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""招聘看板生成器回归测试（mock，不触真网）。

覆盖：
  1. aggregate 确定性聚合：漏斗合计、未匹配岗位计数、Top-N 按 total_score 降序、
     部门分布（在招/总数）、as_of = max(submit_time)（毫秒时间戳派生显示串）。
  2. render_html：非空、含中文标签与关键数字；job=0 渲染空态不崩溃；
     match=0 提示先跑 match-verify。
  3. build 端到端（FakeNT + 临时输出路径）：写文件、返回一行摘要。
  4. 只读纪律：FakeNT 未提供 create/update/delete/call，任何写路径都会 AttributeError。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "recruit-dashboard", "scripts"))

import build_dashboard as bd  # noqa: E402


def _cfg():
    return bd.load_config()


def _ms(y, m, d, hh=0, mm=0):
    return int(time.mktime(time.strptime("%04d-%02d-%02d %02d:%02d" % (y, m, d, hh, mm),
                                         "%Y-%m-%d %H:%M")) * 1000)


class FakeNT:
    """按表返回预置行。无 create/update/delete/call：build 若走写路径立即 AttributeError。"""

    def __init__(self, job=None, match=None):
        self.rows = {"job": job or [], "match": match or []}
        self.calls = []

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        self.calls.append(table)
        return [dict(r, fields=dict(r["fields"])) for r in self.rows.get(table, [])]


JOBS = [
    {"id": "j1", "fields": {"job_id": "J1", "job_name": "暖通工程师", "department": "厂务管理部-暖通组",
                            "status": "招聘中", "submit_time": _ms(2026, 9, 1, 10, 0),
                            "stat_total": 10.0, "stat_recommend": 3.0, "stat_pending": 4.0,
                            "stat_reject": 3.0}},
    {"id": "j2", "fields": {"job_id": "J2", "job_name": "电气工程师", "department": "厂务管理部-电力能源组",
                            "status": "招聘中", "submit_time": _ms(2026, 9, 20, 15, 30),
                            "stat_total": 5.0, "stat_recommend": 1.0, "stat_pending": 2.0,
                            "stat_reject": 2.0}},
    {"id": "j3", "fields": {"job_id": "J3", "job_name": "成本会计", "department": "财经管理部",
                            "status": "已关闭", "submit_time": None,
                            "stat_total": None, "stat_recommend": None,
                            "stat_pending": None, "stat_reject": None}},
]

MATCHES = [
    {"id": "m1", "fields": {"name": "张三", "job_name": "暖通工程师", "total_score": 65.0,
                            "recommend": "待定", "evidence": "e1"}},
    {"id": "m2", "fields": {"name": "李四", "job_name": "暖通工程师", "total_score": 92.0,
                            "recommend": "推荐", "evidence": "e2"}},
    {"id": "m3", "fields": {"name": "王五", "job_name": "电气工程师", "total_score": 88.0,
                            "recommend": "推荐", "evidence": "e3"}},
    {"id": "m4", "fields": {"name": "赵六", "job_name": "电气工程师", "total_score": 95.0,
                            "recommend": "推荐", "evidence": "e4"}},
    {"id": "m5", "fields": {"name": "孙七", "job_name": "成本会计", "total_score": 40.0,
                            "recommend": "不推荐", "evidence": "e5"}},
]


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.agg = bd.aggregate(JOBS, MATCHES, self.cfg, top=2)

    def test_funnel_sums(self):
        # stat_* 为空的 j3 不计入；合计 = j1 + j2
        self.assertEqual(self.agg["funnel"], {"total": 15.0, "recommend": 4.0,
                                              "pending": 6.0, "reject": 5.0})
        self.assertEqual(self.agg["unmatched_jobs"], 1)

    def test_top_n_ordering_and_limit(self):
        top = self.agg["top_candidates"]
        self.assertEqual([c["name"] for c in top], ["赵六", "李四"])   # 推荐内按 total_score 降序，取前 2
        self.assertEqual([c["total_score"] for c in top], [95.0, 92.0])

    def test_department_distribution(self):
        deps = {d: (o, n) for d, o, n in self.agg["departments"]}
        self.assertEqual(deps["厂务管理部-暖通组"], (1, 1))     # 在招 1 / 总 1
        self.assertEqual(deps["财经管理部"], (0, 1))            # 已关闭 → 在招 0
        self.assertEqual(sum(n for _, _, n in self.agg["departments"]), 3)

    def test_as_of_is_max_submit_time(self):
        self.assertEqual(self.agg["as_of_ms"], _ms(2026, 9, 20, 15, 30))
        self.assertEqual(self.agg["as_of"], "2026-09-20 15:30")

    def test_counts(self):
        self.assertEqual(self.agg["job_count"], 3)
        self.assertEqual(self.agg["match_count"], 5)

    def test_empty_tables_no_crash(self):
        agg = bd.aggregate([], [], self.cfg)
        self.assertEqual(agg["job_count"], 0)
        self.assertEqual(agg["as_of"], "—")
        self.assertEqual(agg["funnel"]["total"], 0.0)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def test_render_full(self):
        html = bd.render_html(bd.aggregate(JOBS, MATCHES, self.cfg, top=20), self.cfg, 20)
        self.assertTrue(html.startswith("<!DOCTYPE html>") and html.endswith("</html>"))
        for label in ("招聘看板", "岗位漏斗", "候选人总数", "推荐", "待定", "不推荐",
                      "部门分布", "数据截止时间", "2026-09-20 15:30", "赵六", "李四",
                      "暖通工程师", "厂务管理部-暖通组"):
            self.assertIn(label, html)
        self.assertNotIn("张三", html)   # 待定不进推荐榜

    def test_render_empty_job_table(self):
        html = bd.render_html(bd.aggregate([], [], self.cfg), self.cfg, 20)
        self.assertIn("岗位表为空", html)
        self.assertTrue(html.endswith("</html>"))

    def test_render_empty_match_table(self):
        html = bd.render_html(bd.aggregate(JOBS, [], self.cfg), self.cfg, 20)
        self.assertIn("匹配表为空", html)
        self.assertIn("先跑 match-verify", html)
        self.assertIn("未匹配", html)    # stat_* 为空显示未匹配而非 0


class TestBuildEndToEnd(unittest.TestCase):
    def test_build_writes_file_and_summary(self):
        d = tempfile.mkdtemp(prefix="dash_")
        out = os.path.join(d, "sub", "board.html")
        try:
            nt = FakeNT(job=JOBS, match=MATCHES)
            line = bd.build(nt, _cfg(), out, top=20)
            self.assertEqual(nt.calls, ["job", "match"])   # 只读：仅两次 list_records
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("招聘看板", html)
            self.assertIn("看板已生成：3 岗位 / 5 匹配记录 / 数据截止 2026-09-20 15:30", line)
            self.assertIn(out, line)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
