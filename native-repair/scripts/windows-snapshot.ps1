# Compiled into the helper; never loaded from a caller-selected runtime path.
# Every cmdlet below is read-only. Bulk rows are independently retained in CAS
# context: a foreign rule's undocumented InstanceID join is not ownership proof.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'

function Assert-Q22FilterClass($Row,[string]$Expected) {
 $actual=[string]$Row.CimClass.CimClassName
 if($actual -cne $Expected){
  # Only bounded class metadata enters diagnostics; no rule names or IDs.
  $display=$actual -replace '[^A-Za-z0-9_]','?'
  if($display.Length -gt 96){$display=$display.Substring(0,96)}
  if($display.Length -eq 0){$display='<missing>'}
  throw "Native firewall filter class mismatch: expected=$Expected actual=$display"
 }
}
function New-Q22FilterMap([object[]]$Rows,[string]$ClassName) {
 if($Rows.Count -gt 16384){throw 'Native firewall filter capacity exceeded'}
 $map=[Collections.Hashtable]::new([StringComparer]::Ordinal)
 foreach($row in $Rows){
  $id=[string]$row.InstanceID
  if([string]::IsNullOrWhiteSpace($id)){throw 'Native firewall filter InstanceID is missing'}
  if($id.Length -gt 1024){throw 'Native firewall filter InstanceID exceeds limit'}
  Assert-Q22FilterClass $row $ClassName
  if($map.ContainsKey($id)){throw 'Duplicate native firewall filter identity'}
  $map.Add($id,$row)
 }
 return $map
}
function Get-Q22FilterProjection($Row,[string[]]$Properties) {
 $value=[ordered]@{}
 foreach($property in $Properties){
  # Retain null and array types from the provider; never silently invent defaults.
  if($null -eq $Row.PSObject.Properties[$property]){throw "Native filter property unavailable: $property"}
  $value[$property]=$Row.$property
 }
 return $value
}
function Assert-Q22Association($Rule,$Spec,$Map) {
 $id=[string]$Rule.InstanceID
 if(-not $Map.ContainsKey($id)){throw 'Native firewall rule has no unique bulk filter'}
 $associated=@(& $Spec.command -AssociatedNetFirewallRule $Rule -PolicyStore ActiveStore | Select-Object -First 2)
 if($associated.Count -ne 1){throw 'Native provider filter association is missing or ambiguous'}
 Assert-Q22FilterClass $associated[0] $Spec.class
 if([string]$associated[0].InstanceID -cne $id){throw 'Native provider associated InstanceID differs from the bulk identity contract'}
 $expected=Get-Q22FilterProjection $Map[$id] $Spec.properties | ConvertTo-Json -Depth 8 -Compress
 $actual=Get-Q22FilterProjection $associated[0] $Spec.properties | ConvertTo-Json -Depth 8 -Compress
 if($actual -cne $expected){throw 'Native associated filter changed or differs from the bulk record'}
}
# Provider class names are the documented OUTPUTS, not the cmdlet names:
# MicrosoftDocs/windows-powershell-docs, docset/winserver2025-ps/NetSecurity/
# Get-NetFirewall{Address,Port,Interface,Application,Service,InterfaceType,Security}Filter.md.
function Get-Q22FilterSpecs {
 return @(
  @{kind='address';command='Get-NetFirewallAddressFilter';class='MSFT_NetAddressFilter';properties=@('LocalAddress','RemoteAddress')},
  @{kind='port';command='Get-NetFirewallPortFilter';class='MSFT_NetProtocolPortFilter';properties=@('Protocol','LocalPort','RemotePort','IcmpType','DynamicTarget')},
  @{kind='interface';command='Get-NetFirewallInterfaceFilter';class='MSFT_NetInterfaceFilter';properties=@('InterfaceAlias')},
  @{kind='application';command='Get-NetFirewallApplicationFilter';class='MSFT_NetApplicationFilter';properties=@('Program','Package')},
  @{kind='service';command='Get-NetFirewallServiceFilter';class='MSFT_NetServiceFilter';properties=@('Service')},
  @{kind='interface_type';command='Get-NetFirewallInterfaceTypeFilter';class='MSFT_NetInterfaceTypeFilter';properties=@('InterfaceType')},
  @{kind='security';command='Get-NetFirewallSecurityFilter';class='MSFT_NetNetworkLayerSecurityFilter';properties=@('Authentication','Encryption','OverrideBlockRules','LocalUser','RemoteUser','RemoteMachine')}
 )
}
function Get-Q22NativeSnapshot {
 $ipByIndex=@{}
 Get-NetIPInterface -AddressFamily IPv4 -PolicyStore ActiveStore | Select-Object -First 257 | ForEach-Object {
  $key=[uint32]$_.InterfaceIndex
  if($ipByIndex.ContainsKey($key)){throw 'Duplicate native IPv4 adapter identity'}
  $ipByIndex.Add($key,$_)
 }
 if($ipByIndex.Count -gt 256){throw 'Native adapter capacity exceeded'}
 $nativeAdapters=@(Get-NetAdapter -IncludeHidden | Select-Object -First 257)
 if($nativeAdapters.Count -gt 256){throw 'Native adapter capacity exceeded'}
 $adapters=@($nativeAdapters | ForEach-Object {
  if($ipByIndex.ContainsKey([uint32]$_.ifIndex)){
   $ip=$ipByIndex[[uint32]$_.ifIndex]
   [ordered]@{name=[string]$_.Name;guid=[string]$_.InterfaceGuid;index=[uint32]$_.ifIndex;up=($_.Status -eq 'Up');dhcp=($ip.Dhcp -eq 'Enabled');interface_metric=[uint32]$ip.InterfaceMetric}
  }
 })
 $addresses=@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object -First 4097 | ForEach-Object {[ordered]@{address=[string]$_.IPAddress;prefix=[uint32]$_.PrefixLength;index=[uint32]$_.InterfaceIndex}})
 $routes=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | Select-Object -First 8193 | ForEach-Object {$parts=$_.DestinationPrefix.Split('/');[ordered]@{destination=$parts[0];prefix=[uint32]$parts[1];gateway=[string]$_.NextHop;index=[uint32]$_.InterfaceIndex;metric=[uint32]$_.RouteMetric}})
 $dns=@(Get-DnsClientServerAddress -AddressFamily IPv4 | Select-Object -First 257 | ForEach-Object {[ordered]@{index=[uint32]$_.InterfaceIndex;addresses=@($_.ServerAddresses | ForEach-Object {[string]$_})}})
 $profiles=@(Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object -First 4 | ForEach-Object {[ordered]@{name=[string]$_.Name;enabled=($_.Enabled -eq 'True');allow_local_rules=[string]$_.AllowLocalFirewallRules;default_inbound=[string]$_.DefaultInboundAction;default_outbound=[string]$_.DefaultOutboundAction}})
 $nativeRules=@(Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object {$_.Enabled -eq 'True' -or $_.Name.StartsWith('Q22-Helper-')} | Select-Object -First 8193)
 if($nativeRules.Count -gt 8192){throw 'Native firewall rule capacity exceeded'}
 $ruleById=[Collections.Hashtable]::new([StringComparer]::Ordinal)
 $ruleNames=[Collections.Hashtable]::new([StringComparer]::OrdinalIgnoreCase)
 foreach($rule in $nativeRules){$id=[string]$rule.InstanceID;if([string]::IsNullOrWhiteSpace($id) -or $id.Length -gt 1024 -or $ruleById.ContainsKey($id) -or $ruleNames.ContainsKey([string]$rule.Name)){throw 'Missing or duplicate native firewall rule identity'};$ruleById.Add($id,$rule);$ruleNames.Add([string]$rule.Name,$true)}
 $specs=Get-Q22FilterSpecs
 $maps=@{}
 foreach($spec in $specs){
  $rows=@(& $spec.command -PolicyStore ActiveStore | Select-Object -First 16385)
  $maps[$spec.kind]=New-Q22FilterMap $rows $spec.class
 }
 # A stable pair of non-helper probes checks the provider's key contract. This
 # is not exhaustive foreign association proof; independent bulk context below
 # preserves every foreign filter even when its normalized join is unverified.
 $foreign=@($nativeRules | Where-Object {-not $_.Name.StartsWith('Q22-Helper-')} | Sort-Object InstanceID)
 $verify=[Collections.Hashtable]::new([StringComparer]::Ordinal)
 if($foreign.Count -gt 0){$verify.Add([string]$foreign[0].InstanceID,$foreign[0]);if($foreign.Count -gt 1){$verify.Add([string]$foreign[-1].InstanceID,$foreign[-1])}}
 $helperRules=@($nativeRules | Where-Object {$_.Name.StartsWith('Q22-Helper-')})
 if($helperRules.Count -gt 16){throw 'Helper-rule association verification capacity exceeded; no partial snapshot permitted'}
 foreach($rule in $helperRules){$verify.Add([string]$rule.InstanceID,$rule)}
 foreach($rule in $verify.Values){foreach($spec in $specs){Assert-Q22Association $rule $spec $maps[$spec.kind]}}
 $filters=@(foreach($spec in $specs){foreach($id in $maps[$spec.kind].Keys){
  $association=$null;if($verify.ContainsKey($id)){$association=[string]$verify[$id].Name}
  [ordered]@{kind=$spec.kind;instance_id=$id;properties=(Get-Q22FilterProjection $maps[$spec.kind][$id] $spec.properties);association_rule=$association}
 }})
 $rules=@(foreach($rule in $nativeRules){
  $id=[string]$rule.InstanceID
  foreach($spec in $specs){if(-not $maps[$spec.kind].ContainsKey($id)){throw 'Missing native firewall rule/filter association'}}
  $address=$maps.address[$id];$port=$maps.port[$id];$interface=$maps.interface[$id];$app=$maps.application[$id];$service=$maps.service[$id];$type=$maps.interface_type[$id];$security=$maps.security[$id]
  [ordered]@{name=[string]$rule.Name;instance_id=$id;association_verified=$verify.ContainsKey($id);enabled=($rule.Enabled -eq 'True');direction=[string]$rule.Direction;action=[string]$rule.Action;protocol=[string]$port.Protocol;local_address=@($address.LocalAddress | ForEach-Object {[string]$_});remote_address=@($address.RemoteAddress | ForEach-Object {[string]$_});local_port=@($port.LocalPort | ForEach-Object {[string]$_});remote_port=@($port.RemotePort | ForEach-Object {[string]$_});interfaces=@($interface.InterfaceAlias | ForEach-Object {[string]$_});profile=[string]$rule.Profile;program=[string]$app.Program;package=[string]$app.Package;icmp_type=@($port.IcmpType | ForEach-Object {[string]$_});dynamic_target=[string]$port.DynamicTarget;service=[string]$service.Service;interface_type=[string]$type.InterfaceType;edge_traversal=[string]$rule.EdgeTraversalPolicy;policy_source_type=[string]$rule.PolicyStoreSourceType;authentication=[string]$security.Authentication;encryption=[string]$security.Encryption;override_block_rules=($security.OverrideBlockRules -eq 'True');local_user=[string]$security.LocalUser;remote_user=[string]$security.RemoteUser;remote_machine=[string]$security.RemoteMachine}
 })
 $mdm=@();if(Test-Path 'HKLM:\SOFTWARE\Microsoft\Enrollments' -ErrorAction Stop){$mdm=@(Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Enrollments' -ErrorAction Stop | Get-ItemProperty -ErrorAction Stop | Where-Object {$_.UPN -or $_.EnrollmentType})}
 return [ordered]@{adapters=$adapters;addresses=$addresses;routes=$routes;dns=$dns;profiles=$profiles;rules=$rules;firewall_filters=$filters;domain_joined=[bool](Get-CimInstance Win32_ComputerSystem).PartOfDomain;mdm_enrolled=($mdm.Count -gt 0);firewall_running=((Get-Service MpsSvc).Status -eq 'Running')}
}
