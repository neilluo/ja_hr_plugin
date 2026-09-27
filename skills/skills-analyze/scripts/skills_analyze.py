# -*- coding: utf-8 -*-
"""skills_analyze.py — 简历AI精析：切分批次 / 合并子任务结果 / 队列计数

    python3 skills/skills-analyze/scripts/skills_analyze.py prepare [--batch 7] [--since 30 | --all | --ids file.json] [--force]
    python3 skills/skills-analyze/scripts/skills_analyze.py merge
    python3 skills/skills-analyze/scripts/skills_analyze.py queue

prepare 获取周期租约 outputs/refine_resume.lock（活租约期内第二个周期 refused exit 2，
防并发切批双写；skills_apply 写回时释放；--force 夺回自有租约，见 shared/refine_loop.py）。

prepare 输出：
  outputs/skills_pending_part<N>.json  每批待分析记录（id/name/current_skills/full_text/source_file）
  outputs/skills_analyze_meta.json     {"total":N,"queued":N,"unrefinable":[...],"batches":M,...}
  outputs/job_vocab.json               岗位必备/加分技能同源词表（供子任务优先取词）
候选来源 = 精析队列，谓词与补集（unrefinable）唯一真源 shared/refine_loop.py，本脚本不复述条件；
禁止在本脚本推断"三列是否为空"。扫描件靠 source_file 入队、由 subagent 读图（见 references/subagent-prompt.md）。
--all 连已精析的一起重析；--since N 在队列内只看最近 N 分钟上传的；--ids file.json 指定记录 id（不受队列限制）。
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
# 队列模式的补充字段（full_text / source_file 已由 refine_loop 队列带回，不重复拉）
META_BIZ = ["name", "skills", "upload_time"]
BIZ = META_BIZ + ["full_text", "source_file"]


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
    # 周期租约：prepare 获取、skills_apply 写回时释放，跨越 subagent/merge 全程持租约，
    # 防两个精析周期（如连续上传注册的多个即时任务）并发切批双写。持租未过期 → exit 2 秒退。
    # 同周期内重新切批（改 --batch/--since）用 --force 夺回自有租约。
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if refine_loop.acquire_lock(OUTDIR, "resume", stale_after=1800, force=force) is None:
        print(json.dumps({"refused": True,
                          "reason": "另一精析周期持锁中（outputs/refine_resume.lock 租约未过期）：本轮跳过避免并发双写；"
                                    "确属本周期重切批则加 --force"}))
        sys.exit(2)
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
        # --all/--ids 绕过队列谓词，仍须过滤无信息源的记录（旧手析扫描件 full_text 与 source_file 皆空、
        # 或原件已被删除）：否则 subagent 只能编造或填"未提及"，apply 写回即覆盖已有好数据。
        # 判据唯一真源 refine_loop.refinable()。
        unrefined = [r for r in rows if not refine_loop.refinable(r["fields"], "resume")]
        rows = [r for r in rows if refine_loop.refinable(r["fields"], "resume")]
    else:
        # 候选唯一来源：精析队列；unrefinable = 原件已删的扫描件（谓词补集，同处一源）
        rows, unrefined = refine_loop.queue_with_unrefinable(nt, "resume")
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
              "full_text": str(r["fields"].get("full_text") or "")[:FULL_TEXT_MAX],
              # 扫描件（full_text 为空）的唯一精析依据：本地原件绝对路径，subagent 直接读图
              "source_file": str(r["fields"].get("source_file") or "")}
             for r in rows]
    ap.write_parts(OUTDIR, PREFIX, items,
                   {"total": len(items), "queued": len(items),
                    # 原件已删的扫描件：不入队（防永久卡队列+阻塞门禁），但必须报出供人工知晓
                    "unrefinable": [{"id": r["id"], "name": r["fields"].get("name"),
                                     "source_file": r["fields"].get("source_file")}
                                    for r in unrefined]},
                   _batch_arg(args))
    if not items:
        # 切出 0 条 = 队列已被并发周期吃空，apply 永远不会来释放，此处即时释放租约不留僵尸
        refine_loop.release_lock(refine_loop.lock_path(OUTDIR, "resume"))
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
