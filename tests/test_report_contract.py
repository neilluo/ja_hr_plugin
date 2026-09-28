#!/usr/bin/env python3
"""shared/report.py 契约回归：VERDICT 分级 + 结果字段前置 + 不丢字段。

存在理由：agent 曾扫 JSON 开头误判"报告缺 created"（实为排在 timing_ms 之后被看漏），
白白多绕两回合。修复下沉到代码——先打 VERDICT 结论行、结果字段排最前。本测试锁死该
契约，防止退化回"字段散落、需读全才懂"（AGENTS.md：代码保障 > 文档约束）。
"""

import contextlib
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))

import report  # noqa: E402


class TestVerdict(unittest.TestCase):
    def test_ok(self):
        r = {"created": 28, "readback_missing": [], "failed": [], "needs_ocr": [],
             "skipped_dup": [], "table_total": 28, "refine_queued": 28}
        self.assertTrue(report.verdict(r).startswith("VERDICT:OK "))

    def test_warn_on_needs_ocr(self):
        r = {"created": 28, "readback_missing": [], "failed": [], "needs_ocr": ["a.pdf"],
             "skipped_dup": []}
        self.assertTrue(report.verdict(r).startswith("VERDICT:WARN "))

    def test_warn_on_failed(self):
        r = {"created": 1, "readback_missing": [], "failed": [{"file": "x", "error": "e"}],
             "needs_ocr": []}
        self.assertTrue(report.verdict(r).startswith("VERDICT:WARN "))

    def test_bad_on_readback_missing(self):
        r = {"created": 1, "readback_missing": ["138"], "failed": [], "needs_ocr": []}
        self.assertTrue(report.verdict(r).startswith("VERDICT:BAD "))

    def test_bad_on_error(self):
        r = {"created": 0, "readback_missing": [], "failed": [], "needs_ocr": [],
             "error": "回读/查重失败: boom"}
        v = report.verdict(r)
        self.assertTrue(v.startswith("VERDICT:BAD "))
        # error 自由文本里的花括号必须被剥掉，否则污染下游 {..} 正则抠 JSON
        self.assertNotIn("{", v)
        self.assertNotIn("}", v)


class TestKeyOrdering(unittest.TestCase):
    def test_result_fields_before_timing(self):
        rep = {"total": 31, "parsed": 28, "skipped_dup": [], "needs_ocr": [], "failed": [],
               "duplicates_removed": 0, "timing_ms": {"total": 7199},
               "created": 28, "readback_missing": [], "table_total": 28, "refine_queued": 28}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.print_report(rep)
        out = buf.getvalue().splitlines()
        self.assertTrue(out[0].startswith("VERDICT:OK"))
        ordered = json.loads("\n".join(out[1:]))
        keys = list(ordered.keys())
        # 判成功/失败的关键字段必须排在 timing_ms 之前
        for k in ("created", "readback_missing", "table_total", "refine_queued"):
            self.assertLess(keys.index(k), keys.index("timing_ms"),
                            "%s 应前置于 timing_ms，实际顺序 %s" % (k, keys))

    def test_no_field_dropped(self):
        rep = {"created": 1, "readback_missing": [], "failed": [], "needs_ocr": [],
               "skipped_dup": [], "custom_diag": {"nested": 1}, "timing_ms": {"total": 1}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.print_report(rep)
        ordered = json.loads("\n".join(buf.getvalue().splitlines()[1:]))
        self.assertEqual(set(ordered.keys()), set(rep.keys()))
        self.assertEqual(ordered["custom_diag"], {"nested": 1})


if __name__ == "__main__":
    unittest.main()
