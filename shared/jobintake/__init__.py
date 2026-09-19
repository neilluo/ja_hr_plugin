# -*- coding: utf-8 -*-
"""jobintake 包：岗位入库入口（intake_job.py）的 OO 分解。

    constants.py       B 侧全部常量（组织关键词 B 变体 / SETTLE_WAITS / 粒度护栏
                       阈值与黑名单 / PARSE_FAIL_REASON 5 键——唯一的模块级可变 dict）
    textutil.py        纯函数小工具：new_batch_id / today / clean / count_hits /
                       truncate / write_json
    policy.py          JobOrgPolicy（guess_org B 变体：三段式、low→仍写库）+
                       JobLocationPolicy（guess_locations 多选）——与 A 侧
                       intake/pipeline.py 的同名规则**刻意不合并**（数据与策略相反）
    jdfields.py        JD 语义字段转换（compose_hard_gates / as_text_list /
                       split_skill_items / norm_skill_item）+ SkillGranularityGuard
    docparse.py        JobDocParser —— 单份 JD 提取/扫描件判定/正则预填（文件 IO 边界）
    console.py         JobConsole —— intake_job stdout/stderr 唯一出口（B 侧 icon 4 项）
    table_gateway.py   JobTableGateway —— dws IO 边界唯一入口（全部 tbl.* 薄转发）
    readback.py        JobReadBackVerifier —— verify_jobs（B 口径：job_name 一对多 +
                       job_id/三元组五级精确归属 + 有界轮询）——与 A 侧
                       intake/readback.py **不合并**
    assembler.py       JobFieldAssembler —— 写入行 / draft job 文档 / apply cells
                       组装（键序即产物字节）
    report.py          JobReport —— summary 重算 + 8 键 report 组装 + 落盘 +
                       config 失败 / main 异常两条兜底产物
    pipeline.py        JobPipeline —— Turn 1（9 阶段方法）/ Turn 3（5 阶段方法）/
                       run() 装配调度；全部业务判定与告警文案在此

`skills/job-intake/scripts/intake_job.py` 只剩 CLI 装配（build_parser / main）。

本包 `__init__` 不做任何 re-export（调用方直接 import 子模块）；
shared/ 在 sys.path 上，包内模块用顶层绝对 import。
"""
