#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight checks embedded in business scripts. Zero third-party deps.

每个入口脚本 main() 的 stage 0 调用 run_preflight()，保证环境检查自动执行，
不依赖 agent 记得跑。OpenAPI 直连架构：不再检查 dws，改为检查钉钉应用凭证。

Checks (priority order, first failure blocks):
  1. Python version >= 3.9
  2. credentials: .secrets.json (app_key/app_secret 或 appKey/appSecret)
     或环境变量 DINGTALK_APP_KEY + DINGTALK_APP_SECRET
  3. config.json exists and has base_id + tables
  4. files_dir exists and contains at least one supported file
  5. File(s) exist and are non-empty

On failure: prints a human-readable summary to stderr, a machine-readable
``PREFLIGHT:{...}`` JSON line to stdout, then exits with code 1.
On success: prints a brief summary to stderr and returns normally.
"""

import json
import os
import sys
import time as _time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CREDS_MSG = ("凭证缺失：请创建 .secrets.json (gitignored) "
              "或设置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET 环境变量")

# 整点峰值规避：钉钉 QPS 配额按整点重置，整点前后是全网请求高峰。
# [整点-10s, 整点+10s) 窗口内等待到「整点+10s」（最长 20s）。
_PEAK_WINDOW = 10.0

# 时钟与 sleep 可注入（模块级引用，测试 monkeypatch 后不真等）
_localtime = _time.localtime
_sleep = _time.sleep


def _peak_wait(now=None):
    """返回距「整点+10s」的等待秒数；窗口外返回 0。

    窗口 = [整点-10s, 整点+10s)，即 minute==59 且 second>=50（跨小时边界），
    或 minute==0 且 second<10。
    """
    now = _localtime() if now is None else now
    if now.tm_min == 59 and now.tm_sec >= 60 - _PEAK_WINDOW:
        return (60 - now.tm_sec) + _PEAK_WINDOW
    if now.tm_min == 0 and now.tm_sec < _PEAK_WINDOW:
        return _PEAK_WINDOW - now.tm_sec
    return 0.0


def _avoid_peak(checks):
    """整点峰值规避：窗口内 sleep 到整点+10s，peak_wait 写入 checks 供报告输出。"""
    wait = _peak_wait()
    if wait > 0:
        _sleep(wait)
    checks["peak_wait"] = {"pass": True, "info": "%.1fs" % wait, "error": None,
                           "seconds": round(wait, 1)}
    return wait


# --------------------------------------------------------------------------- #
# Individual checks — each returns (ok: bool, info: str|None, error: str|None)
# --------------------------------------------------------------------------- #
def _check_python():
    """Check Python version is >= 3.9."""
    v = sys.version_info[:2]
    ok = v >= (3, 9)
    return ok, "%d.%d.%d" % (v[0], v[1], sys.version_info[2]), None


def _check_credentials(secrets_path=None):
    """Check DingTalk app credentials: env vars first, then .secrets.json."""
    if os.environ.get("DINGTALK_APP_KEY") and os.environ.get("DINGTALK_APP_SECRET"):
        return True, "环境变量 DINGTALK_APP_KEY/SECRET", None
    p = secrets_path or os.path.join(ROOT, ".secrets.json")
    if not os.path.exists(p):
        return False, None, _CREDS_MSG
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        return False, None, "%s（.secrets.json 解析失败: %s）" % (_CREDS_MSG, e)
    except OSError as e:  # pragma: no cover
        return False, None, "%s（.secrets.json 读取失败: %s）" % (_CREDS_MSG, str(e)[:200])
    if not isinstance(d, dict):
        return False, None, _CREDS_MSG
    key = d.get("app_key") or d.get("appKey")
    sec = d.get("app_secret") or d.get("appSecret")
    if key and sec:
        return True, ".secrets.json", None
    return False, None, _CREDS_MSG


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
    "credentials": _CREDS_MSG,
    "config": "招聘系统尚未部署，请先运行「复刻部署」技能",
    "files": "文件路径有误，请检查",
    "files_dir": "files-dir 目录不存在或无支持格式文件，请检查路径与目录内容",
}

_ORDER = ("python", "credentials", "config", "files_dir", "files")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_preflight(config_path=None, files=None, files_dir=None, secrets_path=None):
    """Run preflight checks.

    If checks fail, prints a human-readable summary to stderr and a
    ``PREFLIGHT:{...}`` JSON line to stdout, then exits with code 1.
    If all pass, prints a brief summary to stderr and returns.

    Args:
        config_path: path to config.json (None = skip config check).
        files: list of file paths to check (None = skip file check).
        files_dir: directory path to scan for resume files (None = skip).
        secrets_path: .secrets.json path override (None = <repo>/.secrets.json).

    Returns:
        (True, checks_dict) if all checks pass.
        Does not return if a check fails (calls sys.exit(1)).
    """
    checks = {}

    # 1. Python — if it fails, stop immediately
    ok, info, err = _check_python()
    checks["python"] = {"pass": ok, "info": info, "error": err}
    if not ok:
        _report_and_exit(checks, "python")

    # 2. credentials
    ok, info, err = _check_credentials(secrets_path)
    checks["credentials"] = {"pass": ok, "info": info, "error": err}

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

    # Determine blocker (priority: python > credentials > config > files_dir > files)
    blocker = None
    for key in _ORDER:
        if key in checks and not checks[key]["pass"]:
            blocker = key
            break

    if blocker:
        _report_and_exit(checks, blocker)

    # 6. 整点峰值规避：全部检查通过、即将进入触网阶段时才等待（阻断路径无请求，不等）
    _avoid_peak(checks)

    # All pass — brief summary to stderr
    _print_summary(checks)
    print("  全部通过，可继续执行", file=sys.stderr)
    print("-" * 28, file=sys.stderr)

    return True, checks


def _print_summary(checks):
    print("-- 环境检查 " + "-" * 20, file=sys.stderr)
    for key in _ORDER:
        if key in checks:
            c = checks[key]
            if c["pass"]:
                print("  [OK] %s: %s" % (key, c.get("info", "")), file=sys.stderr)
            else:
                print("  [FAIL] %s: %s" % (key, c.get("error", "")), file=sys.stderr)
    if "peak_wait" in checks:
        print("  [OK] peak_wait: %.1fs" % checks["peak_wait"]["seconds"], file=sys.stderr)


def _report_and_exit(checks, blocker):
    """Print results and exit with code 1."""
    next_action = _NEXT_ACTIONS.get(blocker, "")

    _print_summary(checks)
    print("-" * 28, file=sys.stderr)
    print("阻断: %s" % blocker, file=sys.stderr)
    print("处理: %s" % next_action, file=sys.stderr)

    # Machine-readable line for agent
    result = {"ok": False, "blocker": blocker, "next_action": next_action,
              "peak_wait": checks.get("peak_wait", {}).get("seconds", 0)}
    print("PREFLIGHT:" + json.dumps(result, ensure_ascii=False))
    sys.exit(1)
