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

架构（P7 OO 重构后：本文件只剩 CLI 装配 + 编排调用）
------------------------------------------------
    intake_resume.py（本文件）  build_parser / main / auto_match——CLI 是对外契约
                               （参数名/默认值/校验/退出码），所以留在脚本里。
    shared/intake/pipeline.py  IntakePipeline —— run() 的 10 个阶段（1~9 + 6b）编排
                               与全部业务判定：条目状态机、checkpoint 跳过/补传判定、
                               20% 闸门、组织/分类/地点预判、写入行与 candidates 组装。
    shared/intake/*            console（stdout 唯一出口）· report（14 键报告 + 产物落盘
                               + 人读清单）· checkpoint（增量 checkpoint v2）· budget
                               （墙钟预算）· extraction_runner（并发提取池）·
                               table_gateway（dws IO 边界）· readback（写后回读）。
    shared/extraction/*        提取责任链：pypdf → JXA-PDFKit → Vision OCR（Tier 1.5）
                               → docx-zip → doc-piece → image → **agent 多模态补丁
                               （Tier 2，agent_patch_ext）**。`--apply-vision-patch`
                               只是把补丁表装进 Tier 2；合并（regex 优先、草稿补空、
                               field_source=agent_vision + needs_review）在链上做。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# --------------------------------------------------------------------------- #
# sys.path：脚本在 skills/<name>/scripts/ 下，插件根 = parents[3]
# --------------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PLUGIN_ROOT / "shared"
_VENDOR_DIR = _SHARED_DIR / "vendor"
for _p in (str(_SHARED_DIR), str(_VENDOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intake.console import IntakeConsole            # noqa: E402
from intake.pipeline import (                       # noqa: E402
    UPLOAD_CONCURRENCY,
    WALL_BUDGET_DEFAULT,
    IntakePipeline,
)


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
    invalid = IntakePipeline.validate_args(args, console)
    if invalid is not None:
        return invalid
    try:
        rc = IntakePipeline(args, console).run()
    except KeyboardInterrupt:
        console.interrupted()
        return 130
    except Exception as exc:                        # 契约 D7：绝不静默早退
        return IntakePipeline.crash_artifact(args, exc, console)
    # O2（W-I 优化，W-J 移植）：入库成功后同进程接着生成判定输入，省一个编排回合。
    # run() 抛异常时上面已 return，不会走到这里 → 入库崩溃绝不触发 auto-match。
    if getattr(args, "auto_match", False):
        return auto_match(args, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
