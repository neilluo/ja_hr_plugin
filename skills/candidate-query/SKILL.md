---
name: candidate-query
version: 0.2.0
description: Query the recruitment data - recommended candidates for a role, matched roles for a candidate, or overall recruiting progress. Read-only, batched queries, clean business-language lists.
name_en: Candidate & Job Query
name_zh: 招聘查询
description_en: Look up candidates for a role, roles for a candidate, or overall pipeline progress; read-only batched queries, output as lists.
description_zh: 查某岗位的推荐候选人、某候选人匹配的岗位、或整体招聘进展。只读、批量查询（不逐条敲命令），以业务化清单返回。
user-invocable: true
argument-hint: 如"暖通工程师有哪些候选人"/"胡裕匹配了哪些岗位"/"现在招聘进展如何"
argument-hint-en: e.g. "candidates for HVAC engineer" / "what roles did Hu Yu match"
argument-hint-zh: 如"暖通工程师有哪些候选人"/"胡裕匹配了哪些岗位"/"现在招聘进展如何"
---

# 招聘查询

纯读操作，**不改任何记录**。先读 [招聘底座](../recruit-model/SKILL.md) 拿业务键与口径（对用户只说业务名，绝不暴露表ID/字段ID/命令/JSON）。

查询没有专用脚本：允许 agent 直接执行 `dws aitable` 的**只读查询命令**，但必须**一次查询取整批**（带 filter/字段裁剪，必要时翻页取全），禁止逐人逐岗循环敲命令；ID 对照以插件根目录 `config.json` 为准（业务键 → fieldId）。

## 分支 A：某岗位 → 候选人

"暖通工程师有哪些候选人"：

1. 岗位JD表按岗位名称定位岗位（命中多条 → 列候选让用户辨认，**绝不默认取第一条**），取其 `job_id`。
2. 智能匹配表按 `岗位ID = 该 job_id` **一次查询**取全部匹配记录（推荐状态/匹配总分/技能得分/加分项得分），按总分降序。极速版不依赖岗位表的「推荐候选人」filterUp 汇总列（D1，可能不存在）；统计四数（候选人总数/推荐数/待定数/不推荐数）由匹配脚本重算回填，可直接读作汇总核对。

输出：候选人 | 匹配度 | 推荐状态，末尾汇总（推荐 N 人 / 待定 N 人 / 不推荐 N 人）。

## 分支 B：某候选人 → 岗位

"胡裕匹配了哪些岗位"：

1. 简历库按姓名（或手机号）定位候选人；**重名 → 列候选（姓名/期望职位/组织）让用户确认，绝不默认取第一条**。
2. 智能匹配表按候选人姓名+手机号一次查询取其全部达标匹配。

输出：岗位 | 匹配度 | 技能 | 加分 | 结论；末尾一句"其余 N 岗未过硬性门槛、无记录"（如可从匹配依据/报告得知）。

## 分支 C：整体进展

"现在招聘进展如何"：一次查询岗位JD表（状态=招聘中，取岗位名称/部门/组织/四个统计数），汇总输出一屏进展表 + 一句结论（哪些岗位有推荐人、哪些还零候选）。统计数为空 = 该岗位尚未跑过匹配重算，如实说明，不编数字。

## 规则（继承老版）

- 只读，不改任何记录；查询与看板全程无写入。
- 结果用清单/表格，一行一个对象，末尾汇总；不堆砌全部字段、不暴露 ID/命令/JSON。
- 关键词命中多条岗位/候选人 → 先列候选让用户辨认。
- 沟通状态=已入职的候选人标注终止态；其匹配记录情况如实呈现。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 直接查库。未连或 `dws` 未登录 → 提示需先开启连接器才能查询真实数据，不做任何猜测性回答。
