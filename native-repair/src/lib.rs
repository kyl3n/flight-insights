//! Privilege boundary for narrowly scoped Q22 host repairs.
//!
//! The shipped binary cannot enroll itself, elevate, or enable writes. This
//! library implements the broker, durable intent log and platform transactions
//! for a future authenticated service host. Caller identity, flight observations,
//! native host state, journal location and qualification NEVER come from RPC.
pub mod enrollment;
pub mod journal;
#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(target_os = "linux")]
pub mod linux_service;
pub mod platform;
pub mod safety;
pub mod windows;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::net::Ipv4Addr;
use uuid::Uuid;

pub type Result<T> = std::result::Result<T, String>;
pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_RECORDS: usize = 64;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Platform {
    Windows,
    Linux,
    Macos,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    HostRoute {
        target: Ipv4Addr,
        local_address: Ipv4Addr,
    },
    AddressAlias {
        address: Ipv4Addr,
        prefix: u8,
    },
    FirewallUdp {
        remote_address: Ipv4Addr,
        local_address: Ipv4Addr,
        port: u16,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candidate {
    pub interface: String,
    pub interface_index: Option<u32>,
    pub operation: Operation,
}

fn unicast(ip: Ipv4Addr) -> bool {
    !ip.is_unspecified() && !ip.is_multicast() && !ip.is_broadcast() && !ip.is_loopback()
}
impl Candidate {
    pub fn validate(&self) -> Result<()> {
        if self.interface.is_empty()
            || self.interface.len() > 256
            || self.interface.chars().any(char::is_control)
            || self.interface_index == Some(0)
        {
            return Err("Invalid adapter selector".into());
        }
        let valid = match self.operation {
            Operation::HostRoute {
                target,
                local_address,
            } => unicast(target) && unicast(local_address) && target != local_address,
            Operation::AddressAlias { address, prefix } => {
                unicast(address) && (1..=32).contains(&prefix)
            }
            Operation::FirewallUdp {
                remote_address,
                local_address,
                port,
            } => unicast(remote_address) && unicast(local_address) && port > 0,
        };
        if !valid {
            return Err("Only individual unicast IPv4 endpoints are allowed".into());
        }
        Ok(())
    }
}

/// The native reader supplies this complete view. Unknown policy/manager state
/// is an error, never an empty successful snapshot.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostState {
    pub platform: Platform,
    pub adapter_id: String,
    pub interface: String,
    pub interface_index: u32,
    pub manager: String,
    pub connection_id: String,
    pub revision: String,
    pub up: bool,
    pub policy_allows_changes: bool,
    pub dhcp: bool,
    pub addresses: Vec<Address>,
    pub routes: Vec<Route>,
    pub default_routes: Vec<Route>,
    pub dns: Vec<Ipv4Addr>,
    pub other_subnets: Vec<Address>,
    pub firewall_zone: Option<String>,
    pub firewall_zone_interfaces: Vec<String>,
    pub firewall_zone_sources: Vec<String>,
    pub owned: Vec<OwnedObject>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Address {
    pub address: Ipv4Addr,
    pub prefix: u8,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub destination: Ipv4Addr,
    pub prefix: u8,
    pub gateway: Option<Ipv4Addr>,
    pub interface_id: String,
    pub metric: u32,
    pub table: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OwnedObject {
    pub owner_id: String,
    pub operation: Operation,
    pub adapter_id: String,
    pub native_identity: String,
}

pub fn fingerprint<T: Serialize>(value: &T) -> String {
    let bytes = serde_json::to_vec(value).expect("serialize typed broker value");
    format!("{:x}", Sha256::digest(bytes))
}
pub fn valid_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FlightState {
    Disarmed,
    Armed,
    Unknown,
}
#[derive(Clone, Debug)]
pub struct Safety {
    pub flight_state: FlightState,
    pub identity_verified: bool,
    pub recording_active: bool,
    pub observed_monotonic_ms: u64,
    pub generation: u64,
}
pub trait SafetyObserver {
    fn observe(&mut self) -> Result<Safety>;
}
pub trait Clock {
    fn utc_ms(&self) -> u64;
    fn monotonic_ms(&self) -> u64;
}
/// Authenticated identity provided by the service transport, not serialized RPC.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Peer {
    pub principal: String,
}
pub trait Authorizer {
    fn authorize(&self, peer: &Peer) -> Result<()>;
}
pub trait Host {
    fn inspect(&mut self, candidate: &Candidate) -> Result<HostState>;
    /// Must use a native compare-and-change/checkpoint transaction; never execute
    /// caller argv. A successful return alone is not evidence of application.
    fn change(&mut self, plan: &Plan, direction: Direction) -> Result<()>;
    /// Independent, configured target AND Internet continuity verification.
    fn verify_paths(&mut self, plan: &Plan) -> Result<bool>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Apply,
    Rollback,
}
pub trait Journal {
    fn load(&mut self) -> Result<Vec<Record>>;
    /// Atomic durable replacement. Failure MUST stop the next native write.
    fn save(&mut self, record: &Record) -> Result<()>;
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub plan_id: String,
    pub principal: String,
    pub candidate: Candidate,
    pub before: HostState,
    pub before_fingerprint: String,
    pub created_utc_ms: u64,
    pub expires_utc_ms: u64,
    pub rollback_after_ms: u64,
    pub review_fingerprint: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Prepared,
    ApplyIntent,
    AppliedTemporary,
    ApplyUncertain,
    RollbackIntent,
    RolledBack,
    RecoveryBlocked,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub utc_ms: u64,
    pub state: State,
    pub detail: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub version: u32,
    pub plan: Plan,
    pub state: State,
    pub rollback_deadline_utc_ms: Option<u64>,
    pub post_state: Option<HostState>,
    pub events: Vec<Event>,
}
#[derive(Clone, Debug, Serialize)]
pub struct Recovery {
    pub record: Record,
    pub ownership: String,
}
impl Record {
    pub fn validate(&self) -> Result<()> {
        if self.version != PROTOCOL_VERSION
            || !valid_id(&self.plan.plan_id)
            || self.events.len() > 256
            || self.plan.principal.is_empty()
            || self.plan.principal.len() > 256
        {
            return Err("Invalid helper journal record".into());
        }
        self.plan.candidate.validate()?;
        platform::preflight(&self.plan.candidate, &self.plan.before)?;
        if fingerprint(&self.plan.before) != self.plan.before_fingerprint
            || review_fingerprint(&self.plan) != self.plan.review_fingerprint
            || self.plan.expires_utc_ms <= self.plan.created_utc_ms
            || self.plan.expires_utc_ms - self.plan.created_utc_ms > 600_000
            || !(10_000..=300_000).contains(&self.plan.rollback_after_ms)
        {
            return Err("Invalid helper journal plan fingerprint or lifetime".into());
        }
        if self.state != State::Prepared && self.rollback_deadline_utc_ms.is_none() {
            return Err("Mutation receipt lacks supervisor deadline".into());
        }
        Ok(())
    }
}
fn review_fingerprint(plan: &Plan) -> String {
    let mut reviewed = plan.clone();
    reviewed.review_fingerprint.clear();
    fingerprint(&reviewed)
}

#[derive(Debug, Deserialize)]
#[serde(
    tag = "method",
    content = "params",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum Request {
    Capabilities(Empty),
    Prepare(Prepare),
    Apply(Approval),
    Rollback(Approval),
    Recovery(Empty),
    RecordingLease(RecordingLease),
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Empty {}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingLease {
    pub active: bool,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prepare {
    pub candidate: Candidate,
    pub expires_in_s: u16,
    pub rollback_after_s: u16,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Approval {
    pub plan_id: String,
    pub review_fingerprint: String,
    pub confirm: bool,
}

pub fn production_capabilities() -> serde_json::Value {
    serde_json::json!({"protocol_version": PROTOCOL_VERSION, "state": "diagnosis_only",
        "platform": std::env::consts::OS, "qualified": false, "signed_helper": false,
        "manager_aware": false, "supervised_rollback": false, "automatic_apply": false,
        "library_features": ["typed_plans", "durable_intent", "exact_ownership", "safety_revalidation", "supervisor_tick", "windows_transactions", "networkmanager_transactions"],
        "blockers": ["Authenticated native service installation and caller policy are not enrolled", "Independent trusted safety observer transport is not installed", "Native writer, privilege-denial and crash qualification is incomplete"],
        "macos": "deferred"})
}

/// Core owns the durable records. A service host must call `supervise()` on an
/// independent timer and immediately after restart, without a live UI session.
/// The unqualified shipped executable deliberately does not instantiate this.
pub struct Broker<H, J, S, C, A> {
    pub host: H,
    pub journal: J,
    pub safety: S,
    pub clock: C,
    pub authorizer: A,
    records: Vec<Record>,
    // Monotonic lifetime prevents a wall-clock rollback from extending a preview.
    prepared_monotonic: std::collections::HashMap<String, u64>,
    mutation_monotonic: std::collections::HashMap<String, u64>,
}
impl<H: Host, J: Journal, S: SafetyObserver, C: Clock, A: Authorizer> Broker<H, J, S, C, A> {
    pub fn open(host: H, mut journal: J, safety: S, clock: C, authorizer: A) -> Result<Self> {
        let records = journal.load()?;
        if records.len() > MAX_RECORDS {
            return Err("Helper journal capacity exceeded".into());
        }
        for record in &records {
            record.validate()?;
        }
        let unique: std::collections::HashSet<_> =
            records.iter().map(|r| &r.plan.plan_id).collect();
        if unique.len() != records.len() {
            return Err("Duplicate helper plan identity".into());
        }
        Ok(Self {
            host,
            journal,
            safety,
            clock,
            authorizer,
            records,
            prepared_monotonic: Default::default(),
            mutation_monotonic: Default::default(),
        })
    }
    fn safe(&mut self) -> Result<Safety> {
        let evidence = self.safety.observe()?;
        let now = self.clock.monotonic_ms();
        if evidence.flight_state != FlightState::Disarmed
            || !evidence.identity_verified
            || evidence.recording_active
            || evidence.observed_monotonic_ms > now
            || now - evidence.observed_monotonic_ms > 3000
        {
            return Err(
                "Fresh, verified disarmed evidence and inactive recording are required".into(),
            );
        }
        Ok(evidence)
    }
    fn event(&mut self, index: usize, state: State, detail: &str) -> Result<()> {
        let mut next = self.records[index].clone();
        next.state = state.clone();
        if next.events.len() >= 256 {
            return Err("Journal event limit reached; manual review required".into());
        }
        next.events.push(Event {
            utc_ms: self.clock.utc_ms(),
            state,
            detail: detail.chars().take(512).collect(),
        });
        self.journal.save(&next)?;
        self.records[index] = next;
        Ok(())
    }
    pub fn prepare(&mut self, peer: &Peer, request: Prepare) -> Result<Plan> {
        self.authorizer.authorize(peer)?;
        request.candidate.validate()?;
        if !(1..=600).contains(&request.expires_in_s)
            || !(10..=300).contains(&request.rollback_after_s)
        {
            return Err(
                "Preview expiry is 1..600 seconds and temporary rollback is 10..300 seconds".into(),
            );
        }
        if self.records.len() >= MAX_RECORDS {
            return Err("Helper journal capacity reached".into());
        }
        self.safe()?;
        let before = self.host.inspect(&request.candidate)?;
        platform::preflight(&request.candidate, &before)?;
        let now = self.clock.utc_ms();
        let mut plan = Plan {
            plan_id: Uuid::new_v4().simple().to_string(),
            principal: peer.principal.clone(),
            candidate: request.candidate,
            before_fingerprint: fingerprint(&before),
            before,
            created_utc_ms: now,
            expires_utc_ms: now + u64::from(request.expires_in_s) * 1000,
            rollback_after_ms: u64::from(request.rollback_after_s) * 1000,
            review_fingerprint: String::new(),
        };
        plan.review_fingerprint = review_fingerprint(&plan);
        let record = Record {
            version: PROTOCOL_VERSION,
            plan: plan.clone(),
            state: State::Prepared,
            rollback_deadline_utc_ms: None,
            post_state: None,
            events: vec![Event {
                utc_ms: now,
                state: State::Prepared,
                detail: "Helper-generated temporary plan; no write".into(),
            }],
        };
        self.journal.save(&record)?;
        self.prepared_monotonic
            .insert(plan.plan_id.clone(), self.clock.monotonic_ms());
        self.records.push(record);
        Ok(plan)
    }
    fn approved(&self, peer: &Peer, approval: &Approval) -> Result<usize> {
        self.authorizer.authorize(peer)?;
        if !approval.confirm || !valid_id(&approval.plan_id) {
            return Err("Explicit approval of a helper plan is required".into());
        }
        let index = self
            .records
            .iter()
            .position(|r| r.plan.plan_id == approval.plan_id)
            .ok_or("Unknown helper plan")?;
        let plan = &self.records[index].plan;
        if plan.principal != peer.principal
            || plan.review_fingerprint != approval.review_fingerprint
        {
            return Err("Caller or reviewed fingerprint does not match helper plan".into());
        }
        Ok(index)
    }
    pub fn apply(&mut self, peer: &Peer, approval: Approval) -> Result<Record> {
        let index = self.approved(peer, &approval)?;
        let record = self.records[index].clone();
        if record.state == State::AppliedTemporary {
            let observed = self.host.inspect(&record.plan.candidate)?;
            if record.post_state.as_ref() != Some(&observed)
                || !platform::exact_addition(&record.plan, &observed)
            {
                return Err("Previously applied native state changed; inspect recovery".into());
            }
            return Ok(record);
        }
        if record.state != State::Prepared {
            return Err("A recorded mutation is never replayed".into());
        }
        let plan = &record.plan;
        let prepared = self
            .prepared_monotonic
            .get(&plan.plan_id)
            .ok_or("Restarted preview must be recreated")?;
        if self.clock.utc_ms() < plan.created_utc_ms
            || self.clock.utc_ms() >= plan.expires_utc_ms
            || self.clock.monotonic_ms().saturating_sub(*prepared)
                >= plan.expires_utc_ms - plan.created_utc_ms
        {
            return Err("Helper plan expired".into());
        }
        let safety = self.safe()?;
        let current = self.host.inspect(&plan.candidate)?;
        if fingerprint(&current) != plan.before_fingerprint {
            return Err("Host state changed since native preview".into());
        }
        platform::preflight(&plan.candidate, &current)?;
        self.records[index].rollback_deadline_utc_ms =
            Some(self.clock.utc_ms() + plan.rollback_after_ms);
        self.mutation_monotonic
            .insert(plan.plan_id.clone(), self.clock.monotonic_ms());
        self.event(
            index,
            State::ApplyIntent,
            "Durable intent before native mutation",
        )?;
        let latest = self.safe()?;
        if latest.generation != safety.generation {
            return Err("Safety generation changed during preflight".into());
        }
        let write = self.host.change(plan, Direction::Apply);
        let observed = self.host.inspect(&plan.candidate);
        if let Ok(ref state) = observed {
            if write.is_ok() && platform::exact_addition(plan, state) {
                self.records[index].post_state = Some(state.clone());
            }
        }
        let still_safe = self
            .safe()
            .map(|now| now.generation == safety.generation)
            .unwrap_or(false);
        let paths = if still_safe && write.is_ok() && self.records[index].post_state.is_some() {
            self.host.verify_paths(plan).unwrap_or(false)
        } else {
            false
        };
        let after_verification = self
            .safe()
            .map(|now| now.generation == safety.generation)
            .unwrap_or(false);
        if write.is_ok() && paths && after_verification && self.records[index].post_state.is_some()
        {
            self.event(index, State::AppliedTemporary, "Exact addition and configured target/Internet paths verified; timed rollback remains armed")?;
        } else {
            self.event(index, State::ApplyUncertain, "Native result, ownership, continuity or safety verification failed; supervised recovery required")?;
            // Never undo immediately on stale/armed evidence. The independently
            // scheduled supervisor will recheck safety and exact ownership.
        }
        Ok(self.records[index].clone())
    }
    pub fn rollback(&mut self, peer: &Peer, approval: Approval) -> Result<Record> {
        let index = self.approved(peer, &approval)?;
        self.rollback_index(index)?;
        Ok(self.records[index].clone())
    }
    fn rollback_index(&mut self, index: usize) -> Result<()> {
        let record = self.records[index].clone();
        if record.state == State::RolledBack {
            return Ok(());
        }
        if record.state == State::Prepared {
            return Err("This plan has no mutation intent".into());
        }
        let safety = self.safe()?;
        let observed = self.host.inspect(&record.plan.candidate)?;
        if !platform::has_addition(&record.plan, &observed) {
            // A foreign replacement of the narrow target is a conflict, not
            // evidence that rollback succeeded.
            if platform::target_present(&record.plan, &observed) {
                return Err("Repair target was replaced; manual review required".into());
            }
            self.event(
                index,
                State::RolledBack,
                "Owned target absent; no command was executed",
            )?;
            return Ok(());
        }
        let post = record
            .post_state
            .as_ref()
            .ok_or("No verified post-state; uncertain creation cannot be adopted")?;
        if &observed != post || !platform::exact_addition(&record.plan, &observed) {
            return Err("Exact owned state changed; automatic rollback refused".into());
        }
        self.event(
            index,
            State::RollbackIntent,
            "Durable intent before exact-owned rollback",
        )?;
        if self.safe()?.generation != safety.generation {
            return Err("Safety generation changed before rollback".into());
        }
        let write = self.host.change(&record.plan, Direction::Rollback);
        let after = self.host.inspect(&record.plan.candidate);
        let absent = after
            .as_ref()
            .map(|s| !platform::target_present(&record.plan, s))
            .unwrap_or(false);
        if write.is_ok() && absent {
            self.event(
                index,
                State::RolledBack,
                "Exact owned addition absent after rollback",
            )?;
        } else {
            self.event(
                index,
                State::RecoveryBlocked,
                "Rollback result uncertain or target remains present",
            )?;
        }
        Ok(())
    }
    /// Call once on service restart and at least once per second thereafter.
    /// It never replays apply. Unknown/armed state leaves an unresolved receipt.
    pub fn supervise(&mut self) -> Result<Vec<Record>> {
        for index in 0..self.records.len() {
            let r = &self.records[index];
            let due = !matches!(r.state, State::Prepared | State::RolledBack)
                && (r.state != State::AppliedTemporary
                    || r.rollback_deadline_utc_ms
                        .is_some_and(|d| self.clock.utc_ms() >= d)
                    || self.clock.utc_ms() < r.plan.created_utc_ms
                    || self
                        .mutation_monotonic
                        .get(&r.plan.plan_id)
                        .is_none_or(|start| {
                            self.clock.monotonic_ms().saturating_sub(*start)
                                >= r.plan.rollback_after_ms
                        }));
            if due {
                if let Err(error) = self.rollback_index(index) {
                    // Avoid unbounded event growth while the aircraft stays armed.
                    if self.records[index]
                        .events
                        .last()
                        .is_none_or(|e| e.detail != error)
                    {
                        self.event(index, State::RecoveryBlocked, &error)?;
                    }
                }
            }
        }
        Ok(self.records.clone())
    }
    pub fn recovery(&self, peer: &Peer) -> Result<Vec<Record>> {
        self.authorizer.authorize(peer)?;
        Ok(self
            .records
            .iter()
            .filter(|r| r.plan.principal == peer.principal)
            .cloned()
            .collect())
    }
    pub fn recovery_checked(&mut self, peer: &Peer) -> Result<Vec<Recovery>> {
        self.authorizer.authorize(peer)?;
        let start = self.clock.monotonic_ms();
        let mut output = Vec::new();
        for record in self.records.clone().into_iter().filter(|r| {
            r.plan.principal == peer.principal
                && !matches!(r.state, State::Prepared | State::RolledBack)
        }) {
            let ownership = if output.len() >= 16
                || self.clock.monotonic_ms().saturating_sub(start) >= 20_000
            {
                "unknown"
            } else {
                match self.host.inspect(&record.plan.candidate) {
                    Ok(ref current) if !platform::target_present(&record.plan, current) => "absent",
                    Ok(ref current)
                        if record.post_state.as_ref() == Some(current)
                            && platform::exact_addition(&record.plan, current) =>
                    {
                        "owned"
                    }
                    Ok(_) => "changed",
                    Err(_) => "unknown",
                }
            };
            output.push(Recovery {
                record,
                ownership: ownership.into(),
            });
        }
        Ok(output)
    }
}
