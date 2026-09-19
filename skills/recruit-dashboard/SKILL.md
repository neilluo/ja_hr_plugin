---
name: recruit-dashboard
version: 0.2.0
description: Generate a one-page HTML recruiting dashboard from the job table - open roles, candidate totals, recommended counts and per-role cards. Read-only, single batched query.
name_en: Recruit Dashboard
name_zh: 招聘看板
description_en: Render a single-file HTML snapshot of open roles and their candidate/recommendation counts (one batched read-only query).
description_zh: 从岗位表生成一份精简的单文件 HTML 招聘看板：在招岗位、候选人总数、推荐数与分岗位卡片。只读、一次批量查询取数；统计数由匹配脚本重算回填，看板直接读取。
user-invocable: true
argument-hint: Say "recruit dashboard" or "show recruiting overview"
argument-hint-en: Say "recruit dashboard"
argument-hint-zh: 说"招聘看板"或"看下招聘全局"
---

# 招聘看板（精简版）

只做全局快照，一屏看清"哪些岗位缺人、哪些已推荐到人"。先读 [招聘底座](../recruit-model/SKILL.md) 取业务键与口径；**全程只读**，对用户只说业务语言。

看板没有专用脚本：允许 agent 直接执行 `dws aitable` 的**只读查询**取数，但必须**一次查询取全部在招岗位**（filter 状态=招聘中 + 字段裁剪，必要时翻页取全），禁止逐岗循环敲命令；ID 对照以插件根目录 `config.json` 为准。

## 流程

1. 一次查询「岗位JD表」状态=招聘中 的全部岗位，取：岗位名称、所属部门、组织分类、候选人总数、推荐数、待定数、不推荐数（这四个统计数由 [定向匹配](../match-verify/SKILL.md) 的 apply 脚本在每次匹配后从表内全部匹配记录重算回填，D15；看板直接读取，**不自行累加、不依赖 lookup/filterUp**，D1）。
2. 聚合 KPI：在招岗位数、候选人总数、推荐总数、"零候选人岗位"数（待补缺口）。统计数为空的岗位 = 尚未跑过匹配重算，如实标"未匹配"，不编数字。
3. 渲染**单文件 HTML**（内联样式，无外部依赖，无 localStorage）到工作区输出目录，命名 `招聘看板_YYYYMMDD.html`。
4. 用 present_files 卡片交付给用户。

## 看板内容（精简，固定这几块就够）

- 顶部 KPI 一行：在招岗位 N ｜ 候选人总数 N ｜ 已推荐 N ｜ 待补缺口（零候选岗位）N
- 岗位卡片网格：每卡显示 岗位名·部门·组织 + 候选人总数/推荐数/待定数；推荐数为 0 的卡片标红提示"待补"。
- 底部一句小结：最紧缺的岗位（候选人最少/为零者 Top3）。

> 暂不做发群通晒、图片导出等；后续需要再增强。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 取真实数据渲染。未连或 `dws` 未登录 → 提示先开启连接器；可退化为把 KPI 与岗位表以 Markdown 清单直接输出到对话（同样只读、不猜测数据）。
