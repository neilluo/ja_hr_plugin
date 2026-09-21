# ai-analysis-spec — 匹配打分与推荐口径

实现：`scripts/match.py`（确定性，无 LLM 参与）。agent 只在语义复核时用算力（见 match-verify）。

## 打分公式

```
必备命中率 = |候选人技能 ∩ 岗位必备| / |岗位必备|      # 子串互含算命中
skill_score = round(100 * must_weight * 必备命中率)    # 岗位无必备技能 → 0
bonus_score = round(100 * bonus_weight * 加分命中率)   # 岗位无加分项 → 0
total_score = skill_score + bonus_score
```

权重取岗位表 `must_weight` / `bonus_weight`（缺省 0.7 / 0.3）。

## 推荐阈值

| total_score | recommend |
|---|---|
| ≥ 70 | 推荐 |
| 40–69 | 待定 |
| < 40 | 不推荐 |

落库门槛：`total_score ≥ --min-score`（默认 1），零分配对不写表。

## 证据格式（evidence 字段）

`必备命中m/M(命中词)；加分命中b/B(命中词)`，空集合省略括号段。
cand_skills/must_skills/bonus_skills/hard_gates 原样冗余进匹配行，便于人工复核不跨表。

## 幂等与统计

- 按 job_id 先删旧匹配再写新匹配；重跑结果稳定。
- 写完后刷新岗位表 stat_total/stat_recommend/stat_pending/stat_reject。
- 回读：每岗位匹配数与写入数一致，不一致 exit 1。

## 语义复核（agent 环节）

确定性打分不处理：技能同义（"暖通运维"≈"HVAC"）、门槛冲突（学历/证书硬条件）。
match-verify skill 让 agent 对「待定」区间做批量语义判定，结论经 Notable.update_records
改 recommend/evidence，source 置「人工匹配」。
