Param(
    [switch]$NoRestart
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# UTF-8
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Write-Host "=== Telegram Bot (Polymarket Whale Agent) — PowerShell ==="

# Check Python
try {
    $py = (Get-Command python).Source
} catch {
    Write-Host "[ERROR] Python not found. Install Python 3.10+ from python.org" -ForegroundColor Red
    Read-Host "Press Enter"
    exit 1
}

# Virtual environment
$venv = Join-Path $ScriptDir ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "First run: creating virtual environment..." -ForegroundColor Yellow
    & $py -m venv $venv
}

$activate = Join-Path $venv "Scripts\Activate.ps1"
. $activate

# Dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# .env
$envFile = Join-Path $ScriptDir ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[ERROR] No .env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID." -ForegroundColor Red
    Write-Host "        Copy .env.example to .env and fill in your keys."
    Read-Host "Press Enter"
    exit 1
}

if ($NoRestart) {
    Write-Host "Starting without auto-restart..."
    python telegram_bot.py
    exit $LASTEXITCODE
}

$restartDelay = 3
while ($true) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "`n--- Starting bot ($ts) ---"

    $p = Start-Process -FilePath "python" -ArgumentList "telegram_bot.py" -NoNewWindow -PassThru -Wait

    switch ($p.ExitCode) {
        3 {
            Write-Host "`n[STOP] Another bot instance is already running. Close it and start again." -ForegroundColor Red
            Read-Host "Press Enter"
            exit
        }
        default {
            Write-Host "`n[!] Bot stopped (exit code $($p.ExitCode)) at $(Get-Date -Format 'HH:mm:ss')." -ForegroundColor Yellow
            Write-Host "    Restarting in ${restartDelay}s... (close this window to quit)"
            Start-Sleep -Seconds $restartDelay
        }
    }
}
