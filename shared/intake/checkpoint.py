# -*- coding: utf-8 -*-
"""CheckpointStore：增量 checkpoint v2 的全部读写语义（P7 刀2）。

收拢 skills/resume-intake/scripts/intake_resume.py 里散落的 checkpoint 状态：
  doc       骨架 8 键（version/batch_id/out_dir/config_path/generated_at/
            done/done_md5/progress），键序即产物字节
  done      resume-skip map（**仅终批 upsert / 阶段8 回读确认后**填，15 键条目
            由 done_entry() 唯一构造——阶段8 逐条落盘与阶段9 终稿共用）
  progress  在飞 map（提取完成 / 附件已传，record_written=False；只作断点
            可见性，不参与跳过判定）
  done_md5  load() 时从 doc 建的 set 快照，此后**不再更新**（与原脚本一致）

落盘时机是硬契约（killcheck_p3.py 直接测），5 处增量 = **原子写**
（tmp + os.replace，persist()），终稿 = **非原子写**（write_final()）。
两者写法不一致是既有行为，不许顺手统一。终稿两步（finalize 的 update +
write_final 的落盘）由编排层夹在 IntakeReport.assemble 与 IntakeReport.write
之间/之后调用，保持原脚本的 now_iso() 取值顺序与「report → candidates →
checkpoint」文件写序。

本类不做任何跳过判定（判定属 Pipeline），不发 dws，不 print
（load() 的 checkpoint_loaded 提示行经注入的 console）。
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aitable.client import now_iso


def _write_json(path: Path, payload: Any) -> None:
    # 与编排层 _write_json 同口径（非原子、indent=2、ensure_ascii=False）；
    # 产物写入口径的 shared 归属是后续 ArtifactWriter 刀，此处不提前统一。
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """tmp + os.replace：进程在任意瞬间被 SIGKILL 也不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))


class CheckpointStore:
    def __init__(self, path: Path, batch_id: str, out_dir: Any,
                 config_path: Any) -> None:
        self.path = Path(path)
        self.batch_id = batch_id
        # progress = 尚未写库文件的中间状态（提取完成/附件已传），
        # P3 增量落盘用；done/done_md5 语义与旧版完全一致
        self.doc: Dict[str, Any] = {"version": 2,
                                    "batch_id": batch_id, "out_dir": str(out_dir),
                                    "config_path": str(Path(config_path).resolve()),
                                    "generated_at": now_iso(), "done": {}, "done_md5": [],
                                    "progress": {}}
        self.done_md5: Set[str] = set()

    # ------------------------------------------------------------------ #
    # 加载（契约 D12 幂等续跑；P3 起逐条落盘，格式 version=2）
    # ------------------------------------------------------------------ #
    def load(self, reset: bool, console: Any, warnings: List[str]) -> None:
        """重跑兼容：旧格式（无 version/progress）照常读 done/done_md5；整个文件
        读不懂（坏 JSON / 结构不认识）→ 告警并按空 checkpoint 处理，绝不崩溃。"""
        if self.path.exists() and not reset:
            try:
                old = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("done"), dict):
                    self.doc["done"] = old["done"]
                    self.doc["done_md5"] = old.get("done_md5") or []
                    if isinstance(old.get("progress"), dict):
                        self.doc["progress"] = old["progress"]
                    console.checkpoint_loaded(len(self.doc["done"]))
                else:
                    warnings.append("checkpoint.json 结构不认识（缺 done 映射，可能是更旧的"
                                    "格式或半截文件），本次按空 checkpoint 处理；已入库项靠"
                                    "手机号查重兜底，不会产生重复记录")
            except (ValueError, OSError) as exc:
                warnings.append("checkpoint.json 读取失败（%s），本次按全新批次处理" % exc)
        self.done_md5 = set(self.doc.get("done_md5") or [])

    @property
    def done(self) -> Dict[str, Any]:
        return self.doc["done"]

    # ------------------------------------------------------------------ #
    # 在飞 progress（record_written=False，只作断点可见性）
    # ------------------------------------------------------------------ #
    def mark_progress(self, ent: Dict[str, Any]) -> None:
        """阶段1 提取完成后：parse_status 落 progress 条目。"""
        md5 = ent["md5"]
        if md5 and md5 not in self.done_md5:
            prog = self.doc["progress"].setdefault(md5, {"md5": md5})
            prog.update({"file_name": ent["file_name"], "record_written": False,
                         "parse_status": ent["parse_status"], "at": now_iso()})

    def mark_attachment(self, ent: Dict[str, Any]) -> None:
        """阶段6 附件状态一确立就记 progress 条目（record_written 仍为 False）。"""
        md5 = ent["md5"]
        if md5 and md5 not in self.done_md5:
            prog = self.doc["progress"].setdefault(md5, {"md5": md5})
            prog.update({"file_name": ent["file_name"], "record_written": False,
                         "parse_status": ent["parse_status"],
                         "attachment_uploaded": ent["attachment_status"] == "uploaded",
                         "attachment_status": ent["attachment_status"],
                         "at": now_iso()})

    # ------------------------------------------------------------------ #
    # done 条目（resume-skip map）
    # ------------------------------------------------------------------ #
    def done_entry(self, ent: Dict[str, Any], prev: Dict[str, Any],
                   result_str: Optional[str] = None) -> Dict[str, Any]:
        """checkpoint.done 条目（v3 §9#6 语义）：「记录已写」与「附件已传」分开记状态，
        并存派生字段供跳过重跑时恢复 candidates.json 完整性。阶段 8 逐条落盘与阶段 9
        终稿共用本构造，保证两处内容一致。"""
        row = ent.get("row") or {}
        return {
            "md5": ent["md5"],
            "file_name": ent["file_name"],
            "record_written": True,
            "attachment_uploaded": ent["attachment_status"] == "uploaded",
            "record_id": ent.get("record_id") or prev.get("record_id"),
            "phone": row.get("phone") or prev.get("phone"),
            "dedupe": ent["dedupe"],
            "result": (result_str if result_str in ("新入库", "已覆盖")
                       else prev.get("result") or "已入库"),
            "attachment_status": ent["attachment_status"],
            "org_guess": ent.get("org_guess") or prev.get("org_guess"),
            "org_confidence": ent.get("org_confidence") or prev.get("org_confidence"),
            "category_guess": ent.get("category_guess") or prev.get("category_guess"),
            "skills": list(row.get("skills") or prev.get("skills") or []),
            "expected_location": row.get("expected_location") or prev.get("expected_location"),
            "at": now_iso(),
        }

    def mark_written(self, ent: Dict[str, Any], result_str: Optional[str]) -> None:
        """done 条目 upsert + done_md5 追加（阶段8 逐条 / 阶段9 终稿重建共用）。"""
        prev = (self.doc["done"] or {}).get(ent["md5"]) or {}
        self.doc["done"][ent["md5"]] = self.done_entry(ent, prev, result_str)
        if ent["md5"] not in self.doc["done_md5"]:
            self.doc["done_md5"].append(ent["md5"])

    def confirm_written(self, ent: Dict[str, Any], result_str: Optional[str]) -> None:
        """阶段8：写库状态一确认（回读到 record_id）就整条落盘（P3 增量 checkpoint）——
        之后任意瞬间被杀，重跑都能按 done 条目整条跳过 / 只补附件。"""
        self.mark_written(ent, result_str)
        self.doc["progress"].pop(ent["md5"], None)
        self.persist()

    def mark_fixup_uploaded(self, ent: Dict[str, Any]) -> None:
        """阶段6b：补传成功即刻落盘（P3 增量 checkpoint：附件状态一确立就持久化）。"""
        md5 = ent["md5"]
        if md5 and (self.doc["done"] or {}).get(md5):
            prev = self.doc["done"][md5]
            prev.update({"attachment_uploaded": True,
                         "attachment_status": "uploaded", "at": now_iso()})
            self.doc["progress"].pop(md5, None)
            self.persist()

    def merge_skipped(self, ent: Dict[str, Any], skills_default: List[str],
                      location_default: str) -> None:
        """纯跳过：把本次恢复/重算出的派生字段合并回旧条目（旧格式 checkpoint 就地升级）。
        skills_default / location_default 由编排层用与首次写库同一套纯函数算好传入。"""
        prev = self.doc["done"][ent["md5"]]
        prev.update({
            "record_written": bool(prev.get("record_written", True)),
            "attachment_uploaded": bool(prev.get("attachment_uploaded",
                                                 prev.get("attachment_status") == "uploaded")),
            "org_guess": prev.get("org_guess") or ent.get("org_guess"),
            "org_confidence": prev.get("org_confidence") or ent.get("org_confidence"),
            "category_guess": prev.get("category_guess") or ent.get("category_guess"),
        })
        if prev.get("skills") is None:
            prev["skills"] = skills_default
        if prev.get("expected_location") is None and ent["parse_status"] == "ok":
            prev["expected_location"] = location_default

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #
    def persist(self) -> None:
        """P3 增量 checkpoint：每份文件的「写库」「附件上传」状态一确立就整文件原子落盘
        （旧实现只在流程末尾写一次，被杀即全丢，N≈150 撞 120s 工具超时悬崖）。"""
        self.doc["generated_at"] = now_iso()
        self.doc["done_count"] = len(self.doc.get("done") or {})
        _write_json_atomic(self.path, self.doc)

    def finalize(self, ok: bool, summary: Dict[str, Any], dws_calls: int,
                 elapsed_ms: int) -> None:
        """终稿 update（键序即产物字节；done_md5 输出前 sorted）。落盘走 write_final()，
        由编排层夹在 IntakeReport.assemble / write 之间与之后调用（交错顺序是红线）。"""
        self.doc.update({
            "generated_at": now_iso(),
            "batch_id": self.batch_id,
            "ok": ok,
            "summary": summary,
            "dws_calls": dws_calls,
            "elapsed_ms": elapsed_ms,
            "done_count": len(self.doc["done"]),
            "done_md5": sorted(self.doc["done_md5"]),
        })

    def write_final(self) -> None:
        """终稿落盘：**非原子写**（与增量 5 处的原子写不一致是既有行为，保持原样）。"""
        _write_json(self.path, self.doc)
