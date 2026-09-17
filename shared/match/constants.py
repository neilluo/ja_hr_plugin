# -*- coding: utf-8 -*-
"""match 侧共享常量（P8 收 build 侧；P9a 收 apply/verify 侧，裁定见下）。

P9a 裁定：apply_decisions 里与 build 侧**同值**的字面量（JOB_STATUS_OPEN /
COMM_STATUS_ONBOARDED）及 apply/verify 专属契约常量（MATCH_SOURCE_* /
FILTER_VALUE_CHUNK / EVIDENCE_MAX_LEN / GATE_ITEMS / RECOMMEND_VALUES /
INVALID_PASSED_RATIO_LIMIT）并入本模块；`CREATE_CHUNK = 100` **留在 apply 入口**
（裁判篡改自证 apply_chunk 的定位锚点，必须在入口唯一且行为支配）。
as_list/as_text 是**同名不同义**，仍禁止合并（见 match/tablevalues.py 头注）。
"""

DEFAULT_MAX_PER_BATCH = 8               # 契约 D3
JOB_STATUS_OPEN = "招聘中"
COMM_STATUS_ONBOARDED = "已入职"        # 老插件铁律：已入职不参与匹配
DEFAULT_LOCATION = "不限"               # 契约 D14 兜底

MATCH_SOURCE_SYSTEM = "系统匹配"        # 老插件口径：只有「系统匹配」参与删旧建新
MATCH_SOURCE_MANUAL = "人工匹配"        # 人工匹配一律不动，但要算进岗位统计（D15）
FILTER_VALUE_CHUNK = 60                 # 单次 filter 的值个数（dws operands 上限 100，留余量）

EVIDENCE_MAX_LEN = 80                   # 契约 §3.3：evidence 原文引用 ≤80 字
GATE_ITEMS = ("education", "major", "years", "certificates")
RECOMMEND_VALUES = ("推荐", "待定", "不推荐")
#: 编造命中项的 pass 条目占比超过这个值 → 升级为硬错误，整批不写库（防模型语义崩塌）
INVALID_PASSED_RATIO_LIMIT = 0.34
