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

# 4. 匹配打分（写 match 表 + 刷新岗位统计，幂等）
python3 skills/match-verify/scripts/match.py

# 5. 简历 AI 三列精析（技能标签 / AI结构化提取 / AI深度解析）
python3 skills/skills-analyze/scripts/skills_analyze.py prepare   # 切批
python3 skills/skills-analyze/scripts/skills_apply.py             # 写回三列

# 6. 岗位 JD 语义回写 + AI 匹配分析（可选增强）
python3 skills/job-intake/scripts/jobs_analyze.py prepare         # JD 精析切批
python3 skills/job-intake/scripts/sync_job_columns.py             # 回写门槛/必备/加分三列
python3 skills/match-verify/scripts/match_analyze.py             # 逐岗 subagent 并发 AI 分析
python3 skills/match-verify/scripts/sync_match_analysis.py       # AI匹配分析回写

# 7. 跨组织复制表结构（可选）
python3 skills/replicate/scripts/replicate_base.py <新baseId>
```

每条命令输出 JSON 报告：`total / parsed / skipped_dup / needs_ocr / failed /
created / readback_missing`。`readback_missing` 非空或 `failed` 非空时 exit 1。
`--dry-run` 只解析不触网写表。

## 代码地图（全部 Python 约 2900 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| `shared/notable.py` | ~355 | 唯一传输层：token 缓存、重试、记录 CRUD、类型转换、richText 归一、附件三步上传 |
| `shared/extract.py` | ~105 | 文本提取：pdf(pdftotext→pypdf) / docx(zip→textutil) / doc(textutil→olefile) / 图片标记 OCR |
| `shared/waves.py` | ~40 | 跨 skill 公共 subagent 并发调度：MAX_AGENTS 硬上限 20、自动均衡 batch=ceil(条数/20)、一次性并发 |
| `skills/resume-intake/scripts/parse_resume.py` | ~160 | 简历字段抽取（正则+词表），resume-intake 私有 |
| `skills/job-intake/scripts/parse_job.py` | ~120 | JD 字段抽取（文件名拆部门+正文切段），job-intake 私有 |
| `skills/resume-intake/scripts/upload_resumes.py` | ~245 | 简历入库入口 |
| `skills/job-intake/scripts/upload_jobs.py` | ~125 | 岗位入库入口 |
| `shared/query.py` | ~65 | 只读查询/统计入口 |
| `skills/match-verify/scripts/match.py` | ~145 | 匹配打分：简历×岗位 → match 表 + 岗位统计（确定性打分入口） |
| `skills/replicate/scripts/replicate_base.py` | ~120 | 新 Base 重建四表结构 |
| `shared/vendor/` | — | 内置 pypdf / olefile（零 pip 依赖） |
| **skills-analyze（简历 AI 三列精析）** | | |
| `skills/skills-analyze/scripts/skills_analyze.py` | ~120 | prepare/merge 切批合并，规划 subagent 并发精析 |
| `skills/skills-analyze/scripts/skills_apply.py` | ~105 | 写回三列：技能标签 / AI结构化提取 / AI深度解析 |
| `skills/skills-analyze/scripts/sync_ai_columns.py` | ~85 | 零散手工修正通道 |
| **match-verify 增强（智能匹配）** | | |
| `skills/match-verify/scripts/match_gated.py` | ~220 | 门槛前置匹配（硬性门槛过滤后再打分） |
| `skills/match-verify/scripts/match_analyze.py` | ~190 | 逐岗 subagent 并发 AI 匹配分析 |
| `skills/match-verify/scripts/sync_match_analysis.py` | ~125 | AI匹配分析回写 match 表 ai_analysis 列 |
| `skills/match-verify/scripts/semantic_score.py` | ~160 | 私有库：语义词典 + 同义命中判定 |
| **job-intake 增强（JD 语义回写）** | | |
| `skills/job-intake/scripts/jobs_analyze.py` | ~100 | JD 精析 prepare/merge |
| `skills/job-intake/scripts/sync_job_columns.py` | ~60 | 回写硬性门槛/必备技能/加分项三列 |
| `skills/job-intake/scripts/check_skill_coverage.py` | ~60 | 岗位技能词表 vs 简历标签命中率自检，低覆盖 exit 2 |

## 表结构（config.json）

四张表：`resume` 简历库管理 / `job` 岗位JD表 / `match` 智能匹配 / `perm` 权限配置。
config.json 只存 `base_id`、`operator_id`、每表的 `table_id` 与 业务键→中文字段名 映射、
字段类型表。OpenAPI 记录接口以中文字段名为 key，无需字段 ID。

简历业务键：name phone email education school school_rank years_experience major
certificates expected_position expected_location expected_salary skills attachment
upload_time category comm_status org full_text attach_md5
ai_extract（AI结构化提取,text） ai_deep（AI深度解析,text）

匹配业务键（match）：name phone job_name job_id org source cand_skills must_skills
bonus_skills hard_gates expected_position years_experience skill_score bonus_score
total_score recommend update_time evidence ai_analysis（AI匹配分析,text）

AI 语义分析三列（ai_extract / ai_deep / ai_analysis）为 schema 净增量，与组织无关：
`skills/skills-analyze` 产出简历技能标签 + AI结构化提取 + AI深度解析，
`skills/match-verify` 产出 AI匹配分析，均为 text 富文本列。

岗位业务键：job_id job_name department org status work_location responsibilities
requirements hard_gates must_skills bonus_skills must_weight bonus_weight submit_time
attachment stat_total stat_recommend stat_pending stat_reject

## 业务规则

- **查重**：简历 附件MD5 → 手机号（含批内）；岗位 md5(部门|岗位名)。重跑幂等。
- **扫描件**：抽不出手机号且抽不出邮箱 → `needs_ocr`，不入库，交 agent 视觉补录。
- **附件**：uploadInfos 取 uploadUrl/resourceId → 裸 PUT OSS → 记录里写
  `[{filename,size,type,url:resourceUrl,resourceId}]`。附件失败则该条不写表。简历与 JD 同纪律。
- **回读**：写后按手机号/job_id 全量回读比对，缺失即失败。
- **限流**：429/5xx/文档初始化中 指数退避重试 3 次；401 自动刷 token 重试一次。
- **并发**：附件上传经 `Notable.map_parallel` 5 线程并发（I/O 密集）；解析与记录写串行。
  实测 28 简历+附件全链路 27s（串行附件版 42s）。
- **留空字段**：`org`（简历库所属组织）与岗位统计四字段由人工/匹配流程维护，脚本不写。

## 依赖

- Python ≥ 3.9，stdlib only（+ vendor 内 pypdf/olefile）。
- 外部二进制（可选回退）：`pdftotext`、`textutil`(macOS)。
- 钉钉应用权限点：`Notable.Base.Read.All`、`Notable.Base.Write.All`、`Storage.File.Read`。

## 测试

```bash
python3 -m unittest discover -s tests
```
