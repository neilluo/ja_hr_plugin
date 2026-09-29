---
name: match-verify
description: 门槛前置机械判定(match_gated) + 逐岗 subagent 并发语义分析(match_analyze)，结论回写 match 表。Use when 用户说 跑匹配/智能匹配/谁适合这个岗位。
argument-hint: [--commit|--stats|--force] [prepare|merge|apply|stats]
argument-hint-en: [--commit|--stats|--force] [prepare|merge|apply|stats]
argument-hint-zh: [--commit|--stats|--force] [prepare|merge|apply|stats]
name_en: Match & Verify
name_zh: 智能匹配与复核
description_en: Gate-first mechanical screening (match_gated) plus per-job subagent semantic analysis (match_analyze), written back to the match sheet.
description_zh: 门槛前置机械判定(match_gated) + 逐岗 subagent 并发语义分析(match_analyze)，结论回写 match 表。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 智能匹配与复核

打分口径见 `../recruit-model/references/ai-analysis-spec.md`（公式/阈值/证据格式）。

## 完整链路（门槛前置 + 逐岗 subagent 并发，agent 数硬上限见 shared/waves.MAX_AGENTS(=20)）

```bash
python3 skills/match-verify/scripts/match_gated.py            # 1 机械门槛（组织/学历/年限/证书/年龄）→ outputs/gate_pairs.json
python3 skills/match-verify/scripts/match_analyze.py prepare  # 2 自动负载均衡：一岗一 agent 起，硬上限 20；注入 baseline + 渲染 per-batch 提示词
# → prepare 渲染 outputs/match_prompt_part<N>.md（每批提示词已把本批 <BATCH_PATH>/<DONE_PATH> 硬绑定），
#   并落分派清单 outputs/match_dispatch.json（{"batches":N,"prompts":[渲染提示词路径…]}），meta.dispatch 指向它；
#   按清单一次性并发发 agent（≤20，不分波不串行），**每批只发一行 match_prompt_part<N>.md 路径指针**
#   （禁止内联提示词原文撑爆工具流；禁止凭记忆手拼 part 号——路径一律复制自 match_dispatch.json）。
#   agent 只读它那份提示词里指定的 <BATCH_PATH>、只写指定的 <DONE_PATH>（done 文件名不再由 agent 自派生）。
python3 skills/match-verify/scripts/match_analyze.py merge    # 3 归属校验 + 分数重算（misattributed/missing_batches 非空 → 报告后 exit 2）
python3 skills/match-verify/scripts/match_analyze.py apply    # 4 只建 keep=true 的配对（幂等：先删该岗位旧的「系统匹配」记录，人工记录不动；写入 ai_analysis）
python3 skills/match-verify/scripts/match_analyze.py stats    # 5 单独一次读取：刷新岗位四项统计
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

**前置门禁（match_gated 阶段1 开头自检）**：三列精析由后台周期任务消费队列异步完成（队列真源
`shared/refine_loop.py`，标记列 `ai_refined_at`）。job 表 0 条、或在岗 must_skills 全空、或
精析队列非空 → 打印 JSON 原因并 exit 2：精析未完成/岗位链未跑，粗值打分无意义。
队列非空可用 `--force` 强跑（结果仅参考）；job 表空与 must_skills 全空 `--force` 也不放行（必出 0 配对）。

**v2 分派契约（2026-09-29 事故重构，见 AGENTS.md 犯错记录）**：agent 只做方向判断与语义增补，
**分数/evidence 一律代码算**——
- prepare 给每个候选人注入 `baseline`（机械命中 + skill/bonus/total/recommend，经 `match_gated.hits`/
  `score_counts`，公式唯一真源在 match_gated）；每批渲染独立提示词、读写路径硬绑定本批（杜绝跨批误写）。
- 子任务只产出三件事：`keep`（是否建配对）+ `keep_reason`（一句话方向理由）+ `grants`（机械没命中、
  但语义该命中的增补，item 必须是岗位 must/bonus 词表原词、basis 是简历里的一句话依据）+ `ai_analysis`
  （给 HR 看的结论/亮点/缺口/建议，目标 150-250 字，软偏好只观察不退回）。agent **不再产出分数/evidence**。
- merge 逐批归属校验（done_part<i> 里 (job_id,name) 不在 pending_part<i> 期望集合 = 串写，进报告
  `misattributed` 不进 rows）+ 用 `score_counts` 重算 keep 行分数（机械命中 ∪ 有效 grants）、代码组装
  evidence（grant 命中项以「项※(依据)」形态出现）；drop 行分数/evidence 统一 None；
  `misattributed` 或 `missing_batches` 非空 → **报告先打（agent 仍能看到 JSON）再 exit 2**，禁止带串写/缺批产物继续 apply。

子任务承担的判断（原来只能我手工做）：
- **keep 判定**：工序/方向不对应、助理岗降配、技能零交集 → false；
- **grants 语义增补**：机械词典没命中、但用别的证据体现了该项能力（如"年降本1400万"证明成本管控），
  报 item（词表原词）+ basis（简历依据）；机械已命中的不重复报。同义/上下位判定仍以
  `semantic_score.py` 的 SYNONYM/HYPERS 为准（baseline 已据此算好机械命中）。
- **ai_analysis**：结论/亮点/缺口/建议，由 `match_analyze.py apply` 一并写入 match 表 `AI匹配分析` 列；
  口径真源在 `references/match-subagent-prompt.md`（该模板由 `match_analyze.render_prompts` 渲染成 per-batch 提示词）。


硬性纪律：
- **agent 数硬上限 20**（`shared/waves.py`，`MAX_AGENTS` 只能下调）：默认一岗一 agent，超过 20 才自动加大
  每 agent 的岗位数；一次性并发发完，**不分波、不串行**。三个环节（分析JD/分析简历/匹配）共用同一调度。
- **不用分数阈值代替方向判断**：`MIN_SCORE=1` 实测会灌进 174 对跨方向噪声，调高又会漏人——
  方向由子任务逐对判，阈值（`match_gated` 的 `MIN_SCORE` 环境变量，默认值以代码为准）只用于剔除零交集。
- **写完必须另起一次读取**（stats 独立执行），同一脚本里回读会拿到索引前的旧值。

- **智能匹配是三个触发点里的第三步**：先「智能分析JD」（门槛与技能词表已写好）→再「智能分析简历」
  （技能标签/两列文本已写好）→才跑本流程；两侧没分析完就匹配等于拿词表噪声打分。
- **不达标的人不建配对，但简历保留在库里**（简历库是人才池，不做删除、不写"暂不匹配"备注）。
- `outputs/gate_pending.json` 是"机械门槛过了、但专业/工序是否对口需要判断"的清单：智能体逐条判
  「方向不对/工序不对应→从 gate_pairs.json 剔除；对口经验充分→实质放行」，并把理由写进剔除日志。
  走 match_analyze 子任务链路时该判定由 subagent 的 keep 结论承担；若走 `match_gated.py --commit`
  直连打分链路，则必须先人工审 gate_pending 再 --commit。**这一步不能用字符串相等替代，也不能省略。**
- `match_gated.py --commit` 是无 subagent 的兜底直连链路（机械打分直接落库），常规流程以
  match_analyze 的判定为准，二者不要混跑。

## 边界

- 打分全量走 subagent 判定：机械门槛只做一票否决，分数与推荐结论以子任务 keep/计分为准。
- 匹配表行数 = outputs/gate_pairs.json 中 keep=true 的配对数；被剔除与零分配对不写表。
- 岗位统计四字段由本流程刷新，入库脚本不写。
- 统计与核对**必须与写入分开一次读取**：同一次脚本里写完立刻回读会拿到索引前的旧值。
