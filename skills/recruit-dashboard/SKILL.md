---
name: recruit-dashboard
description: 从岗位表+匹配表生成单页 HTML 招聘看板（岗位漏斗、推荐 Top、部门分布），只读不写表。Use when 用户说 招聘看板/看板/招聘进展可视化/生成 dashboard。
argument-hint: [输出路径，默认 outputs/dashboard.html]
argument-hint-en: [output path, default outputs/dashboard.html]
argument-hint-zh: [输出路径，默认 outputs/dashboard.html]
name_en: Recruit Dashboard
name_zh: 招聘看板
description_en: Read-only single-page HTML dashboard from the job and match sheets (funnel, top candidates, department split).
description_zh: 岗位+匹配表只读生成单页 HTML 看板（漏斗/Top 候选人/部门分布）。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 招聘看板

## 数据源（只读两条命令）

```bash
python3 shared/query.py job --fields job_id,job_name,department,status,stat_total,stat_recommend,stat_pending,stat_reject
python3 shared/query.py match --stats
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

## 生成

- 单文件 HTML（内联 CSS/JS，无外部依赖），默认写 `outputs/dashboard.html`。
- 三块内容：① 岗位漏斗（总数→推荐→待定→不推荐，按 stat_* 画条）；
  ② 推荐 Top 榜（match 表 recommend=推荐 按 total_score 降序，取前 20，
  用 `shared/query.py match --filter recommend=推荐 --fields name,job_name,total_score,evidence`）；
  ③ 部门分布（job 按 department 聚合在招数）。
- 数字一律来自上面命令的 JSON，不要手填、不要推算；stat_* 为空（未跑匹配）的岗位
  显示「未匹配」而不是 0。
- 生成后用 present_files 交付，并一句话说明数据截止时间（job.submit_time 最大值）。

## 边界

- 本 skill 只读 + 写本地 HTML，绝不写表。
- 匹配表为空时看板只出岗位清单与提示「先跑 match-verify」。
