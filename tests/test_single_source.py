# -*- coding: utf-8 -*-
"""双源消除元测试（回归防线）：解析器无本地词表副本、枚举/城市派生自 config、
parse_resume 无 full_text 死值、扩展名清单同源、match_gated 无 link 残留。"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import extract  # noqa: E402
import parse_job  # noqa: E402
import parse_resume  # noqa: E402
import upload_resumes  # noqa: E402
import vocab  # noqa: E402

_CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))


class TestParseJobSingleSource(unittest.TestCase):
    def test_no_local_skill_words(self):
        self.assertFalse(hasattr(parse_job, "_SKILL_WORDS"))

    def test_uses_vocab_sorted(self):
        self.assertIs(parse_job.SKILL_WORDS_SORTED, vocab.SKILL_WORDS_SORTED)

    def test_dept_enum_from_config(self):
        self.assertEqual(parse_job._DEPT_ENUM,
                         tuple(_CONFIG["options"]["job"]["department"]))

    def test_org_and_city_regex_from_config(self):
        for org in _CONFIG["options"]["job"]["org"]:
            self.assertIn(org, parse_job._ORG_RE.pattern)
        for city in _CONFIG["options"]["job"]["work_location"]:
            self.assertIn(city, parse_job._CITY_RE.pattern)


class TestParseResumeSingleSource(unittest.TestCase):
    def test_no_local_skill_words(self):
        self.assertFalse(hasattr(parse_resume, "_SKILL_WORDS"))

    def test_uses_vocab_sorted(self):
        self.assertIs(parse_resume.SKILL_WORDS_SORTED, vocab.SKILL_WORDS_SORTED)

    def test_cities_from_config(self):
        self.assertEqual(set(parse_resume._CITIES),
                         set(_CONFIG["options"]["resume"]["expected_location"]))
        # 长词优先排序
        lens = [len(c) for c in parse_resume._CITIES]
        self.assertEqual(lens, sorted(lens, reverse=True))

    def test_parse_result_has_no_full_text(self):
        out = parse_resume.parse("姓名：张三\n学历：本科\n13800001111\n技能：CAD、PLC",
                                 "张三.pdf")
        self.assertNotIn("full_text", out)
        self.assertEqual(out["name"], "张三")

    def test_categories_validated_against_config(self):
        for cat, _ in parse_resume._CATS:
            self.assertIn(cat, _CONFIG["options"]["resume"]["category"])

    def test_school_lists_from_config_refs(self):
        # 名单唯一真源 = config.refs.resume.school_985/211，本地不留副本（值同源 + 数量兜底）
        self.assertEqual(parse_resume._985,
                         tuple(_CONFIG["refs"]["resume"]["school_985"]))
        self.assertEqual(parse_resume._211,
                         tuple(_CONFIG["refs"]["resume"]["school_211"]))
        self.assertEqual(len(parse_resume._985), 20)
        self.assertEqual(len(parse_resume._211), 20)

    def test_source_has_no_school_name_literals(self):
        # grep 式断言：parse_resume.py 源码不得再现任何校名字面量（防名单副本复活）
        src = open(os.path.join(ROOT, "skills", "resume-intake", "scripts",
                                "parse_resume.py"), encoding="utf-8").read()
        for school in (_CONFIG["refs"]["resume"]["school_985"]
                       + _CONFIG["refs"]["resume"]["school_211"]):
            self.assertNotIn(school, src)

    def test_school_rank_prefers_long_211_name(self):
        # 211 长名优先：西安电子科技大学判 211，不被短名误判；985 命中判 985；普通兜底不变
        self.assertEqual(parse_resume._school_rank("西安电子科技大学", "本科"), "211")
        self.assertEqual(parse_resume._school_rank("清华大学", "硕士"), "985")
        self.assertEqual(parse_resume._school_rank("某某大学", "大专"), "大专")
        self.assertEqual(parse_resume._school_rank("某某大学", "本科"), "普通本科")
        self.assertEqual(parse_resume._school_rank("", ""), "")


class TestExtractExtsSingleSource(unittest.TestCase):
    def test_upload_resumes_uses_extract_supported(self):
        # from extract import SUPPORTED_EXTS —— 必须是同一对象，不是抄的副本
        self.assertIs(upload_resumes.SUPPORTED_EXTS, extract.SUPPORTED_EXTS)

    def test_supported_exts_content(self):
        self.assertEqual(extract.SUPPORTED_EXTS, extract.DOC_EXTS | extract.IMG_EXTS)
        for e in (".webp", ".tif", ".bmp", ".png", ".jpg"):
            self.assertIn(e, extract.SUPPORTED_EXTS)
        for e in (".pdf", ".doc", ".docx", ".txt", ".md"):
            self.assertIn(e, extract.SUPPORTED_EXTS)


class TestMatchGatedNoLinkResidue(unittest.TestCase):
    def test_source_has_no_link_fields(self):
        src = open(os.path.join(ROOT, "skills", "match-verify", "scripts",
                                "match_gated.py"), encoding="utf-8").read()
        self.assertNotIn("关联岗位", src)
        self.assertNotIn("linkedRecordIds", src)


if __name__ == "__main__":
    unittest.main()
