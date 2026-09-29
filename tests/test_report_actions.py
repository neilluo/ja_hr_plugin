# -*- coding: utf-8 -*-
"""性能确定性改造回归：把"agent 手写/复核/绕路"的确定性动作下沉为脚本产物。

覆盖三处此前靠 agent 自觉、实测浪费回合的环节：
  1) shared/report.py：user_line（一句话结论，agent 原样复述）/ next_action（下一步动作）/
     created_summary（回带入库记录，免再跑 query.py）/ cron_job 字段前置。
  2) upload_resumes._scan：单个文件路径直接入库，不再需要 /tmp 建软链绕路。
  3) refine_loop.trigger：队列非空时产出现成 cron_job 规格（every 型，注册永不过期）。

不触网：report/_scan 是纯函数；trigger 用最小 mock。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import report              # noqa: E402
import refine_loop         # noqa: E402
import upload_resumes as ur  # noqa: E402


class TestUserLine(unittest.TestCase):
    def test_ok_resume_with_queue(self):
        r = {"kind": "简历", "created": 1, "readback_missing": [], "failed": [],
             "needs_ocr": [], "skipped_dup": [], "table_total": 1, "refine_queued": 1}
        line = report.user_line(r)
        self.assertIn("简历入库完成", line)
        self.assertIn("新增 1 条", line)
        self.assertIn("表内共 1 条", line)
        # 延迟与兜底时刻必须从真源派生，不能是写死的数字
        self.assertIn(refine_loop.every_human(), line)
        self.assertIn(refine_loop.fallback_hhmm(), line)

    def test_warn_needs_ocr(self):
        r = {"kind": "简历", "created": 2, "readback_missing": [], "failed": [],
             "needs_ocr": ["a.pdf", "b.png"], "skipped_dup": [], "table_total": 2}
        line = report.user_line(r)
        self.assertIn("部分完成", line)
        self.assertIn("2 份扫描件待视觉补录", line)

    def test_bad_readback_missing(self):
        r = {"kind": "岗位", "created": 0, "readback_missing": ["J1"], "failed": [],
             "needs_ocr": [], "skipped_dup": []}
        line = report.user_line(r)
        self.assertIn("入库失败", line)
        self.assertIn("重跑同目录", line)

    def test_default_kind_when_absent(self):
        r = {"created": 1, "readback_missing": [], "failed": [], "needs_ocr": [], "skipped_dup": []}
        self.assertIn("记录入库完成", report.user_line(r))


class TestNextAction(unittest.TestCase):
    def test_with_cron_job(self):
        r = {"cron_job": {"name": "x"}, "created": 1, "readback_missing": [],
             "failed": [], "needs_ocr": [], "skipped_dup": []}
        na = report.next_action(r)
        self.assertIn("原样", na)                 # cron_job 原样注册
        self.assertIn("不要再跑 query.py", na)      # 禁止违规复核
        self.assertIn("user_line", na)

    def test_with_ocr_and_missing(self):
        r = {"needs_ocr": ["a.png"], "readback_missing": ["138"], "created": 1,
             "failed": [], "skipped_dup": []}
        na = report.next_action(r)
        self.assertIn("--backfill", na)
        self.assertIn("重跑同一目录", na)


class TestSummarize(unittest.TestCase):
    def test_picks_only_summary_fields_and_caps(self):
        rows = [{"name": "张三", "phone": "138", "full_text": "x" * 9999,
                 "attachment": {"url": "y"}, "school": "北大"}
                for _ in range(report.SUMMARY_MAX + 3)]
        s = report.summarize(rows)
        self.assertEqual(len(s), report.SUMMARY_MAX + 1)          # +1 是溢出计数项
        self.assertNotIn("full_text", s[0])                        # 大字段不回带
        self.assertNotIn("attachment", s[0])
        self.assertIn("name", s[0])
        self.assertEqual(s[-1]["_more"], 3)

    def test_within_limit_no_overflow(self):
        s = report.summarize([{"name": "李四"}])
        self.assertEqual(s, [{"name": "李四"}])


class TestEnrichOrdering(unittest.TestCase):
    """user_line / next_action / cron_job 必须前置于 timing_ms，且 enrich 幂等。"""

    def test_enrich_adds_and_print_orders(self):
        import contextlib
        import io
        import json
        r = {"kind": "简历", "created": 1, "readback_missing": [], "failed": [],
             "needs_ocr": [], "skipped_dup": [], "table_total": 1, "refine_queued": 1,
             "cron_job": {"name": "n"}, "timing_ms": {"total": 5}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.print_report(r, created_rows=[{"name": "张三", "phone": "138"}])
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("VERDICT:OK"))
        ordered = json.loads("\n".join(lines[1:]))
        keys = list(ordered.keys())
        self.assertEqual(keys[0], "user_line")
        for k in ("next_action", "created_summary", "cron_job"):
            self.assertIn(k, keys)
            self.assertLess(keys.index(k), keys.index("timing_ms"))


class TestScanSingleFile(unittest.TestCase):
    """单个文件路径直接入库：不再需要 /tmp 建软链绕路（曾多花 2 个回合）。"""

    def test_single_file_returns_its_dir_and_basename(self):
        d = tempfile.mkdtemp(prefix="ur_scan_")
        try:
            p = os.path.join(d, "董荣飞-单晶设备高级工程师.docx")
            with open(p, "wb") as f:
                f.write(b"x")
            # 同目录还有别的文件，单文件模式必须只取它自己
            with open(os.path.join(d, "other.pdf"), "wb") as f:
                f.write(b"y")
            base, files = ur._scan(p)
            self.assertEqual(os.path.realpath(base), os.path.realpath(d))
            self.assertEqual(files, ["董荣飞-单晶设备高级工程师.docx"])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_dir_returns_all_supported_sorted(self):
        d = tempfile.mkdtemp(prefix="ur_scan_dir_")
        try:
            for name in ("b.docx", "a.pdf", "skip.xyz"):
                with open(os.path.join(d, name), "wb") as f:
                    f.write(b"z")
            base, files = ur._scan(d)
            self.assertEqual(base, d)
            self.assertEqual(files, ["a.pdf", "b.docx"])     # 排序、且过滤不支持格式
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class _MiniNT:
    """trigger 的最小 mock：只需 list_records 返回队列行。"""

    def __init__(self, rows):
        self._rows = rows

    def list_records(self, table, flt=None, biz_fields=None):
        return self._rows


class TestTrigger(unittest.TestCase):
    def test_nonempty_queue_emits_cron_job_every_type(self):
        # 一条 ai_refined_at 空、full_text 非空的 resume 行 = 在队列
        nt = _MiniNT([{"id": "r1", "fields": {"ai_refined_at": None, "full_text": "正文",
                                              "source_file": None, "name": "张三"}}])
        rep = {}
        n = refine_loop.trigger(nt, "resume", rep, root="/tmp/repo")
        self.assertEqual(n, 1)
        self.assertEqual(rep["refine_queued"], 1)
        # every 型无绝对时刻：不再有 refine_fire_at 字段（at 型已废弃，见 refine_loop.EVERY_MS 注释）
        self.assertNotIn("refine_fire_at", rep)
        self.assertIn("cron_job", rep)
        self.assertTrue(rep["cron_job"]["name"].startswith(refine_loop.TASK_PREFIX["resume"]))
        self.assertEqual(rep["cron_job"]["schedule"],
                         {"kind": "every", "everyMs": refine_loop.EVERY_MS})

    def test_empty_queue_no_cron_job(self):
        nt = _MiniNT([{"id": "r1", "fields": {"ai_refined_at": 123, "full_text": "正文",
                                              "source_file": None, "name": "张三"}}])
        rep = {}
        n = refine_loop.trigger(nt, "resume", rep, root="/tmp/repo")
        self.assertEqual(n, 0)
        self.assertEqual(rep["refine_queued"], 0)
        self.assertNotIn("refine_fire_at", rep)
        self.assertNotIn("cron_job", rep)


if __name__ == "__main__":
    unittest.main()
