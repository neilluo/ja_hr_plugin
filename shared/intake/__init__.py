# -*- coding: utf-8 -*-
"""intake 包：入口编排层的 OO 分解（P7）。

    console.py    IntakeConsole —— intake 入口脚本 stdout/stderr 的唯一出口，
                                   持有输出格式契约（分隔线 / icon 表 / 截断长度 /
                                   ARTIFACT:·RESUME:·VISION_NEEDED:·FATAL: 协议行前缀）
    report.py     IntakeReport  —— 报告组装（14 键 report dict）+ 产物落盘 + 尾部人读清单
    checkpoint.py CheckpointStore —— 增量 checkpoint v2 全部读写（done/progress 两 map、
                                   5 处增量原子写、终稿 update + 非原子落盘）
    budget.py     WallBudget    —— --wall-budget 墙钟预算（计时/触顶判定/优雅停标志/
                                   partial 语义的 reason 文本；不新增落盘时机）
    extraction_runner.py ExtractionRunner —— 并发提取池编排（建池/index-keyed 收集/
                                   预算触顶 cancel/提取摘要交 Console；两处 workers
                                   表达式刻意不合并）
    table_gateway.py TableGateway —— dws IO 边界唯一入口（装配 counter/client/tbl +
                                   config 失败 fatal；reset/ensure_options/附件上传/
                                   batch_update/batch_upsert_by_key/set_row_count 薄转发；
                                   判定逻辑与回读校验不进本类）
    readback.py     ReadBackVerifier —— 写后回读与核查读路径（verify_by_filter 回读轮询/
                                   6b 补传附件轮询/去重 scan 的 query 薄转发门面/
                                   known_locs config 读；判定逻辑留 run()）

沿 P2 纪律：**本包 `__init__` 不做任何 re-export**（不留 shim，调用方直接 import 子模块）。
import 风格与 shared 既有包一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import。
"""
