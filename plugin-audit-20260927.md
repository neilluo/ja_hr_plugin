# recruit-match-suite-fast 全盘审计（2026-09-27）

> 只读审计，未改任何代码。证据均带 file:line 或 commit 号。
> 功能定义（用户口径）：上传简历、上传岗位、岗位从简历库找人。

## 0. 结论先行

- **骨架质量高，肉质质量低，胶水层缺失。** 传输层/幂等/查重回读/SSOT 纪律是 A 级；但三条精析流水线是 80% 重复的复制品、三列回写有两份实现、9/21 入口零测试、8 条静默降级路径、match 表有两条删除口径不对称的写路径。
- **"割裂感"的真正来源：编排胶水在散文里，不在代码里。** 全仓没有一个端到端入口；"upload→prepare→起 subagent→merge→apply→verify"的衔接知识全部写在 SKILL.md 散文里，由 agent 当调度器。每个脚本是孤岛，岛与岛靠文档连。
- 综合评级 **B-**（上次评审 B+ 看的是传输层与入库侧；这次深挖匹配侧与工程度量，降级）。

## 1. 功能 → 实现映射

| 用户功能 | 实现链 | 编排者 |
|---|---|---|
| 上传简历 | upload_resumes.py（解析/查重/附件/回读）→ needs_ocr 补录 → skills-analyze 三列精析 | SKILL.md 散文 + agent |
| 上传岗位 | upload_jobs.py（正则粗提取）→ jobs_analyze 精析 → sync_job_columns 回写 → check_skill_coverage 自检 | 同上 |
| 岗位匹配简历 | match_gated（机械门槛+打分）→ match_analyze（逐岗 subagent 复核）→ stats → dashboard/query | 同上 |
| （支撑）部署 | replicate_base / sync_schema | 同上 |

## 2. 简历侧：每个字段怎么来的（全部本地脚本，零钉钉原生解析）

文本层 `shared/extract.py`：pdftotext / textutil / docx 解包 XML，零第三方依赖。
字段层 `skills/resume-intake/scripts/parse_resume.py`：逐字段正则。

| 列 | 方法 | 证据 | 失败模式 |
|---|---|---|---|
| name | 标签正则→文件名拆段→前6行独立汉字，过黑名单 | parse_resume.py:24-25,118-146 | 5 字以上姓名被截 4 字 |
| phone | `(?<!\d)(1[3-9]\d{9})(?!\d)` | :21-22,131-134 | `+86` 连写被 lookbehind 拒 |
| email | 通用正则取**全文第一个** | :23,135 | 可能是猎头/公司邮箱 |
| education | 标签→全文首命中→归一；无学历词按校名后缀猜 | :39-40,80-87,154-165 | 独立学院一律判本科 |
| school | 标签或"xx大学/学院"正则+噪声清洗 | :41-44,109-115 | 境外校/无后缀校名取不到 |
| school_rank | **硬编码 985/211 各 20 所**子串匹配 | :55-60,90-98 | 名单外真 985 静默降"普通本科"；config"双一流"选项永不可达 |
| major | 标签+前缀黑名单 | :45-46,166-168 | 标签尾巴吃进（实测方红亮 major="qZ" 水印碎片） |
| years_experience | 四分支巨型正则→松正则→文件名兜底 | :26-28,147-151 | 实测 50% 抽不出（docx 表格型简历） |
| expected_position | (期望\|意向\|应聘…)[:：] 后 2-25 字 | :53,170-171 | 实测 64%；尾巴吃进（"…主管 期望工资"） |
| expected_salary | 薪资标签+区间/面议 | :29,152 | "30万/年""13薪"不匹配 |
| expected_location | 12 字内命中 config 城市枚举 | :31-38,153 | **实测真实命中 0/28，全员静默兜底"不限"** |
| certificates | 固定证书名白名单 findall | :54,169 | 实测 21%；白名单外全漏 |
| skills | SKILL_WORDS 逐词子串包含，前 15 | :172 | 实测与精修重合度 13%，35% 泛词 |
| full_text | ex["text"][:20000] | upload_resumes.py:34,150 | 截断仅伤 2/28 |
| attachment | uploadInfos→OSS PUT→cell，失败则该条不写表 | notable.py:343-355；upload_resumes.py:81-90 | 真闭环 |
| attach_md5 / upload_time / comm_status | 本地 md5 / now / 常量"待筛选" | upload_resumes.py:40-42,151-153 | — |
| org | **全链路无人赋值** | upload_resumes.py 全文无 | 半死列 |

扫描件：图片扩展名或抽不出手机+邮箱 → needs_ocr → **千问办公视觉读图** → --backfill（唯一非脚本环节，且不是钉钉 OCR）。

## 3. 匹配侧：双写路径与消费图

### 3.1 match 表两条写路径

| 维度 | match_gated --commit | match_analyze apply |
|---|---|---|
| recommend | 机械重算：≥80 推荐/≥60 待定（match_gated.py:31-32,106） | subagent 给值，兜底"不推荐"（match_analyze.py:133） |
| 删旧口径 | 按 (name,job_id) 对（:168-170） | **按 job_id 整岗删，不区分 source，会删人工记录**（:111-112） |
| ai_analysis | 不写 | 写（:135） |
| org | 取 jf.org（:180） | **笔误 bug：`jf.get("department") and cf.get("org")`（:124）** |
| 行覆盖 | 全部 gate_pairs（含未审专业） | 仅 keep=true |

SKILL.md:55-56 明说二者不要混跑——但没有任何代码强制。

### 3.2 消费图

- job_id+recommend → stats → job.stat_* → dashboard 漏斗
- recommend+total_score+evidence → dashboard Top 榜
- **ai_analysis：零机器消费方**（全仓仅 match_analyze.py:135 写）
- **ai_deep：零机器消费方**；ai_extract 唯一机器消费 = skills_apply.py:58 年限回填
- cand_skills：零消费方

### 3.3 门槛空转（最危险的静默失败）

- job.must_skills 空 → toks 空 → score 的 `if must else 0`（match_gated.py:103-104）→ tot=0 → `tot < min_score` 全剔（:139）→ **0 配对、0 报错**，报告只写"达标配对:0"
- hard_gates 空 → gate() 全放行（:48-94）
- 即：岗位链没跑时匹配链**静默空转**，与 AGENTS.md 记载的 link 列静默失败同型

## 4. 跨切面度量

- 体量：代码 2,791 行（21 py）/ 测试 2,624 行 / skills 文档 950 行 ≈ 1 : 0.94 : 0.34
- **零测试入口 9/21**：skills_analyze、skills_apply、sync_ai_columns、jobs_analyze、sync_job_columns、check_skill_coverage、sync_schema、replicate_base、query（test_match_analyze 仅测常量与子命令白名单）
- 三流水线 prepare/merge 骨架 ~80% 相同（skills_analyze.py:80-101 / jobs_analyze.py:64-87 / match_analyze.py:81-99），差异仅去重 key 与输出结构；merge 的 nt 参数三者均未使用
- **三列回写两份实现**：sync_ai_columns.py:24 vs skills_apply.py:18 近似复制 → 违反自家不变量 10
- 静默降级 8 条：job_vocab 空（skills_analyze.py:27-33，实测 job_vocab.json=2 字节）、must 空 0 配对、skills 空漏配、扫描件空全文出 []、**重名候选人互相覆盖（cands 以 name 为 key，match_gated.py:126 / match_analyze.py:44）**、pair 对不上静默丢（match_analyze.py:51-53）、merge 缺批仍出部分结果、skills 非法词静默剔除（skills_apply.py:69-72）
- outputs/ 35 文件 304K：gate_pairs/gate_pending 是链路状态（删了匹配链断），其余多为过程垃圾
- config 与真表类型漂移 5 处（phone/email/responsibilities/requirements/match.phone）

## 5. 割裂的五个结构性来源

1. **编排胶水在散文里**：无端到端入口，SKILL.md 是唯一调度器说明书（旧 run_pipeline.py 已在 OpenAPI 重写时删除）
2. **复制式流水线**：三套 prepare/merge + 两套三列回写
3. **测试偏科**：传输层/入库侧有 mock e2e 与崩溃注入，精析链与岗位链回写零覆盖
4. **降级不报错**：8 条静默路径，前置缺失时产出"看起来正常"的空结果
5. **半死资产**：org 列、school_rank 名单、ai_deep、ai_analysis、sync_ai_columns、check_skill_coverage（仅文档提及、无强制）

## 6. 质量记分卡

**好的（A 级证据）**
- 非幂等写禁盲重试 + QPS 403 豁免有注释论证（notable.py:123-176,149-157）
- 查重三级（表内预载/批内/写后自愈）+ readback_missing exit 1（upload_resumes.py:52-64,107-110）
- SSOT：阈值/推荐三值/词表/枚举全部单源 + test_single_source 元测试防复活
- 附件先传后写真闭环（upload_resumes.py:81-90）
- config 缺映射 fail-fast（parse_resume.py:37-38,67-69）

**坏的（C 级证据）**
- match_analyze.py:124 org 笔误；:111 整岗删不辨 source
- 重名覆盖（match_gated.py:126）
- 匹配链前置缺失静默空转（match_gated.py:103,139）
- school_rank 硬编码名单 + 死选项（parse_resume.py:55-60）
- 期望地点静默兜底"不限"（parse_resume.py:153，实测 0/28 命中）
- 两份三列回写实现（不变量 10 自违）

## 7. 修复清单（未动，待拍板）

**P0 正确性（静默数据风险）**
1. match_gated stage_gate 加前置门禁：job 表空 / 全岗 must_skills 空 → exit 2 并明示原因（消掉最危险的静默空转）
2. 修 match_analyze.py:124 org 笔误；两条写路径删旧统一加 source="系统匹配" 过滤
3. 候选人 key 从 name 改 record id（消重名覆盖）

**P1 结构**
4. 删 sync_ai_columns.py 或改为 skills_apply 的薄封装（不变量 10）
5. 抽 shared/merge_parts+prepare_parts（~40 行）统一三流水线
6. 9 个零测试入口补冒烟（至少每个子命令签名一致性，历史 TypeError 教训）

**P2 schema/产品**
7. ai_deep / ai_analysis 去留拍板（零消费方）；年限改 subagent 结构化输出，与 ai_extract 解耦
8. 5 处类型漂移对齐（改 config 或 sync_schema，按 SSOT 规矩二选一）
9. school_rank 名单进 config 或删除该列；org 列赋值或删除

## 8. 异步化改造方案（2026-09-27 用户拍板，实施中）

用户拍板要点：① 上传链路不变，三列精析改异步队列；② 岗位链同构改造；③ 精析窗口期内
匹配必须等待（拒绝粗值打分，--force 逃生）；④ ai_deep/ai_analysis 保留（人读列）；
⑤ OCR 手析记录打标记、agent 永不再碰扫描件三列。

### 8.1 队列设计

- 标记列 `ai_refined_at`（config.fields 两表新增，type date 毫秒）：空 = 在队列。
  不靠"三列是否为空"推断（岗位三列入库即有正则粗值，推断必失效）。
- 队列谓词唯一真源 `shared/refine_loop.py`：resume = 标记空且 full_text 非空
  （扫描件不入队，防覆盖 OCR 手写字段）；job = 标记空且 responsibilities 非空。
- 打标记责任方：skills_apply / sync_job_columns（与三列同一次 update）、OCR backfill（手析视同精析）。
- 消费形态：事件驱动（上传报告 refine_queued>0 → 2 分钟后一次性任务）+ 每日兜底巡检；
  每周期先拿 outputs/refine.lock 周期锁，两异构队列各一波 subagent（峰值≤20），
  解析完一条回写一条（apply 本就逐条 update），未做完留下周期续跑。

### 8.2 实施清单（T0-T3）

- T0 正确性：match_gated 前置门禁（队列非空/岗位未精析 → exit 2，--force 逃生）；
  match_analyze org 笔误修复；两写路径删旧加 source="系统匹配" 过滤；候选人 key 改 record id；
  jobs_analyze prepare 判定改标记列（埋掉粗值跳过新岗坑）。
- T1 异步化：config 加 ai_refined_at + sync_schema 补列（同提交）；refine_loop 真源；
  三入口 prepare 改拉队列 + queue 子命令；SKILL 删自动衔接散文；打标记三处。
- T2 结构：sync_ai_columns 改 skills_apply 薄封装；shared/analyze_parts.py 统一三流水线
  prepare/merge 骨架；13 入口 --help 冒烟 + 子命令签名一致性测试。
- T3 卫生：5 处类型漂移对齐——**实测字段 API 不支持改类型（PUT 静默忽略）**，
  且 text 列收 {"markdown":...} 会存成 JSON 字符串字面量（富文本包装是污染源），
  故对齐方向 = config 向真表看齐（phone/email/responsibilities/requirements/perm.user 全 text），
  删 _cast richText 死分支与 txt() dict 分支；school_rank 名单进 config.refs；
  resume.org 定性人工/预留列（文档说明，不删列）。

### 8.3 验收定义（A 级）

8+2 条静默路径全部报错或显式标注；重复实现清零（grep + 元测试）；入口测试全覆盖；
真表 e2e 全链（upload_jobs → upload_resumes → 精析周期 ×2 → match_gated → match_analyze → stats）
绿 + unittest 全绿；逐阶段耗时记录，长耗时点修复后清表重跑复测。

## 9. 两轮真表 e2e 耗时对比（2026-09-27，清表重跑）

| 阶段 | 轮1 | 轮2 | 说明 |
|---|---|---|---|
| upload_jobs（19 JD） | 4s | 5s | 附件并行 5 并发，含回读 |
| upload_resumes（31 份） | 6s | 6s | 28 入库 + 3 扫描件入 needs_ocr |
| OCR backfill（3 份） | 4s | 3s | 同批打 ai_refined_at |
| wave1 岗位精析（19 agent） | 43.8s | 89.0s | 波次跨度受后端 stall 窗口扰动 |
| wave2 简历精析（14 agent） | 215.9s | 50.4s | 同上，stall 命中与否决定跨度 |
| 门禁+commit | 8s | 21s(含回写) | 队列非空时 exit 2 已验证（轮1） |
| wave3 匹配分析 | 1023.7s（长尾 part8=12 对） | **636.1s**（最大块 8 对） | _cap_blocks 生效：长尾 -38% |
| merge/apply/stats | 10s | 8s | — |

- 机器侧总耗时 ≈ 上传 14s + 三波推理（轮2 实测 775s）+ 收尾 <1min；
  **唯一不可压缩项 = subagent 波次推理**，其中 stall 窗口为平台行为（代码不可修），
  负载不均已用 MAX_PAIRS_PER_AGENT=8 修复（轮1 part8 单块 12 对 → 轮2 最大 8 对）。
- 轮1 发现并修复：年限回填正则不容「约N年」致 4 份简历年限静默漏回填（正则放宽 + prompt 禁修饰词，已补回填 4/4/3/20 年）。
- 轮2 业务观察（非 bug）：keep=48 中 0 条「推荐」（42 不推荐 + 6 待定），
  与 check_skill_coverage exit 2 同源——简历池无安全/EHS 背景候选人，安全两岗 must 命中 3/10；
  分数普遍 <80 是数据事实，需业务侧补简历池或调岗。
- 后台周期：事件驱动（上传报告 refine_queued>0）+ 每日 03:00 兜底巡检（cron 2141bf7e，空队列秒退）。
