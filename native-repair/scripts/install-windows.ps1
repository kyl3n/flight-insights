#Requires -RunAsAdministrator
<#
Explicit private-bench install, never launched by the app; no route/firewall writes.
The frozen engine directory must already be protected under Program Files.
Example (replace all values with the bench's actual identity and endpoints):
 .\install-windows.ps1 -HelperExe C:\build\q22-network-helper.exe `
  -EngineExe 'C:\Program Files\Q22\q22-engine\q22-engine.exe' `
  -ClientSid 'S-1-5-21-...' -InterfaceAlias Ethernet -DeviceHost 10.0.0.2 `
  -DeviceVerificationPort 80 -InternetProbeHost 1.1.1.1 -InternetProbePort 443 `
  -ObserverAddress 10.0.0.1 -ObserverPort 14660 -ObserverSender 10.0.0.2 `
  -SystemId 1 -ComponentId 1 -ExpectedUid 123456
Observer needs its own forwarded HEARTBEAT, AUTOPILOT_VERSION, SYSTEM_TIME.
It never sends. Unsigned telemetry cannot prove authenticity or exclude replay.
Optional -RequireSigned requires -SigningKeyFile containing 32 binary bytes.
Uninstall: .\install-windows.ps1 -Uninstall. Unresolved repairs block removal;
use the enrolled app's recovery review/rollback while disarmed first. Manually
review foreign changes; never delete similarly named rules blindly. Evidence is
retained after uninstall; reenrollment requires explicit archival/migration.
#>
[CmdletBinding(SupportsShouldProcess=$true,ConfirmImpact='High')]
param([switch]$Uninstall,[string]$HelperExe,[string]$EngineExe,[string]$ClientSid,
 [string[]]$InterfaceAlias,[string]$DeviceHost,[ValidateRange(1,65535)][int]$DeviceVerificationPort=80,
 [string]$InternetProbeHost,[ValidateRange(1,65535)][int]$InternetProbePort=443,
 [string]$ObserverAddress,[ValidateRange(1,65535)][int]$ObserverPort=14660,[string]$ObserverSender,
 [ValidateRange(1,255)][int]$SystemId=1,[ValidateRange(1,255)][int]$ComponentId=1,
 [UInt64]$ExpectedUid=0,[switch]$RequireSigned,[string]$SigningKeyFile)
$ErrorActionPreference='Stop';$service='Q22NetworkRepair'
$root=Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'Q22\NetworkRepair'
$binaryRoot=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'Q22\NetworkRepair'
$installed=Join-Path $binaryRoot 'q22-network-helper.exe';$journal=Join-Path $root 'journal'
function Assert-NoReparse([string]$Path){$item=Get-Item -LiteralPath $Path -Force;while($item){if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw "Reparse path rejected: $($item.FullName)"};$item=$item.Parent}}
function Protect-Path([string]$Path,[switch]$Reader){
 Assert-NoReparse $Path;$directory=(Get-Item -LiteralPath $Path).PSIsContainer
 $acl=if($directory){[Security.AccessControl.DirectorySecurity]::new()}else{[Security.AccessControl.FileSecurity]::new()}
 $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'));$acl.SetAccessRuleProtection($true,$false)
 foreach($sid in @('S-1-5-18','S-1-5-32-544')){
  $inherit=if($directory){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),[Security.AccessControl.FileSystemRights]::FullControl,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow))
 }
 if($Reader){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($ClientSid),[Security.AccessControl.FileSystemRights]::ReadAndExecute,[Security.AccessControl.AccessControlType]::Allow))}
 Set-Acl -LiteralPath $Path -AclObject $acl
}
if($Uninstall){
 if(-not $PSCmdlet.ShouldProcess($service,'Stop/drain service, inspect recovery receipts, delete service only if resolved; retain evidence')){return}
 $existing=Get-Service $service -ErrorAction SilentlyContinue
 if($existing){& "$env:SystemRoot\System32\sc.exe" stop $service|Out-Null;$existing.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(120))}
 try {
  if(Test-Path -LiteralPath $journal){Assert-NoReparse $journal;foreach($receipt in Get-ChildItem -LiteralPath $journal -Filter '*.json'){
   $record=Get-Content -LiteralPath $receipt.FullName -Raw|ConvertFrom-Json
   if($record.version -ne 1 -or $record.state -notin @('prepared','rolled_back')){throw 'Unresolved/unrecognized receipt: complete recovery review before uninstall.'}
  }}
 } catch {if($existing){Start-Service $service};throw}
 if($existing){& "$env:SystemRoot\System32\sc.exe" delete $service|Out-Null;if($LASTEXITCODE -ne 0){Start-Service $service;throw 'Service deletion failed'}}
 return
}
if(-not $HelperExe -or -not $EngineExe -or -not $ClientSid -or -not $InterfaceAlias){throw 'Explicit helper/engine, SID and adapter aliases required.'}
if($ObserverPort -in @(14550,5600)){throw 'Use a dedicated observer port, separate from GCS/video.'}
$sid=[Security.Principal.SecurityIdentifier]::new($ClientSid);if($sid.Value -ne $ClientSid){throw 'SID must be canonical.'}
foreach($value in @($DeviceHost,$InternetProbeHost,$ObserverAddress,$ObserverSender)){$address=[Net.IPAddress]::Parse($value);if($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $address.Equals([Net.IPAddress]::Any) -or $address.GetAddressBytes()[0] -ge 224){throw 'Explicit unicast IPv4 required.'}}
$HelperExe=(Resolve-Path -LiteralPath $HelperExe).Path;$EngineExe=(Resolve-Path -LiteralPath $EngineExe).Path
Assert-NoReparse $HelperExe;Assert-NoReparse $EngineExe
if([IO.Path]::GetFileName($EngineExe) -cne 'q22-engine.exe'){throw 'Only frozen q22-engine.exe can be enrolled, no interpreter.'}
if($EngineExe -notlike "$([Environment]::GetFolderPath('ProgramFiles'))\*"){throw 'Install frozen engine under administrator-controlled Program Files first.'}
if($RequireSigned -and -not $SigningKeyFile){throw 'RequireSigned needs a binary key file.'}
$keyBytes=$null;if($SigningKeyFile){$keyBytes=[IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $SigningKeyFile).Path);if($keyBytes.Length -ne 32){throw 'Key must be 32 binary bytes.'}}
if(Get-Service $service -ErrorAction SilentlyContinue){throw 'Service exists; review recovery and uninstall before replacing enrollment.'}
if(Test-Path -LiteralPath (Join-Path $root 'enrollment.json')){throw 'Enrollment/history exists; explicitly archive/migrate before reenrollment. Nothing overwritten.'}
if(-not $PSCmdlet.ShouldProcess($service,'Install experimental LocalSystem helper with exact executable hashes and explicit network policy')){return}
function Assert-ProtectedParent([string]$Path) {
 $item=Get-Item -LiteralPath $Path -Force
 $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
 while($item){
  Assert-NoReparse $item.FullName;$acl=Get-Acl -LiteralPath $item.FullName
  if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted){throw 'Existing installation ancestor is not administrator-owned.'}
  foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
   if($rule.AccessControlType -eq 'Allow' -and -not($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and $rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.FileSystemRights -band 0x500D0150)){throw 'Existing installation ancestor can be replaced by an untrusted user.'}
  };$item=$item.Parent
 }
}
# Check the actual frozen engine tree, not just its bootloader EXE. Existing
# DLL/module files with individual writable ACLs cannot become privileged peers.
$engineDirectory=Split-Path $EngineExe
Assert-ProtectedParent $engineDirectory
$engineFileCount=0
Get-ChildItem -LiteralPath $engineDirectory -Recurse -Force -ErrorAction Stop | ForEach-Object {
 $entry=$_;$engineFileCount++
 if($engineFileCount -gt 50000){throw 'Frozen engine tree exceeds the installation review bound.'}
 Assert-NoReparse $entry.FullName
 $acl=Get-Acl -LiteralPath $entry.FullName
 $trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
 if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted){throw 'Frozen engine tree owner must be SYSTEM, Administrators or TrustedInstaller.'}
 foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
  if($rule.AccessControlType -eq 'Allow' -and -not($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and $rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.FileSystemRights -band 0x500D0156)){throw 'Frozen engine tree includes an untrusted write grant.'}
 }
}
# Fresh, protected subdirectories exclude hostile preexisting links and receipts.
foreach($directory in @($root,$binaryRoot)){if(Test-Path -LiteralPath $directory){throw 'Helper destination exists; review/archive it explicitly before installation.'}}
foreach($parent in @((Split-Path $root),(Split-Path $binaryRoot))){
 if(Test-Path -LiteralPath $parent){Assert-ProtectedParent $parent}
 else{Assert-ProtectedParent (Split-Path $parent);New-Item -ItemType Directory -Path $parent -ErrorAction Stop|Out-Null;Protect-Path $parent}
}
foreach($directory in @($root,$journal,$binaryRoot)){New-Item -ItemType Directory -Path $directory -ErrorAction Stop|Out-Null;Protect-Path $directory}

Copy-Item -LiteralPath $HelperExe -Destination $installed;Protect-Path $installed
$keyPath=$null;if($keyBytes){$keyPath=Join-Path $root 'observer-signing.key';[IO.File]::WriteAllBytes($keyPath,$keyBytes);Protect-Path $keyPath;[Array]::Clear($keyBytes,0,$keyBytes.Length)}
$uid=if($ExpectedUid -eq 0){$null}else{$ExpectedUid}
$config=[ordered]@{version=1;client_principal=$ClientSid;client_image=$EngineExe
 client_sha256=(Get-FileHash -LiteralPath $EngineExe -Algorithm SHA256).Hash.ToLowerInvariant()
 helper_sha256=(Get-FileHash -LiteralPath $installed -Algorithm SHA256).Hash.ToLowerInvariant()
 allowed_interfaces=@($InterfaceAlias);devices=@([ordered]@{host=$DeviceHost;verification_port=$DeviceVerificationPort})
 internet_probe=[ordered]@{host=$InternetProbeHost;port=$InternetProbePort}
 observer=[ordered]@{local_address=$ObserverAddress;port=$ObserverPort;sender=$ObserverSender;system_id=$SystemId;component_id=$ComponentId;expected_uid=$uid;require_signed=[bool]$RequireSigned;signing_key_path=$keyPath};bench_enrollment=$true}
$configPath=Join-Path $root 'enrollment.json';[IO.File]::WriteAllText($configPath,($config|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false));Protect-Path $configPath -Reader
New-Service -Name $service -BinaryPathName ('"'+$installed+'" --service') -DisplayName 'Q22 experimental network repair supervisor' -StartupType Automatic -Description 'Explicit private bench enrollment; scoped repairs and durable rollback supervision.'|Out-Null
Start-Service $service
Write-Output 'Private bench service installed. Repair still requires app opt-in and reviewed plan approval. Hardware qualification/public signing are not established.'
