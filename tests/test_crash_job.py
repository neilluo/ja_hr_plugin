#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃安全故障注入测试：岗位 JD 入库 (skills/job-intake/scripts/upload_jobs.py)。

不触真网。用 ThreadingHTTPServer 起一个内存版 Notable + OSS mock，
monkeypatch notable.API / notable.CACHE 到临时路径，然后进程内 patch
sys.argv 调用 upload_jobs.main()，捕获 SystemExit，检查内存表状态，
再跑一次验证恢复。

覆盖场景 J1-J8（含 J3a-d / J4a-b 共 12 个用例），unittest discover 可直接收集：
    python3 -m unittest discover -s tests
    python3 -m unittest discover -s tests -p "test_crash_job.py" -v

真实 JD 夹具目录经环境变量注入（不写死本机路径）：
    JA_TEST_DATA_DIR=/path/to/岗位说明书 python3 -m unittest discover -s tests
未设置或目录不存在时优雅 skip（夹具目录需含 19 份 .doc/.docx/.pdf JD 文件）。

设计纪律：本文件只读生产代码，绝不修改 shared/*.py / skills/*/scripts/*.py / config.json。
所有 monkeypatch 都在测试进程内、运行期完成（与 tests/test_notable_local.py 同手法）。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time as _real_time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "shared", "preflight"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

# 真实 JD 夹具目录：仅经环境变量注入，缺失则 skip（禁止硬编码本机绝对路径）
DATA_DIR = os.environ.get("JA_TEST_DATA_DIR", "")
EXPECTED_FILES = 19          # 标准夹具：19 份 JD -> 19 created（块1=10 / 块2=9）

import notable as notable_mod            # noqa: E402
from notable import Notable               # noqa: E402
import preflight as preflight_mod        # noqa: E402
import refine_loop                       # noqa: E402  every 型间隔与消费任务规格唯一真源
import upload_jobs                        # noqa: E402

CN_JOB_ID = "岗位ID"
CN_ATTACH = "JD附件"
CN_RESP = "岗位职责"             # config.fields.job.responsibilities
CN_REFINED = "AI精析时间"        # config.fields.job.ai_refined_at
JD_EXTS = (".doc", ".docx", ".pdf")


# --------------------------------------------------------------------------- #
# 快速 time 垫片：sleep 变 no-op，其余（time/monotonic/mktime/...）委托真实 time。
# 必须是「实例 + __getattr__」：notable._pace 会取 time.monotonic。
# --------------------------------------------------------------------------- #
class _FastTime:
    def sleep(self, *a, **k):
        return None

    def __getattr__(self, n):
        return getattr(_real_time, n)


# --------------------------------------------------------------------------- #
# 内存表 + 故障注入配置
# --------------------------------------------------------------------------- #
class MemDB:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = []          # [{id, fields:{中文名: 值}}]
        self._seq = 0
        # 计数器
        self.create_calls = 0      # POST .../records (create) 次数（含重试）
        self.list_calls = 0        # POST .../records/list 次数
        self.uploadinfo_calls = 0  # POST .../uploadInfos/query 次数
        self.put_calls = 0         # OSS PUT 次数
        self.token_calls = 0
        # 故障注入规则
        self.create_rules = {}     # {create_call_index: action}
        self.list_fail_on = None   # 第 N 次 list 调用失败（1-based）
        self.list_fail_code = 400
        self.attach_fail_names = set()   # 指定文件名 uploadInfos 直接失败
        self.put_fail_index = None       # 第 N 次 OSS PUT 失败
        self.token_401_once = False      # 首个业务调用返回一次 401
        self._fired_401 = False
        self.log = []              # 事件流水，供报告取证

    # -- 表操作 --
    def add(self, fields):
        with self.lock:
            self._seq += 1
            rid = "rec%04d" % self._seq
            self.records.append({"id": rid, "fields": dict(fields)})
            return rid

    def all_job_ids(self):
        with self.lock:
            return [r["fields"].get(CN_JOB_ID) for r in self.records]

    def dup_job_ids(self):
        c = Counter(self.all_job_ids())
        return {k: v for k, v in c.items() if v > 1}

    def records_without_attachment(self):
        with self.lock:
            return [r["id"] for r in self.records if not r["fields"].get(CN_ATTACH)]

    def count(self):
        with self.lock:
            return len(self.records)

    def reset(self):
        with self.lock:
            self.records = []
            self._seq = 0


def _make_handler(db, port_holder):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _read_body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(n) if n else b""

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _drop(self):
            """模拟『服务端已处理但响应在网络上丢失』：直接关连接，不发响应。
            客户端会得到 RemoteDisconnected（非 URLError）。"""
            try:
                self.close_connection = True
                self.wfile.flush()
            except Exception:
                pass
            # 强制断开：不调用 send_response
            try:
                self.connection.shutdown(1)
            except Exception:
                pass
            self.connection.close()

        # ---------------- POST ----------------
        def do_POST(self):
            raw = self._read_body()
            path = self.path.split("?")[0]

            # 401 一次性注入（作用于任意业务调用之前）
            if db.token_401_once and not db._fired_401 and "oauth2" not in path:
                db._fired_401 = True
                self._json({"code": "Forbidden.AccessDenied",
                            "message": "token expired"}, 401)
                return

            if "/oauth2/accessToken" in path:
                db.token_calls += 1
                db.log.append("token")
                self._json({"accessToken": "tk-mock", "expireIn": 7200})
                return

            if "/uploadInfos/query" in path:
                db.uploadinfo_calls += 1
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                name = body.get("resourceName", "")
                db.log.append("uploadinfo:%s" % name)
                if name in db.attach_fail_names:
                    # 非重试错误 -> upload_attachment 立即抛 NotableError
                    self._json({"code": "InvalidParameter",
                                "message": "injected uploadInfos failure"}, 400)
                    return
                h = abs(hash(name)) % 100000
                self._json({"result": {
                    "uploadUrl": "http://127.0.0.1:%d/oss-put" % port_holder["port"],
                    "resourceId": "rid-%s" % h,
                    "resourceUrl": "/r/%s" % h}})
                return

            if "/records/list" in path:
                db.list_calls += 1
                db.log.append("list#%d" % db.list_calls)
                if db.list_fail_on == db.list_calls:
                    self._json({"code": "InternalError",
                                "message": "injected list failure"}, db.list_fail_code)
                    return
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                want = body.get("fieldIdOrNames")
                with db.lock:
                    recs = []
                    for r in db.records:
                        if want:
                            f = {k: v for k, v in r["fields"].items() if k in want}
                        else:
                            f = dict(r["fields"])
                        recs.append({"id": r["id"], "fields": f})
                self._json({"hasMore": False, "nextToken": "", "records": recs})
                return

            if "/records/delete" in path:
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                ids = set(body.get("recordIds", []))
                with db.lock:
                    db.records = [r for r in db.records if r["id"] not in ids]
                db.log.append("delete:%d" % len(ids))
                self._json({"success": True})
                return

            # create records:  POST .../sheets/<sheet>/records
            if path.endswith("/records"):
                db.create_calls += 1
                idx = db.create_calls
                action = db.create_rules.get(idx, "ok")
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                chunk = body.get("records", [])
                db.log.append("create#%d:%s(n=%d)" % (idx, action, len(chunk)))

                if action in ("ok", "drop_after_commit", "500_after_commit", "503_after_commit"):
                    ids = []
                    for rec in chunk:
                        ids.append(db.add(rec.get("fields", {})))
                else:  # *_before_commit
                    ids = []

                if action == "drop_before_commit":
                    # 服务端未提交就断连：客户端 RemoteDisconnected（写结果未知）
                    self._drop()
                    return
                if action == "drop_after_commit":
                    # 已提交内存表，然后丢弃响应
                    self._drop()
                    return
                if action == "500_after_commit":
                    self._json({"code": "InternalError",
                                "message": "injected 500 after commit"}, 500)
                    return
                if action == "503_after_commit":
                    self._json({"code": "ServiceUnavailable",
                                "message": "injected 503 after commit"}, 503)
                    return
                if action == "500_before_commit":
                    self._json({"code": "InternalError",
                                "message": "injected 500 before commit"}, 500)
                    return
                if action == "raise_base":
                    raise BaseException("injected process death at create")
                # ok
                self._json({"value": [{"id": i} for i in ids]})
                return

            self._json({"code": "NotFound", "message": path}, 404)

        # ---------------- PUT (OSS) ----------------
        def do_PUT(self):
            self._read_body()
            db.put_calls += 1
            idx = db.put_calls
            db.log.append("put#%d" % idx)
            if db.put_fail_index == idx:
                self._json({"code": "AccessDenied", "message": "injected oss fail"}, 403)
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return H


# --------------------------------------------------------------------------- #
# Harness：起 mock server + patch notable/preflight + 跑 upload_jobs.main()
# --------------------------------------------------------------------------- #
class Harness:
    def __init__(self):
        self.db = MemDB()
        self.port_holder = {"port": 0}
        handler = _make_handler(self.db, self.port_holder)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port_holder["port"] = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

        self._old_api = notable_mod.API
        self._old_cache = notable_mod.CACHE
        self._old_time = notable_mod.time
        self._old_sleep = notable_mod._sleep
        self._old_pf_sleep = preflight_mod._sleep
        self._old_create = Notable.create_records
        notable_mod.API = "http://127.0.0.1:%d" % self.port_holder["port"]
        self._cache_dir = tempfile.mkdtemp(prefix="crash_job_cache_")
        self._cache_path = os.path.join(self._cache_dir, "token_cache.json")
        notable_mod.CACHE = self._cache_path
        notable_mod.time = _FastTime()       # 加速退避（含 monotonic 委托）
        notable_mod._sleep = lambda *a, **k: None    # call() 退避走模块级 _sleep
        preflight_mod._sleep = lambda *a, **k: None  # 整点峰值规避不真等

    def close(self):
        notable_mod.API = self._old_api
        notable_mod.CACHE = self._old_cache
        notable_mod.time = self._old_time
        notable_mod._sleep = self._old_sleep
        preflight_mod._sleep = self._old_pf_sleep
        Notable.create_records = self._old_create
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    # 让下一次 main() 在 create_records 之前抛 BaseException（模拟进程死亡）
    def inject_death_before_create(self):
        def boom(self_nt, table, rows):
            raise BaseException("injected death before create_records")
        Notable.create_records = boom

    def clear_death_injection(self):
        Notable.create_records = self._old_create

    def run_main(self, data_dir, extra_argv=None):
        """进程内调用 upload_jobs.main()。返回 (exit_code_or_None, stdout, exc)。"""
        argv = ["upload_jobs.py", data_dir] + (extra_argv or [])
        old_argv = sys.argv
        old_stdout, old_stderr = sys.stdout, sys.stderr
        buf, errbuf = io.StringIO(), io.StringIO()
        sys.argv = argv
        sys.stdout, sys.stderr = buf, errbuf
        exc = None
        code = None
        try:
            upload_jobs.main()
        except SystemExit as e:
            code = e.code
        except BaseException as e:  # noqa: BLE001 捕获注入的死亡 / 未处理异常
            exc = e
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            sys.argv = old_argv
        return code, buf.getvalue(), exc


def _report(out):
    """从 stdout 提取 JSON 报告（报告均整段输出；兜底 regex 截取）。"""
    out = (out or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {}


# --------------------------------------------------------------------------- #
# TestCase：J1-J8（12 场景）
# --------------------------------------------------------------------------- #
class TestCrashJob(unittest.TestCase):
    """岗位入库崩溃注入。需要真实 JD 夹具目录（环境变量 JA_TEST_DATA_DIR），缺失则 skip。"""

    def setUp(self):
        if not DATA_DIR or not os.path.isdir(DATA_DIR):
            self.skipTest("需设置 JA_TEST_DATA_DIR 指向岗位说明书目录（19 份 JD 夹具）")
        n = len([f for f in os.listdir(DATA_DIR) if f.lower().endswith(JD_EXTS)])
        if n != EXPECTED_FILES:
            self.skipTest("JA_TEST_DATA_DIR 需含 %d 份 JD 夹具（实际 %d 份），场景计数不成立"
                          % (EXPECTED_FILES, n))
        # 假凭证走环境变量，避免依赖本机 .secrets.json（mock server 无条件发 token）
        self._old_env = {k: os.environ.get(k)
                         for k in ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET")}
        os.environ["DINGTALK_APP_KEY"] = "crash-job-test-key"
        os.environ["DINGTALK_APP_SECRET"] = "crash-job-test-secret"
        self.h = Harness()
        self._tmpdirs = []

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        self.h.close()

    def _fresh_data_dir(self, with_dup=False):
        d = tempfile.mkdtemp(prefix="crash_job_data_")
        self._tmpdirs.append(d)
        dst = os.path.join(d, "jd")
        shutil.copytree(DATA_DIR, dst)
        if with_dup:
            files = sorted(f for f in os.listdir(dst) if f.lower().endswith(JD_EXTS))
            src = os.path.join(dst, files[0])
            # 同内容 + 仅在扩展名前追加尾号 -> parse_job 里 re.sub(r"\d+$") 会把尾号去掉，
            # department(来自文件名前缀 token) 与 job_name(来自正文) 完全一致 -> 同 job_id。
            base, ext = os.path.splitext(files[0])
            shutil.copy(src, os.path.join(dst, base + "2" + ext))
        return dst

    # --- J1 基线 ---------------------------------------------------------- #
    def test_j1_baseline(self):
        d = self._fresh_data_dir()
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep.get("created"), EXPECTED_FILES)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(rep.get("readback_missing"), [])
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J2 create 块1 提交、块2 提交前断连 -------------------------------- #
    def test_j2_block2_drop_before_commit(self):
        d = self._fresh_data_dir()
        # 第 2 个 create POST（块2）在服务端提交前断连 -> 客户端未提交块2
        self.h.db.create_rules = {2: "drop_before_commit"}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        # 块1(10)已提交；断连块2 客户端未提交 -> NotableError -> exit 1 + error JSON
        self.assertIsNone(exc, "裸异常应被 NotableError 包裹进 error JSON")
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.count(), 10)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        # 重跑恢复：dedup 跳过块1 只补块2
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), EXPECTED_FILES - 10)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J3a 提交后响应丢失（RemoteDisconnected） --------------------------- #
    def test_j3a_drop_after_commit(self):
        d = self._fresh_data_dir()
        self.h.db.create_rules = {1: "drop_after_commit"}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        # 非幂等写：连接层故障不重试 -> NotableError -> exit 1；块1 只提交 1 次
        self.assertIsNone(exc)
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.create_calls, 1, "非幂等写不得重试")
        self.assertEqual(self.h.db.count(), 10)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        # 重跑：补齐且无重复
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), EXPECTED_FILES - 10)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})

    # --- J3b 提交后返回 500 ------------------------------------------------ #
    def test_j3b_500_after_commit(self):
        d = self._fresh_data_dir()
        self.h.db.create_rules = {1: "500_after_commit"}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        # 修复后语义：create idempotent=False -> 500 不盲重试 -> 块只提交一次，无重复
        self.assertIsNone(exc)
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.create_calls, 1, "500-after-commit 不得重投同一 POST")
        self.assertEqual(self.h.db.dup_job_ids(), {})
        # 重跑：dedup 跳过已存在，补齐 19，无重复
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), EXPECTED_FILES - 10)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})

    # --- J3c 持续 500-after-commit（原「重试耗尽写4遍」最坏放大场景） -------- #
    def test_j3c_500_after_commit_all_retries(self):
        d = self._fresh_data_dir()
        # 前 4 次 create 全部 after-commit-500：修复后非幂等写不重试，第 1 次即抛错，
        # 后续规则不再触发；表内只有块1 的一份，无重复放大。
        self.h.db.create_rules = {1: "500_after_commit", 2: "500_after_commit",
                                  3: "500_after_commit", 4: "500_after_commit"}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.create_calls, 1, "禁盲重试 -> 只有 1 次 POST")
        self.assertEqual(self.h.db.count(), 10)
        self.assertEqual(self.h.db.dup_job_ids(), {}, "重试耗尽不得产生重复放大")

    # --- J3d 块2 提交后返回 503 -------------------------------------------- #
    def test_j3d_503_after_commit(self):
        d = self._fresh_data_dir()
        self.h.db.create_rules = {2: "503_after_commit"}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        # 503 与 500 同属 RETRY_STATUS，但 idempotent=False 一律不重投
        self.assertIsNone(exc)
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.create_calls, 2)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.count(), EXPECTED_FILES, "块1+块2 各提交一次")
        # 重跑：全部 dedup，created=0，表完好
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), 0)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})

    # --- J4a 附件 uploadInfos 按文件名失败 -> 该岗位不写表 ------------------- #
    def test_j4a_uploadinfo_fail_by_name(self):
        d = self._fresh_data_dir()
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(JD_EXTS))
        target = files[3]
        self.h.db.attach_fail_names = {target}
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(rep.get("created"), EXPECTED_FILES - 1)
        self.assertEqual([f.get("file") for f in rep.get("failed", [])], [target])
        self.assertEqual(self.h.db.count(), EXPECTED_FILES - 1)
        self.assertEqual(self.h.db.records_without_attachment(), [],
                         "附件失败条绝不写表（不变量③）")
        # 重跑补齐
        self.h.db.attach_fail_names = set()
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), 1)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J4b OSS PUT 第 N 次失败 -> 该岗位不写表 ---------------------------- #
    def test_j4b_oss_put_fail_by_index(self):
        d = self._fresh_data_dir()
        self.h.db.put_fail_index = 5   # 第 5 次 OSS PUT 失败（并发下哪个岗位不确定，恰 1 个失败）
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(rep.get("created"), EXPECTED_FILES - 1)
        self.assertEqual(len(rep.get("failed", [])), 1)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES - 1)
        self.assertEqual(self.h.db.records_without_attachment(), [])
        # 重跑补齐
        self.h.db.put_fail_index = None
        code2, out2, exc2 = self.h.run_main(d)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J5 附件全传完、create 之前进程死亡 --------------------------------- #
    def test_j5_death_before_create(self):
        d = self._fresh_data_dir()
        self.h.inject_death_before_create()
        code, out, exc = self.h.run_main(d)
        self.assertIsNotNone(exc, "注入的 BaseException 应穿透 main()")
        self.assertEqual(self.h.db.count(), 0, "create 前死亡 -> 表 0 条（无孤儿记录）")
        self.assertGreater(self.h.db.put_calls, 0, "附件已上传但未写表")
        # 重跑完整恢复
        self.h.clear_death_injection()
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), EXPECTED_FILES)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J6 回读阶段 list 失败 --------------------------------------------- #
    def test_j6_readback_list_fail(self):
        d = self._fresh_data_dir()
        # list 调用序列：#1 = 起始 dedup，#2 = 回读。让 #2 失败（非重试 400）
        self.h.db.list_fail_on = 2
        self.h.db.list_fail_code = 400
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        # 修复后语义：回读/查重 list 失败被捕获 -> exit 1 + JSON 报告，不得裸 traceback
        self.assertIsNone(exc, "回读失败不得裸抛异常")
        self.assertEqual(code, 1)
        # 新契约：报告首行是 VERDICT 结论行、其后为 JSON（首行即证明是结构化报告而非裸 traceback）
        self.assertTrue(out.strip().startswith("VERDICT:"), "stdout 必须是 VERDICT+JSON 报告")
        self.assertIn("error", rep)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES, "数据已提交")
        # 重跑：list 正常 -> created=0（全被 dedup 跳过），表完好
        self.h.db.list_fail_on = None
        code2, out2, exc2 = self.h.run_main(d)
        rep2 = _report(out2)
        self.assertIsNone(exc2)
        self.assertEqual(code2, 0)
        self.assertEqual(rep2.get("created"), 0)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})
        self.assertEqual(self.h.db.records_without_attachment(), [])

    # --- J7 批内重复 -------------------------------------------------------- #
    def test_j7_batch_dup(self):
        d = self._fresh_data_dir(with_dup=True)
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep.get("total"), EXPECTED_FILES + 1)
        self.assertEqual(len(rep.get("skipped_dup", [])), 1,
                         "同部门同岗位名批内只建 1 条（不变量②）")
        self.assertEqual(rep.get("created"), EXPECTED_FILES)
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})

    # --- J8 401 中途触发 ---------------------------------------------------- #
    def test_j8_401_refresh(self):
        d = self._fresh_data_dir()
        self.h.db.token_401_once = True
        code, out, exc = self.h.run_main(d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep.get("created"), EXPECTED_FILES)
        self.assertGreaterEqual(self.h.db.token_calls, 2, "401 后必须刷新 token")
        self.assertEqual(self.h.db.count(), EXPECTED_FILES)
        self.assertEqual(self.h.db.dup_job_ids(), {})


class TestJobRefineCronJob(unittest.TestCase):
    """纯 mock 层：岗位报告 cron_job 语义（不依赖 JA_TEST_DATA_DIR 夹具，永不 skip）。

    空目录跑 main()（created=0），队列状态完全由预置内存记录决定：
    队列非空 → 报告含 cron_job（every 型注册规格，无绝对时刻）；队列为空 → 不输出该字段。
    at 型的 refine_fire_at 字段已整体删除（every 型注册永不过期，见 refine_loop.EVERY_MS 注释）。"""

    def setUp(self):
        # 假凭证走环境变量，避免依赖本机 .secrets.json（mock server 无条件发 token）
        self._old_env = {k: os.environ.get(k)
                         for k in ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET")}
        os.environ["DINGTALK_APP_KEY"] = "crash-job-test-key"
        os.environ["DINGTALK_APP_SECRET"] = "crash-job-test-secret"
        self.h = Harness()
        self.d = tempfile.mkdtemp(prefix="crash_job_cronjob_")
        # preflight 要求目录至少含一份支持格式文件；放一份不可解析的占位文件，
        # 解析失败进 failed、created=0，队列状态完全由预置内存记录决定。
        with open(os.path.join(self.d, "placeholder.pdf"), "wb") as f:
            f.write(b"not-a-real-jd")

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)
        self.h.close()

    def test_cron_job_emitted_when_queue_nonempty(self):
        # 预置一条待精析岗位（无标记 + 有职责）→ refine_queued=1 → 输出 every 型 cron_job
        self.h.db.add({CN_JOB_ID: "JSEED000001", CN_RESP: "负责设备维护与保养",
                       CN_REFINED: None})
        code, out, exc = self.h.run_main(self.d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        # 种子记录保证队列非空（占位文件能否解析不影响本断言）
        self.assertGreater(rep.get("refine_queued"), 0)
        self.assertIn("cron_job", rep)
        self.assertEqual(rep["cron_job"]["schedule"],
                         {"kind": "every", "everyMs": refine_loop.EVERY_MS})
        self.assertTrue(rep["cron_job"]["name"].startswith(refine_loop.TASK_PREFIX["job"]))
        # at 型残留字段必须已删干净（防复活）
        self.assertNotIn("refine_fire_at", rep)
        self.assertNotIn("at", rep["cron_job"]["schedule"])

    def test_no_cron_job_when_queue_empty(self):
        # 队列空（唯一记录已打标）→ refine_queued=0 且不输出 cron_job / refine_fire_at
        self.h.db.add({CN_JOB_ID: "JSEED000002", CN_RESP: "已精析",
                       CN_REFINED: 1789924509618})
        code, out, exc = self.h.run_main(self.d)
        rep = _report(out)
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep.get("refine_queued"), 0)
        self.assertNotIn("cron_job", rep)
        self.assertNotIn("refine_fire_at", rep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
