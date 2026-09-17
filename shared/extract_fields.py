#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / extract_fields.py
=====================================================

纯正则 / 启发式字段抽取层的**薄门面**（P2 OO 重构后）。**零第三方依赖**，只用标准库。

对外契约（构建契约 §3.1，签名已冻结，不得改动）
------------------------------------------------
    extract_resume_fields(text: str, file_name: str) -> dict
    extract_job_fields(text: str, file_name: str) -> dict

架构（P2，与 P1 的 `extraction/` 责任链同构）
--------------------------------------------
本文件只保留：门面函数（委托 + `to_dict()` 组装）、公开原语的 re-export、CLI。
实现在 `shared/fields/**`：

    fields/base.py       FieldExtractor ABC —— 抽取器契约（P4 的 agent 抽取器挂点）
    fields/textnorm.py   normalize_text / split_items / slice_sections（简历侧与 JD 侧共用）
    fields/lexicon.py    985/211/双一流、证书、技能词表 + school_rank / degree_to_enum
    fields/regex_ext.py  RegexFieldExtractor —— 全部正则群与启发式（原 2149 行的本体）
    fields/candidate.py  CandidateFields  —— 简历侧 19 业务字段 + 每字段 source + warnings
    fields/job.py        JobFields        —— JD 侧业务字段 + source + warnings
    fields/merger.py     FieldMerger      —— 多源合并（P2 只走单源，P4 接 agent 兜底）

加一路抽取器 = 加一个 `FieldExtractor` 子类 + 在 `_MERGER` 的列表里挂一项。

设计要点（针对前序调研暴露的系统性缺陷逐条修；实现细节的标定依据在 fields/** 各模块）
----------------------------------------------------------------------------------
1. **姓名必须有文件名兜底**（任务书要求）。客户简历文件名高度规律
   （`姓名-岗位.pdf` / `个人简历-姓名-方向.docx` / `【岗位_地点 薪资】姓名 年限.pdf` /
   `岗位-姓名.docx`），比 PDF 文本层可靠得多：
     - `林云-电池设备.pdf` 正文里是 `林之府`（PDF 字体伪影）→ 文件名兜底纠正
     - `姓名：徐志伟性别：男`（无分隔符粘连）→ 正文模式会误抽 `徐志伟性`
     - `姓 名：许金 措施；`（双栏 PDF 串行）→ 正文模式会误抽 `许金措施`
   策略：文件名候选与正文候选**双向校验**，冲突时以文件名为准并在 warnings 里留痕。
2. **手机号优先用带数字边界断言的严格正则**。前序调研「去掉边界断言后 100%」
   的结论建立在 pdfplumber 被污染的文本上（strip-all-tags 会把 XML 属性里的
   长数字串漏进正文，例如 `5118106325870`、`-13081007195820`）。本层配合
   extract_text 的干净提取后，严格正则在 28/28 份有效简历上全中；仍保留
   「去边界断言」作为二级兜底，并把置信度降为 low。
3. **证书必须区分「持证者优先」与硬性门槛**（任务书要求，会造成误杀）。
   `cert_is_preferred_not_required=True` 表示检出的证书**全部**出现在
   「…者优先 /（优先）/ 加分 / 更佳」语境里，上层不得当硬性门槛用。
4. **技能列表必须多分隔符切分并去重去空**（任务书要求，会造成打分分母=1、
   命中即 100 分虚高）。`split_items()` 按 `、，,;；/ 空格 •·|` 等切分，
   JD 侧与简历侧共用同一个切分器。
5. **PDF 字体伪影归一化**：部分 PDF 文本层用「康熙部首 / CJK 部首补充」码位
   代替汉字（`⾦⽓⾃⼤⻘⻛⻓⻋⻔⻆⻄`），实测 50 份样本里 144 处。
   先做 NFKC（修掉 133 处），再用补充映射表修掉剩余 11 处。不归一化会直接
   漏掉 `最高学历：⼤专` 这类字段。
6. **邮箱缺 @ 的原文错字做保守修复**：`邮箱：1873259717qq.com`（原件 XML 里
   @ 确实不存在，count=0）→ 仅在「本地部分是 5 位以上数字 + 已知邮箱服务商
   域名」时补 @，并把该字段置信度标 low。不做其它臆测。

兼容性：D10 —— 全程 pathlib；语法兼容 python 3.8+（不用 match、不用运行时
`X | None`，只用 typing.Optional）。已在 3.9.6 与 3.14.0 实测。
"""

from __future__ import annotations  # noqa: F404

from pathlib import Path
from typing import Any, Dict, List

from fields.lexicon import school_rank  # noqa: F401
from fields.merger import FieldMerger
# 下面这批正则原语过去就是 extract_fields 的模块级公开名（虽然 __all__ 只列 5 个），
# 门面照旧 re-export，`from extract_fields import extract_phone` 这类用法不受影响。
from fields.regex_ext import (  # noqa: F401
    RegexFieldExtractor, estimate_years_from_dates,
    estimate_years_from_graduation, estimate_years_from_section, extract_email,
    extract_phone, extract_years, name_from_filename, name_from_text,
    parse_education_block, text_unusable)
from fields.textnorm import normalize_text, slice_sections, split_items  # noqa: F401

__all__ = [
    "extract_resume_fields",
    "extract_job_fields",
    "split_items",
    "name_from_filename",
    "normalize_text",
]

# --------------------------------------------------------------------------- #
# 抽取器编排（P2 只有正则一路；P4 的 agent 兜底 = 往这个列表尾部再挂一项）
# --------------------------------------------------------------------------- #
_MERGER = FieldMerger([RegexFieldExtractor()])


def extract_resume_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从简历纯文本 + 文件名抽字段。契约 §3.1 冻结签名。

    **永不抛异常**：text 为空/水印/乱码时只回填文件名里确凿的信息，其余一律
    留空（契约 D11：不硬造字段）。返回结构见 `fields.candidate.CandidateFields.to_dict`。
    """
    return _MERGER.resume_fields(text, file_name).to_dict()


def extract_job_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从岗位说明书纯文本 + 文件名抽字段。契约 §3.1 冻结签名。**永不抛异常**。

    返回结构见 `fields.job.JobFields.to_dict`。
    """
    return _MERGER.job_fields(text, file_name).to_dict()


# =========================================================================== #
# CLI（自测用）
# =========================================================================== #
def _main(argv: List[str]) -> int:
    import json
    import sys as _sys
    if len(argv) < 2:
        print("usage: extract_fields.py <resume|job> <text_file> [file_name]",
              file=_sys.stderr)
        return 2
    mode, path = argv[0], argv[1]
    fname = argv[2] if len(argv) > 2 else Path(path).name
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fn = extract_resume_fields if mode == "resume" else extract_job_fields
    print(json.dumps(fn(text, fname), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:]))
