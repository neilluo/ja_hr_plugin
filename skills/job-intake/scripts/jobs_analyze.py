# -*- coding: utf-8 -*-
"""jobs_analyze.py — 岗位JD精析（subagent 并发，一人一岗，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

    python3 skills/job-intake/scripts/jobs_analyze.py prepare [--all|--batch N|--force]
    python3 skills/job-intake/scripts/jobs_analyze.py merge
    python3 skills/job-intake/scripts/jobs_analyze.py queue   # 双表精析队列计数

prepare：从精析队列取岗（谓词唯一真源 shared/refine_loop.py：ai_refined_at 空且 responsibilities 非空），
        按岗位切批，写 outputs/jobs_pending_part<N>.json 与 meta（骨架 = shared/analyze_parts.py）；
        --all = 连已精析的一起重析；--force = 夺回自有租约（同周期重切批）；
        租约被活周期占用时拒绝切批 exit 2（shared/refine_loop.acquire_lock，job 链）；
        同时渲染 per-batch 提示词 outputs/jobs_prompt_part<N>.md + 分派清单 jobs_dispatch.json
        + 词表参考 resume_vocab.json（简历标签池实值派生），与 skills_analyze 对称：
        分派时每批只发一行路径指针，禁内联提示词原文（撑爆工具流入参→截断→被迫分波）
merge ：合并子任务产出为 outputs/jobs_done.json，供 skills/job-intake/scripts/sync_job_columns.py 写回；
        同轮内联盘上产物体检 done_integrity()（bad_batches/missing_batches/all_complete），
        把"failed 先验盘上产物"的人工纪律做成机器判定（对齐 skills_analyze，见 AGENTS.md）；
        行级只保留 L0 硬门槛（validate_row：非 dict / 缺重 job_id / 三列全空），格式问题由
        normalize_row 确定性修复，质量问题降级为 soften.soft_observations 非阻断观察
        （不丢行、不影响 all_complete，见 shared/soften.py 模块 docstring）
子任务提示词：skills/job-intake/references/job-subagent-prompt.md

分派纪律（历史教训：merge(nt) vs prepare(nt,args) 签名不一致必崩 TypeError）：
所有子命令 handler 签名一致 = handler(args)，需要 nt 的自己构造；merge 不触网就不构造 nt。
"""
import sys, os, json

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared"))
os.chdir(ROOT)
from notable import Notable  # noqa: E402
from vocab import toks  # noqa: E402  统一分词（唯一真源 shared/vocab.py），_resume_vocab 派生用
import refine_loop  # noqa: E402  队列谓词唯一真源，禁止本地抄副本
import analyze_parts as ap  # noqa: E402  切批/合并公共骨架（parts 命名唯一真源）
import soften  # noqa: E402  归一化+软观察唯一真源（L2 阈值在此，禁止本地抄数值）

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "jobs"
# 提示词唯一源（模板）；per-batch 渲染产物见 render_prompts（与 skills_analyze 对称）
PROMPT_TPL = os.path.join(ROOT, "skills", "job-intake", "references", "job-subagent-prompt.md")
BIZ = ["job_id", "job_name", "department", "org", "status", "hard_gates", "must_skills",
       "bonus_skills", "requirements", "responsibilities", "work_location"]

# ── done 产物 schema（L0 硬门槛）+ 归一化口径（不变量 10）──────────────────────
# L0（确实无法写回：非 dict、缺/重 job_id、归一化后三列全空）才丢行；由 validate_row + merge 判定。
# 一切质量/审美问题（技能词字数、必备/加分项数量、缺门槛段、must∩bonus 重复、半角分隔符）
# 一律先由 normalize_row 确定性修复，修不了也只进 soft_observations 非阻断观察——
# **观察不参与 all_complete、不丢行、不阻断写回**（约束强度必须匹配违规的可逆性：
# 超长词是表格里肉眼可见随手可改的轻违规，而"整行产物被丢 → 队列卡死 → 阻塞匹配门禁
# ~11 小时"是不可逆的重代价；历史事故见 shared/soften.py 模块 docstring）。
# L2 阈值常量唯一真源 = shared/soften.py（TAG_LEN_MAX/ZH_RANGE/JD_MUST_RANGE/JD_BONUS_RANGE），
# 此处禁止抄数值。
DONE_REQUIRED_FIELDS = ("hard_gates", "must_skills", "bonus_skills")
DONE_GATE_SEGS = ("学历：", "专业：", "经验：", "证书：", "年龄：")   # hard_gates 固定五段（规范段序）
GATE_ABSENT_TEXT = "不作硬性要求"   # 缺段填充值：提示词模板对缺项的既定写法，属确定性修复非编造


def _normalize_gates(v):
    """hard_gates 局部归一，返回 (text, kinds)。

    为什么不复用 soften.normalize_segments：该 helper 面向"每段一行、段名｜内容"的换行分段形态
    （简历链 ai_structured），而 hard_gates 是**单字符串、五段「；」分隔、段内「段名：内容」**。
    修复动作（确定性、幂等）：
      coerced_str  非字符串（数组/数字）→ 字符串，首尾空白剔除；
      punct_fixed  段名后的半角 `:` 与段间半角 `;` → 全角 `：`/`；`（只动分隔符位置）；
      gate_filled  DONE_GATE_SEGS 中缺失的段按规范段序补 `段名：不作硬性要求`。
    门槛整体为空时**不填五段占位**：否则"模型什么都没产出"与"确实无硬性要求"无法区分，
    且会让"三列全空"这一 L0（确实无内容可写回）永远不可达。
    """
    kinds = []
    text = soften.coerce_str(v) or ""
    if text != (v.strip() if isinstance(v, str) else v) and (text or v):
        kinds.append("coerced_str")
    if not text:
        return text, kinds
    fixed = text
    for seg in DONE_GATE_SEGS:
        fixed = fixed.replace(seg.rstrip("：") + ":", seg)   # 段名半角冒号 → 全角
    fixed = fixed.replace(";", "；")                          # 段间半角分号 → 全角
    if fixed != text:
        kinds.append("punct_fixed")
        text = fixed
    missing = [seg for seg in DONE_GATE_SEGS if seg not in text]
    if missing:
        parts = [p.strip() for p in text.split("；") if p.strip()]
        parts += ["%s%s" % (seg, GATE_ABSENT_TEXT) for seg in missing]
        text = "；".join(parts)
        kinds.append("gate_filled")
    return text, kinds


def _canon_skills(v):
    """技能列归一：经 vocab.toks 分词后以「、」重连（数组、半角分隔符、多余空白一律规范化）。
    返回 (字符串, 是否变化)——只有真变了才计 skills_joined，避免"本就规范"的行被误报修复。"""
    joined = "、".join(toks(v))
    if not joined and not v:
        return joined, False
    return joined, joined != (v.strip() if isinstance(v, str) else v)


def normalize_row(r):
    """把 subagent 产出的一行归一成可写回形态。返回 (row, kinds)；不改入参、构造新 dict。

    与简历链 skills_analyze.normalize_row 对称：修复而非报错。
      - hard_gates 经 _normalize_gates（缺段补"不作硬性要求"、半角分隔符转全角、数组转字符串）；
      - must_skills / bonus_skills 经 toks 分词后「、」重连（数组形态天然被接受）；
      - must∩bonus 的重复词从 bonus 剔除、保留在 must（必备优先于加分）→ dup_removed，
        **不是错误**（"成本核算"同时出现在必备与加分是正常业务实况）；
      - DONE_REQUIRED_FIELDS 缺键补空字符串，是否为空由 validate_row 的 L0 判定，缺键本身不算错。
    kinds 为修复动作短标签列表（供 merge 聚合报告），空 = 该行本已规范。
    """
    if not isinstance(r, dict):
        return r, []          # 非 dict 交 validate_row 作 L0 拦下
    kinds = []
    out = {"job_id": r.get("job_id")}
    hg, ks = _normalize_gates(r.get("hard_gates"))
    kinds.extend(ks)
    out["hard_gates"] = hg
    for f in ("must_skills", "bonus_skills"):
        s, changed = _canon_skills(r.get(f))
        if changed:
            kinds.append("skills_joined")
        out[f] = s
    must = set(toks(out["must_skills"]))
    bonus = toks(out["bonus_skills"])
    kept = [w for w in bonus if w not in must]
    if len(kept) != len(bonus):
        out["bonus_skills"] = "、".join(kept)
        kinds.append("dup_removed")
    return out, kinds


def validate_row(r):
    """L0 硬门槛（唯一可丢行的判定）：只报"确实无法写回"的问题，返回错误字符串列表（空=可写回）。

    预期在 normalize_row 之后调用。L0 仅三条：非 dict；job_id 缺失/为空；
    归一化后 hard_gates / must_skills / bonus_skills **三列全空**（无任何可写回内容）。
    技能词字数（中文字数、总长）、必备/加分项数量、must∩bonus 重复、门槛缺段一律不在这里——
    前者已由 normalize_row 修复或降级为 soft_observations（非阻断，只报告，不影响写回）。
    """
    if not isinstance(r, dict):
        return ["行不是 dict（%s）" % type(r).__name__]
    errs = []
    if not soften.coerce_str(r.get("job_id")):
        errs.append("缺 job_id")
    if not any(str(r.get(f) or "").strip() for f in DONE_REQUIRED_FIELDS):
        errs.append("三列全空（hard_gates/must_skills/bonus_skills 归一化后均无内容）")
    return errs


def observation_specs():
    """JD 链软观察规格：复用 soften 的 over_len_words / count_out_of_range，
    阈值取自 shared/soften.py（唯一真源，本地不抄数值）。
    返回值仅用于 merge 报告，禁止参与任何丢行/退出决策。"""
    return (
        {"key": "over_len_words", "field": "must_skills/bonus_skills",
         "check": lambda row: soften.over_len_words(
             toks(row.get("must_skills")) + toks(row.get("bonus_skills")))},
        {"key": "must_count_off", "field": "must_skills",
         "check": lambda row: soften.count_out_of_range(
             toks(row.get("must_skills")), *soften.JD_MUST_RANGE)},
        {"key": "bonus_count_off", "field": "bonus_skills",
         "check": lambda row: soften.count_out_of_range(
             toks(row.get("bonus_skills")), *soften.JD_BONUS_RANGE)},
    )


def render_prompts(n_batches, vocab_path):
    """把提示词模板渲染成 n_batches 个 per-batch 文件 + 一份可直接照抄的分派清单。

    为什么渲染（实测教训，见 AGENTS.md 犯错记录 + 简历链同款）：分派时把模板原文内联进
    每一个 Agent 工具调用，批多时工具流入参撑爆被截断 → 被迫分波。渲染后主 agent
    每批只发一行路径指针（~150 字节），占位符替换由代码保证，不赌 agent 记得填哪些路径。
    产物：
      outputs/jobs_prompt_part<N>.md   该批完整提示词（已填好路径，可直接作为 subagent 任务）
      outputs/jobs_dispatch.json       {"batches":N,"prompts":[...]}
    """
    with open(PROMPT_TPL, encoding="utf-8") as f:
        tpl = f.read()
    ap.prune_prompts(OUTDIR, PREFIX)   # 命名/清旧唯一真源在公共骨架
    paths = []
    for i in range(1, n_batches + 1):
        pend = ap.pending_path(OUTDIR, PREFIX, i)   # 路径经公共骨架派生，禁止本地抄 parts 命名
        body = (tpl.replace("<BATCH_PATH>", pend)
                   .replace("<VOCAB_PATH>", vocab_path)
                   .replace("<N>", str(i)))
        out = ap.prompt_path(OUTDIR, PREFIX, i)
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        paths.append(out)
    return paths


def _resume_vocab(nt):
    """简历库技能标签池（运行时从 resume.skills 实值派生）。
    岗位 subagent 选词的参照对象：匹配打分 = 岗位词对简历词，方向必须与简历链对称
    （简历链 _vocab 参照 job 表实值）。"""
    words = set()
    for r in nt.list_records("resume", biz_fields=["skills"]):
        words.update(toks(r["fields"].get("skills")))
    return sorted(words)


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
        return meta
    # 渲染 per-batch 提示词 + 分派清单（见 render_prompts 注释）：主 agent 分派时每批只发一个路径指针。
    # stdout 只留 write_parts 打过的那一行 meta（守既有单行 JSON 契约），分派信息落 jobs_dispatch.json
    # 并回写进 meta 文件，供 agent 二次读取，不再重复打印。
    vocab_path = os.path.join(OUTDIR, "resume_vocab.json")
    json.dump(_resume_vocab(nt), open(vocab_path, "w", encoding="utf-8"), ensure_ascii=False)
    paths = render_prompts(meta["batches"], vocab_path)
    meta["dispatch"] = ap.write_dispatch(OUTDIR, PREFIX, paths)   # 清单形状唯一真源在公共骨架
    json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def done_integrity():
    """盘上 done 产物体检（只读）：批次数、缺失批次、id 集合不一致/缺行的坏批次。
    把 SKILL.md"failed 先验盘上产物再决定补发"这条人工纪律做成机器判定（与 skills_analyze 对称）。
    解析口径与 read_done 同走 ap.load_done（单源，禁止本函数另抄一套 json.load，
    否则会出现"merged 已含抢救行、体检仍报整批缺失"的自相矛盾）。
    行级 schema 判定不在这里、在 validate_row（唯一真源）；job 链以业务键 job_id 为一致性基准。"""
    missing, bad, total = [], [], ap.count_pending(OUTDIR, PREFIX)
    for i in range(1, total + 1):
        dp = ap.done_path(OUTDIR, PREFIX, i)
        if not os.path.exists(dp):
            missing.append(i)
            continue
        try:
            pend = _load(ap.pending_path(OUTDIR, PREFIX, i))
        except Exception:  # noqa: BLE001  pending 是脚本产物，坏在此属异常，按缺失处理
            missing.append(i)
            continue
        dn, _errs = ap.load_done(dp)
        if not dn:
            missing.append(i)
            continue
        pids = {r.get("job_id") for r in pend if isinstance(r, dict) and r.get("job_id")}
        dids = {r.get("job_id") for r in dn if isinstance(r, dict) and r.get("job_id")}
        if len(dids) != len(dn) or pids != dids:
            bad.append(i)
    return {"batches": total, "missing_batches": missing, "bad_batches": bad,
            "all_complete": not missing and not bad}


def merge(args):
    # 读盘前先续租：长 run 的墙钟由最慢 subagent 批次决定，租约窗口只在 prepare 写一次会被耗光
    # （真源 shared/refine_loop.renew_lock，与 skills_analyze.merge 对称）。
    refine_loop.renew_lock(OUTDIR, "job")
    rows, _ = ap.read_done(OUTDIR, PREFIX)   # 缺失/坏批次判定统一交 done_integrity，此处只取行
    specs = observation_specs()
    out, seen, dropped = {}, set(), []
    normalizations, observations, n_normalized = {}, {}, 0
    for row in rows:
        if not isinstance(row, dict) or not soften.coerce_str(row.get("job_id")):
            # 曾静默 continue（`if not jid: continue`）→ 报告盲点；非 dict 与缺 job_id 同归"缺 job_id"
            dropped.append({"job_id": None, "reason": "缺 job_id"})
            continue
        jid = row["job_id"]
        if jid in seen:
            dropped.append({"job_id": jid, "reason": "job_id 重复"})
            continue
        seen.add(jid)
        nr, kinds = normalize_row(row)
        if kinds:
            n_normalized += 1
            for k in kinds:
                normalizations[k] = normalizations.get(k, 0) + 1
        errs = validate_row(nr)             # L0 唯一可丢行判定（非 dict / 缺 job_id / 三列全空）
        if errs:
            dropped.append({"job_id": jid, "reason": "; ".join(errs)})
            continue
        for key, vals in soften.soft_observations(nr, specs).items():
            observations.setdefault(key, []).extend([[jid, v] for v in vals])
        out[jid] = {"hard_gates": nr.get("hard_gates", ""),
                    "must_skills": nr.get("must_skills", ""),
                    "bonus_skills": nr.get("bonus_skills", "")}
    json.dump(out, open(os.path.join(OUTDIR, "jobs_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    integ = done_integrity()   # 盘上产物体检（缺失/坏批次的记录未打标、下周期自动重析）
    # observations 刻意不影响 all_complete：软观察不阻断写回、不打回队列（见 soften 模块 docstring）
    print(json.dumps({"merged": len(out), "batches": integ["batches"],
                      "missing_batches": integ["missing_batches"],
                      "bad_batches": integ["bad_batches"],
                      "dropped_rows": dropped,
                      "normalized": n_normalized,
                      "normalizations": normalizations,
                      "observations": observations,
                      "all_complete": (not integ["missing_batches"]
                                       and not integ["bad_batches"] and not dropped)},
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
