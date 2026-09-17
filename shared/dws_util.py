#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dws_util.py —— 钉钉 `dws` CLI 的最小封装层（零第三方 pip 依赖，只用标准库 + subprocess）。

为什么需要这一层（实测结论，写代码前先读）：
  1. **一次 dws 网络调用的固定开销 ≈ 1.0~1.3s**（进程启动 ~0.27s + 鉴权/网络 ~0.7s）。
     所以封装层的第一优化目标是「减少调用次数」，不是「让单次调用更快」。
     本模块对每次 subprocess 调用计数（含重试），最终由上层 report 汇总 `dws_calls`。
  2. **dws CLI 在复杂 shell 结构下不稳定**（重定向 / 变量替换 / for 循环 / 管道）。
     → 本模块一律 `subprocess.run(list_of_args)`，**永不 shell=True**，永不让上层拼 shell。
  3. **超长 JSON 必须走文件参数**：`record create/update/upsert` 都支持
     `--records-file <绝对路径>`，避免命令行长度限制与引号转义（Windows 尤其）。
  4. dws 的响应信封有**三种形态**，都要认（见 `unwrap`）：
       A. 原子命令： {"status":"success|error","success":bool,"data":{...},"error":{...},"summary":"..."}
       B. `+` 便捷命令（旧版/部分命令）：直接就是业务 payload（如 {"bases":[...],"count":2}）；
          出错时是 {"error":{"category":"internal","code":1,
                     "message":"[MCP_TOOL_ERROR] {内层 JSON}"}}
       C. `+` 便捷命令**双层信封**（W-G 实测，dws 1.0.60，如 `+me`/`+field-get`/
          `+record-upsert`）：{"ok":bool,"outcome":"success|...","data":{业务 payload}}。
          它既没有 status/success 也没有 summary → 旧版 unwrap 会把**整个信封**当 payload
          返回（调用方拿到 {"ok","outcome","data"} 而不是内层数据）。现在统一剥壳：
          ok==true → 返回 data；ok==false → 按失败处理（error/outcome/data 里挖消息）。
  5. 错误分类纪律（契约 §3.2）：网络/超时/限流/5xx → 3 次指数退避重试；
     权限类 401/403 → **不重试**，直接抛 DwsError 让上层归类到 failed 并保留原始错误码。

模块只依赖标准库：json / os / re / subprocess / tempfile / threading / time / pathlib。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DwsError",
    "DwsCallCounter",
    "DwsRunner",
    "unwrap",
    "classify_error",
    "write_json_file",
    "now_iso",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DWS_BIN = os.environ.get("DWS_BIN", "dws")

DEFAULT_TIMEOUT = 120          # subprocess 墙钟超时（秒）
DEFAULT_HTTP_TIMEOUT = 90      # 传给 dws 的 --timeout（HTTP 请求超时，秒）
DEFAULT_RETRIES = 3            # 网络类错误的额外重试次数（总尝试 = 1 + retries）
DEFAULT_BACKOFF = 1.0          # 指数退避基数（秒）：1s / 2s / 4s
MAX_BACKOFF = 15.0

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
    契约要求：网络类（timeout/network/server/rate_limit）可重试；权限类 auth **不重试**。

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
    report 的 `failed` 与 `warnings`（契约 D6：失败可见，禁止静默丢弃）。
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

    `calls` = 真实发生的 subprocess 次数（**含重试**），这是性能验证的核心指标；
    `logical` = 逻辑调用次数（不含重试），用来看重试放大了多少。
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
    - 信封 C（`+` 命令**双层信封**，dws 1.0.60 实测）：
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


class DwsRunner:
    """执行 `dws` 子命令的封装：计数、超时、JSON 解析、错误归类、指数退避重试。

    用法::

        runner = DwsRunner()
        data = runner.call(["aitable", "record", "query", "--base-id", b, "--table-id", t])
        print(runner.counter.snapshot()["dws_calls"])

    所有 args 都是**参数列表**，绝不经过 shell（契约 §0.5 / 实测坑：dws 在复杂 shell 结构下不稳定）。
    """

    def __init__(self, counter: Optional[DwsCallCounter] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 http_timeout: int = DEFAULT_HTTP_TIMEOUT,
                 retries: int = DEFAULT_RETRIES,
                 backoff: float = DEFAULT_BACKOFF,
                 bin: Optional[str] = None,
                 tmp_dir: Optional[str] = None,
                 verbose: bool = False,
                 sleep=time.sleep) -> None:
        self.counter = counter if counter is not None else DwsCallCounter()
        self.timeout = timeout
        self.http_timeout = http_timeout
        self.retries = retries
        self.backoff = backoff
        self.bin = bin or DWS_BIN
        self.tmp_dir = tmp_dir
        self.verbose = verbose or bool(os.environ.get("DWS_VERBOSE"))
        self._sleep = sleep
        self._files_written = 0

    # -- 便捷属性 ----------------------------------------------------------
    @property
    def dws_calls(self) -> int:
        return self.counter.calls

    def stats(self) -> Dict[str, Any]:
        return self.counter.snapshot()

    # -- 底层执行 ----------------------------------------------------------
    def _build_argv(self, args: Sequence[str], *, fmt: bool, yes: bool,
                    http_timeout: Optional[int]) -> List[str]:
        argv: List[str] = [self.bin] + [str(a) for a in args]
        lowered = [str(a) for a in argv]
        if fmt and "--format" not in lowered and "-f" not in lowered:
            argv += ["--format", "json"]
        if yes and "--yes" not in lowered and "-y" not in lowered:
            argv += ["--yes"]
        if http_timeout and "--timeout" not in lowered:
            argv += ["--timeout", str(http_timeout)]
        return argv

    def _spawn(self, argv: Sequence[str], timeout: int) -> Tuple[int, str, str, int]:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(list(argv), capture_output=True, text=True,
                                  timeout=timeout)  # 永不 shell=True
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic() - t0) * 1000)
            raise DwsError("dws 子进程超时（%ss）" % timeout, category="timeout",
                           retryable=True, argv=argv, elapsed_ms=elapsed)
        except FileNotFoundError:
            raise DwsError("未找到 dws 可执行文件（%s）；请确认钉钉连接器已安装并在 PATH 中"
                           % self.bin, category="env", retryable=False, argv=argv)
        except OSError as exc:  # pragma: no cover - 极少见
            raise DwsError("启动 dws 失败: %s" % exc, category="env",
                           retryable=False, argv=argv)
        elapsed = int((time.monotonic() - t0) * 1000)
        return proc.returncode, proc.stdout or "", proc.stderr or "", elapsed

    def call(self, args: Sequence[str], *, timeout: Optional[int] = None,
             retries: Optional[int] = None, http_timeout: Optional[int] = None,
             raise_on_error: bool = True, yes: bool = True,
             fmt: bool = True) -> Dict[str, Any]:
        """执行一次 dws 调用（自动重试网络类错误）。

        返回归一化结果::

            {"ok": bool, "data": Any, "code": Any, "message": str,
             "raw": dict|None, "attempts": int, "elapsed_ms": int, "category": str|None}

        `raise_on_error=True`（默认）时，失败会抛 `DwsError`；置 False 则把失败也当结果返回，
        由调用方写进 report 的 failed 列表（契约 D6）。
        """
        argv = self._build_argv(args, fmt=fmt, yes=yes,
                                http_timeout=self.http_timeout if http_timeout is None
                                else http_timeout)
        key = _cmd_key(argv[1:])
        max_retries = self.retries if retries is None else retries
        to = self.timeout if timeout is None else timeout

        attempt = 0
        total_elapsed = 0
        last_err: Optional[DwsError] = None
        while True:
            attempt += 1
            try:
                rc, out, err, elapsed = self._spawn(argv, to)
            except DwsError as exc:
                rc, out, err, elapsed = None, "", str(exc), 0
                exc.argv = argv
                last_err = exc
                category, retryable = exc.category, exc.retryable
                code = None
                msg = exc.message
                raw = None
            else:
                raw, parsed_ok = self._parse(out)
                if not parsed_ok:
                    # 实测：`+` 便捷命令失败时把错误信封打到 **stderr** 且退出码非 0
                    raw, parsed_ok = self._parse(err)
                if not parsed_ok:
                    # stdout/stderr 都不是合法 JSON：CLI 崩了或输出了日志噪声，按可重试处理
                    category, retryable = "parse", True
                    code, msg = None, (err.strip() or out.strip()
                                       or "dws 输出无法解析为 JSON")[:800]
                    hint = {}
                else:
                    ok, data, code, msg, hint = unwrap(raw)
                    if ok:
                        self.counter.record(key, elapsed, True, attempt)
                        return {"ok": True, "data": data, "code": None, "message": msg,
                                "raw": raw, "attempts": attempt,
                                "elapsed_ms": elapsed, "category": None}
                    category, retryable = classify_error(msg, code)
                    if category == "unknown":
                        # 服务端内层显式给了 retryable/type（如 SYSTEM_ERROR retryable=true）
                        if hint.get("retryable") is True:
                            category, retryable = "server", True
                        elif hint.get("retryable") is False:
                            category, retryable = category or "invalid", False
                        elif hint.get("type"):
                            category, retryable = classify_error(str(hint.get("type")))
                    if rc != 0 and category == "unknown":
                        category, retryable = classify_error(err or out)
                    last_err = None

            total_elapsed += elapsed
            self._log("dws fail [%s] attempt=%d category=%s code=%s msg=%s"
                      % (key, attempt, category, code, (msg or "")[:200]))

            can_retry = retryable and attempt <= max_retries
            if not can_retry:
                self.counter.record(key, total_elapsed, False, attempt, category)
                exc = last_err or DwsError(
                    msg or "dws 调用失败", category=category, code=code,
                    retryable=retryable, argv=argv, returncode=rc, stdout=out,
                    stderr=err, attempts=attempt, elapsed_ms=total_elapsed)
                if last_err is not None:
                    exc.attempts = attempt
                    exc.elapsed_ms = total_elapsed
                if raise_on_error:
                    raise exc
                return {"ok": False, "data": None, "code": code, "message": msg,
                        "raw": raw, "attempts": attempt, "elapsed_ms": total_elapsed,
                        "category": category, "error": exc.to_dict()}

            delay = min(MAX_BACKOFF, self.backoff * (2 ** (attempt - 1)))
            if category == "rate_limit":
                delay = max(delay, 2.0)
            self._sleep(delay)

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
            print("[dws_util] %s" % msg, flush=True, file=sys.stderr)

    # -- 大 payload -------------------------------------------------------
    def call_with_payload(self, args: Sequence[str], payload: Any, *,
                          file_flag: str = "--records-file",
                          inline_flag: str = "--records",
                          force_file: bool = True,
                          inline_threshold: int = 1500,
                          **kwargs) -> Dict[str, Any]:
        """把大 JSON 通过临时文件传参（`--records-file <绝对路径>`）。

        实测坑：`--records` 内联超长 JSON 在 Windows 上会被命令行长度截断、在复杂 shell 下会被
        引号规则吃掉；文件传参两个问题都没有。写文件失败或 dws 报「文件类」错误时自动回退内联。
        """
        serialized = json.dumps(payload, ensure_ascii=False)
        use_file = force_file or len(serialized) > inline_threshold
        if not use_file:
            return self.call(list(args) + [inline_flag, serialized], **kwargs)

        path = None
        try:
            path = write_json_file(payload, self.tmp_dir)
            self._files_written += 1
            try:
                return self.call(list(args) + [file_flag, path], **kwargs)
            except DwsError as exc:
                blob = "%s %s" % (exc.message, exc.code)
                if any(k in blob.lower() for k in ("records-file", "file not found",
                                                   "open ", "no such file", "路径")):
                    self._log("records-file 传参失败，回退内联 --records")
                    return self.call(list(args) + [inline_flag, serialized], **kwargs)
                raise
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # -- 分片工具 ---------------------------------------------------------
    @staticmethod
    def chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
        size = max(1, int(size))
        for i in range(0, len(items), size):
            yield items[i:i + size]


#: 进程级默认 runner（上层想要全局计数时共享同一个 counter）
_GLOBAL_COUNTER = DwsCallCounter()


def default_runner(**kwargs) -> DwsRunner:
    """返回一个共享全局计数器的 DwsRunner（便于跨模块汇总 dws_calls）。"""
    kwargs.setdefault("counter", _GLOBAL_COUNTER)
    return DwsRunner(**kwargs)


def global_stats() -> Dict[str, Any]:
    return _GLOBAL_COUNTER.snapshot()
