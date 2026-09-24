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

## 扫描件补录（agent 只读图给字段，入库仍走脚本）

1. 用视觉能力读取 `needs_ocr` 里的每个文件（Read 工具直接看图/扫描 PDF）。
2. 把每个文件抽出的字段 + **原文件绝对路径**写成一个 JSON 数组文件（如 `/tmp/ocr.json`）：

```json
[
  {"name": "张三", "phone": "13800000000", "email": "z@x.com",
   "education": "本科", "school": "XX大学", "major": "机械工程",
   "years_experience": 5, "expected_position": "设备工程师",
   "skills": ["PLC", "CAD"], "_file": "/abs/path/扫描件.pdf"}
]
```

3. 交给脚本补录，**不要自己 `python3 -c` 调 API**——脚本会做和批量入库完全一致的
   字段校验、附件上传、MD5 去重与手机号回读（原件也会进表）：

```bash
python3 skills/resume-intake/scripts/upload_resumes.py --backfill /tmp/ocr.json
```

以上命令以仓库根为 CWD；scripts/ 下入口为执行（run）而非阅读。

合法业务键见 README「表结构」；写错字段名（如 `gender`）会在报告 `failed` 里明确提示可用字段。
无法识别联系方式的文件不入库，脚本会拒绝，向用户说明原因。

## 上传后自动衔接：三列交给「简历AI精析」并发流水线

**上传环节本身不做任何 AI 推理**：只解析、去重、写基础字段、传附件。上传脚本一跑完，
立即自动调用 `skills-analyze`（同一回合内并发 subagent，不分步问用户），由它产出三列：
技能标签 / AI结构化提取 / AI深度解析。

- 三列**无条件逐人精析**：不按硬门槛筛人。简历库是人才池，不达标者照样分析、照样保留；
  是否进入智能匹配表由「智能匹配」的门槛判定决定。
- 脚本的词表命中与平台AI字段都不算结果，必须由 subagent 读全文推理得出；
  技能标签要避开"Excel/成本/团队合作"这类无区分度词。
- 只有零散修正或扫描件补录时才用手工通道：`skills/skills-analyze/scripts/sync_ai_columns.py payload.json`
  （该脚本会带 id 全量回传选项、按手机号回写，缺手机号时按姓名兜底）。

> 表内「AI结构化提取」「AI深度解析」两列已由用户改为**普通文本列**，平台不再自动计算，内容以精析流水线写入为准。

## 边界

- 支持 pdf/docx/doc/png/jpg；其他格式报 failed。
- 单目录可重复跑：MD5+手机号双重去重，不会写重复记录。
- 字段口径见 README「表结构」；枚举值（学历/分类/沟通状态）服务端自动补建选项。
- 入库脚本的字段提取有已知偏差，回写前须逐人核对：姓名/院校可能吃进标签（如姓名变"毕业院校"）、
  期望职位会带"求职类型/期望薪资"尾巴、水印重的PDF可能整条手机号丢失、重复段落双写；
  发现即在 payload 里带上对应业务键一并修正（如 `"name": "方红亮", "school": "贵州大学"`）。
