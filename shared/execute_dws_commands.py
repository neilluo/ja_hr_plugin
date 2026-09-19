#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
execute_dws_commands.py — 读取 dws_commands.json，逐条输出 dws 命令供 agent 执行。

用法（agent 侧）:
  python3 execute_dws_commands.py --commands <dws_commands.json> --output <dws_results.json>
  → 脚本读取命令清单，逐条打印 dws 命令到 stdout（一行一条），agent 用 Bash 工具逐条执行

输出格式：dws_results.json 包含每条命令的 argv_hash + 执行结果（stdout JSON + returncode）

注意：本脚本只负责打印命令清单供 agent 逐条用 Bash 工具执行。
dws 是宿主代理 shim，Python 子进程调用只返回占位符 "pending host-side execution"，
永远拿不到真实结果。唯一可用的工作流是 emit（收集命令）→ agent 逐条用 Bash 工具
执行 → replay（回放结果）。agent 执行后将结果汇总为 dws_results.json 供 replay 使用。
"""
import argparse
import json
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="读取 dws_commands.json，逐条打印 dws 命令供 agent 执行")
    ap.add_argument("--commands", required=True, help="dws_commands.json 路径")
    ap.add_argument("--output", required=True, help="dws_results.json 输出路径")
    ap.add_argument("--print-only", action="store_true",
                    help="只打印命令不执行（默认且唯一模式）")
    args = ap.parse_args()

    with open(args.commands, encoding="utf-8") as f:
        data = json.load(f)

    commands = data.get("commands", [])
    if not commands:
        print("没有 dws 命令需要执行", file=sys.stderr)
        # 写空结果文件
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"results": []}, f, ensure_ascii=False, indent=2)
        return 0

    print("共 %d 条 dws 命令待执行" % len(commands), file=sys.stderr)

    # 唯一可用模式：打印命令清单供 agent 逐条用 Bash 工具执行
    for cmd in commands:
        argv = cmd["argv"]
        # 第一项是 dws 二进制路径，统一替换为 "dws" 便于 agent 执行
        if argv and "dws" in argv[0]:
            argv[0] = "dws"
        print(" ".join(argv))

    # 写一个空壳结果文件，agent 执行完命令后自行汇总为 dws_results.json
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"results": [], "_note": "agent 执行完命令后请自行汇总结果到此文件"}, f,
                  ensure_ascii=False, indent=2)

    print("命令清单已打印到 stdout，请 agent 逐条用 Bash 工具执行后汇总结果到 %s" % args.output,
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
