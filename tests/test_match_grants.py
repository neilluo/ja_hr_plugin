# -*- coding: utf-8 -*-
"""match 链 v2 计分契约回归：baseline 注入 + grants 重算 + drop 行归零。

覆盖（TDD 先行，源码尚在旧契约时预期 fail）：
  1. prepare 给每个 candidate 注入 baseline{skill_score,bonus_score,total_score,recommend,
     must_hits,must_miss,bonus_hits,bonus_miss}，取值与 match_gated.hits + score_counts
     直算一致（subagent 不再自己算分，只判增量命中 grants）；
  2. keep 行的分数由 merge 用「机械命中 ∪ 有效 grants」重算，等于 match_gated.score_counts
     的结果，recommend 档位由同一真源派生（match_gated.REC_MIN/PEND_MIN），evidence 由代码组装、
     含 grant 项与 ※ 标记（subagent 自报的分数/推荐/依据一律不采信）；
  3. grant 合法性：{side,item,basis} 三键齐全、item 在岗位词表内、basis 非空；
     违规 grant 被丢弃并记入 observations.invalid_grants，且不影响分数；
  4. drop 行（keep=false）的 skill_score/bonus_score/total_score/recommend/evidence 统一 None。

纪律：纯本地文件操作，不触网（prepare 的 Notable 换成内存 mock，merge 不构造 Notable）；
OUTDIR/PAIRS/FINAL 重定向到 tempfile 目录（隔离手法同 tests/test_refine_lock.py），
绝不污染真实 outputs/。
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import match_analyze  # noqa: E402
import match_gated  # noqa: E402
from semantic_score import toks  # noqa: E402  分词唯一真源 shared/vocab

MUST = "设备维修、点检管理、备件管理、安全管理"
BONUS = "光伏、项目管理"
# 候选人机械命中：设备维修、点检管理（备件管理/安全管理未命中 → 可被 grant 补）
CAND_SKILLS = ["设备维修", "点检管理", "拉晶"]
BASELINE_KEYS = {"skill_score", "bonus_score", "total_score", "recommend",
                 "must_hits", "must_miss", "bonus_hits", "bonus_miss"}


def _jf():
    return {"job_id": "J1", "job_name": "设备工程师", "department": "设备部", "org": "制造中心",
            "hard_gates": "", "must_skills": MUST, "bonus_skills": BONUS,
            "must_weight": 1.0, "bonus_weight": 0.0}


def _cf(skills=None):
    return {"name": "张三", "phone": "13800000000", "skills": list(skills or CAND_SKILLS),
            "education": "本科", "years_experience": 5, "certificates": "", "major": "机械",
            "expected_position": "设备工程师", "org": "制造中心"}


def _vocab():
    """岗位词表：must ∪ bonus 分词（grant.item 的合法性判据）。"""
    return sorted(set(toks(MUST)) | set(toks(BONUS)))


def _expected(hit_set):
    """用 match_gated 真源直算期望值：hit_set = 命中集合（机械 ∪ 有效 grants）。

    形状对齐权威契约：hits 返回 (hm, hb) 元组、score_counts 收六个标量返回四元组
    （2026-09-29 主控仲裁：实现侧形状为准，测试 helper 适配）。"""
    jf = _jf()
    must, bonus = toks(jf["must_skills"]), toks(jf["bonus_skills"])
    n_hm = len([m for m in must if m in hit_set])
    n_hb = len([b for b in bonus if b in hit_set])
    sk, bo, tot, rec = match_gated.score_counts(
        n_hm, n_hb, len(must), len(bonus),
        float(jf["must_weight"]), float(jf["bonus_weight"]))
    return {"skill_score": sk, "bonus_score": bo, "total_score": tot, "recommend": rec}


def _baseline_expected():
    jf = _jf()
    hm, hb = match_gated.hits(_cf(), jf)
    must, bonus = toks(jf["must_skills"]), toks(jf["bonus_skills"])
    sk, bo, tot, rec = match_gated.score_counts(
        len(hm), len(hb), len(must), len(bonus),
        float(jf["must_weight"]), float(jf["bonus_weight"]))
    return {"skill_score": sk, "bonus_score": bo, "total_score": tot, "recommend": rec,
            "must_hits": hm, "must_miss": [m for m in must if m not in hm],
            "bonus_hits": hb, "bonus_miss": [b for b in bonus if b not in hb]}


def _block(jid="J1", cands=(("r1", "张三"),)):
    return {"job": dict(_jf(), job_id=jid),
            "candidates": [{"id": rid, "name": name, "phone": "13800000000",
                            "education": "本科", "years": 5, "certificates": "",
                            "major": "机械", "skills": list(CAND_SKILLS),
                            "expected_position": "设备工程师", "org": "制造中心"}
                           for rid, name in cands]}


def _row(name="张三", job_id="J1", keep=True, grants=None, **ov):
    r = {"job_id": job_id, "name": name, "keep": keep,
         "grants": grants if grants is not None else [],
         "ai_analysis": "结论：待定。亮点：设备维修与点检经验充足。缺口：无明显风险。建议：约技术面。"}
    r.update(ov)
    return r


def _grant(item, side="must", basis="简历项目「年降本1400万」体现该项能力"):
    return {"side": side, "item": item, "basis": basis}


class FakeNotable:
    """最小内存 mock：只提供 prepare 用到的 list_records（忽略 biz_fields 返回全量）。"""

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        return [dict(r) for r in self.tables.get(table, [])]


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ma_grants_")
        self._saved = (match_analyze.OUTDIR, match_analyze.PAIRS, match_analyze.FINAL,
                       match_analyze.Notable)
        match_analyze.OUTDIR = self.tmp
        match_analyze.PAIRS = os.path.join(self.tmp, "gate_pairs.json")
        match_analyze.FINAL = os.path.join(self.tmp, "match_final.json")

    def tearDown(self):
        (match_analyze.OUTDIR, match_analyze.PAIRS, match_analyze.FINAL,
         match_analyze.Notable) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def _prepare(self, blocks=None, pairs=None):
        pairs = pairs if pairs is not None else [
            {"rid": "r1", "name": "张三", "job_id": "J1", "total": 50, "recommend": "不推荐"}]
        with open(match_analyze.PAIRS, "w", encoding="utf-8") as f:
            json.dump(pairs, f, ensure_ascii=False)
        match_analyze.Notable = lambda: FakeNotable(
            {"job": [{"id": "job_J1", "fields": _jf()}],
             "resume": [{"id": "r1", "fields": _cf()}]})
        return match_analyze.prepare(["--batch", "1"])

    def _pending(self, blocks):
        self._write("match_pending_part1.json", blocks)

    def _merge(self):
        """跑 merge 子命令，返回 (exit_code, report, FINAL rows)。"""
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["match_analyze.py", "merge"]
        try:
            with contextlib.redirect_stdout(buf):
                match_analyze.main()
            code = 0
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        finally:
            sys.argv = old_argv
        rep = None
        for ln in buf.getvalue().splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    d = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(d, dict):
                    rep = d
        rows = None
        if os.path.exists(match_analyze.FINAL):
            with open(match_analyze.FINAL, encoding="utf-8") as f:
                rows = json.load(f)
        return code, rep, rows


class TestBaselineInjection(_Sandbox):
    """prepare 注入的 baseline 必须与 match_gated 直算一致（分数真源不下放给 subagent）。"""

    def test_hits_and_score_counts_are_the_oracle(self):
        # 先钉住真源形状：hits 返回 (hm, hb) 命中列表元组，score_counts 收六个标量返回四元组
        hm, hb = match_gated.hits(_cf(), _jf())
        self.assertEqual(sorted(hm), ["点检管理", "设备维修"])
        self.assertEqual(hb, [])
        must, bonus = toks(MUST), toks(BONUS)
        sk, bo, tot, rec = match_gated.score_counts(
            len(hm), len(hb), len(must), len(bonus),
            float(_jf()["must_weight"]), float(_jf()["bonus_weight"]))
        self.assertEqual(sk, 50)          # must_weight=1.0 → 2/4
        self.assertEqual(tot, 50)
        self.assertEqual(rec, match_gated.REJ_LABEL)

    def test_prepare_injects_baseline_matching_gated(self):
        meta = self._prepare()
        self.assertEqual(meta["batches"], 1)
        with open(os.path.join(self.tmp, "match_pending_part1.json"), encoding="utf-8") as f:
            pend = json.load(f)
        cands = [c for b in pend for c in b["candidates"]]
        self.assertEqual(len(cands), 1)
        base = cands[0].get("baseline")
        self.assertIsInstance(base, dict, "prepare 未给 candidate 注入 baseline：%r" % cands[0])
        self.assertEqual(set(base), BASELINE_KEYS)
        self.assertEqual(base, _baseline_expected())

    def test_baseline_is_not_subagent_writable(self):
        """baseline 属输入侧：subagent 改它不该影响重算（分数只由代码从命中集合算）。"""
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json",
                    [_row(grants=[_grant("备件管理")],
                          baseline={"total_score": 999, "recommend": "推荐"})])
        code, rep, rows = self._merge()
        self.assertEqual(code, 0)
        exp = _expected(set(_baseline_expected()["must_hits"]) | {"备件管理"})
        self.assertEqual(rows[0]["total_score"], exp["total_score"])
        self.assertNotEqual(rows[0]["total_score"], 999)


class TestKeepRowRecompute(_Sandbox):
    def test_valid_grant_recomputes_score_from_source(self):
        self._prepare()
        self._pending([_block()])
        # 机械命中 {设备维修, 点检管理} + grant {备件管理} → 3/4
        self._write("match_done_part1.json", [_row(grants=[_grant("备件管理")])])
        code, rep, rows = self._merge()
        self.assertEqual(code, 0, "报告=%r" % rep)
        exp = _expected({"设备维修", "点检管理", "备件管理"})
        r = rows[0]
        self.assertEqual(r["skill_score"], exp["skill_score"])
        self.assertEqual(r["bonus_score"], exp["bonus_score"])
        self.assertEqual(r["total_score"], exp["total_score"])
        self.assertEqual(r["recommend"], exp["recommend"])

    def test_subagent_reported_scores_ignored(self):
        """subagent 自报的分数/推荐不被采信：代码重算值覆盖（乱报到 100 也压回真值）。"""
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json",
                    [_row(grants=[_grant("备件管理")], skill_score=100, bonus_score=100,
                          total_score=100, recommend="推荐")])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        exp = _expected({"设备维修", "点检管理", "备件管理"})
        self.assertEqual(rows[0]["total_score"], exp["total_score"])
        self.assertEqual(rows[0]["recommend"], exp["recommend"])
        self.assertNotEqual(rows[0]["total_score"], 100)

    def test_no_grants_equals_baseline(self):
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json", [_row(grants=[])])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        base = _baseline_expected()
        for k in ("skill_score", "bonus_score", "total_score", "recommend"):
            self.assertEqual(rows[0][k], base[k])

    def test_grant_cannot_exceed_full_marks(self):
        """全部 must 项被 grant 补齐 → must_weight=1.0 下 total=100，档位=推荐。"""
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json",
                    [_row(grants=[_grant("备件管理"), _grant("安全管理")])])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["total_score"], 100)
        self.assertEqual(rows[0]["recommend"], match_gated.REC_LABEL)

    def test_recommend_tier_follows_threshold_constants(self):
        """档位边界由 match_gated.REC_MIN/PEND_MIN 决定（改阈值只改代码，测试随之成立）。"""
        self._prepare()
        self._pending([_block()])
        # bonus_weight=0 → bonus 不计分；must 3/4 = 75 → 落在 [PEND_MIN, REC_MIN) 待定档
        self._write("match_done_part1.json", [_row(grants=[_grant("备件管理")])])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        tot = rows[0]["total_score"]
        want = (match_gated.REC_LABEL if tot >= match_gated.REC_MIN else
                match_gated.PEND_LABEL if tot >= match_gated.PEND_MIN else
                match_gated.REJ_LABEL)
        self.assertEqual(rows[0]["recommend"], want)
        self.assertEqual(tot, 75)
        self.assertEqual(want, match_gated.PEND_LABEL)

    def test_evidence_assembled_by_code_with_grant_marker(self):
        """evidence 由代码组装：含计数口径、grant 项与 ※ 标记（区分机械命中与语义放行）。"""
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json", [_row(grants=[_grant("备件管理")])])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        ev = rows[0]["evidence"]
        self.assertIsInstance(ev, str)
        self.assertIn("语义匹配：必备3/4", ev)
        self.assertIn("备件管理", ev)
        self.assertIn("※", ev, "evidence 未标记 grant 项：%s" % ev)
        self.assertIn("未命中：安全管理", ev)

    def test_evidence_ignores_subagent_text(self):
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json",
                    [_row(grants=[_grant("备件管理")], evidence="subagent 自己写的依据")])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        self.assertNotIn("subagent 自己写的依据", rows[0]["evidence"])
        self.assertIn("语义匹配", rows[0]["evidence"])


class TestInvalidGrants(_Sandbox):
    """非法 grant：丢弃 + observations.invalid_grants 记账，分数按机械命中算（不涨不跌）。"""

    def _run(self, grants):
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json", [_row(grants=grants)])
        code, rep, rows = self._merge()
        return code, rep, rows[0]

    def test_item_outside_vocab_rejected(self):
        code, rep, r = self._run([_grant("拉晶")])       # 候选人有、但岗位词表没有
        self.assertEqual(code, 0)
        exp = _expected({"设备维修", "点检管理"})
        self.assertEqual(r["total_score"], exp["total_score"])
        self.assertEqual(r["recommend"], exp["recommend"])
        self.assertIn("invalid_grants", rep["observations"])
        self.assertTrue(any("拉晶" in json.dumps(x, ensure_ascii=False)
                            for x in rep["observations"]["invalid_grants"]),
                        "invalid_grants 未记该项：%r" % rep["observations"])

    def test_empty_basis_rejected(self):
        for basis in ("", "   ", None):
            with self.subTest(basis=basis):
                code, rep, r = self._run([_grant("备件管理", basis=basis)])
                self.assertEqual(code, 0)
                self.assertEqual(r["total_score"], _expected({"设备维修", "点检管理"})["total_score"])
                self.assertTrue(rep["observations"].get("invalid_grants"))

    def test_bad_side_rejected(self):
        for side in ("musts", "MUST", "必备", "", None):
            with self.subTest(side=side):
                code, rep, r = self._run([_grant("备件管理", side=side)])
                self.assertEqual(code, 0)
                self.assertEqual(r["total_score"], _expected({"设备维修", "点检管理"})["total_score"])
                self.assertTrue(rep["observations"].get("invalid_grants"),
                                "side=%r 未被判非法" % (side,))

    def test_malformed_grant_shapes_rejected(self):
        for g in ("备件管理", ["must", "备件管理"], {"side": "must", "item": "备件管理"},
                  {"side": "must", "basis": "x"}, {"side": "bonus", "item": "", "basis": "x"},
                  42, None):
            with self.subTest(g=g):
                code, rep, r = self._run([g])
                self.assertEqual(code, 0, "非法 grant 只进观察，不得阻断合并")
                self.assertEqual(r["total_score"],
                                 _expected({"设备维修", "点检管理"})["total_score"])
                self.assertTrue(rep["observations"].get("invalid_grants"))

    def test_bonus_side_grant_scores_bonus(self):
        """side=bonus 且 item 在加分词表 → 计入 bonus_score（bonus_weight=0 时不涨总分）。"""
        code, rep, r = self._run([_grant("光伏", side="bonus")])
        self.assertEqual(code, 0)
        self.assertFalse(rep["observations"].get("invalid_grants"))
        exp = _expected({"设备维修", "点检管理", "光伏"})
        self.assertEqual(r["bonus_score"], exp["bonus_score"])
        self.assertEqual(r["total_score"], exp["total_score"])

    def test_mixed_valid_and_invalid_only_valid_counted(self):
        code, rep, r = self._run([_grant("备件管理"), _grant("拉晶"),
                                  _grant("安全管理", basis="")])
        self.assertEqual(code, 0)
        self.assertEqual(r["total_score"],
                         _expected({"设备维修", "点检管理", "备件管理"})["total_score"])
        bad = rep["observations"]["invalid_grants"]
        self.assertEqual(len(bad), 2, "应记 2 条非法 grant：%r" % bad)

    def test_duplicate_grant_not_double_counted(self):
        """同侧重复 grant 同一 item 只计一次：防命中数虚增把档位抬错（review 抓出的通胀缺陷）。"""
        code, rep, r = self._run([_grant("备件管理"), _grant("备件管理"),
                                  _grant("备件管理", basis="另一条依据")])
        self.assertEqual(code, 0)
        exp = _expected({"设备维修", "点检管理", "备件管理"})   # 3/4，不是满配
        self.assertEqual(r["total_score"], exp["total_score"])
        self.assertNotEqual(r["total_score"], 100)
        self.assertFalse(rep["observations"].get("invalid_grants"),
                         "重复 grant 是去重不是非法：%r" % rep["observations"])

    def test_already_hit_grant_is_not_invalid_but_not_double_counted(self):
        """grant 一个机械已命中项：不加分（集合并集天然去重），也不该报非法。"""
        code, rep, r = self._run([_grant("设备维修")])
        self.assertEqual(code, 0)
        self.assertEqual(r["total_score"], _expected({"设备维修", "点检管理"})["total_score"])
        self.assertFalse(rep["observations"].get("invalid_grants"))

    def test_invalid_grants_never_block_merge(self):
        """观察类问题（L2）绝不参与 exit code / 行丢弃（不变量 10 软观察纪律）。"""
        code, rep, r = self._run([_grant("拉晶"), _grant("备件管理", basis="")])
        self.assertEqual(code, 0)
        self.assertIsNotNone(r)
        self.assertTrue(rep["observations"]["invalid_grants"])


class TestDropRowNulls(_Sandbox):
    def test_drop_row_scores_and_evidence_all_none(self):
        self._prepare()
        self._pending([_block(cands=(("r1", "张三"), ("r2", "李四")))])
        self._write("match_done_part1.json",
                    [_row("张三"), _row("李四", keep=False,
                                        skill_score=90, bonus_score=30, total_score=120,
                                        recommend="推荐", evidence="subagent 写的依据",
                                        grants=[_grant("备件管理")])])
        code, rep, rows = self._merge()
        self.assertEqual(code, 0)
        drop = [r for r in rows if r["name"] == "李四"][0]
        self.assertFalse(drop["keep"])
        for k in ("skill_score", "bonus_score", "total_score", "recommend", "evidence"):
            self.assertIsNone(drop[k], "drop 行 %s 必须为 None，实际 %r" % (k, drop[k]))

    def test_keep_row_unaffected_by_sibling_drop(self):
        self._prepare()
        self._pending([_block(cands=(("r1", "张三"), ("r2", "李四")))])
        self._write("match_done_part1.json",
                    [_row("张三", grants=[_grant("备件管理")]), _row("李四", keep=False)])
        code, _rep, rows = self._merge()
        self.assertEqual(code, 0)
        keep = [r for r in rows if r["name"] == "张三"][0]
        exp = _expected({"设备维修", "点检管理", "备件管理"})
        self.assertEqual(keep["total_score"], exp["total_score"])
        self.assertIsInstance(keep["evidence"], str)
        self.assertTrue(keep["evidence"])

    def test_drop_row_grants_not_counted_anywhere(self):
        self._prepare()
        self._pending([_block()])
        self._write("match_done_part1.json",
                    [_row(keep=False, grants=[_grant("备件管理"), _grant("安全管理")])])
        code, rep, rows = self._merge()
        self.assertEqual(code, 0)
        self.assertIsNone(rows[0]["total_score"])
        self.assertFalse(rep["observations"].get("invalid_grants"),
                         "drop 行的 grants 无需校验（整行不写表），不得报非法")


if __name__ == "__main__":
    unittest.main(verbosity=2)
