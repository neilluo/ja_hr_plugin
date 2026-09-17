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

设计纪律与 intake_resume.py 同源（dws 调用次数第一、附件随 create 一次写入不做事后
update、失败可见 D6、不硬造 D11、防静默早退 D7、python 3.9/3.14 双兼容 D18、
零第三方 pip 依赖、sys.path 用 parents[3] 定位插件根、零硬编码 ID D8）。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# sys.path：脚本在 skills/<name>/scripts/ 下，插件根 = parents[3]
# --------------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PLUGIN_ROOT / "shared"
_VENDOR_DIR = _SHARED_DIR / "vendor"
for _p in (str(_SHARED_DIR), str(_VENDOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aitable.client import DwsCallCounter, DwsClient, DwsError, now_iso  # noqa: E402
from aitable.table import AITable                      # noqa: E402
from aitable.values import values_equal                # noqa: E402
from extract_fields import extract_job_fields           # noqa: E402
from extract_text import detect_scanned, extract_text   # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
UPLOAD_CONCURRENCY = 5
MUST_WEIGHT_DEFAULT = 0.7        # 老插件 job-intake/SKILL.md:26：JD 未给比例时 70%/30%
BONUS_WEIGHT_DEFAULT = 0.3
STATUS_DEFAULT = "招聘中"        # 老插件 job-intake/SKILL.md:25
LOCATION_FALLBACK = "不限"
OLD_TURNS_PER_FILE = 25          # 契约 §2：老插件每份 20~40 个工具回合，取中位数
NEW_TURNS = 1
SETTLE_WAITS = (1.5, 3.0, 4.5)
RICHTEXT_MAX = 20000             # richText/text 写入上限（W-B 实测 49,956 字无损）
HARD_GATES_TEXT_MAX = 1500

# 组织分类关键词（老插件口径：system-config.md:28 / job-intake/SKILL.md:24）
ORG_MFG_KEYWORDS = ("制造基地", "厂务", "设备", "EHS", "暖通", "电气", "工艺",
                    "单晶", "硅片", "组件", "电池", "生产", "制造部")
ORG_FUNC_KEYWORDS = ("财务", "财经", "行政", "人力", "人事", "数据信息", "成本会计",
                     "会计", "审计", "法务")
#: 客户在文件名里**显式声明**的组织（最强证据：`岗位说明书-制造中心-曲靖制造基地-…`）
ORG_EXPLICIT = ("制造中心", "职能中心")

# 工作地点候选城市（与 config.json 的 work_location 选项对齐 + 常见补充）
CITY_HINTS = ("曲靖", "昆明", "云南", "北京", "上海", "深圳", "杭州", "成都", "广州",
              "南京", "武汉", "西安", "兰州", "银川", "西宁", "贵阳", "郑州", "合肥",
              "济南", "青岛", "苏州", "无锡", "常州", "盐城", "阜宁", "中卫", "大理",
              "牟定", "遵义", "徐州", "绵阳", "荆州", "焦作", "商丘", "阜阳", "毕节",
              "平凉", "全国", "不限")

PARSE_FAIL_REASON = {
    "no_text_layer": "扫描件/图片无文字层，无法解析；请提供 Word 或 PDF 文字版岗位说明书"
                     "（本期不做 OCR，不硬造字段）",
    "garbled": "文本层疑似乱码/编码错位，无法可靠解析；请提供文字版 JD 或转人工核对原件",
    "encrypted": "文件加密或无读取权限，无法解析；请提供未加密的文字版 JD",
    "unsupported": "不支持的文件格式，无法解析；请提供 PDF/Word(docx/doc) 文字版 JD",
    "error": "文件解析失败；请确认文件完整后重新提供",
}

#: jobs[] 元素必备字段（契约 §3.3 digest.json 的 jobs 数组元素）
JOB_FIELDS_CONTRACT = ("key", "record_id", "job_id", "job_name", "department", "org",
                       "status", "hard_gates", "must_skills", "bonus_skills", "weights")
HARD_GATE_KEYS = ("education", "major", "years", "certificates")
#: Turn 2 必须归一化、Turn 3 才写库的语义字段
LLM_NORMALIZE_FIELDS = ("hard_gates", "must_skills", "bonus_skills")

# --------------------------------------------------------------------------- #
# 任务二（W-J，根因来自 W-I 实测）：JD 技能条目**粒度护栏**配置（只增 warning 不拦写）
#
# W-I 实测根因：JD 抽取把 must_skills 切成了**句子碎片**——j05 里有「熟悉光伏行业生产」
# 「质量」「安全相关标准」，j14 里有「能规范记录」「整理工艺数据」。这种碎片几乎任何简历
# 都能「语义等价」命中 → 既导致匹配放水（崔银亮 j11 命中数在 3/12~10/12 间摆动、10/12 那几次
# 把「熟练使用Office办公软件及工艺分析工具」「沟通协调能力强」「责任心强」全算命中，而简历
# work_text 只有 391 字符全是 ALD 镀膜工艺、这些项零证据），也让判定回合反复斟酌、墙钟在
# 34~435 秒间摆动。**这是 job-intake 的数据质量缺陷，不是 match-verify 的问题。**
#
# 粒度标准（写进 SKILL.md / HOTPATH.md 的 Turn 2 要求）：must_skills[] / bonus_skills[] 每一项
# 必须是**原子的、可独立验证的技能/能力名词短语**，不是句子碎片、不是单个泛化词。
#   ❌ 坏：「熟悉光伏行业生产」「质量」「安全相关标准」「能规范记录」「整理工艺数据」
#   ✅ 好：「光伏生产管理经验」「质量管理体系」「安全生产标准」「工艺数据记录规范」
# 下面三个阈值/黑名单**可配置**（改这里即可；check_skill_granularity 也接受覆盖参数）。
# --------------------------------------------------------------------------- #
#: 单条技能 ≤ 该字数 → 判「单个泛化词」告警（如「质量」=2 字）
SKILL_ITEM_TOO_SHORT_MAXLEN = 2
#: 单条技能 ≥ 该字数 → 判「整句碎片」告警（如「熟练使用Office办公软件及工艺分析工具」）
SKILL_ITEM_TOO_LONG_MINLEN = 30
#: 泛化软技能黑名单（子串匹配，大小写不敏感）——放水的重灾区；JD 明确列为必备时 agent 可保留，
#: 护栏只告警不拦写。**可配置**：增删这里的词即可。
SOFT_SKILL_BLACKLIST = (
    "沟通协调", "沟通能力", "沟通表达",
    "责任心", "责任感",
    "创新思维", "创新能力", "创新精神",
    "团队合作", "团队协作", "团队精神",
    "熟练使用office", "office办公软件", "办公软件", "熟练使用办公",
    "吃苦耐劳", "抗压能力", "抗压性",
    "学习能力", "执行力", "逻辑思维",
    "行业对标", "人才培养",
)


# --------------------------------------------------------------------------- #
# 小工具（与 intake_resume.py 同源；两个脚本各自自包含，不新增 shared 文件）
# --------------------------------------------------------------------------- #
def _new_batch_id() -> str:
    return "%s-%04x" % (time.strftime("%Y%m%d-%H%M%S"), random.getrandbits(16))


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _clean(s: Any) -> Optional[str]:
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        return s or None
    return s


def _count_hits(hay: str, keywords: Sequence[str]) -> int:
    if not hay:
        return 0
    low = hay.lower()
    return sum(low.count(k.lower()) for k in keywords if k)


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    t = str(text)
    return t[:limit] + "…" if (limit and len(t) > limit) else t


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def guess_org(file_name: str, department: Optional[str], job_name: Optional[str],
              text: str) -> Tuple[Optional[str], str, Dict[str, Any]]:
    """岗位「组织分类」预判。返回 (org, confidence, 判据明细)。

    优先级（老插件口径 + 一条实测补强）：
      ① **文件名/正文里显式写了组织名**（客户 19 份 JD 全部命名为
         `岗位说明书-制造中心-曲靖制造基地-…`）→ 这是客户自己的标注，最强证据；
      ② 部门 + 岗位名关键词（厂务/设备/EHS/制造基地 → 制造中心；
         财务/财经/行政/人力/数据信息/成本会计 → 职能中心）；
      ③ 正文关键词兜底。

    ①②**一致** → high；①有而②反对 → 仍取①（显式声明优先），但 confidence="low"
    并写 warning 交 Turn 2 复核；②③都判不了 → (None, "low")，不写库。

    ⚠️ 与简历侧的差别（刻意的）：简历侧低置信度**不写库**（组织留空由 Turn 2 定夺）；
    岗位侧即使 low 也会把①的显式声明写进去，因为「组织分类为空 = 该岗位匹配不到
    任何简历」（老插件 job-intake/SKILL.md:24），留空是硬功能故障，比猜错更糟。
    """
    info: Dict[str, Any] = {}
    blob = "%s %s" % (file_name or "", (text or "")[:1500])
    explicit = [o for o in ORG_EXPLICIT if o in blob]
    info["explicit"] = explicit
    if len(explicit) == 1:
        declared = explicit[0]
        dept_hay = "%s %s" % (department or "", job_name or "")
        m = _count_hits(dept_hay, ORG_MFG_KEYWORDS)
        f = _count_hits(dept_hay, ORG_FUNC_KEYWORDS)
        info.update({"dept_mfg": m, "dept_func": f})
        kw = "制造中心" if (m and not f) else ("职能中心" if (f and not m) else None)
        info["dept_keyword"] = kw
        if kw is None or kw == declared:
            return declared, "high", info
        return declared, "low", info          # 显式声明 vs 部门关键词打架 → 交 Turn 2 复核
    if len(explicit) > 1:
        info["note"] = "文件名/正文同时出现 %s，无法确定" % explicit
        return None, "low", info

    dept_hay = "%s %s %s" % (department or "", job_name or "", file_name or "")
    m = _count_hits(dept_hay, ORG_MFG_KEYWORDS)
    f = _count_hits(dept_hay, ORG_FUNC_KEYWORDS)
    info.update({"dept_mfg": m, "dept_func": f})
    if m and not f:
        return "制造中心", "high", info
    if f and not m:
        return "职能中心", "high", info
    hay2 = "%s\n%s" % (dept_hay, (text or "")[:4000])
    m2, f2 = _count_hits(hay2, ORG_MFG_KEYWORDS), _count_hits(hay2, ORG_FUNC_KEYWORDS)
    info.update({"text_mfg": m2, "text_func": f2})
    if m2 and not f2:
        return "制造中心", "high", info
    if f2 and not m2:
        return "职能中心", "high", info
    if m2 or f2:
        return ("制造中心" if m2 > f2 else ("职能中心" if f2 > m2 else None)), "low", info
    return None, "low", info


def guess_locations(file_name: str, text: str, known: Sequence[str]) -> List[str]:
    """工作地点（multipleSelect）。文件名优先（客户命名含城市），正文兜底。"""
    out: List[str] = []
    pool = list(dict.fromkeys(list(known or []) + list(CITY_HINTS)))
    for hay in (file_name or "", (text or "")[:2500]):
        for c in pool:
            if c and c in hay and c not in out:
                out.append(c)
        if out:
            break
    # 「曲靖制造基地/曲靖基地」只应产出一个「曲靖」；「不限」与其它城市互斥
    if LOCATION_FALLBACK in out and len(out) > 1:
        out.remove(LOCATION_FALLBACK)
    return out[:6]


def compose_hard_gates(hg: Any) -> Optional[str]:
    """把 §3.3 的 hard_gates 四项对象拼成表里的 text 值（岗位JD表「硬性门槛」是 text）。"""
    if hg is None:
        return None
    if isinstance(hg, str):
        return _clean(hg)
    if isinstance(hg, dict):
        labels = {"education": "学历", "major": "专业", "years": "经验年限",
                  "certificates": "证书"}
        bits = []
        for k in HARD_GATE_KEYS:
            v = _clean(hg.get(k))
            if v:
                bits.append("%s：%s" % (labels.get(k, k), v))
        for k, v in hg.items():                      # LLM 可能多给项（如年龄），一并保留
            if k in HARD_GATE_KEYS:
                continue
            v = _clean(v)
            if v:
                bits.append("%s：%s" % (k, v))
        s = "；".join(bits)
        return _clean(s[:HARD_GATES_TEXT_MAX])
    if isinstance(hg, (list, tuple)):
        return _clean("；".join(str(x) for x in hg if x)[:HARD_GATES_TEXT_MAX])
    return None


def as_text_list(v: Any) -> Optional[str]:
    """必备技能/加分项在表里是 text（打分母）：list → 「、」连接，str 原样。"""
    if v is None:
        return None
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return _clean("、".join(items))
    return _clean(str(v))


# --------------------------------------------------------------------------- #
# 任务二（W-J）：JD 技能条目粒度护栏（只增 warning 不拦写、不改任何函数签名）
# --------------------------------------------------------------------------- #
def _as_skill_items(raw: Any) -> List[Any]:
    """把 jobs_final.json 里的 must_skills/bonus_skills 取成**逐条**列表（list 原样，
    str 按「、，,;；\n」切）。粒度护栏要逐条查，不能用 as_text_list 连接后的串。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [x for x in raw]
    if isinstance(raw, str):
        return [x for x in re.split(r"[、,，;；\n]+", raw)]
    return [raw]


def _norm_skill_item(s: Any) -> str:
    """技能条目粒度比对用归一化：去空白/全角空格/首尾标点、转小写（与 verify 的 norm_item 同口径）。"""
    if s is None:
        return ""
    if isinstance(s, dict):
        s = s.get("name") or s.get("text") or s.get("value") or ""
    s = re.sub(r"[\s\u3000]+", "", str(s))
    return s.strip("，,、;；.。:：").lower()


def check_skill_granularity(jobs_skills: Sequence[Tuple[str, Any, Any]],
                            warnings: List[str],
                            blacklist: Sequence[str] = SOFT_SKILL_BLACKLIST,
                            short_maxlen: int = SKILL_ITEM_TOO_SHORT_MAXLEN,
                            long_minlen: int = SKILL_ITEM_TOO_LONG_MINLEN) -> int:
    """任务二：逐条检查 must_skills/bonus_skills 的粒度，命中可疑形态 → 记 warning（不拦写）。

    入参 jobs_skills = [(岗位label, must_skills_raw, bonus_skills_raw), ...]。返回触发的 warning 条数。
    命中以下任一即告警（带**具体岗位与条目**，方便 agent 回 Turn 2 重切）：
      * 归一化后长度 ≤ short_maxlen 字（单个泛化词，如「质量」）
      * 归一化后长度 ≥ long_minlen 字（整句碎片，如「熟练使用Office办公软件及工艺分析工具」）
      * 命中泛化软技能黑名单（沟通协调/责任心强/创新思维/团队合作/熟练使用Office…，可配置）
      * 同一岗位同一字段内，某条目是另一条目的**子串**（碎片化特征，如「光伏」⊂「光伏行业生产」）
    """
    n = 0
    bl = tuple(str(b).lower() for b in (blacklist or ()) if str(b).strip())
    for label, must_raw, bonus_raw in jobs_skills:
        for field_name, raw in (("must_skills", must_raw), ("bonus_skills", bonus_raw)):
            items = [x for x in _as_skill_items(raw) if str(x).strip()]
            norms = [_norm_skill_item(x) for x in items]
            for it, nm in zip(items, norms):
                if not nm:
                    continue
                reasons = []
                if len(nm) <= short_maxlen:
                    reasons.append("过短（%d 字 ≤%d，疑似单个泛化词）" % (len(nm), short_maxlen))
                if len(nm) >= long_minlen:
                    reasons.append("过长（%d 字 ≥%d，疑似整句碎片）" % (len(nm), long_minlen))
                hit_bl = [b for b in bl if b in nm]
                if hit_bl:
                    reasons.append("泛化软技能（命中黑名单：%s）" % "/".join(hit_bl[:2]))
                if reasons:
                    warnings.append("JD技能粒度护栏《%s》%s 条目「%s」：%s → Turn 2 归一化请改成"
                                    "原子、可独立验证的技能名词短语（正反例见 SKILL.md / HOTPATH.md）"
                                    % (label, field_name, str(it).strip(), "；".join(reasons)))
                    n += 1
            # 互为子串（碎片化特征）：同字段内两两比，每个碎片条目只报一次
            reported = set()
            for a in range(len(norms)):
                if a in reported or not norms[a]:
                    continue
                for b in range(len(norms)):
                    if a != b and norms[b] and norms[a] != norms[b] and norms[a] in norms[b]:
                        warnings.append("JD技能粒度护栏《%s》%s：条目「%s」是「%s」的子串"
                                        "（碎片化特征）→ 请合并成一条完整技能条目"
                                        % (label, field_name,
                                           str(items[a]).strip(), str(items[b]).strip()))
                        reported.add(a)
                        n += 1
                        break
    return n


def fetch_current_user_cell(tbl: AITable,
                            warnings: List[str]) -> Tuple[Optional[List[Dict[str, Any]]],
                                                          Optional[str]]:
    """需求提交人 = 当前登录用户（老插件口径 job-intake/SKILL.md:27；契约 v3 §9#5 降级为可选）。

    一次 `dws contact +me` **只读**调用拿 userId，拼成 user 字段的写入格式
    `[{"userId": "..."}]`（dws aitable record create 帮助文档的 user 类型格式）。
    获取失败不致命：返回 (None, None)，该列留空并进 warnings（D6 失败可见）。
    """
    try:
        res = tbl.client.call(["contact", "+me"], yes=False)
        # aitable.client.unwrap 已统一识别 `+` 命令的双层信封 {"ok","outcome","data"}
        # （缺陷4 修复，2026-09-17）：这里拿到的 res["data"] 就是内层业务数据，
        # 不再需要（也不允许）在调用方做局部剥壳 workaround。
        d = (res or {}).get("data") or {}
        uid = d.get("userId") or d.get("user_id")
        if not uid:
            warnings.append("dws contact +me 未返回 userId（实得 %s）→ 需求提交人本次留空"
                            % json.dumps(d, ensure_ascii=False)[:120])
            return None, None
        cell: Dict[str, Any] = {"userId": str(uid)}
        corp = d.get("corpId") or d.get("corp_id")
        if corp:
            cell["corpId"] = str(corp)
        return [cell], (d.get("name") or None)
    except Exception as exc:
        warnings.append("获取当前登录用户失败（%s: %s）→ 需求提交人本次留空，"
                        "可重跑或人工补填" % (type(exc).__name__, str(exc)[:160]))
        return None, None


# --------------------------------------------------------------------------- #
# 回读校验（按 filter 查，一次调用同时拿到 record_id 映射与读回值）
# --------------------------------------------------------------------------- #
def verify_jobs(tbl: AITable, entries: Sequence[Dict[str, Any]],
                fields: Sequence[str], check_attach: bool,
                settle_waits: Sequence[float] = SETTLE_WAITS) -> Dict[str, Any]:
    """岗位侧写后回读（契约 D6）。

    与简历侧不同：**岗位名称不唯一**（19 份真实 JD 里「工程师/高级工程师」在
    硅片制造部-工艺部 / 硅片制造部-设备部 / 组件制造部-工艺部 … 各出现一次），
    所以按 job_name filter 查回来的是「一名多条」，必须再用 **job_id（全表唯一）**
    或 **(岗位名称, 所属部门, 组织分类)** 三元组把 record_id 精确对回本批的每一行，
    否则会串档（把 A 部门的 record_id 写到 B 部门那条上）。

    一次 filter 查询同时完成：record_id 精确归属 + 字段值比对 + 附件非空检查。
    传播延迟用**有界**轮询（1.5/3.0/4.5s），不空转烧调用。
    """
    t0 = time.monotonic()
    calls0 = tbl.dws_calls
    names = sorted({_clean(e["row"]["job_name"]) for e in entries if e.get("row")})
    polls = 0
    group: Dict[str, List[Dict[str, Any]]] = {}
    mismatch: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    attach_missing: List[str] = []
    submitter_missing: List[str] = []
    while True:
        recs: List[Dict[str, Any]] = []
        for i in range(0, len(names), 100):
            recs.extend(tbl.query_records("job", filter={"job_name": names[i:i + 100]},
                                          fields=list(fields), limit=100, all_pages=True))
        group = {}
        for r in recs:
            jn = _clean((r.get("cells") or {}).get("job_name"))
            if jn:
                group.setdefault(jn, []).append(r)

        mismatch, unresolved, attach_missing, submitter_missing = [], [], [], []
        for ent in entries:
            jn = _clean(ent["row"]["job_name"])
            cands = group.get(jn) or []
            pick = None
            jid = _clean(ent.get("job_id"))
            if jid:
                pick = next((r for r in cands
                             if _clean((r.get("cells") or {}).get("job_id")) == jid), None)
            if pick is None:
                dep, org = _clean(ent["row"].get("department")), _clean(ent["row"].get("org"))
                same = [r for r in cands
                        if _clean((r.get("cells") or {}).get("department")) == dep
                        and _clean((r.get("cells") or {}).get("org")) == org]
                pick = same[0] if len(same) == 1 else (same[0] if same else
                                                       (cands[0] if len(cands) == 1 else None))
            if pick is None:
                unresolved.append("%s|%s" % (jn, ent["file_name"]))
                continue
            ent["record_id"] = pick["record_id"]
            if not ent.get("job_id"):
                ent["job_id"] = _clean((pick.get("cells") or {}).get("job_id"))
            cells = pick.get("cells") or {}
            for fk, ev in ent["row"].items():
                # attachment / submitter(user) 读回结构与写入结构不同构，只查非空不比值
                if fk in ("attachment", "submitter") or fk not in fields:
                    continue
                if not values_equal(ev, cells.get(fk)):
                    mismatch.append({"key": jn, "record_id": pick["record_id"], "field": fk,
                                     "expected": ev, "actual": cells.get(fk)})
            if check_attach and not cells.get("attachment"):
                attach_missing.append("%s(%s)" % (jn, ent.get("job_id") or "-"))
            if "submitter" in fields and ent["row"].get("submitter") \
                    and not cells.get("submitter"):
                submitter_missing.append("%s(%s)" % (jn, ent.get("job_id") or "-"))
        if not unresolved and not mismatch:
            break
        if polls >= len(settle_waits):
            break
        time.sleep(settle_waits[polls])
        polls += 1

    return {"ok": not unresolved and not mismatch, "requested": len(entries),
            "found": len(entries) - len(unresolved), "missing": unresolved,
            "mismatch": mismatch, "attachment_missing": attach_missing,
            "submitter_missing": submitter_missing,
            "settle_polls": polls, "dws_calls": tbl.dws_calls - calls0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000)}


# --------------------------------------------------------------------------- #
# Turn 1：提取 → 预填 → 查重 → 批量写 → 附件 → 回读 → jobs_draft.json
# --------------------------------------------------------------------------- #
def run_turn1(args: argparse.Namespace, tbl: AITable, counter: DwsCallCounter,
              out_dir: Path, batch_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    jobs: List[Dict[str, Any]] = []
    summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
               "attachment_uploaded": 0, "attachment_failed": 0}
    fatal: Optional[str] = None
    files: List[str] = list(args.files or [])

    have = set(tbl.field_keys("job"))
    known_locs = [o.get("name") for o in
                  ((tbl.config.get("options") or {}).get("job") or {}).get("work_location", [])
                  if o.get("name")]

    # ---- 阶段 1：提取 + 正则预填（纯本地）----
    entries: List[Dict[str, Any]] = []
    t_extract = time.monotonic()
    for i, fp in enumerate(files):
        p = Path(fp).expanduser()
        fname = p.name
        ex = extract_text(str(p))
        ent: Dict[str, Any] = {
            "seq": i + 1, "file_name": fname, "path": str(p.resolve()),
            "size": ex.get("size") or 0, "kind": ex.get("kind"),
            "parse_status": ex.get("status"), "text": ex.get("text") or "",
            "fields": None, "result": None, "reason": None, "dedupe": "new",
            "attachment_status": "deferred", "record_id": None, "writable": False,
            "warnings": [],
        }
        if ex.get("status") != "ok":
            ent["result"] = "失败"
            reason = PARSE_FAIL_REASON.get(ex.get("status"), PARSE_FAIL_REASON["error"])
            if ex.get("status") == "unsupported":
                reason = "%s（扩展名 %s）" % (reason, ex.get("ext") or p.suffix or "(无)")
            if ex.get("error"):
                reason = "%s（技术细节：%s）" % (reason, str(ex["error"])[:120])
            ent["reason"] = reason
        elif detect_scanned(ent["text"], ent.get("kind") or ""):
            ent["parse_status"] = "no_text_layer"
            ent["result"] = "失败"
            ent["reason"] = PARSE_FAIL_REASON["no_text_layer"]
        else:
            f = extract_job_fields(ent["text"], fname)
            ent["fields"] = f
            ent["warnings"] = list(f.get("warnings") or [])
            if not _clean(f.get("job_name")):
                ent["result"] = "失败"
                ent["reason"] = ("未能解析出岗位名称，无法查重与入库；"
                                 "请人工确认 JD 里的「职位名称 Position:」后补录")
            else:
                ent["writable"] = True
        entries.append(ent)
    extract_ms = int((time.monotonic() - t_extract) * 1000)
    print("提取完成：%d 份 JD，%d 份可用文本；本地耗时 %dms（0 次 dws 调用）"
          % (len(entries), sum(1 for e in entries if e["parse_status"] == "ok"), extract_ms),
          flush=True)

    # ---- 阶段 2：一次全表扫描 → 同时得到「复合键查重索引」与「岗位ID 最大序号」----
    # 岗位ID 必须全表唯一（老插件 job-intake/SKILL.md:25），本来就要全表扫；
    # 查重键（岗位名称+所属部门+组织分类）也一并从这次扫描里取，省一次调用。
    existing: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    max_seq = 0
    if not fatal:
        try:
            t0 = time.monotonic()
            calls0 = counter.calls
            scan_fields = [k for k in ("job_id", "job_name", "department", "org") if k in have]
            recs = tbl.query_records("job", fields=scan_fields, all_pages=True, max_pages=100)
            for r in recs:
                c = r.get("cells") or {}
                jn = _clean(c.get("job_name")) or ""
                dep = _clean(c.get("department")) or ""
                org = _clean(c.get("org")) or ""
                existing.setdefault((jn, dep, org), []).append(
                    {"record_id": r["record_id"], "job_id": _clean(c.get("job_id"))})
                jid = _clean(c.get("job_id")) or ""
                m = re.search(r"(\d+)\s*$", jid)
                if m:
                    max_seq = max(max_seq, int(m.group(1)))
            print("岗位表扫描：%d 条在库记录，复合键 %d 个，现有岗位ID 最大序号 %d"
                  "（%.2fs，%d 次调用）"
                  % (len(recs), len(existing), max_seq, time.monotonic() - t0,
                     counter.calls - calls0), flush=True)
        except DwsError as exc:
            fatal = "岗位表查重扫描失败（%s/%s）：%s" % (exc.category, exc.code, exc.message[:300])
            warnings.append(fatal)

    # ---- 阶段 3：组织/地点预判 + 复合键查重（老插件口径：三者全同才算重复）----
    to_write: List[Dict[str, Any]] = []
    all_departments: List[str] = []
    all_orgs: List[str] = []
    all_locations: List[str] = []
    batch_keys: Dict[Tuple[str, str, str], int] = {}

    for ent in entries:
        if not ent["writable"]:
            continue
        f = ent["fields"]
        job_name = _clean(f.get("job_name"))
        dept = _clean(f.get("department"))
        org, org_conf, org_info = guess_org(ent["file_name"], dept, job_name, ent["text"])
        locs = guess_locations(ent["file_name"], ent["text"], known_locs)
        ent["org"], ent["org_confidence"], ent["org_info"] = org, org_conf, org_info
        ent["locations"] = locs
        if org_conf == "low":
            if org:
                warnings.append("《%s》组织分类低置信度：文件名显式声明与部门关键词打架"
                                "（判据 %s），已按显式声明写 %s，请 Turn 2 复核"
                                % (ent["file_name"], json.dumps(org_info, ensure_ascii=False), org))
            else:
                warnings.append("《%s》组织分类判不了（判据 %s），组织留空 → "
                                "该岗位匹配不到任何简历，请 Turn 2 必须补 org"
                                % (ent["file_name"], json.dumps(org_info, ensure_ascii=False)))
        if not locs:
            warnings.append("《%s》未解析到工作地点，留空待 Turn 2 补" % ent["file_name"])
        if f.get("cert_is_preferred_not_required"):
            ent["warnings"].append("cert_is_preferred_not_required=True：检出的证书全部出现在"
                                   "「…者优先/加分」语境，**不得**当硬性门槛用（会误杀）")

        key3 = (job_name or "", dept or "", org or "")
        if key3 in batch_keys:
            ent["result"] = "跳过"
            ent["dedupe"] = "overwrite"
            ent["reason"] = ("与本批第 %d 份《%s》的「岗位名称+所属部门+组织分类」完全相同，"
                             "判为重复上传，已跳过" % (batch_keys[key3], ent["file_name"]))
            continue
        batch_keys[key3] = ent["seq"]
        hits = existing.get(key3) or []
        if len(hits) > 1:
            ent["result"] = "失败"
            ent["dedupe"] = "conflict"
            ent["writable"] = False
            ent["reason"] = ("库内「%s / %s / %s」命中 %d 条记录（%s），已停下不自动选择，"
                             "请确认保留哪一条后再重跑"
                             % (job_name, dept, org, len(hits),
                                ", ".join(str(h["record_id"]) for h in hits[:5])))
            warnings.append("岗位复合键多条冲突：%s → %d 条，需人工确认" % (key3, len(hits)))
            continue
        if hits:
            ent["dedupe"] = "overwrite"
            ent["record_id"] = hits[0]["record_id"]
            ent["job_id"] = hits[0].get("job_id")       # 沿用库内已有岗位ID（全表唯一）
        else:
            ent["dedupe"] = "new"
            max_seq += 1
            ent["job_id"] = "JOB-%03d" % max_seq
        to_write.append(ent)
        if dept and dept not in all_departments:
            all_departments.append(dept)
        if org and org not in all_orgs:
            all_orgs.append(org)
        for l in locs:
            if l not in all_locations:
                all_locations.append(l)

    # ---- 阶段 4：部门/组织/工作地点选项 ensure_options（只增不删）----
    for field_key, names in (("department", all_departments), ("org", all_orgs),
                             ("work_location", all_locations)):
        if not names or field_key not in have:
            continue
        try:
            t0 = time.monotonic()
            calls0 = counter.calls
            opts = tbl.ensure_options("job", field_key, names)
            print("ensure_options(job.%s)：%d 个候选值 → 字段现有 %d 个选项（%.2fs，%d 次调用）"
                  % (field_key, len(names), len(opts), time.monotonic() - t0,
                     counter.calls - calls0), flush=True)
        except Exception as exc:
            warnings.append("ensure_options(job.%s) 失败：%s: %s；写入时服务端通常会自动补选项"
                            % (field_key, type(exc).__name__, str(exc)[:200]))

    # ---- 阶段 5：整理写入行（Turn 1 只写脚本能确定的字段）----
    # 需求提交人（契约 v3 §9#5：可选。config.json 配了 fields.job.submitter 才回填，
    # 值 = 当前登录用户；未配置 → 留空并在 warnings 里向客户明示语义弱化）
    submitter_cell: Optional[List[Dict[str, Any]]] = None
    if to_write and "submitter" in have:
        submitter_cell, submitter_name = fetch_current_user_cell(tbl, warnings)
        if submitter_cell is not None:
            print("需求提交人：当前登录用户 %s（user 字段回填）" % (submitter_name or "?"),
                  flush=True)
    elif to_write:
        warnings.append("config.json 未配置 job.submitter（需求提交人）字段映射 → 该列留空。"
                        "老插件口径「需求提交人」必填（=当前登录用户），极速版按契约 v3 §9#5 "
                        "降级为可选（D1 不依赖 user 字段）；客户表里有该列时，在 config.json "
                        "的 fields.job 里补 \"submitter\" 映射后重跑即可回填（是否补由客户决定）")
    for ent in to_write:
        f = ent["fields"]
        secs = f.get("sections") or {}
        resp = _clean(secs.get("responsibility_text")) or _clean(secs.get("work_text"))
        req = _clean(secs.get("qualification_text")) or _clean(secs.get("skill_text"))
        row: Dict[str, Any] = {
            "job_name": _clean(f.get("job_name")),
            "status": STATUS_DEFAULT,
            "must_weight": MUST_WEIGHT_DEFAULT,
            "bonus_weight": BONUS_WEIGHT_DEFAULT,
            "submit_time": _today(),
        }
        if ent.get("job_id"):
            row["job_id"] = ent["job_id"]
        if _clean(f.get("department")):
            row["department"] = _clean(f.get("department"))
        if ent.get("org"):
            row["org"] = ent["org"]
        if ent.get("locations"):
            row["work_location"] = ent["locations"]
        if resp:
            row["responsibilities"] = resp[:RICHTEXT_MAX]
        if req:
            row["requirements"] = req[:RICHTEXT_MAX]
        if submitter_cell is not None:
            row["submitter"] = submitter_cell          # v3 §9#5：配置了才回填
        for k in ("job_id", "department", "org", "work_location",
                  "responsibilities", "requirements"):
            if k not in have and k in row:
                row.pop(k)
                warnings.append("config.json 的 job 表没有字段 %s，已跳过写入" % k)
        # 刻意**不写** hard_gates / must_skills / bonus_skills（见模块 docstring）
        ent["row"] = row

    # ---- 阶段 6：并发上传 JD 附件（一律原始文件名），fileToken 随 create 一次写入 ----
    if to_write and not args.no_attachment:
        t0 = time.monotonic()
        calls0 = counter.calls
        results = tbl.upload_attachments([e["path"] for e in to_write],
                                         concurrency=args.concurrency)
        for ent, res in zip(to_write, results):
            if res.get("ok") and res.get("cell"):
                if "attachment" in have:
                    ent["row"]["attachment"] = res["cell"]
                ent["attachment_status"] = "uploaded"
                summary["attachment_uploaded"] += 1
            else:
                ent["attachment_status"] = "failed"
                summary["attachment_failed"] += 1
                warnings.append("《%s》JD 附件上传失败（%s/%s）：%s；岗位正文已照常入库，"
                                "可后续补传"
                                % (ent["file_name"], res.get("category"), res.get("code"),
                                   str(res.get("error"))[:200]))
        print("附件并发上传（concurrency=%d）：%d 成功 / %d 失败，%dms，%d 次调用"
              % (args.concurrency, summary["attachment_uploaded"], summary["attachment_failed"],
                 int((time.monotonic() - t0) * 1000), counter.calls - calls0), flush=True)

    # ---- 阶段 7：批量写（复合键无法用 batch_upsert_by_key，自己拆 create/update）----
    creates = [e for e in to_write if e["dedupe"] == "new"]
    updates = [e for e in to_write if e["dedupe"] == "overwrite"]
    if creates and not fatal:
        t0 = time.monotonic()
        calls0 = counter.calls
        try:
            r = tbl.batch_create("job", [e["row"] for e in creates])
            print("batch_create：%d 条提交 / created=%d / failed=%d（%.2fs，%d 次调用）"
                  % (len(creates), r.get("created", 0), len(r.get("failed") or []),
                     time.monotonic() - t0, counter.calls - calls0), flush=True)
            for fl in (r.get("failed") or []):
                idx = fl.get("row_index")
                ent = creates[idx] if isinstance(idx, int) and idx < len(creates) else None
                msg = str(fl.get("reason"))[:240]
                warnings.append("岗位写入失败《%s》：%s"
                                % (ent["file_name"] if ent else "(未定位)", msg))
                if ent:
                    ent["result"] = "失败"
                    ent["writable"] = False
                    ent["reason"] = "写入岗位JD表失败：%s" % msg[:200]
        except DwsError as exc:
            fatal = "批量新建岗位失败（%s/%s）：%s" % (exc.category, exc.code, exc.message[:300])
            warnings.append(fatal)
    if updates and not fatal:
        t0 = time.monotonic()
        calls0 = counter.calls
        try:
            r = tbl.batch_update("job", [{"record_id": e["record_id"], "cells": e["row"]}
                                         for e in updates])
            print("batch_update：%d 条提交 / updated=%d / failed=%d（%.2fs，%d 次调用）"
                  % (len(updates), r.get("updated", 0), len(r.get("failed") or []),
                     time.monotonic() - t0, counter.calls - calls0), flush=True)
            for fl in (r.get("failed") or []):
                rid = fl.get("record_id") or (fl.get("row") or {}).get("record_id")
                ent = next((e for e in updates if e["record_id"] == rid), None)
                msg = str(fl.get("reason"))[:240]
                warnings.append("岗位覆盖更新失败《%s》：%s"
                                % (ent["file_name"] if ent else "(未定位)", msg))
                if ent:
                    ent["result"] = "失败"
                    ent["writable"] = False
                    ent["reason"] = "覆盖更新岗位JD表失败：%s" % msg[:200]
        except DwsError as exc:
            fatal = "批量覆盖岗位失败（%s/%s）：%s" % (exc.category, exc.code, exc.message[:300])
            warnings.append(fatal)

    # ---- 阶段 8：回读校验（一次 filter 查询；岗位名不唯一，用 job_id/三元组精确归属）----
    written = [e for e in to_write if e["result"] is None]
    verify: Dict[str, Any] = {}
    if written and not fatal:
        rb_fields = [k for k in ("job_name", "job_id", "department", "org", "status",
                                 "work_location", "must_weight", "bonus_weight",
                                 "submit_time", "attachment", "submitter") if k in have]
        t0 = time.monotonic()
        verify = verify_jobs(tbl, written, rb_fields,
                             check_attach=("attachment" in rb_fields and not args.no_attachment))
        print("回读校验：%d 条请求 / %d 条精确归属 / %d 处不一致 / %d 条附件缺失，轮询 %d 次，"
              "%.2fs，%d 次调用"
              % (verify.get("requested", 0), verify.get("found", 0),
                 len(verify.get("mismatch") or []), len(verify.get("attachment_missing") or []),
                 verify.get("settle_polls", 0), time.monotonic() - t0,
                 verify.get("dws_calls", 0)), flush=True)
        if verify.get("missing"):
            warnings.append("回读未能精确归属 %d 个岗位（%s）；写入可能未生效或岗位名+部门+组织"
                            "在库内有多条，请在后续回合重跑本步复核（契约 D6/D7）"
                            % (len(verify["missing"]), list(verify["missing"])[:5]))
        for mm in (verify.get("mismatch") or [])[:20]:
            warnings.append("回读不一致：岗位 %s 字段 %s 期望 %r 实得 %r"
                            % (mm.get("key"), mm.get("field"), str(mm.get("expected"))[:60],
                               str(mm.get("actual"))[:60]))
        if verify.get("attachment_missing"):
            warnings.append("回读发现 %d 个岗位 JD 附件为空：%s；可后续「补传附件」重跑"
                            % (len(verify["attachment_missing"]),
                               list(verify["attachment_missing"])[:5]))
        if verify.get("submitter_missing"):
            warnings.append("回读发现 %d 个岗位「需求提交人」为空（已配置 job.submitter "
                            "但写入未确认）：%s；请重跑本步或人工补填"
                            % (len(verify["submitter_missing"]),
                               list(verify["submitter_missing"])[:5]))

    # ---- 阶段 9：组装 rows / jobs ----
    for ent in entries:
        f = ent.get("fields") or {}
        secs = f.get("sections") or {}
        if ent["result"] is None:
            if not ent["writable"]:
                ent["result"] = "失败"
                ent["reason"] = ent["reason"] or "未能入库（原因见 warnings）"
            elif fatal or not ent.get("record_id"):
                # 契约 D6：回读没精确归属到 record_id 就不许报成功
                ent["result"] = "失败"
                ent["reason"] = fatal or (
                    "已提交写入但回读未能精确归属到岗位记录，无法确认入库；"
                    "请在下一回合重跑本步复核（复合键查重幂等，不会重复建岗）")
            elif ent["dedupe"] == "new":
                ent["result"] = "新入库"
                ent["reason"] = "新岗位，已分配岗位ID %s 并置「招聘中」" % (ent.get("job_id") or "-")
            else:
                ent["result"] = "已覆盖"
                ent["reason"] = "岗位名称+所属部门+组织分类三者全同，已覆盖更新"
        if ent["result"] == "新入库":
            summary["new"] += 1
        elif ent["result"] == "已覆盖":
            summary["overwrite"] += 1
        elif ent["result"] == "跳过":
            summary["skip"] += 1
        else:
            summary["fail"] += 1

        extra = []
        if ent.get("org"):
            extra.append("%s%s" % (ent["org"], "" if ent.get("org_confidence") == "high"
                                   else "(待确认)"))
        if _clean(f.get("department")):
            extra.append(str(f.get("department")))
        if ent["attachment_status"] == "uploaded":
            extra.append("JD附件已传")
        elif ent["attachment_status"] == "failed":
            extra.append("JD附件失败")
        reason = ent.get("reason") or ""
        if extra and ent["result"] in ("新入库", "已覆盖"):
            reason = "%s（%s）" % (reason, "，".join(extra))
        reason += "；硬性门槛/必备技能/加分项待 Turn 2 归一化后由 --apply 写入" \
            if ent["result"] in ("新入库", "已覆盖") else ""
        rows.append({"seq": ent["seq"], "file_name": ent["file_name"],
                     "result": ent["result"], "reason": reason})

        job: Dict[str, Any] = {
            "key": "j%02d" % ent["seq"],
            "record_id": ent.get("record_id"),
            "job_id": ent.get("job_id"),
            "job_name": _clean(f.get("job_name")),
            "department": _clean(f.get("department")),
            "org": ent.get("org"),
            "status": STATUS_DEFAULT if ent["result"] in ("新入库", "已覆盖") else None,
            "hard_gates": {
                "education": _clean(f.get("education_req")),
                "major": _clean(f.get("major_req")),
                "years": _clean(f.get("years_req")),
                "certificates": _clean(f.get("cert_req")),
            },
            "must_skills": [str(x) for x in (f.get("must_skills") or [])],
            "bonus_skills": [str(x) for x in (f.get("bonus_skills") or [])],
            "weights": {"must": MUST_WEIGHT_DEFAULT, "bonus": BONUS_WEIGHT_DEFAULT},
            # ---- 交给 Turn 2 的 LLM 归一化（本脚本刻意不写库）----
            "file_name": ent["file_name"],
            "parse_status": ent["parse_status"],
            "dedupe": ent["dedupe"],
            "attachment_status": ent["attachment_status"],
            "work_location": ent.get("locations") or [],
            "org_confidence": ent.get("org_confidence") or "low",
            "needs_llm_normalization": list(LLM_NORMALIZE_FIELDS),
            "draft_source": "regex(Turn1) —— 命中率实测 26%~79%，必须 Turn 2 复核",
            "hard_gates_raw": _clean(f.get("hard_gates_raw")),
            "must_skills_raw": _clean(f.get("must_skills_raw")),
            "bonus_skills_raw": _clean(f.get("bonus_skills_raw")),
            "cert_is_preferred_not_required": bool(f.get("cert_is_preferred_not_required")),
            "cert_required": [str(x) for x in (f.get("cert_required") or [])],
            "cert_preferred": [str(x) for x in (f.get("cert_preferred") or [])],
            "years_req_min": f.get("years_req_min"),
            "age_req": _clean(f.get("age_req")),
            "regex_confidence": dict(f.get("confidence") or {}),
            "evidence": {k: _truncate(secs.get(k), 4000) for k in
                         ("education_text", "cert_text", "work_text", "skill_text",
                          "qualification_text", "responsibility_text", "kpi_text",
                          "age_text")},
            "warnings": list(ent.get("warnings") or []),
        }
        if ent["parse_status"] != "ok":
            # 契约 D11：不硬造字段
            for k in ("job_id", "job_name", "department", "org", "status",
                      "hard_gates_raw", "must_skills_raw", "bonus_skills_raw",
                      "years_req_min", "age_req"):
                job[k] = None
            job["hard_gates"] = {k: None for k in HARD_GATE_KEYS}
            job["must_skills"] = []
            job["bonus_skills"] = []
            job["cert_is_preferred_not_required"] = False
            job["cert_required"] = []
            job["cert_preferred"] = []
            job["work_location"] = []
            job["org_confidence"] = "low"
            job["evidence"] = {k: "" for k in job["evidence"]}
        jobs.append(job)

    for w in tbl.warnings:
        if w not in warnings:
            warnings.append(w)

    # 基础设施级失败判定（同 intake_resume.py）：该写的岗位一份都没写进去 → ok=false，
    # 让 agent 重跑本步（契约 D7）；全是扫描件导致 rows 全失败则是正常业务结论。
    wrote_ok = [e for e in to_write if e["result"] in ("新入库", "已覆盖")]
    infra_fail = bool(to_write) and not wrote_ok
    if infra_fail:
        warnings.append("本批 %d 份可入库 JD **一份都没写成功**（选项/附件/写库/回读链路"
                        "出现基础设施级错误）；已置 ok=false，请修复后重跑本步"
                        "（幂等：岗位名称+所属部门+组织分类 三者查重，不会重复建岗）"
                        % len(to_write))

    draft = {"batch_id": batch_id, "generated_at": now_iso(),
             "config_path": tbl.config_path, "jobs": jobs, "_warnings": warnings}
    return draft, rows, 0 if (fatal is None and not infra_fail) else 1


# --------------------------------------------------------------------------- #
# Turn 3：--apply jobs_final.json（LLM 归一化结果批量补写）
# --------------------------------------------------------------------------- #
def run_apply(args: argparse.Namespace, tbl: AITable, counter: DwsCallCounter,
              out_dir: Path, batch_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int]:
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
               "attachment_uploaded": 0, "attachment_failed": 0}
    have = set(tbl.field_keys("job"))
    path = Path(args.apply).expanduser()
    if not path.exists():
        return ({"ok": False, "_warnings": ["--apply 文件不存在：%s" % path]},
                [{"seq": 1, "file_name": str(path), "result": "失败",
                  "reason": "jobs_final.json 不存在，无法应用 Turn 2 归一化结果"}], 1)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return ({"ok": False, "_warnings": ["--apply 文件解析失败：%s" % exc]},
                [{"seq": 1, "file_name": str(path), "result": "失败",
                  "reason": "jobs_final.json 不是合法 JSON：%s" % exc}], 1)

    items = doc.get("jobs") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        items = []
        warnings.append("jobs_final.json 里没有 jobs 数组，无可应用项")

    # ---- 需要时用一次 filter 查询把 job_name → record_id 补齐 ----
    need_lookup = [j for j in items if not (j.get("record_id") or j.get("recordId"))]
    name2rec: Dict[str, str] = {}
    if need_lookup:
        names = sorted({_clean(j.get("job_name")) for j in need_lookup if _clean(j.get("job_name"))})
        try:
            recs: List[Dict[str, Any]] = []
            for i in range(0, len(names), 100):
                recs.extend(tbl.query_records("job", filter={"job_name": names[i:i + 100]},
                                              fields=[k for k in ("job_name", "job_id",
                                                                  "department", "org")
                                                      if k in have],
                                              limit=100, all_pages=True))
            for r in recs:
                jn = _clean((r.get("cells") or {}).get("job_name"))
                if jn:
                    name2rec.setdefault(jn, r["record_id"])
        except DwsError as exc:
            warnings.append("按岗位名称回查 record_id 失败（%s/%s）：%s"
                            % (exc.category, exc.code, exc.message[:200]))

    updates: List[Dict[str, Any]] = []
    meta: List[Dict[str, Any]] = []
    granularity_items: List[Tuple[str, Any, Any]] = []   # 任务二：(label, must_raw, bonus_raw)
    for i, j in enumerate(items):
        label = _clean(j.get("file_name")) or _clean(j.get("job_name")) or \
            _clean(j.get("job_id")) or "(第 %d 项)" % (i + 1)
        rid = j.get("record_id") or j.get("recordId")
        if not rid:
            rid = name2rec.get(_clean(j.get("job_name")) or "")
        if not rid:
            rows.append({"seq": i + 1, "file_name": label, "result": "失败",
                         "reason": "未能定位到岗位记录（jobs_final.json 没给 record_id，"
                                   "按岗位名称也没查到），请先跑 Turn 1 或在 jobs_final.json 里补 record_id"})
            summary["fail"] += 1
            continue
        cells: Dict[str, Any] = {}
        hg = compose_hard_gates(j.get("hard_gates"))
        if hg and "hard_gates" in have:
            cells["hard_gates"] = hg
        ms = as_text_list(j.get("must_skills"))
        if ms and "must_skills" in have:
            cells["must_skills"] = ms
        bs = as_text_list(j.get("bonus_skills"))
        if bs and "bonus_skills" in have:
            cells["bonus_skills"] = bs
        w = j.get("weights") or {}
        if isinstance(w, dict):
            for src, dst in (("must", "must_weight"), ("bonus", "bonus_weight")):
                v = w.get(src)
                if isinstance(v, (int, float)) and dst in have:
                    cells[dst] = float(v)
        for src, dst in (("org", "org"), ("department", "department"), ("status", "status"),
                         ("job_name", "job_name"), ("job_id", "job_id")):
            v = _clean(j.get(src))
            if v and dst in have:
                cells[dst] = v
        if "work_location" in have:
            wl = j.get("work_location")
            if isinstance(wl, str) and _clean(wl):
                cells["work_location"] = [_clean(wl)]
            elif isinstance(wl, (list, tuple)) and wl:
                cells["work_location"] = [str(x).strip() for x in wl if str(x).strip()]
        if not cells:
            rows.append({"seq": i + 1, "file_name": label, "result": "跳过",
                         "reason": "jobs_final.json 里这一项没有任何可写语义字段，已跳过"})
            summary["skip"] += 1
            continue
        if not (ms or bs or hg):
            warnings.append("《%s》Turn 2 归一化后必备技能/加分项/硬性门槛仍全空 → "
                            "该岗位无法参与打分（分母为 0），请复核 jobs_final.json" % label)
        updates.append({"record_id": rid, "cells": cells})
        meta.append({"seq": i + 1, "label": label, "rid": rid, "cells": cells})
        # 任务二：收集本岗位 Turn 2 归一化后的技能**逐条原文**（list 或串），供粒度护栏检查
        granularity_items.append((label, j.get("must_skills"), j.get("bonus_skills")))

    # ---- Turn 2 语义偷懒护栏（缺陷2 增补，2026-09-17；与 verify_decisions 的语义护栏同因）----
    # W-F run3 事故：agent 自写 normalize_jobs.py 规则脚本代替 LLM 归一化 JD。
    # 批级形态学护栏（保守阈值，避免误报）：归一化后 must_skills **全部为空**
    # 或**全部雷同**（同一文本）→ 说明 Turn 2 大概率没做真归一化，告警并要求复核。
    # 注：部分岗位（同族岗位）must_skills 相似是正常现象（run1/run3 实测各有 2~3 组
    # 重复），所以只在 100% 空 / 100% 雷同时告警，不做比例阈值。
    if updates and "must_skills" in have:
        ms_norm = [re.sub(r"[\s、，,;；]+", "", str(u["cells"].get("must_skills") or ""))
                   for u in updates]
        n_ms = len(ms_norm)
        if n_ms >= 3:
            if all(not m for m in ms_norm):
                warnings.append("Turn 2 归一化护栏：%d 个岗位的必备技能**全部为空** → "
                                "所有岗位都无法参与打分（分母=0），疑似 Turn 2 没做真归一化"
                                "（禁止用规则脚本代替 LLM 语义归一化，见 SKILL.md）；"
                                "请复核 jobs_final.json 后重跑 --apply" % n_ms)
            elif len(set(ms_norm)) == 1:
                warnings.append("Turn 2 归一化护栏：%d 个岗位的必备技能**全部雷同**（同一文本"
                                "「%s…」）→ 疑似模板化/脚本化归一化产物，请逐岗复核 "
                                "jobs_final.json 后重跑 --apply" % (n_ms, ms_norm[0][:30]))

    # ---- 任务二（W-J，根因来自 W-I 实测）：JD 技能条目**粒度护栏**（只增 warning 不拦写）----
    # 紧接上面的批级护栏往下：逐条检查 must_skills/bonus_skills 的粒度（句子碎片/单个泛化词/
    # 泛化软技能/同岗位互为子串）。碎片几乎任何简历都能「语义等价」命中 → 匹配放水 + 判定回合
    # 反复斟酌。告警带具体岗位与条目，要求 agent 回 Turn 2 重切成原子技能名词短语后重跑 --apply。
    if granularity_items:
        n_gran = check_skill_granularity(granularity_items, warnings)
        if n_gran:
            warnings.append("JD技能粒度护栏小结：本批触发 %d 条可疑技能条目（句子碎片/单个泛化词/"
                            "泛化软技能/同岗位互为子串）。根因是 JD 抽取把技能切成了句子碎片，"
                            "几乎任何简历都能「语义等价」命中 → 匹配放水、命中数虚高、判定回合反复斟酌。"
                            "请在 Turn 2 归一化时把每一项改成**原子、可独立验证**的技能/能力名词短语"
                            "（正反例见 SKILL.md / HOTPATH.md），再重跑 --apply（本护栏只告警不拦写）。"
                            % n_gran)

    # ---- 用 W-B 的安全回填路径：≤10 条/片 + 回读 + 有界重试（不空转烧调用）----
    res: Dict[str, Any] = {}
    if updates:
        t0 = time.monotonic()
        calls0 = counter.calls
        try:
            res = tbl.batch_update_verified("job", updates, chunk_size=10, settle_tries=2)
        except DwsError as exc:
            warnings.append("批量补写语义字段失败（%s/%s）：%s"
                            % (exc.category, exc.code, exc.message[:300]))
        print("batch_update_verified：%d 条提交 / verified=%d / recovered=%d / failed=%d"
              "（%.2fs，%d 次调用）"
              % (len(updates), res.get("verified", 0), res.get("recovered", 0),
                 len(res.get("failed") or []), time.monotonic() - t0, counter.calls - calls0),
              flush=True)
        # ---- 迟到的传播延迟二次复核（1 次调用，不空转）----
        # W-B 实测（aitable/table.py 模块文档第 8 条）：对「不久前刚批量写过的 job 表记录」
        # 再发 update，会返回 success 但读回要几分钟后才见到新值，当场怎么重试都没用。
        # batch_update_verified 只做**有界**重试然后如实报 failed（D6）。这里在全部片
        # 写完后补一次「迟到复核」：短延迟（十几秒内可见）的情况能就地救回来，
        # 长延迟的仍然报 failed 并让 agent 在下一回合重跑 --apply（幂等）。
        lag_ids = [f.get("record_id") for f in (res.get("failed") or [])
                   if f.get("category") == "server_write_lag" and f.get("record_id")]
        late_recovered: List[str] = []
        if lag_ids:
            exp_map = {u["record_id"]: u["cells"] for u in updates}
            late_expected = {rid: exp_map[rid] for rid in lag_ids if rid in exp_map}
            time.sleep(args.late_verify_wait)
            rb2 = tbl.readback_verify("job", lag_ids,
                                      sorted({k for c in late_expected.values() for k in c}),
                                      expected=late_expected, settle_tries=1)
            bad2 = {m["record_id"] for m in rb2["mismatch"]} | set(rb2["missing"])
            late_recovered = [rid for rid in lag_ids if rid not in bad2]
            if late_recovered:
                res["failed"] = [f for f in res["failed"]
                                 if f.get("record_id") not in set(late_recovered)]
                res["verified"] = res.get("verified", 0) + len(late_recovered)
                res["late_recovered"] = len(late_recovered)
                res["verify_ok"] = not res["failed"]
                warnings.append("写入传播延迟二次复核（+%ds）救回 %d 条：%s"
                                % (args.late_verify_wait, len(late_recovered),
                                   late_recovered[:6]))
            still = [f.get("record_id") for f in res.get("failed") or []]
            if still:
                warnings.append("仍有 %d 条「record update 返回 success 但当场回读不到新值」：%s。"
                                "这是钉钉 AI 表格 job 表的**服务端最终一致性异常**（W-B 实测："
                                "同一批刚写过的记录，随后 34s/47s/156s 轮询全是旧值，约 4 分钟后"
                                "才读到正确值；W-C1 复测：有时数分钟后值确实落库，有时整批丢弃）。"
                                "**不要相信 success，也不要当场空转重试**——请在下一回合重跑同一条 "
                                "--apply 命令复核（幂等：按 record_id 覆盖写，不会重复建岗）"
                                % (len(still), still[:6]))
        bad = {}
        for fl in (res.get("failed") or []):
            bad[fl.get("record_id")] = str(fl.get("reason"))[:200]
        for m in meta:
            if m["rid"] in bad:
                rows.append({"seq": m["seq"], "file_name": m["label"], "result": "失败",
                             "reason": "补写语义字段已提交但当场回读不到新值（服务端写入传播"
                                       "延迟）；请重跑本步复核。原始原因：%s" % bad[m["rid"]]})
                summary["fail"] += 1
            else:
                got = "、".join(sorted(m["cells"].keys()))
                late = "（含传播延迟二次复核救回）" if m["rid"] in late_recovered else ""
                rows.append({"seq": m["seq"], "file_name": m["label"], "result": "已覆盖",
                             "reason": "Turn 2 归一化结果已批量写回（%s）并回读校验通过%s"
                                       % (got, late)})
                summary["overwrite"] += 1
    for w in tbl.warnings:
        if w not in warnings:
            warnings.append(w)

    draft = {"batch_id": batch_id, "generated_at": now_iso(), "config_path": tbl.config_path,
             "apply_source": str(path), "applied": len(updates),
             "verify": {k: v for k, v in res.items() if k != "records"},
             "jobs": [], "_warnings": warnings}
    return draft, rows, 0 if (not updates or res.get("verify_ok")) else 1


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    t_start = time.monotonic()
    batch_id = args.batch_id or _new_batch_id()
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
        Path("/tmp/recruit-fast") / batch_id
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "intake_report.json"
    draft_path = out_dir / "jobs_draft.json"

    counter = DwsCallCounter()
    client = DwsClient(counter=counter, timeout=300, http_timeout=180)

    mode = "apply" if args.apply else "turn1"
    print("== 岗位入库 %s（脚本内一次做完，零 agent 回合）=="
          % ("Turn 3：应用 LLM 归一化结果" if mode == "apply" else "Turn 1"), flush=True)
    print("batch_id=%s  out_dir=%s" % (batch_id, out_dir), flush=True)

    try:
        tbl = AITable(args.config, client=client)
    except Exception as exc:
        _write_json(report_path, {
            "ok": False, "elapsed_ms": int((time.monotonic() - t_start) * 1000),
            "dws_calls": counter.calls, "turns_saved_estimate": 0,
            "rows": [{"seq": 1, "file_name": str(args.config), "result": "失败",
                      "reason": "读取 config.json 失败：%s: %s" % (type(exc).__name__, exc)}],
            "summary": {"new": 0, "overwrite": 0, "skip": 0, "fail": 1,
                        "attachment_uploaded": 0, "attachment_failed": 0},
            "warnings": ["config.json 不可用：%s" % exc], "retry_count": 0})
        print("ARTIFACT:%s" % report_path, flush=True)
        return 1
    print("base=%s(%s)  表=%s(%s)" % (tbl.base_name, tbl.base_id,
                                      tbl.table_name("job"), tbl.table_id("job")), flush=True)

    if mode == "apply":
        draft, rows, rc = run_apply(args, tbl, counter, out_dir, batch_id)
    else:
        draft, rows, rc = run_turn1(args, tbl, counter, out_dir, batch_id)

    summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
               "attachment_uploaded": 0, "attachment_failed": 0}
    for r in rows:
        k = {"新入库": "new", "已覆盖": "overwrite", "跳过": "skip", "失败": "fail"}.get(r["result"])
        if k:
            summary[k] += 1
    if mode == "turn1":
        # 附件计数在 run_turn1 内部已算过，这里从 draft 的 jobs 里重算，避免丢失
        summary["attachment_uploaded"] = sum(1 for j in draft.get("jobs", [])
                                             if j.get("attachment_status") == "uploaded")
        summary["attachment_failed"] = sum(1 for j in draft.get("jobs", [])
                                           if j.get("attachment_status") == "failed")

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    n_files = len(args.files or []) if mode == "turn1" else len(rows)
    turns_saved = max(0, n_files * OLD_TURNS_PER_FILE - NEW_TURNS) if n_files else 0
    warnings = list(draft.get("_warnings") or [])
    report = {"ok": rc == 0 and bool(rows), "elapsed_ms": elapsed_ms,
              "dws_calls": counter.calls, "turns_saved_estimate": turns_saved,
              "rows": rows, "summary": summary, "warnings": warnings,
              "retry_count": counter.retries}
    draft["report"] = report
    # 契约 v3 §9#2：所有产物统一 D7 凭证口径「文件存在 且 ok==true」→ jobs_draft 顶层也带 ok
    draft["ok"] = bool(report.get("ok"))

    _write_json(report_path, report)
    if mode == "turn1":
        draft.pop("_warnings", None)
        _write_json(draft_path, draft)

    print("", flush=True)
    print("── 岗位入库结果 ──────────────", flush=True)
    print("序号 | 文件名 | 处理结果 | 说明", flush=True)
    icon = {"新入库": "✅ 新入库", "已覆盖": "✅ 已覆盖", "跳过": "⏭️ 跳过", "失败": "❌ 失败"}
    for r in rows:
        print("%d | %s | %s | %s" % (r["seq"], r["file_name"],
                                     icon.get(r["result"], r["result"]), r["reason"]), flush=True)
    print("────────────────────────", flush=True)
    print("小计：新入库 %d | 覆盖 %d | 跳过 %d | 失败 %d | JD附件已传 %d | JD附件失败 %d"
          % (summary["new"], summary["overwrite"], summary["skip"], summary["fail"],
             summary["attachment_uploaded"], summary["attachment_failed"]), flush=True)
    print("墙钟 %.2fs | dws_calls=%d（重试 %d）| 估算省下 %d 个 agent 回合"
          % (elapsed_ms / 1000.0, counter.calls, counter.retries, turns_saved), flush=True)
    if warnings:
        print("warnings %d 条（前 8 条）：" % len(warnings), flush=True)
        for w in warnings[:8]:
            print("  - %s" % str(w)[:220], flush=True)
    if mode == "turn1":
        print("jobs_draft → %s" % draft_path, flush=True)
        print("下一步：把 jobs_draft.json 交 Turn 2 的 agent 归一化"
              "（硬性门槛四项拆解 / 必备技能与加分项切分），产出 jobs_final.json 后用 "
              "--apply 写回", flush=True)
    print("ARTIFACT:%s" % report_path, flush=True)
    return 0 if report["ok"] else 1


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
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.files and not args.apply:
        print("错误：Turn 1 需要 --files；Turn 3 需要 --apply <jobs_final.json>",
              file=sys.stderr)
        return 2
    if not Path(args.config).expanduser().exists():
        print("错误：--config 不存在：%s" % args.config, file=sys.stderr)
        return 2
    try:
        return run(args)
    except KeyboardInterrupt:
        print("被用户中断", file=sys.stderr)
        return 130
    except Exception as exc:                        # 契约 D7：绝不静默早退
        import traceback
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
            Path("/tmp/recruit-fast") / (args.batch_id or _new_batch_id())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            names = [Path(f).name for f in (args.files or [])] or [str(args.apply or "")]
            _write_json(out_dir / "intake_report.json", {
                "ok": False, "elapsed_ms": 0, "dws_calls": 0, "turns_saved_estimate": 0,
                "rows": [{"seq": i + 1, "file_name": n, "result": "失败",
                          "reason": "脚本异常中止：%s: %s" % (type(exc).__name__, exc)}
                         for i, n in enumerate(names)],
                "summary": {"new": 0, "overwrite": 0, "skip": 0, "fail": len(names),
                            "attachment_uploaded": 0, "attachment_failed": 0},
                "warnings": [traceback.format_exc()[-1500:]], "retry_count": 0})
            print("ARTIFACT:%s" % (out_dir / "intake_report.json"), flush=True)
        except Exception:
            pass
        traceback.print_exc()
        return 1


def main_run(args: argparse.Namespace) -> int:
    """保留的薄封装（便于测试脚本直接调 run 时对齐命名）。"""
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
