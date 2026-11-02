<#
.SYNOPSIS
  Build the container image and package it for a dark site.

.EXAMPLE
  ./scripts/New-OfflineBundle.ps1 -Version 0.1.0 -Target core-pwsh
  # Produces dist/vco-core-pwsh-0.1.0.tar.gz and a .sha256 file.
  # At the dark site:  docker load -i vco-core-pwsh-0.1.0.tar.gz ; docker compose up -d
#>
param(
    [Parameter(Mandatory)][string]$Version,
    [ValidateSet('core', 'core-pwsh')][string]$Target = 'core-pwsh'
)
$ErrorActionPreference = 'Stop'
$image = "vcommunity-observability:$Target-$Version"
$out   = Join-Path 'dist' "vco-$Target-$Version.tar"

New-Item -ItemType Directory -Force -Path dist | Out-Null
docker build --target $Target -t $image .
if ($LASTEXITCODE) { throw 'docker build failed' }
docker save -o $out $image
if ($LASTEXITCODE) { throw 'docker save failed' }

# Compress (works on macOS, Linux and Windows PowerShell 7)
$gz = "$out.gz"
$in = [IO.File]::OpenRead($out); $o = [IO.File]::Create($gz)
$z  = [IO.Compression.GZipStream]::new($o, [IO.Compression.CompressionLevel]::Optimal)
$in.CopyTo($z); $z.Dispose(); $o.Dispose(); $in.Dispose(); Remove-Item $out

$hash = (Get-FileHash -Algorithm SHA256 $gz).Hash.ToLower()
"$hash  $(Split-Path $gz -Leaf)" | Set-Content "$gz.sha256"
Write-Output "Bundle: $gz"
Write-Output "SHA256: $hash"
