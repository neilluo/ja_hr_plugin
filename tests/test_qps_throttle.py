#!/usr/bin/env python3
"""QPS 限流加固单测：不触真网，沿用仓库 mock HTTP 传输测试模式。

覆盖：QPS 403 重试（含 idempotent=False 豁免）、普通 403 零重试、5 次耗尽抛错、
preflight 整点峰值规避窗口、call() 全局 pacing 最小间隔。

历史教训（AGENTS.md）：handler 必须先读 Content-Length body，否则连接 RST。
"""

import json
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "shared", "preflight"))

import notable as mod  # noqa: E402
import preflight as pf  # noqa: E402
from notable import Notable, NotableError  # noqa: E402

QPS_MSG = "请求过于频繁，限制将在 2026-09-21 12:00:04 结束"
QPS_CODE = "Forbidden.AccessDenied.QpsLimitForApi"


class _FakeTime:
    """只冻结 time()，其余（monotonic/mktime/strptime）委托真实 time 模块。"""

    def __init__(self, fixed):
        self._fixed = fixed

    def time(self):
        return self._fixed

    def __getattr__(self, name):
        return getattr(time, name)


def _epoch(local_str):
    return time.mktime(time.strptime(local_str, "%Y-%m-%d %H:%M:%S"))


class _QpsTransportCase(unittest.TestCase):
    """mock HTTP：按 self.script（响应脚本列表）逐次应答，耗尽后重复最后一项。"""

    def setUp(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.hits = {"api": 0}
        self.times = []          # 服务端收包时刻（pacing 测试用）
        case = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # 历史教训：必须先读 Content-Length body，否则连接 RST
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                case.times.append(time.monotonic())
                if "/oauth2/accessToken" in self.path:
                    self._json({"accessToken": "tk", "expireIn": 7200})
                    return
                n = case.hits["api"]
                case.hits["api"] += 1
                script = case.script
                item = script[n] if n < len(script) else script[-1]
                self._json(item[0], item[1])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        self._old = (mod.API, mod.CACHE, mod._sleep, mod.time, mod._PACE_LAST[0])
        mod.API = "http://127.0.0.1:%d" % self.port
        mod.CACHE = os.path.join(ROOT, "tests", ".mock_qps_token_cache.json")
        mod._PACE_LAST[0] = 0.0
        self.sleeps = []
        mod._sleep = self.sleeps.append      # 假 sleep：只记录不真等
        mod.time = _FakeTime(_epoch("2026-09-21 12:00:00"))

        self.nt = Notable(os.path.join(ROOT, "config.json"))
        self.nt.cfg["base_id"] = "B"
        self.nt._token = "tk"                # 跳过 token 获取

    def tearDown(self):
        mod.API, mod.CACHE, mod._sleep, mod.time, _ = self._old
        mod._PACE_LAST[0] = 0.0
        cache = os.path.join(ROOT, "tests", ".mock_qps_token_cache.json")
        if os.path.exists(cache):
            os.remove(cache)
        self.srv.shutdown()

    def qps_sleeps(self):
        """过滤掉 pacing 的小睡，只留 QPS 等待（>=1s）。"""
        return [s for s in self.sleeps if s >= 1.0]

    def call(self, **kw):
        return self.nt.call("POST", "/v1.0/notable/bases/B/sheets/S/records/list",
                            {"maxResults": 10}, **kw)


class TestQpsRetry(_QpsTransportCase):
    OK = ({"hasMore": False, "records": []}, 200)
    QPS = ({"code": QPS_CODE, "message": QPS_MSG}, 403)

    def test_qps_403_then_200_succeeds_with_parsed_wait(self):
        self.script = [self.QPS, self.OK]
        out = self.call()
        self.assertEqual(out, {"hasMore": False, "records": []})
        self.assertEqual(self.hits["api"], 2)
        expected = mod._qps_wait(QPS_MSG, now=_epoch("2026-09-21 12:00:00"))
        self.assertEqual(self.qps_sleeps(), [expected])   # == 4.0（12:00:04 结束）
        self.assertEqual(expected, 4.0)

    def test_plain_403_no_retry_raises(self):
        self.script = [({"code": "Forbidden.AccessDenied.PermissionDenied",
                         "message": "无权限"}, 403)]
        with self.assertRaises(NotableError):
            self.call()
        self.assertEqual(self.hits["api"], 1)             # 零重试
        self.assertEqual(self.qps_sleeps(), [])

    def test_non_idempotent_post_still_retries_qps(self):
        # 关键不变量：QPS 403 是网关级拒绝、请求未被处理，idempotent=False 也豁免门禁
        self.script = [self.QPS, self.OK]
        out = self.call(idempotent=False)
        self.assertEqual(out, {"hasMore": False, "records": []})
        self.assertEqual(self.hits["api"], 2)

    def test_five_consecutive_qps_403_exhausts(self):
        self.script = [self.QPS]
        with self.assertRaises(NotableError) as ctx:
            self.call()
        self.assertEqual(self.hits["api"], 5)             # 预算 5 次请求后抛错
        self.assertIn("QpsLimitForApi", str(ctx.exception))
        self.assertEqual(len(self.qps_sleeps()), 4)       # 第 5 次失败后不再等待


class TestQpsWaitParse(unittest.TestCase):
    def test_clamp_and_fallback(self):
        now = _epoch("2026-09-21 12:00:00")
        far = mod._qps_wait("限制将在 2026-09-21 13:00:00 结束", now=now)
        self.assertEqual(far, 5.0)                        # clamp 上界
        past = mod._qps_wait(QPS_MSG, now=_epoch("2026-09-21 13:00:00"))
        self.assertEqual(past, 1.0)                       # clamp 下界
        bad = mod._qps_wait("没有结束时间", now=now)
        self.assertGreaterEqual(bad, 1.5)                 # 解析失败退化 1.5+抖动
        self.assertLess(bad, 2.0)


class TestPeakWindow(unittest.TestCase):
    """preflight 整点峰值规避：假时钟注入，不真等。"""

    def test_wait_seconds(self):
        cases = [
            ("2026-09-21 11:59:55", 15.0),   # 跨小时边界：等到 12:00:10
            ("2026-09-21 12:00:05", 5.0),
            ("2026-09-21 12:00:30", 0.0),    # 窗口外
            ("2026-09-21 11:59:30", 0.0),    # 窗口外
        ]
        for local, expect in cases:
            now = time.strptime(local, "%Y-%m-%d %H:%M:%S")
            self.assertEqual(pf._peak_wait(now=now), expect, local)

    def test_avoid_peak_sleeps_and_reports(self):
        slept = []
        old_lt, old_sl = pf._localtime, pf._sleep
        try:
            pf._localtime = lambda: time.strptime("2026-09-21 11:59:55",
                                                  "%Y-%m-%d %H:%M:%S")
            pf._sleep = slept.append
            checks = {}
            wait = pf._avoid_peak(checks)
        finally:
            pf._localtime, pf._sleep = old_lt, old_sl
        self.assertEqual(wait, 15.0)
        self.assertEqual(slept, [15.0])
        self.assertEqual(checks["peak_wait"]["seconds"], 15.0)


class TestPacing(unittest.TestCase):
    """6 线程并发打 mock 200：全局 pacing 保证相邻请求间隔 >= 0.045s。"""

    def test_min_interval(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        times = []
        tlock = threading.Lock()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        old_api, old_cache, old_sleep, old_last = (
            mod.API, mod.CACHE, mod._sleep, mod._PACE_LAST[0])
        old_pace = mod._pace
        mod.API = "http://127.0.0.1:%d" % port
        mod.CACHE = os.path.join(ROOT, "tests", ".mock_pace_token_cache.json")
        mod._sleep = time.sleep                # pacing 真等（共 ~0.3s）
        mod._PACE_LAST[0] = 0.0

        def recording_pace():
            # 记录「发包时刻」：pacing 门禁刚放行、即将发包的客户端时间戳
            old_pace()
            with tlock:
                times.append(time.monotonic())
        mod._pace = recording_pace
        try:
            nt = Notable(os.path.join(ROOT, "config.json"))
            nt.cfg["base_id"] = "B"
            nt._token = "tk"
            barrier = threading.Barrier(6)

            def worker():
                barrier.wait()
                nt.call("POST", "/v1.0/notable/bases/B/sheets/S/records/list",
                        {"maxResults": 10})

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            mod.API, mod.CACHE, mod._sleep = old_api, old_cache, old_sleep
            mod._pace = old_pace
            mod._PACE_LAST[0] = old_last
            cache = os.path.join(ROOT, "tests", ".mock_pace_token_cache.json")
            if os.path.exists(cache):
                os.remove(cache)
            srv.shutdown()

        self.assertEqual(len(times), 6)
        gaps = [b - a for a, b in zip(sorted(times), sorted(times)[1:])]
        self.assertGreaterEqual(min(gaps), 0.045,
                                "相邻请求最小间隔应 >= 0.045s，实际 %s" % gaps)


if __name__ == "__main__":
    unittest.main()
