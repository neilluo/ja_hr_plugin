# -*- coding: utf-8 -*-
"""soften.py — 归一化 + 软观察的唯一真源（把旧硬 schema 门槛降级为非阻断观察）。

设计规则（约束强度必须与违规的可逆性匹配）：
  本模块是"把曾经的硬 schema 门槛转成 归一化（normalize）+ 非阻断观察（observation）"
  的唯一真源。L2（审美/质量类）阈值——标签总长、中文字数、技能数量、文本长度——在此
  **仅作观察（OBSERVATION-ONLY）**，任何调用方禁止把它们用于丢行/退出/discard 决策。
  理由：一个超长技能标签是文本列里肉眼可见、随手可改的轻违规（可逆），
  而"整行精析产物被丢弃 → 队列卡死 → 阻塞匹配门禁 ~11 小时"是不可逆的重代价。
  历史事故：7 字中文术语"热镀铝锌硅钢板"触发旧字数硬门槛，整行（含一次扫描件读图
  推理）被 merge 丢弃。真正的 L0（确实无法写回）判定留在各链 validate_row。

- 归一化函数（join_list / coerce_int / coerce_str / normalize_segments）确定性、
  永不抛异常：能自动修复的一律修复而非报错（空行、半角竖线、缺段补"未提及"、
  数组→字符串、"1年"→1）。normalize_segments 幂等：已归一文本再过一遍原样返回、kinds=[]。
- SOFT_SPECS / soft_observations：数据驱动的软观察机制，简历链（skills-analyze）与
  JD 链（job-intake）共用；L2 阈值常量（TAG_LEN_MAX / ZH_RANGE / TAGS_RANGE）只在此
  一处（不变量 10），消费方一律 import 派生、禁止抄数值。
- 分词一律复用 vocab.toks（分隔符唯一真源，禁止本地再抄正则）。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vocab import toks  # noqa: E402  分词唯一真源，禁止本地抄 SEP 正则

# ── L2 阈值唯一真源（全部仅观察，绝不作丢弃/退出依据）────────────────────────
TAG_LEN_MAX = 12          # 单个标签总字符软上限
ZH_RANGE = (2, 6)         # 含中文字符时的中文字数软区间
TAGS_RANGE = (5, 12)      # 技能标签数量软目标区间
TAGS_MIN = TAGS_RANGE[0]  # 低于此数报 thin_tags（不足不硬凑，只观察）
# JD 链（job-intake）数量软区间——OBSERVATION-ONLY：仅供 jobs_analyze.observation_specs
# 组装 must_count_off / bonus_count_off 观察，任何调用方禁止用于丢行/退出决策。
JD_MUST_RANGE = (6, 10)   # 岗位必备技能数软目标
JD_BONUS_RANGE = (4, 8)   # 岗位加分项数软目标
# match 链（match-verify）文本长度软阈值——OBSERVATION-ONLY：仅供 match_analyze.merge
# 组装 overlong_evidence / overlong_analysis 观察，任何调用方禁止用于丢行/退出/截断决策。
EV_LEN_MAX = 80                # evidence（匹配依据）字符软上限
AI_ANALYSIS_RANGE = (150, 250)  # ai_analysis（AI匹配分析）字符软区间（只报上下越界）


def join_list(v, sep="、"):
    """把 list/tuple/str/dict 归一成字符串（或 None）。永不抛异常。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return sep.join(items) or None
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def coerce_int(v):
    """确定性 int-or-None 提取：None/bool→None，int→本身，float→四舍五入，
    str→取其中第一个整数（"1年"→1、"约4年"→4、"3年以上"→3、"多年"→None），其余→None。
    负数一律→None（本函数只服务年限/数量类计数，负值是无意义脏值；弱依赖改造后低质量值
    会照写回，故此处兜住，避免把 -2 这类写进表）。永不抛异常。"""
    n = _coerce_int_raw(v)
    return n if (n is not None and n >= 0) else None


def _coerce_int_raw(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return int(round(v))
        except Exception:  # noqa: BLE001  nan/inf 等病态值
            return None
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else None
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return None


def coerce_str(v):
    """归一成非空字符串或 None：str→strip（空→None），list/tuple→join_list，其余 str()。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (list, tuple)):
        return join_list(v)
    return str(v).strip() or None


def normalize_segments(text, seg_names, placeholder="未提及"):
    """把多段文本归一成"每段一行、段名｜内容、按 seg_names 定序补齐"的规范形态。

    返回 (text, kinds)；kinds 是修复动作的短标签列表：
      list_joined / dict_flattened / blank_lines / pipe_fixed / seg_filled / seg_merged
    修复而非报错：缺段补 placeholder（seg_filled）、多余段并入上一段内容（seg_merged）、
    半角 | 换全角 ｜（pipe_fixed）、空行剔除（blank_lines）。
    **输入整体为空时原样返回、不补任何占位段**：4 缺 1 时补该段是确定性修复（模型显然
    想写 5 段），而全空时补出 5 段"未提及"等于凭空编造一份"什么都没找到"的分析结论
    （可能只是读图失败），归一化不做发明。与 JD 链 _normalize_gates 的空值早退口径对齐。
    幂等：对已归一文本（含"多余段已并到末段"的不动点形态）再次调用，原样返回且 kinds=[]。
    """
    kinds = []
    raw = text
    if text is None:            # 先于 str() 兜住：否则 str(None) 会造出字面量 "None" 当正文
        return "", []
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(x) for x in text)
        kinds.append("list_joined")
    elif isinstance(text, dict):
        lines = ["%s｜%s" % (k, text[k]) for k in seg_names if k in text]
        lines += ["%s｜%s" % (k, v) for k, v in text.items() if k not in seg_names]
        text = "\n".join(lines)
        kinds.append("dict_flattened")
    elif not isinstance(text, str):
        text = str(text)
    stripped = [l.strip() for l in text.split("\n")]
    kept = [l for l in stripped if l]
    if not kept:            # 整体无内容：归一成空串，绝不凭空补占位段（见 docstring）
        return "", kinds    # 早于 blank_lines 判定：空串不动点必须 kinds=[]（幂等契约）
    if len(kept) < len(stripped):
        kinds.append("blank_lines")
    body = "\n".join(kept)
    if "|" in body:
        body = body.replace("|", "｜")
        kinds.append("pipe_fixed")
    parsed = []
    for l in (body.split("\n") if body else []):
        name, _, content = l.partition("｜")
        parsed.append([name.strip(), content.strip()])
    used = [False] * len(parsed)
    out_idx = {}          # parsed 行号 → 其命中的输出段序号
    for name in seg_names:
        idx = next((i for i, (n, _) in enumerate(parsed)
                    if not used[i] and n == name), None)
        if idx is None:   # 模糊兜底：段名互为包含（如"学历"↔"学历背景"）
            idx = next((i for i, (n, _) in enumerate(parsed)
                        if not used[i] and n and (name in n or n in name)), None)
        if idx is None:
            out_idx[name] = None   # 占位段（无来源行）
            continue
        used[idx] = True
        out_idx[name] = idx
    out = []
    src_of = []           # 输出段序号 → parsed 来源行号（None=占位）
    for name in seg_names:
        idx = out_idx[name]
        if idx is None:
            out.append([name, placeholder])
            src_of.append(None)
            kinds.append("seg_filled")
        else:
            out.append([name, parsed[idx][1] or placeholder])
            src_of.append(idx)
    # 多余行并入其"上一个已发出的段"（按源行位置判定）；之前没有匹配段则并入第一段
    extras = []
    for i, (p, u) in enumerate(zip(parsed, used)):
        if u:
            continue
        line = (p[0] + "｜" + p[1]) if p[1] else p[0]
        prev = max((j for j, s in enumerate(src_of)
                    if s is not None and s < i), default=None)
        extras.append((0 if prev is None else prev, line))
    if extras and out:
        for j, line in extras:
            out[j][1] += "\n" + line
        kinds.append("seg_merged")
    result = "\n".join("%s｜%s" % (n, c) for n, c in out)
    if extras and not out:   # seg_names 为空的退化情形：余行原样保留
        result = "\n".join(line for _, line in extras)
    if isinstance(raw, str) and result == raw:
        return result, []  # 不动点 = 已归一，幂等契约要求 kinds 清空
    return result, kinds


def over_len_words(words, zh_lo=ZH_RANGE[0], zh_hi=ZH_RANGE[1], len_max=TAG_LEN_MAX,
                   en_exempt=False):
    """返回值得人工过目的超长标签子集：含中文且中文字数不在 [zh_lo, zh_hi]，
    或（未豁免时）总字符 > len_max。这些阈值曾是丢行硬门槛，现降级为仅观察
    （见模块 docstring）。

    en_exempt 跟随各链 prompt 的声明（凡以文字向模型声明的约束，必须在代码里为真）：
    skills 链声明"纯英文/缩写术语不受字数限制、也没有字符数上限"→ 其 SOFT_SPECS 传
    en_exempt=True，纯英文/缩写一律不报；jobs 链声明"长英文术语照写、过长只记观察"
    → 用默认 False，纯英文超长仍报观察。机制唯一真源在本函数，各链语义以各自 prompt 为准。"""
    out = []
    for w in words:
        s = str(w)
        zh = sum(1 for ch in s if "一" <= ch <= "鿿")
        if not zh:
            if not en_exempt and len(s) > len_max:
                out.append(s)
        elif not (zh_lo <= zh <= zh_hi) or len(s) > len_max:
            out.append(s)
    return out


def count_out_of_range(words, lo, hi):
    """数量软区间检查：len(words) 在 [lo, hi] 内返回 []，否则返回 [实际数量]。"""
    n = len(words)
    return [] if lo <= n <= hi else [n]


# 数据驱动的软观察规格（key=观察名，field=面向的字段，check(row)→问题值列表）。
# 简历链直接取用；JD 链按自己的字段名复用同一套 helper 组装（阈值仍以此处为唯一真源）。
SOFT_SPECS = (
    {"key": "over_len_tags", "field": "skills",
     "check": lambda row: over_len_words(toks(row.get("skills")), en_exempt=True)},
    # 只报"不足"（thin）：数量偏多不是问题，偏少提示可能漏析——但都不阻断写回
    {"key": "thin_tags", "field": "skills",
     "check": lambda row: (lambda s: [len(s)] if 0 < len(s) < TAGS_MIN else [])(
         toks(row.get("skills")))},
)


def soft_observations(row, specs):
    """对一行跑全部软观察规格，返回 {key: [问题值]}（只含非空列表）。
    永不抛异常：单个 check 出错则该观察直接省略。返回值仅供报告，禁止作丢弃/退出依据。"""
    out = {}
    for spec in specs:
        try:
            vals = spec["check"](row)
        except Exception:  # noqa: BLE001  观察层绝不因自身缺陷影响主流程
            continue
        if vals:
            out[spec["key"]] = list(vals)
    return out
