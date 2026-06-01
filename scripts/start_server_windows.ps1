param(
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ServerDir = Join-Path $ProjectDir "server"
Set-Location $ServerDir

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created server\.env - edit API_KEY if needed."
}

Get-Content ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#") -or -not $line.Contains("=")) {
        return
    }
    $key, $value = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($key.Trim(), $value.Trim(), "Process")
}

if (-not $env:STORAGE_DIR -or $env:STORAGE_DIR -eq "/data") {
    $env:STORAGE_DIR = Join-Path $ServerDir "data"
}

$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py -3"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} else {
    throw "Python is not installed. Install it with: winget install -e --id Python.Python.3.12"
}

if (-not (Test-Path ".venv")) {
    Invoke-Expression "$python -m venv .venv"
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host "Starting server on http://localhost:$Port"
Write-Host "Storage dir: $env:STORAGE_DIR"
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port $Port
