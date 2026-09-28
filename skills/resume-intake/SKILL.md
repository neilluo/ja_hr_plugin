---
name: resume-intake
description: 简历批量入库到钉钉 AI 表格。解析 PDF/DOCX/DOC、按 MD5+手机号查重、附件直传、回读校验；扫描件进 OCR 队列由 agent 视觉补录。Use when 用户说 上传简历/简历入库/导入简历/简历传表格。
argument-hint: <简历目录路径>
argument-hint-en: <resume directory path>
argument-hint-zh: <简历目录路径>
name_en: Resume Intake
name_zh: 简历入库
description_en: Batch-upload resumes (pdf/docx/doc) into the DingTalk AI Table with dedupe, attachment upload and readback verification.
description_zh: 简历批量入库：解析、查重、附件直传、回读校验；扫描件走 OCR 补录。
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 简历入库

## 执行

```bash
python3 skills/resume-intake/scripts/upload_resumes.py <目录>            # 整目录批量入库
python3 skills/resume-intake/scripts/upload_resumes.py <单个简历文件>     # 只传这一份（无需建临时目录/软链）
python3 skills/resume-intake/scripts/upload_resumes.py <目录> --dry-run  # 预演
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。`<目录>` 与 `<单个文件>` 二选一，
单份上传直接传文件路径即可（脚本自动只处理它），**不要在 /tmp 建软链或临时目录绕路**。

一条命令跑完全链路，读 stdout 的 JSON 报告即可，**不要拆步骤、不要自己调 API**。

## 报告字段与处置

报告是**给 agent 的成品指令**，不是原始数据：`user_line`（回复用户的原话）与 `next_action`
（下一步该做什么）都由脚本算好排在最前，**照做即可，不要自行组织措辞、不要展开其余字段**。

| 字段 | 处置 |
|---|---|
| `VERDICT`（stdout 首行） | `OK`/`WARN`/`BAD` 一行结论，读报告先看它——不必逐字段扫 JSON 判成败。格式真源 `shared/report.py` |
| `user_line` | **交付用户的原话**：一句话中文结论（含新增数/表内总数/精析入队与兜底时刻）。原样复述即结束，禁止再写长汇报 |
| `next_action` | **agent 下一步动作清单**（机器产出）：如"把 cron_job 原样注册""补录扫描件""重跑同目录"。照它做，不要自己发挥 |
| `cron_job` | 仅 `refine_queued > 0` 时输出：一次性精析消费任务的**完整注册规格**（name/at/message/contextDirs 都已备好）。**原样传给定时任务工具**，禁止改写其中任何字段、禁止手写 payload |
| `created_summary` | 本次入库记录的关键字段回带（姓名/手机号/期望职位等，最多 5 条+溢出计数）：用户问"传进去的是谁"直接引用，**禁止再跑 `query.py` 复核** |
| `created` / `readback_missing` | missing 非空 = 失败，重跑同目录即可（幂等） |
| `skipped_dup` | 正常：MD5 或手机号已存在，向用户说明即可 |
| `failed` | 看 error 文本；附件类错误重跑可恢复 |
| `needs_ocr` | 扫描件/图片，按下节补录 |
| `table_total` | 表内记录总数（回读那趟全表扫描顺带得出）：`user_line` 已引用，不必单独复述 |
| `refine_queued` | 当前待精析队列长度：精析异步进行，**不要在上传回合里跑精析** |
| `refine_fire_at` | 仅 `refine_queued > 0` 时输出：`cron_job.schedule.at` 的同一时刻，供人工核对；注册一律走 `cron_job`，不必单独取它 |
| `timing_ms` | 脚本各阶段机器耗时（毫秒）：`list_existing`/`extract`(批量)/`build_rows`/`attach`/`create`/`readback`/`total`。用户问性能时引用此数据定位瓶颈（`attach`/`create` 为主要网络段），不要凭感觉猜 |

## Agent 执行纪律（性能关键，必须遵守）

本套件的耗时大头不是脚本（31 份机器仅 9 秒、单份 1-2 秒），而是 **agent 回合往返**：每多一轮 =
一次模型思考 + 一次工具往返。实测一次单份上传曾花 185 秒 / 14 个往返，其中 8 个不产生业务价值
（瞎猜路径 3 轮、违规 query 复核 1 轮、手写 payload + 时刻过期重算 3 轮、长汇报 1 轮）。以下六条为硬性纪律：

1. **路径直取，不瞎猜**：CWD 已在运行环境给出、用户给的路径原样用；相对路径以仓库根为基准。
   **禁止凭记忆发明路径**（如猜别的 workspace/目录名）再 `ls` 试探——一次 `ls` 猜错就是白扔一个往返。
   路径存疑时，一条命令里 `pwd` + 直接访问用户给的路径 + 必要时 `find` 兜底，合并发出。
2. **一次往返只推进一件事，禁止让记账独占回合**：本 skill 是线性短任务（跑脚本 → 处理扫描件 →
   注册消费任务），**默认不用 TodoWrite**。若要用，必须与同回合的业务调用在**同一条消息**里一起发出，
   禁止"先更新 todo 再干活"这种单独一轮。
3. **互不依赖的调用必须同消息并发或合并成一条命令**：写 payload 与跑补录
   （用 `--backfill -` heredoc，见下节），一律合进同一条 Bash 或同一条消息发出。
4. **不为脚本内建的保障追加验证回合**：`readback_missing` 为空即已逐手机号回读校验（不变量 4），
   总数取 `table_total`、入库明细取 `created_summary`，**禁止再跑 `query.py` 复核**；
   注册时刻取 `cron_job`（脚本已按唯一常量算好），**禁止再跑 `date` 自行加偏移**。
5. **脚本退出后立刻注册 cron_job，中间不插任何回合**：`refine_fire_at` 的延迟窗口有限，
   多插一轮（哪怕是"顺手复核一下"）就可能让时刻过期、注册被拒，反要多花数轮重算。
   注册动作 = 把 `cron_job` 原样传给定时任务工具，与 `user_line` 回复**同一条消息**发出。
6. **不预探扫描件属性**：`needs_ocr` 里的文件不必先查页数/尺寸，`Read` 可直接读 PDF 与图片，
   单页多页都不影响下一步。多份扫描件必须在**同一条消息里一次性并发读**（含多页 PDF 的每页），
   禁止一份一回合串行读。仅当扫描件 ≥8 份且单消息上下文吃紧时，才拆 subagent 并发（每个 subagent
   只"读图→吐字段 JSON"，入库仍由主 agent 汇总成单一 payload 走一条 `--backfill`，禁止 subagent 直接写表）。

## 扫描件补录（agent 只抽基础字段，三列交后台读图精析）

1. 用视觉能力读取 `needs_ocr` 里的每个文件（Read 工具直接看图/扫描 PDF，多页逐页读）。
2. 只抽**基础字段**——姓名、手机号、邮箱、学历、院校、专业、工作年限、期望职位等入库与去重所需。
   **派生列不用填**：`category`（简历库分类）/`school_rank`（院校排名）/`expected_location`（空缺时
   落兜底枚举）由脚本按与批量 `parse()` **同一批函数**（`parse_resume.derive`）补齐，agent 已给出的值
   不覆盖——手填这些列既是重复劳动，也是历史上"28 有 3 空"静默缺陷的成因。
   **不要写 AI 三列**（`skills` / `ai_extract` / `ai_deep`）：它们由后台精析流水线读原件产出，
   当场手写属重复劳动、且会白烧本回合 30+ 秒。payload 形如（stdin heredoc，见第 3 步）：

```json
[
  {"name": "张三", "phone": "13800000000", "email": "z@x.com",
   "education": "本科", "school": "XX大学", "major": "机械工程",
   "years_experience": 5, "expected_position": "设备工程师",
   "_file": "/abs/path/扫描件.pdf"}
]
```

`_file` 必须是**原件绝对路径**：脚本把它写进「原件本地路径」列（业务键 `source_file`），
这是扫描件入精析队列的唯一凭证——它没有 `full_text`，后台 subagent 靠这个路径直接读图。
表里的附件 url 是 OSS 签名链（约 2 小时过期、无换签接口），不能当精析依据，故必须存本地路径。

3. 交给脚本补录，**不要自己 `python3 -c` 调 API**——脚本会做和批量入库完全一致的
   字段校验、附件上传、MD5 去重与手机号回读（原件也会进表）。**用 stdin heredoc 一条命令写完即跑**，
   不要先写临时文件再跑（那是两个回合）：

```bash
python3 skills/resume-intake/scripts/upload_resumes.py --backfill - <<'EOF'
[ {"name": "张三", "phone": "13800000000", ..., "_file": "/abs/path/扫描件.pdf"} ]
EOF
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。
（`--backfill <路径>` 仍支持，用于 payload 需复用或超长的场景；若走文件，必须用带时间戳的唯一名，
旧会话遗留的 `/tmp/ocr.json` 会触发"File has not been read yet"报错。）

补录记录与批量记录一样进精析队列（报告 `refine_queued` 含之），所以**补录后同样要注册消费任务**
（见下节）；若本次会话只做了补录、没做批量，也要注册。

合法业务键见 README「表结构」；写错字段名（如 `gender`）会在报告 `failed` 里明确提示可用字段。
无法识别联系方式的文件不入库，脚本会拒绝，向用户说明原因。

**原件必须留在原地**：精析在后台异步跑，若届时 `source_file` 指向的文件已被移动/删除，
该记录会被判为不可精析——不入队（防止永久卡队列、阻塞匹配门禁）、三列留空，
并由 `skills_analyze prepare` 在 meta 的 `unrefinable` 字段报出。用户要挪走原件时须先确认精析已完成。

## 上传后：注册精析消费任务（硬性步骤，脚本退出后立即执行）

**上传回合不做任何 AI 推理，也不自己跑精析流水线**：脚本只解析、去重、写基础字段、传附件。
三列（技能标签 / AI结构化提取 / AI深度解析）由**独立后台会话**消费精析队列产出
（prepare → 一波 subagent ≤20 → merge → apply，见 `skills/skills-analyze/SKILL.md`）。
队列谓词唯一真源 `shared/refine_loop.py`（此处不复述条件）：批量记录与扫描件补录记录同样入队。

报告 `refine_queued > 0` 时，脚本已在 **`cron_job` 字段产出完整的注册规格**（name / schedule.at /
payload.message / contextDirs 全部备好，由 `refine_loop.consume_task_spec` 从唯一真源派生）。
agent 的动作退化为一步：**把 `cron_job` 原样传给定时任务工具的 add**，与 `user_line` 回复同一条消息发出。

- **禁止手写 payload、禁止改写 cron_job 里任何字段、禁止再跑 `date` 算时刻**——任务名前缀、
  消费命令、"完成即自删"指令、触发时刻都是代码产物（曾手写 payload 一次吃掉 25s 模型思考，
  且手抄的任务名前缀与流水线细节构成双源）。
- **时刻窗口有限，脚本退出后立刻注册**：`cron_job.schedule.at` = 脚本时刻 + `REFINE_DELAY_S`
  （唯一常量，文档不复述数值）。中间每多插一个回合都可能让它过期、注册被"Scheduled time must be
  in the future"拒收；真被拒时用 `python3 shared/refine_loop.py consume resume` 重取一份规格再注册，
  不要自己拼时刻。
- **完成即自删**已写进 `cron_job` 的 payload（按任务名删除自身）；每日兜底巡检的自清理只是对
  崩溃在自删之前的任务的兜底网。

**不设看门狗/补跑任务**（曾设 +15 分钟看门狗，已裁撤）：消费任务崩溃时未写回记录天然仍在
队列（`ai_refined_at` 为空即在队，队列状态就是表数据本身、无中间态要清），僵尸租约 30 分钟
自动过期，两者都由每日兜底巡检重吃——最坏代价是三列晚半天填好，对不阻塞人的精析
链路，这个代价是免费的；而看门狗既与兜底 cron 功能重叠，又无法凭新鲜度区分"已崩"与"在跑"
（判活窗口 > 看门狗延迟时必然误判静默退出），属过度设计，禁止复活。

- 注册只能由 agent 做（脚本是独立进程，宿主机没有向千问办公写调度的本地 API）；
  脚本的配套职责是把注册规格 `cron_job` 连同触发信号 `refine_queued` 一起产出，agent 只负责透传。
- 补录模式（`--backfill`）**同样带 `cron_job`、同样原样注册**：扫描件靠 `source_file` 入队，
  三列由后台读图产出。
- 同一会话多次批量上传：每次各注册各的；后起的消费任务撞租约（refused exit 2）即秒退，
  队列由持租周期吃干净，不会双开 subagent。
- 已知边界（接受，不再加层）：消费任务崩溃 **且** 30 分钟内又有新上传时，新任务会被僵尸
  租约拒绝、这批延至兜底巡检——双重故障低概率，修它会把复杂度引回来。
- 兜底巡检任务（每日一次）的规格同样由代码产出：`python3 shared/refine_loop.py fallback`，
  部署/重建时原样注册，任务名与自清理前缀取自 `refine_loop`，禁止在 cron 里手抄。
- 注册完成后 `user_line` 已含"队列 N 条、约 X 后自动精析、异常最迟次日 HH:MM 兜底"，原样复述即可。
- 三列**无条件逐人精析**：不按硬门槛筛人。简历库是人才池，是否进匹配表由「智能匹配」门槛判定决定。
- 脚本的词表命中与平台AI字段都不算结果，必须由 subagent 读全文推理得出。
- 零散修正或扫描件补录走手工通道：`skills/skills-analyze/scripts/sync_ai_columns.py payload.json`
  （该脚本会带 id 全量回传选项、按手机号回写，缺手机号时按姓名兜底）。

> 表内「AI结构化提取」「AI深度解析」两列已由用户改为**普通文本列**，平台不再自动计算，内容以精析流水线写入为准。

## 边界

- 支持的文件类型以 `shared/extract.py` 的 `SUPPORTED_EXTS` 为唯一源：pdf/doc/docx/txt/md +
  png/jpg/jpeg/bmp/gif/webp/tif/tiff；图片与抽不出文本/联系方式的一律进 `needs_ocr`，其余格式忽略不扫。
- 单目录可重复跑：MD5+手机号双重去重，不会写重复记录。
- 字段口径见 README「表结构」；枚举值（学历/分类/沟通状态）服务端自动补建选项。
- 入库脚本的正则字段提取有已知偏差（姓名/院校可能吃进标签、期望职位带"应聘企业/期望工资"尾巴、
  水印重的 PDF 可能整条手机号丢失、证书/专业可能截断或漏抓）：**粗值偏差由后台精析链自动校正回补**
  （major/school/certificates/工作年限/期望职位/姓名，subagent 读全文校正、非 null 才写回，
  见 `skills/skills-analyze/SKILL.md`），无需在上传回合逐人核对；发现明显错误想立即修，
  可走 `sync_ai_columns.py` 手工通道（payload 带上对应业务键，如 `"name": "方红亮", "school": "贵州大学"`）。
