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
"""

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

from aitable_io import AITable, AITableConfigError  # noqa: E402
from dws_util import DwsError, now_iso  # noqa: E402

try:                                    # W-A 的 JD 归一化（缺失时降级，不致命）
    from extract_fields import extract_job_fields
except Exception:                       # pragma: no cover
    extract_job_fields = None

try:                                    # W-A 的简历分段（--from-table 切 evidence 用；缺失时降级）
    from extract_fields import extract_resume_fields
except Exception:                       # pragma: no cover
    extract_resume_fields = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_MAX_PER_BATCH = 8               # 契约 D3
JOB_STATUS_OPEN = "招聘中"
COMM_STATUS_ONBOARDED = "已入职"        # 老插件铁律：已入职不参与匹配
DEFAULT_LOCATION = "不限"               # 契约 D14 兜底
CHARS_PER_TOKEN = 1.5                   # 中文粗估：1.5 字符 / token（与前序实验同口径）

#: evidence 里**可截断**的两段的长度上限（字符）。education_text / cert_text 绝不截断（D4）。
WORK_TEXT_LIMIT = 900
SKILL_TEXT_LIMIT = 500
#: 岗位「任职要求原文」进 digest 的长度上限。前序实验用 520 字即达到 190/190 覆盖、零算术错。
REQUIREMENTS_LIMIT = 600

#: 契约 §3.3 candidates 元素的字段清单（只增不减：C1 多给的字段原样透传）
CANDIDATE_PASS_THROUGH = (
    "record_id", "file_name", "name", "phone", "email", "education", "school",
    "school_rank", "major", "years_experience", "certificates", "skills",
    "expected_position", "expected_location", "expected_salary",
    "org_guess", "org_confidence", "category_guess",
    "parse_status", "dedupe", "attachment_status",
)

# ---------------------------------------------------------------------------
# 评分口径原文（内嵌进 digest，让 LLM 不必去读别的文件）
# 抄自老插件 skills/match-verify/SKILL.md 第 4~6 步 与
# skills/recruit-model/references/system-config.md §6「评分与推荐口径」
# ---------------------------------------------------------------------------
SCORING_RULES = {
    "source": "老插件 recruit-match-suite-v0.1.0：skills/match-verify/SKILL.md 步骤 4-6 + "
              "skills/recruit-model/references/system-config.md §6 评分与推荐口径",
    "hard_gates": {
        "items": ["学历", "专业", "经验年限", "证书"],
        "rule": "四项一票否决：任一项不达标 → 该 候选人×岗位 组合进 rejected，"
                "**不创建任何匹配记录、不打分**（这就是省时间的关键：不再「先生成再删」）。",
        "evidence_first": "以岗位 hard_gates 字段为准，requirements_text（任职要求原文）为佐证；"
                          "候选人侧以字段 + evidence 原文段为准。",
        "insufficient": "证据不足以判断某项时**按不达标处理**，reason 写「证据不足:XX」。",
        "certificate_none": "岗位证书要求为「无明确要求」/「无」/空 时，证书项一律视为达标。",
        "certificate_preferred": "岗位 cert_is_preferred_not_required=true（JD 写的是「持证者优先」）时，"
                                 "证书项视为达标，只在加分项里体现。",
        "education_order": "学历达标 = 候选人学历 ≥ 岗位学历要求（博士 > 硕士 > 本科 > 大专）；"
                           "候选人 education 字段缺失但 evidence.education_text 里能看出学历的，以原文为准。",
        "major": "专业达标 = 候选人 major / evidence.education_text 里的专业与岗位 major_req 同大类或相关；"
                 "岗位 major_req 为空或「不限」时一律达标。",
        "years": "经验年限达标 = 候选人 years_experience ≥ 岗位 years_req_min（岗位写「X年以上」取 X）。"
                 "**候选人 needs_review 含 \"years\" 时（该值是脚本估出来的，28 份实测有 9 份），"
                 "必须先用 evidence.work_text / education_text 原文复核，"
                 "复核后的值写进 candidate_overrides[].years_experience，再判门槛。**",
    },
    "skill_score": "技能得分 = 命中必备技能数 / 岗位 must_skills 总数 × 100，四舍五入取整。"
                   "命中 = evidence 原文里有该技能的直接证据（同义词/缩写算命中，"
                   "如 PLC = S7-1200PLC、CAD = AUTO CAD）。",
    "bonus_score": "加分项得分 = 命中加分项数 / 岗位 bonus_skills 总数 × 100，四舍五入取整；"
                   "岗位 bonus_skills 为空列表时记 100。",
    "total_score": "匹配总分 = 技能得分 × weights.must + 加分项得分 × weights.bonus，四舍五入取整。"
                   "权重取岗位表字段（必备技能权重 / 加分项权重），缺省 0.7 / 0.3。",
    "recommend_thresholds": "总分 ≥ 80 → 「推荐」；60~79 → 「待定」；< 60 → 「不推荐」。",
    "empty_denominator": "岗位 must_skills 为空 → 技能得分记 0 并进 warnings（岗位数据有问题，"
                         "需人工确认）；岗位 bonus_skills 为空 → 加分项得分记 100"
                         "（沿用前序实验已验证口径；老插件 system-config §6 要求这种情况"
                         "先与用户确认，本插件取默认值并在 warnings 里提示）。",
    "rounding": "全部四舍五入取整（half-up，不是 python 的 banker's rounding）。",
    "no_fabrication": "禁止编造：skill_hits / bonus_hits 里列出的每一项**必须是岗位 "
                      "must_skills / bonus_skills 列表里的原文条目**，且能在 evidence 里对应到"
                      "简历原文的原词或明显同义词。越界项会被 verify_decisions.py 判为无效。",
    "scores_computed_by_script": "**分数不由模型输出**（契约 D16）：skill_score / bonus_score / "
                                 "total_score / 推荐状态一律由 apply_decisions.py 按上述口径从 "
                                 "skill_hits / bonus_hits / weights 重算。模型只需给 "
                                 "skill_hits / bonus_hits / recommend（recommend 作对照，"
                                 "不一致记 warnings，以脚本重算为准）。",
    "onboarded_rule": "老插件铁律：候选人「沟通状态=已入职」不参与匹配，其现有系统匹配记录一律删除、"
                      "不打分、不推荐。本脚本已把这类候选人从 digest 里剔除（见 meta.excluded_onboarded），"
                      "agent 不需要为他们产出任何决策。",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now() -> str:
    try:
        return now_iso()
    except Exception:
        return _dt.datetime.now().isoformat(timespec="seconds")


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def est_tokens(obj: Any) -> Tuple[int, int]:
    """返回 (字符数, 估算 token 数)。中文按 1.5 字符/token 粗估（与前序实验同口径）。"""
    n = len(compact(obj))
    return n, int(math.ceil(n / CHARS_PER_TOKEN))


def clip(s: Any, limit: int, mark: str = "…[截断]") -> str:
    """截断到 limit 字符；limit<=0 表示不截断。"""
    if s is None:
        return ""
    s = str(s)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit] + mark


def full(s: Any) -> str:
    """D4：教育/证书段**完整保留，不截断**。"""
    return "" if s is None else str(s)


def clean_ws(s: Any) -> str:
    if s is None:
        return ""
    return re.sub(r"[ \t\r\f\v]+", " ", str(s)).strip()


def as_list(v: Any) -> List[str]:
    """把表里读回的值归一成 list[str]（multipleSelect 读回是 list，text 是 str）。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        out = []
        for it in v:
            if isinstance(it, dict):
                it = it.get("name") or it.get("text") or it.get("value")
            if it is None:
                continue
            s = clean_ws(it)
            if s:
                out.append(s)
        return out
    if isinstance(v, dict):
        v = v.get("name") or v.get("text") or v.get("value")
    s = clean_ws(v)
    return [s] if s else []


def as_text(v: Any) -> str:
    """richText 读回可能是 {"markdown": ...}；singleSelect 可能是 {"name": ...}。"""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("markdown", "text", "name", "value"):
            if isinstance(v.get(k), str):
                return v[k]
        return compact(v)
    if isinstance(v, (list, tuple)):
        return "\n".join(as_text(x) for x in v)
    return str(v)


def as_number(v: Any, default: Optional[float] = None) -> Optional[float]:
    """number 字段读回是**字符串**（实测 "0.7"），统一转 float。"""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        if not m:
            return default
        try:
            return float(m.group(0))
        except ValueError:
            return default


def dedupe_keep_order(items: Sequence[str]) -> List[str]:
    seen, out = set(), []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 岗位侧文本解析（表里 hard_gates / must_skills / bonus_skills 都是 text 字段）
# ---------------------------------------------------------------------------
_HG_KEYS = (
    ("education", ("学历", "教育背景", "最低学历", "学历要求", "education")),
    ("major", ("专业", "专业要求", "专业方向", "major")),
    ("years", ("经验年限", "工作年限", "从业经验", "年限", "years", "经验")),
    ("certificates", ("证书", "职业资格", "培训经历", "持证", "证书要求", "certificates", "cert")),
)
_HG_JSON_ALIASES = {
    "学历": "education", "education_req": "education", "最低学历": "education",
    "专业": "major", "major_req": "major", "专业要求": "major",
    "经验年限": "years", "工作年限": "years", "年限": "years", "years_req": "years",
    "证书": "certificates", "cert_req": "certificates", "证书要求": "certificates",
    "education": "education", "major": "major", "years": "years",
    "certificates": "certificates",
}
_NUMBER_PREFIX = re.compile(r"^\s*\d{1,2}\s*[、.．)）,，:：]\s*")
_EMPTY_GATE = ("无", "无明确要求", "不限", "无要求", "/", "-", "None", "null", "")


def _split_skill_text(text: str) -> List[str]:
    """把「必备技能/加分项」text 字段切成条目列表。

    真实 JD 的条目本身可能含逗号（如「思路清晰，能够及时客观公正的进行账务和结算工作」），
    所以**先按换行/分号/竖线切**；只有切完仍是 1 条且看起来是「短标签串」时才按逗号/顿号切。
    """
    text = (text or "").strip()
    if not text:
        return []
    # 0) 也许存的就是 JSON 数组
    if text[0] in "[{":
        try:
            obj = json.loads(text)
            items = as_list(obj if isinstance(obj, list) else [obj])
            if items:
                return items
        except Exception:
            pass
    parts = re.split(r"[\n;；|]+", text)
    items = []
    for p in parts:
        p = clean_ws(_NUMBER_PREFIX.sub("", p))
        p = p.strip(" 　・·,，、")
        if p:
            items.append(p)
    if len(items) <= 1:
        alt = [clean_ws(_NUMBER_PREFIX.sub("", x)).strip(" 　・·")
               for x in re.split(r"[,，、]+", text)]
        alt = [a for a in alt if a]
        # 只有全是短标签时才认为逗号是分隔符（否则会把一句话切碎、虚增分母）
        if len(alt) >= 2 and all(len(a) <= 24 for a in alt):
            items = alt
    return dedupe_keep_order(items)


def _looks_like_prose(items: Sequence[str]) -> bool:
    """切完只有 0/1 条、或单条超长 → 认为表里存的是 JD 散文，需要走 W-A 归一化兜底。"""
    if not items:
        return True
    if len(items) == 1 and len(items[0]) > 40:
        return True
    return any(re.search(r"\d\s*[、.．]\s*\S", it) for it in items[:3]) and len(items) <= 2


def _gate_from_labelled(text: str) -> Dict[str, Optional[str]]:
    """从 `学历:本科|专业:机械/电气|年限:3|证书:无` 这类**带标签**的紧凑文本里取四项。"""
    out: Dict[str, Optional[str]] = {}
    if not text:
        return out
    body = text
    if body.strip().startswith("{"):
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    canon = _HG_JSON_ALIASES.get(str(k).strip())
                    if canon and v not in (None, ""):
                        out[canon] = clean_ws(v)
                return out
        except Exception:
            pass
    for canon, labels in _HG_KEYS:
        for lab in labels:
            m = re.search(r"%s\s*[:：]\s*([^|；;\n]*)" % re.escape(lab), body)
            if m:
                v = clean_ws(m.group(1)).strip(" 　,，、")
                if v:
                    out[canon] = v
                    break
    return out


def parse_job_record(cells: Dict[str, Any], file_name: str = "") -> Dict[str, Any]:
    """把岗位表一行（业务字段名 → 值）解析成契约 §3.3 的 jobs 元素。

    hard_gates / must_skills / bonus_skills 在表里都是 **text** 字段，格式不可控
    （可能是 job-intake 归一化后的紧凑串，也可能直接是 JD 散文），所以这里是
    「带标签解析 → W-A extract_job_fields 兜底」两级策略，并把用到的来源写进
    `skills_source` / `gates_source` 供排查。
    """
    hard_gates_text = as_text(cells.get("hard_gates"))
    req_text = as_text(cells.get("requirements"))
    resp_text = as_text(cells.get("responsibilities"))

    gates = _gate_from_labelled(hard_gates_text)
    gates_source = "table_labelled" if gates else "none"

    must = _split_skill_text(as_text(cells.get("must_skills")))
    bonus = _split_skill_text(as_text(cells.get("bonus_skills")))
    skills_source = "table"

    jd = None
    if extract_job_fields is not None and (req_text or hard_gates_text):
        try:
            jd = extract_job_fields(req_text or hard_gates_text, file_name)
        except Exception:
            jd = None
    if jd:
        # 门槛四项：表里没解析出来的，用 W-A 从「任职要求原文」抽的补
        filled = False
        for canon, jk in (("education", "education_req"), ("major", "major_req"),
                          ("years", "years_req"), ("certificates", "cert_req")):
            if not gates.get(canon) and jd.get(jk):
                gates[canon] = clean_ws(jd.get(jk))
                filled = True
        if filled:
            gates_source = ("table_labelled+extract" if gates_source == "table_labelled"
                            else "extract")
        # 技能列表：表里存的是散文（切不出条目）时，用 W-A 的切分结果
        if _looks_like_prose(must) and jd.get("must_skills"):
            must = [clean_ws(x) for x in jd["must_skills"] if clean_ws(x)]
            skills_source = "extract"
        if _looks_like_prose(bonus) and jd.get("bonus_skills"):
            bonus = [clean_ws(x) for x in jd["bonus_skills"] if clean_ws(x)]
            skills_source = ("extract" if skills_source == "extract" else "table+extract")

    for canon in ("education", "major", "years", "certificates"):
        v = gates.get(canon)
        if v is not None and v.strip() in _EMPTY_GATE:
            gates[canon] = "无明确要求" if canon == "certificates" else (v.strip() or None)
        gates.setdefault(canon, None)
    if not gates.get("certificates"):
        gates["certificates"] = "无明确要求"

    wm = as_number(cells.get("must_weight"), None)
    wb = as_number(cells.get("bonus_weight"), None)
    if wm is None and wb is None:
        wm, wb = 0.7, 0.3
    elif wm is None:
        wm = round(1.0 - wb, 4)
    elif wb is None:
        wb = round(1.0 - wm, 4)

    job = {
        "key": None,                              # 由调用方编号
        "record_id": None,
        "job_id": clean_ws(cells.get("job_id")) or None,
        "job_name": clean_ws(cells.get("job_name")) or None,
        "department": clean_ws(as_text(cells.get("department"))) or None,
        "org": clean_ws(as_text(cells.get("org"))) or None,
        "status": clean_ws(as_text(cells.get("status"))) or None,
        "work_location": as_list(cells.get("work_location")),
        "hard_gates": {
            "education": gates.get("education"),
            "major": gates.get("major"),
            "years": gates.get("years"),
            "certificates": gates.get("certificates"),
        },
        "must_skills": must,
        "bonus_skills": bonus,
        "weights": {"must": wm, "bonus": wb},
        # ---- 契约之外的只增字段（前序实验证明「任职要求原文」是判门槛的关键证据）----
        "requirements_text": clip(clean_ws(req_text), REQUIREMENTS_LIMIT),
        "responsibilities_text": clip(clean_ws(resp_text), 200),
        "years_req_min": (jd or {}).get("years_req_min"),
        "cert_is_preferred_not_required": bool((jd or {}).get("cert_is_preferred_not_required")),
        "hard_gates_raw": clean_ws(hard_gates_text) or None,
        "gates_source": gates_source,
        "skills_source": skills_source,
    }
    return job


# ---------------------------------------------------------------------------
# 候选人侧
# ---------------------------------------------------------------------------
def build_evidence(cand: Dict[str, Any]) -> Dict[str, str]:
    """D4：education_text / cert_text **完整保留**；work_text / skill_text 可截断。

    兼容 C1 把原文段放在 `evidence` 里或放在 `sections` 里（W-A extract_fields 用 `sections`）。
    """
    src = cand.get("evidence")
    if not isinstance(src, dict):
        src = cand.get("sections") if isinstance(cand.get("sections"), dict) else {}
    edu = full(src.get("education_text"))
    cert = full(src.get("cert_text"))
    work = clip(src.get("work_text"), WORK_TEXT_LIMIT)
    skill = clip(src.get("skill_text"), SKILL_TEXT_LIMIT)
    if not (edu or cert or work or skill):
        # 兜底：C1 只给了简历全文
        ft = full(cand.get("full_text") or cand.get("resume_text"))
        work = clip(ft, WORK_TEXT_LIMIT)
    return {"education_text": edu, "cert_text": cert, "work_text": work, "skill_text": skill}


def evidence_truncated(cand: Dict[str, Any], ev: Dict[str, str]) -> Dict[str, bool]:
    src = cand.get("evidence")
    if not isinstance(src, dict):
        src = cand.get("sections") if isinstance(cand.get("sections"), dict) else {}
    return {
        "education_text": len(full(src.get("education_text"))) != len(ev["education_text"]),
        "cert_text": len(full(src.get("cert_text"))) != len(ev["cert_text"]),
        "work_text": len(full(src.get("work_text"))) != len(ev["work_text"]),
        "skill_text": len(full(src.get("skill_text"))) != len(ev["skill_text"]),
    }


# --- evidence 兜底：W-A 的分段是按标题正则切的，实测 17 份真实简历里有 6 份切不出
#     「教育经历」段、2 份切不出「工作经历」段（简历根本没有这些标题）。D4 的坑
#     （许金×财务主管误杀、代文超×单晶生产主管漏判）正是「教育/证书段丢了」造成的，
#     所以段落为空时**必须**从简历全文里按关键词开窗兜底，而不是给 LLM 一个空字符串。
_EDU_HINT = re.compile(
    r"(学历|教育(?:经历|背景|情况|程度)?|毕业(?:院校|时间|学校)?|全日制|"
    r"大学本科|本科|大专|硕士|博士|学士|大学|学院)")
_CERT_HINT = re.compile(
    r"(证\s*书|资格证书|职业资格|职称|执业|注册[\u4e00-\u9fff]{0,6}(?:师|证)|"
    r"特种作业|电工证|会计师|驾驶证)")
_WORK_HINT = re.compile(
    r"(工作(?:经历|经验|履历)|职业经历|从业经历|任职经历|项目(?:经历|经验|描述)|"
    r"实习经历|主要工作|履历)")
_SKILL_HINT = re.compile(r"(专业技能|个人技能|技能特长|技能技巧|技术能力|专业能力|核心能力|技能)")


def _flat_window(text: str, hint: "re.Pattern", before: int = 40, after: int = 340) -> str:
    """在全文里找关键词，取一个压缩过空白的窗口。找不到返回 ""。"""
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text)
    m = hint.search(flat)
    if not m:
        return ""
    st = max(0, m.start() - before)
    return flat[st:m.start() + after].strip()


def enrich_evidence(cand: Dict[str, Any], full_text: str) -> List[str]:
    """段落为空时用简历全文兜底补 evidence。返回本次兜底的说明（进 warnings）。"""
    notes: List[str] = []
    ev = cand.get("evidence") or {}
    prov = cand.setdefault("evidence_provenance", {})
    for k in ("education_text", "cert_text", "work_text", "skill_text"):
        prov[k] = "section" if (ev.get(k) or "").strip() else "empty"
    ft = full_text[:20000] if full_text else ""
    if not ft:
        return notes
    plan = (("education_text", _EDU_HINT, 40, 340, WORK_TEXT_LIMIT),
            ("cert_text", _CERT_HINT, 40, 260, WORK_TEXT_LIMIT),
            ("work_text", _WORK_HINT, 20, WORK_TEXT_LIMIT, WORK_TEXT_LIMIT),
            ("skill_text", _SKILL_HINT, 20, SKILL_TEXT_LIMIT, SKILL_TEXT_LIMIT))
    for key, hint, before, after, limit in plan:
        if (ev.get(key) or "").strip():
            continue
        win = _flat_window(ft, hint, before, after)
        if not win:
            continue
        # 教育/证书段（D4）：兜底窗口也**不截断**；work/skill 段按各自上限截断
        ev[key] = win if key in ("education_text", "cert_text") else clip(win, limit)
        prov[key] = "full_text_fallback"
        notes.append("%s(%s)：evidence.%s 上游分段为空，已从简历全文按关键词兜底开窗 %d 字"
                     % (cand.get("name") or "?", cand.get("key"), key, len(ev[key])))
    if not (ev.get("work_text") or "").strip():
        # 连关键词都找不到 → 直接给正文头部（总比空字符串强）
        head = clip(re.sub(r"\s+", " ", ft), WORK_TEXT_LIMIT)
        if head:
            ev["work_text"] = head
            prov["work_text"] = "full_text_head"
            notes.append("%s(%s)：evidence.work_text 兜底也找不到段落标题，改取简历全文头部 %d 字"
                         % (cand.get("name") or "?", cand.get("key"), len(head)))
    cand["evidence"] = ev
    cand["evidence_chars"] = sum(len(v or "") for v in ev.values())
    return notes


def years_source_of(cand: Dict[str, Any]) -> Optional[str]:
    """W-A 输出的键名是 `years_experience_source`，契约 D13 写的是 `years_source`，两个都认。"""
    for k in ("years_source", "years_experience_source"):
        v = cand.get(k)
        if isinstance(v, str) and v:
            return v
    conf = cand.get("confidence")
    if isinstance(conf, dict) and conf.get("years_experience") == "estimated":
        return "estimated"
    return None


def normalize_candidate(cand: Dict[str, Any], idx: int, warnings: List[str]) -> Dict[str, Any]:
    """把 C1 的 candidates.json 元素归一成契约 §3.3 的 digest candidates 元素。"""
    key = clean_ws(cand.get("key")) or "c%02d" % (idx + 1)
    out: Dict[str, Any] = {"key": key}
    for f in CANDIDATE_PASS_THROUGH:
        if f in cand:
            out[f] = cand[f]
    out.setdefault("record_id", cand.get("record_id"))
    out.setdefault("name", cand.get("name"))
    out.setdefault("phone", cand.get("phone"))

    # 列表字段归一
    for f in ("certificates", "skills"):
        out[f] = dedupe_keep_order(as_list(cand.get(f)))

    # D14：期望地点缺失 → 兜底「不限」
    loc = clean_ws(cand.get("expected_location"))
    if not loc:
        loc = DEFAULT_LOCATION
        warnings.append("%s(%s)：期望地点缺失，按老插件铁律兜底填「不限」"
                        "（agent 若从 evidence 看出明确城市请在 candidate_overrides 覆盖）"
                        % (out.get("name") or "?", key))
    out["expected_location"] = loc

    # D13：估计出来的工作年限必须标 needs_review
    ysrc = years_source_of(cand)
    out["years_source"] = ysrc
    needs = dedupe_keep_order(as_list(cand.get("needs_review")))
    if ysrc == "estimated" and "years" not in needs:
        needs.append("years")
        warnings.append("%s(%s)：工作年限 %s 是脚本估算的（years_source=estimated），"
                        "已标 needs_review=[years]；agent 必须用 evidence 原文复核后在 "
                        "candidate_overrides.years_experience 回填，不要直接当硬门槛用"
                        % (out.get("name") or "?", key, out.get("years_experience")))
    out["needs_review"] = needs

    # evidence（D4）
    ev = build_evidence(cand)
    out["evidence"] = ev
    trunc = evidence_truncated(cand, ev)
    if trunc["education_text"] or trunc["cert_text"]:
        # 理论上不可能：build_evidence 对这两段不截断。真发生说明 C1 给的就是截断值。
        warnings.append("%s(%s)：education_text/cert_text 被上游截断，违反 D4，"
                        "门槛判定可能误杀/漏判" % (out.get("name") or "?", key))
    out["evidence_truncated"] = trunc
    out["evidence_chars"] = sum(len(v) for v in ev.values())

    # 透传 C1 的其余只增字段（years_experience_est / confidence / warnings 等）
    for k, v in cand.items():
        if k not in out and k not in ("evidence", "sections", "full_text", "resume_text"):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_candidates_file(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """吃 C1 产出的 candidates.json（契约 §6 接缝）。也容忍直接给一个 candidates 数组。"""
    if not path.exists():
        raise SystemExit("candidates.json 不存在：%s\n"
                         "（它由 resume-intake 的 intake_resume.py 产出，契约 §6；"
                         "请先跑简历入库，或用 --candidates 指向正确的绝对路径）" % path)
    with open(str(path), "r", encoding="utf-8") as fh:
        raw = fh.read()
    body = raw.strip()
    if body.startswith("```"):                       # 容错：agent 有时会带 markdown 围栏
        body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
    try:
        data = json.loads(body)
    except Exception as exc:
        raise SystemExit("candidates.json 不是合法 JSON：%s: %s" % (type(exc).__name__, exc))
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        raise SystemExit("candidates.json 结构不认识（既不是 dict 也不是 list）")
    cands = data.get("candidates")
    if not isinstance(cands, list):
        raise SystemExit("candidates.json 缺 candidates 数组（契约 §6）")
    return cands, data


def fetch_open_jobs(table: AITable, warnings: List[str]) -> List[Dict[str, Any]]:
    """从**表里**查在招岗位（契约 §6 b：不依赖 jobs_draft.json）。"""
    try:
        recs = table.query_records("job", filter={"status": JOB_STATUS_OPEN},
                                   all_pages=True)
    except (DwsError, AITableConfigError) as exc:
        raise SystemExit("查在招岗位失败：%s\n"
                         "（检查 config.json 里 job 表的 status 字段映射，"
                         "以及 base 是否可访问）" % exc)
    jobs = []
    for i, r in enumerate(recs):
        cells = r.get("cells") or {}
        j = parse_job_record(cells, clean_ws(cells.get("job_name")))
        j["record_id"] = r.get("record_id")
        j["key"] = "j%02d" % (i + 1)
        if not j["must_skills"]:
            warnings.append("岗位 %s(%s)：必备技能解析为空 → 技能得分分母为 0，"
                            "该岗位无法打分（请检查 job 表「必备技能」字段）"
                            % (j["job_name"], j["key"]))
        jobs.append(j)
    # 排序稳定：按 job_id → job_name → key（避免 set/dict 迭代序影响分片可复现性）
    jobs.sort(key=lambda x: (x.get("job_id") or "", x.get("job_name") or "", x["key"]))
    for i, j in enumerate(jobs):                      # 排序后重编号，保证 key 连续可读
        j["key"] = "j%02d" % (i + 1)
    return jobs


def check_onboarded(table: AITable, candidates: List[Dict[str, Any]],
                    warnings: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """老插件铁律：沟通状态=已入职 → 不参与匹配。

    以**表**为准（不是 candidates.json），1 次 `record query --record-ids` 拿回 comm_status，
    顺手用表里的 org / name / phone 校正 candidates.json（表是唯一事实源）。
    """
    ids = [c.get("record_id") for c in candidates if c.get("record_id")]
    if not ids:
        warnings.append("候选人没有 record_id，无法核对「沟通状态」，本批全部按参与处理")
        return candidates, []
    try:
        recs = table.query_records("resume", record_ids=ids,
                                   fields=["name", "phone", "comm_status", "org", "category",
                                           "full_text"])
    except (DwsError, AITableConfigError) as exc:
        warnings.append("核对「沟通状态」失败（%s），本批全部按参与处理" % str(exc)[:160])
        return candidates, []
    by_id = {r.get("record_id"): (r.get("cells") or {}) for r in recs}
    active, onboarded = [], []
    for c in candidates:
        cells = by_id.get(c.get("record_id"))
        if cells:
            cs = clean_ws(as_text(cells.get("comm_status")))
            c["comm_status"] = cs or None
            if not c.get("org_guess"):
                c["org_guess"] = clean_ws(as_text(cells.get("org"))) or None
            if not c.get("category_guess"):
                c["category_guess"] = clean_ws(as_text(cells.get("category"))) or None
            ft = as_text(cells.get("full_text"))
            if ft:
                c["_full_text_from_table"] = ft
            if cs == COMM_STATUS_ONBOARDED:
                onboarded.append({"key": c.get("key"), "name": c.get("name"),
                                  "record_id": c.get("record_id"),
                                  "reason": "沟通状态=已入职（老插件铁律：不参与匹配，"
                                            "其系统匹配记录由 apply_decisions 删除）"})
                continue
        active.append(c)
    missing = [i for i in ids if i not in by_id]
    if missing:
        warnings.append("有 %d 个候选人 record_id 在简历表里读不到（%s...），已按参与处理"
                        % (len(missing), ",".join(str(m) for m in missing[:3])))
    return active, onboarded


#: --from-table 导出的候选人字段集合：**与 C1 intake_resume.py 产出的 candidates.json
#: 元素完全同构**（契约 v3 §9#7 的验收断言就是「两种模式产出的键集合完全一致」）。
#: 表里独有的 email / 期望薪资列不导出（不在匹配判定范围内，导出会破坏键集合一致性）。
def fetch_candidates_from_table(table: AITable, org: Optional[str] = None,
                                exclude_onboarded: bool = False,
                                warnings: Optional[List[str]] = None
                                ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """契约 v3 §9#7：从简历库表批量导出**存量候选人**，映射成与 candidates.json
    完全相同的 candidates 结构（key 用 c01/c02…，record_id 用表里的真实 id）。

    * 一次 filter 查询取全量（≤100/页由 aitable_io 自动翻页）；`--org` 时在服务端过滤。
    * evidence 从「简历全文」text 列取，按 D4 保留教育/证书段：全文可用 → 用 W-A 的
      同一套分段规则（extract_resume_fields）切；切不出来 → 整段截断进 work_text，
      并在 meta 里标注 evidence_source="full_text_fallback"（后续 enrich_evidence
      还会按关键词开窗补教育/证书段，双保险）。
    * `--exclude-onboarded` 时在查询映射阶段就排除 沟通状态=已入职；不带该参数时
      已入职候选人也会走 check_onboarded 的老插件铁律剔除（进 meta.excluded_onboarded）。
    """
    warns = warnings if warnings is not None else []
    fields = list(table.field_keys("resume") or []) or None
    flt = {"org": org} if org else None
    try:
        recs = table.query_records("resume", filter=flt, fields=fields, all_pages=True)
    except (DwsError, AITableConfigError) as exc:
        raise SystemExit("从简历库表批量查询存量候选人失败：%s\n"
                         "（检查 config.json 的 resume 表映射与 base 可访问性；"
                         "若本次是「先入库再匹配」，请改用 --candidates 指向 "
                         "intake_resume.py 产出的 candidates.json）" % exc)
    # 稳定排序：姓名 → record_id（保证 key 编号跨次运行可复现）
    recs = sorted(recs, key=lambda r: (as_text((r.get("cells") or {}).get("name")).strip(),
                                       str(r.get("record_id"))))
    out: List[Dict[str, Any]] = []
    n_excluded_onboarded = 0
    ev_sources = {"sections": 0, "full_text_fallback": 0, "no_full_text": 0}
    for r in recs:
        cells = r.get("cells") or {}
        cs = clean_ws(as_text(cells.get("comm_status")))
        if exclude_onboarded and cs == COMM_STATUS_ONBOARDED:
            n_excluded_onboarded += 1
            continue
        name = clean_ws(as_text(cells.get("name"))) or None
        att = cells.get("attachment") or []
        fname = ""
        if isinstance(att, list):
            for a in att:
                if isinstance(a, dict):
                    fname = as_text(a.get("filename") or a.get("name")).strip()
                    if fname:
                        break
        cand: Dict[str, Any] = {
            "key": "c%02d" % (len(out) + 1),
            "record_id": r.get("record_id"),
            "file_name": fname or ("%s（在库简历）" % name if name else None),
            "name": name,
            "phone": clean_ws(as_text(cells.get("phone"))) or None,
            "education": clean_ws(as_text(cells.get("education"))) or None,
            "school": clean_ws(as_text(cells.get("school"))) or None,
            "school_rank": clean_ws(as_text(cells.get("school_rank"))) or None,
            "major": clean_ws(as_text(cells.get("major"))) or None,
            "years_experience": None,
            "certificates": [],
            "skills": as_list(cells.get("skills")),
            "expected_position": clean_ws(as_text(cells.get("expected_position"))) or None,
            "expected_location": clean_ws(as_text(cells.get("expected_location"))) or None,
            "org_guess": clean_ws(as_text(cells.get("org"))) or None,
            "org_confidence": "high" if clean_ws(as_text(cells.get("org"))) else "low",
            "category_guess": clean_ws(as_text(cells.get("category"))) or None,
            "parse_status": "ok",
            "dedupe": "existing",                # 存量导出，不是本批 new/overwrite/conflict
            "attachment_status": "uploaded" if att else "missing",
            "years_source": None,                # 表里不存来源；估算值复核以 evidence 原文为准
            "evidence": {"education_text": "", "cert_text": "", "work_text": "", "skill_text": ""},
        }
        y = as_number(cells.get("years_experience"), None)
        if y is not None:
            cand["years_experience"] = int(y) if float(y).is_integer() else y
        certs_text = clean_ws(as_text(cells.get("certificates")))
        if certs_text:
            cand["certificates"] = dedupe_keep_order(
                [clean_ws(x) for x in re.split(r"[、,，;；\n]+", certs_text) if clean_ws(x)])
        # evidence（D4）：表内「简历全文」→ W-A 分段规则；切不出来整段截断 + 标注
        ft = as_text(cells.get("full_text"))
        if ft:
            secs: Optional[Dict[str, Any]] = None
            if extract_resume_fields is not None:
                try:
                    secs = (extract_resume_fields(ft, fname or "") or {}).get("sections") or {}
                except Exception:
                    secs = None
            if secs and (clean_ws(secs.get("education_text")) or clean_ws(secs.get("cert_text"))):
                cand["evidence"] = {
                    "education_text": full(secs.get("education_text")),
                    "cert_text": full(secs.get("cert_text")),
                    "work_text": clip(secs.get("work_text"), WORK_TEXT_LIMIT),
                    "skill_text": clip(secs.get("skill_text"), SKILL_TEXT_LIMIT),
                }
                ev_sources["sections"] += 1
            else:
                cand["evidence"]["work_text"] = clip(ft, WORK_TEXT_LIMIT)
                ev_sources["full_text_fallback"] += 1
            # enrich_evidence 兜底开窗仍可用全文（教育/证书段为空时按关键词补，D4 双保险）
            cand["_full_text_from_table"] = ft
        else:
            ev_sources["no_full_text"] += 1
            warns.append("%s(%s)：简历表里「简历全文」为空 → evidence 无原文可给，"
                         "门槛判定只能靠字段值；建议重新上传该候选人简历原件"
                         % (name or "?", cand["key"]))
        out.append(cand)
    meta = {
        "from_table_resume_records": len(recs),
        "from_table_excluded_onboarded": n_excluded_onboarded,
        "from_table_evidence_sources": ev_sources,
    }
    return out, meta


def make_shards(candidates: Sequence[Dict[str, Any]], max_per_batch: int) -> List[List[Dict[str, Any]]]:
    """按 --max-per-batch 分片（D3）。**只在同分片内做候选人×岗位组合**。

    排序稳定：先按 key（C1 给的顺序本身就是入库顺序，key 是 c01..cNN），保证可复现。
    """
    size = max(1, int(max_per_batch))
    items = list(candidates)
    return [items[i:i + size] for i in range(0, len(items), size)] or []


def shard_meta(shard: Sequence[Dict[str, Any]], jobs: Sequence[Dict[str, Any]],
               idx: int, total_shards: int) -> Dict[str, Any]:
    """单片的规模与 token 估算（供 agent 判断是否超载）。"""
    orgs = {j.get("org") for j in jobs}
    combos = 0
    for c in shard:
        corg = c.get("org_guess")
        combos += len([j for j in jobs if (not corg or j.get("org") == corg)])
    payload = {"candidates": list(shard), "jobs": list(jobs)}
    chars, toks = est_tokens(payload)
    out_chars, out_toks = _estimate_output(shard, jobs, combos)
    return {
        "shard_index": idx,
        "shard_total": total_shards,
        "candidate_count": len(shard),
        "job_count": len(jobs),
        "combo_count": combos,
        "candidate_orgs": sorted([c.get("org_guess") for c in shard if c.get("org_guess")]),
        "job_orgs": sorted([o for o in orgs if o]),
        "input_chars": chars,
        "input_est_tokens": toks,
        "output_est_chars": out_chars,
        "output_est_tokens": out_toks,
        "token_basis": "中文按 %.1f 字符/token 粗估（与前序实验同口径）" % CHARS_PER_TOKEN,
    }


def _estimate_output(shard: Sequence[Dict[str, Any]], jobs: Sequence[Dict[str, Any]],
                     combos: int) -> Tuple[int, int]:
    """稀疏 decisions 的输出量估算：pass 条目详写、reject 条目按 candidate 聚合。

    前序实验的实测通过率约 31/190 ≈ 16%，这里按 25% 保守估（宁可高估不要低估）。
    """
    n_pass = int(math.ceil(combos * 0.25))
    n_rej = max(0, combos - n_pass)
    avg_must = sum(len(j.get("must_skills") or []) for j in jobs) / max(1, len(jobs))
    avg_bonus = sum(len(j.get("bonus_skills") or []) for j in jobs) / max(1, len(jobs))
    # 一条 pass：键名 + gate_detail 四项 + 命中项（按 60% 命中率）+ evidence ≤80 字
    per_pass = 150 + int(avg_must * 0.6 * 12) + int(avg_bonus * 0.6 * 12) + 80
    # reject 是 {"candidate_key","job_keys":[...],"reason"}：每人一条聚合
    per_cand_rej = 60 + n_rej / max(1, len(shard)) * 6 + 20
    chars = int(n_pass * per_pass + len(shard) * per_cand_rej)
    return chars, int(math.ceil(chars / CHARS_PER_TOKEN))


# --------------------------------------------------------------------------- #
# W-I 性能优化（W-J 移植）：分片岗位裁剪（L3 组织预筛 + L4 冗余字段裁剪）
#
# 背景：合并版 digest.json 与每个分片 digest_batch_NN.json 过去都携带**全部**在招岗位的
# **全部**字段。实测单份简历场景：19 岗 × 全字段 = 16315 字符，其中只有 16 个组合是同组织
# 可判定的；agent 还同时 Read 了 digest.json 与 digest_batch_01.json 两份近似重复的文件，
# 一次就把 ~30k token 灌进上下文。
#
# 设计约束（不可破坏）：
#   * **合并版 digest.json 保持全量不动** —— verify_decisions.py / apply_decisions.py
#     一律吃 digest.json，下游脚本的输入契约零变化，零回归风险。
#   * 裁剪只作用于**分片文件**（agent 唯一需要读进上下文的东西）。
#   * 保留字段 = verify_decisions.py 实读字段（key/org/status/must_skills/bonus_skills/
#     weights/job_id/job_name）+ Turn 2 判定必需（hard_gates/requirements_text/
#     years_req_min/cert_is_preferred_not_required）+ 清单输出用（job_name/department）。
# --------------------------------------------------------------------------- #

#: L4：分片里裁掉的岗位字段（Turn 2 语义判定读不到它们，apply/verify 走 digest.json）
SLIM_DROP_JOB_FIELDS = ("record_id", "responsibilities_text", "hard_gates_raw",
                        "gates_source", "skills_source")


def slim_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """裁掉判定用不到的岗位字段，保持键序不变。"""
    return {k: v for k, v in job.items() if k not in SLIM_DROP_JOB_FIELDS}


def select_shard_jobs(shard: Sequence[Dict[str, Any]],
                      jobs: Sequence[Dict[str, Any]],
                      org_prefilter: bool = True) -> Tuple[List[Dict[str, Any]], str]:
    """L3：分片只带**本片候选人同组织**的在招岗位（组合本来就只在同组织内发生）。

    安全阀（务必保留，否则会漏判）：
      * 本片任一候选人 `org_confidence == "low"` → **保留全部岗位**。因为 agent 可能在
        Turn 2 依 evidence 把组织改判到另一个中心（回填 candidate_overrides.org），
        改判后它仍需看到另一侧的岗位；预筛掉就等于把改判后的可判岗位删了。
      * 本片候选人全都没有 `org_guess` → 保留全部岗位（口径同 verify：组织缺失退化为全部在招岗位）。
      * 预筛后一个岗位都不剩 → 保留全部岗位，让 agent 看得见"为什么无可判组合"，
        而不是拿到空 jobs 数组无从交代。

    返回 (要落盘的岗位列表, note)；note ∈
      {"disabled","prefiltered","low_confidence_org","no_org_guess","no_same_org_jobs"}
    """
    if not org_prefilter:
        return list(jobs), "disabled"
    if any(str(c.get("org_confidence") or "").strip().lower() == "low" for c in shard):
        return list(jobs), "low_confidence_org"
    orgs = {c.get("org_guess") for c in shard if c.get("org_guess")}
    if not orgs:
        return list(jobs), "no_org_guess"
    kept = [j for j in jobs if j.get("org") in orgs]
    if not kept:
        return list(jobs), "no_same_org_jobs"
    return kept, "prefiltered"


#: L4-c 的 stdout 预算（字节）。实测 qodercli 对 Bash tool_result 有**硬上限**：
#: 30,002 字节（≈29.3 KB）处**静默切断、不留任何截断标记**（W-I 实测：一次
#: intake+auto-match 的 stdout 被切在第 15 个岗位的 requirements_text 中间，
#: 既没有 `== 判定输入结束 ==` 也没有 `SHARD:` 行，agent 只能自己去 Read 文件补救）。
#: 所以默认**不**把判定输入打进 stdout；本函数与 --emit-stdout 开关**均不移植进 CLI**
#: （W-I 实测 O4-c 负收益：agent 拿到半截 JSON 反而去 Read digest.json / Grep .py 源码自证，
#: 多烧 3~4 回合与 2 万多 output token）。保留本函数仅为 report_and_emit 的冻结签名
#: `emit_stdout=False` 参数有定义可依，**任何入口都不会把它打开**。
EMIT_BUDGET_BYTES = 20000


def emit_shard_stdout(shard_doc: Dict[str, Any]) -> int:
    """L4-c（**默认关闭、CLI 不可达**）：把单片判定输入打到 stdout。

    返回打出的字符数；**超出 EMIT_BUDGET_BYTES 就返回 -1 并且什么都不打**。
    W-I 实测此路负收益，故 W-J 移植时**不接任何 CLI 开关**，仅保留函数体以满足
    report_and_emit 的冻结签名（emit_stdout 恒为 False，本函数永不被调用）。
    """
    payload = {
        "batch_id": shard_doc.get("batch_id"),
        "shard": shard_doc.get("shard"),
        "candidates": shard_doc.get("candidates"),
        "jobs": shard_doc.get("jobs"),
    }
    s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    nb = len(s.encode("utf-8"))
    if nb > EMIT_BUDGET_BYTES:
        print("判定输入 %d 字节 > stdout 预算 %d 字节（qodercli Bash 输出 ~30 KB 处会静默截断）"
              "→ 未打 stdout，请按上面的 SHARD: 路径 Read 分片文件。"
              % (nb, EMIT_BUDGET_BYTES), flush=True)
        return -1
    print("== 判定输入（本片全部信息，无需再 Read 分片文件；打分口径见 HOTPATH 卡片）==",
          flush=True)
    print(s, flush=True)
    print("== 判定输入结束 ==", flush=True)
    return len(s)


def build_digest(config_path: str, candidates_path: Optional[str], out_dir: str,
                 max_per_batch: int = DEFAULT_MAX_PER_BATCH,
                 batch_id: Optional[str] = None,
                 from_table: bool = False, org: Optional[str] = None,
                 exclude_onboarded: bool = False,
                 org_prefilter: bool = True, slim_jobs: bool = True) -> Dict[str, Any]:
    t0 = time.time()
    cfg = Path(config_path).expanduser()
    cand_path = Path(candidates_path).expanduser() if candidates_path else None
    outdir = Path(out_dir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []
    errors: List[str] = []          # 契约 v3 §9#2：不可判定的原因（digest.ok=false 时非空）

    try:
        table = AITable(str(cfg))
    except Exception as exc:                          # config 坏 → 失败也要落产物（D7）
        errors.append("打不开 config.json（%s: %s）→ 无法查岗位/候选人，本批不可判定"
                      % (type(exc).__name__, exc))
        digest = {"batch_id": batch_id or ("match-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S")),
                  "generated_at": _now(), "ok": False, "errors": errors,
                  "scoring_rules": SCORING_RULES, "candidates": [], "jobs": [],
                  "meta": {"config_path": str(cfg), "python": "%d.%d.%d" % sys.version_info[:3],
                           "warnings": warnings}}
        dpath = outdir / "digest.json"
        with open(str(dpath), "w", encoding="utf-8") as fh:
            json.dump(digest, fh, ensure_ascii=False, indent=1)
        return {"digest_path": os.path.abspath(str(dpath)), "digest": digest, "table": None,
                "shard_paths": [], "shard_docs": []}

    ft_meta: Dict[str, Any] = {}
    if from_table:
        # 契约 v3 §9#7：存量候选人反向匹配（指定岗位反向匹配 / 重建全部匹配的数据源）
        try:
            raw_cands, ft_meta = fetch_candidates_from_table(table, org=org,
                                                             exclude_onboarded=exclude_onboarded,
                                                             warnings=warnings)
        except SystemExit as exc:
            errors.append(str(exc))
            raw_cands, ft_meta = [], {}
        cand_doc = {}
        if not raw_cands and not errors:
            errors.append("简历库表里没有可导出的存量候选人%s → 本批不可判定；"
                          "请先跑简历入库，或检查 --org 过滤值是否与表内「组织」选项一致"
                          % ("（--org=%s 过滤后为空）" % org if org else ""))
    else:
        try:
            raw_cands, cand_doc = load_candidates_file(cand_path)
        except SystemExit as exc:
            errors.append(str(exc))
            raw_cands, cand_doc = [], {}
        if not raw_cands and not errors:
            errors.append("candidates.json 里 candidates 数组是空的，没东西可匹配")

    normalized: List[Dict[str, Any]] = []
    for i, c in enumerate(raw_cands):
        if not isinstance(c, dict):
            warnings.append("candidates[%d] 不是对象，已跳过" % i)
            continue
        normalized.append(normalize_candidate(c, i, warnings))
    # key 去重（C1 没给 key 时按序号生成，理论上不会撞）
    seen: Dict[str, int] = {}
    for c in normalized:
        k = c["key"]
        if k in seen:
            seen[k] += 1
            newk = "%s_%d" % (k, seen[k])
            warnings.append("候选人 key %r 重复，已改名为 %r" % (k, newk))
            c["key"] = newk
        else:
            seen[k] = 0

    active, onboarded = ([], []) if errors else check_onboarded(table, normalized, warnings)

    # D4 兜底：上游分段为空的 evidence，用简历表里的「简历全文」按关键词开窗补上
    n_enriched = 0
    for c in active:
        ft = c.pop("_full_text_from_table", "") or full(c.get("full_text"))
        notes = enrich_evidence(c, ft)
        if notes:
            n_enriched += 1
            warnings.extend(notes)
    empty_edu = [c["key"] for c in active
                 if not (c.get("evidence", {}).get("education_text") or "").strip()]
    if empty_edu:
        warnings.append("有 %d 个候选人连兜底后 evidence.education_text 仍为空（%s）→ "
                        "学历门槛只能靠 education 字段判，agent 判 fail 前请特别小心（D4 误杀坑）"
                        % (len(empty_edu), ",".join(empty_edu[:8])))

    jobs: List[Dict[str, Any]] = []
    if not errors:
        try:
            jobs = fetch_open_jobs(table, warnings)
        except SystemExit as exc:
            errors.append(str(exc))
    if not jobs and not errors:
        errors.append("表里查不到任何「状态=招聘中」的岗位 → agent 无从判定；"
                      "请先跑 job-intake 把岗位入库，或检查 job 表「状态」字段取值")
    if active == [] and normalized and not errors:
        errors.append("候选人全部被剔除（如 沟通状态=已入职）→ 本批无可判定候选人")

    bid = batch_id or cand_doc.get("batch_id") or (
        "match-table-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S") if from_table
        else "match-%s" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

    # 组合数（只在同组织内）
    combos = 0
    unmatched: List[str] = []
    for c in active:
        corg = c.get("org_guess")
        n = len([j for j in jobs if (not corg or j.get("org") == corg)])
        combos += n
        if n == 0:
            unmatched.append("%s(%s) 组织=%s" % (c.get("name"), c["key"], corg))
    if unmatched:
        warnings.append("有 %d 个候选人在表里找不到同组织的在招岗位，本轮不产生任何判定：%s"
                        % (len(unmatched), "; ".join(unmatched[:8])))

    shards = make_shards(active, max_per_batch)
    in_chars, in_toks = est_tokens({"candidates": active, "jobs": jobs})
    meta = {
        "config_path": str(cfg.resolve()),
        "candidates_path": (str(cand_path.resolve()) if cand_path else None),
        "source_mode": "from_table" if from_table else "candidates_file",
        "base_name": table.base_name,
        "base_id": table.base_id,
        "max_per_batch": max(1, int(max_per_batch)),
        "shard_count": len(shards),
        "candidate_count_total": len(normalized),
        "candidate_count_active": len(active),
        "excluded_onboarded": onboarded,
        "job_count": len(jobs),
        "combo_count": combos,
        "input_chars": in_chars,
        "input_est_tokens": in_toks,
        "token_basis": "中文按 %.1f 字符/token 粗估（与前序实验同口径）" % CHARS_PER_TOKEN,
        "shards": [],
        "evidence_policy": "D4：education_text / cert_text 完整不截断；"
                           "work_text ≤%d 字、skill_text ≤%d 字（超出截断）"
                           % (WORK_TEXT_LIMIT, SKILL_TEXT_LIMIT),
        "requirements_text_limit": REQUIREMENTS_LIMIT,
        "needs_review_years": sorted([c["key"] for c in active if "years" in c.get("needs_review", [])]),
        "evidence_enriched_candidates": n_enriched,
        "evidence_empty_education": empty_edu,
        "dws_calls": table.dws_calls,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "warnings": warnings,
        "errors": errors,
    }
    if from_table:
        meta["from_table"] = dict(ft_meta, org_filter=org,
                                  exclude_onboarded=bool(exclude_onboarded))
        # 契约 v3 §9#7：evidence 来源标注（整段截断的计数在 from_table_evidence_sources 里）
        if (ft_meta.get("from_table_evidence_sources") or {}).get("full_text_fallback"):
            meta["evidence_source"] = "full_text_fallback"

    # 先把耗时/调用数补进 meta，再一次性落盘（避免写两遍）
    meta["elapsed_ms"] = int((time.time() - t0) * 1000)
    meta["dws_calls"] = table.dws_calls

    ok = not errors
    digest = {
        "batch_id": bid,
        "generated_at": _now(),
        "ok": ok,                        # 契约 v3 §9#2：D7 产物凭证统一口径
        "errors": errors,
        "scoring_rules": SCORING_RULES,
        "candidates": active,
        "jobs": jobs,
        "meta": meta,
    }

    # 分片文件：candidates 只放本片；jobs 默认按 L3 组织预筛 + L4 字段裁剪（W-I 优化，W-J 移植），
    # 把 agent 要读进上下文的字符数压到最小。
    #
    # ⚠️ **合并版 digest.json 的构造完全不动**：它的 meta.shards 仍按**全量 jobs** 计算、
    # 不带任何裁剪遥测键，因此 digest.json 在开关开/关两种情况下**逐字节不变**、且与移植前
    # 逐字节相同——下游 verify/apply 吃 digest.json，输入契约零变化、零回归（任务 3.1 md5 证明）。
    # 裁剪与遥测只作用于**分片文件**（agent 唯一读进上下文的东西）。
    # 两个开关都关时，分片文件也逐字节回到移植前形态（optimize=False → 不写遥测键、jobs 全量）。
    optimize = bool(org_prefilter or slim_jobs)
    shard_paths: List[str] = []
    shard_docs: List[Dict[str, Any]] = []
    for i, sh in enumerate(shards, 1):
        # digest.json 的 meta.shards：保持移植前口径（全量 jobs、无遥测键），保证 digest 逐字节稳定
        sm_digest = shard_meta(sh, jobs, i, len(shards))
        meta["shards"].append(sm_digest)
        # 分片文件的 shard meta：按预筛/裁剪后的 shard_jobs 计算；开优化时才带遥测键
        shard_jobs, pf_note = select_shard_jobs(sh, jobs, org_prefilter)
        if slim_jobs:
            shard_jobs = [slim_job(j) for j in shard_jobs]
        sm_shard = shard_meta(sh, shard_jobs, i, len(shards))
        if optimize:
            sm_shard["job_count_all"] = len(jobs)
            sm_shard["jobs_org_prefiltered"] = (pf_note == "prefiltered")
            sm_shard["jobs_prefilter_note"] = pf_note
            sm_shard["jobs_slimmed"] = bool(slim_jobs)
            sm_shard["dropped_job_fields"] = list(SLIM_DROP_JOB_FIELDS) if slim_jobs else []
        shard_doc = {
            "batch_id": bid,
            "generated_at": digest["generated_at"],
            "ok": ok,
            "errors": errors,
            "shard": sm_shard,
            "scoring_rules": SCORING_RULES,
            "candidates": list(sh),
            "jobs": shard_jobs,
            "meta": {k: v for k, v in meta.items() if k != "shards"},
        }
        p = outdir / ("digest_batch_%02d.json" % i)
        with open(str(p), "w", encoding="utf-8") as fh:
            json.dump(shard_doc, fh, ensure_ascii=False, indent=1)
        # 同上：abspath 而非 resolve()，避开 macOS /tmp → /private/tmp 符号链接
        shard_paths.append(os.path.abspath(str(p)))
        shard_docs.append(shard_doc)

    dpath = outdir / "digest.json"
    with open(str(dpath), "w", encoding="utf-8") as fh:
        json.dump(digest, fh, ensure_ascii=False, indent=1)

    # 注意：这里用 abspath 而不是 resolve()——macOS 上 /tmp 是 /private/tmp 的符号链接，
    # resolve() 会把 ARTIFACT 行打成 /private/tmp/... ，跟用户传进来的 --out-dir 长得不一样。
    return {"digest_path": os.path.abspath(str(dpath)), "digest": digest, "table": table,
            "shard_paths": shard_paths, "shard_docs": shard_docs}


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


def report_and_emit(res: Dict[str, Any], emit_stdout: bool = False,
                    emit_always: bool = False) -> int:
    """打印 digest 摘要 + ARTIFACT 行 + SHARD: 行（agent 下一步唯一该 Read 的东西）。

    独立成函数是为了让 intake_resume.py --auto-match 能在同一进程里复用完全相同的输出口径
    （O2/L2），两条入口的 stdout 一致，agent 学一次就够。

    `emit_stdout` / `emit_always` 是冻结签名参数，但**恒为 False、无任何 CLI 开关能打开**：
    W-I 实测 O4-c（把判定输入打进 stdout）负收益——qodercli 在 ~30 KB 处静默切断 Bash 输出，
    半截 JSON 会让 agent 转而去 Read digest.json、甚至 Read/Grep .py 源码自证，多烧 3~4 回合
    与 2 万多 output token。所以判定输入**不打 stdout**，一律按 SHARD: 路径 Read 分片文件。
    返回 0 = digest.ok；1 = 不可判定（errors 里有业务话原因）。
    """
    d = res["digest"]
    m = d["meta"]
    print("digest: ok=%s 候选人 %d（原始 %d，已入职剔除 %d）× 在招岗位 %d = %d 个组合"
          % (d.get("ok"), m["candidate_count_active"], m["candidate_count_total"],
             len(m["excluded_onboarded"]), m["job_count"], m["combo_count"]))
    if m.get("source_mode") == "from_table":
        ft = m.get("from_table") or {}
        print("来源: 简历库表存量导出（表内 %s 条，--org=%s，--exclude-onboarded=%s，"
              "查询阶段排除已入职 %s；evidence 来源 %s）"
              % (ft.get("from_table_resume_records"), ft.get("org_filter") or "不过滤",
                 bool(ft.get("exclude_onboarded")), ft.get("from_table_excluded_onboarded"),
                 json.dumps(ft.get("from_table_evidence_sources"), ensure_ascii=False)))
    print("分片: %d 片（--max-per-batch=%d）；输入 %d chars ≈ %d tokens"
          % (m["shard_count"], m["max_per_batch"], m["input_chars"], m["input_est_tokens"]))
    # 每片摘要 + 裁剪遥测从 shard_docs 读（其 shard meta 按预筛/裁剪后的 shard_jobs 计算，
    # 因此「输入 chars」反映 agent 真正要读进上下文的体量）；两开关都关时与全量一致、遥测=disabled。
    shard_docs = res.get("shard_docs") or []
    paths = res.get("shard_paths") or []
    digest_shards = m.get("shards") or []
    for idx, doc in enumerate(shard_docs):
        s = doc.get("shard") or (digest_shards[idx] if idx < len(digest_shards) else {})
        print("  片 %02d: %d 人 × %d 岗 = %d 组合 | 输入 %d chars ≈ %d tok | "
              "稀疏输出估算 ≈ %d chars ≈ %d tok | 岗位预筛=%s 字段裁剪=%s（全量 %d 岗）"
              % (s.get("shard_index", idx + 1), s.get("candidate_count", 0),
                 s.get("job_count", 0), s.get("combo_count", 0),
                 s.get("input_chars", 0), s.get("input_est_tokens", 0),
                 s.get("output_est_chars", 0), s.get("output_est_tokens", 0),
                 s.get("jobs_prefilter_note", "disabled"), s.get("jobs_slimmed", False),
                 s.get("job_count_all", m.get("job_count", 0))))
    if m["needs_review_years"]:
        print("D13 needs_review=[years]（工作年限是估算的，agent 必须复核）: %s"
              % ",".join(m["needs_review_years"]))
    if m["excluded_onboarded"]:
        print("已入职剔除（老插件铁律）: %s"
              % ",".join(str(o["name"]) for o in m["excluded_onboarded"]))
    for e in d.get("errors") or []:
        print("ERROR: %s" % e)
    for w in m["warnings"]:
        print("WARN: %s" % w)
    print("dws_calls=%d elapsed_ms=%d python=%s"
          % (m["dws_calls"], m["elapsed_ms"], m["python"]))
    print("ARTIFACT:%s" % res["digest_path"])

    # SHARD: 行永远打印 —— 这是 agent 下一步**唯一**该 Read 的东西（判定输入不打 stdout）。
    if len(paths) == 1:
        print("下一步：Read 这个分片文件拿判定输入（**不要 Read digest.json** —— 它是给 "
              "verify/apply 用的全量版，内容与分片重复且更大；也**不要 Read/Grep 任何 .py 源码**）：",
              flush=True)
    elif len(paths) > 1:
        print("多片（%d 片）：按「一片一个回合」各自 Read 下面的分片文件"
              "（**不要 Read digest.json**，也**不要 Read/Grep 任何 .py 源码**）：" % len(paths),
              flush=True)
    for p in paths:
        print("SHARD:%s" % p, flush=True)
    # emit_stdout 恒 False（无 CLI 开关能打开；O4-c 实测负收益，见函数 docstring）
    if emit_stdout and shard_docs and (len(shard_docs) == 1 or emit_always):
        total = 0
        for doc in shard_docs:
            n = emit_shard_stdout(doc)
            if n > 0:
                total += n
        if total:
            print("emit_stdout_chars=%d（stdout 已含本片全部判定输入，可省掉上面那次 Read）"
                  % total, flush=True)
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
