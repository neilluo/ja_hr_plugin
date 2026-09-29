# -*- coding: utf-8 -*-
"""match 链 v2 逐批归属校验回归：done_part<i> 只许写 pending_part<i> 的行。

覆盖（TDD 先行，源码尚在旧契约时预期 fail）：
  1. 串批（part1 的行出现在 done_part2）→ 报告 misattributed 含 (part, job_id, name)、
     该行不进 match_final.json、进程 exit 2（打印报告后再退，报告必须已落 stdout）；
  2. 缺批（done_part2 缺失）→ missing_batches=[2] 且 exit 2；
  3. 正常场景 → exit 0、rows 完整、misattributed/missing_batches 均空。

防的事故：subagent 写错文件名/写串批时，旧 merge 用**全局** (job_id,name)→rid 映射照样收行，
错批产物被静默写回、rid 也可能张冠李戴（apply 按 rid 取简历）。归属校验把"串批"从
静默污染变成显式 exit 2。

纪律：纯本地文件操作，不触网（merge 不构造 Notable）；OUTDIR/PAIRS/FINAL 全部重定向到
tempfile 目录（隔离手法同 tests/test_refine_lock.py），绝不污染真实 outputs/。
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

MUST = "设备维修、点检管理"


def _block(jid, cands):
    """pending part 的一个岗位块（形态与 match_analyze.prepare 产出一致）。"""
    return {"job": {"job_id": jid, "job_name": "设备工程师", "department": "设备部",
                    "org": "制造中心", "hard_gates": "", "must_skills": MUST,
                    "bonus_skills": "", "must_weight": 1.0, "bonus_weight": 0.0},
            "candidates": [{"id": rid, "name": name, "phone": "13800000000",
                            "education": "本科", "years": 5, "certificates": "",
                            "major": "机械", "skills": ["设备维修"],
                            "expected_position": "设备工程师", "org": "制造中心"}
                           for rid, name in cands]}


def _row(job_id, name, keep=True, **ov):
    r = {"job_id": job_id, "name": name, "keep": keep, "grants": [],
         "ai_analysis": "结论：待定。亮点：设备维修经验充足。缺口：无明显风险。建议：约技术面。"}
    r.update(ov)
    return r


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ma_attr_")
        self._saved = (match_analyze.OUTDIR, match_analyze.PAIRS, match_analyze.FINAL)
        match_analyze.OUTDIR = self.tmp
        match_analyze.PAIRS = os.path.join(self.tmp, "gate_pairs.json")
        match_analyze.FINAL = os.path.join(self.tmp, "match_final.json")

    def tearDown(self):
        (match_analyze.OUTDIR, match_analyze.PAIRS,
         match_analyze.FINAL) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 夹具与调用 ────────────────────────────────────────────────────────────
    def _write(self, name, obj):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def _two_parts(self):
        self._write("match_pending_part1.json", [_block("J1", [("r1", "张三")])])
        self._write("match_pending_part2.json", [_block("J2", [("r2", "李四")])])

    def _run_merge(self):
        """跑 merge 子命令，返回 (exit_code, report_dict, stdout)。

        走 main()（与 CLI 同一路径），SystemExit 被捕获成 exit code；正常返回记 0。
        报告 = stdout 里最后一个可解析的 JSON 对象（read_done 的告警行不是 JSON，天然跳过）。"""
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
        out = buf.getvalue()
        rep = None
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            try:
                d = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(d, dict):
                rep = d
        return code, rep, out

    def _final(self):
        if not os.path.exists(match_analyze.FINAL):
            return None
        with open(match_analyze.FINAL, encoding="utf-8") as f:
            return json.load(f)

    # ── 1. 串批 ──────────────────────────────────────────────────────────────
    def test_cross_batch_row_flagged_not_collected_exit2(self):
        self._two_parts()
        # done_part1 只交自己的行；done_part2 把 part1 的 (J1,张三) 也写了进来
        self._write("match_done_part1.json", [_row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "李四"), _row("J1", "张三")])
        code, rep, out = self._run_merge()

        self.assertIsNotNone(rep, "merge 必须先打印报告再 exit 2，stdout=%r" % out)
        self.assertIn("misattributed", rep, "报告缺 misattributed 键：%r" % rep)
        self.assertEqual(rep["misattributed"],
                         [[2, "J1", "张三"]])
        self.assertEqual(code, 2, "串批必须 exit 2（实际 %s）" % code)

        final = self._final()
        self.assertIsNotNone(final, "报告已打印，match_final.json 仍应落盘供人工排查")
        rows = [(r.get("job_id"), r.get("name")) for r in final]
        # 归属正确的行照常收（part1 的张三、part2 的李四），串批副本被丢弃
        self.assertEqual(sorted(rows), [("J1", "张三"), ("J2", "李四")])
        self.assertEqual(len([x for x in rows if x == ("J1", "张三")]), 1,
                         "串批副本被收进 FINAL：%r" % rows)

    def test_misattributed_row_absent_from_final(self):
        """精确断言：FINAL 里 (J1,张三) 只允许出现一次（来自 done_part1），串批副本被丢弃。"""
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三", ai_analysis="part1 的产物")])
        self._write("match_done_part2.json", [_row("J2", "李四"),
                                             _row("J1", "张三", ai_analysis="串批的产物")])
        code, _rep, _out = self._run_merge()
        self.assertEqual(code, 2)
        final = self._final()
        zs = [r for r in final if (r.get("job_id"), r.get("name")) == ("J1", "张三")]
        self.assertEqual(len(zs), 1, "串批副本被收进 FINAL：%r" % zs)
        self.assertEqual(zs[0].get("ai_analysis"), "part1 的产物")
        self.assertNotIn("串批的产物", json.dumps(final, ensure_ascii=False))

    def test_row_in_wrong_part_only_is_misattributed(self):
        """行只出现在错批（自己那批没交）：仍是 misattributed + exit 2，且不得被当作产出收下。"""
        self._two_parts()
        self._write("match_done_part1.json", [])
        self._write("match_done_part2.json", [_row("J2", "李四"), _row("J1", "张三")])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 2)
        self.assertEqual(rep["misattributed"], [[2, "J1", "张三"]])
        final = self._final()
        self.assertNotIn(("J1", "张三"), {(r.get("job_id"), r.get("name")) for r in final})

    # ── 2. 缺批 ──────────────────────────────────────────────────────────────
    def test_missing_batch_exits_2(self):
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三")])   # part2 的 done 缺失
        code, rep, _out = self._run_merge()
        self.assertIsNotNone(rep)
        self.assertEqual(rep["missing_batches"], [2])
        self.assertEqual(code, 2, "缺批必须 exit 2（实际 %s）" % code)

    def test_missing_batch_reported_even_when_others_misattributed(self):
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三")])   # part2 done 缺失
        self._write("match_done_part3.json", [_row("J1", "张三")])   # 无 pending_part3 的孤儿
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 2)
        self.assertEqual(rep["missing_batches"], [2])

    # ── 3. 正常场景 ──────────────────────────────────────────────────────────
    def test_happy_path_exit0_rows_complete(self):
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "李四")])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 0, "正常场景不得非零退出，报告=%r" % rep)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["misattributed"], [])
        self.assertEqual(rep["missing_batches"], [])
        final = self._final()
        self.assertIsNotNone(final)
        self.assertEqual(sorted((r["job_id"], r["name"]) for r in final),
                         [("J1", "张三"), ("J2", "李四")])
        # rid 由 pending 回联补全（apply 按 record id 取简历，重名不覆盖）
        self.assertEqual({r["name"]: r.get("rid") for r in final},
                         {"张三": "r1", "李四": "r2"})

    def test_same_name_across_parts_is_not_cross_contaminated(self):
        """重名候选人分属两岗两块：各自归属各自批，不得因 (job_id,name) 撞车被判串批。"""
        self._write("match_pending_part1.json", [_block("J1", [("r1", "张三")])])
        self._write("match_pending_part2.json", [_block("J2", [("r2", "张三")])])
        self._write("match_done_part1.json", [_row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "张三")])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 0)
        self.assertEqual(rep["misattributed"], [])
        final = self._final()
        self.assertEqual({(r["job_id"], r["name"]): r.get("rid") for r in final},
                         {("J1", "张三"): "r1", ("J2", "张三"): "r2"})

    def test_drop_rows_still_attributed(self):
        """keep=false 的行同样受归属校验（不能因为不写表就放行串批）。"""
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "李四", keep=False),
                                             _row("J1", "张三", keep=False)])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 2)
        self.assertEqual(rep["misattributed"], [[2, "J1", "张三"]])

    def test_duplicate_row_within_batch_first_wins(self):
        """同一 done 文件内重复 (job_id,name)：首行生效、重复只报不收（防 grants 双计通胀）。"""
        self._two_parts()
        self._write("match_done_part1.json", [_row("J1", "张三"), _row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "李四")])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 0, "批内重复不阻断（首行已生效、不丢数据），报告=%r" % rep)
        self.assertEqual(rep["duplicate_rows"], [[1, "J1", "张三"]])
        self.assertEqual(rep["decided"], 2)
        final = self._final()
        self.assertEqual(sum(1 for r in final if r["name"] == "张三"), 1)

    def test_pending_corrupt_reported_separately_from_misattributed(self):
        """pending 自身损坏 ≠ agent 串写：单独成键报出（恢复动作是重跑 prepare 而非补发）。"""
        self._two_parts()
        with open(os.path.join(self.tmp, "match_pending_part1.json"), "w",
                  encoding="utf-8") as f:
            f.write("{这不是 JSON")
        self._write("match_done_part1.json", [_row("J1", "张三")])
        self._write("match_done_part2.json", [_row("J2", "李四")])
        code, rep, _out = self._run_merge()
        self.assertEqual(code, 2)
        self.assertEqual(rep["pending_corrupt"], [1],
                         "pending 损坏须独立成键，报告=%r" % rep)
        # 损坏批的 done 行按归属从严报串写，但恢复指引靠 pending_corrupt 区分
        self.assertEqual(rep["misattributed"], [[1, "J1", "张三"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
