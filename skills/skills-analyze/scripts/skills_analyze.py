# -*- coding: utf-8 -*-
"""skills_analyze.py — 简历AI精析：切分批次 / 合并子任务结果 / 队列计数

    python3 skills/skills-analyze/scripts/skills_analyze.py prepare [--batch 7] [--since 30 | --all | --ids file.json]
    python3 skills/skills-analyze/scripts/skills_analyze.py merge
    python3 skills/skills-analyze/scripts/skills_analyze.py queue

prepare 输出：
  outputs/skills_pending_part<N>.json  每批待分析记录（id/name/current_skills/full_text）
  outputs/skills_analyze_meta.json     {"total":N,"queued":N,"batches":M,...}
  outputs/job_vocab.json               岗位必备/加分技能同源词表（供子任务优先取词）
候选来源 = 精析队列，谓词唯一真源 shared/refine_loop.py（ai_refined_at 为空 且 full_text 非空），
禁止在本脚本推断"三列是否为空"。--all 连已精析的一起重析；--since N 在队列内只看最近 N 分钟上传的；
--ids file.json 指定记录 id（不受队列限制）。
queue 输出 refine_loop.queue_counts 的 JSON（{"resume":n,"job":m}），供上传报告与后台周期判断。

切批/清旧/写 meta 与 done 合并的公共骨架 = shared/analyze_parts.py（parts 命名唯一真源）。

分派纪律（历史教训）：所有子命令 handler 签名一致 = handler(args)，需要 nt 的自己构造，
merge 不触网就不传/不构造 nt。
"""
import sys, os, json, time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from vocab import toks       # noqa: E402  分词分隔符唯一源，禁止本地再抄正则
import refine_loop           # noqa: E402  队列谓词唯一真源，禁止本地抄副本
import analyze_parts as ap   # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "skills"
FULL_TEXT_MAX = 6000
# 队列模式的补充字段（full_text 已由 refine_loop.queue 带回，不重复拉）
META_BIZ = ["name", "skills", "upload_time"]
BIZ = META_BIZ + ["full_text"]


def _vocab(nt):
    words = set()
    for j in nt.list_records("job", biz_fields=["must_skills", "bonus_skills"]):
        f = j["fields"]
        for k in ("must_skills", "bonus_skills"):
            words.update(toks(f.get(k)))
    return sorted(words)


def _batch_arg(args):
    return int(args[args.index("--batch") + 1]) if "--batch" in args else None


def prepare(args):
    nt = Notable()
    since, all_mode = None, False   # 不传 --batch 时自动铺满 agent
    ids_file = None
    if "--since" in args:
        since = int(args[args.index("--since") + 1])
    if "--ids" in args:
        ids_file = args[args.index("--ids") + 1]
    all_mode = "--all" in args

    if ids_file or all_mode:
        rows = nt.list_records("resume", biz_fields=BIZ)
        if ids_file:
            want = set(json.load(open(ids_file, encoding="utf-8")))
            rows = [r for r in rows if r["id"] in want]
    else:
        rows = refine_loop.queue(nt, "resume")   # 候选唯一来源：精析队列
        extra = {r["id"]: r["fields"]
                 for r in nt.list_records("resume", biz_fields=META_BIZ)}
        for r in rows:
            r["fields"].update(extra.get(r["id"], {}))
        if since is not None:
            cut = int((time.time() - since * 60) * 1000)
            rows = [r for r in rows if (r["fields"].get("upload_time") or 0) >= cut]
    items = [{"id": r["id"],
              "name": (r["fields"].get("name") or "").strip(),
              "current_skills": r["fields"].get("skills") or [],
              "full_text": str(r["fields"].get("full_text") or "")[:FULL_TEXT_MAX]}
             for r in rows]
    ap.write_parts(OUTDIR, PREFIX, items,
                   {"total": len(items), "queued": len(items)}, _batch_arg(args))
    json.dump(_vocab(nt), open(os.path.join(OUTDIR, "job_vocab.json"), "w", encoding="utf-8"),
              ensure_ascii=False)


def merge(args):
    rows, missing = ap.read_done(OUTDIR, PREFIX)
    done, seen = [], set()
    for row in rows:
        if row.get("id") and row["id"] not in seen:
            seen.add(row["id"])
            done.append(row)
    json.dump(done, open(os.path.join(OUTDIR, "skills_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"merged": len(done), "batches": ap.count_pending(OUTDIR, PREFIX),
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
