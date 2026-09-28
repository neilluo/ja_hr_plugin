# -*- coding: utf-8 -*-
"""JD 链（job-intake）弱依赖回归锁：约束强度必须匹配违规的可逆性。
与简历链 tests/test_soft_dependencies.py 对称。

锁定"曾被旧硬门槛整行丢弃的样例，如今一律接受并写回"：
  - 必备技能 3 / 15 个；加分项 2 / 12 个；
  - 7 字中文术语（质量管理体系认证）、1 字中文词、21 字符英文词（Continuous Plating Line）；
  - hard_gates 缺「证书：」段（自动补"不作硬性要求"）、五段全缺、半角「;」「:」分隔；
  - must∩bonus 重复词（从 bonus 剔除、保留在 must，不是错误）；
  - must_skills/bonus_skills 给成数组而非「、」串；
  - 三列俱在但 hard_gates 为空串。
只有 L0（非 dict、缺/空 job_id、job_id 重复、三列全空）才产生 dropped_rows。
observations 不影响 all_complete；dropped_rows 非空才 false。normalize_row 幂等。
纯本地文件操作，不触网。OUTDIR 一律重定向临时目录（历史教训：硬编码输出路径会把
活租约锁漏进真实 outputs/）。
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
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

import jobs_analyze as ja                # noqa: E402
import soften                            # noqa: E402

GATES = ("学历：本科及以上；专业：机械相关专业；经验：2年及以上设备维护经验；"
         "证书：不作硬性要求；年龄：25-40岁")


def _row(**ov):
    base = {"job_id": "J1",
            "hard_gates": GATES,
            "must_skills": "设备运维、点检、故障维修、备件管理、机械、电气",
            "bonus_skills": "光伏、TPM、项目管理、培训带教"}
    base.update(ov)
    return base


class TestNormalizeRow(unittest.TestCase):
    def test_gate_segment_missing_is_filled_not_rejected(self):
        nr, kinds = ja.normalize_row(
            _row(hard_gates=GATES.replace("证书：不作硬性要求；", "")))
        self.assertIn("gate_filled", kinds)
        self.assertIn("证书：不作硬性要求", nr["hard_gates"])
        self.assertEqual(ja.validate_row(nr), [])
        # 段序按 DONE_GATE_SEGS 规范（补的段追加在末尾，已有段保持原序）
        self.assertTrue(nr["hard_gates"].startswith("学历：本科及以上；专业："))

    def test_all_five_segments_missing(self):
        nr, kinds = ja.normalize_row(_row(hard_gates="仅限内部推荐"))
        self.assertIn("gate_filled", kinds)
        for seg in ja.DONE_GATE_SEGS:
            self.assertIn(seg, nr["hard_gates"])
        self.assertIn("仅限内部推荐", nr["hard_gates"])   # 原文不丢
        self.assertEqual(ja.validate_row(nr), [])

    def test_halfwidth_separators_fixed(self):
        nr, kinds = ja.normalize_row(
            _row(hard_gates="学历:本科及以上;专业:机械相关;经验:2年及以上;证书:无;年龄:25-40"))
        self.assertIn("punct_fixed", kinds)
        self.assertNotIn(";", nr["hard_gates"])
        for seg in ja.DONE_GATE_SEGS:
            self.assertIn(seg, nr["hard_gates"])
        self.assertEqual(ja.validate_row(nr), [])

    def test_empty_gates_not_padded_with_placeholders(self):
        # hard_gates 空时不填五段占位：否则"什么都没产出"与"确实无硬性要求"无法区分，
        # 且"三列全空"这一 L0 将永不可达
        nr, _ = ja.normalize_row(_row(hard_gates=""))
        self.assertEqual(nr["hard_gates"], "")
        self.assertEqual(ja.validate_row(nr), [])        # 另两列有内容 → 照常写回
        nr, _ = ja.normalize_row(_row(hard_gates="   ", must_skills="", bonus_skills=""))
        self.assertEqual(ja.validate_row(nr)[0][:4], "三列全空")

    def test_must_bonus_overlap_deduped_not_rejected(self):
        nr, kinds = ja.normalize_row(_row(bonus_skills="光伏、TPM、成本核算",
                                          must_skills="成本核算、设备运维、点检"))
        self.assertIn("dup_removed", kinds)
        self.assertEqual(nr["bonus_skills"], "光伏、TPM")   # 必备优先于加分，重复词留在 must
        self.assertIn("成本核算", nr["must_skills"])
        self.assertEqual(ja.validate_row(nr), [])

    def test_arrays_accepted_as_skill_lists(self):
        nr, kinds = ja.normalize_row(_row(must_skills=["设备运维", "点检"],
                                          bonus_skills=["光伏", "TPM"]))
        self.assertIn("skills_joined", kinds)
        self.assertEqual(nr["must_skills"], "设备运维、点检")
        self.assertEqual(nr["bonus_skills"], "光伏、TPM")
        self.assertEqual(ja.validate_row(nr), [])

    def test_idempotent(self):
        messy = _row(hard_gates="学历:本科;经验:2年",           # 半角 + 缺三段
                     must_skills=["质量管理体系认证", "点检"],   # 数组 + 7 字中文词
                     bonus_skills="点检、光伏")                  # 与 must 重复
        once, kinds1 = ja.normalize_row(messy)
        self.assertTrue(kinds1)
        twice, kinds2 = ja.normalize_row(once)
        self.assertEqual(kinds2, [])
        self.assertEqual(once, twice)
        self.assertEqual(ja.validate_row(twice), [])

    def test_input_not_mutated(self):
        row = _row(must_skills=["设备运维"], hard_gates="学历:本科")
        snapshot = json.loads(json.dumps(row))
        ja.normalize_row(row)
        self.assertEqual(row, snapshot)

    def test_missing_required_key_defaults_to_empty(self):
        nr, _ = ja.normalize_row({"job_id": "J1", "must_skills": "设备运维"})
        self.assertEqual(nr["hard_gates"], "")
        self.assertEqual(nr["bonus_skills"], "")
        self.assertEqual(ja.validate_row(nr), [])


class TestMergeAcceptance(unittest.TestCase):
    """端到端：曾被拒的样例经 merge 全部写回；仅 L0 进 dropped_rows。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jobs_soft_merge_")
        self.old_outdir = ja.OUTDIR
        ja.OUTDIR = self.tmp

    def tearDown(self):
        ja.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_merge(self, done_rows, pending=None):
        if pending is None:
            pending = [{"job_id": r["job_id"]} for r in done_rows
                       if isinstance(r, dict) and r.get("job_id")]
        with open(os.path.join(self.tmp, "jobs_pending_part1.json"), "w",
                  encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False)
        with open(os.path.join(self.tmp, "jobs_done_part1.json"), "w", encoding="utf-8") as f:
            json.dump(done_rows, f, ensure_ascii=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ja.merge([])
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_all_former_rejects_written_back(self):
        rows = [
            _row(job_id="must3", must_skills="设备运维、点检、故障维修"),
            _row(job_id="must15", must_skills="、".join("技能%d" % i for i in range(15))),
            _row(job_id="bonus2", bonus_skills="光伏、TPM"),
            _row(job_id="bonus12", bonus_skills="、".join("加分%d" % i for i in range(12))),
            _row(job_id="zh7", must_skills="质量管理体系认证、设备运维、点检、故障维修、机械、电气"),
            _row(job_id="zh1", must_skills="焊、设备运维"),
            _row(job_id="en21", must_skills="Continuous Plating Line、PLC"),
            _row(job_id="gatemiss", hard_gates=GATES.replace("证书：不作硬性要求；", "")),
            _row(job_id="gatenone", hard_gates="仅限内部推荐"),
            _row(job_id="gatehalf",
                 hard_gates="学历:本科及以上;专业:机械相关;经验:2年及以上;证书:无;年龄:25-40"),
            _row(job_id="dup", must_skills="成本核算、设备运维、点检", bonus_skills="成本核算、光伏"),
            _row(job_id="arr", must_skills=["设备运维", "点检"], bonus_skills=["光伏", "TPM"]),
            _row(job_id="gateempty", hard_gates=""),
        ]
        rep = self._run_merge(rows)
        self.assertEqual(rep["dropped_rows"], [])
        self.assertEqual(rep["merged"], len(rows))       # 一行不丢
        self.assertTrue(rep["all_complete"])             # 仅观察存在时 all_complete=True
        with open(os.path.join(self.tmp, "jobs_done.json"), encoding="utf-8") as f:
            done = json.load(f)
        self.assertEqual(set(done), {r["job_id"] for r in rows})
        self.assertIn("证书：不作硬性要求", done["gatemiss"]["hard_gates"])
        self.assertNotIn(";", done["gatehalf"]["hard_gates"])
        self.assertEqual(done["dup"]["bonus_skills"], "光伏")
        self.assertIn("成本核算", done["dup"]["must_skills"])
        self.assertEqual(done["arr"]["must_skills"], "设备运维、点检")
        self.assertEqual(done["gateempty"]["hard_gates"], "")
        # 质量问题只进 observations，照常写回
        self.assertIn("over_len_words", rep["observations"])
        obs = rep["observations"]["over_len_words"]
        self.assertEqual([p[0] for p in obs if p[1] == "质量管理体系认证"], ["zh7"])
        self.assertEqual([p[0] for p in obs if p[1] == "Continuous Plating Line"], ["en21"])
        self.assertIn("must_count_off", rep["observations"])
        self.assertEqual(sorted({p[0] for p in rep["observations"]["must_count_off"]}),
                         ["arr", "dup", "en21", "must15", "must3", "zh1"])
        self.assertIn("bonus_count_off", rep["observations"])
        self.assertEqual(sorted({p[0] for p in rep["observations"]["bonus_count_off"]}),
                         ["arr", "bonus12", "bonus2", "dup"])
        self.assertTrue(rep["all_complete"])

    def test_l0_failures_visible_in_dropped_rows(self):
        no_id = {k: v for k, v in _row().items() if k != "job_id"}
        rep = self._run_merge([_row(job_id="a"), "不是dict", no_id, _row(job_id="")],
                              pending=[{"job_id": "a"}])
        self.assertEqual(rep["merged"], 1)
        self.assertEqual([d["reason"] for d in rep["dropped_rows"]], ["缺 job_id"] * 3)
        self.assertEqual([d["job_id"] for d in rep["dropped_rows"]], [None, None, None])
        self.assertFalse(rep["all_complete"])

        rep = self._run_merge([_row(job_id="dup"), _row(job_id="dup")],
                              pending=[{"job_id": "dup"}])
        self.assertEqual(rep["merged"], 1)
        self.assertEqual(rep["dropped_rows"], [{"job_id": "dup", "reason": "job_id 重复"}])
        self.assertFalse(rep["all_complete"])

        rep = self._run_merge([_row(job_id="e", hard_gates="", must_skills="", bonus_skills="")],
                              pending=[{"job_id": "e"}])
        self.assertEqual(rep["merged"], 0)
        self.assertIn("三列全空", rep["dropped_rows"][0]["reason"])
        self.assertFalse(rep["all_complete"])

    def test_report_keys_and_normalized_counter(self):
        rep = self._run_merge([_row(job_id="a"),
                               _row(job_id="b", hard_gates=GATES + "\n",
                                    bonus_skills="光伏、设备运维",
                                    must_skills="设备运维、点检、故障维修、备件管理、机械、电气")])
        self.assertEqual(list(rep.keys()),
                         ["merged", "batches", "missing_batches", "bad_batches",
                          "dropped_rows", "normalized", "normalizations",
                          "observations", "all_complete"])
        self.assertEqual(rep["merged"], 2)
        self.assertEqual(rep["normalized"], 1)            # 只有 b 被修复
        self.assertIn("dup_removed", rep["normalizations"])
        self.assertTrue(rep["all_complete"])

    def test_observations_do_not_block_all_complete(self):
        rep = self._run_merge([_row(job_id="o", must_skills="质量管理体系认证",
                                    bonus_skills="Continuous Plating Line")])
        self.assertTrue(rep["observations"])              # 三项观察全中
        self.assertEqual(rep["dropped_rows"], [])
        self.assertTrue(rep["all_complete"])
        self.assertEqual(rep["merged"], 1)


class TestThresholdSingleSource(unittest.TestCase):
    """不变量 10：JD 数量阈值只在 shared/soften.py 一处，本链禁止抄数值。"""

    def test_no_local_threshold_literals(self):
        src = open(os.path.join(ROOT, "skills", "job-intake", "scripts",
                                "jobs_analyze.py"), encoding="utf-8").read()
        for residue in ("DONE_MUST_RANGE", "DONE_BONUS_RANGE",
                        "DONE_SKILL_LEN_MAX", "DONE_SKILL_ZH_RANGE"):
            self.assertNotIn(residue, src)
        self.assertIn("soften.JD_MUST_RANGE", src)
        self.assertIn("soften.JD_BONUS_RANGE", src)

    def test_specs_use_soften_helpers(self):
        keys = [s["key"] for s in ja.observation_specs()]
        self.assertEqual(keys, ["over_len_words", "must_count_off", "bonus_count_off"])
        row = {"must_skills": "、".join(["质量管理体系认证"] + ["SK%d" % i for i in range(10)]),
               "bonus_skills": "光伏"}
        obs = soften.soft_observations(row, ja.observation_specs())
        self.assertEqual(obs["over_len_words"], ["质量管理体系认证"])
        self.assertEqual(obs["must_count_off"], [11])     # 11 个 → 超出 soften.JD_MUST_RANGE
        self.assertEqual(obs["bonus_count_off"], [1])
        self.assertEqual(soften.JD_MUST_RANGE, (6, 10))
        self.assertEqual(soften.JD_BONUS_RANGE, (4, 8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
