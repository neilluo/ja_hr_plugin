# -*- coding: utf-8 -*-
"""soften 回归锁：约束强度必须匹配违规的可逆性。

锁定"曾被旧硬门槛拒绝的样例，如今一律接受并写回"：
  - 7 字中文术语标签（热镀铝锌硅钢板/扫描电子显微镜）、1 字标签、21 字符英文标签；
  - 技能数 4 / 13；certificates 数组；years_experience "1年"/1.5/"约4年"/True；
  - ai_structured 尾空行、半角竖线、缺段、多余第 6 段、乱序、数组形态、201/7000 字超长；
  - 六个校正字段整体缺失。
只有 L0（非 dict、缺 id、id 重复、三列全空）才产生 dropped_rows。
三列全空必须在 merge 拦下（进 dropped_rows 可见），不能漏到 skills_apply 的 require_three
——后者不写回不打标，记录会永远留在队列每周期重析（无限循环）。
提示词规定的"无内容可析"正规出口（五段填未提及 + ai_deep 写明）不算三列全空、照常出队。
normalize_segments 对空输入不得凭空补出 5 段"未提及"，也不得造出字面量 "None"。
observations 不影响 all_complete；dropped_rows 非空才 false。normalize_row 幂等。
纯本地文件操作，不触网。OUTDIR 一律重定向临时目录（历史教训：硬编码输出路径会把
活锁漏进真实 outputs/）。
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

SEGS = sa.DONE_STRUCTURED_SEGS
CLEAN_STRUCT = "学历背景｜a\n工作经验｜b\n核心技能｜c\n求职意向｜d\n匹配度评估｜e"


def _row(**ov):
    base = {"id": "r1",
            "skills": ["拉晶", "单晶", "拉棒工艺", "切片", "设备管理"],
            "ai_structured": CLEAN_STRUCT,
            "ai_deep": "亮点：x 风险：y 建议：z",
            "name": None, "major": None, "school": None, "certificates": None,
            "years_experience": 3, "expected_position": None}
    base.update(ov)
    return base


class TestSoftenHelpers(unittest.TestCase):
    """shared/soften.py 公共 API（JD 链将逐字复用，签名不得漂移）。"""

    def test_join_list(self):
        self.assertIsNone(soften.join_list(None))
        self.assertEqual(soften.join_list(" a b "), "a b")
        self.assertEqual(soften.join_list(["低压电工证", "特种作业操作证"]),
                         "低压电工证、特种作业操作证")
        self.assertEqual(soften.join_list(["甲", "", None, "乙"]), "甲、乙")
        self.assertIsNone(soften.join_list(["", None]))
        self.assertEqual(soften.join_list(["x", "y"], "/"), "x/y")
        self.assertEqual(json.loads(soften.join_list({"k": "v"})), {"k": "v"})
        self.assertEqual(soften.join_list(42), "42")

    def test_coerce_int(self):
        self.assertIsNone(soften.coerce_int(None))
        self.assertIsNone(soften.coerce_int(True))     # bool 是 int 子类，历史坑
        self.assertIsNone(soften.coerce_int(False))
        self.assertEqual(soften.coerce_int(3), 3)
        self.assertEqual(soften.coerce_int(1.5), 2)    # 四舍五入
        self.assertEqual(soften.coerce_int("1年"), 1)
        self.assertEqual(soften.coerce_int("约4年"), 4)
        self.assertEqual(soften.coerce_int("3年以上"), 3)
        self.assertIsNone(soften.coerce_int("多年"))
        # 负值兜住：弱依赖后低质量值照写回，年限为负是无意义脏值，不得进表
        self.assertIsNone(soften.coerce_int(-2))
        self.assertIsNone(soften.coerce_int("-3年"))
        self.assertIsNone(soften.coerce_int(["x"]))    # 永不抛异常
        self.assertIsNone(soften.coerce_int(object()))

    def test_coerce_str(self):
        self.assertIsNone(soften.coerce_str(None))
        self.assertIsNone(soften.coerce_str("  "))
        self.assertEqual(soften.coerce_str(" a "), "a")
        self.assertEqual(soften.coerce_str(["x", "y"]), "x、y")
        self.assertEqual(soften.coerce_str(42), "42")

    def test_normalize_segments_rules(self):
        text, kinds = soften.normalize_segments(
            ["学历背景｜a", "工作经验｜b"], SEGS)
        self.assertIn("list_joined", kinds)
        self.assertIn("seg_filled", kinds)             # 缺 3 段自动补"未提及"
        self.assertEqual([l.split("｜")[0] for l in text.split("\n")], SEGS)
        self.assertIn("核心技能｜未提及", text)

        text, kinds = soften.normalize_segments(
            {"学历背景": "a", "求职意向": "d"}, SEGS)
        self.assertIn("dict_flattened", kinds)
        self.assertEqual(text.split("\n")[0], "学历背景｜a")   # 按 seg_names 定序

        text, kinds = soften.normalize_segments("学历｜本科", SEGS)
        self.assertIn("seg_filled", kinds)
        self.assertTrue(text.startswith("学历背景｜本科"))      # 模糊兜底：互为包含

    def test_normalize_segments_idempotent(self):
        messy = "学历背景 | a\n\n工作经验｜b\n额外段｜x"
        once, kinds1 = soften.normalize_segments(messy, SEGS)
        self.assertTrue(kinds1)
        twice, kinds2 = soften.normalize_segments(once, SEGS)
        self.assertEqual((once, twice), (twice, once))
        self.assertEqual(kinds2, [])

    def test_normalize_segments_empty_never_fabricates(self):
        """空输入不得凭空补出 5 段"未提及"，也不得造出字面量 "None"。
        归一化只做机械修复、不做发明：4 缺 1 补该段合理，全空时补满 5 段等于伪造一份
        "什么都没找到"的分析结论（可能只是读图失败），会把空值伪装成有效产物写回。"""
        for v in ("", None, "   \n \n", [], {}, ["", ""]):
            text, kinds = soften.normalize_segments(v, SEGS)
            self.assertEqual(text, "", "空输入 %r 应归一为空串，实得 %r" % (v, text))
            self.assertNotIn("seg_filled", kinds)
            self.assertNotIn("未提及", text)
        # None 尤其不得变成字面量 "None" 混进正文
        self.assertNotIn("None", soften.normalize_segments(None, SEGS)[0])
        # 空串是不动点：二次调用 kinds 必须为空（幂等契约）
        self.assertEqual(soften.normalize_segments("", SEGS)[1], [])

    def test_over_len_words_and_count(self):
        # 默认（jobs 链语义）：纯英文超长仍报观察
        self.assertEqual(soften.over_len_words(["拉晶", "热镀铝锌硅钢板",
                                                "Continuous Plating Line"]),
                         ["热镀铝锌硅钢板", "Continuous Plating Line"])
        self.assertEqual(soften.over_len_words(["PLC", "Kubernetes"]), [])
        # en_exempt（skills 链语义，prompt 声明纯英文无字符数上限）：纯英文一律不报
        self.assertEqual(soften.over_len_words(["拉晶", "热镀铝锌硅钢板",
                                                "Continuous Plating Line"], en_exempt=True),
                         ["热镀铝锌硅钢板"])
        self.assertEqual(soften.over_len_words(["Continuous Plating Line"], en_exempt=True), [])
        self.assertEqual(soften.count_out_of_range(["a"] * 5, 5, 12), [])
        self.assertEqual(soften.count_out_of_range(["a"] * 4, 5, 12), [4])

    def test_soft_observations_never_raises(self):
        specs = tuple(soften.SOFT_SPECS) + ({"key": "boom", "field": "x",
                                             "check": lambda row: 1 / 0},)
        obs = soften.soft_observations({"skills": "拉晶、热镀铝锌硅钢板"}, specs)
        self.assertNotIn("boom", obs)                  # check 抛异常 → 该观察省略
        self.assertEqual(obs["over_len_tags"], ["热镀铝锌硅钢板"])


class TestNormalizeRow(unittest.TestCase):
    def test_former_rejects_now_normalize(self):
        cases = [
            # (覆盖字段, 期望归一结果)
            ({"certificates": ["低压电工证", "特种作业操作证"]},
             lambda r: r["certificates"] == "低压电工证、特种作业操作证"),
            ({"years_experience": "1年"}, lambda r: r["years_experience"] == 1),
            ({"years_experience": 1.5}, lambda r: r["years_experience"] == 2),
            ({"years_experience": "约4年"}, lambda r: r["years_experience"] == 4),
            ({"years_experience": True}, lambda r: r["years_experience"] is None),
            ({"name": 42}, lambda r: r["name"] == "42"),
            ({"skills": "拉晶、单晶"}, lambda r: r["skills"] == ["拉晶", "单晶"]),
        ]
        for ov, expect in cases:
            nr, kinds = sa.normalize_row(_row(**ov))
            self.assertTrue(expect(nr), "归一化未达期望: %r -> %r" % (ov, nr))
            self.assertTrue(kinds)
            self.assertEqual(sa.validate_row(nr), [])   # L0 一律放过

    def test_structured_variants_all_fixed(self):
        # 规范形变体（无多余段）：归一后必须恰好 5 行、段序 == SEGS
        clean_variants = [
            CLEAN_STRUCT + "\n",                        # 尾空行
            CLEAN_STRUCT.replace("｜", "|"),            # 半角竖线
            "\n".join(l for l in CLEAN_STRUCT.split("\n") if not l.startswith("核心技能")),
            "工作经验｜b\n学历背景｜a\n核心技能｜c\n求职意向｜d\n匹配度评估｜e",   # 乱序
            CLEAN_STRUCT.split("\n"),                   # 5 段字符串数组
        ]
        for v in clean_variants:
            nr, _ = sa.normalize_row(_row(ai_structured=v))
            self.assertEqual(sa.validate_row(nr), [], "变体被 L0 拒绝: %r" % (v,))
            self.assertEqual([l.split("｜")[0] for l in nr["ai_structured"].split("\n")], SEGS)
            self.assertNotIn("|", nr["ai_structured"].replace("｜", ""))
        # 缺段必须自动补"未提及"
        nr, kinds = sa.normalize_row(_row(ai_structured=clean_variants[2]))
        self.assertIn("核心技能｜未提及", nr["ai_structured"])
        self.assertIn("seg_filled", kinds)

        # 末尾多余段：并入末段后文本与输入一致 → 是不动点，kinds 应为空（幂等契约）
        nr, kinds = sa.normalize_row(_row(ai_structured=CLEAN_STRUCT + "\n自我评价｜x"))
        self.assertIn("自我评价｜x", nr["ai_structured"])
        self.assertEqual(sa.validate_row(nr), [])
        self.assertEqual(kinds, [])

        # 中段多余段（远离其归属段）：被重排并入上一段 → 触发 seg_merged
        messy = "工作经验｜b\n自我评价｜x\n学历背景｜a\n核心技能｜c\n求职意向｜d\n匹配度评估｜e"
        nr, kinds = sa.normalize_row(_row(ai_structured=messy))
        self.assertIn("自我评价｜x", nr["ai_structured"])
        self.assertIn("seg_merged", kinds)
        self.assertEqual(sa.validate_row(nr), [])
        # 多余段并入的是其上一段（工作经验），未新开第 6 段；输出仍按 SEGS 定序
        seg_heads = [l.split("｜")[0] for l in nr["ai_structured"].split("\n") if "｜" in l]
        self.assertTrue(all(s in SEGS or s == "自我评价" for s in seg_heads))
        self.assertIn("工作经验", nr["ai_structured"])

    def test_correction_fields_entirely_absent(self):
        r = {"id": "r1", "skills": ["拉晶"], "ai_structured": CLEAN_STRUCT, "ai_deep": "x"}
        nr, kinds = sa.normalize_row(r)
        self.assertIn("field_defaulted", kinds)
        self.assertEqual(sa.validate_row(nr), [])
        for f in ("name", "major", "school", "certificates", "years_experience",
                  "expected_position"):
            self.assertIsNone(nr[f])

    def test_oversize_text_kept(self):
        for n in (201, 7000):
            nr, _ = sa.normalize_row(_row(ai_structured="学历背景｜" + "长" * n))
            self.assertEqual(sa.validate_row(nr), [])
            self.assertGreaterEqual(len(nr["ai_structured"]), n)

    def test_idempotent(self):
        messy = _row(ai_structured="学历背景 | a\n\n工作经验｜b",
                     certificates=["低压电工证"], years_experience="约4年",
                     skills=["热镀铝锌硅钢板", "PLC"])
        once, kinds1 = sa.normalize_row(messy)
        self.assertTrue(kinds1)
        twice, kinds2 = sa.normalize_row(once)
        self.assertEqual(kinds2, [])
        self.assertEqual(once, twice)
        self.assertEqual(twice["id"], "r1")

    def test_input_not_mutated(self):
        row = _row(certificates=["低压电工证"], years_experience="1年")
        snapshot = json.loads(json.dumps(row))
        sa.normalize_row(row)
        self.assertEqual(row, snapshot)

    def test_empty_row_idempotent(self):
        """全空/缺字段行归一化必须幂等：二次调用 kinds 为空。
        曾回归：ai_deep 由 None 兜成 "" 后与原始 None 比较不等，每轮都记一次 coerced_str。"""
        for row in ({"id": "e1", "skills": [], "ai_structured": "", "ai_deep": ""},
                    {"id": "e2", "skills": None, "ai_structured": None, "ai_deep": None},
                    {"id": "e3"}):
            once, kinds1 = sa.normalize_row(row)
            twice, kinds2 = sa.normalize_row(once)
            self.assertEqual(kinds2, [], "%r 二次归一化仍报修复 %s" % (row["id"], kinds2))
            self.assertEqual(once, twice)
            # 三列产物恒为 str（skills_apply 写回侧直接取用，非 str 会在 PUT 才炸）
            self.assertIsInstance(once["ai_structured"], str)
            self.assertIsInstance(once["ai_deep"], str)


class TestMergeAcceptance(unittest.TestCase):
    """端到端：曾被拒的样例经 merge 全部写回；仅 L0 进 dropped_rows。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="soft_merge_")
        self.old_outdir = sa.OUTDIR
        sa.OUTDIR = self.tmp

    def tearDown(self):
        sa.OUTDIR = self.old_outdir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_merge(self, done_rows, pending=None):
        pending = pending if pending is not None else [{"id": r["id"]} for r in done_rows
                                                       if isinstance(r, dict) and r.get("id")]
        with open(os.path.join(self.tmp, "skills_pending_part1.json"), "w",
                  encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False)
        with open(os.path.join(self.tmp, "skills_done_part1.json"), "w", encoding="utf-8") as f:
            json.dump(done_rows, f, ensure_ascii=False)
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["skills_analyze.py", "merge"]
        try:
            with contextlib.redirect_stdout(buf):
                sa.main()
        finally:
            sys.argv = old_argv
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_empty_row_dropped_at_merge_not_at_apply(self):
        """三列全空的行必须在 merge 就进 dropped_rows（可见），不能漏到 skills_apply 的
        require_three 才进 bad——后者不写回、不打 ai_refined_at，记录会永远留在精析队列
        每周期重析（无限循环），且报告只有 merged 计数看不出少了谁。"""
        rep = self._run_merge([{"id": "empty1", "skills": [], "ai_structured": "",
                                "ai_deep": ""}])
        self.assertEqual(rep["merged"], 0)
        self.assertEqual([d["id"] for d in rep["dropped_rows"]], ["empty1"])
        self.assertIn("三列全空", rep["dropped_rows"][0]["reason"])
        self.assertFalse(rep["all_complete"])
        with open(os.path.join(self.tmp, "skills_done.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_legit_no_content_row_still_written_back(self):
        """提示词规定的"无内容可析"正规出口（五段填未提及 + ai_deep 写明）必须照常写回出队，
        不能被三列全空 L0 误伤——这是扫描件/乱码件的合法终态。"""
        legit = _row(id="ocr_none", skills=[],
                     ai_structured="\n".join("%s｜未提及" % s for s in sa.DONE_STRUCTURED_SEGS),
                     ai_deep="亮点：无 风险：原件与文本均缺失 建议：人工补录")
        rep = self._run_merge([legit])
        self.assertEqual(rep["merged"], 1)
        self.assertEqual(rep["dropped_rows"], [])
        self.assertTrue(rep["all_complete"])

    def test_all_former_rejects_written_back(self):
        rows = [
            _row(id="zh7", skills=["热镀铝锌硅钢板", "扫描电子显微镜", "透射电子显微镜",
                                    "质量管理体系认证", "拉晶"]),
            _row(id="zh1", skills=["焊"]),
            _row(id="en21", skills=["Continuous Plating Line", "PLC"]),
            _row(id="few4", skills=["拉晶", "单晶", "切片", "设备管理"]),
            _row(id="many13", skills=["标签%d" % i for i in range(13)]),
            _row(id="certlist", certificates=["低压电工证", "特种作业操作证"]),
            _row(id="ye1", years_experience="1年"),
            _row(id="ye15", years_experience=1.5),
            _row(id="ye4", years_experience="约4年"),
            _row(id="yebool", years_experience=True),
            _row(id="tailnl", ai_structured=CLEAN_STRUCT + "\n"),
            _row(id="ascpipe", ai_structured=CLEAN_STRUCT.replace("｜", "|")),
            _row(id="missseg",
                 ai_structured="\n".join(l for l in CLEAN_STRUCT.split("\n")
                                         if not l.startswith("核心技能"))),
            _row(id="extra6", ai_structured=CLEAN_STRUCT + "\n自我评价｜x"),
            _row(id="shuffled",
                 ai_structured="工作经验｜b\n学历背景｜a\n核心技能｜c\n求职意向｜d\n匹配度评估｜e"),
            _row(id="structlist", ai_structured=CLEAN_STRUCT.split("\n")),
            _row(id="long201", ai_deep="长" * 201),
            _row(id="long7000", ai_structured="学历背景｜" + "长" * 7000),
            {"id": "nocorr", "skills": ["拉晶"], "ai_structured": CLEAN_STRUCT, "ai_deep": "x"},
        ]
        rep = self._run_merge(rows)
        self.assertEqual(rep["dropped_rows"], [])
        self.assertEqual(rep["merged"], len(rows))       # 一行不丢
        self.assertTrue(rep["all_complete"])             # 仅观察存在时 all_complete=True
        with open(os.path.join(self.tmp, "skills_done.json"), encoding="utf-8") as f:
            done = json.load(f)
        by_id = {r["id"]: r for r in done}
        self.assertEqual(by_id["certlist"]["certificates"], "低压电工证、特种作业操作证")
        self.assertEqual(by_id["ye1"]["years_experience"], 1)
        self.assertEqual(by_id["ye15"]["years_experience"], 2)
        self.assertEqual(by_id["ye4"]["years_experience"], 4)
        self.assertIsNone(by_id["yebool"]["years_experience"])
        self.assertEqual([l.split("｜")[0] for l in by_id["missseg"]["ai_structured"].split("\n")],
                         SEGS)
        self.assertIn("自我评价｜x", by_id["extra6"]["ai_structured"])
        # 质量问题只进 observations，照常写回
        self.assertIn("over_len_tags", rep["observations"])
        self.assertEqual([p[0] for p in rep["observations"]["over_len_tags"] if p[1] == "热镀铝锌硅钢板"],
                         ["zh7"])
        self.assertIn("thin_tags", rep["observations"])
        self.assertIn("oversize_text", rep["observations"])

    def test_l0_failures_visible_in_dropped_rows(self):
        no_id = {k: v for k, v in _row().items() if k != "id"}
        rep = self._run_merge([_row(id="a"), "不是dict", no_id],
                              pending=[{"id": "a"}])
        self.assertEqual(rep["merged"], 1)
        self.assertEqual([d["reason"] for d in rep["dropped_rows"]],
                         ["缺 id", "缺 id"])             # 非 dict 与缺 id 同归"缺 id"
        self.assertFalse(rep["all_complete"])

        rep = self._run_merge([_row(id="dup"), _row(id="dup")],
                              pending=[{"id": "dup"}])
        self.assertEqual(rep["merged"], 1)
        self.assertEqual(rep["dropped_rows"], [{"id": "dup", "reason": "id 重复"}])
        self.assertFalse(rep["all_complete"])

    def test_report_keys_and_normalized_counter(self):
        rep = self._run_merge([_row(id="a"),
                               _row(id="b", certificates=["低压电工证"],
                                    ai_structured=CLEAN_STRUCT + "\n")])
        self.assertEqual(list(rep.keys()),
                         ["merged", "batches", "missing_batches", "bad_batches",
                          "dropped_rows", "normalized", "normalizations",
                          "observations", "all_complete"])
        self.assertEqual(rep["merged"], 2)
        self.assertEqual(rep["normalized"], 1)            # 只有 b 被修复
        self.assertGreaterEqual(rep["normalizations"]["list_joined"], 1)
        self.assertGreaterEqual(rep["normalizations"]["blank_lines"], 1)
        self.assertTrue(rep["all_complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
