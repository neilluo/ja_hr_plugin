# -*- coding: utf-8 -*-
"""JobDocParser：Turn 1 阶段 1 的单份 JD 解析（文件 IO 边界）。

红线：
  * B 侧**没有** agent 多模态兜底通道——PARSE_FAIL_REASON 只有 5 键（A 侧 6 键，
    多一个 needs_agent_vision），扫描件直接 result="失败"。**禁止**把 A 侧的
    VisionPatchChannel / AgentPatchExt「顺手」接到 B 上。
  * B 侧提取是**串行**的（A 侧并发 4）；本类只管单份解析，批量循环与计时留在
    Pipeline（不引入并发 = 不改变行为）。
  * ent 骨架 14 键的键序、失败 reason 的拼接顺序（基础文案 → 扩展名 → 技术细节）
    逐字保持——reason 直接进 rows → stdout 清单与 intake_report.json。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from extract_fields import extract_job_fields       # noqa: E402
from extract_text import detect_scanned, extract_text  # noqa: E402
from jobintake.constants import PARSE_FAIL_REASON
from jobintake.textutil import clean

__all__ = ["JobDocParser"]


class JobDocParser:
    """单份 JD：提取文本 → 扫描件判定 → 正则预填，产出 entry 骨架。"""

    def parse_one(self, i: int, fp: str) -> Dict[str, Any]:
        p = Path(fp).expanduser()
        fname = p.name
        ex = extract_text(str(p))
        ent: Dict[str, Any] = {
            "seq": i + 1, "file_name": fname, "path": str(p.resolve()),
            "size": ex.get("size") or 0, "kind": ex.get("kind"),
            "parse_status": ex.get("status"), "text": ex.get("text") or "",
            "fields": None, "result": None, "reason": None, "dedupe": "new",
            "attachment_status": "deferred", "record_id": None, "writable": False,
            "warnings": [],
        }
        if ex.get("status") != "ok":
            ent["result"] = "失败"
            reason = PARSE_FAIL_REASON.get(ex.get("status"), PARSE_FAIL_REASON["error"])
            if ex.get("status") == "unsupported":
                reason = "%s（扩展名 %s）" % (reason, ex.get("ext") or p.suffix or "(无)")
            if ex.get("error"):
                reason = "%s（技术细节：%s）" % (reason, str(ex["error"])[:120])
            ent["reason"] = reason
        elif detect_scanned(ent["text"], ent.get("kind") or ""):
            ent["parse_status"] = "no_text_layer"
            ent["result"] = "失败"
            ent["reason"] = PARSE_FAIL_REASON["no_text_layer"]
        else:
            f = extract_job_fields(ent["text"], fname)
            ent["fields"] = f
            ent["warnings"] = list(f.get("warnings") or [])
            if not clean(f.get("job_name")):
                ent["result"] = "失败"
                ent["reason"] = ("未能解析出岗位名称，无法查重与入库；"
                                 "请人工确认 JD 里的「职位名称 Position:」后补录")
            else:
                ent["writable"] = True
        return ent
