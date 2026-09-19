# -*- coding: utf-8 -*-
"""`dws` CLI 的最小封装层（零第三方 pip 依赖，只用标准库）。

提供两阶段模式（emit/replay）、调用计数、错误归类、三种响应信封剥壳。
emit 阶段收集命令到内部列表并返回模拟成功；agent 逐条用 Bash 工具执行；
replay 阶段从预加载的结果文件返回真实结果。Python 子进程无法调用 dws。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DwsError",
    "DwsCallCounter",
    "DwsClient",
    "unwrap",
    "classify_error",
    "write_json_file",
    "now_iso",
    "default_client",
    "global_stats",
    "HTTP_TIMEOUT",
    "MAX_RECORDS_PER_CALL",
    "MAX_QUERY_LIMIT",
    "MAX_RECORD_IDS_PER_CALL",
    "MAX_FIELDS_PER_CALL",
    "MAX_FIELD_IDS_PER_GET",
    "MODE_EMIT",
    "MODE_REPLAY",
]

#: 两阶段模式常量
# emit：不调 dws，收集命令到内部列表，返回模拟成功
# replay：不调 dws，从预加载的结果文件返回真实结果
# dws 是宿主代理 shim，Python 子进程调用只返回占位符 "pending host-side execution"，
# 永远拿不到真实结果，因此 dws 调用只能由 agent 通过 Bash 工具完成。
MODE_EMIT = "emit"               # 收集命令到内部列表，返回模拟成功
MODE_REPLAY = "replay"           # 从预加载的结果文件返回真实结果

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 传给 dws 的 --timeout（HTTP 请求超时，秒），用于 emit 模式下构建 argv
HTTP_TIMEOUT = 90

#: `record create/update/upsert` 单次上限（服务端硬限制）
MAX_RECORDS_PER_CALL = 100
#: `record query --limit` 单次上限
MAX_QUERY_LIMIT = 100
#: `record query --record-ids` 单次上限
MAX_RECORD_IDS_PER_CALL = 100
#: `field create --fields` / `table create --fields` 单次上限
MAX_FIELDS_PER_CALL = 15
#: `field get --field-ids` 单次上限
MAX_FIELD_IDS_PER_GET = 10

_MCP_TOOL_ERROR_RE = re.compile(r"\[MCP_TOOL_ERROR\]\s*(\{.*\})", re.S)

# 错误分类关键字（全部小写比较）
_KW_RATE_LIMIT = ("qpslimit", "90018", "rate limit", "ratelimit", "too many requests",
                  "429", "throttl")
_KW_AUTH = ("401", "403", "unauthorized", "forbidden", "permission", "accessdenied",
            "access_denied", "not authorized", "no_permission", "nopermission",
            "invalid token", "token expired", "无权限", "越权", "未登录", "not login")
_KW_TIMEOUT = ("timeout_error", "timeout", "timed out", "deadline exceeded", "超时")
_KW_NETWORK = ("network", "connection", "connect:", "eof", "i/o timeout", "dial tcp",
               "reset by peer", "broken pipe", "no such host", "temporarily unavailable",
               "socket", "unreachable", "网络")
_KW_SERVER = ("500", "502", "503", "504", "internalerror", "internal error",
              "serviceunavailable", "service unavailable", "system_error", "systemerror",
              "server error", "bad gateway", "gateway")
_KW_NOT_FOUND = ("not_found", "notfound", "not found", "does not exist", "no such",
                 "不存在", "已被删除")
_KW_INVALID = ("input_error", "invalid", "illegal", "bad request", "unsupported",
               "not supported", "not allowed", "duplicate", "参数", "校验失败",
               "must be", "required", "invalidargument")


def now_iso() -> str:
    """本地时区 ISO8601 时间串（秒级），用于产物时间戳。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def classify_error(message: str = "", code: Any = None) -> Tuple[str, bool]:
    """把 dws 的错误输出归类 → (category, retryable)。

    category ∈ rate_limit | auth | timeout | network | server | not_found | invalid | unknown
    网络类（timeout/network/server/rate_limit）可重试；权限类 auth **不重试**。

    注意顺序：QPS 限流的错误码是 `Forbidden.AccessDenied.QpsLimitForAppkeyAndApi`，
    字面含 "Forbidden" 看着像权限错误，实际是限流、退避后可重试 —— 所以 rate_limit 必须先判。
    """
    blob = " ".join(str(x) for x in (code, message) if x is not None).lower()
    if not blob.strip():
        return "unknown", False
    if any(k in blob for k in _KW_RATE_LIMIT):
        return "rate_limit", True
    if any(k in blob for k in _KW_AUTH):
        return "auth", False
    if any(k in blob for k in _KW_TIMEOUT):
        return "timeout", True
    if any(k in blob for k in _KW_NETWORK):
        return "network", True
    if any(k in blob for k in _KW_SERVER):
        return "server", True
    if any(k in blob for k in _KW_NOT_FOUND):
        return "not_found", False
    if any(k in blob for k in _KW_INVALID):
        return "invalid", False
    return "unknown", False


class DwsError(Exception):
    """dws 调用失败的统一异常。

    携带 `category` / `code` / `retryable` / `attempts`，上层可据此把失败项写进
    report 的 `failed` 与 `warnings`（失败可见，禁止静默丢弃）。
    """

    def __init__(self, message: str, *, category: str = "unknown", code: Any = None,
                 retryable: bool = False, argv: Optional[Sequence[str]] = None,
                 returncode: Optional[int] = None, stdout: str = "", stderr: str = "",
                 attempts: int = 1, elapsed_ms: int = 0):
        super().__init__(message)
        self.message = message
        self.category = category
        self.code = code
        self.retryable = retryable
        self.argv = list(argv) if argv else []
        self.returncode = returncode
        self.stdout = (stdout or "")[:2000]
        self.stderr = (stderr or "")[:2000]
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "category": self.category,
            "code": self.code,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        head = self.message or "(无错误消息)"
        return "[%s/%s] %s" % (self.category, self.code, head)


class DwsCallCounter:
    """线程安全的 dws 调用计数器。

    `calls` = 真实发生的调用次数（emit/replay 模式下不调 dws，但仍计数用于统计）；
    `logical` = 逻辑调用次数。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.logical = 0
        self.retries = 0
        self.failures = 0
        self.elapsed_ms = 0
        self.by_cmd: Dict[str, int] = {}
        self.by_category: Dict[str, int] = {}

    def record(self, cmd_key: str, elapsed_ms: int, ok: bool,
               attempts: int = 1, category: Optional[str] = None) -> None:
        with self._lock:
            self.calls += attempts
            self.logical += 1
            self.retries += max(0, attempts - 1)
            self.elapsed_ms += elapsed_ms
            self.by_cmd[cmd_key] = self.by_cmd.get(cmd_key, 0) + attempts
            if not ok:
                self.failures += 1
                key = category or "unknown"
                self.by_category[key] = self.by_category.get(key, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "dws_calls": self.calls,
                "dws_logical_calls": self.logical,
                "dws_retries": self.retries,
                "dws_failures": self.failures,
                "dws_elapsed_ms": self.elapsed_ms,
                "dws_by_cmd": dict(self.by_cmd),
                "dws_error_categories": dict(self.by_category),
            }

    def reset(self) -> None:
        with self._lock:
            self.calls = 0
            self.logical = 0
            self.retries = 0
            self.failures = 0
            self.elapsed_ms = 0
            self.by_cmd = {}
            self.by_category = {}


def unwrap(raw: Any) -> Tuple[bool, Any, Any, str, Dict[str, Any]]:
    """把 dws 的三种响应信封归一化 → (ok, data, code, message, hint)。

    - 信封 A（原子命令）：{"status":"success","success":true,"data":{...},"error":{},"summary":"..."}
    - 信封 B（`+` 命令，旧形态）：成功时直接就是业务 payload；失败时
      {"error":{"category","code","message"}}，其中 message 里内嵌
      `[MCP_TOOL_ERROR] {...}` 的内层 JSON（真正的 code / retryable 在里面）。
    - 信封 C（`+` 命令**双层信封**）：
      {"ok":true,"outcome":"success","data":{业务 payload}}。
      它没有 status/success/summary → 旧实现会把整个信封当 payload 返回。
      现在：`ok` 为 True 且带 `data`/`outcome` → 返回内层 `data`；
      `ok` 为 False → 按失败处理（从 error/outcome/data 里挖消息与 code）。
      兼容性：信封 A 优先（先判 status/success/summary + data），
      普通单层 payload（没有 ok 键）原样返回，现有调用方零影响。

    `hint` = {"retryable": bool|None, "type": str|None}，来自服务端内层错误的显式提示。
    """
    hint: Dict[str, Any] = {"retryable": None, "type": None}
    if not isinstance(raw, dict):
        return True, raw, None, "", hint

    def _dig_inner(msg: str) -> Optional[Dict[str, Any]]:
        m = _MCP_TOOL_ERROR_RE.search(msg or "")
        if not m:
            return None
        try:
            inner = json.loads(m.group(1))
        except (ValueError, TypeError):
            return None
        return inner if isinstance(inner, dict) else None

    err = raw.get("error")
    if isinstance(err, dict) and err:
        msg = str(err.get("message") or err.get("summary") or "")
        code = err.get("code")
        if code is None:
            code = err.get("category")
        inner = _dig_inner(msg)
        if inner:
            inner_err = inner.get("error")
            if isinstance(inner_err, dict) and inner_err:
                code = inner_err.get("code") or code
                msg = inner_err.get("message") or msg
                hint["retryable"] = inner_err.get("retryable")
                hint["type"] = inner_err.get("type")
        return False, None, code, msg or json.dumps(err, ensure_ascii=False), hint

    status = raw.get("status")
    if status == "error" or raw.get("success") is False:
        summary = str(raw.get("summary") or "")
        code, msg = None, summary
        inner = _dig_inner(summary)
        if inner:
            inner_err = inner.get("error") or {}
            code = inner_err.get("code")
            msg = inner_err.get("message") or summary
            hint["retryable"] = inner_err.get("retryable")
            hint["type"] = inner_err.get("type")
        return False, None, code, msg or "dws returned status=error", hint

    # 成功：信封 A 取 data；信封 B 整体就是 payload
    if "data" in raw and ("status" in raw or "success" in raw or "summary" in raw):
        return True, raw.get("data"), None, str(raw.get("summary") or ""), hint

    # 信封 C（`+` 命令双层信封 {"ok","outcome","data"}）：
    # 只有不带 status/success/summary（信封 A 已在上面处理）且显式带 ok 键时才剥壳，
    # 普通业务 payload 恰好带 "ok" 字段但没有 "outcome"/"data" 时不碰它。
    if "ok" in raw and ("outcome" in raw or "data" in raw) and \
            "status" not in raw and "success" not in raw and "summary" not in raw:
        if raw.get("ok") is False:
            inner_data = raw.get("data")
            code = None
            msg = ""
            if isinstance(inner_data, dict):
                inner_err = inner_data.get("error")
                if isinstance(inner_err, dict) and inner_err:
                    code = inner_err.get("code") or inner_err.get("category")
                    msg = str(inner_err.get("message") or "")
                    hint["retryable"] = inner_err.get("retryable")
                    hint["type"] = inner_err.get("type")
                else:
                    msg = str(inner_data.get("message") or "")
            if not msg:
                msg = json.dumps(raw, ensure_ascii=False)[:400]
            return False, None, code, msg or "dws +command returned ok=false", hint
        return True, raw.get("data"), None, str(raw.get("outcome") or ""), hint

    return True, raw, None, "", hint


def write_json_file(payload: Any, tmp_dir: Optional[str] = None,
                    prefix: str = "dws_payload_") -> str:
    """把 payload 写成 UTF-8 JSON 临时文件，返回**绝对路径**（调用方负责删除）。"""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=tmp_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return os.path.abspath(path)


def _cmd_key(argv: Sequence[str]) -> str:
    """从 argv 里提取一个短命令标识用于计数，如 'aitable record create'。"""
    parts = [p for p in argv if not str(p).startswith("-")]
    return " ".join(str(p) for p in parts[:3]) or "dws"


class DwsClient:
    """执行 `dws` 子命令的封装：两阶段模式（emit/replay）、计数、JSON 解析、错误归类。

    模式自动检测：传入 ``replay_path`` → replay 模式；否则 → emit 模式。

    用法::

        client = DwsClient()                              # emit 模式
        client = DwsClient(replay_path='/path/dws_results.json')  # replay 模式
        data = client.call(["aitable", "record", "query", "--base-id", b, "--table-id", t])
        print(client.counter.snapshot()["dws_calls"])

    所有 args 都是**参数列表**，绝不经过 shell（dws 在复杂 shell 结构下不稳定）。
    """

    def __init__(self, counter: Optional[DwsCallCounter] = None,
                 http_timeout: int = HTTP_TIMEOUT,
                 tmp_dir: Optional[str] = None,
                 verbose: bool = False,
                 replay_path: Optional[str] = None) -> None:
        self.counter = counter if counter is not None else DwsCallCounter()
        self.http_timeout = http_timeout
        self.tmp_dir = tmp_dir
        self.verbose = verbose or bool(os.environ.get("DWS_VERBOSE"))
        self._files_written = 0
        # 两阶段模式：自动检测
        self.mode = MODE_REPLAY if replay_path else MODE_EMIT
        self._emit_commands: List[Dict[str, Any]] = []
        self._emit_seq = 0
        self._replay_map: Dict[str, Any] = {}
        if self.mode == MODE_REPLAY and replay_path:
            self._load_replay(replay_path)

    def _load_replay(self, path: str) -> None:
        """加载 dws_results.json，构建 cmd_hash → result 映射。"""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "results" in data:
            results = data["results"]
        elif isinstance(data, list):
            results = data
        else:
            results = [data]
        for item in results:
            if isinstance(item, dict) and "argv_hash" in item:
                self._replay_map[item["argv_hash"]] = item

    @staticmethod
    def _hash_argv(argv: Sequence[str]) -> str:
        """对 argv 做稳定哈希（用于 emit→replay 配对）。"""
        import hashlib
        return hashlib.md5(
            "\x00".join(str(a) for a in argv).encode("utf-8")
        ).hexdigest()

    def _fake_response(self, argv: Sequence[str]) -> Tuple[int, str, str, int]:
        """在 emit 模式下，根据命令类型返回模拟成功响应。

        返回 (returncode, stdout, stderr, elapsed_ms)。
        stdout 是模拟的 dws JSON 输出。
        """
        t0 = time.monotonic()
        argv_strs = [str(a) for a in argv]
        joined = " ".join(argv_strs)

        # 判定命令类型并构造模拟响应
        if "query" in argv_strs:
            # record query → 返回空列表（无重复）
            fake_data = {"records": [], "nextCursor": None, "total": 0}
        elif "stats" in argv_strs:
            # record stats → 返回 0
            fake_data = {"total": 0}
        elif "field" in argv_strs and "get" in argv_strs:
            # field get → 返回空选项
            fake_data = {"options": []}
        elif "attachment" in argv_strs and "upload" in argv_strs:
            # attachment upload → 返回模拟 uploadUrl + fileToken
            fake_data = {"fileToken": "emit_fake_token_%s" % self._emit_seq,
                         "fileName": argv_strs[-1] if argv_strs else "unknown",
                         "uploadUrl": "https://emit-fake.example.com/upload/%s" % self._emit_seq,
                         "downloadUrl": "https://emit-fake.example.com/download/%s" % self._emit_seq}
        elif "record" in argv_strs and ("create" in argv_strs or "upsert" in argv_strs):
            # record create/upsert → 返回模拟 record_ids
            fake_data = {"records": [{"record_id": "emit_fake_id_%s" % self._emit_seq,
                                       "fields": {}}]}
        elif "record" in argv_strs and "update" in argv_strs:
            # record update → 返回成功
            fake_data = {"records": [{"record_id": "emit_fake_id", "fields": {}}]}
        elif "field" in argv_strs and "create" in argv_strs:
            # field create → 返回模拟 field_id
            fake_data = {"field_id": "emit_fake_field_%s" % self._emit_seq}
        else:
            # 默认模拟成功
            fake_data = {"ok": True}

        fake_raw = {"status": "success", "success": True,
                    "data": fake_data, "error": {}, "summary": "emit mode"}
        stdout = json.dumps(fake_raw, ensure_ascii=False)
        elapsed = int((time.monotonic() - t0) * 1000)
        return 0, stdout, "", elapsed

    # -- 便捷属性 ----------------------------------------------------------
    @property
    def dws_calls(self) -> int:
        return self.counter.calls

    def stats(self) -> Dict[str, Any]:
        return self.counter.snapshot()

    # -- 底层执行 ----------------------------------------------------------
    def _build_argv(self, args: Sequence[str], *, fmt: bool, yes: bool,
                    http_timeout: Optional[int]) -> List[str]:
        argv: List[str] = ["dws"] + [str(a) for a in args]
        lowered = [str(a) for a in argv]
        if fmt and "--format" not in lowered and "-f" not in lowered:
            argv += ["--format", "json"]
        if yes and "--yes" not in lowered and "-y" not in lowered:
            argv += ["--yes"]
        if http_timeout and "--timeout" not in lowered:
            argv += ["--timeout", str(http_timeout)]
        return argv

    def _spawn(self, argv: Sequence[str], timeout: int) -> Tuple[int, str, str, int]:
        # emit 模式：不调 dws，记录命令并返回模拟成功
        if self.mode == MODE_EMIT:
            argv_list = list(argv)
            argv_hash = self._hash_argv(argv_list)
            self._emit_seq += 1
            self._emit_commands.append({
                "seq": self._emit_seq,
                "argv_hash": argv_hash,
                "argv": argv_list,
            })
            return self._fake_response(argv_list)

        # replay 模式：不调 dws，从预加载结果返回
        argv_list = list(argv)
        argv_hash = self._hash_argv(argv_list)
        entry = self._replay_map.get(argv_hash)
        if entry is not None:
            rc = entry.get("returncode", 0)
            stdout = entry.get("stdout", "")
            stderr = entry.get("stderr", "")
            elapsed = entry.get("elapsed_ms", 0)
            return rc, stdout, stderr, elapsed
        # 没找到匹配结果 → 返回模拟成功（emit 阶段用伪造数据跳过的命令）
        self._log("replay: no match for %s, using fake" % _cmd_key(argv_list[1:]))
        return self._fake_response(argv_list)

    def emit_commands(self) -> List[Dict[str, Any]]:
        """返回 emit 模式下收集到的所有 dws 命令。"""
        return self._emit_commands

    def write_emit_file(self, path: str) -> None:
        """将收集到的 emit 命令写入 JSON 文件。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mode": "emit", "commands": self._emit_commands},
                      f, ensure_ascii=False, indent=2)

    def call(self, args: Sequence[str], *, timeout: Optional[int] = None,
             http_timeout: Optional[int] = None,
             raise_on_error: bool = True, yes: bool = True,
             fmt: bool = True) -> Dict[str, Any]:
        """执行一次 dws 调用（emit/replay 模式，无重试：emit 总是返回模拟成功，replay 从文件读取）。

        ``timeout`` 参数已废弃（emit/replay 模式下无墙钟超时），仅为兼容调用方签名保留。

        返回归一化结果::

            {"ok": bool, "data": Any, "code": Any, "message": str,
             "raw": dict|None, "attempts": int, "elapsed_ms": int, "category": str|None}

        `raise_on_error=True`（默认）时，失败会抛 `DwsError`；置 False 则把失败也当结果返回，
        由调用方写进 report 的 failed 列表。
        """
        argv = self._build_argv(args, fmt=fmt, yes=yes,
                                http_timeout=self.http_timeout if http_timeout is None
                                else http_timeout)
        key = _cmd_key(argv[1:])

        rc, out, err, elapsed = self._spawn(argv, 0)
        raw, parsed_ok = self._parse(out)
        if not parsed_ok:
            raw, parsed_ok = self._parse(err)
        if not parsed_ok:
            category = "parse"
            code = None
            msg = (err.strip() or out.strip() or "dws 输出无法解析为 JSON")[:800]
        else:
            ok, data, code, msg, hint = unwrap(raw)
            if ok:
                self.counter.record(key, elapsed, True, 1)
                return {"ok": True, "data": data, "code": None, "message": msg,
                        "raw": raw, "attempts": 1,
                        "elapsed_ms": elapsed, "category": None}
            category, _ = classify_error(msg, code)
            if category == "unknown":
                if hint.get("retryable") is True:
                    category = "server"
                elif hint.get("retryable") is False:
                    category = category or "invalid"
                elif hint.get("type"):
                    category, _ = classify_error(str(hint.get("type")))
            if rc != 0 and category == "unknown":
                category, _ = classify_error(err or out)

        self._log("dws fail [%s] category=%s code=%s msg=%s"
                  % (key, category, code, (msg or "")[:200]))
        self.counter.record(key, elapsed, False, 1, category)
        exc = DwsError(
            msg or "dws 调用失败", category=category, code=code,
            retryable=False, argv=argv, returncode=rc, stdout=out,
            stderr=err, attempts=1, elapsed_ms=elapsed)
        if raise_on_error:
            raise exc
        return {"ok": False, "data": None, "code": code, "message": msg,
                "raw": raw, "attempts": 1, "elapsed_ms": elapsed,
                "category": category, "error": exc.to_dict()}

    @staticmethod
    def _parse(out: str) -> Tuple[Optional[Any], bool]:
        text = (out or "").strip()
        if not text:
            return None, False
        try:
            return json.loads(text), True
        except ValueError:
            pass
        # 容错：dws 偶尔在 JSON 前后打印日志行 → 抓第一个平衡的 JSON 对象/数组
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1]), True
                except ValueError:
                    continue
        return None, False

    def _log(self, msg: str) -> None:
        if self.verbose:
            print("[aitable.client] %s" % msg, flush=True, file=sys.stderr)

    # -- 大 payload -------------------------------------------------------
    def call_with_payload(self, args: Sequence[str], payload: Any, *,
                          file_flag: str = "--records-file",
                          inline_flag: str = "--records",
                          **kwargs) -> Dict[str, Any]:
        """把大 JSON 通过内联 `--records` 传参。

        emit/replay 模式下始终使用内联，使 argv 自包含（无临时文件路径）
        且确定性（同一 payload 在 emit 和 replay 阶段产生相同的 argv_hash，从而能正确配对）。
        """
        serialized = json.dumps(payload, ensure_ascii=False)
        return self.call(list(args) + [inline_flag, serialized], **kwargs)

    # -- 分片工具 ---------------------------------------------------------
    @staticmethod
    def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
        size = max(1, int(size))
        for i in range(0, len(items), size):
            yield items[i:i + size]


#: 进程级默认计数器（上层想要全局计数时共享同一个 counter）
_GLOBAL_COUNTER = DwsCallCounter()


def default_client(**kwargs) -> DwsClient:
    """返回一个共享全局计数器的 DwsClient（便于跨模块汇总 dws_calls）。"""
    kwargs.setdefault("counter", _GLOBAL_COUNTER)
    return DwsClient(**kwargs)


def global_stats() -> Dict[str, Any]:
    return _GLOBAL_COUNTER.snapshot()
