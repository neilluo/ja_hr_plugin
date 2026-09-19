# -*- coding: utf-8 -*-
"""Pipeline 侧计时小工具（重构：消除 16 处 ``t0=monotonic(); calls0=…`` 样板）。

两个 pipeline（``IntakePipeline`` / ``JobPipeline``）各有约 8 处相同的计时模式：

    t0 = time.monotonic()
    calls0 = self.gateway.counter.calls   # 或 self.counter.calls
    # ... dws 调用 ...
    delta = time.monotonic() - t0
    calls_delta = self.gateway.counter.calls - calls0  # 或 self.counter.calls - calls0

本模块提供一个 ``StageTimer`` 上下文管理器，在进入时记下 ``monotonic`` 与调用计数，
在退出时返回 ``(delta_seconds, calls_delta)``。两个 pipeline 用法略有不同（counter
的路径不同），所以 ``StageTimer`` 接受一个 ``calls_fn`` 回调而非硬编码路径：

    with StageTimer(self.gateway.counter.calls) as st:
        # ... dws 调用 ...
    delta, calls_delta = st.delta, st.calls_delta
"""

from __future__ import annotations

import time
from typing import Callable

__all__ = ["StageTimer"]


class StageTimer:
    """计时 + 调用计数差的上下文管理器。

    用法::

        with StageTimer(lambda: self.gateway.counter.calls) as st:
            result = some_dws_call()
        console.some_stage(st.delta, st.calls_delta)

    ``calls_fn`` 是一个零参数回调，返回当前调用计数（int）。
    如果不需要调用计数，传 ``calls_fn=None``，``calls_delta`` 将为 0。
    """

    __slots__ = ("_calls_fn", "_t0", "_calls0", "delta", "calls_delta")

    def __init__(self, calls_fn: Callable[[], int]) -> None:
        self._calls_fn = calls_fn
        self._t0 = 0.0
        self._calls0 = 0
        self.delta = 0.0
        self.calls_delta = 0

    def __enter__(self) -> "StageTimer":
        self._t0 = time.monotonic()
        self._calls0 = self._calls_fn() if self._calls_fn else 0
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.delta = time.monotonic() - self._t0
        if self._calls_fn:
            self.calls_delta = self._calls_fn() - self._calls0
        else:
            self.calls_delta = 0
