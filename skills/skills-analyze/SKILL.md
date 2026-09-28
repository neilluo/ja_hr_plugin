---
name: skills-analyze
description: 简历 AI 精析（技能标签 + 结构化分析 + 深度分析）。后台周期消费精析队列：切分批次 → 并发 subagent 精析 → 合并写回 AI 表格并打 ai_refined_at 出队标记。Use when 用户说 分析简历/精析简历/简历AI分析/跑分析/智能分析简历/消费精析队列。
argument-hint: [--batch N] [--since 分钟] [--all] [--ids 文件]
argument-hint-en: [--batch N] [--since min] [--all] [--ids file]
argument-hint-zh: [--batch N] [--since 分钟] [--all] [--ids 文件]
name_en: Resume AI Analyze
name_zh: 简历AI精析
description_en: Periodic background pipeline that consumes the refine queue: parallel subagents refine skill tags, structured analysis and deep analysis from full text, then write back with an ai_refined_at dequeue mark.
description_zh: 后台周期消费精析队列：并行 subagent 精析技能标签、结构化提取、深度解析，写回并打 ai_refined_at 出队标记。
author:
  name: QwenWork
---

# 简历 AI 精析（后台周期流水线）

精析**不由上传同步触发**：上传写完表即结束，本流水线由后台任务消费精析队列
（消费入口 = 上传后 agent 注册的一次性消费任务，注册规格由上传报告的 `cron_job` 字段带出、agent 原样透传
（延迟唯一真源 = `shared/refine_loop.py` 的 `REFINE_DELAY_S`，规格由其 `consume_task_spec` 产出）
+ 每日兜底巡检（时刻见 `refine_loop.FALLBACK_CRON`），触发纪律见
`skills/resume-intake/SKILL.md`；提示词唯一源 = `references/subagent-prompt.md`）。
队列谓词唯一真源 `shared/refine_loop.py`（此处不复述条件）。被触发后**全自动执行，不分步等用户确认**：
查队列 → 切分 → 同一消息内并发 subagent → 合并 → 写回（含出队标记）→ 回读校验。
三列（技能标签 / AI结构化提取 / AI深度解析）+ 基础字段校正（name/major/school/certificates/
years_experience/expected_position，subagent 读全文校正正则粗值，null 不覆盖表内已有值）
**必须由 subagent 读简历全文推理得出**，
脚本的正则词表命中不算结果；表内这些列是普通文本列，平台不会自动算。
校正字段的产出口径唯一源 = `references/subagent-prompt.md`（此处不复述）。

## 流水线（6 步，一步接一步）

以下命令一律以仓库根为 CWD 执行。

### Step 0：查队列（空则直接结束）

```bash
python3 skills/skills-analyze/scripts/skills_analyze.py queue
```

输出 `{"resume": n, "job": m}`（refine_loop.queue_counts）。`resume == 0` 时本轮无事可做，
直接结束，不进 Step 1。

### Step 1：切分

```bash
python3 skills/skills-analyze/scripts/skills_analyze.py prepare
```

候选来源 = 精析队列（`refine_loop.queue(nt, "resume")`），不再按"三列是否为空"推断。
`--all` = 连已精析的一起重析（用户说"全部重跑"时用）；`--since N` 在队列内只看最近 N 分钟上传的；
`--ids file.json` 指定记录 id（不受队列限制）。
**周期租约**：prepare 获取 `outputs/refine_resume.lock`（30 分钟新鲜期——真源 `refine_loop.STALE_AFTER_S`——内拒绝第二个周期，
exit 2 秒退——看到 refused 说明已有周期在跑，直接结束本轮、不要抢跑）；apply 写回时释放。
同周期内重新切批（改 --batch/--since）加 `--force` 夺回自有租约。

**不传 `--batch` 时自动负载均衡**（batch = ceil(条数/20)），**agent 数硬上限 20、不可超过**：
26条→batch 2→**13个agent**；≤20条→一条一个agent；200条→batch 10→20个agent（封顶）。
显式 `--batch N` = 指定每个 subagent 扛几条，agent 数 = ceil(条数/N)，超 20 自动加大 N 压回。
经验：agent 越多总耗时越短（每个 agent 内部是串行的），所以**小批量优先多开 agent**，
不要手动设大 batch。

输出 `outputs/skills_analyze_meta.json`：`{"total":26,"queued":26,"batches":13,"batch_size":2,"agents":13,"agent_sizes":[...],"max_agents":20,"dispatch":"outputs/skills_dispatch.json"}`，
并生成 `outputs/skills_pending_part<N>.json`（N=1..13）、`outputs/job_vocab.json`（岗位技能同源词表）、
**`outputs/skills_prompt_part<N>.md`（每批完整提示词，占位符已填好）**与 `outputs/skills_dispatch.json`（分派清单）。
**meta 的 batches 就是要起的 agent 数，照它发，不要自己另算。**
**dispatch.json 的 prompts 数组就是要发的 N 个提示词文件绝对路径，直接照抄，禁止凭记忆拼路径。**

**total == 0 时**：直接结束（队列已在 Step 0 判过，此处兜底）。

### Step 2：一次性并发启动全部 agent（≤20，不分波）

读 meta 的 `batches = M`，**在同一条消息里一次性发出 M 个 Agent 调用**（M ≤ 20，不分波、不串行）。

**每个 agent 的 prompt 字段只放两行**（不再内联提示词原文，见 AGENTS.md 犯错记录）：

```
Read <dispatch.prompts[N-1]> 文件，把里面全部内容作为你的任务并严格执行。
仓库根：<仓库绝对路径>，命令以仓库根为 CWD 执行。
```

其中 `dispatch.prompts[N-1]` = 从 `outputs/skills_dispatch.json` 直接照抄的第 N 个路径，
**不指定 done 输出文件名**（提示词内已含 `<N>` 展开后的具体 pending/done 路径）。
agent 之间零依赖、各读写自己的文件。**提示词已不含内嵌校验脚本**——done 产物 schema 判定
唯一真源在 `merge`（`validate_row`），subagent 只负责"读输入 → 推理 → 写产物"，不跑自检 bash。

**为什么只发路径**：旧写法把 8.6KB 提示词原文内联进每一个 Agent 工具调用的 prompt 字段，
16 批 ≈ 138KB 工具调用入参，实测第 13 个调用的 prompt 在工具流中途被截断（只剩半段），
只能事后补发批次 14-16 → 16 个 agent 被迫分成 13+3 两波 → 违反"一次性并发发完、不分波"，
多花约 250s 纯串行等待、并多暴露一次后端 stall 窗口。
改为只发路径后派发载荷降两个数量级（每批 ~150 字节），截断诱因消除。

纪律：**分波发送=犯错**（历史教训见 AGENTS.md 犯错记录）——每多一波就多暴露一次后端 stall 窗口，
且多花一轮组装时间。若一条消息实在塞不下 M 个调用，宁可下调 `--batch`（减少批数、每批多扛几条），
也不许把已经切好的 M 批拆成多波。

### Step 3：合并（含盘上产物体检）

```bash
python3 skills/skills-analyze/scripts/skills_analyze.py merge
```

产出 `outputs/skills_done.json`，报告字段：

```
{"merged":N,"batches":M,"missing_batches":[...],"bad_batches":[...],"bad_rows":[...],"all_complete":true|false}
```

`missing_batches` = 该批 done 不存在或 JSON 不可解析；`bad_batches` = 能解析但 id 集合与 pending
不一致 / 有行缺 id；`bad_rows` = **schema 违规**的行（`validate_row` 返回非空 errors），
这些行**已被 merge 从 skills_done.json 剔除、不会写回、其 record 不打 `ai_refined_at` → 下周期自动重析**。
三类都不阻断其余行的写回。`all_complete = true` 当且仅当三类全空。

**schema 唯一真源 = `skills_analyze.validate_row`**（不变量 10），模板与 subagent 不再自校验——
过去每 agent 一个 bash 回合跑内嵌 assert 脚本、失败要再"修正 + 复验"至多 2 回合，
把每 agent 固定开销抬高 30-60s。现 agent 只 Write 产物，merge 一次性扫合规性并把违规行丢回队列。

### Step 4：写回（同批打出队标记）

```bash
python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json
```

自动做：技能标签选项扩展 → **逐条**更新三列 + 基础字段校正（非 null 才写，null 不覆盖）+ `ai_refined_at`（同一次 update，出队凭证）→ 输出
`{"input,updated,options_added,bad,failed"}`。遇到 `the option 'X' is invalid` 会自动剔掉该词重试，
所以 subagent 偶发新词不会阻断写回（该词被丢弃，可在报告 `failed` 为空时视为正常）。
写失败的条目不打标记 → 仍在队列 → 下一周期重试。

### Step 5：回读校验（必须单独一次调用）

```bash
python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json --verify
```

刚写完立刻读会拿到索引前的旧值，所以校验独立成一次调用。报告字段：
`record_not_found`（分析期间被删，忽略）、`readback_mismatch`（下一周期会自动重试）。

最后汇报：队列数、总条数、更新数、失败/缺失批次、耗时。

## Subagent Prompt（唯一源：references/subagent-prompt.md）

提示词全文**只维护一份**：`skills/skills-analyze/references/subagent-prompt.md`（模板，含占位符）。
`prepare` 会把它渲染成 per-batch 的 `outputs/skills_prompt_part<N>.md`（占位符已替换），
分派时**只发该文件的路径指针**（见 Step 2），不再把模板原文内联进工具调用。
模板内的占位符与替换规则：

- `<BATCH_PATH>` → `outputs/skills_pending_part<N>.json` 的绝对路径
- `<N>` → 批次号 N（done 文件名从 pending→done 自派生，N 不变）
- `<VOCAB_PATH>` → `outputs/job_vocab.json` 的绝对路径
- schema 数值/段名/校正字段名 → 由 `render_prompts` 从**唯一真源**注入，模板禁止手抄：
  `<SKILLS_RANGE>`/`<SKILL_ZH_RANGE>`/`<TEXT_MAX>`/`<SEGS_N>`/`<SEG_1..5>` ← `skills_analyze.DONE_*`；
  `<CORR_N>`/`<CORR_1..6>` ← `skills_apply.CORRECTIONS`。改口径只改这两处 .py 常量。

**schema 校验不再内嵌进模板**：done 产物合规性判定唯一真源 = `skills_analyze.validate_row` +
`merge`，违规行 merge 丢弃、record 保持未打 `ai_refined_at` → 下周期自动重析。
subagent 端不跑 bash 校验、不做"修正-复验"多回合，回合省到"读输入 → 推理 → 写产物"三步。

分派方禁止凭记忆拼路径（N 与路径以 `outputs/skills_dispatch.json` 为准），
也禁止把模板原文重新内联回工具调用。

修改提示词只改 references/subagent-prompt.md（模板），**禁止在本文件里再抄一份全文**（双源零容忍）；
`tests/test_dispatch_slim.py` 会锁定"渲染产物必须把上述占位符全部替换干净、且不得复活内嵌校验"。

## 边界与容错

- `--batch N` = 指定每个 agent 扛几条；**不传则自动负载均衡**：batch = ceil(条数/20)。
- **agent 数硬上限 20，不可超过**（`MAX_AGENTS` 只能下调）。小批量尽量多开 agent（≤20 条→一条一个）；
  条数超 20 时自动加大 batch 压回 20。**一次性并发发完，不分波、不串行。**
- 子任务之间零依赖、各读写独立文件；部分失败 merge 自动跳过缺失批次，全部失败才整体失败。
  **subagent 报 failed 时不要立刻补发**：先跑 merge，它已内联盘上产物体检（missing_batches=不存在/
  不可解析、bad_batches=id 集合不一致、bad_rows=schema 违规行），产物齐全即视为该批成功、
  直接进 apply——失败常只发生在产物已落盘后的收尾回合（历史批次 13 即此例：产物已完整，
  收尾回合模型流被 TLS 掐断报 failed，补发白费一轮还覆盖了好产物）。仅当体检报出该批 missing/bad
  时，才补发该批；bad_rows 里的 record 未打标记、下一周期自动重析，也无需手工补发。
- 扫描件（`full_text` 为空、`source_file` 指向本地原件）**照常入队精析**：subagent 用 Read 直接读原件
  （PDF/图片，多页逐页、同一条消息并发发出），读图规则见 `references/subagent-prompt.md`。
  原件已被移动/删除的记录判为不可精析、不入队（防永久卡队列并阻塞匹配门禁），
  prepare 的 meta 会以 `unrefinable` 字段报出，需人工把原件放回原位后重跑。
- 技能标签是 multipleSelect：`skills_apply.py` 写回前自动扩选项，**已有选项必须带 id 回传**。
- 三列无条件逐人精析：**不按硬门槛筛人**——简历库是人才池，不达标者照样分析、照样保留，
  是否进匹配表由「智能匹配」的门槛判定决定。
- 基础字段校正只在 subagent 读到原件真实载明值时给出，`null` 一律不覆盖表内已有值
  （口径唯一源 references/subagent-prompt.md）；这是正则粗值偏差的异步回补层，
  发现明显错误想立即修也可走 sync_ai_columns 手工通道。
- 路径固定为本套件 `skills/skills-analyze/scripts/`，不要照抄其他机器的绝对路径；Windows 用 `py -X utf8`（`python` 别名会静默失败）。
- ai_extract / ai_deep 是文本列，无需选项扩展。
- 零散手工修正（不经队列）仍走 `sync_ai_columns.py payload.json`（手工修正薄通道，实现委托 skills_apply.apply_rows，不打 ai_refined_at）。
