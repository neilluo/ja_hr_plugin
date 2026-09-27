# -*- coding: utf-8 -*-
"""jobs_analyze.py — 岗位JD精析（subagent 并发，一人一岗，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

    python3 skills/job-intake/scripts/jobs_analyze.py prepare [--all|--batch N]
    python3 skills/job-intake/scripts/jobs_analyze.py merge
    python3 skills/job-intake/scripts/jobs_analyze.py queue   # 双表精析队列计数

prepare：从精析队列取岗（谓词唯一真源 shared/refine_loop.py：ai_refined_at 空且 responsibilities 非空），
        按岗位切批，写 outputs/jobs_pending_part<N>.json 与 meta（骨架 = shared/analyze_parts.py）；
        --all = 连已精析的一起重析
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
    nt = Notable()
    rows = nt.list_records("job", biz_fields=BIZ)
    # 队列谓词唯一真源 refine_loop（ai_refined_at 空且 responsibilities 非空）；
    # --all = 连已精析的一起重析
    queued_ids = {r["id"] for r in refine_loop.queue(nt, "job")}
    if "--all" not in args:
        rows = [r for r in rows if r["id"] in queued_ids]
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
    ap.write_parts(OUTDIR, PREFIX, items,
                   {"total": len(items), "queued": sum(1 for r in rows if r["id"] in queued_ids)},
                   batch)


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
