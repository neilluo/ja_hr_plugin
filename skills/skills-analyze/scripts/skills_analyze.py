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
import soften                # noqa: E402  归一化+软观察唯一真源（L2 阈值在此，禁止本地抄）

OUTDIR = os.path.join(ROOT, "outputs")
PREFIX = "skills"
FULL_TEXT_MAX = 6000
# 提示词唯一源（模板）；per-batch 渲染产物见 render_prompts
PROMPT_TPL = os.path.join(ROOT, "skills", "skills-analyze", "references", "subagent-prompt.md")
# 队列模式的补充字段（full_text / source_file 已由 refine_loop 队列带回，不重复拉）
META_BIZ = ["name", "skills", "upload_time"]
BIZ = META_BIZ + ["full_text", "source_file"]

# ── done 产物 schema（L0 硬门槛）+ 归一化口径（不变量 10）──────────────────────
# L0（确实无法写回：非 dict、缺/重 id、归一化后三列全空）才丢行；由 validate_row + merge 判定。
# 一切质量/审美问题（标签字数、技能数量、文本长度、段名/格式）一律先由 normalize_row 自动修复，
# 修不了也只进 soft_observations 非阻断观察——**观察不参与 all_complete、不丢行、不阻断写回**。
# L2 阈值常量唯一真源 = shared/soften.py（TAG_LEN_MAX/ZH_RANGE/TAGS_*），此处禁止抄数值。
DONE_REQUIRED_FIELDS = ("skills", "ai_structured", "ai_deep",
                        "name", "major", "school", "certificates",
                        "years_experience", "expected_position")
DONE_NULLABLE_STR_FIELDS = ("name", "major", "school", "certificates", "expected_position")
DONE_STRUCTURED_SEGS = ["学历背景", "工作经验", "核心技能", "求职意向", "匹配度评估"]
DONE_TEXT_MAX = FULL_TEXT_MAX  # 仅观察口径（oversize_text）：超长只报告不丢行；
                               # 长度软约束由 prompt 负责（模板注入 <TEXT_MAX>，真源 FULL_TEXT_MAX）


def normalize_row(r):
    """把 subagent 产出的一行归一成可写回形态。返回 (row, kinds)；不改入参、构造新 dict。

    修复而非报错（历史事故：7 字术语"热镀铝锌硅钢板"触发旧字数硬门槛 → 整行读图产物被丢、
    队列卡死 ~11 小时并阻塞匹配门禁；约束强度必须匹配违规可逆性）：
      - ai_structured 经 soften.normalize_segments 定序补齐五段（空行/半角竖线/缺段/数组均自动修）；
      - ai_deep / name / major / school / expected_position 经 coerce_str（数组→顿号串、数字→字符串）；
      - certificates 经 join_list（数组→"、"连接字符串）；
      - years_experience 经 coerce_int（"1年"→1、1.5→2、bool→None）；
      - DONE_REQUIRED_FIELDS 缺键补 None（缺的可空校正字段 ≡ null，绝不算错误）。
    kinds 为修复动作短标签列表（供 merge 聚合报告），空 = 该行本已规范。
    """
    kinds = []
    if not isinstance(r, dict):
        return r, []          # 非 dict 交 validate_row 作 L0 拦下
    out = {"id": r.get("id")}
    st, ks = soften.normalize_segments(r.get("ai_structured"), DONE_STRUCTURED_SEGS)
    kinds.extend(ks)
    # 兜底成字符串：skills_apply 写回侧对 ai_extract/ai_deep 直接取用（_cast text 列），
    # 非字符串会在 PUT 时才炸、且报错信息不指向本行；在此收口保证 L0 之后恒为 str。
    out["ai_structured"] = st if isinstance(st, str) else soften.coerce_str(st) or ""
    raw_dp = r.get("ai_deep")
    dp = soften.coerce_str(raw_dp) or ""
    # 与"已归一形态"比较（None 与 "" 视为等价），否则空值行每轮都记一次 coerced_str、
    # 破坏 normalize_row 的幂等契约（第二次调用必须 kinds=[]）
    if dp != (raw_dp.strip() if isinstance(raw_dp, str) else ""):
        kinds.append("coerced_str")
    out["ai_deep"] = dp
    for f in DONE_NULLABLE_STR_FIELDS:
        # certificates 走 join_list（数组→顿号串）；其余 coerce_str（list 亦委托 join_list）
        v = soften.join_list(r.get(f)) if f == "certificates" else soften.coerce_str(r.get(f))
        if v != r.get(f):
            kinds.append("list_joined" if isinstance(r.get(f), (list, tuple)) else "coerced_str")
        out[f] = v
    ye = soften.coerce_int(r.get("years_experience"))
    if ye != r.get("years_experience"):
        kinds.append("coerced_int")
    out["years_experience"] = ye
    sk = r.get("skills")
    out["skills"] = [str(x).strip() for x in sk if str(x).strip()] \
        if isinstance(sk, (list, tuple)) else (toks(sk) if isinstance(sk, str) else [])
    if out["skills"] != sk:
        kinds.append("skills_coerced")
    for f in DONE_REQUIRED_FIELDS:
        if f not in r:
            kinds.append("field_defaulted")   # 缺的可空校正字段 ≡ null，补 None 不算错误
    return out, kinds


def validate_row(r):
    """L0 硬门槛（唯一可丢行的判定）：只报"确实无法写回"的问题，返回错误字符串列表（空=可写回）。

    预期在 normalize_row 之后调用。L0 仅两条：非 dict；三列（skills/ai_structured/ai_deep）
    归一化后全空——无任何可写回内容。
    第二条**必须在 merge 拦下**，不能留给 skills_apply 的 require_three：apply 侧把该行记进
    `bad` 而不写回、不打 `ai_refined_at`，于是它永远留在精析队列、每周期重析（无限循环），
    且报告里只有 merged 计数看不出少了谁。放这里则进 `dropped_rows`（带 id 与 reason）可见。
    与 JD 链 jobs_analyze.validate_row 的"三列全空"L0 口径对称。
    质量/审美检查（标签字数、技能数量、文本长度、段名、缺可空字段、years_experience 类型、
    校正字段 list-vs-str）一律不在这里——已移入 soft_observations（非阻断，只报告）。
    """
    if not isinstance(r, dict):
        return ["行不是 dict（%s）" % type(r).__name__]
    sk = [x for x in (r.get("skills") or []) if str(x).strip()]
    if not (sk or str(r.get("ai_structured") or "").strip()
            or str(r.get("ai_deep") or "").strip()):
        return ["三列全空（skills/ai_structured/ai_deep 归一化后均无内容）"]
    return []


def observation_specs():
    """简历链软观察规格：soften 数据驱动 SOFT_SPECS（标签类）+ 本链专有的 oversize_text。
    返回值仅用于 merge 报告，禁止参与任何丢行/退出决策。"""
    return tuple(soften.SOFT_SPECS) + (
        {"key": "oversize_text", "field": "ai_structured/ai_deep",
         "check": lambda row: [f for f in ("ai_structured", "ai_deep")
                               if isinstance(row.get(f), str) and len(row[f]) > DONE_TEXT_MAX]},
    )


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
    # 数值/段名/校正字段名从唯一真源注入模板（不变量 10）：模板禁止手抄，改口径只改真源一处。
    # 真源分工：软阈值（标签字数/数量/文本长度）在 shared/soften.py，段名在本文件，
    # 校正字段名在同 skill 的 skills_apply.py。
    # 注意：注入的是**软偏好**文案所需的数字，模板措辞不得把它们说成机器强制——
    # validate_row 只留 L0，超长/数量偏少一律进 observations 不丢行（见 soften 模块 docstring）。
    from skills_apply import CORRECTIONS   # 延迟 import：render 才需要，避免模块加载环
    subs = {"<SKILLS_RANGE>": "%d-%d" % soften.TAGS_RANGE,
            "<SKILL_ZH_RANGE>": "%d-%d" % soften.ZH_RANGE,
            "<TEXT_MAX>": str(DONE_TEXT_MAX),
            "<SEGS_N>": str(len(DONE_STRUCTURED_SEGS)),
            "<CORR_N>": str(len(CORRECTIONS))}
    for i, seg in enumerate(DONE_STRUCTURED_SEGS, 1):
        subs["<SEG_%d>" % i] = seg
    for i, k in enumerate(CORRECTIONS, 1):
        subs["<CORR_%d>" % i] = k
    # 清旧 prompt 文件：write_parts 只清 pending/done，prompt 是本函数产物须自清，
    # 否则上轮大批次（如 16）残留的 prompt_part15/16.md 会在小批次（如 2）轮里成僵尸文件。
    ap.prune_prompts(OUTDIR, PREFIX)
    paths = []
    for i in range(1, n_batches + 1):
        pend = ap.pending_path(OUTDIR, PREFIX, i)   # 路径经公共骨架派生，禁止本地抄 parts 命名
        body = (tpl.replace("<BATCH_PATH>", pend)
                   .replace("<VOCAB_PATH>", vocab_path))
        for ph, val in subs.items():
            body = body.replace(ph, val)
        body = body.replace("<N>", str(i))   # <N> 最后替换：防其他占位符名里含 "<N" 子串误伤
        out = ap.prompt_path(OUTDIR, PREFIX, i)
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        paths.append(out)
    return paths



def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def done_integrity():
    """盘上 done 产物体检（只读）：批次数、缺失批次、id 集合不一致/缺行的坏批次。
    把 SKILL.md"failed 先验盘上产物再决定补发"这条人工纪律做成机器判定。
    解析口径与 read_done 同走 ap.load_done（单源，禁止本函数另抄一套 json.load，
    否则会出现"merged 已含抢救行、体检仍报整批缺失"的自相矛盾）：
    抢救后一行不剩 = missing（补发该批）；剩了但 id 集合不全 = bad（同样整批补发，
    补发产物 merge 时覆盖抢救版，同批好行不丢数据；抢救的意义在于 merged 计数如实、
    报告不再自相矛盾，并为 apply 先行部分写回留好通路）。"""
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
        pids = {r.get("id") for r in pend if isinstance(r, dict) and r.get("id")}
        dids = {r.get("id") for r in dn if isinstance(r, dict) and r.get("id")}
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
    if refine_loop.acquire_lock(OUTDIR, "resume", force=force) is None:
        print(json.dumps({"refused": True,
                          "reason": "另一精析周期持锁中（outputs/refine_resume.lock 租约未过期）：本轮跳过避免并发双写；"
                                    "确属本周期重切批则加 --force"}))
        sys.exit(2)
    nt = Notable()
    since = None   # 不传 --batch 时自动铺满 agent
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
        paths = render_prompts(meta["batches"], vocab_path)
        meta["dispatch"] = ap.write_dispatch(OUTDIR, PREFIX, paths)  # 清单形状唯一真源在公共骨架
        json.dump(meta, open(ap.meta_path(OUTDIR, PREFIX), "w", encoding="utf-8"))
    return meta



def merge(args):
    # 读盘前先续租：一个 run 的墙钟由最慢 subagent 批次决定，租约判活窗口只在 prepare 写一次
    # 就会被工作本身耗光（实测一次 26 分 10 秒，距 30 分钟失效仅剩 4 分 11 秒），
    # 过期后并存的其他消费周期会接管并清掉已落盘的好产物。真源 shared/refine_loop.renew_lock。
    refine_loop.renew_lock(OUTDIR, "resume")
    rows, _ = ap.read_done(OUTDIR, PREFIX)   # 缺失/坏批次判定统一交 done_integrity，此处只取行
    specs = observation_specs()
    done, seen, dropped = [], set(), []
    normalizations, observations, n_normalized = {}, {}, 0
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            dropped.append({"id": None, "reason": "缺 id"})   # 曾静默跳过，现为报告盲点补齐
            continue
        rid = row["id"]
        if rid in seen:
            dropped.append({"id": rid, "reason": "id 重复"})
            continue
        seen.add(rid)
        nr, kinds = normalize_row(row)
        if kinds:
            n_normalized += 1
            for k in kinds:
                normalizations[k] = normalizations.get(k, 0) + 1
        errs = validate_row(nr)             # L0 唯一可丢行判定（非 dict / 三列全空）
        if errs:
            dropped.append({"id": rid, "reason": "; ".join(errs)})
            continue
        for key, vals in soften.soft_observations(nr, specs).items():
            observations.setdefault(key, []).extend([[rid, v] for v in vals])
        done.append(nr)
    json.dump(done, open(os.path.join(OUTDIR, "skills_done.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    integ = done_integrity()   # 盘上产物体检（缺失/坏批次的记录未打标、下周期自动重析）
    # observations 刻意不影响 all_complete：软观察不阻断写回、不打回队列（见 soften 模块 docstring）
    print(json.dumps({"merged": len(done), "batches": integ["batches"],
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
