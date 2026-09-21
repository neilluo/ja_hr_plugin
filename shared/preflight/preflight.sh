#!/usr/bin/env bash
# preflight.sh — macOS/Linux preflight checks for recruit-match-suite-fast
# Zero runtime dependencies (pure bash, no Python required by this script itself).
# macOS bash 3.x compatible: no associative arrays (declare -A), uses case instead.
#
# OpenAPI-direct architecture: NO dws check. Credentials are read from
# .secrets.json (app_key/app_secret or appKey/appSecret) or from the
# DINGTALK_APP_KEY / DINGTALK_APP_SECRET environment variables.
#
# Manual verification (no automated smoke test in CI):
#   bash shared/preflight/preflight.sh                       # repo config + credentials
#   bash shared/preflight/preflight.sh --config /tmp/x.json  # missing config -> blocker=config
#   bash shared/preflight/preflight.sh --files /nope.pdf     # missing file   -> blocker=files
#   DINGTALK_APP_KEY= DINGTALK_APP_SECRET= ... (no .secrets.json) -> blocker=credentials

# ----------------------------------------------------------------------------
# Variable computation
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
CONFIG_PATH=""
SECRETS_PATH=""
FILES_LIST=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --secrets)
      SECRETS_PATH="$2"
      shift 2
      ;;
    --files)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        FILES_LIST+=("$1")
        shift
      done
      ;;
    *)
      shift
      ;;
  esac
done

# Resolve default paths
if [[ -z "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$PLUGIN_DIR/config.json"
fi
if [[ -z "$SECRETS_PATH" ]]; then
  SECRETS_PATH="$PLUGIN_DIR/.secrets.json"
fi

# ----------------------------------------------------------------------------
# Result accumulators
# ----------------------------------------------------------------------------
PYTHON_OK=false
CREDS_OK=false
CONFIG_OK=false
FILES_OK=true
FILES_MISSING=()

BLOCKER=""
NEXT_ACTION=""

get_next_action() {
  case "$1" in
    python)      echo "macOS: brew install python@3.12 | 下载: https://www.python.org/downloads/" ;;
    credentials) echo "凭证缺失：请创建 .secrets.json (gitignored) 或设置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET 环境变量" ;;
    config)      echo "招聘系统尚未部署，请先运行「复刻部署」技能" ;;
    files)       echo "文件路径有误，请检查" ;;
    *)           echo "" ;;
  esac
}

emit_and_exit() {
  echo "PREFLIGHT:{\"ok\":false,\"blocker\":\"$BLOCKER\",\"next_action\":\"$NEXT_ACTION\"}"
  exit 1
}

# ----------------------------------------------------------------------------
# Check 1: Python version >= 3.9
# ----------------------------------------------------------------------------
PYTHON_CMDS=("python3" "python3.12" "python3.11" "python3.10" "python3.9")
PYTHON_FOUND=""

for cmd in "${PYTHON_CMDS[@]}"; do
  if command -v "$cmd" &>/dev/null; then
    if "$cmd" -c "import sys;assert sys.version_info[:2]>=(3,9)" &>/dev/null; then
      PYTHON_FOUND="$cmd"
      break
    fi
  fi
done

if [[ -n "$PYTHON_FOUND" ]]; then
  PYTHON_OK=true
  echo "[OK] python: $PYTHON_FOUND (版本 >=3.9)"
else
  echo "[FAIL] python: 未找到 >=3.9 版本"
fi

# If Python fails, stop — everything downstream depends on it
if [[ "$PYTHON_OK" != "true" ]]; then
  BLOCKER="python"
  NEXT_ACTION=$(get_next_action "$BLOCKER")
  echo "[SKIP] credentials: 跳过 (Python 未通过)"
  echo "[SKIP] config.json: 跳过 (Python 未通过)"
  echo "[SKIP] 文件存在性: 跳过 (Python 未通过)"
  emit_and_exit
fi

# ----------------------------------------------------------------------------
# Check 2: credentials (.secrets.json OR env vars)
# ----------------------------------------------------------------------------
if [[ -n "$DINGTALK_APP_KEY" && -n "$DINGTALK_APP_SECRET" ]]; then
  CREDS_OK=true
  echo "[OK] credentials: 环境变量 DINGTALK_APP_KEY/SECRET"
elif [[ -f "$SECRETS_PATH" ]]; then
  if grep -Eq '"app_key"|"appKey"' "$SECRETS_PATH" 2>/dev/null \
     && grep -Eq '"app_secret"|"appSecret"' "$SECRETS_PATH" 2>/dev/null; then
    CREDS_OK=true
    echo "[OK] credentials: .secrets.json ($SECRETS_PATH)"
  else
    echo "[FAIL] credentials: .secrets.json 缺少 app_key/app_secret"
  fi
else
  echo "[FAIL] credentials: 无 .secrets.json 且未设置环境变量"
fi

# ----------------------------------------------------------------------------
# Check 3: config.json exists and has base_id + tables
# ----------------------------------------------------------------------------
if [[ -f "$CONFIG_PATH" ]]; then
  if grep -q '"base_id"' "$CONFIG_PATH" 2>/dev/null && grep -q '"tables"' "$CONFIG_PATH" 2>/dev/null; then
    CONFIG_OK=true
    echo "[OK] config.json: 存在且含 base_id + tables ($CONFIG_PATH)"
  else
    echo "[FAIL] config.json: 缺少 base_id 或 tables 键 ($CONFIG_PATH)"
  fi
else
  echo "[FAIL] config.json: 文件不存在 ($CONFIG_PATH)"
fi

# ----------------------------------------------------------------------------
# Check 4: file existence
# ----------------------------------------------------------------------------
if [[ ${#FILES_LIST[@]} -gt 0 ]]; then
  for f in "${FILES_LIST[@]}"; do
    if [[ ! -f "$f" ]]; then
      FILES_OK=false
      FILES_MISSING+=("$f")
    fi
  done
  if [[ "$FILES_OK" == "true" ]]; then
    echo "[OK] files: 全部 ${#FILES_LIST[@]} 个文件均存在"
  else
    echo "[FAIL] files: 缺少 ${#FILES_MISSING[@]} 个文件: ${FILES_MISSING[*]}"
  fi
else
  echo "[SKIP] files: 跳过 (未指定 --files)"
fi

# ----------------------------------------------------------------------------
# Determine blocker (priority: python > credentials > config > files)
# ----------------------------------------------------------------------------
if [[ "$PYTHON_OK" != "true" ]]; then
  BLOCKER="python"
elif [[ "$CREDS_OK" != "true" ]]; then
  BLOCKER="credentials"
elif [[ "$CONFIG_OK" != "true" ]]; then
  BLOCKER="config"
elif [[ "$FILES_OK" != "true" ]]; then
  BLOCKER="files"
fi

if [[ -n "$BLOCKER" ]]; then
  NEXT_ACTION=$(get_next_action "$BLOCKER")
  emit_and_exit
else
  echo "PREFLIGHT:{\"ok\":true}"
  exit 0
fi
