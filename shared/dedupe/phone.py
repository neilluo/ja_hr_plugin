# -*- coding: utf-8 -*-
"""手机号查重：一次 filter 查询判 new / overwrite / conflict（契约要求）。

扫描口径原值保留：
  * OR 条件 **≤100/片**（服务端硬限制），超出自己分片，每片仍是一次 dws 调用；
  * `fields=["phone","name"]`、`limit=100`、`all_pages=True`。

判定纪律（老插件铁律「同号多条即停止并报告」，契约 D6：不自动选）：
  * 本批内同一个手机号出现两次 → 失败/conflict，停下问用户；
  * 库内命中 0 条 → new；
  * 库内命中 >1 条 → 失败/conflict，**record_id 显式清空**，绝不自动挑一条覆盖；
  * 库内命中 1 条但姓名不符 → 失败/conflict（疑似重录/错录），record_id 保留库内那条；
  * 库内命中 1 条且姓名相符（或有一方为空）→ overwrite。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from dedupe.base import Deduper, DedupeDecision, ScanResult, clean_value

__all__ = ["PhoneDeduper", "MAX_FILTER_OPERANDS"]

#: 一次 filter 查询里最多塞多少个 OR 条件（服务端硬限制，超出自动分片）
MAX_FILTER_OPERANDS = 100


class PhoneDeduper(Deduper):

    key_label = "手机号"
    scan_fields: Sequence[str] = ("phone", "name")

    def __init__(self) -> None:
        #: {手机号: [{"record_id":.., "name":..}, ...]}
        self.index: Dict[str, List[Dict[str, Any]]] = {}
        #: 本批内已用掉的手机号 -> 序号（第几份）
        self.batch_seen: Dict[str, int] = {}

    def build_index(self, records: Sequence[Dict[str, Any]]) -> int:
        for r in records or []:
            cells = r.get("cells") or {}
            ph = cells.get("phone")
            ph = ph.strip() if isinstance(ph, str) else ph
            if not ph:
                continue
            self.index.setdefault(str(ph), []).append(
                {"record_id": r["record_id"], "name": clean_value(cells.get("name"))})
        return len(self.index)

    def scan(self, table: Any, phones: Sequence[str],
             table_key: str = "resume") -> ScanResult:
        """按 ≤100 个 OR 条件分片查库内手机号，返回 (命中数, 分片数)。

        与 `Deduper.scan` 不同签名：这里必须**按待查键分片**（一次查完 N 个键），
        不是全表扫。截断状态由编排层直接看 `table.last_query_truncated`
        （每片 all_pages=True、max_pages 用默认 50 ≈5000 条/片，实测远打不满）。
        """
        phones = list(phones or [])
        records: List[Dict[str, Any]] = []
        pieces = 0
        for i in range(0, len(phones), MAX_FILTER_OPERANDS):
            records.extend(table.query_records(
                table_key, filter={"phone": phones[i:i + MAX_FILTER_OPERANDS]},
                fields=list(self.scan_fields), limit=MAX_FILTER_OPERANDS,
                all_pages=True))
            pieces += 1
        indexed = self.build_index(records)
        return ScanResult(records=len(records), indexed=indexed, pages=pieces,
                          truncated=False, max_pages=0)

    def hits(self, phone: str) -> List[Dict[str, Any]]:
        return self.index.get(phone) or []

    def decide(self, phone: str, name: Optional[str], seq: int,
               file_name: str) -> DedupeDecision:
        """对一份待入库简历给判定；顺带把 `phone` 记进本批已用集合。"""
        if phone in self.batch_seen:
            first = self.batch_seen[phone]
            return DedupeDecision(
                action="fail", dedupe="conflict",
                reason=("本批内第 %d 份《%s》已用了同一个手机号 %s，"
                        "已停下不自动覆盖，请确认是否同一人" % (first, file_name, phone)),
                warnings=["本批内手机号重复：%s（%s 与第 %d 份）" % (phone, file_name, first)])
        self.batch_seen[phone] = seq

        hits = self.hits(phone)
        if not hits:
            return DedupeDecision(dedupe="new")
        if len(hits) > 1:
            return DedupeDecision(
                action="fail", dedupe="conflict", record_id=None,
                reason=("库内手机号 %s 命中 %d 条记录（%s），已停下不自动选择，"
                        "请确认保留哪一条后再重跑"
                        % (phone, len(hits),
                           ", ".join(str(h["record_id"]) for h in hits[:5]))),
                warnings=["同手机号多条冲突：%s → %d 条，需人工确认" % (phone, len(hits))])
        h = hits[0]
        if name and h["name"] and name != h["name"]:
            return DedupeDecision(
                action="fail", dedupe="conflict", record_id=h["record_id"],
                reason=("手机号 %s 已在库内（姓名 %s），但本次简历姓名是 %s，"
                        "疑似重录或错录，已停下等你确认（record_id=%s）"
                        % (phone, h["name"], name, h["record_id"])),
                warnings=["疑似重录：手机号 %s 库内姓名 %r ≠ 本次 %r，需用户确认"
                          % (phone, h["name"], name)])
        return DedupeDecision(dedupe="overwrite", record_id=h["record_id"])
