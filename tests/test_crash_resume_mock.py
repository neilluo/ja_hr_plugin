#!/usr/bin/env python3
"""崩溃安全故障注入测试（阶段一：确定性 mock，不触真网）。

验证「网络中断 / 进程掉线」下简历入库的数据完整性：
  R1 基线           R2 附件失败        R3 create 前进程死亡
  R4 create 块2 未提交断连  R5 【重点】create 已提交但响应丢失 → 重试重复写
  R6 回读 list 失败  R7 backfill 重复 R4/R5  R8 401 刷新 sanity

mock: ThreadingHTTPServer + 内存表（按 sheet 隔离，支持分页/删除/故障规则）。
monkeypatch: notable.API / notable.CACHE / notable.time(sleep 加速)。
不修改任何生产代码。
"""

import contextlib
import io
import json
import os
import re
import sys
import threading
import time as _real_time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import notable as notable_mod                    # noqa: E402
from notable import Notable                      # noqa: E402
import upload_resumes as ur                      # noqa: E402

DATA_DIR = "/Users/neil/Desktop/qwenworklearn/jahrplugin/data/AI简历"
OCR_JSON = "/tmp/ocr.json"
PHONE_CN = "手机号"
MD5_CN = "附件内容MD5"
ATT_CN = "简历附件"


# ─────────────────────────── mock 服务端 ───────────────────────────

class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.tables = {}            # sheet_id -> [ {"id","fields"} ]
        self.rec_n = 0
        self.token_hits = 0
        self.create_n = 0           # create POST 计数（含重试）
        self.put_n = 0
        self.create_rules = {}      # {create_n: action}
        # action: ok | drop_after_commit | fail500_after_commit
        #       | drop_before_commit | fail500_before_commit
        self.fail_upload_nth = 0    # 第 N 个「不同 resourceName」的 uploadInfos 永久 500
        self.fail_put_nth = 0       # 第 N 个不同 name 的 OSS PUT 永久 500
        self.fail_list_phone_only = False   # 只让「仅查手机号」的 list（回读）失败
        self.fail401_once = False
        self.page_cap = 10          # 强制分页
        self._names = []            # uploadInfos 到达的不同文件名顺序
        self.poisoned_names = set()
        self.rid_to_name = {}       # rid(URL安全) -> 原始文件名

    def note_name(self, name):
        """登记上传文件名，返回 (rid, 是否首次见到)。rid 为 URL 安全标识。"""
        with self.lock:
            if name not in self._names:
                self._names.append(name)
                idx = len(self._names)
                if self.fail_upload_nth and idx == self.fail_upload_nth:
                    self.poisoned_names.add("q:" + name)
                if self.fail_put_nth and idx == self.fail_put_nth:
                    self.poisoned_names.add("p:" + name)
        rid = "r%03d" % (self._names.index(name) + 1)
        self.rid_to_name[rid] = name
        return rid

    def upload_poisoned(self, name):
        return ("q:" + name) in self.poisoned_names

    def put_poisoned(self, name):
        return ("p:" + name) in self.poisoned_names

    def commit(self, sheet, records):
        ids = []
        with self.lock:
            tbl = self.tables.setdefault(sheet, [])
            for rec in records:
                self.rec_n += 1
                rid = "rec%d" % self.rec_n
                tbl.append({"id": rid, "fields": dict(rec.get("fields", {}))})
                ids.append(rid)
        return ids

    def count(self, sheet):
        with self.lock:
            return len(self.tables.get(sheet, []))

    def phone_dupes(self, sheet):
        from collections import Counter
        with self.lock:
            c = Counter(r["fields"].get(PHONE_CN) for r in self.tables.get(sheet, []))
        return {p: n for p, n in c.items() if p and n > 1}

    def all_records(self, sheet):
        with self.lock:
            return [dict(r) for r in self.tables.get(sheet, [])]


def make_handler(state):
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

        def _drop(self):
            """读完 body 后直接断连，不发任何响应（模拟响应在网络上丢失）。"""
            self.close_connection = True

        def _sheet(self):
            m = re.search(r"/sheets/([^/]+)/records", self.path)
            return m.group(1) if m else "?"

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            path = self.path.split("?")[0]
            if "/oauth2/accessToken" in path:
                with state.lock:
                    state.token_hits += 1
                return self._json({"accessToken": "tk%d" % state.token_hits,
                                   "expireIn": 7200})
            if "/uploadInfos/query" in path:
                name = json.loads(raw).get("resourceName", "?")
                rid = state.note_name(name)
                if state.upload_poisoned(name):
                    return self._json({"code": "internalError",
                                       "message": "injected"}, 500)
                return self._json({"result": {
                    "uploadUrl": "http://127.0.0.1:%d/oss/%s" % (PORT, rid),
                    "resourceId": rid, "resourceUrl": "/res/" + rid}})
            if path.endswith("/records/list"):
                sheet = self._sheet()
                body = json.loads(raw or b"{}")
                names = body.get("fieldIdOrNames") or []
                if state.fail_list_phone_only and names == [PHONE_CN]:
                    return self._json({"code": "internalError",
                                       "message": "injected readback fail"}, 500)
                off = int(body.get("nextToken") or 0)
                cap = min(int(body.get("maxResults", 100)), state.page_cap)
                with state.lock:
                    tbl = state.tables.get(sheet, [])
                    page = tbl[off:off + cap]
                    more = off + cap < len(tbl)
                    recs = [{"id": r["id"],
                             "fields": ({k: v for k, v in r["fields"].items()
                                         if k in names} if names else dict(r["fields"]))}
                            for r in page]
                out = {"records": recs, "hasMore": more}
                if more:
                    out["nextToken"] = str(off + cap)
                return self._json(out)
            if path.endswith("/records/delete"):
                sheet = self._sheet()
                ids = set(json.loads(raw).get("recordIds", []))
                with state.lock:
                    tbl = state.tables.get(sheet, [])
                    state.tables[sheet] = [r for r in tbl if r["id"] not in ids]
                return self._json({"success": True})
            if path.endswith("/records"):          # create
                with state.lock:
                    state.create_n += 1
                    n = state.create_n
                    if state.fail401_once:
                        state.fail401_once = False
                        return self._json({"code": "Forbidden.AccessDenied",
                                           "message": "injected 401"}, 401)
                    action = state.create_rules.get(n, "ok")
                records = json.loads(raw).get("records", [])
                if action == "drop_before_commit":
                    return self._drop()
                if action == "fail500_before_commit":
                    return self._json({"code": "internalError",
                                       "message": "injected"}, 500)
                ids = state.commit(self._sheet(), records)   # 先提交
                if action == "drop_after_commit":
                    return self._drop()                       # 响应丢失
                if action == "fail500_after_commit":
                    return self._json({"code": "internalError",
                                       "message": "injected after commit"}, 500)
                return self._json({"value": [{"id": i} for i in ids]})
            return self._json({"message": "unknown path " + path}, 404)

        def do_PUT(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            with state.lock:
                state.put_n += 1
            rid = self.path.split("/oss/", 1)[-1]
            name = state.rid_to_name.get(rid, rid)
            if state.put_poisoned(name):
                return self._json({"code": "internalError", "message": "injected"}, 500)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    return H


PORT = 0


class CrashTestBase(unittest.TestCase):
    SHEET = "ohY4Dp6"   # config.json resume 表 table_id（mock 按 sheet 隔离）

    def setUp(self):
        global PORT
        self.state = MockState()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        PORT = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        self._old = (notable_mod.API, notable_mod.CACHE, notable_mod.time, ur.Notable)
        notable_mod.API = "http://127.0.0.1:%d" % PORT
        self.cache_tmp = os.path.join("/tmp", "crash_mock_token_cache.json")
        notable_mod.CACHE = self.cache_tmp

        slept = []
        self.slept = slept

        class FastTime:
            @staticmethod
            def sleep(s):
                slept.append(s)

            def __getattr__(self, n):
                return getattr(_real_time, n)

        notable_mod.time = FastTime()

    def tearDown(self):
        notable_mod.API, notable_mod.CACHE, notable_mod.time, ur.Notable = self._old
        if os.path.exists(self.cache_tmp):
            os.remove(self.cache_tmp)
        self.srv.shutdown()

    # ── 驱动器 ──────────────────────────────────────────
    def run_main(self, argv, notable_cls=None):
        """在进程内跑 upload_resumes.main()，返回 (exit_code, report_dict|None, exc)。"""
        old_notable = ur.Notable
        if notable_cls is not None:
            ur.Notable = notable_cls
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = argv
        code, exc = 0, None
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    ur.main()
                except SystemExit as e:
                    code = e.code if e.code is not None else 0
        except Exception as e:      # noqa: BLE001  未捕获异常（如 R6）
            exc = e
        finally:
            sys.argv = old_argv
            ur.Notable = old_notable
        out = buf.getvalue().strip()
        report = None
        if out:
            try:
                report = json.loads(out)
            except ValueError:
                m = re.search(r"\{.*\}", out, re.S)
                if m:
                    try:
                        report = json.loads(m.group(0))
                    except ValueError:
                        pass
        return code, report, exc

    def run_batch(self, notable_cls=None):
        return self.run_main(["upload_resumes.py", DATA_DIR], notable_cls)

    def run_backfill(self, notable_cls=None):
        return self.run_main(["upload_resumes.py", "--backfill", OCR_JSON], notable_cls)


# ─────────────────────────── 场景 ───────────────────────────

class TestR1Baseline(CrashTestBase):
    def test_baseline_then_idempotent_rerun(self):
        code, rep, exc = self.run_batch()
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 28)
        self.assertEqual(len(rep["needs_ocr"]), 3)
        self.assertEqual(rep["readback_missing"], [])
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 不变量①：每条都有附件 cell 与 attach_md5
        for r in self.state.all_records(self.SHEET):
            att = r["fields"].get(ATT_CN)
            self.assertTrue(att and att[0].get("resourceId"), r["id"])
            self.assertTrue(r["fields"].get(MD5_CN), r["id"])
        # 重跑幂等
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(len(rep2["skipped_dup"]), 28)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR2AttachmentFail(CrashTestBase):
    def _rerun_clean(self):
        self.state.fail_upload_nth = 0
        self.state.fail_put_nth = 0
        self.state.poisoned_names.clear()
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 1, rep)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_uploadinfos_poisoned(self):
        self.state.fail_upload_nth = 3          # 第 3 个文件的 uploadInfos 永久 500
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 27)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertEqual(self.state.count(self.SHEET), 27)   # 失败条不写表（不变量①）
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        self._rerun_clean()

    def test_oss_put_poisoned(self):
        self.state.fail_put_nth = 2             # 第 2 个文件的 OSS PUT 永久 500
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 27)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertEqual(self.state.count(self.SHEET), 27)
        self._rerun_clean()


class TestR3DeathBeforeCreate(CrashTestBase):
    def test_kill_between_attachment_and_create(self):
        class Dying(Notable):
            def create_records(self, table, rows):
                raise SystemExit(137)           # 模拟 SIGKILL：附件已传、POST 未发
        code, rep, exc = self.run_batch(notable_cls=Dying)
        self.assertEqual(code, 137)
        self.assertEqual(self.state.put_n, 28)          # 28 个附件已上传
        self.assertEqual(self.state.create_n, 0)        # 没有任何 create POST
        self.assertEqual(self.state.count(self.SHEET), 0)   # 表中 0 条（不变量①）
        # 重跑完整恢复
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 28)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR4CreateChunk2LostBeforeCommit(CrashTestBase):
    def test_chunk2_drop_before_commit(self):
        # 块1(create_n=1) ok；块2「服务端未提交先断连」（FIN，无响应，不重试）
        self.state.create_rules = {2: "drop_before_commit"}
        code, rep, exc = self.run_batch()
        res = dict(code=code, exc=type(exc).__name__ if exc else None,
                   count=self.state.count(self.SHEET))
        print("\n[R4 FIN-before-commit] %s" % json.dumps(res, default=str))
        self.assertTrue(exc is not None or code != 0, "块2 失败但脚本静默成功")
        self.assertEqual(self.state.count(self.SHEET), 10)   # 只有块1
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：跳过已入库 10 条，补 18 条
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 18)
        self.assertEqual(len(rep2["skipped_dup"]), 10)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_chunk2_500_before_commit_exhausted(self):
        # 500 属 RETRY_STATUS：4 次全 500（未提交）→ NotableError → exit 1 + error JSON
        self.state.create_rules = {i: "fail500_before_commit" for i in (2, 3, 4, 5)}
        code, rep, exc = self.run_batch()
        self.assertEqual(code, 1)
        self.assertIsNotNone(rep)
        self.assertIn("error", rep)
        self.assertEqual(self.state.count(self.SHEET), 10)
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 18)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR5CreateRetryDuplication(CrashTestBase):
    """【重点】POST 不幂等 + 重试 = 重复记录？（修复后语义）

    修复前：500/503-after-commit 命中 RETRY_STATUS 盲重试 → 整块写两遍且静默 exit 0；
            FIN/RST/超时的 RemoteDisconnected 不被捕获 → 裸 traceback。
    修复后：create 标记 idempotent=False，5xx/429 与连接层故障均不重试、包成
            NotableError → exit 1 + error JSON；块只提交一次，无重复；
            残留重复由 _finalize 的写后查重自愈清理。
    """

    def test_drop_after_commit_wrapped_error_no_dupes(self):
        """FIN 断连（响应丢失）：异常被 NotableError 包裹、非幂等写不重试、无重复。"""
        self.state.create_rules = {2: "drop_after_commit"}   # 块2 已提交、响应丢失
        code, rep, exc = self.run_batch()
        res = dict(code=code, exc=type(exc).__name__ if exc else None,
                   report_error=rep and rep.get("error"),
                   create_posts=self.state.create_n,
                   count=self.state.count(self.SHEET))
        print("\n[R5 FIN-after-commit] %s" % json.dumps(res, ensure_ascii=False, default=str))
        self.assertIsNone(exc, "裸异常应已被 NotableError 包裹")
        self.assertEqual(code, 1)
        self.assertIn("error", rep)
        self.assertEqual(self.state.create_n, 2, "发生了重试 → 可能重复提交")
        self.assertEqual(self.state.count(self.SHEET), 20, "块1+块2 应已提交且各一次")
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：补 8 条，总 28，无重复
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 8)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_500_after_commit_no_retry_no_dupes(self):
        """500-after-commit：非幂等写禁盲重试 → 块只提交一次，无重复；重跑补齐。"""
        self.state.create_rules = {2: "fail500_after_commit"}
        code, rep, exc = self.run_batch()
        res = dict(code=code, exc=type(exc).__name__ if exc else None,
                   report_error=rep and rep.get("error"),
                   create_posts=self.state.create_n,
                   count=self.state.count(self.SHEET),
                   dupe_phones=len(self.state.phone_dupes(self.SHEET)))
        print("\n[R5 500-after-commit] %s" % json.dumps(res, ensure_ascii=False, default=str))
        self.assertIsNone(exc)
        self.assertEqual(code, 1, "写失败必须非 0 退出")
        self.assertIn("error", rep)
        self.assertEqual(self.state.create_n, 2, "块2 只应 POST 一次（禁盲重试）")
        self.assertEqual(self.state.count(self.SHEET), 20)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑补齐且无重复残留
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 8)
        self.assertEqual(rep2.get("duplicates_removed"), 0)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR6ReadbackListFail(CrashTestBase):
    def test_readback_failure_then_rerun(self):
        self.state.fail_list_phone_only = True     # 回读 list 永久 500
        code, rep, exc = self.run_batch()
        # 记录实际形态：回读异常是否被捕获？
        shape = dict(code=code, exc=type(exc).__name__ if exc else None,
                     msg=str(exc)[:120] if exc else None, report=rep)
        print("\n[R6] %s" % json.dumps(shape, ensure_ascii=False, default=str))
        self.assertEqual(self.state.count(self.SHEET), 28)      # 数据已提交
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：created=0，表完好
        self.state.fail_list_phone_only = False
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(self.state.count(self.SHEET), 28)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 首次运行应当以非 0 退出（数据是否可发现异常）——记录断言
        self.assertTrue(exc is not None or code != 0,
                        "回读失败但脚本正常退出，异常被吞")


class TestR7Backfill(CrashTestBase):
    def test_backfill_baseline_idempotent(self):
        code, rep, _ = self.run_backfill()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 3)
        recs = self.state.all_records(self.SHEET)
        self.assertEqual(len(recs), 3)
        for r in recs:
            self.assertTrue(r["fields"].get(ATT_CN), r["id"])   # 原件附件在表
            self.assertTrue(r["fields"].get(MD5_CN), r["id"])   # attach_md5 写入
        code2, rep2, _ = self.run_backfill()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(len(rep2["skipped_dup"]), 3)
        self.assertEqual(self.state.count(self.SHEET), 3)

    def test_backfill_drop_before_commit(self):
        self.state.create_rules = {1: "drop_before_commit"}
        code, rep, exc = self.run_backfill()
        self.assertTrue(exc is not None or code != 0)
        self.assertEqual(self.state.count(self.SHEET), 0)
        self.state.create_rules = {}
        code2, rep2, _ = self.run_backfill()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 3)
        self.assertEqual(self.state.count(self.SHEET), 3)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_backfill_500_after_commit_duplicates(self):
        """backfill 单块(3 条)提交后 500 → 重试 → 重复？"""
        self.state.create_rules = {1: "fail500_after_commit"}
        code, rep, exc = self.run_backfill()
        n = self.state.count(self.SHEET)
        dupes = self.state.phone_dupes(self.SHEET)
        res = dict(code=code, exc=type(exc).__name__ if exc else None,
                   count=n, dupe_phones=len(dupes))
        print("\n[R7 500-after-commit] %s" % json.dumps(res, default=str))
        self.state.create_rules = {}
        code2, rep2, _ = self.run_backfill()
        dupes2 = self.state.phone_dupes(self.SHEET)
        n2 = self.state.count(self.SHEET)
        print("[R7 500-after-commit rerun] count=%d dupes=%d" % (n2, len(dupes2)))
        self.assertEqual(dupes, {}, "backfill 重试产生重复: %s" % dupes)
        self.assertEqual(n, 3)
        self.assertEqual(dupes2, {}, "重跑后重复残留: %s" % dupes2)
        self.assertEqual(n2, 3)


class TestR8Auth401(CrashTestBase):
    def test_401_refresh_and_succeed(self):
        self.state.fail401_once = True
        code, rep, _ = self.run_backfill()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 3)
        self.assertEqual(self.state.count(self.SHEET), 3)
        self.assertGreaterEqual(self.state.token_hits, 2)   # 初始 1 次 + 401 刷新 1 次


class TestR9SelfHeal(CrashTestBase):
    """写后查重自愈：预置历史重复记录，跑一次批量即被清理。"""

    def test_preexisting_dupes_removed(self):
        self.state.tables[self.SHEET] = [
            {"id": "seed1", "fields": {PHONE_CN: "13900000001"}},
            {"id": "seed2", "fields": {PHONE_CN: "13900000001"}},
        ]
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 28)
        self.assertEqual(rep.get("duplicates_removed"), 1)
        self.assertEqual(self.state.count(self.SHEET), 29)   # 28 + 保留 1 条种子
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
