# -*- coding: utf-8 -*-
"""PipelineBase：IntakePipeline 与 JobPipeline 的共享基类。

消除两个 pipeline 的跨文件重复样板：
  * **计时样板**（``t0 = time.monotonic(); calls0 = <counter>.calls`` →
    ``delta = time.monotonic() - t0; calls_delta = <counter>.calls - calls0``）
    出现 14+ 处，计时逻辑完全相同，只有 counter 的访问路径不同（intake 走
    ``self.gateway.counter.calls``，jobintake 走 ``self.counter.calls``）。
  * **DwsError → fatal + warning** 内联 6 处：intake 用 ``_set_fatal(msg, warn=True)``
    （刀5 遗留①收归），jobintake 用 ``self.fatal = …; self.warnings.append(self.fatal)``
    （行为一致但 API 分裂），基类统一为 ``_set_fatal`` 写点。

红线（与两个 pipeline 的模块文档一致）：
  * **不改异常传播顺序**：计时上下文管理器只在进入/退出时记录，不吞异常
    （``__exit__`` 不返回 True，异常照常传播）。
  * **不改 fatal 写点语义**：``_set_fatal`` 的 ``warn=True`` 时同一条文本同时进
    warnings——与原 intake ``_set_fatal`` 和 jobintake ``self.fatal=…;
    self.warnings.append(self.fatal)`` 逐字一致。
  * **不收 readback 警告文案**：两个 pipeline 的回读警告文本是 oracle 冻结面，
    差异大（intake 2 类、jobintake 4 类含 submitter_missing），**不提取**。
  * **不收 ensure_options 循环**：两 pipeline 的 except 警告文案尾部不同
    （intake 多「，但建议重跑本步确认」），是冻结面差异，**不提取**。
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

__all__ = ["PipelineBase"]


class PipelineBase:
    """IntakePipeline / JobPipeline 的共享基类。

    子类需要提供：
      * ``self.warnings: List[str]``  —— 告警列表（两 pipeline 都在 ``__init__`` 里建）
      * ``self.fatal: Optional[str]``  —— 致命错误消息（两 pipeline 都在 ``__init__`` 里建）
      * ``_calls_fn()`` 方法  —— 返回当前 dws 调用计数 int（子类 override，
        intake 走 ``self.gateway.counter.calls``，jobintake 走 ``self.counter.calls``）
    """

    # ------------------------------------------------------------------ #
    # 计时：上下文管理器封装 t0=monotonic(); calls0=... 样板
    # ------------------------------------------------------------------ #
    class _StageTimer:
        """计时 + 调用计数差的上下文管理器。

        进入时记下 ``monotonic`` 与调用计数，退出时计算差值。
        不吞异常（``__exit__`` 返回 None，异常照常传播）。

        用法::

            with self._time_stage() as st:
                result = some_dws_call()
            console.some_stage(st.delta, st.calls_delta)
            # 或需要毫秒时：int(st.delta * 1000)
        """

        __slots__ = ("_calls_fn", "_t0", "_calls0", "delta", "calls_delta")

        def __init__(self, calls_fn: Callable[[], int]) -> None:
            self._calls_fn = calls_fn
            self._t0 = 0.0
            self._calls0 = 0
            self.delta = 0.0
            self.calls_delta = 0

        def __enter__(self) -> "PipelineBase._StageTimer":
            self._t0 = time.monotonic()
            self._calls0 = self._calls_fn()
            return self

        def __exit__(self, *exc_info: object) -> None:
            self.delta = time.monotonic() - self._t0
            self.calls_delta = self._calls_fn() - self._calls0

    def _calls_fn(self) -> int:
        """当前 dws 调用计数。子类必须 override（counter 路径不同）。"""
        raise NotImplementedError

    def _time_stage(self) -> "PipelineBase._StageTimer":
        """计时上下文管理器：封装 ``t0=monotonic(); calls0=...`` 样板。"""
        return self._StageTimer(self._calls_fn)

    # ------------------------------------------------------------------ #
    # fatal 唯一写点（统一 intake _set_fatal 与 jobintake 的内联 fatal=…;append）
    # ------------------------------------------------------------------ #
    def _set_fatal(self, msg: Optional[str], warn: bool = False) -> None:
        """fatal 的唯一写点。

        ``warn=True`` 时同一条文本同时进 warnings——与原 intake ``_set_fatal``
        和 jobintake ``self.fatal=…; self.warnings.append(self.fatal)`` 行为一致。
        """
        self.fatal = msg
        if warn and msg:
            self.warnings.append(msg)

    def _set_dws_fatal(self, exc: Any, msg_template: str, *,
                       msg_trunc: int = 300, warn: bool = True) -> None:
        """封装 DwsError → fatal + warning 的内联代码。

        ``msg_template`` 必须含三个 ``%s`` 占位符（category / code / message），
        与原两个 pipeline 的 ``"…（%s/%s）：%s" % (exc.category, exc.code,
        exc.message[:N])`` 逐字一致。

        用法::

            except DwsError as exc:
                self._set_dws_fatal(exc, "批量写简历库失败（%s/%s）：%s")
        """
        self._set_fatal(msg_template % (exc.category, exc.code,
                                        exc.message[:msg_trunc]),
                        warn=warn)
