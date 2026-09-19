#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / skills / resume-intake / scripts / intake_resume.py
==============================================================================

简历入库编排层。一个进程内做完全部确定性工作，零 agent 回合。
"""

from __future__ import annotations

import argparse
import sys
import time
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

_PLUGIN_ROOT = bootstrap(__file__)

from intake.console import IntakeConsole            # noqa: E402
from preflight import run_preflight                  # noqa: E402
from intake.pipeline import (                       # noqa: E402
    UPLOAD_CONCURRENCY,
    WALL_BUDGET_DEFAULT,
    IntakePipeline,
)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="intake_resume.py",
        description="简历入库 Turn 1：一个进程内做完提取/抽字段/查重/批量写/附件/回读，"
                    "产出 candidates.json + intake_report.json + checkpoint.json")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源）")
    ap.add_argument("--files", nargs="*", default=[], help="简历文件路径（可多个）")
    ap.add_argument("--files-dir", default=None,
                    help="简历文件目录：扫描该目录顶层（非递归）的 "
                         "*.pdf|*.docx|*.doc|*.png|*.jpg|*.jpeg 作为输入文件。"
                         "可与 --files 同时使用，文件列表合并去重。"
                         "目录不存在或无支持格式的文件 → PREFLIGHT 失败。")
    ap.add_argument("--out-dir", default=None,
                    help="产物目录绝对路径；缺省 <系统临时目录>/recruit-fast/<batch_id>/")
    ap.add_argument("--no-attachment", action="store_true",
                    help="跳过附件上传（老插件「方案C·延后」，之后可补传）")
    ap.add_argument("--reset", action="store_true",
                    help="先清空 resume 表全部记录（仅供重复性能测量；生产 base 严禁使用）")
    ap.add_argument("--wall-budget", type=float, default=WALL_BUDGET_DEFAULT,
                    help="墙钟预算秒数（默认 %.0f，必须小于 agent 工具 120s 超时）。"
                         "到点 graceful 停：checkpoint 逐条落盘、打印已完成/未完成清单与"
                         "一行 RESUME: 提示、退出码 0、报告 ok=true 且 partial=true；"
                         "续跑 = 重跑同一条命令（checkpoint 幂等，不产生重复记录）"
                         % WALL_BUDGET_DEFAULT)
    ap.add_argument("--apply-vision-patch", default=None,
                    help="agent 多模态兜底补丁 json 绝对路径。补丁 schema："
                         '{"<文件绝对路径>": {"text": "...", '
                         '"fields_draft": {"name": "...", "phone": "...", ...}, '
                         '"confidence": 0.0, "notes": "..."}}。'
                         "合并规则：先对 patch.text 跑正则抽取，regex 有值的字段用 regex，"
                         "regex 为空才取 fields_draft；取自草稿的字段打 "
                         "field_source=agent_vision 并追加进该候选人 needs_review，"
                         "patch.text 写入简历全文并记 backend=agent_vision。"
                         "agent 只产出补丁、绝不写库——入库仍走本脚本正常查重/护栏/回读")
    # ---- 合并入口：入库 + 生成判定输入一次调用做完，省一个 agent 回合 ----
    ap.add_argument("--auto-match", action="store_true",
                    help="入库成功后在同一进程内接着跑 build_match_input，产出 digest.json + "
                         "分片并打印 SHARD: 路径（省一个编排回合）。语义判定与 apply_decisions "
                         "仍是独立回合，本开关不代替它们。")
    ap.add_argument("--match-out-dir", default=None,
                    help="--auto-match 时 digest 的输出目录；缺省 = <out-dir>/../match")
    ap.add_argument("--max-per-batch", type=int, default=8,
                    help="--auto-match 时的分片人数上限（默认 8）")
    # 非冻结面的内部旋钮（有默认值，SKILL.md 不需要暴露）
    ap.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--concurrency", type=int, default=UPLOAD_CONCURRENCY,
                    help=argparse.SUPPRESS)
    ap.add_argument("--no-dedupe-scan", action="store_true", help=argparse.SUPPRESS)
    # ---- 两阶段模式 ----
    ap.add_argument("--replay-path", default=None,
                    help="提供时进入 replay 模式（从 dws_results.json 读取预执行结果）；"
                         "不提供则进入 emit 模式（不调 dws，收集命令到 dws_commands.json）")
    return ap


def auto_match(args: argparse.Namespace, intake_rc: int) -> int:
    """入库成功后同进程接着生成定向匹配判定输入。

    纪律（不许为了省回合牺牲正确性）：
      * 入库没成功（rc != 0）→ **不**继续匹配：拿半成品 candidates.json 去判定会污染结论。
        如实打印原因并把入库的退出码原样回传。
      * build_match_input 导入失败 / 抛异常 → 打印原因、退出码 1；入库产物已落地，agent 只需
        单独重跑 build_match_input.py，**不会重复入库**（checkpoint 幂等）。
      * 语义判定（Turn 2）与 apply_decisions（Turn 3）仍是独立回合，本开关不代替它们。
      * 岗位预筛/字段裁剪由 build_match_input 默认开启；判定输入不打 stdout，
        agent 按打印的 SHARD: 路径 Read 分片文件。
    """
    console = IntakeConsole()
    if intake_rc != 0:
        console.auto_match_skip_banner()
        console.auto_match_skip_failed(intake_rc)
        return intake_rc
    if getattr(args, "_partial", False):
        console.auto_match_skip_banner()
        console.auto_match_skip_partial()
        return intake_rc
    if not args.out_dir:
        console.auto_match_skip_banner()
        console.auto_match_skip_no_out_dir()
        return intake_rc

    out_dir = Path(args.out_dir).expanduser().resolve()
    candidates = out_dir / "candidates.json"
    if not candidates.exists():
        console.auto_match_skip_banner()
        console.auto_match_skip_no_candidates(candidates)
        return 1
    match_out = (Path(args.match_out_dir).expanduser().resolve() if args.match_out_dir
                 else out_dir.parent / "match")
    match_out.mkdir(parents=True, exist_ok=True)

    bmi_dir = _PLUGIN_ROOT / "skills" / "match-verify" / "scripts"
    if str(bmi_dir) not in sys.path:
        sys.path.insert(0, str(bmi_dir))
    console.auto_match_banner()
    t0 = time.time()
    try:
        from build_match_input import build_digest, report_and_emit  # noqa: E402
    except Exception as exc:
        console.auto_match_import_error(type(exc).__name__, exc, bmi_dir)
        console.auto_match_rerun_hint()
        return 1
    try:
        res = build_digest(str(Path(args.config).expanduser()), str(candidates), str(match_out),
                           max_per_batch=args.max_per_batch)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        console.auto_match_error(type(exc).__name__, exc)
        return 1
    rc = report_and_emit(res)
    console.auto_match_wall(time.time() - t0, match_out)
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    console = IntakeConsole()
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    # --- Merge --files-dir into --files (before preflight checks file existence) ---
    if getattr(args, 'files_dir', None):
        dir_path = Path(args.files_dir).expanduser().resolve()
        if not dir_path.is_dir():
            print("PREFLIGHT: files-dir 目录不存在: %s" % args.files_dir, file=sys.stderr)
            import json as _json
            print("PREFLIGHT:" + _json.dumps(
                {"ok": False, "blocker": "files_dir",
                 "error": "目录不存在: %s" % args.files_dir},
                ensure_ascii=False))
            return 1
        _supported_exts = {'.pdf', '.docx', '.doc', '.png', '.jpg', '.jpeg'}
        _scanned = sorted(
            f for f in dir_path.iterdir()
            if f.is_file() and f.suffix.lower() in _supported_exts
        )
        if not _scanned:
            print("PREFLIGHT: files-dir 目录无支持格式的文件: %s" % args.files_dir,
                  file=sys.stderr)
            import json as _json
            print("PREFLIGHT:" + _json.dumps(
                {"ok": False, "blocker": "files_dir",
                 "error": "目录无支持格式文件: %s" % args.files_dir},
                ensure_ascii=False))
            return 1
        # Merge with --files, deduplicate by resolved path
        existing = {str(Path(f).expanduser().resolve()) for f in args.files}
        merged = list(args.files)  # preserve --files order first
        for f in _scanned:
            if str(f) not in existing:
                merged.append(str(f))
        args.files = merged

    # --- Preflight (stage 0): environment checks before any business logic ---
    run_preflight(
        config_path=args.config,
        files=getattr(args, 'files', None),
        files_dir=getattr(args, 'files_dir', None),
    )

    invalid = IntakePipeline.validate_args(args, console)
    if invalid is not None:
        return invalid
    try:
        rc = IntakePipeline(args, console).run()
    except KeyboardInterrupt:
        console.interrupted()
        return 130
    except Exception as exc:                        # 绝不静默早退
        return IntakePipeline.crash_artifact(args, exc, console)
    # 入库成功后同进程接着生成判定输入，省一个编排回合。
    # run() 抛异常时上面已 return，不会走到这里 → 入库崩溃绝不触发 auto-match。
    if getattr(args, "auto_match", False):
        return auto_match(args, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
