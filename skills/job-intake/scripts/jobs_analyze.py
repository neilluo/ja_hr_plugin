# -*- coding: utf-8 -*-
"""jobs_analyze.py — 岗位JD精析（subagent 并发，一人一岗，上限7）

    python3 skills/job-intake/scripts/jobs_analyze.py prepare [--all|--since N|--ids file.json]
    python3 skills/job-intake/scripts/jobs_analyze.py merge

prepare：按岗位切批，写 outputs/jobs_pending_part<N>.json 与 meta（含 waves 波次计划）
merge ：合并子任务产出为 outputs/jobs_done.json，供 skills/job-intake/scripts/sync_job_columns.py 写回
子任务提示词：skills/job-intake/references/job-subagent-prompt.md
"""
import sys, os, json

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from waves import plan, MAX_AGENTS  # noqa: E402  agent数封顶8，超限自动加大 batch

OUTDIR = os.path.join(ROOT, "outputs")
DEFAULT_BATCH = 7        # 每个 agent 分析几个岗位
BIZ = ["job_id", "job_name", "department", "org", "status", "hard_gates", "must_skills",
       "bonus_skills", "requirements", "responsibilities", "work_location"]


def txt(v):
    if isinstance(v, dict):
        return v.get("markdown") or v.get("text") or json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "、".join(str(x.get("name") if isinstance(x, dict) else x) for x in v)
    return "" if v is None else str(v)


def prepare(nt, args):
    os.makedirs(OUTDIR, exist_ok=True)
    rows = nt.list_records("job", biz_fields=BIZ)
    if "--all" not in args:
        rows = [r for r in rows if not (txt(r["fields"].get("hard_gates")) and
                                        txt(r["fields"].get("must_skills")))]
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
    batch, groups = plan(len(items), batch)
    parts = [items[g[0] - 1:g[-1]] for g in groups]
    for old in os.listdir(OUTDIR):
        if old.startswith("jobs_pending_part") or old.startswith("jobs_done_part"):
            os.remove(os.path.join(OUTDIR, old))
    for i, p in enumerate(parts, 1):
        json.dump(p, open(os.path.join(OUTDIR, "jobs_pending_part%d.json" % i), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=1)
    meta = {"total": len(items), "batches": len(parts), "batch_size": batch,
            "agents": len(parts), "agent_sizes": [len(p) for p in parts], "max_agents": MAX_AGENTS}
    json.dump(meta, open(os.path.join(OUTDIR, "jobs_analyze_meta.json"), "w", encoding="utf-8"))
    print(json.dumps(meta, ensure_ascii=False))


def merge(nt):
    n = 0
    while os.path.exists(os.path.join(OUTDIR, "jobs_pending_part%d.json" % (n + 1))):
        n += 1
    out, missing = {}, []
    for i in range(1, n + 1):
        p = os.path.join(OUTDIR, "jobs_done_part%d.json" % i)
        if not os.path.exists(p):
            missing.append(i)
            continue
        try:
            for row in json.load(open(p, encoding="utf-8")):
                jid = row.get("job_id")
                if jid:
                    out[jid] = {"hard_gates": row.get("hard_gates", ""),
                                "must_skills": row.get("must_skills", ""),
                                "bonus_skills": row.get("bonus_skills", "")}
        except Exception as e:
            missing.append(i)
            print("批次%d解析失败: %s" % (i, str(e)[:120]))
    json.dump(out, open(os.path.join(OUTDIR, "jobs_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"merged": len(out), "batches": n, "missing_batches": missing},
                     ensure_ascii=False))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("prepare", "merge"):
        print(__doc__)
        sys.exit(1)
    nt = Notable()
    (prepare if sys.argv[1] == "prepare" else merge)(nt, sys.argv[2:])


if __name__ == "__main__":
    main()
