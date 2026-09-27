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


def read_done(outdir, prefix):
    """扫描 done part 逐批解析合并。返回 (rows, missing_batches)：
    缺失或解析失败的批次进 missing 并打印告警，其余照常返回（部分失败不整体失败）。
    总批次数用 count_pending(outdir, prefix) 另取。"""
    rows, missing = [], []
    for i in range(1, count_pending(outdir, prefix) + 1):
        p = done_path(outdir, prefix, i)
        if not os.path.exists(p):
            missing.append(i)
            continue
        try:
            rows.extend(json.load(open(p, encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            missing.append(i)
            print("批次%d解析失败: %s" % (i, str(e)[:120]))
    return rows, missing
