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
python3 skills/resume-intake/scripts/upload_resumes.py <目录>            # 真实入库
python3 skills/resume-intake/scripts/upload_resumes.py <目录> --dry-run  # 预演
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

一条命令跑完全链路，读 stdout 的 JSON 报告即可，**不要拆步骤、不要自己调 API**。

## 报告字段与处置

| 字段 | 处置 |
|---|---|
| `created` / `readback_missing` | missing 非空 = 失败，重跑同目录即可（幂等） |
| `skipped_dup` | 正常：MD5 或手机号已存在，向用户说明即可 |
| `failed` | 看 error 文本；附件类错误重跑可恢复 |
| `needs_ocr` | 扫描件/图片，按下节补录 |
| `refine_queued` | 当前待精析队列长度（信息项）：精析异步进行，向用户说明"已入队，后台周期消费"即可，**不要在上传回合里跑精析** |

## 扫描件补录（agent 只读图给字段，入库仍走脚本）

1. 用视觉能力读取 `needs_ocr` 里的每个文件（Read 工具直接看图/扫描 PDF）。
2. 把每个文件抽出的字段 + **原文件绝对路径**写成一个 JSON 数组文件（如 `/tmp/ocr.json`）：

```json
[
  {"name": "张三", "phone": "13800000000", "email": "z@x.com",
   "education": "本科", "school": "XX大学", "major": "机械工程",
   "years_experience": 5, "expected_position": "设备工程师",
   "skills": ["暖通空调", "PCW系统"],
   "ai_extract": "学历背景｜XX大学机械工程本科\n工作经验｜5年厂务设备经验\n核心技能｜暖通空调、PCW系统\n求职意向｜设备工程师\n匹配度评估｜厂务岗高度匹配",
   "ai_deep": "亮点：5年一线设备维护经验。风险：行业集中在单一厂区。建议：优先推厂务/设备类岗位。",
   "_file": "/abs/path/扫描件.pdf"}
]
```

补录 payload **应一并携带 AI 三列**（业务键 `skills` / `ai_extract` / `ai_deep`，注意是 `ai_extract`
不是 `ai_structured`）；格式口径以 `skills/skills-analyze/references/subagent-prompt.md` 的输出规范为准
（ai_extract 固定 5 段、ai_deep 亮点/风险/建议、各 ≤200 字），此处只引用不复述全文。理由：补录时 agent
刚用视觉读完原件、信息最全，不必让 subagent 对着扫描件记录的空 full_text 保守猜；脚本写回时会同批打
`ai_refined_at` 出队标记——手析视同精析，后台精析周期永不再碰扫描件三列。

3. 交给脚本补录，**不要自己 `python3 -c` 调 API**——脚本会做和批量入库完全一致的
   字段校验、附件上传、MD5 去重与手机号回读（原件也会进表）：

```bash
python3 skills/resume-intake/scripts/upload_resumes.py --backfill /tmp/ocr.json
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

合法业务键见 README「表结构」；写错字段名（如 `gender`）会在报告 `failed` 里明确提示可用字段。
无法识别联系方式的文件不入库，脚本会拒绝，向用户说明原因。

## 上传后：三列精析走异步队列，不在本回合衔接

**上传环节不做任何 AI 推理，也不触发精析**：只解析、去重、写基础字段、传附件，写完表即结束。
报告里的 `refine_queued` 是当前待精析队列长度；三列（技能标签 / AI结构化提取 / AI深度解析）
由后台周期任务消费队列产出（prepare → 一波 subagent ≤20 → merge → apply，见
`skills/skills-analyze/SKILL.md`）。队列谓词唯一真源 `shared/refine_loop.py`
（`ai_refined_at` 为空 且 `full_text` 非空 = 在队列）。

- 上传回合**只需向用户说明 `refine_queued` 已入队**，不要自己跑精析流水线。
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
- 入库脚本的字段提取有已知偏差，回写前须逐人核对：姓名/院校可能吃进标签（如姓名变"毕业院校"）、
  期望职位会带"求职类型/期望薪资"尾巴、水印重的PDF可能整条手机号丢失、重复段落双写；
  发现即在 payload 里带上对应业务键一并修正（如 `"name": "方红亮", "school": "贵州大学"`）。
