# AGENTS.md — 招聘智能匹配极速版插件工作手册

> **代码是唯一事实来源。** 本文件描述代码的实际行为，不规定行为。
> 代码变更时必须同步更新本文件。如果本文件与代码矛盾，以代码为准并立即修正本文件。

## 架构铁律

### dws 是宿主代理 shim，Python subprocess 调不了

`dws` 不是真正的 CLI 二进制——它是千问办公宿主代理的占位符。Python `subprocess.run(["dws", ...])` 返回 `"pending host-side execution"` 占位符，不真正执行。**只有 agent 通过 Bash 工具直接调 dws 才有效。**

这是整个 emit/replay 架构存在的根本原因：

- `DwsClient`（`shared/aitable/client.py`）只有两种模式：`MODE_EMIT`（收集命令、返回模拟成功）和 `MODE_REPLAY`（从预加载结果文件返回真实结果）。**没有 live 执行模式，不要尝试加一个。**
- `upload_attachments.py` 的三阶段（prepare → upload → verify）把 OSS PUT（纯本地 urllib）和 dws 调用分离，agent 只在 dws 步骤介入。

### 入库完整流程（6 步，agent 只在 step 2 和 4 介入）

```
Step 1  脚本（纯本地）   intake_resume.py emit 模式 → 解析简历 + 收集 dws 命令
Step 2  agent 调 dws     执行 dws record upsert → 记录写进 AI 表格，拿到 record_id
Step 3  脚本（纯本地）   upload_attachments.py --phase prepare → 生成附件上传清单
Step 4  agent 调 dws     逐条执行 dws attachment upload → 拿 uploadUrl + fileToken
Step 5  脚本（纯本地）   upload_attachments.py --phase upload → 并发 PUT 到 OSS + 组装 update JSON
Step 6  agent 调 dws     执行 dws record update → 附件字段写回表格
```

**Step 2 也可走 build_replay.py + replay 路径**（见下方"两种执行路径"）。

### 操作文档索引（详细步骤在各 skill 的 HOTPATH.md / SKILL.md 里，本文件只留指针）

| 操作 | 文档位置 |
|---|---|
| 简历入库 Turn 1b（emit 后 6 步精确清单） | `skills/resume-intake/HOTPATH.md` 路径 B |
| 简历入库 Turn 1（脚本调用） | `skills/resume-intake/HOTPATH.md` §1 |
| 岗位入库 | `skills/job-intake/SKILL.md` |
| 定向匹配 | `skills/match-verify/SKILL.md` |
| 复刻部署 | `skills/replicate/SKILL.md` |
| 查询与看板 | `skills/candidate-query/SKILL.md`、`skills/recruit-dashboard/SKILL.md` |
| 通用铁律与犯错记录 | 本文件下方 |
| 脚本索引 | 本文件下方 |

### 两种执行路径

**路径 A（run_pipeline.py 编排，推荐）**：
```bash
# Step 1: emit
# 注意：run_pipeline.py 不支持 --files-dir（只有 intake_resume.py 支持），
# 必须用 --files 传入具体文件路径，可用 shell glob 展开目录中的文件。
python3 scripts/run_pipeline.py --phase emit --intake-type resume \
    --config config.json --files /path/to/resumes/*.pdf /path/to/resumes/*.docx \
    --out-dir <out>
# stdout 打印结构化 JSON，含所有 step 和 dws 命令
# Step 2-6: agent 按 stdout 指引执行 dws 命令，每条结果写 dws_out_<seq>.json
# 最后: replay
python3 scripts/run_pipeline.py --phase replay --intake-type resume \
    --config config.json --out-dir <out> --replay-path <out>/dws_results.json
```

**路径 B（直接调脚本）**：
```bash
# Step 1: emit（直接调 intake_resume.py）
python3 skills/resume-intake/scripts/intake_resume.py \
    --config config.json --files-dir <dir> --out-dir <out>
# Step 2: agent 从 dws_commands.json 提取 upsert 命令执行
# Step 3-6: 用 upload_attachments.py 三阶段
```

### 脚本索引（代码是事实来源，这里只是导航）

| 脚本 | 作用 | dws 依赖 |
|---|---|---|
| `skills/resume-intake/scripts/intake_resume.py` | 简历解析 + emit 收集命令 | emit 模式无 |
| `skills/job-intake/scripts/intake_job.py` | 岗位解析 + emit 收集命令 | emit 模式无 |
| `scripts/run_pipeline.py` | 编排 emit → agent exec → replay | 无（subprocess 调 intake 脚本） |
| `scripts/build_replay.py` | 从 dws_out_*.json 组装 dws_results.json + 替换假 token | 无 |
| `scripts/upload_attachments.py` | 附件三阶段（prepare/upload/verify） | prepare/verify 无；upload 阶段做 OSS PUT |
| `shared/aitable/client.py` | DwsClient（emit/replay 双模式） | 无 |
| `shared/aitable/uploader.py` | AttachmentUploader（并发 OSS PUT） | emit 模式跳过 OSS PUT |
| `shared/intake/pipeline.py` | 入库流水线（9 阶段） | emit 模式无 |
| `shared/intake/checkpoint.py` | CheckpointStore（幂等续跑） | 无 |

## 常见犯错记录（每次都要读，不要重蹈覆辙）

### 1. 读了 HOTPATH/SKILL 文档就以为"一条命令搞定"——错

**症状**：跑完 `intake_resume.py` 报告"31 份新入库"，但 AI 表格是空的。
**根因**：HOTPATH.md 写了"脚本内部完成批量写库"——这在 emit 模式下是模拟成功，不是真写库。必须走完 emit → agent 执行 dws → replay（或 upload_attachments 三阶段）才算真正入库。
**教训**：**代码第一，文档第二。** 先读 `DwsClient._spawn()` 确认当前模式的行为，再决定流程。文档描述的是期望结果，不是当前步骤。

### 2. checkpoint 在 emit 模式下虚假标记 record_written=true——已修（2026-09-19）

**症状**：`--reset` 重跑后 emit 正确收集了 38 条 dws 命令，但不加 `--reset` 时全部跳过、dws_commands.json 为空。
**根因**：`pipeline.py` 的 `assemble()` 在 emit 模式下把 result 设成"新入库"，`mark_written()` 无条件写 `record_written=true`。但 emit 模式下记录并没真正写入表格。
**修复**：`mark_written()` 新增 `emit_pending` 参数，emit 模式下 `record_written=False`、`emit_pending=True`，重跑不跳过。
**文件**：`shared/intake/checkpoint.py`、`shared/intake/pipeline.py`
**教训**：checkpoint 的状态语义必须与实际行为一致。emit ≠ 写入。

### 3. 文件名人工转写导致 preflight 失败

**症状**：把"訾金保"写成"滕金保"，preflight 拦截，浪费 3 个回合调试。
**修复**：新增 `--files-dir` 参数，传目录路径自动扫描，绕过人工转写。
**文件**：`intake_resume.py`、`shared/preflight.py`
**教训**：agent 从 `ls` 输出复制文件名到命令行时会看错。能用目录就别逐个列文件名。

### 4. 在 Python subprocess 里调 dws 只回 placeholder

**症状**：写了个 `execute_dws_commands.py` 用 `subprocess.run` 逐条执行 dws 命令，38 条全部返回 placeholder。
**根因**：dws 是宿主 shim，subprocess 调用不触发宿主执行。
**教训**：**agent 只能通过 Bash 工具直接调 dws。** 不要写脚本批量执行 dws 命令——那不工作。`upload_attachments.py` 的设计是对的：脚本做纯本地计算（OSS PUT、JSON 组装），dws 调用留给 agent 用 Bash 工具逐条执行。

### 5. checkpoint stale 检测：record_written=true 但 record_id=null（2026-09-19 修）

**症状**：上次 session emit 模式 bug 把 `record_written` 标成 `true`，但 `record_id` 是 `null`（记录没真写）。下次不加 `--reset` 跑，脚本看到 `record_written=true` 就跳过，dws_commands.json 为空。
**根因**：`dedupe_local()` 只看 `record_written`，不看 `record_id` 是否存在。
**修复**：在 `pipeline.py` `dedupe_local()` 加 stale checkpoint guard：`record_written=true` 且 `record_id` 为空 → 视为未写，重新处理。
**文件**：`shared/intake/pipeline.py`（第 637 行后）
**教训**：checkpoint 状态的语义要完整——`record_written=true` 必须同时有 `record_id` 才可信。

### 6. agent 不按流程走、自创步骤（2026-09-19）

**症状**：emit 完成后，agent 没有先执行 upsert 拿 record_id，而是先跑附件上传，导致 attachment_update_records.json 里 record_id 全是占位符。又把 dws_out 文件放到了 `dws_out/` 子目录而非 out_dir 根目录，导致 upload_attachments.py 报 missing。还试图自己写脚本批量执行 dws 命令（已被犯错 #4 覆盖但再犯）。upsert 时没删 fake token 导致 `INVALID_ATTACHMENT_FILE_TOKEN` 报错。
**根因**：Turn 1b 的精确步骤没有写死，agent 每次 session 都靠"理解"流程而非"执行"清单。
**修复**：在 `skills/resume-intake/HOTPATH.md` 路径 B 写死 6 步精确清单 + 禁止事项。AGENTS.md 只留索引指针，不塞操作细节。
**教训**：**流程纪律不能靠 agent 自觉——必须写死在操作文档里。** 写死步骤 + 禁止事项比说"理解原理后自行决定"有效得多。AGENTS.md 是索引不是操作手册。

## 代码变更时必须更新的文件

当修改以下代码时，必须同步更新对应文档：

| 改了什么 | 必须更新 |
|---|---|
| `DwsClient` 模式/行为 | 本文件 + `resume-intake/HOTPATH.md` |
| `pipeline.py` 阶段/流程 | 本文件 + `resume-intake/HOTPATH.md` + `resume-intake/SKILL.md` |
| `checkpoint.py` 状态语义 | 本文件（常见犯错记录） |
| `intake_resume.py` CLI 参数 | `resume-intake/HOTPATH.md` 命令示例 |
| `upload_attachments.py` 阶段 | `resume-intake/HOTPATH.md` 路径 B |
| `config.json` 字段结构 | 不需要更新文档（config 是数据不是文档） |

## 环境要求

- Python 3.9+（不设上限，向后兼容）
- dws 已安装且已登录（agent Bash 工具直接调 `dws aitable base list --limit 1` 验证）
- config.json 存在且 base_id/table_id/field_id 正确（由 replicate 技能生成）
- macOS 上扫描件/图片简历自动走系统 Vision OCR（首次可能弹授权窗）
