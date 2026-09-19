#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / shared / extract_fields.py
=====================================================

纯正则 / 启发式字段抽取层的**薄门面**。**零第三方依赖**，只用标准库。

对外契约（签名已冻结，不得改动）
------------------------------------------------
    extract_resume_fields(text: str, file_name: str) -> dict
    extract_job_fields(text: str, file_name: str) -> dict

实现在 `shared/fields/**`，加一路抽取器 = 加一个 `FieldExtractor` 子类 +
在 `_MERGER` 的列表里挂一项。
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
# 抽取器编排（只有正则一路；agent 兜底 = 往这个列表尾部再挂一项）
# --------------------------------------------------------------------------- #
_MERGER = FieldMerger([RegexFieldExtractor()])


def extract_resume_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从简历纯文本 + 文件名抽字段。签名已冻结。

    **永不抛异常**：text 为空/水印/乱码时只回填文件名里确凿的信息，其余一律
    留空（不硬造字段）。返回结构见 `fields.candidate.CandidateFields.to_dict`。
    """
    return _MERGER.resume_fields(text, file_name).to_dict()


def extract_job_fields(text: str, file_name: str) -> Dict[str, Any]:
    """从岗位说明书纯文本 + 文件名抽字段。签名已冻结。**永不抛异常**。

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
