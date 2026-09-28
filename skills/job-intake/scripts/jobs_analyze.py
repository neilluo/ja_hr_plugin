# -*- coding: utf-8 -*-
"""jobs_analyze.py — 岗位JD精析（subagent 并发，一人一岗，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

    python3 skills/job-intake/scripts/jobs_analyze.py prepare [--all|--batch N|--force]
    python3 skills/job-intake/scripts/jobs_analyze.py merge
    python3 skills/job-intake/scripts/jobs_analyze.py queue   # 双表精析队列计数

prepare：从精析队列取岗（谓词唯一真源 shared/refine_loop.py：ai_refined_at 空且 responsibilities 非空），
        按岗位切批，写 outputs/jobs_pending_part<N>.json 与 meta（骨架 = shared/analyze_parts.py）；
        --all = 连已精析的一起重析；--force = 夺回自有租约（同周期重切批）；
        租约被活周期占用时拒绝切批 exit 2（shared/refine_loop.acquire_lock，job 链）
merge ：合并子任务产出为 outputs/jobs_done.json，供 skills/job-intake/scripts/sync_job_columns.py 写回
子任务提示词：skills/job-intake/references/job-subagent-prompt.md

分派纪律（历史教训：merge(nt) vs prepare(nt,args) 签名不一致必崩 TypeError）：
所有子命令 handler 签名一致 = handler(args)，需要 nt 的自己构造；merge 不触网就不构造 nt。
"""
import sys, os, json

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
import refine_loop  # noqa: E402  队列谓词唯一真源，禁止本地抄副本
import analyze_parts as ap  # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "jobs"
BIZ = ["job_id", "job_name", "department", "org", "status", "hard_gates", "must_skills",
       "bonus_skills", "requirements", "responsibilities", "work_location"]


def txt(v):
    """字段取值归一：多选列读回为名字数组→「、」连接；其余转字符串（config 类型对齐真表）。"""
    if isinstance(v, list):
        return "、".join(str(x) for x in v)
    return "" if v is None else str(v)


def prepare(args):
    # 周期租约（job 链独立于 resume 链，两链可并行、同链防并发双写），
    # sync_job_columns 写回时释放。同周期重切批 --force 夺回自有租约。
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if refine_loop.acquire_lock(OUTDIR, "job", force=force) is None:
        print(json.dumps({"refused": True,
                          "reason": "另一精析周期持锁中（outputs/refine_job.lock 租约未过期）：本轮跳过避免并发双写；"
                                    "确属本周期重切批则加 --force"}))
        sys.exit(2)
    nt = Notable()
    rows = nt.list_records("job", biz_fields=BIZ)
    # 队列谓词唯一真源 refine_loop（ai_refined_at 空且 responsibilities 非空）；
    # --all = 连已精析的一起重析
    queued_ids = {r["id"] for r in refine_loop.queue(nt, "job")}
    if "--all" not in args:
        rows = [r for r in rows if r["id"] in queued_ids]
    else:
        # --all 绕过队列谓词，但仍须过滤无正文的岗（responsibilities 空）：
        # 否则 subagent 无从精析只能编造三列，sync_job_columns 连同 ai_refined_at 一次落库即静默出队，
        # 坏数据被当成已精析成果。判据唯一真源 refine_loop.refinable()（与 skills_analyze --all 对称）。
        rows = [r for r in rows if refine_loop.refinable(r["fields"], "job")]
    items = []
    for r in rows:
        f = r["fields"]
        items.append({"id": r["id"], "job_id": f.get("job_id"), "job_name": f.get("job_name"),
                      "department": f.get("department"), "org": f.get("org"),
                      "work_location": txt(f.get("work_location")),
                      "current_hard_gates": txt(f.get("hard_gates")),
                      "current_must": txt(f.get("must_skills")),
                      "current_bonus": txt(f.get("bonus_skills")),
                      "responsibilities": txt(f.get("responsibilities"))[:4000],
                      "requirements": txt(f.get("requirements"))[:4000]})
    batch = int(args[args.index("--batch") + 1]) if "--batch" in args else None
    meta = ap.write_parts(OUTDIR, PREFIX, items,
                          {"total": len(items), "queued": sum(1 for r in rows if r["id"] in queued_ids)},
                          batch)
    if not items:
        # 切出 0 条 = 队列已被并发周期吃空，sync_job_columns 永远不会来释放，此处即时释放租约
        refine_loop.release_lock(refine_loop.lock_path(OUTDIR, "job"))
    elif meta["batches"]:
        # 分派清单落盘：agent 复制真实 pending 路径指针分派，不再凭记忆手拼 *_pending_part<N>.json
        # （清单形态唯一真源 = shared/analyze_parts.write_dispatch，与 match_analyze 同机制；
        # stdout 仍只留 write_parts 打的单行 meta）
        meta["dispatch"] = ap.write_dispatch(OUTDIR, PREFIX, meta["batches"])
        json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta


def merge(args):
    rows, missing = ap.read_done(OUTDIR, PREFIX)
    out = {}
    for row in rows:
        jid = row.get("job_id")
        if jid:
            out[jid] = {"hard_gates": row.get("hard_gates", ""),
                        "must_skills": row.get("must_skills", ""),
                        "bonus_skills": row.get("bonus_skills", "")}
    json.dump(out, open(os.path.join(OUTDIR, "jobs_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"merged": len(out), "batches": ap.count_pending(OUTDIR, PREFIX),
                      "missing_batches": missing}, ensure_ascii=False))


def queue(args):
    print(json.dumps(refine_loop.queue_counts(Notable()), ensure_ascii=False))


HANDLERS = {"prepare": prepare, "merge": merge, "queue": queue}


def main():
    if "-h" in sys.argv or "--help" in sys.argv:   # --help 早退：不构造 Notable、不触网
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        print(__doc__)
        sys.exit(1)
    HANDLERS[sys.argv[1]](sys.argv[2:])   # 统一签名 handler(args)，需要 nt 的 handler 内部构造


if __name__ == "__main__":
    main()
