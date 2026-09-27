# -*- coding: utf-8 -*-
"""waves.py — subagent 调度规划：agent 数尽量铺满，硬上限 20，条数多时自动加大 batch

规则（用户定稿）：
  - **agent 数硬上限 20，不可超过**（`MAX_AGENTS` 只能下调）。
  - 默认自动负载均衡：batch = ceil(总条数 / 20)，即**小批量就多开 agent**：
    26 条 → 13 个 agent（每个2条）；≤20 条 → 一条一个 agent；200 条 → batch 10 → 20 个 agent。
  - 显式 `--batch N`：agent 数 = ceil(条数/N)；若 >20，自动加大 batch 压回 20 以内。
  - **一次性把 agent 全发出去，不分波、不串行。**
"""
import os
import math

# agent 数硬上限 20，只能下调不能上调：环境变量可压小，但 min(...,20) 硬顶不可被 env 突破。
_MAX_AGENTS_CEIL = 20
MAX_AGENTS = min(int(os.environ.get("MAX_AGENTS", _MAX_AGENTS_CEIL)), _MAX_AGENTS_CEIL)


def plan(n, batch=None, cap=None):
    """返回 (实际batch, 分组列表)。分组 = 每个 agent 负责的条目序号（从1开始）。

    默认（不传 batch）自动负载均衡：batch = ceil(n / cap)，**尽量多开 agent 但绝不超过 20**：
      26 条 → batch 2 → 13 个 agent；20 条以内 → batch 1 → 一条一个 agent；
      200 条 → batch 10 → 20 个 agent（封顶）。
    显式传 batch 时按传入值分组，若算出的 agent 数 > cap，自动加大 batch 把它压回 cap 以内。
    """
    cap = min(cap or MAX_AGENTS, MAX_AGENTS)          # 硬顶 20
    if n <= 0:
        return max(1, int(batch or 1)), []
    batch = max(1, int(batch)) if batch else max(1, math.ceil(n / cap))
    if math.ceil(n / batch) > cap:                   # agent 数超限 -> 自动加大 batch
        batch = math.ceil(n / cap)
    groups = [list(range(i, min(i + batch - 1, n) + 1)) for i in range(1, n + 1, batch)]
    return batch, groups
