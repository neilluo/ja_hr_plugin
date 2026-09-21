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
python3 scripts/upload_resumes.py <目录>            # 真实入库
python3 scripts/upload_resumes.py <目录> --dry-run  # 预演
```

一条命令跑完全链路，读 stdout 的 JSON 报告即可，**不要拆步骤、不要自己调 API**。

## 报告字段与处置

| 字段 | 处置 |
|---|---|
| `created` / `readback_missing` | missing 非空 = 失败，重跑同目录即可（幂等） |
| `skipped_dup` | 正常：MD5 或手机号已存在，向用户说明即可 |
| `failed` | 看 error 文本；附件类错误重跑可恢复 |
| `needs_ocr` | 扫描件/图片，按下节补录 |

## 扫描件补录（agent 唯一需要动脑的环节）

1. 用视觉能力读取 `needs_ocr` 里的每个文件（Read 工具直接看图/扫描 PDF）。
2. 抽出姓名/手机号/学历/技能等字段后，用 Python 一行补录：

```bash
python3 -c "
import sys; sys.path.insert(0,'shared')
from notable import Notable
nt = Notable()
print(nt.create_records('resume', [{'name':'张三','phone':'138...','education':'本科',
      'comm_status':'待筛选'}]))"
```

3. 补录后 `python3 scripts/query.py resume --filter phone=<手机号>` 确认。
   无法识别联系方式的文件不入库，向用户说明原因。

## 边界

- 支持 pdf/docx/doc/png/jpg；其他格式报 failed。
- 单目录可重复跑：MD5+手机号双重去重，不会写重复记录。
- 字段口径见 README「表结构」；枚举值（学历/分类/沟通状态）服务端自动补建选项。
