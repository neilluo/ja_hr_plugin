# -*- coding: utf-8 -*-
"""match 侧共享常量（P8 第一刀：只收 build_match_input.py 一侧的定义）。

⚠️ apply_decisions.py 里有 JOB_STATUS_OPEN / COMM_STATUS_ONBOARDED 的逐字重复定义
（值相同）；是否并入本模块由 P9 逐案裁定——与 as_list/as_text 不同（那两组
**同名不同义**，禁止合并，见 match/tablevalues.py 头注）。
"""

DEFAULT_MAX_PER_BATCH = 8               # 契约 D3
JOB_STATUS_OPEN = "招聘中"
COMM_STATUS_ONBOARDED = "已入职"        # 老插件铁律：已入职不参与匹配
DEFAULT_LOCATION = "不限"               # 契约 D14 兜底
