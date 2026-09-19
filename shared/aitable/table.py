# -*- coding: utf-8 -*-
"""
table.py —— 钉钉 AI 表格 IO 层的**组合根**（零第三方 pip 依赖）。

组合 client/values/schema/query/writer/uploader/verifier/optionpool，提供统一的方法转发。
**调用方唯一要改的就是 import 路径**（`from aitable.table import AITable`）。
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
    由 config.json 映射到真实 ID。"""

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
        """最近一次 `query_records` 是否因 `max_pages` 截断（截断必须可见）。"""
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
        """把已知的表行数喂给写前护栏，省掉一次 `record stats`。"""
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
