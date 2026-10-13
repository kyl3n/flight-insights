//! OS transactions are compiled exclusively from the helper's typed plan.
//! Native read/verification providers are separate from caller RPC. In particular,
//! a JSON receipt or Python response can never act as a native state provider.
use crate::*;
use base64::{engine::general_purpose::STANDARD, Engine};

fn same_subnet(a: Ipv4Addr, b: &Address) -> bool {
    if b.prefix > 32 {
        return false;
    }
    let mask = if b.prefix == 0 {
        0
    } else {
        u32::MAX << (32 - b.prefix)
    };
    u32::from(a) & mask == u32::from(b.address) & mask
}
fn canonical_uuid(value: &str) -> bool {
    Uuid::parse_str(value).is_ok_and(|id| id.hyphenated().to_string() == value.to_ascii_lowercase())
}
fn safe_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
}
pub fn preflight(candidate: &Candidate, state: &HostState) -> Result<()> {
    candidate.validate()?;
    if !state.up
        || !state.policy_allows_changes
        || state.interface != candidate.interface
        || candidate
            .interface_index
            .is_some_and(|n| n != state.interface_index)
        || state.interface_index == 0
        || state.revision.is_empty()
        || state.revision.len() > 256
        || state
            .addresses
            .iter()
            .chain(&state.other_subnets)
            .any(|a| a.prefix > 32)
        || state
            .routes
            .iter()
            .chain(&state.default_routes)
            .any(|r| r.prefix > 32 || r.interface_id.is_empty())
    {
        return Err("Complete unchanged adapter and effective-policy evidence is required".into());
    }
    match state.platform {
        Platform::Windows if state.manager == "Windows" && canonical_uuid(&state.adapter_id) => {}
        Platform::Linux
            if state.manager == "NetworkManager"
                && canonical_uuid(&state.connection_id)
                && safe_token(&state.interface)
                && !state.adapter_id.is_empty() => {}
        Platform::Macos => return Err("macOS managed writers are deferred; diagnosis only".into()),
        _ => return Err("No qualified transaction for this network manager".into()),
    }
    match candidate.operation {
        Operation::HostRoute {
            target,
            local_address,
        } => {
            if !state
                .addresses
                .iter()
                .any(|a| a.address == local_address && same_subnet(target, a))
            {
                return Err("The target must be on the selected existing local subnet".into());
            }
            if state
                .routes
                .iter()
                .any(|r| r.destination == target && r.prefix == 32)
            {
                return Err("An existing target host route will never be replaced".into());
            }
        }
        Operation::AddressAlias { address, prefix } => {
            if state.platform != Platform::Linux {
                return Err("Windows address aliases can change DHCP; unsupported".into());
            }
            if prefix != 32 {
                return Err("Only a narrow /32 additional address is supported".into());
            }
            if state
                .default_routes
                .iter()
                .any(|r| r.interface_id == state.adapter_id)
                || state.addresses.iter().any(|a| a.address == address)
                || state.other_subnets.iter().any(|a| same_subnet(address, a))
            {
                return Err(
                    "Address addition overlaps an existing subnet or Internet interface".into(),
                );
            }
        }
        Operation::FirewallUdp { local_address, .. } => {
            if !state.addresses.iter().any(|a| a.address == local_address) {
                return Err("Firewall destination must be a selected local address".into());
            }
            if state.platform == Platform::Linux {
                let zone = state
                    .firewall_zone
                    .as_ref()
                    .ok_or("Active firewalld zone is required")?;
                if !safe_token(zone)
                    || state.firewall_zone_interfaces != vec![state.interface.clone()]
                    || !state.firewall_zone_sources.is_empty()
                {
                    return Err("Only an existing dedicated firewalld zone is supported".into());
                }
            }
        }
    }
    Ok(())
}

pub fn metric(plan: &Plan) -> u32 {
    // Native receipt + exact snapshot establishes ownership, not metric alone.
    30_000 + u32::from_str_radix(&plan.plan_id[..4], 16).unwrap_or(0) % 20_000
}
pub fn object(plan: &Plan) -> OwnedObject {
    OwnedObject {
        owner_id: plan.plan_id.clone(),
        operation: plan.candidate.operation.clone(),
        adapter_id: plan.before.adapter_id.clone(),
        native_identity: match plan.candidate.operation {
            Operation::FirewallUdp { .. } if plan.before.platform == Platform::Windows => {
                format!("Q22-Helper-{}", plan.plan_id)
            }
            Operation::FirewallUdp { .. } => linux_rule(plan),
            Operation::HostRoute { .. } => format!("main-host-route-metric-{}", metric(plan)),
            Operation::AddressAlias { address, prefix } => {
                format!("nm:{}:{address}/{prefix}", plan.before.connection_id)
            }
        },
    }
}
pub fn has_addition(plan: &Plan, state: &HostState) -> bool {
    state.owned.iter().any(|entry| entry == &object(plan))
}
pub fn target_present(plan: &Plan, state: &HostState) -> bool {
    has_addition(plan, state)
        || match plan.candidate.operation {
            Operation::HostRoute { target, .. } => state
                .routes
                .iter()
                .any(|r| r.destination == target && r.prefix == 32),
            Operation::AddressAlias { address, .. } => {
                state.addresses.iter().any(|a| a.address == address)
            }
            Operation::FirewallUdp { .. } => state
                .owned
                .iter()
                .any(|o| o.native_identity == object(plan).native_identity),
        }
}
pub fn expected_after(plan: &Plan) -> HostState {
    let mut after = plan.before.clone();
    match plan.candidate.operation {
        Operation::HostRoute { target, .. } => after.routes.push(Route {
            destination: target,
            prefix: 32,
            gateway: None,
            interface_id: after.adapter_id.clone(),
            metric: metric(plan),
            table: 254,
        }),
        Operation::AddressAlias { address, prefix } => {
            after.addresses.push(Address { address, prefix })
        }
        Operation::FirewallUdp { .. } => {}
    }
    after.owned.push(object(plan));
    after
        .addresses
        .sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    after
        .routes
        .sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    after
        .owned
        .sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
    after
}
pub fn exact_addition(plan: &Plan, observed: &HostState) -> bool {
    let mut expected = expected_after(plan);
    // Native profile revision is expected to advance; all actual settings,
    // default routes, DNS, policy, adapter identity and unrelated objects match.
    expected.revision.clone_from(&observed.revision);
    &expected == observed
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Program {
    PowerShell,
    NetworkManager,
    Firewalld,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Command {
    pub program: Program,
    pub args: Vec<String>,
}
fn ps(script: String) -> Command {
    Command {
        program: Program::PowerShell,
        args: vec![
            "-NoProfile".into(),
            "-NonInteractive".into(),
            "-Command".into(),
            format!(
                "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; {script}"
            ),
        ],
    }
}
fn utf8_literal(value: &str) -> String {
    format!(
        "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{}'))",
        STANDARD.encode(value.as_bytes())
    )
}
fn linux_rule(plan: &Plan) -> String {
    if let Operation::FirewallUdp {
        remote_address,
        local_address,
        port,
    } = plan.candidate.operation
    {
        format!("rule priority=\"{}\" family=\"ipv4\" source address=\"{remote_address}/32\" destination address=\"{local_address}/32\" port port=\"{port}\" protocol=\"udp\" accept",
            1 + u32::from_str_radix(&plan.plan_id[..4], 16).unwrap_or(0) % 30_000)
    } else {
        String::new()
    }
}
/// Reviewable native steps. Never deserialized from RPC or a journal.
pub fn compile(plan: &Plan, direction: Direction) -> Result<Vec<Command>> {
    if !valid_id(&plan.plan_id) || fingerprint(&plan.before) != plan.before_fingerprint {
        return Err("Invalid native plan".into());
    }
    preflight(&plan.candidate, &plan.before)?;
    let add = direction == Direction::Apply;
    let operation = &plan.candidate.operation;
    let state = &plan.before;
    match state.platform {
        Platform::Windows => match operation {
            Operation::HostRoute { target, .. } => {
                let suffix = format!("-DestinationPrefix '{target}/32' -InterfaceIndex {} -NextHop '0.0.0.0' -RouteMetric {} -PolicyStore ActiveStore", state.interface_index, metric(plan));
                Ok(vec![ps(if add {
                    format!("New-NetRoute {suffix} | Out-Null")
                } else {
                    format!("Get-NetRoute {suffix} | Remove-NetRoute -Confirm:$false")
                })])
            }
            Operation::FirewallUdp {
                remote_address,
                local_address,
                port,
            } => {
                let name = format!("Q22-Helper-{}", plan.plan_id);
                let script = if add {
                    format!("$alias={}; New-NetFirewallRule -Name '{name}' -DisplayName '{name}' -Direction Inbound -Action Allow -Protocol UDP -LocalAddress '{local_address}' -RemoteAddress '{remote_address}' -LocalPort {port} -InterfaceAlias $alias -Profile Any -PolicyStore PersistentStore | Out-Null", utf8_literal(&state.interface))
                } else {
                    format!("Remove-NetFirewallRule -Name '{name}' -PolicyStore PersistentStore")
                };
                Ok(vec![ps(script)])
            }
            _ => Err("Windows aliases are unsupported".into()),
        },
        Platform::Linux => match operation {
            Operation::HostRoute { target, .. }
            | Operation::AddressAlias {
                address: target, ..
            } => {
                let (property, value) = match operation {
                    Operation::HostRoute { .. } => (
                        "ipv4.routes",
                        format!("{target}/32 0.0.0.0 {}", metric(plan)),
                    ),
                    Operation::AddressAlias { prefix, .. } => {
                        ("ipv4.addresses", format!("{target}/{prefix}"))
                    }
                    _ => unreachable!(),
                };
                // Modify only one tuple on the active UUID. Native provider must
                // prove profile/effective equality and checkpoint/reapply support.
                Ok(vec![
                    Command {
                        program: Program::NetworkManager,
                        args: vec![
                            "--wait".into(),
                            "8".into(),
                            "connection".into(),
                            "modify".into(),
                            "uuid".into(),
                            state.connection_id.clone(),
                            format!("{}{property}", if add { "+" } else { "-" }),
                            value,
                        ],
                    },
                    Command {
                        program: Program::NetworkManager,
                        args: vec![
                            "--wait".into(),
                            "8".into(),
                            "device".into(),
                            "reapply".into(),
                            state.interface.clone(),
                        ],
                    },
                ])
            }
            Operation::FirewallUdp { .. } => Ok(vec![Command {
                program: Program::Firewalld,
                args: vec![
                    format!(
                        "--zone={}",
                        state.firewall_zone.as_deref().ok_or("No zone")?
                    ),
                    format!(
                        "--{}-rich-rule={}",
                        if add { "add" } else { "remove" },
                        linux_rule(plan)
                    ),
                ],
            }]),
        },
        Platform::Macos => Err("macOS managed writers are deferred".into()),
    }
}

/// Service-host native boundaries. Implementations must query the actual OS,
/// detect active policy/manager ownership and verify profile==effective state.
/// The transaction is *not* considered qualified just because commands compile.
pub trait NativeProvider {
    fn inspect_native(&mut self, candidate: &Candidate) -> Result<HostState>;
    fn verify_native_paths(&mut self, plan: &Plan) -> Result<bool>;
    /// Opens the manager checkpoint/CAS transaction and rejects changed state.
    /// For NetworkManager this includes profile and applied connection version;
    /// raw `ip` operations and reapply without this guard are forbidden.
    fn begin_transaction(&mut self, expected: &HostState, plan_id: &str) -> Result<()>;
    fn execute_native(&mut self, command: &Command) -> Result<()>;
    /// End releases the transaction even on partial failure; never restores an
    /// entire interface/firewall snapshot over independently changed settings.
    fn end_transaction(&mut self) -> Result<()>;
}
pub struct ManagedHost<P> {
    pub provider: P,
}
impl<P: NativeProvider> Host for ManagedHost<P> {
    fn inspect(&mut self, candidate: &Candidate) -> Result<HostState> {
        self.provider.inspect_native(candidate)
    }
    fn verify_paths(&mut self, plan: &Plan) -> Result<bool> {
        self.provider.verify_native_paths(plan)
    }
    fn change(&mut self, plan: &Plan, direction: Direction) -> Result<()> {
        let current = self.provider.inspect_native(&plan.candidate)?;
        if direction == Direction::Apply {
            if fingerprint(&current) != plan.before_fingerprint {
                return Err("Native pre-write state changed".into());
            }
        } else if !exact_addition(plan, &current) {
            return Err("Native rollback object changed".into());
        }
        let commands = compile(plan, direction)?;
        self.provider.begin_transaction(&current, &plan.plan_id)?;
        let result = commands
            .iter()
            .try_for_each(|command| self.provider.execute_native(command));
        let end = self.provider.end_transaction();
        result.and(end)
    }
}
