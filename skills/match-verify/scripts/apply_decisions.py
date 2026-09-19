#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_decisions.py —— 匹配编排层第 3 步：校验并落库 decisions.json，重算岗位统计。

三段式流水线的最后一段：校验(覆盖率 + 引用 + 集合 + 算术复核) → 幂等删旧 → 批量建匹配记录
→ 脚本重算岗位统计并批量回填 → 回读校验 → 产出用户可读清单。
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

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:                                    # JD 归一化（缺失时降级，不致命）
    from extract_fields import extract_job_fields   # 无 digest 降级路径 parse 岗位行用）
except Exception:                       # pragma: no cover
    extract_job_fields = None

from match.applyflow import ApplyOrchestrator                      # noqa: E402
from match.applyreport import ApplyReportBuilder                   # noqa: E402
from match.jobparse import JobRecordParser, _LateBoundExtractor    # noqa: E402
from preflight import run_preflight                                # noqa: E402
from verify_decisions import verify as _verify                     # noqa: E402

# ---------------------------------------------------------------------------
# 常量（CREATE_CHUNK 留在此处，值经构造注入 ApplyOrchestrator）
# ---------------------------------------------------------------------------
CREATE_CHUNK = 100                      # batch_create ≤100/片

# 无 --digest 降级路径的岗位行解析与 build 侧同一套口径（match.jobparse），不重复
# 实现；抽取器保持「调用时查名」的原全局语义（_LateBoundExtractor）。
_THIS = sys.modules[__name__]
_JOB_PARSER = JobRecordParser(_LateBoundExtractor(_THIS, "extract_job_fields"))
_REPORT_BUILDER = ApplyReportBuilder()


def apply(config_path: str, decisions_path: str, out_dir: str,
          digest_path: Optional[str] = None, dry_run: bool = False,
          batch_id: Optional[str] = None,
          replay_path: Optional[str] = None) -> Dict[str, Any]:
    """校验并落库 decisions.json，返回 apply_report dict（含 exit_code）。

    `--dry-run` 时 table=None、dws_calls=0（全部表操作分支短路），退出码语义不变。
    提供 replay_path 时进入 replay 模式，否则 emit 模式。
    """
    return ApplyOrchestrator(config_path, decisions_path, out_dir,
                             digest_path=digest_path, dry_run=dry_run, batch_id=batch_id,
                             create_chunk=CREATE_CHUNK, job_parser=_JOB_PARSER,
                             verify_fn=_verify,
                             replay_path=replay_path).run()


def _printable_table(match_rows: Sequence[Dict[str, Any]]):
    return _REPORT_BUILDER.printable_table(match_rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="校验并应用 decisions.json：批量建匹配记录 + 脚本重算岗位统计 + 回读")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源）")
    ap.add_argument("--decisions", required=True, help="decisions.json 绝对路径（agent 产出）")
    ap.add_argument("--out-dir", required=True, help="apply_report.json 输出目录（绝对路径）")
    ap.add_argument("--digest", default=None, help="digest.json 绝对路径（强烈建议传：覆盖率与分母校验要用）")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库（零 dws 写调用）")
    ap.add_argument("--batch-id", default=None, help="批次号（缺省用 digest/decisions 里的）")
    ap.add_argument("--replay-path", default=None,
                    help="提供时进入 replay 模式（从 dws_results.json 读取预执行结果）；"
                         "不提供则进入 emit 模式（不调 dws，收集命令到 dws_commands.json）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    # --- Preflight (stage 0): environment checks before any business logic ---
    _pf_files = [args.decisions]
    if args.digest:
        _pf_files.append(args.digest)
    run_preflight(
        config_path=args.config,
        files=_pf_files,
    )

    rep = apply(args.config, args.decisions, args.out_dir, digest_path=args.digest,
                dry_run=args.dry_run, batch_id=args.batch_id,
                replay_path=args.replay_path)
    s = rep.get("summary") or {}
    v = rep.get("verify") or {}
    print("verify: %s（errors=%d warnings=%d 覆盖 %s/%s）"
          % ("PASS" if v.get("ok") else "FAIL", len(v.get("errors") or []),
             len(v.get("warnings") or []),
             (v.get("counts") or {}).get("covered_pairs"),
             (v.get("counts") or {}).get("expected_pairs")))
    if rep.get("dry_run"):
        print("dry-run：只校验，未写库（dws_calls=%d）" % rep.get("dws_calls", 0))
    else:
        print("删旧「系统匹配」%s 条 → 新建匹配记录 %s 条（失败 %s）→ 重算并回填 %s 个岗位统计"
              % (s.get("stale_deleted"), s.get("created"), s.get("create_failed"),
                 s.get("jobs_stat_refreshed")))
        print("推荐 %s ｜ 待定 %s ｜ 不推荐 %s ｜ 已入职跳过 %s"
              % (s.get("recommend"), s.get("pending"), s.get("reject"), s.get("skip")))
    for line in _printable_table(rep.get("match_rows") or [])[:40]:
        print(line)
    for e in (rep.get("errors") or [])[:20]:
        print("ERROR: %s" % e)
    for w in (rep.get("warnings") or [])[:25]:
        print("WARN: %s" % w)
    print("dws_calls=%s elapsed_ms=%s retry_count=%s ok=%s python=%s"
          % (rep.get("dws_calls"), rep.get("elapsed_ms"), rep.get("retry_count"),
             rep.get("ok"), rep.get("python")))
    print("ARTIFACT:%s" % rep.get("_report_path"))

    # ---- 两阶段模式：emit 模式下输出 dws 命令清单 ----
    if args.replay_path is None and rep.get("_dws_client") is not None:
        client = rep["_dws_client"]
        emit_path = Path(args.out_dir).expanduser().resolve() / "dws_commands.json"
        client.write_emit_file(str(emit_path))
        print("emit 模式：%d 条 dws 命令已写入 %s"
              % (len(client.emit_commands()), emit_path))

    return int(rep.get("exit_code") or 0)


if __name__ == "__main__":
    sys.exit(main())
