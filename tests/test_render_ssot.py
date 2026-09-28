# -*- coding: utf-8 -*-
"""单源渲染与死代码清理回归（code-first sweep）。

覆盖三块：
  1. render_prompts 的 SSOT 注入：subagent-prompt.md 模板不得手抄 schema 数值
     （5-12 / 2-6 / 200 字 / 段名 / 校正字段名），渲染产物必须包含 DONE_* 与
     CORRECTIONS 的真源值、且占位符全部替换干净（不变量 10，与 test_dispatch_slim
     的"占位符替换干净"契约互补：那边锁路径占位符，这边锁 schema 占位符）。
  2. upload_resumes 的 needs_ocr 报告绝对路径（agent 直接当 `_file` 用，不再手拼目录+文件名）。
  3. refine_loop.STALE_AFTER_S 是 1800 的唯一出现：acquire_lock 默认值引用它，
     模块内不得再有第二份 1800 字面量。
纯本地文件操作，不触网。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "skills-analyze", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import refine_loop                                # noqa: E402
import skills_analyze as sa                       # noqa: E402
from skills_apply import CORRECTIONS              # noqa: E402
import upload_resumes as ur                       # noqa: E402

TPL = os.path.join(ROOT, "skills", "skills-analyze", "references", "subagent-prompt.md")


class TestPromptSchemaSSOT(unittest.TestCase):
    """schema 数值/段名/校正字段名只存在于 .py 常量，模板用占位符、渲染时注入。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sa_ssot_")
        self.old_outdir = sa.OUTDIR
        sa.OUTDIR = self.tmp
        with open(TPL, encoding="utf-8") as f:
            self.tpl = f.read()
        with open(sa.render_prompts(1, os.path.join(self.tmp, "job_vocab.json"))[0],
                  encoding="utf-8") as f:
            self.body = f.read()

    def tearDown(self):
        sa.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_has_no_hand_copied_schema_values(self):
        lo, hi = sa.DONE_SKILLS_RANGE
        zlo, zhi = sa.DONE_SKILL_ZH_RANGE
        # 数值口径不得手抄进模板（"5-12 个"/"2-6 字"/"200 字以内"）
        for residue in ("%d-%d 个" % (lo, hi), "%d-%d 字" % (zlo, zhi),
                        "%d 字以内" % sa.DONE_TEXT_MAX):
            self.assertNotIn(residue, self.tpl, "模板手抄了 schema 数值：%s" % residue)
        # 段名不得手抄进模板
        for seg in sa.DONE_STRUCTURED_SEGS:
            self.assertNotIn(seg, self.tpl, "模板手抄了段名：%s" % seg)
        # §4 的字段名枚举必须是占位符驱动（CORRECTIONS 注入），而非手写清单：
        # 模板须含 <SEG_i>/<CORR_i> 占位符（渲染时由 DONE_*/CORRECTIONS 填入）。
        for i in range(1, len(sa.DONE_STRUCTURED_SEGS) + 1):
            self.assertIn("<SEG_%d>" % i, self.tpl)
        for i in range(1, len(CORRECTIONS) + 1):
            self.assertIn("<CORR_%d>" % i, self.tpl)

    def test_rendered_contains_ssot_values_and_no_placeholders(self):
        lo, hi = sa.DONE_SKILLS_RANGE
        zlo, zhi = sa.DONE_SKILL_ZH_RANGE
        self.assertIn("%d-%d 个" % (lo, hi), self.body)
        self.assertIn("%d-%d 字" % (zlo, zhi), self.body)
        self.assertIn("%d 字以内" % sa.DONE_TEXT_MAX, self.body)
        for seg in sa.DONE_STRUCTURED_SEGS:
            self.assertIn(seg, self.body)
        for k in CORRECTIONS:
            self.assertIn("`%s`" % k, self.body)
        # 全部占位符（路径类 + schema 类）替换干净
        for ph in re.findall(r"<[A-Z][A-Z_0-9]*>", self.body):
            self.fail("渲染产物残留占位符：%s" % ph)


class TestNeedsOcrAbsPaths(unittest.TestCase):
    """needs_ocr 报绝对路径：无联系方式的 .txt 落 needs_ocr，dry-run 零触网。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ur_ocr_")
        self.old_cwd = os.getcwd()
        # 相对目录入参：证明 needs_ocr 也是绝对路径（agent 的 CWD 未必相同）
        self.rel = os.path.relpath(self.tmp, ROOT)
        os.chdir(ROOT)
        with open(os.path.join(self.tmp, "无联系方式.txt"), "w", encoding="utf-8") as f:
            f.write("这是一份抽不出手机号和邮箱的简历文本。")
        self._old_pf = ur.run_preflight
        ur.run_preflight = lambda **kw: None  # 免环境依赖，只测 needs_ocr 口径

    def tearDown(self):
        ur.run_preflight = self._old_pf
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_needs_ocr_entries_are_absolute(self):
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["upload_resumes.py", self.rel, "--dry-run"]
        try:
            with redirect_stdout(buf):
                ur.main()
        except SystemExit as e:
            self.assertIn(e.code, (0, None))
        finally:
            sys.argv = old_argv
        lines = buf.getvalue().strip().splitlines()
        rep = json.loads("\n".join(lines[1:]))   # 首行 VERDICT，其后 JSON（report.py 契约）
        self.assertEqual(len(rep["needs_ocr"]), 1)
        p = rep["needs_ocr"][0]
        self.assertTrue(os.path.isabs(p), "needs_ocr 必须是绝对路径：%s" % p)
        self.assertEqual(p, os.path.join(self.tmp, "无联系方式.txt"))
        self.assertTrue(os.path.exists(p))


class TestStaleAfterSingleSource(unittest.TestCase):
    """1800 只允许以 STALE_AFTER_S 具名常量出现一次。"""

    def test_literal_1800_only_in_constant(self):
        with open(os.path.join(ROOT, "shared", "refine_loop.py"), encoding="utf-8") as f:
            src = f.read()
        hits = re.findall(r"1800", src)
        self.assertEqual(len(hits), 1, "refine_loop.py 出现多处 1800：%d" % len(hits))
        self.assertIn("STALE_AFTER_S = 1800", src)
        self.assertEqual(refine_loop.STALE_AFTER_S, 1800)

    def test_acquire_lock_default_uses_constant(self):
        import inspect
        sig = inspect.signature(refine_loop.acquire_lock)
        self.assertEqual(sig.parameters["stale_after"].default, refine_loop.STALE_AFTER_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
