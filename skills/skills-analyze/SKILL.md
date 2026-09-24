---
name: skills-analyze
description: 简历 AI 精析（技能标签 + 结构化分析 + 深度分析）。上传简历后自动启动：切分批次 → 并发 subagent 精析 → 合并写回 AI 表格。Use when 用户说 分析简历/精析简历/简历AI分析/上传简历并分析/跑分析/智能分析简历。
argument-hint: [--batch N] [--since 分钟] [--all] [--ids 文件]
argument-hint-en: [--batch N] [--since min] [--all] [--ids file]
argument-hint-zh: [--batch N] [--since 分钟] [--all] [--ids 文件]
name_en: Resume AI Analyze
name_zh: 简历AI精析
description_en: After resume upload, auto-launch parallel subagents to refine skill tags, structured analysis and deep analysis from full text, then write back to the DingTalk AI Table.
description_zh: 上传后自动并行 subagent 精析技能标签、结构化提取、深度解析，合并写回钉钉 AI 表格。
author:
  name: QwenWork
---

# 简历 AI 精析（自动并发流水线）

本 Skill 被触发后**全自动执行，不分步等用户确认**：切分 → 同一消息内并发 subagent → 合并 → 写回 → 回读校验。
三列（技能标签 / AI结构化提取 / AI深度解析）**必须由 subagent 读简历全文推理得出**，脚本的正则词表命中不算结果；
表内这两列已是普通文本列，平台不会自动算。

## 流水线（5 步，一步接一步）

以下命令一律以仓库根为 CWD 执行。

### Step 1：切分

```bash
python3 skills/skills-analyze/scripts/skills_analyze.py prepare --since 30
```

**不传 `--batch` 时自动负载均衡**（batch = ceil(条数/20)），**agent 数硬上限 20、不可超过**：
26条→batch 2→**13个agent**；≤20条→一条一个agent；200条→batch 10→20个agent（封顶）。
显式 `--batch N` = 指定每个 subagent 扛几条，agent 数 = ceil(条数/N)，超 20 自动加大 N 压回。
经验：agent 越多总耗时越短（每个 agent 内部是串行的），所以**小批量优先多开 agent**，
不要手动设大 batch。

默认只切「最近30分钟上传 且 三列有缺」的记录；用户说"全部重跑"加 `--all`；指定记录用 `--ids file.json`。
输出 `outputs/skills_analyze_meta.json`：`{"total":26,"batches":13,"batch_size":2,"agents":13,"agent_sizes":[2,2,2,2,2,2,2,2,2,2,2,2,2],"max_agents":20}`，
并生成 `outputs/skills_pending_part<N>.json`（N=1..13）与 `outputs/job_vocab.json`（岗位技能同源词表）。
**meta 的 batches 就是要起的 agent 数，照它发，不要自己另算。**

**total == 0 时**：直接告诉用户"没有待分析记录"，不启动 subagent，结束。

### Step 2：一次性并发启动全部 agent（≤20，不分波）

读 meta 的 `batches = M`，**在同一条消息里一次性发出 M 个 Agent 调用**（M ≤ 20，不分波、不串行）。
每个 agent 的指令只给：提示词文件路径 + 批次号 N + 输入/输出 part 路径；agent 之间零依赖，各读写自己的文件。
原则：agent 内部是逐条串行的，所以**在 20 的硬上限内尽量多开 agent**，让总耗时趋近"一条的耗时"；
条数超过 20 时 prepare 自动加大 batch，把 agent 数压回 20 以内。

### Step 3：合并

```bash
python3 skills/skills-analyze/scripts/skills_analyze.py merge
```

产出 `outputs/skills_done.json`；某批次缺失（子任务失败）会在 `missing_batches` 里列出——向用户说明失败批次，
其余照常写回，缺失批次可单独重跑一个 subagent 后再 merge。

### Step 4：写回

```bash
python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json
```

自动做：技能标签选项扩展 → **逐条**更新三列（不用批量：一个非法选项会整批 400）→ 输出
`{"input,updated,options_added,bad,failed"}`。遇到 `the option 'X' is invalid` 会自动剔掉该词重试，
所以 subagent 偶发新词不会阻断写回（该词被丢弃，可在报告 `failed` 为空时视为正常）。

### Step 5：回读校验（必须单独一次调用）

```bash
python3 skills/skills-analyze/scripts/skills_apply.py outputs/skills_done.json --verify
```

刚写完立刻读会拿到索引前的旧值，所以校验独立成一次调用。报告字段：
`record_not_found`（分析期间被删，忽略）、`readback_mismatch`（重跑一次即可）。

最后向用户汇报：总条数、更新数、失败/缺失批次、耗时。

## Subagent Prompt 模板（固化，除两个占位符外一字不改）

每个 subagent 收到以下 prompt，仅替换 `<BATCH_INDEX>`、`<BATCH_PATH>` 和 `<VOCAB_PATH>`：

```
你在协助一个光伏企业（晶澳）HR 系统做简历 AI 精析。这是研究+写一个 JSON 文件的任务，不要调用任何外部 API。

读 <BATCH_PATH> —— 这是一个数组，每条含 id、name、current_skills（脚本正则粗提取的，可能不准/不全）、full_text（简历全文，可能被截断）。
再读 <VOCAB_PATH> —— 这是岗位必备/加分技能词表，选词时优先与它同源（否则后续匹配打分会对不上）。

对每条记录，通读 full_text，产出三个字段：

### 1. skills（技能标签数组）
提炼该候选人真实、可验证的硬技能/专业标签：
- 简体中文，2-6 个字，具体不空泛。禁止"学习能力""团队合作""抗压能力"这类软素质词。
- 优先从 <VOCAB_PATH> 岗位词表与光伏行业词表选（若简历确实涉及）：单晶、拉晶、切片、硅片、电池、组件、镀膜、丝网印刷、扩散、刻蚀、PERC、TOPCon、HJT、工艺、设备、EHS、安全、质量、精益、成本、财务、审计、税务、暖通、电气、机械、PLC、自动化、CAD、SolidWorks、MES、ERP、SAP、Python、项目管理、采购、仓储。
- 词表外确有的硬技能可直接新增（如"焊接机器人""AOI""SPC"）。
- 数量 5-12 个，按重要性排序；纠正 current_skills 里明显错抓的词（如把"Excel/成本"当技能）。
- 全文是乱码/空/与技能无关的，输出 []。
- 不要编造简历中未提及的技能。

### 2. ai_structured（AI 结构化提取，字符串）
固定 5 段，每段一行，段名后用"｜"分隔：
学历背景｜<最高学历> <院校> <专业> <院校层次判断>
工作经验｜<总年限>年，<最近一份工作的公司+岗位+时长>，<行业关键词>
核心技能｜<Top 5 硬技能，逗号分隔>
求职意向｜<期望职位> <期望地点> <期望薪资>
匹配度评估｜<一句话判断适合什么岗位/产线，如"适合单晶拉棒产线设备工程师">
信息缺失用"未提及"填充，不要编造。整段控制在 200 字以内。

### 3. ai_deep（AI 深度解析，字符串）
给 HR 的决策参考：
- 亮点：1-2 条最突出优势，必须具体到项目/指标/金额/年限（如"年运维成本节约1400万元"）
- 风险：1-2 条顾虑（跳槽频繁/空窗/技能与岗位偏差/学历短板等），没有就写"无明显风险"
- 建议：一句话（"建议安排技术面""可作储备""暂不推荐"等）
整段控制在 200 字以内，自然语言，不用列表符号。

### 输出

把结果写入 <BATCH_PATH> 同目录下的 skills_done_part<BATCH_INDEX>.json，格式：
[
  {"id":"recordId","skills":["技能1","技能2"],
   "ai_structured":"学历背景｜...\n工作经验｜...",
   "ai_deep":"亮点：… 风险：… 建议：…"}
]
必须覆盖输入文件里的全部 id。写完后用 `py -X utf8` 校验该 JSON 可解析且 id 集合与输入一致。
回复只需报告条数和 2-3 个典型标签例子，200 字以内。
```

## 边界与容错

- `--batch N` = 指定每个 agent 扛几条；**不传则自动负载均衡**：batch = ceil(条数/20)。
- **agent 数硬上限 20，不可超过**（`MAX_AGENTS` 只能下调）。小批量尽量多开 agent（≤20 条→一条一个）；
  条数超 20 时自动加大 batch 压回 20。**一次性并发发完，不分波、不串行。**
- 子任务之间零依赖、各读写独立文件；部分失败 merge 自动跳过缺失批次，全部失败才整体失败。
  子任务回复没明确说"已写入文件"时，以 merge 的 `missing_batches` 为准，缺哪批补发哪批。
- 技能标签是 multipleSelect：`skills_apply.py` 写回前自动扩选项，**已有选项必须带 id 回传**。
- 三列无条件逐人精析：**不按硬门槛筛人**——简历库是人才池，不达标者照样分析、照样保留，
  是否进匹配表由「智能匹配」的门槛判定决定。
- 路径固定为本套件 `skills/skills-analyze/scripts/`，不要照抄其他机器的绝对路径；Windows 用 `py -X utf8`（`python` 别名会静默失败）。
- ai_structured / ai_deep 是文本列，无需选项扩展。
