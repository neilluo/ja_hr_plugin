# -*- coding: utf-8 -*-
"""入口脚本冒烟测试（不触网）：

  1. 全部可执行入口 `--help` 必须 exit 0（--help 早退/argparse 帮助，不构造 Notable）；
  2. 多子命令入口（skills_analyze / jobs_analyze / match_analyze）用 inspect 断言
     所有 handler 签名一致 = handler(args)（历史教训：merge(nt) vs prepare(nt,args) TypeError）；
  3. 未知子命令 exit 非 0；
  4. 骨架单源（不变量 10）：parts 命名/清理只存在于 shared/analyze_parts.py；
     三流水线脚本必须 import analyze_parts 且本地无命名副本；
  5. sync_ai_columns 薄封装（不变量 10）：写入委托 skills_apply.apply_rows，
     本地无 top_up_options / 剔词重试副本。
"""
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
for _d in (("skills", "skills-analyze", "scripts"),
           ("skills", "job-intake", "scripts"),
           ("skills", "match-verify", "scripts")):
    sys.path.insert(0, os.path.join(ROOT, *_d))

import analyze_parts  # noqa: E402
import jobs_analyze  # noqa: E402
import match_analyze  # noqa: E402
import skills_analyze  # noqa: E402
import skills_apply  # noqa: E402
import sync_ai_columns  # noqa: E402

# 全部可执行入口（preflight 三件套除外；parse_*/semantic_score/refine_loop 等为库非入口）
ENTRIES = [
    "shared/query.py",
    "skills/resume-intake/scripts/upload_resumes.py",
    "skills/job-intake/scripts/upload_jobs.py",
    "skills/job-intake/scripts/jobs_analyze.py",
    "skills/job-intake/scripts/sync_job_columns.py",
    "skills/job-intake/scripts/check_skill_coverage.py",
    "skills/skills-analyze/scripts/skills_analyze.py",
    "skills/skills-analyze/scripts/skills_apply.py",
    "skills/skills-analyze/scripts/sync_ai_columns.py",
    "skills/match-verify/scripts/match_gated.py",
    "skills/match-verify/scripts/match_analyze.py",
    "skills/replicate/scripts/sync_schema.py",
    "skills/replicate/scripts/replicate_base.py",
]

SUBCMD_ENTRIES = {
    "skills_analyze": skills_analyze,
    "jobs_analyze": jobs_analyze,
    "match_analyze": match_analyze,
}


class TestHelpSmoke(unittest.TestCase):
    def test_all_entries_help_exit_zero(self):
        for rel in ENTRIES:
            path = os.path.join(ROOT, rel)
            with self.subTest(entry=rel):
                r = subprocess.run([sys.executable, path, "--help"],
                                   capture_output=True, text=True, timeout=60, cwd=ROOT)
                self.assertEqual(r.returncode, 0,
                                 "%s --help exit=%d stderr=%s" % (rel, r.returncode, r.stderr[-400:]))
                self.assertTrue(r.stdout.strip(), "%s --help 无输出" % rel)


class TestHandlerSignatures(unittest.TestCase):
    def test_all_handlers_single_args_param(self):
        # 历史教训：入口分派的多分支签名必须一致 = handler(args)，防 merge 必崩 TypeError
        for name, mod in SUBCMD_ENTRIES.items():
            handlers = mod.HANDLERS
            self.assertTrue(handlers, "%s 缺 HANDLERS 分派表" % name)
            sigs = {fn: str(inspect.signature(fn)) for fn in handlers.values()}
            for fn, sig in sigs.items():
                params = inspect.signature(fn).parameters
                with self.subTest(entry=name, handler=fn.__name__):
                    self.assertEqual(len(params), 1, "%s.%s 签名应为 handler(args): %s"
                                     % (name, fn.__name__, sig))
                    p = next(iter(params.values()))
                    self.assertEqual(p.default, inspect.Parameter.empty)
            self.assertEqual(len(set(sigs.values())), 1,
                             "%s handler 签名不一致: %s" % (name, sigs))

    def test_handlers_cover_doc_whitelist(self):
        # 分派表键 = main() 白名单（match_analyze 白名单为元组字面量，其余用 HANDLERS）
        src = open(os.path.join(ROOT, "skills", "match-verify", "scripts",
                                "match_analyze.py"), encoding="utf-8").read()
        self.assertIn('cmd not in ("prepare", "merge", "apply", "stats")', src)
        self.assertEqual(set(match_analyze.HANDLERS),
                         {"prepare", "merge", "apply", "stats"})

    def test_unknown_subcommand_exits_nonzero(self):
        for rel in ("skills/skills-analyze/scripts/skills_analyze.py",
                    "skills/job-intake/scripts/jobs_analyze.py",
                    "skills/match-verify/scripts/match_analyze.py"):
            with self.subTest(entry=rel):
                r = subprocess.run([sys.executable, os.path.join(ROOT, rel), "no-such-cmd"],
                                   capture_output=True, text=True, timeout=60, cwd=ROOT)
                self.assertNotEqual(r.returncode, 0)


class TestPartsSingleSource(unittest.TestCase):
    """不变量 10：parts 命名/清理逻辑只存在于 shared/analyze_parts.py。"""

    def _src(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_pipelines_use_analyze_parts(self):
        for parts in (("skills", "skills-analyze", "scripts", "skills_analyze.py"),
                      ("skills", "job-intake", "scripts", "jobs_analyze.py"),
                      ("skills", "match-verify", "scripts", "match_analyze.py")):
            src = self._src(*parts)
            self.assertIn("import analyze_parts", src, "%s 未走公共骨架" % (parts,))
            # 命名/清旧/批计数副本零残留（不变量 9）
            for residue in ("_pending_part%d", "_done_part%d",
                            'old.startswith("skills_', 'old.startswith("jobs_',
                            'old.startswith("match_', "while os.path.exists"):
                self.assertNotIn(residue, src, "%s 残留骨架副本 %r" % (parts, residue))

    def test_write_read_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="ap_smoke_")
        try:
            # 清旧：预置陈旧 part 必须被 write_parts 清掉
            for stale in ("skills_pending_part9.json", "skills_done_part9.json"):
                open(os.path.join(tmp, stale), "w").write("[]")
            items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                meta = analyze_parts.write_parts(tmp, "skills", items, {"total": 3, "queued": 3})
            self.assertEqual(meta["batches"], meta["agents"])
            self.assertEqual(sum(meta["agent_sizes"]), 3)
            self.assertEqual(json.loads(buf.getvalue()), meta)
            self.assertFalse(os.path.exists(os.path.join(tmp, "skills_pending_part9.json")))
            self.assertTrue(os.path.exists(analyze_parts.meta_path(tmp, "skills")))
            # 全 done → missing 空
            n = analyze_parts.count_pending(tmp, "skills")
            for i in range(1, n + 1):
                pend = json.load(open(analyze_parts.pending_path(tmp, "skills", i)))
                json.dump(pend, open(analyze_parts.done_path(tmp, "skills", i), "w"))
            rows, missing = analyze_parts.read_done(tmp, "skills")
            self.assertEqual([r["id"] for r in rows], ["a", "b", "c"])
            self.assertEqual(missing, [])
            # 缺一批 + 坏一批 → 都进 missing（坏批打印告警不抛）
            os.remove(analyze_parts.done_path(tmp, "skills", 1))
            with open(analyze_parts.done_path(tmp, "skills", n), "w") as f:
                f.write("{broken json")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rows, missing = analyze_parts.read_done(tmp, "skills")
            self.assertEqual(sorted(missing), sorted([1, n]))
            self.assertIn("解析失败", buf.getvalue())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestSyncAiColumnsThinWrapper(unittest.TestCase):
    """不变量 10：三列写回唯一实现 = skills_apply.apply_rows，sync_ai_columns 是薄封装。"""

    def test_no_duplicated_write_logic(self):
        src = open(os.path.join(ROOT, "skills", "skills-analyze", "scripts",
                                "sync_ai_columns.py"), encoding="utf-8")
        src = src.read()
        self.assertIn("from skills_apply import apply_rows", src)
        self.assertNotIn("def top_up_options", src)          # 扩选项副本已删
        self.assertNotIn("is invalid", src)                    # 剔词重试副本已删
        self.assertNotIn("update_records", src)                # 直写副本已删

    def test_delegates_to_apply_rows_without_stamp(self):
        # payload 行格式（extract/deep + 档案字段）→ apply_rows 转换正确、不打 ai_refined_at
        captured = {}

        def fake_apply_rows(nt, rows, stamp=True, require_three=True):
            captured.update(rows=rows, stamp=stamp, require_three=require_three)
            return {"updated": len(rows), "options_added": 0, "bad": [], "failed": []}

        old = sync_ai_columns.apply_rows
        sync_ai_columns.apply_rows = fake_apply_rows
        try:
            d = tempfile.mkdtemp(prefix="sac_smoke_")
            pj = os.path.join(d, "payload.json")
            json.dump({"13800000000": {"skills": ["暖通"], "extract": "e", "deep": "d",
                                       "name": "张三"}},
                      open(pj, "w", encoding="utf-8"), ensure_ascii=False)

            class FakeNT:
                def list_records(self, table, flt=None, biz_fields=None, limit=0):
                    return [{"id": "r1", "fields": {"name": "张三", "phone": "13800000000"}}]

            sync_ai_columns.Notable = lambda *a, **k: FakeNT()
            old_argv = sys.argv
            sys.argv = ["sync_ai_columns.py", pj]
            try:
                buf = io.StringIO()
                with self.assertRaises(SystemExit) as ctx:
                    with contextlib.redirect_stdout(buf):
                        sync_ai_columns.main()
                self.assertEqual(ctx.exception.code, 0)
                rep = json.loads(buf.getvalue())
                self.assertEqual(rep, {"total": 1, "synced": 1, "options_added": 0,
                                       "unmatched": [], "bad": [], "failed": []})
            finally:
                sys.argv = old_argv
                import shutil
                shutil.rmtree(d, ignore_errors=True)
        finally:
            sync_ai_columns.apply_rows = old

        self.assertFalse(captured["stamp"])
        self.assertFalse(captured["require_three"])
        row = captured["rows"][0]
        self.assertEqual(row["id"], "r1")
        self.assertEqual(row["extract"], "e")
        self.assertEqual(row["deep"], "d")
        self.assertEqual(row["phone"], "13800000000")
        self.assertNotIn("ai_refined_at", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
