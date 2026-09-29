# -*- coding: utf-8 -*-
"""分派发瘦 + L0/观察分层回归。

覆盖三块：
  1. render_prompts：per-batch 提示词渲染，所有占位符必须替换干净；模板不再内嵌 bash 校验；
  2. done_integrity：盘上 done 产物结构体检（缺失/坏批），merge 内联判定；
  3. validate_row / normalize_row：L0 硬门槛（仅"确实无法写回"）+ 自动归一化；
     质量/审美问题只进 soft_observations 非阻断观察（口径唯一真源 shared/soften.py），
     **不再丢行、不打回重析**（约束强度必须匹配违规可逆性，见 soften 模块 docstring）。

防的事故（AGENTS.md 犯错记录）：
  - 主 agent 把 8.6KB 提示词原文内联进每个 Agent 工具调用 → 工具流截断 → 被迫分波；
  - subagent 自写/自运行校验脚本 → 每 agent 多 1-3 回合、拉长 stall 暴露窗；
  - 7 字中文术语触发旧字数硬门槛 → 整行读图产物被丢、队列卡死 ~11 小时并阻塞匹配门禁。
现渲染后每批只发路径指针；L0 判定唯一真源在 merge。纯本地文件操作，不触网。
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
import soften                            # noqa: E402
import analyze_parts as sa_ap            # noqa: E402  公共骨架（write_dispatch 唯一真源）


def _row(**ov):
    """合规基线，ov 覆盖单字段以构造样例。"""
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
        paths = sa.render_prompts(2, os.path.join(self.tmp, "job_vocab.json"))
        sa_ap.write_dispatch(self.tmp, "skills", paths)   # 清单落盘归 prepare 侧公共骨架
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
        # 校验脚本已从模板剥离，schema 判定唯一真源在 merge（AGENTS.md 教训）
        with open(sa.render_prompts(1, "/tmp/vocab.json")[0], encoding="utf-8") as f:
            body = f.read()
        for residue in ("assert 'id 集合不一致'", "标签字数违规",
                        "python3 -X utf8 - <<'EOF'"):
            self.assertNotIn(residue, body, "模板仍含旧内嵌校验残留：%s" % residue)


class TestValidateRowL0Only(unittest.TestCase):
    """L0 硬门槛：只报"确实无法写回"的问题；一切质量/审美问题不再是错误。"""

    def test_happy_path(self):
        self.assertEqual(sa.validate_row(sa.normalize_row(_row())[0]), [])

    def test_non_dict_rejected(self):
        self.assertTrue(sa.validate_row("不是dict"))
        self.assertTrue(sa.validate_row(None))

    def test_missing_nullable_fields_are_not_errors(self):
        # 缺的可空校正字段 ≡ null：normalize_row 补 None 后 validate_row 必须放过
        r = {"id": "r1", "skills": [], "ai_structured": "学历背景｜a", "ai_deep": "x"}
        nr, kinds = sa.normalize_row(r)
        self.assertIn("field_defaulted", kinds)
        self.assertEqual(sa.validate_row(nr), [])

    def test_aesthetic_issues_never_rejected(self):
        # 旧硬门槛样例（标签字数/技能数/文本长度/段名）在 L0 一律不报错
        for ov in (
            {"skills": ["热镀铝锌硅钢板", "扫描电子显微镜", "质量管理体系认证"]},
            {"skills": ["只一个"]},
            {"skills": ["标签%d" % i for i in range(13)]},
            {"ai_deep": "长" * (sa.DONE_TEXT_MAX + 1)},
            {"ai_structured": "学历｜x\n工作经验｜y"},          # 段名不全（可归一化修复）
            {"years_experience": True},                        # bool → coerce_int 归 None
            {"name": 42},                                      # 数字 → coerce_str 归 "42"
            {"certificates": ["低压电工证", "特种作业操作证"]},  # list → join_list 归字符串
        ):
            nr, _ = sa.normalize_row(_row(**ov))
            self.assertEqual(sa.validate_row(nr), [], "L0 不应拒绝 %r" % (ov,))


class TestObservationSpecs(unittest.TestCase):
    """软观察：质量问题只进 observations，不影响写回。阈值真源 = shared/soften.py。"""

    def test_over_len_tags_observed_not_rejected(self):
        row = sa.normalize_row(_row(skills=["拉晶", "热镀铝锌硅钢板"]))[0]
        obs = soften.soft_observations(row, sa.observation_specs())
        self.assertEqual(obs.get("over_len_tags"), ["热镀铝锌硅钢板"])

    def test_thin_tags_only_reports_under(self):
        thin = sa.normalize_row(_row(skills=["拉晶", "单晶"]))[0]
        obs = soften.soft_observations(thin, sa.observation_specs())
        self.assertEqual(obs.get("thin_tags"), [2])
        ok = sa.normalize_row(_row())[0]
        self.assertNotIn("thin_tags", soften.soft_observations(ok, sa.observation_specs()))

    def test_oversize_text_observed(self):
        row = sa.normalize_row(_row(ai_deep="长" * (sa.DONE_TEXT_MAX + 1)))[0]
        obs = soften.soft_observations(row, sa.observation_specs())
        self.assertEqual(obs.get("oversize_text"), ["ai_deep"])

    def test_clean_row_has_no_observations(self):
        obs = soften.soft_observations(sa.normalize_row(_row())[0], sa.observation_specs())
        self.assertEqual(obs, {})


class TestMergeEndToEnd(unittest.TestCase):
    """merge 新契约：质量行照常写回（只进 observations）；仅缺 id/id 重复/L0 进 dropped_rows。"""

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

    def test_quality_row_written_back_with_observation(self):
        # 旧行为：skills=["只一个"] 触发技能数/字数门槛被丢；新行为：照常写回 + 观察
        self._write("skills_pending_part1.json", [{"id": "ok"}, {"id": "thin"}])
        self._write("skills_done_part1.json",
                    [_row(id="ok"), _row(id="thin", skills=["只一个"])])
        rep = self._run_merge()
        self.assertEqual(rep["merged"], 2)                          # 两行都落盘
        self.assertEqual(rep["dropped_rows"], [])
        self.assertIn("thin_tags", rep["observations"])
        self.assertEqual(rep["observations"]["thin_tags"], [["thin", 1]])
        self.assertTrue(rep["all_complete"])                        # 观察不影响 all_complete
        with open(os.path.join(self.tmp, "skills_done.json"), encoding="utf-8") as f:
            done = json.load(f)
        self.assertEqual([r["id"] for r in done], ["ok", "thin"])

    def test_missing_and_duplicate_id_dropped_and_visible(self):
        # 旧行为：缺 id/重复 id 静默跳过（报告盲点）；新行为：进 dropped_rows 可见
        self._write("skills_pending_part1.json", [{"id": "a"}, {"id": "a"}])
        self._write("skills_done_part1.json",
                    [_row(id="a"), _row(id="a"), {k: v for k, v in _row().items() if k != "id"}])
        rep = self._run_merge()
        self.assertEqual(rep["merged"], 1)
        self.assertEqual([d["reason"] for d in rep["dropped_rows"]], ["id 重复", "缺 id"])
        self.assertFalse(rep["all_complete"])

    def test_report_key_order_exact(self):
        self._write("skills_pending_part1.json", [{"id": "a"}])
        self._write("skills_done_part1.json", [_row(id="a")])
        rep = self._run_merge()
        self.assertEqual(list(rep.keys()),
                         ["merged", "batches", "missing_batches", "bad_batches",
                          "dropped_rows", "normalized", "normalizations",
                          "observations", "all_complete"])

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
