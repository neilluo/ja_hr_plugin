# -*- coding: utf-8 -*-
"""库内附件去重：键 = **附件内容真 MD5**（P4b 起的内容级比对）。

为什么要这个类（缺陷本体）：`NameSizeDeduper` 的键是 (文件名, 字节大小)，两个失效
都是实测过的——同一文件名同大小、内容改了一版重投会被**误判**成重复静默跳过；
同一份内容换个文件名会**漏判**，判成新候选人重复建档。钉钉附件读回不带内容哈希、
下载重算又贵（预签名 url 只有 2h 时效），所以主控裁决的方案是**把哈希存进库里**：
简历库加一个 text 字段「附件内容MD5」，写入侧附件上传成功后落库，比对时直接读它。

索引与判定（三层，按优先级）：
  1. 本地文件真 MD5 命中库内「附件内容MD5」→ 判重复，跳过（理由如实说"内容 MD5 相同"）；
  2. 未命中，但 (文件名, 大小) 命中且**库内那条有哈希** → 内容确实不同 →
     **不判重复**，改判「同名同大小但内容不同」，交给手机号查重决定 new/overwrite
     （覆盖更新语义 = 同一候选人的简历新版本），并在清单说明；
  3. (文件名, 大小) 命中但**库内那条没有哈希**（早于 P4b 写入的老记录）→ 无从比内容，
     按老键回退判重复并告警（宁可保守：这类记录若放行走手机号查重，库内手机号对不上时
     会新建出重复档案）。老记录被覆盖更新时会**懒回填**哈希，库随之收敛到内容级去重。

老库容忍（硬要求）：config/schema 里根本没有「附件内容MD5」字段时，编排层**不构造本类**，
直接用 `NameSizeDeduper` 并打一条 warning（见 intake_resume.py 的 `attach_md5_field`）。
本类不负责建字段——建字段是 replicate 部署时的事，脚本绝不自建。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from dedupe.base import DedupeDecision, ScanResult, clean_value
from dedupe.name_size import NameSizeDeduper

__all__ = ["ContentHashDeduper", "ATTACH_MD5_FIELD_KEY"]

#: 简历库「附件内容MD5」字段的业务键（config.json 的 fields.resume.attach_md5）。
#: 可选键：缺失 = 老库，编排层回退 NameSizeDeduper（见模块文档）。
ATTACH_MD5_FIELD_KEY = "attach_md5"


class ContentHashDeduper(NameSizeDeduper):
    """内容级库内去重器。继承 (文件名, 大小) 索引：它仍是第 2/3 层的判定依据。"""

    key_label = "附件内容MD5"
    #: 回退层（老记录）用的键名，进用户可见文案
    fallback_key_label = "文件名+字节大小"
    scan_fields: Sequence[str] = ("attachment", ATTACH_MD5_FIELD_KEY)

    def __init__(self, hash_field_key: str = ATTACH_MD5_FIELD_KEY) -> None:
        super().__init__()
        #: 实际读的哈希字段业务键（正常就是 attach_md5）
        self.hash_field_key = hash_field_key
        #: {内容MD5(小写): record_id}
        self.hash_index: Dict[str, str] = {}
        #: {record_id: 内容MD5(小写)}——用于区分「内容不同」与「老记录没哈希」
        self.hash_by_record: Dict[str, str] = {}
        #: 带附件但没有哈希的老记录数（P4b 之前写入的）
        self.legacy_records = 0

    # -- 建索引 -----------------------------------------------------------
    def build_index(self, records: Sequence[Dict[str, Any]]) -> int:
        """返回值仍是 (文件名,大小) 索引条目数（与父类同口径，ScanResult.indexed）。

        哈希覆盖情况另挂在 `self.hash_indexed` / `self.legacy_records` 上，
        由编排层打进 stdout（ScanResult 是三个去重器共用的通用载体，不塞专用字段）。
        """
        indexed = super().build_index(records)
        for r in records or []:
            cells = r.get("cells") or {}
            rid = r.get("record_id")
            h = clean_value(cells.get(self.hash_field_key))
            if h:
                hs = str(h).strip().lower()
                self.hash_index[hs] = rid
                if rid:
                    self.hash_by_record[str(rid)] = hs
            elif cells.get("attachment"):
                self.legacy_records += 1
        return indexed

    @property
    def hash_indexed(self) -> int:
        """建出来的「内容MD5 → record_id」索引条目数。"""
        return len(self.hash_index)

    def scan(self, table: Any, table_key: str = "resume",
             max_pages: int = 100) -> ScanResult:
        """全表扫（父类同形态），只是多读一个「附件内容MD5」字段——**不增加调用次数**。"""
        records = table.query_records(table_key, fields=list(self.scan_fields),
                                      all_pages=True, max_pages=max_pages)
        return ScanResult(records=len(records), indexed=self.build_index(records),
                          pages=int(getattr(table, "last_query_pages", 0) or 0),
                          truncated=bool(getattr(table, "last_query_truncated", False)),
                          max_pages=max_pages)

    # -- 判定 -------------------------------------------------------------
    def find_by_hash(self, md5: Optional[str]) -> Optional[str]:
        h = self._norm(md5)
        return self.hash_index.get(h) if h else None

    @staticmethod
    def _norm(md5: Optional[str]) -> Optional[str]:
        s = clean_value(md5)
        return str(s).strip().lower() if s else None

    def decide(self, file_name: str, size: Any,
               md5: Optional[str] = None) -> DedupeDecision:
        """对一份待入库简历给判定（`md5` = 本地文件真 MD5，提取层已算好）。

        `action="pass"` 且 `reason` 非空 = **不判重复但要在清单说明**（第 2 层：
        同名同大小、内容不同）；编排层把它塞进该行的说明文字，不当跳过处理。
        """
        h = self._norm(md5)
        if h:
            rid = self.hash_index.get(h)
            if rid:
                return DedupeDecision(action="skip", dedupe="overwrite", record_id=rid,
                                      reason=self.hit_reason(rid),
                                      warnings=["内容级重复：%s 命中库内 record_id=%s"
                                                % (h, rid)])
        ns_rid = self.find(file_name, size)
        if ns_rid is None:
            return DedupeDecision()
        known = self.hash_by_record.get(str(ns_rid))
        if known:
            # 第 2 层：内容确实不同 → 走覆盖更新语义，交给手机号查重定 new/overwrite。
            # reason 是**进清单那行**的短说明（嵌套括号会难读），哈希明细进 warnings。
            note = ("库内已有同名同大小的附件但**内容不同**（%s 不一致）→ 不判重复，"
                    "按同一候选人的简历新版本处理" % self.key_label)
            detail = ("同名同大小但内容不同：%s（库内 record_id=%s；本地 %s=%s / 库内=%s）"
                      "→ **不跳过**，new/overwrite 由手机号查重决定"
                      % (file_name, ns_rid, self.key_label, h or "(本地未算出)", known))
            return DedupeDecision(action="pass", reason=note, warnings=[detail])
        # 第 3 层：老记录没有哈希 → 按老键回退判重复，并如实告警
        return DedupeDecision(
            action="skip", dedupe="overwrite", record_id=ns_rid,
            reason=self.legacy_hit_reason(ns_rid),
            warnings=["库内 record_id=%s 早于内容级去重（无「附件内容MD5」），"
                      "本次只能按「%s」回退判重复；该记录被覆盖更新时会自动回填哈希"
                      % (ns_rid, self.fallback_key_label)])

    # -- 文案 -------------------------------------------------------------
    def hit_reason(self, record_id: str) -> str:
        return ("库内已存在内容相同的简历附件（%s 比对命中，record_id=%s），"
                "判为重复上传，已跳过" % (self.key_label, record_id))

    def legacy_hit_reason(self, record_id: str) -> str:
        return ("库内已存在同名同大小的简历附件（record_id=%s），但该记录早于内容级去重、"
                "没有存「附件内容MD5」，无从比对内容，已按「%s」回退判为重复上传并跳过；"
                "如确认是改过内容的新版本，请让用户确认后重跑覆盖更新（会顺带回填哈希）"
                % (record_id, self.fallback_key_label))

    def coverage_note(self) -> str:
        """一行覆盖度说明（stdout 用）：多少条有哈希、多少条是老记录。"""
        return "其中 %d 条有内容哈希、%d 条老记录无哈希" % (
            len(self.hash_by_record), self.legacy_records)

    def legacy_warning(self) -> List[str]:
        """老记录告警（库里有 P4b 之前写入的记录时给一条，说明回退口径与收敛方式）。"""
        if not self.legacy_records:
            return []
        return ["库内 %d 条记录带附件但没有「附件内容MD5」（早于内容级去重写入）："
                "这些记录的重复判定仍回退到「%s」，同名同大小即判重复、内容改了也可能"
                "误判；它们被覆盖更新（手机号命中）或补传附件时会**自动回填**哈希，"
                "库随之收敛到内容级去重" % (self.legacy_records, self.fallback_key_label)]
