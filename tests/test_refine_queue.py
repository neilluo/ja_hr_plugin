#!/usr/bin/env python3
"""精析异步队列回归测试（mock，不触真网）。

覆盖：
  1. 队列谓词（refine_loop.queue 经 mock nt）：有标记不入队 / 无标记有全文入队 /
     扫描件凭 source_file 入队（原件存在）/ 原件已删判 unrefinable 不入队；job 按 responsibilities。
  2. skills_analyze：prepare 候选来源 = 队列（meta 含 queued/unrefinable）；queue 子命令输出
     queue_counts JSON；merge 与 prepare/queue 签名一致（历史教训：分派签名不一致 TypeError）。
  3. skills_apply：写三列的同一次 update 里打 ai_refined_at（毫秒时间戳，每条都打）；
     三字段皆空的记录进 bad、不写不打标。
  4. upload_resumes：--backfill 写 source_file、不打 ai_refined_at（三列交后台读图，扫描件照常入队）；
     报告含 refine_queued（经 mock 全链路）。
  5. 谓词唯一真源：消费方脚本无本地谓词副本（不变量 10）。
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "skills-analyze", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import refine_loop                       # noqa: E402
import skills_analyze as sa              # noqa: E402
import skills_apply as sapi              # noqa: E402
import upload_resumes as ur              # noqa: E402
from notable import Notable              # noqa: E402

# 无 source_file 的最小夹具：供不关心原件路径的用例（queue 子命令计数、prepare 切批）复用。
# 谓词与原件存在性相关的用例（含扫描件入队/unrefinable）用 TestQueuePredicate._rows()，
# 那里须在 setUp 内建真实临时文件，模块级常量做不到。
RESUME_ROWS = [
    {"id": "r1", "fields": {"ai_refined_at": 1789924509618, "full_text": "已精析",
                            "name": "甲", "skills": ["CAD"], "upload_time": 1}},
    {"id": "r2", "fields": {"ai_refined_at": None, "full_text": "待精析全文",
                            "name": "乙", "skills": [], "upload_time": 2}},
    {"id": "r3", "fields": {"ai_refined_at": None, "full_text": "",
                            "name": "丙", "skills": [], "upload_time": 3}},
]
JOB_ROWS = [
    {"id": "j1", "fields": {"ai_refined_at": 5, "responsibilities": "有标记"}},
    {"id": "j2", "fields": {"ai_refined_at": None, "responsibilities": "无标记有职责"}},
    {"id": "j3", "fields": {"ai_refined_at": None, "responsibilities": ""}},
]


class FakeNT:
    """按表返回预置行；biz_fields 不过滤（真 Notable 会过滤，谓词只依赖字段存在性）。"""

    def __init__(self, resume=None, job=None):
        self.rows = {"resume": resume or [], "job": job or []}
        self.calls = []

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        self.calls.append((table, tuple(biz_fields or ())))
        return [dict(r, fields=dict(r["fields"])) for r in self.rows.get(table, [])]


def _part_ids(outdir, prefix):
    """汇总 outdir 下全部 <prefix>_pending_part<N>.json 的记录 id。
    批次数随 waves 自动铺满而变（2 条 → 2 个 agent → 2 个文件），故不能写死 part1。"""
    ids = []
    for name in os.listdir(outdir):
        if name.startswith("%s_pending_part" % prefix):
            with open(os.path.join(outdir, name), encoding="utf-8") as f:
                ids.extend(p["id"] for p in json.load(f))
    return ids


class TestQueuePredicate(unittest.TestCase):
    """谓词涉及原件存在性判定，夹具须在 setUp 内用真实临时文件构造（模块级常量做不到）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="refine_q_")
        self.real = os.path.join(self.d, "scan_real.pdf")
        with open(self.real, "wb") as f:
            f.write(b"%PDF-fixture")
        self.gone = os.path.join(self.d, "scan_gone.pdf")   # 故意不创建：原件已删

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _rows(self):
        return [
            {"id": "r1", "fields": {"ai_refined_at": 1789924509618, "full_text": "已精析",
                                    "name": "甲", "skills": ["CAD"], "upload_time": 1}},
            {"id": "r2", "fields": {"ai_refined_at": None, "full_text": "待精析全文",
                                    "name": "乙", "skills": [], "upload_time": 2}},
            {"id": "r3", "fields": {"ai_refined_at": None, "full_text": "",
                                    "name": "丙", "skills": [], "upload_time": 3}},
            # 扫描件：无全文但有原件路径，原件在盘 → 入队读图精析
            {"id": "r4", "fields": {"ai_refined_at": None, "full_text": "", "source_file": self.real,
                                    "name": "丁", "skills": [], "upload_time": 4}},
            # 扫描件：原件已被移动/删除 → 不入队（防永久卡队列），但须进 unrefinable
            {"id": "r5", "fields": {"ai_refined_at": None, "full_text": "", "source_file": self.gone,
                                    "name": "戊", "skills": [], "upload_time": 5}},
        ]

    def test_resume_predicate(self):
        ids = [r["id"] for r in refine_loop.queue(FakeNT(resume=self._rows()), "resume")]
        self.assertEqual(ids, ["r2", "r4"])   # 有标记/无全文无原件/原件已删 均不入队

    def test_unrefinable_is_predicate_complement(self):
        """原件已删的扫描件：不入队，但必须被 unrefinable 报出（否则三列静默留空无人知晓）。"""
        rows = self._rows()
        q, un = refine_loop.queue_with_unrefinable(FakeNT(resume=rows), "resume")
        self.assertEqual([r["id"] for r in q], ["r2", "r4"])
        self.assertEqual([r["id"] for r in un], ["r5"])

    def test_unrefinable_excludes_refined_and_plain_scan(self):
        """已打标记、或既无全文又无路径（真·空记录）都不算 unrefinable——只有"路径失效"才算。"""
        self.assertFalse(refine_loop.unrefinable(
            {"ai_refined_at": 5, "full_text": "", "source_file": self.gone}, "resume"))
        self.assertFalse(refine_loop.unrefinable(
            {"ai_refined_at": None, "full_text": "", "source_file": ""}, "resume"))
        self.assertTrue(refine_loop.unrefinable(
            {"ai_refined_at": None, "full_text": "", "source_file": self.gone}, "resume"))

    def test_job_predicate(self):
        ids = [r["id"] for r in refine_loop.queue(FakeNT(job=JOB_ROWS), "job")]
        self.assertEqual(ids, ["j2"])

    def test_job_chain_ignores_source_file(self):
        """source_file 只对 resume 链有意义：job 行即便带失效路径也不该进 unrefinable。"""
        rows = [{"id": "j9", "fields": {"ai_refined_at": None, "responsibilities": "",
                                        "source_file": self.gone}}]
        q, un = refine_loop.queue_with_unrefinable(FakeNT(job=rows), "job")
        self.assertEqual(q, [])
        self.assertEqual(un, [])

    def test_queue_counts(self):
        nt = FakeNT(resume=self._rows(), job=JOB_ROWS)
        self.assertEqual(refine_loop.queue_counts(nt), {"resume": 2, "job": 1})

    def test_unknown_table_raises(self):
        with self.assertRaises(ValueError):
            refine_loop.queue(FakeNT(), "match")

    def test_is_queued_truthy_mark_excluded(self):
        # 标记为任意真值（毫秒时间戳）即出队；空白全文且无原件路径不入队
        self.assertFalse(refine_loop._is_queued({"ai_refined_at": 1, "full_text": "x"}, "resume"))
        self.assertTrue(refine_loop._is_queued({"ai_refined_at": None, "full_text": " x "}, "resume"))
        self.assertFalse(refine_loop._is_queued({"ai_refined_at": None, "full_text": "  "}, "resume"))
        # 无全文但原件在盘 → 入队（扫描件读图精析）
        self.assertTrue(refine_loop._is_queued(
            {"ai_refined_at": None, "full_text": "", "source_file": self.real}, "resume"))



class TestSkillsAnalyzeQueueCmd(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        old_argv, old_nt = sys.argv, sa.Notable
        sys.argv = argv
        sa.Notable = lambda *a, **k: FakeNT(resume=RESUME_ROWS, job=JOB_ROWS)
        try:
            with contextlib.redirect_stdout(buf):
                sa.main()
        finally:
            sys.argv, sa.Notable = old_argv, old_nt
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_queue_subcommand_prints_counts(self):
        out = self._run(["skills_analyze.py", "queue"])
        self.assertEqual(out, {"resume": 1, "job": 1})

    def test_handler_signatures_consistent(self):
        # 历史教训：prepare(nt,args)/merge(nt) 签名不一致 → merge 必崩 TypeError。
        # 现在所有子命令 handler 统一 handler(args)，nt 由 handler 内部按需构造。
        import inspect
        for name, fn in sa.HANDLERS.items():
            params = inspect.signature(fn).parameters
            self.assertEqual(len(params), 1, "%s 签名应为 handler(args)" % name)

    def test_merge_subcommand_smoke(self):
        tmp = tempfile.mkdtemp(prefix="sa_merge_")
        old_outdir, old_nt = sa.OUTDIR, sa.Notable
        sa.OUTDIR = tmp
        sa.Notable = lambda *a, **k: FakeNT()      # merge 不应触网：FakeNT 无 call 等方法
        try:
            json.dump([{"id": "r2", "skills": ["PLC"], "ai_extract": "e", "ai_deep": "d"}],
                      open(os.path.join(tmp, "skills_pending_part1.json"), "w"))
            json.dump([{"id": "r2", "skills": ["PLC"], "ai_extract": "e", "ai_deep": "d"}],
                      open(os.path.join(tmp, "skills_done_part1.json"), "w"))
            buf = io.StringIO()
            old_argv = sys.argv
            sys.argv = ["skills_analyze.py", "merge"]
            try:
                with contextlib.redirect_stdout(buf):
                    sa.main()
            finally:
                sys.argv = old_argv
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(out, {"merged": 1, "batches": 1, "missing_batches": []})
        finally:
            sa.OUTDIR, sa.Notable = old_outdir, old_nt
            shutil.rmtree(tmp, ignore_errors=True)


class TestSkillsAnalyzePrepareFromQueue(unittest.TestCase):
    def test_prepare_candidates_come_from_queue(self):
        tmp = tempfile.mkdtemp(prefix="sa_prepare_")
        old_outdir, old_nt = sa.OUTDIR, sa.Notable
        nt = FakeNT(resume=RESUME_ROWS, job=JOB_ROWS)
        sa.OUTDIR = tmp
        sa.Notable = lambda *a, **k: nt
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sa.prepare([])
            meta = json.loads(buf.getvalue().strip())
            self.assertEqual(meta["total"], 1)
            self.assertEqual(meta["queued"], 1)     # meta 必含 queued
            part = json.load(open(os.path.join(tmp, "skills_pending_part1.json"),
                                  encoding="utf-8"))
            self.assertEqual([p["id"] for p in part], ["r2"])   # 只有队列内记录
            self.assertEqual(part[0]["full_text"], "待精析全文")
            self.assertEqual(part[0]["name"], "乙")
        finally:
            sa.OUTDIR, sa.Notable = old_outdir, old_nt
            shutil.rmtree(tmp, ignore_errors=True)

    def test_all_mode_skips_records_without_readable_source(self):
        """--all 绕过队列谓词，但必须过滤无信息源记录：旧手析扫描件（full_text 与 source_file 皆空）
        与原件已删的记录若被喂给 subagent，只会得到"未提及"，apply 写回即覆盖已有好数据。"""
        d = tempfile.mkdtemp(prefix="sa_all_")
        real = os.path.join(d, "scan.pdf")
        with open(real, "wb") as f:
            f.write(b"%PDF-x")
        rows = [
            {"id": "ok1", "fields": {"ai_refined_at": 9, "full_text": "已精析有全文",
                                     "name": "甲", "skills": [], "upload_time": 1}},
            {"id": "legacy", "fields": {"ai_refined_at": 9, "full_text": "",
                                        "name": "旧手析扫描件", "skills": ["CAD"], "upload_time": 2}},
            {"id": "gone", "fields": {"ai_refined_at": 9, "full_text": "", "source_file": real + ".gone",
                                      "name": "原件已删", "skills": [], "upload_time": 3}},
            {"id": "ok2", "fields": {"ai_refined_at": 9, "full_text": "", "source_file": real,
                                     "name": "扫描件原件在盘", "skills": [], "upload_time": 4}},
        ]
        tmp = tempfile.mkdtemp(prefix="sa_prepare_all_")
        old_outdir, old_nt = sa.OUTDIR, sa.Notable
        sa.OUTDIR = tmp
        sa.Notable = lambda *a, **k: FakeNT(resume=rows, job=JOB_ROWS)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sa.prepare(["--all"])
            meta = json.loads(buf.getvalue().strip())
            got = sorted(_part_ids(tmp, "skills"))
            self.assertEqual(got, ["ok1", "ok2"])
            self.assertNotIn("legacy", got)
            self.assertNotIn("gone", got)
            self.assertEqual(meta["total"], 2)
            # 被跳过的两条都要报出来，否则静默留空无人知晓
            self.assertEqual(sorted(u["id"] for u in meta["unrefinable"]), ["gone", "legacy"])
            self.assertEqual([u["name"] for u in sorted(meta["unrefinable"], key=lambda x: x["id"])],
                             ["原件已删", "旧手析扫描件"])   # 报告须带姓名，光有 id 无从处理
        finally:
            sa.OUTDIR, sa.Notable = old_outdir, old_nt
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)

    def test_ids_mode_also_skips_unreadable(self):
        """--ids 显式点名也不能绕过：指定 legacy 扫描件 id 时不产出批次、只报 unrefinable。"""
        rows = [{"id": "legacy", "fields": {"ai_refined_at": 9, "full_text": "",
                                            "name": "旧手析扫描件", "skills": ["CAD"], "upload_time": 1}}]
        tmp = tempfile.mkdtemp(prefix="sa_prepare_ids_")
        idf = os.path.join(tmp, "ids.json")
        with open(idf, "w", encoding="utf-8") as f:
            json.dump(["legacy"], f)
        old_outdir, old_nt = sa.OUTDIR, sa.Notable
        sa.OUTDIR = tmp
        sa.Notable = lambda *a, **k: FakeNT(resume=rows, job=JOB_ROWS)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sa.prepare(["--ids", idf])
            meta = json.loads(buf.getvalue().strip())
            self.assertEqual(meta["total"], 0)
            self.assertEqual([u["id"] for u in meta["unrefinable"]], ["legacy"])
        finally:
            sa.OUTDIR, sa.Notable = old_outdir, old_nt
            shutil.rmtree(tmp, ignore_errors=True)


class _ApplyNT(FakeNT):
    """skills_apply 所需的最小 nt：字段 GET / 选项 PUT / update 捕获。"""

    def __init__(self, resume=None):
        super().__init__(resume=resume or [])
        self.cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        self.base = "baseX"
        self.updated, self.puts = [], []

    def sheet(self, table):
        return "sheetX"

    def cn(self, table, biz):
        return Notable.cn(self, table, biz)

    def call(self, method, path, body=None, **kw):
        self.calls.append((method, path))
        if method == "GET":
            return {"value": [{"id": "f1", "name": self.cn("resume", "skills"),
                               "type": "multipleSelect",
                               "property": {"choices": [{"id": "c1", "name": "CAD"}]}}]}
        self.puts.append(body)
        return {}

    def update_records(self, table, rows):
        self.updated.extend(rows)


class TestSkillsApplyStamp(unittest.TestCase):
    def _apply(self, records):
        d = tempfile.mkdtemp(prefix="sa_apply_")
        p = os.path.join(d, "done.json")
        json.dump(records, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        nt = _ApplyNT()
        before = int(time.time() * 1000)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sapi.apply_(nt, p)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return nt, json.loads(buf.getvalue().strip()), before

    def test_stamp_written_with_three_columns(self):
        nt, rep, before = self._apply([
            {"id": "r2", "skills": ["PLC"], "ai_extract": "工作经验｜5年", "ai_deep": "d"},
            {"id": "r9", "skills": [], "ai_extract": "e", "ai_deep": ""},
        ])
        self.assertEqual(rep["updated"], 2)
        self.assertEqual(len(nt.updated), 2)
        for row in nt.updated:
            self.assertIn("ai_refined_at", row)      # 每条都打，与三列同一次 update
            self.assertIsInstance(row["ai_refined_at"], int)
            self.assertGreaterEqual(row["ai_refined_at"], before)
            self.assertLessEqual(row["ai_refined_at"], int(time.time() * 1000))
        # 标记是毫秒时间戳（config types: ai_refined_at=date，_cast 毫秒直传）
        self.assertEqual(Notable._cast(nt.updated[0]["ai_refined_at"], "date"),
                         nt.updated[0]["ai_refined_at"])

    def test_empty_record_not_stamped(self):
        nt, rep, _ = self._apply([{"id": "r5", "skills": [], "ai_extract": "", "ai_deep": ""}])
        self.assertEqual(nt.updated, [])
        self.assertEqual(rep["updated"], 0)
        self.assertEqual([b["id"] for b in rep["bad"]], ["r5"])

    def test_years_backfill_untouched(self):
        # 年限回填正则保留：ai_extract「工作经验｜N年」回填 years_experience
        nt, _, _ = self._apply([{"id": "r2", "skills": [], "ai_extract": "工作经验｜7年",
                                 "ai_deep": "d"}])
        self.assertEqual(nt.updated[0].get("years_experience"), 7)


class _UploadNT(FakeNT):
    """upload_resumes --backfill 全链路 mock：附件/create/回读/查重自愈。"""

    def __init__(self):
        super().__init__(resume=[])
        self.cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        self.store = []          # [{"id","fields":业务键行}]
        self.created_rows = []

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        return [dict(r, fields=dict(r["fields"])) for r in self.store]

    def map_parallel(self, fn, items, workers=5):
        return [fn(i) for i in items], []

    def upload_attachment(self, path):
        return {"filename": os.path.basename(path), "size": 1, "type": "application/pdf",
                "url": "/res/x", "resourceId": "x"}

    def create_records(self, table, rows):
        ids = []
        for row in rows:
            self.created_rows.append(dict(row))
            rid = "rec%d" % (len(self.store) + 1)
            self.store.append({"id": rid, "fields": dict(row)})
            ids.append(rid)
        return ids

    def delete_records(self, table, ids):
        self.store = [r for r in self.store if r["id"] not in ids]


class TestBackfillQueueReport(unittest.TestCase):
    """补录口径（扫描件交后台读图）：写 source_file、不打 ai_refined_at、照常入队。"""

    def test_backfill_writes_source_file_and_queues(self):
        d = tempfile.mkdtemp(prefix="ur_backfill_")
        try:
            fpath = os.path.join(d, "scan.pdf")
            with open(fpath, "wb") as f:
                f.write(b"ocr-fixture")
            payload = [{"name": "张三", "phone": "13800000000", "email": "z@x.com",
                        "_file": fpath}]
            pj = os.path.join(d, "ocr.json")
            json.dump(payload, open(pj, "w", encoding="utf-8"), ensure_ascii=False)
            nt = _UploadNT()
            buf = io.StringIO()
            args = type("A", (), {"backfill": pj})()
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(buf):
                    ur.run_backfill(nt, args)
            self.assertEqual(ctx.exception.code, 0)
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep["created"], 1)
            row = nt.created_rows[0]
            # 原件绝对路径入列：这是扫描件唯一的入队凭证与 subagent 读图入口
            self.assertEqual(row["source_file"], os.path.abspath(fpath))
            # 三列交后台读图，补录不写、不出队标记
            self.assertNotIn("ai_refined_at", row)
            self.assertNotIn("ai_extract", row)
            self.assertNotIn("ai_deep", row)
            # 扫描件照常入队 → refine_queued=1 且给出注册时刻（补录后同样要注册消费任务）
            self.assertEqual(rep["refine_queued"], 1)
            self.assertIn("refine_fire_at", rep)
            self.assertEqual(rep["table_total"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_backfill_stdin_payload_equivalent(self):
        """--backfill - 从 stdin 读：与文件路径完全同义（省 agent 一次回合往返）。"""
        d = tempfile.mkdtemp(prefix="ur_backfill_stdin_")
        try:
            fpath = os.path.join(d, "scan.png")
            with open(fpath, "wb") as f:
                f.write(b"\x89PNG-fixture")
            payload = json.dumps([{"name": "李四", "phone": "13900000000",
                                   "_file": fpath}], ensure_ascii=False)
            nt = _UploadNT()
            buf = io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(payload)
            args = type("A", (), {"backfill": "-"})()
            try:
                with self.assertRaises(SystemExit) as ctx:
                    with contextlib.redirect_stdout(buf):
                        ur.run_backfill(nt, args)
            finally:
                sys.stdin = old_stdin
            self.assertEqual(ctx.exception.code, 0)
            rep = json.loads(buf.getvalue())
            self.assertEqual(rep["created"], 1)
            self.assertEqual(nt.created_rows[0]["source_file"], os.path.abspath(fpath))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_finalize_report_counts_unmarked_as_queued(self):
        # 批量入库（不打标记）→ refine_queued = 有全文未标记条数
        nt = _UploadNT()
        nt.store = [{"id": "a", "fields": {"phone": "1", "full_text": "t"}},
                    {"id": "b", "fields": {"phone": "2", "full_text": ""}}]
        report = {"failed": []}
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(buf):
                ur._finalize(nt, "resume", [], report)
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["refine_queued"], 1)


class TestQueuePredicateSingleSource(unittest.TestCase):
    """不变量 10：队列谓词只在 refine_loop，消费方禁止本地副本。"""

    def _src(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_consumers_import_refine_loop(self):
        self.assertIn("import refine_loop",
                      self._src("skills", "skills-analyze", "scripts", "skills_analyze.py"))
        self.assertIn("import refine_loop",
                      self._src("skills", "resume-intake", "scripts", "upload_resumes.py"))

    def test_no_local_predicate_copy(self):
        for parts in (("skills", "skills-analyze", "scripts", "skills_analyze.py"),
                      ("skills", "resume-intake", "scripts", "upload_resumes.py"),
                      ("skills", "skills-analyze", "scripts", "skills_apply.py")):
            src = self._src(*parts)
            self.assertNotIn('fields.get("ai_refined_at")', src, "%s 出现谓词副本" % (parts,))
            self.assertNotIn("_is_queued", src.replace("refine_loop._is_queued", ""))

    def test_old_inference_filter_removed(self):
        # 旧推断式过滤（三列非空即跳过）已删干净（不变量 9）
        src = self._src("skills", "skills-analyze", "scripts", "skills_analyze.py")
        self.assertNotIn('r["fields"].get("ai_extract")', src)
        self.assertNotIn('r["fields"].get("ai_deep")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
