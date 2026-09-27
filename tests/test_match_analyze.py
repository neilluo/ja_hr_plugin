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


if __name__ == "__main__":
    unittest.main()
