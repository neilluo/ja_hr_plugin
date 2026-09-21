---
name: candidate-query
description: 只读查询钉钉 AI 表格四张表（简历/岗位/匹配/权限），支持等值过滤、字段裁剪与按岗位聚合推荐统计。Use when 用户说 查简历/查候选人/查岗位/匹配统计/招聘看板数据。
argument-hint: <表名> [--filter 键=值] [--fields 键,键] [--stats]
argument-hint-en: <table> [--filter k=v] [--fields k,k] [--stats]
argument-hint-zh: <表名> [--filter 键=值] [--fields 键,键] [--stats]
name_en: Candidate Query
name_zh: 候选人查询
description_en: Read-only queries over the four DingTalk AI Table sheets with filters, field projection and per-job recommend stats.
description_zh: 四表只读查询：等值过滤、字段裁剪、按岗位聚合推荐统计。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 查询与统计

```bash
python3 shared/query.py resume --filter phone=13812345678
python3 shared/query.py resume --fields name,phone,education,skills
python3 shared/query.py job --fields job_id,job_name,department,status
python3 shared/query.py match --stats          # 按岗位聚合 推荐/待定/不推荐
python3 shared/query.py perm
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

- 表名：`resume | job | match | perm`；过滤键用业务键（name/phone/job_id/recommend…）。
- `--filter` 可重复传多个，关系为 AND，等值匹配。
- 输出 JSON：`{count, records:[{id, fields}]}`；select 类字段已归一为字符串。
- 本 skill 只读；写操作一律走 resume-intake / job-intake 的脚本。
- 需要 HTML 看板时：读 `shared/query.py job` + `match --stats` 的 JSON 自行渲染，勿改脚本。
