#!/usr/bin/env python3
"""本地无副作用单测：不触网，覆盖类型转换/读回归一/字段映射/解析冒烟。"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

from notable import Notable  # noqa: E402
from parse_job import parse as parse_job  # noqa: E402
from parse_resume import parse as parse_resume  # noqa: E402


class TestCast(unittest.TestCase):
    def setUp(self):
        self.nt = Notable(os.path.join(ROOT, "config.json"))

    def test_number(self):
        self.assertEqual(self.nt._cast(3, "number"), 3.0)
        self.assertEqual(self.nt._cast("2.5", "number"), 2.5)

    def test_date_ms_passthrough(self):
        self.assertEqual(self.nt._cast(1789924509618, "date"), 1789924509618)

    def test_date_seconds_scaled(self):
        self.assertEqual(self.nt._cast(1789924509, "date"), 1789924509000)

    def test_date_string(self):
        self.assertIsInstance(self.nt._cast("2026-09-21", "date"), int)

    def test_multiselect(self):
        self.assertEqual(self.nt._cast("曲靖", "multipleSelect"), ["曲靖"])
        self.assertEqual(self.nt._cast(["a", "b"], "multipleSelect"), ["a", "b"])

    def test_norm_select(self):
        self.assertEqual(self.nt._norm({"id": "x", "name": "本科"}), "本科")
        self.assertEqual(self.nt._norm([{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]), ["a", "b"])
        self.assertEqual(self.nt._norm("plain"), "plain")
        self.assertEqual(self.nt._norm([]), [])

    def test_norm_number_string(self):
        self.assertEqual(self.nt._norm("22", "number"), 22.0)
        self.assertEqual(self.nt._norm("abc", "number"), "abc")
        self.assertEqual(self.nt._norm("22"), "22")  # 无类型信息不动

    def test_cells_uses_cn_names_and_skips_empty(self):
        cells = self.nt._cells("resume", {"name": "张三", "phone": "", "skills": ["CAD"],
                                          "years_experience": None})
        self.assertEqual(cells, {"姓名": "张三", "技能标签": ["CAD"]})

    def test_biz_reverse_map(self):
        self.assertEqual(self.nt._biz("resume", "手机号"), "phone")
        self.assertEqual(self.nt._biz("resume", "不存在的字段"), "不存在的字段")

    def test_cn_unknown_key_raises_readable(self):
        from notable import NotableError
        with self.assertRaises(NotableError) as ctx:
            self.nt.cn("resume", "gender")
        msg = str(ctx.exception)
        self.assertIn("gender", msg)
        self.assertIn("phone", msg)  # 提示里列出可用字段


class TestBackfillValidate(unittest.TestCase):
    """扫描件补录的字段校验：非法业务键被挑出，内部键(_file)与合法键放行。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))
        self.nt = Notable(os.path.join(ROOT, "config.json"))
        import upload_resumes as ur
        self.validate = ur._validate

    def test_flags_unknown_and_ignores_internal(self):
        bad, valid = self.validate(self.nt, "resume", {
            "name": "张三", "phone": "13800000000", "gender": "男", "_file": "/x.pdf"})
        self.assertEqual(bad, ["gender"])
        self.assertIn("phone", valid)  # 可用字段提示

    def test_all_valid_returns_empty(self):
        bad, _ = self.validate(self.nt, "resume", {"name": "张三", "skills": ["CAD"], "_file": "/x.pdf"})
        self.assertEqual(bad, [])


class TestParse(unittest.TestCase):
    def test_resume_phone_email(self):
        c = parse_resume("姓名：张三\n电话：13812345678\n邮箱：z@x.com\n学历：本科\n5年工作经验",
                         "张三.pdf")
        self.assertEqual(c["phone"], "13812345678")
        self.assertEqual(c["email"], "z@x.com")
        self.assertEqual(c["education"], "本科")
        self.assertEqual(c["years_experience"], 5)

    def test_job_filename(self):
        j = parse_job("岗位职责\n1、负责设备运维。",
                      "岗位说明书-制造中心-曲靖制造基地-单晶制造部-设备部 - 工程师.doc")
        self.assertEqual(j["department"], "单晶制造部-设备部")
        self.assertEqual(j["org"], "制造中心")
        self.assertTrue(j["job_name"])

    def test_job_id_deterministic(self):
        sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))
        from upload_jobs import job_id_of
        self.assertEqual(job_id_of("技术部", "工程师"), job_id_of("技术部", "工程师"))
        self.assertNotEqual(job_id_of("技术部", "工程师"), job_id_of("技术部", "主管"))


class TestTransport(unittest.TestCase):
    """本地 mock HTTP：覆盖 call 重试/401 刷新、put 重试、附件三步。不触真网。"""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.hits = {"api": 0, "put": 0, "rec": 0}
        hits = self.hits

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                hits["api"] += 1
                if "/oauth2/accessToken" in self.path:
                    self._json({"accessToken": "tk", "expireIn": 7200})
                elif "/uploadInfos/query" in self.path:
                    self._json({"result": {"uploadUrl": "http://127.0.0.1:%d/put" % PORT,
                                            "resourceId": "rid", "resourceUrl": "/r/rid"}})
                elif "/records/list" in self.path:
                    self._json({"hasMore": False, "records": []})
                else:                            # records 写：首击 429，重试成功
                    hits["rec"] += 1
                    if hits["rec"] == 1:
                        self._json({"code": "TooManyRequests"}, 429)
                    else:
                        self._json({"value": [{"id": "rec1"}]})

            def do_PUT(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                hits["put"] += 1
                self.send_response(500 if hits["put"] == 1 else 200)
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        global PORT
        PORT = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        import notable as mod
        self._old_api, self._old_cache = mod.API, mod.CACHE
        mod.API = "http://127.0.0.1:%d" % PORT
        mod.CACHE = os.path.join(ROOT, "tests", ".mock_token_cache.json")  # 不污染真缓存
        self.nt = Notable(os.path.join(ROOT, "config.json"))
        self.nt.cfg["base_id"] = "B"
        self.nt._token = None

    def tearDown(self):
        import notable as mod
        mod.API, mod.CACHE = self._old_api, self._old_cache
        mock_cache = os.path.join(ROOT, "tests", ".mock_token_cache.json")
        if os.path.exists(mock_cache):
            os.remove(mock_cache)
        self.srv.shutdown()

    def test_call_retries_429(self):
        out = self.nt.call("POST", "/v1.0/notable/bases/B/sheets/S/records",
                           {"records": []})
        self.assertEqual(out, {"value": [{"id": "rec1"}]})
        self.assertEqual(self.hits["rec"], 2)  # 429 一次 + 重试成功一次

    def test_put_retries_500(self):
        with open(os.path.join(ROOT, "README.md"), "rb") as f:
            self.nt.put("http://127.0.0.1:%d/put" % PORT, f.read(), "text/plain")
        self.assertEqual(self.hits["put"], 2)

    def test_upload_attachment_shape(self):
        cell = self.nt.upload_attachment(os.path.join(ROOT, "README.md"))
        self.assertEqual(cell["resourceId"], "rid")
        self.assertEqual(cell["url"], "/r/rid")
        self.assertGreater(cell["size"], 0)


    def test_map_parallel_order_and_errors(self):
        def flaky(x):
            if x == 2:
                raise ValueError("boom")
            return x * 10
        results, errs = self.nt.map_parallel(flaky, [1, 2, 3], workers=3)
        self.assertEqual(results, [10, None, 30])
        self.assertEqual([(i, str(e)) for i, e in errs], [(1, "boom")])


if __name__ == "__main__":
    unittest.main()
