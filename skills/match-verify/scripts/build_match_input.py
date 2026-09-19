#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_match_input.py —— 匹配编排层第 1 步：把「候选人 + 表内在招岗位」压成一次批量判定的输入。

压缩输入 + 分片 + 内嵌评分口径，让 LLM 只需在一个回合里输出稀疏决策（decisions.json）。
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

# --------------------------------------------------------------------------- #
# Bootstrap sys.path so cli_bootstrap / runtime_compat are importable
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[3]
_shared = str(_ROOT / "shared")
if _shared not in sys.path:
    sys.path.insert(0, _shared)

from cli_bootstrap import bootstrap                          # noqa: E402

_ROOT = bootstrap(__file__)

try:                                    # JD 归一化（缺失时降级，不致命）
    from extract_fields import extract_job_fields
except Exception:                       # pragma: no cover
    extract_job_fields = None

try:                                    # 简历分段（--from-table 切 evidence 用；缺失时降级）
    from extract_fields import extract_resume_fields
except Exception:                       # pragma: no cover
    extract_resume_fields = None

from match.match_basics import DEFAULT_MAX_PER_BATCH          # noqa: E402
from match.digest import DigestBuilder                         # noqa: E402
from match.jobparse import JobRecordParser, _LateBoundExtractor  # noqa: E402
from match.reporting import MatchReporter                      # noqa: E402
from preflight import run_preflight                            # noqa: E402

# ---------------------------------------------------------------------------
# 常量（REQUIREMENTS_LIMIT 留在此处，值经构造注入 JobRecordParser / DigestBuilder）
# ---------------------------------------------------------------------------
#: 岗位「任职要求原文」进 digest 的长度上限。
REQUIREMENTS_LIMIT = 600

# 抽取器经 _LateBoundExtractor 代理注入，查名时机逐字不变。
_THIS = sys.modules[__name__]
_JOB_PARSER = JobRecordParser(_LateBoundExtractor(_THIS, "extract_job_fields"),
                              requirements_limit=REQUIREMENTS_LIMIT)
_REPORTER = MatchReporter()


def parse_job_record(cells: Dict[str, Any], file_name: str = "") -> Dict[str, Any]:
    """把岗位表一行（业务字段名 → 值）解析成 jobs 元素。"""
    return _JOB_PARSER.parse(cells, file_name)


def build_digest(config_path: str, candidates_path: Optional[str], out_dir: str,
                 max_per_batch: int = DEFAULT_MAX_PER_BATCH,
                 batch_id: Optional[str] = None,
                 from_table: bool = False, org: Optional[str] = None,
                 exclude_onboarded: bool = False,
                 org_prefilter: bool = True, slim_jobs: bool = True,
                 replay_path: Optional[str] = None) -> Dict[str, Any]:
    """返回结构：{digest_path, digest, table, shard_paths, shard_docs}。"""
    return DigestBuilder(
        config_path, candidates_path, out_dir,
        max_per_batch=max_per_batch, batch_id=batch_id,
        from_table=from_table, org=org, exclude_onboarded=exclude_onboarded,
        org_prefilter=org_prefilter, slim_jobs=slim_jobs,
        requirements_limit=REQUIREMENTS_LIMIT,
        job_field_extractor=_LateBoundExtractor(_THIS, "extract_job_fields"),
        resume_field_extractor=_LateBoundExtractor(_THIS, "extract_resume_fields"),
        replay_path=replay_path,
    ).run()


def report_and_emit(res: Dict[str, Any], emit_stdout: bool = False,
                    emit_always: bool = False) -> int:
    """打印 digest 摘要 + ARTIFACT 行 + SHARD: 行（agent 下一步唯一该 Read 的东西）。

    返回 0 = digest.ok；1 = 不可判定。"""
    return _REPORTER.report(res, emit_stdout=emit_stdout, emit_always=emit_always)


def emit_shard_stdout(shard_doc: Dict[str, Any]) -> int:
    """把单片判定输入打到 stdout。默认关闭、CLI 不可达。"""
    return _REPORTER.emit_shard_stdout(shard_doc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="合并 候选人（candidates.json 或 --from-table 表内存量）+ 表内在招岗位 "
                    "→ digest.json（批量判定输入）")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源）")
    ap.add_argument("--candidates", default=None,
                    help="candidates.json 绝对路径（W-C1 intake_resume.py 产出）；"
                         "与 --from-table 二选一")
    ap.add_argument("--from-table", action="store_true",
                    help="从简历库表批量导出存量候选人（反向匹配/重建全部匹配用）；"
                         "与 --candidates 二选一")
    ap.add_argument("--org", default=None,
                    help="仅 --from-table：只导出该「简历库所属组织」的候选人"
                         "（如 制造中心 / 职能中心）")
    ap.add_argument("--exclude-onboarded", action="store_true",
                    help="仅 --from-table：查询阶段即排除 沟通状态=已入职 的候选人"
                         "（不带时也会按老插件铁律在 digest 里剔除并记入 meta）")
    ap.add_argument("--out-dir", required=True, help="产物输出目录（绝对路径）")
    ap.add_argument("--max-per-batch", type=int, default=DEFAULT_MAX_PER_BATCH,
                    help="单片最多几个候选人（默认 %d）" % DEFAULT_MAX_PER_BATCH)
    ap.add_argument("--batch-id", default=None, help="批次号（缺省用 candidates.json 里的或时间戳）")
    # ---- 性能优化开关（默认全开，关掉即逐字节退回优化前行为，便于 A/B 与 md5 回归）----
    ap.add_argument("--no-org-prefilter", action="store_true",
                    help="关掉 L3：分片携带全部在招岗位，不按候选人组织预筛")
    ap.add_argument("--no-slim-jobs", action="store_true",
                    help="关掉 L4：分片岗位保留全部字段（含 responsibilities_text 等）")
    # ---- 两阶段模式 ----
    ap.add_argument("--replay-path", default=None,
                    help="提供时进入 replay 模式（从 dws_results.json 读取预执行结果）；"
                         "不提供则进入 emit 模式（不调 dws，收集命令到 dws_commands.json）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    # --- Preflight (stage 0): environment checks before any business logic ---
    run_preflight(
        config_path=args.config,
        files=[args.candidates] if args.candidates else None,
    )

    if bool(args.from_table) == bool(args.candidates):
        print("错误：--candidates 与 --from-table 必须二选一。\n"
              "  · 本次上传的简历要匹配 → 先跑简历入库，再 --candidates <candidates.json绝对路径>；\n"
              "  · 对表里已有的存量候选人做「指定岗位反向匹配 / 重建全部匹配」→ --from-table"
              "（可加 --org / --exclude-onboarded 过滤）。", file=sys.stderr)
        return 2
    if args.org and not args.from_table:
        print("错误：--org 只在 --from-table 模式下有效", file=sys.stderr)
        return 2

    res = build_digest(args.config, args.candidates, args.out_dir,
                       max_per_batch=args.max_per_batch, batch_id=args.batch_id,
                       from_table=args.from_table, org=args.org,
                       exclude_onboarded=args.exclude_onboarded,
                       org_prefilter=not args.no_org_prefilter,
                       slim_jobs=not args.no_slim_jobs,
                       replay_path=args.replay_path)
    # emit 模式：把收集到的 dws 命令写入 dws_commands.json
    if args.replay_path is None:
        tbl = res.get("table")
        if tbl is not None:
            emit_path = str(Path(args.out_dir).expanduser().resolve() / "dws_commands.json")
            tbl.client.write_emit_file(emit_path)
    return report_and_emit(res)


if __name__ == "__main__":
    sys.exit(main())
