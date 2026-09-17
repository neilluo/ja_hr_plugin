#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_match_input.py —— 匹配编排层第 1 步：把「候选人 + 表内在招岗位」压成一次批量判定的输入。

为什么要有这个脚本（性能根因）
----------------------------
老插件对**每份简历 × 每个在招岗位**逐个做大模型门槛判定与打分：10 人 × 19 岗 = 190 次判定，
摊在几十个 agent 回合里，实测每回合边际成本 5.4 s。前序实验把 190 次判定折叠成**一次**批量判定：
墙钟 368 s、JSON 合法、190/190 覆盖、零算术错；对比逐人串行外推 2534 s（42 分钟）→ **6.88×**。
本脚本负责这条路径里「确定性」的那一半：**压缩输入 + 分片 + 内嵌评分口径**，
让 LLM 只需在一个回合里输出稀疏决策（decisions.json）。

契约依据
--------
* §3.3  digest.json 结构（本脚本产出）
* §6    C1→C2 接缝：吃 `candidates.json`；岗位侧**从表里查在招岗位**，不依赖 jobs_draft.json
* v3§9#2 digest.json 顶层必带 `"ok": true/false`（不可判定时 false 并给 `errors[]`），
        让 D7 产物凭证校验在所有产物上口径统一为「文件存在 且 ok==true」
* v3§9#7 `--from-table`：从简历库表导出**存量候选人**为 candidates 结构（防功能回退：
        老插件「指定岗位反向匹配」「重建全部匹配」需要对表里已有候选人做匹配，
        不只是本次上传的批次）
* D3    批量判定分片 ≤8 人/批（`--max-per-batch`，默认 8）
* D4    每个候选人的 evidence **必须保留 education_text / cert_text 原文段完整（不截断）**；
        work_text / skill_text 可截断。实测截掉教育/证书段会同时造成
        误杀（许金×财务主管：四项门槛全达标却判 fail）与
        漏判（代文超×单晶生产主管：专业工商管理却判 pass）。
* D8    config.json 是唯一 ID 源，脚本内零硬编码 ID
* D13   `years_source == "estimated"` 的候选人必须标 `needs_review: ["years"]`
* D14   `期望地点` 缺失时脚本兜底填「不限」，agent 若从 evidence 看出明确城市则在
        candidate_overrides 里覆盖
* D18   同时兼容 python 3.9 与 3.14（禁 match 语句 / 禁 `X | None` 运行时标注 / 禁 3.10+ API）
* P5    组织预筛错杀可见化 + 身份字段安全阀（W6 召回审计的 13 条盲区里的 org/name/email/
        地点四类）：① 对每个候选人**跨组织**的在招岗位做机械硬门槛复查（学历 ordinal /
        年限数值 / 证书非空粗筛，专业跳过），全过 → 分片候选人加 `prefilter_suspicious`
        + needs_review 追加 "org" + 聚合 warning（不把被删岗位 JD 塞回分片，O4 收益不动）；
        ② evidence 只增 name_text / email_text / location_text（命中行原文 ≤60 字），
        name 来源 filename/OCR/agent 草稿、email 命中 OCR 噪声规则 → needs_review 追加。
        判据在 shared/fields/identity.py；消费面规则见 HOTPATH.md 回合 2。

用法（CLI 接口面：--candidates 与 --from-table 二选一，其余冻结）
-----------------------------------------------------------------
    # 模式 A：本次上传批次（C1 的 candidates.json）
    python3 scripts/build_match_input.py --config <config.json绝对路径> \\
            --candidates <candidates.json绝对路径> --out-dir <绝对路径> [--max-per-batch 8]

    # 模式 B：表内存量候选人（反向匹配 / 重建全部匹配，契约 v3 §9#7）
    python3 scripts/build_match_input.py --config <config.json绝对路径> \\
            --from-table [--org 制造中心|职能中心] [--exclude-onboarded] \\
            --out-dir <绝对路径> [--max-per-batch 8]

产出：`<out-dir>/digest.json`（顶层含 `ok`/`errors`）以及分片 `<out-dir>/digest_batch_01.json` ...
stdout 末行：`ARTIFACT:<out-dir>/digest.json`（契约 D7：产物必须存在且 ok==true 才进 Turn 2）
退出码：0 = ok；1 = digest.ok==false（errors 里有业务话原因）。

P8 OO 分解（任务 #19 第一刀）
-----------------------------
实现在 `shared/match/`：jobparse（JobRecordParser）/ candidates（CandidateNormalizer）/
gates（PrefilterAuditor）/ source（MatchSourceGateway）/ chunking（ShardPlanner）/
digest（DigestBuilder）/ reporting（MatchReporter）/ tablevalues / scoring / jsonio /
constants。本脚本只剩 CLI 装配 + 四个**冻结签名**薄壳：`build_digest` 与
`report_and_emit`（intake_resume.py --auto-match 同进程消费）、`parse_job_record`
（apply_decisions.py L176/L234 消费）、`emit_shard_stdout`（死代码，保留仅为
report_and_emit 的冻结签名参数有定义可依）。
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

# ---------------------------------------------------------------------------
# sys.path：用 __file__ 定位插件根（禁止硬编码绝对路径）
#   <root>/skills/match-verify/scripts/build_match_input.py
#     parents[0]=scripts  [1]=match-verify  [2]=skills  [3]=<root>
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT / "shared", _ROOT / "shared" / "vendor"):
    _s = str(_p)
    if _p.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

try:                                    # W-A 的 JD 归一化（缺失时降级，不致命）
    from extract_fields import extract_job_fields
except Exception:                       # pragma: no cover
    extract_job_fields = None

try:                                    # W-A 的简历分段（--from-table 切 evidence 用；缺失时降级）
    from extract_fields import extract_resume_fields
except Exception:                       # pragma: no cover
    extract_resume_fields = None

from match.constants import DEFAULT_MAX_PER_BATCH              # noqa: E402
from match.digest import DigestBuilder                         # noqa: E402
from match.jobparse import JobRecordParser, _LateBoundExtractor  # noqa: E402
from match.reporting import MatchReporter                      # noqa: E402

# ---------------------------------------------------------------------------
# 常量（其余常量随实现搬进 shared/match/；REQUIREMENTS_LIMIT 留在此处且**必须行为支配**：
# 裁判篡改自证 build_l114 以该赋值行在本文件唯一定位——本注释刻意不复写该字面量，
# 值经构造注入 JobRecordParser / DigestBuilder，改动即反映到 digest 字节。）
# ---------------------------------------------------------------------------
#: 岗位「任职要求原文」进 digest 的长度上限。前序实验用 520 字即达到 190/190 覆盖、零算术错。
REQUIREMENTS_LIMIT = 600

# 原实现里两个 W-A 抽取器是模块级条件绑定的全局名，调用点**每次调用时**才查
# （is not None 分叉）。薄壳经 _LateBoundExtractor 代理注入，查名时机逐字不变。
_THIS = sys.modules[__name__]
_JOB_PARSER = JobRecordParser(_LateBoundExtractor(_THIS, "extract_job_fields"),
                              requirements_limit=REQUIREMENTS_LIMIT)
_REPORTER = MatchReporter()


def parse_job_record(cells: Dict[str, Any], file_name: str = "") -> Dict[str, Any]:
    """把岗位表一行（业务字段名 → 值）解析成契约 §3.3 的 jobs 元素。

    **冻结签名**（apply_decisions.py L176/L234 函数内延迟 import 消费，
    「同一套解析口径，不重复实现」）；两级解析策略与实现见 match.jobparse。
    """
    return _JOB_PARSER.parse(cells, file_name)


def build_digest(config_path: str, candidates_path: Optional[str], out_dir: str,
                 max_per_batch: int = DEFAULT_MAX_PER_BATCH,
                 batch_id: Optional[str] = None,
                 from_table: bool = False, org: Optional[str] = None,
                 exclude_onboarded: bool = False,
                 org_prefilter: bool = True, slim_jobs: bool = True) -> Dict[str, Any]:
    """**冻结签名**（intake_resume.py --auto-match 同进程消费），返回结构逐字不变：
    {digest_path, digest, table, shard_paths, shard_docs}。
    实现见 match.digest.DigestBuilder（原 291 行 build_digest 的阶段化分解）。"""
    return DigestBuilder(
        config_path, candidates_path, out_dir,
        max_per_batch=max_per_batch, batch_id=batch_id,
        from_table=from_table, org=org, exclude_onboarded=exclude_onboarded,
        org_prefilter=org_prefilter, slim_jobs=slim_jobs,
        requirements_limit=REQUIREMENTS_LIMIT,
        job_field_extractor=_LateBoundExtractor(_THIS, "extract_job_fields"),
        resume_field_extractor=_LateBoundExtractor(_THIS, "extract_resume_fields"),
    ).run()


def report_and_emit(res: Dict[str, Any], emit_stdout: bool = False,
                    emit_always: bool = False) -> int:
    """打印 digest 摘要 + ARTIFACT 行 + SHARD: 行（agent 下一步唯一该 Read 的东西）。

    **冻结签名**（intake_resume.py --auto-match 在同一进程里复用完全相同的输出口径，
    O2/L2，两条入口的 stdout 一致，agent 学一次就够）。`emit_stdout` / `emit_always`
    恒为 False、无任何 CLI 开关能打开（W-I 实测 O4-c 负收益，全文见
    match.reporting.MatchReporter.report）。返回 0 = digest.ok；1 = 不可判定。"""
    return _REPORTER.report(res, emit_stdout=emit_stdout, emit_always=emit_always)


def emit_shard_stdout(shard_doc: Dict[str, Any]) -> int:
    """L4-c（**默认关闭、CLI 不可达**）：把单片判定输入打到 stdout。死代码薄壳，
    保留仅为 report_and_emit 的冻结签名 `emit_stdout=False` 参数有定义可依
    （W-I 实测负收益，见 match.reporting.EMIT_BUDGET_BYTES 注释）。"""
    return _REPORTER.emit_shard_stdout(shard_doc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="合并 候选人（candidates.json 或 --from-table 表内存量）+ 表内在招岗位 "
                    "→ digest.json（批量判定输入，契约 §3.3）")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源，契约 D8）")
    ap.add_argument("--candidates", default=None,
                    help="candidates.json 绝对路径（W-C1 intake_resume.py 产出）；"
                         "与 --from-table 二选一")
    ap.add_argument("--from-table", action="store_true",
                    help="从简历库表批量导出存量候选人（反向匹配/重建全部匹配用，"
                         "契约 v3 §9#7）；与 --candidates 二选一")
    ap.add_argument("--org", default=None,
                    help="仅 --from-table：只导出该「简历库所属组织」的候选人"
                         "（如 制造中心 / 职能中心）")
    ap.add_argument("--exclude-onboarded", action="store_true",
                    help="仅 --from-table：查询阶段即排除 沟通状态=已入职 的候选人"
                         "（不带时也会按老插件铁律在 digest 里剔除并记入 meta）")
    ap.add_argument("--out-dir", required=True, help="产物输出目录（绝对路径）")
    ap.add_argument("--max-per-batch", type=int, default=DEFAULT_MAX_PER_BATCH,
                    help="单片最多几个候选人（契约 D3，默认 %d）" % DEFAULT_MAX_PER_BATCH)
    ap.add_argument("--batch-id", default=None, help="批次号（缺省用 candidates.json 里的或时间戳）")
    # ---- W-I 性能优化开关（W-J 移植；默认全开，关掉即逐字节退回优化前行为，便于 A/B 与 md5 回归）----
    # 注：O4-c（--emit-stdout，把判定输入打进 stdout）W-I 实测**负收益**（qodercli ~30 KB 处静默截断
    # Bash 输出，半截 JSON 反而诱导 agent 去 Read digest.json / Grep 源码自证，多烧 3~4 回合），
    # 故**不移植该 CLI 开关**；判定输入一律走 SHARD: 路径 Read 分片文件。
    ap.add_argument("--no-org-prefilter", action="store_true",
                    help="关掉 L3：分片携带全部在招岗位，不按候选人组织预筛")
    ap.add_argument("--no-slim-jobs", action="store_true",
                    help="关掉 L4：分片岗位保留全部字段（含 responsibilities_text 等）")
    args = ap.parse_args(list(argv) if argv is not None else None)

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
                       slim_jobs=not args.no_slim_jobs)
    return report_and_emit(res)


if __name__ == "__main__":
    sys.exit(main())
