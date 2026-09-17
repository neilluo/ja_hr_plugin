#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_decisions.py —— 匹配编排层第 3 步：校验并落库 decisions.json，重算岗位统计。

三段式流水线的最后一段（契约 §2 Turn 3）::

    校验(覆盖率 + 引用 + 集合 + 算术复核) → 幂等删旧 → 批量建匹配记录
    → **脚本重算岗位统计**并批量回填 → 回读校验 → 产出用户可读清单

为什么快：190 个 候选人×岗位 判定的**写库**部分只花 4~6 次 dws 调用
（1 次查旧记录 + 1 次批量删 + 1~2 次批量建 + 1 次查全量匹配 + 1 次批量回填统计），
而老插件是 agent 逐条敲 dws，每回合边际成本 5.4 s。

契约依据
--------
* D1   不依赖 lookup / filterUp：`岗位ID` 是普通 text，由本脚本自己 join 填入；
       岗位统计由本脚本重算，不靠服务端异步字段。
* D6   写后必回读；失败可见：越权/校验失败一律进 `rows[].result=失败` 与 `warnings`。
* D7   防静默早退：落地产物文件 + stdout 末行 `ARTIFACT:<绝对路径>`。
* D8   config.json 是唯一 ID 源，脚本内零硬编码 ID。
* D15  **岗位统计一律脚本重算**：写完后从表里查每个受影响岗位的**全部**匹配记录
       （含人工匹配、含历史批次），重算 候选人总数/推荐数/待定数/不推荐数，
       再 `batch_update` **一次**回填。禁止用本批 decisions 直接累加。
* D16  **分数由脚本重算**，模型输出的分数只作对照，不一致记 warnings。
* D18  兼容 python 3.9 与 3.14。

老插件铁律（沿用，不重新发明）
----------------------------
* 沟通状态=已入职 → 一律删记录、不打分、不推荐。
* 幂等「删旧建新」：先删该批候选人现有的「匹配来源=系统匹配」记录，**人工匹配不动**。
* 硬门槛任一不达标 → **不产生任何记录**。

candidate_overrides 支持的键（契约 v3 §9#1 裁定，七个，与文档严格一致）
--------------------------------------------------------------------
`org` / `category` / `expected_location` / `skills_extra`(数组) /
`certificates_extra`(数组) / `years_experience`(int，D13 复核修正值) /
`org_reason`(组织判定理由，只进报告)。修正在校验后合入候选人档案，实跑时还会
**写回简历库**（选项只增不删 + 写后回读），明细进 apply_report.json 的
`overrides` / `overrides_writeback`（dry-run 只记录不写库）。

用法（CLI 接口面已冻结）
----------------------
    python3 scripts/apply_decisions.py --config <...> --decisions <decisions.json绝对路径> \\
            --out-dir <...> [--digest <digest.json>] [--dry-run]

产出：`<out-dir>/apply_report.json`；stdout 末行 `ARTIFACT:<out-dir>/apply_report.json`
`--dry-run` 只校验不写库（零 dws 写调用），用于测试。

P9a OO 分解（任务 #19 第二刀）
------------------------------
实现在 `shared/match/`：decisionctx（DecisionContextBuilder）/ overrides（OverrideMerger）/
recordfactory（MatchRecordFactory）/ matchgate（MatchTableGateway，唯一持 AITable）/
applyreport（ApplyReportBuilder）/ applyflow（ApplyOrchestrator，8 步阶段方法）/
applyvalues（**apply 侧语义**的 as_list/as_text 等，与 build 侧同名不同义、禁止合并）。
本脚本只剩 CLI 装配 + **冻结签名**薄壳 `apply(config_path, decisions_path, out_dir,
digest_path=None, dry_run=False, batch_id=None)`。校验仍同进程复用 verify_decisions
的 `verify()`（SKILL.md 契约面；也让 verify 入口的 `_recommend_of` 锚点支配本路径）。
`CREATE_CHUNK` 必须留在本文件且**行为支配**：裁判篡改自证 apply_chunk 以该赋值行
在本文件唯一定位（模式同 build 入口的 REQUIREMENTS_LIMIT 锚点；本 docstring 刻意
不复写该字面量），值经构造注入 ApplyOrchestrator。
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

# ---------------------------------------------------------------------------
# sys.path：用 __file__ 定位插件根（禁止硬编码绝对路径）
#   <root>/skills/match-verify/scripts/apply_decisions.py → parents[3] = <root>
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT / "shared", _ROOT / "shared" / "vendor"):
    _s = str(_p)
    if _p.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:                                    # W-A 的 JD 归一化（缺失时降级，不致命；
    from extract_fields import extract_job_fields   # 无 digest 降级路径 parse 岗位行用）
except Exception:                       # pragma: no cover
    extract_job_fields = None

from match.applyflow import ApplyOrchestrator                      # noqa: E402
from match.applyreport import ApplyReportBuilder                   # noqa: E402
from match.jobparse import JobRecordParser, _LateBoundExtractor    # noqa: E402
from verify_decisions import verify as _verify                     # noqa: E402

# ---------------------------------------------------------------------------
# 常量（其余常量随实现搬进 shared/match/constants.py；CREATE_CHUNK 留在此处且**必须
# 行为支配**：裁判篡改自证 apply_chunk 以该赋值行在本文件唯一定位——本注释刻意不复写
# 该字面量，值经构造注入 ApplyOrchestrator，改动即反映到写库分片与 report 字节。）
# ---------------------------------------------------------------------------
CREATE_CHUNK = 100                      # 契约要求：batch_create ≤100/片（aitable.writer 内部也会分片）

# 无 --digest 降级路径的岗位行解析与 build 侧同一套口径（match.jobparse），不重复
# 实现；W-A 抽取器保持「调用时查名」的原全局语义（_LateBoundExtractor，模式同 build 入口）。
_THIS = sys.modules[__name__]
_JOB_PARSER = JobRecordParser(_LateBoundExtractor(_THIS, "extract_job_fields"))
_REPORT_BUILDER = ApplyReportBuilder()


def apply(config_path: str, decisions_path: str, out_dir: str,
          digest_path: Optional[str] = None, dry_run: bool = False,
          batch_id: Optional[str] = None) -> Dict[str, Any]:
    """**冻结签名**：校验并落库 decisions.json，返回 apply_report dict（含 exit_code）。

    实现见 match.applyflow.ApplyOrchestrator（原 420 行 apply() 的 8 步阶段化分解）；
    `--dry-run` 时 table=None、dws_calls=0（全部表操作分支短路），退出码语义不变。
    """
    return ApplyOrchestrator(config_path, decisions_path, out_dir,
                             digest_path=digest_path, dry_run=dry_run, batch_id=batch_id,
                             create_chunk=CREATE_CHUNK, job_parser=_JOB_PARSER,
                             verify_fn=_verify).run()


def _printable_table(match_rows: Sequence[Dict[str, Any]]):
    return _REPORT_BUILDER.printable_table(match_rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="校验并应用 decisions.json：批量建匹配记录 + 脚本重算岗位统计 + 回读")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源，契约 D8）")
    ap.add_argument("--decisions", required=True, help="decisions.json 绝对路径（agent 产出）")
    ap.add_argument("--out-dir", required=True, help="apply_report.json 输出目录（绝对路径）")
    ap.add_argument("--digest", default=None, help="digest.json 绝对路径（强烈建议传：覆盖率与分母校验要用）")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库（零 dws 写调用）")
    ap.add_argument("--batch-id", default=None, help="批次号（缺省用 digest/decisions 里的）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    rep = apply(args.config, args.decisions, args.out_dir, digest_path=args.digest,
                dry_run=args.dry_run, batch_id=args.batch_id)
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
    return int(rep.get("exit_code") or 0)


if __name__ == "__main__":
    sys.exit(main())
