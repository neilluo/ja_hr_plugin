# -*- coding: utf-8 -*-
"""B（行级抢救）回归：shared/analyze_parts.load_done / _top_level_objects / read_done。

防的事故（2026-09-29 实测）：subagent 批次里一条记录多对裸引号/少个逗号，
旧 read_done 把整批判 missing → merge 报 merged=29，掩盖"同批其实有完好记录"，
报告与盘面自相矛盾（抢救行已可用却称整批不存在）。

现口径（补发粒度不变，仍是批次级）：
  - 部分损坏：好行照常并入 merged，批次由 done_integrity 按 id 集合不一致判 bad、照常整批补发；
  - 整批不可解析：仍 missing（绝不比旧版更差）；
  - 抢救只产出"能独立 json.loads 的完整对象"——被杂散引号吞掉闭合花括号的行要么完整、
    要么不出现，绝无"半截合法行静默写回"；
  - read_done 与 done_integrity 同走 load_done（口径单源，禁止"merged 含抢救行、
    体检仍谎报整批缺失"的自相矛盾）。
纪律（AGENTS.md 犯错记录同款教训）：不许用 prepare --ids 做单条增量补发——prepare→write_parts
会清空全部 *_done_part*，其余批次已抢救的好行反而丢光、退化成等次日兜底。
纯本地文件操作，不触网。
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
import analyze_parts as ap  # noqa: E402


class TestSalvage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ap_salvage_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)
        return os.path.join(self.tmp, name)

    def test_clean_batch_untouched(self):
        p = self._w("skills_done_part1.json",
                    json.dumps([{"id": "a", "skills": ["x"]}, {"id": "b", "skills": ["y"]}]))
        rows, errs = ap.load_done(p)
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        self.assertEqual(errs, [])

    def test_trailing_comma_recovers_all(self):
        p = self._w("skills_done_part1.json", '[{"id":"a"},{"id":"b"},]')
        rows, errs = ap.load_done(p)
        self.assertEqual(sorted(r["id"] for r in rows), ["a", "b"])

    def test_one_bad_quote_rescues_good_rows(self):
        # 中间那条含未转义引号（真实 part9 形态）：首尾两条必须救回，坏的只丢它自己
        p = self._w("skills_done_part1.json",
                    '[{"id":"a","skills":["拉晶"],"ai_deep":"好"},'
                    '{"id":"b","skills":["切片"],"ai_deep":"他说"你好"的建议"},'
                    '{"id":"c","skills":["焊接"],"ai_deep":"好"}]')
        rows, errs = ap.load_done(p)
        self.assertEqual(sorted(r["id"] for r in rows), ["a", "c"])
        self.assertEqual([e["id"] for e in errs], ["b"])

    def test_brace_in_string_not_truncated(self):
        # ai_deep 里的 { } 不得被当结构花括号：整行必须完整救回（内容逐字相等）
        text = "含 { 花括号 } 的长文本内容"
        p = self._w("skills_done_part1.json", '[{"id":"a","ai_deep":"%s"}]' % text)
        rows, errs = ap.load_done(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ai_deep"], text)

    def test_swallowed_close_brace_never_yields_half_row(self):
        # 安全网核心性质：坏行吞掉后继闭合花括号时，该记录要么完整出现、要么整个不出现，
        # 绝不产出"合法但缺列"的半截行——merge 的 L0 校验之外还有这层底线。
        p = self._w("skills_done_part1.json",
                    '[{"id":"a"},{"id":"b","ai_deep":"他说"hi"}]')
        rows, _errs = ap.load_done(p)
        ids = [r["id"] for r in rows]
        self.assertIn("a", ids)
        if "b" in ids:
            self.assertEqual([r for r in rows if r["id"] == "b"][0].get("ai_deep"), "hi")
        else:  # b 未救回 = 缺席而非半截
            self.assertTrue(all("v" not in r or r.get("id") != "b" for r in rows))

    def test_total_garbage_is_not_partial(self):
        p = self._w("skills_done_part1.json", "{not json")
        rows, errs = ap.load_done(p)
        self.assertEqual(rows, [])
        self.assertTrue(errs)


class TestReadDoneIntegration(unittest.TestCase):
    """read_done 与 done_integrity 同走 load_done：部分损坏批次好行并入、不报整批缺失，
    补发判定（bad_batches）由消费方 done_integrity 按 id 集合做，此处只取行。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ap_rd_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_partial_damage_not_counted_missing(self):
        self._w("skills_pending_part1.json", '[{"id":"a"},{"id":"b"}]')
        self._w("skills_done_part1.json",
                '[{"id":"a","skills":["x"]},'
                '{"id":"b","skills":["切片"],"ai_deep":"他说"你好"的建议"}]')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows, missing = ap.read_done(self.tmp, "skills")
        self.assertEqual([r["id"] for r in rows], ["a"])   # a 救回
        self.assertEqual(missing, [])                        # 有抢救行 → 不报整批缺失
        self.assertIn("部分损坏", buf.getvalue())

    def test_whole_garbage_still_missing(self):
        self._w("skills_pending_part1.json", '[{"id":"a"}]')
        self._w("skills_done_part1.json", "{broken json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows, missing = ap.read_done(self.tmp, "skills")
        self.assertEqual(rows, [])
        self.assertEqual(missing, [1])
        self.assertIn("解析失败", buf.getvalue())

    def test_absent_batch_still_missing(self):
        self._w("skills_pending_part1.json", '[{"id":"a"}]')
        # done 根本没写（subagent 死在半路）→ 与旧口径一致进 missing
        rows, missing = ap.read_done(self.tmp, "skills")
        self.assertEqual((rows, missing), ([], [1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
