# -*- coding: utf-8 -*-
"""岗位侧业务预判规则（纯函数对象，无 IO）。自 intake_job.py 逐字搬移（P9b）。

红线（P6 分析 §3.2 D9）：JobOrgPolicy 是 guess_org 的 **B 变体**——三段式
（①文件名/正文显式声明 ②部门+岗位名关键词 ③正文兜底）、关键词表 13/10 项 +
ORG_EXPLICIT、**low 置信度仍写库**（组织留空 = 该岗位匹配不到任何简历，留空是
硬功能故障）。简历侧 A 变体（两段式、11/5 项、low 不写库）在
shared/intake/pipeline.py，两者同名同签名形状但数据与策略相反，**禁止抽共同
基类共享关键词表**；共享的只有 count_hits 这个机械动作（textutil）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from jobintake.constants import (CITY_HINTS, LOCATION_FALLBACK, ORG_EXPLICIT,
                                 ORG_FUNC_KEYWORDS, ORG_MFG_KEYWORDS)
from jobintake.textutil import count_hits

__all__ = ["JobOrgPolicy", "JobLocationPolicy"]


class JobOrgPolicy:
    """岗位「组织分类」预判（B 变体，low → 仍写库）。"""

    MFG_KEYWORDS = ORG_MFG_KEYWORDS
    FUNC_KEYWORDS = ORG_FUNC_KEYWORDS
    EXPLICIT = ORG_EXPLICIT

    def guess(self, file_name: str, department: Optional[str], job_name: Optional[str],
              text: str) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """返回 (org, confidence, 判据明细)。

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
        explicit = [o for o in self.EXPLICIT if o in blob]
        info["explicit"] = explicit
        if len(explicit) == 1:
            declared = explicit[0]
            dept_hay = "%s %s" % (department or "", job_name or "")
            m = count_hits(dept_hay, self.MFG_KEYWORDS)
            f = count_hits(dept_hay, self.FUNC_KEYWORDS)
            info.update({"dept_mfg": m, "dept_func": f})
            kw = "制造中心" if (m and not f) else ("职能中心" if (f and not m) else None)
            info["dept_keyword"] = kw
            if kw is None or kw == declared:
                return declared, "high", info
            return declared, "low", info      # 显式声明 vs 部门关键词打架 → 交 Turn 2 复核
        if len(explicit) > 1:
            info["note"] = "文件名/正文同时出现 %s，无法确定" % explicit
            return None, "low", info

        dept_hay = "%s %s %s" % (department or "", job_name or "", file_name or "")
        m = count_hits(dept_hay, self.MFG_KEYWORDS)
        f = count_hits(dept_hay, self.FUNC_KEYWORDS)
        info.update({"dept_mfg": m, "dept_func": f})
        if m and not f:
            return "制造中心", "high", info
        if f and not m:
            return "职能中心", "high", info
        hay2 = "%s\n%s" % (dept_hay, (text or "")[:4000])
        m2, f2 = count_hits(hay2, self.MFG_KEYWORDS), count_hits(hay2, self.FUNC_KEYWORDS)
        info.update({"text_mfg": m2, "text_func": f2})
        if m2 and not f2:
            return "制造中心", "high", info
        if f2 and not m2:
            return "职能中心", "high", info
        if m2 or f2:
            return ("制造中心" if m2 > f2 else ("职能中心" if f2 > m2 else None)), "low", info
        return None, "low", info


class JobLocationPolicy:
    """工作地点（multipleSelect）预判：文件名优先（客户命名含城市），正文兜底。"""

    CITY_HINTS = CITY_HINTS
    FALLBACK = LOCATION_FALLBACK

    def guess(self, file_name: str, text: str, known: Sequence[str]) -> List[str]:
        out: List[str] = []
        pool = list(dict.fromkeys(list(known or []) + list(self.CITY_HINTS)))
        for hay in (file_name or "", (text or "")[:2500]):
            for c in pool:
                if c and c in hay and c not in out:
                    out.append(c)
            if out:
                break
        # 「曲靖制造基地/曲靖基地」只应产出一个「曲靖」；「不限」与其它城市互斥
        if self.FALLBACK in out and len(out) > 1:
            out.remove(self.FALLBACK)
        return out[:6]
