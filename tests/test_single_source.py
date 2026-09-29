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
sys.path.insert(0, os.path.join(ROOT, "skills", "replicate", "scripts"))

import datefmt  # noqa: E402
import extract  # noqa: E402
import parse_job  # noqa: E402
import parse_resume  # noqa: E402
import refine_loop  # noqa: E402
import upload_jobs  # noqa: E402
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

    def test_preflight_uses_extract_supported(self):
        # preflight 曾自持一份 _SUPPORTED_RESUME_EXTS 且已漂移（少 .webp/.tif/.txt/.md 等），
        # 目录里只有 .webp 简历时被误拦。现改为 import 同一对象，副本禁止复活。
        sys.path.insert(0, os.path.join(ROOT, "shared", "preflight"))
        import preflight
        self.assertIs(preflight._SUPPORTED_EXTS, extract.SUPPORTED_EXTS)
        self.assertFalse(hasattr(preflight, "_SUPPORTED_RESUME_EXTS"),
                         "漂移的第二份扩展名清单已删，禁止复活（不变量 10）")

    def test_supported_exts_content(self):
        self.assertEqual(extract.SUPPORTED_EXTS, extract.DOC_EXTS | extract.IMG_EXTS)
        for e in (".webp", ".tif", ".bmp", ".png", ".jpg"):
            self.assertIn(e, extract.SUPPORTED_EXTS)
        for e in (".pdf", ".doc", ".docx", ".txt", ".md"):
            self.assertIn(e, extract.SUPPORTED_EXTS)


class TestRefineIntervalSingleSource(unittest.TestCase):
    """不变量 10/11：精析消费任务用 every 型（非 at 型绝对时刻），间隔唯一真源 =
    shared/refine_loop.py 的 EVERY_MS，两条上传链（upload_resumes/upload_jobs）禁止本地副本。
    every 型注册永不过期，结构性消除 at 型"时刻必须在未来"的拒收（见 AGENTS 犯错记录）。"""

    def test_constant_defined_only_in_refine_loop(self):
        # every 型间隔必须 > 0 且是 60s 的整毫秒倍数（人话表述按分钟取整）；
        # at 型的 REFINE_DELAY_S / fire_at 已整体删除，禁止复活（绝对时刻 = 过期失败模式根源）。
        self.assertGreater(refine_loop.EVERY_MS, 0)
        self.assertEqual(refine_loop.EVERY_MS % 1000, 0)
        self.assertFalse(hasattr(refine_loop, "REFINE_DELAY_S"), "at 型延迟常量已删，禁止复活")
        self.assertFalse(hasattr(refine_loop, "fire_at"), "at 型时刻函数已删，禁止复活")
        self.assertFalse(hasattr(refine_loop, "delay_human"), "已改名 every_human，旧名禁止复活")
        for mod in (upload_resumes, upload_jobs):
            self.assertFalse(hasattr(mod, "EVERY_MS"))
            self.assertFalse(hasattr(mod, "REFINE_DELAY_S"))
            self.assertFalse(hasattr(mod, "fire_at"))

    def test_upload_scripts_have_no_local_copy(self):
        # grep 式断言：上传脚本源码不得再出现间隔常量定义或本地时刻函数（防副本复活）；
        # docstring 里以"指向 shared/refine_loop.py"形式引用常量名属指针、不算副本。
        for parts in (("skills", "resume-intake", "scripts", "upload_resumes.py"),
                      ("skills", "job-intake", "scripts", "upload_jobs.py")):
            src = open(os.path.join(ROOT, *parts), encoding="utf-8").read()
            self.assertNotIn("EVERY_MS =", src, "%s 出现间隔常量定义副本" % (parts,))
            self.assertNotIn("def fire_at", src, "%s 出现本地 fire_at 函数副本" % (parts,))
            self.assertNotIn("refine_fire_at", src, "%s 出现已废弃的 at 型注册时刻字段" % (parts,))

    def test_task_spec_is_code_product(self):
        """消费/兜底任务规格是代码产物：任务名前缀、schedule、payload 均从 refine_loop 派生，
        禁止 agent 手写 payload 或手抄前缀（曾一次手写 payload 吃掉 25s 模型思考）。
        consume_task_spec 不再接受 at 参数——every 型无绝对时刻。"""
        import report
        spec = refine_loop.consume_task_spec("resume", root="/tmp/repo")
        self.assertTrue(spec["name"].startswith(refine_loop.TASK_PREFIX["resume"]))
        # 关键断言：schedule 是 every 型，无 at 字段（注册永不过期的根源）
        self.assertEqual(spec["schedule"], {"kind": "every", "everyMs": refine_loop.EVERY_MS})
        self.assertNotIn("at", spec["schedule"])
        self.assertEqual(spec["payload"]["contextDirs"], ["/tmp/repo"])
        # 自删措辞覆盖所有出口（every 型漏删会反复触发）：队列空/refused/正常消费完都要删
        self.assertIn("删除本任务自身", spec["payload"]["message"])
        self.assertIn("refused", spec["payload"]["message"])
        self.assertIn(refine_loop._CHAIN["resume"]["queue_cmd"], spec["payload"]["message"])
        fb = refine_loop.fallback_task_spec(root="/tmp/repo")
        self.assertEqual(fb["name"], refine_loop.FALLBACK_NAME)
        self.assertEqual(fb["schedule"]["kind"], "cron")
        # 兜底自清理的前缀取自 TASK_PREFIX，不是手抄字面量
        for t in refine_loop.TABLES:
            self.assertIn(refine_loop.TASK_PREFIX[t], fb["payload"]["message"])
        # 人话表述与真源一致（汇报里"约 N 分钟内""次日 HH:MM"由此派生）
        self.assertIn(str(refine_loop.EVERY_MS // 60000), report.refine_loop.every_human())
        self.assertRegex(refine_loop.fallback_hhmm(), r"^\d{2}:\d{2}$")


class TestMatchGatedNoLinkResidue(unittest.TestCase):
    def test_source_has_no_link_fields(self):
        src = open(os.path.join(ROOT, "skills", "match-verify", "scripts",
                                "match_gated.py"), encoding="utf-8").read()
        self.assertNotIn("关联岗位", src)
        self.assertNotIn("linkedRecordIds", src)


class TestDateFormatSingleSource(unittest.TestCase):
    """不变量 10：date 列显示格式唯一真源 = config.formats.date，
    建表/补列经 datefmt.property_for 派生，脚本禁止本地抄 formatter 字面量。"""

    def test_declared_matches_config(self):
        self.assertEqual(datefmt.declared(_CONFIG), _CONFIG["formats"]["date"])
        self.assertEqual(datefmt.property_for("resume", "ai_refined_at", _CONFIG),
                         {"formatter": "YYYY-MM-DD HH:mm"})
        self.assertIsNone(datefmt.property_for("perm", "user", _CONFIG))

    def test_build_scripts_have_no_formatter_literal(self):
        # grep 式断言：建表/补列源码不得出现 formatter 字面量（防副本复活），只许经 datefmt 派生
        for parts in (("skills", "replicate", "scripts", "replicate_base.py"),
                      ("skills", "replicate", "scripts", "sync_schema.py")):
            src = open(os.path.join(ROOT, *parts), encoding="utf-8").read()
            self.assertNotIn("formatter", src, "%s 出现 formatter 字面量副本" % (parts,))
            self.assertIn("datefmt.property_for", src)


if __name__ == "__main__":
    unittest.main()
