# -*- coding: utf-8 -*-
"""去重包：一个去重维度一个 Deduper 子类。

    base.py          Deduper(ABC) + DedupeDecision + ScanResult
    content_hash.py  ContentHashDeduper —— 库内附件**内容级**去重（P4b 默认档）：
                     读库内「附件内容MD5」字段建 哈希→record_id 索引；同名同大小但
                     内容不同 → 不判重复，走覆盖更新；老记录无哈希 → 回退老键并告警
    name_size.py     NameSizeDeduper    —— 库内附件 (文件名, 字节大小) 扫描去重
                     （**老库回退档**：config/schema 没有「附件内容MD5」字段时用）
    phone.py         PhoneDeduper       —— 手机号 filter 分片查重（≤100/片）

分工：Deduper 只负责「建库内索引 + 对单条给判定」，**不改调用方的数据结构**；
把判定落到 entry（result/reason/writable/dedupe/record_id）是编排层的事，
stdout 的计时与调用计数行也留在编排层（那是编排层的账）。

import 风格与调用方一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import
（dedupe.*），与 extract_text、extraction.*、fields.*、aitable.* 的既有惯例相同。
"""
