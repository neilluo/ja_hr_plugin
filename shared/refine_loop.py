# -*- coding: utf-8 -*-
"""refine_loop.py — AI 精析队列唯一真源：队列谓词 / 队列计数 / 周期锁 / 触发时刻（fire_at）。

背景：三列精析（skills/ai_extract/ai_deep 与岗位三列）是匹配的前置而非上传的前置，
上传链路写完表即返回；精析消费方 = 上传后 agent 注册的一次性消费任务（注册时刻取上传报告字段
refine_fire_at，延迟秒数唯一真源 = 本文件 REFINE_DELAY_S；触发纪律见
skills/resume-intake/SKILL.md）+ 每日 03:00 兜底巡检。无看门狗层：崩溃恢复靠
"未打标记录仍在队列 + 租约过期自动接管 + 兜底重吃"，理由见 resume-intake SKILL 裁撤记录。
队列状态不靠"三列是否为空"推断（岗位三列入库即有正则粗值、推断必失效），
而以显式标记列 ai_refined_at（config.fields，type date 毫秒）为准：空 = 在队列。

队列谓词（唯一真源，消费方禁止抄副本）：
  resume：ai_refined_at 为空 且（full_text 非空 或 source_file 指向的原件存在）
          （扫描件 full_text 为空，靠 source_file 入队——精析 subagent 直接读本地原件出图；
           原件已被删除时谓词为假，不入队也不反复重吃，否则一条坏记录会永久阻塞 match_gated 门禁。
           代价是该记录三列留空，prepare 会以 unrefinable 字段单独报出供人工知晓）
  job   ：ai_refined_at 为空 且 responsibilities 非空

打标记的责任方（写 ai_refined_at 与三列同一次 update）：
  skills_apply.py（简历三列，含扫描件读图）、sync_job_columns.py（岗位三列）。

match_gated 前置门禁经 queue_counts() 判断：队列非空 → exit 2 拒绝粗值打分（--force 逃生）。

周期租约（防并发双写）：prepare 获取 outputs/refine_<chain>.lock（resume/job 两链各自独立、
可并行），写回端（skills_apply / sync_job_columns）完成后释放；租约跨进程存活（prepare 进程即退，
PID 无法判活，故以 mtime 新鲜度为凭），30 分钟内视为有周期在跑、后来者 exit 2 秒退。
崩溃遗留的租约随过期自然失效，队列项由下一周期（新即时任务/03:00 兜底）重吃；
prepare 切出 0 条时即时自释（并发周期已吃空，不留僵尸租约）。
--force = 夺回自有租约（同周期内重切批用）。
"""
import json
import os
import time

# 队列判定只需标记列、文本列与原件路径列，禁止拉全字段。
# name 不参与判定，但 unrefinable 报告须带姓名，否则人工拿到一条只有 id 的记录无从处理。
QUEUE_BIZ = {"resume": ["ai_refined_at", "full_text", "source_file", "name"],
             "job": ["ai_refined_at", "responsibilities"]}
_TEXT_KEY = {"resume": "full_text", "job": "responsibilities"}
TABLES = ("resume", "job")

# 精析消费任务的触发延迟（秒）：唯一真源，resume/job 两条上传链共用（AGENTS.md 不变量 10/11）。
# 各 SKILL.md 与 prompt 一律只引用报告字段 refine_fire_at，禁止复述本数字。
REFINE_DELAY_S = 45


def fire_at(delay=REFINE_DELAY_S):
    """注册精析消费任务的目标时刻：当前 +delay 秒，UTC ISO8601 秒级带 Z（cron schedule.at 直接可用）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))


def refinable(fields, table):
    """是否存在可读的信息源：全文文本，或盘上真实存在的原件（扫描件）。
    入队判定与 --all/--ids 过滤共用此唯一判据——绕过它把无信息源的记录喂给 subagent，
    只会得到编造或"未提及"，写回即覆盖已有好数据（旧手析扫描件 full_text 与 source_file 皆空）。"""
    if (fields.get(_TEXT_KEY[table]) or "").strip():
        return True
    if table != "resume":
        return False
    src = (fields.get("source_file") or "").strip()
    return bool(src) and os.path.isfile(src)


def _is_queued(fields, table):
    if fields.get("ai_refined_at"):
        return False
    return refinable(fields, table)


def unrefinable(fields, table):
    """坏记录：原件路径有值但文件已不存在（或不可读）。
    不入队（否则永久卡队列、天天重吃并阻塞 match_gated 门禁），但必须由 prepare 报出，
    否则三列静默留空无人知晓。仅 resume 链有意义。
    判据复用 refinable()，禁止在此重抄 full_text/source_file 的存在性逻辑（不变量 10）。
    注：既无全文又无路径的记录（如旧一代手析扫描件）不在此列——它无从补救，也不该刷屏；
    但 --all/--ids 显式要求重析时会被 refinable() 挡下并另行报出（语义不同，非口径漂移）。"""
    if table != "resume" or fields.get("ai_refined_at"):
        return False
    return bool((fields.get("source_file") or "").strip()) and not refinable(fields, table)


def queue(nt, table):
    """返回待精析记录列表（含 id 与 QUEUE_BIZ 字段）。"""
    if table not in TABLES:
        raise ValueError("未知表 %r（可用 %s）" % (table, list(TABLES)))
    rows = nt.list_records(table, biz_fields=QUEUE_BIZ[table])
    return [r for r in rows if _is_queued(r["fields"], table)]


def queue_with_unrefinable(nt, table):
    """(待精析行, 坏记录行)：谓词与其补集同处一源，禁止消费方各抄一半。"""
    if table not in TABLES:
        raise ValueError("未知表 %r（可用 %s）" % (table, list(TABLES)))
    rows = nt.list_records(table, biz_fields=QUEUE_BIZ[table])
    return ([r for r in rows if _is_queued(r["fields"], table)],
            [r for r in rows if unrefinable(r["fields"], table)])


def queue_counts(nt):
    """{"resume": n, "job": m}：match 门禁与上传报告共用。"""
    return {t: len(queue(nt, t)) for t in TABLES}


def lock_path(outdir, chain="resume"):
    """租约文件与 pending/done 批次同放 outputs/，按链独立：resume 与 job 队列不相交，
    只需防同链并发双写，两链可并行跑。放 OUTDIR 而非硬编码 ROOT 亦便于测试重定向隔离。"""
    return os.path.join(outdir, "refine_%s.lock" % chain)


def acquire_lock(outdir, chain="resume", stale_after=1800, force=False):
    """周期租约：防同一条精析链被两个周期并发双写。返回锁路径或 None（租约被活周期占用）。

    force=True：夺回租约（同周期内重新 prepare 切批时用，覆盖锁文件刷新租约）。"""
    p = lock_path(outdir, chain)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p) and not force:
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
