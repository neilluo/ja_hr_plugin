#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aitable_io.py —— 钉钉 AI 表格 IO 层（契约 §3.2 冻结签名，零第三方 pip 依赖）。

设计纪律（都是实测换来的，改代码前先读）：

1. **调用次数是第一优化目标。** 一次 dws 网络调用固定开销 ≈1.0~1.3s（进程启动 ~0.27s +
   鉴权/网络 ~0.7s）。所以本层所有方法都是「一次调用干完一批」：批量写 ≤100 条/次、
   查重一次 OR filter 查完 N 个键、结构读回一次 `field get` 全量、翻页用 `--all` 交给 CLI。
   每次 subprocess 都计数，`AITable.dws_calls` / `.stats()` 就是 report 里的 `dws_calls`。

2. **绝不经过 shell。** 一律 `subprocess.run(list_of_args)`；超长 JSON 走
   `--records-file <绝对路径>`（`record create/update/upsert` 都支持）。

3. **附件严禁直传 URL。** cells 里写 `{"url":"https://..."}` 会让服务端同步下载，
   10 条记录就 TIMEOUT_ERROR。必须 `attachment upload` 拿 `fileToken` → urllib PUT 到 OSS
   → cells 里写 `[{"fileToken":"ft_xxx"}]`。写入是**整体覆盖不是追加**。
   附件无批量接口（3 步/文件），只能并发：`upload_attachments(paths, concurrency=5)`
   （API 限 20 QPS，5 是留足余量的默认值，契约 D5）。

4. **写前净化、写后回读。** `sanitize_text` 去掉 `ord(c)<32` 的控制字符（保留 `\n`）——
   pdftotext 的输出含 `\x0c`/`\t`，原样写入会被 API 拒收。写完用 `readback_verify` 读回。

5. **读回值必须过 `val()`。** 实测：`record query` 返回的 `singleSelect` 是 dict（要取 `.name`），
   `multipleSelect` 是 dict 数组且**读回不保序** → 所有比对按集合（`val_set` / `values_equal`），
   不能按列表；`number` 读回是**字符串形式**的数字。

6. **错误分类与重试**（见 dws_util）：网络/超时/限流/5xx → 3 次指数退避；
   权限类 401/403 → **不重试**，直接进 `failed` 并保留原始错误码（契约 D6：失败可见，禁止静默丢弃）。

7. **幂等**（契约 D12）：`batch_upsert_by_key` 先**一次性**批量查出已存在键（不逐条查），
   再拆 create/update，走 `record upsert` 一次提交。

8. ⚠️ **对「刚批量创建出来的记录」做 update，写入可能要几分钟后才可读（本层实测最阴的坑）**：
   `record update` 立刻返回 success + recordIds，但随后 34s / 47s / 156s 连续轮询读回**全是旧值**，
   约 4 分钟后再读就是正确值了（也遇到过更久）。期间当场怎么重试都没用
   （实测一轮 41 次调用 / 70 秒全废，含逐条重发）。
   复现条件不唯一：job 表「一次 create 19 条 → 0/2/5/20s 后 update 12~19 条」多次复现
   （number/text 字段都会）；同样写法也有一轮直接 1.3s 就可读；resume 表 19 条一次 update
   从没出现过；拆 10+9 两片、或 19 次单条发，多数正常，可也有 10 条一片照样延迟的一轮。
   → 工程结论：**① 能在 create 里一次写全的就别事后再 update（附件先上传拿 fileToken，
   随 create 一起写入）；② 回填用 `batch_update_verified()`（≤10 条/片 + 回读 + 有界重试）；
   ③ 回读不到时不要空转（按 D6 报进 failed/warnings、按 D7 在后续回合重跑该步复核），
   因为值通常几分钟后就在了，当场重试只会白烧调用次数。**

9. **写后读回有传播延迟**：update/create 落地到可读约 1.1~2.7s（实测中位数 ~1.3s），
   所以 `readback_verify(expected=...)` 会自动轮询（默认 3 次，1.2s/2.4s/3.6s），
   不会把「还没同步」误报成「写错了」。

10. **`record query --all` 是坏的**：当前 dws 版本带不带 filter 都返回 0 条，
    本层一律自己用 `--cursor` 翻页。

字段类型相关的 config.json 结构见 §3.4；`types` / `formatters` / `options` 是本层额外读取的
可选段（缺失时按 text 兜底），由 replicate/bootstrap 生成，脚本内零硬编码 ID（契约 D8）。
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from dws_util import (  # noqa: E402
    MAX_FIELD_IDS_PER_GET,
    MAX_FIELDS_PER_CALL,
    MAX_QUERY_LIMIT,
    MAX_RECORD_IDS_PER_CALL,
    MAX_RECORDS_PER_CALL,
    DwsCallCounter,
    DwsError,
    DwsRunner,
)

__all__ = [
    "AITable", "AITableConfigError", "val", "val_set", "values_equal",
    "MAX_RECORDS_PER_CALL", "MAX_QUERY_LIMIT",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 附件上传并发默认值（契约 D5：API 限 20 QPS，留余量）
DEFAULT_UPLOAD_CONCURRENCY = 5
#: 附件单文件大小上限（沿用官方 skill 脚本 upload_attachment.py 的取值）
MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024
#: OSS PUT 超时（秒）
OSS_PUT_TIMEOUT = 180
#: 一次 filter 查询里最多塞多少个 OR 条件（超出自动分片查询）
MAX_FILTER_OPERANDS = 100
#: `batch_update_verified` 的单片条数上限。
#: 实测：对**刚批量创建**出来的 job 表记录，一次 update 12~19 条时写入要几分钟后才可读
#: （返回 success + recordIds，但当场轮询 156s 全是旧值）；≤10 条、或拆成多次、
#: 或逐条发，大多数轮次都能 1~2s 内读到（不是 100%，所以还要配回读校验）。
SAFE_UPDATE_CHUNK = 10

_TEXT_TYPES = {"text", "telephone", "email", "barcode", "idCard", "primaryDoc"}
_READONLY_TYPES = {"formula", "lookup", "filterUp", "creator", "lastModifier",
                   "createdTime", "lastModifiedTime"}
_SELECT_TYPES = {"singleSelect", "multipleSelect"}


class AITableConfigError(Exception):
    """config.json 缺 base_id / table / field 映射时抛出（契约 D8：唯一 ID 源）。"""


# ---------------------------------------------------------------------------
# 读回值归一化
# ---------------------------------------------------------------------------
def val(v: Any, default: Any = None) -> Any:
    """把 `record query` 读回的单元格值归一化成好用的 python 值。

    实测坑（必须走这个函数，别直接当字符串用）：
      * `singleSelect` → dict `{"id","name"}`，取 `.name`；
      * `multipleSelect` → dict 数组，且**读回不保序**；
      * `number` / `currency` / `progress` → **字符串形式**的数字（如 `"0.7"`）；
      * `richText` → `{"markdown": "..."}`；
      * `url` → `{"text","link"}`；
      * `attachment` → `[{"url","filename","size"}]`（url 是 2h 时效的 OSS 预签名链接，
        且 dws 输出里 `&` 会被转义成 `\\u0026`）。
    """
    if v is None:
        return default
    if isinstance(v, list):
        return [val(x, default) for x in v]
    if isinstance(v, dict):
        if "name" in v and ("id" in v or len(v) <= 3):        # select / user-ish
            return v.get("name")
        if "markdown" in v:                                    # richText
            return v.get("markdown")
        if "fileToken" in v or "filename" in v or ("url" in v and "size" in v):
            return v                                           # attachment：整体保留
        if "text" in v and "link" in v:                        # url
            return v
        if "userId" in v or "deptId" in v or "linkedRecordIds" in v:
            return v
        if "value" in v:
            return val(v.get("value"), default)
        return v
    return v


def val_set(v: Any) -> frozenset:
    """把单元格值变成可比较的**集合**（多选读回不保序，只能按集合比）。"""
    x = val(v)
    if x is None:
        return frozenset()
    if isinstance(x, (list, tuple, set, frozenset)):
        return frozenset(_hashable(i) for i in x)
    return frozenset({_hashable(x)})


def _hashable(x: Any) -> Any:
    if isinstance(x, dict):
        return json.dumps(x, ensure_ascii=False, sort_keys=True)
    if isinstance(x, (list, tuple)):
        return json.dumps(list(x), ensure_ascii=False)
    return x


def values_equal(a: Any, b: Any) -> bool:
    """比较写入值与读回值。列表/多选按集合比（读回不保序），数字按数值比（读回是字符串）。

    日期也算相等：写 `"2026-09-17"`，读回是 RFC3339 `"2026-09-17T00:00:00+08:00"`。
    """
    na, nb = val(a), val(b)
    if isinstance(na, (list, tuple, set)) or isinstance(nb, (list, tuple, set)):
        return val_set(na) == val_set(nb)
    if isinstance(na, bool) or isinstance(nb, bool):
        return bool(na) == bool(nb)
    if _is_numberish(na) and _is_numberish(nb):
        try:
            return abs(float(na) - float(nb)) < 1e-9
        except (TypeError, ValueError):
            pass
    if isinstance(na, dict) and isinstance(nb, dict):
        return _hashable(na) == _hashable(nb)
    if isinstance(na, str) and isinstance(nb, str) and _date_prefix_eq(na, nb):
        return True
    return na == nb


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_prefix_eq(a: str, b: str) -> bool:
    """date 字段写入 `YYYY-MM-DD`、读回 `YYYY-MM-DDThh:mm:ss+08:00` → 视为相等。"""
    if _DATE_ONLY_RE.match(a) and b.startswith(a):
        return True
    if _DATE_ONLY_RE.match(b) and a.startswith(b):
        return True
    return False


def _is_numberish(x: Any) -> bool:
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, (int, float)):
        return True
    if isinstance(x, str):
        try:
            float(x)
            return True
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------
class AITable:
    """钉钉 AI 表格 IO 层。所有 table_key / field_key 一律用**业务名**，由 config.json 映射到真实 ID。"""

    def __init__(self, config_path: str, runner: Optional[DwsRunner] = None,
                 counter: Optional[DwsCallCounter] = None, verbose: bool = False):
        self.config_path = str(Path(config_path).expanduser().resolve())
        if not Path(self.config_path).exists():
            raise AITableConfigError("config.json 不存在：%s" % self.config_path)
        with open(self.config_path, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.base_id = self.config.get("base_id")
        if not self.base_id:
            raise AITableConfigError("config.json 缺 base_id：%s" % self.config_path)
        self.base_name = self.config.get("base_name", "")
        self._tables = self.config.get("tables") or {}
        self._fields = self.config.get("fields") or {}
        self._types = self.config.get("types") or {}
        self._formatters = self.config.get("formatters") or {}
        self._options_cache = self.config.get("options") or {}
        self.runner = runner or DwsRunner(counter=counter, verbose=verbose)
        self.warnings: List[str] = []
        #: ensure_options（只读实现）记下的待补建选项名：{(table_key, field_key): [name,...]}
        #: 实际补建由服务端在 record create/update/upsert 写选项名时自动完成（见
        #: ensure_options 文档串：`field update` 路径因 option id churn 已彻底移除）。
        self.pending_options: Dict[Tuple[str, str], List[str]] = {}

    # -- 计数 / 元信息 ----------------------------------------------------
    @property
    def dws_calls(self) -> int:
        """本对象（及共享同一 counter 的对象）真实发生的 dws 进程调用次数（含重试）。"""
        return self.runner.counter.calls

    def stats(self) -> Dict[str, Any]:
        out = self.runner.counter.snapshot()
        out["warnings"] = list(self.warnings)
        return out

    def table_keys(self) -> List[str]:
        return sorted(self._tables.keys())

    def table_id(self, table_key: str) -> str:
        t = self._tables.get(table_key)
        if not t:
            raise AITableConfigError("config.json 里没有表 %r（现有：%s）"
                                     % (table_key, self.table_keys()))
        return t.get("table_id") or t.get("tableId")

    def table_name(self, table_key: str) -> str:
        return (self._tables.get(table_key) or {}).get("name", table_key)

    def field_keys(self, table_key: str) -> List[str]:
        return list((self._fields.get(table_key) or {}).keys())

    def field_id(self, table_key: str, field_key: str) -> str:
        fmap = self._fields.get(table_key) or {}
        fid = fmap.get(field_key)
        if not fid:
            # 容错：允许直接传中文字段名
            for k, v in (self.config.get("field_names") or {}).get(table_key, {}).items():
                if v == field_key:
                    fid = fmap.get(k)
                    break
        if not fid:
            raise AITableConfigError("表 %r 里没有字段 %r（现有：%s）"
                                     % (table_key, field_key, sorted(fmap.keys())))
        return fid

    def field_type(self, table_key: str, field_key: str) -> str:
        return (self._types.get(table_key) or {}).get(field_key, "text")

    def field_formatter(self, table_key: str, field_key: str) -> Optional[str]:
        return (self._formatters.get(table_key) or {}).get(field_key)

    def _fid_map(self, table_key: str) -> Dict[str, str]:
        """fieldId → 业务 key 的反向映射。"""
        return {v: k for k, v in (self._fields.get(table_key) or {}).items()}

    # -- 文本净化 ---------------------------------------------------------
    #: 必须丢掉的码位区间（含端点）。实测依据见 sanitize_text 文档串。
    _DROP_RANGES = (
        (0x00, 0x09), (0x0B, 0x1F),          # C0 控制字符（含 \t），只保留 \n = 0x0A
        (0x7F, 0x9F),                        # DEL + C1 控制字符
        (0x00AD, 0x00AD),                    # 软连字符
        (0x061C, 0x061C),                    # 阿拉伯字母标记
        (0x200B, 0x200F),                    # 零宽空格/连接符/LRM/RLM
        (0x202A, 0x202E),                    # bidi 嵌入与覆盖
        (0x2060, 0x2064), (0x2066, 0x2069),  # 不可见运算符 + bidi 隔离
        (0xFEFF, 0xFEFF),                    # BOM / 零宽不换行空格
        (0xFFF9, 0xFFFB),                    # 行间注释
        (0xE0000, 0xE007F),                  # Tags 区块（隐写用）
    )
    #: 语义上是换行、但会被服务端判成「dangerous Unicode」的码位 → 换成 \n
    _NEWLINE_MAP = ((0x2028, "\n"), (0x2029, "\n"))
    _TRANSLATE_TABLE = None

    @classmethod
    def _translate_table(cls) -> Dict[int, Any]:
        if cls._TRANSLATE_TABLE is None:
            table: Dict[int, Any] = {}
            for lo, hi in cls._DROP_RANGES:
                for cp in range(lo, hi + 1):
                    table[cp] = None
            for cp, rep in cls._NEWLINE_MAP:
                table[cp] = rep
            table[0x0D] = None               # \r（\r\n 先归一成 \n 再丢单独的 \r）
            cls._TRANSLATE_TABLE = table
        return cls._TRANSLATE_TABLE

    @classmethod
    def sanitize_text(cls, s: Any) -> Any:
        """写入前净化文本；非字符串原样返回。

        规则（契约要求 + 实测）：
          1. `\\r\\n` → `\\n`，剩下的孤立 `\\r` 丢掉；
          2. **U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR → `\\n`**。
             实测：dws 会以 `[UNCLASSIFIED] <fieldId> contains dangerous Unicode characters`
             **拒绝整批写入**（错误码 3、无 retryable 提示）。31 份真实简历里 3 份的
             docx 提取文本含 U+2028 → 那 3 条记录写不进去。这类字符不是 C0/C1 控制符，
             `ord(c) < 32` 的老规则抓不到，必须显式处理。
          3. 丢掉 `ord(c) < 32`（**保留 `\\n`**）、DEL 与 C1（0x7F~0x9F）；
          4. 丢掉零宽/bidi 等不可见格式符（U+200B~U+200F、U+202A~U+202E、U+2060~U+2069、
             U+FEFF、U+00AD、U+061C、U+FFF9~U+FFFB、Tags 区块）。
             注：实测这些字符当前服务端**是收的**（有简历含 17~38 个 U+200F 也写成功了），
             但它们是 PDF/docx 提取噪声、不可见、且属于典型注入载体，统一清掉更稳。

        ⚠️ 净化会改变字节内容 → 任何「写入 vs 读回」的逐字节比对都必须拿
        `sanitize_text(原文)` 当基准，不能拿原文（否则会误判成截断）。
        """
        if not isinstance(s, str) or not s:
            return s
        if "\r\n" in s:
            s = s.replace("\r\n", "\n")
        return s.translate(cls._translate_table())

    # -- 写值格式化 -------------------------------------------------------
    def format_cell(self, table_key: str, field_key: str, value: Any,
                    keep_empty: bool = False) -> Tuple[bool, Any, Optional[str]]:
        """把业务值格式化成 cells 里该 fieldId 的写入值。

        返回 (是否要写这个 cell, 值, 错误原因)。错误原因非空时上层应把该行记进 failed（D6）。
        """
        ftype = self.field_type(table_key, field_key)
        if ftype in _READONLY_TYPES:
            return False, None, "字段 %s 是只读类型 %s，不可写入" % (field_key, ftype)
        if value is None:
            return False, None, None
        if isinstance(value, str) and not value.strip() and not keep_empty:
            return False, None, None

        if ftype == "attachment":
            return self._format_attachment(field_key, value)
        if ftype == "multipleSelect":
            if isinstance(value, str):
                value = [value]
            if isinstance(value, (set, frozenset, tuple)):
                value = list(value)
            if not isinstance(value, list):
                return False, None, "多选字段 %s 需要 list，实得 %s" % (field_key, type(value).__name__)
            seen, out = set(), []
            for it in value:
                if isinstance(it, dict):
                    key = it.get("id") or it.get("name")
                    out.append(it)
                else:
                    key = self.sanitize_text(str(it))
                    out.append(key)
                if key in seen:
                    continue
                seen.add(key)
            if not out and not keep_empty:
                return False, None, None
            return True, out, None
        if ftype == "singleSelect":
            if isinstance(value, dict):
                return True, value, None
            return True, self.sanitize_text(str(value)), None
        if ftype == "number":
            if isinstance(value, bool):
                return True, int(value), None
            if isinstance(value, (int, float)):
                return True, value, None
            if isinstance(value, str):
                t = value.strip()
                try:
                    f = float(t)
                except ValueError:
                    return False, None, ("数字字段 %s 收到非数字文本 %r" % (field_key, value[:40]))
                return True, (int(f) if f.is_integer() and "." not in t and "e" not in t.lower()
                              else f), None
            return False, None, "数字字段 %s 收到 %s" % (field_key, type(value).__name__)
        if ftype == "richText":
            if isinstance(value, dict):
                md = value.get("markdown")
                if isinstance(md, str):
                    value = dict(value, markdown=self.sanitize_text(md))
                return True, value, None
            return True, {"markdown": self.sanitize_text(str(value))}, None
        if ftype == "checkbox":
            return True, bool(value), None
        if ftype == "date":
            if isinstance(value, (_dt.datetime, _dt.date)):
                return True, value.strftime("%Y-%m-%d"), None
            if isinstance(value, (int, float)):
                return True, int(value), None          # 毫秒时间戳
            return True, self.sanitize_text(str(value)), None
        if ftype == "url":
            if isinstance(value, dict):
                return True, value, None
            return True, {"text": str(value), "link": str(value)}, None
        if ftype in ("user", "department", "group",
                     "unidirectionalLink", "bidirectionalLink", "geolocation"):
            return True, value, None                   # 结构由调用方负责
        # text / telephone / email / barcode / idCard / 未知类型
        return True, self.sanitize_text(value if isinstance(value, str) else str(value)), None

    @staticmethod
    def _format_attachment(field_key: str, value: Any) -> Tuple[bool, Any, Optional[str]]:
        """attachment 字段：只接受 fileToken。

        **严禁**在 cells 里直传 `{"url":"https://..."}` 或本地路径 —— 服务端会同步下载，
        10 条记录即触发 TIMEOUT_ERROR。必须先 `upload_attachments` 拿 fileToken。
        写入是**整体覆盖不是追加**。
        """
        items = value if isinstance(value, list) else [value]
        out = []
        for it in items:
            if isinstance(it, dict):
                tok = it.get("fileToken") or it.get("file_token")
                if tok:
                    out.append({"fileToken": tok})
                    continue
                if it.get("url"):
                    return False, None, ("附件字段 %s 收到 {\"url\":...}：严禁直传 URL，"
                                         "服务端会同步下载并 TIMEOUT_ERROR；"
                                         "请先 upload_attachments 拿 fileToken" % field_key)
                return False, None, "附件字段 %s 收到无法识别的对象：%s" % (
                    field_key, json.dumps(it, ensure_ascii=False)[:120])
            if isinstance(it, str):
                s = it.strip()
                if s.startswith("ft_") or re.match(r"^[A-Za-z0-9_-]{16,}$", s):
                    out.append({"fileToken": s})
                    continue
                return False, None, ("附件字段 %s 收到字符串 %r：既不是 fileToken 也不允许"
                                     "（本地路径/URL 都要先 upload_attachments）" % (field_key, s[:80]))
            return False, None, "附件字段 %s 收到 %s" % (field_key, type(it).__name__)
        if not out:
            return False, None, None
        return True, out, None

    def build_cells(self, table_key: str, row: Dict[str, Any],
                    keep_empty: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        """业务 key 的 row → {fieldId: 写入值}；同时返回该行的错误原因列表。"""
        cells: Dict[str, Any] = {}
        errors: List[str] = []
        for key, value in row.items():
            if key in ("record_id", "recordId", "_key", "_meta"):
                continue
            try:
                fid = self.field_id(table_key, key)
            except AITableConfigError as exc:
                errors.append(str(exc))
                continue
            ok, formatted, reason = self.format_cell(table_key, key, value, keep_empty)
            if reason:
                errors.append(reason)
            if ok:
                cells[fid] = formatted
        return cells, errors

    # -- 查询 -------------------------------------------------------------
    def build_filter(self, table_key: str, filter: Any = None) -> Optional[Dict[str, Any]]:
        """把业务语义的 filter 翻成 dws 的 filters JSON（字段名 → fieldId）。

        支持四种写法：
          * `None` → 不过滤（全表，受 limit 限制）
          * `{"phone": ["138...", "139..."]}` → 同一字段多值 OR（**查重就靠它，一次查完 N 个键**）
          * `{"org": "制造中心", "status": "招聘中"}` → 多字段 AND（eq）
          * `{"phone": {"op": "contain", "value": "138"}}` → 显式操作符
            （op ∈ eq/ne/contain/exclusive/exist/un_exist/lt/gt/lte/gte/all_of/any_of/none_of/
              date_eq/before/after/not_before/not_after）
          * 已经是 dws 原生结构（含 "operator" 键）→ 原样透传，但里面的业务字段名会被替换成 fieldId

        注意：singleSelect/multipleSelect 的过滤值传**选项名字面量**，不要传 option id；
        date 字段只能用 date_eq/before/after/not_before/not_after，通用 eq/gte 会静默返回 0 条。
        """
        if filter is None:
            return None
        if isinstance(filter, dict) and "operator" in filter:
            return self._map_raw_filter(table_key, filter)
        if isinstance(filter, dict):
            items = list(filter.items())
        elif isinstance(filter, (list, tuple)):
            items = []
            for part in filter:
                if isinstance(part, dict):
                    items.extend(part.items())
        else:
            raise ValueError("filter 只能是 dict / list[dict] / dws 原生结构，实得 %s"
                             % type(filter).__name__)
        operands = []
        for key, value in items:
            fid = self.field_id(table_key, key)
            if isinstance(value, dict) and "op" in value:
                op = value["op"]
                val_ = value.get("value")
                if op in ("exist", "un_exist"):
                    operands.append({"operator": op, "operands": [fid]})
                else:
                    operands.append({"operator": op, "operands": [fid, val_]})
            elif isinstance(value, (list, tuple, set, frozenset)):
                vals = list(value)
                if len(vals) == 1:
                    operands.append({"operator": "eq", "operands": [fid, vals[0]]})
                else:
                    operands.append({"operator": "or",
                                     "operands": [{"operator": "eq", "operands": [fid, v]}
                                                  for v in vals]})
            elif value is None:
                operands.append({"operator": "un_exist", "operands": [fid]})
            else:
                operands.append({"operator": "eq", "operands": [fid, value]})
        if not operands:
            return None
        if len(operands) == 1:
            only = operands[0]
            # 只有一个条件且它本身就是并列关系（如同一字段多值 OR）→ 直接当最外层，
            # dws 要求 filters 最外层必须是 and / or
            if only.get("operator") in ("and", "or"):
                return only
            return {"operator": "and", "operands": operands}
        return {"operator": "and", "operands": operands}

    def _map_raw_filter(self, table_key: str, node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "operands" and isinstance(v, list) and v and isinstance(v[0], str):
                    head = v[0]
                    try:
                        head = self.field_id(table_key, head)
                    except AITableConfigError:
                        pass
                    out[k] = [head] + [self._map_raw_filter(table_key, x) for x in v[1:]]
                else:
                    out[k] = self._map_raw_filter(table_key, v)
            return out
        if isinstance(node, list):
            return [self._map_raw_filter(table_key, x) for x in node]
        return node

    def query_records(self, table_key: str, filter: Any = None,
                      fields: Optional[Sequence[str]] = None, limit: int = 100,
                      all_pages: bool = False, sort: Optional[Sequence[Dict[str, str]]] = None,
                      record_ids: Optional[Sequence[str]] = None,
                      cursor: Optional[str] = None,
                      max_pages: int = 50) -> List[Dict[str, Any]]:
        """查询记录，返回归一化后的 list[dict]。

        每条形如::

            {"record_id": "recXXX",
             "cells": {"phone": "138...", "education": "本科", "skills": ["PLC", "CAD"]},
             "raw":   {"bYoewPy": "138...", "cWX4BSj": {"id": "opt..", "name": "本科"}}}

        `cells` 的 key 是**业务字段名**、值已过 `val()`（singleSelect 取到 name、
        multipleSelect 是 name 列表但**不保序**）；`raw` 保留 fieldId → 原始值供排查。

        分页：`limit ≤ 100`（服务端硬限制）。`all_pages=True` 时本层**自己用 `--cursor` 翻页**，
        每页一次 dws 调用，翻到空页或 `max_pages`（默认 50 页 ≈5000 条）为止。

        ⚠️ 实测坑：**`record query --all` 在当前 dws 版本返回 0 条记录**（带 filter、不带
        filter 都一样，`hasMore`/`nextCursor` 也都是 null），所以本层绝不用 `--all`。
        """
        tid = self.table_id(table_key)
        args = ["aitable", "record", "query", "--base-id", self.base_id, "--table-id", tid]
        if record_ids:
            ids = [r for r in record_ids if r]
            if len(ids) > MAX_RECORD_IDS_PER_CALL:
                # 超过 100 个 id 只能分片（服务端硬限制）
                out: List[Dict[str, Any]] = []
                for chunk in self.runner.chunks(ids, MAX_RECORD_IDS_PER_CALL):
                    out.extend(self.query_records(table_key, fields=fields,
                                                  record_ids=list(chunk)))
                return out
            args += ["--record-ids", ",".join(ids)]
        else:
            f = self.build_filter(table_key, filter)
            if f is not None:
                args += ["--filters", json.dumps(f, ensure_ascii=False)]
            if sort:
                args += ["--sort", json.dumps(
                    [{"fieldId": self.field_id(table_key, s["field_key"])
                      if "field_key" in s else s.get("fieldId"),
                      "direction": s.get("direction", "asc")} for s in sort],
                    ensure_ascii=False)]
        if fields:
            fids = [self.field_id(table_key, k) for k in fields]
            if len(fids) > MAX_QUERY_LIMIT:
                self.warnings.append("query_records 字段数 %d 超过单次上限 %d，已截断"
                                     % (len(fids), MAX_QUERY_LIMIT))
            args += ["--field-ids", ",".join(fids[:MAX_QUERY_LIMIT])]
        lim = max(1, min(int(limit or MAX_QUERY_LIMIT), MAX_QUERY_LIMIT))
        args += ["--limit", str(lim)]

        page_args = list(args) + (["--cursor", cursor] if cursor else [])
        out = []
        pages = 0
        while True:
            res = self.runner.call(page_args, timeout=180)
            data = res["data"] if isinstance(res["data"], dict) else {}
            page = self._normalize_records(table_key, data)
            out.extend(page)
            pages += 1
            if record_ids or not all_pages:
                nc = data.get("nextCursor") or data.get("cursor")
                if nc and not record_ids and not all_pages and len(page) >= lim:
                    self.warnings.append(
                        "query_records 结果可能被截断（还有下一页 nextCursor=%s）；"
                        "需要全量请传 all_pages=True" % nc)
                break
            nc = data.get("nextCursor") or data.get("cursor")
            if not nc or not page or pages >= max_pages:
                if pages >= max_pages and nc:
                    self.warnings.append("query_records 翻到 max_pages=%d 仍有下一页，已停止"
                                         % max_pages)
                break
            page_args = list(args) + ["--cursor", str(nc)]
        return out

    def _normalize_records(self, table_key: str, data: Any) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        raw_records = data.get("records") or data.get("items") or []
        rev = self._fid_map(table_key)
        out = []
        for r in raw_records:
            if not isinstance(r, dict):
                continue
            rid = r.get("recordId") or r.get("record_id") or r.get("id")
            raw_cells = r.get("cells") or r.get("fields") or r.get("values") or {}
            cells, unknown = {}, {}
            for fid, v in raw_cells.items():
                bkey = rev.get(fid)
                if bkey:
                    cells[bkey] = val(v)
                else:
                    unknown[fid] = val(v)
            item = {"record_id": rid, "cells": cells, "raw": raw_cells}
            if unknown:
                item["unknown_fields"] = unknown
            out.append(item)
        return out

    # -- 批量写 -----------------------------------------------------------
    def _write_chunk(self, cmd: str, tid: str, chunk: List[Dict[str, Any]],
                     origin: List[Dict[str, Any]], id_keys: Sequence[str],
                     isolate: bool) -> Tuple[List[Any], List[Dict[str, Any]], int]:
        """提交一片记录（一次 dws 调用）；整片失败时**二分递归**定位坏行。

        为什么必须二分：实测服务端校验是**整批原子**的 —— 一片 100 条里只要有 1 个
        非法值（例如 email 格式不合法 `invalid email format: a@b.c`），整片全部写不进去，
        错误消息还不告诉你是第几条。二分定位一条坏行的额外成本只有 ~log2(N) 次调用
        （100 条 ≈ 7 次），远好过逐条重放的 100 次，也好过整批丢弃（契约 D6：失败可见）。

        权限类错误（401/403）**不二分不重试**：整片直接进 failed 并保留原始错误码。

        返回 (成功 recordId 列表, failed 列表, 二分额外调用次数)。
        """
        args = ["aitable", "record", cmd, "--base-id", self.base_id, "--table-id", tid]
        try:
            res = self.runner.call_with_payload(args, list(chunk), timeout=240)
        except DwsError as exc:
            if exc.category == "auth" or not isolate or len(chunk) == 1:
                return [], [{"row_index": o.get("index"), "row": o.get("row"),
                             "reason": "%s 失败：%s" % (cmd, exc.message[:200]),
                             "code": exc.code, "category": exc.category} for o in origin], 0
            mid = len(chunk) // 2
            li, lf, lc = self._write_chunk(cmd, tid, chunk[:mid], origin[:mid], id_keys, isolate)
            ri, rf, rc = self._write_chunk(cmd, tid, chunk[mid:], origin[mid:], id_keys, isolate)
            extra = lc + rc + 1
            self.warnings.append(
                "record %s 一片 %d 条整体失败（%s/%s），已二分定位坏行，额外 %d 次调用"
                % (cmd, len(chunk), exc.category, exc.code, extra))
            return li + ri, lf + rf, extra
        ids = self._extract_ids(res["data"], id_keys)
        failed: List[Dict[str, Any]] = []
        data = res["data"] if isinstance(res["data"], dict) else {}
        if isinstance(data, dict) and data.get("failedCount"):
            self.warnings.append("record %s 返回 failedCount=%s" % (cmd, data.get("failedCount")))
        if ids and len(ids) < len(chunk):
            self.warnings.append("record %s 返回 %d 个 id < 提交 %d 条，未返回 id 的行需回读确认"
                                 % (cmd, len(ids), len(chunk)))
            for o in origin[len(ids):]:
                failed.append({"row_index": o.get("index"), "row": o.get("row"),
                               "reason": "服务端未返回 recordId（可能未写入），需回读确认"})
        if not ids and cmd == "update":
            ids = [o.get("record_id") for o in origin if o.get("record_id")]
        return ids, failed, 0

    def batch_create(self, table_key: str, rows: Sequence[Dict[str, Any]],
                     keep_empty: bool = False, isolate_failures: bool = True) -> Dict[str, Any]:
        """批量新增。内部按 100 条分片（服务端硬限制），每片**一次** dws 调用。

        `rows`：业务字段名 → 值 的 dict 列表（`_meta` / `record_id` 等下划线开头的键会被忽略）。
        整片被服务端拒（如某个 email 格式非法）时，`isolate_failures=True` 会二分定位坏行、
        只把坏行记进 failed，好行照常入库。

        返回::

            {"created": n, "failed": [{"row_index":i,"row":{...},"reason":"业务话","code":...}],
             "record_ids": [...], "submitted": n, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        tid = self.table_id(table_key)
        payloads, failed = [], []
        origin: List[Dict[str, Any]] = []
        for i, row in enumerate(rows or []):
            cells, errors = self.build_cells(table_key, row, keep_empty)
            if errors:
                failed.append({"row_index": i, "row": row, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row_index": i, "row": row, "reason": "该行没有任何可写字段"})
                continue
            payloads.append({"cells": cells})
            origin.append({"index": i, "row": row})

        record_ids: List[Any] = []
        extra_calls = 0
        for start in range(0, len(payloads), MAX_RECORDS_PER_CALL):
            chunk = payloads[start:start + MAX_RECORDS_PER_CALL]
            ori = origin[start:start + MAX_RECORDS_PER_CALL]
            ids, f, extra = self._write_chunk("create", tid, chunk, ori,
                                              ("newRecordIds", "recordIds",
                                               "createdRecordIds", "records"),
                                              isolate_failures)
            record_ids.extend([r for r in ids if r])
            failed.extend(f)
            extra_calls += extra
        return {"created": len(record_ids), "failed": failed, "record_ids": record_ids,
                "submitted": len(payloads), "isolate_extra_calls": extra_calls,
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_update(self, table_key: str, updates: Sequence[Dict[str, Any]],
                     keep_empty: bool = False, isolate_failures: bool = True) -> Dict[str, Any]:
        """批量更新。`updates` = [{"record_id": "recXXX", "cells": {业务字段名: 值}}, ...]。

        一次调用最多 100 条（超出自动分片）。只传需要改的字段，未传的保持原值。
        **attachment 字段是整体覆盖不是追加**：要保留原附件必须把原 fileToken 一起写回。
        """
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        tid = self.table_id(table_key)
        payloads, failed = [], []
        origin: List[Dict[str, Any]] = []
        for i, u in enumerate(updates or []):
            rid = u.get("record_id") or u.get("recordId")
            row = u.get("cells") if isinstance(u.get("cells"), dict) else u
            if not rid:
                failed.append({"row_index": i, "row": u, "reason": "缺 record_id"})
                continue
            cells, errors = self.build_cells(table_key, row or {}, keep_empty)
            if errors:
                failed.append({"row_index": i, "row": u, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row_index": i, "row": u, "reason": "没有可更新的字段"})
                continue
            payloads.append({"recordId": rid, "cells": cells})
            origin.append({"index": i, "row": u, "record_id": rid})

        updated_ids: List[Any] = []
        extra_calls = 0
        for start in range(0, len(payloads), MAX_RECORDS_PER_CALL):
            chunk = payloads[start:start + MAX_RECORDS_PER_CALL]
            ori = origin[start:start + MAX_RECORDS_PER_CALL]
            ids, f, extra = self._write_chunk("update", tid, chunk, ori,
                                              ("updatedRecordIds", "recordIds", "records"),
                                              isolate_failures)
            updated_ids.extend([r for r in ids if r])
            failed.extend(f)
            extra_calls += extra
        return {"updated": len(updated_ids), "failed": failed,
                "record_ids": [p["recordId"] for p in payloads],
                "submitted": len(payloads), "isolate_extra_calls": extra_calls,
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_update_verified(self, table_key: str, updates: Sequence[Dict[str, Any]],
                              chunk_size: int = SAFE_UPDATE_CHUNK,
                              settle_tries: int = 2,
                              fallback_single: bool = False) -> Dict[str, Any]:
        """`batch_update` + 写后回读 + **有界**重试。回填统计数字这类「必须写成功」的场景用它。

        为什么需要它（实测坑，见模块文档第 8 条）：
          * 对「刚批量创建出来的记录」发 `record update`，会返回 success + recordIds，
            但随后 34s / 47s / 156s 连续轮询读回**全是旧值**，约 4 分钟后才读到正确值。
          * 那段时间里当场怎么重试都没用（实测一轮 41 次调用 / 70 秒全废，含逐条重发）。
          * 所以本方法只做**有界**重试（每片重发一次），然后把仍未生效的记录如实报进
            `failed`（契约 D6 失败可见 + D7 后续回合重跑该步），**绝不空转烧调用**。

        流程（每片 ≤ chunk_size 条，默认 10）：update → readback_verify(expected=本次写入值)
        → 不一致就把该片重发一次 → 仍不一致 → 记 failed。
        `fallback_single=True` 时再逐条重发一次（默认关闭：实测「一开始就逐条发」有效，
        但「批量发失败后再逐条补」在延迟期内同样无效）。

        返回::

            {"updated": n, "verified": n, "recovered": n, "failed": [{"record_id","reason"}],
             "verify_ok": bool, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        normalized: List[Tuple[str, Dict[str, Any]]] = []
        failed: List[Dict[str, Any]] = []
        for u in updates or []:
            rid = u.get("record_id") or u.get("recordId")
            row = u.get("cells") if isinstance(u.get("cells"), dict) else u
            if not rid:
                failed.append({"record_id": None, "reason": "缺 record_id"})
                continue
            normalized.append((rid, dict(row or {})))

        verified = 0
        recovered = 0
        size = max(1, int(chunk_size))
        for start in range(0, len(normalized), size):
            piece = normalized[start:start + size]
            pending = piece
            for attempt in (1, 2):
                res = self.batch_update(table_key,
                                        [{"record_id": rid, "cells": cells}
                                         for rid, cells in pending])
                failed.extend(res["failed"])
                expected = {rid: cells for rid, cells in pending}
                rb = self.readback_verify(table_key, [rid for rid, _ in pending],
                                          sorted({k for _, c in pending for k in c}),
                                          expected=expected, settle_tries=settle_tries)
                bad = {m["record_id"] for m in rb["mismatch"]} | set(rb["missing"])
                if not bad:
                    verified += len(pending)
                    if attempt == 2:
                        recovered += len(pending)
                    pending = []
                    break
                pending = [(rid, cells) for rid, cells in pending if rid in bad]
                self.warnings.append(
                    "batch_update_verified(%s) 第 %d 片第 %d 次写入后仍有 %d 条未生效"
                    % (table_key, start // size + 1, attempt, len(pending)))
            if pending and fallback_single:
                # 逐条重发（仅在「一开始就逐条发」的场景实测有效；丢写生效期间同样无效）
                for rid, cells in pending:
                    self.batch_update(table_key, [{"record_id": rid, "cells": cells}])
                rb = self.readback_verify(table_key, [rid for rid, _ in pending],
                                          sorted({k for _, c in pending for k in c}),
                                          expected={rid: cells for rid, cells in pending},
                                          settle_tries=settle_tries)
                bad = {m["record_id"] for m in rb["mismatch"]} | set(rb["missing"])
                good = [rid for rid, _ in pending if rid not in bad]
                verified += len(good)
                recovered += len(good)
                pending = [(rid, cells) for rid, cells in pending if rid in bad]
            for rid, _ in pending:
                failed.append({
                    "record_id": rid,
                    "reason": "写入返回成功但当场回读不到新值（实测：新建记录的批量 update "
                              "可能要几分钟后才可读，期间重试无效）。请在后续回合重跑本步回填复核；"
                              "详见 aitable_io.batch_update_verified 文档串",
                    "category": "server_write_lag"})
        return {"updated": len(normalized), "verified": verified, "recovered": recovered,
                "failed": failed, "verify_ok": not failed,
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_delete(self, table_key: str, record_ids: Sequence[str]) -> Dict[str, Any]:
        """批量删除（不可逆）。一次最多 100 个 id，超出自动分片。"""
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        tid = self.table_id(table_key)
        ids = [r for r in (record_ids or []) if r]
        failed, deleted = [], 0
        for chunk in self.runner.chunks(ids, MAX_RECORD_IDS_PER_CALL):
            try:
                self.runner.call(["aitable", "record", "delete", "--base-id", self.base_id,
                                  "--table-id", tid, "--record-ids", ",".join(chunk)],
                                 timeout=180)
                deleted += len(chunk)
            except DwsError as exc:
                failed.append({"record_ids": list(chunk),
                               "reason": "批量删除失败：%s" % exc.message[:200],
                               "code": exc.code, "category": exc.category})
        return {"deleted": deleted, "failed": failed,
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def batch_upsert_by_key(self, table_key: str, unique_field: str,
                            rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """按业务唯一键幂等写入（契约 D12）。

        流程（**调用次数已压到最小**）：
          1. 一次 OR filter 查询把 N 个键全部查出来（N>100 才分片，每片仍是一次调用）；
          2. 命中 → 带 recordId 走 update；未命中 → 走 create；
          3. 用 `record upsert` 一次提交（≤100 条/片）；该命令不可用时自动回退成
             create + update 两次调用。

        返回::

            {"created": n, "updated": n, "failed": [{"row","reason","code"}],
             "record_ids": [...], "dws_calls": n, "elapsed_ms": n,
             "existing": {键: recordId}}

        纪律：库内同键命中**多条**、或本批内键重复 → 该行进 failed 并说明，
        **不自动挑一条覆盖**（沿用老插件「同号多条即停止并报告」的规则）。
        """
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        tid = self.table_id(table_key)
        fid_key = self.field_id(table_key, unique_field)
        failed: List[Dict[str, Any]] = []

        # --- 收集键 ---
        keyed: List[Tuple[Any, Dict[str, Any]]] = []
        for i, row in enumerate(rows or []):
            k = row.get(unique_field)
            k = val(k)
            if k is None or (isinstance(k, str) and not k.strip()):
                failed.append({"row_index": i, "row": row,
                               "reason": "缺唯一键 %s，无法幂等判定" % unique_field})
                continue
            keyed.append((k if not isinstance(k, str) else k.strip(), row))

        # --- 本批内重复键：后来的进 failed，不静默覆盖 ---
        seen_in_batch: Dict[Any, int] = {}
        deduped: List[Tuple[Any, Dict[str, Any]]] = []
        for k, row in keyed:
            if k in seen_in_batch:
                failed.append({"row": row, "reason": "本批内唯一键 %s=%s 重复（第 %d 行已用该键），"
                                                    "未写入" % (unique_field, k, seen_in_batch[k])})
                continue
            seen_in_batch[k] = len(deduped)
            deduped.append((k, row))

        # --- 一次性批量查已存在键（不逐条查！） ---
        existing: Dict[Any, str] = {}
        duplicated: Dict[Any, int] = {}
        keys = [k for k, _ in deduped]
        row_of = {k: row for k, row in deduped}
        for chunk in self.runner.chunks(keys, MAX_FILTER_OPERANDS):
            try:
                found = self.query_records(table_key, filter={unique_field: list(chunk)},
                                           fields=[unique_field], limit=MAX_QUERY_LIMIT,
                                           all_pages=True)
            except DwsError as exc:
                for k in chunk:
                    failed.append({"row": row_of.get(k),
                                   "reason": "查重查询失败：%s" % exc.message[:200],
                                   "code": exc.code, "category": exc.category})
                continue
            for rec in found:
                v = val((rec.get("raw") or {}).get(fid_key))
                if isinstance(v, str):
                    v = v.strip()
                if v is None:
                    continue
                if v in existing:
                    duplicated[v] = duplicated.get(v, 1) + 1
                else:
                    existing[v] = rec["record_id"]

        # --- 拆 create / update ---
        payloads = []
        for k, row in deduped:
            if k in duplicated:
                failed.append({"row": row,
                               "reason": "库内 %s=%s 命中 %d 条记录，停止不自动选（需人工确认）"
                                         % (unique_field, k, duplicated[k])})
                continue
            cells, errors = self.build_cells(table_key, row)
            if errors:
                failed.append({"row": row, "reason": "; ".join(errors)})
                continue
            if not cells:
                failed.append({"row": row, "reason": "该行没有任何可写字段"})
                continue
            rid = existing.get(k)
            payloads.append(({"recordId": rid, "cells": cells} if rid else {"cells": cells},
                             bool(rid), row))

        created = updated = 0
        record_ids: List[str] = []
        if payloads:
            created, updated, record_ids, fail2 = self._submit_upsert(tid, payloads)
            failed.extend(fail2)

        return {"created": created, "updated": updated, "failed": failed,
                "record_ids": record_ids, "existing": {str(k): v for k, v in existing.items()},
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}

    def _submit_upsert(self, tid: str, payloads: Sequence[Tuple[Dict[str, Any], bool, Dict[str, Any]]]
                       ) -> Tuple[int, int, List[str], List[Dict[str, Any]]]:
        """提交 upsert：优先 `record upsert`（一次调用同时处理增改），失败回退 create+update。"""
        created = updated = 0
        record_ids: List[str] = []
        failed: List[Dict[str, Any]] = []
        for chunk in self.runner.chunks(list(payloads), MAX_RECORDS_PER_CALL):
            body = [p for p, _, _ in chunk]
            try:
                res = self.runner.call_with_payload(
                    ["aitable", "record", "upsert", "--base-id", self.base_id,
                     "--table-id", tid], body, timeout=240)
            except DwsError as exc:
                if exc.category in ("invalid", "not_found", "unknown"):
                    self.warnings.append("record upsert 不可用（%s/%s），回退 create+update"
                                         % (exc.category, exc.code))
                    c, u, ids, f2 = self._submit_split(tid, chunk)
                    created += c
                    updated += u
                    record_ids.extend(ids)
                    failed.extend(f2)
                    continue
                for _, _, row in chunk:
                    failed.append({"row": row, "reason": "upsert 失败：%s" % exc.message[:200],
                                   "code": exc.code, "category": exc.category})
                continue
            data = res["data"] or {}
            c_ids = self._extract_ids(data, ("createdRecordIds", "newRecordIds"))
            u_ids = self._extract_ids(data, ("updatedRecordIds",))
            if not c_ids and not u_ids:
                # 服务端没回 id 明细：按提交内容推断
                c_ids = [None] * sum(1 for _, is_upd, _ in chunk if not is_upd)
                u_ids = [None] * sum(1 for _, is_upd, _ in chunk if is_upd)
                self.warnings.append("record upsert 未返回 id 明细，按提交内容计数")
            created += len(c_ids)
            updated += len(u_ids)
            record_ids.extend([r for r in (list(c_ids) + list(u_ids)) if r])
        return created, updated, record_ids, failed

    def _submit_split(self, tid: str, chunk: Sequence[Tuple[Dict[str, Any], bool, Dict[str, Any]]]
                      ) -> Tuple[int, int, List[str], List[Dict[str, Any]]]:
        """upsert 不可用时的回退路径：拆成 create + update 两次调用（各自也能二分定位坏行）。"""
        creates = [(p, row) for p, is_upd, row in chunk if not is_upd]
        updates = [(p, row) for p, is_upd, row in chunk if is_upd]
        created = updated = 0
        ids: List[str] = []
        failed: List[Dict[str, Any]] = []
        if creates:
            cids, cf, _ = self._write_chunk(
                "create", tid, [p for p, _ in creates],
                [{"index": None, "row": row} for _, row in creates],
                ("newRecordIds", "recordIds", "records"), True)
            created += len(cids)
            ids.extend([r for r in cids if r])
            failed.extend(cf)
        if updates:
            uids, uf, _ = self._write_chunk(
                "update", tid, [p for p, _ in updates],
                [{"index": None, "row": row, "record_id": p["recordId"]}
                 for p, row in updates],
                ("updatedRecordIds", "recordIds", "records"), True)
            updated += len(uids)
            ids.extend([r for r in uids if r])
            failed.extend(uf)
        return created, updated, ids, failed

    @staticmethod
    def _extract_ids(data: Any, keys: Sequence[str]) -> List[Any]:
        if not isinstance(data, dict):
            return []
        for k in keys:
            v = data.get(k)
            if isinstance(v, list) and v:
                out = []
                for it in v:
                    if isinstance(it, dict):
                        out.append(it.get("recordId") or it.get("record_id") or it.get("id"))
                    else:
                        out.append(it)
                return [x for x in out if x]
            if isinstance(v, list):
                return []
        return []

    # -- 附件 -------------------------------------------------------------
    def upload_attachment(self, file_path: Any, concurrency: int = DEFAULT_UPLOAD_CONCURRENCY) -> Dict[str, Any]:
        """上传单个附件，返回::

            {"ok": bool, "path": str, "file_name": str, "size": int, "mime_type": str,
             "fileToken": str|None, "cell": [{"fileToken": "ft_.."}]|None,
             "elapsed_ms": int, "error": str|None, "code": ..., "category": ...}

        `concurrency` 参数是为与 `upload_attachments` 对齐而保留（单文件用不上）；
        传 list 时自动等价于 `upload_attachments(file_path, concurrency)` 的第一项。
        """
        if isinstance(file_path, (list, tuple)):
            results = self.upload_attachments(list(file_path), concurrency=concurrency)
            return results[0] if results else {"ok": False, "error": "空文件列表"}
        return self._upload_one(file_path)

    def upload_attachments(self, file_paths: Sequence[str],
                           concurrency: int = DEFAULT_UPLOAD_CONCURRENCY) -> List[Dict[str, Any]]:
        """并发上传多个附件（契约 D5：默认并发 5，API 限 20 QPS 留余量）。

        返回顺序与入参一致。每项含 `cell`，可直接塞进 rows 的 attachment 字段::

            rows[i]["attachment"] = results[i]["cell"]

        单个文件 3 步：`attachment upload`（1 次 dws 调用）→ urllib PUT 到 OSS → 返回 fileToken。
        附件**没有批量接口**，所以并发是唯一优化手段。
        """
        paths = list(file_paths or [])
        if not paths:
            return []
        conc = max(1, min(int(concurrency or 1), 20))
        if conc == 1 or len(paths) == 1:
            return [self._upload_one(p) for p in paths]
        results: List[Optional[Dict[str, Any]]] = [None] * len(paths)
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
            fut2idx = {ex.submit(self._upload_one, p): i for i, p in enumerate(paths)}
            for fut in concurrent.futures.as_completed(fut2idx):
                i = fut2idx[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:      # 不让一个文件炸掉整批（D6 失败可见）
                    results[i] = {"ok": False, "path": str(paths[i]),
                                  "file_name": Path(str(paths[i])).name,
                                  "error": "%s: %s" % (type(exc).__name__, exc),
                                  "category": "unknown", "fileToken": None, "cell": None,
                                  "elapsed_ms": 0, "size": 0, "mime_type": None}
        return [r for r in results if r is not None]

    def _upload_one(self, file_path: Any) -> Dict[str, Any]:
        t0 = time.monotonic()
        p = Path(str(file_path)).expanduser()
        base = {"ok": False, "path": str(p), "file_name": p.name, "size": 0,
                "mime_type": None, "fileToken": None, "cell": None,
                "error": None, "code": None, "category": None}
        try:
            if not p.exists() or not p.is_file():
                base.update(error="文件不存在或不是文件", category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base
            size = p.stat().st_size
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            base["size"], base["mime_type"] = size, mime
            if size <= 0:
                base.update(error="文件为空", category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base
            if size > MAX_ATTACHMENT_SIZE:
                base.update(error="文件过大 %d 字节（上限 %d）" % (size, MAX_ATTACHMENT_SIZE),
                            category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            # 步骤 1：申请 OSS 直传地址（1 次 dws 调用）
            res = self.runner.call(["aitable", "attachment", "upload",
                                    "--base-id", self.base_id, "--file-name", p.name,
                                    "--size", str(size), "--mime-type", mime], timeout=120)
            data = res["data"] or {}
            upload_url = data.get("uploadUrl") or data.get("upload_url")
            token = data.get("fileToken") or data.get("file_token")
            if not upload_url or not token:
                base.update(error="attachment upload 未返回 uploadUrl/fileToken：%s"
                                  % json.dumps(data, ensure_ascii=False)[:200],
                            category="invalid",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            # 步骤 2：PUT 到 OSS（Content-Type 必须是文件的具体 MIME type）
            err = self._put_to_oss(upload_url, p, mime, data.get("headers") or {})
            if err:
                base.update(error=err, category="network",
                            elapsed_ms=int((time.monotonic() - t0) * 1000))
                return base

            base.update(ok=True, fileToken=token, cell=[{"fileToken": token}],
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base
        except DwsError as exc:
            base.update(error=exc.message[:300], code=exc.code, category=exc.category,
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base
        except OSError as exc:
            base.update(error="读文件失败：%s" % exc, category="invalid",
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
            return base

    @staticmethod
    def _put_to_oss(upload_url: str, path: Path, mime: str,
                    extra_headers: Optional[Dict[str, str]] = None,
                    attempts: int = 3) -> Optional[str]:
        """把文件 PUT 到 OSS 预签名地址。返回 None 表示成功，否则返回错误描述。"""
        last = "unknown"
        for i in range(attempts):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
                req = urllib.request.Request(upload_url, data=body, method="PUT")
                req.add_header("Content-Type", mime)
                for k, v in (extra_headers or {}).items():
                    req.add_header(str(k), str(v))
                with urllib.request.urlopen(req, timeout=OSS_PUT_TIMEOUT) as resp:
                    if 200 <= int(resp.status) < 300:
                        return None
                    last = "OSS 返回 HTTP %s" % resp.status
            except urllib.error.HTTPError as exc:
                last = "OSS PUT HTTP %s: %s" % (exc.code, exc.reason)
                if exc.code in (401, 403):        # 预签名过期/签名不符，重试无益
                    return last
            except urllib.error.URLError as exc:
                last = "OSS PUT 网络错误: %s" % getattr(exc, "reason", exc)
            except OSError as exc:
                last = "OSS PUT 失败: %s" % exc
            if i < attempts - 1:
                time.sleep(1.0 * (2 ** i))
        return last

    # -- 选项 -------------------------------------------------------------
    def get_field_options(self, table_key: str, field_key: str,
                          use_cache: bool = False) -> List[Dict[str, Any]]:
        """读回单个字段的选项 [{"id","name"}]（一次 `field get`，≤10 字段/次的限制这里只用 1 个）。"""
        if use_cache:
            cached = (self._options_cache.get(table_key) or {}).get(field_key)
            if cached:
                return [{"id": o.get("id"), "name": o.get("name")} for o in cached]
        fid = self.field_id(table_key, field_key)
        res = self.runner.call(["aitable", "field", "get", "--base-id", self.base_id,
                                "--table-id", self.table_id(table_key),
                                "--field-ids", fid], timeout=120)
        return self._parse_options(res["data"], fid)

    @staticmethod
    def _parse_options(data: Any, fid: Optional[str] = None) -> List[Dict[str, Any]]:
        fields = []
        if isinstance(data, dict):
            fields = data.get("fields") or data.get("items") or []
        elif isinstance(data, list):
            fields = data
        for f in fields:
            if not isinstance(f, dict):
                continue
            if fid and (f.get("fieldId") or f.get("id")) != fid:
                continue
            cfg = f.get("config") or f.get("property") or {}
            opts = cfg.get("options") or []
            return [{"id": o.get("id") or o.get("optionId"), "name": o.get("name")}
                    for o in opts if isinstance(o, dict)]
        return []

    def ensure_options(self, table_key: str, field_key: str, names: Sequence[str],
                       settle_tries: int = 5) -> List[Dict[str, Any]]:
        """确保 singleSelect/multipleSelect 的选项池最终包含 `names`，返回现有 [{"id","name"}]。

        ⚠️ **2026-09-17 起为「只读 + 延迟补建」实现**（缺陷1 根治；契约 §3.2 的签名与
        返回结构不变，只增不删）。本方法**不再调用 `field update`**，只做三件事：
          ① `field get` **只读**拉回现有选项（1 次 dws 调用；给 agent/report 报告选项池现状）；
          ② 把池里没有的 `names` 记进 `self.pending_options[(table_key, field_key)]`
             （去重、只增不删），供调用方与审计检查「哪些选项将由服务端补建」；
          ③ 原样返回现有选项列表（**含全部已有选项及其原 id**）。
        实际补建**延迟到写记录时**：`record create/update/upsert` 写不存在的选项名，
        服务端会自动补建该选项，且**不动已有选项的 id**（W-B/W-G 实测；W-H 受控实验
        复证：21 条存量记录 585 个多选值 + 21 个单选值，intake 追加 3 个新标签后
        **零丢失**，91 个已有 option id 零 churn，新标签全部进池）。

        **为什么必须移除 `field update` 路径（生产数据丢失级根因，W-H 实验查明）**：
        ① `field get` 对字段选项配置存在**最终一致性**，可能返回任意陈旧的快照
           （W-H 实测：真实池 94 个选项时读回**建表时的 7 个**；W-B 旧记录还有
           10→9→11 的中间态）。② 旧实现用这份快照构建「全量 payload」发给
           `field update`（整体覆盖语义），并用**同一份快照**做读回校验：
           快照陈旧时 payload 会漏掉大量现有选项、或带上仍在传播中的**中间态 id**，
           服务端据此重建选项 → option id 被重新分配（churn）；而存量记录的多选/
           单选单元格是按 option id 引用的，id 一变旧引用悬空、值被**静默清空**
           （W-G 在 G base 实测：一次追加把 27 条简历的「技能标签」清掉大半，
           30→1、10→0、40→null；选项重写之后创建的记录完好）。③ 校验基线
           before_ids 同样来自陈旧快照，对「快照里没见到/见到中间态 id」的选项
           **检不出丢失**（W-H 对照实验：payload 只含 10 个选项时旧校验照样通过）。
        是否触发全凭读回快照的新鲜度 = 时序运气（W-H 两轮对照实验：一轮读到陈旧
        7/94 快照、一轮读到全量新鲜快照，都恰好没触发丢值；W-G 在 G base 一轮就
        丢大半）——**这条路径永远无法安全**，唯一根治是不触发 `field update`。
        选项上限 3000/字段（记忆库口径），intake 场景远达不到。

        `settle_tries` 参数仅为兼容旧签名保留（只读实现没有写传播延迟可轮询），传任意值等效。
        """
        # field_id 先行校验：config 缺映射时照旧抛 AITableConfigError（调用方已有兜底）
        self.field_id(table_key, field_key)
        existing = self.get_field_options(table_key, field_key)
        by_name = {o.get("name") for o in existing if o.get("name") is not None}
        pending = self.pending_options.setdefault((table_key, field_key), [])
        missing: List[str] = []
        for n in names or []:
            n = self.sanitize_text(str(n)).strip()
            if n and n not in by_name and n not in pending:
                pending.append(n)
                missing.append(n)
        if missing:
            self.warnings.append(
                "ensure_options(%s.%s)：%d 个新选项不在当前选项池（%s%s）；"
                "本层已**不再执行 field update**（会触发 option id churn、静默清空存量"
                "单元格，见方法文档串），这些名字已记入 pending_options，写记录时由服务端"
                "自动补建（不动已有选项 id）"
                % (table_key, field_key, len(missing), "、".join(missing[:5]),
                   "…" if len(missing) > 5 else ""))
        self._options_cache.setdefault(table_key, {})[field_key] = existing
        return existing

    def pending_option_names(self, table_key: str, field_key: str) -> List[str]:
        """`ensure_options` 记下的「待服务端在写记录时自动补建」的选项名（只读视图）。"""
        return list(self.pending_options.get((table_key, field_key), []))

    # -- 回读校验 ---------------------------------------------------------
    def readback_verify(self, table_key: str, record_ids: Sequence[str],
                        field_keys: Sequence[str],
                        expected: Optional[Dict[str, Dict[str, Any]]] = None,
                        settle_tries: int = 3, settle_wait: float = 1.2) -> Dict[str, Any]:
        """写后必回读（契约 D6）。一次 `record query --record-ids`（≤100 个/次，超出自动分片）。

        `expected` 可选：{record_id: {业务字段名: 期望值}}。给了就顺手比对，
        比对规则用 `values_equal`（多选按集合、数字按数值、日期按前缀），
        避免「多选读回不保序」造成假失败。

        ⚠️ **实测：写入到可读有传播延迟**（update 后 ≈1.2s、create 后按条件查 ≈2.7s 才可命中）。
        所以给了 `expected` 时本方法会**轮询**：不一致就等 `settle_wait` 秒再读，
        最多 `settle_tries` 次（默认 3 次 → 1.2s/2.4s/3.6s），避免把「还没同步」误报成「写错了」。
        不给 expected 时只读一次（无从判断对错，也就不轮询）。

        返回::

            {"ok": bool, "requested": n, "found": n, "missing": [record_id...],
             "records": [{"record_id":..,"cells":{..}}],
             "values": {record_id: {field_key: value}},
             "mismatch": [{"record_id":..,"field":..,"expected":..,"actual":..}],
             "settle_polls": n, "dws_calls": n, "elapsed_ms": n}
        """
        t0 = time.monotonic()
        calls0 = self.runner.counter.calls
        ids = [r for r in (record_ids or []) if r]
        polls = 0
        recs, values, missing, mismatch = [], {}, [], []
        while True:
            recs = self.query_records(table_key, fields=list(field_keys or []) or None,
                                      record_ids=ids) if ids else []
            values = {r["record_id"]: r["cells"] for r in recs}
            missing = [r for r in ids if r not in values]
            mismatch = []
            if expected:
                for rid, exp in expected.items():
                    got = values.get(rid)
                    if got is None:
                        continue
                    for fk, ev in exp.items():
                        if not values_equal(ev, got.get(fk)):
                            mismatch.append({"record_id": rid, "field": fk,
                                             "expected": val(ev), "actual": got.get(fk)})
            if expected is None or (not mismatch and not missing) or polls >= settle_tries:
                break
            polls += 1
            time.sleep(settle_wait * polls)
        if polls and (mismatch or missing):
            self.warnings.append(
                "readback_verify(%s) 轮询 %d 次后仍有 %d 处不一致 / %d 条读不到"
                % (table_key, polls, len(mismatch), len(missing)))
        return {"ok": not missing and not mismatch, "requested": len(ids),
                "found": len(recs), "missing": missing, "records": recs,
                "values": values, "mismatch": mismatch, "settle_polls": polls,
                "dws_calls": self.runner.counter.calls - calls0,
                "elapsed_ms": int((time.monotonic() - t0) * 1000)}


# ---------------------------------------------------------------------------
# 自检：python3 aitable_io.py <config.json>  → 打印 config 映射概况，不发任何写请求
# ---------------------------------------------------------------------------
def _selfcheck(config_path: str) -> int:
    t = AITable(config_path)
    print("base: %s (%s)" % (t.base_name, t.base_id))
    for key in t.table_keys():
        print("  %-8s %-12s table_id=%-10s 字段 %d 个"
              % (key, t.table_name(key), t.table_id(key), len(t.field_keys(key))))
    print("sanitize_text 自检: %r"
          % t.sanitize_text("a\x0cb\tc\nd\r\ne\x7ff"))
    print("dws_calls（自检不发网络请求）=%d" % t.dws_calls)
    return 0


if __name__ == "__main__":
    sys.exit(_selfcheck(sys.argv[1] if len(sys.argv) > 1 else "/tmp/build/testbase-config.json"))

