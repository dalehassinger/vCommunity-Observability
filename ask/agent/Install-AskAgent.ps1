<#
.SYNOPSIS
    Installs (or removes) the Ask host agent on macOS as a LaunchAgent.

.DESCRIPTION
    Copies ask-host-agent.py to ~/Library/Application Support/ask-host-agent, creates a random
    token in ~/.config/ask-host-agent/token (only readable by you) if there is none, writes
    ~/Library/LaunchAgents/com.vcommunity.ask-host-agent.plist and starts it. The agent then
    starts at every login and is restarted if it stops. Logs: ~/Library/Logs/ask-host-agent.log

    Put the printed token in the repository's .env as ASK_AGENT_TOKEN=... and restart Ask:
        docker compose up -d vco-ask

.PARAMETER Port
    Port the agent listens on (default 9101).

.PARAMETER McpConfig
    An .mcp.json file (for example the one next to your PowerShell MCP server). It is copied to
    ~/.config/ask-host-agent/mcp.json, where the agent reads the MCP servers it offers to Ask
    (MCP tools page). The server scripts stay where they are, so you keep editing them in place.

.PARAMETER Uninstall
    Stops the agent and removes the LaunchAgent and the copied script (the token is kept).

.EXAMPLE
    pwsh ./ask/agent/Install-AskAgent.ps1
.EXAMPLE
    pwsh ./ask/agent/Install-AskAgent.ps1 -McpConfig ~/Documents/GitHub/PS-TAM-Lab/MCP/.mcp.json
.EXAMPLE
    pwsh ./ask/agent/Install-AskAgent.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [int]$Port = 9101,
    [string]$McpConfig,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
if (-not $IsMacOS) { throw 'This installer is for macOS. On Linux run ask-host-agent.py with systemd or cron @reboot.' }

$label     = 'com.vcommunity.ask-host-agent'
$appDir    = Join-Path $HOME 'Library/Application Support/ask-host-agent'
$plistPath = Join-Path $HOME "Library/LaunchAgents/$label.plist"
$tokenDir  = Join-Path $HOME '.config/ask-host-agent'
$tokenFile = Join-Path $tokenDir 'token'
$logFile   = Join-Path $HOME 'Library/Logs/ask-host-agent.log'
$uid       = (& id -u).Trim()

# Stop a running copy first (ignore "not loaded").
& launchctl bootout "gui/$uid/$label" 2>$null | Out-Null

if ($Uninstall) {
    Remove-Item -LiteralPath $plistPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $appDir 'ask-host-agent.py') -ErrorAction SilentlyContinue
    Write-Host "Removed the Ask host agent. The token is still in $tokenFile."
    return
}

$python = (Get-Command python3 -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python3 not found. Install the Xcode Command Line Tools (xcode-select --install) or Python 3.9+.' }
& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
if ($LASTEXITCODE -ne 0) { throw "$python is older than Python 3.9." }

New-Item -ItemType Directory -Force -Path $appDir, $tokenDir, (Split-Path $plistPath), (Split-Path $logFile) | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ask-host-agent.py') -Destination $appDir -Force

if (-not (Test-Path $tokenFile)) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [System.IO.File]::WriteAllText($tokenFile, ([Convert]::ToHexString($bytes).ToLower()))
}
if ($McpConfig) {
    $src = (Resolve-Path -LiteralPath $McpConfig).Path
    $null = Get-Content -Raw -LiteralPath $src | ConvertFrom-Json   # stops here if it is not JSON
    Copy-Item -LiteralPath $src -Destination (Join-Path $tokenDir 'mcp.json') -Force
    Write-Host "MCP servers: copied $src to $(Join-Path $tokenDir 'mcp.json')."
}
& chmod 700 $tokenDir
& chmod 600 $tokenFile
$token = (Get-Content -Raw $tokenFile).Trim()

$script = Join-Path $appDir 'ask-host-agent.py'
$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
$plist = @"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(& $esc $python)</string>
    <string>$(& $esc $script)</string>
    <string>--port</string><string>$Port</string>
    <string>--token-file</string><string>$(& $esc $tokenFile)</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(& $esc $logFile)</string>
  <key>StandardErrorPath</key><string>$(& $esc $logFile)</string>
</dict>
</plist>
"@
Set-Content -LiteralPath $plistPath -Value $plist -Encoding utf8
& launchctl bootstrap "gui/$uid" $plistPath
Start-Sleep -Seconds 3

try {
    $m = Invoke-RestMethod -Uri "http://localhost:$Port/metrics" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 10
    Write-Host "Ask host agent is running on port $Port ($($m.host.chip), $($m.host.os))." -ForegroundColor Green
} catch {
    Write-Warning "The agent did not answer yet. Check $logFile and: launchctl print gui/$uid/$label"
}
Write-Host ''
Write-Host 'Add this line to the .env file of vCommunity-Observability, then run: docker compose up -d vco-ask'
Write-Host "ASK_AGENT_TOKEN=$token" -ForegroundColor Cyan
if (Test-Path (Join-Path $tokenDir 'mcp.json')) {
    Write-Host ''
    Write-Host 'MCP servers (what Ask will offer on the MCP tools page):'
    & $python $script --mcp-config (Join-Path $tokenDir 'mcp.json') --mcp-list
    Write-Host "If a server script lives under ~/Documents or ~/Desktop and the MCP tools page says 'Operation not permitted',"
    $realPy = (& $python -c 'import sys; print(sys.executable)').Trim()
    Write-Host "give $realPy Full Disk Access (System Settings > Privacy & Security), then: launchctl kickstart -k gui/$uid/$label"
}
if (-not (Get-Command macmon -ErrorAction SilentlyContinue)) {
    Write-Host ''
    Write-Host 'Optional: for temperature and power, install macmon (brew install macmon). The agent finds it on its own.'
}
