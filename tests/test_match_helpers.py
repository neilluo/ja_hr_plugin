# -*- coding: utf-8 -*-
"""match/job-intake 双源收敛与可观测性回归（不触网）。

覆盖本轮清扫的确定性缺陷：
  #1 match_analyze 不再硬编码"不推荐"，兜底走 match_gated.REJ_LABEL（config 派生）。
  #2 岗位四项统计刷新唯一实现 match_gated.refresh_job_stats：stage_stats 与 match_analyze.stats
     对同一输入产出相同的 job update 行。
  #3 match 行组装唯一实现 match_gated._match_row：stage_commit 与 match_analyze.apply_ 产出相同键集。
  #5 upload_jobs.EXTS 由 extract.DOC_EXTS 派生（是 DOC_EXTS 的子集，且排除 .txt/.md）。
  #7 jobs_analyze / match_analyze prepare 落分派清单，parts 是盘上真实存在的 pending 路径。
  #9 upload_jobs 报告带 timing_ms，分段键与简历链一致。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

import match_analyze  # noqa: E402
import match_gated  # noqa: E402


class FakeNotable:
    """内存表：list_records 忽略 biz_fields 返回全量；记录 create/update/delete 供断言。"""

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.created = []
        self.deleted = []
        self.updated = []

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        return [dict(r) for r in self.tables.get(table, [])]

    def create_records(self, table, rows):
        rows = [dict(r) for r in rows]
        self.created.append((table, rows))
        ids = []
        for i, r in enumerate(rows):
            rid = "%s_new%d" % (table, i)
            r["id"] = rid
            self.tables.setdefault(table, []).append({"id": rid, "fields": r})
            ids.append(rid)
        return ids

    def update_records(self, table, rows):
        self.updated.append((table, [dict(r) for r in rows]))

    def delete_records(self, table, ids):
        self.deleted.append((table, list(ids)))


def _job(jid="J1", org="制造中心", must="设备维修、点检管理"):
    return {"id": "job_" + jid, "fields": {
        "job_id": jid, "job_name": "设备工程师", "department": "设备部", "org": org,
        "status": "招聘中", "hard_gates": "", "must_skills": must, "bonus_skills": "",
        "must_weight": 1.0, "bonus_weight": 0.0}}


def _resume(rid, name="张三", skills=("设备维修", "点检管理")):
    return {"id": rid, "fields": {
        "name": name, "phone": "13800000000", "education": "本科", "years_experience": 5,
        "certificates": "", "skills": list(skills), "org": "制造中心", "major": "机械",
        "expected_position": "设备工程师"}}


# ── #1 兜底标签单源 ────────────────────────────────────────────────────────────
class TestRejectLabelSingleSource(unittest.TestCase):
    def test_no_hardcoded_reject_literal_in_match_analyze(self):
        src = open(os.path.join(ROOT, "skills", "match-verify", "scripts",
                                "match_analyze.py"), encoding="utf-8").read()
        self.assertNotIn('"不推荐"', src)
        self.assertNotIn("'不推荐'", src)

    def test_match_row_falls_back_to_rej_label(self):
        # scores 无 recommend → 兜底 match_gated.REJ_LABEL（config.options.match.recommend 派生）
        row = match_gated._match_row(_resume("r1")["fields"], _job()["fields"], {"name": "张三"})
        self.assertEqual(row["recommend"], match_gated.REJ_LABEL)
        self.assertEqual(match_gated.REJ_LABEL, "不推荐")

    def test_match_analyze_imports_rej_label_source(self):
        # apply_ 经 _match_row 兜底，禁止自带字面量：_match_row 从 match_gated 导入
        self.assertIs(match_analyze._match_row, match_gated._match_row)


# ── #2 四项统计刷新单源 ────────────────────────────────────────────────────────
class TestStatsSingleSource(unittest.TestCase):
    def _match_rows(self):
        return [
            {"id": "m1", "fields": {"job_id": "J1", "recommend": "推荐"}},
            {"id": "m2", "fields": {"job_id": "J1", "recommend": "待定"}},
            {"id": "m3", "fields": {"job_id": "J1", "recommend": "不推荐"}},
            {"id": "m4", "fields": {"job_id": "J2", "recommend": "推荐"}},
        ]

    def test_both_entrypoints_identical_job_updates(self):
        tables = {"match": self._match_rows(), "job": [_job("J1"), _job("J2")]}
        nt_a = FakeNotable(tables)
        match_gated.stage_stats(nt_a)          # 打印 match_rows/jobs_updated
        nt_b = FakeNotable(tables)
        match_analyze.stats(nt_b)              # 打印 match_rows/jobs_updated/分布
        self.assertEqual(nt_a.updated, nt_b.updated)
        upd = nt_a.updated[0][1]
        by_id = {u["id"]: u for u in upd}
        self.assertEqual(by_id["job_J1"], {"id": "job_J1", "stat_total": 3, "stat_recommend": 1,
                                           "stat_pending": 1, "stat_reject": 1})
        self.assertEqual(by_id["job_J2"], {"id": "job_J2", "stat_total": 1, "stat_recommend": 1,
                                           "stat_pending": 0, "stat_reject": 0})

    def test_refresh_job_stats_is_shared_callable(self):
        self.assertIs(match_analyze.refresh_job_stats, match_gated.refresh_job_stats)


# ── #3 match 行组装单源 ────────────────────────────────────────────────────────
class TestMatchRowSingleSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._g_paths = (match_gated.OUTDIR, match_gated.GATE_PAIRS)
        match_gated.OUTDIR = self._tmp
        match_gated.GATE_PAIRS = os.path.join(self._tmp, "gate_pairs.json")
        self._a_paths = (match_analyze.OUTDIR, match_analyze.FINAL)
        match_analyze.OUTDIR = self._tmp
        match_analyze.FINAL = os.path.join(self._tmp, "match_final.json")

    def tearDown(self):
        import shutil
        match_gated.OUTDIR, match_gated.GATE_PAIRS = self._g_paths
        match_analyze.OUTDIR, match_analyze.FINAL = self._a_paths
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_stage_commit_and_apply_same_row_keys(self):
        # stage_commit 路径
        pairs = [{"rid": "r1", "name": "张三", "phone": "13800000000",
                  "job_id": "J1", "total": 100, "recommend": "推荐"}]
        json.dump(pairs, open(match_gated.GATE_PAIRS, "w", encoding="utf-8"))
        nt_a = FakeNotable({"job": [_job("J1")], "resume": [_resume("r1")], "match": []})
        match_gated.stage_commit(nt_a)
        keys_commit = set(nt_a.created[0][1][0].keys())

        # apply_ 路径（keep=true 的同一判定）
        final = [{"name": "张三", "job_id": "J1", "rid": "r1", "keep": True,
                  "skill_score": 100, "bonus_score": 0, "total_score": 100,
                  "recommend": "推荐", "evidence": "语义匹配：必备2/2", "ai_analysis": "结论…"}]
        json.dump(final, open(match_analyze.FINAL, "w", encoding="utf-8"))
        nt_b = FakeNotable({"job": [_job("J1")], "resume": [_resume("r1")], "match": []})
        match_analyze.apply_(nt_b)
        keys_apply = set(nt_b.created[0][1][0].keys())

        self.assertEqual(keys_commit, keys_apply)
        # 关键字段齐全（含 ai_analysis 列）
        for k in ("job_id", "name", "phone", "job_name", "org", "source", "cand_skills",
                  "must_skills", "bonus_skills", "hard_gates", "recommend", "evidence",
                  "ai_analysis", "update_time"):
            self.assertIn(k, keys_commit)

    def test_both_rows_source_is_sys(self):
        row = match_gated._match_row(_resume("r1")["fields"], _job("J1")["fields"],
                                     {"name": "张三", "recommend": "推荐"})
        self.assertEqual(row["source"], match_gated.SYS_SOURCE)


# ── #5 upload_jobs.EXTS 派生自 extract.DOC_EXTS ────────────────────────────────
class TestUploadJobsExts(unittest.TestCase):
    def test_exts_is_subset_of_doc_exts(self):
        import upload_jobs
        from extract import DOC_EXTS
        self.assertTrue(set(upload_jobs.EXTS) <= DOC_EXTS)
        self.assertTrue(set(upload_jobs.EXTS))  # 非空

    def test_exts_excludes_txt_and_md(self):
        import upload_jobs
        self.assertNotIn(".txt", upload_jobs.EXTS)
        self.assertNotIn(".md", upload_jobs.EXTS)
        for e in (".pdf", ".doc", ".docx"):
            self.assertIn(e, upload_jobs.EXTS)

    def test_exts_no_hardcoded_tuple(self):
        src = open(os.path.join(ROOT, "skills", "job-intake", "scripts",
                                "upload_jobs.py"), encoding="utf-8").read()
        self.assertNotIn('EXTS = (".doc"', src)
        self.assertIn("DOC_EXTS", src)


# ── #7 jobs 链分派：render_prompts 落 per-batch 提示词 + 简历标签池 ──────────────
class TestJobsDispatchManifest(unittest.TestCase):
    """jobs 链走 render_prompts（与简历链对称）：dispatch = 渲染好的提示词文件路径，
    prepare 同时产出 resume_vocab.json（简历标签池，供 JD subagent 选词同源）。
    注意：这与 match 链的 write_dispatch（dispatch = pending 路径清单）是两种机制——
    jobs/skills 链要注入 vocab + 完整提示词，match 链模板自包含、只发 pending 路径。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        import jobs_analyze
        self._ja = jobs_analyze
        self._paths = (jobs_analyze.OUTDIR,)
        jobs_analyze.OUTDIR = self._tmp

    def tearDown(self):
        import shutil
        self._ja.OUTDIR = self._paths[0]
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_prepare_renders_prompts_and_vocab(self):
        jobs = [{"id": "j%d" % i, "fields": {
            "job_id": "J%d" % i, "job_name": "岗%d" % i, "department": "部", "org": "制造中心",
            "status": "招聘中", "hard_gates": "", "must_skills": "设备维修", "bonus_skills": "",
            "requirements": "本科", "responsibilities": "维护设备", "work_location": "邢台",
            "ai_refined_at": None}} for i in range(3)]
        nt = FakeNotable({"job": jobs})
        old_notable = self._ja.Notable
        self._ja.Notable = lambda: nt
        try:
            meta = self._ja.prepare(["--batch", "1"])
        finally:
            self._ja.Notable = old_notable
        self.assertEqual(meta["batches"], 3)
        dispatch = os.path.join(self._tmp, "jobs_dispatch.json")
        self.assertEqual(meta["dispatch"], dispatch)
        man = json.load(open(dispatch, encoding="utf-8"))
        self.assertEqual(man["batches"], 3)
        # render_prompts 契约：dispatch 键是 prompts（渲染好的提示词文件），非 parts
        self.assertEqual(len(man["prompts"]), 3)
        for i, p in enumerate(man["prompts"], 1):
            self.assertEqual(p, os.path.join(self._tmp, "jobs_prompt_part%d.md" % i))
            self.assertTrue(os.path.exists(p))
        # 简历标签池注入：prepare 落 resume_vocab.json（此处 resume 表为空 → 空数组）
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "resume_vocab.json")))
        self.assertEqual(json.load(open(os.path.join(self._tmp, "resume_vocab.json"),
                                        encoding="utf-8")), [])


class TestMatchDispatchManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._paths = (match_analyze.OUTDIR, match_analyze.PAIRS)
        match_analyze.OUTDIR = self._tmp
        match_analyze.PAIRS = os.path.join(self._tmp, "gate_pairs.json")

    def tearDown(self):
        import shutil
        match_analyze.OUTDIR, match_analyze.PAIRS = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_prepare_writes_manifest_with_real_paths(self):
        pairs = [{"rid": "r%d" % i, "name": "候%d" % i, "job_id": "J1",
                  "total": 90, "recommend": "推荐"} for i in range(3)]
        json.dump(pairs, open(match_analyze.PAIRS, "w", encoding="utf-8"))
        nt = FakeNotable({"job": [_job("J1")],
                          "resume": [_resume("r%d" % i, "候%d" % i) for i in range(3)]})
        old_notable = match_analyze.Notable
        match_analyze.Notable = lambda: nt
        try:
            meta = match_analyze.prepare(["--batch", "1"])
        finally:
            match_analyze.Notable = old_notable
        dispatch = os.path.join(self._tmp, "match_dispatch.json")
        self.assertEqual(meta["dispatch"], dispatch)
        man = json.load(open(dispatch, encoding="utf-8"))
        self.assertEqual(man["batches"], meta["batches"])
        import analyze_parts as ap
        for i, p in enumerate(man["parts"], 1):
            self.assertEqual(p, ap.pending_path(self._tmp, "match", i))
            self.assertTrue(os.path.exists(p))


# ── #9 upload_jobs 报告带 timing_ms ────────────────────────────────────────────
class TestUploadJobsTiming(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        # 造两个 dummy JD 文件（扩展名在 EXTS 内即可，extract/parse 会被 patch）
        self.f1 = os.path.join(self._tmp, "jd1.pdf")
        self.f2 = os.path.join(self._tmp, "jd2.pdf")
        for p in (self.f1, self.f2):
            with open(p, "wb") as fh:
                fh.write(b"dummy")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_report_has_timing_ms_with_segments(self):
        import upload_jobs
        captured = {}

        def fake_print_report(report, created_rows=None):
            captured["report"] = report

        def fake_parse(text, fn):
            jd = {"job_name": "工程师_" + fn, "department": "设备部", "org": "制造中心",
                  "status": "招聘中", "work_location": "邢台", "responsibilities": "维护",
                  "requirements": "本科", "hard_gates": ["学历：本科及以上"],
                  "must_skills": ["设备维修"], "bonus_skills": [], "must_weight": 0.7,
                  "bonus_weight": 0.3}
            return jd

        class FakeUploadNT(FakeNotable):
            def map_parallel(self, fn, items, workers=5):
                return [fn(p) for p in items], []

            def upload_attachment(self, path):
                return {"name": os.path.basename(path)}

        patches = [
            (upload_jobs, "run_preflight", lambda **kw: None),
            (upload_jobs, "Notable", lambda: FakeUploadNT({"job": []})),
            (upload_jobs, "extract", lambda p: {"text": "职责正文", "needs_ocr": False}),
            (upload_jobs, "parse", fake_parse),
            (upload_jobs, "print_report", fake_print_report),
        ]
        saved = []
        for obj, name, val in patches:
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        old_argv = sys.argv
        sys.argv = ["upload_jobs.py", self._tmp]
        try:
            with self.assertRaises(SystemExit):
                upload_jobs.main()
        finally:
            sys.argv = old_argv
            for obj, name, val in saved:
                setattr(obj, name, val)

        timing = captured["report"].get("timing_ms")
        self.assertIsNotNone(timing, "job 报告缺 timing_ms")
        for seg in ("list_existing", "build_rows", "attach", "create", "readback", "total"):
            self.assertIn(seg, timing)
        # 字段顺序：timing_ms 垫底（report._KEY_ORDER/_TAIL_KEYS 保障）
        self.assertEqual(captured["report"]["created"], 2)


if __name__ == "__main__":
    unittest.main()
