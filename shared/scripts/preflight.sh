#!/usr/bin/env bash
# preflight.sh — macOS/Linux preflight checks for recruit-match-suite-fast
# Zero runtime dependencies (pure bash, no Python required by this script itself)

# ----------------------------------------------------------------------------
# Variable computation
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------
CONFIG_PATH=""
FILES_LIST=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_PATH="$2"
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

# Resolve config path
if [[ -z "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$PLUGIN_DIR/config.json"
fi

# ----------------------------------------------------------------------------
# Result accumulators
# ----------------------------------------------------------------------------
PYTHON_OK=false
DWS_OK=false
CONFIG_OK=false
FILES_OK=true
FILES_MISSING=()

BLOCKER=""
NEXT_ACTION=""

get_next_action() {
  case "$1" in
    python) echo "macOS: brew install python@3.12 | 下载: https://www.python.org/downloads/" ;;
    dws)    echo "请在千问办公「设置 → 连接器」中开启并授权钉钉" ;;
    config) echo "招聘系统尚未部署，请先运行「复刻部署」技能" ;;
    files)  echo "文件路径有误，请检查" ;;
    *)      echo "" ;;
  esac
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
  echo "✅ Python: $PYTHON_FOUND (版本 >=3.9)"
else
  echo "❌ Python: 未找到 >=3.9 版本"
fi

# If Python fails, stop — everything downstream depends on it
if [[ "$PYTHON_OK" != "true" ]]; then
  BLOCKER="python"
  NEXT_ACTION=$(get_next_action "$BLOCKER")
  echo "⏭️  dws 登录态: 跳过 (Python 未通过)"
  echo "⏭️  config.json: 跳过 (Python 未通过)"
  echo "⏭️  文件存在性: 跳过 (Python 未通过)"
  echo "PREFLIGHT:{\"ok\":false,\"blocker\":\"$BLOCKER\",\"next_action\":\"$NEXT_ACTION\"}"
  exit 1
fi

# ----------------------------------------------------------------------------
# Temp files for parallel dws check
# ----------------------------------------------------------------------------
DWS_TMP="/tmp/preflight_dws_$$.out"
DWS_PID=""

# ----------------------------------------------------------------------------
# Check 2: dws login state (background, 15s timeout)
# ----------------------------------------------------------------------------
(
  dws aitable base list --limit 1 > "$DWS_TMP" 2>&1
) &
DWS_PID=$!

# ----------------------------------------------------------------------------
# Check 3: config.json exists and has base_id + tables (while dws runs)
# ----------------------------------------------------------------------------
if [[ -f "$CONFIG_PATH" ]]; then
  if grep -q '"base_id"' "$CONFIG_PATH" 2>/dev/null && grep -q '"tables"' "$CONFIG_PATH" 2>/dev/null; then
    CONFIG_OK=true
    echo "✅ config.json: 存在且含 base_id + tables ($CONFIG_PATH)"
  else
    echo "❌ config.json: 缺少 base_id 或 tables 键 ($CONFIG_PATH)"
  fi
else
  echo "❌ config.json: 文件不存在 ($CONFIG_PATH)"
fi

# ----------------------------------------------------------------------------
# Check 4: file existence (while dws runs)
# ----------------------------------------------------------------------------
if [[ ${#FILES_LIST[@]} -gt 0 ]]; then
  for f in "${FILES_LIST[@]}"; do
    if [[ ! -f "$f" ]]; then
      FILES_OK=false
      FILES_MISSING+=("$f")
    fi
  done
  if [[ "$FILES_OK" == "true" ]]; then
    echo "✅ 文件存在性: 全部 ${#FILES_LIST[@]} 个文件均存在"
  else
    echo "❌ 文件存在性: 缺少 ${#FILES_MISSING[@]} 个文件: ${FILES_MISSING[*]}"
  fi
else
  echo "⏭️  文件存在性: 跳过 (未指定 --files)"
fi

# ----------------------------------------------------------------------------
# Wait for dws background check (15s timeout)
# ----------------------------------------------------------------------------
DWS_TIMEOUT=15
DWS_ELAPSED=0
DWS_DONE=false

while kill -0 "$DWS_PID" 2>/dev/null; do
  if [[ $DWS_ELAPSED -ge $DWS_TIMEOUT ]]; then
    kill "$DWS_PID" 2>/dev/null
    wait "$DWS_PID" 2>/dev/null
    break
  fi
  sleep 1
  ((DWS_ELAPSED++))
done

if [[ -f "$DWS_TMP" ]]; then
  DWS_OUTPUT=$(cat "$DWS_TMP" 2>/dev/null)
  if echo "$DWS_OUTPUT" | grep -q '"success": true' 2>/dev/null; then
    DWS_OK=true
    echo "✅ dws 登录态: 已登录"
  elif echo "$DWS_OUTPUT" | grep -q 'pending.*host-side' 2>/dev/null; then
    # dws shim returned placeholder — hook didn't inject in subprocess context
    # dws is installed but can't verify login from script; treat as pass (agent verifies)
    DWS_OK=true
    echo "⚠️ dws 登录态: dws 已安装，脚本环境无法验证登录态（将在对话中确认）"
  else
    echo "❌ dws 登录态: 未登录或超时"
  fi
  rm -f "$DWS_TMP"
else
  echo "❌ dws 登录态: 超时或无输出"
fi

# ----------------------------------------------------------------------------
# Determine blocker (priority: python > dws > config > files)
# ----------------------------------------------------------------------------
if [[ "$PYTHON_OK" != "true" ]]; then
  BLOCKER="python"
elif [[ "$DWS_OK" != "true" ]]; then
  BLOCKER="dws"
elif [[ "$CONFIG_OK" != "true" ]]; then
  BLOCKER="config"
elif [[ "$FILES_OK" != "true" ]]; then
  BLOCKER="files"
fi

if [[ -n "$BLOCKER" ]]; then
  NEXT_ACTION=$(get_next_action "$BLOCKER")
  echo "PREFLIGHT:{\"ok\":false,\"blocker\":\"$BLOCKER\",\"next_action\":\"$NEXT_ACTION\"}"
  exit 1
else
  echo "PREFLIGHT:{\"ok\":true}"
  exit 0
fi
