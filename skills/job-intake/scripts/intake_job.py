#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / skills / job-intake / scripts / intake_job.py
========================================================================

岗位说明书入库编排层。三个 agent 回合里的第 1 与第 3。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

# --------------------------------------------------------------------------- #
# sys.path：脚本在 skills/<name>/scripts/ 下，插件根 = parents[3]
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Bootstrap sys.path so cli_bootstrap / runtime_compat are importable
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[3]
_shared = str(_ROOT / "shared")
if _shared not in sys.path:
    sys.path.insert(0, _shared)

from cli_bootstrap import bootstrap                # noqa: E402

bootstrap(__file__)

from jobintake.console import JobConsole              # noqa: E402
from jobintake.constants import UPLOAD_CONCURRENCY    # noqa: E402
from jobintake.pipeline import JobPipeline            # noqa: E402
from jobintake.report import JobReport                # noqa: E402
from preflight import run_preflight                  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="intake_job.py",
        description="岗位入库：Turn 1 提取+预填+查重+批量写+附件+回读 → jobs_draft.json；"
                    "Turn 3 --apply 吃 jobs_final.json 批量补写语义字段")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源）")
    ap.add_argument("--files", nargs="*", default=[], help="岗位说明书文件路径（Turn 1，可多个）")
    ap.add_argument("--apply", default=None,
                    help="Turn 3：jobs_final.json 绝对路径（Turn 2 的 LLM 归一化结果）")
    ap.add_argument("--out-dir", default=None,
                    help="产物目录绝对路径；缺省 <系统临时目录>/recruit-fast/<batch_id>/")
    ap.add_argument("--no-attachment", action="store_true", help="跳过 JD 附件上传")
    ap.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--concurrency", type=int, default=UPLOAD_CONCURRENCY,
                    help=argparse.SUPPRESS)
    ap.add_argument("--late-verify-wait", type=int, default=15,
                    help=argparse.SUPPRESS)   # Turn 3 写入传播延迟的二次复核等待秒数
    # ---- 两阶段模式 ----
    ap.add_argument("--replay-path", default=None,
                    help="提供时进入 replay 模式（从 dws_results.json 读取预执行结果）；"
                         "不提供则进入 emit 模式（不调 dws，收集命令到 dws_commands.json）")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    console = JobConsole()
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    # --- Preflight (stage 0): environment checks before any business logic ---
    run_preflight(
        config_path=args.config,
        files=getattr(args, 'files', None),
    )

    invalid = JobPipeline.validate_args(args, console)
    if invalid is not None:
        return invalid
    try:
        return JobPipeline(args, console).run()
    except KeyboardInterrupt:
        console.interrupted()
        return 130
    except Exception as exc:                        # 绝不静默早退
        return JobReport.crash_artifact(args, exc, console)


if __name__ == "__main__":
    sys.exit(main())
