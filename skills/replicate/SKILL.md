---
name: replicate
description: 在新组织/新 Base 复制四表结构（表+字段+单选选项）并生成 config 片段，用于套件分发到新钉钉组织。Use when 用户说 复制一套/新组织部署/重建表结构/换个 Base。
argument-hint: <新baseId> [--operator unionId]
argument-hint-en: <new baseId> [--operator unionId]
argument-hint-zh: <新baseId> [--operator unionId]
name_en: Replicate Base
name_zh: 复制表结构
description_en: Recreate the four-table schema (sheets, fields, select options) in a new Base and emit a config fragment for distribution.
description_zh: 在新 Base 重建四表结构并输出 config 片段，用于跨组织分发。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 复制表结构到新 Base

## 执行

```bash
python3 skills/replicate/scripts/replicate_base.py <新baseId> [--operator <unionId>]
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

脚本从 `config.json`（唯一事实源：fields/types/options，字段书写顺序=建表顺序）派生，
逐表建 sheet + 逐字段建列（单选/多选带选项），
stdout 输出可合并进 config.json 的片段：`{base_id, tables, fields, types}`。

## 已有表补列/结构自检

```bash
python3 skills/replicate/scripts/sync_schema.py --check   # 只读：报 config 与真实 Base 的漂移，缺列 exit 2
python3 skills/replicate/scripts/sync_schema.py           # 补建缺失列（只补不删不改）
python3 skills/replicate/scripts/sync_schema.py --rename "表名:旧列名=新列名"  # config 改名后把物理列跟上
python3 skills/replicate/scripts/sync_schema.py --drop "表名:列名"            # 删 config 未声明的物理列
```

rename/drop 是显式对齐操作（不做自动猜测）：drop 拒绝删 config 已声明的列；平台主键列（首列）不计入漂移报告。

新增字段的标准动作：改 config.json（fields/types/options）→ 跑 sync_schema.py 对目标 Base 补列，
两步同一提交；禁止手工在 Base 里建列后只改 config 半边。

## 收尾三步

1. 备份现 config.json 后，用输出片段替换 `base_id / tables / fields / types` 四段
   （`operator_id` 换成新组织操作人 unionId）。
2. 新 Base 需给应用加协作者：钉钉 AI 表格无组织级公共表，企业应用身份操作必须把
   机器人显式加为协作者，仅开 API 权限点不够。
3. 跑 `python3 shared/query.py job` 验证连通（应返回 count=0）。

## 边界与已知约束

- 只建结构不搬数据；数据迁移用 upload_jobs/upload_resumes 对源目录重跑（幂等）。
- 权限：应用需 Notable.Base.Write.All；operator 对新 Base 有编辑权限。
- 字段口径以 `config.json` 为唯一真源（含 options 选项清单）；改字段先改 config，再跑 sync_schema.py 补列。
- 建字段间隔 0.2s 是限流余量，别去掉。
