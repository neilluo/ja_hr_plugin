---
name: job-intake
description: 岗位说明书（JD）批量入库到钉钉 AI 表格岗位JD表。从文件名拆组织/部门，正文切岗位职责/任职要求/硬性门槛，按 部门+岗位名 幂等去重。Use when 用户说 岗位入库/JD上传/导入岗位说明书/建岗位。
argument-hint: <岗位说明书目录路径>
argument-hint-en: <JD directory path>
argument-hint-zh: <岗位说明书目录路径>
name_en: Job Intake
name_zh: 岗位入库
description_en: Batch-upload job descriptions into the DingTalk AI Table job sheet with idempotent dedupe by department+title.
description_zh: 岗位说明书批量入库：文件名拆部门、正文切段、幂等去重。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 岗位 JD 入库

## 执行

```bash
python3 skills/job-intake/scripts/upload_jobs.py <目录>            # 真实入库
python3 skills/job-intake/scripts/upload_jobs.py <目录> --dry-run  # 预演，stdout 含全部 rows
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

## 规则

- `job_id = J + md5(部门|岗位名)` 前 10 位 hex，天然幂等：同部门同岗位名重跑即 skipped_dup。
- 组织/部门从文件名拆（形如 `岗位说明书-制造中心-曲靖制造基地-单晶制造部-设备部 - 工程师.doc`），
  部门归一到 Base 已有枚举；两级部门（硅片制造部-工艺部）保留连字符。
- 必备/加分技能从任职要求按词表命中；权重默认 0.7/0.3，dry-run 里可先看再人工调整：
  用 `shared/query.py job --fields job_id,must_weight` 查 id，再经 Notable.update_records 改。
- JD 附件与简历同纪律：先传后写，附件失败该条不入库。
- 统计字段（候选人总数/推荐数等）入库时留空，由匹配流程后续刷新。

## 报告处置

`created` 与 `readback_missing`：missing 非空重跑即可（幂等）。`failed` 看 error，
多为文本提取失败（加密 doc 等），向用户列出文件名。
