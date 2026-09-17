#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recruit-match-suite-fast / skills / resume-intake / scripts / intake_resume.py
==============================================================================

简历入库编排层（契约 §2 Turn 1 / §6 W-C1）。**一个进程内做完全部确定性工作，
零 agent 回合**：

    提取文本 → 扫描件判定 → 正则抽字段 → 去重（本批内/checkpoint 走真 MD5，
    库内走「附件内容MD5」内容级比对；老库无该字段则回退「文件名+字节大小」并告警）
    → 一次批量手机号查重
    → 组织/分类预判 → 一次 ensure_options 补技能标签 → 并发上传附件
    → 一次批量 upsert 写简历库 → 回读校验 → 产出 candidates.json /
      intake_report.json / checkpoint.json

调用面（冻结，W-D 的 SKILL.md 照此写，不得偏离；--wall-budget 为 P3 只增开关、
--apply-vision-patch 为 P4a 只增开关）
------------------------------------------------
    python3 scripts/intake_resume.py --config <config.json绝对路径> \
            --files <f1> [f2 ...] --out-dir <绝对路径> [--no-attachment] [--reset] \
            [--wall-budget <秒>] [--apply-vision-patch <patch.json>]

    产出: <out-dir>/candidates.json, <out-dir>/intake_report.json,
          <out-dir>/checkpoint.json
    stdout 末行: ARTIFACT:<out-dir>/intake_report.json 的绝对路径
    partial 时: 额外一行 `RESUME: <原命令>`——重跑同一命令续跑（见纪律 9）
    有读不出文字的文件时: 额外一行 `VISION_NEEDED: <绝对路径1> <绝对路径2> ...`
            ——agent 多模态兜底协议触发器（见纪律 11）

`--out-dir` 缺省 = /tmp/recruit-fast/<batch_id>/（batch_id = 时间戳 + 短随机）。

设计纪律（每条都是实测/契约换来的，改代码前先读）
------------------------------------------------
1. **dws 调用次数是第一优化目标**（契约 §0.5、W-B 实测单次固定开销 ≈1.0~1.3s）。
   本脚本对一个批次只发这么几次：库内附件「附件内容MD5」内容级扫描 1 次（多读一个
   字段，**不增加调用次数**）+ 手机号批量查重 1 次
   + ensure_options(技能标签/期望地点) 各 1 次起 + 批量 upsert 1 次/100 条
   + 回读 1 次/100 条（+ 传播延迟轮询）+ 附件 1 次/文件（无批量接口，只能并发）。
   全部计数进 report 的 `dws_calls`。
2. **附件先上传、随 create 一起写入，不做事后 update**。W-B 实测最阴的坑
   （aitable/table.py 模块文档第 8 条）：对「刚批量创建出来的记录」发 record update，
   返回 success 但写入可能要几分钟后才可读，期间怎么重试都没用（一轮 41 次调用
   /70 秒全废）。所以本脚本把 fileToken 直接塞进 upsert 的 cells，一次写完；
   只有在回读发现附件缺失时才补一次 batch_update（正常路径不会触发）。
3. **失败可见（契约 D6）**：解析失败 / 无手机号 / 手机号冲突 / 附件失败 /
   回读不一致，全部进 `rows[].result=失败` + `warnings`，绝不静默丢弃，
   并且**仍然计入 report.rows**（一份都不少）。
4. **不硬造字段（契约 D11）**：扫描件/乱码/加密/不支持 → parse_status 记真实状态、
   字段一律留空、进 ❌ 清单并给业务话处置建议。
5. **组织归属只在高置信度时写库**。关键词规则判不了 → org_confidence="low"、
   组织单元格留空，交给 Turn 2 的 LLM 用 evidence 定夺（老插件铁律：判不了问用户，
   不猜；组织为空 = 匹配不到任何岗位）。
6. **幂等续跑（契约 D12 + v3 §9#6）**：checkpoint.json 逐文件记 MD5，且把
   **「记录已写」（record_written）与「附件已传」（attachment_uploaded）分开记状态**。
   重跑判定 = 记录已写 **且**（附件已传 **或** 本次指定 --no-attachment）才算完成可跳过；
   否则只补做缺失的那一步——记录已写但附件缺失时**只补传附件**（upload + 按 record_id
   batch_update，不重复 create）。这是老插件 execution-notes 历史 bug
   「曾因批量路径跳过附件导致『单个有、批量空』」的防线：--no-attachment 跑完后，
   再次运行**不带**该参数必须能把附件补上。用**同一个 --out-dir** 重跑生效；
   想重测「全 update 零 create」请换 out-dir（靠 batch_upsert_by_key 的库内查重幂等），
   想彻底清空请加 --reset。
7. **防静默早退（契约 D7）**：一定落地产物文件，stdout 末行一定打印 ARTIFACT:<绝对路径>。
8. 兼容 python 3.9 与 3.14（契约 D18）：无 match、无运行时 `X | None`、只用 typing.Optional。
   零第三方 pip 依赖（契约 §0.4）：sys.path 由 `Path(__file__).resolve().parents[3]`
   定位插件根后注入 `<root>/shared` 与 `<root>/shared/vendor`，禁止硬编码绝对路径。
9. **增量 checkpoint + 墙钟预算（P3）**：checkpoint.json 不再只在流程末尾写一次——
   「附件已传」（阶段 6 逐片）与「记录已写」（阶段 8 回读确认后逐条）各自一确立就
   原子落盘（tmp + os.replace，SIGKILL 不留半截 JSON）；`progress` 段记录尚未写库
   文件的中间状态（只作断点可见性，不参与跳过判定，done/done_md5 语义与旧版一致，
   旧格式照常可读、读不懂视为空并告警）。`--wall-budget`（默认 100s < agent 工具
   120s 超时）到点即 graceful 停：附件停止上传（记录仍照常 upsert，欠附件的重跑走
   6b 只补附件）、打印已完成/未完成清单与一行 `RESUME:`、退出码 0、报告 ok=true 且
   partial=true。**脚本内部绝不循环子批**（总墙钟仍会超外部超时）；续跑 = 重跑同一条
   命令，checkpoint 幂等保证不产生重复记录。
10. **扫描件/图片在 macOS 上自动 OCR 救回（P3）**：提取并发 4（EXTRACT_CONCURRENCY，
   多份扫描件并行 Vision OCR，实测 3 份并行 1.764s vs 串行 3.810s），提取层细节见
   shared/extraction/vision_ext.py。失败清单语义随之收窄为「仅加密/损坏/OCR 不可信
   （非 macOS、或 OCR 文本仍是水印/重复串/0 数字字符）才失败」。
11. **agent 多模态兜底通道（P4a，客户硬需求「不能接受简历解析报错」的最后一环）**：
   chain 终态仍 no_text_layer 的文件不再判死 → parse_status="needs_agent_vision"，
   stdout 打印一行 `VISION_NEEDED: <绝对路径...>`（清单同时进报告 vision_needed_files）。
   agent **一轮**多模态读完全部列出文件，按补丁 schema 写 json，重跑同命令加
   `--apply-vision-patch <json>`；脚本用 FieldMerger 合并（regex 有值用 regex，
   regex 为空才取 fields_draft；取自草稿的字段打 field_source="agent_vision" 并追加进
   needs_review 由回合 2 复核）。**agent 只产出结构化补丁，绝不写库**——写库仍走本
   脚本正常查重/护栏/回读流程。**20% 闸门（用户拍板）**：needs_agent_vision 份数 /
   总份数 > 0.20 → 疑似整批格式问题，**不写任何记录**，退出码 0、ok=true、
   partial=true、reason="vision_gate"，请用户确认后重试或提供文字版。
   `RECRUIT_NO_VISION=1`（仅测试用）令 Vision OCR 梯队恒不受理，用于在非 macOS
   语义下演练本通道。
12. **库内附件去重 = 真内容 MD5（P4b）**：简历库多一个 text 字段「附件内容MD5」
    （config.json 的 `fields.resume.attach_md5`，**可选键**）。三层判定见
    `shared/dedupe/content_hash.py`：① 本地文件真 MD5 命中库内哈希 → 判重复跳过
    （理由如实说"内容 MD5 相同"，修掉了老键的**漏判**：同一内容换文件名也命中）；
    ② 未命中但 (文件名,大小) 命中且库内那条有哈希 → 内容确实不同 → **不判重复**，
    按同一候选人的简历新版本走覆盖更新（new/overwrite 由手机号查重定），说明进清单
    （修掉了老键的**误判**：改一版重投不再被静默跳过）；③ 命中的老记录没有哈希
    （P4b 之前写入）→ 无从比内容，按老键回退判重复 + 告警。
    写入侧：附件上传成功后把本地文件真 MD5 随记录写入该字段；**懒回填** = 覆盖更新
    老记录 / 6b 补传附件 / 回读补附件时一并把哈希补上，库随之收敛到内容级去重。
    **老库容忍（硬要求）**：config/schema 里找不到该字段（客户现存库）→ 自动回退
    `NameSizeDeduper` + 一条 warning（说明未启用内容级去重与启用方法），**不崩溃、
    不自建字段**（建字段是 replicate 部署时的事）。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# sys.path：脚本在 skills/<name>/scripts/ 下，插件根 = parents[3]
# --------------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PLUGIN_ROOT / "shared"
_VENDOR_DIR = _SHARED_DIR / "vendor"
for _p in (str(_SHARED_DIR), str(_VENDOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aitable.client import DwsCallCounter, DwsClient, DwsError  # noqa: E402
from aitable.table import AITable                   # noqa: E402
from aitable.values import sanitize_text, values_equal  # noqa: E402
from dedupe.base import UNSET                       # noqa: E402
from dedupe.content_hash import (                   # noqa: E402
    ATTACH_MD5_FIELD_KEY,
    ContentHashDeduper,
)
from dedupe.name_size import NameSizeDeduper        # noqa: E402
from dedupe.phone import PhoneDeduper               # noqa: E402
from extract_fields import extract_resume_fields    # noqa: E402
from extract_text import detect_scanned             # noqa: E402
from fields.identity import IDENTITY_EVIDENCE_KEYS, identity_evidence  # noqa: E402
from fields.merger import FieldMerger               # noqa: E402
from fields.regex_ext import RegexFieldExtractor    # noqa: E402
from intake.budget import WallBudget                # noqa: E402
from intake.checkpoint import CheckpointStore       # noqa: E402
from intake.console import IntakeConsole            # noqa: E402
from intake.extraction_runner import ExtractionRunner  # noqa: E402
from intake.report import IntakeReport              # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: 简历全文写入上限（字符）。W-B 实测 text 字段 49,956 字逐字节无损；真实样本最长
#: 6,827 字，所以 20000 既是安全余量又不会截掉任何真实简历。
FULL_TEXT_MAX = 20000
#: 附件并发度（契约 D5：API 限 20 QPS，5 留足余量）
UPLOAD_CONCURRENCY = 5
#: 提取/OCR 并发度（P3）：多份扫描件并行 Vision OCR，实测 3 份并行 1.764s vs
#: 串行 3.810s；上限 4 是派工契约值，与附件并发 5 互不相干。
EXTRACT_CONCURRENCY = 4
#: --wall-budget 默认秒数：必须小于 agent 工具 120s 超时，留出 graceful 停止
#: （落 checkpoint + 打印 RESUME + 写报告）与提交尾段（upsert+回读）的余量。
WALL_BUDGET_DEFAULT = 100.0
#: 每个候选人写入「技能标签」的上限（多选字段，避免选项池被噪声撑爆）
SKILLS_MAX = 40
#: evidence 各段长度上限（契约 D4：字段级全量 + 工作经历正文截断，不是「取前 N 字」）
EVIDENCE_LIMITS = {"education_text": 4000, "cert_text": 4000,
                   "skill_text": 4000, "work_text": 1500}
#: 老插件每份简历的 agent 工具回合数（契约 §2：20~40，取中位数）→ 用于 turns_saved_estimate
OLD_TURNS_PER_FILE = 25
#: 本脚本自己占的 agent 回合数（Turn 1 = 1 次脚本调用）
NEW_TURNS = 1
#: 写后读回传播延迟（W-B 实测：create 后按条件查 ≈2.7s 才可命中）
SETTLE_WAITS = (1.5, 3.0, 4.5)
#: 库内去重扫描的翻页上限（100 页 × 100 条/页 ≈ 10000 条）。存量打满就会截断，
#: 而去重是按「扫回来的这批」判的 → 截断即漏判重复，所以必须报出来（缺陷2）。
DEDUPE_SCAN_MAX_PAGES = 100
#: P4b 老库容忍：简历库没有「附件内容MD5」字段（config.json 的 fields.resume.attach_md5
#: 缺失 / 表里没建这一列）时，库内去重自动回退 (文件名, 字节大小) 并给这一条 warning。
#: **不得崩溃、不得自建字段**——建字段是 replicate 部署时的事，脚本只如实说明怎么启用。
OLD_LIB_DEDUPE_WARNING = (
    "当前简历库没有「附件内容MD5」字段（config.json 的 fields.resume.attach_md5 缺失），"
    "库内附件去重**未启用内容级比对**，已回退到「文件名+字节大小」：同一文件名同一大小、"
    "内容改过一版重投会被误判成重复跳过；同一份内容换个文件名会漏判（靠手机号查重兜底）。"
    "启用方法：在简历库管理表加一个 text 字段「附件内容MD5」，把它的 fieldId 写进 "
    "config.json 的 fields.resume.attach_md5（并同步 field_names/types），重跑即生效；"
    "存量记录会在被覆盖更新/补传附件时自动回填哈希（脚本绝不自建字段）")
#: P4a 20% 闸门（用户拍板）：needs_agent_vision 份数 / 总份数 > 此比例 →
#: 疑似整批格式问题，不写任何记录，报告 reason="vision_gate"。
VISION_GATE_RATIO = 0.20
#: 6b 补传附件（只补附件、记录不重建）单轮处理上限（主控裁决回写）：
#: 超出部分 defer 到下一轮 RESUME，防大批量补传把墙钟拖爆。
FIXUP_ROUND_MAX = 100

#: P4a：agent 多模态兜底合并器（regex 优先、补丁草稿补空、逐字段打 field_source）
_VISION_MERGER = FieldMerger([RegexFieldExtractor()])

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

#: 期望地点兜底值（契约 D14 + 老插件铁律「没有明确地点一律填『不限』」）
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

#: candidates[] 元素必备字段（契约 §3.3 digest.json 的 candidates 数组元素，字段完全一致）
CANDIDATE_FIELDS = (
    "key", "record_id", "file_name", "name", "phone", "education", "school",
    "school_rank", "major", "years_experience", "certificates", "skills",
    "expected_position", "expected_location", "org_guess", "org_confidence",
    "category_guess", "parse_status", "dedupe", "attachment_status", "evidence",
)
#: 契约 D13 要求额外透传的工作年限来源（text|filename|estimated）；
#: P4a 只增：needs_review（agent 兜底草稿字段复核清单）与 field_sources（逐字段来源）
#: P5 只增：email（身份阀判据+原文行）、name_source / parse_backend（姓名复核判据，
#: C2 的 normalize_candidate 据此决定 needs_review 是否追加 "name"）
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
    """期望地点归一（契约 D14 + 老插件铁律）。

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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _load_vision_patch(raw_path: str) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
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


def _agent_vision_entry(ent: Dict[str, Any],
                        vision_patch: Dict[str, Dict[str, Any]]) -> None:
    """P4a：提取终态 no_text_layer 的文件不再判死，转 agent 多模态兜底通道。

    - 补丁覆盖本文件（按解析后绝对路径匹配）→ FieldMerger 合并：先对 patch.text
      跑 RegexFieldExtractor，**regex 有值的字段用 regex**，regex 为空才取
      fields_draft；取自草稿的字段打 field_source="agent_vision" 并追加进该候选人
      needs_review；patch.text 写入简历全文（阶段 4 照常 sanitize/截断），notes
      记 backend="agent_vision"。之后按正常候选走查重/写库/回读（agent 绝不写库）。
    - 补丁未覆盖 → parse_status="needs_agent_vision" + 失败清单语义（如实告知，
      不硬造字段），等下一轮带补丁重跑。
    """
    ent["parse_status"] = "needs_agent_vision"
    patch_entry = vision_patch.get(ent["path"])
    if patch_entry is None:
        ent["result"] = "失败"
        ent["reason"] = PARSE_FAIL_REASON["needs_agent_vision"]
        return
    p_text = str(patch_entry.get("text") or "")
    draft = patch_entry.get("fields_draft")
    drafts = [draft] if isinstance(draft, dict) else []
    cf = _VISION_MERGER.resume_fields_with_drafts(p_text, ent["file_name"], drafts)
    f = cf.to_dict()
    f["field_sources"] = dict(cf.field_sources)
    f["needs_review"] = list(cf.needs_review)
    ent["text"] = p_text
    ent["fields"] = f
    ent["warnings"] = list(f.get("warnings") or [])
    ent["parse_status"] = "ok"
    ent["backend"] = "agent_vision"          # notes 记 backend="agent_vision"
    ent["vision_patched"] = True
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
    ent["warnings"].append(note)
    if not p_text.strip():
        ent["warnings"].append("agent 补丁 text 为空——仅 fields_draft 生效，"
                               "简历全文留空，回合 2 请重点复核")
    if not _clean(f.get("phone")):
        ent["result"] = "失败"
        ent["reason"] = ("agent 兜底补丁合并后仍未解析到手机号，无法按手机号查重入库；"
                         "请人工确认简历里的联系方式后补录（不硬造字段）")
    else:
        ent["writable"] = True


# --------------------------------------------------------------------------- #
# 回读校验（按 filter 查，一次调用同时拿到 record_id 映射与读回值）
# --------------------------------------------------------------------------- #
def verify_by_filter(tbl: AITable, table_key: str, key_field: str,
                     keys: Sequence[str], fields: Sequence[str],
                     expected: Dict[Any, Dict[str, Any]],
                     attach_field: Optional[str] = None,
                     settle_waits: Sequence[float] = SETTLE_WAITS) -> Dict[str, Any]:
    """写后必回读（契约 D6），但**按业务键 filter 查**而不是按 record_id 查。

    为什么不用 `AITable.readback_verify`：`batch_upsert_by_key` 返回的 record_ids 是
    「本片 created ids + updated ids」拼接，**无法可靠对回具体行**；而本脚本必须知道
    每份简历落到哪个 record_id（要写进 candidates.json 给 C2 用）。按手机号 filter 查
    一次就同时拿到 record_id 映射 + 读回值，省一次调用。

    传播延迟：W-B 实测 create 后按条件查 ≈2.7s 才可命中，所以这里自带**有界**轮询
    （1.5/3.0/4.5s），绝不空转烧调用；轮询完仍不一致就如实报进 mismatch（D6）。
    """
    t0 = time.monotonic()
    calls0 = tbl.dws_calls
    keys = [k for k in (keys or []) if k]
    found: Dict[Any, Dict[str, Any]] = {}
    polls = 0
    mismatch: List[Dict[str, Any]] = []
    while True:
        recs: List[Dict[str, Any]] = []
        # filter 的 OR 条件也有 100 个上限，超出必须自己分片（每片仍是一次调用）
        for i in range(0, len(keys), 100):
            recs.extend(tbl.query_records(table_key, filter={key_field: keys[i:i + 100]},
                                          fields=list(fields), limit=100, all_pages=True))
        found = {}
        for r in recs:
            k = (r.get("cells") or {}).get(key_field)
            if isinstance(k, str):
                k = k.strip()
            if k is None:
                continue
            found.setdefault(k, r)
        mismatch = []
        for k, exp in (expected or {}).items():
            rec = found.get(k)
            if rec is None:
                continue
            cells = rec.get("cells") or {}
            for fk, ev in exp.items():
                if not values_equal(ev, cells.get(fk)):
                    mismatch.append({"key": k, "record_id": rec.get("record_id"),
                                     "field": fk, "expected": ev, "actual": cells.get(fk)})
        missing = [k for k in keys if k not in found]
        if not missing and not mismatch:
            break
        if polls >= len(settle_waits):
            break
        time.sleep(settle_waits[polls])
        polls += 1

    missing = [k for k in keys if k not in found]
    attach_missing: List[Any] = []
    if attach_field:
        for k, rec in found.items():
            if not (rec.get("cells") or {}).get(attach_field):
                attach_missing.append(k)
    return {
        "ok": not missing and not mismatch,
        "requested": len(keys), "found": len(found), "missing": missing,
        "mismatch": mismatch, "attachment_missing": attach_missing,
        "records": found, "settle_polls": polls,
        "dws_calls": tbl.dws_calls - calls0,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def reset_table(tbl: AITable, table_key: str) -> Dict[str, Any]:
    """--reset：清空表内全部记录（供重复测量用；生产 base 严禁使用）。"""
    recs = tbl.query_records(table_key, fields=["phone"], all_pages=True, max_pages=100)
    ids = [r["record_id"] for r in recs if r.get("record_id")]
    if not ids:
        return {"deleted": 0, "failed": [], "found": 0}
    res = tbl.batch_delete(table_key, ids)
    res["found"] = len(ids)
    return res


def run(args: argparse.Namespace) -> int:
    console = IntakeConsole()
    budget = WallBudget(args, WALL_BUDGET_DEFAULT)
    runner = ExtractionRunner(EXTRACT_CONCURRENCY, console)

    batch_id = args.batch_id or _new_batch_id()
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else \
        Path("/tmp/recruit-fast") / batch_id
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    report_path = out_dir / "intake_report.json"
    candidates_path = out_dir / "candidates.json"
    checkpoint_path = out_dir / "checkpoint.json"

    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    summary = {"new": 0, "overwrite": 0, "skip": 0, "fail": 0,
               "attachment_uploaded": 0, "attachment_failed": 0,
               # v3 §9#6 只增字段：本次「只补附件」路径的计数（记录不重建）
               "attachment_fixup_uploaded": 0, "attachment_fixup_failed": 0,
               # P3 只增字段：墙钟预算内未处理的文件数（重跑同一命令续跑）
               "pending_budget": 0,
               # P4a 只增字段：转 agent 多模态兜底的文件数（VISION_NEEDED 清单）
               "needs_agent_vision": 0}
    fatal: Optional[str] = None

    counter = DwsCallCounter()
    client = DwsClient(counter=counter, timeout=300, http_timeout=180)
    try:
        tbl = AITable(args.config, client=client)
    except Exception as exc:                      # config 缺失/格式错 → 立刻可见地失败
        fatal = "读取 config.json 失败：%s: %s" % (type(exc).__name__, exc)
        tbl = None

    console.banner()
    console.batch_info(batch_id, out_dir)
    if tbl is not None:
        console.base_info(tbl.base_name, tbl.base_id,
                          tbl.table_name("resume"), tbl.table_id("resume"))

    files: List[str] = list(args.files or [])
    store = CheckpointStore(checkpoint_path, batch_id, out_dir, args.config)

    if args.reset:
        # --reset 先把 checkpoint 清成空骨架并立即落盘：清表与重跑之间被杀也不会
        # 留下「表已清空但旧 done 还在」的假断点
        store.persist()
    if tbl is not None and args.reset:
        r = reset_table(tbl, "resume")
        console.reset_result(r.get("found", 0), r.get("deleted", 0),
                             len(r.get("failed") or []))
        if r.get("failed"):
            warnings.append("--reset 删除失败 %d 片：%s"
                            % (len(r["failed"]), json.dumps(r["failed"], ensure_ascii=False)[:300]))

    # ---- checkpoint（契约 D12 幂等续跑；P3 起逐条落盘，格式 version=2）----
    store.load(args.reset, console, warnings)
    done_md5 = store.done_md5

    # ---- P4a：agent 多模态兜底补丁（--apply-vision-patch，可缺省）----
    vision_patch: Dict[str, Dict[str, Any]] = {}
    if getattr(args, "apply_vision_patch", None):
        vision_patch, patch_err = _load_vision_patch(args.apply_vision_patch)
        if patch_err:
            fatal = "--apply-vision-patch %s" % patch_err
            warnings.append(fatal)
        else:
            console.vision_patch_loaded(len(vision_patch), args.apply_vision_patch)

    # ------------------------------------------------------------------ #
    # 阶段 1：提取 + 抽字段（纯本地，零 dws 调用）
    # P3：提取并发跑（EXTRACT_CONCURRENCY=4，多份扫描件并行 Vision OCR）；
    # 墙钟预算触顶时取消未开始的提取，对应文件进「未完成」清单（RESUME 续跑）。
    # ------------------------------------------------------------------ #
    entries: List[Dict[str, Any]] = []
    t_extract = time.monotonic()
    ex_by_idx = runner.run(files, budget)
    budget_reason = budget.pending_reason()
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
                # P4a：chain 终态 no_text_layer 不再判死 → agent 多模态兜底通道
                # （补丁覆盖则合并入库，未覆盖则 needs_agent_vision 如实失败）
                _agent_vision_entry(ent, vision_patch)
            else:
                ent["result"] = "失败"
                reason = PARSE_FAIL_REASON.get(ex.get("status"),
                                               "文件解析失败；请确认文件完整后重新提供")
                if ex.get("status") == "unsupported":
                    reason = "%s（扩展名 %s）" % (reason, ex.get("ext") or p.suffix or "(无)")
                if ex.get("error"):
                    reason = "%s（技术细节：%s）" % (reason, str(ex["error"])[:120])
                ent["reason"] = reason
        elif detect_scanned(ent["text"], ent.get("kind") or ""):
            # 双保险：extract_text 已判过，这里再判一次（契约 D11）。
            # P4a：与 chain 终态 no_text_layer 同语义，转 agent 多模态兜底通道
            _agent_vision_entry(ent, vision_patch)
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
        entries.append(ent)
    extract_ms = int((time.monotonic() - t_extract) * 1000)
    n_ok = sum(1 for e in entries if e["parse_status"] == "ok")
    n_pending = sum(1 for e in entries if e["result"] == "未完成")
    # ---- P4a：agent 多模态兜底清单（VISION_NEEDED）+ 20% 闸门（用户拍板）----
    vision_needed = [e for e in entries if e["parse_status"] == "needs_agent_vision"]
    vision_needed_paths = [e["path"] for e in vision_needed]
    summary["needs_agent_vision"] = len(vision_needed)
    vision_gated = bool(entries) and \
        (len(vision_needed) / float(len(entries))) > VISION_GATE_RATIO
    runner.summarize(entries, files, n_ok, n_pending, len(vision_needed), extract_ms)
    if vision_needed and not vision_gated:
        # 单行、空格分隔的绝对路径清单——agent 兜底协议触发器（见 HOTPATH.md）
        console.vision_needed(vision_needed_paths)
        console.vision_needed_hint(len(vision_needed))
    if vision_gated:
        # 20% 闸门：疑似整批格式问题 → 不写任何记录（halted 拦掉全部 dws 写阶段），
        # 退出码 0、报告 ok=true、partial=true、reason="vision_gate"
        budget.halted = True
        console.vision_gate(len(vision_needed), len(entries))
        for e in vision_needed:
            console.vision_gate_file(e["file_name"], e["path"])

    # 提取进度即刻落盘（record_written=False 的 progress 条目：只作断点可见性，
    # 不参与 done/done_md5 的跳过判定，重跑语义不变）
    for ent in entries:
        store.mark_progress(ent)
    store.persist()

    # ---- 预算检查点 A：写库前。已触顶 → graceful 停止，不进入任何 dws 写阶段 ----
    # done_md5 里的文件不标「未完成」：阶段 2 的本地跳过/补附件判定照常给它们结果
    if budget.budget_stopped or budget.over_budget():
        budget.halt_before_write(console)
        for ent in entries:
            if ent["result"] is None and ent["md5"] not in done_md5:
                ent["result"] = "未完成"
                ent["writable"] = False
                ent["reason"] = budget.halt_reason()

    # ------------------------------------------------------------------ #
    # 阶段 2：去重（checkpoint 与本批内部走**真 MD5**；库内附件 P4b 起也走真 MD5——
    #         比对键是库内「附件内容MD5」字段，老库无该字段则回退「文件名+字节大小」）
    # ------------------------------------------------------------------ #
    seen_md5: Dict[str, Tuple[int, str]] = {}
    need_lib_scan = False
    fixups: List[Dict[str, Any]] = []          # v3 §9#6：记录已写、只欠附件的补传队列
    for ent in entries:
        md5 = ent["md5"]
        if ent["result"] == "失败":
            continue
        if md5 and md5 in done_md5:
            prev = (store.done or {}).get(md5) or {}
            # v3 §9#6：checkpoint 把「记录已写」与「附件已传」分开记状态。
            # 兼容旧格式：done 里只记过写库成功的行 → record_written 缺省视为 True；
            # attachment_uploaded 缺省从旧的 attachment_status 推导。
            record_written = bool(prev.get("record_written", True))
            attach_done = bool(prev.get("attachment_uploaded",
                                        prev.get("attachment_status") == "uploaded"))
            ent["dedupe"] = prev.get("dedupe") or "overwrite"
            ent["record_id"] = prev.get("record_id")
            ent["attachment_status"] = prev.get("attachment_status") or "deferred"
            # 派生字段恢复：跳过路径不会重跑阶段 4，若不从 checkpoint 恢复，
            # 重跑产出的 candidates.json 会丢 org_guess/category_guess/归一 skills/
            # 归一 expected_location → C2 匹配会判「找不到同组织岗位」（实测接缝 bug，
            # W-G 修复）。旧格式 checkpoint 没存这些 → 用本地纯函数重算一遍（零 dws，
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
                # 本次没带 --no-attachment → **只补附件**，绝不重复 create（v3 §9#6）
                if ent["record_id"] and Path(ent["path"]).exists():
                    ent["writable"] = False
                    ent["fix_attachment"] = True
                    fixups.append(ent)
                    continue
                ent["result"] = "跳过"
                ent["writable"] = False
                warnings.append("《%s》checkpoint 显示附件欠传，但%s → 无法补传，"
                                "记录保持已入库状态"
                                % (ent["file_name"],
                                   "缺 record_id" if not ent["record_id"] else "本地文件不存在"))
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

    # ---- P4b：库内去重**选档**（内容级 MD5 优先；老库无该字段 → 回退「文件名+字节大小」）----
    # 硬要求：config/schema 里找不到「附件内容MD5」字段（客户现存库）时**不得崩溃、
    # 不得自建字段**——回退 NameSizeDeduper + 一条 warning（说明未启用内容级去重与
    # 如何启用）。建字段是 replicate 部署时的事。
    attach_md5_field: Optional[str] = None
    if tbl is not None:
        try:
            if ATTACH_MD5_FIELD_KEY in tbl.field_keys("resume"):
                attach_md5_field = ATTACH_MD5_FIELD_KEY
        except Exception as exc:                    # 防御：读 config 出问题也只回退，不崩
            warnings.append("读取简历库字段清单失败（%s: %s）→ 库内去重按老库回退处理"
                            % (type(exc).__name__, str(exc)[:120]))
    if tbl is not None and attach_md5_field is None:
        warnings.append(OLD_LIB_DEDUPE_WARNING)
        console.old_lib_dedupe_fallback()
    lib_deduper = (ContentHashDeduper(attach_md5_field) if attach_md5_field
                   else NameSizeDeduper())
    if tbl is not None and need_lib_scan and not args.no_dedupe_scan and not budget.halted:
        try:
            t0 = time.monotonic()
            scan = lib_deduper.scan(tbl, "resume",
                                    max_pages=DEDUPE_SCAN_MAX_PAGES)
            console.dedupe_scan(lib_deduper.key_label, scan.records, scan.indexed,
                                (lib_deduper.coverage_note()
                                 if isinstance(lib_deduper, ContentHashDeduper) else None),
                                time.monotonic() - t0, tbl.dws_calls)
            if isinstance(lib_deduper, ContentHashDeduper):
                # 老记录容忍：库里有 P4b 之前写入（无哈希）的记录 → 如实告警一条
                warnings.extend(lib_deduper.legacy_warning())
            if scan.truncated:
                # 缺陷2：翻页打满 max_pages 时旧实现静默截断，去重就此失效而用户无从得知
                warnings.append(
                    "库内去重扫描被截断：翻到 max_pages=%d（%d 页）仍有下一页，"
                    "只取回 %d 条记录，库内实际存量更多 → 本次「%s」比对不完整，"
                    "漏判重复上传的风险高。可加 --no-dedupe-scan 跳过库内比对"
                    "（此时只靠手机号查重兜底），或调大 DEDUPE_SCAN_MAX_PAGES 后重跑"
                    % (scan.max_pages, scan.pages, scan.records,
                       lib_deduper.key_label))
                console.dedupe_scan_truncated(scan.max_pages, scan.records)
            else:
                # 缺陷3 的行数护栏：这次全表扫描已经**免费**给出了行数，喂给写前护栏，
                # 省掉一次 record stats 调用（截断时数字不可信，就不喂）
                tbl.set_row_count("resume", scan.records)
        except DwsError as exc:
            warnings.append("库内附件查重扫描失败（%s/%s）：%s；本次跳过「%s」的库内比对"
                            % (exc.category, exc.code, exc.message[:200],
                               lib_deduper.key_label))

    for ent in entries:
        if ent["result"] is not None or not ent["writable"]:
            continue
        d = lib_deduper.decide(ent["file_name"], ent["size"], ent.get("md5"))
        warnings.extend(d.warnings)
        if d.action == "skip":
            ent["result"] = "跳过"
            ent["writable"] = False
            ent["dedupe"] = d.dedupe
            ent["record_id"] = d.record_id
            ent["reason"] = d.reason
        elif d.reason:
            # P4b：同名同大小但**内容不同** → 不判重复，走覆盖更新语义
            # （new/overwrite 由下面的手机号查重决定），说明进清单该行
            ent["dedupe_note"] = d.reason

    # ---- P4a 20% 闸门执行：进入任何 dws 写阶段之前，本轮**零写入** ----
    # （库内扫描已被 halted 拦掉；这里把仍未定论的条目全部转「未完成」并清空
    # 补传队列，阶段 3~8 因此自然全跳过；重跑同一命令续处理，checkpoint 幂等）
    if vision_gated:
        fixups = []
        for ent in entries:
            if ent["result"] is None:
                ent["result"] = "未完成"
                ent["writable"] = False
                if ent.get("fix_attachment"):
                    ent["reason"] = ("20% 闸门触发（本批 %d/%d 份读不出文字 > %.0f%%），"
                                     "本轮不写任何记录，补传附件一并顺延；确认后重跑"
                                     "同一命令续补"
                                     % (len(vision_needed), len(entries),
                                        VISION_GATE_RATIO * 100))
                else:
                    ent["reason"] = ("20%% 闸门触发（本批 %d/%d 份读不出文字 > %.0f%%），"
                                     "疑似整批格式问题，本轮未写任何记录；请确认后重跑"
                                     "同一命令，或提供文字版简历"
                                     % (len(vision_needed), len(entries),
                                        VISION_GATE_RATIO * 100))

    # ------------------------------------------------------------------ #
    # 阶段 3：一次批量手机号查重（契约要求：单次 filter 查询判 new/overwrite/conflict）
    # ------------------------------------------------------------------ #
    phone_deduper = PhoneDeduper()
    pend = [e for e in entries if e["result"] is None and e["writable"]]
    if tbl is not None and pend:
        phones = sorted({str(_clean(e["fields"].get("phone"))) for e in pend})
        try:
            t0 = time.monotonic()
            calls0 = counter.calls
            scan = phone_deduper.scan(tbl, phones, "resume")
            console.phone_scan(len(phones), scan.indexed,
                               time.monotonic() - t0, counter.calls - calls0)
        except DwsError as exc:
            fatal = "批量手机号查重失败（%s/%s）：%s" % (exc.category, exc.code, exc.message[:300])
            warnings.append(fatal)

    for ent in entries:
        if ent["result"] is not None or not ent["writable"]:
            continue
        phone = str(_clean(ent["fields"].get("phone")))
        name = _clean(ent["fields"].get("name"))
        d = phone_deduper.decide(phone, name, ent["seq"], ent["file_name"])
        warnings.extend(d.warnings)
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
    to_write: List[Dict[str, Any]] = []
    all_skills: List[str] = []
    all_locations: List[str] = []
    known_locs: List[str] = []
    if tbl is not None:
        try:
            known_locs = [o.get("name") for o in
                          ((tbl.config.get("options") or {}).get("resume") or {})
                          .get("expected_location", []) if o.get("name")]
        except Exception:
            known_locs = []

    for ent in entries:
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
                warnings.append("《%s》组织归属判不了（制造命中 %d / 职能命中 %d），"
                                "组织留空待 Turn 2 确认；提示值=%s"
                                % (ent["file_name"], org_counts.get("stage2_mfg", 0),
                                   org_counts.get("stage2_func", 0), org or "无"))
            if loc_fallback:
                ent["warnings"].append("期望地点未明确/为多城市，按老插件铁律兜底填「%s」"
                                       % LOCATION_FALLBACK)
            if f.get("years_experience_source") == "estimated":
                ent["warnings"].append("工作年限 %s 是**估算值**（years_source=estimated），"
                                       "会直接喂给「经验年限一票否决」硬门槛，"
                                       "Turn 2 必须用 evidence 原文复核（契约 D13）"
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

    # ------------------------------------------------------------------ #
    # 阶段 5：一次 ensure_options 补齐本批全部新技能标签 / 期望地点（只增不删）
    # ------------------------------------------------------------------ #
    if tbl is not None and to_write:
        for field_key, names in (("skills", all_skills), ("expected_location", all_locations)):
            if not names:
                continue
            try:
                t0 = time.monotonic()
                calls0 = counter.calls
                opts = tbl.ensure_options("resume", field_key, names)
                console.ensure_options(field_key, len(names), len(opts),
                                       time.monotonic() - t0, counter.calls - calls0)
            except Exception as exc:
                warnings.append("ensure_options(resume.%s) 失败：%s: %s；"
                                "写入时服务端通常会自动补选项，但建议重跑本步确认"
                                % (field_key, type(exc).__name__, str(exc)[:200]))

    # ------------------------------------------------------------------ #
    # 阶段 6：并发上传附件（契约 D5，一律用原始文件名），fileToken 随 upsert 一次写入
    # P3：按并发度分片串行推进，每片完成后**立即落 checkpoint**（附件状态逐条持久化）；
    # 片间检查墙钟预算，触顶即停止上传——但**已进提交阶段的记录照常 upsert**（欠附件的
    # 记 record_written=True + attachment_uploaded=False，重跑走 6b 只补附件不重建记录）。
    # ------------------------------------------------------------------ #
    if tbl is not None and to_write and not args.no_attachment:
        chunk_n = max(1, int(args.concurrency))
        t0 = time.monotonic()
        calls0 = counter.calls
        truncated_at = len(to_write)
        for ci in range(0, len(to_write), chunk_n):
            if budget.over_budget():
                truncated_at = ci
                budget.budget_stopped = True
                break
            chunk = to_write[ci:ci + chunk_n]
            results = tbl.upload_attachments([e["path"] for e in chunk],
                                             concurrency=args.concurrency)
            for ent, res in zip(chunk, results):
                if res.get("ok") and res.get("cell"):
                    ent["row"]["attachment"] = res["cell"]
                    # P4b 写入侧：附件上传成功后，把**本地文件真 MD5**（提取层已算好，
                    # 与上传的是同一个文件）随记录写进「附件内容MD5」字段——库内内容级
                    # 去重就靠它。字段不存在（老库）时 attach_md5_field 为 None，一个
                    # 键都不写（build_cells 遇到没映射的键会报错，绝不硬塞）。
                    if attach_md5_field and ent.get("md5"):
                        ent["row"][attach_md5_field] = ent["md5"]
                    ent["attachment_status"] = "uploaded"
                    summary["attachment_uploaded"] += 1
                else:
                    ent["attachment_status"] = "failed"
                    summary["attachment_failed"] += 1
                    ent["warnings"].append("附件上传失败：%s" % (res.get("error") or "未知错误"))
                    warnings.append("《%s》附件上传失败（%s/%s）：%s；简历正文已照常入库，"
                                    "可后续用「补传附件」重跑"
                                    % (ent["file_name"], res.get("category"), res.get("code"),
                                       str(res.get("error"))[:200]))
                # 附件状态一确立就落盘（progress 条目；record_written 仍为 False）
                store.mark_attachment(ent)
            store.persist()
        if truncated_at < len(to_write):
            for ent in to_write[truncated_at:]:
                ent["attachment_status"] = "deferred"
                msg = budget.deferred_attachment_msg()
                ent["warnings"].append(msg)
                warnings.append("《%s》%s" % (ent["file_name"], msg))
                budget.mark_deferred(ent["file_name"])
        console.upload_summary(args.concurrency, summary["attachment_uploaded"],
                               summary["attachment_failed"],
                               len(to_write) - truncated_at,
                               int((time.monotonic() - t0) * 1000),
                               counter.calls - calls0)

    # ------------------------------------------------------------------ #
    # 阶段 6b：补传附件（v3 §9#6）——上次 --no-attachment 或附件失败、记录已写库的文件，
    #          本次只补附件：upload 拿 fileToken → 按 record_id batch_update 附件字段，
    #          **绝不重复 create**；写后回读附件非空（D6），失败如实报并保留 checkpoint
    #          欠传状态（下次重跑继续补）。
    # ------------------------------------------------------------------ #
    if tbl is not None and fixups and not fatal:
        # ---- 主控裁决回写（P4a）：补传路径每轮最多处理 FIXUP_ROUND_MAX 份，
        # 超出 defer 到下一轮 RESUME（防大批量补传把墙钟拖爆；记录保持已入库）----
        if len(fixups) > FIXUP_ROUND_MAX:
            n_all = len(fixups)
            deferred_fix = fixups[FIXUP_ROUND_MAX:]
            fixups = fixups[:FIXUP_ROUND_MAX]
            for ent in deferred_fix:
                ent["result"] = "未完成"
                ent["writable"] = False
                ent["reason"] = ("记录上次已入库；补传附件队列 %d 份超过单轮上限 %d，"
                                 "本份顺延到下一轮——重跑同一命令续补（RESUME，幂等）"
                                 % (n_all, FIXUP_ROUND_MAX))
                budget.mark_deferred(ent["file_name"])
            console.fixup_deferred(n_all, FIXUP_ROUND_MAX, len(fixups), len(deferred_fix))
        t0 = time.monotonic()
        calls0 = counter.calls
        results = tbl.upload_attachments([e["path"] for e in fixups],
                                         concurrency=args.concurrency)
        updates: List[Dict[str, Any]] = []
        for ent, res in zip(fixups, results):
            if res.get("ok") and res.get("cell"):
                fix_cells: Dict[str, Any] = {"attachment": res["cell"]}
                # P4b 懒回填：补传附件时顺带把「附件内容MD5」补上——P4b 之前入库的
                # 老记录（无哈希、只能按文件名+大小回退判重）就此收敛到内容级去重
                if attach_md5_field and ent.get("md5"):
                    fix_cells[attach_md5_field] = ent["md5"]
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
                warnings.append("《%s》补传附件上传失败（%s/%s）：%s；记录保持已入库，"
                                "重跑同一命令可继续补"
                                % (ent["file_name"], res.get("category"), res.get("code"),
                                   str(res.get("error"))[:200]))
        ok_rids: set = set()
        if updates:
            r = tbl.batch_update("resume", updates)
            failed_rids = {str(f.get("record_id") or (f.get("row") or {}).get("record_id"))
                           for f in (r.get("failed") or [])}
            ids = [u["record_id"] for u in updates if str(u["record_id"]) not in failed_rids]
            # 写后必回读：附件字段非空才算补传成功（有界轮询，不空转烧调用）
            polls = 0
            while ids:
                try:
                    recs = tbl.query_records("resume", record_ids=ids, fields=["attachment"])
                except DwsError as exc:
                    warnings.append("补传附件回读查询失败（%s）：按未确认处理，请重跑复核"
                                    % str(exc)[:160])
                    recs = []
                ok_rids = {r2.get("record_id") for r2 in recs
                           if (r2.get("cells") or {}).get("attachment")}
                if len(ok_rids) >= len(ids) or polls >= len(SETTLE_WAITS):
                    break
                time.sleep(SETTLE_WAITS[polls])
                polls += 1
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
                    # 补传成功即刻落盘（P3 增量 checkpoint：附件状态一确立就持久化）
                    store.mark_fixup_uploaded(ent)
                else:
                    ent["result"] = "失败"
                    ent["attachment_status"] = "failed"
                    ent["reason"] = ("记录上次已入库（本次未重复建记录），但补传的附件"
                                     "未确认写入（回读为空或服务端拒绝）；请重跑同一命令复核")
                    summary["attachment_failed"] += 1
                    summary["attachment_fixup_failed"] += 1
                    warnings.append("《%s》补传附件未确认写入（record_id=%s）；"
                                    "记录保持已入库，重跑同一命令可继续补"
                                    % (ent["file_name"], rid))
        console.fixup_summary(len(fixups), summary["attachment_fixup_uploaded"],
                              summary["attachment_fixup_failed"],
                              int((time.monotonic() - t0) * 1000),
                              counter.calls - calls0)

    # ------------------------------------------------------------------ #
    # 阶段 7：一次批量写简历库（batch_upsert_by_key，unique=手机号，≤100/片）
    # ------------------------------------------------------------------ #
    upsert_res: Dict[str, Any] = {}
    if tbl is not None and to_write and not fatal:
        rows_in = [e["row"] for e in to_write]
        t0 = time.monotonic()
        calls0 = counter.calls
        try:
            upsert_res = tbl.batch_upsert_by_key("resume", "phone", rows_in)
        except DwsError as exc:
            fatal = "批量写简历库失败（%s/%s）：%s" % (exc.category, exc.code, exc.message[:300])
            warnings.append(fatal)
        console.upsert_summary(upsert_res.get("created"), upsert_res.get("updated"),
                               len(upsert_res.get("failed") or []),
                               time.monotonic() - t0, counter.calls - calls0)
        for fl in (upsert_res.get("failed") or []):
            row = fl.get("row") or {}
            ph = row.get("phone")
            fn = next((e["file_name"] for e in to_write
                       if e["row"].get("phone") == ph), "(未定位到文件)")
            warnings.append("写入失败《%s》手机号 %s：%s" % (fn, ph, str(fl.get("reason"))[:240]))
            for ent in to_write:
                if ent["row"].get("phone") == ph:
                    ent["result"] = "失败"
                    ent["writable"] = False
                    ent["reason"] = "写入简历库失败：%s" % str(fl.get("reason"))[:200]

    # ------------------------------------------------------------------ #
    # 阶段 8：回读校验（一次 filter 查询同时拿 record_id 映射 + 读回值）
    # ------------------------------------------------------------------ #
    verify: Dict[str, Any] = {}
    written = [e for e in to_write if e["result"] is None]
    if tbl is not None and written and not fatal:
        rb_fields = ["name", "phone", "education", "org", "full_text", "attachment",
                     "expected_location", "skills", ATTACH_MD5_FIELD_KEY]
        rb_fields = [k for k in rb_fields if k in tbl.field_keys("resume")]
        expected: Dict[str, Dict[str, Any]] = {}
        for ent in written:
            exp = {}
            for fk in ("name", "phone", "education", "org", "full_text",
                       "expected_location", "skills", ATTACH_MD5_FIELD_KEY):
                if fk in rb_fields and ent["row"].get(fk) is not None:
                    exp[fk] = ent["row"][fk]
            expected[str(_clean(ent["row"]["phone"]))] = exp
        t0 = time.monotonic()
        verify = verify_by_filter(tbl, "resume", "phone",
                                  [str(_clean(e["row"]["phone"])) for e in written],
                                  rb_fields, expected,
                                  attach_field=("attachment"
                                                if ("attachment" in rb_fields
                                                    and not args.no_attachment)
                                                else None))
        for ent in written:
            ph = str(_clean(ent["row"]["phone"]))
            rec = (verify.get("records") or {}).get(ph)
            ent["record_id"] = rec["record_id"] if rec else None
            # 写库状态一确认（回读到 record_id）就逐条落盘（P3 增量 checkpoint）：
            # 之后任意瞬间被杀，重跑都能按 done 条目整条跳过 / 只补附件
            if ent["record_id"] and ent["md5"]:
                store.confirm_written(ent, "新入库" if ent["dedupe"] == "new" else "已覆盖")
        console.readback_summary(verify.get("requested", 0), verify.get("found", 0),
                                 len(verify.get("mismatch") or []),
                                 len(verify.get("attachment_missing") or []),
                                 verify.get("settle_polls", 0), time.monotonic() - t0,
                                 verify.get("dws_calls", 0))
        if verify.get("missing"):
            warnings.append("回读未读到 %d 条记录（手机号 %s）；写入可能未生效，"
                            "请在后续回合重跑本步复核（契约 D6/D7）"
                            % (len(verify["missing"]), list(verify["missing"])[:5]))
        for mm in (verify.get("mismatch") or [])[:20]:
            warnings.append("回读不一致：手机号 %s 字段 %s 期望 %r 实得 %r"
                            % (mm.get("key"), mm.get("field"),
                               str(mm.get("expected"))[:60], str(mm.get("actual"))[:60]))
        # 附件缺失 → 补一次 batch_update（正常路径不触发；见模块文档纪律 2）
        amiss = verify.get("attachment_missing") or []
        if amiss and not args.no_attachment:
            fixes = []
            for ent in written:
                ph = str(_clean(ent["row"]["phone"]))
                if ph in amiss and ent["row"].get("attachment"):
                    fix_cells2: Dict[str, Any] = {"attachment": ent["row"]["attachment"]}
                    # 附件与它的「附件内容MD5」必须同步写回（P4b：只补附件不补哈希
                    # 会让这条记录永远停在老键回退档）
                    if attach_md5_field and ent["row"].get(attach_md5_field):
                        fix_cells2[attach_md5_field] = ent["row"][attach_md5_field]
                    fixes.append({"record_id": ent["record_id"], "cells": fix_cells2})
            if fixes:
                r = tbl.batch_update("resume", fixes)
                warnings.append("回读发现 %d 条附件缺失，已补一次 batch_update（updated=%s failed=%d）"
                                % (len(fixes), r.get("updated"), len(r.get("failed") or [])))

    # ------------------------------------------------------------------ #
    # 阶段 9：组装 rows / candidates / summary
    # ------------------------------------------------------------------ #
    for ent in entries:
        if ent["result"] is None:
            if not ent["writable"]:
                ent["result"] = "失败"
                ent["reason"] = ent["reason"] or "未能入库（原因见 warnings）"
            elif fatal or tbl is None or not ent.get("record_id"):
                # 契约 D6：「写后必回读」没读到 record_id 就不许报成功。
                # 覆盖三种情况：config/查重阶段致命错误、批量写整批失败、回读没读到。
                ent["result"] = "失败"
                ent["reason"] = fatal or (
                    "已提交写入但回读没读到该记录（record_id 为空），无法确认入库；"
                    "请在下一回合重跑本步复核（幂等，不会产生重复记录）")
            elif ent["dedupe"] == "new":
                ent["result"] = "新入库"
                ent["reason"] = "新候选人，已写入简历库"
            else:
                ent["result"] = "已覆盖"
                ent["reason"] = "手机号已存在，本次用最新简历覆盖更新"
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
        # P4b：「同名同大小但内容不同 → 不判重复、按新版本覆盖更新」的说明进清单该行
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
        rows.append({"seq": ent["seq"], "file_name": ent["file_name"],
                     "result": ent["result"], "reason": reason})

        f = ent.get("fields") or {}
        secs = f.get("sections") or {}
        # 契约 D14：期望地点一律不留空。写库的行用 row 里的归一值；未写库（无手机号/
        # 冲突/重复）的行也按同一规则归一，保证 candidates.json 交给 C2 时口径一致。
        loc_out = (ent.get("row") or {}).get("expected_location")
        if not loc_out and ent["parse_status"] == "ok":
            loc_out = normalize_location(f.get("expected_location"), known_locs)[0]
        # P5 身份原文行（安全阀的原文保留面，判据见 shared/fields/identity.py）：
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
            # P5 只增键：邮箱值 + 姓名来源 + 解析 backend（C2 身份阀判据）
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
            # 契约 D13：工作年限来源必须透传给 C2 / Turn 2
            "years_source": f.get("years_experience_source"),
            # P4a（只增键）：agent 多模态兜底的复核清单与逐字段来源。
            # needs_review = 取自补丁 fields_draft 的字段名（field_source=agent_vision），
            # 回合 2 必须用 evidence 原文复核；未经补丁的候选人两键为空。
            "needs_review": [str(x) for x in (f.get("needs_review") or [])],
            "field_sources": dict(f.get("field_sources") or {}),
            # P5 只增键（姓名复核判据的另一半；见 CANDIDATE_EXTRA_FIELDS 注释）
            "name_source": f.get("name_source"),
            "parse_backend": ent.get("backend"),
        }
        if ent["parse_status"] != "ok" or ent["result"] == "未完成":
            # 契约 D11：不硬造字段 → 一律留空；「未完成」（预算内未处理）同样不给字段
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
        candidates.append(cand)

        # checkpoint（v3 §9#6）：「记录已写」与「附件已传」分开记状态；
        # 同时存档派生字段（org/category/skills/expected_location），跳过重跑时恢复，
        # 保证 candidates.json 跨次运行字段完整（W-G 修复的接缝 bug）。
        # 新入库/已覆盖 → 记全新条目；补传附件路径（fix_attachment）→ 合并更新旧条目。
        # P3：写库成功的条目在阶段 8 回读确认后已**逐条落盘**；这里是终稿重建（内容
        # 由同一个 CheckpointStore.done_entry 构造，两处一致），随收尾整体再写一次。
        if ent["md5"] and (ent["result"] in ("新入库", "已覆盖")
                           or (ent.get("fix_attachment") and ent["result"] in ("跳过", "失败"))):
            store.mark_written(ent, ent["result"])
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
            warnings.append("《%s》%s" % (ent["file_name"], w))

    report = IntakeReport(console, report_path, candidates_path,
                          OLD_TURNS_PER_FILE, NEW_TURNS, VISION_GATE_RATIO)
    ok = report.assemble(
        args=args, budget=budget.budget, t_start=budget.t_start, counter=counter,
        entries=entries, files=files, to_write=to_write, rows=rows,
        summary=summary, warnings=warnings, fatal=fatal,
        deferred_files=budget.deferred_files, budget_stopped=budget.budget_stopped,
        vision_needed_paths=vision_needed_paths, vision_gated=vision_gated,
        fixups=fixups)
    store.finalize(ok, summary, report.dws_calls, report.elapsed_ms)
    report.write(tbl, batch_id,
                 str(Path(args.config).expanduser().resolve()), candidates)
    store.write_final()

    # ---- 人读清单（沿用老插件「清单式留痕」铁律）----
    report.emit(extract_ms, checkpoint_path, _resume_cmd)
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="intake_resume.py",
        description="简历入库 Turn 1：一个进程内做完提取/抽字段/查重/批量写/附件/回读，"
                    "产出 candidates.json + intake_report.json + checkpoint.json")
    ap.add_argument("--config", required=True, help="config.json 绝对路径（唯一 ID 源，契约 D8）")
    ap.add_argument("--files", nargs="*", default=[], help="简历文件路径（可多个）")
    ap.add_argument("--out-dir", default=None,
                    help="产物目录绝对路径；缺省 /tmp/recruit-fast/<batch_id>/")
    ap.add_argument("--no-attachment", action="store_true",
                    help="跳过附件上传（老插件「方案C·延后」，之后可补传）")
    ap.add_argument("--reset", action="store_true",
                    help="先清空 resume 表全部记录（仅供重复性能测量；生产 base 严禁使用）")
    ap.add_argument("--wall-budget", type=float, default=WALL_BUDGET_DEFAULT,
                    help="墙钟预算秒数（默认 %.0f，必须小于 agent 工具 120s 超时）。"
                         "到点 graceful 停：checkpoint 逐条落盘、打印已完成/未完成清单与"
                         "一行 RESUME: 提示、退出码 0、报告 ok=true 且 partial=true；"
                         "续跑 = 重跑同一条命令（checkpoint 幂等，不产生重复记录）"
                         % WALL_BUDGET_DEFAULT)
    ap.add_argument("--apply-vision-patch", default=None,
                    help="agent 多模态兜底补丁 json 绝对路径（P4a）。补丁 schema："
                         '{"<文件绝对路径>": {"text": "...", '
                         '"fields_draft": {"name": "...", "phone": "...", ...}, '
                         '"confidence": 0.0, "notes": "..."}}。'
                         "合并规则：先对 patch.text 跑正则抽取，regex 有值的字段用 regex，"
                         "regex 为空才取 fields_draft；取自草稿的字段打 "
                         "field_source=agent_vision 并追加进该候选人 needs_review，"
                         "patch.text 写入简历全文并记 backend=agent_vision。"
                         "agent 只产出补丁、绝不写库——入库仍走本脚本正常查重/护栏/回读")
    # ---- W-I 性能优化 O2（W-J 移植）：合并入口（入库 + 生成判定输入 一次调用做完，省一个 agent 回合）----
    # 只加这三个参数；岗位预筛/字段裁剪（O4）默认全开、由 build_match_input 内部控制，
    # O4-c（--emit-stdout）实测负收益**不移植** → 判定输入一律走 SHARD: 路径 Read 分片文件。
    ap.add_argument("--auto-match", action="store_true",
                    help="入库成功后在**同一进程内**接着跑 build_match_input，产出 digest.json + "
                         "分片并打印 SHARD: 路径（省一个编排回合）。语义判定与 apply_decisions "
                         "仍是独立回合，本开关不代替它们。")
    ap.add_argument("--match-out-dir", default=None,
                    help="--auto-match 时 digest 的输出目录；缺省 = <out-dir>/../match")
    ap.add_argument("--max-per-batch", type=int, default=8,
                    help="--auto-match 时的分片人数上限（契约 D3，默认 8）")
    # 非冻结面的内部旋钮（有默认值，SKILL.md 不需要暴露）
    ap.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--concurrency", type=int, default=UPLOAD_CONCURRENCY,
                    help=argparse.SUPPRESS)
    ap.add_argument("--no-dedupe-scan", action="store_true", help=argparse.SUPPRESS)
    return ap


def auto_match(args: argparse.Namespace, intake_rc: int) -> int:
    """O2（W-I 性能优化，W-J 移植）：入库成功后**同进程**接着生成定向匹配判定输入。

    省掉的是「agent 再开一个工具回合去调 build_match_input.py」这一个回合
    （实测一个 agent 工具回合边际墙钟 ≈4.2~5.8s，其中命令本身仅 ≈1.3s）。

    纪律（不许为了省回合牺牲正确性）：
      * 入库没成功（rc != 0）→ **不**继续匹配：拿半成品 candidates.json 去判定会污染结论。
        如实打印原因并把入库的退出码原样回传（D7 失败不隐藏）。
      * build_match_input 导入失败 / 抛异常 → 打印原因、退出码 1；入库产物已落地，agent 只需
        单独重跑 build_match_input.py，**不会重复入库**（checkpoint 幂等，D12）。
      * 语义判定（Turn 2）与 apply_decisions（Turn 3）仍是独立回合，本开关不代替它们。
      * 岗位预筛/字段裁剪（O4）由 build_match_input 默认开启；判定输入**不打 stdout**
        （O4-c 实测负收益，未移植），agent 按打印的 SHARD: 路径 Read 分片文件。
    """
    console = IntakeConsole()
    if intake_rc != 0:
        console.auto_match_skip_banner()
        console.auto_match_skip_failed(intake_rc)
        return intake_rc
    if getattr(args, "_partial", False):
        console.auto_match_skip_banner()
        console.auto_match_skip_partial()
        return intake_rc
    if not args.out_dir:
        console.auto_match_skip_banner()
        console.auto_match_skip_no_out_dir()
        return intake_rc

    out_dir = Path(args.out_dir).expanduser().resolve()
    candidates = out_dir / "candidates.json"
    if not candidates.exists():
        console.auto_match_skip_banner()
        console.auto_match_skip_no_candidates(candidates)
        return 1
    match_out = (Path(args.match_out_dir).expanduser().resolve() if args.match_out_dir
                 else out_dir.parent / "match")
    match_out.mkdir(parents=True, exist_ok=True)

    bmi_dir = _PLUGIN_ROOT / "skills" / "match-verify" / "scripts"
    if str(bmi_dir) not in sys.path:
        sys.path.insert(0, str(bmi_dir))
    console.auto_match_banner()
    t0 = time.time()
    try:
        from build_match_input import build_digest, report_and_emit  # noqa: E402
    except Exception as exc:
        console.auto_match_import_error(type(exc).__name__, exc, bmi_dir)
        console.auto_match_rerun_hint()
        return 1
    try:
        res = build_digest(str(Path(args.config).expanduser()), str(candidates), str(match_out),
                           max_per_batch=args.max_per_batch)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        console.auto_match_error(type(exc).__name__, exc)
        return 1
    rc = report_and_emit(res)
    console.auto_match_wall(time.time() - t0, match_out)
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    console = IntakeConsole()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
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
    try:
        rc = run(args)
    except KeyboardInterrupt:
        console.interrupted()
        return 130
    except Exception as exc:                        # 契约 D7：绝不静默早退
        import traceback
        out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else \
            Path("/tmp/recruit-fast") / (args.batch_id or _new_batch_id())
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
    # O2（W-I 优化，W-J 移植）：入库成功后同进程接着生成判定输入，省一个编排回合。
    # run(args) 抛异常时上面已 return，不会走到这里 → 入库崩溃绝不触发 auto-match。
    if getattr(args, "auto_match", False):
        return auto_match(args, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
