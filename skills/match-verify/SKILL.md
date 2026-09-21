---
name: match-verify
description: 智能匹配两步走：先跑 skills/match-verify/scripts/match.py 确定性打分落库并刷新岗位统计，再由 agent 对「待定」区间做批量语义复核（技能同义/门槛冲突），结论回写 match 表。Use when 用户说 跑匹配/智能匹配/匹配复核/刷新匹配/谁适合这个岗位。
argument-hint: [--job-id Jxxx] [--min-score N]
argument-hint-en: [--job-id Jxxx] [--min-score N]
argument-hint-zh: [--job-id Jxxx] [--min-score N]
name_en: Match & Verify
name_zh: 智能匹配与复核
description_en: Deterministic scoring into the match sheet plus agent semantic review of borderline pairs, written back via OpenAPI.
description_zh: 确定性打分落库 + agent 对边界配对语义复核并回写。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 智能匹配与复核

打分口径见 `../recruit-model/references/ai-analysis-spec.md`（公式/阈值/证据格式）。

## 第一步：确定性打分（一条命令，agent 不介入）

```bash
python3 skills/match-verify/scripts/match.py                      # 全量：所有岗位 × 所有简历
python3 skills/match-verify/scripts/match.py --job-id J64B1DFFB12 # 单岗位
python3 skills/match-verify/scripts/match.py --dry-run            # 预演，stdout 含全部 rows
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

脚本自带：按 job_id 先删旧匹配（幂等）、落库、刷新岗位 stat_*、按岗位回读比对。
`failed` 非空或 exit 1 时重跑一次再看 error。

## 第二步：语义复核（agent 唯一动脑环节，可选）

对 `recommend=待定` 的配对做批量判定，一次回合处理完：

1. `python3 shared/query.py match --filter recommend=待定 --fields name,job_name,cand_skills,must_skills,hard_gates,evidence`
2. 逐条判断：技能是否同义（暖通运维≈HVAC）、hard_gates 是否硬冲突（学历/证书/年限）。
   结论三选一：升「推荐」/降「不推荐」/维持「待定」。
3. 回写（source 置「人工匹配」）：

```bash
python3 -c "
import sys; sys.path.insert(0,'shared')
from notable import Notable
nt = Notable()
rows = nt.list_records('match', flt={'recommend':'待定'}, biz_fields=[])
# 按 id 更新：nt.update_records('match', [{'id': <recordId>, 'recommend':'推荐',
#   'source':'人工匹配', 'evidence':'语义复核：暖通运维=HVAC 同义'}])
"
```

4. 复核后跑 `python3 shared/query.py match --stats` 给用户看分布。

## 边界

- 打分零 LLM：同义与门槛冲突才用 agent，别对「推荐/不推荐」区间重复劳动。
- 匹配表行数 = 岗位×有技能简历 中 total≥min-score 的配对数；零分配对不写表。
- 岗位统计四字段由本流程刷新，入库脚本不写。
