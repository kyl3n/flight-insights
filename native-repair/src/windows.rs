//! Windows native state normalization and service entry. Tests never mutate a host.
#[cfg(windows)]
use crate::enrollment::Enrollment;
use crate::{platform, *};
use serde::Deserialize;
use std::collections::HashMap;
use std::net::Ipv4Addr;
#[cfg(windows)]
use std::net::{SocketAddr, TcpStream};
#[cfg(windows)]
use std::time::Duration;

#[cfg(windows)]
#[path = "windows_native.rs"]
mod native;
#[cfg(windows)]
pub use native::{serve, service_dispatch, WindowsJournal};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Adapter {
    pub name: String,
    pub guid: String,
    pub index: u32,
    pub up: bool,
    pub dhcp: bool,
    pub interface_metric: u32,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeAddress {
    pub address: Ipv4Addr,
    pub prefix: u8,
    pub index: u32,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeRoute {
    pub destination: Ipv4Addr,
    pub prefix: u8,
    pub gateway: Ipv4Addr,
    pub index: u32,
    pub metric: u32,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeDns {
    pub index: u32,
    pub addresses: Vec<Ipv4Addr>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FirewallProfile {
    pub name: String,
    pub enabled: bool,
    pub allow_local_rules: String,
    pub default_inbound: String,
    pub default_outbound: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeRule {
    pub name: String,
    pub instance_id: String,
    pub association_verified: bool,
    pub enabled: bool,
    pub direction: String,
    pub action: String,
    pub protocol: String,
    pub local_address: Vec<String>,
    pub remote_address: Vec<String>,
    pub local_port: Vec<String>,
    pub remote_port: Vec<String>,
    pub interfaces: Vec<String>,
    pub profile: String,
    pub program: String,
    pub package: String,
    pub icmp_type: Vec<String>,
    pub dynamic_target: String,
    pub service: String,
    pub interface_type: String,
    pub edge_traversal: String,
    pub policy_source_type: String,
    pub authentication: String,
    pub encryption: String,
    pub override_block_rules: bool,
    pub local_user: String,
    pub remote_user: String,
    pub remote_machine: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NativeFilter {
    pub kind: String,
    pub instance_id: String,
    pub properties: serde_json::Value,
    pub association_rule: Option<String>,
}
const FILTER_KINDS: [&str; 7] = [
    "address",
    "port",
    "interface",
    "application",
    "service",
    "interface_type",
    "security",
];
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub adapters: Vec<Adapter>,
    pub addresses: Vec<NativeAddress>,
    pub routes: Vec<NativeRoute>,
    pub dns: Vec<NativeDns>,
    pub profiles: Vec<FirewallProfile>,
    pub rules: Vec<NativeRule>,
    pub firewall_filters: Vec<NativeFilter>,
    pub domain_joined: bool,
    pub mdm_enrolled: bool,
    pub firewall_running: bool,
}

fn normalize_guid(value: &str) -> Result<String> {
    Uuid::parse_str(value.trim_matches(['{', '}']))
        .map(|id| id.hyphenated().to_string())
        .map_err(|_| "Invalid native adapter GUID".into())
}
fn exact_rule(rule: &NativeRule, plan: &Plan) -> bool {
    let Operation::FirewallUdp {
        remote_address,
        local_address,
        port,
    } = plan.candidate.operation
    else {
        return false;
    };
    rule.association_verified
        && rule.name == platform::object(plan).native_identity
        && rule.enabled
        && rule.direction == "Inbound"
        && rule.action == "Allow"
        && matches!(rule.protocol.as_str(), "UDP" | "17")
        && rule.local_address == [local_address.to_string()]
        && rule.remote_address == [remote_address.to_string()]
        && rule.local_port == [port.to_string()]
        && rule.remote_port == ["Any"]
        && rule.interfaces == [plan.before.interface.clone()]
        && rule.profile == "Any"
        && rule.program == "Any"
        && rule.package == "Any"
        && rule.icmp_type == ["Any"]
        && rule.dynamic_target == "Any"
        && rule.service == "Any"
        && rule.interface_type == "Any"
        && rule.edge_traversal == "Block"
        && rule.policy_source_type == "Local"
        && rule.authentication == "NotRequired"
        && rule.encryption == "NotRequired"
        && !rule.override_block_rules
        && rule.local_user == "Any"
        && rule.remote_user == "Any"
        && rule.remote_machine == "Any"
}

/// Only service-journal-backed, successfully witnessed plans belong in `known`.
pub fn normalize_snapshot(
    mut raw: Snapshot,
    candidate: &Candidate,
    known: &[Plan],
) -> Result<HostState> {
    candidate.validate()?;
    if raw.adapters.len() > 256
        || raw.addresses.len() > 4096
        || raw.routes.len() > 8192
        || raw.rules.len() > 8192
        || raw.firewall_filters.len() > 7 * 16384
        || raw.dns.len() > 256
        || raw.profiles.len() != 3
    {
        return Err("Native snapshot is incomplete or exceeds bounds".into());
    }
    let mut filter_ids = std::collections::HashMap::new();
    for filter in &raw.firewall_filters {
        if !FILTER_KINDS.contains(&filter.kind.as_str())
            || filter.instance_id.is_empty()
            || filter.instance_id.len() > 1024
            || !filter.properties.is_object()
            || filter.properties.to_string().len() > 65536
            || filter_ids
                .insert((filter.kind.as_str(), filter.instance_id.as_str()), filter)
                .is_some()
        {
            return Err("Invalid, duplicate or oversized native filter evidence".into());
        }
    }
    let mut rule_ids = std::collections::HashSet::new();
    let mut rule_names = std::collections::HashSet::new();
    for rule in &raw.rules {
        if rule.instance_id.is_empty()
            || rule.instance_id.len() > 1024
            || !rule_ids.insert(rule.instance_id.as_str())
            || !rule_names.insert(rule.name.to_uppercase())
        {
            return Err("Invalid or duplicate native rule identity".into());
        }
        for kind in FILTER_KINDS {
            let filter = filter_ids
                .get(&(kind, rule.instance_id.as_str()))
                .ok_or("Native rule is missing independent filter evidence")?;
            if rule.association_verified
                && filter.association_rule.as_deref() != Some(rule.name.as_str())
            {
                return Err(
                    "Native rule/filter association proof is missing or contradictory".into(),
                );
            }
        }
    }
    for adapter in &mut raw.adapters {
        adapter.guid = normalize_guid(&adapter.guid)?;
    }
    raw.adapters.sort_by_key(fingerprint);
    raw.addresses.sort_by_key(fingerprint);
    raw.routes.sort_by_key(fingerprint);
    raw.dns.sort_by_key(fingerprint);
    raw.profiles.sort_by_key(fingerprint);
    raw.rules.sort_by_key(fingerprint);
    raw.firewall_filters.sort_by_key(fingerprint);
    let selected: Vec<_> = raw
        .adapters
        .iter()
        .filter(|a| {
            a.name == candidate.interface && candidate.interface_index.is_none_or(|i| i == a.index)
        })
        .collect();
    if selected.len() != 1 {
        return Err("Adapter selection is missing or ambiguous".into());
    }
    let adapter = selected[0];
    if adapter.index == 0
        || raw.addresses.iter().any(|a| a.prefix > 32)
        || raw.routes.iter().any(|r| r.prefix > 32)
    {
        return Err("Invalid native IPv4 schema".into());
    }
    let ids: HashMap<_, _> = raw
        .adapters
        .iter()
        .map(|a| (a.index, a.guid.clone()))
        .collect();
    let mut addresses: Vec<_> = raw
        .addresses
        .iter()
        .filter(|a| a.index == adapter.index)
        .map(|a| Address {
            address: a.address,
            prefix: a.prefix,
        })
        .collect();
    let mut other_subnets: Vec<_> = raw
        .addresses
        .iter()
        .filter(|a| a.index != adapter.index)
        .map(|a| Address {
            address: a.address,
            prefix: a.prefix,
        })
        .collect();
    let map_route = |r: &NativeRoute| Route {
        destination: r.destination,
        prefix: r.prefix,
        gateway: (!r.gateway.is_unspecified()).then_some(r.gateway),
        interface_id: ids
            .get(&r.index)
            .cloned()
            .unwrap_or_else(|| format!("windows-ifindex-{}", r.index)),
        metric: r.metric,
        table: 254,
    };
    let mut routes: Vec<_> = raw
        .routes
        .iter()
        .filter(|r| r.prefix != 0)
        .map(map_route)
        .collect();
    let mut default_routes: Vec<_> = raw
        .routes
        .iter()
        .filter(|r| r.prefix == 0)
        .map(map_route)
        .collect();
    let selected_dns: Vec<_> = raw
        .dns
        .iter()
        .filter(|row| row.index == adapter.index)
        .collect();
    if selected_dns.len() != 1 {
        return Err("Selected adapter DNS state is unknown".into());
    }
    let mut dns = selected_dns[0].addresses.clone();
    let mut owned = Vec::new();
    for plan in known {
        if plan.before.adapter_id != adapter.guid {
            continue;
        }
        if let Operation::HostRoute { target, .. } = plan.candidate.operation {
            if routes.iter().any(|r| {
                r.destination == target
                    && r.prefix == 32
                    && r.gateway.is_none()
                    && r.interface_id == adapter.guid
                    && r.metric == platform::metric(plan)
            }) {
                owned.push(platform::object(plan));
            }
        }
    }
    for rule in raw
        .rules
        .iter()
        .filter(|rule| rule.name.starts_with("Q22-Helper-"))
    {
        if let Some(plan) = known.iter().find(|plan| exact_rule(rule, plan)) {
            owned.push(platform::object(plan));
        } else {
            owned.push(OwnedObject {
                owner_id: "foreign_or_unverified".into(),
                operation: candidate.operation.clone(),
                adapter_id: adapter.guid.clone(),
                native_identity: rule.name.clone(),
            });
        }
    }
    let sort = |value: &mut Vec<Route>| {
        value.sort_by_key(|v| serde_json::to_string(v).unwrap_or_default())
    };
    sort(&mut routes);
    sort(&mut default_routes);
    addresses.sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    other_subnets.sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    dns.sort();
    owned.sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    // Context contains native properties that must not disappear into the
    // revision field (the broker permits only that field to advance on a write).
    let mut unrelated_rules: Vec<_> = raw
        .rules
        .iter()
        .filter(|rule| !known.iter().any(|plan| exact_rule(rule, plan)))
        .collect();
    unrelated_rules.sort_by_key(|rule| rule.name.clone());
    // Keep all bulk filters independently, including disabled/unselected rules.
    // A sampled foreign join is not ownership proof. Only a positively matched
    // helper association can disappear as part of that exact expected addition.
    let verified_owned_ids: std::collections::HashSet<_> = raw
        .rules
        .iter()
        .filter(|rule| known.iter().any(|plan| exact_rule(rule, plan)))
        .map(|rule| (rule.instance_id.as_str(), rule.name.as_str()))
        .collect();
    let mut foreign_filters: Vec<_> = raw.firewall_filters.iter().filter(|filter| {
        filter.association_rule.as_deref().is_none_or(|name|!verified_owned_ids.contains(&(filter.instance_id.as_str(),name)))
    }).map(|filter|serde_json::json!({"kind":filter.kind,"instance_id":filter.instance_id,"properties":filter.properties})).collect();
    foreign_filters.sort_by_key(|value| value.to_string());
    let mut profiles = raw.profiles.clone();
    profiles.sort_by_key(|p| p.name.clone());
    let context = serde_json::json!({"adapters":raw.adapters,"profiles":profiles,"unrelated_rules":unrelated_rules,"independent_foreign_filters":foreign_filters,"all_addresses":raw.addresses,"all_dns":raw.dns,
        "domain_joined":raw.domain_joined,"mdm_enrolled":raw.mdm_enrolled,"firewall_running":raw.firewall_running});
    let policy_allows_changes = !raw.domain_joined
        && !raw.mdm_enrolled
        && raw.firewall_running
        && raw
            .profiles
            .iter()
            .all(|p| p.enabled && matches!(p.allow_local_rules.as_str(), "True" | "NotConfigured"))
        && !raw.rules.iter().any(|r| r.enabled && r.action == "Block");
    Ok(HostState {
        platform: Platform::Windows,
        adapter_id: adapter.guid.clone(),
        interface: adapter.name.clone(),
        interface_index: adapter.index,
        manager: "Windows".into(),
        connection_id: format!("{}:{}", adapter.guid, fingerprint(&context)),
        revision: fingerprint(&raw),
        up: adapter.up,
        policy_allows_changes,
        dhcp: adapter.dhcp,
        addresses,
        routes,
        default_routes,
        dns,
        other_subnets,
        firewall_zone: None,
        firewall_zone_interfaces: vec![],
        firewall_zone_sources: vec![],
        owned,
    })
}

/// Entire script is compiled here. No caller text becomes PowerShell source.
pub const SNAPSHOT_LIBRARY: &str = include_str!("../scripts/windows-snapshot.ps1");
pub const SNAPSHOT_SCRIPT: &str = concat!(
    include_str!("../scripts/windows-snapshot.ps1"),
    "\nGet-Q22NativeSnapshot | ConvertTo-Json -Depth 12 -Compress"
);

#[cfg(windows)]
pub struct WindowsHost {
    enrollment: Enrollment,
    known: Vec<Plan>,
    transaction: Option<HostState>,
    pending: Option<(Plan, Direction)>,
    observer: crate::safety::NativeObserver,
}
#[cfg(windows)]
impl WindowsHost {
    pub fn new(
        enrollment: Enrollment,
        records: &[Record],
        observer: crate::safety::NativeObserver,
    ) -> Self {
        Self {
            enrollment,
            known: records
                .iter()
                .filter(|r| r.post_state.is_some() && r.state != State::RolledBack)
                .map(|r| r.plan.clone())
                .collect(),
            transaction: None,
            pending: None,
            observer,
        }
    }
    fn read(&self, candidate: &Candidate) -> Result<HostState> {
        self.enrollment.permits(candidate)?;
        let raw: Snapshot = serde_json::from_value(native::powershell(SNAPSHOT_SCRIPT)?)
            .map_err(|e| format!("Native state schema: {e}"))?;
        normalize_snapshot(raw, candidate, &self.known)
    }
}
#[cfg(windows)]
impl Host for WindowsHost {
    fn inspect(&mut self, candidate: &Candidate) -> Result<HostState> {
        self.read(candidate)
    }
    fn change(&mut self, plan: &Plan, direction: Direction) -> Result<()> {
        let safety = self.observer.observe()?;
        if safety.flight_state != FlightState::Disarmed
            || !safety.identity_verified
            || safety.recording_active
        {
            return Err("Independent native safety state blocks write".into());
        }
        let current = self.read(&plan.candidate)?;
        if (direction == Direction::Apply && fingerprint(&current) != plan.before_fingerprint)
            || (direction == Direction::Rollback && !platform::exact_addition(plan, &current))
        {
            return Err("Native compare-before-write state changed".into());
        }
        self.transaction = Some(current);
        self.pending = Some((plan.clone(), direction));
        let result = (|| {
            let before = self.read(&plan.candidate)?;
            if self.transaction.as_ref() != Some(&before) {
                return Err("Native state changed while opening transaction".into());
            }
            for command in platform::compile(plan, direction)? {
                if command.program != platform::Program::PowerShell {
                    return Err("Unsupported Windows native command".into());
                }
                let now = self.observer.observe()?;
                if now.flight_state != FlightState::Disarmed
                    || !now.identity_verified
                    || now.recording_active
                    || now.generation != safety.generation
                {
                    return Err("Native safety state changed before write".into());
                }
                native::run_generated(&command)?;
            }
            // Do not adopt an identical foreign target after a failed write.
            if direction == Direction::Apply {
                if !self.known.iter().any(|p| p.plan_id == plan.plan_id) {
                    self.known.push(plan.clone());
                }
            } else {
                self.known.retain(|p| p.plan_id != plan.plan_id);
            }
            Ok(())
        })();
        self.transaction = None;
        self.pending = None;
        result
    }
    fn verify_paths(&mut self, plan: &Plan) -> Result<bool> {
        self.enrollment.permits(&plan.candidate)?;
        let target = match plan.candidate.operation {
            Operation::HostRoute { target, .. } => target,
            Operation::FirewallUdp { remote_address, .. } => remote_address,
            _ => return Err("Windows aliases unsupported".into()),
        };
        let device = self
            .enrollment
            .devices
            .iter()
            .find(|d| d.host == target)
            .ok_or("Unenrolled target")?;
        let connect = |ip, port| {
            TcpStream::connect_timeout(&SocketAddr::from((ip, port)), Duration::from_secs(2))
                .is_ok()
        };
        Ok(connect(device.host, device.verification_port)
            && connect(
                self.enrollment.internet_probe.host,
                self.enrollment.internet_probe.port,
            ))
    }
}
