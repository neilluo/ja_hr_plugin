# 招聘智能匹配（OpenAPI 直连版）

基于钉钉 AI 表格（Notable）OpenAPI 的 HR 招聘套件。Python 直连云端，无 dws 中转、
无状态机：解析 → 查重 → 附件上传 → 批量写表 → 回读校验，一条命令完成。

## 快速开始

```bash
# 0. 凭证（二选一，仓库 public 切勿提交）
echo '{"app_key":"...","app_secret":"..."}' > .secrets.json
# 或 export DINGTALK_APP_KEY=... DINGTALK_APP_SECRET=...

# 1. 岗位 JD 入库
python3 skills/job-intake/scripts/upload_jobs.py /path/to/岗位说明书

# 2. 简历入库（含附件上传）
python3 skills/resume-intake/scripts/upload_resumes.py /path/to/AI简历

# 3. 查询
python3 shared/query.py resume --fields name,phone,education
python3 shared/query.py match --stats

# 4. 智能匹配（唯一主链路：机械门槛 → 逐岗 subagent 语义判定 → 落库 → 统计）
python3 skills/match-verify/scripts/match_gated.py                    # 门槛判定 → outputs/gate_pairs.json
python3 skills/match-verify/scripts/match_analyze.py prepare          # 一岗一批切批（agent ≤ 20）
#   → 按 outputs/match_analyze_meta.json 一次性并发发 subagent（提示词见 match-verify/references）
python3 skills/match-verify/scripts/match_analyze.py merge            # 合并判定 → outputs/match_final.json
python3 skills/match-verify/scripts/match_analyze.py apply            # 只建 keep=true 配对（幂等：先删该岗位旧配对）
python3 skills/match-verify/scripts/match_analyze.py stats            # 另起一次读取，刷新岗位四项统计
#   兜底：match_gated.py --commit（无 subagent 直连打分落库，与 apply 不要混跑）

# 5. 简历 AI 三列精析（技能标签 / AI结构化提取 / AI深度解析）
python3 skills/skills-analyze/scripts/skills_analyze.py prepare   # 切批
python3 skills/skills-analyze/scripts/skills_apply.py             # 写回三列

# 6. 岗位 JD 语义回写（可选增强，建议在跑匹配前完成）
python3 skills/job-intake/scripts/jobs_analyze.py prepare         # JD 精析切批
python3 skills/job-intake/scripts/sync_job_columns.py             # 回写门槛/必备/加分三列
python3 skills/job-intake/scripts/check_skill_coverage.py         # 岗位词表 vs 简历标签命中率自检

# 7. 跨组织复制表结构（可选）
python3 skills/replicate/scripts/replicate_base.py <新baseId>
python3 skills/replicate/scripts/sync_schema.py --check           # 只读：报 config 与真实 Base 漂移，缺列 exit 2
```

入库类命令输出 JSON 报告：`total / parsed / skipped_dup / needs_ocr / failed /
created / readback_missing`。`readback_missing` 非空或 `failed` 非空时 exit 1。
`--dry-run` 只解析不触网写表。匹配链路的报告字段见各脚本 stdout。
所有入口 stage 0 会跑环境预检（`shared/preflight/`），缺凭证/缺依赖直接失败。

## 代码地图（约 2800 行 Python，不含 vendor；以实际为准）

| 文件 | 行数 | 职责 |
|---|---|---|
| `shared/notable.py` | ~355 | 唯一传输层：token 缓存、重试与 pacing、记录 CRUD、类型转换、附件三步上传 |
| `shared/extract.py` | ~110 | 文本提取：pdf(pdftotext→pypdf) / docx(zip→textutil) / doc(textutil→olefile) / txt,md 直读 / 图片标记 OCR；`SUPPORTED_EXTS` 唯一源 |
| `shared/vocab.py` | ~35 | 技能词表 `SKILL_WORDS` 与统一分词 `toks`/`SEP` 唯一源 |
| `shared/waves.py` | ~35 | 跨 skill 公共 subagent 并发调度：MAX_AGENTS 硬上限 20、自动均衡 batch=ceil(条数/20)、一次性并发 |
| `shared/query.py` | ~70 | 只读查询/统计入口 |
| `shared/preflight/preflight.py` | ~260 | stage 0 预检：凭证/依赖/目标目录/整点峰值规避（同目录 .sh/.ps1 为外壳） |
| `skills/resume-intake/scripts/parse_resume.py` | ~180 | 简历字段抽取（正则+词表），resume-intake 私有 |
| `skills/resume-intake/scripts/upload_resumes.py` | ~245 | 简历入库入口（查重→附件→写表→回读） |
| `skills/job-intake/scripts/parse_job.py` | ~115 | JD 字段抽取（文件名拆部门+正文切段），job-intake 私有 |
| `skills/job-intake/scripts/upload_jobs.py` | ~125 | 岗位入库入口（job_id=J+md5(部门\|岗位名)[:10]） |
| `skills/job-intake/scripts/jobs_analyze.py` | ~100 | JD 精析 prepare/merge 切批合并 |
| `skills/job-intake/scripts/sync_job_columns.py` | ~60 | 回写硬性门槛/必备技能/加分项三列 |
| `skills/job-intake/scripts/check_skill_coverage.py` | ~60 | 岗位技能词表 vs 简历标签命中率自检，低覆盖 exit 2 |
| `skills/match-verify/scripts/match_gated.py` | ~230 | 匹配主链路第一段：机械门槛一票否决 + 语义打分（阈值 REC_MIN/PEND_MIN、MIN_SCORE） |
| `skills/match-verify/scripts/match_analyze.py` | ~175 | 匹配主链路第二段：prepare/merge/apply/stats，逐岗 subagent 并发 AI 判定并落库 |
| `skills/match-verify/scripts/semantic_score.py` | ~110 | 私有库：SYNONYM 同义 + HYPERS 单向上下位词典与 `hit()` 判定 |
| `skills/skills-analyze/scripts/skills_analyze.py` | ~115 | 简历三列精析 prepare/merge |
| `skills/skills-analyze/scripts/skills_apply.py` | ~100 | 写回三列：技能标签 / AI结构化提取 / AI深度解析 |
| `skills/skills-analyze/scripts/sync_ai_columns.py` | ~60 | 手工修正薄通道（实现委托 skills_apply） |
| `skills/replicate/scripts/replicate_base.py` | ~70 | 新 Base 重建四表结构（从 config 派生） |
| `skills/replicate/scripts/sync_schema.py` | ~160 | 结构自检/补列（`--check` 只读报漂移 exit 2；`--rename`/`--drop` 显式对齐） |
| `shared/vendor/` | — | 内置 pypdf / olefile / typing_extensions（零 pip 依赖） |

## 表结构（config.json 是唯一事实源）

四张表业务键：`resume` 简历库管理 / `job` 岗位JD表 / `match` 智能匹配 / `perm` 权限配置。
**base_id、table_id、字段全集、字段类型、单选/多选选项清单一律见 `config.json`
（`tables` / `fields.<表>` / `types.<表>` / `options.<表>`），本文件与文档都不手抄**，
抄进文档的 id 与清单会随改表漂移成谎报。OpenAPI 记录接口以中文字段名为 key，无需字段 ID。

新增/改字段 = 改 config（fields/types/options）+ 跑 `sync_schema.py` 对目标 Base 补列，两步同一提交。

AI 语义分析三列（`resume.ai_extract` AI结构化提取 / `resume.ai_deep` AI深度解析 /
`match.ai_analysis` AI匹配分析）为 schema 净增量、与组织无关，类型均 text：
`skills/skills-analyze` 产出前两列 + 简历技能标签，`skills/match-verify` 产出 AI匹配分析。

## 业务规则

- **查重**：简历 附件MD5 → 手机号（含批内）；岗位 md5(部门|岗位名)。重跑幂等。
- **扫描件**：抽不出手机号且抽不出邮箱 → `needs_ocr`，不入库，交 agent 视觉补录。
- **附件**：uploadInfos 取 uploadUrl/resourceId → 裸 PUT OSS → 记录里写
  `[{filename,size,type,url:resourceUrl,resourceId}]`。附件失败则该条不写表。简历与 JD 同纪律。
- **回读**：写后按手机号/job_id 全量回读比对，缺失即失败。
- **匹配**：机械门槛（组织/学历/年限/证书/年龄）一票否决 → subagent 逐岗判 keep 与语义计分 →
  推荐阈值 total≥80 推荐 / 60–79 待定 / <60 不推荐；落库门槛 `MIN_SCORE` 环境变量默认 20。
  口径细节见 `skills/recruit-model/references/ai-analysis-spec.md`。
- **限流**：429/5xx/文档初始化中 指数退避重试；401 自动刷 token 重试一次；QPS 403 属网关级拒绝可重试。
- **并发**：附件上传经 `Notable.map_parallel` 5 线程并发（I/O 密集）；解析与记录写串行
  （记录每批 10 条，`Notable.call()` 全局 pacing 20 req/s）。subagent 并发一律经 `shared/waves.py`，
  agent 数硬上限 20、一次性并发发出、不分波不串行。
- **留空字段**：`resume.org`（简历库所属组织）为人工/预留列：全链路脚本均不赋值，由 HR
  手工维护或留空预留，匹配不用它做写入源（机械门槛里简历侧 org 为空即自动跳过组织比对，
  组织口径以岗位侧 org 为准）；岗位统计四字段（`job.stat_*`）由匹配流程 stats 维护，入库脚本不写。
- **match.org 写入方**：`match_gated.py --commit` 与 `match_analyze.py apply` 落库匹配记录时，
  `org` 一律取所匹配岗位（job 表）的 org，即 match.org = 岗位侧组织分类，不取简历侧。
- **死代码零容忍**：改动同提交内删掉不再使用的逻辑/入口/孤儿 import。

## 依赖

- Python ≥ 3.9，stdlib only（+ vendor 内 pypdf/olefile/typing_extensions）。
- 外部二进制（可选回退）：`pdftotext`、`textutil`(macOS)。
- 钉钉应用权限点：`Notable.Base.Read.All`、`Notable.Base.Write.All`、`Storage.File.Read`；
  目标 Base 还需把应用机器人加为协作者。

## 测试

```bash
python3 -m unittest discover -s tests                        # 无副作用（含 mock HTTP 传输），用例数以实跑为准
python3 -m py_compile shared/*.py skills/*/scripts/*.py
bash shared/preflight/preflight.sh                           # Windows: shared/preflight/preflight.ps1
python3 skills/replicate/scripts/sync_schema.py --check      # 只读：config 与真实 Base 漂移，缺列 exit 2
```
