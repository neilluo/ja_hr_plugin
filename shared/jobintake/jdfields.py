# -*- coding: utf-8 -*-
"""JD 语义字段转换 + 技能粒度护栏（纯函数/纯规则，无 IO）。

红线：
  * split_skill_items（原 _as_skill_items）的分隔符集是「、,，;；\\n」6 个，
    **禁止**换成 shared/fields/textnorm.split_items（后者分隔符更多且会剥动词
    前缀/过滤/去重——粒度护栏要的是 Turn 2 给的**原样逐条**，换了会让「过长」
    护栏永不触发，warnings 文本变 → 产物字节变）。
  * norm_skill_item 与 match-verify 的 verify_decisions.norm_item 同口径，但
    跨脚本合并属另一刀，本包不 import match 侧。
  * SkillGranularityGuard 只 append warning **不拦写**；告警文案逐字进
    intake_report.json 的 warnings（裁判 REPORT 面）。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from jobintake.constants import (HARD_GATE_KEYS, HARD_GATES_TEXT_MAX,
                                 SKILL_ITEM_TOO_LONG_MINLEN,
                                 SKILL_ITEM_TOO_SHORT_MAXLEN, SOFT_SKILL_BLACKLIST)
from jobintake.textutil import clean

__all__ = ["compose_hard_gates", "as_text_list", "split_skill_items",
           "norm_skill_item", "SkillGranularityGuard"]


def compose_hard_gates(hg: Any) -> Optional[str]:
    """把 §3.3 的 hard_gates 四项对象拼成表里的 text 值（岗位JD表「硬性门槛」是 text）。"""
    if hg is None:
        return None
    if isinstance(hg, str):
        return clean(hg)
    if isinstance(hg, dict):
        labels = {"education": "学历", "major": "专业", "years": "经验年限",
                  "certificates": "证书"}
        bits = []
        for k in HARD_GATE_KEYS:
            v = clean(hg.get(k))
            if v:
                bits.append("%s：%s" % (labels.get(k, k), v))
        for k, v in hg.items():                      # LLM 可能多给项（如年龄），一并保留
            if k in HARD_GATE_KEYS:
                continue
            v = clean(v)
            if v:
                bits.append("%s：%s" % (k, v))
        s = "；".join(bits)
        return clean(s[:HARD_GATES_TEXT_MAX])
    if isinstance(hg, (list, tuple)):
        return clean("；".join(str(x) for x in hg if x)[:HARD_GATES_TEXT_MAX])
    return None


def as_text_list(v: Any) -> Optional[str]:
    """必备技能/加分项在表里是 text（打分母）：list → 「、」连接，str 原样。"""
    if v is None:
        return None
    if isinstance(v, str):
        return clean(v)
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return clean("、".join(items))
    return clean(str(v))


def split_skill_items(raw: Any) -> List[Any]:
    """把 jobs_final.json 里的 must_skills/bonus_skills 取成**逐条**列表（list 原样，
    str 按「、，,;；\\n」切）。粒度护栏要逐条查，不能用 as_text_list 连接后的串。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [x for x in raw]
    if isinstance(raw, str):
        return [x for x in re.split(r"[、,，;；\n]+", raw)]
    return [raw]


def norm_skill_item(s: Any) -> str:
    """技能条目粒度比对用归一化：去空白/全角空格/首尾标点、转小写（与 verify 的 norm_item 同口径）。"""
    if s is None:
        return ""
    if isinstance(s, dict):
        s = s.get("name") or s.get("text") or s.get("value") or ""
    s = re.sub(r"[\s\u3000]+", "", str(s))
    return s.strip("，,、;；.。:：").lower()


class SkillGranularityGuard:
    """JD 技能条目粒度护栏（只增 warning 不拦写）。"""

    def check(self, jobs_skills: Sequence[Tuple[str, Any, Any]],
              warnings: List[str],
              blacklist: Sequence[str] = SOFT_SKILL_BLACKLIST,
              short_maxlen: int = SKILL_ITEM_TOO_SHORT_MAXLEN,
              long_minlen: int = SKILL_ITEM_TOO_LONG_MINLEN) -> int:
        """逐条检查 must_skills/bonus_skills 的粒度，命中可疑形态 → 记 warning（不拦写）。

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
                items = [x for x in split_skill_items(raw) if str(x).strip()]
                norms = [norm_skill_item(x) for x in items]
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
