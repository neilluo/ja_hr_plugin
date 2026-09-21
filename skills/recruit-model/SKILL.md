---
name: recruit-model
description: 招聘智能匹配的共享知识库：四表结构、字段口径、匹配打分规则、解析方法、执行纪律。其他 recruit-* skill 引用本 skill 的 references，不重复定义口径。Use when 需要查表结构/打分口径/解析规则，或其他 recruit skill 要求先读本 skill。
argument-hint: (知识库，直接读 references/)
argument-hint-en: (knowledge base, read references/)
argument-hint-zh: (知识库，直接读 references/)
name_en: Recruit Model
name_zh: 招聘匹配知识库
description_en: Shared knowledge base for the recruitment suite: table schema, field semantics, scoring rules, parsing methods, execution discipline.
description_zh: 招聘套件共享知识库：表结构、字段口径、打分规则、解析方法、执行纪律。
user-invocable: false
author:
  name: QwenWork
  url: https://qwenwork.cn
---

# 招聘匹配知识库（OpenAPI 直连版）

本 skill 不含可执行流程，只定义口径。执行入口见 resume-intake / job-intake /
candidate-query / match-verify 四个 skill。

## 引用

| 文档 | 内容 |
|---|---|
| `references/system-config.md` | 四表结构、业务键↔中文字段名、凭证与权限点 |
| `references/parsing-methods.md` | 简历/JD 文本提取与字段抽取方法、扫描件判据 |
| `references/ai-analysis-spec.md` | 匹配打分公式、推荐阈值、证据格式 |
| `references/execution-notes.md` | 执行纪律：幂等、附件先行、回读、限流、并发 |

## 三条铁律

1. 代码是唯一事实来源：口径以 `shared/*.py` + `skills/*/scripts/*.py` 为准，文档与代码冲突时信代码。
2. 所有远端调用走 `shared/notable.py`，禁止脚本里直接 urllib 调钉钉（OSS PUT 除外）。
3. 凭证只在 `.secrets.json` 或环境变量；仓库 public，任何凭证不得进 git。
