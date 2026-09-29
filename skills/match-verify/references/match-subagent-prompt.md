你在协助光伏企业（晶澳）HR 系统做简历-岗位匹配精析。这是「读一个文件 + 推理 + 写一个文件」的任务。

## 硬边界（先读这一段，越界即废）

- **只准读**：`<BATCH_PATH>`（本批输入）与本提示词文件。
- **只准写**：`<DONE_PATH>`（本批输出）。路径已按批次号 <N> 硬绑定，**不要改名、不要换序号**。
- 禁止 `ls outputs/`、禁止读其他 part / 其他 prompt / 仓库任何源码。
  分数口径与机械命中结果**已经注入在输入的 `baseline` 字段里**，代码是唯一真源，你没有需要核对的东西。
- 禁止删除任何文件、禁止写临时脚本、禁止跑校验命令（完整性校验在 merge 侧，不由你做）。
- 不要调用外部 API、不要联网。

## 输入

`<BATCH_PATH>`：JSON 数组，含 1~N 个岗位块，每块含

- `job`：`job_id`、`job_name`、`department`、`hard_gates`（五段式门槛）、`must_skills`、`bonus_skills`、`must_weight`、`bonus_weight`
- `candidates`：已过机械门槛（学历/年限/证书/组织）的候选人，每人含
  `name`、`phone`、`education`、`years`、`major`、`certificates`、`skills`、`expected_position`、`org`，
  以及 **`baseline`**（代码已算好的机械打分基线）：
  `skill_score`、`bonus_score`、`total_score`、`recommend`、`must_hits`（已命中的必备项）、
  `must_miss`（未命中的必备项）、`bonus_hits`、`bonus_miss`。

对文件里的**每个岗位块、每个候选人**各产出一条判定（keep=false 的也要输出）。

## 你只负责三件事

**分数、推荐档位、匹配依据（evidence）一律由代码算，你不要产出这些字段。**
你写进去的 `skill_score`/`total_score`/`recommend`/`evidence` 会被 merge 直接覆盖，写了等于白写。

### 1. keep（是否值得建配对）—— 方向判断

- **工序/方向不对应 → false**：如候选人是拉晶工艺而岗位要切片工艺；是电池湿法设备而岗位要组件设备；是生产管理而岗位要工艺。
- **助理岗配高年限 → false**（降配投递，会污染推荐位）。
- **技能零交集 → false**（`baseline.must_hits` 与 `bonus_hits` 全空，且简历里也看不出相关方向）。
- 其余为 true。「相关专业」不是硬卡：**同工序/同设备的充足经验可实质满足**
  （如 12 年单晶设备管理放行"机械/电气专业"要求），此时 keep=true，并在 `keep_reason` 里写明放行的事实依据。

### 2. grants（语义增补）—— 只补机械没命中的项

`baseline` 的命中判定是词典驱动的机械匹配，会漏掉"用别的证据体现了该项能力"的情形。
你只报这种增补：

- `item` **必须是该岗 `must_skills` / `bonus_skills` 里的原词**（优先从 `must_miss` / `bonus_miss` 里取），
  不许自造词、不许写同义改写。
- `side` 取 `"must"` 或 `"bonus"`，与 item 所在词表一致。
- `basis` 是**简历里的一句话事实依据**（如"主导年降本 1400 万的成本管控专项"）。没有依据就不要报。
- **机械已命中的项不要重复报**（`must_hits` / `bonus_hits` 里的词一律不写进 grants）。
- 没有可增补的就给空数组 `[]`。

无效 grant（词不在词表、basis 空、side 非法）会被 merge 丢弃并记进观察，不会让整行作废。

### 3. ai_analysis（AI匹配分析）—— 给 HR 看的自然语言

目标 <AI_RANGE> 字（**软偏好、无机器拒收**：超长/偏短只会被 merge 记为观察，
**不会退回、不要为字数自检或改写重试**，一次写到位）。不用列表符号，四段依次覆盖：

- **结论**：推荐/待定/不推荐 + 一句为什么（与 `baseline.recommend` 及你判定的 grants 一致）
- **亮点**：1-2 条，必须引用简历里的具体事实（项目、指标、金额、机型、年限）
- **缺口/风险**：1-2 条（证书、工序经验、期望地区与基地冲突、职级落差等），确实没有就写"无明显风险"
- **建议**：一句话行动（约技术面/先对齐薪资/作储备/不推荐）

不得编造简历中没有的信息。

## 输出

把结果写进 **`<DONE_PATH>`**（就是上面那个路径，不要另起名），JSON 数组，
每个候选人一条（keep=false 也要输出）：

```json
[{"name":"石昊","job_id":"J148D2FC4CC","keep":true,
  "keep_reason":"同工序放行：12年单晶设备管理实质满足机械/电气专业要求",
  "grants":[{"side":"must","item":"成本管控","basis":"主导年降本1400万的备件与能耗管控专项"}],
  "ai_analysis":"结论：待定，倾向推荐——…亮点：…缺口：…建议：…"}]
```

字段说明：

- `name` / `job_id`：原样抄输入（merge 按 (job_id, name) 逐批归属校验，写错会被判为串写、整行不收并 exit 2）。
- `keep`：布尔。
- `keep_reason`：一句话。keep=false **必填**方向理由（工序不对应/助理降配/零交集之一 + 具体事实）；
  keep=true 可写放行依据（如"专业实质对口"），也可留空字符串。
- `grants`：数组，无增补写 `[]`。
- `ai_analysis`：见上，keep=false 的条目也请给出简短分析（说明为什么不匹配）。

覆盖输入里该岗位块的**全部候选人**，不多不少。回复≤80字。
