# Scheduled task wrapper for distribute_secrets.py
# Runs every 30 min to keep JIRA_TOKEN fresh across all tracked repos.
# Uses GH_TOKEN from file instead of keyring (keyring not available in non-interactive sessions).

$ErrorActionPreference = 'Stop'
$REPO = "C:\Users\Administrator\Desktop\Github Repos\vw-jira-sync"
$LOG  = "C:\Users\Administrator\Desktop\Github Repos\vw-jira-sync\logs\distribute-secrets.log"
$GH_TOKEN_FILE   = "C:\Users\Administrator\.config\gh\token.txt"
$JIRA_TOKEN_FILE = "C:\Users\Administrator\Desktop\jira-token.txt"

# Rotate log (keep last 500 lines)
if (Test-Path $LOG) {
    $lines = Get-Content $LOG
    if ($lines.Count -gt 500) { $lines | Select-Object -Last 500 | Set-Content $LOG }
}

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $LOG "[$ts] $msg"
}

# Validate prerequisites
if (-not (Test-Path $GH_TOKEN_FILE))   { Write-Log "ERROR: GH_TOKEN_FILE not found: $GH_TOKEN_FILE"; exit 1 }
if (-not (Test-Path $JIRA_TOKEN_FILE)) { Write-Log "ERROR: JIRA_TOKEN_FILE not found: $JIRA_TOKEN_FILE"; exit 1 }

# Inject GH_TOKEN so gh CLI bypasses keyring
$env:GH_TOKEN        = (Get-Content $GH_TOKEN_FILE -Raw).Trim()
$env:JIRA_TOKEN_FILE = $JIRA_TOKEN_FILE

New-Item -ItemType Directory -Path (Split-Path $LOG) -Force | Out-Null
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
    # Clear token from env
    $env:GH_TOKEN = $null
}
