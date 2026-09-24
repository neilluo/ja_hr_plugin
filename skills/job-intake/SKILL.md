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
- **必做：三列由智能体分析后回写**（脚本从正文词表命中的结果不算数，例如只写出「会计、财务、账务」）。
  **触发时机是用户说「智能分析JD」，不是在上传环节**——上传只做解析/幂等去重/写原文与附件；
  推理时逐岗读完任职要求与岗位职责，产出 payload 并执行
  `python3 skills/job-intake/scripts/sync_job_columns.py payload.json`：
  - `hard_gates` 固定五段、缺项写"不作硬性要求"：`学历：…；专业：…；经验：…；证书：…；年龄：…`
    （这四五项是一票否决依据，也是匹配复核的对照清单）；
  - `must_skills` 6~10 个、`bonus_skills` 4~8 个，用「、」分隔的短词，
    **词表必须与简历库技能标签同源**（成本会计写"账务处理"而候选人标签是"总账处理"，命中率直接归零）；
  - 不要塞"Excel/Word/办公软件"这类无区分度词，除非 JD 把它写成核心要求。
- 回写后跑一次 `python3 skills/job-intake/scripts/check_skill_coverage.py`，把可命中率低于 50% 的岗位改词再同步，
  直到该脚本 exit 0 才算完成入库。
  **但要先分辨低覆盖的两种原因**：① 用词与简历标签不同源 → 改词；② 库里确实没有这类候选人（如成本会计
  岗，库内财务候选人只有主管级、无成本核算经验）→ 属有效业务信号，保留原词并在汇报里说明。
- 权重默认 0.7/0.3；如需调整用 `shared/query.py job --fields job_id,must_weight` 查 id，
  再经 Notable.update_records 改。
- JD 附件与简历同纪律：先传后写，附件失败该条不入库。
- 统计字段（候选人总数/推荐数等）入库时留空，由匹配流程后续刷新。

## 「智能分析JD」并发流水线（subagent，agent数硬上限20）

以下命令一律以仓库根为 CWD 执行：

```bash
python3 skills/job-intake/scripts/jobs_analyze.py prepare --all     # 自动负载均衡：agent 数尽量铺满，硬上限 20
# → 按 meta.batches 的数量，在同一条消息里一次性并发发 agent（≤20，不分波、不串行）
#   每个只给：提示词 skills/job-intake/references/job-subagent-prompt.md + 批次号 + part 路径
python3 skills/job-intake/scripts/jobs_analyze.py merge             # → outputs/jobs_done.json（missing_batches 非空则补发该批）
python3 skills/job-intake/scripts/sync_job_columns.py outputs/jobs_done.json   # 写回三列
python3 skills/job-intake/scripts/check_skill_coverage.py           # 词表同源校验，必须 exit 0
```

19 个岗位 → 19 个 agent（每个1岗，未超20）。子任务判不了时才手工兜底（直接在 payload 里写三字段）。
**新增岗位后重跑本流水线即可，已分析过的岗位 prepare 会自动跳过（加 `--all` 才强制重做）。**

## 报告处置

`created` 与 `readback_missing`：missing 非空重跑即可（幂等）。`failed` 看 error，
多为文本提取失败（加密 doc 等），向用户列出文件名。
