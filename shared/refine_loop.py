# -*- coding: utf-8 -*-
"""refine_loop.py — AI 精析队列唯一真源：队列谓词 / 队列计数 / 周期锁。

背景：三列精析（skills/ai_extract/ai_deep 与岗位三列）是匹配的前置而非上传的前置，
上传链路写完表即返回；精析由后台周期（事件触发的一次性任务 + 每日兜底巡检）消费队列。
队列状态不靠"三列是否为空"推断（岗位三列入库即有正则粗值、推断必失效），
而以显式标记列 ai_refined_at（config.fields，type date 毫秒）为准：空 = 在队列。

队列谓词（唯一真源，消费方禁止抄副本）：
  resume：ai_refined_at 为空 且 full_text 非空
          （扫描件 full_text 为空不入队，防 agent 拿空文本精析覆盖 OCR 手写字段；
           OCR backfill 写回时直接打标记，手析视同精析）
  job   ：ai_refined_at 为空 且 responsibilities 非空

打标记的责任方（写 ai_refined_at 与三列同一次 update）：
  skills_apply.py（简历三列）、sync_job_columns.py（岗位三列）、resume-intake OCR backfill。

match_gated 前置门禁经 queue_counts() 判断：队列非空 → exit 2 拒绝粗值打分（--force 逃生）。
"""
import json
import os
import time

# 队列判定只需标记列与文本列，禁止拉全字段
QUEUE_BIZ = {"resume": ["ai_refined_at", "full_text"],
             "job": ["ai_refined_at", "responsibilities"]}
_TEXT_KEY = {"resume": "full_text", "job": "responsibilities"}
TABLES = ("resume", "job")


def _is_queued(fields, table):
    if fields.get("ai_refined_at"):
        return False
    return bool((fields.get(_TEXT_KEY[table]) or "").strip())


def queue(nt, table):
    """返回待精析记录列表（含 id 与 QUEUE_BIZ 字段）。"""
    if table not in TABLES:
        raise ValueError("未知表 %r（可用 %s）" % (table, list(TABLES)))
    rows = nt.list_records(table, biz_fields=QUEUE_BIZ[table])
    return [r for r in rows if _is_queued(r["fields"], table)]


def queue_counts(nt):
    """{"resume": n, "job": m}：match 门禁与上传报告共用。"""
    return {t: len(queue(nt, t)) for t in TABLES}


def lock_path(root):
    return os.path.join(root, "outputs", "refine.lock")


def acquire_lock(root, stale_after=3600):
    """周期锁：防两个精析周期并发双写。返回锁路径或 None（已被活锁占用）。"""
    p = lock_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        try:
            if time.time() - os.path.getmtime(p) < stale_after:
                return None
        except OSError:
            pass
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": int(time.time() * 1000)}, f)
    return p


def release_lock(path):
    if path and os.path.exists(path):
        os.remove(path)
