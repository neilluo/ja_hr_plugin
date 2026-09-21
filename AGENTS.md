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
- 入口脚本：跨 skill 公共入口 `shared/query.py`；单 skill 私有入口
  `skills/resume-intake/scripts/upload_resumes.py`、`skills/job-intake/scripts/upload_jobs.py`、
  `skills/match-verify/scripts/match.py`（匹配打分）、`skills/replicate/scripts/replicate_base.py`（建表）。
  agent 直接 Bash 跑脚本，读 JSON 报告即可，不需要中间回合。
- 命令一律以仓库根为 CWD 执行：跨 skill 公共入口 python3 shared/query.py、bash shared/preflight.sh（Windows 用 shared/preflight.ps1）；单 skill 私有入口 python3 skills/<skill>/scripts/<entry>.py；shared/ 只放跨 skill 公共库与公共入口，skills/<skill>/scripts/ 只放该 skill 私有入口，scripts/ 下入口为执行而非阅读。

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
6. 读回值已归一：singleSelect→字符串、multipleSelect→字符串数组（`Notable._norm`）。

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
| 简历入库（含 OCR 补录） | `skills/resume-intake/SKILL.md` |
| 岗位 JD 入库 | `skills/job-intake/SKILL.md` |
| 查询与统计 | `skills/candidate-query/SKILL.md` |
| 匹配打分与语义复核 | `skills/match-verify/SKILL.md` |
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
- 曾在 OpenAPI 重写时连带删掉 shared/preflight.* → preflight 是 stage 0 强制门禁，重写业务脚本时必须同步迁移，不得丢弃。
