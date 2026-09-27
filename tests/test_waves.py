# -*- coding: utf-8 -*-
"""shared/waves.py 冻结契约回归：MAX_AGENTS 硬顶 20、plan 负载均衡分组无遗漏无重叠。"""

import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))

import waves  # noqa: E402


def _flatten(groups):
    return [i for g in groups for i in g]


class TestMaxAgents(unittest.TestCase):
    def test_default_is_20(self):
        self.assertEqual(waves._MAX_AGENTS_CEIL, 20)
        if "MAX_AGENTS" not in os.environ:
            self.assertEqual(waves.MAX_AGENTS, 20)

    def test_env_cannot_raise_above_20(self):
        old = os.environ.get("MAX_AGENTS")
        try:
            os.environ["MAX_AGENTS"] = "50"
            importlib.reload(waves)
            self.assertEqual(waves.MAX_AGENTS, 20)   # min(50, 20) 硬顶
        finally:
            if old is None:
                os.environ.pop("MAX_AGENTS", None)
            else:
                os.environ["MAX_AGENTS"] = old
            importlib.reload(waves)

    def test_env_can_lower(self):
        old = os.environ.get("MAX_AGENTS")
        try:
            os.environ["MAX_AGENTS"] = "5"
            importlib.reload(waves)
            self.assertEqual(waves.MAX_AGENTS, 5)    # 只能下调
        finally:
            if old is None:
                os.environ.pop("MAX_AGENTS", None)
            else:
                os.environ["MAX_AGENTS"] = old
            importlib.reload(waves)
        self.assertEqual(waves.MAX_AGENTS, 20)       # 恢复后回到 20


class TestPlan(unittest.TestCase):
    def test_n26_thirteen_agents_two_each(self):
        batch, groups = waves.plan(26)
        self.assertEqual(batch, 2)
        self.assertEqual(len(groups), 13)
        self.assertTrue(all(len(g) == 2 for g in groups))
        self.assertEqual(_flatten(groups), list(range(1, 27)))

    def test_n20_one_item_per_agent(self):
        batch, groups = waves.plan(20)
        self.assertEqual(batch, 1)
        self.assertEqual(len(groups), 20)
        self.assertEqual(_flatten(groups), list(range(1, 21)))

    def test_n200_capped_at_20_agents(self):
        batch, groups = waves.plan(200)
        self.assertEqual(batch, 10)
        self.assertEqual(len(groups), 20)
        self.assertEqual(_flatten(groups), list(range(1, 201)))

    def test_n_nonpositive_empty_groups(self):
        batch, groups = waves.plan(0)
        self.assertEqual(groups, [])
        self.assertGreaterEqual(batch, 1)
        batch, groups = waves.plan(-3)
        self.assertEqual(groups, [])
        self.assertGreaterEqual(batch, 1)

    def test_no_gap_no_overlap_many_sizes(self):
        for n in (1, 2, 7, 19, 20, 21, 39, 41, 100, 200, 501):
            batch, groups = waves.plan(n)
            self.assertEqual(_flatten(groups), list(range(1, n + 1), ),
                             "n=%d 分组有遗漏或重叠" % n)
            self.assertLessEqual(len(groups), waves.MAX_AGENTS)
            self.assertTrue(all(len(g) == batch for g in groups[:-1]))

    def test_explicit_batch_over_cap_is_enlarged(self):
        # batch=2、100 条 → 50 agent 超限 → 自动加大 batch 压回 20
        batch, groups = waves.plan(100, batch=2)
        self.assertLessEqual(len(groups), 20)
        self.assertEqual(batch, 5)
        self.assertEqual(len(groups), 20)
        self.assertEqual(_flatten(groups), list(range(1, 101)))

    def test_explicit_batch_within_cap_honored(self):
        batch, groups = waves.plan(30, batch=3)
        self.assertEqual(batch, 3)
        self.assertEqual(len(groups), 10)
        self.assertEqual(_flatten(groups), list(range(1, 31)))

    def test_cap_argument_cannot_exceed_20(self):
        batch, groups = waves.plan(100, cap=50)
        self.assertLessEqual(len(groups), 20)


if __name__ == "__main__":
    unittest.main()
