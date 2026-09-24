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

## 第三步（必做）：生成「AI匹配分析」

匹配表的 AI匹配分析 已是普通文本列，平台不会自动算，必须由智能体产出后回写：

```bash
python3 skills/match-verify/scripts/sync_match_analysis.py      # 全量覆盖式回写，幂等
```

分级口径（脚本已实现，新配对只需往 `DETAIL` 补人工分析后再跑）：
- **推荐 / 待定**：人工写四段【命中】【门槛】【缺口】【建议动作】，必须引用简历里的具体事实
  （金额、指标、机型、公司、年限），不得写通用套话；
- **不推荐**：按「命中项 + 未命中项 + 门槛原文 + 一句判定」生成，判定只允许来自规则表：
  降配投递（助理岗配高年限）／经验不足／跨方向匹配（技能零交集）／必备覆盖不足；
  不得编造简历中没有的事实。

## 完整链路（门槛前置 + 逐岗 subagent 并发，agent数硬上限20）

```bash
python3 skills/match-verify/scripts/match_gated.py            # 1 机械门槛（组织/学历/年限/证书/年龄）→ gate_pairs.json
python3 skills/match-verify/scripts/match_analyze.py prepare  # 2 自动负载均衡：一岗一 agent 起，硬上限 20
# → 按 meta.batches 一次性并发发 agent（≤20，不分波不串行），每个只给：
#   提示词 skills/match-verify/references/match-subagent-prompt.md + 批次号 + part 路径
python3 skills/match-verify/scripts/match_analyze.py merge    # 3 合并判定（missing_batches 非空则补发该批）
python3 skills/match-verify/scripts/match_analyze.py apply    # 4 只建 keep=true 的配对（幂等：先删该岗位旧配对）
python3 skills/match-verify/scripts/match_analyze.py link     # 5 单独一次读取：连接「关联岗位」
python3 skills/match-verify/scripts/match_analyze.py stats    # 6 单独一次读取：刷新岗位四项统计
```

子任务承担的判断（原来只能我手工做）：
- **keep 判定**：工序/方向不对应、助理岗降配、技能零交集 → false；
- **语义计分**：同义与单向上下位（禁把暖通/排风/冷却水/空压机互相顶替），部分覆盖可给分但必须写依据；
- **产出**：`evidence`（命中口径一句话）+ `ai_analysis`（结论/亮点/缺口/建议，150-250字）。

硬性纪律：
- **agent 数硬上限 20**（`shared/waves.py`，`MAX_AGENTS` 只能下调）：默认一岗一 agent，超过 20 才自动加大
  每 agent 的岗位数；一次性并发发完，**不分波、不串行**。三个环节（分析JD/分析简历/匹配）共用同一调度。
- **不用分数阈值代替方向判断**：`MIN_SCORE=1` 实测会灌进 174 对跨方向噪声，默认 20 又会漏人——
  方向由子任务逐对判，阈值只用于剔除零交集。
- **写完必须另起一次读取**（link / stats 独立执行），同一脚本里回读会拿到索引前的旧值。

- **智能匹配是三个触发点里的第三步**：先「智能分析JD」（门槛与技能词表已写好）→再「智能分析简历」
  （技能标签/两列文本已写好）→才跑本流程；两侧没分析完就匹配等于拿词表噪声打分。
- **不达标的人不建配对，但简历保留在库里**（简历库是人才池，不做删除、不写"暂不匹配"备注）。
- `gate_pending.json` 是"机械门槛过了、但专业/工序是否对口需要判断"的清单：智能体逐条判
  「方向不对/工序不对应→从 gate_pairs.json 剔除；对口经验充分→实质放行」，并把理由写进剔除日志，
  再执行 --commit。**这一步不能用字符串相等替代，也不能省略。**
- 默认 `MIN_SCORE=20`：门槛过了但技能几乎无交集的配对同样不建（可用环境变量调）。
- 已知坑：**写完立刻回读会拿到索引前的旧值**，连接「关联岗位」与统计必须各自单独一次读取（--commit 里
  的 link 数小于 created 时就补跑一次连接）。

## 边界

- 打分零 LLM：同义与门槛冲突才用 agent，别对「推荐/不推荐」区间重复劳动。
- 匹配表行数 = 岗位×有技能简历 中 total≥min-score 的配对数；零分配对不写表。
- 岗位统计四字段由本流程刷新，入库脚本不写。
- 统计与核对**必须与写入分开一次读取**：同一次脚本里写完立刻回读会拿到索引前的旧值。
