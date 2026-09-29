# -*- coding: utf-8 -*-
"""job 链分派发瘦回归：jobs_analyze.render_prompts（per-batch 提示词渲染）、
validate_row（done 行 L0 硬门槛唯一真源）与 done_integrity（盘上产物体检，供 merge 内联判定）。
与 test_dispatch_slim（resume 链）对称。

防的事故（AGENTS.md 犯错记录）：分派时内联提示词原文撑爆工具流入参 → 截断 → 被迫分波；
"failed 先验盘上产物"靠人工 bash 回合；subagent 内嵌自检每 agent 固定多 1-3 回合。
现口径：agent 只"读→推理→写"，格式问题由 normalize_row 确定性修复、质量问题降级为
soften.soft_observations 非阻断观察，validate_row 只留 L0（非 dict / 缺重 job_id / 三列全空）；
仅 L0 违规行进 dropped_rows、不打 ai_refined_at → 下周期自动重析。纯本地文件操作，不触网。
JD 链弱依赖的端到端回归锁见 tests/test_jobs_soft_dependencies.py。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

import jobs_analyze as ja  # noqa: E402
import analyze_parts as ja_ap  # noqa: E402  公共骨架（write_dispatch 唯一真源）

GOOD_ROW = {"job_id": "J1",
            "hard_gates": "学历：本科及以上；专业：机械相关专业；经验：2年及以上设备维护经验；"
                          "证书：不作硬性要求；年龄：25-40岁",
            "must_skills": "设备运维、点检、故障维修、备件管理、机械、电气、自动化、安全管理",
            "bonus_skills": "光伏、TPM、项目管理、培训带教"}


class TestRenderPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ja_render_")
        self.old_outdir = ja.OUTDIR
        ja.OUTDIR = self.tmp

    def tearDown(self):
        ja.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_renders_one_file_per_batch_all_placeholders_filled(self):
        vocab = os.path.join(self.tmp, "resume_vocab.json")
        paths = ja.render_prompts(3, vocab)
        self.assertEqual(len(paths), 3)
        for i, p in enumerate(paths, 1):
            self.assertTrue(os.path.exists(p))
            self.assertEqual(p, os.path.join(self.tmp, "jobs_prompt_part%d.md" % i))
            with open(p, encoding="utf-8") as f:
                body = f.read()
            for ph in ("<BATCH_PATH>", "<VOCAB_PATH>", "<N>"):
                self.assertNotIn(ph, body, "占位符未替换：%s" % ph)
            self.assertIn("jobs_pending_part%d.json" % i, body)
            self.assertIn("jobs_done_part%d.json" % i, body)
            self.assertIn(vocab, body)

    def test_dispatch_index_written(self):
        paths = ja.render_prompts(2, os.path.join(self.tmp, "resume_vocab.json"))
        ja_ap.write_dispatch(self.tmp, "jobs", paths)   # 清单落盘归 prepare 侧公共骨架
        with open(os.path.join(self.tmp, "jobs_dispatch.json"), encoding="utf-8") as f:
            idx = json.load(f)
        self.assertEqual(idx["batches"], 2)
        self.assertEqual(len(idx["prompts"]), 2)

    def test_prompt_carries_no_self_validator_block(self):
        # 新口径：模板不得再内嵌 bash 校验（每 agent 省 1-3 回合），只许引用 validate_row
        with open(ja.render_prompts(1, "/tmp/vocab.json")[0], encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("python3 -X utf8", body)
        self.assertIn("validate_row", body)


class TestValidateRow(unittest.TestCase):
    """L0-only 新口径：只有"确实无法写回"才拒。质量/审美问题一律不再是硬失败
    （端到端接受样例见 tests/test_jobs_soft_dependencies.py）。"""

    def _errs(self, row):
        nr, _ = ja.normalize_row(row)
        return ja.validate_row(nr)

    def test_good_row_passes(self):
        self.assertEqual(self._errs(dict(GOOD_ROW)), [])

    def test_english_skill_exempt_from_zh_range(self):
        row = dict(GOOD_ROW, must_skills=GOOD_ROW["must_skills"] + "、PLC、CAD")
        self.assertEqual(self._errs(row), [])

    def test_l0_rejections(self):
        # 非 dict
        self.assertTrue(any("不是 dict" in e for e in ja.validate_row("不是dict")))
        # 缺/空 job_id
        self.assertTrue(any("缺 job_id" in e for e in self._errs({"hard_gates": "学历：本科"})))
        self.assertTrue(any("缺 job_id" in e
                            for e in self._errs(dict(GOOD_ROW, job_id="  "))))
        # 三列全空 = 无任何可写回内容
        self.assertTrue(any("三列全空" in e
                            for e in self._errs({"job_id": "J1", "hard_gates": "",
                                                 "must_skills": "", "bonus_skills": ""})))

    def test_quality_issues_no_longer_hard_fail(self):
        # 旧口径下这些都曾整行丢弃；现一律放过（只进观察/归一化）
        for ov in ({"must_skills": "设备运维、点检、机械"},                       # 仅 3 词
                   {"bonus_skills": "光伏"},                                      # 仅 1 词
                   {"must_skills": "、".join(["词%d" % i for i in range(15)])},    # 15 词
                   {"must_skills": "质量管理体系认证、设备运维"},                  # 7 字中文词
                   {"must_skills": "Continuous Plating Line、PLC"},               # 21 字符英文词
                   {"must_skills": "焊、设备运维"},                                # 1 字中文词
                   {"bonus_skills": "光伏、设备运维"},                            # must∩bonus 重复
                   {"hard_gates": GOOD_ROW["hard_gates"].replace("年龄：25-40岁", "")},
                   {"hard_gates": ""}):                                          # 缺门槛段/门槛空
            with self.subTest(ov=ov):
                self.assertEqual(self._errs(dict(GOOD_ROW, **ov)), [])

    def test_validate_row_is_the_only_schema_home(self):
        # SSOT：模板只许散文描述 + 引用 validate_row，禁止再内嵌可执行校验块（防口径两处抄）
        with open(os.path.join(ROOT, "skills", "job-intake", "references",
                               "job-subagent-prompt.md"), encoding="utf-8") as f:
            tpl = f.read()
        self.assertNotIn("```bash", tpl)
        self.assertNotIn("python3 -X utf8", tpl)
        self.assertIn("validate_row", tpl)


class TestMergeValidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ja_merge_")
        self.old_outdir = ja.OUTDIR
        ja.OUTDIR = self.tmp

    def tearDown(self):
        ja.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def _merge(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ja.merge([])
        return json.loads(buf.getvalue())

    def test_good_rows_merge_and_clean_integrity(self):
        self._write("jobs_pending_part1.json", [{"job_id": "J1"}, {"job_id": "J2"}])
        r2 = dict(GOOD_ROW, job_id="J2")
        self._write("jobs_done_part1.json", [GOOD_ROW, r2])
        out = self._merge()
        self.assertEqual(out["merged"], 2)
        self.assertTrue(out["all_complete"])
        done = json.load(open(os.path.join(self.tmp, "jobs_done.json"), encoding="utf-8"))
        self.assertEqual(set(done), {"J1", "J2"})

    def test_l0_row_dropped_and_reported(self):
        # L0 违规（三列全空）：不进 payload（sync 无从打标 → 下周期自动重析），进 dropped_rows
        bad = {"job_id": "J2", "hard_gates": "", "must_skills": "", "bonus_skills": ""}
        self._write("jobs_pending_part1.json", [{"job_id": "J1"}, {"job_id": "J2"}])
        self._write("jobs_done_part1.json", [GOOD_ROW, bad])
        out = self._merge()
        self.assertEqual(out["merged"], 1)
        self.assertEqual([d["job_id"] for d in out["dropped_rows"]], ["J2"])
        self.assertFalse(out["all_complete"])
        done = json.load(open(os.path.join(self.tmp, "jobs_done.json"), encoding="utf-8"))
        self.assertNotIn("J2", done)

    def test_missing_or_dup_job_id_visible_in_dropped_rows(self):
        # 曾静默 continue（`if not jid: continue`）→ 报告盲点；现必须报出
        self._write("jobs_pending_part1.json", [{"job_id": "J1"}])
        self._write("jobs_done_part1.json",
                    [GOOD_ROW, dict(GOOD_ROW, job_id="J1"), dict(GOOD_ROW, job_id="")])
        out = self._merge()
        self.assertEqual(out["merged"], 1)
        self.assertEqual([d["reason"] for d in out["dropped_rows"]],
                         ["job_id 重复", "缺 job_id"])
        self.assertFalse(out["all_complete"])

    def test_report_key_order_matches_resume_chain(self):
        self._write("jobs_pending_part1.json", [{"job_id": "J1"}])
        self._write("jobs_done_part1.json", [GOOD_ROW])
        out = self._merge()
        self.assertEqual(list(out.keys()),
                         ["merged", "batches", "missing_batches", "bad_batches",
                          "dropped_rows", "normalized", "normalizations",
                          "observations", "all_complete"])


class TestDoneIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ja_integ_")
        self.old_outdir = ja.OUTDIR
        ja.OUTDIR = self.tmp

    def tearDown(self):
        ja.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def test_all_complete(self):
        self._write("jobs_pending_part1.json", [{"job_id": "a"}, {"job_id": "b"}])
        self._write("jobs_done_part1.json", [{"job_id": "a"}, {"job_id": "b"}])
        r = ja.done_integrity()
        self.assertEqual(r, {"batches": 1, "missing_batches": [], "bad_batches": [],
                             "all_complete": True})

    def test_missing_batch_detected(self):
        self._write("jobs_pending_part1.json", [{"job_id": "a"}])
        self._write("jobs_pending_part2.json", [{"job_id": "b"}])
        self._write("jobs_done_part1.json", [{"job_id": "a"}])
        r = ja.done_integrity()
        self.assertEqual(r["missing_batches"], [2])
        self.assertFalse(r["all_complete"])

    def test_id_mismatch_flagged_as_bad(self):
        self._write("jobs_pending_part1.json", [{"job_id": "a"}, {"job_id": "b"}])
        self._write("jobs_done_part1.json", [{"job_id": "a"}, {"job_id": "x"}])
        r = ja.done_integrity()
        self.assertEqual(r["bad_batches"], [1])
        self.assertFalse(r["all_complete"])

    def test_unparsable_done_counted_missing(self):
        self._write("jobs_pending_part1.json", [{"job_id": "a"}])
        with open(os.path.join(self.tmp, "jobs_done_part1.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        r = ja.done_integrity()
        self.assertEqual(r["missing_batches"], [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
