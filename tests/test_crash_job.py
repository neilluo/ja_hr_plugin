#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""崩溃安全故障注入测试：岗位 JD 入库 (skills/job-intake/scripts/upload_jobs.py)。

不触真网。用 ThreadingHTTPServer 起一个内存版 Notable + OSS mock，
monkeypatch notable.API / notable.CACHE 到临时路径，然后进程内 patch
sys.argv 调用 upload_jobs.main()，捕获 SystemExit，检查内存表状态，
再跑一次验证恢复。

覆盖场景 J1-J8（见 report）。运行：
    python3 tests/test_crash_job.py

设计纪律：本文件只读生产代码，绝不修改 shared/*.py / skills/*/scripts/*.py / config.json。
所有 monkeypatch 都在测试进程内、运行期完成（与 tests/test_notable_local.py 同手法）。
"""
import json
import os
import shutil
import sys
import threading
import time as _real_time
import traceback
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "job-intake", "scripts"))

DATA_DIR = "/Users/neil/Desktop/qwenworklearn/jahrplugin/data/岗位说明书"

import notable as notable_mod            # noqa: E402
from notable import Notable, NotableError  # noqa: E402
import upload_jobs                        # noqa: E402

JOB_SHEET = "5Y4JylL"          # config.json tables.job.table_id
CN_JOB_ID = "岗位ID"
CN_ATTACH = "JD附件"


# --------------------------------------------------------------------------- #
# 快速 time 垫片：把指数退避 sleep 变 no-op，其余委托真实 time
# --------------------------------------------------------------------------- #
class _FastTime:
    mktime = staticmethod(_real_time.mktime)
    strptime = staticmethod(_real_time.strptime)
    time = staticmethod(_real_time.time)

    @staticmethod
    def sleep(*a, **k):
        return None


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

                committed = False
                if action in ("ok", "drop_after_commit", "500_after_commit", "503_after_commit"):
                    ids = []
                    for rec in chunk:
                        ids.append(db.add(rec.get("fields", {})))
                    committed = True
                else:  # *_before_commit
                    ids = []

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
# Harness：起 mock server + patch notable + 跑 upload_jobs.main()
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
        self._old_create = Notable.create_records
        notable_mod.API = "http://127.0.0.1:%d" % self.port_holder["port"]
        self._cache_path = os.path.join(ROOT, "tests", ".crash_job_token_cache.json")
        notable_mod.CACHE = self._cache_path
        notable_mod.time = _FastTime  # 加速退避

    def close(self):
        notable_mod.API = self._old_api
        notable_mod.CACHE = self._old_cache
        notable_mod.time = self._old_time
        Notable.create_records = self._old_create
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass
        if os.path.exists(self._cache_path):
            os.remove(self._cache_path)

    # 让下一次 main() 在 create_records 之前抛 BaseException（模拟进程死亡）
    def inject_death_before_create(self):
        orig = self._old_create

        def boom(self_nt, table, rows):
            raise BaseException("injected death before create_records")
        Notable.create_records = boom

    def clear_death_injection(self):
        Notable.create_records = self._old_create

    def run_main(self, data_dir, extra_argv=None):
        """进程内调用 upload_jobs.main()。返回 (exit_code_or_None, stdout, exc)。"""
        argv = ["upload_jobs.py", data_dir] + (extra_argv or [])
        old_argv = sys.argv
        old_stdout = sys.stdout
        import io
        buf = io.StringIO()
        sys.argv = argv
        sys.stdout = buf
        exc = None
        code = None
        try:
            upload_jobs.main()
        except SystemExit as e:
            code = e.code
        except BaseException as e:  # noqa: BLE001 捕获注入的死亡 / 未处理异常
            exc = e
        finally:
            sys.stdout = old_stdout
            sys.argv = old_argv
        return code, buf.getvalue(), exc


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def _fresh_data_dir(with_dup=False):
    d = "/tmp/crash_job_data"
    if os.path.exists(d):
        shutil.rmtree(d)
    shutil.copytree(DATA_DIR, d)
    if with_dup:
        files = sorted(os.listdir(d))
        src = os.path.join(d, files[0])
        # 同内容 + 仅在扩展名前追加尾号 -> parse_job 里 re.sub(r"\d+$") 会把尾号去掉，
        # department(来自文件名前缀 token) 与 job_name(来自正文) 完全一致 -> 同 job_id。
        base, ext = os.path.splitext(files[0])
        shutil.copy(src, os.path.join(d, base + "2" + ext))
    return d


def scenario(name):
    def deco(fn):
        fn._scenario_name = name
        return fn
    return deco


RESULTS = []


def record(name, status, evidence):
    RESULTS.append({"scenario": name, "status": status, "evidence": evidence})
    print("\n[%s] %s" % (status, name))
    for k, v in evidence.items():
        print("    %s: %s" % (k, v))


# --- J1 基线 -------------------------------------------------------------- #
def j1_baseline():
    h = Harness()
    try:
        d = _fresh_data_dir()
        code, out, exc = h.run_main(d)
        rep = json.loads(out)
        jids = h.db.all_job_ids()
        ok = (code == 0 and exc is None and rep.get("created") == 19
              and h.db.count() == 19 and not h.db.dup_job_ids()
              and not rep.get("readback_missing")
              and not h.db.records_without_attachment())
        record("J1 基线：19 created / 无重复 / 全带附件",
               "PASS" if ok else "FAIL",
               {"exit_code": code, "created": rep.get("created"),
                "table_count": h.db.count(), "unique_job_id": len(set(jids)),
                "dup": h.db.dup_job_ids(), "no_attachment": h.db.records_without_attachment(),
                "readback_missing": rep.get("readback_missing")})
    finally:
        h.close()


# --- J2 create 块1 提交、块2 连接断开（服务端未提交块2） ------------------ #
def j2_block2_drop_before_commit():
    h = Harness()
    try:
        d = _fresh_data_dir()
        # 第 2 个 create POST（块2）在服务端提交前断连 -> 客户端未提交块2
        h.db.create_rules = {2: "drop_before_commit"}
        code, out, exc = h.run_main(d)
        first = {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                 "table_count": h.db.count(), "dup": h.db.dup_job_ids()}
        # 重跑恢复
        code2, out2, exc2 = h.run_main(d)
        rep2 = json.loads(out2) if out2.strip().startswith("{") else {}
        jids = h.db.all_job_ids()
        ok = (h.db.count() == 19 and not h.db.dup_job_ids()
              and not h.db.records_without_attachment())
        record("J2 块2 提交前断连 -> 报错退出；重跑补齐 19 无重复",
               "PASS" if ok else "FAIL",
               {"first_run": first,
                "rerun_exit": code2, "rerun_exc": type(exc2).__name__ if exc2 else None,
                "rerun_created": rep2.get("created"),
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "note": "块1(10)已提交；断连块2 客户端未提交，重跑 dedup 跳过块1 只补块2"})
    finally:
        h.close()


# --- J3 重点：提交后响应丢失 / 500-after-commit -> 客户端重试 -------------- #
def j3a_drop_after_commit():
    """块1 服务端已提交但响应丢失（RemoteDisconnected）。"""
    h = Harness()
    try:
        d = _fresh_data_dir()
        h.db.create_rules = {1: "drop_after_commit"}
        code, out, exc = h.run_main(d)
        first = {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                 "exc_is_urlerror": isinstance(exc, __import__("urllib.error", fromlist=["URLError"]).URLError) if exc else None,
                 "table_count": h.db.count(), "dup": h.db.dup_job_ids(),
                 "create_calls": h.db.create_calls}
        # 重跑
        code2, out2, exc2 = h.run_main(d)
        jids = h.db.all_job_ids()
        record("J3a 块1 提交后响应丢失(RemoteDisconnected) -> 是否重复?",
               "PASS" if not h.db.dup_job_ids() else "FAIL",
               {"first_run": first,
                "rerun_exit": code2, "rerun_exc": type(exc2).__name__ if exc2 else None,
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "note": "RemoteDisconnected 非 URLError -> call() 不重试；块1 只提交 1 次"})
    finally:
        h.close()


def j3b_500_after_commit():
    """块1 服务端已提交，然后返回 500 -> call() 认为可重试 -> 重复 POST 同块。"""
    h = Harness()
    try:
        d = _fresh_data_dir()
        # 第 1 次 create：提交后返回 500；重试(第2次)正常
        h.db.create_rules = {1: "500_after_commit"}
        code, out, exc = h.run_main(d)
        first = {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                 "table_count": h.db.count(), "dup": h.db.dup_job_ids(),
                 "create_calls": h.db.create_calls}
        # 重跑（dedup 应跳过已存在，但重复项已在表里）
        code2, out2, exc2 = h.run_main(d)
        dup_after = h.db.dup_job_ids()
        record("J3b 块1 提交后返回500 -> 客户端重试 -> 重复写入?",
               "FAIL" if dup_after else "PASS",
               {"first_run": first,
                "rerun_exit": code2, "rerun_created": (json.loads(out2).get("created") if out2.strip().startswith("{") else None),
                "final_count": h.db.count(), "dup_after_rerun": dup_after,
                "dup_records_sample": [r for r in h.db.records if r["fields"].get(CN_JOB_ID) in dup_after][:4],
                "note": "500 属 RETRY_STATUS -> call() 重投同一 POST；POST 非幂等 -> 块1 写两遍"})
    finally:
        h.close()


# --- J4 附件阶段第 k 个失败 -> 该岗位不写表 ------------------------------ #
def j4a_uploadinfo_fail_by_name():
    h = Harness()
    try:
        d = _fresh_data_dir()
        files = sorted(f for f in os.listdir(d) if f.lower().endswith((".doc", ".docx", ".pdf")))
        target = files[3]
        h.db.attach_fail_names = {target}
        code, out, exc = h.run_main(d)
        rep = json.loads(out) if out.strip().startswith("{") else {}
        first = {"exit_code": code, "created": rep.get("created"),
                 "table_count": h.db.count(),
                 "failed": [f.get("file") for f in rep.get("failed", [])],
                 "no_attachment": h.db.records_without_attachment()}
        # 重跑补齐
        h.db.attach_fail_names = set()
        code2, out2, exc2 = h.run_main(d)
        rep2 = json.loads(out2) if out2.strip().startswith("{") else {}
        ok = (first["created"] == 18 and first["no_attachment"] == []
              and h.db.count() == 19 and not h.db.dup_job_ids()
              and not h.db.records_without_attachment())
        record("J4a 附件 uploadInfos 第k个失败 -> 该岗位不写表；重跑补齐19；无空附件岗位",
               "PASS" if ok else "FAIL",
               {"target_file": target, "first_run": first,
                "rerun_exit": code2, "rerun_created": rep2.get("created"),
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "final_no_attachment": h.db.records_without_attachment()})
    finally:
        h.close()


def j4b_oss_put_fail_by_index():
    h = Harness()
    try:
        d = _fresh_data_dir()
        h.db.put_fail_index = 5     # 第 5 次 OSS PUT 失败（并发下哪个岗位不确定，但恰好 1 个失败）
        code, out, exc = h.run_main(d)
        rep = json.loads(out) if out.strip().startswith("{") else {}
        first = {"exit_code": code, "created": rep.get("created"),
                 "table_count": h.db.count(),
                 "no_attachment": h.db.records_without_attachment()}
        h.db.put_fail_index = None
        code2, out2, exc2 = h.run_main(d)
        ok = (first["created"] == 18 and first["no_attachment"] == []
              and h.db.count() == 19 and not h.db.records_without_attachment())
        record("J4b OSS PUT 第5次失败 -> 该岗位不写表；重跑补齐19；无空附件岗位",
               "PASS" if ok else "FAIL",
               {"first_run": first, "rerun_exit": code2,
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "final_no_attachment": h.db.records_without_attachment()})
    finally:
        h.close()


# --- J5 附件全成功、create 之前进程死亡 --------------------------------- #
def j5_death_before_create():
    h = Harness()
    try:
        d = _fresh_data_dir()
        h.inject_death_before_create()
        code, out, exc = h.run_main(d)
        first = {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                 "table_count": h.db.count(), "put_calls": h.db.put_calls}
        h.clear_death_injection()
        code2, out2, exc2 = h.run_main(d)
        rep2 = json.loads(out2) if out2.strip().startswith("{") else {}
        ok = (first["table_count"] == 0 and h.db.count() == 19
              and not h.db.dup_job_ids() and not h.db.records_without_attachment())
        record("J5 附件全传完、create 前进程死亡 -> 表 0 条；重跑完整 19",
               "PASS" if ok else "FAIL",
               {"first_run": first, "rerun_exit": code2, "rerun_created": rep2.get("created"),
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "note": "附件已上传(put_calls)但无写表 -> 无孤儿记录；重跑重新上传附件"})
    finally:
        h.close()


# --- J6 回读阶段 list 失败 ---------------------------------------------- #
def j6_readback_list_fail():
    h = Harness()
    try:
        d = _fresh_data_dir()
        # list 调用序列：#1 = 起始 dedup，#2 = 回读。让 #2 失败（非重试 400）
        h.db.list_fail_on = 2
        h.db.list_fail_code = 400
        code, out, exc = h.run_main(d)
        first = {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                 "exc_is_notableerror": isinstance(exc, NotableError) if exc else None,
                 "uncaught": exc is not None,
                 "stdout_is_report": out.strip().startswith("{"),
                 "table_count": h.db.count()}
        # 重跑：list 正常 -> created=0（全被 dedup 跳过），表完好
        h.db.list_fail_on = None
        code2, out2, exc2 = h.run_main(d)
        rep2 = json.loads(out2) if out2.strip().startswith("{") else {}
        ok_table = (h.db.count() == 19 and not h.db.dup_job_ids()
                    and not h.db.records_without_attachment())
        # 修复后语义：回读/查重 list 失败应被捕获 → exit 1 + JSON 报告，不得裸 traceback
        ok_shape = (first["exit_code"] == 1 and first["stdout_is_report"]
                    and not first["uncaught"])
        record("J6 回读阶段 list 失败 -> exit 1 + JSON 报告（不得裸 traceback）；重跑 created=0 表完好",
               "PASS" if (ok_table and ok_shape) else "FAIL",
               {"first_run": first, "rerun_exit": code2, "rerun_created": rep2.get("created"),
                "final_count": h.db.count(), "dup": h.db.dup_job_ids(),
                "note": "修复后：_finalize/回读段包 try/except NotableError，失败输出 error JSON + exit 1"})
    finally:
        h.close()


# --- J7 批内重复 --------------------------------------------------------- #
def j7_batch_dup():
    h = Harness()
    try:
        d = _fresh_data_dir(with_dup=True)
        code, out, exc = h.run_main(d)
        rep = json.loads(out) if out.strip().startswith("{") else {}
        jids = h.db.all_job_ids()
        ok = (h.db.count() == 19 and not h.db.dup_job_ids()
              and len(rep.get("skipped_dup", [])) == 1 and rep.get("total") == 20)
        record("J7 批内重复(复制1个JD改名) -> 同部门同岗位名只建1条",
               "PASS" if ok else "FAIL",
               {"exit_code": code, "total": rep.get("total"), "parsed": rep.get("parsed"),
                "created": rep.get("created"), "skipped_dup": rep.get("skipped_dup"),
                "table_count": h.db.count(), "unique_job_id": len(set(jids)),
                "dup": h.db.dup_job_ids()})
    finally:
        h.close()


# --- J8 401 中途触发 ---------------------------------------------------- #
def j8_401_refresh():
    h = Harness()
    try:
        d = _fresh_data_dir()
        h.db.token_401_once = True
        code, out, exc = h.run_main(d)
        rep = json.loads(out) if out.strip().startswith("{") else {}
        ok = (code == 0 and exc is None and h.db.count() == 19
              and not h.db.dup_job_ids() and h.db.token_calls >= 2)
        record("J8 401 中途触发 -> 刷新 token 后成功 19",
               "PASS" if ok else "FAIL",
               {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                "created": rep.get("created"), "token_calls": h.db.token_calls,
                "table_count": h.db.count(), "dup": h.db.dup_job_ids()})
    finally:
        h.close()


def j3c_500_after_commit_all_retries():
    """块1 每次都『提交后返回500』-> 重试耗尽(retries=3 共4次)-> 块1 被写 4 遍。
    演示最坏放大倍数：单个块重复 = 1 + retries 次。"""
    h = Harness()
    try:
        d = _fresh_data_dir()
        # 前 4 次 create 全部 after-commit-500（初始+3 重试），第 5 次(块2)正常
        h.db.create_rules = {1: "500_after_commit", 2: "500_after_commit",
                             3: "500_after_commit", 4: "500_after_commit"}
        code, out, exc = h.run_main(d)
        jids = h.db.all_job_ids()
        dups = h.db.dup_job_ids()
        maxmult = max(dups.values()) if dups else 0
        record("J3c 块1 持续500-after-commit -> 重试耗尽 -> 块1 写4遍(最坏放大)",
               "FAIL" if dups else "PASS",
               {"exit_code": code, "exc": type(exc).__name__ if exc else None,
                "create_calls": h.db.create_calls,
                "table_count": h.db.count(), "dup_job_ids_count": len(dups),
                "max_dup_multiplicity": maxmult,
                "note": "retries=3 -> 同一 POST 最多发 4 次；每次都 after-commit -> 块1 10 条各写 4 遍"})
    finally:
        h.close()


def j3d_503_after_commit():
    """块2 提交后返回 503（同属 RETRY_STATUS）-> 块2 被重投 -> 重复。"""
    h = Harness()
    try:
        d = _fresh_data_dir()
        h.db.create_rules = {2: "503_after_commit"}
        code, out, exc = h.run_main(d)
        dups = h.db.dup_job_ids()
        record("J3d 块2 提交后返回503 -> 客户端重试 -> 重复写入?",
               "FAIL" if dups else "PASS",
               {"exit_code": code, "create_calls": h.db.create_calls,
                "table_count": h.db.count(), "dup_job_ids_count": len(dups),
                "dup_sample_ids": list(dups.keys())[:5],
                "note": "503 与 500 同在 RETRY_STATUS 集合，触发同样的非幂等重投"})
    finally:
        h.close()


SCENARIOS = [j1_baseline, j2_block2_drop_before_commit, j3a_drop_after_commit,
             j3b_500_after_commit, j3c_500_after_commit_all_retries, j3d_503_after_commit,
             j4a_uploadinfo_fail_by_name, j4b_oss_put_fail_by_index,
             j5_death_before_create, j6_readback_list_fail, j7_batch_dup, j8_401_refresh]


def main():
    for fn in SCENARIOS:
        try:
            fn()
        except Exception:
            record(fn.__name__, "ERROR", {"traceback": traceback.format_exc()})
    print("\n" + "=" * 72)
    print("汇总：")
    for r in RESULTS:
        print("  %-6s %s" % (r["status"], r["scenario"]))
    json.dump(RESULTS, open("/tmp/crash_job_results.json", "w"),
              ensure_ascii=False, indent=2, default=str)
    print("\n结果 JSON: /tmp/crash_job_results.json")


if __name__ == "__main__":
    main()
