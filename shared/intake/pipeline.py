# -*- coding: utf-8 -*-
"""IntakePipeline：简历入库 Turn 1 的编排层。

10 个阶段（阶段 1~9 + 6b）+ 序幕 + 收尾各成一个方法，跨阶段长寿命状态全部提升为
实例属性；脚本侧只剩 CLI 装配（build_parser / main / auto_match）。
"""

from __future__ import annotations

import json
import random
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aitable.client import DwsError                 # noqa: E402
from aitable.values import sanitize_text            # noqa: E402
from dedupe.base import UNSET                       # noqa: E402
from dedupe.content_hash import (                   # noqa: E402
    ATTACH_MD5_FIELD_KEY,
    ContentHashDeduper,
)
from dedupe.name_size import NameSizeDeduper        # noqa: E402
from dedupe.phone import PhoneDeduper               # noqa: E402
from extract_fields import extract_resume_fields    # noqa: E402
from extract_text import detect_scanned             # noqa: E402
from extraction.agent_patch_ext import AgentPatchExt, agent_patch_tier  # noqa: E402
from fields.identity import IDENTITY_EVIDENCE_KEYS, identity_evidence  # noqa: E402
from intake.budget import WallBudget                # noqa: E402
from intake.checkpoint import CheckpointStore       # noqa: E402
from intake.console import IntakeConsole            # noqa: E402
from intake.extraction_runner import ExtractionRunner  # noqa: E402
from intake.readback import ReadBackVerifier        # noqa: E402
from intake.report import IntakeReport              # noqa: E402
from intake.table_gateway import TableGateway       # noqa: E402
from runtime_compat import default_out_root         # noqa: E402

from jsonio import write_json as _write_json          # noqa: E402

from pipeline_base import PipelineBase               # noqa: E402

__all__ = ["IntakePipeline", "WALL_BUDGET_DEFAULT", "UPLOAD_CONCURRENCY"]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 简历全文写入上限（字符）。安全余量，不会截掉任何真实简历。
FULL_TEXT_MAX = 20000
#: 附件并发度（API 限 20 QPS，5 留足余量）
UPLOAD_CONCURRENCY = 5
#: 提取/OCR 并发度：多份扫描件并行 Vision OCR；上限 4，与附件并发 5 互不相干。
EXTRACT_CONCURRENCY = 4
#: --wall-budget 默认秒数：必须小于 agent 工具 120s 超时，留出 graceful 停止
#: （落 checkpoint + 打印 RESUME + 写报告）与提交尾段（upsert+回读）的余量。
WALL_BUDGET_DEFAULT = 100.0
#: 每个候选人写入「技能标签」的上限（多选字段，避免选项池被噪声撑爆）
SKILLS_MAX = 40
#: evidence 各段长度上限（字段级全量 + 工作经历正文截断，不是「取前 N 字」）
EVIDENCE_LIMITS = {"education_text": 4000, "cert_text": 4000,
                   "skill_text": 4000, "work_text": 1500}
#: 老插件每份简历的 agent 工具回合数（20~40，取中位数）→ 用于 turns_saved_estimate
OLD_TURNS_PER_FILE = 25
#: 本脚本自己占的 agent 回合数（Turn 1 = 1 次脚本调用）
NEW_TURNS = 1
#: 库内去重扫描的翻页上限（100 页 × 100 条/页 ≈ 10000 条）。存量打满就会截断，
#: 而去重是按「扫回来的这批」判的 → 截断即漏判重复，所以必须报出来（缺陷2）。
DEDUPE_SCAN_MAX_PAGES = 100
#: 老库容忍：简历库没有「附件内容MD5」字段（config.json 的 fields.resume.attach_md5
#: 缺失 / 表里没建这一列）时，库内去重自动回退 (文件名, 字节大小) 并给这一条 warning。
#: **不得崩溃、不得自建字段**——建字段是 replicate 部署时的事，脚本只如实说明怎么启用。
OLD_LIB_DEDUPE_WARNING = (
    "当前简历库没有「附件内容MD5」字段（config.json 的 fields.resume.attach_md5 缺失），"
    "库内附件去重**未启用内容级比对**，已回退到「文件名+字节大小」：同一文件名同一大小、"
    "内容改过一版重投会被误判成重复跳过；同一份内容换个文件名会漏判（靠手机号查重兜底）。"
    "启用方法：在简历库管理表加一个 text 字段「附件内容MD5」，把它的 fieldId 写进 "
    "config.json 的 fields.resume.attach_md5（并同步 field_names/types），重跑即生效；"
    "存量记录会在被覆盖更新/补传附件时自动回填哈希（脚本绝不自建字段）")
#: 20% 闸门（用户拍板）：needs_agent_vision 份数 / 总份数 > 此比例 →
#: 本轮不写任何记录，报告 reason="vision_gate"。
#: 闸门只拦「本轮写入」，**不拦补救**：VISION_NEEDED 清单照常打印，agent 打完
#: 补丁重跑同一命令即可入库。补丁之后仍读不出的才是真正的整批格式问题。
VISION_GATE_RATIO = 0.20
#: 闸门最小分母：不足此份数不判比例。热路径是「1 份或几份」，单份
#: 扫描件按比例算必然 100% 触发；而 agent 一轮就能补完这么几份，拦下来纯属卡死
#: 非 macOS（无本机 OCR）用户，没有任何安全收益。
VISION_GATE_MIN_ENTRIES = 5
#: 6b 补传附件（只补附件、记录不重建）单轮处理上限（主控裁决回写）：
#: 超出部分 defer 到下一轮 RESUME，防大批量补传把墙钟拖爆。
FIXUP_ROUND_MAX = 100

# 组织归属关键词（老插件口径，见 v0.1.0/skills/recruit-model/references/system-config.md:28
# 与 resume-intake/SKILL.md:24）
ORG_MFG_KEYWORDS = ("制造基地", "厂务", "设备", "EHS", "暖通", "电气", "工艺",
                    "单晶", "硅片", "组件", "电池")
ORG_FUNC_KEYWORDS = ("财务", "行政", "人力", "数据信息", "成本会计")

# 简历库分类关键词（老插件口径 resume-intake/SKILL.md:27）
CAT_TECH = ("制造", "设备", "工艺", "EHS", "暖通", "电气", "单晶", "硅片", "组件",
            "电池", "MES", "工程师", "技术", "生产", "质量", "厂务", "动力", "运维",
            "安全", "环保", "拉晶", "切片", "镀膜", "焊接", "层压")
CAT_PRODUCT = ("产品经理", "产品专员", "产品设计", "产品运营", "产品")
CAT_MARKET = ("市场", "销售", "营销", "品牌", "渠道", "商务", "客户")
CAT_OPS = ("运营", "供应链", "物流", "仓储", "计划")
CAT_OTHER = ("财务", "会计", "审计", "税务", "出纳", "行政", "人力", "人事", "法务",
             "薪酬", "招聘")

#: 期望地点兜底值（老插件铁律「没有明确地点一律填『不限』」）
LOCATION_FALLBACK = "不限"
#: 沟通状态默认值（老插件 resume-intake/SKILL.md:8）
COMM_STATUS_DEFAULT = "待筛选"
#: 简历库分类默认值
CATEGORY_DEFAULT = "技术类"

#: parse_status -> 业务话（给用户看的，不是技术错误码）
PARSE_FAIL_REASON = {
    "no_text_layer": "扫描件/图片无文字层，且本机 OCR 未能救回（非 macOS 无 OCR 能力，"
                     "或 OCR 文本未通过可信护栏：仍是水印/重复串/无数字字符）；"
                     "请提供 Word 或 PDF 文字版简历（不硬造字段）",
    "needs_agent_vision": "本机读不出文字（扫描件/图片无文字层且 OCR 不可用或不可信），"
                          "已列入 VISION_NEEDED 清单等 agent 多模态兜底；本轮未提供"
                          "覆盖该文件的 --apply-vision-patch 补丁，如实不入库（不硬造字段）。"
                          "agent 读清单内文件产出补丁后重跑同一命令即可入库",
    "garbled": "文本层疑似乱码/编码错位，无法可靠解析；请提供文字版简历或转人工核对原件",
    "encrypted": "文件加密或无读取权限，无法解析；请提供未加密的文字版简历",
    "unsupported": "不支持的文件格式，无法解析；请提供 PDF/Word(docx/doc) 文字版简历",
    "error": "文件解析失败；请确认文件完整后重新提供",
}

#: candidates[] 元素必备字段（digest.json 的 candidates 数组元素，字段完全一致）
CANDIDATE_FIELDS = (
    "key", "record_id", "file_name", "name", "phone", "education", "school",
    "school_rank", "major", "years_experience", "certificates", "skills",
    "expected_position", "expected_location", "org_guess", "org_confidence",
    "category_guess", "parse_status", "dedupe", "attachment_status", "evidence",
)
#: 额外透传的工作年限来源（text|filename|estimated）；
#: needs_review（agent 兜底草稿字段复核清单）与 field_sources（逐字段来源）
#: email（身份阀判据+原文行）、name_source / parse_backend（姓名复核判据）
CANDIDATE_EXTRA_FIELDS = ("years_source", "needs_review", "field_sources",
                          "email", "name_source", "parse_backend")

EVIDENCE_KEYS = ("education_text", "cert_text", "work_text", "skill_text")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _new_batch_id() -> str:
    return "%s-%04x" % (time.strftime("%Y%m%d-%H%M%S"), random.getrandbits(16))


def _resume_cmd() -> str:
    """RESUME: 行内容 = 原命令原样重跑（脚本路径换成绝对路径，防换 cwd 后失效）。"""
    argv = [str(Path(sys.argv[0]).resolve())] + list(sys.argv[1:])
    return shlex.join(argv)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _clean(s: Any) -> Optional[str]:
    """None/空串归一成 None（format_cell 也会丢空串，这里提前统一，便于判定）。"""
    if s is None:
        return None
    if isinstance(s, str):
        s = s.strip()
        return s or None
    return s


def _count_hits(hay: str, keywords: Sequence[str]) -> int:
    if not hay:
        return 0
    low = hay.lower()
    return sum(low.count(k.lower()) for k in keywords if k)


def _join(items: Sequence[str], sep: str = "、", limit: int = 0) -> Optional[str]:
    out = [str(x).strip() for x in (items or []) if x and str(x).strip()]
    if not out:
        return None
    s = sep.join(out)
    return s[:limit] if limit else s


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    t = str(text)
    if limit and len(t) > limit:
        return t[:limit] + "…"
    return t


def guess_org(file_name: str, position: Optional[str], text: str) -> Tuple[Optional[str], str, Dict[str, int]]:
    """组织归属预判（老插件关键词口径）。

    返回 (org_guess, org_confidence, 命中计数)。

    两段式，**只有干净命中才给 high**：
      ① 文件名 + 期望岗位（信号最强：`【财务主管_曲靖】许金` / `工艺工程师-张彬彬`）；
      ② 正文（工作经历段 + 技能段 + 全文前 4000 字）。
    两段都出现「制造/职能两族同时命中」或「一族都没命中」→ org_confidence="low"，
    org_guess 只给多数派当**提示**（可能为 None），脚本**不写库**，留给 Turn 2 的 LLM
    用 evidence 定夺（老插件铁律：判不了问用户，不猜）。
    """
    stage1 = "%s %s" % (file_name or "", position or "")
    m1 = _count_hits(stage1, ORG_MFG_KEYWORDS)
    f1 = _count_hits(stage1, ORG_FUNC_KEYWORDS)
    counts = {"stage1_mfg": m1, "stage1_func": f1}
    if m1 and not f1:
        return "制造中心", "high", counts
    if f1 and not m1:
        return "职能中心", "high", counts

    stage2 = "%s\n%s" % (stage1, (text or "")[:4000])
    m2 = _count_hits(stage2, ORG_MFG_KEYWORDS)
    f2 = _count_hits(stage2, ORG_FUNC_KEYWORDS)
    counts.update({"stage2_mfg": m2, "stage2_func": f2})
    if m2 and not f2:
        return "制造中心", "high", counts
    if f2 and not m2:
        return "职能中心", "high", counts
    if m2 or f2:
        hint = "制造中心" if m2 > f2 else ("职能中心" if f2 > m2 else None)
        return hint, "low", counts
    return None, "low", counts


def guess_category(file_name: str, position: Optional[str], text: str) -> str:
    """简历库分类预判：技术类/产品类/市场类/运营类/其他，默认技术类（老插件口径）。"""
    for hay in ("%s %s" % (file_name or "", position or ""),
                "%s %s %s" % (file_name or "", position or "", (text or "")[:3000])):
        if _count_hits(hay, CAT_TECH):
            return "技术类"
        if _count_hits(hay, CAT_PRODUCT):
            return "产品类"
        if _count_hits(hay, CAT_MARKET):
            return "市场类"
        if _count_hits(hay, CAT_OPS):
            return "运营类"
        if _count_hits(hay, CAT_OTHER):
            return "其他"
    return CATEGORY_DEFAULT


def normalize_location(raw: Optional[str], known: Sequence[str]) -> Tuple[str, bool]:
    """期望地点归一（老插件铁律）。

    返回 (写入值, 是否兜底)。规则：
      * 抽到单值且是已知选项/干净短词 → 原值；
      * 抽到多值（`安徽、河北、河南`）或空 → 「不限」（singleSelect 存不下多城市）。
    """
    s = _clean(raw)
    if not s:
        return LOCATION_FALLBACK, True
    if re.search(r"[、,，/|;；]", s):
        return LOCATION_FALLBACK, True
    if len(s) > 8 or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{1,8}", s):
        return LOCATION_FALLBACK, True
    if s in set(known or ()):
        return s, False
    return s, False          # 新城市交给 ensure_options 追加（只增不删）


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
class IntakePipeline(PipelineBase):
    """一个进程内做完全部确定性工作、零 agent 回合的简历入库编排（Turn 1）。

    `gateway.tbl` 可为 None（config 失败但不致命）：全流程 12 处 `has_table` 门控
    据此判定，报告照产、退出码由 ok 决定。
    """

    def __init__(self, args: Any, console: Optional[IntakeConsole] = None) -> None:
        self.args = args
        self.console = console if console is not None else IntakeConsole()
        self.budget = WallBudget(args, WALL_BUDGET_DEFAULT)
        self.runner = ExtractionRunner(EXTRACT_CONCURRENCY, self.console)
        #: 链上 Tier 2 的唯一实例（extract_text._CHAIN 注册的就是它）
        self.agent_patch: AgentPatchExt = agent_patch_tier()

        # ---- 跨阶段状态（prepare 里定值；此处只声明，便于阅读生命周期）----
        self.batch_id = ""
        self.out_dir: Optional[Path] = None
        self.report_path: Optional[Path] = None
        self.candidates_path: Optional[Path] = None
        self.checkpoint_path: Optional[Path] = None
        self.files: List[str] = []
        self.entries: List[Dict[str, Any]] = []
        self.to_write: List[Dict[str, Any]] = []
        self.fixups: List[Dict[str, Any]] = []
        self.rows: List[Dict[str, Any]] = []
        self.candidates: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.summary: Dict[str, int] = {}
        self.all_skills: List[str] = []
        self.all_locations: List[str] = []
        self.known_locs: List[str] = []
        self.vision_needed: List[Dict[str, Any]] = []
        self.vision_needed_paths: List[str] = []
        self.vision_gated = False
        self.need_lib_scan = False
        self.attach_md5_field: Optional[str] = None
        self.extract_ms = 0
        #: budget 与 vision gate 共用的「不进任何 dws 写阶段」标志
        self.halted = False
        self.fatal: Optional[str] = None
        self.gateway: Optional[TableGateway] = None
        self.readback: Optional[ReadBackVerifier] = None
        self.store: Optional[CheckpointStore] = None
        self.report: Optional[IntakeReport] = None
        self.ok = False

    # ------------------------------------------------------------------ #
    # 状态与所有权
    # ------------------------------------------------------------------ #
    @property
    def has_table(self) -> bool:
        """`tbl is not None` 门控的唯一读点（不再有 tbl 局部别名）。"""
        return self.gateway is not None and self.gateway.tbl is not None

    def _calls_fn(self) -> int:
        """当前 dws 调用计数（intake 侧：gateway.counter.calls）。"""
        return self.gateway.counter.calls

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        self.prepare()
        self.extract()
        self.budget_gate_a()
        self.dedupe_local()
        self.dedupe_library()
        self.apply_vision_gate()
        self.dedupe_phone()
        self.build_rows()
        self.ensure_options()
        self.upload_attachments()
        self.fixup_attachments()
        self.write_records()
        self.readback_verify()
        self.assemble()
        return self.finish()

    # ------------------------------------------------------------------ #
    # 序幕：路径 / 状态 / dws 装配 / 开场打印 / --reset / checkpoint / 补丁装载
    # ------------------------------------------------------------------ #
    def prepare(self) -> None:
        args = self.args
        batch_id = args.batch_id or _new_batch_id()
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
            default_out_root() / batch_id
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.batch_id = batch_id
        self.out_dir = out_dir
        self.report_path = out_dir / "intake_report.json"
        self.candidates_path = out_dir / "candidates.json"
        self.checkpoint_path = out_dir / "checkpoint.json"

        self.summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
                        "attachment_uploaded": 0, "attachment_failed": 0,
                        # 本次「只补附件」路径的计数（记录不重建）
                        "attachment_fixup_uploaded": 0, "attachment_fixup_failed": 0,
                        # 墙钟预算内未处理的文件数（重跑同一命令续跑）
                        "pending_budget": 0,
                        # 转 agent 多模态兜底的文件数（VISION_NEEDED 清单）
                        "needs_agent_vision": 0}

        self.gateway = TableGateway(args.config,
                                    replay_path=getattr(args, "replay_path", None))
        self._set_fatal(self.gateway.fatal)
        self.readback = ReadBackVerifier(self.gateway.tbl, self.warnings)

        self.console.banner()
        self.console.batch_info(batch_id, out_dir)
        tbl = self.gateway.tbl
        if tbl is not None:
            self.console.base_info(tbl.base_name, tbl.base_id,
                                   tbl.table_name("resume"), tbl.table_id("resume"))

        self.files = list(args.files or [])
        self.store = CheckpointStore(self.checkpoint_path, batch_id, out_dir,
                                     args.config)

        if args.reset:
            # --reset 先把 checkpoint 清成空骨架并立即落盘：清表与重跑之间被杀也不会
            # 留下「表已清空但旧 done 还在」的假断点
            self.store.persist()
        if tbl is not None and args.reset:
            r = self.gateway.reset("resume")
            self.console.reset_result(r.get("found", 0), r.get("deleted", 0),
                                      len(r.get("failed") or []))
            if r.get("failed"):
                self.warnings.append("--reset 删除失败 %d 片：%s"
                                     % (len(r["failed"]),
                                        json.dumps(r["failed"], ensure_ascii=False)[:300]))

        # ---- checkpoint（幂等续跑；逐条落盘，格式 version=2）----
        self.store.load(args.reset, self.console, self.warnings)

        # ---- agent 多模态兜底补丁（--apply-vision-patch，可缺省）----
        # 补丁表装在链上 Tier 2（AgentPatchExt）里：提取时由链直接受理，编排层只在
        # 链没走到的 kind=unknown 兜底路径上复用同一份合并实现。
        if getattr(args, "apply_vision_patch", None):
            n_entries, patch_err = self.agent_patch.load(args.apply_vision_patch)
            if patch_err:
                self._set_fatal("--apply-vision-patch %s" % patch_err, warn=True)
            else:
                self.console.vision_patch_loaded(n_entries, args.apply_vision_patch)

    # ------------------------------------------------------------------ #
    # 阶段 1：提取 + 抽字段（纯本地，零 dws 调用）
    # 提取并发跑（EXTRACT_CONCURRENCY=4，多份扫描件并行 Vision OCR）；
    # 墙钟预算触顶时取消未开始的提取，对应文件进「未完成」清单（RESUME 续跑）。
    # ------------------------------------------------------------------ #
    def extract(self) -> None:
        files = self.files
        entries: List[Dict[str, Any]] = []
        t_extract = time.monotonic()
        ex_by_idx = self.runner.run(files, self.budget)
        budget_reason = self.budget.pending_reason()
        for i, fp in enumerate(files):
            p = Path(fp).expanduser()
            fname = p.name
            ex = ex_by_idx.get(i)
            if ex is None:
                entries.append({
                    "seq": i + 1, "file_name": fname, "path": str(p.resolve()),
                    "md5": "", "size": 0, "kind": None, "backend": "none",
                    "parse_status": "pending_budget", "extract_ms": None,
                    "text": "", "fields": None,
                    "result": "未完成", "reason": budget_reason, "dedupe": "new",
                    "attachment_status": "deferred", "record_id": None,
                    "writable": False, "warnings": [],
                })
                continue
            ent: Dict[str, Any] = {
                "seq": i + 1, "file_name": fname, "path": str(p.resolve()),
                "md5": ex.get("md5") or "", "size": ex.get("size") or 0,
                "kind": ex.get("kind"), "backend": ex.get("backend"),
                "parse_status": ex.get("status"), "extract_ms": ex.get("elapsed_ms"),
                "text": ex.get("text") or "", "fields": None,
                "result": None, "reason": None, "dedupe": "new",
                "attachment_status": "deferred", "record_id": None,
                "writable": False, "warnings": [],
            }
            if ex.get("status") != "ok":
                if ex.get("status") == "no_text_layer":
                    # chain 终态 no_text_layer 不再判死 → agent 多模态兜底通道
                    # （补丁覆盖则合并入库，未覆盖则 needs_agent_vision 如实失败）。
                    # 链上 Tier 2 已受理时 status 就是 ok，走不到本分支——这里覆盖的是
                    # 不进链的 kind=unknown（纯文本兜底路径）。
                    self._agent_vision_entry(ent)
                else:
                    ent["result"] = "失败"
                    reason = PARSE_FAIL_REASON.get(ex.get("status"),
                                                   "文件解析失败；请确认文件完整后重新提供")
                    if ex.get("status") == "unsupported":
                        reason = "%s（扩展名 %s）" % (reason, ex.get("ext") or p.suffix or "(无)")
                    if ex.get("error"):
                        reason = "%s（技术细节：%s）" % (reason, str(ex["error"])[:120])
                    ent["reason"] = reason
            elif ex.get("backend") == AgentPatchExt.BACKEND:
                # 链上 Tier 2（agent_patch_ext）已经取到补丁文本并合并了字段草稿
                self._adopt_agent_patch(ent)
            elif detect_scanned(ent["text"], ent.get("kind") or ""):
                # 双保险：extract_text 已判过，这里再判一次。
                # 与 chain 终态 no_text_layer 同语义，转 agent 多模态兜底通道
                self._agent_vision_entry(ent)
            else:
                f = extract_resume_fields(ent["text"], fname)
                ent["fields"] = f
                ent["warnings"] = list(f.get("warnings") or [])
                if not _clean(f.get("phone")):
                    ent["result"] = "失败"
                    ent["reason"] = ("未解析到手机号，无法按手机号查重入库；"
                                     "请人工确认简历里的联系方式后补录")
                else:
                    ent["writable"] = True
                # ---- OCR 成功但姓名抽取低置信度（firstline 启发式）→ 升级 agent 视觉兜底 ----
                # OCR 提取了文字（parse_status=ok, backend=vision_ocr），但姓名是用
                # firstline 启发式取的（最低置信度，容易取错如「本汉族」←「张震宇」）。
                # 此时姓名不可信，必须让 agent 读图纠正。
                _pb = str(ent.get("backend") or "").lower()
                _ns = str(f.get("name_source") or "").lower()
                _is_ocr_escalated = (
                    _pb and ("ocr" in _pb or "vision" in _pb) and _ns == "firstline")
                if _is_ocr_escalated and ent.get("writable"):
                    ent["escalated_from_ocr"] = True
                    ent["writable"] = False
                    # 复用 _agent_vision_entry：有补丁 → 合并入库；无补丁 → needs_agent_vision
                    self._agent_vision_entry(ent)
            entries.append(ent)
        self.entries = entries
        self.extract_ms = int((time.monotonic() - t_extract) * 1000)
        n_ok = sum(1 for e in entries if e["parse_status"] == "ok")
        n_pending = sum(1 for e in entries if e["result"] == "未完成")
        # ---- agent 多模态兜底清单（VISION_NEEDED）+ 20% 闸门（用户拍板）----
        # 升级自 OCR 的条目（escalated_from_ocr=True）**不计入 20% 闸门分母**：
        # 这些文件已有 OCR 文本，只是姓名抽取低置信度，不是「整批读不出文字」的
        # 格式问题；用它们拦掉整批会让一张图片简历的坏姓名卡死全部入库。
        vision_needed = [e for e in entries if e["parse_status"] == "needs_agent_vision"]
        self.vision_needed = vision_needed
        self.vision_needed_paths = [e["path"] for e in vision_needed]
        self.summary["needs_agent_vision"] = len(vision_needed)
        n_gate_vision = sum(
            1 for e in vision_needed if not e.get("escalated_from_ocr"))
        n_gate_entries = sum(
            1 for e in entries if not e.get("escalated_from_ocr"))
        self.vision_gated = n_gate_entries >= VISION_GATE_MIN_ENTRIES and \
            (n_gate_vision / float(n_gate_entries)) > VISION_GATE_RATIO
        self.runner.summarize(entries, files, n_ok, n_pending, len(vision_needed),
                              self.extract_ms)
        if vision_needed:
            # 单行、空格分隔的绝对路径清单——agent 兜底协议触发器。
            # **闸门触发时也照打**：闸门只决定本轮不写库，不能把补救通道一起关掉，
            # 否则非 macOS（无本机 OCR 梯队）的批次会卡死在「读不出 + 无从补救」，
            # 而这类文件恰恰是 agent 一轮多模态就能读出来的。
            self.console.vision_needed(self.vision_needed_paths)
            self.console.vision_needed_hint(len(vision_needed))
        if self.vision_gated:
            # 20% 闸门：本轮零写入（halted 拦掉全部 dws 写阶段），
            # 退出码 0、报告 ok=true、partial=true、reason="vision_gate"
            self.halted = True
            _gate_needed = sum(1 for e in vision_needed
                               if not e.get("escalated_from_ocr"))
            _gate_total = sum(1 for e in entries
                             if not e.get("escalated_from_ocr"))
            self.console.vision_gate(_gate_needed, _gate_total)
            for e in vision_needed:
                if not e.get("escalated_from_ocr"):
                    self.console.vision_gate_file(e["file_name"], e["path"])

        # 提取进度即刻落盘（record_written=False 的 progress 条目：只作断点可见性，
        # 不参与 done/done_md5 的跳过判定，重跑语义不变）
        for ent in entries:
            self.store.mark_progress(ent)
        self.store.persist()

    # ---- agent 多模态兜底通道的两个入口（合并实现在链上 Tier 2）-------- #
    def _agent_vision_entry(self, ent: Dict[str, Any]) -> None:
        """提取终态 no_text_layer（或双保险判定）的文件不再判死，转 agent 兜底通道。

        - 补丁覆盖本文件 → 走 `_adopt_agent_patch()`（与链上 Tier 2 共用
          `AgentPatchExt.merge_entry()`，绝不合并两次）；
        - 补丁未覆盖 → parse_status="needs_agent_vision" + 失败清单语义（如实告知，
          不硬造字段），等下一轮带补丁重跑。
        """
        ent["parse_status"] = "needs_agent_vision"
        if not self.agent_patch.has_entry(ent["path"]):
            ent["result"] = "失败"
            ent["reason"] = PARSE_FAIL_REASON["needs_agent_vision"]
            return
        self._adopt_agent_patch(ent)

    def _adopt_agent_patch(self, ent: Dict[str, Any]) -> None:
        """补丁合并结果落条目。

        warnings 顺序 = 字段告警 → 合并说明 → 补丁 text 为空提示（顺序即产物字节）；
        backend="agent_vision" 由链上赢家给出，这里再显式写一次（notes 记 backend）。
        合并后仍无手机号 → 判失败（无法按手机号查重入库，不硬造字段）。

        escalated_from_ocr=True 时（OCR 成功但姓名 firstline 低置信度升级）：
        OCR 已抽到字段（phone/email 通常正确），补丁 regex 重抽结果**对补丁提供的
        字段一律优先**（尤其 name），补丁未提供的字段保留 OCR 抽取值。
        """
        merged = self.agent_patch.merge_entry(ent["path"], ent["file_name"])
        f = merged["fields"]
        if ent.get("escalated_from_ocr"):
            # OCR 升级路径：以 OCR 抽取字段为底，补丁 regex 重抽的字段覆盖之。
            # 补丁提供的字段（非空）一律优先；补丁未提供的保留 OCR 值。
            ocr_f = ent.get("fields") or {}
            for k, v in f.items():
                if v is not None and str(v).strip():
                    # 补丁有值 → 覆盖
                    ocr_f[k] = v
                # 补丁无值 → 保留 OCR 的原值（已在 ocr_f 里）
            # field_sources / needs_review 用补丁的（标记 agent_vision 来源）
            ocr_f["field_sources"] = dict(f.get("field_sources") or {})
            ocr_f["needs_review"] = list(f.get("needs_review") or [])
            f = ocr_f
        ent["text"] = merged["text"] if merged["text"] else (ent.get("text") or "")
        ent["fields"] = f
        ent["warnings"] = list(f.get("warnings") or []) + list(merged["notes"])
        ent["parse_status"] = "ok"
        ent["backend"] = AgentPatchExt.BACKEND
        ent["vision_patched"] = True
        if not _clean(f.get("phone")):
            ent["result"] = "失败"
            ent["reason"] = ("agent 兜底补丁合并后仍未解析到手机号，无法按手机号查重入库；"
                             "请人工确认简历里的联系方式后补录（不硬造字段）")
        else:
            ent["writable"] = True

    # ------------------------------------------------------------------ #
    # 预算检查点 A：写库前。已触顶 → graceful 停止，不进入任何 dws 写阶段
    # done_md5 里的文件不标「未完成」：阶段 2 的本地跳过/补附件判定照常给它们结果
    # ------------------------------------------------------------------ #
    def budget_gate_a(self) -> None:
        if self.budget.budget_stopped or self.budget.over_budget():
            self.halted = True
            self.budget.halt_before_write(self.console)
            done_md5 = self.store.done_md5
            for ent in self.entries:
                if ent["result"] is None and ent["md5"] not in done_md5:
                    ent["result"] = "未完成"
                    ent["writable"] = False
                    ent["reason"] = self.budget.halt_reason()

    # ------------------------------------------------------------------ #
    # 阶段 2a：去重（checkpoint 与本批内部走**真 MD5**）+ fixups 补传队列
    # ------------------------------------------------------------------ #
    def dedupe_local(self) -> None:
        args = self.args
        store = self.store
        done_md5 = store.done_md5
        seen_md5: Dict[str, Tuple[int, str]] = {}
        need_lib_scan = False
        fixups: List[Dict[str, Any]] = []      # 记录已写、只欠附件的补传队列
        for ent in self.entries:
            md5 = ent["md5"]
            if ent["result"] == "失败":
                continue
            if md5 and md5 in done_md5:
                prev = (store.done or {}).get(md5) or {}
                # checkpoint 把「记录已写」与「附件已传」分开记状态。
                # 兼容旧格式：done 里只记过写库成功的行 → record_written 缺省视为 True；
                # attachment_uploaded 缺省从旧的 attachment_status 推导。
                record_written = bool(prev.get("record_written", True))
                # Stale checkpoint guard: emit-mode old bug could mark
                # record_written=true while the record was never actually
                # written (record_id is null/empty).  In that case treat the
                # entry as not-yet-written so it gets reprocessed.
                if record_written and not prev.get("record_id"):
                    record_written = False
                attach_done = bool(prev.get("attachment_uploaded",
                                            prev.get("attachment_status") == "uploaded"))
                ent["dedupe"] = prev.get("dedupe") or "overwrite"
                ent["record_id"] = prev.get("record_id")
                ent["attachment_status"] = prev.get("attachment_status") or "deferred"
                # 派生字段恢复：跳过路径不会重跑阶段 4，若不从 checkpoint 恢复，
                # 重跑产出的 candidates.json 会丢 org_guess/category_guess/归一 skills/
                # 归一 expected_location → C2 匹配会判「找不到同组织岗位」。
                # 旧格式 checkpoint 没存这些 → 用本地纯函数重算一遍（零 dws，
                # 与首次写库时同一套规则，结果一致）。
                ent["org_guess"] = prev.get("org_guess")
                ent["org_confidence"] = prev.get("org_confidence")
                ent["category_guess"] = prev.get("category_guess")
                if ent["parse_status"] == "ok":
                    f_prev = ent.get("fields") or {}
                    if not ent["org_guess"]:
                        o, oc, _ = guess_org(ent["file_name"], f_prev.get("expected_position"),
                                             ent.get("text") or "")
                        ent["org_guess"], ent["org_confidence"] = o, oc
                    if not ent["category_guess"]:
                        ent["category_guess"] = guess_category(ent["file_name"],
                                                               f_prev.get("expected_position"),
                                                               ent.get("text") or "")
                if prev.get("skills") is not None or prev.get("expected_location"):
                    ent["row"] = {"skills": list(prev.get("skills") or []),
                                  "expected_location": prev.get("expected_location")}
                if record_written and (attach_done or args.no_attachment):
                    # 完成态：记录已写 且（附件已传 或 本次明确不传附件）→ 整条跳过
                    ent["result"] = "跳过"
                    ent["writable"] = False      # ⚠️ 必须清掉，否则阶段 4 还会把它写一遍
                    if attach_done:
                        ent["attachment_status"] = "uploaded"
                    ent["reason"] = ("上次已成功入库（checkpoint 幂等，本次未重放%s）；"
                                     "如需重新覆盖请换 --out-dir 或加 --reset"
                                     % ("，附件本次按 --no-attachment 继续不传"
                                        if (args.no_attachment and not attach_done) else ""))
                    continue
                if record_written and not attach_done:
                    # 半成品态：记录已写、附件欠传（上次 --no-attachment 或上次上传失败），
                    # 本次没带 --no-attachment → **只补附件**，绝不重复 create
                    if ent["record_id"] and Path(ent["path"]).exists():
                        ent["writable"] = False
                        ent["fix_attachment"] = True
                        fixups.append(ent)
                        continue
                    ent["result"] = "跳过"
                    ent["writable"] = False
                    self.warnings.append("《%s》checkpoint 显示附件欠传，但%s → 无法补传，"
                                         "记录保持已入库状态"
                                         % (ent["file_name"],
                                            "缺 record_id" if not ent["record_id"]
                                            else "本地文件不存在"))
                    ent["reason"] = ("上次已成功入库；附件仍欠传但本次无法补传（%s），"
                                     "请提供原文件后重跑"
                                     % ("checkpoint 缺 record_id" if not ent["record_id"]
                                        else "本地文件不存在"))
                    continue
                # record_written=False（异常旧数据）→ 不跳过，走正常入库路径
            if md5 and md5 in seen_md5:
                ent["result"] = "跳过"
                ent["writable"] = False
                ent["dedupe"] = "overwrite"
                ent["reason"] = ("与本批第 %d 份《%s》文件内容完全相同（MD5 一致），"
                                 "判为重复上传，已跳过"
                                 % (seen_md5[md5][0], seen_md5[md5][1]))
                continue
            if md5:
                seen_md5[md5] = (ent["seq"], ent["file_name"])
            need_lib_scan = True
        self.need_lib_scan = need_lib_scan
        self.fixups = fixups

    # ------------------------------------------------------------------ #
    # 阶段 2b/2c：库内去重（比对键是库内「附件内容MD5」字段，
    #             老库无该字段则回退「文件名+字节大小」）
    # ------------------------------------------------------------------ #
    def dedupe_library(self) -> None:
        args = self.args
        # ---- 库内去重**选档**（内容级 MD5 优先；老库无该字段 → 回退「文件名+字节大小」）----
        # 硬要求：config/schema 里找不到「附件内容MD5」字段（客户现存库）时**不得崩溃、
        # 不得自建字段**——回退 NameSizeDeduper + 一条 warning（说明未启用内容级去重与
        # 如何启用）。建字段是 replicate 部署时的事。
        attach_md5_field: Optional[str] = None
        if self.has_table:
            try:
                if ATTACH_MD5_FIELD_KEY in self.gateway.field_keys("resume"):
                    attach_md5_field = ATTACH_MD5_FIELD_KEY
            except Exception as exc:                # 防御：读 config 出问题也只回退，不崩
                self.warnings.append("读取简历库字段清单失败（%s: %s）→ 库内去重按老库回退处理"
                                     % (type(exc).__name__, str(exc)[:120]))
        self.attach_md5_field = attach_md5_field
        if self.has_table and attach_md5_field is None:
            self.warnings.append(OLD_LIB_DEDUPE_WARNING)
            self.console.old_lib_dedupe_fallback()
        lib_deduper = (ContentHashDeduper(attach_md5_field) if attach_md5_field
                       else NameSizeDeduper())
        if (self.has_table and self.need_lib_scan and not args.no_dedupe_scan
                and not self.halted):
            try:
                with self._time_stage() as st:
                    # scan 的 `table` 实参必须是 readback 门面（不是 tbl），
                    # dedupe 包里的 getattr(last_query_pages/last_query_truncated) 读点靠它
                    scan = lib_deduper.scan(self.readback, "resume",
                                            max_pages=DEDUPE_SCAN_MAX_PAGES)
                self.console.dedupe_scan(lib_deduper.key_label, scan.records, scan.indexed,
                                         (lib_deduper.coverage_note()
                                          if isinstance(lib_deduper, ContentHashDeduper)
                                          else None),
                                         st.delta, self.gateway.tbl.dws_calls)
                if isinstance(lib_deduper, ContentHashDeduper):
                    # 老记录容忍：库里有之前写入（无哈希）的记录 → 如实告警一条
                    self.warnings.extend(lib_deduper.legacy_warning())
                if scan.truncated:
                    # 缺陷2：翻页打满 max_pages 时旧实现静默截断，去重就此失效而用户无从得知
                    self.warnings.append(
                        "库内去重扫描被截断：翻到 max_pages=%d（%d 页）仍有下一页，"
                        "只取回 %d 条记录，库内实际存量更多 → 本次「%s」比对不完整，"
                        "漏判重复上传的风险高。可加 --no-dedupe-scan 跳过库内比对"
                        "（此时只靠手机号查重兜底），或调大 DEDUPE_SCAN_MAX_PAGES 后重跑"
                        % (scan.max_pages, scan.pages, scan.records,
                           lib_deduper.key_label))
                    self.console.dedupe_scan_truncated(scan.max_pages, scan.records)
                else:
                    # 缺陷3 的行数护栏：这次全表扫描已经**免费**给出了行数，喂给写前护栏，
                    # 省掉一次 record stats 调用（截断时数字不可信，就不喂）
                    self.gateway.set_row_count("resume", scan.records)
            except DwsError as exc:
                self.warnings.append("库内附件查重扫描失败（%s/%s）：%s；本次跳过「%s」的库内比对"
                                     % (exc.category, exc.code, exc.message[:200],
                                        lib_deduper.key_label))

        for ent in self.entries:
            if ent["result"] is not None or not ent["writable"]:
                continue
            d = lib_deduper.decide(ent["file_name"], ent["size"], ent.get("md5"))
            self.warnings.extend(d.warnings)
            if d.action == "skip":
                ent["result"] = "跳过"
                ent["writable"] = False
                ent["dedupe"] = d.dedupe
                ent["record_id"] = d.record_id
                ent["reason"] = d.reason
            elif d.reason:
                # 同名同大小但**内容不同** → 不判重复，走覆盖更新语义
                # （new/overwrite 由下面的手机号查重决定），说明进清单该行
                ent["dedupe_note"] = d.reason

    # ------------------------------------------------------------------ #
    # 20% 闸门执行：进入任何 dws 写阶段之前，本轮**零写入**
    # （库内扫描已被 halted 拦掉；这里把仍未定论的条目全部转「未完成」并清空
    # 补传队列，阶段 3~8 因此自然全跳过；重跑同一命令续处理，checkpoint 幂等）
    # ------------------------------------------------------------------ #
    def apply_vision_gate(self) -> None:
        if not self.vision_gated:
            return
        self.fixups = []
        n_needed = sum(1 for e in self.vision_needed
                       if not e.get("escalated_from_ocr"))
        n_entries = sum(1 for e in self.entries
                       if not e.get("escalated_from_ocr"))
        for ent in self.entries:
            if ent["result"] is None:
                ent["result"] = "未完成"
                ent["writable"] = False
                if ent.get("fix_attachment"):
                    ent["reason"] = ("20% 闸门触发（本批 %d/%d 份读不出文字 > %.0f%%），"
                                     "本轮不写任何记录，补传附件一并顺延；确认后重跑"
                                     "同一命令续补"
                                     % (n_needed, n_entries, VISION_GATE_RATIO * 100))
                else:
                    ent["reason"] = ("20%% 闸门触发（本批 %d/%d 份读不出文字 > %.0f%%），"
                                     "本轮未写任何记录；先按 VISION_NEEDED 清单走 agent "
                                     "多模态兜底，打完补丁重跑同一命令即可入库，补丁之后"
                                     "仍读不出的再请用户提供文字版简历"
                                     % (n_needed, n_entries, VISION_GATE_RATIO * 100))

    # ------------------------------------------------------------------ #
    # 阶段 3：一次批量手机号查重（单次 filter 查询判 new/overwrite/conflict）
    # ------------------------------------------------------------------ #
    def dedupe_phone(self) -> None:
        phone_deduper = PhoneDeduper()
        pend = [e for e in self.entries if e["result"] is None and e["writable"]]
        if self.has_table and pend:
            phones = sorted({str(_clean(e["fields"].get("phone"))) for e in pend})
            try:
                with self._time_stage() as st:
                    scan = phone_deduper.scan(self.readback, phones, "resume")
                self.console.phone_scan(len(phones), scan.indexed,
                                        st.delta, st.calls_delta)
            except DwsError as exc:
                self._set_dws_fatal(exc, "批量手机号查重失败（%s/%s）：%s")

        for ent in self.entries:
            if ent["result"] is not None or not ent["writable"]:
                continue
            phone = str(_clean(ent["fields"].get("phone")))
            name = _clean(ent["fields"].get("name"))
            d = phone_deduper.decide(phone, name, ent["seq"], ent["file_name"])
            self.warnings.extend(d.warnings)
            if d.dedupe is not None:
                ent["dedupe"] = d.dedupe
            if d.record_id is not UNSET:
                ent["record_id"] = d.record_id
            if d.action == "fail":
                ent["result"] = "失败"
                ent["writable"] = False
                ent["reason"] = d.reason

    # ------------------------------------------------------------------ #
    # 阶段 4：组织/分类预判 + 字段整理（纯本地）
    # ------------------------------------------------------------------ #
    def build_rows(self) -> None:
        to_write: List[Dict[str, Any]] = []
        all_skills: List[str] = []
        all_locations: List[str] = []
        known_locs: List[str] = []
        if self.has_table:
            known_locs = self.readback.known_option_names("resume", "expected_location")
        self.known_locs = known_locs

        for ent in self.entries:
            f = ent.get("fields") or {}
            text = ent.get("text") or ""
            if ent["writable"]:
                org, org_conf, org_counts = guess_org(ent["file_name"],
                                                      f.get("expected_position"), text)
                cat = guess_category(ent["file_name"], f.get("expected_position"), text)
                loc, loc_fallback = normalize_location(f.get("expected_location"), known_locs)
                skills = [str(s).strip() for s in (f.get("skills") or []) if str(s).strip()]
                skills = list(dict.fromkeys(skills))[:SKILLS_MAX]
                certs = [str(c).strip() for c in (f.get("certificates") or []) if str(c).strip()]
                full_text = sanitize_text(text or "")
                if len(full_text) > FULL_TEXT_MAX:
                    full_text = full_text[:FULL_TEXT_MAX]
                    ent["warnings"].append("简历全文超过 %d 字，已截断写入（原文 %d 字）"
                                           % (FULL_TEXT_MAX, len(text or "")))
                row = {
                    "name": _clean(f.get("name")),
                    "phone": _clean(f.get("phone")),
                    "email": _clean(f.get("email")),
                    "education": _clean(f.get("education")),
                    "school": _clean(f.get("school")),
                    "school_rank": _clean(f.get("school_rank")),
                    "years_experience": f.get("years_experience"),
                    "major": _clean(f.get("major")),
                    "certificates": _join(certs),
                    "expected_position": _clean(f.get("expected_position")),
                    "expected_location": loc,
                    "expected_salary": _clean(f.get("expected_salary")),
                    "skills": skills,
                    "category": cat,
                    "comm_status": COMM_STATUS_DEFAULT,
                    "upload_time": _today(),
                    "full_text": full_text or None,
                }
                # 组织：只在高置信度时写库（低置信度留空 → Turn 2 的 LLM 定夺）
                if org_conf == "high" and org:
                    row["org"] = org
                else:
                    self.warnings.append("《%s》组织归属判不了（制造命中 %d / 职能命中 %d），"
                                         "组织留空待 Turn 2 确认；提示值=%s"
                                         % (ent["file_name"], org_counts.get("stage2_mfg", 0),
                                            org_counts.get("stage2_func", 0), org or "无"))
                if loc_fallback:
                    ent["warnings"].append("期望地点未明确/为多城市，按老插件铁律兜底填「%s」"
                                           % LOCATION_FALLBACK)
                if f.get("years_experience_source") == "estimated":
                    ent["warnings"].append("工作年限 %s 是**估算值**（years_source=estimated），"
                                           "会直接喂给「经验年限一票否决」硬门槛，"
                                           "Turn 2 必须用 evidence 原文复核"
                                           % f.get("years_experience"))
                ent["row"] = row
                ent["org_guess"], ent["org_confidence"] = org, org_conf
                ent["category_guess"] = cat
                ent["location_fallback"] = loc_fallback
                to_write.append(ent)
                for s in skills:
                    if s not in all_skills:
                        all_skills.append(s)
                if loc and loc != LOCATION_FALLBACK and loc not in all_locations:
                    all_locations.append(loc)
        self.to_write = to_write
        self.all_skills = all_skills
        self.all_locations = all_locations

    # ------------------------------------------------------------------ #
    # 阶段 5：一次 ensure_options 补齐本批全部新技能标签 / 期望地点（只增不删）
    # ------------------------------------------------------------------ #
    def ensure_options(self) -> None:
        if not (self.has_table and self.to_write):
            return
        for field_key, names in (("skills", self.all_skills),
                                 ("expected_location", self.all_locations)):
            if not names:
                continue
            try:
                with self._time_stage() as st:
                    opts = self.gateway.ensure_options("resume", field_key, names)
                self.console.ensure_options(field_key, len(names), len(opts),
                                            st.delta, st.calls_delta)
            except Exception as exc:
                self.warnings.append("ensure_options(resume.%s) 失败：%s: %s；"
                                     "写入时服务端通常会自动补选项，但建议重跑本步确认"
                                     % (field_key, type(exc).__name__, str(exc)[:200]))

    # ------------------------------------------------------------------ #
    # 阶段 6：并发上传附件（一律用原始文件名），fileToken 随 upsert 一次写入
    # 按并发度分片串行推进，每片完成后**立即落 checkpoint**（附件状态逐条持久化）；
    # 片间检查墙钟预算，触顶即停止上传——但**已进提交阶段的记录照常 upsert**（欠附件的
    # 记 record_written=True + attachment_uploaded=False，重跑走 6b 只补附件不重建记录）。
    # ------------------------------------------------------------------ #
    def upload_attachments(self) -> None:
        args = self.args
        to_write = self.to_write
        summary = self.summary
        if not (self.has_table and to_write and not args.no_attachment):
            return
        chunk_n = max(1, int(args.concurrency))
        truncated_at = len(to_write)
        with self._time_stage() as st:
            for ci in range(0, len(to_write), chunk_n):
                if self.budget.over_budget():
                    truncated_at = ci
                    self.budget.budget_stopped = True
                    break
                chunk = to_write[ci:ci + chunk_n]
                results = self.gateway.upload_attachments([e["path"] for e in chunk],
                                                          args.concurrency)
                for ent, res in zip(chunk, results):
                    if res.get("ok") and res.get("cell"):
                        ent["row"]["attachment"] = res["cell"]
                        # 写入侧：附件上传成功后，把**本地文件真 MD5**（提取层已算好，
                        # 与上传的是同一个文件）随记录写进「附件内容MD5」字段——库内内容级
                        # 去重就靠它。字段不存在（老库）时 attach_md5_field 为 None，一个
                        # 键都不写（build_cells 遇到没映射的键会报错，绝不硬塞）。
                        if self.attach_md5_field and ent.get("md5"):
                            ent["row"][self.attach_md5_field] = ent["md5"]
                        ent["attachment_status"] = "uploaded"
                        summary["attachment_uploaded"] += 1
                    else:
                        ent["attachment_status"] = "failed"
                        summary["attachment_failed"] += 1
                        ent["warnings"].append("附件上传失败：%s" % (res.get("error") or "未知错误"))
                        self.warnings.append("《%s》附件上传失败（%s/%s）：%s；简历正文已照常入库，"
                                             "可后续用「补传附件」重跑"
                                             % (ent["file_name"], res.get("category"),
                                                res.get("code"), str(res.get("error"))[:200]))
                    # 附件状态一确立就落盘（progress 条目；record_written 仍为 False）
                    self.store.mark_attachment(ent)
                self.store.persist()
        if truncated_at < len(to_write):
            for ent in to_write[truncated_at:]:
                ent["attachment_status"] = "deferred"
                msg = self.budget.deferred_attachment_msg()
                ent["warnings"].append(msg)
                self.warnings.append("《%s》%s" % (ent["file_name"], msg))
                self.budget.mark_deferred(ent["file_name"])
        self.console.upload_summary(args.concurrency, summary["attachment_uploaded"],
                                    summary["attachment_failed"],
                                    len(to_write) - truncated_at,
                                    int(st.delta * 1000), st.calls_delta)

    # ------------------------------------------------------------------ #
    # 阶段 6b：补传附件——上次 --no-attachment 或附件失败、记录已写库的文件，
    #          本次只补附件：upload 拿 fileToken → 按 record_id batch_update 附件字段，
    #          **绝不重复 create**；写后回读附件非空，失败如实报并保留 checkpoint
    #          欠传状态（下次重跑继续补）。
    # ------------------------------------------------------------------ #
    def fixup_attachments(self) -> None:
        args = self.args
        summary = self.summary
        if not (self.has_table and self.fixups and not self.fatal):
            return
        # ---- 主控裁决回写：补传路径每轮最多处理 FIXUP_ROUND_MAX 份，
        # 超出 defer 到下一轮 RESUME（防大批量补传把墙钟拖爆；记录保持已入库）----
        fixups = self.fixups
        if len(fixups) > FIXUP_ROUND_MAX:
            n_all = len(fixups)
            deferred_fix = fixups[FIXUP_ROUND_MAX:]
            fixups = fixups[:FIXUP_ROUND_MAX]
            self.fixups = fixups
            for ent in deferred_fix:
                ent["result"] = "未完成"
                ent["writable"] = False
                ent["reason"] = ("记录上次已入库；补传附件队列 %d 份超过单轮上限 %d，"
                                 "本份顺延到下一轮——重跑同一命令续补（RESUME，幂等）"
                                 % (n_all, FIXUP_ROUND_MAX))
                self.budget.mark_deferred(ent["file_name"])
            self.console.fixup_deferred(n_all, FIXUP_ROUND_MAX, len(fixups), len(deferred_fix))
        with self._time_stage() as st:
            results = self.gateway.upload_attachments([e["path"] for e in fixups],
                                                      args.concurrency)
            updates: List[Dict[str, Any]] = []
            for ent, res in zip(fixups, results):
                if res.get("ok") and res.get("cell"):
                    fix_cells: Dict[str, Any] = {"attachment": res["cell"]}
                    # 懒回填：补传附件时顺带把「附件内容MD5」补上——之前入库的
                    # 老记录（无哈希、只能按文件名+大小回退判重）就此收敛到内容级去重
                    if self.attach_md5_field and ent.get("md5"):
                        fix_cells[self.attach_md5_field] = ent["md5"]
                    updates.append({"record_id": ent["record_id"], "cells": fix_cells})
                else:
                    ent["result"] = "失败"
                    ent["attachment_status"] = "failed"
                    ent["reason"] = ("记录上次已入库（本次未重复建记录），但补传附件上传失败"
                                     "（%s/%s）：%s；请重跑同一命令继续补传"
                                     % (res.get("category"), res.get("code"),
                                        str(res.get("error"))[:160]))
                    summary["attachment_failed"] += 1
                    summary["attachment_fixup_failed"] += 1
                    self.warnings.append("《%s》补传附件上传失败（%s/%s）：%s；记录保持已入库，"
                                         "重跑同一命令可继续补"
                                         % (ent["file_name"], res.get("category"), res.get("code"),
                                            str(res.get("error"))[:200]))
            ok_rids: set = set()
            if updates:
                r = self.gateway.batch_update("resume", updates)
                failed_rids = {str(f.get("record_id") or (f.get("row") or {}).get("record_id"))
                               for f in (r.get("failed") or [])}
                ids = [u["record_id"] for u in updates if str(u["record_id"]) not in failed_rids]
                # 写后必回读：附件字段非空才算补传成功（有界轮询，不空转烧调用）
                ok_rids = self.readback.poll_fixup_attachments(ids)
                for ent in fixups:
                    if ent.get("result"):                 # 上传已失败的前面处理过
                        continue
                    rid = str(ent["record_id"])
                    if rid in ok_rids:
                        ent["result"] = "跳过"
                        ent["attachment_status"] = "uploaded"
                        ent["reason"] = ("上次已成功入库（记录未重建），本次仅补传附件成功"
                                         "并回读校验通过")
                        summary["attachment_uploaded"] += 1
                        summary["attachment_fixup_uploaded"] += 1
                        # 补传成功即刻落盘（增量 checkpoint：附件状态一确立就持久化）
                        self.store.mark_fixup_uploaded(ent)
                    else:
                        ent["result"] = "失败"
                        ent["attachment_status"] = "failed"
                        ent["reason"] = ("记录上次已入库（本次未重复建记录），但补传的附件"
                                         "未确认写入（回读为空或服务端拒绝）；请重跑同一命令复核")
                        summary["attachment_failed"] += 1
                        summary["attachment_fixup_failed"] += 1
                        self.warnings.append("《%s》补传附件未确认写入（record_id=%s）；"
                                             "记录保持已入库，重跑同一命令可继续补"
                                             % (ent["file_name"], rid))
        self.console.fixup_summary(len(fixups), summary["attachment_fixup_uploaded"],
                                   summary["attachment_fixup_failed"],
                                   int(st.delta * 1000), st.calls_delta)

    # ------------------------------------------------------------------ #
    # 阶段 7：一次批量写简历库（batch_upsert_by_key，unique=手机号，≤100/片）
    # ------------------------------------------------------------------ #
    def write_records(self) -> None:
        to_write = self.to_write
        upsert_res: Dict[str, Any] = {}
        self._upsert_res = upsert_res
        if not (self.has_table and to_write and not self.fatal):
            return
        rows_in = [e["row"] for e in to_write]
        with self._time_stage() as st:
            try:
                upsert_res = self.gateway.batch_upsert_by_key("resume", "phone", rows_in)
                self._upsert_res = upsert_res
            except DwsError as exc:
                self._set_dws_fatal(exc, "批量写简历库失败（%s/%s）：%s")
        self.console.upsert_summary(upsert_res.get("created"), upsert_res.get("updated"),
                                    len(upsert_res.get("failed") or []),
                                    st.delta, st.calls_delta)
        for fl in (upsert_res.get("failed") or []):
            row = fl.get("row") or {}
            ph = row.get("phone")
            fn = next((e["file_name"] for e in to_write
                       if e["row"].get("phone") == ph), "(未定位到文件)")
            self.warnings.append("写入失败《%s》手机号 %s：%s"
                                 % (fn, ph, str(fl.get("reason"))[:240]))
            for ent in to_write:
                if ent["row"].get("phone") == ph:
                    ent["result"] = "失败"
                    ent["writable"] = False
                    ent["reason"] = "写入简历库失败：%s" % str(fl.get("reason"))[:200]

    # ------------------------------------------------------------------ #
    # 阶段 8：回读校验（一次 filter 查询同时拿 record_id 映射 + 读回值）
    # ------------------------------------------------------------------ #
    def readback_verify(self) -> None:
        # emit/replay 模式下跳过回读（record_id 是模拟或预填的，不查表）
        return

    # ------------------------------------------------------------------ #
    # 阶段 9：组装 rows / candidates / summary（+ checkpoint 终稿重建）
    # ------------------------------------------------------------------ #
    def assemble(self) -> None:
        known_locs = self.known_locs
        summary = self.summary
        store = self.store
        replay_path = getattr(self.args, "replay_path", None)
        is_replay = replay_path is not None
        # replay 模式：从 upsert 结果中提取 record_id（按 to_write 顺序对应）
        if is_replay:
            record_ids = (getattr(self, "_upsert_res", None) or {}).get("record_ids") or []
            for i, e in enumerate(self.to_write):
                if i < len(record_ids) and record_ids[i]:
                    e["record_id"] = record_ids[i]
        for ent in self.entries:
            if ent["result"] is None:
                if not ent["writable"]:
                    ent["result"] = "失败"
                    ent["reason"] = ent["reason"] or "未能入库（原因见 warnings）"
                elif not is_replay:
                    # emit 模式：命令已收集到 dws_commands.json，标记为待执行
                    ent["result"] = "新入库"
                    ent["reason"] = "emit 模式：dws 命令已收集，等待 agent 执行后 replay"
                else:
                    # replay 模式：dws 命令已用真实结果重放，回读被跳过，
                    # record_id 已从 upsert 响应中提取（见循环上方）
                    ent["result"] = "新入库" if ent["dedupe"] == "new" else "已覆盖"
                    ent["reason"] = "replay 模式：已用真实 dws 结果重放，记录已写入"
                    if ent.get("md5") and ent.get("record_id"):
                        self.store.confirm_written(
                            ent, ent["result"])
            if not ent.get("reason"):
                ent["reason"] = ""
            if ent["result"] == "新入库":
                summary["new"] += 1
            elif ent["result"] == "已覆盖":
                summary["overwrite"] += 1
            elif ent["result"] == "跳过":
                summary["skip"] += 1
            elif ent["result"] == "未完成":
                summary["pending_budget"] += 1
            else:
                summary["fail"] += 1

            extra = []
            # 「同名同大小但内容不同 → 不判重复、按新版本覆盖更新」的说明进清单该行
            if ent.get("dedupe_note"):
                extra.append(ent["dedupe_note"])
            if ent.get("org_guess"):
                extra.append("%s%s" % (ent["org_guess"],
                                       "" if ent.get("org_confidence") == "high" else "(待确认)"))
            if ent.get("category_guess"):
                extra.append(ent["category_guess"])
            if ent["attachment_status"] == "uploaded":
                extra.append("附件已传")
            elif ent["attachment_status"] == "failed":
                extra.append("附件失败")
            elif ent["writable"] is False and ent["parse_status"] != "ok":
                extra.append("附件未传")
            reason = ent["reason"]
            if extra and ent["result"] in ("新入库", "已覆盖"):
                reason = "%s（%s）" % (reason, "，".join(extra))
            self.rows.append({"seq": ent["seq"], "file_name": ent["file_name"],
                              "result": ent["result"], "reason": reason})

            f = ent.get("fields") or {}
            secs = f.get("sections") or {}
            # 期望地点一律不留空。写库的行用 row 里的归一值；未写库（无手机号/
            # 冲突/重复）的行也按同一规则归一，保证 candidates.json 交给 C2 时口径一致。
            loc_out = (ent.get("row") or {}).get("expected_location")
            if not loc_out and ent["parse_status"] == "ok":
                loc_out = normalize_location(f.get("expected_location"), known_locs)[0]
            # 身份原文行（安全阀的原文保留面，判据见 shared/fields/identity.py）：
            # 用**抽取原值**在简历全文里搜命中行（期望地点用归一前的原值——串栏垃圾值
            # 恰恰要在原文里看得见）；解析不可用的文件不给行（下方统一置空）。
            if ent["parse_status"] == "ok":
                ident_lines = identity_evidence(ent.get("text") or "", f.get("name"),
                                                f.get("email"), f.get("expected_location"))
            else:
                ident_lines = {k: "" for k in IDENTITY_EVIDENCE_KEYS}
            cand: Dict[str, Any] = {
                "key": "c%02d" % ent["seq"],
                "record_id": ent.get("record_id"),
                "file_name": ent["file_name"],
                "name": _clean(f.get("name")),
                "phone": _clean(f.get("phone")),
                # 邮箱值 + 姓名来源 + 解析 backend（C2 身份阀判据）
                "email": _clean(f.get("email")),
                "education": _clean(f.get("education")),
                "school": _clean(f.get("school")),
                "school_rank": _clean(f.get("school_rank")),
                "major": _clean(f.get("major")),
                "years_experience": f.get("years_experience"),
                "certificates": [str(c) for c in (f.get("certificates") or [])],
                "skills": [str(s) for s in ((ent.get("row") or {}).get("skills")
                                            or f.get("skills") or [])],
                "expected_position": _clean(f.get("expected_position")),
                "expected_location": loc_out,
                "org_guess": ent.get("org_guess"),
                "org_confidence": ent.get("org_confidence") or "low",
                "category_guess": ent.get("category_guess") or CATEGORY_DEFAULT,
                "parse_status": ent["parse_status"],
                "dedupe": ent["dedupe"],
                "attachment_status": ent["attachment_status"],
                "evidence": dict(
                    {k: _truncate(secs.get(k), EVIDENCE_LIMITS.get(k, 2000))
                     for k in EVIDENCE_KEYS},
                    **ident_lines),
                # 工作年限来源必须透传给 C2 / Turn 2
                "years_source": f.get("years_experience_source"),
                # agent 多模态兜底的复核清单与逐字段来源。
                # needs_review = 取自补丁 fields_draft 的字段名（field_source=agent_vision），
                # 回合 2 必须用 evidence 原文复核；未经补丁的候选人两键为空。
                "needs_review": [str(x) for x in (f.get("needs_review") or [])],
                "field_sources": dict(f.get("field_sources") or {}),
                # 姓名复核判据的另一半（见 CANDIDATE_EXTRA_FIELDS 注释）
                "name_source": f.get("name_source"),
                "parse_backend": ent.get("backend"),
            }
            if ent["parse_status"] != "ok" or ent["result"] == "未完成":
                # 不硬造字段 → 一律留空；「未完成」（预算内未处理）同样不给字段
                for k in ("name", "phone", "email", "education", "school", "school_rank",
                          "major", "years_experience", "expected_position",
                          "expected_location"):
                    cand[k] = None
                cand["certificates"] = []
                cand["skills"] = []
                cand["org_guess"] = None
                cand["category_guess"] = CATEGORY_DEFAULT
                cand["years_source"] = None
                cand["needs_review"] = []
                cand["field_sources"] = {}
                cand["name_source"] = None
                cand["parse_backend"] = ent.get("backend")
                cand["evidence"] = {k: "" for k in EVIDENCE_KEYS + IDENTITY_EVIDENCE_KEYS}
            # OCR/vision 解析的姓名必须复核（OCR 常见噪声/拼接错误，如「本汉族」←「张震宇」）
            _pb = str(cand.get("parse_backend") or "").lower()
            if _pb and ("ocr" in _pb or "vision" in _pb):
                if "name" not in cand.get("needs_review", []):
                    cand["needs_review"].append("name")
            # firstline 启发式取的姓名必须复核（最低置信度，容易取错）
            _ns = str(cand.get("name_source") or "").lower()
            if _ns == "firstline":
                if "name" not in cand.get("needs_review", []):
                    cand["needs_review"].append("name")
            self.candidates.append(cand)

            # checkpoint：「记录已写」与「附件已传」分开记状态；
            # 同时存档派生字段（org/category/skills/expected_location），跳过重跑时恢复，
            # 保证 candidates.json 跨次运行字段完整。
            # 新入库/已覆盖 → 记全新条目；补传附件路径（fix_attachment）→ 合并更新旧条目。
            # 写库成功的条目在阶段 8 回读确认后已**逐条落盘**；这里是终稿重建（内容
            # 由同一个 CheckpointStore.done_entry 构造，两处一致），随收尾整体再写一次。
            if ent["md5"] and (ent["result"] in ("新入库", "已覆盖")
                               or (ent.get("fix_attachment")
                                   and ent["result"] in ("跳过", "失败"))):
                # emit 模式下 record_written 必须为 False：记录尚未真正写入 AI 表，
                # mark_written 用 emit_pending=True 构造 done 条目，重跑不跳过这些文件。
                # replay 模式（is_replay=True）下记录已真正写入 → emit_pending=False（默认）。
                store.mark_written(ent, ent["result"],
                                   emit_pending=(not is_replay))
            elif ent["md5"] and ent["result"] == "跳过" and (store.done or {}).get(ent["md5"]):
                # 纯跳过：把本次恢复/重算出的派生字段合并回旧条目（旧格式 checkpoint 就地升级；
                # 兜底值与首次写库同一套纯函数，零 dws，结果一致）
                f9 = ent.get("fields") or {}
                sk9 = [str(s).strip() for s in (f9.get("skills") or []) if str(s).strip()]
                store.merge_skipped(
                    ent,
                    list(dict.fromkeys(sk9))[:SKILLS_MAX],
                    normalize_location((ent.get("fields") or {}).get("expected_location"),
                                       known_locs)[0])
            for w in ent.get("warnings") or []:
                self.warnings.append("《%s》%s" % (ent["file_name"], w))

    # ------------------------------------------------------------------ #
    # 收尾：统计 / ok / partial / 三份产物落盘 / 人读清单 + 协议行
    # ------------------------------------------------------------------ #
    def finish(self) -> int:
        args = self.args
        self.report = IntakeReport(self.console, self.report_path, self.candidates_path,
                                   OLD_TURNS_PER_FILE, NEW_TURNS, VISION_GATE_RATIO)
        ok = self.report.assemble(
            args=args, budget=self.budget.budget, t_start=self.budget.t_start,
            counter=self.gateway.counter,
            entries=self.entries, files=self.files, to_write=self.to_write,
            rows=self.rows, summary=self.summary, warnings=self.warnings,
            fatal=self.fatal,
            deferred_files=self.budget.deferred_files,
            budget_stopped=self.budget.budget_stopped,
            vision_needed_paths=self.vision_needed_paths,
            vision_gated=self.vision_gated,
            fixups=self.fixups)
        self.ok = ok
        self.store.finalize(ok, self.summary, self.report.dws_calls, self.report.elapsed_ms)
        self.report.write(self.gateway.tbl, self.batch_id,
                          str(Path(args.config).expanduser().resolve()), self.candidates)
        self.store.write_final()

        # ---- 人读清单（沿用老插件「清单式留痕」铁律）----
        self.report.emit(self.extract_ms, self.checkpoint_path, _resume_cmd)

        # ---- 两阶段模式：emit 模式下输出 dws 命令清单 ----
        replay_path = getattr(args, "replay_path", None)
        if replay_path is None and self.has_table:
            emit_path = self.out_dir / "dws_commands.json"
            self.gateway.client.write_emit_file(str(emit_path))
            print("emit 模式：%d 条 dws 命令已写入 %s" %
                  (len(self.gateway.client.emit_commands()), emit_path))

        return 0 if ok else 1

    # ------------------------------------------------------------------ #
    # CLI 侧（main 用；不属于任何阶段）
    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_args(args: Any, console: IntakeConsole) -> Optional[int]:
        """main() 的四处参数校验。返回退出码（2），全过返回 None。"""
        if not args.files and not args.reset:
            console.cli_error("--files 至少要给一个文件（或单独用 --reset 清表）")
            return 2
        if args.wall_budget <= 0:
            console.cli_error("--wall-budget 必须是正秒数（默认 %.0f，须小于 agent 工具 120s 超时）"
                              % WALL_BUDGET_DEFAULT)
            return 2
        if not Path(args.config).expanduser().exists():
            console.cli_error("--config 不存在：%s" % args.config)
            return 2
        if args.apply_vision_patch and not Path(args.apply_vision_patch).expanduser().exists():
            console.cli_error("--apply-vision-patch 不存在：%s" % args.apply_vision_patch)
            return 2
        return None

    @staticmethod
    def crash_artifact(args: Any, exc: BaseException, console: IntakeConsole) -> int:
        """绝不静默早退——run() 抛异常也要落一份 ok=false 的报告 + ARTIFACT: 行。

        out_dir 与 prepare() 各自独立计算：未给 --out-dir 时**另取一个新 batch_id**
        （原 main() 语义，逐字保持）。
        """
        import traceback
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
            default_out_root() / (args.batch_id or _new_batch_id())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _write_json(out_dir / "intake_report.json", {
                "ok": False, "elapsed_ms": 0, "dws_calls": 0, "turns_saved_estimate": 0,
                "rows": [{"seq": i + 1, "file_name": Path(f).name, "result": "失败",
                          "reason": "脚本异常中止：%s: %s" % (type(exc).__name__, exc)}
                         for i, f in enumerate(args.files or [])],
                "summary": {"new": 0, "overwrite": 0, "skip": 0, "fail": len(args.files or []),
                            "attachment_uploaded": 0, "attachment_failed": 0},
                "warnings": [traceback.format_exc()[-1500:]], "retry_count": 0,
            })
            console.artifact(out_dir / "intake_report.json")
        except Exception:
            pass
        traceback.print_exc()
        return 1
