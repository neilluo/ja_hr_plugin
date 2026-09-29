# -*- coding: utf-8 -*-
"""match 链 v2 渲染契约回归：prepare 落 per-batch 提示词 + prompts 形态分派清单 + 模板去打分公式镜像。

覆盖（TDD 先行，源码尚在旧契约时预期 fail）：
  1. prepare 渲染 outputs/match_prompt_part<N>.md（模板 = references/match-subagent-prompt.md，
     替换 <N> / <BATCH_PATH> / <DONE_PATH>），渲染产物零占位符残留；
  2. 分派清单 match_dispatch.json 含 prompts 键，与磁盘上的 prompt 文件一一对应；
  3. 渲染进提示词的 pending/done 路径必须等于 shared/analyze_parts 的派生值
     （parts 命名唯一真源，防在模板/脚本里手抄文件名）；
  4. 模板不得再镜像打分公式/阈值（"total>=" / "REC_MIN" / "skill_score =" 公式行）：
     分数由 merge 用 match_gated 真源重算，subagent 只报 grants，模板里的公式镜像 = 双源。

纪律：纯本地文件操作，不触网（Notable 被替换为内存 mock）；OUTDIR/PAIRS/FINAL 全部
重定向到 tempfile 目录（隔离手法同 tests/test_refine_lock.py），绝不污染真实 outputs/。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import analyze_parts as ap  # noqa: E402  parts 命名唯一真源
import match_analyze  # noqa: E402

TPL = os.path.join(ROOT, "skills", "match-verify", "references", "match-subagent-prompt.md")
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z_0-9]*>")
# 打分公式镜像行：skill_score = ... / total_score = ... / recommend = ...（JSON 示例里的
# "skill_score":64 带引号与冒号，不会被本式误伤）
FORMULA_RE = re.compile(r"^\s*(skill_score|bonus_score|total_score|recommend)\s*=", re.M)


class FakeNotable:
    """最小内存 mock：只提供 prepare 用到的 list_records（忽略 biz_fields 返回全量）。"""

    def __init__(self, tables):
        self.tables = {k: list(v) for k, v in tables.items()}

    def list_records(self, table, flt=None, biz_fields=None, limit=0):
        return [dict(r) for r in self.tables.get(table, [])]


def _job(jid, must="设备维修、点检管理", bonus=""):
    return {"id": "job_" + jid, "fields": {
        "job_id": jid, "job_name": "设备工程师", "department": "设备部", "org": "制造中心",
        "status": "招聘中", "hard_gates": "", "must_skills": must, "bonus_skills": bonus,
        "must_weight": 1.0, "bonus_weight": 0.0}}


def _resume(rid, name, skills=("设备维修", "点检管理")):
    return {"id": rid, "fields": {
        "name": name, "phone": "13800000000", "education": "本科", "years_experience": 5,
        "certificates": "", "skills": list(skills), "org": "制造中心", "major": "机械",
        "expected_position": "设备工程师"}}


class TestTemplateCarriesNoScoreMirror(unittest.TestCase):
    """模板去公式化：分数/档位由 merge 用 match_gated 真源重算，模板不许再抄一份。"""

    def setUp(self):
        with open(TPL, encoding="utf-8") as f:
            self.tpl = f.read()

    def test_no_threshold_formula_mirror(self):
        self.assertNotIn("total>=", self.tpl.replace(" ", ""),
                         "模板仍镜像推荐阈值公式（真源 match_gated.REC_MIN/PEND_MIN）")
        self.assertNotIn("REC_MIN", self.tpl,
                         "模板不得复述阈值常量名/数值（改阈值只改代码一处）")

    def test_no_score_formula_lines(self):
        self.assertIsNone(FORMULA_RE.search(self.tpl),
                          "模板仍含打分公式行：%s" % FORMULA_RE.findall(self.tpl))

    def test_template_still_exists_and_nonempty(self):
        self.assertTrue(os.path.exists(TPL))
        self.assertGreater(len(self.tpl.strip()), 200)


class _PrepareSandbox(unittest.TestCase):
    """OUTDIR/PAIRS/FINAL 重定向到临时目录 + Notable 换内存 mock（不触网、不写真实 outputs/）。"""

    N_JOBS = 3

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ma_render_")
        self._saved = (match_analyze.OUTDIR, match_analyze.PAIRS, match_analyze.FINAL,
                       match_analyze.Notable)
        match_analyze.OUTDIR = self.tmp
        match_analyze.PAIRS = os.path.join(self.tmp, "gate_pairs.json")
        match_analyze.FINAL = os.path.join(self.tmp, "match_final.json")
        jobs = [_job("J%d" % i) for i in range(1, self.N_JOBS + 1)]
        resumes = [_resume("r%d" % i, "候%d" % i) for i in range(1, self.N_JOBS + 1)]
        pairs = [{"rid": "r%d" % i, "name": "候%d" % i, "job_id": "J%d" % i,
                  "total": 100, "recommend": "推荐"} for i in range(1, self.N_JOBS + 1)]
        with open(match_analyze.PAIRS, "w", encoding="utf-8") as f:
            json.dump(pairs, f, ensure_ascii=False)
        match_analyze.Notable = lambda: FakeNotable({"job": jobs, "resume": resumes})

    def tearDown(self):
        (match_analyze.OUTDIR, match_analyze.PAIRS, match_analyze.FINAL,
         match_analyze.Notable) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prepare(self):
        return match_analyze.prepare(["--batch", "1"])

    def dispatch(self):
        with open(os.path.join(self.tmp, "match_dispatch.json"), encoding="utf-8") as f:
            return json.load(f)


class TestRenderedPrompts(_PrepareSandbox):
    def test_one_prompt_per_batch_placeholders_all_filled(self):
        meta = self.prepare()
        n = meta["batches"]
        self.assertEqual(n, self.N_JOBS)
        for i in range(1, n + 1):
            p = os.path.join(self.tmp, "match_prompt_part%d.md" % i)
            self.assertTrue(os.path.exists(p), "缺 per-batch 提示词：%s" % p)
            with open(p, encoding="utf-8") as f:
                body = f.read()
            for ph in ("<N>", "<BATCH_PATH>", "<DONE_PATH>"):
                self.assertNotIn(ph, body, "part%d 占位符未替换：%s" % (i, ph))
            residue = PLACEHOLDER_RE.findall(body)
            self.assertEqual(residue, [], "part%d 渲染产物残留占位符：%s" % (i, residue))
            # 批号注入正确（不得所有批共用同一个数字）
            self.assertIn(str(i), body)

    def test_dispatch_prompts_match_disk_one_to_one(self):
        meta = self.prepare()
        man = self.dispatch()
        self.assertEqual(meta["dispatch"], os.path.join(self.tmp, "match_dispatch.json"))
        self.assertIn("prompts", man, "match_dispatch.json 必须是 prompts 形态（非 parts）")
        self.assertEqual(man["batches"], meta["batches"])
        self.assertEqual(len(man["prompts"]), meta["batches"])
        on_disk = sorted(x for x in os.listdir(self.tmp) if x.startswith("match_prompt_part"))
        self.assertEqual(sorted(os.path.basename(p) for p in man["prompts"]), on_disk)
        for i, p in enumerate(man["prompts"], 1):
            self.assertTrue(os.path.isabs(p))
            self.assertTrue(os.path.exists(p), "清单指向的提示词不存在：%s" % p)
            self.assertEqual(p, os.path.join(self.tmp, "match_prompt_part%d.md" % i))

    def test_paths_derived_from_analyze_parts(self):
        """提示词里的 pending/done 路径必须等于 analyze_parts 派生值（命名唯一真源，防手抄）。"""
        self.prepare()
        for i in range(1, self.N_JOBS + 1):
            with open(os.path.join(self.tmp, "match_prompt_part%d.md" % i), encoding="utf-8") as f:
                body = f.read()
            self.assertIn(ap.pending_path(self.tmp, match_analyze.PREFIX, i), body)
            self.assertIn(ap.done_path(self.tmp, match_analyze.PREFIX, i), body)
            # done 路径逐字符等于公共骨架派生值（含目录），不允许模板里写死相对文件名
            self.assertIn(os.path.basename(ap.done_path(self.tmp, match_analyze.PREFIX, i)), body)

    def test_rendered_prompt_body_comes_from_template(self):
        """渲染产物 = 模板正文（替换占位符），不是另起一份提示词（防模板被架空成死文件）。"""
        self.prepare()
        with open(TPL, encoding="utf-8") as f:
            tpl = f.read()
        with open(os.path.join(self.tmp, "match_prompt_part1.md"), encoding="utf-8") as f:
            body = f.read()
        # 模板里不含占位符的长句必须原样出现在渲染产物中
        anchors = [ln.strip() for ln in tpl.splitlines()
                   if len(ln.strip()) >= 20 and not PLACEHOLDER_RE.search(ln)]
        self.assertTrue(anchors, "模板缺可用于比对的正文行")
        for ln in anchors[:5]:
            self.assertIn(ln, body, "渲染产物未沿用模板正文：%s" % ln[:40])

    def test_stale_prompts_pruned_on_smaller_run(self):
        """上一轮大批次残留的 prompt 文件必须被清（僵尸文件会让 agent 分派到过期批次）。"""
        self.prepare()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "match_prompt_part%d.md"
                                                   % self.N_JOBS)))
        # 只留一个岗位重跑：批次数收缩后旧 part 文件不得残留
        pairs = [{"rid": "r1", "name": "候1", "job_id": "J1", "total": 100, "recommend": "推荐"}]
        with open(match_analyze.PAIRS, "w", encoding="utf-8") as f:
            json.dump(pairs, f, ensure_ascii=False)
        meta = match_analyze.prepare(["--batch", "1"])
        self.assertEqual(meta["batches"], 1)
        remaining = sorted(x for x in os.listdir(self.tmp) if x.startswith("match_prompt_part"))
        self.assertEqual(remaining, ["match_prompt_part1.md"])
        self.assertEqual(len(self.dispatch()["prompts"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
