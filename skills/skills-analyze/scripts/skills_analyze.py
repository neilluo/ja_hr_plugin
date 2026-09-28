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
# 提示词唯一源（模板）；per-batch 渲染产物见 render_prompts
PROMPT_TPL = os.path.join(ROOT, "skills", "skills-analyze", "references", "subagent-prompt.md")
# 队列模式的补充字段（full_text / source_file 已由 refine_loop 队列带回，不重复拉）
META_BIZ = ["name", "skills", "upload_time"]
BIZ = META_BIZ + ["full_text", "source_file"]

# ── done 产物 schema 唯一真源（不变量 10）────────────────────────────────────
# 由 merge 一次性判定所有行；模板与 agent 不再自校验，也不允许在别处再抄口径。
# 违反此 schema 的行被 merge 丢回队列（ai_refined_at 未打），下一周期自动重析。
DONE_REQUIRED_FIELDS = ("skills", "ai_structured", "ai_deep",
                        "name", "major", "school", "certificates",
                        "years_experience", "expected_position")
DONE_NULLABLE_STR_FIELDS = ("name", "major", "school", "certificates", "expected_position")
DONE_STRUCTURED_SEGS = ["学历背景", "工作经验", "核心技能", "求职意向", "匹配度评估"]
DONE_TEXT_MAX = 200      # ai_structured / ai_deep 上限（字符）
DONE_SKILLS_RANGE = (5, 12)
DONE_SKILL_LEN_MAX = 12  # 任意标签总字符 ≤ 12
DONE_SKILL_ZH_RANGE = (2, 6)   # 含中文字符时，中文字数 ∈ [2, 6]；纯英文/缩写不限


def validate_row(r):
    """返回违规说明列表；空列表 = 合规。禁止在任何调用方再抄 assert 口径（SSOT）。"""
    errs = []
    for f in DONE_REQUIRED_FIELDS:
        if f not in r:
            errs.append("缺字段 %s" % f)
    if errs:
        return errs   # 缺字段已足矣说明问题，后续判定会 KeyError
    st, dp = r["ai_structured"], r["ai_deep"]
    if not isinstance(st, str) or not isinstance(dp, str):
        errs.append("ai_structured/ai_deep 须为字符串")
        return errs
    segs = [l.split("｜")[0] for l in st.split("\n")]
    if segs != DONE_STRUCTURED_SEGS:
        errs.append("段名错 %s" % segs)
    if len(st) > DONE_TEXT_MAX:
        errs.append("ai_structured 超 %d 字" % DONE_TEXT_MAX)
    if len(dp) > DONE_TEXT_MAX:
        errs.append("ai_deep 超 %d 字" % DONE_TEXT_MAX)
    sk = r["skills"]
    lo, hi = DONE_SKILLS_RANGE
    if not isinstance(sk, list) or (sk and not (lo <= len(sk) <= hi)):
        errs.append("技能数越界 %s" % (len(sk) if isinstance(sk, list) else type(sk).__name__))
    else:
        for s in sk:
            if not isinstance(s, str) or not (0 < len(s) <= DONE_SKILL_LEN_MAX):
                errs.append("标签长度违规 %r" % (s,)); continue
            zh = sum(1 for ch in s if "一" <= ch <= "鿿")
            if zh and not (DONE_SKILL_ZH_RANGE[0] <= zh <= DONE_SKILL_ZH_RANGE[1]):
                errs.append("标签中文字数违规 %r(zh=%d)" % (s, zh))
    ye = r["years_experience"]
    if ye is not None and (not isinstance(ye, int) or isinstance(ye, bool)):
        errs.append("years_experience 须为 int 或 null %r" % (ye,))
    for f in DONE_NULLABLE_STR_FIELDS:
        if r[f] is not None and not isinstance(r[f], str):
            errs.append("%s 须为字符串或 null" % f)
    return errs


def render_prompts(n_batches, vocab_path):
    """把提示词模板渲染成 n_batches 个 per-batch 文件 + 一份可直接照抄的分派清单。

    为什么渲染（实测教训，见 AGENTS.md 犯错记录）：主 agent 原先要把 8.6KB 提示词原文
    内联进每一个 Agent 工具调用，16 批 = 约 138KB 工具调用入参。本仓一次真实运行里
    第 13 个调用的 prompt 在工具流中途被截断（只剩半段），只能事后补发批次 14-16，
    于是 16 个 agent 被迫分成 13+3 两波——违反"一次性并发发完、不分波"，
    多花约 250s 纯串行等待，并多暴露一次后端 stall 窗口。
    渲染后主 agent 每批只发一行路径指针（约 150 字节），载荷降两个数量级，
    截断诱因消除；占位符替换由代码保证，agent 无需记得"必须替换哪些占位符"。

    产物：
      outputs/skills_prompt_part<N>.md   该批完整提示词（已填好路径，可直接作为 subagent 任务）
      outputs/skills_dispatch.json       {"batches":N,"prompts":[...]}

    done 产物 schema 校验唯一真源 = validate_row + merge，模板不再内嵌 bash 校验脚本
    （见 AGENTS.md 犯错记录：subagent 自写/自运行校验会多 1-3 回合、拉长 stall 暴露窗）。
    """
    with open(PROMPT_TPL, encoding="utf-8") as f:
        tpl = f.read()
    # 清旧 prompt 文件：write_parts 只清 pending/done，prompt 是本函数产物须自清，
    # 否则上轮大批次（如 16）残留的 prompt_part15/16.md 会在小批次（如 2）轮里成僵尸文件。
    for old in os.listdir(OUTDIR):
        if old.startswith("%s_prompt_part" % PREFIX):
            os.remove(os.path.join(OUTDIR, old))
    paths = []
    for i in range(1, n_batches + 1):
        pend = ap.pending_path(OUTDIR, PREFIX, i)   # 路径经公共骨架派生，禁止本地抄 parts 命名
        body = (tpl.replace("<BATCH_PATH>", pend)
                   .replace("<VOCAB_PATH>", vocab_path)
                   .replace("<N>", str(i)))
        out = os.path.join(OUTDIR, "%s_prompt_part%d.md" % (PREFIX, i))
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        paths.append(out)

    idx = os.path.join(OUTDIR, "skills_dispatch.json")
    with open(idx, "w", encoding="utf-8") as f:
        json.dump({"batches": n_batches, "prompts": paths}, f, ensure_ascii=False, indent=1)
    return paths



def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def done_integrity():
    """盘上 done 产物体检（只读）：批次数、缺失批次、id 集合不一致/缺行的坏批次。
    把 SKILL.md"failed 先验盘上产物再决定补发"这条人工纪律做成机器判定。"""
    missing, bad, total = [], [], ap.count_pending(OUTDIR, PREFIX)
    for i in range(1, total + 1):
        dp = ap.done_path(OUTDIR, PREFIX, i)
        if not os.path.exists(dp):
            missing.append(i)
            continue
        try:
            pend = _load(ap.pending_path(OUTDIR, PREFIX, i))
            dn = _load(dp)
        except Exception:  # noqa: BLE001  不可解析按缺失处理，与 read_done 同口径
            missing.append(i)
            continue
        pids = {r.get("id") for r in pend if r.get("id")}
        dids = {r.get("id") for r in dn if r.get("id")}
        if len(dids) != len(dn) or pids != dids:
            bad.append(i)
    return {"batches": total, "missing_batches": missing, "bad_batches": bad,
            "all_complete": not missing and not bad}



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
    meta = ap.write_parts(OUTDIR, PREFIX, items,
                          {"total": len(items), "queued": len(items),
                           # 原件已删的扫描件：不入队（防永久卡队列+阻塞门禁），但必须报出供人工知晓
                           "unrefinable": [{"id": r["id"], "name": r["fields"].get("name"),
                                            "source_file": r["fields"].get("source_file")}
                                           for r in unrefined]},
                          _batch_arg(args))
    if not items:
        # 切出 0 条 = 队列已被并发周期吃空，apply 永远不会来释放，此处即时释放租约不留僵尸
        refine_loop.release_lock(refine_loop.lock_path(OUTDIR, "resume"))
    vocab_path = os.path.join(OUTDIR, "job_vocab.json")
    json.dump(_vocab(nt), open(vocab_path, "w", encoding="utf-8"), ensure_ascii=False)
    # 渲染 per-batch 提示词 + 分派清单（见 render_prompts 注释）：主 agent 分派时每批只发一个路径指针。
    # stdout 只留 write_parts 打过的那一行 meta（守既有单行 JSON 契约），分派信息落 skills_dispatch.json
    # 并回写进 meta 文件，供 agent 二次读取，不再重复打印。
    if meta["batches"]:
        render_prompts(meta["batches"], vocab_path)
        meta["dispatch"] = os.path.join(OUTDIR, "skills_dispatch.json")
        json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta



def merge(args):
    rows, _ = ap.read_done(OUTDIR, PREFIX)   # 缺失/坏批次判定统一交 done_integrity，此处只取行
    done, seen, bad_rows = [], set(), []
    for row in rows:
        rid = row.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        errs = validate_row(row)             # schema 唯一真源：违规行丢弃、不写 ai_refined_at → 下周期重析
        if errs:
            bad_rows.append({"id": rid, "errors": errs})
            continue
        done.append(row)
    json.dump(done, open(os.path.join(OUTDIR, "skills_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    integ = done_integrity()   # 盘上产物体检（缺失/坏批次的记录未打标、下周期自动重析）
    print(json.dumps({"merged": len(done), "batches": integ["batches"],
                      "missing_batches": integ["missing_batches"],
                      "bad_batches": integ["bad_batches"],
                      "bad_rows": bad_rows,
                      "all_complete": (integ["all_complete"] and not bad_rows)},
                     ensure_ascii=False))



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
