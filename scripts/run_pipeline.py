#!/usr/bin/env python3
"""
编排脚本：入库三步走（emit → agent exec → replay），支持简历和岗位两种入库类型

用法：
  Step 1 (emit):   python3 scripts/run_pipeline.py --phase emit --intake-type <resume|job> \
                       --config <config.json> --files <file1> <file2> ... --out-dir <dir>
  Step 2 (exec):   agent 逐条执行 stdout 打印的 dws 命令，
                   每条结果写入 <out-dir>/dws_out_<seq>.json
  Step 3 (replay): python3 scripts/run_pipeline.py --phase replay --intake-type <resume|job> \
                       --config <config.json> --out-dir <dir> \
                       --replay-path <dir>/dws_results.json

Phase emit 做什么：
  1. 以子进程方式运行 intake 脚本（emit 模式，不传 --replay-path）
     - resume: skills/resume-intake/scripts/intake_resume.py（可传 --wall-budget）
     - job:    skills/job-intake/scripts/intake_job.py（无 --wall-budget）
  2. 读取 <out-dir>/dws_commands.json
  3. 解析每条命令的 argv，按类型分类
  4. 对附件上传命令：从 argv 中提取 --file-name，匹配本地文件路径
  5. 对 upsert/create/update 命令：从 argv 中提取 --records（内联 JSON），保存为模板文件
  6. 向 stdout 打印结构化 JSON（包含所有命令信息和执行步骤）

Phase replay 做什么：
  1. 以子进程方式运行 intake 脚本（replay 模式，传 --replay-path）
  2. 读取 <out-dir>/intake_report.json
  3. 向 stdout 打印最终摘要

注意：
  - 本脚本不直接调用 dws（dws 是宿主 shim，Python 子进程无法拿到真实结果）
  - 本脚本只负责"准备"和"重放"，真正的 dws 执行由 agent 通过 Bash 工具完成
  - --intake-type 默认 resume（向后兼容）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
SHARED_DIR = str(PLUGIN_ROOT / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from performance_timing import (  # noqa: E402
    TIMING_FILE_NAME,
    append_event,
    read_summary,
    start_run,
)

INTAKE_SCRIPTS = {
    "resume": PLUGIN_ROOT / "skills" / "resume-intake" / "scripts" / "intake_resume.py",
    "job": PLUGIN_ROOT / "skills" / "job-intake" / "scripts" / "intake_job.py",
}

# ---------------------------------------------------------------------------
# 命令分类
# ---------------------------------------------------------------------------

CMD_RECORD_QUERY = "record query"
CMD_FIELD_GET = "field get"
CMD_ATTACHMENT_UPLOAD = "attachment upload"
CMD_RECORD_UPSERT = "record upsert"
CMD_RECORD_CREATE = "record create"
CMD_RECORD_UPDATE = "record update"
CMD_RECORD_STATS = "record stats"
CMD_FIELD_CREATE = "field create"
CMD_UNKNOWN = "unknown"


def classify_command(argv: List[str]) -> str:
    """根据 argv 判定 dws 命令类型。

    argv 格式: ["dws", "aitable", "record", "query", "--base-id", ...]
    argv[0] = "dws", argv[1] = "aitable", argv[2] = 子命令域, argv[3] = 动作
    """
    if len(argv) < 4:
        return CMD_UNKNOWN
    argv_strs = [str(a) for a in argv]
    # 取 argv[2:] 中的关键词（跳过 "dws" 和 "aitable"）
    if argv[2] == "record" and argv[3] == "query":
        return CMD_RECORD_QUERY
    elif argv[2] == "record" and argv[3] == "stats":
        return CMD_RECORD_STATS
    elif argv[2] == "field" and argv[3] == "get":
        return CMD_FIELD_GET
    elif argv[2] == "field" and argv[3] == "create":
        return CMD_FIELD_CREATE
    elif argv[2] == "attachment" and argv[3] == "upload":
        return CMD_ATTACHMENT_UPLOAD
    elif argv[2] == "record" and argv[3] == "upsert":
        return CMD_RECORD_UPSERT
    elif argv[2] == "record" and argv[3] == "create":
        return CMD_RECORD_CREATE
    elif argv[2] == "record" and argv[3] == "update":
        return CMD_RECORD_UPDATE
    return CMD_UNKNOWN


def extract_flag_value(argv: List[str], flag: str) -> Optional[str]:
    """从 argv 中提取 --flag value 对的值。"""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


# ---------------------------------------------------------------------------
# Phase: emit
# ---------------------------------------------------------------------------

def phase_emit(args: argparse.Namespace) -> int:
    """Phase emit：运行 intake 脚本，解析 dws_commands.json，打印执行指令。"""
    intake_script = INTAKE_SCRIPTS[args.intake_type]
    # --- 1. 运行 intake 脚本（emit 模式）---
    cmd = [
        sys.executable,
        str(intake_script),
        "--config", args.config,
        "--out-dir", args.out_dir,
    ]
    if args.files:
        cmd += ["--files"] + list(args.files)
    # --wall-budget only applies to resume intake
    if args.wall_budget and args.intake_type == "resume":
        cmd += ["--wall-budget", str(args.wall_budget)]

    print("[emit] 运行 intake_%s.py ..." % args.intake_type, file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[emit] intake_%s.py 失败 (exit %d)" % (args.intake_type, result.returncode),
              file=sys.stderr)
        if result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
        return 1
    # 打印 intake 脚本的 stderr 供调试
    if result.stderr:
        for line in result.stderr.strip().splitlines()[-10:]:
            print("  [intake] " + line, file=sys.stderr)

    # --- 2. 读取 dws_commands.json ---
    commands_path = Path(args.out_dir) / "dws_commands.json"
    if not commands_path.exists():
        print("[emit] dws_commands.json 不存在: %s" % commands_path, file=sys.stderr)
        return 1

    with open(commands_path, encoding="utf-8") as f:
        cmd_data = json.load(f)

    commands = cmd_data.get("commands", [])
    if not commands:
        print("[emit] dws_commands.json 中没有命令", file=sys.stderr)
        # 打印空结果
        print(json.dumps({
            "phase": "emit_done",
            "out_dir": args.out_dir,
            "total_commands": 0,
            "steps": [
                {"step": 1, "action": "run_replay", "cmd": _replay_cmd(args),
                 "description": "无 dws 命令，直接 replay"}
            ]
        }, ensure_ascii=False, indent=2))
        return 0

    # --- 3. 分类命令 ---
    auto_seqs: List[int] = []  # 查询/字段类 — agent 直接跑
    upload_items: List[Dict[str, Any]] = []  # 附件上传 — 需要额外 PUT OSS
    records_commands: List[Dict[str, Any]] = []  # upsert/create/update — 需要替换假 token
    other_items: List[Dict[str, Any]] = []  # 其他类型（stats, field create 等）

    # Map command type to template/corrected file names
    RECORDS_FILE_NAMES = {
        CMD_RECORD_UPSERT: ("upsert_records_template.json", "upsert_records_corrected.json"),
        CMD_RECORD_CREATE: ("create_records_template.json", "create_records_corrected.json"),
        CMD_RECORD_UPDATE: ("update_records_template.json", "update_records_corrected.json"),
    }

    input_files = [Path(f).resolve() for f in (args.files or [])]
    input_dir_map: Dict[str, Path] = {}
    for p in input_files:
        input_dir_map[p.name] = p

    for cmd_entry in commands:
        seq = cmd_entry["seq"]
        argv = cmd_entry["argv"]
        argv_hash = cmd_entry["argv_hash"]
        cmd_type = classify_command(argv)

        if cmd_type in (CMD_RECORD_QUERY, CMD_FIELD_GET, CMD_RECORD_STATS):
            auto_seqs.append(seq)
        elif cmd_type == CMD_ATTACHMENT_UPLOAD:
            file_name = extract_flag_value(argv, "--file-name")
            # 尝试在输入文件列表中找到对应的本地文件
            file_path = None
            if file_name and file_name in input_dir_map:
                file_path = str(input_dir_map[file_name])
            elif file_name:
                # 在输入文件的父目录中查找
                for p in input_files:
                    if p.name == file_name:
                        file_path = str(p)
                        break
                if not file_path:
                    # 在 out_dir 或其父目录中查找
                    candidate = Path(args.out_dir) / file_name
                    if candidate.exists():
                        file_path = str(candidate)
            upload_items.append({
                "seq": seq,
                "type": cmd_type,
                "argv": argv,
                "argv_hash": argv_hash,
                "file_name": file_name,
                "file_path": file_path,
                "needs_oss_put": True,
            })
        elif cmd_type in (CMD_RECORD_UPSERT, CMD_RECORD_CREATE, CMD_RECORD_UPDATE):
            # 提取 --records（内联 JSON）
            records_json_str = extract_flag_value(argv, "--records")
            records_data = None
            records_file = None
            template_name = RECORDS_FILE_NAMES[cmd_type][0]
            if records_json_str:
                try:
                    records_data = json.loads(records_json_str)
                except json.JSONDecodeError:
                    print("[emit] 警告: %s --records 不是有效 JSON, seq=%d" % (cmd_type, seq),
                          file=sys.stderr)
                # 保存为模板文件
                if records_data is not None:
                    records_file = str(Path(args.out_dir) / template_name)
                    with open(records_file, "w", encoding="utf-8") as f:
                        json.dump(records_data, f, ensure_ascii=False, indent=2)
            records_commands.append({
                "seq": seq,
                "type": cmd_type,
                "argv": argv,
                "argv_hash": argv_hash,
                "records_file": records_file,
                "needs_real_tokens": True,
                "description": "%s 记录中的 fileToken 是 emit 模式的假 token，"
                               "需要用 build_replay.py 替换为真实 token 后再执行" % cmd_type,
            })
        else:
            other_items.append({
                "seq": seq,
                "type": cmd_type,
                "argv": argv,
                "argv_hash": argv_hash,
            })
            auto_seqs.append(seq)  # 其他类型也归类为自动执行

    # --- 4. 构建步骤 ---
    steps: List[Dict[str, Any]] = []

    # Step 1: 自动执行查询/字段命令
    if auto_seqs:
        steps.append({
            "step": 1,
            "action": "run_dws",
            "seqs": sorted(auto_seqs),
            "description": "查询和字段获取 — agent 逐条执行 dws 命令，"
                           "每条结果写入 <out-dir>/dws_out_<seq>.json",
        })

    # Step 2: 附件上传
    if upload_items:
        steps.append({
            "step": 2,
            "action": "upload_attachment",
            "items": [
                {
                    "seq": item["seq"],
                    "dws_cmd": _argv_to_cmd(item["argv"]),
                    "file_name": item["file_name"],
                    "file_path": item["file_path"],
                    "argv_hash": item["argv_hash"],
                    "instructions": (
                        "1) 执行 dws_cmd 拿 uploadUrl + fileToken\n"
                        "2) PUT 本地文件到 uploadUrl (curl -X PUT -H 'Content-Type: <mime>' "
                        "--data-binary @<file_path> <uploadUrl>)\n"
                        "3) 将 dws 完整输出写入 <out-dir>/dws_out_%d.json" % item["seq"]
                    ) if item["file_path"] else
                    "1) 执行 dws_cmd 拿 uploadUrl + fileToken\n"
                    "2) PUT 本地文件到 uploadUrl\n"
                    "3) 将 dws 完整输出写入 <out-dir>/dws_out_%d.json" % item["seq"],
                }
                for item in upload_items
            ],
            "description": "附件上传（dws 拿 URL+token → PUT 文件 → 记录 token）",
        })

    # Step 3: 构建 replay 数据
    build_cmd = (
        "%s %s --commands %s --results-dir %s --out %s"
        % (sys.executable,
           str(PLUGIN_ROOT / "scripts" / "build_replay.py"),
           str(Path(args.out_dir) / "dws_commands.json"),
           args.out_dir,
           str(Path(args.out_dir) / "dws_results.json"))
    )
    steps.append({
        "step": len(steps) + 1,
        "action": "build_replay",
        "cmd": build_cmd,
        "description": (
            "构建 replay 数据：读取所有 dws_out_*.json，"
            "替换记录中的假 token 为真实 token，"
            "组装 dws_results.json。"
            + (" 如果记录命令结果已存在则直接输出 replay 就绪。"
               if records_commands else "")
        ),
    })

    # Step 4: 执行记录命令（upsert/create/update，如果有）
    for rc_info in records_commands:
        rc_type = rc_info["type"]
        corrected_name = RECORDS_FILE_NAMES[rc_type][1]
        build_cmd_rc = (
            "%s %s --commands %s --results-dir %s --out %s"
            % (sys.executable,
               str(PLUGIN_ROOT / "scripts" / "build_replay.py"),
               str(Path(args.out_dir) / "dws_commands.json"),
               args.out_dir,
               str(Path(args.out_dir) / "dws_results.json"))
        )
        steps.append({
            "step": len(steps) + 1,
            "action": "run_%s" % rc_type.replace(" ", "_"),
            "dws_cmd": _records_cmd_with_file(rc_info, args.out_dir),
            "records_file": str(Path(args.out_dir) / corrected_name),
            "build_cmd": build_cmd_rc,
            "description": (
                "1) 先执行 build_cmd 生成 %s\n"
                "2) 执行 dws_cmd（用 --records-file 而非 --records）\n"
                "3) 将 dws 输出写入 <out-dir>/dws_out_%d.json\n"
                "4) 再次执行 build_cmd 将 %s 结果纳入 dws_results.json"
                % (corrected_name, rc_info["seq"], rc_type)
            ),
        })

    # Step 5: replay
    steps.append({
        "step": len(steps) + 1,
        "action": "run_replay",
        "cmd": _replay_cmd(args),
        "description": "重放模式运行 intake 脚本，读取真实结果",
    })

    # --- 5. 打印结构化 JSON ---
    output: Dict[str, Any] = {
        "phase": "emit_done",
        "out_dir": str(Path(args.out_dir).resolve()),
        "total_commands": len(commands),
        "auto_command_seqs": sorted(auto_seqs),
        "upload_seqs": [item["seq"] for item in upload_items],
        "records_command_seqs": [rc["seq"] for rc in records_commands],
        "steps": steps,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Phase: replay
# ---------------------------------------------------------------------------

def phase_replay(args: argparse.Namespace) -> int:
    """Phase replay：运行 intake 脚本 --replay-path，打印摘要。"""
    replay_path = args.replay_path
    if not replay_path:
        print("[replay] 缺少 --replay-path 参数", file=sys.stderr)
        return 1

    # 检查 dws_results.json 是否存在
    if not Path(replay_path).exists():
        print("[replay] dws_results.json 不存在: %s" % replay_path, file=sys.stderr)
        print("[replay] 请先完成 emit 和 exec 阶段", file=sys.stderr)
        return 1

    # 运行 intake 脚本（replay 模式）
    intake_script = INTAKE_SCRIPTS[args.intake_type]
    cmd = [
        sys.executable,
        str(intake_script),
        "--config", args.config,
        "--out-dir", args.out_dir,
        "--replay-path", replay_path,
    ]
    if args.files:
        cmd += ["--files"] + list(args.files)

    print("[replay] 运行 intake_%s.py --replay-path %s ..." % (args.intake_type, replay_path),
          file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[replay] intake_%s.py 失败 (exit %d)" % (args.intake_type, result.returncode),
              file=sys.stderr)
        if result.stderr:
            print(result.stderr[-2000:], file=sys.stderr)
        return 1

    if result.stderr:
        for line in result.stderr.strip().splitlines()[-10:]:
            print("  [intake] " + line, file=sys.stderr)

    # 读取 intake_report.json
    report_path = Path(args.out_dir) / "intake_report.json"
    if not report_path.exists():
        print("[replay] intake_report.json 不存在: %s" % report_path, file=sys.stderr)
        return 1

    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    # 打印摘要
    summary: Dict[str, Any] = {
        "phase": "replay_done",
        "ok": report.get("ok", False),
        "partial": report.get("partial", False),
        "elapsed_ms": report.get("elapsed_ms"),
        "dws_calls": report.get("dws_calls"),
        "rows": report.get("rows"),
        "summary": report.get("summary"),
        "warnings": report.get("warnings", []),
        "report_file": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _argv_to_cmd(argv: List[str]) -> str:
    """将 argv 列表转为可执行的命令字符串（shell-safe 引号）。"""
    import shlex
    return " ".join(shlex.quote(str(a)) for a in argv)


def _records_cmd_with_file(rc_info: Dict[str, Any], out_dir: str) -> str:
    """构建使用 --records-file 的记录命令（upsert/create/update）。"""
    import shlex
    argv = list(rc_info["argv"])
    cmd_type = rc_info["type"]
    # Determine corrected file name based on command type
    NAMES = {
        CMD_RECORD_UPSERT: "upsert_records_corrected.json",
        CMD_RECORD_CREATE: "create_records_corrected.json",
        CMD_RECORD_UPDATE: "update_records_corrected.json",
    }
    corrected_name = NAMES.get(cmd_type, "%s_records_corrected.json" % cmd_type.replace(" ", "_"))
    records_file = str(Path(out_dir) / corrected_name)
    # 找到 --records 的位置，替换为 --records-file
    for i, a in enumerate(argv):
        if a == "--records" and i + 1 < len(argv):
            argv[i] = "--records-file"
            argv[i + 1] = records_file
            break
    return " ".join(shlex.quote(str(a)) for a in argv)


def _replay_cmd(args: argparse.Namespace) -> str:
    """构建 replay 阶段的命令字符串。"""
    import shlex
    parts = [
        sys.executable,
        str(SCRIPT_DIR / "run_pipeline.py"),
        "--phase", "replay",
        "--intake-type", args.intake_type,
        "--config", shlex.quote(args.config),
        "--out-dir", shlex.quote(args.out_dir),
        "--replay-path", shlex.quote(str(Path(args.out_dir) / "dws_results.json")),
    ]
    if args.files:
        parts += ["--files"] + [shlex.quote(f) for f in args.files]
    return " ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="入库编排脚本（emit → exec → replay 三步走，支持简历/岗位）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Step 1: emit 模式（简历）
  python3 scripts/run_pipeline.py --phase emit --intake-type resume \\
      --config config.json --files resume1.pdf resume2.docx \\
      --out-dir /tmp/intake_output

  # Step 1: emit 模式（岗位）
  python3 scripts/run_pipeline.py --phase emit --intake-type job \\
      --config config.json --files jd1.docx \\
      --out-dir /tmp/intake_output

  # Step 2: agent 执行 stdout 打印的 dws 命令
  #         每条结果写入 <out-dir>/dws_out_<seq>.json

  # Step 3: replay 模式
  python3 scripts/run_pipeline.py --phase replay --intake-type resume \\
      --config config.json --out-dir /tmp/intake_output \\
      --replay-path /tmp/intake_output/dws_results.json
""")
    parser.add_argument("--phase", required=True,
                        choices=["emit", "replay"],
                        help="执行阶段: emit（收集命令）或 replay（重放结果）")
    parser.add_argument("--intake-type", default="resume",
                        choices=["resume", "job"],
                        help="入库类型: resume（简历，默认）或 job（岗位）")
    parser.add_argument("--config", required=True,
                        help="config.json 绝对路径")
    parser.add_argument("--files", nargs="*", default=[],
                        help="输入文件路径（简历或岗位说明书，可多个）")
    parser.add_argument("--out-dir", required=True,
                        help="输出目录绝对路径")
    parser.add_argument("--replay-path", default=None,
                        help="dws_results.json 路径（仅 replay 阶段需要）")
    parser.add_argument("--wall-budget", type=float, default=None,
                        help="Wall-clock 预算秒数（仅 emit 阶段，仅 resume）")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # 确保 out_dir 存在
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    started_at_ms = int(time.time() * 1000)
    if args.phase == "emit":
        start_run(args.out_dir, args.intake_type, "run_pipeline", started_at_ms)

    previous_parent = os.environ.get("RECRUIT_TIMING_PARENT")
    os.environ["RECRUIT_TIMING_PARENT"] = "1"
    rc = 2
    try:
        if args.phase == "emit":
            rc = phase_emit(args)
        elif args.phase == "replay":
            rc = phase_replay(args)
        else:
            print("未知 phase: %s" % args.phase, file=sys.stderr)
            rc = 2
        return rc
    finally:
        if previous_parent is None:
            os.environ.pop("RECRUIT_TIMING_PARENT", None)
        else:
            os.environ["RECRUIT_TIMING_PARENT"] = previous_parent
        finished_at_ms = int(time.time() * 1000)
        append_event(
            args.out_dir,
            "run_pipeline.%s" % args.phase,
            "orchestrator_local",
            started_at_ms,
            finished_at_ms,
            metadata={"returncode": rc, "intake_type": args.intake_type},
        )
        timing_path = Path(args.out_dir).resolve() / TIMING_FILE_NAME
        print("PERFORMANCE:%s" % timing_path, file=sys.stderr)
        print("[performance] %s" % json.dumps(read_summary(args.out_dir),
                                                ensure_ascii=False),
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
