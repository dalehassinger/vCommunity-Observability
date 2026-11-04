<#
.SYNOPSIS
  Build the container images and package them for a dark site.

.DESCRIPTION
  Builds the dashboards image (vcommunity-observability) and, unless -SkipAsk is
  given, the vCommunity Ask image (vcommunity-ask). Each image is saved as a
  gzipped tarball in dist/ with a .sha256 file next to it.

.EXAMPLE
  ./scripts/New-OfflineBundle.ps1 -Version 0.1.0 -Target core-pwsh
  # Produces dist/vco-core-pwsh-0.1.0.tar.gz and dist/vco-ask-0.1.0.tar.gz, each with a .sha256 file.
  # At the dark site:  docker load -i <file>.tar.gz  (both files), then  docker compose up -d --no-build

.EXAMPLE
  ./scripts/New-OfflineBundle.ps1 -Version 0.1.0 -Target core -SkipAsk
  # Dashboards image only, without PowerShell.
#>
param(
    [Parameter(Mandatory)][string]$Version,
    [ValidateSet('core', 'core-pwsh')][string]$Target = 'core-pwsh',
    [switch]$SkipAsk
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path dist | Out-Null

function Save-Bundle([string]$Image, [string]$Name) {
    $out = Join-Path 'dist' "$Name.tar"
    docker save -o $out $Image
    if ($LASTEXITCODE) { throw "docker save failed for $Image" }

    # Compress (works on macOS, Linux and Windows PowerShell 7)
    $gz = "$out.gz"
    $in = [IO.File]::OpenRead($out); $o = [IO.File]::Create($gz)
    $z  = [IO.Compression.GZipStream]::new($o, [IO.Compression.CompressionLevel]::Optimal)
    $in.CopyTo($z); $z.Dispose(); $o.Dispose(); $in.Dispose(); Remove-Item $out

    $hash = (Get-FileHash -Algorithm SHA256 $gz).Hash.ToLower()
    "$hash  $(Split-Path $gz -Leaf)" | Set-Content "$gz.sha256"
    Write-Output "Bundle: $gz"
    Write-Output "SHA256: $hash"
}

# Dashboards image (same tag as docker-compose.yml)
$image = "vcommunity-observability:$Target-$Version"
docker build --target $Target -t $image .
if ($LASTEXITCODE) { throw 'docker build failed (dashboards image)' }
Save-Bundle $image "vco-$Target-$Version"

# vCommunity Ask image (built from the repo root, same tag as docker-compose.yml)
if (-not $SkipAsk) {
    $askImage = "vcommunity-ask:$Version"
    docker build -f ask/Dockerfile -t $askImage .
    if ($LASTEXITCODE) { throw 'docker build failed (Ask image)' }
    Save-Bundle $askImage "vco-ask-$Version"
}
