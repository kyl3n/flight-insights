<# Read-only host evidence and actual installed WebView/IPC. No network repairs,
   camera requests, vehicle commands, elevation, signing changes, or uploads. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ApplicationPath,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$MissionPlannerVersion = 'not supplied',
    [string]$QGroundControlVersion = 'not supplied'
)
$ErrorActionPreference = 'Stop'
$app = (Resolve-Path -LiteralPath $ApplicationPath).Path
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'Use an absolute new report-directory path.' }
if ((Test-Path -LiteralPath $OutputDirectory) -or (Test-Path -LiteralPath ($OutputDirectory+'.zip'))) {
    throw 'Report path exists; choose a new path to preserve prior evidence.'
}
$null = New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'native-gui')
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$gui = Join-Path $output 'native-gui'
$report = Join-Path $gui 'result.json'
$random = New-Object byte[] 32
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $generator.GetBytes($random) } finally { $generator.Dispose() }
$nonce = ([BitConverter]::ToString($random)).Replace('-','').ToLowerInvariant()
$hostEvidence = @{
    schema_version = 1; observed_at_utc = [DateTime]::UtcNow.ToString('o')
    mission_planner_version_operator_declared = $MissionPlannerVersion
    qgroundcontrol_version_operator_declared = $QGroundControlVersion
    radio_operator_declared = 'DigiQ SkyPulse'; camera_operator_declared = 'SIYI A8 Mini'
    physical_streams_verified = $false; network_changes_requested = $false; uploads = $false
}
$exitCode = 1
try {
$hostEvidence.os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
$hostEvidence.application = Get-FileHash -Algorithm SHA256 -LiteralPath $app | Select-Object Algorithm,Hash
Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $output 'adapters.json')
Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength,AddressState | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $output 'ipv4-addresses.json')
Get-NetRoute -AddressFamily IPv4 | Select-Object InterfaceAlias,DestinationPrefix,NextHop,RouteMetric | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $output 'ipv4-routes.json')
# Start-Process must receive explicit quoting for space-containing paths. No shell
# or command expression is used; nonce is fresh hexadecimal and paths are local.
if ($gui.Contains('"') -or $report.Contains('"')) { throw 'Unsupported quote in report path.' }
$arguments = '--verify-gui --data-dir "'+$gui+'" --report "'+$report+'" --nonce '+$nonce
$process = Start-Process -FilePath $app -ArgumentList $arguments -PassThru
# Keep the process handle available while waiting; do not reopen it by PID.
$null = $process.Handle
if (-not $process.WaitForExit(90000)) {
    $hostEvidence.native_gui_timed_out = $true
    $process.Kill()
    $null = $process.WaitForExit(5000)
    throw 'Native GUI qualification exceeded its 90-second outer deadline.'
}
$process.Refresh()
$hostEvidence.native_gui_exit_code = $process.ExitCode
$exitCode = $process.ExitCode
if ($process.ExitCode -eq 0) {
    if (-not (Test-Path -LiteralPath $report) -or (Get-Item -LiteralPath $report).Length -gt 131072) {
        throw 'Native GUI qualification did not produce bounded evidence.'
    }
    $guiEvidence = Get-Content -Raw -LiteralPath $report | ConvertFrom-Json
    if ($guiEvidence.nonce -ne $nonce -or $guiEvidence.native_gui_rendering -ne 'verified' -or
        $guiEvidence.owned_engine_shutdown_completed -ne $true) {
        throw 'Native GUI evidence does not match this invocation or did not pass.'
    }
}
} catch {
    $exitCode = 1
    $hostEvidence.qualification_error = $_.Exception.Message
    Write-Warning ('Qualification failed; preserving local evidence: '+$_.Exception.Message)
} finally {
$hostEvidence | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $output 'host.json')
@'
This local package proves only what native-gui/result.json reports for this
installed binary and host. It does not establish physical streams, GCS
coexistence, automatic repairs, sleep/wake, or minimum desktop Windows support.
Network addresses/interface names and local paths may be present. Review before
sharing. No upload or network change was requested. Record actual GCS versions
and separate physical bench results before qualification.
'@ | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $output 'READ-ME.txt')
Compress-Archive -Path (Join-Path $output '*') -DestinationPath ($output+'.zip')
Write-Output ('Local qualification package: '+$output+'.zip')
}
exit $exitCode
