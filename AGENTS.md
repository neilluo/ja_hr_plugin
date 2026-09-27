# AGENTS.md — 招聘智能匹配（OpenAPI 直连版）工程约束

> 代码是唯一事实来源。CLI 参数以 `python3 <入口脚本> --help` 为准。
> 旧 emit/replay + agent 调 dws 的链路已整体废弃并删除，禁止参考、禁止复活。

## 架构

- Python 经钉钉 Notable OpenAPI **直连** AI 表格（`shared/notable.py` 是唯一传输层）。
  不使用 dws、不经过 agent 中转、没有状态机/checkpoint 文件。
- 凭证：`.secrets.json`（已 gitignore）或环境变量 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET`。
  仓库是 public 的，**任何凭证不得写入会被提交的文件**。
- **表结构唯一事实源是 `config.json`**（fields 业务键→中文名、types、options 单选/多选选项清单；
  字段书写顺序 = 建表顺序）。建表（replicate_base.py）与补列（sync_schema.py）一律从 config 派生，
  仓内不得维护第二份字段清单。新增/改字段 = 改 config（fields/types/options）+ 跑 `sync_schema.py`
  对目标 Base 补列，两步同一提交；脚本禁止运行时回写 config.json，config 缺映射即报错人工修。
  字段 ID 是 Base 私有的：记录接口用中文名，结构接口（扩选项）按中文名运行时解析 ID，
  禁止硬编码 ID 或把 ID 写进 config。
  AI 语义分析三列为 schema 净增量（与组织无关）：resume 表 `ai_extract`→AI结构化提取、
  `ai_deep`→AI深度解析；match 表 `ai_analysis`→AI匹配分析，类型均为 text。
  README/recruit-model 中的字段描述仅作文档，以 config 为准。
- 入口脚本：跨 skill 公共入口 `shared/query.py`；单 skill 私有入口
  `skills/resume-intake/scripts/upload_resumes.py`、
  `skills/job-intake/scripts/upload_jobs.py`、`skills/job-intake/scripts/jobs_analyze.py`（JD 精析 prepare/merge）、
  `skills/job-intake/scripts/sync_job_columns.py`（回写硬性门槛/必备技能/加分项）、
  `skills/job-intake/scripts/check_skill_coverage.py`（岗位技能词表 vs 简历标签命中率自检，低覆盖 exit 2）、
  `skills/match-verify/scripts/match_gated.py`（门槛前置匹配：机械门槛一票否决 + 语义打分）、
  `skills/match-verify/scripts/match_analyze.py`（逐岗 subagent 并发 AI 匹配分析 prepare/merge/apply/stats）、
  `skills/skills-analyze/scripts/skills_analyze.py`（简历三列精析 prepare/merge）、
  `skills/skills-analyze/scripts/skills_apply.py`（写回三列）、
  `skills/skills-analyze/scripts/sync_ai_columns.py`（手工修正薄通道，实现委托 skills_apply）、
  `skills/replicate/scripts/replicate_base.py`（建表，从 config 派生）、
  `skills/replicate/scripts/sync_schema.py`（结构自检/补列：`--check` 只读报 config 与真实 Base 漂移，
  缺列 exit 2；默认模式补建缺失列，只补不删不改）。
  agent 直接 Bash 跑脚本，读 JSON 报告即可，不需要中间回合。
- 跨 skill 公共库 `shared/waves.py`：subagent 并发调度规划。agent 数硬上限 20（`MAX_AGENTS`
  只能下调不能上调），自动负载均衡 batch=ceil(条数/20)，一次性并发发出、不分波不串行；
  被 skills-analyze / job-intake / match-verify 三方共用。
- match-verify 私有库 `skills/match-verify/scripts/semantic_score.py`：语义词典 + 同义命中判定，
  只服务该 skill，禁止提升到 shared/。
- 命令一律以仓库根为 CWD 执行：跨 skill 公共入口 python3 shared/query.py、bash shared/preflight/preflight.sh（Windows 用 shared/preflight/preflight.ps1）；预检三件套（py/sh/ps1）同居 shared/preflight/；单 skill 私有入口 python3 skills/<skill>/scripts/<entry>.py；shared/ 只放跨 skill 公共库与公共入口，skills/<skill>/scripts/ 只放该 skill 私有入口，scripts/ 下入口为执行而非阅读。
- 代码归属按共享范围：**只被单个 skill 引用的代码（入口或库）一律放该 skill 的 scripts/，禁止放 shared/**；
  shared/ 只收跨 skill 公共物——公共库（notable/extract/waves/vocab）、公共入口（query.py）、预检三件套（preflight/）。
  `shared/vocab.py` 是技能词表（SKILL_WORDS）与分词（SEP/toks）的唯一源，parse_job/parse_resume/check_skill_coverage/
  skills_analyze/semantic_score 五方共用，禁止在任一脚本再抄一份词表或分词正则。
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
   写出侧一律按 config.types 经 `Notable._cast` 转换（text 直传字符串）。
   真表无 richText/telephone/email/user 类型列（字段 API 不支持改类型，PUT 静默忽略），
   config.types 已对齐真表实况；若发现 config 与真表类型漂移，改 config 不改表。
7. QPS 403（QpsLimitForApi/QpsLimitForAppkeyAndApi）为网关级拒绝、请求未被服务端处理，故可重试且不受 idempotent 门禁约束；stage-0 含整点峰值规避（整点±10s 内等待至整点+10s）；call() 全局 pacing 20 req/s。
8. subagent 并发调度一律经 `shared/waves.py` 规划：agent 数硬上限 `MAX_AGENTS=20`，
   只能下调不能上调（环境变量可压小、代码内 `min(cap, MAX_AGENTS)` 硬顶），
   批大小 batch=ceil(条数/agent数) 自动负载均衡，一次性并发发出，不分波、不串行。
9. 死代码零容忍：每次改动必须在同一提交内删除因此不再被使用的逻辑、函数、入口与孤儿 import，
   禁止留僵尸代码；删除后全仓 grep 确认被删名字零引用，且 unittest 全绿。
10. 双源零容忍：任何枚举/选项/词表/阈值/字段名/表ID 只允许存在于唯一真源，禁止第二份副本。
    - 表结构（fields/types/options）唯一真源 = `config.json`；解析器与文档一律运行时从 config 派生，
      禁止在 .py 里硬编码部门/org/城市/学历/状态/分类等枚举副本（parse_job/parse_resume 的枚举正则、
      match_gated/match_analyze 的推荐位次均由 config.options 动态生成）。
    - 技能词表与分词唯一真源 = `shared/vocab.py`（SKILL_WORDS/SEP/toks）；语义同义词典唯一真源 =
      `semantic_score.py`（SYNONYM/HYPERS）；简历支持扩展名唯一真源 = `shared/extract.py`（SUPPORTED_EXTS）。
    - 匹配阈值/MIN_SCORE 等标量提为**唯一具名常量**（match_gated.REC_MIN/PEND_MIN），文档与 prompt 只引用不复述。
    - 中文字段名禁止硬编码进脚本：记录接口经 `Notable.cn()/_cells()` 从 config 取，结构接口按中文名运行时解析。
    - 文档（README/recruit-model/SKILL）不得手抄 base_id/table_id/完整字段清单/技能词表，一律指向真源。
    - 例外：写入值字面量（如 source="系统匹配"、status="招聘中"）、解析私有规则（_DEPT_ALIAS 别名、
      分类→关键词映射）、安全白名单（sync_job_columns.KEYS）属单一出现，不算双源，但须注释指向 config 真源。
    - 每次新增枚举/阈值：先落唯一真源，再让消费方派生；同一提交内 grep 确认无第二份副本，unittest 全绿。

## 验证命令

```bash
python3 -m unittest discover -s tests          # 本地无副作用（含 mock HTTP 传输测试）；
                                               # 崩溃注入用例需真实数据目录，未设则 skip：
JA_TEST_DATA_DIR=<岗位说明书目录> JA_TEST_RESUME_DIR=<简历目录> \
  python3 -m unittest discover -s tests        # 带夹具：崩溃/回填 e2e 全跑（无 skip）
python3 -m py_compile shared/*.py skills/*/scripts/*.py
python3 skills/resume-intake/scripts/upload_resumes.py <目录> --dry-run   # 只解析不触网写表
python3 shared/query.py resume --fields name,phone  # 只读，需真实凭证
python3 skills/replicate/scripts/sync_schema.py --check  # 只读：报 config 与真实 Base 漂移，缺列 exit 2
```

真实端到端：对 data 目录跑 upload_jobs → upload_resumes → query 核对计数。

## 文档索引

| 场景 | 文档 |
|---|---|
| 简历入库（含 OCR 补录；精析为异步队列，由后台周期消费） | `skills/resume-intake/SKILL.md` |
| 岗位 JD 入库 + JD 语义回写（硬性门槛/必备技能/加分项） | `skills/job-intake/SKILL.md` |
| 查询与统计 | `skills/candidate-query/SKILL.md` |
| 匹配打分（门槛前置 match_gated + 逐岗 subagent AI 分析 match_analyze） | `skills/match-verify/SKILL.md` |
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
- 曾双份维护表结构（config.json + replicate 本地 SCHEMA「手工对齐」）→ AI 三列漏建、richText/user 类型漂移；现 config 为唯一真源，建表/补列一律派生。
- 曾把字段 ID（ldMkQqp）硬编码进脚本 → 换 Base 即失效；现按中文名运行时解析 ID，ID 永不进代码与 config。
- 曾让脚本运行时回写 config.json（自动补缺映射）→ 真源失控；现 config 缺映射即报错，人工修。
- 曾把"关联岗位"link 列名硬编码进 match_gated/match_analyze，但真表与 config 都无此列 → 每次 PUT 静默写失败、
  link 功能从未生效却无人察觉（e2e created 计数正常掩盖了它）；现 link 死功能整体删除。教训：写任何列前，
  该列必须先在 config.fields 声明、真表 sync_schema --check 通过；硬编码字段名 = 绕过 SSOT = 静默失败温床。
- 曾让枚举/词表/阈值散落多份（parse_job 部门 vs config.options、技能词表 job/resume 各一份、阈值 match.py 70/40
  与 match_gated 80/60 并存、扩展名 upload/preflight/extract 三套）→ 部门漂移 18 vs 16、org 多两个中心、
  扩展名漏 .webp/.tif；现全部收敛唯一真源（不变量 10），并加 test_single_source.py 元测试防副本复活。
- 曾让 test_crash_job.py 以脚本式裸跑（无 TestCase 类）→ unittest discover 收集不到、12 个岗位崩溃场景从未进回归，
  还依赖本机绝对路径 DATA_DIR 换机即崩；现 TestCase 化 + 数据目录走环境变量（缺失优雅 skip），discover 才真跑它。
- 曾在 match 同一 skill 里并存两条完整矛盾链路（旧 match.py 两步走 + 新 match_gated 门槛前置），SKILL.md 双份描述、
  阈值口径打架；旧链路被架空却没删 = 死代码；现统一到 match_gated+match_analyze 单链路，删旧入口同步删文档。
- 曾把 merge(nt) 与 prepare(nt,args) 签名不一致却用 `(prepare if ... else merge)(nt, argv[2:])` 统一调用 →
  jobs_analyze.py merge 子命令必崩 TypeError；入口分派的多分支签名必须一致，且要有冒烟测试覆盖每个子命令。
- 曾把 16 个精析 subagent 分成 8+8 两波发（SKILL 明写"同一条消息一次性发完、不分波"）→ 每多一波多暴露一次
  后端 stall 窗口（实测两波各撞一次 3-6 分钟全局冻结）且多花一轮组装时间；并发 subagent 必须一波发完。
- 曾见 subagent 报 failed 就立刻补发重跑 → 批次 13 的产物在 failed 前已完整落盘（失败只发生在收尾回合的
  模型流被 TLS 掐断），补发白费一轮还覆盖了好产物；现纪律：failed 先验盘上产物（JSON 可解析、id 集合一致、
  字段齐全即视为成功进 merge），仅产物缺失/不完整才补发。
- 曾让 subagent 自写校验 assert 口径（把英文术语按字符数判"2-6 字"超长）→ 多轮试探性 Edit 返工、拉长暴露
  stall 窗口的时间；现校验脚本固化在 references/subagent-prompt.md（字数只数中文字符），agent 只许原样运行。
