# -*- coding: utf-8 -*-
"""semantic_score.py 冻结契约回归：SYNONYM 双向、HYPERS 单向、字面包含、txt/toks。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import vocab  # noqa: E402
from semantic_score import HYPERS, SYNONYM, hit, toks, txt  # noqa: E402


class TestHitSynonym(unittest.TestCase):
    def test_bidirectional(self):
        self.assertTrue(hit(["成本控制"], "成本管控"))
        self.assertTrue(hit(["成本管控"], "成本控制"))

    def test_another_group_bidirectional(self):
        self.assertTrue(hit(["良率提升"], "良率管控"))
        self.assertTrue(hit(["良率管控"], "良率提升"))

    def test_unrelated_false(self):
        self.assertFalse(hit(["团队管理"], "成本控制"))


class TestHitHypers(unittest.TestCase):
    def test_specific_satisfies_generic(self):
        # 候选人具体项可满足岗位宽泛项（单向）
        self.assertTrue(hit(["单晶炉"], "光伏设备"))
        self.assertTrue(hit(["切片机"], "光伏设备"))

    def test_reverse_not_allowed(self):
        # 岗位要具体项，候选人只有宽泛项不算命中
        self.assertFalse(hit(["光伏设备"], "单晶炉"))

    def test_different_subsystem_not_interchangeable(self):
        # 排风系统 不在 HYPERS["空调系统"]，也不是同义/包含
        self.assertNotIn("排风系统", HYPERS["空调系统"])
        self.assertFalse(hit(["排风系统"], "空调系统"))
        self.assertFalse(hit(["空调系统"], "排风系统"))
        # 暖通 ∈ HYPERS["空调系统"]（词典实际收录，双向可命中）
        self.assertTrue(hit(["暖通"], "空调系统"))


class TestHitLiteral(unittest.TestCase):
    def test_substring_both_directions(self):
        # 代码语义：need in c 或 c in need 均算字面命中
        self.assertTrue(hit(["设备"], "设备管理"))
        self.assertTrue(hit(["设备管理"], "设备"))

    def test_exact_equal(self):
        self.assertTrue(hit(["PLC"], "PLC"))

    def test_empty_candidates(self):
        self.assertFalse(hit([], "PLC"))


class TestTxt(unittest.TestCase):
    # 契约（config 类型已对齐真表全 text，无 richText 列，读回即字符串）：
    # txt = None→""，其余 str(v)
    def test_str_passthrough(self):
        self.assertEqual(txt("abc"), "abc")

    def test_none_to_empty(self):
        self.assertEqual(txt(None), "")

    def test_list_joined_by_str(self):
        self.assertEqual(txt(["a", "b"]), "['a', 'b']")

    def test_number_to_str(self):
        self.assertEqual(txt(3.5), "3.5")


class TestToks(unittest.TestCase):
    def test_same_as_vocab_toks(self):
        self.assertIs(toks, vocab.toks)

    def test_behavior(self):
        self.assertEqual(toks("甲、乙,丙"), ["甲", "乙", "丙"])
        self.assertEqual(toks(["a", " b "]), ["a", "b"])
        self.assertEqual(toks(None), [])


class TestDictStructure(unittest.TestCase):
    def test_synonym_groups_are_lists_of_str(self):
        for g in SYNONYM:
            self.assertIsInstance(g, list)
            self.assertTrue(all(isinstance(w, str) for w in g))

    def test_hypers_values_are_lists(self):
        for k, v in HYPERS.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, list)


if __name__ == "__main__":
    unittest.main()
