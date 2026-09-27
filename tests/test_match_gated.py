# -*- coding: utf-8 -*-
"""match_gated.py 纯函数回归：score 阈值边界 / evidence 格式 / gate 一票否决 / config 同源。
不触网：只调 score/gate 纯函数与模块级常量。"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "match-verify", "scripts"))

import match_gated  # noqa: E402
from match_gated import (GATE_PAIRS, GATE_PENDING, PEND_LABEL, PEND_MIN,  # noqa: E402
                         REC_LABEL, REC_MIN, REJ_LABEL, gate, score)

_CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))


def _skills(n):
    """定宽命名，避免互为子串导致 hit() 字面包含误命中。"""
    return ["s%04d" % i for i in range(n)]


def _jf(must, bonus=(), mw=1.0):
    # bonus_weight 留 0.0 会因 `or 0.3` 回落，故 bonus 为空时权重不影响 bo=0
    return {"must_skills": list(must), "bonus_skills": list(bonus),
            "must_weight": mw, "bonus_weight": 0.3}


class TestScoreThresholds(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(REC_MIN, 80)
        self.assertEqual(PEND_MIN, 60)

    def test_total_80_recommend(self):
        must = _skills(5)
        sk, bo, tot, rec, ev = score({"skills": must[:4]}, _jf(must))
        self.assertEqual((sk, bo, tot), (80, 0, 80))
        self.assertEqual(rec, REC_LABEL)

    def test_total_79_pending(self):
        must = _skills(100)
        sk, bo, tot, rec, ev = score({"skills": must[:79]}, _jf(must))
        self.assertEqual(tot, 79)
        self.assertEqual(rec, PEND_LABEL)

    def test_total_60_pending(self):
        must = _skills(5)
        sk, bo, tot, rec, ev = score({"skills": must[:3]}, _jf(must))
        self.assertEqual(tot, 60)
        self.assertEqual(rec, PEND_LABEL)

    def test_total_59_reject(self):
        must = _skills(100)
        sk, bo, tot, rec, ev = score({"skills": must[:59]}, _jf(must))
        self.assertEqual(tot, 59)
        self.assertEqual(rec, REJ_LABEL)

    def test_default_weights_split(self):
        # 缺权重时回落 0.7/0.3：must 全中 + bonus 全中 → 70+30=100
        must, bonus = _skills(4), ["b%04d" % i for i in range(2)]
        jf = {"must_skills": must, "bonus_skills": bonus}
        sk, bo, tot, rec, ev = score({"skills": must + bonus}, jf)
        self.assertEqual((sk, bo, tot), (70, 30, 100))
        self.assertEqual(rec, REC_LABEL)

    def test_evidence_format(self):
        must = _skills(4)
        sk, bo, tot, rec, ev = score({"skills": must[:2]}, _jf(must))
        self.assertIn("语义匹配：必备2/4", ev)
        self.assertIn("加分0/0", ev)
        self.assertIn("未命中：", ev)

    def test_semantic_hit_counts(self):
        # 命中判定走 semantic_score.hit：同义/上下位也算命中
        jf = _jf(["成本管控", "光伏设备"])
        sk, bo, tot, rec, ev = score({"skills": ["成本控制", "单晶炉"]}, jf)
        self.assertIn("必备2/2", ev)
        self.assertEqual(tot, 100)


class TestGate(unittest.TestCase):
    def _cand(self, **kw):
        c = {"org": None, "education": "", "years": 0, "certificates": "",
             "skills": [], "major": "", "age": None}
        c.update(kw)
        return c

    def test_pass_no_gates(self):
        ok, why, need_major = gate(self._cand(), {"hard_gates": ""}, [])
        self.assertTrue(ok)
        self.assertEqual(why, [])
        self.assertFalse(need_major)

    def test_education_veto(self):
        jf = {"hard_gates": "学历：本科及以上；经验：不作要求"}
        ok, why, _ = gate(self._cand(education="大专"), jf, [])
        self.assertFalse(ok)
        self.assertTrue(any("学历" in w for w in why))

    def test_education_pass(self):
        jf = {"hard_gates": "学历：本科及以上"}
        ok, why, _ = gate(self._cand(education="本科"), jf, [])
        self.assertTrue(ok)
        self.assertEqual(why, [])

    def test_years_veto(self):
        jf = {"hard_gates": "经验：5年及以上"}
        ok, why, _ = gate(self._cand(years=3), jf, [])
        self.assertFalse(ok)
        self.assertTrue(any("经验" in w for w in why))

    def test_years_fresh_grad_exempt(self):
        jf = {"hard_gates": "经验：应届可"}
        ok, why, _ = gate(self._cand(years=0), jf, [])
        self.assertTrue(ok)

    def test_cert_veto(self):
        jf = {"hard_gates": "证书：高压电工证"}
        ok, why, _ = gate(self._cand(certificates="", skills=[]), jf, [])
        self.assertFalse(ok)
        self.assertTrue(any("证书缺证据" in w for w in why))

    def test_cert_in_skills_passes(self):
        jf = {"hard_gates": "证书：高压电工证"}
        ok, why, _ = gate(self._cand(skills=["高压电工证"]), jf, ["高压电工证"])
        self.assertTrue(ok)

    def test_org_mismatch_veto(self):
        jf = {"org": "甲公司", "hard_gates": ""}
        ok, why, _ = gate(self._cand(org="乙公司"), jf, [])
        self.assertFalse(ok)
        self.assertIn("组织不一致", why)

    def test_major_needs_manual(self):
        # 专业判不动 → 不机械否决，但第三元置 True 交人工
        jf = {"hard_gates": "专业：冶金工程"}
        ok, why, need_major = gate(self._cand(major=""), jf, [])
        self.assertTrue(ok)
        self.assertTrue(need_major)

    def test_major_auto_pass(self):
        jf = {"hard_gates": "专业：暖通相关"}
        ok, why, need_major = gate(self._cand(), jf, ["暖通"])
        self.assertTrue(ok)
        self.assertFalse(need_major)


class TestConfigSingleSource(unittest.TestCase):
    def test_labels_from_config(self):
        rec = _CONFIG["options"]["match"]["recommend"]
        self.assertEqual((REC_LABEL, PEND_LABEL, REJ_LABEL), tuple(rec[:3]))

    def test_gate_output_paths_under_outputs(self):
        self.assertIn("outputs", GATE_PAIRS)
        self.assertIn("outputs", GATE_PENDING)
        self.assertTrue(GATE_PAIRS.endswith("gate_pairs.json"))
        self.assertTrue(GATE_PENDING.endswith("gate_pending.json"))
        self.assertEqual(os.path.dirname(GATE_PAIRS), os.path.dirname(GATE_PENDING))

    def test_level_keys_match_config_education(self):
        for k in match_gated.LEVEL:
            self.assertIn(k, _CONFIG["options"]["resume"]["education"])


if __name__ == "__main__":
    unittest.main()
