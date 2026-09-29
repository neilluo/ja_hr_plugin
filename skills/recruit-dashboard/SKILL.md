---
name: recruit-dashboard
description: 从岗位表+匹配表只读生成单页 HTML 招聘看板（岗位漏斗、推荐 Top、部门分布、数据截止时间），全部聚合由脚本确定性完成，agent 只运行并转述一行摘要。Use when 用户说 招聘看板/看板/招聘进展可视化/生成 dashboard。
argument-hint: [output path, default outputs/dashboard.html] [--top N]
argument-hint-en: [output path, default outputs/dashboard.html] [--top N]
argument-hint-zh: [输出路径，默认 outputs/dashboard.html] [--top N 推荐榜条数]
name_en: Recruit Dashboard
name_zh: 招聘看板
description_en: Read-only single-page HTML dashboard from the job and match sheets (funnel, top candidates, department split, as-of time), all aggregation done deterministically by the script.
description_zh: 岗位+匹配表只读生成单页 HTML 看板（漏斗/Top 候选人/部门分布/数据截止时间），聚合一律由脚本确定性完成。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 招聘看板

## 生成（一条命令）

```bash
python3 skills/recruit-dashboard/scripts/build_dashboard.py                 # 默认写 outputs/dashboard.html
python3 skills/recruit-dashboard/scripts/build_dashboard.py out.html --top 10
```

以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

脚本一次性完成全部**确定性**工作（漏斗合计、Top-N 按 total_score 降序、部门分布、
数据截止时间 = job.submit_time 最大值、单文件 HTML 渲染），字段/推荐标签/日期格式一律
派生自 config.json 唯一真源。stdout 是一行现成摘要，形如：

```
看板已生成：19 岗位 / 0 匹配记录 / 数据截止 2026-09-28 21:05 → outputs/dashboard.html
```

agent 只做两件事：**运行脚本 → 原样转述这一行摘要**（可加一句用 present_files 交付
outputs/dashboard.html）。禁止手拼 HTML、禁止手算/复核任何数字、禁止再跑 query.py 对照。

## 边界

- 本 skill 只读 + 写本地 HTML，绝不写表。
- 岗位表为空：脚本渲染空态看板并提示先入库，不崩溃。
- 匹配表为空或岗位 stat_* 为空：看板照常出岗位清单/部门分布，漏斗区提示「先跑 match-verify」。
