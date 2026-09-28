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
    - date 列显示格式（钉钉字段 property.formatter）唯一真源 = `config.formats.date`：建表（replicate_base）/
      补列（sync_schema）经 `skills/replicate/scripts/datefmt.py` 派生 property，真表存量列经 datefmt.align 对齐；
      存储值恒为毫秒时间戳（notable._cast），formatter 只管显示；脚本禁止抄 formatter 字面量（元测试防复活）。
    - 入库报告（stdout 首行 VERDICT 结论 + JSON 结果字段前置顺序）唯一真源 = `shared/report.py`
      （print_report/verdict/_KEY_ORDER）：upload_resumes/upload_jobs 一律经它输出，禁止各自
      `print(json.dumps(...))` 抄字段顺序；README/SKILL 只引用"VERDICT 首行 + 结果前置"这一契约、
      禁止复述字段清单顺序（元测试 test_report_contract.py 防退化）。
    - 中文字段名禁止硬编码进脚本：记录接口经 `Notable.cn()/_cells()` 从 config 取，结构接口按中文名运行时解析。
    - 文档（README/recruit-model/SKILL）不得手抄 base_id/table_id/完整字段清单/技能词表，一律指向真源。
    - 例外：写入值字面量（如 source="系统匹配"、status="招聘中"）、解析私有规则（_DEPT_ALIAS 别名、
      分类→关键词映射）、安全白名单（sync_job_columns.KEYS）属单一出现，不算双源，但须注释指向 config 真源。
    - 每次新增枚举/阈值：先落唯一真源，再让消费方派生；同一提交内 grep 确认无第二份副本，unittest 全绿。
11. 精析异步队列三层闭环（触发/出队/并发防护），禁止加第四层：
    - 触发 = 上传后 agent 注册一次性消费任务，**时刻直接取报告字段 `refine_fire_at`**
      （延迟秒数唯一真源 = `shared/refine_loop.REFINE_DELAY_S`，resume/job 两条上传链共用同一触发机制，
      时刻均取各自报告的 `refine_fire_at`；文档与 prompt 只引用字段名、禁止复述数值，
      也不许再跑 `date` 现算；纪律在 resume-intake / job-intake SKILL.md，`refine_queued` 供触发信号；
      注册只能由 agent 做，脚本无本地调度 API）。
    - 出队唯一凭证 = `ai_refined_at` 与三列同一次 update 落表（空即在队，崩溃无中间态要清）。
      队列谓词与它的补集（`unrefinable`：原件已删的扫描件）必须同处 `refine_loop.py` 一源，
      消费方禁止各抄一半。扫描件靠 `source_file`（原件本地绝对路径）入队、由 subagent 读图精析——
      表内附件 url 是 OSS 签名链（约 2 小时过期、无换签接口），不得作为精析依据。
    - 并发防护 = `shared/refine_loop.py` 周期租约：prepare 获取 `outputs/refine_{resume,job}.lock`、
      写回端释放，30 分钟 mtime 判活，撞活租约 refused exit 2（消费方见 refused 即退、禁止抢跑），
      同周期重切批 --force 夺回；锁路径走 OUTDIR 供测试隔离。
    - 每日 09:30 兜底 cron 是**唯一崩溃恢复层**（时刻选白天工作时段，电脑通常在开机状态）；
      看门狗/补跑任务层已裁撤（它既与兜底重叠、又无法凭新鲜度区分"已崩"与"在跑"，判活窗口>看门狗延迟时必然误判），禁止复活。
      消费任务**完成即自删**：payload 末条指令为结束时（含队列为 0 静默结束）按任务名删除本一次性任务自身，
      已消费完的不留列表；兜底 payload 的**自清理**（每次巡检删除已停用且有执行记录的一次性消费任务，
      名称以「简历精析消费」/「岗位JD精析消费」开头）只是对崩溃在自删之前的任务的兜底网；兜底自身是周期任务不在清理范围。
      已接受的边界：消费任务崩溃且 30 分钟内又有新上传时，新任务被僵尸租约拒绝、延至兜底重吃。

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
- 曾把 8.6KB 提示词原文内联进每一个 Agent 工具调用的 prompt 字段（16 批 ≈ 138KB 工具入参）→ 第 13 个调用
  在工具流中途被截断（只剩半段），只能事后补发批次 14-16，被迫分波（见上一条）；实测多花 ~250s 纯串行等待，
  并多暴露一次后端 stall 窗口。教训重申：**"必须一次性发完 M 个"这类靠 agent 自觉的纪律，可靠性再高也扛不住
  载荷本身把工具调用撑爆**——凡能预生成的（per-batch 提示词文件）一律由 prepare 落盘（skills_analyze.render_prompts），
  分派时每批只发一行路径指针（~150 字节），把"内联提示词原文"这条路本身杀死；
  由 test_dispatch_slim.py 锁定"渲染产物所有占位符必须替换干净"防退化。
- 曾见 subagent 报 failed 就立刻补发重跑 → 批次 13 的产物在 failed 前已完整落盘（失败只发生在收尾回合的
  模型流被 TLS 掐断），补发白费一轮还覆盖了好产物；现纪律：failed 先验盘上产物（JSON 可解析、id 集合一致、
  字段齐全即视为成功进 merge），仅产物缺失/不完整才补发。该"验盘"已从人工 bash 回合下沉进 merge
  （skills_analyze.done_integrity 输出 missing_batches/bad_batches/all_complete），省一次往返。
- 曾让 subagent 自写校验 assert 口径（把英文术语按字符数判"2-6 字"超长）→ 多轮试探性 Edit 返工、拉长暴露
  stall 窗口的时间；曾一度改为"提示词内嵌 bash 校验、agent 原样运行"消除了自写口径，
  但每 agent 仍固定多 1 个 Bash 回合、失败要"修正+复验"至多再 2 回合，把 31 条流水线的墙钟抬高 30-60s；
  现 schema 判定唯一真源下沉到 merge 的 `validate_row`（不变量 10：口径单一出现，禁止模板/脚本两处抄），
  违规行 merge 直接剔除、record 不打 `ai_refined_at` → 下周期自动重析（不新增恢复层，复用既有出队凭证），
  agent 回合省到"读输入 → 推理 → 写产物"三步。教训：校验放在离产物最近的地方最省回合，
  让"写者"自检是"多一次工具往返"，让"读者"（merge）在批处理入口统一校验才是"零额外回合"。
- 曾给精析队列加 +15 分钟看门狗补跑任务 → 与 03:00 兜底功能重叠，且判活窗口（30 分钟租约）> 看门狗延迟，
  崩溃场景必然被误判成"还在跑"而静默退出——三层恢复互相矛盾的空转层，已整体裁撤（不变量 11）。
  教训：每加一层恢复/兜底，必须先回答"它覆盖了哪一层覆盖不到的真实场景"，答不出就不加。
- 曾把周期租约路径硬编码 ROOT/outputs → 测试直调 prepare 后把活锁漏进真实目录、30 分钟拒绝后续周期；
  现锁路径走 OUTDIR（与 pending/done 同目录），测试重定向 OUTDIR 即天然隔离。
- 曾把"回合合并纪律"只写进 SKILL.md 就以为优化到位 → 同一份文档明写"耗时大头是 agent 回合往返"，
  我照样空转 6 个回合（TodoWrite 独占 3 轮、预探 PDF 页数 1 轮、重复回读 1 轮）；
  教训：文档约束的可靠性远低于代码保障，凡脚本能直接产出的（table_total、refine_fire_at、stdin payload）
  一律下沉到脚本，别写一条"你必须记得"赌 agent 自觉。
- 曾让扫描件在补录当场手析三列（"手析视同精析、永不再碰"）→ 单项占 51.4s 是整回合最大开销，
  且与后台精析重复劳动；现扫描件凭 source_file 入队、subagent 读图出三列（实测质量不低于手析）。
  改此口径时必须同步处理两个衍生风险：① 原件被删的记录若仍入队会永久卡队列并阻塞 match_gated 门禁
  （故谓词要判"文件真实存在"，坏记录走 unrefinable 报出）；② 旧一代手析记录无 source_file，
  --all 重析会把它们捞进批次、用"未提及"覆盖已有好数据（故入队与 --all/--ids 共用 refinable() 判据）。
- 曾只测到"prepare 带出了 source_file"就宣称读图链路打通 → 能力核心是 subagent 能否真读图出三列，
  必须端到端跑到 apply 写回与出队标记才算验证；现 e2e 用真 PNG 派真 subagent 全链跑通后才落文档。
- 曾把入库报告用 `json.dumps(indent=2)` 直打、结果字段（created/readback_missing）排在 timing_ms 之后 →
  agent 扫 stdout 开头只见 total/needs_ocr 就误判"报告缺 created"，白白多绕 Grep+纠正两回合（实测一次
  上传 162s 里 63s 是这桩空转，占非 OCR 成本的大头）。教训重申：文档约束赌 agent"读全"不可靠，
  现由 `shared/report.py` 先把 VERDICT 结论行打到首行、再把结果字段前置，把"读全才懂成败"变成"一眼可见"。
