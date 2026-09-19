# preflight.ps1 — Windows PowerShell preflight checks for recruit-match-suite-fast
# Zero runtime dependencies (pure PowerShell, no Python required by this script itself)

param(
  [string]$Config = "",
  [string[]]$Files = @()
)

# ----------------------------------------------------------------------------
# Variable computation
# ----------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginDir = Resolve-Path (Join-Path $ScriptDir "..\..")

# Resolve config path
if ([string]::IsNullOrEmpty($Config)) {
  $ConfigPath = Join-Path $PluginDir "config.json"
} else {
  $ConfigPath = $Config
}

# ----------------------------------------------------------------------------
# Result accumulators
# ----------------------------------------------------------------------------
$PythonOk = $false
$DwsOk = $false
$ConfigOk = $false
$FilesOk = $true
$FilesMissing = @()

$NextActions = @{
  "python" = "macOS: brew install python@3.12 | 下载: https://www.python.org/downloads/"
  "dws"    = "请在千问办公「设置 → 连接器」中开启并授权钉钉"
  "config" = "招聘系统尚未部署，请先运行「复刻部署」技能"
  "files"  = "文件路径有误，请检查"
}

# ----------------------------------------------------------------------------
# Check 1: Python version >= 3.9
# ----------------------------------------------------------------------------
$PythonCmds = @("py -3", "python3", "python")
$PythonFound = ""

foreach ($cmd in $PythonCmds) {
  try {
    $expr = "$cmd -c ""import sys;assert sys.version_info[:2]>=(3,9)"""
    Invoke-Expression $expr 2>`$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
      $PythonFound = $cmd
      break
    }
  } catch {
    # continue to next candidate
  }
}

if ($PythonFound) {
  $PythonOk = $true
  Write-Host "✅ Python: $PythonFound (版本 >=3.9)"
} else {
  Write-Host "❌ Python: 未找到 >=3.9 版本"
}

# If Python fails, stop — everything downstream depends on it
if (-not $PythonOk) {
  $Blocker = "python"
  $NextAction = $NextActions[$Blocker]
  Write-Host "⏭️  dws 登录态: 跳过 (Python 未通过)"
  Write-Host "⏭️  config.json: 跳过 (Python 未通过)"
  Write-Host "⏭️  文件存在性: 跳过 (Python 未通过)"
  Write-Host "PREFLIGHT:{`"ok`":false,`"blocker`":`"$Blocker`",`"next_action`":`"$NextAction`"}"
  exit 1
}

# ----------------------------------------------------------------------------
# Check 2: dws login state (Start-Job, 15s timeout)
# ----------------------------------------------------------------------------
$DwsJob = Start-Job -ScriptBlock {
  dws aitable base list --limit 1 2>&1
}

# ----------------------------------------------------------------------------
# Check 3: config.json exists and has base_id + tables (while dws runs)
# ----------------------------------------------------------------------------
if (Test-Path $ConfigPath) {
  $configContent = Get-Content $ConfigPath -Raw -ErrorAction SilentlyContinue
  if ($configContent -match '"base_id"' -and $configContent -match '"tables"') {
    $ConfigOk = $true
    Write-Host "✅ config.json: 存在且含 base_id + tables ($ConfigPath)"
  } else {
    Write-Host "❌ config.json: 缺少 base_id 或 tables 键 ($ConfigPath)"
  }
} else {
  Write-Host "❌ config.json: 文件不存在 ($ConfigPath)"
}

# ----------------------------------------------------------------------------
# Check 4: file existence (while dws runs)
# ----------------------------------------------------------------------------
if ($Files.Count -gt 0) {
  foreach ($f in $Files) {
    if (-not (Test-Path $f)) {
      $FilesOk = $false
      $FilesMissing += $f
    }
  }
  if ($FilesOk) {
    Write-Host "✅ 文件存在性: 全部 $($Files.Count) 个文件均存在"
  } else {
    Write-Host "❌ 文件存在性: 缺少 $($FilesMissing.Count) 个文件: $($FilesMissing -join ' ')"
  }
} else {
  Write-Host "⏭️  文件存在性: 跳过 (未指定 -Files)"
}

# ----------------------------------------------------------------------------
# Wait for dws job (15s timeout)
# ----------------------------------------------------------------------------
$DwsTimeoutSeconds = 15
$DwsResult = ""

try {
  $DwsResult = Receive-Job -Job $DwsJob -Wait -Timeout $DwsTimeoutSeconds -ErrorAction SilentlyContinue
} catch {
  # timeout or error
}

# Clean up job
if ($DwsJob.State -ne 'Completed') {
  Stop-Job $DwsJob -ErrorAction SilentlyContinue
}
Remove-Job $DwsJob -Force -ErrorAction SilentlyContinue

if ($DwsResult -match '"success": true') {
  $DwsOk = $true
  Write-Host "✅ dws 登录态: 已登录"
} else {
  Write-Host "❌ dws 登录态: 未登录或超时"
}

# ----------------------------------------------------------------------------
# Determine blocker (priority: python > dws > config > files)
# ----------------------------------------------------------------------------
$Blocker = ""
if (-not $PythonOk) {
  $Blocker = "python"
} elseif (-not $DwsOk) {
  $Blocker = "dws"
} elseif (-not $ConfigOk) {
  $Blocker = "config"
} elseif (-not $FilesOk) {
  $Blocker = "files"
}

if ($Blocker) {
  $NextAction = $NextActions[$Blocker]
  Write-Host "PREFLIGHT:{`"ok`":false,`"blocker`":`"$Blocker`",`"next_action`":`"$NextAction`"}"
  exit 1
} else {
  Write-Host "PREFLIGHT:{`"ok`":true}"
  exit 0
}
