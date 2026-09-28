# -*- coding: utf-8 -*-
"""refine_loop.py — AI 精析队列唯一真源：队列谓词 / 队列计数 / 周期锁 / 触发时刻（fire_at）/ 任务规格。

背景：三列精析（skills/ai_extract/ai_deep 与岗位三列）是匹配的前置而非上传的前置，
上传链路写完表即返回；精析消费方 = 上传后 agent 注册的一次性消费任务（注册规格由本文件
consume_task_spec 产出、经上传报告的 cron_job 字段带出，agent 只透传；延迟唯一真源 =
REFINE_DELAY_S，触发纪律见 skills/resume-intake/SKILL.md）+ 每日兜底巡检（时刻见
FALLBACK_CRON / fallback_hhmm()，规格由 fallback_task_spec 产出）。无看门狗层：崩溃恢复靠
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
崩溃遗留的租约随过期自然失效，队列项由下一周期（新即时任务/每日兜底巡检）重吃；
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
# 取值必须吃得下"脚本返回 → agent 发出注册请求"的模型往返（实测单回合 25-30s）：
# 曾取 45s，单份小文件秒回时窗口被 agent 往返吃光，注册被"Scheduled time must be in the future"
# 拒收，反要多花 3 个回合重算时刻。代价（三列晚几分钟填好）对不阻塞人的异步链路是免费的。
REFINE_DELAY_S = 180

# 一次性消费任务名前缀 / 每日兜底任务名（唯一真源）：兜底 cron 的自清理按前缀匹配已消费完的
# 一次性任务，前缀与兜底规格在此定义一次、由本文件 CLI 产出，禁止在 cron payload 里手抄副本。
TASK_PREFIX = {"resume": "简历精析消费", "job": "岗位JD精析消费"}
FALLBACK_NAME = "招聘精析队列兜底巡检"
FALLBACK_CRON = "30 9 * * *"        # 每日 09:30：白天工作时段，电脑通常在开机状态
FALLBACK_TZ = "Asia/Shanghai"

# 两条链的消费入口与写回入口（唯一真源，供 payload 生成；SKILL.md 只引用不复述命令原文）。
_CHAIN = {
    "resume": {
        "queue_cmd": "python3 skills/skills-analyze/scripts/skills_analyze.py queue",
        "skill": "skills/skills-analyze/SKILL.md",
        "writeback": "skills_apply.py",
    },
    "job": {
        "queue_cmd": "python3 skills/job-intake/scripts/jobs_analyze.py queue",
        "skill": "skills/job-intake/SKILL.md",
        "writeback": "sync_job_columns.py",
    },
}


def repo_root():
    """仓库根 = shared/ 的上一级；消费任务的 contextDirs 与"以仓库根为 CWD"均由此派生。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def delay_human():
    """REFINE_DELAY_S 的人话表述：汇报里"约 N 分钟后自动精析"由此派生。
    禁止在 SKILL.md / cron payload / agent 措辞里手抄分钟数（改延迟必漏一处）。"""
    if REFINE_DELAY_S % 60 == 0:
        return "%d 分钟" % (REFINE_DELAY_S // 60)
    return "%d 秒" % REFINE_DELAY_S


def fallback_hhmm():
    """每日兜底巡检的 HH:MM（由 FALLBACK_CRON 派生）：汇报里"最迟次日 HH:MM 兜底"与
    任务描述共用此函数，禁止各处手抄时刻字面量。"""
    minute, hour = FALLBACK_CRON.split()[:2]
    return "%s:%s" % (hour.zfill(2), minute.zfill(2))


def fire_at(delay=REFINE_DELAY_S):
    """注册精析消费任务的目标时刻：当前 +delay 秒，UTC ISO8601 秒级带 Z（cron schedule.at 直接可用）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))


def _consume_message(chain, name, root):
    """一次性消费任务的 payload 指令：只写"跑什么命令、按哪份 SKILL、结束时自删"，
    不复述流水线细节（细节唯一真源在各 SKILL.md，此处复述即双源、且必与文档漂移）。"""
    c = _CHAIN[chain]
    return (
        "在仓库 %s（以仓库根为 CWD）消费%s精析队列：\n"
        "1. 跑 `%s`；该链计数为 0 则静默结束，不要通知任何人。\n"
        "2. 非空则按 %s 的流水线全自动执行到 %s 写回并打 ai_refined_at 出队标记："
        "prepare → 同一条消息一次性并发发出全部 subagent（不分波、不串行，上限见 shared/waves.py）"
        "→ 每批 prompt 只写一行 pending 提示词文件路径（禁止内联提示词原文）→ merge → 写回。"
        "prepare 返回 refused（撞活租约）即退出、禁止抢跑；不要向用户提问、不要等待确认。\n"
        "3. 结束时（含第 1 步队列为 0 的静默结束），按任务名「%s」删除本一次性任务自身。"
        % (root, "简历" if chain == "resume" else "岗位JD", c["queue_cmd"], c["skill"],
           c["writeback"], name)
    )


def consume_task_spec(chain, at=None, root=None):
    """一次性精析消费任务的完整注册规格（定时任务管理工具 add 的 job 入参，可原样使用）。

    存在理由：agent 手写这份 payload 曾一次吃掉 25s 模型思考，且手抄的任务名前缀与流水线细节
    与代码/文档构成双源。规格改由代码产出后，注册动作退化为"把 cron_job 原样传给工具"。"""
    if chain not in _CHAIN:
        raise ValueError("未知链 %r（可用 %s）" % (chain, list(_CHAIN)))
    root = root or repo_root()
    name = "%s-%s" % (TASK_PREFIX[chain], time.strftime("%Y%m%d-%H%M%S"))
    return {
        "name": name,
        "description": "消费%s精析队列（入库脚本触发注册，完成即自删）"
                       % ("简历" if chain == "resume" else "岗位JD"),
        "schedule": {"kind": "at", "at": at or fire_at()},
        "payload": {"kind": "agentTurn", "message": _consume_message(chain, name, root),
                    "contextDirs": [root]},
        "missedRunPolicy": "run_latest",
    }


def fallback_task_spec(root=None):
    """每日兜底巡检任务的完整注册规格：唯一崩溃恢复层 + 一次性消费任务的自清理兜底网。
    自清理的任务名前缀取自 TASK_PREFIX（不再手抄），本任务是周期任务、不在清理范围。"""
    root = root or repo_root()
    prefixes = "、".join("「%s」" % TASK_PREFIX[t] for t in TABLES)
    cmds = "；".join("%s 链跑 `%s`" % ("简历" if t == "resume" else "岗位JD", _CHAIN[t]["queue_cmd"])
                     for t in TABLES)
    msg = (
        "在仓库 %s（以仓库根为 CWD）执行精析队列每日兜底巡检：\n"
        "1. 依次查看两条链队列计数：%s。若两条链均为 0，直接结束、不做任何事（不要发消息）。\n"
        "2. 非空的链按其 SKILL 流水线消费到写回打标（%s）：prepare → 同一条消息一次性并发发出"
        "全部 subagent（不分波）→ 每批只发一行 pending 提示词文件路径 → merge → 写回；"
        "prepare 返回 refused 即退出、禁止抢跑。\n"
        "3. 自清理（无论队列是否为空都做）：列出定时任务，删除名称以 %s 开头、"
        "已停用且有执行记录的一次性消费任务（已消费完的 at 型任务留着只会堆积列表）；"
        "本巡检任务是周期任务，禁止删除自身。完成后无需通知任何人。"
        % (root, cmds, "、".join(_CHAIN[t]["skill"] for t in TABLES), prefixes)
    )
    # 每日 HH:MM 经 fallback_hhmm() 从 FALLBACK_CRON 派生，不另写字面量（改时刻只改一处）
    return {
        "name": FALLBACK_NAME,
        "description": "每日 %s 兜底巡检：消费简历/岗位精析队列（事件驱动漏网时的兜底），"
                       "空队列秒退；顺带清理已执行完的一次性消费任务" % fallback_hhmm(),
        "schedule": {"kind": "cron", "expr": FALLBACK_CRON, "tz": FALLBACK_TZ},
        "payload": {"kind": "agentTurn", "message": msg, "contextDirs": [root]},
        "missedRunPolicy": "run_latest",
    }


def trigger(nt, table, report, root=None):
    """入库脚本的统一收尾：计队列 → 非空则给出注册时刻与现成注册规格。

    唯一真源（不变量 10）：resume/job 两条上传链共用本函数，禁止各自再抄
    "refine_queued = len(queue(...)) + fire_at()" 三行（曾两处并存，改延迟时必漏一处）。
    写入 report 的三个键：refine_queued（触发信号）、refine_fire_at（注册时刻）、
    cron_job（可直接传给定时任务工具的 job 规格）。NotableError 由调用方统一捕获。"""
    n = len(queue(nt, table))
    report["refine_queued"] = n
    if n:
        at = fire_at()
        report["refine_fire_at"] = at
        report["cron_job"] = consume_task_spec(table, at, root)
    return n


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


def _cli():
    """产出定时任务注册规格（JSON），供 agent 原样传给定时任务工具：
      python3 shared/refine_loop.py consume resume   # 一次性简历精析消费任务规格
      python3 shared/refine_loop.py consume job      # 一次性岗位精析消费任务规格
      python3 shared/refine_loop.py fallback         # 每日兜底巡检任务规格
    规格是代码产物而非手抄：任务名前缀、payload 措辞、时刻均从此处派生（不变量 10）。
    正常链路由 upload_* 脚本在报告 cron_job 字段直接带出，本 CLI 主要用于重建/巡检兜底任务。"""
    import argparse
    ap = argparse.ArgumentParser(description="产出精析消费/兜底任务的注册规格 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("consume", help="一次性精析消费任务规格")
    c.add_argument("chain", choices=list(_CHAIN))
    sub.add_parser("fallback", help="每日兜底巡检任务规格")
    a = ap.parse_args()
    spec = consume_task_spec(a.chain) if a.cmd == "consume" else fallback_task_spec()
    print(json.dumps(spec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
