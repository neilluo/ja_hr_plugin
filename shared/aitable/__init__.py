# -*- coding: utf-8 -*-
"""aitable 包：钉钉 AI 表格 IO 层的 OO 分解。

    client.py      DwsClient     —— emit/replay 两阶段模式：emit 收集命令到内部列表并返回
                                    模拟成功（agent 用 Bash 执行），replay 从预加载结果文件
                                    返回真实结果。不做子进程调用、无重试退避。
    values.py      值语义        —— sanitize_text（写前净化）、val/values_equal（读回归一）
    schema.py      TableSchema   —— config.json 是唯一 ID 源：表/字段/类型映射、filter 构造、
                                    写值格式化（纯类型驱动，不发 dws）
    query.py       RecordQuery   —— 读路径：翻页、record-ids 分片、截断状态
    writer.py      RecordWriter  —— 写路径：≤100 自动分片、整片失败二分定位、幂等 upsert、
                                    行数上限护栏
    uploader.py    AttachmentUploader —— 附件三步上传 + 并发池（并发度 5）
    optionpool.py  OptionPool    —— 选项池只增不删 + 3000 上限护栏
    table.py       AITable       —— 组合根：5 个调用方唯一要改的就是 import 路径

    ReadBackVerifier（写后回读 + 有界轮询）位于 shared/intake/readback.py，不在本包内。

import 风格与调用方一致：shared/ 在 sys.path 上，包内模块用顶层绝对 import
（aitable.* / documents / fields.*），与 extract_text、extraction.* 的既有惯例相同。
**本包 `__init__` 不做任何 re-export**（不留 shim，调用方直接 import 子模块）。
"""
