# -*- coding: utf-8 -*-
"""match_analyze.py 回归：cmd 白名单已删 link（SystemExit）、PAIRS 与 match_gated.GATE_PAIRS 同源。
不触网：只测白名单分支（Notable() 构造前即 exit）与模块常量。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import match_analyze  # noqa: E402
import match_gated  # noqa: E402


class TestPairsSingleSource(unittest.TestCase):
    def test_pairs_same_path_as_gate_pairs(self):
        # 双源收敛：match_analyze.PAIRS 与 match_gated.GATE_PAIRS 必须是同一路径
        self.assertEqual(match_analyze.PAIRS, match_gated.GATE_PAIRS)

    def test_pairs_under_outputs(self):
        self.assertIn("outputs", match_analyze.PAIRS)


class TestCmdWhitelist(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._argv = sys.argv

    def tearDown(self):
        os.chdir(self._cwd)
        sys.argv = self._argv

    def test_link_subcommand_exits(self):
        # link 子命令已删：不在白名单 → 打印文档并 sys.exit(1)，不构造 Notable、不触网
        sys.argv = ["match_analyze.py", "link"]
        with self.assertRaises(SystemExit) as ctx:
            match_analyze.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_empty_subcommand_exits(self):
        sys.argv = ["match_analyze.py"]
        with self.assertRaises(SystemExit) as ctx:
            match_analyze.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_source_whitelist_has_no_link(self):
        src = open(os.path.join(ROOT, "skills", "match-verify", "scripts",
                                "match_analyze.py"), encoding="utf-8").read()
        # 白名单行本身不含 link 子命令
        wl = [ln for ln in src.splitlines() if '"prepare"' in ln and "cmd not in" in ln]
        self.assertEqual(len(wl), 1)
        self.assertNotIn("link", wl[0])


class TestCapBlocks(unittest.TestCase):
    """单 agent 配对上限：超 MAX_PAIRS_PER_AGENT 的岗位拆多块，块内不超、配对不丢不重。"""

    def _blk(self, jid, n):
        return {"job": {"job_id": jid}, "candidates": [{"name": "c%d" % i} for i in range(n)]}

    def test_under_cap_untouched(self):
        out = match_analyze._cap_blocks([self._blk("J1", 8)])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["candidates"]), 8)

    def test_over_cap_split(self):
        out = match_analyze._cap_blocks([self._blk("J1", 14)])
        self.assertEqual([len(b["candidates"]) for b in out], [8, 6])
        self.assertTrue(all(b["job"]["job_id"] == "J1" for b in out))

    def test_pairs_preserved(self):
        src = [self._blk("J1", 14), self._blk("J2", 3), self._blk("J3", 0)]
        out = match_analyze._cap_blocks(src)
        names = [c["name"] for b in out for c in b["candidates"]]
        self.assertEqual(sorted(names), sorted(c["name"] for b in src for c in b["candidates"]))
        self.assertTrue(all(len(b["candidates"]) <= match_analyze.MAX_PAIRS_PER_AGENT for b in out))
        self.assertEqual([b["job"]["job_id"] for b in out], ["J1", "J1", "J2"])  # 空候选块丢弃


class TestMatchObservations(unittest.TestCase):
    """match 链 L2 软观察：只报、绝不阻断（不截断/不丢行/不改行内容）。

    2026-09-29 事故回归：evidence 80 字上限曾由 subagent 自写 assert 执行导致 9/11 批返工；
    现长度降级为观察，阈值唯一真源 shared/soften.EV_LEN_MAX / AI_ANALYSIS_RANGE。"""

    def _row(self, **kw):
        r = {"name": "张三", "job_id": "J1", "keep": True, "skill_score": 50,
             "bonus_score": 10, "total_score": 60, "recommend": "待定",
             "evidence": "语义匹配：必备5/8；加分1/3", "ai_analysis": "结" * 160}
        r.update(kw)
        return r

    def test_clean_rows_no_observations(self):
        self.assertEqual(match_analyze.match_observations(
            [self._row()], {("J1", "张三")}), {})

    def test_overlong_evidence_observed_not_truncated(self):
        import soften
        ev = "语义匹配：必备8/8（" + "、".join(
            "命中项%02d" % i for i in range(soften.EV_LEN_MAX)) + "）"   # 长度随真源阈值撑过
        self.assertGreater(len(ev), soften.EV_LEN_MAX)   # 夹具长度从真源派生，不抄字面量
        r = self._row(evidence=ev)
        before = r["evidence"]
        obs = match_analyze.match_observations([r], {("J1", "张三")})
        self.assertEqual(obs["overlong_evidence"], [["J1/张三", len(before)]])
        self.assertEqual(r["evidence"], before)   # 行内容原样，不截断

    def test_overlong_and_thin_analysis(self):
        obs = match_analyze.match_observations(
            [self._row(ai_analysis="结" * 300), self._row(name="李四", ai_analysis="短")],
            {("J1", "张三"), ("J1", "李四")})
        self.assertEqual(obs["overlong_analysis"], [["J1/张三", 300]])
        self.assertEqual(obs["thin_analysis"], [["J1/李四", 1]])

    def test_missing_fields_on_keep(self):
        # v2 契约：分数/evidence 由 merge 代码重算，agent 不供分 → skill_score 缺失检查已删；
        # missing_fields 现在只报 keep=true 却缺 ai_analysis 的情形
        obs = match_analyze.match_observations(
            [self._row(ai_analysis=""), self._row(name="李四", skill_score=None)],
            {("J1", "张三"), ("J1", "李四")})
        self.assertIn(["J1/张三", "ai_analysis"], obs["missing_fields"])
        self.assertNotIn(["J1/李四", "skill_score"], obs.get("missing_fields", []))

    def test_missing_keep_and_missing_pairs(self):
        nokeep = self._row(name="漏keep")
        del nokeep["keep"]
        obs = match_analyze.match_observations(
            [nokeep], {("J1", "漏keep"), ("J1", "没产出")})
        self.assertEqual(obs["missing_keep"], ["J1/漏keep"])
        self.assertEqual(obs["missing_pairs"], [["J1", "没产出"]])

    def test_drop_rows_exempt_from_field_checks(self):
        # keep=false 的条目不要求三字段/ai_analysis（prompt 只要求 keep=true 齐全）
        obs = match_analyze.match_observations(
            [self._row(keep=False, ai_analysis="", skill_score=None)], {("J1", "张三")})
        self.assertEqual(obs, {})

    def test_thresholds_single_source_in_soften(self):
        import soften
        self.assertEqual(soften.EV_LEN_MAX, 200)   # v2 代码组装 evidence 后重标定（soften 注释载理由）
        self.assertEqual(soften.AI_ANALYSIS_RANGE, (150, 250))


class TestPromptV2Contract(unittest.TestCase):
    """prompt v2 契约（2026-09-29 事故重构）：agent 不再算分、不再自检；读写路径硬绑定；
    长度是软偏好、禁止为长度自检返工（事故根因防复活）。"""

    PROMPT = os.path.join(ROOT, "skills", "match-verify", "references",
                          "match-subagent-prompt.md")

    def setUp(self):
        with open(self.PROMPT, encoding="utf-8") as f:
            self.src = f.read()

    def test_lengths_declared_soft_only(self):
        self.assertIn("软偏好、无机器拒收", self.src)
        self.assertIn("不要为字数自检或改写重试", self.src)

    def test_paths_are_hard_injected_placeholders(self):
        # 读写路径经 render_prompts 硬注入本批（<BATCH_PATH>/<DONE_PATH>/<N>），
        # 不再是"文件名由输入自派生"（旧表述让 agent 自己拼 part 号 → 跨批误写事故）
        self.assertIn("<BATCH_PATH>", self.src)
        self.assertIn("<DONE_PATH>", self.src)
        self.assertNotIn("由输入自派生", self.src)
        self.assertNotIn("把 pending 换成 done", self.src)

    def test_agent_side_formula_and_mirror_removed(self):
        # 删掉 §2 打分公式与 80/60 阈值镜像段（agent 不算分，镜像双源随之消失）
        self.assertNotIn("round(100", self.src)
        self.assertNotIn("REC_MIN", self.src)
        self.assertNotIn(">=80", self.src)

    def test_agent_side_selfcheck_removed(self):
        # 删掉 agent 侧 py 自检段（完整性校验在 merge）
        self.assertNotIn("只检完整性", self.src)
        self.assertNotIn("py -X utf8", self.src)
        self.assertNotIn("自检 JSON 可解析", self.src)

    def test_hard_boundary_present(self):
        self.assertIn("硬边界", self.src)
        self.assertIn("禁止删除任何文件", self.src)

    def test_new_output_schema_fields(self):
        for field in ("keep_reason", "grants", '"side"', "baseline"):
            self.assertIn(field, self.src)


if __name__ == "__main__":
    unittest.main()
