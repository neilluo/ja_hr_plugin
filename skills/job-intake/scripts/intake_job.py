#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / skills / job-intake / scripts / intake_job.py
========================================================================

岗位说明书入库编排层（契约 §2 岗位侧 / §6 W-C1）。三个 agent 回合里的第 1 与第 3：

    Turn 1  python3 scripts/intake_job.py --config C --files <jd...> --out-dir D
              提取 JD → 正则预填 → 复合键查重（岗位名称+所属部门+组织分类）
              → 部门/组织/工作地点 ensure_options → 分配岗位ID → 并发上传 JD 附件
              → 一次批量写（权重 0.7/0.3、提交时间当天、需求提交人=当前登录用户
                ——仅当 config.json 配了可选的 fields.job.submitter，契约 v3 §9#5）
              → 回读校验 → 产出 jobs_draft.json + intake_report.json
    Turn 2  （agent 一次批量归一化，不在本脚本内）→ jobs_final.json
    Turn 3  python3 scripts/intake_job.py --config C --apply jobs_final.json --out-dir D
              批量补写语义字段（硬性门槛/必备技能/加分项/权重/组织/部门）→ 回读 → 清单

jobs_final.json 结构（契约 v3 §9#3 冻结）
----------------------------------------
= jobs_draft.json 的 `jobs[]` 元素**原样保留**（含 `key` / `record_id`），仅修订语义字段：
`hard_gates` 四项 / `must_skills[]` / `bonus_skills[]` / `department` / `org` / `weights`
（`--apply` 同时容忍 `status` / `job_name` / `job_id` / `work_location` 的显式修订）。
岗位ID 由 Turn 1 脚本分配（JOB-序号，全表唯一，契约 v3 §9#4）；agent 只在缺失时续编。

补传附件语义（契约 v3 §9#6）
---------------------------
岗位侧**不落 checkpoint**：幂等靠「岗位名称+所属部门+组织分类」复合键查表。
`--no-attachment` 跑完后重跑同一命令（不带该参数）会命中复合键 → 覆盖更新路径
重新上传附件并随 batch_update 写入，**不会重复建岗**，附件能补上（与简历侧
checkpoint 的「记录已写/附件已传」分离判定等效）。

为什么 Turn 1 **不写** 硬性门槛 / 必备技能 / 加分项
----------------------------------------------------
W-A 实测：JD 的「硬性门槛四项拆解」与「必备技能/加分项切分」正则命中率只有
26%~79%（证书 52.6%、专业 73.7%），并且有两个**系统性缺陷**：
  ① 「持××证书者优先」会被当成硬性证书门槛 → 误杀（extract_fields 已用
     `cert_is_preferred_not_required` 标出来，本脚本原样透传给 Turn 2 的 LLM）；
  ② 技能逗号分隔被并成 1 条 → 打分分母 = 1，命中即 100 分虚高。
所以 Turn 1 只写**脚本能确定**的字段（岗位ID/名称/部门/组织/状态/工作地点/
岗位职责/任职要求/权重/提交时间/附件），把 `hard_gates_raw / must_skills_raw /
bonus_skills_raw` 原文段连同 `cert_is_preferred_not_required` 一起放进
jobs_draft.json，交 Turn 2 归一化，Turn 3 `--apply` 再批量补写。

调用面（冻结，W-D 的 SKILL.md 照此写，不得偏离）
------------------------------------------------
    python3 scripts/intake_job.py --config <...> --files <...> --out-dir <...> [--no-attachment]
        产出: <out-dir>/jobs_draft.json, <out-dir>/intake_report.json
    python3 scripts/intake_job.py --config <...> --apply <jobs_final.json绝对路径> --out-dir <...>
        产出: <out-dir>/intake_report.json
    stdout 末行: ARTIFACT:<out-dir>/intake_report.json 的绝对路径

架构（P9b OO 化，行为逐字节不变；裁判 run_job_oracle.sh 53 面门禁）
------------------------------------------------------------------
本脚本只剩 CLI 装配（build_parser / main）+ 一次 `JobPipeline(args, console).run()`；
编排/纯规则/IO 边界三分收进 `shared/jobintake/`：

    jobintake.pipeline.JobPipeline        Turn 1（9 阶段）/ Turn 3（5 阶段）编排 + 全部判定
    jobintake.policy.JobOrgPolicy         组织分类预判 **B 变体**（low→仍写库；禁与 A 侧合并）
    jobintake.policy.JobLocationPolicy    工作地点多选预判
    jobintake.jdfields                    compose_hard_gates / as_text_list /
                                          split_skill_items / SkillGranularityGuard
    jobintake.docparse.JobDocParser       单份 JD 提取+预填（B 侧无 vision 兜底通道）
    jobintake.table_gateway.JobTableGateway  dws IO 边界唯一入口（薄转发）
    jobintake.readback.JobReadBackVerifier   verify_jobs（B 口径回读，禁与 A 侧合并）
    jobintake.assembler.JobFieldAssembler    写入行 / draft / apply cells（键序即字节）
    jobintake.report.JobReport            8 键 report + 落盘 + 两条兜底产物
    jobintake.console.JobConsole          stdout/stderr 唯一出口（冻结文案）
    jobintake.constants / textutil        常量与纯函数小工具

设计纪律与 intake_resume.py 同源（dws 调用次数第一、附件随 create 一次写入不做事后
update、失败可见 D6、不硬造 D11、防静默早退 D7、python 3.9/3.14 双兼容 D18、
零第三方 pip 依赖、sys.path 用 parents[3] 定位插件根、零硬编码 ID D8）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

# --------------------------------------------------------------------------- #
# sys.path：脚本在 skills/<name>/scripts/ 下，插件根 = parents[3]
# --------------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PLUGIN_ROOT / "shared"
_VENDOR_DIR = _SHARED_DIR / "vendor"
for _p in (str(_SHARED_DIR), str(_VENDOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from jobintake.console import JobConsole              # noqa: E402
from jobintake.constants import UPLOAD_CONCURRENCY    # noqa: E402
from jobintake.pipeline import JobPipeline            # noqa: E402
from jobintake.report import JobReport                # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="intake_job.py",
        description="岗位入库：Turn 1 提取+预填+查重+批量写+附件+回读 → jobs_draft.json；"
                    "Turn 3 --apply 吃 jobs_final.json 批量补写语义字段")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源，契约 D8）")
    ap.add_argument("--files", nargs="*", default=[], help="岗位说明书文件路径（Turn 1，可多个）")
    ap.add_argument("--apply", default=None,
                    help="Turn 3：jobs_final.json 绝对路径（Turn 2 的 LLM 归一化结果）")
    ap.add_argument("--out-dir", default=None,
                    help="产物目录绝对路径；缺省 /tmp/recruit-fast/<batch_id>/")
    ap.add_argument("--no-attachment", action="store_true", help="跳过 JD 附件上传")
    ap.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--concurrency", type=int, default=UPLOAD_CONCURRENCY,
                    help=argparse.SUPPRESS)
    ap.add_argument("--late-verify-wait", type=int, default=15,
                    help=argparse.SUPPRESS)   # Turn 3 写入传播延迟的二次复核等待秒数
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    console = JobConsole()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    invalid = JobPipeline.validate_args(args, console)
    if invalid is not None:
        return invalid
    try:
        return JobPipeline(args, console).run()
    except KeyboardInterrupt:
        console.interrupted()
        return 130
    except Exception as exc:                        # 契约 D7：绝不静默早退
        return JobReport.crash_artifact(args, exc, console)


if __name__ == "__main__":
    sys.exit(main())
