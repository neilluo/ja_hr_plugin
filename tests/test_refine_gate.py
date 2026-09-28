# -*- coding: utf-8 -*-
"""精析队列门禁 + record-id key + source 过滤 + org 取值 回归（不触网）。

用内存 FakeNotable 驱动 match_gated.stage_gate/stage_commit 与 match_analyze.apply_，
校验：
  - 队列非空 → stage_gate exit 2；--force 放行；job 表 0 条 / must_skills 全空 → --force 也拒。
  - 候选人 key 改 record id：重名简历不再互相覆盖（两条都进配对）。
  - 删旧加 source=系统匹配 过滤：人工匹配记录不被删。
  - match_analyze.apply_ 的 org 取岗位 jf.org（旧代码误取 department and cf.org）。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

import check_skill_coverage  # noqa: E402
import match_analyze  # noqa: E402
import match_gated  # noqa: E402

SYS = "系统匹配"
MANUAL = "人工匹配"


class FakeNotable:
    """内存表：list_records 忽略 biz_fields 返回全量；记录 create/delete 供断言。"""

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.created = []
        self.deleted = []
        self.updated = []

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        rows = self.tables.get(table, [])
        if flt:
            rows = [r for r in rows
                    if all(r["fields"].get(k) == v for k, v in flt.items())]
        return [dict(r) for r in rows]

    def create_records(self, table, rows):
        self.created.append((table, [dict(r) for r in rows]))
        return ["new%d" % i for i in range(len(rows))]

    def update_records(self, table, rows):
        self.updated.append((table, [dict(r) for r in rows]))

    def delete_records(self, table, ids):
        self.deleted.append((table, list(ids)))


def _job(jid="J1", org="制造中心", dept="单晶制造部-设备部", status="招聘中",
         must="设备维修、点检管理", resp="负责设备维护", refined=True):
    return {"id": "job_" + jid, "fields": {
        "job_id": jid, "job_name": "设备工程师", "department": dept, "org": org,
        "status": status, "hard_gates": "", "must_skills": must, "bonus_skills": "",
        "must_weight": 1.0, "bonus_weight": 0.0,
        "responsibilities": resp, "ai_refined_at": 1700000000000 if refined else None}}


def _resume(rid, name, skills, org="制造中心", refined=True):
    return {"id": rid, "fields": {
        "name": name, "phone": "13800000000", "education": "本科",
        "years_experience": 5, "certificates": "", "skills": skills, "org": org,
        "major": "机械", "expected_position": "设备工程师",
        "full_text": "简历全文" if refined else "", "ai_refined_at": 1700000000000 if refined else None}}


class GateHarness(unittest.TestCase):
    """把 match_gated 的产物路径重定向到临时目录，避免污染仓库 outputs/。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._paths = (match_gated.OUTDIR, match_gated.GATE_PAIRS, match_gated.GATE_PENDING)
        match_gated.OUTDIR = self._tmp
        match_gated.GATE_PAIRS = os.path.join(self._tmp, "gate_pairs.json")
        match_gated.GATE_PENDING = os.path.join(self._tmp, "gate_pending.json")

    def tearDown(self):
        match_gated.OUTDIR, match_gated.GATE_PAIRS, match_gated.GATE_PENDING = self._paths
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestStageGateQueue(GateHarness):
    def _tables(self, jobs, resumes):
        return {"job": jobs, "resume": resumes, "match": []}

    def test_queue_nonempty_exits_2(self):
        # 岗位未精析（ai_refined_at 空 + responsibilities 非空）→ 队列非空 → exit 2
        nt = FakeNotable(self._tables([_job(refined=False)],
                                      [_resume("r1", "张三", ["设备维修"])]))
        with self.assertRaises(SystemExit) as ctx:
            match_gated.stage_gate(nt)
        self.assertEqual(ctx.exception.code, 2)

    def test_force_bypasses_queue(self):
        nt = FakeNotable(self._tables([_job(refined=False)],
                                      [_resume("r1", "张三", ["设备维修", "点检管理"])]))
        match_gated.stage_gate(nt, force=True)   # 不应抛
        pairs = json.load(open(match_gated.GATE_PAIRS, encoding="utf-8"))
        self.assertEqual(len(pairs), 1)

    def test_job_empty_refused_even_force(self):
        nt = FakeNotable(self._tables([], [_resume("r1", "张三", ["设备维修"])]))
        with self.assertRaises(SystemExit) as ctx:
            match_gated.stage_gate(nt, force=True)
        self.assertEqual(ctx.exception.code, 2)

    def test_must_all_empty_refused_even_force(self):
        nt = FakeNotable(self._tables([_job(must="")],
                                      [_resume("r1", "张三", ["设备维修"])]))
        with self.assertRaises(SystemExit) as ctx:
            match_gated.stage_gate(nt, force=True)
        self.assertEqual(ctx.exception.code, 2)

    def test_clean_queue_passes(self):
        nt = FakeNotable(self._tables([_job(refined=True)],
                                      [_resume("r1", "张三", ["设备维修", "点检管理"])]))
        match_gated.stage_gate(nt)
        pairs = json.load(open(match_gated.GATE_PAIRS, encoding="utf-8"))
        self.assertEqual(len(pairs), 1)


class TestRecordIdKey(GateHarness):
    def test_same_name_two_records_both_kept(self):
        # 两个「张三」不同 record id、不同技能 → key 用 record id，两条都进配对（旧代码按 name 会覆盖）
        nt = FakeNotable({"job": [_job(must="设备维修")], "resume": [
            _resume("rA", "张三", ["设备维修"]),
            _resume("rB", "张三", ["设备维修"]),
        ], "match": []})
        match_gated.stage_gate(nt)
        pairs = json.load(open(match_gated.GATE_PAIRS, encoding="utf-8"))
        self.assertEqual(len(pairs), 2)
        self.assertEqual({p["rid"] for p in pairs}, {"rA", "rB"})
        self.assertTrue(all(p["name"] == "张三" for p in pairs))


class TestSourceFilterCommit(GateHarness):
    def test_commit_deletes_only_system_rows(self):
        # match 表里同一 (name,job_id) 既有系统匹配又有人工匹配 → 只删系统匹配
        pairs = [{"rid": "rA", "name": "张三", "job_id": "J1", "total": 100, "recommend": "推荐"}]
        json.dump(pairs, open(match_gated.GATE_PAIRS, "w", encoding="utf-8"))
        nt = FakeNotable({
            "job": [_job(must="设备维修")],
            "resume": [_resume("rA", "张三", ["设备维修"])],
            "match": [
                {"id": "m_sys", "fields": {"name": "张三", "job_id": "J1", "recommend": "推荐", "source": SYS}},
                {"id": "m_man", "fields": {"name": "张三", "job_id": "J1", "recommend": "待定", "source": MANUAL}},
            ],
        })
        match_gated.stage_commit(nt)
        self.assertEqual(nt.deleted, [("match", ["m_sys"])])
        self.assertEqual(len(nt.created[0][1]), 1)


class ApplyHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._final = match_analyze.FINAL
        match_analyze.FINAL = os.path.join(self._tmp, "match_final.json")

    def tearDown(self):
        match_analyze.FINAL = self._final
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestApplyOrgAndSource(ApplyHarness):
    def test_org_from_job_and_source_filter(self):
        final = [{"name": "张三", "rid": "rA", "job_id": "J1", "keep": True,
                  "skill_score": 100, "bonus_score": 0, "total_score": 100,
                  "recommend": "推荐", "evidence": "e", "ai_analysis": "a"}]
        json.dump(final, open(match_analyze.FINAL, "w", encoding="utf-8"))
        nt = FakeNotable({
            # 岗位 org 与候选人 org 故意不同，验证取的是岗位 org
            "job": [{"id": "job_J1", "fields": {"job_id": "J1", "job_name": "设备工程师",
                                                "org": "制造中心", "department": "单晶制造部-设备部",
                                                "must_skills": "设备维修", "bonus_skills": "",
                                                "hard_gates": ""}}],
            "resume": [{"id": "rA", "fields": {"name": "张三", "phone": "138", "skills": ["设备维修"],
                                               "years_experience": 5, "expected_position": "设备",
                                               "org": "职能中心"}}],
            "match": [
                {"id": "m_sys", "fields": {"job_id": "J1", "source": SYS}},
                {"id": "m_man", "fields": {"job_id": "J1", "source": MANUAL}},
            ],
        })
        match_analyze.apply_(nt)
        # 只删系统匹配记录
        self.assertEqual(nt.deleted, [("match", ["m_sys"])])
        row = nt.created[0][1][0]
        # org 取岗位 org（制造中心），而非旧代码的 department/cf.org
        self.assertEqual(row["org"], "制造中心")
        self.assertEqual(row["source"], SYS)
        self.assertEqual(row["name"], "张三")


class TestCoverageEmptyPool(unittest.TestCase):
    """check_skill_coverage：简历标签池为空 → skipped exit 0（不误判全岗低覆盖）。
    OUTDIR 重定向到临时目录，防 pool_drift 快照写到真实 outputs/（同 refine 租约教训）。"""

    def _run(self, nt, argv_extra=()):
        tmp = tempfile.mkdtemp(prefix="csc_gate_")
        old_outdir = check_skill_coverage.OUTDIR
        check_skill_coverage.OUTDIR = tmp
        try:
            with mock.patch.object(check_skill_coverage, "Notable", return_value=nt), \
                    mock.patch.object(sys, "argv", ["check_skill_coverage.py", *argv_extra]):
                with self.assertRaises(SystemExit) as ctx:
                    check_skill_coverage.main()
            return ctx.exception.code, tmp
        finally:
            check_skill_coverage.OUTDIR = old_outdir

    def test_empty_pool_skips_exit_0(self):
        nt = FakeNotable({"job": [_job(refined=True, must="设备维修、点检管理")],
                          "resume": [_resume("r1", "张三", [])]})
        code, tmp = self._run(nt)
        self.assertEqual(code, 0)

    def test_nonempty_pool_low_coverage_exit_2(self):
        # 对照组：池非空且岗位词不同源 → 照常判低覆盖 exit 2
        nt = FakeNotable({"job": [_job(refined=True, must="账务处理、金蝶")],
                          "resume": [_resume("r1", "张三", ["设备维修"])]})
        code, tmp = self._run(nt)
        self.assertEqual(code, 2)


class TestCoverageEmitFixes(unittest.TestCase):
    """--emit-fixes：两因分辨 → sync_job_columns 兼容 payload + 逐词诊断报告。"""

    def _run(self, nt, argv):
        tmp = tempfile.mkdtemp(prefix="csc_emit_")
        old = check_skill_coverage.OUTDIR
        check_skill_coverage.OUTDIR = tmp
        try:
            with mock.patch.object(check_skill_coverage, "Notable", return_value=nt), \
                    mock.patch.object(sys, "argv", ["check_skill_coverage.py", *argv, tmp]):
                with self.assertRaises(SystemExit) as ctx:
                    check_skill_coverage.main()
            return ctx.exception.code, tmp
        finally:
            check_skill_coverage.OUTDIR = old

    def test_emit_fixes_writes_payload_and_report(self):
        # ①类"账务处理"（池有"总账处理"、共享 账/处/理 3 字）→ 建议替换；
        # ②类"金蝶""危险源辨识"（池无 ≥2 字近邻）→ 保留原词，post 仍低=合法保留态。
        nt = FakeNotable({
            "job": [_job(refined=True, must="账务处理、金蝶、危险源辨识")],
            "resume": [_resume("r1", "张三", ["总账处理", "设备维修"])]})
        code, tmp = self._run(nt, ["--emit-fixes"])
        self.assertEqual(code, 2)  # 改前低覆盖 exit 语义不变
        payload = json.load(open(os.path.join(tmp, "jobs_fix.json"), encoding="utf-8"))
        self.assertIn("J1", payload)
        # KEYS 契约：payload 值只允许 must_skills（不碰 hard_gates/bonus）
        self.assertEqual(set(payload["J1"]), {"must_skills"})
        self.assertIn("金蝶", payload["J1"]["must_skills"])  # ②类保留
        report = json.load(open(os.path.join(tmp, "jobs_fix_report.json"), encoding="utf-8"))
        words = {w["word"]: w for w in report["J1"]["words"]}
        self.assertEqual(words["账务处理"]["cause"], "①词不同源")
        self.assertEqual(words["账务处理"]["suggested"], "总账处理")
        self.assertEqual(words["金蝶"]["cause"], "②库内无此类候选人")
        self.assertIsNone(words["金蝶"]["suggested"])
        # ②类保留后 post_ratio 仍低（本例 1/3），still_low=True 供汇报识别合法保留
        self.assertTrue(report["J1"]["still_low"])

    def test_emit_fixes_collision_keeps_words_and_denominator(self):
        """撞车仲裁（防丢词/防分母虚高回归）：应急管理/事故管理/安全评价 三条独立要求
        全被 near_tags 猜成已有的「安全管理」=同形尾缀噪声。旧实现按结果去重 → 9→6 词、
        post_ratio 分母被自己缩小、still_low 虚高。现须逐位保留原词、长度恒等。"""
        nt = FakeNotable({
            "job": [_job(refined=True, must="EHS、安全管理、应急管理、事故管理、安全评价")],
            "resume": [_resume("r1", "张三", ["安全管理", "EHS"])]})
        code, tmp = self._run(nt, ["--emit-fixes"])
        self.assertEqual(code, 2)
        payload = json.load(open(os.path.join(tmp, "jobs_fix.json"), encoding="utf-8"))
        report = json.load(open(os.path.join(tmp, "jobs_fix_report.json"), encoding="utf-8"))
        new_words = payload["J1"]["must_skills"].split("、")
        # 一词不丢、一词不塌（撞车处保留原词而非折成重复的安全管理）
        for w in ("应急管理", "事故管理", "安全评价"):
            self.assertIn(w, new_words)
        self.assertEqual(len(new_words), 5)
        self.assertEqual(new_words.count("安全管理"), 1)
        # 撞车词标 noise cause，suggested 仍留在报告里供人工翻案
        words = {w["word"]: w for w in report["J1"]["words"]}
        self.assertEqual(words["应急管理"]["cause"], "①词不同源-撞车(疑同形噪声)")
        self.assertEqual(words["应急管理"]["suggested"], "安全管理")
        self.assertEqual(words["应急管理"]["kept"], "应急管理")
        self.assertGreaterEqual(report["J1"]["collisions"], 3)
        # 分母诚实：post 命中率 = 命中数/5（不因删词而抬高）
        self.assertTrue(report["J1"]["still_low"])

    def test_emit_fixes_legit_reword_still_applied(self):
        """对照组：建议词不撞车（账务处理→总账处理，池有且 must 内无）时仍照常替换，
        证明撞车仲裁没有把正常 ① 类改词一起封死。"""
        nt = FakeNotable({
            "job": [_job(refined=True, must="账务处理、设备维修、点检")],
            "resume": [_resume("r1", "张三", ["总账处理", "设备维修"])]})
        code, tmp = self._run(nt, ["--emit-fixes"])
        payload = json.load(open(os.path.join(tmp, "jobs_fix.json"), encoding="utf-8"))
        self.assertIn("总账处理", payload["J1"]["must_skills"])
        self.assertNotIn("账务处理", payload["J1"]["must_skills"])

    def test_emit_fixes_payload_passes_precheck(self):
        # 建议词替换后 --precheck 应 exit 0（可放心 sync）
        nt = FakeNotable({
            "job": [_job(refined=True, must="账务处理、总账处理、设备维修")],
            "resume": [_resume("r1", "张三", ["总账处理", "设备维修"])]})
        # 造一份 payload：把"账务处理"→"总账处理"（去重后 2 词全命中）
        tmp = tempfile.mkdtemp(prefix="csc_pre_")
        pj = os.path.join(tmp, "jobs_fix.json")
        json.dump({"J1": {"must_skills": "总账处理、设备维修"}},
                  open(pj, "w", encoding="utf-8"), ensure_ascii=False)
        try:
            old = check_skill_coverage.OUTDIR
            check_skill_coverage.OUTDIR = tmp
            with mock.patch.object(check_skill_coverage, "Notable", return_value=nt), \
                    mock.patch.object(sys, "argv", ["check_skill_coverage.py", "--precheck", pj]):
                with self.assertRaises(SystemExit) as ctx:
                    check_skill_coverage.main()
            self.assertEqual(ctx.exception.code, 0)
        finally:
            check_skill_coverage.OUTDIR = old


class TestPoolDrift(unittest.TestCase):
    """并发的简历精析刷池 → 与快照 sig 比对时报 pool_drift（上一轮改词结论作废信号）。"""

    def test_drift_detected_between_runs(self):
        tmp = tempfile.mkdtemp(prefix="csc_drift_")
        try:
            first = check_skill_coverage.pool_drift(["总账处理", "设备维修"], tmp)
            self.assertIsNone(first)                    # 首次运行无快照 → None
            again_same = check_skill_coverage.pool_drift(["总账处理", "设备维修"], tmp)
            self.assertIsNone(again_same)               # 池不变 → 不告警
            drift = check_skill_coverage.pool_drift(["总账处理", "设备维修", "会计核算"], tmp)
            self.assertEqual(drift, (2, 3))             # 池变 → (旧数, 新数)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
