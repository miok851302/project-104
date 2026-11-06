# Launcher for a local app. Keep this file next to server.py.
# Finds a working Python 3.10+ (never the Microsoft Store stub) and starts the app.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# The Store ships a python.exe that only opens the Store page. It "exists" and
# exits cleanly, so it passes a naive version check and then fails at run time.
function Test-Python($candidate) {
    if (-not $candidate) { return $false }
    if (-not (Test-Path $candidate)) { return $false }
    if ($candidate -like "*WindowsApps*") { return $false }
    try {
        $probe = & $candidate -c "import sys, json; print(json.dumps([sys.version_info[0], sys.version_info[1]]))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $parts = $probe | ConvertFrom-Json
        return ($parts[0] -gt 3) -or ($parts[0] -eq 3 -and $parts[1] -ge 10)
    } catch {
        return $false
    }
}

$python = $null

# 1. Explicit override
if ($env:SAWIT_PYTHON -and (Test-Python $env:SAWIT_PYTHON)) {
    $python = $env:SAWIT_PYTHON
}

# 2. The runtime this project was developed against
if (-not $python) {
    $bundled = Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Python $bundled) { $python = $bundled }
}

# 3. A normal system install
if (-not $python) {
    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-Python $command.Source)) {
            $python = $command.Source
            break
        }
    }
}

# 4. Common install locations
if (-not $python) {
    foreach ($guess in @(
        "C:\Python313\python.exe", "C:\Python312\python.exe", "C:\Python311\python.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )) {
        if (Test-Python $guess) { $python = $guess; break }
    }
}

if (-not $python) {
    Write-Host ""
    Write-Host "  Python 3.10 or newer was not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Install it from https://python.org/downloads and tick"
    Write-Host "  'Add python.exe to PATH' during setup, then run this again."
    Write-Host ""
    Write-Host "  If you already have Python somewhere unusual, set"
    Write-Host "  SAWIT_PYTHON to the full path of python.exe."
    Write-Host ""
    Write-Host "  Note: the Microsoft Store shortcut for Python does not count."
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

$server = Join-Path $root "server.py"
if (-not (Test-Path $server)) {
    Write-Host ""
    Write-Host "  server.py is missing from $root" -ForegroundColor Red
    Write-Host "  Keep this launcher in the same folder as server.py."
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "  Starting Sawit..." -ForegroundColor Green
Write-Host "  Python: $python"
Write-Host "  Close this window to stop the app."
Write-Host ""

try {
    & $python $server --open @args
} catch {
    Write-Host ""
    Write-Host "  The app stopped with an error:" -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)"
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "  Sawit has stopped." -ForegroundColor Yellow
Read-Host "  Press Enter to close"

