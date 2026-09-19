#!/usr/bin/env python3
"""
构建 replay 数据：从逐条 dws 执行结果组装 dws_results.json

用法：
  python3 scripts/build_replay.py \\
      --commands <out-dir>/dws_commands.json \\
      --results-dir <out-dir> \\
      --out <out-dir>/dws_results.json

工作流程：
  1. 读取 dws_commands.json，获取所有 emit 模式收集的命令
  2. 对每条命令，查找 <results-dir>/dws_out_<seq>.json（agent 写入的 dws 输出）
  3. 对附件上传命令：解析 dws 输出，提取真实 fileToken
  4. 构建 token_map: {fake_token -> real_token}
     - fake_token 格式: emit_fake_token_<seq>
     - 通过 seq 号匹配附件上传命令和记录命令中的假 token
  5. 读取 records_template.json（upsert/create/update），用 token_map 替换所有假 token
  6. 写入 records_corrected.json（upsert_records_corrected.json /
     create_records_corrected.json / update_records_corrected.json）
  7. 检查记录命令结果是否已存在（dws_out_<seq>.json）
     - 如果不存在：打印记录命令供 agent 执行，不包含该结果
     - 如果存在：包含所有结果，打印 "ready for replay"

dws_out_<seq>.json 格式（agent 写入）：
  方式 A — 原始 dws stdout（纯 JSON 字符串）:
    {"status":"success","data":{...},...}
  方式 B — 包装格式（含 returncode/stdout/stderr）:
    {"returncode":0, "stdout":"...json...", "stderr":"", "elapsed_ms":123}
  方式 C — 纯 data 部分:
    {"fileToken":"ft_xxx","uploadUrl":"https://...",...}

dws_results.json 格式（本脚本输出，供 replay 使用）:
  {
    "results": [
      {"argv_hash":"<md5>", "returncode":0, "stdout":"<json string>", "stderr":"", "elapsed_ms":123},
      ...
    ]
  }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

FAKE_TOKEN_PATTERN = re.compile(r"emit_fake_token_(\d+)")
FAKE_TOKEN_PREFIX = "emit_fake_token_"

# ---------------------------------------------------------------------------
# 命令分类（与 run_pipeline.py 保持一致）
# ---------------------------------------------------------------------------

def classify_command(argv: List[str]) -> str:
    """根据 argv 判定 dws 命令类型。"""
    if len(argv) < 4:
        return "unknown"
    if argv[2] == "record" and argv[3] == "query":
        return "record query"
    elif argv[2] == "record" and argv[3] == "stats":
        return "record stats"
    elif argv[2] == "field" and argv[3] == "get":
        return "field get"
    elif argv[2] == "field" and argv[3] == "create":
        return "field create"
    elif argv[2] == "attachment" and argv[3] == "upload":
        return "attachment upload"
    elif argv[2] == "record" and argv[3] == "upsert":
        return "record upsert"
    elif argv[2] == "record" and argv[3] == "create":
        return "record create"
    elif argv[2] == "record" and argv[3] == "update":
        return "record update"
    return "unknown"


def extract_flag_value(argv: List[str], flag: str) -> Optional[str]:
    """从 argv 中提取 --flag value 对的值。"""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


# ---------------------------------------------------------------------------
# 读取 dws 输出文件
# ---------------------------------------------------------------------------

def read_dws_output(file_path: Path) -> Optional[Dict[str, Any]]:
    """读取 agent 写入的 dws 输出文件，支持多种格式。

    返回标准化后的 dict:
    - 如果是包装格式（含 returncode/stdout）：直接返回
    - 如果是纯 JSON（dws stdout）：包装为 {returncode:0, stdout: <json>, stderr:"", elapsed_ms:0}
    - 如果是纯 data：包装 stdout 为 {status:success, data: <data>}
    """
    if not file_path.exists():
        return None

    try:
        with open(file_path, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError as e:
        print("[build_replay] 读取失败 %s: %s" % (file_path, e), file=sys.stderr)
        return None

    if not raw:
        return None

    # 尝试解析为 JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 可能是纯文本 stdout，包装一下
        return {
            "returncode": 0,
            "stdout": raw,
            "stderr": "",
            "elapsed_ms": 0,
        }

    # 方式 B: 包装格式
    if isinstance(data, dict) and ("returncode" in data or "stdout" in data):
        # 确保有必要字段存在
        return {
            "returncode": data.get("returncode", 0),
            "stdout": data.get("stdout", raw if isinstance(raw, str) else json.dumps(data)),
            "stderr": data.get("stderr", ""),
            "elapsed_ms": data.get("elapsed_ms", 0),
        }

    # 方式 A: dws 完整输出 {status:success, data:{...}}
    if isinstance(data, dict) and "data" in data and "status" in data:
        return {
            "returncode": 0,
            "stdout": raw,
            "stderr": "",
            "elapsed_ms": 0,
        }

    # 方式 C: 纯 data（如 {fileToken:"ft_xxx", uploadUrl:"..."}）
    # 包装为完整 dws 输出
    wrapped = {"status": "success", "success": True, "data": data,
               "error": {}, "summary": "agent executed"}
    return {
        "returncode": 0,
        "stdout": json.dumps(wrapped, ensure_ascii=False),
        "stderr": "",
        "elapsed_ms": 0,
    }


def parse_attachment_token(stdout_str: str) -> Optional[Tuple[str, str]]:
    """从 dws attachment upload 的 stdout 中提取 fileToken 和 uploadUrl。

    返回 (fileToken, uploadUrl) 或 None。
    """
    try:
        data = json.loads(stdout_str)
    except (json.JSONDecodeError, TypeError):
        return None

    # 处理包装格式 {data: {fileToken: ...}}
    inner = data
    if isinstance(data, dict) and "data" in data:
        inner = data["data"]

    if not isinstance(inner, dict):
        return None

    token = inner.get("fileToken") or inner.get("file_token")
    upload_url = inner.get("uploadUrl") or inner.get("upload_url")

    if token:
        return str(token), str(upload_url or "")
    return None


# ---------------------------------------------------------------------------
# Token 替换
# ---------------------------------------------------------------------------

def build_token_map(commands: List[Dict[str, Any]],
                    results_dir: Path) -> Dict[str, str]:
    """构建 {fake_token -> real_token} 映射。

    附件上传命令的 fake_token 格式为 emit_fake_token_<seq>。
    通过读取 dws_out_<seq>.json 获取真实 token。
    """
    token_map: Dict[str, str] = {}

    for cmd in commands:
        seq = cmd["seq"]
        argv = cmd["argv"]
        cmd_type = classify_command(argv)

        if cmd_type != "attachment upload":
            continue

        # fake token 就是 emit_fake_token_<seq>
        fake_token = "%s%d" % (FAKE_TOKEN_PREFIX, seq)

        # 读取 dws 输出
        result_file = results_dir / ("dws_out_%d.json" % seq)
        result = read_dws_output(result_file)
        if result is None:
            print("[build_replay] 警告: 附件上传结果缺失: %s" % result_file.name,
                  file=sys.stderr)
            continue

        parsed = parse_attachment_token(result["stdout"])
        if parsed is None:
            print("[build_replay] 警告: 无法从 dws_out_%d.json 提取 fileToken" % seq,
                  file=sys.stderr)
            continue

        real_token, _ = parsed
        token_map[fake_token] = real_token
        print("[build_replay] token 映射: %s -> %s" % (fake_token, real_token),
              file=sys.stderr)

    return token_map


def replace_tokens_in_records(records: Any, token_map: Dict[str, str]) -> Any:
    """递归替换 records 中的所有假 token。

    支持替换：
    - 字符串值 "emit_fake_token_N" -> 真实 token
    - dict 中 {"fileToken": "emit_fake_token_N"} -> {"fileToken": "<real>"}
    """
    if isinstance(records, str):
        # 整个字符串就是假 token
        if records in token_map:
            return token_map[records]
        # 字符串中包含假 token（不太可能，但以防万一）
        for fake, real in token_map.items():
            if fake in records:
                records = records.replace(fake, real)
        return records

    if isinstance(records, list):
        return [replace_tokens_in_records(item, token_map) for item in records]

    if isinstance(records, dict):
        result = {}
        for key, val in records.items():
            result[key] = replace_tokens_in_records(val, token_map)
        return result

    return records


def deep_find_fake_tokens(obj: Any) -> List[str]:
    """递归查找对象中所有假 token 字符串。"""
    found: List[str] = []
    if isinstance(obj, str):
        for m in FAKE_TOKEN_PATTERN.finditer(obj):
            found.append(obj)  # the whole string is the fake token
    elif isinstance(obj, list):
        for item in obj:
            found.extend(deep_find_fake_tokens(item))
    elif isinstance(obj, dict):
        for val in obj.values():
            found.extend(deep_find_fake_tokens(val))
    return found


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="从逐条 dws 执行结果组装 dws_results.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--commands", required=True,
                        help="dws_commands.json 路径")
    parser.add_argument("--results-dir", required=True,
                        help="dws_out_*.json 所在目录")
    parser.add_argument("--out", required=True,
                        help="输出 dws_results.json 路径")
    args = parser.parse_args()

    commands_path = Path(args.commands)
    results_dir = Path(args.results_dir)
    out_path = Path(args.out)

    # --- 1. 读取 dws_commands.json ---
    if not commands_path.exists():
        print("[build_replay] dws_commands.json 不存在: %s" % commands_path,
              file=sys.stderr)
        return 1

    with open(commands_path, encoding="utf-8") as f:
        cmd_data = json.load(f)

    commands = cmd_data.get("commands", [])
    if not commands:
        print("[build_replay] 没有命令", file=sys.stderr)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"results": []}, f, ensure_ascii=False, indent=2)
        return 0

    # --- 2. 构建 token map（附件上传 → 真实 token）---
    token_map = build_token_map(commands, results_dir)
    if token_map:
        print("[build_replay] token_map (%d 个):" % len(token_map), file=sys.stderr)
        for k, v in token_map.items():
            print("  %s -> %s" % (k, v), file=sys.stderr)

    # --- 3. 处理记录命令（upsert/create/update，替换假 token） ---
    # Command types that carry --records with fake tokens
    RECORDS_CMD_TYPES = ["record upsert", "record create", "record update"]
    # Map type to template/corrected file names
    RECORDS_FILE_NAMES = {
        "record upsert": ("upsert_records_template.json", "upsert_records_corrected.json"),
        "record create": ("create_records_template.json", "create_records_corrected.json"),
        "record update": ("update_records_template.json", "update_records_corrected.json"),
    }

    # Collect all records commands
    records_cmds: List[Dict[str, Any]] = []
    for cmd in commands:
        cmd_type = classify_command(cmd["argv"])
        if cmd_type in RECORDS_CMD_TYPES:
            records_cmds.append(cmd)

    # Process each records command: replace fake tokens
    # Track corrected file paths and result readiness
    rc_info_list: List[Dict[str, Any]] = []
    for rc_cmd in records_cmds:
        rc_type = classify_command(rc_cmd["argv"])
        rc_seq = rc_cmd["seq"]
        template_name, corrected_name = RECORDS_FILE_NAMES[rc_type]
        template_file = results_dir / template_name
        corrected_file = results_dir / corrected_name

        if token_map:
            # 尝试从模板文件读取，否则从 argv 提取
            records_data = None
            if template_file.exists():
                try:
                    with open(template_file, encoding="utf-8") as f:
                        records_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            if records_data is None:
                # 从 argv 的 --records 参数提取
                records_json_str = extract_flag_value(rc_cmd["argv"], "--records")
                if records_json_str:
                    try:
                        records_data = json.loads(records_json_str)
                    except json.JSONDecodeError:
                        print("[build_replay] 警告: 无法解析 %s --records JSON"
                              % rc_type, file=sys.stderr)

            if records_data is not None:
                # 替换假 token
                corrected = replace_tokens_in_records(records_data, token_map)
                with open(corrected_file, "w", encoding="utf-8") as f:
                    json.dump(corrected, f, ensure_ascii=False, indent=2)
                print("[build_replay] 已写入修正后的 %s 记录: %s"
                      % (rc_type, corrected_file), file=sys.stderr)

                # 检查是否还有未替换的假 token
                remaining = deep_find_fake_tokens(corrected)
                if remaining:
                    print("[build_replay] 警告: %s 仍有 %d 个假 token 未被替换"
                          % (rc_type, len(remaining)), file=sys.stderr)
                    for t in remaining[:5]:
                        print("  未替换: %s" % t, file=sys.stderr)

        rc_info_list.append({
            "cmd": rc_cmd,
            "type": rc_type,
            "seq": rc_seq,
            "corrected_file": corrected_file,
            "result_ready": False,  # will be updated in step 4
        })

    # --- 4. 组装 dws_results.json ---
    results: List[Dict[str, Any]] = []
    missing_seqs: List[int] = []
    # Track which records commands have results
    for rc_info in rc_info_list:
        rc_info["result_ready"] = False

    for cmd in commands:
        seq = cmd["seq"]
        argv_hash = cmd["argv_hash"]
        cmd_type = classify_command(cmd["argv"])

        # 跳过记录命令（upsert/create/update）— 检查是否已执行
        if cmd_type in RECORDS_CMD_TYPES:
            rc_result_file = results_dir / ("dws_out_%d.json" % seq)
            result = read_dws_output(rc_result_file)
            if result is not None:
                # 记录命令已执行
                for rc_info in rc_info_list:
                    if rc_info["seq"] == seq:
                        rc_info["result_ready"] = True
                        break
                results.append({
                    "argv_hash": argv_hash,
                    "returncode": result["returncode"],
                    "stdout": result["stdout"],
                    "stderr": result["stderr"],
                    "elapsed_ms": result["elapsed_ms"],
                })
            else:
                # 记录命令未执行，跳过
                continue
            continue

        # 其他命令
        result_file = results_dir / ("dws_out_%d.json" % seq)
        result = read_dws_output(result_file)
        if result is None:
            missing_seqs.append(seq)
            continue

        results.append({
            "argv_hash": argv_hash,
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "elapsed_ms": result["elapsed_ms"],
        })

    # 写入 dws_results.json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)

    print("[build_replay] 已写入 %s（%d 条结果）" % (out_path, len(results)),
          file=sys.stderr)

    if missing_seqs:
        print("[build_replay] 警告: 以下 seq 的结果缺失: %s" %
              ", ".join(str(s) for s in missing_seqs), file=sys.stderr)

    # --- 5. 判断状态 ---
    pending_cmds = [rc for rc in rc_info_list if not rc["result_ready"]]
    ready_cmds = [rc for rc in rc_info_list if rc["result_ready"]]

    if pending_cmds:
        # 有记录命令未执行，打印待执行的命令
        import shlex
        pending_output: List[Dict[str, Any]] = []
        for rc_info in pending_cmds:
            argv = list(rc_info["cmd"]["argv"])
            records_file = str(rc_info["corrected_file"])
            for i, a in enumerate(argv):
                if a == "--records" and i + 1 < len(argv):
                    argv[i] = "--records-file"
                    argv[i + 1] = records_file
                    break
            cmd_str = " ".join(shlex.quote(str(a)) for a in argv)
            pending_output.append({
                "type": rc_info["type"],
                "seq": rc_info["seq"],
                "command": cmd_str,
                "records_file": records_file,
            })

        print(json.dumps({
            "status": "records_pending",
            "message": "附件结果已处理，token 已替换。请执行以下记录命令：",
            "pending_commands": pending_output,
            "instructions": (
                "1) 执行上面的每条 command\n"
                "2) 将 dws 输出写入 %s/dws_out_<seq>.json\n"
                "3) 重新运行本脚本以纳入记录命令结果" % results_dir
            ),
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "status": "ready_for_replay",
            "message": "所有结果已就绪，可以执行 replay。",
            "results_count": len(results),
            "results_file": str(out_path),
        }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
