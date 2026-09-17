---
name: recruit-model
version: 0.2.0
description: Internal shared knowledge base for the fast recruitment matching suite. Holds the table structure, business keys, org rules, scoring rubric, pipeline discipline and execution pitfalls. Referenced by all other skills; not shown in the user menu.
name_en: Recruitment Model (Internal)
name_zh: 招聘底座（内部）
description_en: Internal knowledge base of the fast recruitment suite - table structure, business keys, org rules, scoring, pipeline discipline and pitfalls.
description_zh: 极速版招聘套件内部知识库：表结构与业务键对照、组织口径、评分口径、三段式流水线纪律、执行踩坑。被其它技能引用，不在菜单露出。
user-invocable: false
---

# 招聘底座（内部知识库）

本技能不直接面向用户，是套件所有业务技能的共享事实源。任何入库、匹配、查询、看板操作前，先加载本技能指向的参考文件，并全程遵守：

1. [系统配置 system-config.md](references/system-config.md) —— 四表结构、业务字段键、组织口径、评分与推荐口径、查重主键、config.json 说明。**ID 一律以 config.json 为准，本文件是人读视图；禁止在对话中向用户暴露任何 ID。**
2. [执行注意事项 execution-notes.md](references/execution-notes.md) —— 沟通铁律、三段式流水线纪律、产物凭证校验（防静默早退）、批量分片、跨平台调用、选项维护、清单式输出。
3. 按需取用：
   - 脚本如何解析原文、解析失败怎么办 → [parsing-methods.md](references/parsing-methods.md)
   - 批量语义判定（Turn 2）输出规范 / 旧版 AI 字段同构规范 → [ai-analysis-spec.md](references/ai-analysis-spec.md)

## 本版与老版的本质区别（执行机制，不是业务语义）

老版由 agent 在对话里逐条敲 `dws aitable` 命令，一份简历要 20~40 个工具回合，每回合边际墙钟成本约 5.4 秒。本版改为**三段式流水线**：

```
Turn 1  跑一个 Python 脚本 —— 提取/抽字段/查重/批量写库/并发附件/回读，全部确定性工作
Turn 2  agent 做一次批量语义判定 —— 唯一真正用算力的地方，产出 decisions.json
Turn 3  跑一个 Python 脚本 —— 校验/批量建匹配记录/脚本重算分数与岗位统计/回读/出报告
```

**铁律：除 Turn 2 的语义判定外，禁止 agent 逐条敲 dws 命令。** 所有表格读写必须发生在脚本内部（批量、带重试、带回读）。这是全部性能收益的来源。

业务规则（组织口径、查重主键、评分口径、沟通铁律、清单式输出等）与老版**完全一致**，逐条保留在上述参考文件中。

## 何时读本文件的哪部分

- 要理解字段/组织/评分/查重口径 → system-config.md
- 跑任何脚本前后（调用方式、产物校验、分片、跨平台） → execution-notes.md
- 用户问"为什么解析失败/扫描件怎么办" → parsing-methods.md
- 做匹配 Turn 2 批量判定、产出 decisions.json → ai-analysis-spec.md + [../match-verify/SKILL.md](../match-verify/SKILL.md)

## 数据底座

钉钉 AI 表格 Base「招聘筛选」（或 replicate 复刻出的同构 Base），四表：岗位JD表 / 简历库管理 / 智能匹配 / 权限配置。脚本通过 `dws` CLI 的 subprocess 调用访问表格，**业务名 → 真实 ID 的映射唯一来源是插件根目录的 `config.json`**（由 [复刻部署](../replicate/SKILL.md) 技能生成，格式见根目录 `config.example.json`）。脚本内零硬编码 ID；若实查发现 config.json 与实际表结构不符，以实查为准并同时回填 config.json 与 system-config.md（双写）。

## 运行时前提

- 机器上存在可用的 Python 3（3.9~3.14 均可，脚本零第三方 pip 依赖，olefile/pypdf 已 vendor 进 `shared/vendor/`）。千问办公不自带 python 运行时，缺失时需先安装。
- `dws` CLI 已登录且授权组织与目标 Base 一致。
- 自检一条命令：`python3 -V && dws aitable base list --limit 1`（Windows 用 `py -3 -V && dws aitable base list --limit 1`）。
