---
name: match-verify
version: 0.2.0
description: Fast targeted matching - a script builds the digest (candidates x open jobs of same org), the agent makes ONE batched semantic judgement per shard (<=8 candidates) producing sparse decisions.json, and a script validates, batch-creates match records, recomputes scores and job statistics, and reads back. No per-record dws commands.
name_en: Targeted Matching
name_zh: 定向匹配
description_en: Script-built digest, one batched agent judgement (shards of <=8), script-applied decisions with recomputed scores and job stats. Hard-gate failures never create records.
description_zh: 定向匹配极速版——脚本生成候选人×同组织在招岗位的判定输入，agent 按 ≤8 人/片做批量语义判定产出稀疏 decisions.json，脚本校验后批量建匹配记录、重算分数与岗位统计并回读。不达标组合根本不建记录；全程禁止逐条敲 dws 命令。
user-invocable: true
argument-hint: 说"匹配"或"定向匹配"，可指定候选人/岗位（如"给汪一兵做匹配""重建全部匹配"）
argument-hint-en: Say "match" or name a candidate/role; "rebuild all matches" for full recompute
argument-hint-zh: 说"匹配"或"定向匹配"，可指定候选人/岗位（如"给汪一兵做匹配""重建全部匹配"）
---

# 定向匹配（生成 + 判定 + 打分回填）

> 匹配只为人↔岗中**真正通过硬性门槛**的组合创建记录，从源头消灭笛卡尔积与大批删除。先读 [招聘底座](../recruit-model/SKILL.md)；判定输出规范见 [ai-analysis-spec.md](../recruit-model/references/ai-analysis-spec.md) A 部分；执行纪律见 [execution-notes.md](../recruit-model/references/execution-notes.md)。
>
> **铁律：agent 只动手做 Turn 2 的批量语义判定，其余全部交给脚本；禁止逐条敲 `dws` 命令。**实测：批量判定 10人×19岗 一次 368s，逐人串行外推 2534s（6.88×）；批量写 31 条 1.38s/1 次 vs 逐条 38.79s/31 次。

## 触发范围

- **本批候选人**（主场景）：简历入库 Turn 3 接续，`--candidates` 传 intake 产出的 `candidates.json`。
- **存量候选人（指定岗位反向匹配 / 重建全部匹配）**：用 `--from-table` 从简历库表批量导出存量候选人（可加 `--org 制造中心|职能中心` 只导一个组织、`--exclude-onboarded` 在查询阶段就排除 沟通状态=已入职）。"重建全部匹配"是**破坏性重算**（清空全部「系统匹配」记录后重算，人工匹配不动），执行前必须用业务话讲清"会删除并重建哪些记录、后果是什么"，经用户明确确认，并先加 `--dry-run` 预演。
- `--candidates` 与 `--from-table` **二选一**；两者都不给脚本会报错并给业务话提示。

## 三步走

### Turn 1 —— 生成判定输入（脚本）

```bash
# macOS / Linux（模式 A：本次上传批次）
python3 scripts/build_match_input.py --config <config.json绝对路径> \
        --candidates <candidates.json绝对路径> \
        --out-dir <输出目录绝对路径> [--max-per-batch 8]

# macOS / Linux（模式 B：表内存量候选人 → 反向匹配 / 重建全部匹配）
python3 scripts/build_match_input.py --config <config.json绝对路径> \
        --from-table [--org 制造中心|职能中心] [--exclude-onboarded] \
        --out-dir <输出目录绝对路径> [--max-per-batch 8]

# Windows（不要用裸 python，别名可能静默失败、退出码 49）
py -3 scripts\build_match_input.py --config <...> --candidates <...> --out-dir <...>
```

- `--config`：插件根目录 `config.json`（相对本技能目录 `../../config.json`，展开为绝对路径传入）。
- 脚本内部完成：吃 `candidates.json`（或 `--from-table` 时从简历库表批量导出存量候选人，字段结构与 `candidates.json` 完全同构，`record_id` 用表里真实 id，evidence 从「简历全文」列按 D4 切段）→ 从表里查**在招岗位**（组织分类+状态=招聘中，含岗位ID/硬性门槛/必备技能/加分项/权重；不依赖 jobs_draft.json 存在，"只上传简历、岗位早已在库"可独立跑通）→ 过滤已入职候选人（终止态不参与，其系统匹配记录按规则清理）→ 合并生成判定输入并按 **≤8 人/片**（D3，`--max-per-batch` 缺省 8；用户明确知情时可上调到 16，禁止 >16，契约 v3 §8）切分。
- 产物：`<out-dir>/digest.json` + `digest_batch_01.json`、`digest_batch_02.json`…；stdout 末行 `ARTIFACT:<绝对路径>/digest.json`。
- **产物凭证校验（D7，契约 v3 §9#2 统一口径）**：确认 `digest.json` **文件存在 且顶层 `ok == true`** 才进 Turn 2。`ok == false` 时 `errors[]` 里是业务话原因（如"表里查不到任何在招岗位""没有可导出的存量候选人"）→ 按 errors 处置后重跑本步（最多 2 次）→ 仍失败如实告知，禁止跳过或假装成功。分片文件 `digest_batch_NN.json` 顶层同样带 `ok`/`errors`。

### Turn 2 —— agent 批量语义判定（唯一动手环节）

> **⛔ 显式禁止条款（W-F run3 实测事故后新增，2026-09-17）：Turn 2 的语义判定必须由 agent 自己（大模型）完成，禁止编写或调用任何规则脚本 / 关键词匹配脚本（如自写 `generate_decisions.py`）来代替语义判定。** 理由（实测）：run3 里 agent 用自写规则脚本产出 decisions.json，`verify_decisions` PASS、`apply_decisions` ok=true、**全部形式校验通过，但业务结果崩塌**——0 条推荐（run1/run2 = 22 条）、skill_hits 普遍 ≤3、evidence 同人同模板跨岗位复用、岗位组织错归。这是新架构最危险的失效模式：**静默产出看起来正常的错误结果**。为此 `verify_decisions.py` 已加语义合理性护栏（evidence 复用/命中数异常/推荐率异常/门槛通过率异常/override 覆盖率异常）；**若护栏报出 `sem_*` 告警，agent 必须如实向用户呈现、说明可能需要重做判定，不得静默吞掉告警继续写库**（护栏指标在 verify 输出 `summary.semantic_guards` 里，告警都带具体证据：哪个 candidate/job、实测值 vs 阈值）。

**分片纪律（D3）：按 `digest_batch_NN.json` 逐片处理，一片一个回合，禁止一次性吞下全部候选人。**实测 10 人已逼近输出上限、20 人语义崩塌（漏人、编造命中项）、28 人撞顶不可恢复。

每片内，对每个候选人 × 每个**同组织**在招岗位：

1. **硬门槛四项一票否决**（学历/专业/经验年限/证书）：逐项 `pass|fail`；任一 fail → 进 `rejected`（同人多岗合并成一条，`job_keys` 列全 + 一句业务话 `reason`），**不建任何记录**。
2. 全过 → 进 `passed`：给 `skill_hits`（**只能从该岗 `must_skills` 里选**）、`bonus_hits`（只能从 `bonus_skills` 里选）、`recommend` 预判（推荐|待定|不推荐）、`evidence`（简历原文引用 ≤80 字，支撑结论；空话不合格）。
3. **`needs_review:["years"]` 的候选人必须复核工作年限**（D13）：用 `evidence.work_text` 原文逐段累加重算，修正值回填 `candidate_overrides.years_experience`；原文支撑不了 → 不拿估算值当年限否决依据，该岗从宽记「待定」并在 evidence 注明。
4. **稀疏字段补齐**（D14）：期望地点缺失时脚本已兜底「不限」，evidence 里有明确城市 → `candidate_overrides.expected_location` 覆盖；证书缺失 → 从 `evidence.cert_text` 补 `certificates_extra`；漏抽技能 → `skills_extra`。
5. **组织**：`org_confidence=="low"` 由 agent 依 evidence 判定并回填 `candidate_overrides.org` + `org_reason`；**判不了才问用户，不猜**。
6. `dedupe=="conflict"`（手机号同名不同人）的候选人**不进判定**，先停下请用户确认；`parse_status != "ok"` 的如实进 ❌ 清单。
7. **不输出任何分数**（D16）：`skill_score`/`bonus_score`/`total` 一律不写——分数、最终推荐状态由 Turn 3 脚本按口径重算（防模型编造命中项与算术漂移，脚本会校验 `skill_hits ⊆ must_skills`、`bonus_hits ⊆ bonus_skills`，越界条目作废进 warnings；agent 的 `recommend` 预判与重算不一致时以脚本为准）。

每片写一个 `<out-dir>/decisions_batch_NN.json`；全部片完成后**合并为一个 `<out-dir>/decisions.json`**：`passed`/`rejected`/`candidate_overrides` 三个数组按片序拼接，`batch_id` 与 digest 一致。

**decisions.json 准确结构（稀疏格式，照此一次性产出合法 JSON）**：

```json
{
  "batch_id": "<与 digest.json 的 batch_id 完全一致>",
  "passed": [
    {
      "candidate_key": "c01",
      "job_key": "j01",
      "gate_detail": {"education": "pass", "major": "pass", "years": "pass", "certificates": "pass"},
      "skill_hits": ["暖通运维", "PLC编程"],
      "bonus_hits": ["节能改造"],
      "recommend": "推荐",
      "evidence": "8年暖通运维经验，持电工证；'负责洁净厂房暖通系统运维，西门子PLC调试'"
    }
  ],
  "rejected": [
    {"candidate_key": "c01", "job_keys": ["j02", "j03"], "reason": "学历不足（大专，岗位要求本科及以上）"}
  ],
  "candidate_overrides": [
    {"candidate_key": "c01",
     "org": "制造中心",
     "category": "技术类",
     "expected_location": "曲靖",
     "skills_extra": ["PLC编程"],
     "certificates_extra": ["电工证"],
     "years_experience": 12,
     "org_reason": "工作经历均为制造基地厂务设备岗"}
  ]
}
```

- 键名、层级**严格照此**；`candidate_key`/`job_key` 用 digest 里的 `key`（如 `c01`/`j01`），不要用姓名或 record_id。
- **`candidate_overrides` 支持且仅支持七个键（契约 v3 §9#1，与 `apply_decisions.py` 代码完全一致）**：`org`（组织归一）、`category`（简历库分类）、`expected_location`（期望地点，覆盖脚本兜底的「不限」）、`skills_extra`（数组，与已有技能合并去重）、`certificates_extra`（数组，从 `evidence.cert_text` 补出的证书，与已有证书合并去重）、`years_experience`（整数，D13 估算年限的原文复核修正值）、`org_reason`（组织判定理由，进报告与清单）。不支持的键会被忽略并进 warnings；有实际变化的修正会由 Turn 3 脚本**写回简历库**（写后回读，明细在 apply_report.json 的 `overrides`/`overrides_writeback`）。
- 稀疏原则：没通过的组合只进 `rejected`（合并 job_keys），不要在 `passed` 里给 fail 条目；候选人字段无需修正就不出 `candidate_overrides` 条目。
- **评分口径原文**（脚本重算所依，agent 理解用、不算分）：技能得分 = 必备技能命中数 / 岗位必备技能总数 × 100；加分项得分 = 命中加分项数 / 岗位加分项总数 × 100（**岗位加分项为空时记 100**，脚本会在 warnings 里提示先与用户确认口径——与 `verify_decisions.py`/digest 内嵌 `scoring_rules` 一致）；匹配总分 = 技能得分 × 必备技能权重 + 加分项得分 × 加分项权重（权重取岗位字段，缺省 70%/30%）；总分 ≥ 80「推荐」、60–79「待定」、< 60「不推荐」；**硬性门槛任一不达标 → 一票否决，不建记录、不评分**。

**校验已内置在 Turn 3 的 `apply_decisions.py`（带 `--digest`）里 —— 正常流程不必单独跑 `verify_decisions.py`**（W-I O2b：省一个独立校验回合）。`apply_decisions.py` 内部 `import` 的就是本脚本的同一个 `verify()`；带 `--digest` 时 `check_coverage=True`，覆盖率 + 算术 + 命中项越界 + evidence + 语义护栏校验与独立 verify **完全等价**，不过则**拒绝写库**、退出码 1、把全部问题打到 stdout。`verify_decisions.py` 保留为**可选的独立诊断工具**（想在不写库的前提下单独校验一轮时才用）：

```bash
# 可选：只校验不写库（诊断用）。正常流程直接跑 Turn 3 的 apply_decisions.py --digest，其内部校验与此完全等价
python3 scripts/verify_decisions.py --digest <out-dir>/digest.json --decisions <out-dir>/decisions.json
# Windows: py -3 scripts\verify_decisions.py --digest <...> --decisions <...>
```

- **校验范围（以代码为准，契约 v3 §9#9；完整清单见 [execution-notes.md](../recruit-model/references/execution-notes.md)）**——硬错误（退出码 1，整批不许进 Turn 3）：文件不存在/不可读、非法 JSON、digest/decisions 不是对象或缺 candidates/jobs、passed/rejected 不是数组或两者全空、条目不是对象或缺必需键（candidate_key/job_key/skill_hits/bonus_hits/gate_detail/job_keys）、引用了 digest 里不存在的 key、**覆盖率缺失/重复**（同组织在招组合必须且只能判一次）、passed 里 gate_detail 含不达标项或非对象、evidence 为空或 >80 字、recommend 取值非法、**编造命中项的 pass 条目占比 >34%**（模型整体输出不可信，拒绝写库）、**evidence 被不同候选人复用且占比 >20%**（`sem_evidence_cross_candidate`：evidence 必须是本人简历原文，跨人复用=编造，拒绝写库）。
- 告警（不拦路但必须转述）：命中项越界判编造（该条 pass 无效、不建记录）、命中项归一化映射（切碎条目合并/缩写）、重复命中去重、模型分数与脚本重算不一致（以脚本为准，D16）、分母告警（必备技能为空记 0 / 加分项为空记 100）、gate_detail 缺项、batch_id 不一致、rejected 缺 reason 或空 job_keys、跨组织/不在招组合、无 digest 降级时覆盖率未校验；**语义合理性护栏（`sem_*`，2026-09-17 新增，阈值可用 `--sem-*` CLI 覆盖）**：evidence 去重率过低（模板化复用，`sem_evidence_reuse`）、evidence 跨候选人复用低占比（`sem_evidence_cross_candidate`）、skill_hits 普遍过少（`sem_skill_hits_too_few`）或普遍 100% 全命中放水（`sem_skill_hits_full_inflated`）、整批 0 推荐（`sem_zero_recommend`）/推荐率 >60%（`sem_recommend_ratio_high`）/推荐分布塌缩到单一取值（`sem_recommend_collapsed`）、门槛全拒（`sem_all_rejected`）或全过（`sem_all_passed`）、needs_review:["years"] 有标记却零 override（D13 复核没做，`sem_years_review_missing`）。**`sem_*` 告警必须如实转述给用户并说明可能需要重做 Turn 2，禁止静默吞掉继续写库**；每条告警都带具体证据（哪个 candidate/job、实测值 vs 阈值），护栏实测指标在输出 `summary.semantic_guards.metrics`。
- 退出码语义（独立 `verify_decisions.py` 与 `apply_decisions.py` 内置 verify 一致）：`0` = 校验结论 `ok == true`；`1` = 有问题 → 按 stdout 指出的条目修正 `decisions.json` 后重跑（最多 2 轮）；仍不过 → 如实告知用户，禁止带着非法 decisions 硬写库（`apply_decisions.py` 内置校验本来也会自动拒写、退出码 1）。**decisions.json 是 agent 产物，它的「ok==true」就是校验的退出码 0**（D7 口径统一）。

### Turn 3 —— 应用判定（脚本）

```bash
python3 scripts/apply_decisions.py --config <config.json绝对路径> \
        --decisions <out-dir>/decisions.json \
        --out-dir <同一out-dir> [--digest <out-dir>/digest.json] [--dry-run]
# Windows: py -3 scripts\apply_decisions.py --config <...> --decisions <...> --out-dir <...>
```

- 脚本内部完成：算术复核与命中项校验（D16，重算 技能得分/加分项得分/匹配总分/推荐状态，不一致与越界进 warnings）→ 幂等清理该批候选人的旧「系统匹配」记录（人工匹配不动）→ **批量创建**达标匹配记录（含岗位ID、匹配依据=evidence）→ **岗位统计重算**：对每个受影响岗位从表里查其**全部**匹配记录（含人工匹配与历史）重算 候选人总数/推荐数/待定数/不推荐数，再一次批量回填（D15，禁止用本批 decisions 直接累加）；「受影响岗位」= 本批建/删过匹配记录的岗位 **+ 本批做过判定但零匹配的岗位**，后者的四个统计字段即使全为 0 也**显式写入 0**（不留空——recruit-dashboard 靠它区分"还没算"与"算出来是 0"）→ 候选人修正字段（overrides）写回简历库 → 写后回读。
- `--dry-run`：破坏性场景（重建全部匹配）先预演，报告给用户确认后再实跑。
- 产物：`<out-dir>/apply_report.json`；stdout 末行 `ARTIFACT:` 指向它。
- **D7 校验**：存在且 `ok == true` → 转述清单；否则重跑（最多 2 次）→ 仍失败如实告知失败原因与已写入部分，**禁止假装成功**。`rows[].result=失败` 与 `warnings`（含"模型预判与重算不一致 N 条"）必须业务话转述，不隐藏。

## 输出（每次必出清单）

```
── 定向匹配结果（本批 5 人）─────
候选人 | 岗位 | 匹配度 | 技能 | 加分 | 结论
汪一兵 | 组件工艺工程师 | 88 | 90 | 83 | ✅ 推荐
石昊   | 暖通主管       | 74 | 80 | 60 | ⏸ 待定
（其余 18 岗未过硬性门槛，未生成记录：学历不足 9、专业不符 6、证书缺失 3）
──────────────────────────
新建匹配 2 条（推荐 1 | 待定 1 | 不推荐 0）｜ 门槛不符未建记录 N 组合 ｜ 岗位统计已重算回填 2 个岗位
待关注：胡裕的工作年限系估算，已按原文复核修正为 12 年
```

批量汇总清单末尾附：本批人数、判定时长、脚本重算与预判不一致条数（如有）。

## 边界（继承老版）

- 关联勾连用**岗位ID 文本**（极速版不依赖 lookup/双向关联，D1）；同名岗不串档。
- 表格侧 AI 字段（如客户保留）对新记录可能异步生成分析，仅作参考——**门槛判定与分数以本套件口径为准**，不等它。
- 已入职候选人不参与匹配（终止态，长期规则无需逐次确认）。
- 查询与看板全程只读；本技能的一切写操作只发生在 Turn 3 脚本内。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 脚本直接批量建记录、重算回填。未连或 `dws` 未登录 → 只输出"该候选人对哪些岗达标、命中了什么"的建议清单，不落库，并提示先开启连接器。
