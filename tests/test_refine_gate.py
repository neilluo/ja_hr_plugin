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
    """check_skill_coverage：简历标签池为空 → skipped exit 0（不误判全岗低覆盖）。"""

    def _run(self, nt):
        with mock.patch.object(check_skill_coverage, "Notable", return_value=nt), \
                mock.patch.object(sys, "argv", ["check_skill_coverage.py"]):
            with self.assertRaises(SystemExit) as ctx:
                check_skill_coverage.main()
        return ctx.exception.code

    def test_empty_pool_skips_exit_0(self):
        nt = FakeNotable({"job": [_job(refined=True, must="设备维修、点检管理")],
                          "resume": [_resume("r1", "张三", [])]})
        self.assertEqual(self._run(nt), 0)

    def test_nonempty_pool_low_coverage_exit_2(self):
        # 对照组：池非空且岗位词不同源 → 照常判低覆盖 exit 2
        nt = FakeNotable({"job": [_job(refined=True, must="账务处理、金蝶")],
                          "resume": [_resume("r1", "张三", ["设备维修"])]})
        self.assertEqual(self._run(nt), 2)


if __name__ == "__main__":
    unittest.main()
