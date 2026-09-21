# preflight.ps1 — Windows PowerShell preflight checks for recruit-match-suite-fast
# Zero runtime dependencies (pure PowerShell, no Python required by this script itself).
#
# OpenAPI-direct architecture: NO dws check. Credentials are read from
# .secrets.json (app_key/app_secret or appKey/appSecret) or from the
# DINGTALK_APP_KEY / DINGTALK_APP_SECRET environment variables.
#
# Manual verification (no automated smoke test in CI):
#   powershell -File shared\preflight.ps1                          # repo defaults
#   powershell -File shared\preflight.ps1 -Config C:\tmp\x.json    # missing config -> blocker=config
#   powershell -File shared\preflight.ps1 -Files nope.pdf          # missing file   -> blocker=files

param(
  [string]$Config = "",
  [string]$Secrets = "",
  [string[]]$Files = @()
)

# ----------------------------------------------------------------------------
# Variable computation
# ----------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginDir = Resolve-Path (Join-Path $ScriptDir "..")

if ([string]::IsNullOrEmpty($Config)) {
  $ConfigPath = Join-Path $PluginDir "config.json"
} else {
  $ConfigPath = $Config
}
if ([string]::IsNullOrEmpty($Secrets)) {
  $SecretsPath = Join-Path $PluginDir ".secrets.json"
} else {
  $SecretsPath = $Secrets
}

# ----------------------------------------------------------------------------
# Result accumulators
# ----------------------------------------------------------------------------
$PythonOk = $false
$CredsOk = $false
$ConfigOk = $false
$FilesOk = $true
$FilesMissing = @()

function Get-NextAction($key) {
  switch ($key) {
    "python"      { return "Windows: winget install Python.Python.3.12 | 下载: https://www.python.org/downloads/" }
    "credentials" { return "凭证缺失：请创建 .secrets.json (gitignored) 或设置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET 环境变量" }
    "config"      { return "招聘系统尚未部署，请先运行「复刻部署」技能" }
    "files"       { return "文件路径有误，请检查" }
    default       { return "" }
  }
}

function Emit-AndExit($blocker) {
  $na = Get-NextAction $blocker
  Write-Host "PREFLIGHT:{`"ok`":false,`"blocker`":`"$blocker`",`"next_action`":`"$na`"}"
  exit 1
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
  Write-Host "[OK] python: $PythonFound (版本 >=3.9)"
} else {
  Write-Host "[FAIL] python: 未找到 >=3.9 版本"
}

if (-not $PythonOk) {
  Write-Host "[SKIP] credentials: 跳过 (Python 未通过)"
  Write-Host "[SKIP] config.json: 跳过 (Python 未通过)"
  Write-Host "[SKIP] 文件存在性: 跳过 (Python 未通过)"
  Emit-AndExit "python"
}

# ----------------------------------------------------------------------------
# Check 2: credentials (.secrets.json OR env vars)
# ----------------------------------------------------------------------------
$EnvKey = $env:DINGTALK_APP_KEY
$EnvSec = $env:DINGTALK_APP_SECRET
if ($EnvKey -and $EnvSec) {
  $CredsOk = $true
  Write-Host "[OK] credentials: 环境变量 DINGTALK_APP_KEY/SECRET"
} elseif (Test-Path $SecretsPath) {
  $secretContent = Get-Content $SecretsPath -Raw -ErrorAction SilentlyContinue
  if (($secretContent -match '"app_key"' -or $secretContent -match '"appKey"') -and
      ($secretContent -match '"app_secret"' -or $secretContent -match '"appSecret"')) {
    $CredsOk = $true
    Write-Host "[OK] credentials: .secrets.json ($SecretsPath)"
  } else {
    Write-Host "[FAIL] credentials: .secrets.json 缺少 app_key/app_secret"
  }
} else {
  Write-Host "[FAIL] credentials: 无 .secrets.json 且未设置环境变量"
}

# ----------------------------------------------------------------------------
# Check 3: config.json exists and has base_id + tables
# ----------------------------------------------------------------------------
if (Test-Path $ConfigPath) {
  $configContent = Get-Content $ConfigPath -Raw -ErrorAction SilentlyContinue
  if ($configContent -match '"base_id"' -and $configContent -match '"tables"') {
    $ConfigOk = $true
    Write-Host "[OK] config.json: 存在且含 base_id + tables ($ConfigPath)"
  } else {
    Write-Host "[FAIL] config.json: 缺少 base_id 或 tables 键 ($ConfigPath)"
  }
} else {
  Write-Host "[FAIL] config.json: 文件不存在 ($ConfigPath)"
}

# ----------------------------------------------------------------------------
# Check 4: file existence
# ----------------------------------------------------------------------------
if ($Files.Count -gt 0) {
  foreach ($f in $Files) {
    if (-not (Test-Path $f)) {
      $FilesOk = $false
      $FilesMissing += $f
    }
  }
  if ($FilesOk) {
    Write-Host "[OK] files: 全部 $($Files.Count) 个文件均存在"
  } else {
    Write-Host "[FAIL] files: 缺少 $($FilesMissing.Count) 个文件: $($FilesMissing -join ' ')"
  }
} else {
  Write-Host "[SKIP] files: 跳过 (未指定 -Files)"
}

# ----------------------------------------------------------------------------
# Determine blocker (priority: python > credentials > config > files)
# ----------------------------------------------------------------------------
$Blocker = ""
if (-not $PythonOk) {
  $Blocker = "python"
} elseif (-not $CredsOk) {
  $Blocker = "credentials"
} elseif (-not $ConfigOk) {
  $Blocker = "config"
} elseif (-not $FilesOk) {
  $Blocker = "files"
}

if ($Blocker) {
  Emit-AndExit $Blocker
} else {
  Write-Host "PREFLIGHT:{`"ok`":true}"
  exit 0
}
