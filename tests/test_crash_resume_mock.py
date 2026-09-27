#!/usr/bin/env python3
"""崩溃安全故障注入测试（阶段一：确定性 mock，不触真网）。

验证「网络中断 / 进程掉线」下简历入库的数据完整性：
  R1 基线           R2 附件失败        R3 create 前进程死亡
  R4 create 块2 未提交断连  R5 【重点】create 已提交但响应丢失 → 重试重复写
  R6 回读 list 失败  R7 backfill 重复 R4/R5  R8 401 刷新 sanity

mock: ThreadingHTTPServer + 内存表（按 sheet 隔离，支持分页/删除/故障规则）。
monkeypatch: notable.API / notable.CACHE / notable.time(sleep 加速)。
不修改任何生产代码。

真实简历夹具目录经环境变量注入（不写死本机路径）：
    JA_TEST_RESUME_DIR=/path/to/AI简历 python3 -m unittest discover -s tests
未设置或目录不存在时优雅 skip（夹具需含 31 份简历：28 可解析 + 3 扫描件）。
backfill 补录夹具（3 条含 _file + 手机号）在本文件内联生成到临时目录，
不依赖任何外部文件。
"""

import contextlib
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared"))
sys.path.insert(0, os.path.join(ROOT, "skills", "resume-intake", "scripts"))

import notable as notable_mod                    # noqa: E402
from notable import Notable                      # noqa: E402
import upload_resumes as ur                      # noqa: E402
import preflight as preflight_mod                # noqa: E402  (upload_resumes 已把其加入 sys.path)

# 真实简历夹具目录：仅经环境变量注入，缺失则 skip（禁止硬编码本机绝对路径）
DATA_DIR = os.environ.get("JA_TEST_RESUME_DIR", "")
EXPECTED_TOTAL = 31          # 标准夹具：31 份简历
EXPECTED_CREATED = 28        # 其中 28 份可解析入库，3 份扫描件进 needs_ocr
CHUNK = 10                   # create_records 分块大小（与 notable.py 一致）
BACKFILL_N = 3               # 内联 backfill 夹具条数
PHONE_CN = "手机号"
MD5_CN = "附件内容MD5"
ATT_CN = "简历附件"
SRC_CN = "原件本地路径"        # config.fields.resume.source_file
REFINED_CN = "AI精析时间"      # config.fields.resume.ai_refined_at

# 内联 backfill 夹具（原外部 ocr.json 的等价内容；_filename 指向 setUp 生成的临时真实文件）
OCR_FIXTURE = [
    {
        "name": "邹文飞",
        "phone": "18202532607",
        "email": "930282610@qq.com",
        "education": "本科",
        "school": "江西南昌航空大学",
        "major": "光伏材料应用技术",
        "years_experience": 10,
        "expected_position": "暖通高级工程师",
        "expected_salary": "10000元/月",
        "skills": ["暖通系统", "中央空调", "空压机", "制冷机组", "PVC管道", "设备维护"],
        "_filename": "暖通-邹文飞.pdf",
    },
    {
        "name": "张震宇",
        "phone": "15911360409",
        "email": "369020211@qq.com",
        "education": "硕士",
        "school": "昆明理工大学",
        "major": "材料工程",
        "years_experience": 1,
        "expected_position": "工艺工程师",
        "skills": ["UG", "ABAQUS", "SolidWorks", "Origin", "材料表征", "热镀铝锌工艺"],
        "_filename": "简历-工艺工程师.png",
    },
    {
        "name": "訾金保",
        "phone": "18272866223",
        "email": "1216222547@qq.com",
        "education": "本科",
        "school": "黄河科技学院",
        "major": "机械设计制造及自动化",
        "years_experience": 8,
        "expected_position": "设备高级工程师",
        "expected_salary": "18k-20k",
        "skills": ["镀膜设备维护", "PVD镀膜", "罗博特科自动化", "设备改造", "良率提升"],
        "_filename": "訾金保-电池设备.pdf",
    },
]


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
        self._tmpdirs = []
        self.state = MockState()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        PORT = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        self._old = (notable_mod.API, notable_mod.CACHE, notable_mod.time,
                     notable_mod._sleep, preflight_mod._sleep, ur.Notable)
        notable_mod.API = "http://127.0.0.1:%d" % PORT
        cache_dir = tempfile.mkdtemp(prefix="crash_mock_cache_")
        self._tmpdirs.append(cache_dir)
        self.cache_tmp = os.path.join(cache_dir, "token_cache.json")
        notable_mod.CACHE = self.cache_tmp
        preflight_mod._sleep = lambda *a, **k: None  # 整点峰值规避不真等

        slept = []
        self.slept = slept

        class FastTime:
            @staticmethod
            def sleep(s):
                slept.append(s)

            def __getattr__(self, n):
                return getattr(_real_time, n)

        notable_mod.time = FastTime()
        notable_mod._sleep = lambda *a, **k: None   # call() 退避走模块级 _sleep

        # 假凭证走环境变量，避免依赖本机 .secrets.json（mock server 无条件发 token）
        self._old_env = {k: os.environ.get(k)
                         for k in ("DINGTALK_APP_KEY", "DINGTALK_APP_SECRET")}
        os.environ["DINGTALK_APP_KEY"] = "crash-resume-test-key"
        os.environ["DINGTALK_APP_SECRET"] = "crash-resume-test-secret"

    def tearDown(self):
        notable_mod.API, notable_mod.CACHE, notable_mod.time, notable_mod._sleep, \
            preflight_mod._sleep, ur.Notable = self._old
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        self.srv.shutdown()
        self.srv.server_close()

    # ── 驱动器 ──────────────────────────────────────────
    def run_main(self, argv, notable_cls=None, stdin_text=None):
        """在进程内跑 upload_resumes.main()，返回 (exit_code, report_dict|None, exc)。
        stdin_text 非空时替换 sys.stdin（供 --backfill - 走管道读 payload 的用例）。"""
        old_notable = ur.Notable
        if notable_cls is not None:
            ur.Notable = notable_cls
        buf = io.StringIO()
        old_argv = sys.argv
        old_stderr = sys.stderr
        old_stdin = sys.stdin
        sys.argv = argv
        sys.stderr = io.StringIO()     # 吃掉 preflight 摘要，保持测试输出干净
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
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
            sys.stderr = old_stderr
            sys.stdin = old_stdin
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
        if not DATA_DIR or not os.path.isdir(DATA_DIR):
            self.skipTest("需设置 JA_TEST_RESUME_DIR 指向简历夹具目录（31 份简历）")
        from extract import SUPPORTED_EXTS
        n = len([f for f in os.listdir(DATA_DIR)
                 if os.path.splitext(f)[1].lower() in SUPPORTED_EXTS])
        if n != EXPECTED_TOTAL:
            self.skipTest("JA_TEST_RESUME_DIR 需含 %d 份简历夹具（实际 %d 份），场景计数不成立"
                          % (EXPECTED_TOTAL, n))
        return self.run_main(["upload_resumes.py", DATA_DIR], notable_cls)

    def _ocr_records(self):
        """backfill 夹具内联生成：临时目录放 3 个真实小文件，返回 records 列表。"""
        d = tempfile.mkdtemp(prefix="crash_mock_ocr_")
        self._tmpdirs.append(d)
        records = []
        for i, rec in enumerate(OCR_FIXTURE):
            row = {k: v for k, v in rec.items() if k != "_filename"}
            path = os.path.join(d, rec["_filename"])
            with open(path, "wb") as f:      # 3 份内容互异的小文件（md5 去重需要）
                f.write(("ocr-scan-fixture-%d\n" % i).encode())
            row["_file"] = path
            records.append(row)
        return records

    def run_backfill(self, notable_cls=None):
        records = self._ocr_records()
        ocr_json = os.path.join(self._tmpdirs[-1], "ocr.json")
        with open(ocr_json, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
        return self.run_main(["upload_resumes.py", "--backfill", ocr_json], notable_cls)

    def run_backfill_stdin(self, notable_cls=None):
        """同一 payload 经 stdin 喂入（--backfill -），验证与文件路径等价。"""
        payload = json.dumps(self._ocr_records(), ensure_ascii=False)
        return self.run_main(["upload_resumes.py", "--backfill", "-"],
                             notable_cls, stdin_text=payload)


# ─────────────────────────── 场景 ───────────────────────────

class TestR1Baseline(CrashTestBase):
    def test_baseline_then_idempotent_rerun(self):
        code, rep, exc = self.run_batch()
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], EXPECTED_CREATED)
        self.assertEqual(rep["total"], EXPECTED_TOTAL)
        self.assertEqual(len(rep["needs_ocr"]), EXPECTED_TOTAL - EXPECTED_CREATED)
        self.assertEqual(rep["readback_missing"], [])
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
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
        self.assertEqual(len(rep2["skipped_dup"]), EXPECTED_CREATED)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR2AttachmentFail(CrashTestBase):
    def _rerun_clean(self):
        self.state.fail_upload_nth = 0
        self.state.fail_put_nth = 0
        self.state.poisoned_names.clear()
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], 1, rep)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_uploadinfos_poisoned(self):
        self.state.fail_upload_nth = 3          # 第 3 个文件的 uploadInfos 永久 500
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], EXPECTED_CREATED - 1)
        self.assertEqual(len(rep["failed"]), 1)
        # 失败条不写表（不变量①）
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED - 1)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        self._rerun_clean()

    def test_oss_put_poisoned(self):
        self.state.fail_put_nth = 2             # 第 2 个文件的 OSS PUT 永久 500
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], EXPECTED_CREATED - 1)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED - 1)
        self._rerun_clean()


class TestR3DeathBeforeCreate(CrashTestBase):
    def test_kill_between_attachment_and_create(self):
        class Dying(Notable):
            def create_records(self, table, rows):
                raise SystemExit(137)           # 模拟 SIGKILL：附件已传、POST 未发
        code, rep, exc = self.run_batch(notable_cls=Dying)
        self.assertEqual(code, 137)
        self.assertEqual(self.state.put_n, EXPECTED_CREATED)   # 附件已全部上传
        self.assertEqual(self.state.create_n, 0)        # 没有任何 create POST
        self.assertEqual(self.state.count(self.SHEET), 0)   # 表中 0 条（不变量①）
        # 重跑完整恢复
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], EXPECTED_CREATED)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
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
        self.assertEqual(self.state.count(self.SHEET), CHUNK)   # 只有块1
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：跳过已入库 CHUNK 条，补其余
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], EXPECTED_CREATED - CHUNK)
        self.assertEqual(len(rep2["skipped_dup"]), CHUNK)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_chunk2_500_before_commit_exhausted(self):
        # 500 属 RETRY_STATUS：非幂等写禁盲重试 → NotableError → exit 1 + error JSON
        self.state.create_rules = {i: "fail500_before_commit" for i in (2, 3, 4, 5)}
        code, rep, exc = self.run_batch()
        self.assertEqual(code, 1)
        self.assertIsNotNone(rep)
        self.assertIn("error", rep)
        self.assertEqual(self.state.count(self.SHEET), CHUNK)
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], EXPECTED_CREATED - CHUNK)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
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
        self.assertEqual(self.state.count(self.SHEET), CHUNK * 2, "块1+块2 应已提交且各一次")
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：补齐剩余，总数 EXPECTED_CREATED，无重复
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], EXPECTED_CREATED - CHUNK * 2)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
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
        self.assertEqual(self.state.count(self.SHEET), CHUNK * 2)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑补齐且无重复残留
        self.state.create_rules = {}
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], EXPECTED_CREATED - CHUNK * 2)
        self.assertEqual(rep2.get("duplicates_removed"), 0)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR6ReadbackListFail(CrashTestBase):
    def test_readback_failure_then_rerun(self):
        self.state.fail_list_phone_only = True     # 回读 list 永久 500
        code, rep, exc = self.run_batch()
        # 记录实际形态：回读异常是否被捕获？
        shape = dict(code=code, exc=type(exc).__name__ if exc else None,
                     msg=str(exc)[:120] if exc else None, report=rep)
        print("\n[R6] %s" % json.dumps(shape, ensure_ascii=False, default=str))
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)   # 数据已提交
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 重跑：created=0，表完好
        self.state.fail_list_phone_only = False
        code2, rep2, _ = self.run_batch()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})
        # 首次运行应当以非 0 退出（数据是否可发现异常）——记录断言
        self.assertTrue(exc is not None or code != 0,
                        "回读失败但脚本正常退出，异常被吞")


class TestR7Backfill(CrashTestBase):
    """扫描件补录：夹具在本文件内联（run_backfill 生成临时 _file + records.json）。"""

    def test_backfill_baseline_idempotent(self):
        code, rep, _ = self.run_backfill()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], BACKFILL_N)
        self.assertEqual(rep["failed"], [])
        recs = self.state.all_records(self.SHEET)
        self.assertEqual(len(recs), BACKFILL_N)
        for r in recs:
            self.assertTrue(r["fields"].get(ATT_CN), r["id"])   # 原件附件在表
            self.assertTrue(r["fields"].get(MD5_CN), r["id"])   # attach_md5 写入
        code2, rep2, _ = self.run_backfill()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(len(rep2["skipped_dup"]), BACKFILL_N)
        self.assertEqual(self.state.count(self.SHEET), BACKFILL_N)

    def test_backfill_drop_before_commit(self):
        self.state.create_rules = {1: "drop_before_commit"}
        code, rep, exc = self.run_backfill()
        self.assertTrue(exc is not None or code != 0)
        self.assertEqual(self.state.count(self.SHEET), 0)
        self.state.create_rules = {}
        code2, rep2, _ = self.run_backfill()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], BACKFILL_N)
        self.assertEqual(self.state.count(self.SHEET), BACKFILL_N)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})

    def test_backfill_500_after_commit_duplicates(self):
        """backfill 单块(BACKFILL_N 条)提交后 500 → 禁盲重试 → 无重复。"""
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
        self.assertEqual(n, BACKFILL_N)
        self.assertEqual(dupes2, {}, "重跑后重复残留: %s" % dupes2)
        self.assertEqual(n2, BACKFILL_N)


class TestR8Auth401(CrashTestBase):
    def test_401_refresh_and_succeed(self):
        self.state.fail401_once = True
        code, rep, _ = self.run_backfill()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], BACKFILL_N)
        self.assertEqual(self.state.count(self.SHEET), BACKFILL_N)
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
        self.assertEqual(rep["created"], EXPECTED_CREATED)
        self.assertEqual(rep.get("duplicates_removed"), 1)
        # EXPECTED_CREATED + 保留 1 条种子
        self.assertEqual(self.state.count(self.SHEET), EXPECTED_CREATED + 1)
        self.assertEqual(self.state.phone_dupes(self.SHEET), {})


class TestR10RoundTripFields(CrashTestBase):
    """回合瘦身配套：报告新增 refine_fire_at / table_total 两字段 + --backfill - 走 stdin。

    这两个字段的目的是消灭 agent 的两次验证往返（date 算偏移、query.py 复核总数），
    故必须锁死其语义：fire_at 由唯一常量 REFINE_DELAY_S 派生、补录模式不输出（手析不入队）、
    table_total 恒等于表内真实条数。"""

    def test_fire_at_derives_from_single_source_constant(self):
        """refine_fire_at = 当前 + REFINE_DELAY_S（UTC ISO8601）。延迟秒数不许有第二份副本。"""
        import datetime
        before = _real_time.time()
        iso = ur._fire_at()
        after = _real_time.time()
        self.assertRegex(iso, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        fire = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        lo = before + ur.REFINE_DELAY_S - 2      # strftime 截断到秒，留 2 秒容差
        hi = after + ur.REFINE_DELAY_S + 2
        self.assertTrue(lo <= fire <= hi, "fire_at=%s 不在 [%s, %s]" % (iso, lo, hi))

    def test_backfill_queues_via_source_file(self):
        """补录口径（扫描件交后台读图）：写 source_file、不打 ai_refined_at → 照常入队，
        故 table_total 有值且输出 refine_fire_at（补录后同样要注册消费任务）。"""
        code, rep, _ = self.run_backfill()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], BACKFILL_N)
        self.assertEqual(rep["table_total"], BACKFILL_N)
        self.assertEqual(rep["refine_queued"], BACKFILL_N)   # 原件在盘 → 入队读图
        self.assertIn("refine_fire_at", rep)
        for r in self.state.all_records(self.SHEET):
            self.assertTrue(r["fields"].get(SRC_CN), r["id"])   # 原件绝对路径已入列
            self.assertFalse(r["fields"].get(REFINED_CN), r["id"])  # 未打出队标记

    def test_backfill_stdin_equivalent_to_file(self):
        """--backfill - 从 stdin 读：与文件路径走同一套校验/去重/回读，产物完全一致。"""
        code, rep, exc = self.run_backfill_stdin()
        self.assertIsNone(exc)
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], BACKFILL_N)
        self.assertEqual(rep["failed"], [])
        self.assertEqual(rep["table_total"], BACKFILL_N)
        recs = self.state.all_records(self.SHEET)
        self.assertEqual(len(recs), BACKFILL_N)
        for r in recs:
            self.assertTrue(r["fields"].get(ATT_CN), r["id"])
            self.assertTrue(r["fields"].get(MD5_CN), r["id"])
        # 幂等：stdin 重跑仍 0 新增
        code2, rep2, _ = self.run_backfill_stdin()
        self.assertEqual(code2, 0)
        self.assertEqual(rep2["created"], 0)
        self.assertEqual(rep2["table_total"], BACKFILL_N)

    def test_batch_emits_fire_at_when_queue_nonempty(self):
        """批量入库后队列非空 → 输出 refine_fire_at；table_total 与表内条数一致。"""
        if not DATA_DIR or not os.path.isdir(DATA_DIR):
            self.skipTest("需设置 JA_TEST_RESUME_DIR 指向简历夹具目录（31 份简历）")
        code, rep, _ = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(rep["created"], EXPECTED_CREATED)
        self.assertGreater(rep["refine_queued"], 0)
        self.assertIn("refine_fire_at", rep)
        self.assertEqual(rep["table_total"], EXPECTED_CREATED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
