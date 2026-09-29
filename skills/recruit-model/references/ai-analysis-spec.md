# ai-analysis-spec — 匹配打分与推荐口径

实现分两段（唯一主链路，代码为准）：

1. `skills/match-verify/scripts/match_gated.py` —— 机械门槛一票否决 + 语义打分，
   产出 `outputs/gate_pairs.json`（达标配对）与 `outputs/gate_pending.json`（门槛过了但
   专业/工序是否对口需判定的清单）。`--commit` 是无 subagent 的兜底直连落库，`--stats` 单独刷新岗位统计。
2. `skills/match-verify/scripts/match_analyze.py prepare → merge → apply → stats` —— 逐岗 subagent
   并发做语义判定（keep/计分/evidence/ai_analysis），常规流程以此为准。agent 数硬上限见
   `shared/waves.MAX_AGENTS`，一次性并发发出、不分波不串行。子任务提示词：
   `skills/match-verify/references/match-subagent-prompt.md`。

## 机械门槛（match_gated.gate，先于打分）

一票否决、缺证据即不通过：组织一致、学历档次（`LEVEL` 映射博士/硕士/本科/大专）、
经验年限、证书、年龄（简历有年龄才判）。「相关专业 / 工序是否对口」不机械否决，
先尝试语义自动放行，判不动的写进 `gate_pending.json` 交判定环节。

## 打分公式

```
必备命中率 = |候选人技能 ∩ 岗位必备| / |岗位必备|
skill_score = round(100 * must_weight * 必备命中率)    # 岗位无必备技能 → 0
bonus_score = round(100 * bonus_weight * 加分命中率)   # 岗位无加分项 → 0
total_score = skill_score + bonus_score
```

权重取岗位表 `must_weight` / `bonus_weight`（回落语义唯一真源 `match_gated.weights`：
仅 None/空串回落默认，0.0 是合法业务值保留；公式与档位唯一真源 `match_gated.score_counts`，
本段仅镜像、改代码须同步本段）。

命中判定**不是字面相等、也不是子串互含就算完**：统一走
`skills/match-verify/scripts/semantic_score.py` 的 `hit()` —— 同义词典 `SYNONYM`（双向等价）+
上下位词典 `HYPERS`（单向：候选人写的具体项可满足岗位的宽泛项，反向不算）+ 保守字面包含。
分词与技能词表唯一源是 `shared/vocab.py`（`toks` / `SEP` / `SKILL_WORDS`）。
公式与档位的唯一实现是 `match_gated.score_counts`（命中判定 `match_gated.hits`、证据串
`match_gated.evidence`）：机械打分（stage_gate/--commit）与 subagent 链共用同一套，禁止第二份公式副本。

**v2 分派契约（分数由代码算，不再由 subagent 手算）**：`match_analyze.py prepare` 给每个候选人注入
`baseline`（经 hits+score_counts 算好的机械命中与 skill/bonus/total/recommend）；subagent 只产出
keep/keep_reason/grants（机械没命中、语义该命中的增补，item 须是岗位词表原词、basis 是简历依据）/
ai_analysis。`match_analyze.py merge` 用 score_counts 重算 keep 行分数（机械命中 ∪ 有效 grants）、
代码组装 evidence，并逐批做 (job_id,name) 归属校验（串写进 `misattributed`、不收，报告后 exit 2）。
机械门槛只做一票否决与初筛，方向对不对由 subagent 的 keep 判定；分数与 evidence 恒由代码产出。

## 推荐阈值

推荐/待定/不推荐的分数阈值唯一真源是 `match_gated.py` 的 `REC_MIN` / `PEND_MIN`，
本文档与 prompt 只引用不复述数值；三个状态标签按顺序派生自
`config.json options.match.recommend`，不在代码里另抄一份。

落库门槛：`total_score ≥ MIN_SCORE`（**环境变量**，默认值以 `match_gated.py` 代码为准）。
门槛过了但技能几乎无交集的
人-岗不建废配对；方向对不对由 subagent 的 keep 判定，不靠调阈值。

## 证据格式（evidence 字段）

由代码组装（唯一实现 `match_gated.evidence`，agent 不产出该字段）：

```
语义匹配：必备x/y（主要命中项，最多 8 项）；加分z/w；未命中：…（最多 6 项，无则省略）
```

subagent 报的有效 grant 命中项，在命中列表里以「项※(依据)」形态出现（依据 = 该 grant 的 basis）。

cand_skills / must_skills / bonus_skills / hard_gates / expected_position / years_experience
原样冗余进匹配行，便于人工复核不跨表。`ai_analysis`（AI匹配分析）由 subagent 产出结论/亮点/
缺口/建议，随配对一并写入。

## 幂等与统计

- `match_analyze.py apply`：先按本次涉及的 job_id 删除 match 表旧配对，再只建 `keep=true` 的
  新配对（含 evidence / ai_analysis），重跑结果稳定。
- `match_gated.py --commit`：先按 (name, job_id) 删除旧系统匹配记录再写，与 apply 是两条
  不要混跑的落库路径。
- 统计**必须另起一次读取**：`match_analyze.py stats` / `match_gated.py --stats` 独立执行，
  重新读 match 表后刷新岗位表 `stat_total / stat_recommend / stat_pending / stat_reject`
  （位次同样派生自 `config.options.match.recommend` 顺序）。同一脚本里写完立刻回读会拿到
  索引前的旧值。

## 语义判定的分工

机械层不做同义/上下位归并之外的推理；`gate_pending.json` 里的「专业/工序是否实质对口」、
助理岗降配、跨方向噪声，一律由 subagent 逐对给 keep 结论并写依据，
结论经 `match_analyze.py apply` 落库，`source` 统一写「系统匹配」（真源 `config.options.match.source`）。
