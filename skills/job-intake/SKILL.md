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
  上传只做解析/幂等去重/写原文与附件；精析由后台周期任务消费队列异步完成，用户说「智能分析JD」
  时也可手动触发（流水线见下）。推理时逐岗读完任职要求与岗位职责，产出 payload 并执行
  `python3 skills/job-intake/scripts/sync_job_columns.py payload.json`（同一次 update 打 ai_refined_at 标记出队）：
  - `hard_gates` 固定五段、缺项写"不作硬性要求"：`学历：…；专业：…；经验：…；证书：…；年龄：…`
    （这四五项是一票否决依据，也是匹配复核的对照清单）；
  - `must_skills` 尽量 6~10 个、`bonus_skills` 尽量 4~8 个（软偏好，不是硬门槛：数量/字数
    不符只会被 merge 记为观察，不丢行、不阻断写回），用「、」分隔的短词，
    **词表必须与简历库技能标签同源**（成本会计写"账务处理"而候选人标签是"总账处理"，命中率直接归零）；
  - 不要塞"Excel/Word/办公软件"这类无区分度词，除非 JD 把它写成核心要求。
- 回写后跑一次 `python3 skills/job-intake/scripts/check_skill_coverage.py --emit-fixes`，
  低覆盖岗位的 ①/② 两因分辨与建议词由脚本产出（jobs_fix.json / jobs_fix_report.json，
  详见下节「三模式」），agent 复核报告后 `sync_job_columns.py outputs/jobs_fix.json` 重同步，
  直到仅剩 ②类才算完成入库。
  **两因分辨口径**：① 用词与简历标签不同源 → 改词；② 库里确实没有这类候选人（如成本会计
  岗，库内财务候选人只有主管级、无成本核算经验）→ 属有效业务信号，保留原词并在汇报里说明。
  建议词是启发式候选（共享汉字近邻），落库前必须过目 report，同形不同义不能盲收。
- 权重默认 0.7/0.3；如需调整用 `shared/query.py job --fields job_id,must_weight` 查 id，
  再经 Notable.update_records 改。
- JD 附件与简历同纪律：先传后写，附件失败该条不入库。
- 统计字段（候选人总数/推荐数等）入库时留空，由匹配流程后续刷新。

## 上传后：注册精析消费任务（硬性步骤，脚本退出后立即执行）

**上传回合不跑精析流水线**：脚本只解析、去重、写原文与附件（三列先落正则粗值入队）。
岗位三列由**独立后台会话**消费精析队列产出，与简历链完全对称（机制与边界见 AGENTS.md 不变量 11，
此处不复述细节）。

脚本跑完、报告 `refine_queued > 0` 时，agent **必须立即注册一个消费任务**
（定时任务管理工具，schedule kind=every）。**注册规格直接取报告字段 `cron_job` 原样透传**——
脚本已备好完整规格（`shared/refine_loop.py` 的 `consume_task_spec` 产出，resume/job 两链共用），
禁止手写 payload、禁止改写任何字段：

- **消费任务（schedule = every 型，无绝对时刻）**：payload 指令——进本仓库跑
  `python3 skills/job-intake/scripts/jobs_analyze.py queue`；job 为 0 则静默结束；
  非空则按本 SKILL「智能分析JD」流水线全自动执行到 `sync_job_columns.py` 写回。
- **完成即自删**：payload 末条指令必须是"无论队列空静默结束 / refused 撞租约退出 / 正常消费完，
  结束前都按任务名删除本任务自身"——every 型任务任一出口漏删就会每 60s 反复触发，故三个出口都写死自删；
  已消费完的任务不留列表；每日 09:30 兜底的自清理只是对崩溃在自删之前的任务的兜底网。
- **为何是 every 型而非 at 型**：at 型要求 `schedule.at > now`，而"脚本返回 → agent 发出注册"至少要一个
  模型回合（实测 25-30s），曾取 `now+45s` 时窗口被吃光、注册被"Scheduled time must be in the future"拒收。
  every 型注册即启用、每 60s 触发、消费完自删，注册永不过期（间隔真源 = `EVERY_MS`，文档不复述数值）。
- 写回后跑 `python3 skills/job-intake/scripts/check_skill_coverage.py --emit-fixes`，低覆盖按本 SKILL
  两因分辨纪律处理：① 用词与简历标签不同源 → 脚本产建议词进 jobs_fix.json，过目 report 复核后
  重同步；② 库里确实没有这类候选人 → 保留原词并在任务结果里说明，不强行凑 exit 0。
  简历标签池为空（resume 表无 skills）时跳过覆盖率自检。
- prepare 返回 refused（exit 2，撞活租约）即自删退出、禁止抢跑；不设看门狗，消费任务崩溃由
  每日 09:30 兜底巡检重吃（不变量 11）。
- 注册只能由 agent 做（脚本是独立进程，宿主机没有向千问办公写调度的本地 API）；
  脚本的配套职责是输出 `refine_queued`（触发信号）与 `cron_job`（现成注册规格）。
- 注册完成后向用户一句话说明：队列 N 条、约 1 分钟后后台自动精析、异常时最迟次日 09:30 兜底
  （措辞取 `user_line`，禁止手抄分钟数）。

## 「智能分析JD」并发流水线（subagent，agent数硬上限20）

三列精析不必卡在上传环节：上传即写正则粗值入队（标记列 `ai_refined_at` 空）。精析消费入口 =
上传后 agent 注册的一次性消费任务（见上节）+ 每日 09:30 兜底巡检；队列谓词唯一真源
`shared/refine_loop.py`（job 队列 = ai_refined_at 空且 responsibilities 非空）。
用户说「智能分析JD」时也可手动触发或补跑，用以下命令（一律以仓库根为 CWD）：

```bash
python3 skills/job-intake/scripts/jobs_analyze.py prepare            # 只取队列中的岗；--all = 连已精析的一起重析
#   周期租约 outputs/refine_job.lock：prepare 获取（活租约期内第二个周期 refused exit 2，
#   勿抢跑），sync_job_columns 写回时释放；同周期重切批加 --force
#   prepare 同时渲染 per-batch 提示词到 outputs/jobs_prompt_part<N>.md（占位符已由代码替换），
#   分派清单落 outputs/jobs_dispatch.json（{"batches":N,"prompts":[...]}）；
#   词表参考 outputs/resume_vocab.json = 简历标签池实值派生（岗位选词的打分对手方，非 job 表自参照）
# → 按 jobs_dispatch.json 的 prompts 列表，在同一条消息里一次性并发发 agent（≤20，不分波、不串行），
#   每个任务的 prompt 只写一行指针：读 <jobs_prompt_partN.md 路径> 并执行（禁止内联提示词原文，
#   大批次会撑爆工具流入参导致截断分波，见 AGENTS.md 犯错记录）；
#   subagent 只"读→推理→写产物"，不自检（schema 判定唯一真源在 merge 的 validate_row）
python3 skills/job-intake/scripts/jobs_analyze.py merge   # → outputs/jobs_done.json
#   merge 内联两级判定：done_integrity（盘上体检 missing_batches/bad_batches）+
#   行级 normalize_row（自动修格式：缺门槛段补"不作硬性要求"、must/bonus 去重、半角标点转全角、数组转串）
#   + validate_row（L0 唯一丢行判定：非 dict / 缺或重复 job_id / 三列全空 → dropped_rows、不打标、下周期重析）；
#   质量问题（技能词字数、必备/加分项数量）只进 observations 非阻断观察，照常写回、不影响 all_complete；
#   报告键序 = merged/batches/missing_batches/bad_batches/dropped_rows/normalized/normalizations/observations/all_complete；
#   failed 先信体检——all_complete=true 即进写回，仅 missing/bad 批次才补发（省人工验盘回合）
python3 skills/job-intake/scripts/sync_job_columns.py outputs/jobs_done.json   # 写回三列，同一次 update 打 ai_refined_at 标记（出队）
python3 skills/job-intake/scripts/check_skill_coverage.py --emit-fixes         # 词表同源校验+两因分辨
```

**check_skill_coverage 三模式**（低覆盖处置从"agent 现场考古"下沉为脚本判定）：
- 默认：报低覆盖岗与未命中词，存在低覆盖 exit 2（②类合法保留岗同样 exit 2，处置见下）。
- `--emit-fixes [OUTDIR]`：对每个未命中词做 ①/② 分辨（①=池内存在 ≥2 共享汉字的近邻 → 建议替换；
  ②=无近邻 → 库内无此人、保留原词），产出 `jobs_fix.json`（sync_job_columns 可直接消费的 payload，
  仅改 must_skills）与 `jobs_fix_report.json`（逐词 cause/suggested/still_low）。
  agent 扫一眼 report 复核建议词（启发式候选，同形不同义如"生产排班"↔"生产计划"不能盲收），
  再 `sync_job_columns.py outputs/jobs_fix.json` → 复跑 `--emit-fixes` 收敛。
  仅剩 ②类（报告里 still_low=True）即视为达标，保留原词、汇报说明，不强行凑 exit 0。
- `--precheck payload.json`：写回前预校验 payload 建议词命中率，全 ≥min 即 exit 0 再 sync
  （把"写回→自检→发现没修好→再改"的往返掐在写库之前）。
- **池变动告警**：每次运行与上次标签池快照（outputs/resume_pool_snapshot.json）比对，池被并发
  简历精析刷新时打 pool_drift 行——上一轮改词结论作废，必须以本次为准（实测两链并行刷池曾使
  刚修完的词二次返工）。
子任务判不了时才手工兜底（直接在 payload 里写三字段）。
**check_skill_coverage 与 match_gated 同纪律**：岗位队列非空即 exit 2（粗词表覆盖率无意义），
先跑完本流水线清空队列再自检。

## 报告处置

| 字段 | 处置 |
|---|---|
| `VERDICT`（stdout 首行） | `OK`/`WARN`/`BAD` 一行结论，读报告先看它——不必逐字段扫 JSON 判成败。格式真源 `shared/report.py` |
| `created` / `readback_missing` | missing 非空重跑即可（幂等） |
| `needs_ocr` | 图片型/抽不出正文的 JD，**不入库**（岗位侧无简历那样的 OCR 补录链，空正文记录会静默卡在精析队列外、匹配时表现为"没人合适"）。向用户列出文件名，请其提供可提取文本的原件 |
| `failed` | 看 error 文本，多为文本提取失败（加密 doc 等），向用户列出文件名 |
| `refine_queued` | 当前待精析队列长度：精析异步进行，向用户说明"已入队，后台周期消费"即可，**不要在上传回合里跑精析** |
| `cron_job` | 仅 `refine_queued > 0` 时输出：精析消费任务的**完整注册规格**（name/schedule/message/contextDirs 都已备好，schedule 为 every 型无绝对时刻）。**原样传给定时任务工具**，禁止手写字段、禁止改写 |
