# system-config — 表结构与运行配置（OpenAPI 直连版）

## Base 与表

四表业务键：`resume` / `job` / `match` / `perm`。
**base_id、每表 table_id 与表名一律见 `config.json` 的 `base_id` / `tables`**，文档不手抄
（换 Base 只改 config，抄进文档的 id 会立刻变成谎报）。

config.json 是表结构唯一事实源，存：`base_id` / `operator_id` /
`tables`（业务键→table_id+表名）/ `fields`（业务键→中文字段名，书写顺序=建表顺序）/
`types`（字段类型）/ `options`（单选、多选选项清单）。建表
（`skills/replicate/scripts/replicate_base.py`）与补列（`sync_schema.py`）都从 config 派生，
仓内不维护第二份字段清单；脚本禁止运行时回写 config，缺映射即报错人工修。

**OpenAPI 记录接口以中文字段名为 key，不需要字段 ID**；结构接口（扩选项）按中文名运行时解析 ID，
ID 是 Base 私有的，禁止硬编码进代码或 config。

## 字段口径

**字段全集见 `config.json fields.<表>`**（配 `types.<表>` 看类型、`options.<表>` 看选项）。
文档只记口径约定，不抄清单：

- AI 语义分析三列为 schema 净增量（与组织无关），类型均 text：resume 表 `ai_extract`
  （AI结构化提取）/ `ai_deep`（AI深度解析），match 表 `ai_analysis`（AI匹配分析）。
- 留空字段：`resume.org`（简历库所属组织）为人工/预留列——全链路脚本均不赋值，由 HR 手工
  维护或留空预留；匹配的组织口径以岗位侧 org 为准（简历侧 org 为空时机械门槛自动跳过组织比对）。
  `job.stat_*` 由匹配流程 stats 维护，入库脚本不写。
- `match.org` 写入方：`match_gated.py --commit` 与 `match_analyze.py apply` 落库时取所匹配
  岗位（job 表）的 org，即 match.org = 岗位侧组织分类，不取简历侧。
- 枚举字段（学历、部门、组织、城市、recommend、source、status、comm_status 等）的取值
  真源是 `config.json options`；打分/统计代码按顺序取用（如 recommend 三态顺序），不另抄副本。

## 类型与写入格式

- singleSelect 写字符串、读回 `{id,name}`（客户端 `Notable._norm` 归一成字符串）
- multipleSelect 写字符串数组、读回归一成字符串数组
- number 写 float；date 写毫秒时间戳
- attachment 写 `[{"filename","size","type","url":resourceUrl,"resourceId"}]`
- text 一律字符串；解析器产出 list 的（certificates）由入口脚本 join 成「、」分隔
- 真表无 richText/telephone/email/user 类型列：字段 API 不支持改类型（PUT 静默忽略），
  config.types 已对齐真表实况（phone/email/responsibilities/requirements/perm.user 均 text）；
  发现类型漂移改 config 不改表

## 凭证与权限

- 凭证：`.secrets.json`（gitignore）或 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET`
- 应用权限点：`Notable.Base.Read.All`、`Notable.Base.Write.All`、`Storage.File.Read`
- operatorId = 操作人 unionId，所有 notable 接口 query 必传（config.operator_id）
- 新 Base 需把应用机器人显式加为协作者，仅开 API 权限点不够
