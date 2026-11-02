<#
.SYNOPSIS
  Collect ESXi host health from one or more vCenters with PowerCLI.

.DESCRIPTION
  Follows the vCommunity-Observability collector contract:
    * Connection details arrive as $env:VCO_VCENTER_HOST / _USER / _PASSWORD
      and optionally $env:VCO_VCENTERS (JSON array of instances).
    * $env:VCO_MOCK = '1' returns built-in sample data instead of connecting.
    * One JSON object keyed by table name is written to $env:VCO_OUTPUT_FILE
      (or stdout when run by hand). PowerCLI warnings and banners can appear
      on stdout, so the file is what the core reads.
    * Logs go to stderr (never Write-Host, which pwsh sends to stdout).
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'
$WarningPreference     = 'SilentlyContinue'   # PowerCLI deprecation/certificate warnings
$InformationPreference = 'SilentlyContinue'

function Write-Log([string]$Message) { [Console]::Error.WriteLine($Message) }

function Get-VCenters {
    if ($env:VCO_VCENTERS) {
        try {
            $items = $env:VCO_VCENTERS | ConvertFrom-Json
            if ($items -is [System.Array] -and $items.Count -gt 0) { return @($items) }
            if ($items) { return @($items) }
        } catch { }
    }
    $hostName = $env:VCO_VCENTER_HOST
    if (-not $hostName -and $env:VCO_MOCK -ne '1') { return @() }
    return @(
        [pscustomobject]@{
            id = 'default'; label = $(if ($hostName) { $hostName } else { 'vCenter' })
            host = $hostName; user = $env:VCO_VCENTER_USER
            password = $env:VCO_VCENTER_PASSWORD; verify_ssl = $env:VCO_VCENTER_VERIFY_SSL
        }
    )
}

function Get-SampleHosts([string]$VCenterId, [int]$Index) {
    $suffix = if ($Index -eq 0) { '' } else { "-$VCenterId" }
    $hosts = @(
        @("esx01.vcrocs.local$suffix", "cl-wld01$suffix", 'green',  0, 42.5, 61.2, 18, 41.2),
        @("esx02.vcrocs.local$suffix", "cl-wld01$suffix", 'green',  0, 55.1, 72.8, 22, 41.2),
        @("esx03.vcrocs.local$suffix", "cl-wld01$suffix", 'yellow', 0, 81.3, 88.4, 27, 12.7),
        @("esx04.vcrocs.local$suffix", "cl-wld01$suffix", 'green',  1,  0.4,  3.1,  0,  0.3),
        @("esx05.vcrocs.local$suffix", "cl-mgmt$suffix",  'green',  0, 33.0, 64.5, 11, 96.8),
        @("esx06.vcrocs.local$suffix", "cl-mgmt$suffix",  'green',  0, 29.7, 59.9,  9, 96.8)
    )
    foreach ($h in $hosts) {
        [ordered]@{
            vcenter          = $VCenterId
            name             = $h[0]
            cluster          = $h[1]
            connection_state = 'Connected'
            power_state      = 'PoweredOn'
            overall_status   = $h[2]
            maintenance_mode = $h[3]
            version          = '9.0.1'
            build            = '24957456'
            cpu_usage_pct    = $h[4]
            mem_usage_pct    = $h[5]
            vm_count         = $h[6]
            uptime_days      = $h[7]
        }
    }
}

function Get-LiveHosts($Vc) {
    if (-not $Vc.host)     { throw "vCenter '$($Vc.id)' has no host; configure it on the Settings page" }
    if (-not $Vc.password) { throw "vCenter '$($Vc.id)' password is empty: set it on the Settings page or in .env" }
    Import-Module VMware.VimAutomation.Core -WarningAction SilentlyContinue | Out-Null
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -ParticipateInCEIP $false `
        -DisplayDeprecationWarnings $false -Confirm:$false | Out-Null

    $secure = ConvertTo-SecureString $Vc.password -AsPlainText -Force
    $cred   = [pscredential]::new($Vc.user, $secure)
    $server = Connect-VIServer -Server $Vc.host -Credential $cred
    try {
        $rows = foreach ($vmhost in Get-VMHost -Server $server) {
            $view  = $vmhost.ExtensionData
            $stats = $view.Summary.QuickStats
            [ordered]@{
                vcenter          = $Vc.id
                name             = $vmhost.Name
                cluster          = if ($vmhost.Parent) { $vmhost.Parent.Name } else { '' }
                connection_state = "$($vmhost.ConnectionState)"
                power_state      = "$($vmhost.PowerState)"
                overall_status   = "$($view.OverallStatus)"
                maintenance_mode = [int]($vmhost.ConnectionState -eq 'Maintenance')
                version          = $vmhost.Version
                build            = $vmhost.Build
                cpu_usage_pct    = if ($vmhost.CpuTotalMhz) { [math]::Round($vmhost.CpuUsageMhz / $vmhost.CpuTotalMhz * 100, 1) } else { $null }
                mem_usage_pct    = if ($vmhost.MemoryTotalGB) { [math]::Round($vmhost.MemoryUsageGB / $vmhost.MemoryTotalGB * 100, 1) } else { $null }
                vm_count         = @($view.Vm).Count
                uptime_days      = if ($stats.Uptime) { [math]::Round($stats.Uptime / 86400, 1) } else { $null }
            }
        }
        Write-Log "collected $(@($rows).Count) hosts from $($Vc.host) ($($Vc.id))"
        $rows
    }
    finally {
        Disconnect-VIServer -Server $server -Confirm:$false | Out-Null
    }
}

$instances = @(Get-VCenters)
if ($instances.Count -eq 0) { throw 'No vCenter configured; add one on the Settings page or in config.yaml' }
$all = [System.Collections.Generic.List[object]]::new()
$i = 0
foreach ($vc in $instances) {
    $batch = if ($env:VCO_MOCK -eq '1') { @(Get-SampleHosts -VCenterId $vc.id -Index $i) } else { @(Get-LiveHosts $vc) }
    foreach ($row in $batch) { $all.Add($row) }
    $i++
}
$json = @{ hosts = @($all.ToArray()) } | ConvertTo-Json -Depth 4 -Compress
if ($env:VCO_OUTPUT_FILE) { [IO.File]::WriteAllText($env:VCO_OUTPUT_FILE, $json) } else { $json }
