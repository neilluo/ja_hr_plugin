# -*- coding: utf-8 -*-
"""skills_analyze.py — 简历AI精析：切分批次 / 合并子任务结果

    python3 skills/skills-analyze/scripts/skills_analyze.py prepare [--batch 7] [--since 30 | --all | --ids file.json]
    python3 skills/skills-analyze/scripts/skills_analyze.py merge

prepare 输出：
  outputs/skills_pending_part<N>.json  每批待分析记录（id/name/current_skills/full_text）
  outputs/skills_analyze_meta.json     {"total":N,"batches":M,"batch_size":7}
  outputs/job_vocab.json               岗位必备/加分技能同源词表（供子任务优先取词）
默认只挑「三列有缺失」的记录；--all 全量重跑；--since N 只看最近N分钟上传的。
"""
import sys, os, re, json, time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable  # noqa: E402

OUTDIR = os.path.join(ROOT, "outputs")
FULL_TEXT_MAX = 6000
sys.path.insert(0, os.path.join(ROOT, "shared"))
from waves import plan, summary, MAX_AGENTS, DEFAULT_BATCH  # noqa: E402  agent数封顶调度
BIZ = ["name", "phone", "skills", "full_text", "upload_time"]


def _vocab(nt):
    words = set()
    for j in nt.list_records("job", biz_fields=["must_skills", "bonus_skills"]):
        f = j["fields"]
        for k in ("must_skills", "bonus_skills"):
            words.update(t.strip() for t in re.split(r"[、,，;；]\s*", str(f.get(k) or "")) if t.strip())
    return sorted(words)


def prepare(nt, args):
    os.makedirs(OUTDIR, exist_ok=True)
    batch, since, all_mode = None, None, False   # 不传 --batch 时自动铺满 8 个 agent
    ids_file = None
    it = iter(args)
    for a in args:
        pass
    if "--batch" in args:
        batch = int(args[args.index("--batch") + 1])
    if "--since" in args:
        since = int(args[args.index("--since") + 1])
    if "--ids" in args:
        ids_file = args[args.index("--ids") + 1]
    all_mode = "--all" in args

    rows = nt.list_records("resume", biz_fields=BIZ)
    if ids_file:
        want = set(json.load(open(ids_file, encoding="utf-8")))
        rows = [r for r in rows if r["id"] in want]
    elif not all_mode:
        if since is not None:
            cut = int((time.time() - since * 60) * 1000)
            rows = [r for r in rows if (r["fields"].get("upload_time") or 0) >= cut]
        rows = [r for r in rows if not (r["fields"].get("skills") and r["fields"].get("ai_extract")
                                        and r["fields"].get("ai_deep"))]
    items = [{"id": r["id"],
              "name": (r["fields"].get("name") or "").strip(),
              "current_skills": r["fields"].get("skills") or [],
              "full_text": str(r["fields"].get("full_text") or "")[:FULL_TEXT_MAX]}
             for r in rows]
    batch, groups = plan(len(items), batch)     # agent 数 >8 时自动加大 batch
    parts = [items[g[0] - 1:g[-1]] for g in groups]
    for old in os.listdir(OUTDIR):
        if old.startswith("skills_pending_part") or old.startswith("skills_done_part"):
            os.remove(os.path.join(OUTDIR, old))
    for i, p in enumerate(parts, 1):
        json.dump(p, open(os.path.join(OUTDIR, "skills_pending_part%d.json" % i), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(_vocab(nt), open(os.path.join(OUTDIR, "job_vocab.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    meta = {"total": len(items), "batches": len(parts), "batch_size": batch,
            "agents": len(parts), "agent_sizes": [len(p) for p in parts],
            "max_agents": MAX_AGENTS}
    json.dump(meta, open(os.path.join(OUTDIR, "skills_analyze_meta.json"), "w", encoding="utf-8"))
    print(json.dumps(meta, ensure_ascii=False))


def merge(nt):
    done, seen, missing = [], set(), []
    n = 0
    while os.path.exists(os.path.join(OUTDIR, "skills_pending_part%d.json" % (n + 1))):
        n += 1
    for i in range(1, n + 1):
        p = os.path.join(OUTDIR, "skills_done_part%d.json" % i)
        if not os.path.exists(p):
            missing.append(i)
            continue
        try:
            for row in json.load(open(p, encoding="utf-8")):
                if row.get("id") and row["id"] not in seen:
                    seen.add(row["id"])
                    done.append(row)
        except Exception as e:
            missing.append(i)
            print("批次%d解析失败: %s" % (i, e))
    json.dump(done, open(os.path.join(OUTDIR, "skills_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"merged": len(done), "batches": n, "missing_batches": missing},
                     ensure_ascii=False))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("prepare", "merge"):
        print(__doc__)
        sys.exit(1)
    nt = Notable()
    if sys.argv[1] == "prepare":
        prepare(nt, sys.argv[2:])
    else:
        merge(nt)


if __name__ == "__main__":
    main()
