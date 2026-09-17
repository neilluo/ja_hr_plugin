# -*- coding: utf-8 -*-
"""intake 包：入口编排层的 OO 分解（P7）。

    console.py    IntakeConsole —— intake 入口脚本 stdout/stderr 的唯一出口，
                                   持有输出格式契约（分隔线 / icon 表 / 截断长度 /
                                   ARTIFACT:·RESUME:·VISION_NEEDED:·FATAL: 协议行前缀）
    report.py     IntakeReport  —— 报告组装（14 键 report dict）+ 产物落盘 + 尾部人读清单
    checkpoint.py CheckpointStore —— 增量 checkpoint v2 全部读写（done/progress 两 map、
                                   5 处增量原子写、终稿 update + 非原子落盘）

沿 P2 纪律：**本包 `__init__` 不做任何 re-export**（不留 shim，调用方直接 import 子模块）。
import 风格与 shared 既有包一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import。
"""
