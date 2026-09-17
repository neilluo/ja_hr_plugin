# -*- coding: utf-8 -*-
"""去重器抽象基类 + 判定/扫描结果载体。

契约（编排层按这个用）：
    build_index(records)  把一批库内记录（`AITable.query_records` 的返回）吃进索引
    scan(table, ...)      自己发查询 + build_index，返回 ScanResult（含截断状态）
    decide(...)           对**一条**待入库项给判定，返回 DedupeDecision；不改调用方数据

`key_label` 是这个去重器实际用的键的**业务话名称**，给用户看的理由串必须用它——
缺陷1 的教训：文案写「MD5 去重命中」而实际键是 (文件名, 字节大小)，
用户会以为做了内容级比对（P4 才会接真 MD5）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["Deduper", "DedupeDecision", "ScanResult", "clean_value", "UNSET"]


class _Unset(object):
    """「本次判定不碰这个字段」的哨兵。

    必须与 None 区分开：手机号库内命中**多条**时现状是把 record_id 显式清成 None
    （不许留着上一次的残值去写库），而「本批内重号」那条路径根本不碰 record_id。
    """

    def __repr__(self) -> str:                       # pragma: no cover
        return "<UNSET>"


UNSET = _Unset()


def clean_value(s: Any) -> Optional[Any]:
    """None/空串归一成 None（与编排层 `_clean` 同口径，读回值一律先过它）。"""
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        return s or None
    return s


@dataclass
class DedupeDecision:
    #: pass=照常入库 / skip=判重复跳过 / fail=停下等人确认（契约 D6：不自动选）
    action: str = "pass"
    #: new | overwrite | conflict；None = 不改调用方现值
    dedupe: Optional[str] = None
    #: UNSET = 不碰；None = 显式清空；str = 指向库内那条记录
    record_id: Any = UNSET
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class ScanResult:
    #: 查询返回的记录条数
    records: int = 0
    #: 建出来的索引条目数
    indexed: int = 0
    #: 实际翻页数
    pages: int = 0
    #: 因 max_pages 截断（缺陷2：截断必须可见，据此做的去重不可信）
    truncated: bool = False
    max_pages: int = 0


class Deduper(ABC):

    #: 实际去重键的业务话名称（进用户可见文案，必须与实现一致）
    key_label = ""

    #: 建索引要读的字段（子类覆盖）
    scan_fields: Sequence[str] = ()

    @abstractmethod
    def build_index(self, records: Sequence[Dict[str, Any]]) -> int:
        """把一批库内记录（`AITable.query_records` 的返回）吃进索引，返回索引条目数。"""
        raise NotImplementedError

    @abstractmethod
    def scan(self, table: Any, *args: Any, **kwargs: Any) -> ScanResult:
        """自己发查询 + `build_index`，并把截断状态收进 ScanResult。

        两个子类的查询形态天生不同，签名也就不同（这是维度差异，不是设计漏洞）：
        NameSizeDeduper 是**全表扫**（要看完所有附件才知道有没有同名同大小的），
        PhoneDeduper 是**按待查键分片查**（一次 OR filter 查完 N 个手机号）。
        """
        raise NotImplementedError

    @abstractmethod
    def decide(self, *args: Any, **kwargs: Any) -> DedupeDecision:
        """对**一条**待入库项给判定；不改调用方的数据结构。"""
        raise NotImplementedError
