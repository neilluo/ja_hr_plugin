# -*- coding: utf-8 -*-
"""
table.py —— 钉钉 AI 表格 IO 层的**组合根**（契约 §3.2 冻结签名，零第三方 pip 依赖）。

原 `shared/aitable_io.py` 的 `AITable` 上帝类在 P2 被拆成 6 个各司其职的类
（client / values / schema / query / writer / uploader / verifier / optionpool），
本类只保留：config 装载的委托、组件装配、以及调用方一直在用的那层方法转发。
**调用方唯一要改的就是 import 路径**（`from aitable.table import AITable`）。

设计纪律（都是实测换来的，改代码前先读；各条的完整依据在对应模块的文档串里）：

1. **调用次数是第一优化目标。** 一次 dws 网络调用固定开销 ≈1.0~1.3s（进程启动 ~0.27s +
   鉴权/网络 ~0.7s）。所以本层所有方法都是「一次调用干完一批」：批量写 ≤100 条/次、
   查重一次 OR filter 查完 N 个键、结构读回一次 `field get` 全量、翻页自己用 `--cursor`。
   每次 subprocess 都计数，`AITable.dws_calls` / `.stats()` 就是 report 里的 `dws_calls`。
   → `aitable/client.py`

2. **绝不经过 shell。** 一律 `subprocess.run(list_of_args)`；超长 JSON 走
   `--records-file <绝对路径>`（`record create/update/upsert` 都支持）。→ `client.py`

3. **附件严禁直传 URL。** cells 里写 `{"url":"https://..."}` 会让服务端同步下载，
   10 条记录就 TIMEOUT_ERROR。必须 `attachment upload` 拿 `fileToken` → urllib PUT 到 OSS
   → cells 里写 `[{"fileToken":"ft_xxx"}]`。写入是**整体覆盖不是追加**。
   附件无批量接口（3 步/文件），只能并发：`upload_attachments(paths, concurrency=5)`
   （API 限 20 QPS，5 是留足余量的默认值，契约 D5）。→ `uploader.py` / `schema.py`

4. **写前净化、写后回读。** `sanitize_text` 去掉 `ord(c)<32` 的控制字符（保留 `\n`）与
   U+2028/2029 —— pdftotext/docx 的输出原样写入会被 API 拒收。写完用 `readback_verify`
   读回。→ `values.py` / `verifier.py`

5. **读回值必须过 `val()`。** `singleSelect` 读回是 dict（要取 `.name`），
   `multipleSelect` 是 dict 数组且**读回不保序** → 所有比对按集合（`val_set` / `values_equal`），
   不能按列表；`number` 读回是**字符串形式**的数字。→ `values.py`

6. **错误分类与重试**：网络/超时/限流/5xx → 3 次指数退避；权限类 401/403 → **不重试**，
   直接进 `failed` 并保留原始错误码（契约 D6：失败可见，禁止静默丢弃）。→ `client.py`

7. **幂等**（契约 D12）：`batch_upsert_by_key` 先**一次性**批量查出已存在键（不逐条查），
   再拆 create/update，走 `record upsert` 一次提交。→ `writer.py`

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
   因为值通常几分钟后就在了，当场重试只会白烧调用次数。** → `writer.py` 模块文档第 3 条

9. **写后读回有传播延迟**：update/create 落地到可读约 1.1~2.7s（实测中位数 ~1.3s），
   所以 `readback_verify(expected=...)` 会自动轮询（默认 3 次，1.2s/2.4s/3.6s），
   不会把「还没同步」误报成「写错了」。→ `verifier.py`

10. **`record query --all` 是坏的**：当前 dws 版本带不带 filter 都返回 0 条，
    本层一律自己用 `--cursor` 翻页。→ `query.py`

11. **选项池只增不删，且绝不 `field update`**：`field update` 会触发 option id churn、
    静默清空存量单元格（生产数据丢失级）；补建交给写记录时的服务端自动行为。
    → `optionpool.py`

12. **上限护栏**（缺陷3）：选项池 3000/字段、免费版单表 20000 行，逼近 90% 时加 warning。
    → `optionpool.py` / `writer.py`

字段类型相关的 config.json 结构见 §3.4；`types` / `formatters` / `options` 是本层额外读取的
可选段（缺失时按 text 兜底），由 replicate/bootstrap 生成，脚本内零硬编码 ID（契约 D8）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parents[1]
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from aitable.client import DwsCallCounter, DwsClient  # noqa: E402
from aitable.optionpool import OptionPool  # noqa: E402
from aitable.query import RecordQuery  # noqa: E402
from aitable.schema import TableSchema  # noqa: E402
from aitable.uploader import (  # noqa: E402
    DEFAULT_UPLOAD_CONCURRENCY,
    AttachmentUploader,
)
from aitable.values import sanitize_text  # noqa: E402
from aitable.verifier import ReadBackVerifier  # noqa: E402
from aitable.writer import SAFE_UPDATE_CHUNK, RecordWriter  # noqa: E402

__all__ = ["AITable"]


class AITable:
    """钉钉 AI 表格 IO 层的组合根。所有 table_key / field_key 一律用**业务名**，
    由 config.json 映射到真实 ID（契约 D8）。"""

    def __init__(self, config_path: str, client: Optional[DwsClient] = None,
                 counter: Optional[DwsCallCounter] = None, verbose: bool = False):
        self.schema = TableSchema(config_path)
        #: 全组件共享**同一个** warnings list，append 顺序与拆分前逐条一致
        self.warnings: List[str] = []
        self.client = client or DwsClient(counter=counter, verbose=verbose)
        self.query = RecordQuery(self.client, self.schema, self.warnings)
        self.verifier = ReadBackVerifier(self.client, self.query, self.warnings)
        self.writer = RecordWriter(self.client, self.schema, self.query,
                                   self.verifier, self.warnings)
        self.uploader = AttachmentUploader(self.client, self.schema.base_id)
        self.options = OptionPool(self.client, self.schema, self.warnings)

    # -- 计数 / 元信息 ----------------------------------------------------
    @property
    def dws_calls(self) -> int:
        """本对象（及共享同一 counter 的对象）真实发生的 dws 进程调用次数（含重试）。"""
        return self.client.counter.calls

    def stats(self) -> Dict[str, Any]:
        out = self.client.counter.snapshot()
        out["warnings"] = list(self.warnings)
        return out

    @property
    def config(self) -> Dict[str, Any]:
        return self.schema.config

    @property
    def config_path(self) -> str:
        return self.schema.config_path

    @property
    def base_id(self) -> str:
        return self.schema.base_id

    @property
    def base_name(self) -> str:
        return self.schema.base_name

    @property
    def pending_options(self) -> Dict[Tuple[str, str], List[str]]:
        return self.options.pending_options

    def table_keys(self) -> List[str]:
        return self.schema.table_keys()

    def table_id(self, table_key: str) -> str:
        return self.schema.table_id(table_key)

    def table_name(self, table_key: str) -> str:
        return self.schema.table_name(table_key)

    def field_keys(self, table_key: str) -> List[str]:
        return self.schema.field_keys(table_key)

    def field_id(self, table_key: str, field_key: str) -> str:
        return self.schema.field_id(table_key, field_key)

    def field_type(self, table_key: str, field_key: str) -> str:
        return self.schema.field_type(table_key, field_key)

    def field_formatter(self, table_key: str, field_key: str) -> Optional[str]:
        return self.schema.field_formatter(table_key, field_key)

    def format_cell(self, table_key: str, field_key: str, value: Any,
                    keep_empty: bool = False) -> Tuple[bool, Any, Optional[str]]:
        return self.schema.format_cell(table_key, field_key, value, keep_empty)

    def build_cells(self, table_key: str, row: Dict[str, Any],
                    keep_empty: bool = False) -> Tuple[Dict[str, Any], List[str]]:
        return self.schema.build_cells(table_key, row, keep_empty)

    def build_filter(self, table_key: str,
                     filter: Any = None) -> Optional[Dict[str, Any]]:
        return self.schema.build_filter(table_key, filter)

    # -- 读 ---------------------------------------------------------------
    def query_records(self, table_key: str, filter: Any = None,
                      fields: Optional[Sequence[str]] = None, limit: int = 100,
                      all_pages: bool = False,
                      sort: Optional[Sequence[Dict[str, str]]] = None,
                      record_ids: Optional[Sequence[str]] = None,
                      cursor: Optional[str] = None,
                      max_pages: int = 50) -> List[Dict[str, Any]]:
        return self.query.query_records(table_key, filter=filter, fields=fields,
                                        limit=limit, all_pages=all_pages, sort=sort,
                                        record_ids=record_ids, cursor=cursor,
                                        max_pages=max_pages)

    @property
    def last_query_truncated(self) -> bool:
        """最近一次 `query_records` 是否因 `max_pages` 截断（缺陷2：截断必须可见）。"""
        return self.query.last_truncated

    @property
    def last_query_pages(self) -> int:
        return self.query.last_pages

    @property
    def last_query_returned(self) -> int:
        return self.query.last_returned

    def readback_verify(self, table_key: str, record_ids: Sequence[str],
                        field_keys: Sequence[str],
                        expected: Optional[Dict[str, Dict[str, Any]]] = None,
                        settle_tries: int = 3,
                        settle_wait: float = 1.2) -> Dict[str, Any]:
        return self.verifier.readback_verify(table_key, record_ids, field_keys,
                                             expected=expected,
                                             settle_tries=settle_tries,
                                             settle_wait=settle_wait)

    # -- 写 ---------------------------------------------------------------
    def batch_create(self, table_key: str, rows: Sequence[Dict[str, Any]],
                     keep_empty: bool = False,
                     isolate_failures: bool = True) -> Dict[str, Any]:
        return self.writer.batch_create(table_key, rows, keep_empty, isolate_failures)

    def batch_update(self, table_key: str, updates: Sequence[Dict[str, Any]],
                     keep_empty: bool = False,
                     isolate_failures: bool = True) -> Dict[str, Any]:
        return self.writer.batch_update(table_key, updates, keep_empty, isolate_failures)

    def batch_update_verified(self, table_key: str, updates: Sequence[Dict[str, Any]],
                              chunk_size: int = SAFE_UPDATE_CHUNK,
                              settle_tries: int = 2,
                              fallback_single: bool = False) -> Dict[str, Any]:
        return self.writer.batch_update_verified(table_key, updates, chunk_size,
                                                 settle_tries, fallback_single)

    def batch_delete(self, table_key: str, record_ids: Sequence[str]) -> Dict[str, Any]:
        return self.writer.batch_delete(table_key, record_ids)

    def batch_upsert_by_key(self, table_key: str, unique_field: str,
                            rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return self.writer.batch_upsert_by_key(table_key, unique_field, rows)

    def set_row_count(self, table_key: str, count: int) -> None:
        """把已知的表行数喂给写前护栏（缺陷3），省掉一次 `record stats`。"""
        self.writer.set_row_count(table_key, count)

    def row_count(self, table_key: str) -> Optional[int]:
        return self.writer.row_count(table_key)

    # -- 附件 -------------------------------------------------------------
    def upload_attachment(self, file_path: Any,
                          concurrency: int = DEFAULT_UPLOAD_CONCURRENCY) -> Dict[str, Any]:
        return self.uploader.upload_attachment(file_path, concurrency)

    def upload_attachments(self, file_paths: Sequence[str],
                           concurrency: int = DEFAULT_UPLOAD_CONCURRENCY
                           ) -> List[Dict[str, Any]]:
        return self.uploader.upload_attachments(file_paths, concurrency)

    # -- 选项 -------------------------------------------------------------
    def get_field_options(self, table_key: str, field_key: str,
                          use_cache: bool = False) -> List[Dict[str, Any]]:
        return self.options.get_field_options(table_key, field_key, use_cache)

    def ensure_options(self, table_key: str, field_key: str, names: Sequence[str],
                       settle_tries: int = 5) -> List[Dict[str, Any]]:
        return self.options.ensure_options(table_key, field_key, names, settle_tries)

    def pending_option_names(self, table_key: str, field_key: str) -> List[str]:
        return self.options.pending_option_names(table_key, field_key)


# ---------------------------------------------------------------------------
# 自检：python3 -m aitable.table <config.json>  → 打印 config 映射概况，不发任何写请求
# ---------------------------------------------------------------------------
def _selfcheck(config_path: str) -> int:
    t = AITable(config_path)
    print("base: %s (%s)" % (t.base_name, t.base_id))
    for key in t.table_keys():
        print("  %-8s %-12s table_id=%-10s 字段 %d 个"
              % (key, t.table_name(key), t.table_id(key), len(t.field_keys(key))))
    print("sanitize_text 自检: %r"
          % sanitize_text("a\x0cb\tc\nd\r\ne\x7ff"))
    print("dws_calls（自检不发网络请求）=%d" % t.dws_calls)
    return 0


if __name__ == "__main__":
    sys.exit(_selfcheck(sys.argv[1] if len(sys.argv) > 1 else "/tmp/build/testbase-config.json"))
