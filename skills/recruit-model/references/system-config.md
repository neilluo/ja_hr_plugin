# system-config — 表结构与运行配置（OpenAPI 直连版）

## Base 与表

Base「招聘筛选」`0eMKjyp813njqxkNhrKkx0nPVxAZB1Gv`，四表：

| 业务键 | 表名 | table_id |
|---|---|---|
| resume | 简历库管理 | ohY4Dp6 |
| job | 岗位JD表 | 5Y4JylL |
| match | 智能匹配 | PpMmjfG |
| perm | 权限配置 | Sz7mZDn |

config.json 只存 `base_id` / `operator_id` / 每表 `table_id` / 业务键→中文字段名 /
字段类型表。**OpenAPI 记录接口以中文字段名为 key，不需要字段 ID**；Base 里新增列时
往 config.json 加一行即可。

## 字段口径（业务键）

- resume：name phone email education school school_rank years_experience major
  certificates expected_position expected_location expected_salary skills attachment
  upload_time category comm_status org full_text attach_md5
- job：job_id job_name department org status work_location responsibilities
  requirements hard_gates must_skills bonus_skills must_weight bonus_weight
  submit_time attachment stat_total stat_recommend stat_pending stat_reject
- match：name phone job_name job_id org source cand_skills must_skills bonus_skills
  hard_gates expected_position years_experience skill_score bonus_score total_score
  recommend update_time evidence
- perm：user org

留空字段：`resume.org` 与 `job.stat_*` 由人工/匹配流程维护，入库脚本不写。

## 类型与写入格式

- singleSelect 写字符串、读回 `{id,name}`（客户端 `_norm` 归一成字符串）
- multipleSelect 写字符串数组、读回归一成字符串数组
- number 写 float；date 写毫秒时间戳
- attachment 写 `[{"filename","size","type","url":resourceUrl,"resourceId"}]`
- text 一律字符串；解析器产出 list 的（certificates）由入口脚本 join 成「、」分隔

## 凭证与权限

- 凭证：`.secrets.json`（gitignore）或 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET`
- 应用 ja_hr_poc 权限点：`Notable.Base.Read.All`、`Notable.Base.Write.All`、`Storage.File.Read`
- operatorId = 操作人 unionId，所有 notable 接口 query 必传（config.operator_id）
