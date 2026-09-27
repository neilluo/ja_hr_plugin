# parsing-methods — 文本提取与字段抽取

## 支持的文件类型（唯一源）

- 简历链路：`shared/extract.py` 的 `SUPPORTED_EXTS`（`DOC_EXTS ∪ IMG_EXTS`）。入口脚本扫描目录、
  preflight 校验都从这里引用，文档与脚本都不得再抄一份扩展名清单。图片类一律标 `needs_ocr`，
  不进文本提取。
- 岗位 JD 链路：`skills/job-intake/scripts/upload_jobs.py` 的 `EXTS`（.doc/.docx/.pdf）。

## 文本提取（shared/extract.py）

统一入口 `extract(path) -> {text, needs_ocr, error}`，异常路径键齐全。

| 格式 | 主路径 | 回退 |
|---|---|---|
| .pdf | `pdftotext -enc UTF-8 <file> -` | vendor/pypdf |
| .docx | zipfile 读 word/document.xml 去标签 | `textutil -convert txt -stdout` |
| .doc | `textutil -convert txt -stdout` | vendor/olefile 读 WordDocument 流（utf-16-le 粗提取） |
| .txt / .md | 直接按 utf-8 读（ignore 非法字节） | — |
| 图片（png/jpg/jpeg/bmp/gif/webp/tif/tiff） | 不提取 | 标记 `needs_ocr=True` |

subprocess 一律 capture_output + timeout=30。

## 扫描件判据（重要）

**抽不出手机号且抽不出邮箱 → 进 needs_ocr 队列，不入库。**
不用文本长度判：扫描件 pdftotext 输出是乱码但非空（实测数百至上千字）。

## 词表与枚举的唯一源

- 技能词表与分词：`shared/vocab.py`（`SKILL_WORDS` 原始词清单、`SKILL_WORDS_SORTED` 长词优先、
  `SEP` / `toks()` 统一分词）。parse_job 与 parse_resume 共用同一份，杜绝岗位/简历词表漂移。
- 语义等价（同义 `SYNONYM` / 单向上下位 `HYPERS`）：`skills/match-verify/scripts/semantic_score.py`，
  只服务匹配打分，属该 skill 私有，禁止提升到 shared/。
- 部门 / 组织 / 城市 / 学历 / 分类 / 沟通状态 等枚举：`config.json options`（如
  `options.job.department`、`options.job.org`、`options.job.work_location`、
  `options.resume.category`、`options.resume.education`）。解析脚本运行时从 config 派生正则与校验，
  仓内不维护清单副本；分类名不在 config 枚举里会直接抛错。
- 部门别名映射（如 电池设备部→电池制造部-设备部）是解析归一逻辑，留在 `parse_job.py` 的
  `_DEPT_ALIAS`，不是枚举副本。

## 简历字段（skills/resume-intake/scripts/parse_resume.py）

姓名优先序：①「姓名：X」标签 ②文件名拆段（剔除【岗位_城市_薪资】前缀、黑名单段）
③首行启发式；三者都过黑名单（自我评价/专业技能/核心优势/学历词/裸地名）。
学校/专业带左右边界与裸词黑名单；年限正文未命中时用文件名「N年」兜底；
薪资支持区间（15k-20k）。技能标签按 `SKILL_WORDS_SORTED` 命中启发式（最多 15 个），
分类走「分类→关键词」映射后以 `config.options.resume.category` 校验，允许少量偏差。

## JD 字段（skills/job-intake/scripts/parse_job.py）

- 组织/部门从文件名拆：`岗位说明书-制造中心-曲靖制造基地-<部门段> - <岗位名>.doc`，
  部门归一到 `config.options.job.department` 枚举（含两级部门连字符、别名映射）。
- 岗位名优先取正文「岗位名称/职位名称/职位 Position:」标签，回退文件名尾段。
- 正文按标题切段：岗位职责/工作职责、任职要求/资格条件；硬性门槛由关键词正则从任职要求抓
  （学历/年限/证书类句子，最多 6 条）。工作城市按 `config.options.job.work_location` 命中，
  兜底默认值见代码。
- must/bonus 技能从任职要求按 `SKILL_WORDS_SORTED` 命中（must 上限 12、bonus 上限 8）；
  权重默认值写在 `parse()` 返回值常量里（缺省见代码，不在 config 声明）。
- `job_id` 由入库入口 `upload_jobs.job_id_of()` 生成：`"J" + md5(部门|岗位名)[:10].upper()`，
  天然幂等（查重键）。

## 已知边界

- 竖排水印扫描件 phone 抽不出 → needs_ocr，属源文件质量问题。
- .doc 的 textutil 偶发空输出（时序），重跑可恢复；空 section 不影响部门/岗位名。
- JD 精析（`jobs_analyze.py prepare/merge` + `sync_job_columns.py`）会用 subagent 重写
  hard_gates / must_skills / bonus_skills 三列，覆盖 parse_job 的启发式结果；
  改词表后跑 `check_skill_coverage.py` 自检岗位词表 vs 简历标签命中率，低覆盖 exit 2。
