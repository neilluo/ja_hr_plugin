# -*- coding: utf-8 -*-
"""shared/vocab.py 冻结契约回归：toks 分词、SKILL_WORDS 并集、长词优先排序、SEP 双源收敛。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import vocab  # noqa: E402
from vocab import SEP, SKILL_WORDS, SKILL_WORDS_SORTED, toks  # noqa: E402
import semantic_score  # noqa: E402


class TestToks(unittest.TestCase):
    def test_list_form(self):
        self.assertEqual(toks(["CAD", "PLC"]), ["CAD", "PLC"])

    def test_list_strips_and_drops_empty(self):
        self.assertEqual(toks([" a ", "", "  ", "b"]), ["a", "b"])
        # 实际语义：list 分支对元素做 str(x).strip()，None 会被字符串化为 "None" 保留
        self.assertEqual(toks(["a", None]), ["a", "None"])

    def test_string_all_five_separators(self):
        # 顿号 / 英文逗号 / 中文逗号 / 英文分号 / 中文分号 / 斜杠
        self.assertEqual(toks("甲、乙,丙，丁;戊；己/庚"),
                         ["甲", "乙", "丙", "丁", "戊", "己", "庚"])

    def test_string_strips_whitespace(self):
        self.assertEqual(toks(" a 、 b 、\tc\n"), ["a", "b", "c"])

    def test_none_and_empty(self):
        self.assertEqual(toks(None), [])
        self.assertEqual(toks(""), [])

    def test_only_separators(self):
        self.assertEqual(toks("、，;；/"), [])


class TestSkillWords(unittest.TestCase):
    def test_67_unique_words(self):
        self.assertEqual(len(SKILL_WORDS), 67)
        self.assertEqual(len(set(SKILL_WORDS)), 67)

    def test_sorted_is_dedup_of_words(self):
        self.assertEqual(set(SKILL_WORDS_SORTED), set(SKILL_WORDS))
        self.assertEqual(len(SKILL_WORDS_SORTED), len(set(SKILL_WORDS)))

    def test_sorted_long_word_first(self):
        # 全局性质：长度非递增（长词优先，避免短词抢先命中）
        lens = [len(w) for w in SKILL_WORDS_SORTED]
        self.assertEqual(lens, sorted(lens, reverse=True))
        # 若词表中存在「短词是长词子串」的对，长词必须排在前面
        # （当前词表无此类子串对，如「设备」单独不在词表中，故不强制 checked>0）
        checked = 0
        for i, a in enumerate(SKILL_WORDS_SORTED):
            for b in SKILL_WORDS_SORTED[i + 1:]:
                if b in a:
                    self.assertLess(SKILL_WORDS_SORTED.index(a),
                                    SKILL_WORDS_SORTED.index(b),
                                    "%s 应排在子串 %s 之前" % (a, b))
                    checked += 1
        # 关键性质直接验证：排序 key 为 (-len, w)，任取相邻对复核长度非增
        for i in range(len(SKILL_WORDS_SORTED) - 1):
            self.assertGreaterEqual(len(SKILL_WORDS_SORTED[i]),
                                    len(SKILL_WORDS_SORTED[i + 1]))


class TestSepSingleSource(unittest.TestCase):
    def test_sep_value(self):
        self.assertEqual(SEP, r"[、,，;；/]\s*")

    def test_sep_same_as_semantic_score(self):
        # 双源已收敛：semantic_score.SEP 由 vocab 转出，必须同值同对象来源
        self.assertEqual(SEP, semantic_score.SEP)

    def test_toks_same_function_object(self):
        self.assertIs(semantic_score.toks, vocab.toks)


if __name__ == "__main__":
    unittest.main()
