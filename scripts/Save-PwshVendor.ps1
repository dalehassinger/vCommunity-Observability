<#
.SYNOPSIS
    Saves PowerShell for Linux and PowerCLI into vendor/pwsh/, so the core-pwsh image
    build uses this local copy instead of downloading them (faster rebuilds, offline sites).

.EXAMPLE
    ./scripts/Save-PwshVendor.ps1                       # PWSH_VERSION from .env (or 7.6.9), newest PowerCLI, this Mac's CPU
    ./scripts/Save-PwshVendor.ps1 -PowerCLIVersion 13.4.0.24798382 -Arch both
#>
[CmdletBinding()]
param(
    # PowerShell version to save. Default: PWSH_VERSION in .env, else 7.6.9 (the Dockerfile default).
    [string]$PwshVersion,
    # PowerCLI version to save. Empty = newest. Set POWERCLI_VERSION to the same value when you pin it.
    [string]$PowerCLIVersion,
    # CPU of the image: arm64 (Apple Silicon), x64, both, or auto (this computer's CPU).
    [ValidateSet('auto', 'arm64', 'x64', 'both')]
    [string]$Arch = 'auto'
)
$ErrorActionPreference = 'Stop'
$repo   = Split-Path $PSScriptRoot -Parent
$vendor = Join-Path $repo 'vendor/pwsh'
New-Item -ItemType Directory -Force $vendor | Out-Null

if (-not $PwshVersion) {
    $envFile = Join-Path $repo '.env'
    $line = if (Test-Path $envFile) { Get-Content $envFile | Where-Object { $_ -match '^\s*PWSH_VERSION\s*=' } | Select-Object -Last 1 }
    $PwshVersion = if ($line) { ($line -split '=', 2)[1].Trim().Trim('"', "'") } else { '7.6.9' }
}
$archs = switch ($Arch) {
    'both'  { 'arm64', 'x64' }
    'auto'  { if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'Arm64') { 'arm64' } else { 'x64' } }
    default { $Arch }
}

# 1. PowerShell for Linux, checked against the release's hashes.sha256 when it can be read.
$base = "https://github.com/PowerShell/PowerShell/releases/download/v$PwshVersion"
$hashes = $null
# hashes.sha256 is UTF-16 with a byte order mark, lines "<hash> *<file name>"; Get-Content reads it right.
$hashFile = Join-Path ([IO.Path]::GetTempPath()) "pwsh-$PwshVersion-hashes.sha256"
try {
    Invoke-WebRequest "$base/hashes.sha256" -OutFile $hashFile -UseBasicParsing
    $hashes = Get-Content $hashFile
    Remove-Item $hashFile
} catch { Write-Warning "Could not read hashes.sha256 for v$PwshVersion; the download is not checked." }
foreach ($a in $archs) {
    $name = "powershell-$PwshVersion-linux-$a.tar.gz"
    $file = Join-Path $vendor $name
    if (-not (Test-Path $file)) {
        Write-Host "Downloading $name ..."
        Invoke-WebRequest "$base/$name" -OutFile "$file.part" -UseBasicParsing
        Move-Item "$file.part" $file -Force
    } else { Write-Host "$name is already here." }
    if ($hashes) {
        $want = ($hashes | Where-Object { $_ -match ('[\s*]' + [regex]::Escape($name) + '\s*$') } | Select-Object -First 1) -replace '\s.*$', ''
        if (-not $want) { Write-Warning "$name is not listed in hashes.sha256; not checked." }
        $got  = (Get-FileHash $file -Algorithm SHA256).Hash
        if ($want -and $want -ne $got) { Remove-Item $file; throw "SHA256 mismatch for $name (removed). Expected $want, got $got." }
        if ($want) { Write-Host "  SHA256 OK" }
    }
}
# Older PowerShell tarballs in the folder are not used by the build; remove them to save space.
Get-ChildItem $vendor -Filter 'powershell-*-linux-*.tar.gz' | Where-Object { $_.Name -notlike "powershell-$PwshVersion-*" } |
    ForEach-Object { Write-Host "Removing old $($_.Name)"; Remove-Item $_.FullName }

# 2. PowerCLI and the modules it needs, saved to a temp folder first, then swapped in.
$tmp = Join-Path $vendor 'Modules.new'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory $tmp | Out-Null
$p = @{ Name = 'VMware.PowerCLI'; Path = $tmp; TrustRepository = $true }
if ($PowerCLIVersion) { $p.Version = $PowerCLIVersion }
Write-Host "Saving VMware.PowerCLI $(if ($PowerCLIVersion) { $PowerCLIVersion } else { '(newest)' }) from the PowerShell Gallery ..."
try { Save-PSResource @p } catch { Remove-Item $tmp -Recurse -Force; throw }
$mods = Join-Path $vendor 'Modules'
if (Test-Path $mods) { Remove-Item $mods -Recurse -Force }
Move-Item $tmp $mods

$saved = Get-ChildItem (Join-Path $mods 'VMware.PowerCLI') -Directory | Select-Object -ExpandProperty Name
$size  = [math]::Round(((Get-ChildItem $vendor -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 0)
Write-Host ""
Write-Host "Saved PowerShell $PwshVersion ($($archs -join ', ')) and PowerCLI $saved in vendor/pwsh ($size MB)."
Write-Host "Next build: docker compose build vco   (the log shows 'using local vendor/pwsh')"
