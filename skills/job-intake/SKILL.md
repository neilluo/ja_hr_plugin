---
name: job-intake
version: 0.2.0
description: Fast job ingestion - one script call extracts JD text, pre-fills fields, dedupes by name+department+org, batch-writes and uploads attachments; the agent does one batched normalization pass (hard gates / must-vs-bonus skills / org & department) and a second script call applies it.
name_en: Job Intake
name_zh: 岗位入库
description_en: Upload JD docs (single or batch). Script drafts, agent normalizes in one batched turn, script applies and verifies.
description_zh: 上传岗位说明书（单个或批量）。脚本一次完成解析预填、按岗位+部门+组织查重、批量写入与并发附件，agent 只做一个回合的批量归一化（硬性门槛四项拆解/必备与加分技能切分/部门与组织归一），再由脚本一次应用并回读。
user-invocable: true
argument-hint: Upload one or more JD files, or describe the role to add
argument-hint-en: Upload one or more JD files, or describe the role to add
argument-hint-zh: 上传 1 个或多个岗位说明书(JD)文件，或说明要新增的岗位
---

# 岗位入库（三段式流水线）

先读 [招聘底座](../recruit-model/SKILL.md)（字段与规则以其为准）；执行纪律以 [execution-notes.md](../recruit-model/references/execution-notes.md) 为准。

**铁律：本技能只允许调用脚本，禁止 agent 逐条敲 `dws` 命令**；把用户上传的岗位说明书写入「岗位JD表」。

## 三步走

### Turn 1 —— 跑岗位入库脚本（草稿模式，一次调用整批完成）

```bash
# macOS / Linux
python3 scripts/intake_job.py --config <config.json绝对路径> \
        --files <JD文件1> [JD文件2 ...] \
        --out-dir <输出目录绝对路径> [--no-attachment]

# Windows（不要用裸 python，别名可能静默失败、退出码 49）
py -3 scripts\intake_job.py --config <config.json绝对路径> --files <文件...> --out-dir <绝对路径>
```

- `--config`：插件根目录 `config.json`（相对本技能目录 `../../config.json`，展开为绝对路径）。
- 脚本内部完成：提取文本 → 正则预填（岗位名称/部门/硬性门槛原文/必备技能原文/加分项原文/学历·年限·专业·证书要求）→ 按「岗位名称+所属部门+组织分类」批量查重 → 批量写入岗位表（状态=招聘中、权重缺省 0.7/0.3、提交时间=当天、分配唯一岗位ID；配置了可选 `job.submitter` 时回填需求提交人=当前登录用户）→ 并发上传 JD 附件（原始文件名）→ 回读。**脚本不做「JD vs 简历」类型分流**：本技能只应传岗位说明书文件；解析不出岗位名称的文件会进 ❌ 清单（不硬造）。
- 产物：`<out-dir>/jobs_draft.json`、`intake_report.json`；stdout 末行 `ARTIFACT:<绝对路径>/intake_report.json`。

**产物凭证校验（D7，必做，口径按契约 v3 §9#2 统一）**：确认 ARTIFACT 指向的 `intake_report.json` **存在且 `ok == true`**，且 `jobs_draft.json` **存在且顶层 `ok == true`**，才进 Turn 2；否则重跑本步（最多 2 次），仍失败如实告知用户，**禁止跳过或假装成功**。

### Turn 2 —— agent 一次批量归一化（本技能唯一动手环节）

> **⛔ 显式禁止条款（W-F run3 实测事故后新增，2026-09-17）：Turn 2 的语义归一化必须由 agent 自己（大模型）逐份 JD 完成，禁止编写或调用任何规则脚本 / 关键词匹配脚本（如自写 `normalize_jobs.py`）来代替语义判定。** 理由（实测）：run3 里 agent 自写规则脚本跑 Turn 2，产物通过了全部形式校验（`--apply` ok=true），但业务结果崩塌——3 个岗位被错误归到职能中心、门槛与技能拆分失真，直接改变了下游匹配的组合面。规则脚本没有语义理解，形式校验防不了它。**若 `intake_job.py --apply` 报出「Turn 2 归一化护栏」告警（必备技能全空/全部雷同等），agent 必须如实向用户呈现并说明可能需要重做归一化，不得静默吞掉告警继续写库。**

读 `jobs_draft.json`，对**全部岗位一个回合内**做语义归一化，产出 `<out-dir>/jobs_final.json`：

1. **硬性门槛四项拆解**：把 `hard_gates_raw` 原文拆成结构化四项——`hard_gates: {"education":"本科及以上","major":"电气类","years":"5年以上","certificates":"电工证"}`；某项 JD 没写就置空字符串（空 = 该项不做否决）。
2. **"持证者优先" ≠ 硬性证书门槛**：草稿里 `cert_is_preferred_not_required == true` 时，证书**不得**放进 `hard_gates.certificates`，应归入 `bonus_skills`。
3. **必备技能与加分项切分**：把 `must_skills_raw` / `bonus_skills_raw` 切成逗号分隔的技能清单（`must_skills: ["..",".."]`、`bonus_skills: [".."]`），去掉"熟练掌握""优先"等修饰词，只留可比对的技能名；这两个数组是匹配打分的**分母**，粒度要均匀（不要一条塞三个技能）。
4. **部门与组织归一**：`department` 归到现有部门选项（不在选项内 → 选最贴近的现有归类并在报告转述时告知用户，或让用户确认后补选项）；`org` 按口径判定（厂务/设备/EHS/制造基地 → 制造中心；财务/行政/人力/数据信息 → 职能中心），**判不了问用户，不猜**。组织为空 = 该岗位匹配不到任何简历。
5. **权重与岗位ID核对**：JD 明确写了比例的按 JD 改 `weights`（`{"must":0.7,"bonus":0.3}`，PERCENT 字段写小数）；**岗位ID 由 Turn 1 脚本自动分配**（JOB-序号，全表唯一，契约 v3 §9#4），agent 只核对、**仅在 jobs_final 里发现某条缺失 job_id 时**按现有最大序号续编，不要重排已有编号。
6. `jobs_final.json` 结构（**契约 v3 §9#3 冻结，`--apply` 按此验收**）= `{"batch_id": <与草稿一致>, "jobs": [<草稿 jobs 数组的元素，agent 修订上述字段后原样保留>]}`：每条**原样保留 `key` 与 `record_id`**，仅修订语义字段——`hard_gates` 四项 / `must_skills[]` / `bonus_skills[]` / `department` / `org` / `weights`（`--apply` 同时容忍对 `status`/`job_name`/`job_id`/`work_location` 的显式修订）。**不改 record_id、不删条目**；解析失败的岗位（报告 ❌ 行）不在 jobs 里，不要硬造。

### Turn 3 —— 跑应用脚本（批量写回 + 回读 + 清单）

```bash
python3 scripts/intake_job.py --config <config.json绝对路径> \
        --apply <out-dir>/jobs_final.json --out-dir <同一out-dir>
# Windows: py -3 scripts\intake_job.py --config <...> --apply <...> --out-dir <...>
```

- 产物：`intake_report.json`；stdout 末行 `ARTIFACT:` 指向它。
- **D7 校验**：存在且 `ok == true` → 转述岗位入库清单（rows/summary/warnings，业务话）；失败 → 重跑（最多 2 次）→ 仍失败如实告知。
- 报告核对点：每条岗位的 硬性门槛/必备技能/加分项/权重/提交时间/JD附件 均非空（**批量绝不等于省略字段**）；有失败或警告必须转述。

### 入库后 —— 反向匹配

老口径「新岗位入库后立即对存量候选人做反向匹配」保持：需要时走 [定向匹配](../match-verify/SKILL.md)（其脚本会从表里查全部在招岗位，因此新岗位落库后即进入匹配范围）。

## 输出（每次必出清单，单个文件也出）

```
── 岗位入库结果 ──────────────
序号 | 文件名 | 处理结果 | 说明
1 | 岗位说明书-电气工程师.doc | ✅ 新入库 | 厂务管理部·制造中心，招聘中，硬性门槛：本科+电气类+5年+电工证
2 | 暖通工程师_c1bafb8e.doc | ⏭️ 跳过 | 与系统内已有岗位（同名+同部门+同组织）完全相同，已覆盖更新
3 | 加密版JD.pdf | ❌ 失败 | 无法解析（加密），请提供未加密文字版（扫描件/图片 JD 在 macOS 上会自动 Vision OCR 救回，同简历侧口径）
────────────────────────
小计：新入库 1 | 覆盖 1 | 跳过 0 | 失败 1 | 附件已传 2 | 附件失败 0
```

## 业务规则（与老版一致）

- 查重主键 = **岗位名称 + 所属部门 + 组织分类**，三者全同才算重复并覆盖更新；不同组织下的同名同部门岗视为不同岗位、各自新建。
- 组织分类**必填**；部门不在选项内时**不需要也不允许**手工改字段选项——脚本写记录时服务端会按选项名自动补建（shared 层 `ensure_options` 已是只读实现，彻底移除了会触发 option id churn、静默清空存量单元格的 `field update` 路径；见 execution-notes「选项维护」），或按现有归类并告知。
- 权重必填勿留空：JD 未显式给比例时默认 必备 70% / 加分 30%。
- JD 附件一律**原始文件名**，单个/批量都必须传，禁止改名。
- 解析失败如实告知，不硬造字段。
- 老版"需求提交人=当前登录用户"规则**降级为可选（契约 v3 §9#5，这是相对老插件的一处业务语义弱化，需向客户明示）**：config.json 的 `fields.job` 里配置了可选键 `submitter` 时，Turn 1 脚本自动取当前登录用户回填该 user 字段（取不到 → 留空 + warnings）；未配置时该列**留空**并在 `report.warnings` 里说明（是否补配该列由客户决定）。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 脚本直接批量落库。未连或 `dws` 未登录 → 只能输出岗位结构化草案（Markdown），提示先开启钉钉连接器；机器缺 python → 引导安装后重跑（自检两条命令，**分开执行、不要用 `&&` 串**：PowerShell 5.1 不认 `&&`，Windows 用户粘进去直接报「标记"&&"不是此版本中的有效语句分隔符」；① `python3 -c "import sys;assert sys.version_info[:2]>=(3,9),sys.version;print(sys.version)"` 查版本界，要求 Python **3.9+**、低于 3.9 当场抛 AssertionError 而不会拖到 import 才炸；② `dws aitable base list --limit 1` 查登录态；Windows 把 `python3` 换成 `py -3`，**不要用裸 `python`**，可能是 Microsoft Store 别名、静默失败退出码 49）。
