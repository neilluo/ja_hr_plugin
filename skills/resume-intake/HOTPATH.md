---
name: resume-intake-hotpath
version: 0.2.0
description: Single-file hot path card for ingesting one or a few resumes and running targeted matching. Contains every command, the decisions.json structure, the scoring rubric and all business iron rules - read this ONE file and you can run the whole flow without opening any other doc.
name_zh: 简历入库+定向匹配 单文件热路径卡
user-invocable: false
---

# 热路径卡：简历入库 + 定向匹配（读这一个文件就够，不要再读别的文档）

适用范围：**1 份或几份简历**「解析入库并匹配」。岗位已在库里。
本卡是 `recruit-model/SKILL.md` + `references/{system-config,execution-notes,ai-analysis-spec}.md`
+ `resume-intake/SKILL.md` + `match-verify/SKILL.md` 六个文件的**热路径浓缩**，业务语义逐条保留。
只有这些情况才需要翻别的文档：JD 入库（`job-intake/SKILL.md`）、复刻部署到新 Base
（`replicate/SKILL.md`）、扫描件/加密件解析细节（`references/parsing-methods.md`）、
查询与看板（`candidate-query` / `recruit-dashboard`）。

## 0. 开工前 30 秒（不要多花回合）

**本流程只有 5 个回合，多一个都是浪费。以下五件事一律禁止：**

- **禁止 `mkdir`**：脚本自己建 `--out-dir`（`parents=True`）。
- **禁止 TodoWrite / 计划类工具**：本流程太短，列清单纯亏一个回合。
- **禁止 Read 或 Grep 任何 `.py` 脚本源码**：脚本做什么、产物长什么样、退出码什么语义，
  本卡已写全。读源码只会白烧回合与上下文（实测会引发"自我怀疑式"长思考，多花 60 秒以上）。
- **禁止 Read `digest.json`**：它是给 verify/apply 吃的全量版，内容与分片文件重复且更大。
  你只需要 Read `SHARD:` 指向的**分片文件**。
- **禁止逐条敲 `dws` 命令读写表格**：唯一例外是环境自检那**一条只读**命令，
  且要与首个脚本调用**合并在同一个回合**里：
  `python3 -V && dws aitable base list --limit 1`（Windows：`py -3 -V && ...`）。
  自检失败（没有 python / dws 未登录）→ 停下用业务话告诉用户先装 Python 3 或在
  「设置 → 连接器」开启并授权钉钉，**不要**继续。

路径一律传**绝对路径**（`--config` / `--files` / `--out-dir`）。
Windows 用 `py -3 scripts\xxx.py`，**绝不用裸 `python`**（别名会静默失败、退出码 49）。
下文 `<PLUGIN>` = 本插件根目录绝对路径；`<CFG>` = 该目录下 `config.json` 绝对路径。

## 1. 回合 1 —— 入库 + 生成判定输入（一条命令做完）

```bash
python3 <PLUGIN>/skills/resume-intake/scripts/intake_resume.py \
        --config <CFG> --files <简历1> [简历2 ...] \
        --out-dir <工作区>/resume --auto-match
```

- `--files` **一次全给**，不要分多次调用。
- `--auto-match`：入库成功后同进程接着生成定向匹配判定输入（省一个编排回合）。
  digest 默认落在 `<out-dir>/../match`；要换位置加 `--match-out-dir <目录>`。
  本批 partial（见下）时 auto-match 会自动跳过，先续跑补齐再单独跑 build_match_input。
- `--wall-budget <秒>`（默认 100，勿超过 agent 工具 120s 超时）：墙钟预算。到点脚本
  **graceful 停**：checkpoint 逐条落盘、打印已完成/未完成清单与一行 `RESUME:`、
  退出码 0、报告 `ok=true` 且 `partial=true`。
- **agent 多模态兜底协议（P4a，跨平台扫描件/图片的最后一线）**：stdout 出现一行
  `VISION_NEEDED: <绝对路径1> <绝对路径2> ...`（= 有文件本机读不出文字，
  `parse_status=needs_agent_vision`，清单同时进报告 `vision_needed_files`）→
  你在**一轮**里用多模态能力 Read 完列出的**全部**文件，把每份的文字与关键字段誊出来，
  用 Write 按下面 schema 写出补丁 json，然后**重跑同一条命令**加
  `--apply-vision-patch <补丁json绝对路径>`（checkpoint 幂等，已入库项不重放）：

  ```json
  {"<文件绝对路径>": {"text": "...", "fields_draft": {"name": "...", "phone": "...", ...},
                       "confidence": 0.0, "notes": "..."}}
  ```

  - `text` = 你读出的简历全文（**必填**，脚本用它跑正则抽字段并写入简历全文）；
    `fields_draft` = 字段草稿，**只填你从图里确凿读出的字段，可以留空**——脚本先对
    `text` 跑正则，**regex 有值的字段用 regex，regex 为空才取 fields_draft**；
    `confidence`（0~1）与 `notes` 可选，会原样记进该候选人的 warnings。
  - **agent 只产出结构化补丁，绝不写库、禁止逐条敲 dws 写表**；入库仍由脚本走正常
    查重/护栏/回读流程。凡取自 `fields_draft` 的字段会被打 `field_source="agent_vision"`
    并追加进该候选人 `needs_review`，回合 2 **必须**用 evidence 原文复核后照转。
  - **20% 闸门（用户拍板）**：读不出的份数超过本批 20% 时脚本**不写任何记录**，stdout
    提示「本批 X/Y 份读不出文字，超过 20% 阈值，疑似整批格式问题，请确认后重试或提供
    文字版」并列出名单，退出码 0、报告 `ok=true`、`partial=true`、`reason="vision_gate"`。
    此时**不要**走补丁协议——把这段业务话如实转给用户确认（疑似整批格式问题），
    用户确认后再重跑 / 打补丁 / 换文字版。
  - `RECRUIT_NO_VISION=1` 是**仅测试用**环境变量（关掉本机 Vision OCR 演练本通道），
    生产流程绝不设置。
- 用户明确说"先不传附件" → 加 `--no-attachment`；之后"补传附件" = **重跑同一条命令去掉该参数**
  （`checkpoint.json` 幂等，已入库的不重放；checkpoint 是**增量落盘**的——每份文件的
  「记录已写」「附件已传」状态一确立就写盘，中途被杀也不丢已完成进度）。
  用户说"从头重来"才加 `--reset`。
- 脚本内部完成（**你一件都不用自己做**）：提取文本（**macOS 上扫描件/图片简历自动走系统
  Vision OCR 救回**，零依赖、纯本地不出网，多份并行 ≤4；首次运行可能弹 macOS 授权弹窗，
  请用户点允许）→ 正则预抽字段 → 库内附件**内容级比对**（键 = 简历库「附件内容MD5」
  字段，P4b）→ 一次批量手机号查重 → 批量写简历库（≤100 条/次，命中即覆盖更新）→ 技能标签**只增不删**补选项 →
  期望地点兜底「不限」→ 原始文件名并发上传附件（并发 5）→ 写后回读 →
  生成 digest.json + 分片，并打印 `SHARD:<分片文件绝对路径>`。

**凭证校验（必做，防静默早退）**：stdout 会有两行 `ARTIFACT:` —— 第一行是
`intake_report.json`，第二行是 `digest.json`。规则：

- stdout 有 `RESUME:` 行（= 报告 `partial=true`，墙钟预算耗尽或 20% 闸门触发）→
  预算耗尽：**重跑同一条命令续跑**（checkpoint 幂等，不产生重复记录），**最多 3 次**；
  仍 partial → 把已完成/未完成清单如实报给用户。闸门触发（报告 `reason="vision_gate"`）：
  不重跑、不打补丁，先把「疑似整批格式问题」业务话报给用户确认。
  partial 时不会有 `SHARD:` 行，属预期，别当故障。
- stdout 有 `VISION_NEEDED:` 行（且没有闸门提示）→ 走上面的 **agent 多模态兜底协议**：
  一轮读完全部列出文件 → Write 补丁 json → 重跑同命令加 `--apply-vision-patch`。
- stdout 同时有「── 简历入库结果 ──」清单 + `digest:` / `分片:` 摘要 + `SHARD:` 行 →
  **两件事都成了**。入库清单直接在 stdout 里读，**不要**再去 Read `intake_report.json`
  或 `candidates.json`（stdout 已经给了逐行结果与小计）。
- 有入库清单但没有 `SHARD:` 行（且没有 `RESUME:` 行）→ 入库成了、判定输入没生成：单独补跑
  `python3 <PLUGIN>/skills/match-verify/scripts/build_match_input.py --config <CFG>
  --candidates <out-dir>/candidates.json --out-dir <out-dir>/../match --max-per-batch 8`。
- 入库清单显示失败 / 脚本异常 / 没有 `ARTIFACT:` 行 → **重跑同一条命令**（幂等续跑），
  最多 2 次；仍失败 → 如实告知失败原因与已完成部分，**禁止跳过或假装成功**。
- 失败清单语义（P4a 起再收窄）：macOS 上扫描件/图片自动 OCR 入库；非 macOS 或 OCR
  不可信的文件**不再直接判死**，转 agent 多模态兜底（见上面的 `VISION_NEEDED:` 协议）。
  ❌ 失败只剩 **加密 / 损坏 / 补丁未覆盖或补丁后仍读不出手机号**（`needs_agent_vision`
  如实报，不硬造字段）。OCR/补丁救回的简历带「文本可能有小误读」类警告，
  姓名/手机号等关键字段的人工确认警告必须照转。

> 注：判定输入**不会**打在 stdout 上（qodercli 会在约 30 KB 处静默切断 Bash 输出，
> 半截 JSON 比不给更危险）。一律按 `SHARD:` 路径 Read 分片文件。

## 2. 回合 2 —— Read 分片文件（只此一个 Read）

Read `SHARD:` 指向的 `digest_batch_NN.json`。单片场景就是**一次 Read**，读完直接进回合 3。
多片时（stdout 会明说"多片"）**一片一个回合** Read，**每片 ≤8 人**，
绝不一次吞下全部候选人（实测 10 人逼近输出上限、20 人语义崩塌、28 人不可恢复）。

分片文件里已按你的组织做了岗位预筛、并裁掉了判定用不到的岗位字段，所以它比 `digest.json` 小得多。
`shard.jobs_org_prefiltered` / `shard.jobs_slimmed` / `shard.job_count_all` 会告诉你裁了什么；
**这不影响判定口径**，`combo_count` 与全量时完全一致。
P5 起候选人还可能带两类复核信号（处置规则见回合 3 的规则 3 / 规则 5）：
`prefilter_suspicious`（组织预筛删掉的、**全部通过机械门槛**（学历/年限/证书）的跨组织岗位
清单——预筛可能错杀该候选人的正确组织）与 `evidence.name_text / email_text / location_text`
（姓名/邮箱/期望地点的**命中行原文**，供身份字段复核）。

## 3. 回合 3 —— 你亲自做一次批量语义判定（唯一真正用算力的回合）

**禁止用自写规则脚本代替语义判定。禁止逐条敲 `dws` 命令读写表格。**
判定输入就是回合 2 读到的那份分片内容（`candidates` + `jobs`）。
**想清楚就一次写出 `decisions.json`，写完直接进回合 4**——`apply_decisions.py` 带 `--digest`
时已内置与独立 verify 完全等价的校验，**不需要**再单独跑一遍 `verify_decisions.py`（省一个回合）。

> **本回合是整条流程的墙钟大头，务必省 output token：**
> - **想清楚就直接用 Write 一次写出 `decisions.json`，禁止在思考里逐字起草整份 JSON。**
>   实测在思考里先草一遍 JSON 会让本回合 output token 翻 4 倍、墙钟多 100~250 秒，
>   而结论完全一样。逐组合的分析在脑内做完即可，**只把结论写进文件**。
> - 思考只写"哪几项门槛为什么不过"的要点，不复述岗位 JD、不抄简历原文段落
>   （`evidence` 字段里引用一次就够）。
> - `rejected` 一定按人合并 `job_keys`（一个人一条），不要一个组合一条。
> - 未过门槛的组合**不要**写 `gate_detail`、不要写 `evidence`、不要解释每一个岗位；
>   一句合并的业务话 `reason` 即可。

对每个候选人 × 每个岗位：

1. **硬门槛四项一票否决**（学历 / 专业 / 经验年限 / 证书），逐项给 `pass|fail`。
   任一 `fail` → 进 `rejected`（同一人的多个岗位**合并成一条**，`job_keys` 列全 + 一句业务话 `reason`），
   **不建任何记录、不打分**。证据不足以判断某项 → **按不达标处理**，`reason` 写「证据不足:XX」。
   岗位证书要求为「无」/「无明确要求」/空，或 JD 写的是「持证者优先」
   （`cert_is_preferred_not_required=true`）→ 证书项一律视为达标（后者只在加分项体现）。
   学历达标 = 候选人学历 ≥ 岗位要求（博士 > 硕士 > 本科 > 大专）；`education` 字段缺失但
   `evidence.education_text` 看得出学历的，**以原文为准**。
2. 全过 → 进 `passed`，给：
   - `skill_hits`：**只能从该岗 `must_skills` 里选**；语义等价算命中
     （"PLC编程" ↔ "西门子PLC调试"），但**绝不许发明分母里没有的技能名**。
   - `bonus_hits`：只能从该岗 `bonus_skills` 里选。岗位 `bonus_skills` 为空 →
     不默认满分，按「无加分项」处理并在汇总里提示与用户确认口径。
   - `recommend`：推荐 | 待定 | 不推荐（**只是预判**，最终由脚本按阈值重算）。
   - `evidence`：**简历原文引用 ≤80 字**，要能支撑门槛结论与命中判断；
     "经验丰富、能力强"这类空话不合格。
3. **`needs_review` 含 `"years"` 的候选人必须复核工作年限**：用 `evidence.work_text` 原文
   按「公司 + 起止年月」逐段累加重算（重叠期不重复计），修正值写进
   `candidate_overrides.years_experience`，复核依据写进该人各条目的 `evidence`。
   原文实在支撑不了 → **不拿估算值当年限否决依据**，该岗从宽记「待定」，
   并在 evidence 注明"年限无法从原文确认"。
   `needs_review` 含**字段名**（如 `"phone"`、`"school"`，来自 agent 多模态兜底补丁的
   `field_source="agent_vision"` 字段）→ 同样必须用 evidence 原文逐个复核，发现误读在
   `candidate_overrides` 里回填修正值，并把「该字段来自图片识别草稿、已复核」照转。
   **P5 身份安全阀**：`needs_review` 含 `"name"` / `"email"`（姓名来自文件名/OCR/agent
   草稿，或邮箱命中 OCR 噪声规则——域名无点、TLD 含非字母、域名主体字母数字混排如
   qq.com 误读成 q9.com）→ 必须对照 `evidence.name_text` / `email_text` **原文逐字比对**；
   `candidate_overrides` 没有 name/email 键——确认误读就业务话照转请用户人工修正库内记录，
   且无论对错都要把复核结论（如「姓名已按原文复核无误」「邮箱疑似 OCR 误读已照转」）写进
   该人**任一条目**的 `evidence`（apply 内置校验以此确认复核做过，缺了会出
   `sem_name_review_missing` / `sem_email_review_missing` 告警）。
   `evidence.location_text` 是期望地点的命中行原文：发现串栏垃圾值（地点串里混着电话/
   微信/标签）→ 按 D14 处置，`candidate_overrides.expected_location` 覆盖成干净城市，
   或原文确无明确城市时保持「不限」并在 evidence 注明。
4. **稀疏字段补齐**：`evidence` 里有明确城市 → `candidate_overrides.expected_location`；
   证书缺失 → 从 `evidence.cert_text` 补 `certificates_extra`；漏抽技能 → `skills_extra`。
   没有要修正的字段就**不要**给这个人出 `candidate_overrides` 条目。
5. **组织复核**：`org_confidence=="low"` 的候选人，以及 **P5 起带 `prefilter_suspicious`
   / `needs_review` 含 `"org"` 的候选人**（组织预筛把「学历/年限/证书**全部机械达标**」的
   跨组织岗位删掉了——预筛可能错杀了该候选人的正确组织），都必须依 evidence 复核组织 ——
   制造基地 / 厂务 / 设备 / EHS → **制造中心**；财务 / 行政 / 人力 / 数据信息 → **职能中心**。
   - 复核**确认无误** → 该人 `candidate_overrides.org_reason` 写清依据（apply 内置校验以
     org/org_reason 为复核落点，缺了会出 `sem_org_review_missing` 告警）。
   - 复核**确认有误**（组织判错）→ 写 `candidate_overrides.org` + `org_reason`；该人新组织的
     组合本轮分片里没有，按 `prefilter_suspicious` 列出的 `job_key` 合并进 `rejected`
     （reason 写「组织已改判为X，待预筛按新组织重切后补判」，别硬造门槛结论），让 apply
     把改判组织写回简历库，然后**重跑同一条生成判定输入的命令**——脚本以库内组织为准
     重切分片（stdout 会有一条「以库内组织为准」的 WARN），对新分片里该候选人的新组合
     补判、更新 decisions 后再跑一次 apply（幂等，旧系统匹配记录自动清理）。
   - **判不了才问用户，不猜。**（组织是匹配的前提：简历组织必须等于岗位组织，否则匹配不到任何岗位。）
6. **不进判定、必须先停下问用户**：`dedupe=="conflict"`（手机号相同但姓名不同 = 疑似重名/错录）
   → 停止该候选人后续流程，业务话请用户确认，**不自动选第一条**。
   `parse_status != "ok"`（`needs_agent_vision` 补丁未覆盖 / 加密 / 乱码）→ 如实进 ❌ 清单，
   写"无法解析，请提供文字版"，**绝不硬造字段**。注意 P3 起 macOS 上扫描件/图片已被
   自动 OCR 救回，P4a 起非 macOS/OCR 不可信的文件走 `VISION_NEEDED:` agent 多模态兜底
   （补丁覆盖后 parse_status=ok、backend=agent_vision，草稿字段带 needs_review 标记——
   警告必须照转）；到判定时仍 `needs_agent_vision` 的只剩补丁没覆盖的文件，如实报。
   沟通状态=已入职 → 终止态，不参与匹配（长期规则，无需逐次确认）。
7. **不输出任何分数**：`skill_score` / `bonus_score` / `total` 一律不写。
   分数与最终推荐状态由回合 3 脚本重算（它会校验 `skill_hits ⊆ must_skills`、
   `bonus_hits ⊆ bonus_skills`，越界条目作废进 warnings；你的 `recommend` 预判与重算不一致时
   **以脚本为准**，不要争辩或手改分数）。

**评分口径**（脚本重算所依，你理解用、**不用来算分**）：
技能得分 = 必备技能命中数 / 岗位必备技能总数 × 100；
加分项得分 = 命中加分项数 / 岗位加分项总数 × 100；
匹配总分 = 技能得分 × 必备技能权重 + 加分项得分 × 加分项权重（权重取岗位字段，缺省 70% / 30%）；
总分 ≥ 80「推荐」、60–79「待定」、< 60「不推荐」；硬门槛任一不达标 → 一票否决，不建记录不评分。

**decisions.json 准确结构（稀疏格式，一次写出合法 JSON；单片直接写 `decisions.json`，
多片先写 `decisions_batch_NN.json` 再按片序把三个数组拼接成 `decisions.json`）**：

```json
{
  "batch_id": "<与 digest 的 batch_id 完全一致>",
  "passed": [
    {"candidate_key": "c01", "job_key": "j01",
     "gate_detail": {"education": "pass", "major": "pass", "years": "pass", "certificates": "pass"},
     "skill_hits": ["暖通运维", "PLC编程"], "bonus_hits": ["节能改造"], "recommend": "推荐",
     "evidence": "8年暖通运维经验，持电工证；'负责洁净厂房暖通系统运维，西门子PLC调试'"}
  ],
  "rejected": [
    {"candidate_key": "c01", "job_keys": ["j02", "j03"], "reason": "学历不足（大专，岗位要求本科及以上）"}
  ],
  "candidate_overrides": [
    {"candidate_key": "c01", "org": "制造中心", "category": "技术类",
     "expected_location": "曲靖", "years_experience": 13, "skills_extra": ["PLC"],
     "org_reason": "工作经历均为制造基地厂务设备岗"}
  ]
}
```

- 键名与层级**严格照此**。`candidate_key` / `job_key` 用判定输入里的 `key`（`c01` / `j01`），
  **不要用姓名、不要用 record_id**。
- 稀疏原则：没通过的组合只进 `rejected`（合并 `job_keys`），**不要**在 `passed` 里放 fail 条目。
- 用 Write 工具写出 `<match-out-dir>/decisions.json` 后，**直接进回合 4 跑 `apply_decisions.py`
  （带 `--digest`）即可，不用再单独跑 `verify_decisions.py`**：apply 带 `--digest` 时内部已调用
  与独立 verify **完全等价**的校验（覆盖率 `check_coverage=True` + 算术复核 + 命中项越界 +
  evidence + 语义护栏），校验不过会**拒绝写库**、把全部问题打到 stdout、退出码 `1`。
- apply 退出码 `0` = 校验通过且已写库 → 进回合 5；`1` = 校验没过（**未写库**）→ 按 stdout 指出的
  条目改 `decisions.json` 后**重跑 apply**，最多 2 轮；仍不过 → 如实告知用户，
  **禁止带着非法 decisions 硬跑**（apply 本来也会自动拒写，你不需要赌）。
  校验与应用**一律用合并版 `digest.json`**（它带全量岗位与全量字段），不要用分片文件。

## 4. 回合 4 —— 应用判定，真写库（一条命令，**内置校验**，不必先单独 verify）

```bash
python3 <PLUGIN>/skills/match-verify/scripts/apply_decisions.py --config <CFG> \
        --decisions <match-out-dir>/decisions.json --out-dir <match-out-dir> \
        --digest <match-out-dir>/digest.json
```

- 脚本内部完成：**内置 verify 校验**（带 `--digest` 时 `check_coverage=True`，覆盖率 + 算术复核 +
  命中项越界 + evidence + 语义护栏，与独立 `verify_decisions.py` 完全等价；不过则**拒绝写库**、退出码 1）→
  幂等清理该批候选人的旧「系统匹配」记录（**人工匹配不动**）→
  批量创建达标匹配记录（含岗位ID、匹配依据=evidence）→ **岗位统计重算**（对每个受影响岗位从表里查
  其**全部**匹配记录含人工匹配与历史，重算 候选人总数/推荐数/待定数/不推荐数再一次批量回填）→
  候选人修正字段写回简历库 → 写后回读。
- **凭证校验**：stdout 末行 `ARTIFACT:` 指向 `apply_report.json`，且报告 `ok == true`。
  stdout 已打出 `verify:` / `删旧…→ 新建匹配…` 摘要即视为成功；**不要**再花一个回合去 Read 它，
  除非 stdout 缺摘要或报异常（那时才 Read，或重跑，最多 2 次）。
- `rows[].result=失败` 与 `warnings`（含"模型预判与重算不一致 N 条"）**必须业务话转述，不隐藏**。
- **破坏性场景**（用户要求"重建全部匹配"= 清空全部「系统匹配」记录后重算）：
  必须先用业务话讲清"会删除并重建哪些记录、后果是什么"，**经用户明确确认**，
  并先加 `--dry-run` 预演、把预演结果报告给用户，确认后再去掉 `--dry-run` 实跑。

## 5. 回合 5 —— 输出（每次必出清单，**单个文件也出清单**）

```
── 简历入库结果 ──────────────
序号 | 文件名 | 处理结果 | 说明
1 | 【暖通主管_曲靖】石昊.pdf | ✅ 新入库 | 制造中心·技术类，附件已传
2 | 胡裕_14年.pdf | ✅ 已覆盖 | 手机号已存在，用最新简历覆盖更新
3 | 张三.pdf | ⏭️ 跳过 | 库内已有内容完全相同的简历附件（内容 MD5 相同，重复上传）
4 | 扫描件.png | ✅ 新入库 | macOS Vision OCR 救回入库；OCR 文本可能有小误读，姓名请人工确认
5 | 加密件.pdf | ❌ 失败 | 无法解析（加密），请提供未加密文字版
────────────────────────
小计：新入库 2 | 覆盖 1 | 跳过 1 | 失败 1 | 附件已传 3 | 附件失败 0
待关注：李四（手机号与库内"王芳"相同，请确认是否同一人）

审阅备注：低置信组织复核结论 + 估算年限的原文复核结论（写清依据）

── 定向匹配结果（本批 N 人）─────
候选人 | 岗位 | 匹配度 | 技能 | 加分 | 结论
汪一兵 | 组件工艺工程师 | 88 | 90 | 83 | ✅ 推荐
石昊   | 暖通主管       | 74 | 80 | 60 | ⏸ 待定
（其余 N 个组合未过硬性门槛，未生成记录：学历不足 9、专业不符 6、证书缺失 3）
──────────────────────────
新建匹配 2 条（推荐 1 | 待定 1 | 不推荐 0）｜ 门槛不符未建记录 N 组合 ｜ 岗位统计已重算回填 2 个岗位

一行汇总：intake ok=? ｜digest ok=? ｜verify PASS/FAIL（errors/warnings/覆盖 x/y）｜apply ok=? ｜新建匹配 N 条
```

## 6. 铁律（与老版业务语义完全一致，任何一条都不许为了省时间丢掉）

1. **只说业务语言**：对用户只讲「岗位 / 候选人 / 匹配结果 / 推荐状态」。
   **绝不出现**表ID、字段ID、fieldId、record_id、命令、JSON、脚本路径、产物文件名。
   （本卡里的路径与 JSON 是给你执行用的，不是给你转述用的。）
2. **清单式输出**：汇报用清单/表格，一张表只说一件事（新简历 / 匹配结果 / 未达标），
   不堆砌全部字段；查询类同样一行一个候选人/岗位 + 末尾汇总。
3. **失败不隐藏**：脚本报告说失败就是失败；`rows[].result=失败` 与 `warnings` 全部业务话转述。
   只认产物报告 `ok==true` 与 `rows` 逐行结果，**不只凭退出码宣称成功**。
4. **不硬造字段**：解析失败/证据不足时如实说，绝不编造学历、年限、证书、技能命中项。
5. **覆盖前确认**：用户说"上传并解析"即视为入库与覆盖更新授权（覆盖要在清单里标"已覆盖"并说明对象）；
   但**删除类、重建全部匹配类破坏性操作必须先业务话确认**，并先 `--dry-run` 预演。
6. **手机号冲突停下**：查重主键 = 手机号。命中 → 覆盖更新并标注；同号多条 → 停止并报告，不自动选；
   **同号不同名 → 停下请用户确认**，该人不进判定。姓名相同但手机号不同 = 不同人，正常新建。
   **附件级去重（P4b）**：库内比对键 = 附件**内容真 MD5**（简历库「附件内容MD5」列，
   由脚本在附件上传成功后写入）——内容相同（哪怕换了文件名）→ 判重复上传跳过；
   **同名同大小但内容不同 → 不判重复**，按同一候选人的简历新版本走覆盖更新，
   清单里那句说明照转。库内某条老记录还没存哈希时，脚本按老键回退判重并在 warnings
   里说明（照转）；整个库没有该列（客户现存库）→ 自动回退「文件名+字节大小」并告警
   说明"未启用内容级去重、如何启用"，**照转给用户**，建议按复刻部署补建该列。
7. **除本卡回合 3 的语义判定外，禁止逐条敲 `dws` 命令**；所有表格读写都在脚本内部完成
   （批量、带重试、带写后回读）。环境自检那一条只读命令是唯一例外。
8. **附件一律原始文件名**，禁止改名/加序号前缀。技能标签**只增不删**（追加新选项时保留全部已有选项及其 id）。
9. 必填口径：简历「所属组织」必填（判不了问用户，组织为空 = 匹配不到任何岗位）；
   「期望地点」必填，没有明确地点一律「不限」；简历库分类默认「技术类」；沟通状态新入库默认「待筛选」。
10. 关联勾连用**岗位ID 文本**（不依赖 lookup / 双向关联）；同名岗不串档。
    表格侧 AI 字段对新记录可能异步生成分析，**仅作参考**——门槛判定与分数以本套件口径为准，不等它。
11. 新增组织（如「研发中心」）时，岗位表 / 简历库 / 匹配表**三处**组织选项都要同步新增，缺一侧即匹配不到。
12. 一次只调用一个脚本；同一批次的入库与匹配产物各自复用同一个 `--out-dir`。
