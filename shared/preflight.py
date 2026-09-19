#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks embedded in business scripts. Zero third-party deps.

Ported from shared/scripts/preflight.sh into Python so that each CLI
entry point can call run_preflight() at the top of main(), guaranteeing
the four checks run automatically without relying on the agent remembering.

Checks (priority order, first failure blocks):
  1. Python version >= 3.9
  2. dws login state (skippable via skip_dws=True)
  3. config.json exists and has base_id + tables
  4. File(s) exist and are non-empty

On failure: prints a human-readable summary to stderr, a machine-readable
``PREFLIGHT:{...}`` JSON line to stdout, then exits with code 1.
On success: prints a brief summary to stderr and returns normally.
"""

import json
import os
import subprocess
import sys


# --------------------------------------------------------------------------- #
# Individual checks — each returns (ok: bool, info: str|None, error: str|None)
# --------------------------------------------------------------------------- #
def _check_python():
    """Check Python version is >= 3.9."""
    v = sys.version_info[:2]
    ok = v >= (3, 9)
    return ok, "%d.%d.%d" % (v[0], v[1], sys.version_info[2]), None


def _check_dws():
    """Check dws is available and logged in.

    When called from a Python subprocess, the dws shim may return a
    placeholder ('pending host-side execution'); this is treated as
    installed-but-unverifiable (pass with a warning), matching the
    bash preflight.sh behaviour.
    """
    try:
        r = subprocess.run(
            ["dws", "aitable", "base", "list", "--limit", "1"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout or ""
        if '"success": true' in out or '"success":true' in out:
            return True, "已登录", None
        elif "pending" in out and "host-side" in out:
            return True, "dws 已安装，脚本环境无法验证登录态", None
        else:
            return False, None, "dws 未登录或不可达"
    except FileNotFoundError:
        return False, None, "dws 命令未找到，钉钉连接器未安装"
    except subprocess.TimeoutExpired:
        return False, None, "dws 超时（>10s）"
    except Exception as e:  # pragma: no cover
        return False, None, str(e)[:200]


def _check_config(config_path):
    """Check config.json exists and has base_id + tables keys."""
    if not config_path:
        return True, "未指定 config，跳过", None
    if not os.path.exists(config_path):
        return False, None, "config.json 不存在: %s" % config_path
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("base_id") or not cfg.get("tables"):
            return False, None, "config.json 缺少 base_id 或 tables 键"
        return True, (cfg.get("base_id", "") or "")[:8] + "...", None
    except json.JSONDecodeError as e:
        return False, None, "config.json 解析失败: %s" % e
    except Exception as e:  # pragma: no cover
        return False, None, "config.json 读取失败: %s" % str(e)[:200]


def _check_files(files):
    """Check all files exist and are non-empty."""
    if not files:
        return True, "未指定文件，跳过", None
    missing = [os.path.basename(f) for f in files if not os.path.exists(f)]
    empty = [os.path.basename(f) for f in files
             if os.path.exists(f) and os.path.getsize(f) == 0]
    if not missing and not empty:
        return True, "%d 份文件全部存在" % len(files), None
    reason = ""
    if missing:
        reason += "文件不存在: %s; " % ", ".join(missing)
    if empty:
        reason += "文件为空: %s" % ", ".join(empty)
    return False, None, reason


# Supported resume file extensions for --files-dir scanning
_SUPPORTED_RESUME_EXTS = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"}


def _check_files_dir(files_dir):
    """Check that --files-dir exists and contains at least one supported file."""
    if not files_dir:
        return True, "未指定 files-dir，跳过", None
    if not os.path.isdir(files_dir):
        return False, None, "files-dir 目录不存在: %s" % files_dir
    found = [
        f for f in os.listdir(files_dir)
        if os.path.isfile(os.path.join(files_dir, f))
        and os.path.splitext(f)[1].lower() in _SUPPORTED_RESUME_EXTS
    ]
    if not found:
        return False, None, "files-dir 目录无支持格式文件: %s" % files_dir
    return True, "%d 份文件" % len(found), None


# --------------------------------------------------------------------------- #
# Next-action guidance for each blocker type
# --------------------------------------------------------------------------- #
_NEXT_ACTIONS = {
    "python": "本机未安装 Python 3.9+。macOS: brew install python@3.12; "
              "Windows: winget install Python.Python.3.12; "
              "下载: https://www.python.org/downloads/",
    "dws": "请在千问办公「设置 → 连接器」中开启并授权钉钉",
    "config": "招聘系统尚未部署，请先运行「复刻部署」技能",
    "files": "文件路径有误，请检查",
    "files_dir": "files-dir 目录不存在或无支持格式文件，请检查路径与目录内容",
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_preflight(config_path=None, files=None, files_dir=None, skip_dws=False):
    """Run preflight checks.

    If checks fail, prints a human-readable summary to stderr and a
    ``PREFLIGHT:{...}`` JSON line to stdout, then exits with code 1.
    If all pass, prints a brief summary to stderr and returns.

    Args:
        config_path: path to config.json (None = skip config check).
        files: list of file paths to check (None = skip file check).
        files_dir: directory path to scan for resume files (None = skip).
        skip_dws: if True, skip the dws check (for scripts that don't need dws).

    Returns:
        (True, checks_dict) if all checks pass.
        Does not return if a check fails (calls sys.exit(1)).
    """
    checks = {}

    # 1. Python
    ok, info, err = _check_python()
    checks["python"] = {"pass": ok, "info": info, "error": err}

    # If Python fails, stop immediately
    if not ok:
        _report_and_exit(checks, "python")

    # 2. dws
    if not skip_dws:
        ok, info, err = _check_dws()
        checks["dws"] = {"pass": ok, "info": info, "error": err}

    # 3. config
    ok, info, err = _check_config(config_path)
    checks["config"] = {"pass": ok, "info": info, "error": err}

    # 4. files_dir (if provided)
    if files_dir:
        ok, info, err = _check_files_dir(files_dir)
        checks["files_dir"] = {"pass": ok, "info": info, "error": err}

    # 5. files
    ok, info, err = _check_files(files)
    checks["files"] = {"pass": ok, "info": info, "error": err}

    # Determine blocker (priority: python > dws > config > files_dir > files)
    blocker = None
    for key in ("python", "dws", "config", "files_dir", "files"):
        if key in checks and not checks[key]["pass"]:
            blocker = key
            break

    if blocker:
        _report_and_exit(checks, blocker)

    # All pass — brief summary to stderr
    print("-- 环境检查 " + "-" * 20, file=sys.stderr)
    for key in ("python", "dws", "config", "files_dir", "files"):
        if key in checks:
            c = checks[key]
            if c["pass"]:
                print("  [OK] %s: %s" % (key, c.get("info", "")), file=sys.stderr)
            else:
                print("  [FAIL] %s: %s" % (key, c.get("error", "")), file=sys.stderr)
    print("  全部通过，可继续执行", file=sys.stderr)
    print("-" * 28, file=sys.stderr)

    return True, checks


def _report_and_exit(checks, blocker):
    """Print results and exit with code 1."""
    next_action = _NEXT_ACTIONS.get(blocker, "")

    print("-- 环境检查 " + "-" * 20, file=sys.stderr)
    for key in ("python", "dws", "config", "files_dir", "files"):
        if key in checks:
            c = checks[key]
            if c["pass"]:
                print("  [OK] %s: %s" % (key, c.get("info", "")), file=sys.stderr)
            else:
                print("  [FAIL] %s: %s" % (key, c.get("error", "")), file=sys.stderr)
    print("-" * 28, file=sys.stderr)
    print("阻断: %s" % blocker, file=sys.stderr)
    print("处理: %s" % next_action, file=sys.stderr)

    # Machine-readable line for agent
    result = {"ok": False, "blocker": blocker, "next_action": next_action}
    print("PREFLIGHT:" + json.dumps(result, ensure_ascii=False))
    sys.exit(1)
