<#
.SYNOPSIS
  Collect ESXi host health from vCenter with PowerCLI.

.DESCRIPTION
  Follows the vCommunity-Observability collector contract:
    * Connection details arrive as $env:VCO_VCENTER_HOST / _USER / _PASSWORD.
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

function Get-SampleHosts {
    $hosts = @(
        @('esx01.vcrocs.local', 'cl-wld01', 'green',  0, 42.5, 61.2, 18, 41.2),
        @('esx02.vcrocs.local', 'cl-wld01', 'green',  0, 55.1, 72.8, 22, 41.2),
        @('esx03.vcrocs.local', 'cl-wld01', 'yellow', 0, 81.3, 88.4, 27, 12.7),
        @('esx04.vcrocs.local', 'cl-wld01', 'green',  1,  0.4,  3.1,  0,  0.3),
        @('esx05.vcrocs.local', 'cl-mgmt',  'green',  0, 33.0, 64.5, 11, 96.8),
        @('esx06.vcrocs.local', 'cl-mgmt',  'green',  0, 29.7, 59.9,  9, 96.8)
    )
    foreach ($h in $hosts) {
        [ordered]@{
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

function Get-LiveHosts {
    if (-not $env:VCO_VCENTER_HOST)     { throw 'VCO_VCENTER_HOST is not set; configure the vcenter connection in config.yaml' }
    if (-not $env:VCO_VCENTER_PASSWORD) { throw 'vCenter password is empty: set VCO_VCENTER_PASSWORD in .env and make sure config.yaml has password_env: VCO_VCENTER_PASSWORD' }
    Import-Module VMware.VimAutomation.Core -WarningAction SilentlyContinue | Out-Null
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Ignore -ParticipateInCEIP $false `
        -DisplayDeprecationWarnings $false -Confirm:$false | Out-Null

    $secure = ConvertTo-SecureString $env:VCO_VCENTER_PASSWORD -AsPlainText -Force
    $cred   = [pscredential]::new($env:VCO_VCENTER_USER, $secure)
    $vc     = Connect-VIServer -Server $env:VCO_VCENTER_HOST -Credential $cred
    try {
        $rows = foreach ($vmhost in Get-VMHost -Server $vc) {
            $view  = $vmhost.ExtensionData
            $stats = $view.Summary.QuickStats
            [ordered]@{
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
        Write-Log "collected $(@($rows).Count) hosts from $($env:VCO_VCENTER_HOST)"
        $rows
    }
    finally {
        Disconnect-VIServer -Server $vc -Confirm:$false | Out-Null
    }
}

$rows = if ($env:VCO_MOCK -eq '1') { Get-SampleHosts } else { Get-LiveHosts }
$json = @{ hosts = @($rows) } | ConvertTo-Json -Depth 4 -Compress
if ($env:VCO_OUTPUT_FILE) { [IO.File]::WriteAllText($env:VCO_OUTPUT_FILE, $json) } else { $json }
