# -*- coding: utf-8 -*-
"""analyze_parts.py — 三条精析流水线（skills/jobs/match analyze）的公共切批/合并骨架。

parts 文件命名与清理逻辑的唯一真源（不变量 10）：
  <prefix>_pending_part<N>.json / <prefix>_done_part<N>.json / <prefix>_analyze_meta.json，
prefix 由调用方参数化（skills / jobs / match），流水线脚本禁止再抄命名或清旧文件逻辑。
各流水线只保留差异：候选来源、item 构造、merge 后的聚合结构。
批大小规划唯一真源仍是 shared/waves.py（agent 数硬上限 MAX_AGENTS）。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waves import plan, MAX_AGENTS  # noqa: E402


def pending_path(outdir, prefix, i):
    return os.path.join(outdir, "%s_pending_part%d.json" % (prefix, i))


def done_path(outdir, prefix, i):
    return os.path.join(outdir, "%s_done_part%d.json" % (prefix, i))


def meta_path(outdir, prefix):
    return os.path.join(outdir, "%s_analyze_meta.json" % prefix)


def count_pending(outdir, prefix):
    n = 0
    while os.path.exists(pending_path(outdir, prefix, n + 1)):
        n += 1
    return n


def write_parts(outdir, prefix, items, meta_extra, batch=None):
    """清旧 pending/done → waves.plan 切批 dump → 写 meta 并打印，返回 meta。

    meta_extra 放各流水线自有计数（如 total/queued/total_pairs），公共键
    batches/batch_size/agents/agent_sizes/max_agents 由本函数补齐。
    """
    os.makedirs(outdir, exist_ok=True)
    batch, groups = plan(len(items), batch)     # agent 数超上限时自动加大 batch
    parts = [items[g[0] - 1:g[-1]] for g in groups]
    for old in os.listdir(outdir):
        if old.startswith("%s_pending_part" % prefix) or old.startswith("%s_done_part" % prefix):
            os.remove(os.path.join(outdir, old))
    for i, p in enumerate(parts, 1):
        json.dump(p, open(pending_path(outdir, prefix, i), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    meta = dict(meta_extra)
    meta.update({"batches": len(parts), "batch_size": batch,
                 "agents": len(parts), "agent_sizes": [len(p) for p in parts],
                 "max_agents": MAX_AGENTS})
    json.dump(meta, open(meta_path(outdir, prefix), "w", encoding="utf-8"))
    print(json.dumps(meta, ensure_ascii=False))
    return meta


def write_dispatch(outdir, prefix, batches, key="parts"):
    """分派清单落盘 outputs/<prefix>_dispatch.json = {batches:N, key:[真实 pending 路径]}，
    返回清单路径。agent 复制真实路径指针分派，不再凭记忆手拼 *_pending_part<N>.json
    （jobs/match 两流水线同机制；skills_analyze 的 skills_dispatch.json 是 prompts 形态、走 render_prompts）。"""
    path = os.path.join(outdir, "%s_dispatch.json" % prefix)
    json.dump({"batches": batches,
               key: [pending_path(outdir, prefix, i) for i in range(1, batches + 1)]},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return path


def _top_level_objects(text):
    """把 JSON 数组文本按**顶层对象**切成若干 "{...}" 片段（括号深度在字符串外统计，
    字符串内的 {} 与转义引号不误算）。行级抢救的取材层：一条坏记录不该带走同批好记录。"""
    spans, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append(text[start:i + 1])
                    start = None
    return spans


_ID_RE = re.compile(r'"(?:id|job_id|rid)"\s*:\s*"([^"]*)"')


def load_done(path):
    """读一个 done part，返回 (rows, errors)：errors 为 [{"id":..., "reason":...}]。
    整文件合法 JSON → 一次成功、errors 空；解析失败则退到行级抢救（_top_level_objects）
    逐对象解析，坏的只丢那一条，并把它的 id 尽量带进 error 供报告与补发定位。
    存在的理由：subagent 产物是模型写的 JSON，一个未转义引号就会让整批（含一次扫描件读图
    推理）凭空作废——2026-09-29 实测批次 9 因此整批重跑（~2 分钟），且 merge 报 merged=29
    掩盖了"其中一条其实完好"。抢救把损失从"整批"降到"坏的那一条"。
    判废口径由调用方决定（read_done/done_integrity 同用本函数，口径不分叉）。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:  # noqa: BLE001
        return [], [{"id": None, "reason": "读取失败: %s" % str(e)[:80]}]
    try:
        obj = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        rows, errors = [], []
        for frag in _top_level_objects(raw):
            try:
                rows.append(json.loads(frag))
            except Exception as e2:  # noqa: BLE001
                m = _ID_RE.search(frag)
                errors.append({"id": m.group(1) if m else None,
                               "reason": "记录不可解析: %s" % str(e2)[:80]})
        if not rows and not errors:                       # 连一个对象都没切出来 = 整批废
            errors.append({"id": None, "reason": "整批不可解析: %s" % str(e)[:80]})
        return rows, errors
    return (obj if isinstance(obj, list) else [obj]), []


def read_done(outdir, prefix):
    """扫描 done part 逐批解析合并。返回 (rows, missing_batches)：
    整批不可用（文件不存在，或抢救后一行都不剩）进 missing 并打印告警，其余照常返回
    （部分失败不整体失败）。总批次数用 count_pending(outdir, prefix) 另取。
    单条坏记录经 load_done 行级抢救后不再带走同批好行；坏的那条由调用方的
    done_integrity 按 id 集合不一致判入 bad_batches（补发该批），故此处不重复报它。"""
    rows, missing = [], []
    for i in range(1, count_pending(outdir, prefix) + 1):
        p = done_path(outdir, prefix, i)
        if not os.path.exists(p):
            missing.append(i)
            continue
        got, errors = load_done(p)
        if not got:
            missing.append(i)
            print("批次%d解析失败: %s" % (i, errors[0]["reason"][:120]))
            continue
        if errors:
            print("批次%d部分损坏: 抢救 %d 行，坏 %d 行（%s）"
                  % (i, len(got), len(errors),
                     "、".join(str(x["id"]) for x in errors)))
        rows.extend(got)
    return rows, missing
