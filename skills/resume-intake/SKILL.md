---
name: resume-intake
version: 0.2.0
description: Fast resume ingestion - one bundled Python script does text extraction, field pre-fill, dedupe (real content MD5 within the batch/checkpoint, filename+byte-size against library attachments) + phone dedupe, batch write (<=100/call), concurrent attachment upload and readback in a single turn; the agent only reviews the report and confirms low-confidence orgs. Matching then continues via match-verify.
name_en: Resume Intake
name_zh: 简历入库
description_en: Upload resumes (single or batch). One script call parses, dedupes, batch-writes and attaches; agent reviews the intake report only.
description_zh: 上传简历（单个或批量）。一次脚本调用完成解析、按手机号查重、批量落库、原文件名并发附件与回读；agent 只审阅入库报告并复核低置信组织，随后接续定向匹配。
user-invocable: true
argument-hint: 上传 1 个或多个简历文件(PDF/Word)
argument-hint-en: Upload one or more resume files (PDF/Word)
argument-hint-zh: 上传 1 个或多个简历文件(PDF/Word)
---

# 简历入库（三段式流水线）

> **⚡ 热路径优先：1 份或几份简历「解析入库并匹配」→ 只读 [HOTPATH.md](HOTPATH.md) 这一个文件就能跑完整条流程。**
> HOTPATH.md 是 `recruit-model/SKILL.md` + `references/{execution-notes,ai-analysis-spec,system-config}.md`
> \+ 本 SKILL + `match-verify/SKILL.md` 六个文件的**热路径浓缩**，业务铁律逐条保留（58/58），
> 且已内联 `--auto-match` 一步入库+判定输入、岗位预筛/字段裁剪、apply 内置校验（不必单独 verify）。
>
> `recruit-model/SKILL.md` 与 4 个 references（execution-notes / ai-analysis-spec / system-config /
> parsing-methods）**仅异常时查阅**——JD 入库（`job-intake/SKILL.md`）、复刻部署到新 Base
> （`replicate/SKILL.md`）、扫描件/加密件解析细节、查询与看板、或 HOTPATH 未覆盖的边界情况。
> 下面保留本 SKILL 的完整三步走说明作为**异常/批量场景的参考**，热路径不必逐字读它。

**铁律：本技能全程只允许调用脚本，禁止 agent 逐条敲 `dws` 命令。**解析、查重、批量写库、并发附件、回读全部在脚本内部完成（实测：批量写 31 条 1.38s/1 次调用 vs 逐条 38.79s/31 次；批量查重 1.33s/1 次 vs 39.50s/31 次；附件并发 5 为 2.79s vs 串行 5.59s）。

## 三步走

### Turn 1 —— 跑入库脚本（一次调用，整批完成）

```bash
# macOS / Linux
python3 scripts/intake_resume.py --config <config.json绝对路径> \
        --files <简历文件1> [简历文件2 ...] \
        --out-dir <输出目录绝对路径> [--no-attachment] [--reset] [--wall-budget <秒>]

# Windows（不要用裸 python，别名可能静默失败、退出码 49）
py -3 scripts\intake_resume.py --config <config.json绝对路径> --files <文件...> --out-dir <绝对路径>
```

- `--config`：插件根目录 `config.json`（与本技能目录的相对位置是 `../../config.json`，按平台注入的技能基目录展开成绝对路径后传入；config.json 由 [复刻部署](../replicate/SKILL.md) 生成）。
- `--files`：用户本次上传的全部简历文件，**一次全给**，不要分多次调用。
- `--out-dir`：本批产物目录（建议工作区下专用目录），同一批次的后续步骤复用同一目录。
- `--wall-budget <秒>`：墙钟预算（默认 100，必须小于 agent 工具 120s 超时）。到点脚本 **graceful 停**：checkpoint 逐条落盘、stdout 打印已完成/未完成清单与一行 `RESUME:`、退出码 0、报告 `ok=true` 且 `partial=true`（附 `pending_files`/`deferred_attachment_files` 名单）。**续跑 = 重跑同一条命令**（checkpoint 增量落盘 + 幂等，不产生重复记录）；见 `RESUME:` 就重跑，最多 3 次，仍 partial 才把已完成/未完成清单报给用户。脚本内部**不循环子批**（那只会把总墙钟拖过外部超时）。
- `--no-attachment`：用户明确说"先不传附件"时加；之后"补传附件"= **重跑同一命令不带此参数**——checkpoint 把「记录已写」与「附件已传」分开记状态（契约 v3 §9#6），且 P3 起**每个状态一确立就原子落盘**（中途被杀/超时不丢已完成进度），重跑时已完整成功的文件整条跳过，只欠附件的文件**仅补传附件**（按 record_id 更新附件字段并回读，绝不重复建记录）。
- `--reset`：仅用户明确要求"从头重来"时加。
- 脚本内部完成：提取文本（**macOS 上扫描件/图片简历自动走系统自带 Vision OCR 救回**：零 pip 依赖、纯本地不出网、多份并行 ≤4、单份约 1.2~1.6s；OCR 文本必须过「水印/重复串/数字字符数」护栏，不可信则如实报失败，绝不假成功。**首次运行可能触发 macOS TCC 授权弹窗**，需用户点一次允许）→ 正则预抽字段 → 去重（本批内/checkpoint 走真 MD5；库内附件走「文件名+字节大小」，**不是**内容级比对，转述时别说成"MD5 相同"）→ 一次批量查重（手机号主键）→ 批量写简历库（≤100 条/次，命中即覆盖更新）→ 技能标签只增不删补选项 → 期望地点兜底「不限」→ 并发上传附件（原始文件名）→ 写后回读。
- 产物：`<out-dir>/candidates.json`、`intake_report.json`、`checkpoint.json`；stdout 末行 `ARTIFACT:<绝对路径>/intake_report.json`。

**产物凭证校验（D7，必做）**：读取 ARTIFACT 指向的 `intake_report.json`，确认文件存在且 `ok == true` 才能进 Turn 2。`partial == true`（有 `RESUME:` 行）→ 先重跑同一命令续跑（最多 3 次）补齐再进 Turn 2。不存在或非 ok → 重跑本步（checkpoint 幂等续跑，最多 2 次）；仍失败 → 如实告知用户失败原因与已完成部分，**禁止跳过或假装成功**。

### Turn 2 —— agent 审阅与复核（不动表格，只读产物）

读 `intake_report.json` 与 `candidates.json`，做四件事：

1. **转述上传处理清单**（业务话，见下方固定格式）：`rows` 逐行转述，`summary` 做小计，`warnings` 全部转述，失败项不隐藏。
2. **解析失败项如实告知**：`parse_status` 为 `no_text_layer`/`garbled`/`encrypted` 的候选人 → ❌ 行写明"无法解析，请提供文字版"，**绝不硬造字段**。P3 起 macOS 上扫描件/图片已被自动 Vision OCR 救回（`parse_status=ok`、`backend=vision_ocr`），仍失败的只剩**加密/损坏/OCR 不可信**（OCR 文本仍是水印/重复串/无数字字符）；非 macOS 机器的扫描件/图片仍如实报 `no_text_layer`（跨平台 OCR 兜底在后续版本规划中，当前未实现）。OCR 救回件的「OCR 文本可能有小误读」与姓名/手机号人工确认警告必须照转。
3. **冲突停下问用户**：`dedupe == "conflict"`（手机号相同但姓名不同 = 疑似重名/错录）→ 停止该候选人后续流程，业务话请用户确认，不自动选。
4. **低置信组织复核**：`org_confidence == "low"` 的候选人，依 `evidence` 原文判定组织（制造基地/厂务/设备/EHS → 制造中心；财务/行政/人力/数据信息 → 职能中心）；**判不了才问用户，不猜**。判定结果记录下来，在匹配环节的 `candidate_overrides` 里回填（见 [定向匹配](../match-verify/SKILL.md)）；`needs_review` 含 `"years"` 的候选人同样留到匹配回合用原文复核工作年限。

本回合**禁止**敲任何 dws 命令、禁止手改表格；需要修正的字段一律走匹配环节的 `candidate_overrides` 由脚本统一写回。

### Turn 3 —— 接续定向匹配（默认执行）

老口径「入库后立即定向匹配」保持：Turn 2 完成后，默认按 [定向匹配](../match-verify/SKILL.md) 的三步走继续（`build_match_input.py --candidates <out-dir>/candidates.json` → 批量判定 → `apply_decisions.py`），只对通过硬性门槛的组合建记录并由脚本打分、重算岗位统计。用户明确说"只入库、先不匹配"时停在 Turn 2 清单即可。

## 输出（每次必出清单，单个文件也出）

```
── 简历入库结果 ──────────────
序号 | 文件名 | 处理结果 | 说明
1 | 【暖通主管_曲靖】石昊.pdf | ✅ 新入库 | 制造中心·技术类，附件已传
2 | 胡裕_14年.pdf | ✅ 已覆盖 | 手机号已存在，用最新简历覆盖更新
3 | 张三.pdf | ⏭️ 跳过 | 与系统内已有附件完全相同（重复上传）
4 | 扫描件.png | ✅ 新入库 | macOS Vision OCR 救回入库；OCR 文本可能有小误读，姓名请人工确认
5 | 加密件.pdf | ❌ 失败 | 无法解析（加密），请提供未加密文字版
────────────────────────
小计：新入库 2 | 覆盖 1 | 跳过 1 | 失败 1 | 附件已传 3 | 附件失败 0
待关注：李四（手机号与库内"王芳"相同，请确认是否同一人）
```

## 业务规则（与老版一致，脚本与 agent 共同保证）

- 查重主键 = **手机号**：命中 → 覆盖更新并在清单标注"已覆盖"；同号多条 → 停止报告；同号不同名 → 停下请用户确认。
- 所属组织**必填**：判不了问用户；组织为空 = 匹配不到任何岗位。
- 期望地点**必填**：没有明确地点一律「不限」（脚本兜底），agent 从原文看出明确城市再覆盖。
- 简历库分类必填（技术类/产品类/市场类/运营类/其他，默认技术类）；沟通状态新入库默认「待筛选」。
- 技能标签**只增不删**：新标签追加选项时保留全部已有选项及其 id。
- 附件一律**原始文件名**，禁止改名/加序号前缀。

## If Connectors Available

数据表格（钉钉 AI 表格）已连（默认）→ 脚本直接批量落库。未连或 `dws` 未登录 → 脚本会失败并给出原因；此时只输出解析后的结构化草案，提示用户先在「设置 → 连接器」开启并授权钉钉。机器缺 python 运行时 → 引导安装 Python 3 后重跑（自检：`python3 -V && dws aitable base list --limit 1`）。
