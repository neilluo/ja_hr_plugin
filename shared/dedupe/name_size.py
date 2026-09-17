# -*- coding: utf-8 -*-
"""库内附件去重：键 = (附件文件名, 附件字节大小)。

⚠️ 这**不是内容级比对**（缺陷1 的诚实化口径）：库内索引来自 `attachment` 字段读回的
`filename` / `size`，本地侧用的是 `file_name` 与 `stat().st_size`。同名同大小即判重复，
改一个字节内容也照样命中；反过来，同一份内容换个文件名就漏掉。真 MD5 去重是 P4 的事
（钉钉附件读回不带内容哈希，要做就得下载附件重算）。

扫描口径原值保留：`fields=["attachment"]`、`all_pages=True`、`max_pages=100`
（≈10000 条上限；截断由 `ScanResult.truncated` 报出去，见缺陷2）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from dedupe.base import Deduper, DedupeDecision, ScanResult

__all__ = ["NameSizeDeduper"]


class NameSizeDeduper(Deduper):

    key_label = "文件名+字节大小"
    scan_fields: Sequence[str] = ("attachment",)

    def __init__(self) -> None:
        #: {(附件文件名, 附件字节大小): record_id}
        self.index: Dict[Tuple[str, Any], str] = {}

    def build_index(self, records: Sequence[Dict[str, Any]]) -> int:
        for r in records or []:
            att = (r.get("cells") or {}).get("attachment") or []
            if not isinstance(att, list):
                continue
            for a in att:
                if isinstance(a, dict) and a.get("filename"):
                    self.index[(str(a["filename"]), a.get("size"))] = r["record_id"]
        return len(self.index)

    def scan(self, table: Any, table_key: str = "resume",
             max_pages: int = 100) -> ScanResult:
        records = table.query_records(table_key, fields=list(self.scan_fields),
                                      all_pages=True, max_pages=max_pages)
        return ScanResult(records=len(records), indexed=self.build_index(records),
                          pages=int(getattr(table, "last_query_pages", 0) or 0),
                          truncated=bool(getattr(table, "last_query_truncated", False)),
                          max_pages=max_pages)

    def find(self, file_name: str, size: Any) -> Optional[str]:
        return self.index.get((file_name, size))

    def decide(self, file_name: str, size: Any) -> DedupeDecision:
        rid = self.find(file_name, size)
        if rid is None:
            return DedupeDecision()
        return DedupeDecision(action="skip", dedupe="overwrite", record_id=rid,
                              reason=self.hit_reason(rid))

    def hit_reason(self, record_id: str) -> str:
        return ("库内已存在同名同大小的简历附件（%s 比对命中，record_id=%s），"
                "判为重复上传，已跳过" % (self.key_label, record_id))
