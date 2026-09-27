# recruit-match-suite-fast 插件质量评审报告

评审日期：2026-09-26 · 评审方式：3 个 subagent 并行全文深读 + 主控亲验关键指控 + 本地全量测试
仓库：/Users/neil/Desktop/qwenworklearn/jahrplugin/recruit-match-suite-fast（main @ 9e2099a）

---

## 一、这个插件是做什么的

面向晶澳（JA Solar）HR 场景的**智能招聘套件**：把简历/岗位文件批量入库到钉钉 AI 表格（Notable），再做 AI 语义精析与人岗智能匹配。核心特点是 **Python 直连钉钉 Notable OpenAPI**（不经 dws、不经 agent 中转），`shared/notable.py` 是唯一传输层；表结构唯一事实源是 `config.json`；agent 只在两类环节介入——扫描件视觉补录（OCR）和 subagent 并发语义分析（JD 精析、简历三列精析、逐岗匹配分析），并发调度统一经 `shared/waves.py`（硬上限 20 agent）。

8 个 skill：resume-intake（简历入库）、job-intake（岗位入库+JD 语义回写）、candidate-query（查询统计）、match-verify（门槛前置匹配+AI 分析）、skills-analyze（简历三列精析）、recruit-dashboard（看板，纯文档）、recruit-model（口径知识库，纯文档）、replicate（跨组织复制表结构）。

## 二、完整链路

```
0. 建表    replicate_base.py（从 config.json 派生 4 表 59 字段）
           sync_schema.py --check（漂移自检，缺列 exit 2）/ 默认补列
1. 岗位    upload_jobs.py：文件名拆部门 → job_id=md5(部门|岗位名) 查重
           → 附件先传后写 → 回读（missing 即 exit 1）
           JD 精析：jobs_analyze.py prepare → ≤20 subagent 并发
           → merge → sync_job_columns.py 回写硬性门槛/必备技能/加分项
           → check_skill_coverage.py 词表命中率自检（低覆盖 exit 2）
2. 简历    upload_resumes.py：并行文本提取（pdftotext/pypdf/textutil/olefile，
           全 vendor 零 pip 依赖）→ 附件MD5→手机号两级查重（含批内）
           → 附件先传后写 → 写后查重自愈 → 回读
           扫描件（抽不出手机号/邮箱）→ needs_ocr 队列不入库
           → agent 视觉读取 → --backfill JSON 走同一尾部流程补录
3. 精析    skills_analyze.py prepare（挑三列有缺记录+岗位词表同源）
           → ≤20 subagent 产出 技能/AI结构化提取/AI深度解析 → merge
           → skills_apply.py 写回（自动扩 multipleSelect 选项、剔词重试）
4. 匹配    match_gated.py：机械门槛（组织/学历/年限/证书/年龄，语义词典命中）
           → gate_pairs.json → match_analyze.py prepare（一岗一 agent）
           → subagent 判定 keep/打分/evidence/ai_analysis → merge
           → apply（只建 keep=true，先删旧配对幂等）→ link → stats
           sync_match_analysis.py 把 AI 匹配分析回写 match 表
5. 查询    shared/query.py（四表只读 + match 聚合统计）
```

每条写入路径都遵守 AGENTS.md 四大不变量：查重先行、附件先传后写、写完必回读、扫描件不入库。传输层重试语义按「服务端是否已提交」精细分层：create 传 idempotent=False 不盲重试（防双写）、连接层故障写请求一律不重试、QPS 403 属网关拒绝可重试且豁免幂等门禁（独立预算 5 次）、401 只首刷一次、全局 pacing 20 req/s、整点峰值规避。

## 三、验证结果（本地实跑）

| 验证项 | 结果 |
|---|---|
| `python3 -m unittest discover -s tests` | **57/57 OK**（67.6s，含 mock 故障注入） |
| `python3 -m py_compile`（全部自有脚本） | **通过** |
| 凭证卫生 | `.secrets.json`、`.dingtalk_token_cache.json` 均被 .gitignore 覆盖，`git status` 无凭证被跟踪 |
| AGENTS.md 不变量落地 | 查重/先传后写/回读/needs_ocr 均有代码证据（upload_resumes.py:134-157、:80-88、:105-108、:137-142） |

## 四、质量评价

### 总评：B+（架构与传输层是 A 级，分析侧和文档一致性拖了后腿）

### 亮点（真材实料，不是文档自夸）

1. **重试语义分层严谨**（notable.py:123-176）：区分「网关拒绝可重试」「连接层失败不可盲重试」「POST 非幂等禁重试」，注释即设计文档；配套 test_crash_resume_mock.py 做 R1-R9 故障注入，覆盖「提交后响应丢失」这一最微妙的分布式语义。
2. **写后查重自愈**（upload_resumes.py:50-62、upload_jobs.py:102-112）：兜住非幂等 create 在重试窗口可能产生的双写。
3. **SSOT 纪律执行到位**：replicate_base.py 确从 config.json 的 fields/types/options 派生建表，无第二份 schema；sync_schema --check 按中文名 diff、缺列 exit 2；全仓无字段 ID 硬编码（ldMkQqp 教训已根治）。
4. **零依赖工程**：pypdf/olefile/typing_extensions 全 vendor，预检三件套（py/sh/ps1）+ 脚本内嵌 stage 0 门禁，失败输出机器可读 PREFLIGHT JSON。
5. **测试占比高**：约 1900 行测试 / 3100 行业务代码，且测的是故障模式而非 happy path。

### 问题（按严重度排序，均经主控亲验）

**P0 — 功能性 bug（1 个）**

- **`jobs_analyze.py:96` merge 子命令必崩**：`merge(nt)` 只收 1 参，调用处按 `(prepare if … else merge)(nt, sys.argv[2:])` 传 2 参 → TypeError。JD 精析的 merge 环节当前不可用（prepare 正常）。测试没盖到 job-intake 任何脚本。

**P1 — 违反自家 AGENTS.md「死代码零容忍」条文（4 处）**

- `notable.py:123-133` `call(raw=...)` 裸请求分支全仓零调用（OSS PUT 实际走独立 `put()`，notable.py:178）。
- `match.py`（147 行）被 match_gated+match_analyze 主链路架空，但 SKILL.md 的 description 和前半段命令示例仍以它为主入口——**一个 skill 文档里并存两条互相矛盾的匹配链路**。
- `jobs_analyze.py:20` DEFAULT_BATCH 定义未用；`skills_analyze.py:40-42` `it=iter(args); for a in args: pass` 空转死代码；`upload_resumes.py:129-131` 空 pass 分支。

**P1 — 数据进代码（重蹈 AGENTS.md 犯错记录覆辙）**

- `sync_match_analysis.py:36-51`：14 条特定候选人（石昊/徐志伟…）的分析文本 + 本 Base 专属 job_id 硬编码成 DETAIL 字典。换 Base 即失效，且与 match_analyze 产出的 ai_analysis 职责重叠。

**P2 — 约定与实现不符**

- `waves.py:14` `MAX_AGENTS=int(os.environ.get(...))`：环境变量设 >20 时封顶失效，违反 AGENTS.md 不变量 8「只能下调不能上调」（应 `min(env, 20)`）。
- `jobs_analyze.py:2/17`、`match_analyze.py:2/21` 注释写「上限 7/封顶 8」，与实际 20 矛盾（历史注释未清）。
- `recruit-model/references/ai-analysis-spec.md` 只描述旧 match.py 口径（阈值 70/40、min-score 1），与主链路 match_gated（80/60、MIN_SCORE=20）冲突；system-config.md match 字段清单缺 ai_analysis；execution-notes.md 称「15 用例」实为 57。
- `match_gated.py` 缺 stage 0 preflight（旧 match.py:68 反而有）——违反「前置检查嵌入业务脚本 main()」的自家工程原则。
- preflight.sh 与 preflight.py 行为不对齐（sh 版缺 files_dir 检查与整点峰值规避）。

**P2 — 正确性/健壮性隐患**

- 查重自愈「保留最早」按远端 list 返回顺序（upload_resumes.py:59、upload_jobs.py:109），钉钉不保证返回顺序时可能误删新记录（低概率，但值得按 createdTime 排序）。
- `semantic_score.hit()` 子串包含过宽（semantic_score.py:99），短词如「设备」会假命中。
- `notable.py:328` date 转换用 `time.mktime` 依赖本机时区，跨时区写入有日期偏移风险。
- `parse_job.py:6-16` 硬编码部门枚举/城市（曲靖等晶澳专名），与 config options 形成双源——replicate 到其他组织会漏。
- `test_crash_job.py`（691 行、12 场景）**无 TestCase 类，unittest discover 根本不执行它**，且依赖本机绝对路径；`test_crash_resume_mock.py:34` 依赖 /tmp/ocr.json 外部夹具。「57 全绿」实际不含岗位崩溃场景。

**P3 — 小项**

- `sync_match_analysis.py:109` `cands.get(...) or jobs and {} or {}` 运算符优先级混乱表达式。
- notable.py `_type/_biz` 每记录每字段线性反查 config，量大 O(n·m²)，可预建反向索引。
- config.json 含 base_id/operator_id/table_id 组织私有标识，提交在 public 仓（非凭证但环境耦合，replicate 流程可自洽，属可接受取舍）。

## 五、修复优先级建议

1. **立即**：修 jobs_analyze.py:96 merge 签名（1 行）；给 job-intake 补一条 merge 单测。
2. **本周期**：删 call(raw) 死分支、DEFAULT_BATCH、空 pass 分支；裁决 match.py 去留（建议删除并同步改 SKILL.md/recruit-model 为单链路 80/60 口径）；DETAIL 硬编码迁出到数据文件或改由 match_analyze 产物驱动；test_crash_job.py 补 TestCase 化或明确标注为手工脚本；waves.py 改 `min(env,20)`。
3. **择机**：match_gated 补 preflight；文档三处漂移修正；hit() 短词防假命中；自愈排序改按 createdTime。
