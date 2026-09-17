#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_decisions.py —— 匹配编排层第 2 步：对 LLM 产出的 decisions.json 做**纯本地**校验。

零 dws 调用、零网络、零第三方依赖。apply_decisions.py 内部会先调本模块的 `verify()`，
校验不通过就**不写库**（契约 D6：失败可见，禁止静默丢弃）。

校验项（任务书冻结）
------------------
1. **覆盖率**：每个 candidate × 每个**同组织**在招岗位，必须在 `passed` 或 `rejected` 里
   出现且**仅出现一次**；缺失或重复都报错并列出具体 key。
2. **引用合法性**：所有 `candidate_key` / `job_key` 必须在 digest 里存在。
3. **集合校验**：`skill_hits ⊆ must_skills`、`bonus_hits ⊆ bonus_skills`，
   越界即判该条无效（**防模型编造命中项**，契约 D16）。
4. **算术复核**：按口径重算 技能得分 / 加分项得分 / 总分 / 推荐状态。
   契约 D16：**分数一律脚本算**，模型输出的分数只作对照，不一致记 warnings。
5. **JSON 结构完整性** + `evidence` 非空且长度 ≤ 80 字。
6. **语义合理性护栏**（缺陷2 修复，2026-09-17；W-F run3 事故：agent 用自写规则脚本
   代替 Turn 2 语义判定 → 形式校验全 PASS 但 0 推荐、evidence 模板化复用）。
   见 `SEM_GUARD_DEFAULTS` 与 `semantic_guardrails()`：evidence 跨候选人复用（高占比
   → error 拒写库）、evidence 去重率过低、skill_hits 普遍过少 / 普遍 100% 全命中、
   0 推荐 / 推荐率异常高 / 推荐分布塌缩、门槛全拒或全过、needs_review:["years"]
   却零 override（D13 复核没做）。阈值可用 --sem-* CLI 覆盖。
   **护栏告警必须如实转述给用户并说明可能需要重做判定，禁止静默吞掉继续写库。**

用法
----
    python3 scripts/verify_decisions.py --digest <digest.json> --decisions <decisions.json>

stdout 打印结构化校验结论 JSON；退出码 0 = 通过，1 = 有问题。

P9a OO 分解（任务 #19 第二刀）
------------------------------
实现在 `shared/match/`：scoring（ScoreCalculator + round_half_up）/ hitmap（HitMapper +
GateVerdictReader）/ coverage（CoverageChecker）/ guardrails（SemanticGuardrails +
SEM_GUARD_DEFAULTS）/ verifier（DecisionVerifier + 结果组装原语）/ jsonio（load_json）/
constants（EVIDENCE_MAX_LEN 等契约常量）。本脚本只剩 CLI 装配 + **冻结签名**薄壳：
`verify`（apply_decisions.py 同进程 import 复用同一个 verify()）、`load_json` /
`norm_item` / `EVIDENCE_MAX_LEN` / `RECOMMEND_VALUES`（旧 apply L77 的历史出口面）
及其余纯函数出口。`_recommend_of` 必须留在本文件且**行为支配**：裁判篡改自证
threshold 以该行在本文件唯一定位（模式同 build 入口的 REQUIREMENTS_LIMIT 锚点）。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# sys.path：用 __file__ 定位插件根（禁止硬编码绝对路径）
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT / "shared", _ROOT / "shared" / "vendor"):
    _s = str(_p)
    if _p.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

from match.constants import EVIDENCE_MAX_LEN, GATE_ITEMS, \
    INVALID_PASSED_RATIO_LIMIT, RECOMMEND_VALUES                    # noqa: E402,F401
from match.coverage import CoverageChecker                          # noqa: E402
from match.guardrails import SEM_GUARD_DEFAULTS, SemanticGuardrails  # noqa: E402,F401
from match.hitmap import FAIL_LIKE as _FAIL_LIKE, PASS_LIKE as _PASS_LIKE, \
    GateVerdictReader, HitMapper, as_str_list                       # noqa: E402,F401
from match.jsonio import load_json                                  # noqa: E402,F401
from match.scoring import ScoreCalculator, round_half_up            # noqa: E402,F401
from match.verifier import DecisionVerifier, build_result as _result, \
    dist as _dist, slim_for_stdout as _slim_for_stdout              # noqa: E402

_HITS = HitMapper()
_GATE_READER = GateVerdictReader()
_COVERAGE = CoverageChecker()
_GUARDS = SemanticGuardrails()


def _recommend_of(total: int) -> str:
    """总分 → 推荐档位。裁判篡改自证 threshold 的定位锚点：下面这行赋值必须留在
    本文件、恰好出现一次且行为支配（经构造注入 ScoreCalculator，改动即反映到
    verify 输出与 apply 写库字节）。"""
    recommend = "推荐" if total >= 80 else ("待定" if total >= 60 else "不推荐")
    return recommend


_CALC = ScoreCalculator(recommend_of=_recommend_of)


# ---------------------------------------------------------------------------
# 冻结出口薄壳（历史消费面：apply_decisions.py 与诊断调用方按原名 import）
# ---------------------------------------------------------------------------
def compute_scores(skill_hits: Sequence[str], bonus_hits: Sequence[str],
                   must_skills: Sequence[str], bonus_skills: Sequence[str],
                   weights: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按老插件口径重算四个数字（契约 D16；实现见 match.scoring.ScoreCalculator）。"""
    return _CALC.compute(skill_hits, bonus_hits, must_skills, bonus_skills, weights)


def norm_item(s: Any) -> str:
    """**冻结出口**（旧 apply_decisions L77 import）。实现见 match.hitmap.HitMapper。"""
    return _HITS.norm_item(s)


def squash(s: Any) -> str:
    """比对用最强归一化（实现与实测坑说明见 match.hitmap.HitMapper.squash）。"""
    return _HITS.squash(s)


def dedupe_norm(items: Sequence[Any]) -> List[str]:
    """按归一化去重，保留原词（分子不能靠重复命中虚增）。"""
    return _HITS.dedupe_norm(items)


def map_hits_to_items(hits: Sequence[Any],
                      items: Sequence[Any]) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """命中项映射回岗位原文条目（三条映射规则见 match.hitmap.HitMapper）。"""
    return _HITS.map_hits_to_items(hits, items)


def gate_verdict(v: Any) -> Optional[bool]:
    """把 gate_detail 的值归一成 True(达标)/False(不达标)/None(读不懂)。"""
    return _GATE_READER.verdict(v)


def expected_pairs(digest: Dict[str, Any],
                   overrides: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[str, str]],
                                                                 Dict[str, List[str]],
                                                                 List[str]]:
    """「同组织 + 在招」的必须覆盖组合集（实现见 match.coverage.CoverageChecker）。"""
    return _COVERAGE.expected_pairs(digest, overrides)


def semantic_guardrails(digest: Dict[str, Any], decisions: Dict[str, Any],
                        passed_audit: Sequence[Dict[str, Any]],
                        rejected_pairs_n: int,
                        thresholds: Optional[Dict[str, float]] = None,
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """语义合理性护栏（缺陷2）。返回 (errors, warnings, metrics)；
    实现与阈值标定见 match.guardrails.SemanticGuardrails。"""
    return _GUARDS.evaluate(digest, decisions, passed_audit, rejected_pairs_n, thresholds)


def verify(digest: Any, decisions: Any, check_coverage: bool = True,
           sem_thresholds: Optional[Dict[str, float]] = None,
           semantic_guards: bool = True) -> Dict[str, Any]:
    """核心校验函数（**冻结签名**：apply_decisions.py 直接 import 它）。

    `check_coverage=False` 用于「没传 --digest、岗位信息是从表里现查的」降级场景；
    `sem_thresholds` 的 9 个键（含 5 个仅 API 键）与 `semantic_guards` 开关必须继续
    可传。返回结构化结论 dict；`ok` 为 False 时调用方**不得写库**。
    实现见 match.verifier.DecisionVerifier（推荐档位判定用本文件 `_recommend_of`）。
    """
    return DecisionVerifier(_CALC).verify(digest, decisions, check_coverage=check_coverage,
                                          sem_thresholds=sem_thresholds,
                                          semantic_guards=semantic_guards)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="纯本地校验 decisions.json（零 dws 调用）")
    ap.add_argument("--digest", required=True, help="digest.json（或某个 digest_batch_NN.json）绝对路径")
    ap.add_argument("--decisions", required=True, help="decisions.json 绝对路径（agent 产出）")
    ap.add_argument("--quiet", action="store_true", help="只打结论摘要，不打逐条 score_audit")
    # ---- 语义护栏阈值覆盖（缺陷2；缺省值与标定依据见 SEM_GUARD_DEFAULTS 注释）----
    ap.add_argument("--no-semantic-guards", action="store_true",
                    help="关闭语义合理性护栏（仅调试用；正常流程禁止关闭——W-F run3 事故"
                         "证明形式校验防不了语义偷懒）")
    ap.add_argument("--sem-evidence-unique-min", type=float, default=None,
                    help="evidence 去重率下限（缺省 %.2f）" % SEM_GUARD_DEFAULTS["evidence_unique_ratio_min"])
    ap.add_argument("--sem-low-hits-ratio", type=float, default=None,
                    help="skill_hits≤%d 条目占比告警阈值（缺省 %.2f）"
                         % (SEM_GUARD_DEFAULTS["low_skill_hits_max"],
                            SEM_GUARD_DEFAULTS["low_skill_hits_ratio"]))
    ap.add_argument("--sem-full-hit-ratio", type=float, default=None,
                    # 注意双写 %%：先经本行的 %% 格式化，再由 argparse 帮助展开一次
                    # （python 3.14 的 argparse 会在 add_argument 时就校验，单个 %% 会炸）
                    help="100%%%% 全命中（放水）占比告警阈值（缺省 %.2f）"
                         % SEM_GUARD_DEFAULTS["full_hit_ratio"])
    ap.add_argument("--sem-recommend-high", type=float, default=None,
                    help="推荐率异常高告警阈值（缺省 %.2f）" % SEM_GUARD_DEFAULTS["recommend_ratio_high"])
    args = ap.parse_args(list(argv) if argv is not None else None)

    digest, e1 = load_json(Path(args.digest).expanduser(), "digest")
    decisions, e2 = load_json(Path(args.decisions).expanduser(), "decisions")
    if e1 or e2:
        res = _result(False, [x for x in (e1, e2) if x and x["code"] != "markdown_fence_stripped"],
                      [x for x in (e1, e2) if x and x["code"] == "markdown_fence_stripped"],
                      {}, {}, {})
        print(json.dumps(_slim_for_stdout(res), ensure_ascii=False, indent=1))
        return 1
    sem_th = {
        "evidence_unique_ratio_min": args.sem_evidence_unique_min,
        "low_skill_hits_ratio": args.sem_low_hits_ratio,
        "full_hit_ratio": args.sem_full_hit_ratio,
        "recommend_ratio_high": args.sem_recommend_high,
    }
    res = verify(digest, decisions, sem_thresholds=sem_th,
                 semantic_guards=not args.no_semantic_guards)
    out = _slim_for_stdout(res)
    if args.quiet:
        out.pop("score_audit", None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sem = (res.get("summary") or {}).get("semantic_guards") or {}
    sys.stderr.write("VERIFY %s：errors=%d warnings=%d 覆盖 %s/%s 语义护栏=%s\n"
                     % ("PASS" if res["ok"] else "FAIL", len(res["errors"]),
                        len(res["warnings"]), res["counts"].get("covered_pairs"),
                        res["counts"].get("expected_pairs"),
                        (sem.get("triggered_warnings") or []) + (sem.get("triggered_errors") or [])
                        or "未触发"))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
