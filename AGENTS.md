# AGENTS.md — 招聘智能匹配（OpenAPI 直连版）工程约束

> 代码是唯一事实来源。CLI 参数以 `python3 <入口脚本> --help` 为准。
> 旧 emit/replay + agent 调 dws 的链路已整体废弃，代码在 `.trash/` 仅供考古，禁止参考、禁止复活。

## 架构

- Python 经钉钉 Notable OpenAPI **直连** AI 表格（`shared/notable.py` 是唯一传输层）。
  不使用 dws、不经过 agent 中转、没有状态机/checkpoint 文件。
- 凭证：`.secrets.json`（已 gitignore）或环境变量 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET`。
  仓库是 public 的，**任何凭证不得写入会被提交的文件**。
- 表结构映射在 `config.json`：业务键 → 中文字段名。OpenAPI 记录接口以中文字段名为 key，
  不需要字段 ID；新增字段只需在 Base 里建列并往 config.json 加一行。
  AI 语义分析三列为 schema 净增量（与组织无关）：resume 表 `ai_extract`→AI结构化提取、
  `ai_deep`→AI深度解析；match 表 `ai_analysis`→AI匹配分析，类型均为 text。
- 入口脚本：跨 skill 公共入口 `shared/query.py`；单 skill 私有入口
  `skills/resume-intake/scripts/upload_resumes.py`、
  `skills/job-intake/scripts/upload_jobs.py`、`skills/job-intake/scripts/jobs_analyze.py`（JD 精析 prepare/merge）、
  `skills/job-intake/scripts/sync_job_columns.py`（回写硬性门槛/必备技能/加分项）、
  `skills/job-intake/scripts/check_skill_coverage.py`（岗位技能词表 vs 简历标签命中率自检，低覆盖 exit 2）、
  `skills/match-verify/scripts/match.py`（匹配打分）、`skills/match-verify/scripts/match_gated.py`（门槛前置匹配）、
  `skills/match-verify/scripts/match_analyze.py`（逐岗 subagent 并发 AI 匹配分析）、
  `skills/match-verify/scripts/sync_match_analysis.py`（AI匹配分析回写）、
  `skills/skills-analyze/scripts/skills_analyze.py`（简历三列精析 prepare/merge）、
  `skills/skills-analyze/scripts/skills_apply.py`（写回三列）、
  `skills/skills-analyze/scripts/sync_ai_columns.py`（零散手工修正通道）、
  `skills/replicate/scripts/replicate_base.py`（建表）。
  agent 直接 Bash 跑脚本，读 JSON 报告即可，不需要中间回合。
- 跨 skill 公共库 `shared/waves.py`：subagent 并发调度规划。agent 数硬上限 20（`MAX_AGENTS`
  只能下调不能上调），自动负载均衡 batch=ceil(条数/20)，一次性并发发出、不分波不串行；
  被 skills-analyze / job-intake / match-verify 三方共用。
- match-verify 私有库 `skills/match-verify/scripts/semantic_score.py`：语义词典 + 同义命中判定，
  只服务该 skill，禁止提升到 shared/。
- 命令一律以仓库根为 CWD 执行：跨 skill 公共入口 python3 shared/query.py、bash shared/preflight/preflight.sh（Windows 用 shared/preflight/preflight.ps1）；预检三件套（py/sh/ps1）同居 shared/preflight/；单 skill 私有入口 python3 skills/<skill>/scripts/<entry>.py；shared/ 只放跨 skill 公共库与公共入口，skills/<skill>/scripts/ 只放该 skill 私有入口，scripts/ 下入口为执行而非阅读。
- 代码归属按共享范围：**只被单个 skill 引用的代码（入口或库）一律放该 skill 的 scripts/，禁止放 shared/**；
  shared/ 只收跨 skill 公共物——公共库（notable/extract/waves）、公共入口（query.py）、预检三件套（preflight/）。
  其他 skill 的 references/ 可以文档化指引私有脚本（只读），但禁止代码级跨 skill import。

## 不变量

1. 所有远端调用走 `Notable.call()`：自带 token 缓存、401 自动刷新、429/5xx 指数退避。
   禁止在脚本里直接 urllib 调钉钉（OSS PUT 除外，那是阿里云域名）。
2. 查重先行：简历按 附件MD5 → 手机号 两级去重（含批内去重）；岗位按
   `job_id = md5(部门|岗位名)` 去重。重跑同一目录必须 created=0。
3. 附件先传后写：`upload_attachment()`（uploadInfos → OSS PUT → cell）任一失败则该条
   不写表，绝不产生无附件的简历记录。
4. 写完必回读：按手机号/job_id 回读确认记录真实存在，`readback_missing` 非空即 exit 1。
5. 扫描件/图片（抽不出手机号和邮箱）进 `needs_ocr` 队列不入库，由 agent 用视觉读取后
   经 `Notable.create_records` 补录（见 skills/resume-intake）。
6. 读回值已归一：singleSelect→字符串、multipleSelect→字符串数组（`Notable._norm`）；
   写出侧 richText 字段由 `Notable._cast` 归一成 `{"markdown": ...}`，纯文本直传会 400。
7. QPS 403（QpsLimitForApi/QpsLimitForAppkeyAndApi）为网关级拒绝、请求未被服务端处理，故可重试且不受 idempotent 门禁约束；stage-0 含整点峰值规避（整点±10s 内等待至整点+10s）；call() 全局 pacing 20 req/s。
8. subagent 并发调度一律经 `shared/waves.py` 规划：agent 数硬上限 `MAX_AGENTS=20`，
   只能下调不能上调（环境变量可压小、代码内 `min(cap, MAX_AGENTS)` 硬顶），
   批大小 batch=ceil(条数/agent数) 自动负载均衡，一次性并发发出，不分波、不串行。

## 验证命令

```bash
python3 -m unittest discover -s tests          # 本地无副作用（含 mock HTTP 传输测试）
python3 -m py_compile shared/*.py skills/*/scripts/*.py
python3 skills/resume-intake/scripts/upload_resumes.py <目录> --dry-run   # 只解析不触网写表
python3 shared/query.py resume --fields name,phone  # 只读，需真实凭证
```

真实端到端：对 data 目录跑 upload_jobs → upload_resumes → query 核对计数。

## 文档索引

| 场景 | 文档 |
|---|---|
| 简历入库（含 OCR 补录，上传后自动衔接简历AI精析） | `skills/resume-intake/SKILL.md` |
| 岗位 JD 入库 + JD 语义回写（硬性门槛/必备技能/加分项） | `skills/job-intake/SKILL.md` |
| 查询与统计 | `skills/candidate-query/SKILL.md` |
| 匹配打分与语义复核（门槛前置 + 逐岗 AI 分析） | `skills/match-verify/SKILL.md` |
| AI 三列精析（技能/AI结构化提取/AI深度解析） | `skills/skills-analyze/SKILL.md` |
| 招聘看板 | `skills/recruit-dashboard/SKILL.md` |
| 跨组织复制表结构 | `skills/replicate/SKILL.md` |
| 表结构/口径/打分规则知识库 | `skills/recruit-model/SKILL.md` |
| 表结构与字段口径（速查） | `README.md` |

## 犯错记录（历史教训，勿重犯）

- 曾把 secret 硬编码进脚本 → 现在凭证只在 `.secrets.json`/环境变量。
- 曾用字段 ID 做映射导致 config 膨胀 → OpenAPI 用中文字段名即可。
- 扫描件 PDF 的 pdftotext 输出是乱码但非空 → 用"抽不出手机号/邮箱"判扫描件，不用文本长度。
- 解析器返回 list 而表字段是 text（certificates）→ 由入口脚本 join，_cast 不做 str(list)。
- mock HTTP 测试：handler 必须先读 Content-Length body，否则连接 RST；测试模块别漏 import。
- 曾在 OpenAPI 重写时连带删掉 shared/preflight.* → preflight 是 stage 0 强制门禁，重写业务脚本时必须同步迁移，不得丢弃。（现位于 shared/preflight/ 目录）
