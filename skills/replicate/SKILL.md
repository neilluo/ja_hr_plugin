---
name: replicate
version: 0.2.0
description: Replicate the recruitment four-table base in a new organization from the standard schema, then reverse-look-up real IDs and generate config.json (the scripts' single ID source) plus the human-readable system-config.md.
name_en: Replicate System
name_zh: 复刻部署
description_en: Build the 招聘筛选 four-table base in a new org from standard schema, then backfill config.json (single ID source for scripts) and system-config.md.
description_zh: 在新组织按标准表结构搭建「招聘筛选」四表，再反查真实 ID 生成 config.json（脚本唯一 ID 源）并同步 system-config.md 人读视图；各机器各自一份 config.json，互不污染。
user-invocable: true
argument-hint: 说"在新组织复刻一套招聘系统"
argument-hint-en: Say "replicate the recruitment system in a new org"
argument-hint-zh: 说"在新组织复刻一套招聘系统"
---

# 复刻部署

适用：要在**另一个钉钉组织/账号/公司**从零搭一套同样的招聘筛选系统，或已有表需要**接线生成本机 config.json** 时。若只是本组织已有表且 config.json 已生成，直接用其它技能即可，不走本技能。先读 [招聘底座](../recruit-model/SKILL.md)。

**config.json 是本套件脚本的唯一 ID 源（D8）**：`skills/*/scripts/` 里的所有脚本通过 `--config` 读它做业务名→真实 ID 映射，脚本内零硬编码 ID。`recruit-model/references/system-config.md` 保留为**人读视图**，两者**双写**——改任何一侧必须同步另一侧。**两家公司、或一部门一表的场景：各自机器上各自生成自己的 config.json（插件根目录，不进版本库），互不污染**；换机器时复制 config.json 或重跑本技能的阶段③。

本技能是一次性建表/接线操作（非热路径），允许 agent 直接使用 `dws aitable` 命令；日常入库/匹配一律走各技能的脚本流水线，禁止逐条敲命令。

## 四阶段（每阶段结束汇报确认再继续）

### ① 环境与口径确认

- 确认部署目标是用户自己的钉钉组织 + 钉钉 AI 表格；连接器授权组织、`dws` 登录态与目标 Base 同一组织（授权哪个组织读写的就是哪个组织的表）。
- 对齐组织分类取值（默认 职能中心/制造中心，可按新组织改名，但**岗位表/简历库/匹配表三处必须一致**）。
- 自检运行时：`python3 -V && dws aitable base list --limit 1`（Windows 用 `py -3 -V && ...`；裸 `python` 别名可能静默失败、退出码 49）。缺 python 先引导安装（脚本兼容 3.9~3.14，零第三方 pip 依赖）。

### ② 搭 Base 与四表（标准结构见下）

用 `dws aitable +base-bootstrap`（或逐表 `table create` + `field create`）按标准 schema 建 4 张表及字段。**不猜 schema**：逐字段按下方标准结构建，任何改动须用户显式确认。

### ③ 接线：反查 ID，生成 config.json（+ 同步 system-config.md）

1. 反查新 Base/表/字段 ID 与单选/多选**选项 id**（`dws aitable field list` / `+field-get` 实查，不信记忆不信缓存）。
2. 在**插件根目录**生成 `config.json`，结构完全对照根目录 `config.example.json`：
   - `base_name` / `base_id`
   - `tables`：`job|resume|match|perm` → `{table_id, name}`
   - `fields`：每表 业务键 → fieldId（业务键清单以 `config.example.json` 为准，如 resume 的 `name/phone/education/major/certificates/expected_location/skills/attachment/category/comm_status/org/full_text/attach_md5…`（`attach_md5`=「附件内容MD5」，**可选键**：配了库内附件去重才走内容级比对，缺失则回退「文件名+字节大小」并告警；见 `config.example.json` 的 `_可选字段键`）、job 的 `job_id/job_name/department/org/status/hard_gates/must_skills/bonus_skills/must_weight/bonus_weight/submit_time/attachment/stat_total/stat_recommend/stat_pending/stat_reject…`、match 的 `name/phone/job_name/job_id/org/source/cand_skills/must_skills/bonus_skills/hard_gates/expected_position/years_experience/skill_score/bonus_score/total_score/recommend/update_time/evidence`）
   - `field_names`：业务键 → 中文字段名（人读/容错用）
   - `types` / `formatters`：字段类型（text/singleSelect/multipleSelect/number/date/telephone/email/attachment/richText/user）与数字格式（INT/PERCENT）
   - `options`：各单选/多选字段的选项缓存 `[{id,name}]`（脚本写记录与"只增不删"补选项都用它）
   - `options_cache_generated_at`：生成时间戳
3. 同步更新 `skills/recruit-model/references/system-config.md` 的附录参考 ID（人读视图，双写）。
4. 把 Base 名/地址告知用户（业务话），**config.json 不进版本库、不在对话中贴出内容**（含 ID）。

### ④ 验收（端到端跑通即交付）

各入库 1 个 JD + 1 份简历：走 [岗位入库](../job-intake/SKILL.md) 与 [简历入库](../resume-intake/SKILL.md) 的三步走，再接 [定向匹配](../match-verify/SKILL.md) 跑一轮，确认：组织一致时能产出匹配记录与分数、岗位统计被重算回填、附件为原始文件名、报告 `ok==true`、清单正常。跑通即交付；任何一步失败按 D7 重跑（≤2 次）并如实报告。

## 标准表结构（最小可运行，极速版）

**岗位JD表**：岗位ID(text，唯一键 JOB-序号) · 岗位名称(text) · 所属部门(singleSelect) · 组织分类(singleSelect:职能中心/制造中心) · 状态(singleSelect:招聘中/草稿/已关闭) · 工作地点(multipleSelect) · 岗位职责(richText) · 任职要求(richText) · 硬性门槛(text) · 必备技能(text) · 加分项(text) · 必备技能权重(number,PERCENT) · 加分项权重(number,PERCENT) · 提交时间(date) · JD附件(attachment) · 候选人总数/推荐数/待定数/不推荐数(number,INT) ·（可选：需求提交人(user)，配置后在 config.json 补 `job.submitter` 映射）

**简历库管理**：姓名(text) · 手机号(telephone) · 邮箱(email) · 最高学历(singleSelect:博士/硕士/本科/大专) · 院校(text) · 院校排名(singleSelect:985/211/双一流/普通本科/大专) · 工作年限(number,INT) · **专业(text)** · **证书(text)** · 期望职位(text) · 期望地点(singleSelect，含「不限」) · 期望薪资(text) · 技能标签(multipleSelect) · 简历库分类(singleSelect:技术类/产品类/市场类/运营类/其他) · 沟通状态(singleSelect:已入职/流程中/待筛选/简历未通过/面试未通过) · 简历库所属组织(singleSelect:职能中心/制造中心) · 上传时间(date) · 简历附件(attachment) · **简历全文(text，可选留档)** · **附件内容MD5(text，可选但强烈建议建：库内附件内容级去重的比对键，业务键 `attach_md5`)**（加粗为极速版新增，供硬门槛对照与复核／内容级查重）

> **「附件内容MD5」建不建的后果（P4b）**：建了 → 库内附件去重按**内容真 MD5** 判（同一份内容换文件名也判重复；同名同大小但内容改过一版 → 不判重复、按简历新版本覆盖更新），脚本在附件上传成功后自动写入该列，存量老记录被覆盖更新/补传附件时自动回填。**不建**（或客户现存库没这一列）→ 脚本自动回退「文件名+字节大小」并打一条 warning，不崩溃、不自建字段，但"改内容重投"会被误判重复、"同内容换文件名"会漏判。给老客户库补建时用 `dws aitable field create --base-id <base> --table-id <简历库管理> --name "附件内容MD5" --type text`，再把 fieldId 写进 config.json 的 `fields.resume.attach_md5`（同步 `field_names.resume` / `types.resume`）。

**智能匹配**：候选人姓名(text) · 手机号(telephone) · 匹配岗位(text) · **岗位ID(text，与岗位表勾连的唯一键)** · 组织分类(singleSelect) · 匹配来源(singleSelect:系统匹配/人工匹配) · 候选人技能/岗位必备技能/岗位加分项/硬性门槛/期望职位(text) · 工作年限(text) · 技能得分/加分项得分/匹配总分(number,INT) · 推荐状态(singleSelect:推荐/待定/不推荐) · 更新时间(date) · **匹配依据(text，agent 判定的原文引用)**

**权限配置**：用户(user) · 组织分类(singleSelect)

> **AI 字段（AI结构化提取/AI深度解析/AI匹配分析）、表间双向关联（岗位↔匹配）、filterUp 汇总（推荐候选人）均为表格侧可选增强：极速版一律不依赖（D1/D2）**——岗位统计由脚本重算、勾连用岗位ID文本、判定依据写普通 text 字段。是否补建由客户决定（AI 字段异步 1~3 分钟且吃 AI 额度，免费版 500 次/月）；补建与否不影响任何技能运行，config.json 也不需要为它们配映射。

## 铁律（继承老版）

不跨组织搭建、不猜 schema（逐字段按本文件，改动须显式确认）、写前确认卡 + 写后回读；覆盖/删除前用业务话讲清"对谁做什么、后果"。接线阶段反查到的一切 ID 只写入 config.json 与 system-config.md 附录，**绝不在对话中向用户展示**。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 直接建表与反查。未连 → 先开启钉钉连接器再走本技能；可先输出标准表结构文档供人工在钉钉端搭建，搭完再回来只做阶段③接线。
