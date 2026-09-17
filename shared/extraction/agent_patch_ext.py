# -*- coding: utf-8 -*-
"""agent 多模态兜底梯队（Tier 2）：`--apply-vision-patch` 补丁入链（P4a 通道 / P7 刀6）。

用户原方案图里 Tier 2 就是本梯队：**agent 多模态补丁是提取责任链的最后一环**，
不是编排层的一个过程式开关。P4a 落地时它散在 intake_resume.py（`_load_vision_patch`
+ `_agent_vision_entry` + 模块级 `_VISION_MERGER`），刀6 把它升格成 `TextExtractor`：

    Tier 1    PypdfExt / JxaExt / DocxZipExt / DocPieceExt   本机文本层
    Tier 1.5  VisionOcrExt                                   本机 OCR（仅 darwin）
    Tier 2    AgentPatchExt（本模块）                        agent 多模态补丁
    终态      ImageExt / 链耗尽                              no_text_layer（不进补丁）

受理条件（`can_handle`）与 P4a 编排层的触发条件逐字对应：
  * 补丁表里有本文件（按解析后绝对路径匹配，与编排层 `ent["path"]` 同一归一口径）；
  * 且链上已走过的梯队里**至少有一级是 no_text_layer**（跑通但没读到文字）——
    即链终态本来会是 `no_text_layer`。终态是 error / encrypted / garbled /
    unsupported 时一律不打补丁（原编排层只在 `ex["status"] == "no_text_layer"`
    分支调 `_agent_vision_entry`，其余分支直接判失败）。

注册位置是**红线**：必须排在链尾（ImageExt 之后）。`can_handle` 靠 `doc.prior`
判断「本机梯队全都没拿到可用文本」，而 prior 只含**已走过**的梯队——若把它插在
VisionOcrExt 之后，docx/doc/image 这些在链更后面才失败的 kind 走到本梯队时 prior
还是空的，补丁会被静默漏掉（P4a 通道对这些文件失效）。

不进链的兜底：`kind == "unknown"`（纯文本兜底路径）由门面直接处理、**不进链**，
其 no_text_layer 终态由编排层调 `merge_entry()` 走同一份合并实现（绝不合并两次：
结果按解析后绝对路径缓存）。

字段合并（主控裁决口径，逐字执行）：先对补丁 text 跑 RegexFieldExtractor，
**regex 有值的字段用 regex**，regex 为空才取 fields_draft（白名单见
fields.merger._DRAFT_FIELDS）；取自草稿的字段打 `field_source="agent_vision"`
并追加进 `needs_review`（编排层透传给 candidates.json，回合 2 用 evidence 复核）。
agent 只产出结构化补丁、**绝不写库**——写库仍走编排层正常查重/护栏/回读流程。

依赖方向说明（WHY）：本模块是 extraction 包里唯一向上依赖 fields 包的梯队，
因为补丁 schema 自带 `fields_draft`（文本 + 字段草稿一起来）。fields 包不回依赖
extraction（`fields/regex_ext.py` 对 detect_scanned 是函数内延迟 import），无环。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from documents import ExtractionResult, ResumeDocument
from extraction.base import TextExtractor
from fields.merger import FieldMerger
from fields.regex_ext import RegexFieldExtractor

__all__ = ["AgentPatchExt", "agent_patch_tier"]


def _read_patch_file(raw_path: str) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """P4a：读 agent 多模态兜底补丁 json（--apply-vision-patch）。

    补丁 schema（与 HOTPATH.md 逐字一致）：
        {"<文件绝对路径>": {"text": "...", "fields_draft": {"name": "...", "phone": "...", ...},
                             "confidence": 0.0, "notes": "..."}}
    返回 (按解析后绝对路径归一的补丁 dict, 错误消息或 None)。永不抛异常；
    非法条目直接忽略（宁缺勿造）。
    """
    try:
        raw = json.loads(Path(raw_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, "补丁文件读取失败：%s: %s" % (type(exc).__name__, exc)
    if not isinstance(raw, dict):
        return {}, '补丁顶层结构必须是对象 {"<文件绝对路径>": {...}}'
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(Path(str(k)).expanduser().resolve())] = v
        except (OSError, ValueError):
            continue
    return out, None


class AgentPatchExt(TextExtractor):
    """Tier 2：agent 多模态补丁梯队（backend=agent_vision）。

    持有状态（一次运行的进程级输入，与 extract_text._CHAIN 同生命周期）：
      entries  解析后绝对路径 -> 补丁条目（`load()` 装载；空表时 can_handle 恒 False，
               链行为与 P4a 之前逐字一致——B 侧 intake_job 共用同一条链，从不装补丁）
      merged   解析后绝对路径 -> 合并结果（`merge_entry()` 计算并缓存）
    """

    #: 记录链路里的 backend 名（LOCAL 裁判的 backend 字段会抓；不得改写）
    BACKEND = "agent_vision"

    def __init__(self, merger: Optional[FieldMerger] = None) -> None:
        self.merger = (merger if merger is not None
                       else FieldMerger([RegexFieldExtractor()]))
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.merged: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 补丁装载
    # ------------------------------------------------------------------ #
    def load(self, raw_path: str) -> Tuple[int, Optional[str]]:
        """装载补丁表。返回 (条目数, 错误消息或 None)；读失败/结构不认识 → 空表 + 错误。"""
        entries, err = _read_patch_file(raw_path)
        self.entries = entries
        self.merged = {}
        return len(entries), err

    @staticmethod
    def _key(path: Any) -> str:
        """补丁键归一：与编排层 `ent["path"] = str(Path(fp).expanduser().resolve())`
        同一口径（门面传进链的 doc.path 只 expanduser 未 resolve，这里补齐）。"""
        return str(Path(str(path)).expanduser().resolve())

    def entry(self, path: Any) -> Optional[Dict[str, Any]]:
        if not self.entries:
            return None
        return self.entries.get(self._key(path))

    def has_entry(self, path: Any) -> bool:
        return self.entry(path) is not None

    # ------------------------------------------------------------------ #
    # 链契约
    # ------------------------------------------------------------------ #
    def can_handle(self, doc: ResumeDocument) -> bool:
        if not self.entries:
            return False
        if self.entry(doc.path) is None:
            return False
        return any(getattr(r, "status", None) == "no_text_layer"
                   for r in (getattr(doc, "prior", None) or []))

    def extract(self, doc: ResumeDocument) -> ExtractionResult:
        merged = self.merge_entry(doc.path, doc.path.name)
        text = merged["text"]
        # npages 交给 chain 的 carry-forward（取已走过梯队里首个非零值，P1 评审裁决①）：
        # 补丁本身没有页数概念。
        return ExtractionResult(text, 0, "ok", self.BACKEND, [
            "agent 多模态兜底补丁接管提取：%d 字符，字段草稿已按「regex 优先、"
            "草稿补空」合并（取自草稿的字段 field_source=agent_vision 并进 needs_review）"
            % len(text)])

    # ------------------------------------------------------------------ #
    # 合并（链上 extract() 与编排层 unknown-kind 兜底路径共用同一实现）
    # ------------------------------------------------------------------ #
    def merge_entry(self, path: Any, file_name: str) -> Dict[str, Any]:
        """取补丁文本 + 合并字段草稿，返回 {"text", "fields", "notes"}。

        fields = CandidateFields.to_dict() 再挂 field_sources / needs_review 两键
        （P4a 口径：打标结果不进 to_dict，只在兜底通道上显式附加）。
        notes  = 合并说明（+ 补丁 text 为空时的重点复核提示），由编排层接在
                 字段告警之后落进 `ent["warnings"]`（顺序即产物字节）。
        结果按解析后绝对路径缓存：同一份文件绝不合并两次。
        """
        key = self._key(path)
        hit = self.merged.get(key)
        if hit is not None:
            return hit
        patch_entry = self.entries.get(key) or {}
        p_text = str(patch_entry.get("text") or "")
        draft = patch_entry.get("fields_draft")
        drafts = [draft] if isinstance(draft, dict) else []
        cf = self.merger.resume_fields_with_drafts(p_text, file_name, drafts)
        f = cf.to_dict()
        f["field_sources"] = dict(cf.field_sources)
        f["needs_review"] = list(cf.needs_review)
        adopted = list(cf.needs_review)
        note = "agent 多模态兜底补丁已合并（backend=agent_vision"
        if patch_entry.get("confidence") is not None:
            note += "，confidence=%s" % patch_entry.get("confidence")
        note += "）"
        if adopted:
            note += ("；字段[%s]取自 agent 草稿（field_source=agent_vision），"
                     "已标 needs_review，回合 2 必须用 evidence 原文复核"
                     % "、".join(adopted))
        if patch_entry.get("notes"):
            note += "；agent 补丁备注：%s" % str(patch_entry.get("notes"))[:200]
        notes = [note]
        if not p_text.strip():
            notes.append("agent 补丁 text 为空——仅 fields_draft 生效，"
                         "简历全文留空，回合 2 请重点复核")
        payload = {"text": p_text, "fields": f, "notes": notes}
        self.merged[key] = payload
        return payload


#: 链上 Tier 2 的**唯一实例**：extract_text._CHAIN 注册的就是它，编排层经
#: `agent_patch_tier()` 取同一实例装补丁——避免「链上一张补丁表、编排层另一张」。
_TIER = AgentPatchExt()


def agent_patch_tier() -> AgentPatchExt:
    return _TIER
