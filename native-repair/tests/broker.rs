use q22_network_helper::*;
use std::{cell::RefCell, collections::VecDeque, rc::Rc};

#[derive(Clone)]
struct FakeClock(Rc<RefCell<(u64, u64)>>);
impl Clock for FakeClock {
    fn utc_ms(&self) -> u64 {
        self.0.borrow().0
    }
    fn monotonic_ms(&self) -> u64 {
        self.0.borrow().1
    }
}
#[derive(Clone)]
struct Observer {
    clock: FakeClock,
    values: Rc<RefCell<VecDeque<Safety>>>,
}
impl SafetyObserver for Observer {
    fn observe(&mut self) -> Result<Safety> {
        Ok(self.values.borrow_mut().pop_front().unwrap_or(Safety {
            flight_state: FlightState::Disarmed,
            identity_verified: true,
            recording_active: false,
            observed_monotonic_ms: self.clock.monotonic_ms(),
            generation: 1,
        }))
    }
}
struct Auth;
impl Authorizer for Auth {
    fn authorize(&self, p: &Peer) -> Result<()> {
        if p.principal == "signed-client" {
            Ok(())
        } else {
            Err("Unauthenticated peer".into())
        }
    }
}
#[derive(Clone, Default)]
struct Memory {
    records: Rc<RefCell<Vec<Record>>>,
    fail: Rc<RefCell<bool>>,
}
impl Journal for Memory {
    fn load(&mut self) -> Result<Vec<Record>> {
        Ok(self.records.borrow().clone())
    }
    fn save(&mut self, r: &Record) -> Result<()> {
        if *self.fail.borrow() {
            return Err("disk failed".into());
        }
        r.validate()?;
        let mut all = self.records.borrow_mut();
        if let Some(old) = all.iter_mut().find(|v| v.plan.plan_id == r.plan.plan_id) {
            *old = r.clone()
        } else {
            all.push(r.clone())
        }
        Ok(())
    }
}
#[derive(Clone)]
struct FakeHost {
    state: HostState,
    writes: usize,
    fail_write: bool,
    paths: bool,
    mutate_on_inspect: bool,
}
impl Host for FakeHost {
    fn inspect(&mut self, _: &Candidate) -> Result<HostState> {
        if self.mutate_on_inspect {
            self.state.revision.push('x');
        }
        Ok(self.state.clone())
    }
    fn change(&mut self, p: &Plan, d: Direction) -> Result<()> {
        self.writes += 1;
        self.state = if d == Direction::Apply {
            platform::expected_after(p)
        } else {
            p.before.clone()
        };
        if self.fail_write {
            Err("write timed out after side effect".into())
        } else {
            Ok(())
        }
    }
    fn verify_paths(&mut self, _: &Plan) -> Result<bool> {
        Ok(self.paths)
    }
}
fn ip(s: &str) -> std::net::Ipv4Addr {
    s.parse().unwrap()
}
fn host(platform: Platform) -> HostState {
    HostState {
        platform: platform.clone(),
        adapter_id: "11111111-2222-3333-4444-555555555555".into(),
        interface: "device0".into(),
        interface_index: 8,
        manager: if platform == Platform::Windows {
            "Windows"
        } else {
            "NetworkManager"
        }
        .into(),
        connection_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee".into(),
        revision: "complete-native-state-v1".into(),
        up: true,
        policy_allows_changes: true,
        dhcp: false,
        addresses: vec![Address {
            address: ip("10.0.0.5"),
            prefix: 24,
        }],
        routes: vec![],
        default_routes: vec![Route {
            destination: ip("0.0.0.0"),
            prefix: 0,
            gateway: Some(ip("192.168.0.1")),
            interface_id: "internet-adapter".into(),
            metric: 100,
            table: 254,
        }],
        dns: vec![ip("192.168.0.1")],
        other_subnets: vec![Address {
            address: ip("192.168.0.3"),
            prefix: 24,
        }],
        firewall_zone: Some("q22".into()),
        firewall_zone_interfaces: vec!["device0".into()],
        firewall_zone_sources: vec![],
        owned: vec![],
    }
}
fn candidate() -> Candidate {
    Candidate {
        interface: "device0".into(),
        interface_index: Some(8),
        operation: Operation::HostRoute {
            target: ip("10.0.0.2"),
            local_address: ip("10.0.0.5"),
        },
    }
}
type TestBroker = Broker<FakeHost, Memory, Observer, FakeClock, Auth>;
fn broker() -> TestBroker {
    let clock = FakeClock(Rc::new(RefCell::new((1_000_000, 500_000))));
    Broker::open(
        FakeHost {
            state: host(Platform::Linux),
            writes: 0,
            fail_write: false,
            paths: true,
            mutate_on_inspect: false,
        },
        Memory::default(),
        Observer {
            clock: clock.clone(),
            values: Default::default(),
        },
        clock,
        Auth,
    )
    .unwrap()
}
fn peer() -> Peer {
    Peer {
        principal: "signed-client".into(),
    }
}
fn prepare(b: &mut TestBroker) -> Plan {
    b.prepare(
        &peer(),
        Prepare {
            candidate: candidate(),
            expires_in_s: 60,
            rollback_after_s: 30,
        },
    )
    .unwrap()
}
fn approval(p: &Plan) -> Approval {
    Approval {
        plan_id: p.plan_id.clone(),
        review_fingerprint: p.review_fingerprint.clone(),
        confirm: true,
    }
}
fn unsafe_observation(b: &TestBroker, state: FlightState, age: u64) -> Safety {
    Safety {
        flight_state: state,
        identity_verified: true,
        recording_active: false,
        observed_monotonic_ms: b.clock.monotonic_ms() - age,
        generation: 1,
    }
}

#[test]
fn dual_nic_apply_undo_preserves_default_and_dns() {
    let mut b = broker();
    let p = prepare(&mut b);
    let before = p.before.clone();
    let r = b.apply(&peer(), approval(&p)).unwrap();
    assert_eq!(r.state, State::AppliedTemporary);
    assert_eq!(b.host.state.default_routes, before.default_routes);
    assert_eq!(b.host.state.dns, before.dns);
    let r = b.rollback(&peer(), approval(&p)).unwrap();
    assert_eq!(r.state, State::RolledBack);
    assert_eq!(b.host.state, before);
    assert_eq!(b.host.writes, 2);
}
#[test]
fn production_never_qualifies_through_environment_or_admin() {
    let caps = production_capabilities();
    assert_eq!(caps["automatic_apply"], false);
    assert_eq!(caps["qualified"], false);
}
#[test]
fn unauthorized_peer_and_mismatched_review_rejected() {
    let mut b = broker();
    let p = prepare(&mut b);
    assert!(b
        .apply(
            &Peer {
                principal: "renderer".into()
            },
            approval(&p)
        )
        .is_err());
    let mut a = approval(&p);
    a.review_fingerprint = "f".repeat(64);
    assert!(b.apply(&peer(), a).is_err());
    let mut a = approval(&p);
    a.confirm = false;
    assert!(b.apply(&peer(), a).is_err());
    assert_eq!(b.host.writes, 0);
}
#[test]
fn client_cannot_send_argv_safety_paths_or_qualify() {
    for raw in [
        r#"{"method":"apply","params":{"plan_id":"a","review_fingerprint":"b","confirm":true,"commands":["sh"]}}"#,
        r#"{"method":"capabilities","params":{"qualified":true}}"#,
        r#"{"method":"recovery","params":{"journal_path":"/tmp/forged"}}"#,
    ] {
        assert!(serde_json::from_str::<Request>(raw).is_err());
    }
}
#[test]
fn changed_adapter_default_route_or_dns_invalidates_native_preview() {
    for n in 0..3 {
        let mut b = broker();
        let p = prepare(&mut b);
        match n {
            0 => b.host.state.adapter_id.push('x'),
            1 => b.host.state.default_routes[0].metric += 1,
            _ => b.host.state.dns.push(ip("8.8.8.8")),
        };
        assert!(b.apply(&peer(), approval(&p)).is_err());
        assert_eq!(b.host.writes, 0);
    }
}
#[test]
fn stale_armed_unknown_and_recording_block_both_directions() {
    for state in [
        FlightState::Armed,
        FlightState::Unknown,
        FlightState::Disarmed,
    ] {
        let mut b = broker();
        let p = prepare(&mut b);
        let age = if state == FlightState::Disarmed {
            3001
        } else {
            0
        };
        let bad = unsafe_observation(&b, state, age);
        b.safety.values.borrow_mut().push_back(bad.clone());
        assert!(b.apply(&peer(), approval(&p)).is_err());
        assert_eq!(b.host.writes, 0);
        b.apply(&peer(), approval(&p)).unwrap();
        b.safety.values.borrow_mut().push_back(bad);
        assert!(b.rollback(&peer(), approval(&p)).is_err());
        assert_eq!(b.host.writes, 1);
    }
    let mut b = broker();
    let p = prepare(&mut b);
    let mut s = unsafe_observation(&b, FlightState::Disarmed, 0);
    s.recording_active = true;
    b.safety.values.borrow_mut().push_back(s);
    assert!(b.apply(&peer(), approval(&p)).is_err());
}
#[test]
fn generation_change_during_preflight_blocks_native_write() {
    let mut b = broker();
    let p = prepare(&mut b);
    let s = unsafe_observation(&b, FlightState::Disarmed, 0);
    let mut next = s.clone();
    next.generation = 2;
    b.safety.values.borrow_mut().extend([s, next]);
    assert!(b.apply(&peer(), approval(&p)).is_err());
    assert_eq!(b.host.writes, 0);
    assert_eq!(b.journal.records.borrow()[0].state, State::ApplyIntent);
}
#[test]
fn safety_change_after_write_does_not_blindly_undo() {
    let mut b = broker();
    let p = prepare(&mut b);
    let s = unsafe_observation(&b, FlightState::Disarmed, 0);
    let mut arm = s.clone();
    arm.flight_state = FlightState::Armed;
    b.safety
        .values
        .borrow_mut()
        .extend([s.clone(), s, arm.clone(), arm]);
    let r = b.apply(&peer(), approval(&p)).unwrap();
    assert_eq!(r.state, State::ApplyUncertain);
    assert_eq!(b.host.writes, 1);
    assert!(r.post_state.is_some());
}
#[test]
fn journal_failure_before_mutation_prevents_side_effect() {
    let mut b = broker();
    let p = prepare(&mut b);
    *b.journal.fail.borrow_mut() = true;
    assert!(b.apply(&peer(), approval(&p)).is_err());
    assert_eq!(b.host.writes, 0);
}
#[test]
fn command_timeout_preserves_intent_without_adopting_identical_object() {
    let mut b = broker();
    let p = prepare(&mut b);
    b.host.fail_write = true;
    let r = b.apply(&peer(), approval(&p)).unwrap();
    assert_eq!(r.state, State::ApplyUncertain);
    assert!(r.post_state.is_none());
    assert_eq!(r.events[1].state, State::ApplyIntent);
    assert_eq!(r.plan.before, p.before);
}
#[test]
fn target_and_internet_verification_required() {
    let mut b = broker();
    let p = prepare(&mut b);
    b.host.paths = false;
    assert_eq!(
        b.apply(&peer(), approval(&p)).unwrap().state,
        State::ApplyUncertain
    );
}
#[test]
fn restart_never_replays_preview_or_apply() {
    let mut b = broker();
    let p = prepare(&mut b);
    let mut restarted = Broker::open(
        b.host.clone(),
        b.journal.clone(),
        b.safety.clone(),
        b.clock.clone(),
        Auth,
    )
    .unwrap();
    assert!(restarted.apply(&peer(), approval(&p)).is_err());
    assert_eq!(restarted.host.writes, 0);
    b.apply(&peer(), approval(&p)).unwrap();
    let mut restarted = Broker::open(
        b.host.clone(),
        b.journal.clone(),
        b.safety.clone(),
        b.clock.clone(),
        Auth,
    )
    .unwrap();
    restarted.supervise().unwrap();
    assert_eq!(restarted.host.writes, 2);
    assert_eq!(
        restarted.recovery(&peer()).unwrap()[0].state,
        State::RolledBack
    );
}
#[test]
fn expired_preview_and_clock_rollback_cannot_extend_lifetime() {
    for wall in [999_999, 1_060_000] {
        let mut b = broker();
        let p = prepare(&mut b);
        b.clock.0.borrow_mut().0 = wall;
        assert!(b.apply(&peer(), approval(&p)).is_err());
        assert_eq!(b.host.writes, 0);
    }
    let mut b = broker();
    let p = prepare(&mut b);
    b.clock.0.borrow_mut().1 += 60_001;
    assert!(b.apply(&peer(), approval(&p)).is_err());
}
#[test]
fn due_timer_keeps_armed_unknown_receipt_unresolved() {
    let mut b = broker();
    let p = prepare(&mut b);
    b.apply(&peer(), approval(&p)).unwrap();
    b.clock.0.borrow_mut().0 += 30_001;
    let arm = unsafe_observation(&b, FlightState::Armed, 0);
    b.safety.values.borrow_mut().push_back(arm);
    let records = b.supervise().unwrap();
    assert_eq!(records[0].state, State::RecoveryBlocked);
    assert_eq!(b.host.writes, 1);
    b.supervise().unwrap();
    assert_eq!(b.host.writes, 2);
}
#[test]
fn crash_between_write_and_post_observation_is_not_adopted() {
    let mut b = broker();
    let p = prepare(&mut b);
    let mut r = b.journal.records.borrow()[0].clone();
    r.state = State::ApplyIntent;
    r.rollback_deadline_utc_ms = Some(1_030_000);
    b.journal.records.borrow_mut()[0] = r;
    b.host.state = platform::expected_after(&p);
    let mut restart = Broker::open(b.host, b.journal, b.safety, b.clock, Auth).unwrap();
    let records = restart.supervise().unwrap();
    assert_eq!(records[0].state, State::RecoveryBlocked);
    assert_eq!(restart.host.writes, 0);
}
#[test]
fn external_replacement_or_changed_default_blocks_rollback() {
    for changed in [true, false] {
        let mut b = broker();
        let p = prepare(&mut b);
        b.apply(&peer(), approval(&p)).unwrap();
        if changed {
            b.host.state.routes[0].gateway = Some(ip("10.0.0.99"))
        } else {
            b.host.state.default_routes[0].metric += 1
        }
        assert!(b.rollback(&peer(), approval(&p)).is_err());
        assert_eq!(b.host.writes, 1);
    }
}
#[test]
fn manager_and_unsupported_alias_gates_fail_closed() {
    let c = candidate();
    let mut s = host(Platform::Linux);
    s.manager = "networkd".into();
    assert!(platform::preflight(&c, &s).is_err());
    let c = Candidate {
        operation: Operation::AddressAlias {
            address: ip("10.8.0.5"),
            prefix: 32,
        },
        ..c
    };
    assert!(platform::preflight(&c, &host(Platform::Windows)).is_err());
    assert!(platform::preflight(&c, &host(Platform::Macos)).is_err());
}
#[test]
fn exact_linux_commands_use_manager_uuid_not_transient_ip() {
    let mut b = broker();
    let p = prepare(&mut b);
    let commands = platform::compile(&p, Direction::Apply).unwrap();
    assert_eq!(commands.len(), 2);
    assert_eq!(commands[0].program, platform::Program::NetworkManager);
    assert!(commands[0].args.contains(&"+ipv4.routes".into()));
    assert!(commands[0].args.contains(&p.before.connection_id));
    assert!(!commands
        .iter()
        .flat_map(|c| &c.args)
        .any(|a| a == "default" || a == "ip"));
}
#[test]
fn windows_rule_alias_is_encoded_not_interpolated() {
    let mut b = broker();
    b.host.state = host(Platform::Windows);
    let evil = "WiFi’; $(whoami); '摄影";
    b.host.state.interface = evil.into();
    let mut c = candidate();
    c.interface = evil.into();
    c.operation = Operation::FirewallUdp {
        remote_address: ip("10.0.0.2"),
        local_address: ip("10.0.0.5"),
        port: 5600,
    };
    let p = b
        .prepare(
            &peer(),
            Prepare {
                candidate: c,
                expires_in_s: 60,
                rollback_after_s: 30,
            },
        )
        .unwrap();
    let cmd = platform::compile(&p, Direction::Apply).unwrap();
    let script = cmd[0].args.last().unwrap();
    assert!(!script.contains(evil));
    assert!(!script.contains("whoami"));
    assert!(script.contains("FromBase64String"));
    assert!(script.contains("-LocalAddress '10.0.0.5'"));
    assert!(script.contains("-RemoteAddress '10.0.0.2'"));
}
#[test]
fn firewall_shared_zone_and_source_zone_membership_blocked() {
    let mut c = candidate();
    c.operation = Operation::FirewallUdp {
        remote_address: ip("10.0.0.2"),
        local_address: ip("10.0.0.5"),
        port: 5600,
    };
    let mut s = host(Platform::Linux);
    s.firewall_zone_interfaces.push("internet0".into());
    assert!(platform::preflight(&c, &s).is_err());
    s.firewall_zone_interfaces.pop();
    s.firewall_zone_sources.push("10.0.0.0/8".into());
    assert!(platform::preflight(&c, &s).is_err());
}

#[cfg(unix)]
#[test]
fn durable_journal_restart_exclusion_and_untrusted_file_rejection() {
    use q22_network_helper::journal::FileJournal;
    use std::os::unix::fs::{symlink, PermissionsExt};
    let directory = std::path::PathBuf::from(format!(
        "/tmp/q22-helper-test-{}",
        uuid::Uuid::new_v4().simple()
    ));
    let mut store = FileJournal::open_fixture(&directory).unwrap();
    assert!(FileJournal::open_fixture(&directory).is_err());
    let mut b = broker();
    let p = prepare(&mut b);
    let record = b.journal.records.borrow()[0].clone();
    store.save(&record).unwrap();
    drop(store);
    let mut reopened = FileJournal::open_fixture(&directory).unwrap();
    assert_eq!(reopened.load().unwrap(), vec![record]);
    let file = directory.join(format!("{}.json", p.plan_id));
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert!(reopened.load().is_err());
    std::fs::remove_file(&file).unwrap();
    symlink("/etc/passwd", &file).unwrap();
    assert!(reopened.load().is_err());
    drop(reopened);
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn monotonic_deadline_and_restart_never_extend_temporary_repair() {
    let mut b = broker();
    let p = prepare(&mut b);
    b.apply(&peer(), approval(&p)).unwrap();
    b.clock.0.borrow_mut().1 += 30_001; // UTC deliberately does not advance.
    b.supervise().unwrap();
    assert_eq!(b.host.writes, 2);
    assert_eq!(b.recovery(&peer()).unwrap()[0].state, State::RolledBack);
    let mut b = broker();
    let p = prepare(&mut b);
    b.apply(&peer(), approval(&p)).unwrap();
    b.clock.0.borrow_mut().0 = p.created_utc_ms + 1; // a restart cannot trust remaining wall-clock duration.
    let mut restart = Broker::open(b.host, b.journal, b.safety, b.clock, Auth).unwrap();
    let mut bad = unsafe_observation(&restart, FlightState::Unknown, 0);
    bad.recording_active = true;
    restart.safety.values.borrow_mut().push_back(bad);
    restart.supervise().unwrap();
    assert_eq!(restart.host.writes, 1);
    assert_eq!(
        restart.recovery(&peer()).unwrap()[0].state,
        State::RecoveryBlocked
    );
    restart.supervise().unwrap();
    assert_eq!(restart.host.writes, 2);
}
