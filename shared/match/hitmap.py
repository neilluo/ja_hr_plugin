# -*- coding: utf-8 -*-
"""命中项归一与映射（HitMapper / GateVerdictReader）：verify 侧的比对基础设施。

原 verify_decisions.py 的 `norm_item`(L149) / `gate_verdict`(L160) / `as_str_list`(L202)
/ `squash`(L215) / `map_hits_to_items`(L227) / `dedupe_norm`(L955) 搬入。
`norm_item` 是**冻结出口**（旧 apply_decisions L77 经 verify_decisions import；
P9a 起 apply 侧改经本模块取用，verify 入口留同名薄壳）。

⚠️ 本模块的归一化（norm_item / squash）是**命中比对**口径，与 build 侧
match/tablevalues.clean_ws（digest 组装口径）regex 与用途都不同，禁止混用。
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: gate_verdict 的判定词表（原 verify_decisions._PASS_LIKE / _FAIL_LIKE，L56-57 冻结）
PASS_LIKE = ("pass", "passed", "达标", "符合", "满足", "yes", "true", "y", "✓", "✅")
FAIL_LIKE = ("fail", "failed", "不达标", "不符", "不满足", "no", "false", "n", "✗", "❌")

_SQUASH_DROP = re.compile(r"[\s\u3000、，,;；.。:：/／\-—_()（）\[\]【】\"'“”‘’]+")


def as_str_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [v]


class HitMapper:
    """技能命中项比对：归一化 + 「映射不上=编造」的三条映射规则（契约 D16）。"""

    def norm_item(self, s: Any) -> str:
        """技能条目比对用归一化：去空白/全角空格/末尾标点、转小写。"""
        if s is None:
            return ""
        if isinstance(s, dict):
            s = s.get("name") or s.get("text") or s.get("value") or ""
        s = re.sub(r"[\s\u3000]+", "", str(s))
        s = s.strip("，,、;；.。:：")
        return s.lower()

    def squash(self, s: Any) -> str:
        """比对用最强归一化：去掉所有空白与中英文标点、转小写。

        为什么需要它（实测坑）：W-A 的 `must_skills` 切分粒度比 JD 原文细——
        「对应工序生产设备的结构、原理及运维规范」被按顿号切成 2 条，模型在 skill_hits 里
        很自然的把它**合回一条**引用。严格逐字 ⊆ 会把这种「合并引用」误判成编造，
        实测 8人×19岗 一批里 3/10 条 pass 因此被判无效（白白丢掉 3 条匹配记录）。
        所以先做**可解释的归一化映射**，映射不上才算编造。
        """
        return _SQUASH_DROP.sub("", str(s or "")).lower()

    def dedupe_norm(self, items: Sequence[Any]) -> List[str]:
        """按归一化去重，保留原词（分子不能靠重复命中虚增）。"""
        seen, out = set(), []
        for it in items:
            k = self.norm_item(it)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it if isinstance(it, str) else str(it))
        return out

    def map_hits_to_items(self, hits: Sequence[Any],
                          items: Sequence[Any]) -> Tuple[List[Any], List[Dict[str, Any]]]:
        """把模型给的命中项映射回岗位 must_skills/bonus_skills 的**原文条目**。

        三条映射规则（任一命中即认为不是编造）：
          a) 归一化后与某一条目完全相等；
          b) 归一化后等于**连续若干条**目的拼接（模型把被切碎的条目合回一句）；
          c) 归一化后与某一条目互为子串且长度占比 ≥0.6（模型做了缩写/同义改写）。
        映射不上 → 该项算编造（D16：越界即判该条无效并进 warnings）。

        返回 (映射后的原文条目列表, 归一化明细)。
        """
        items = list(items or [])
        sq_items = [self.squash(x) for x in items]
        exact: Dict[str, int] = {}
        for i, s in enumerate(sq_items):
            if s and s not in exact:
                exact[s] = i
        out: List[Any] = []
        picked = set()
        detail: List[Dict[str, Any]] = []
        for h in hits:
            sq = self.squash(h)
            if not sq:
                continue
            idxs: List[int] = []
            rule = None
            if sq in exact:                                   # a) 精确
                idxs, rule = [exact[sq]], "exact"
            else:
                for st in range(len(sq_items)):                # b) 连续拼接
                    acc = ""
                    cov = []
                    for k in range(st, len(sq_items)):
                        acc += sq_items[k]
                        cov.append(k)
                        if acc == sq:
                            idxs, rule = cov, "join_consecutive"
                            break
                        if len(acc) > len(sq):
                            break
                    if idxs:
                        break
                if not idxs:                                   # c) 子串（占比 ≥0.6）
                    best, best_ratio = None, 0.0
                    for i, s in enumerate(sq_items):
                        if not s:
                            continue
                        if sq in s or s in sq:
                            ratio = min(len(sq), len(s)) / float(max(len(sq), len(s)))
                            if ratio > best_ratio:
                                best, best_ratio = i, ratio
                    if best is not None and best_ratio >= 0.6:
                        idxs, rule = [best], "substring(%.2f)" % best_ratio
            if not idxs:
                detail.append({"original": h, "mapped_to": None, "rule": "fabricated"})
                continue
            for i in idxs:
                if i not in picked:
                    picked.add(i)
                    out.append(items[i])
            if rule != "exact":
                detail.append({"original": h, "rule": rule,
                               "mapped_to": [items[i] for i in idxs]})
        return out, detail


class GateVerdictReader:
    """gate_detail 值 → True(达标)/False(不达标)/None(读不懂)。"""

    def verdict(self, v: Any) -> Optional[bool]:
        """把 gate_detail 的值归一成 True(达标)/False(不达标)/None(读不懂)。"""
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        s = re.sub(r"[\s\u3000]+", "", str(v)).lower()
        if not s:
            return None
        for t in FAIL_LIKE:
            if s.startswith(t.lower()):
                return False
        for t in PASS_LIKE:
            if s.startswith(t.lower()):
                return True
        return None
