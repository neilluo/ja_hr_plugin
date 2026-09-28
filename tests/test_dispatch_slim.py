# -*- coding: utf-8 -*-
"""分派发瘦 + schema SSOT 下沉回归。

覆盖三块：
  1. render_prompts：per-batch 提示词渲染，所有占位符必须替换干净；模板不再内嵌 bash 校验；
  2. done_integrity：盘上 done 产物结构体检（缺失/坏批），merge 内联判定；
  3. validate_row：done 行 schema 唯一真源（SSOT），merge 用它决定丢弃哪些行、
     违规行不打 ai_refined_at 标记 → 下周期自动重析。

防的事故（AGENTS.md 犯错记录）：
  - 主 agent 把 8.6KB 提示词原文内联进每个 Agent 工具调用 → 工具流截断 → 被迫分波；
  - subagent 自写/自运行校验脚本 → 每 agent 多 1-3 回合、拉长 stall 暴露窗。
现渲染后每批只发路径指针；schema 校验唯一真源在 merge。纯本地文件操作，不触网。
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
sys.path.insert(0, os.path.join(ROOT, "skills", "skills-analyze", "scripts"))

import skills_analyze as sa              # noqa: E402


def _row(**ov):
    """合规基线，ov 覆盖单字段以构造违规样例。"""
    base = {"id": "r1",
            "skills": ["拉晶", "单晶", "拉棒工艺", "切片", "设备管理"],
            "ai_structured": "学历背景｜a\n工作经验｜b\n核心技能｜c\n求职意向｜d\n匹配度评估｜e",
            "ai_deep": "亮点：x 风险：y 建议：z",
            "name": None, "major": None, "school": None, "certificates": None,
            "years_experience": 3, "expected_position": None}
    base.update(ov)
    return base


class TestRenderPrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sa_render_")
        self.old_outdir = sa.OUTDIR
        sa.OUTDIR = self.tmp

    def tearDown(self):
        sa.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_renders_one_file_per_batch_all_placeholders_filled(self):
        vocab = os.path.join(self.tmp, "job_vocab.json")
        paths = sa.render_prompts(3, vocab)
        self.assertEqual(len(paths), 3)
        for i, p in enumerate(paths, 1):
            self.assertTrue(os.path.exists(p))
            self.assertEqual(p, os.path.join(self.tmp, "skills_prompt_part%d.md" % i))
            with open(p, encoding="utf-8") as f:
                body = f.read()
            # 所有占位符均已替换
            for ph in ("<BATCH_PATH>", "<VOCAB_PATH>", "<N>"):
                self.assertNotIn(ph, body, "占位符未替换：%s" % ph)
            # 该批 pending/done 路径按序号自派生注入
            self.assertIn("skills_pending_part%d.json" % i, body)
            self.assertIn("skills_done_part%d.json" % i, body)
            self.assertIn(vocab, body)

    def test_dispatch_index_written(self):
        sa.render_prompts(2, os.path.join(self.tmp, "job_vocab.json"))
        with open(os.path.join(self.tmp, "skills_dispatch.json"), encoding="utf-8") as f:
            idx = json.load(f)
        self.assertEqual(idx["batches"], 2)
        self.assertEqual(len(idx["prompts"]), 2)

    def test_stale_prompt_files_pruned(self):
        sa.render_prompts(4, os.path.join(self.tmp, "job_vocab.json"))
        sa.render_prompts(2, os.path.join(self.tmp, "job_vocab.json"))
        remaining = sorted(x for x in os.listdir(self.tmp) if x.startswith("skills_prompt_part"))
        self.assertEqual(remaining, ["skills_prompt_part1.md", "skills_prompt_part2.md"])

    def test_prompt_no_longer_carries_bash_validator(self):
        # 校验脚本已从模板剥离，schema SSOT 移到 validate_row + merge（AGENTS.md 教训）
        with open(sa.render_prompts(1, "/tmp/vocab.json")[0], encoding="utf-8") as f:
            body = f.read()
        for residue in ("assert 'id 集合不一致'", "标签字数违规",
                        "python3 -X utf8 - <<'EOF'"):
            self.assertNotIn(residue, body, "模板仍含旧内嵌校验残留：%s" % residue)


class TestValidateRow(unittest.TestCase):
    """schema 唯一真源：所有违规类别必须被抓到，所有合法边缘必须放过。"""

    def test_happy_path(self):
        self.assertEqual(sa.validate_row(_row()), [])

    def test_missing_required_field(self):
        r = _row(); r.pop("ai_deep")
        self.assertTrue(any("缺字段" in e for e in sa.validate_row(r)))

    def test_ai_structured_wrong_segment_names(self):
        errs = sa.validate_row(_row(ai_structured="学历｜x\n工作经验｜y\n核心｜z\n求职｜a\n匹配｜b"))
        self.assertTrue(any("段名错" in e for e in errs))

    def test_text_over_length(self):
        self.assertTrue(any("ai_structured" in e for e in sa.validate_row(_row(ai_structured="长" * 201))))
        self.assertTrue(any("ai_deep" in e for e in sa.validate_row(_row(ai_deep="长" * 201))))

    def test_skill_count_out_of_range(self):
        self.assertTrue(any("技能数" in e for e in sa.validate_row(_row(skills=["a", "b", "c", "d"]))))
        self.assertTrue(any("技能数" in e for e in
                            sa.validate_row(_row(skills=["标签%d" % i for i in range(13)]))))

    def test_zh_skill_length(self):
        errs = sa.validate_row(_row(skills=["拉晶", "单晶", "很长很长很长很长标签", "切片", "设备管理"]))
        self.assertTrue(any("中文字数" in e for e in errs))

    def test_years_experience_bool_rejected(self):
        # bool 是 int 子类，历史坑：True/False 不能当年限
        self.assertTrue(any("years_experience" in e for e in sa.validate_row(_row(years_experience=True))))

    def test_nullable_str_field_wrong_type(self):
        self.assertTrue(any("name" in e for e in sa.validate_row(_row(name=42))))

    def test_edge_cases_pass(self):
        # 纯英文/缩写术语不限字数
        self.assertEqual(sa.validate_row(_row(skills=["Kubernetes", "PLC", "MES", "Docker", "拉晶"])), [])
        # skills 空数组（"无内容可析"）合法
        self.assertEqual(sa.validate_row(_row(skills=[])), [])
        # 6 个校正字段全 null 合法
        self.assertEqual(sa.validate_row(_row(name=None, major=None, school=None,
                                              certificates=None, years_experience=None,
                                              expected_position=None)), [])


class TestMergeEndToEnd(unittest.TestCase):
    """merge 消费 validate_row：违规行不写进 skills_done.json、也不进 bad_batches（批次仍算齐），
    但报告 bad_rows + all_complete=false。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sa_merge_")
        self.old_outdir = sa.OUTDIR
        sa.OUTDIR = self.tmp

    def tearDown(self):
        sa.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def test_bad_row_dropped_and_reported(self):
        self._write("skills_pending_part1.json", [{"id": "ok"}, {"id": "bad"}])
        self._write("skills_done_part1.json", [_row(id="ok"), _row(id="bad", skills=["只一个"])])
        rep = self._run_merge()
        self.assertEqual(rep["merged"], 1)                          # 只有 ok 落盘
        self.assertEqual(rep["bad_batches"], [])                    # 批次结构齐
        self.assertEqual([b["id"] for b in rep["bad_rows"]], ["bad"])
        self.assertFalse(rep["all_complete"])
        with open(os.path.join(self.tmp, "skills_done.json"), encoding="utf-8") as f:
            done = json.load(f)
        self.assertEqual([r["id"] for r in done], ["ok"])

    def test_all_good_marks_all_complete(self):
        self._write("skills_pending_part1.json", [{"id": "a"}, {"id": "b"}])
        self._write("skills_done_part1.json", [_row(id="a"), _row(id="b")])
        rep = self._run_merge()
        self.assertTrue(rep["all_complete"])
        self.assertEqual(rep["bad_rows"], [])

    def _run_merge(self):
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["skills_analyze.py", "merge"]
        try:
            with contextlib.redirect_stdout(buf):
                sa.main()
        finally:
            sys.argv = old_argv
        return json.loads(buf.getvalue().strip().splitlines()[-1])


class TestDoneIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sa_integ_")
        self.old_outdir = sa.OUTDIR
        sa.OUTDIR = self.tmp

    def tearDown(self):
        sa.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, rows):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    def test_all_complete(self):
        self._write("skills_pending_part1.json", [{"id": "a"}, {"id": "b"}])
        self._write("skills_done_part1.json", [{"id": "a"}, {"id": "b"}])
        r = sa.done_integrity()
        self.assertEqual(r, {"batches": 1, "missing_batches": [], "bad_batches": [],
                             "all_complete": True})

    def test_missing_batch_detected(self):
        self._write("skills_pending_part1.json", [{"id": "a"}])
        self._write("skills_pending_part2.json", [{"id": "b"}])
        self._write("skills_done_part1.json", [{"id": "a"}])   # part2 done 缺失
        r = sa.done_integrity()
        self.assertEqual(r["missing_batches"], [2])
        self.assertFalse(r["all_complete"])

    def test_id_mismatch_flagged_as_bad(self):
        self._write("skills_pending_part1.json", [{"id": "a"}, {"id": "b"}])
        self._write("skills_done_part1.json", [{"id": "a"}, {"id": "x"}])   # b 漏、x 多出
        r = sa.done_integrity()
        self.assertEqual(r["bad_batches"], [1])
        self.assertFalse(r["all_complete"])

    def test_unparsable_done_counted_missing(self):
        self._write("skills_pending_part1.json", [{"id": "a"}])
        with open(os.path.join(self.tmp, "skills_done_part1.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        r = sa.done_integrity()
        self.assertEqual(r["missing_batches"], [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
