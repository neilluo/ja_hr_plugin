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
python3 skills/match-verify/scripts/match_analyze.py prepare  # 2 自动负载均衡：一岗一 agent 起，硬上限 20
# → prepare 落分派清单 outputs/match_dispatch.json（{"batches":N,"parts":[真实 pending 路径…]}），
#   meta.dispatch 指向它；按清单一次性并发发 agent（≤20，不分波不串行），每个只给：
#   提示词 skills/match-verify/references/match-subagent-prompt.md + 批次号 + 清单里的 part 路径
#   （禁止凭记忆手拼前缀/序号——路径一律复制自 match_dispatch.json）；
#   done 文件名由 agent 从输入自派生（pending→done，N 不变），分派方不指定
python3 skills/match-verify/scripts/match_analyze.py merge    # 3 合并判定（missing_batches 非空则补发该批）
python3 skills/match-verify/scripts/match_analyze.py apply    # 4 只建 keep=true 的配对（幂等：先删该岗位旧的「系统匹配」记录，人工记录不动；写入 ai_analysis）
python3 skills/match-verify/scripts/match_analyze.py stats    # 5 单独一次读取：刷新岗位四项统计
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

**前置门禁（match_gated 阶段1 开头自检）**：三列精析由后台周期任务消费队列异步完成（队列真源
`shared/refine_loop.py`，标记列 `ai_refined_at`）。job 表 0 条、或在岗 must_skills 全空、或
精析队列非空 → 打印 JSON 原因并 exit 2：精析未完成/岗位链未跑，粗值打分无意义。
队列非空可用 `--force` 强跑（结果仅参考）；job 表空与 must_skills 全空 `--force` 也不放行（必出 0 配对）。

子任务承担的判断（原来只能我手工做）：
- **keep 判定**：工序/方向不对应、助理岗降配、技能零交集 → false；
- **语义计分**：同义与单向上下位（禁把暖通/排风/冷却水/空压机互相顶替），部分覆盖可给分但必须写依据；
- **产出**：`evidence`（命中口径一句话）+ `ai_analysis`（结论/亮点/缺口/建议，150-250字），
  由 `match_analyze.py apply` 一并写入 match 表 `AI匹配分析` 列；口径真源在
  `references/match-subagent-prompt.md`。

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
