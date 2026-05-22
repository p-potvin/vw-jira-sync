# Scheduled task wrapper for distribute_secrets.py
# Runs every 30 min to keep JIRA_TOKEN fresh across all tracked repos.
# Fetches GH_TOKEN and JIRA_TOKEN from the GreenCloud VPS via Tailscale SSH.
# No tokens are stored locally.

$ErrorActionPreference = 'Stop'
$REPO = "C:\Users\Administrator\Desktop\Github Repos\vw-jira-sync"
$LOG  = "C:\Users\Administrator\Desktop\Github Repos\vw-jira-sync\logs\distribute-secrets.log"
$VPS  = "root@100.73.93.84"   # greencloud-vps via Tailscale

# Rotate log (keep last 500 lines)
if (Test-Path $LOG) {
    $lines = Get-Content $LOG
    if ($lines.Count -gt 500) { $lines | Select-Object -Last 500 | Set-Content $LOG }
}

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $LOG "[$ts] $msg"
}

New-Item -ItemType Directory -Path (Split-Path $LOG) -Force | Out-Null

# Fetch tokens from VPS — single SSH call per token, nothing stored on disk
Write-Log "Fetching tokens from VPS ($VPS)"
try {
    $env:GH_TOKEN = (ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no `
        $VPS "cat /etc/vw-webhookd/gh-token | tr -d '[:space:]'" 2>$null).Trim()
    if (-not $env:GH_TOKEN) { throw "GH_TOKEN came back empty" }

    $env:JIRA_TOKEN = (ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no `
        $VPS "/usr/local/bin/vw-print-jira-token" 2>$null).Trim()
    if (-not $env:JIRA_TOKEN) { throw "JIRA_TOKEN came back empty" }
} catch {
    Write-Log "ERROR fetching tokens from VPS: $_"
    exit 1
}

Write-Log "Starting distribute_secrets.py"

Push-Location $REPO
try {
    $out = & ".venv\Scripts\python.exe" "scripts\distribute_secrets.py" 2>&1
    $out | ForEach-Object { Write-Log $_ }
    Write-Log "Finished (exit $LASTEXITCODE)"
} catch {
    Write-Log "EXCEPTION: $_"
} finally {
    Pop-Location
    # Clear tokens from env
    $env:GH_TOKEN    = $null
    $env:JIRA_TOKEN  = $null
}
