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
"""

import argparse
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

from aitable_io import AITable, AITableConfigError  # noqa: E402
from dws_util import DwsError, now_iso  # noqa: E402
from verify_decisions import EVIDENCE_MAX_LEN, RECOMMEND_VALUES, load_json, norm_item  # noqa: E402

MATCH_SOURCE_SYSTEM = "系统匹配"        # 老插件口径：只有「系统匹配」参与删旧建新
MATCH_SOURCE_MANUAL = "人工匹配"        # 人工匹配一律不动，但要算进岗位统计（D15）
COMM_STATUS_ONBOARDED = "已入职"        # 老插件铁律
JOB_STATUS_OPEN = "招聘中"
FILTER_VALUE_CHUNK = 60                 # 单次 filter 的值个数（dws operands 上限 100，留余量）
CREATE_CHUNK = 100                      # 契约要求：batch_create ≤100/片（aitable_io 内部也会分片）


def _now() -> str:
    try:
        return now_iso()
    except Exception:
        return _dt.datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("markdown", "text", "name", "value"):
            if isinstance(v.get(k), str):
                return v[k]
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "\n".join(as_text(x) for x in v)
    return str(v)


def as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        out = []
        for it in v:
            s = (as_text(it) if isinstance(it, dict) else str(it)).strip()
            if s:
                out.append(s)
        return out
    s = as_text(v).strip()
    return [s] if s else []


def join_list(items: Sequence[Any], sep: str = "、") -> str:
    out = []
    for it in items or []:
        s = as_text(it).strip()
        if s and s not in out:
            out.append(s)
    return sep.join(out)


def chunks(items: Sequence[Any], size: int) -> List[List[Any]]:
    n = max(1, int(size))
    return [list(items[i:i + n]) for i in range(0, len(items), n)]


def gate_text(job: Dict[str, Any], gate_detail: Optional[Dict[str, Any]]) -> str:
    """写进「硬性门槛」字段：岗位要求原文 + 本条判定结果（✓/✗），一眼能看懂为什么过。"""
    hg = job.get("hard_gates") or {}
    base = (job.get("hard_gates_raw") or "").strip()
    if not base:
        base = "|".join("%s:%s" % (lab, hg.get(k) or "无明确要求")
                        for k, lab in (("education", "学历"), ("major", "专业"),
                                       ("years", "年限"), ("certificates", "证书")))
    if isinstance(gate_detail, dict) and gate_detail:
        marks = []
        for k, lab in (("education", "学历"), ("major", "专业"),
                       ("years", "年限"), ("certificates", "证书")):
            if k in gate_detail:
                v = str(gate_detail[k]).strip().lower()
                ok = v.startswith(("pass", "达标", "符合", "满足", "true", "yes"))
                marks.append("%s%s" % (lab, "✓" if ok else "✗"))
        if marks:
            base = "%s ｜判定:%s" % (base, " ".join(marks))
    return base[:500]


# ---------------------------------------------------------------------------
# 索引构建：digest 优先，缺 digest 时从表里/decisions 里降级解析
# ---------------------------------------------------------------------------
def rows_needed(decisions: Dict[str, Any]) -> bool:
    """decisions 里有没有需要落库的 pass 条目（决定「查不到候选人」是不是致命）。"""
    return bool([p for p in (decisions.get("passed") or []) if isinstance(p, dict)])


def synth_digest_from_table(table: AITable, decisions: Dict[str, Any],
                            warnings: List[str]) -> Dict[str, Any]:
    """没传 --digest 时的降级路径：从**表里**现查岗位，拼一个够 verify 用的最小 digest。

    verify 真正需要的是岗位的 `must_skills / bonus_skills / weights`（算 ⊆ 与分数）
    和候选人的 `key / org_guess`（算覆盖率）。组织归属在这里不可靠，所以调用方必须
    用 `check_coverage=False`。
    """
    from build_match_input import parse_job_record      # 同一套解析口径，不重复实现
    jobs: List[Dict[str, Any]] = []
    alias: Dict[str, str] = {}
    try:
        recs = table.query_records("job", filter={"status": JOB_STATUS_OPEN}, all_pages=True)
    except (DwsError, AITableConfigError) as exc:
        warnings.append("没传 --digest 且查在招岗位失败（%s）→ 无法做集合/算术校验" % str(exc)[:160])
        recs = []
    for i, r in enumerate(recs, 1):
        cells = r.get("cells") or {}
        j = parse_job_record(cells, as_text(cells.get("job_name")))
        j["record_id"] = r.get("record_id")
        j["key"] = "j%02d" % i
        jobs.append(j)
        if j.get("job_id"):
            alias["job_id:%s" % j["job_id"]] = j["key"]
        if j.get("job_name"):
            alias["job_name:%s" % j["job_name"]] = j["key"]
    # decisions 里引用的 job_key 如果本身不是 j01..jNN，就用别名映射改写成一个存在的 key
    renamed = 0
    for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
        if not isinstance(e, dict):
            continue
        if e.get("job_key") and e["job_key"] not in {j["key"] for j in jobs} \
                and e["job_key"] in alias:
            e["job_key"] = alias[e["job_key"]]
            renamed += 1
        jks = e.get("job_keys")
        if isinstance(jks, list):
            new = []
            for jk in jks:
                if str(jk) not in {j["key"] for j in jobs} and str(jk) in alias:
                    new.append(alias[str(jk)])
                    renamed += 1
                else:
                    new.append(jk)
            e["job_keys"] = new
    if renamed:
        warnings.append("没传 --digest：%d 个 job_key 用 job_id/岗位名称 从表里映射回了岗位 key"
                        % renamed)
    keys = set()
    for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
        if isinstance(e, dict) and e.get("candidate_key"):
            keys.add(str(e["candidate_key"]))
    cands = [{"key": k, "name": None, "org_guess": None, "evidence": {}} for k in sorted(keys)]
    return {"batch_id": decisions.get("batch_id"), "candidates": cands, "jobs": jobs,
            "synthesized_from_table": True}


def build_job_index(table: Optional[AITable], digest: Optional[Dict[str, Any]],
                    warnings: List[str]) -> Dict[str, Dict[str, Any]]:
    """{job_key: job}。digest 缺失时按 job_id / job_name 从表里查在招岗位重建索引。"""
    if digest and isinstance(digest.get("jobs"), list) and digest["jobs"]:
        return {str(j["key"]): j for j in digest["jobs"] if isinstance(j, dict) and j.get("key")}
    if table is None:
        return {}
    warnings.append("没有 --digest：岗位信息改从表里查（job_key 需要 decisions 里带 "
                    "job_id 或 job_name 才能对上）")
    from build_match_input import parse_job_record      # 同一套解析口径，不重复实现
    recs = table.query_records("job", filter={"status": JOB_STATUS_OPEN}, all_pages=True)
    idx: Dict[str, Dict[str, Any]] = {}
    for i, r in enumerate(recs):
        cells = r.get("cells") or {}
        j = parse_job_record(cells, as_text(cells.get("job_name")))
        j["record_id"] = r.get("record_id")
        j["key"] = "j%02d" % (i + 1)
        idx[j["key"]] = j
        if j.get("job_id"):
            idx["job_id:%s" % j["job_id"]] = j
        if j.get("job_name"):
            idx["job_name:%s" % j["job_name"]] = j
    return idx


def resolve_job(job_index: Dict[str, Dict[str, Any]], entry: Dict[str, Any],
                jk: Optional[str]) -> Optional[Dict[str, Any]]:
    for cand in (jk, entry.get("job_id") and "job_id:%s" % entry.get("job_id"),
                 entry.get("job_name") and "job_name:%s" % entry.get("job_name")):
        if cand and cand in job_index:
            return job_index[cand]
    return None


def build_candidate_index(table: Optional[AITable], digest: Optional[Dict[str, Any]],
                          decisions: Dict[str, Any], warnings: List[str]) -> Dict[str, Dict[str, Any]]:
    """{candidate_key: candidate}。digest 缺失时退化成「decisions 里带的 name/phone」。"""
    idx: Dict[str, Dict[str, Any]] = {}
    if digest and isinstance(digest.get("candidates"), list):
        for c in digest["candidates"]:
            if isinstance(c, dict) and c.get("key"):
                idx[str(c["key"])] = dict(c)
    for e in (decisions.get("passed") or []) + (decisions.get("rejected") or []):
        if not isinstance(e, dict):
            continue
        ck = e.get("candidate_key")
        if ck is None:
            continue
        ck = str(ck)
        slot = idx.setdefault(ck, {"key": ck})
        for f in ("name", "candidate_name", "phone", "record_id", "org", "org_guess"):
            if e.get(f) and not slot.get(f):
                slot["org_guess" if f == "org" else f] = e[f]
    if not digest:
        warnings.append("没有 --digest：候选人档案信息只能从 decisions/表里取，"
                        "写入匹配记录的「候选人技能/期望职位/工作年限」可能不全")
    return idx


#: 契约 v3 §9#1 裁定：candidate_overrides 支持且仅支持这七个键（+定位用 candidate_key）。
#: 文档（match-verify/SKILL.md、ai-analysis-spec.md）与本清单必须保持一致。
OVERRIDE_KEYS = ("org", "category", "expected_location", "skills_extra",
                 "certificates_extra", "years_experience", "org_reason")


def apply_overrides(cand_index: Dict[str, Dict[str, Any]],
                    decisions: Dict[str, Any], warnings: List[str]) -> int:
    """把 agent 在批量判定回合补齐的稀疏字段（D13 years 复核 / D14 地点与证书 / 组织归一）
    合回候选人。

    支持的键（契约 v3 §9#1，七个）：`org`、`category`、`expected_location`、
    `skills_extra`(数组，与已有技能合并去重)、`certificates_extra`(数组，与已有证书合并去重)、
    `years_experience`(int，D13 复核后的修正值)、`org_reason`(组织判定理由，只进报告)。

    每处**实际改变值**的修正会记进 `cand["_override_changes"]`（业务字段名→新值），
    供 `writeback_overrides` 写回简历库与报告留痕使用。
    """
    n = 0
    for o in decisions.get("candidate_overrides") or []:
        if not isinstance(o, dict):
            continue
        ck = o.get("candidate_key")
        if ck is None or str(ck) not in cand_index:
            warnings.append("candidate_overrides 里的 candidate_key=%r 不在候选人索引里，已忽略" % ck)
            continue
        unknown = [k for k in o if k not in OVERRIDE_KEYS and k != "candidate_key"]
        if unknown:
            warnings.append("candidate_overrides[%s] 含不支持的键 %s（支持的键：%s），已忽略这些键"
                            % (ck, ",".join(sorted(unknown)), "/".join(OVERRIDE_KEYS)))
        c = cand_index[str(ck)]
        changed: Dict[str, Any] = c.get("_override_changes") or {}
        for src, dst in (("org", "org_guess"), ("category", "category_guess"),
                         ("expected_location", "expected_location"),
                         ("years_experience", "years_experience")):
            if o.get(src) is not None and o.get(src) != "":
                if c.get(dst) != o[src]:
                    c[dst] = o[src]
                    changed[dst] = o[src]
                    n += 1
        extra = as_list(o.get("skills_extra"))
        if extra:
            have = {norm_item(x) for x in as_list(c.get("skills"))}
            add = [x for x in extra if norm_item(x) not in have]
            if add:
                c["skills"] = as_list(c.get("skills")) + add
                changed["skills"] = list(c["skills"])
                n += 1
        certs_extra = as_list(o.get("certificates_extra"))
        if certs_extra:
            have = {norm_item(x) for x in as_list(c.get("certificates"))}
            add = [x for x in certs_extra if norm_item(x) not in have]
            if add:
                c["certificates"] = as_list(c.get("certificates")) + add
                changed["certificates"] = list(c["certificates"])
                n += 1
        if o.get("org_reason"):
            c["org_override_reason"] = o["org_reason"]
        if "years" in as_list(c.get("needs_review")) and o.get("years_experience") is not None:
            c["needs_review"] = [x for x in as_list(c.get("needs_review")) if x != "years"]
            c["years_reviewed"] = True
        if changed:
            c["_override_changes"] = changed
    return n


#: override 内部字段名 → 简历库业务字段名（writeback_overrides 用）
OVERRIDE_WRITEBACK_MAP = (("org_guess", "org"), ("category_guess", "category"),
                          ("expected_location", "expected_location"),
                          ("years_experience", "years_experience"),
                          ("skills", "skills"), ("certificates", "certificates"))


def writeback_overrides(table: AITable, cand_index: Dict[str, Dict[str, Any]],
                        warnings: List[str]) -> Dict[str, Any]:
    """把 candidate_overrides 的实际修正**写回简历库**（表是唯一事实源，修正必须持久化）。

    只写有变化且 config 里有对应字段的项；没有 record_id 的候选人只进 warnings
    （修正在本批匹配记录里仍然生效）。选项类字段先 ensure_options（只增不删），
    写后回读（D6）；失败如实进 warnings，不静默。
    """
    out: Dict[str, Any] = {"submitted": 0, "updated": 0, "failed": [],
                           "readback_ok": None, "dws_calls": 0, "elapsed_ms": 0,
                           "skipped_no_record_id": 0}
    have = set(table.field_keys("resume") or [])
    updates: List[Dict[str, Any]] = []
    for ck in sorted(cand_index):
        c = cand_index[ck]
        ch = c.get("_override_changes") or {}
        if not ch:
            continue
        rid = c.get("record_id")
        if not rid:
            out["skipped_no_record_id"] += 1
            warnings.append("override 写回：候选人 %s(%s) 没有 record_id，修正（%s）无法写回"
                            "简历库，仅在本批匹配记录里生效"
                            % (as_text(c.get("name")) or "?", ck, ",".join(sorted(ch))))
            continue
        cells: Dict[str, Any] = {}
        for src, dst in OVERRIDE_WRITEBACK_MAP:
            if src not in ch or dst not in have:
                continue
            v = ch[src]
            if dst == "skills":
                v = as_list(v)
            elif dst == "certificates":
                v = join_list(v, "、")[:500]
            if v in (None, "", []):
                continue
            cells[dst] = v
        if cells:
            updates.append({"record_id": rid, "cells": cells})
    if not updates:
        return out
    # ⚠️ 刻意**不依赖 ensure_options 建选项**（W-G 实测 2026-09-18，G base；
    #    W-H 已根治，2026-09-17）：旧版 ensure_options 走 `field update` 整体覆盖写，
    #    而 `field get` 的选项快照有最终一致性（W-H 实测能读到 7/94 的陈旧快照），
    #    陈旧/中间态快照进 payload 就会触发服务端给选项**重新分配 id**（churn）→
    #    存量记录里按旧 id 引用的多选/单选单元格悬空、值被**静默清空**（W-G 实测把
    #    27 条记录的「技能标签」清掉大半）。现 shared 层的 ensure_options 已改为
    #    「只读 + 延迟补建」（彻底移除 field update）：record create/update 写**选项名**
    #    时服务端自动补建缺失选项且不动已有 id（W-B/W-G/W-H 均实测，W-H 受控实验
    #    585 个存量多选值零丢失）。所以写回直接按名字写，靠 readback 校验兜底；
    #    读回缺值 → warnings 提示重跑（幂等）。
    out["submitted"] = len(updates)
    try:
        res = table.batch_update("resume", updates)
    except (DwsError, AITableConfigError) as exc:
        warnings.append("override 写回简历库失败（%s）：修正只在本批匹配记录里生效，"
                        "请重跑本步复核" % str(exc)[:200])
        out["failed"] = [{"record_id": u["record_id"], "reason": str(exc)[:200]} for u in updates]
        return out
    out["updated"] = res.get("updated", 0)
    out["failed"] = res.get("failed") or []
    out["dws_calls"] = res.get("dws_calls", 0)
    out["elapsed_ms"] = res.get("elapsed_ms", 0)
    for f in out["failed"]:
        warnings.append("override 写回失败：%s" % json.dumps(f, ensure_ascii=False)[:200])
    rb = table.readback_verify("resume", [u["record_id"] for u in updates],
                               sorted({k for u in updates for k in u["cells"]}),
                               expected={u["record_id"]: u["cells"] for u in updates},
                               settle_tries=2)
    out["readback_ok"] = bool(rb.get("ok"))
    out["dws_calls"] += rb.get("dws_calls", 0)
    if not rb.get("ok"):
        for m in (rb.get("mismatch") or [])[:10]:
            warnings.append("override 写回回读不一致：%s.%s 期望=%r 实得=%r"
                            % (m["record_id"], m["field"], m["expected"], m["actual"]))
        for mid in (rb.get("missing") or [])[:10]:
            warnings.append("override 写回回读不到记录 %s（写入传播延迟，请下一回合重跑复核）" % mid)
    return out


# ---------------------------------------------------------------------------
# 表侧操作
# ---------------------------------------------------------------------------
def fetch_comm_status(table: AITable, cand_index: Dict[str, Dict[str, Any]],
                      warnings: List[str]) -> Dict[str, Dict[str, Any]]:
    """1 次查询拿回本批候选人的「沟通状态」等表内事实（表是唯一事实源）。"""
    ids = [c.get("record_id") for c in cand_index.values() if c.get("record_id")]
    out: Dict[str, Dict[str, Any]] = {}
    fields = ["name", "phone", "comm_status", "org", "category", "expected_position",
              "years_experience", "skills", "education"]
    if ids:
        try:
            recs = table.query_records("resume", record_ids=ids, fields=fields)
            for r in recs:
                out[str(r.get("record_id"))] = r.get("cells") or {}
        except (DwsError, AITableConfigError) as exc:
            warnings.append("按 record_id 查简历表失败（%s），改用姓名查" % str(exc)[:160])
    names = sorted({as_text(c.get("name")).strip() for c in cand_index.values()
                    if as_text(c.get("name")).strip()})
    if len(out) < len(ids) and names:
        for part in chunks(names, FILTER_VALUE_CHUNK):
            try:
                recs = table.query_records("resume", filter={"name": part}, fields=fields,
                                           all_pages=True)
            except (DwsError, AITableConfigError) as exc:
                warnings.append("按姓名查简历表失败（%s）" % str(exc)[:160])
                continue
            for r in recs:
                cells = r.get("cells") or {}
                nm = as_text(cells.get("name")).strip()
                if nm:
                    out.setdefault("name:%s" % nm, cells)
    return out


def cells_for(cand: Dict[str, Any], table_cells: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """表里的值优先（唯一事实源），digest 的值兜底。"""
    tc = table_cells or {}
    def pick(field_key: str, *alt: str) -> Any:
        v = tc.get(field_key)
        if v not in (None, "", []):
            return v
        for a in alt:
            v = cand.get(a)
            if v not in (None, "", []):
                return v
        return None
    return {
        "name": as_text(pick("name", "name")) or None,
        "phone": as_text(pick("phone", "phone")) or None,
        "org": as_text(pick("org", "org_guess", "org")) or None,
        "comm_status": as_text(tc.get("comm_status")) or cand.get("comm_status") or None,
        "expected_position": as_text(pick("expected_position", "expected_position")) or None,
        "years_experience": pick("years_experience", "years_experience"),
        "skills": as_list(pick("skills", "skills")),
        "record_id": cand.get("record_id"),
    }


def find_stale_match_records(table: AITable, names: Sequence[str],
                             warnings: List[str]) -> List[Dict[str, Any]]:
    """查本批候选人在 match 表已有的「系统匹配」记录（幂等删旧用）。

    **一次批量查**（filter 里 name 传多值），不逐条查。人工匹配不动。

    ⚠️ 实测坑（本 worker 发现，W-B 层无法改，只能在这里绕）：
    dws 的 filters **不支持嵌套 or**。`aitable_io.build_filter` 对
    `{"source":"系统匹配","name":[n1,n2,...]}` 会生成
    `and[ eq(source), or[eq(name,n1), eq(name,n2)...] ]`，服务端直接报
    `INVALID_FILTER_OPERATOR: Invalid filter operator: 'or'. Supported operators:
    [all_of, exist, not_after, any_of, contain, after, none_of, lt, gt, ne, before,
    from_now, date_between, date_eq, exclusive, gte, un_exist, not_before, eq, lte]`
    —— **or 根本不在支持列表里**，只有当它是 filters 的**最外层**时才被接受
    （单字段多值那种情况 build_filter 会把 or 提到最外层，所以能跑通）。
    → 这里只用**单字段多值**（name）当最外层 or 查，`source` 拿回本地再过滤。
    """
    out: List[Dict[str, Any]] = []
    uniq = sorted({n for n in names if n})
    if not uniq:
        return out
    for part in chunks(uniq, FILTER_VALUE_CHUNK):
        try:
            recs = table.query_records("match", filter={"name": list(part)},
                                       fields=["name", "job_id", "job_name", "recommend",
                                               "source"],
                                       all_pages=True)
        except (DwsError, AITableConfigError) as exc:
            warnings.append("查旧「系统匹配」记录失败（%s）→ 本次无法保证幂等，"
                            "可能产生重复匹配记录，请重跑" % str(exc)[:200])
            continue
        for r in recs:
            cells = r.get("cells") or {}
            if as_text(cells.get("source")).strip() == MATCH_SOURCE_SYSTEM:
                out.append(r)
    return out


def recompute_job_stats(table: AITable, job_ids: Sequence[str],
                        warnings: List[str]) -> Dict[str, Dict[str, int]]:
    """D15：从表里查每个受影响岗位的**全部**匹配记录（含人工匹配、含历史批次），重算四个数字。

    **禁止**用本批 decisions 直接累加（会漏历史与人工记录导致统计漂移）。
    """
    stats: Dict[str, Dict[str, int]] = {j: {"total": 0, "recommend": 0, "pending": 0, "reject": 0}
                                        for j in job_ids if j}
    uniq = sorted({j for j in job_ids if j})
    for part in chunks(uniq, FILTER_VALUE_CHUNK):
        try:
            recs = table.query_records("match", filter={"job_id": part},
                                       fields=["job_id", "recommend", "source"],
                                       all_pages=True)
        except (DwsError, AITableConfigError) as exc:
            warnings.append("D15 统计重算：查岗位 %s 的全部匹配记录失败（%s）→ "
                            "该批岗位统计**不回填**（宁可不写也不写错）"
                            % (",".join(part[:3]), str(exc)[:160]))
            for j in part:
                stats.pop(j, None)
            continue
        for r in recs:
            cells = r.get("cells") or {}
            jid = as_text(cells.get("job_id")).strip()
            if not jid or jid not in stats:
                # 表里可能有本批之外的岗位记录（历史），不影响；只统计受影响的
                continue
            stats[jid]["total"] += 1
            rec = as_text(cells.get("recommend")).strip()
            if rec == "推荐":
                stats[jid]["recommend"] += 1
            elif rec == "待定":
                stats[jid]["pending"] += 1
            elif rec == "不推荐":
                stats[jid]["reject"] += 1
    return stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def apply(config_path: str, decisions_path: str, out_dir: str,
          digest_path: Optional[str] = None, dry_run: bool = False,
          batch_id: Optional[str] = None) -> Dict[str, Any]:
    t0 = time.time()
    outdir = Path(out_dir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "ok": False, "dry_run": bool(dry_run), "generated_at": _now(),
        "elapsed_ms": 0, "dws_calls": 0, "retry_count": 0,
        "decisions_path": os.path.abspath(str(Path(decisions_path).expanduser())),
        "digest_path": None, "config_path": os.path.abspath(str(Path(config_path).expanduser())),
        "rows": [], "match_rows": [], "summary": {}, "warnings": [],
        "verify": None, "job_stats": {}, "errors": [],
    }
    # ⚠️ 让 warnings 与 report["warnings"] 是**同一个 list 对象**：
    #    本函数有多条提前 return 的失败路径，逐个赋值容易漏（第一版就漏了，
    #    结果 apply_report.json 里 warnings 恒为空，违反契约 D6「失败可见」）。
    warnings: List[str] = report["warnings"]

    # ---- 1. 读 decisions / digest ----
    decisions, derr = load_json(Path(decisions_path).expanduser(), "decisions")
    if derr and derr["code"] != "markdown_fence_stripped":
        report["errors"].append(derr["detail"])
        return _finish(report, outdir, t0, exit_code=1)
    if derr:
        warnings.append(derr["detail"])
    digest = None
    if digest_path:
        digest, gerr = load_json(Path(digest_path).expanduser(), "digest")
        if gerr and gerr["code"] != "markdown_fence_stripped":
            report["errors"].append(gerr["detail"])
            return _finish(report, outdir, t0, exit_code=1)
        if gerr:
            warnings.append(gerr["detail"])
        report["digest_path"] = os.path.abspath(str(Path(digest_path).expanduser()))
    else:
        guess = Path(os.path.abspath(str(Path(decisions_path).expanduser()))).parent / "digest.json"
        if guess.exists():
            digest, _ = load_json(guess, "digest")
            report["digest_path"] = os.path.abspath(str(guess))
            warnings.append("没传 --digest，自动用了同目录的 %s" % guess)

    table = None
    if not dry_run:
        try:
            table = AITable(config_path)
        except Exception as exc:                          # config 坏 → 直接失败，别猜
            report["errors"].append("打不开 config.json：%s" % exc)
            return _finish(report, outdir, t0, exit_code=1)

    # ---- 2. 先校验（不通过就不写库）----
    from verify_decisions import verify as _verify
    check_coverage = True
    if digest is None and table is not None:
        # 降级路径：从表里现查岗位拼一个最小 digest（集合/算术/结构照查，覆盖率查不了）
        digest = synth_digest_from_table(table, decisions, warnings)
        check_coverage = False
        warnings.append("没有 --digest：岗位分母改从表里现查，**覆盖率无法校验**"
                        "（强烈建议传 --digest，否则漏判的组合发现不了）")
    if digest is None:
        # dry-run 且没 digest：连岗位分母都没有，只能做结构校验
        warnings.append("没有 digest（dry-run 不查表）：无法做覆盖率与分母校验，"
                        "只查 JSON 结构/evidence/推荐状态取值")
        vres = _verify({"candidates": [], "jobs": []}, decisions, check_coverage=False)
        keep = ("bad_json", "decisions_not_object", "decisions_empty", "passed_not_list",
                "rejected_not_list", "entry_not_object", "missing_field", "job_keys_not_list",
                "evidence_too_long", "evidence_empty", "recommend_invalid_value",
                "gate_detail_inconsistent", "gate_detail_not_object")
        vres["errors"] = [e for e in vres["errors"] if e["code"] in keep]
        vres["ok"] = not vres["errors"]
    else:
        vres = _verify(digest, decisions, check_coverage=check_coverage)
    report["verify"] = {k: vres.get(k) for k in ("ok", "counts", "coverage", "summary",
                                                 "errors", "warnings")}
    for w in vres.get("warnings") or []:
        warnings.append("verify[%s] %s: %s" % (w["code"], w["key"], w["detail"]))
    if not vres.get("ok"):
        report["errors"] = ["verify 不通过（%d 个问题），按契约 D6 **不写库**" % len(vres["errors"])]
        report["errors"] += ["%s | %s | %s" % (e["code"], e["key"], e["detail"])
                             for e in vres["errors"]]
        report["verify_problems"] = vres["errors"]
        return _finish(report, outdir, t0, exit_code=1)

    job_index = build_job_index(table, digest, warnings)
    cand_index = build_candidate_index(table, digest, decisions, warnings)
    n_ovr = apply_overrides(cand_index, decisions, warnings)
    if n_ovr:
        warnings.append("已合入 %d 处 candidate_overrides（组织/分类/期望地点/工作年限复核/"
                        "技能补充/证书补充）" % n_ovr)
    # override 明细进报告（含 org_reason，dry-run 也记录 → 契约 v3 §9#1 的凭证）
    report["overrides"] = [
        {"candidate_key": ck,
         "name": as_text(cand_index[ck].get("name")) or None,
         "record_id": cand_index[ck].get("record_id"),
         "changed": dict(cand_index[ck].get("_override_changes") or {}),
         "org_reason": cand_index[ck].get("org_override_reason")}
        for ck in sorted(cand_index)
        if cand_index[ck].get("_override_changes") or cand_index[ck].get("org_override_reason")]
    if n_ovr and table is not None:
        # D13/D14：修正必须持久化到简历库（表是唯一事实源）；失败只进 warnings（D6 可见）
        report["overrides_writeback"] = writeback_overrides(table, cand_index, warnings)
    elif n_ovr and dry_run:
        report["overrides_writeback"] = {"skipped": "dry-run 不写库；实跑时会把上述修正写回简历库"}

    audit = {(a["candidate_key"], a["job_key"]): a for a in (vres.get("passed_audit") or [])}
    passed = [p for p in (decisions.get("passed") or []) if isinstance(p, dict)]
    rejected = [r for r in (decisions.get("rejected") or []) if isinstance(r, dict)]

    # ---- 3. 表内事实：沟通状态（已入职铁律）----
    table_cells: Dict[str, Dict[str, Any]] = {}
    onboarded: List[str] = []
    if table is not None:
        raw = fetch_comm_status(table, cand_index, warnings)
        for ck, c in cand_index.items():
            rid = c.get("record_id")
            cells = raw.get(str(rid)) if rid else None
            if cells is None:
                cells = raw.get("name:%s" % as_text(c.get("name")).strip())
            if cells:
                table_cells[ck] = cells
                c.setdefault("name", as_text(cells.get("name")) or c.get("name"))
        # override 修正值优先于刚读回的表值：写回简历库与本次查询之间可能有传播延迟
        # （契约 §10 R2），读到的可能是写回前的旧值 → 以 agent 修正值为准。
        for ck, c in cand_index.items():
            ch = c.get("_override_changes") or {}
            if ch and ck in table_cells:
                tc = table_cells[ck]
                for src, dst in OVERRIDE_WRITEBACK_MAP:
                    if src not in ch:
                        continue
                    tc[dst] = (join_list(ch[src], "、")[:500] if dst == "certificates"
                               else as_list(ch[src]) if dst == "skills" else ch[src])
        for ck, c in cand_index.items():
            cs = as_text((table_cells.get(ck) or {}).get("comm_status")).strip() \
                or as_text(c.get("comm_status")).strip()
            if cs == COMM_STATUS_ONBOARDED:
                onboarded.append(ck)
        if onboarded:
            warnings.append("老插件铁律：%d 个候选人「沟通状态=已入职」→ 删记录、不打分、不推荐：%s"
                            % (len(onboarded),
                               ",".join(as_text(cand_index[k].get("name")) for k in onboarded)))

    names = sorted({as_text(cells_for(cand_index[k], table_cells.get(k)).get("name")).strip()
                    for k in cand_index} - {""})
    # build_match_input 已把「沟通状态=已入职」的人从 digest 剔除（他们不会出现在 decisions 里），
    # 但老插件铁律要求他们的「系统匹配」记录**照样要删** → 从 digest.meta.excluded_onboarded 补进来
    excluded_onboarded_names: List[str] = []
    if isinstance(digest, dict) and isinstance(digest.get("meta"), dict):
        for o in digest["meta"].get("excluded_onboarded") or []:
            nm = as_text((o or {}).get("name")).strip()
            if nm and nm not in names:
                excluded_onboarded_names.append(nm)
    if excluded_onboarded_names:
        warnings.append("老插件铁律：另需清理 %d 个已入职候选人的历史「系统匹配」记录（他们已被 "
                        "build_match_input 排除在本批判定之外）：%s"
                        % (len(excluded_onboarded_names), ",".join(excluded_onboarded_names)))
    names_for_delete = sorted(set(names) | set(excluded_onboarded_names))
    if table is not None and not names and rows_needed(decisions):
        # 没姓名就没法定位本批候选人做「删旧建新」→ 宁可不写也不写出重复记录（D6 失败可见）
        report["errors"].append(
            "无法定位本批候选人（decisions 里没有姓名，且没传 --digest 拿不到候选人档案）→ "
            "幂等「删旧建新」无法保证，拒绝写库。请加 --digest <digest.json> 重跑")
        return _finish(report, outdir, t0, exit_code=1)

    # ---- 4. 幂等：批量查旧「系统匹配」记录 → 一次批量删 ----
    stale: List[Dict[str, Any]] = []
    del_res: Dict[str, Any] = {"deleted": 0, "failed": [], "dws_calls": 0}
    if table is not None:
        stale = find_stale_match_records(table, names_for_delete, warnings)
        stale_ids = [s.get("record_id") for s in stale if s.get("record_id")]
        if stale_ids:
            del_res = table.batch_delete("match", stale_ids)
            if del_res.get("failed"):
                for f in del_res["failed"]:
                    warnings.append("删旧记录失败：%s" % json.dumps(f, ensure_ascii=False)[:200])
        else:
            warnings.append("本批候选人在 match 表没有旧的「系统匹配」记录（首次匹配或已清理干净）")

    # ---- 5. 批量建匹配记录（只建 passed 且候选人未入职的）----
    rows_to_create: List[Dict[str, Any]] = []
    row_meta: List[Dict[str, Any]] = []
    affected_job_ids: List[str] = []
    for s in stale:
        jid = as_text((s.get("cells") or {}).get("job_id")).strip()
        if jid and jid not in affected_job_ids:
            affected_job_ids.append(jid)

    skipped_onboarded = 0
    skipped_invalid: List[Dict[str, Any]] = []
    for p in passed:
        ck = str(p.get("candidate_key"))
        jk = str(p.get("job_key"))
        a = audit.get((ck, jk))
        if not a or not a.get("valid"):
            # D16：命中项越界（编造）→ 该条无效，不建记录；D6：必须可见，进 warnings 与清单
            skipped_invalid.append({
                "candidate_key": ck, "job_key": jk,
                "candidate_name": (cand_index.get(ck) or {}).get("name"),
                "job_name": ((resolve_job(job_index, p, jk) or {}).get("job_name")),
                "reason": (a or {}).get("invalid_reason") or "verify 未给出该条的算术复核结果",
                "fabricated_hits": (a or {}).get("fabricated_hits")})
            continue
        if ck in onboarded:
            skipped_onboarded += 1
            continue
        job = resolve_job(job_index, p, jk)
        cand = cand_index.get(ck, {"key": ck})
        if not job:
            warnings.append("passed %s×%s：在岗位索引里找不到岗位，未建记录" % (ck, jk))
            continue
        tc = table_cells.get(ck) or {}
        merged = cells_for(cand, tc)
        rc = a["recomputed"]
        jid = as_text(job.get("job_id")).strip() or None
        rec = {
            "name": merged["name"],
            "phone": merged["phone"],
            "job_name": as_text(job.get("job_name")) or None,
            "job_id": jid,                                     # D1：普通 text，脚本自己 join
            "org": merged["org"] or as_text(job.get("org")) or None,
            "source": MATCH_SOURCE_SYSTEM,
            "cand_skills": join_list(merged["skills"], "、")[:500] or None,
            "must_skills": join_list(job.get("must_skills"), "\n")[:1000] or None,
            "bonus_skills": join_list(job.get("bonus_skills"), "\n")[:1000] or None,
            "hard_gates": gate_text(job, p.get("gate_detail")) or None,
            "expected_position": merged["expected_position"] or None,
            "years_experience": (_years_text(merged["years_experience"]) or None),
            "skill_score": rc["skill_score"],
            "bonus_score": rc["bonus_score"],
            "total_score": rc["total_score"],
            "recommend": rc["recommend"],
            "update_time": _today(),
            "evidence": (as_text(p.get("evidence")).strip()[:EVIDENCE_MAX_LEN] or None),
        }
        rec = {k: v for k, v in rec.items() if v is not None}
        rows_to_create.append(rec)
        row_meta.append({"candidate_key": ck, "job_key": jk, "candidate_name": merged["name"],
                         "job_name": rec.get("job_name"), "job_id": jid,
                         "department": as_text(job.get("department")) or None,
                         "record": rec, "recomputed": rc,
                         "model_scores": a.get("model") or {},
                         "mismatch": a.get("mismatch") or [],
                         "evidence": rec.get("evidence")})
        if jid and jid not in affected_job_ids:
            affected_job_ids.append(jid)

    # 缺陷3 修复（W-F S9 实测，2026-09-17）：**受本批影响的岗位**不止「建了记录/删了旧记录」
    # 的岗位，还包括「本批对它做过判定但一个达标者都没有」的零匹配岗位。这类岗位此前
    # 不进 affected_job_ids → 四个统计字段（候选人总数/推荐数/待定数/不推荐数）留空，
    # recruit-dashboard 的「待补/标红」逻辑分不清"还没算"和"算出来是 0"。
    # 现在把 decisions 里 passed/rejected 引用到的岗位全部算作受影响：
    # 即使重算结果四个数字都是 0，也**显式写入 0**（recompute_job_stats 对查不到
    # 匹配记录的岗位返回全 0，stat_updates 会原样回填并回读校验）。
    # 边界：只动 decisions 引用到的岗位（= 与本批判定相关），digest 里没有被本批
    # 判定触及的岗位（如其它组织的岗位）不写，避免无谓写调用与权限风险。
    for p in passed:
        job_r = resolve_job(job_index, p, str(p.get("job_key")))
        jid_r = as_text((job_r or {}).get("job_id")).strip()
        if jid_r and jid_r not in affected_job_ids:
            affected_job_ids.append(jid_r)
    for r in rejected:
        for jk in (r.get("job_keys") or []):
            job_r = resolve_job(job_index, r, str(jk))
            jid_r = as_text((job_r or {}).get("job_id")).strip()
            if jid_r and jid_r not in affected_job_ids:
                affected_job_ids.append(jid_r)

    create_res: Dict[str, Any] = {"created": 0, "failed": [], "record_ids": [],
                                  "dws_calls": 0, "elapsed_ms": 0, "submitted": 0,
                                  "isolate_extra_calls": 0}
    if table is not None and rows_to_create:
        # 契约要求 ≤100 条/片：这里显式分片（aitable_io 内部也会兜底再切一次）
        for piece in chunks(rows_to_create, CREATE_CHUNK):
            r = table.batch_create("match", piece)
            create_res["created"] += r.get("created", 0)
            create_res["submitted"] += r.get("submitted", 0)
            create_res["record_ids"] += r.get("record_ids", [])
            create_res["failed"] += r.get("failed", [])
            create_res["dws_calls"] += r.get("dws_calls", 0)
            create_res["elapsed_ms"] += r.get("elapsed_ms", 0)
            create_res["isolate_extra_calls"] += r.get("isolate_extra_calls", 0)
        if create_res.get("failed"):
            # 选项不存在导致的失败 → ensure_options 后重试一次（retry_count 记进报告）
            opt_failed = [f for f in create_res["failed"]
                          if "option" in str(f.get("reason", "")).lower()
                          or "选项" in str(f.get("reason", ""))]
            if opt_failed:
                report["retry_count"] += 1
                warnings.append("有 %d 行因选项问题写失败，ensure_options 后重试一次"
                                % len(opt_failed))
                _ensure_select_options(table, rows_to_create, warnings)
                retry_rows = [f["row"] for f in opt_failed if isinstance(f.get("row"), dict)]
                res2 = table.batch_create("match", retry_rows)
                create_res["created"] += res2.get("created", 0)
                create_res["record_ids"] += res2.get("record_ids", [])
                create_res["dws_calls"] += res2.get("dws_calls", 0)
                still = res2.get("failed", [])
                retried = {(f.get("row_index")) for f in opt_failed}
                create_res["failed"] = [f for f in create_res["failed"]
                                        if f.get("row_index") not in retried] + still
                if res2.get("created"):
                    warnings.append("ensure_options 后重试成功 %d 行（剩余失败 %d 行）"
                                    % (res2.get("created", 0), len(still)))
            for f in create_res["failed"]:
                warnings.append("建匹配记录失败：%s" % json.dumps(f, ensure_ascii=False)[:300])

    # ---- 6. D15：岗位统计脚本重算 + 一次 batch_update 回填 ----
    stats: Dict[str, Dict[str, int]] = {}
    stat_update_res: Dict[str, Any] = {"updated": 0, "failed": [], "dws_calls": 0}
    stat_updates: List[Dict[str, Any]] = []
    job_records: Dict[str, str] = {}
    for j in job_index.values():
        jid = as_text(j.get("job_id")).strip()
        if jid and j.get("record_id"):
            job_records[jid] = j["record_id"]
    stat_job_ids = [j for j in affected_job_ids if j in job_records]
    missing_rec = [j for j in affected_job_ids if j not in job_records]
    if missing_rec:
        warnings.append("有 %d 个受影响岗位拿不到 record_id（%s）→ 无法回填统计，"
                        "请传 --digest 或检查 job 表 岗位ID"
                        % (len(missing_rec), ",".join(missing_rec[:5])))
    if table is not None and stat_job_ids:
        stats = recompute_job_stats(table, stat_job_ids, warnings)
        stat_updates = [{"record_id": job_records[jid],
                         "cells": {"stat_total": s["total"], "stat_recommend": s["recommend"],
                                   "stat_pending": s["pending"], "stat_reject": s["reject"]}}
                        for jid, s in sorted(stats.items())]
        if stat_updates:
            stat_update_res = table.batch_update("job", stat_updates)   # **一次**回填（D15）
            if stat_update_res.get("failed"):
                for f in stat_update_res["failed"]:
                    warnings.append("回填岗位统计失败：%s" % json.dumps(f, ensure_ascii=False)[:200])

    # ---- 7. 写后必回读（D6）----
    rb_match: Dict[str, Any] = {}
    rb_job: Dict[str, Any] = {}
    if table is not None and create_res.get("record_ids"):
        want = ["name", "job_id", "job_name", "source", "skill_score", "bonus_score",
                "total_score", "recommend", "evidence"]
        want = [w for w in want if w in (table.field_keys("match") or [])]
        expected = {}
        for rid, meta in zip(create_res["record_ids"], row_meta):
            # ⚠️ 实测坑（aitable_io.sanitize_text 文档串）：写入前会净化文本，
            # 所以「写入 vs 读回」的比对基准必须拿 sanitize_text(原文)，否则会误判成不一致。
            expected[rid] = {k: (table.sanitize_text(v) if isinstance(v, str) else v)
                             for k, v in meta["record"].items() if k in want and v is not None}
        rb_match = table.readback_verify("match", create_res["record_ids"], want,
                                         expected=expected, settle_tries=3)
        if not rb_match.get("ok"):
            for m in (rb_match.get("mismatch") or [])[:20]:
                warnings.append("回读不一致：%s.%s 期望=%r 实得=%r"
                                % (m["record_id"], m["field"], m["expected"], m["actual"]))
            for mid in (rb_match.get("missing") or [])[:20]:
                warnings.append("回读不到刚建的匹配记录 %s（服务端写入传播延迟，"
                                "请在下一回合重跑回读复核）" % mid)
    if table is not None and stat_update_res.get("record_ids"):
        rb_job = table.readback_verify(
            "job", stat_update_res["record_ids"],
            ["stat_total", "stat_recommend", "stat_pending", "stat_reject"],
            expected={u["record_id"]: u["cells"] for u in stat_updates},
            settle_tries=2)
        if not rb_job.get("ok"):
            for m in (rb_job.get("mismatch") or [])[:20]:
                warnings.append("岗位统计回读不一致：%s.%s 期望=%r 实得=%r"
                                % (m["record_id"], m["field"], m["expected"], m["actual"]))
            for jid in (rb_job.get("missing") or [])[:10]:
                warnings.append("岗位统计回读不到记录 %s" % jid)

    # ---- 8. 用户可读清单 ----
    report.update(_build_rows(cand_index, table_cells, row_meta, rejected, job_index,
                              onboarded, create_res, skipped_onboarded, skipped_invalid))
    combos = (vres.get("counts") or {}).get("expected_pairs") or 0
    # 老插件按「每人每岗一个判定回合」的下界估；新插件 = 1 判定回合 + 2 脚本回合
    report["turns_saved_estimate"] = max(0, combos - 3)
    report["summary"].update({
        "batch_id": batch_id or (digest or {}).get("batch_id") or decisions.get("batch_id"),
        "stale_deleted": del_res.get("deleted", 0),
        "created": create_res.get("created", 0),
        "create_failed": len(create_res.get("failed") or []),
        "skipped_onboarded": skipped_onboarded,
        "skipped_invalid_hits": len(skipped_invalid),
        "onboarded_history_cleaned": len(excluded_onboarded_names),
        "passed_entries": len(passed),
        "rejected_entries": len(rejected),
        "combo_count": combos,
        "jobs_stat_refreshed": len(stats),
        "overrides_applied": n_ovr,
        "readback_match_ok": (rb_match.get("ok") if rb_match else None),
        "readback_job_ok": (rb_job.get("ok") if rb_job else None),
        "turns_saved_estimate": report["turns_saved_estimate"],
        "turns_saved_basis": "老插件下界 = 组合数 %d 个判定回合；新插件 = 3 个回合"
                             "（build / 批量判定 / apply）" % combos,
    })
    report["job_stats"] = {jid: dict(s, job_record_id=job_records.get(jid),
                                     record_id=job_records.get(jid))
                           for jid, s in sorted(stats.items())}
    report["skipped_invalid"] = skipped_invalid
    for si in skipped_invalid:
        warnings.append("未建记录（D16 命中项越界）：%s × %s ｜%s ｜越界项=%s"
                        % (si.get("candidate_name"), si.get("job_name"), si.get("reason"),
                           json.dumps(si.get("fabricated_hits"), ensure_ascii=False)[:200]))
    report["dws_calls"] = table.dws_calls if table is not None else 0
    report["dws_stats"] = table.stats() if table is not None else {}
    report["readback"] = {
        "match": {k: rb_match.get(k) for k in ("ok", "requested", "found", "settle_polls",
                                               "dws_calls", "elapsed_ms")} if rb_match else None,
        "job": {k: rb_job.get(k) for k in ("ok", "requested", "found", "settle_polls",
                                           "dws_calls", "elapsed_ms")} if rb_job else None,
    }
    report["delete"] = {k: del_res.get(k) for k in ("deleted", "dws_calls", "elapsed_ms")}
    report["create"] = {k: create_res.get(k) for k in ("created", "submitted", "dws_calls",
                                                       "elapsed_ms", "isolate_extra_calls")}
    report["stat_update"] = {k: stat_update_res.get(k) for k in ("updated", "submitted",
                                                                 "dws_calls", "elapsed_ms")}
    ok = (not create_res.get("failed")) and not report["errors"]
    if table is not None and create_res.get("record_ids") and rb_match and not rb_match.get("ok"):
        ok = False
        report["errors"].append("回读校验未通过（见 warnings），请重跑本步复核")
    report["ok"] = bool(ok) or bool(dry_run and not report["errors"])
    return _finish(report, outdir, t0, exit_code=0 if report["ok"] else 1)


def _years_text(v: Any) -> Optional[str]:
    if v in (None, ""):
        return None
    s = as_text(v).strip()
    if not s:
        return None
    try:
        f = float(s)
        return "%d年" % int(f) if f.is_integer() else "%s年" % s
    except ValueError:
        return s[:40]


def _ensure_select_options(table: AITable, rows: Sequence[Dict[str, Any]],
                           warnings: List[str]) -> None:
    """只在「因选项写失败」时才付这个成本（正常路径省 3 次 dws 调用）。

    注意：shared 层 ensure_options 已是**只读**实现（缺陷1 根治后不再 field update），
    这里调它只是刷新选项池现状 + 把缺失名记入 pending_options；真正把选项补进池子
    的是紧接着的 batch_create 重试——写选项名时服务端自动补建（不动已有 id）。
    """
    try:
        table.ensure_options("match", "source", [MATCH_SOURCE_SYSTEM])
        table.ensure_options("match", "recommend", list(RECOMMEND_VALUES))
        orgs = sorted({as_text(r.get("org")).strip() for r in rows if r.get("org")})
        if orgs:
            table.ensure_options("match", "org", orgs)
    except (DwsError, AITableConfigError) as exc:
        warnings.append("ensure_options 失败（%s）；服务端通常会自动补选项，继续重试写入"
                        % str(exc)[:160])


def _build_rows(cand_index: Dict[str, Dict[str, Any]], table_cells: Dict[str, Dict[str, Any]],
                row_meta: List[Dict[str, Any]], rejected: List[Dict[str, Any]],
                job_index: Dict[str, Dict[str, Any]], onboarded: Sequence[str],
                create_res: Dict[str, Any], skipped_onboarded: int,
                skipped_invalid: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """产出用户可读清单：岗位｜匹配度｜技能｜加分｜结论（未过门槛标注原因）。"""
    by_cand: Dict[str, List[Dict[str, Any]]] = {}
    for m in row_meta:
        by_cand.setdefault(m["candidate_key"], []).append(m)
    invalid_by_cand: Dict[str, List[Dict[str, Any]]] = {}
    for si in (skipped_invalid or []):
        invalid_by_cand.setdefault(str(si.get("candidate_key")), []).append(si)
    rej_by_cand: Dict[str, List[Dict[str, Any]]] = {}
    for r in rejected:
        ck = str(r.get("candidate_key") or "")
        for jk in (r.get("job_keys") or []):
            job = resolve_job(job_index, r, str(jk)) or {}
            rej_by_cand.setdefault(ck, []).append({
                "job_key": str(jk), "job_name": as_text(job.get("job_name")) or None,
                "job_id": as_text(job.get("job_id")) or None,
                "reason": as_text(r.get("reason")) or None})

    created_ids = set(create_res.get("record_ids") or [])
    rows: List[Dict[str, Any]] = []
    match_rows: List[Dict[str, Any]] = []
    seq = 0
    ordered = sorted(cand_index.keys(), key=lambda x: str(x))
    for ck in ordered:
        c = cand_index[ck]
        merged = cells_for(c, table_cells.get(ck))
        seq += 1
        if ck in onboarded:
            rows.append({"seq": seq, "candidate_key": ck, "name": merged["name"],
                         "file_name": c.get("file_name"), "org": merged["org"],
                         "result": "跳过", "reason": "沟通状态=已入职（老插件铁律：删记录、不打分、不推荐）",
                         "matches": [], "rejected": []})
            continue
        ms = by_cand.get(ck, [])
        inv = invalid_by_cand.get(ck, [])
        for m in ms:
            rc = m["recomputed"]
            status = "%s %d" % ("✅" if rc["recommend"] == "推荐" else
                                ("⏸" if rc["recommend"] == "待定" else "❌"), rc["total_score"])
            match_rows.append({
                "候选人": m["candidate_name"], "岗位": m["job_name"], "岗位ID": m["job_id"],
                "部门": m.get("department"),
                "匹配度": rc["total_score"], "技能": rc["skill_score"], "加分": rc["bonus_score"],
                "结论": rc["recommend"], "结论图标": status,
                "技能命中": "%d/%d" % (rc["skill_hits"], rc["skill_total"]),
                "加分命中": "%d/%d" % (rc["bonus_hits"], rc["bonus_total"]),
                "匹配依据": m.get("evidence"),
                "模型分数不一致": m.get("mismatch") or None,
                "written": bool(created_ids),
            })
        if ms and inv:
            result, reason = "已匹配", ("另有 %d 条 pass 因命中项越界（D16 判为编造）未建记录：%s"
                                       % (len(inv), "; ".join("%s×%s" % (x.get("candidate_key"),
                                                                          x.get("job_key"))
                                                              for x in inv)))
        elif ms:
            result, reason = "已匹配", None
        elif inv:
            result, reason = "失败", ("有 %d 条 pass 但命中项越界（D16 判为编造）→ 未建记录：%s"
                                     % (len(inv), "; ".join(str(x.get("fabricated_hits"))
                                                            for x in inv)))
        else:
            result, reason = "门槛不符", "全部同组织在招岗位均未通过硬性门槛，未产生记录"
        rows.append({"seq": seq, "candidate_key": ck, "name": merged["name"],
                     "file_name": c.get("file_name"), "org": merged["org"],
                     "result": result, "reason": reason,
                     "matches": [{"job_name": m["job_name"], "job_id": m["job_id"],
                                  "total_score": m["recomputed"]["total_score"],
                                  "skill_score": m["recomputed"]["skill_score"],
                                  "bonus_score": m["recomputed"]["bonus_score"],
                                  "recommend": m["recomputed"]["recommend"],
                                  "skill_hits": "%d/%d" % (m["recomputed"]["skill_hits"],
                                                           m["recomputed"]["skill_total"]),
                                  "bonus_hits": "%d/%d" % (m["recomputed"]["bonus_hits"],
                                                           m["recomputed"]["bonus_total"]),
                                  "evidence": m.get("evidence"),
                                  "model_mismatch": m.get("mismatch") or None}
                                 for m in ms],
                     "rejected": rej_by_cand.get(ck, [])})
    summary = {
        "candidates": len(ordered),
        "matched": len([r for r in rows if r["result"] == "已匹配"]),
        "gate_failed": len([r for r in rows if r["result"] == "门槛不符"]),
        "skip": len([r for r in rows if r["result"] == "跳过"]),
        "fail": len(create_res.get("failed") or []) + len([r for r in rows
                                                           if r["result"] == "失败"]),
        "match_records": len(match_rows),
        "recommend": len([m for m in match_rows if m["结论"] == "推荐"]),
        "pending": len([m for m in match_rows if m["结论"] == "待定"]),
        "reject": len([m for m in match_rows if m["结论"] == "不推荐"]),
        "skipped_onboarded_pairs": skipped_onboarded,
        "skipped_invalid_hits": len(skipped_invalid or []),
    }
    return {"rows": rows, "match_rows": match_rows, "summary": summary}


def _finish(report: Dict[str, Any], outdir: Path, t0: float, exit_code: int) -> Dict[str, Any]:
    report["elapsed_ms"] = int((time.time() - t0) * 1000)
    report["warnings"] = report.get("warnings") or []
    report["generated_at"] = _now()
    report.setdefault("summary", {})
    report["exit_code"] = exit_code
    report["python"] = "%d.%d.%d" % sys.version_info[:3]
    path = outdir / "apply_report.json"
    tmp = outdir / ("apply_report.json.tmp-%d" % int(time.time() * 1000))
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)                                 # 原子落盘（D7：产物必须存在且完整）
    # abspath 而非 resolve()：macOS 上 /tmp 是 /private/tmp 的符号链接，
    # resolve() 会让 ARTIFACT 行跟用户传进来的 --out-dir 长得不一样。
    report["_report_path"] = os.path.abspath(str(path))
    return report


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


def _printable_table(match_rows: Sequence[Dict[str, Any]]) -> List[str]:
    if not match_rows:
        return []
    out = ["候选人 | 岗位 | 匹配度 | 技能 | 加分 | 结论"]
    for m in match_rows:
        out.append("%s | %s | %s | %s | %s | %s"
                   % (m.get("候选人"), m.get("岗位"), m.get("匹配度"),
                      m.get("技能"), m.get("加分"), m.get("结论图标") or m.get("结论")))
    return out


if __name__ == "__main__":
    sys.exit(main())
