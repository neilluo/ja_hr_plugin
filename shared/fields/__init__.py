# -*- coding: utf-8 -*-
"""字段抽取包：正则/启发式抽取器 + 字段载体 dataclass + 多源合并器。

分层（与 extraction/ 同构）：
    base.py       FieldExtractor ABC —— agent 抽取器按此接入
    textnorm.py   文本归一化 / 条目切分 / 分段（简历侧与 JD 侧共用）
    lexicon.py    词表与校名库（985/211/双一流、证书、技能）—— 纯数据
    regex_ext.py  RegexFieldExtractor —— 现状全部正则群与启发式
    candidate.py  CandidateFields —— 简历侧 19 业务字段 + 每字段 source + warnings
    job.py        JobFields       —— JD 侧业务字段 + source + warnings
    merger.py     FieldMerger     —— 多源合并（单源 + agent 草稿补空）

import 风格与调用方一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import
（fields.* / documents），与 extract_text、extraction.* 的既有惯例相同。
"""
