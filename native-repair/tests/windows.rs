//! Actual Windows native schema, synthetic snapshots only. No host writes.
use q22_network_helper::{
    platform,
    windows::{normalize_snapshot, Snapshot},
    *,
};
use serde_json::json;
const GUID: &str = "f09bbc4a-80d5-4c22-9506-a7b627dfe89d";
fn snapshot() -> Snapshot {
    serde_json::from_value(json!({
 "adapters":[{"name":"Ethernet","guid":GUID,"index":7,"up":true,"dhcp":false,"interface_metric":25},{"name":"Wi-Fi","guid":"d8c82bdd-2b48-4a69-ae81-9f18f1e85d80","index":9,"up":true,"dhcp":true,"interface_metric":40}],
 "addresses":[{"address":"10.0.0.1","prefix":24,"index":7},{"address":"172.16.7.1","prefix":24,"index":7},{"address":"192.168.1.5","prefix":24,"index":9}],
 "routes":[{"destination":"172.16.7.0","prefix":24,"gateway":"0.0.0.0","index":7,"metric":256},{"destination":"10.0.0.0","prefix":24,"gateway":"0.0.0.0","index":7,"metric":256},{"destination":"0.0.0.0","prefix":0,"gateway":"192.168.1.1","index":9,"metric":25}],
 "dns":[{"index":7,"addresses":[]},{"index":9,"addresses":["192.168.1.1"]}],
 "profiles":[{"name":"Domain","enabled":true,"allow_local_rules":"NotConfigured","default_inbound":"Block","default_outbound":"Allow"},{"name":"Private","enabled":true,"allow_local_rules":"True","default_inbound":"Block","default_outbound":"Allow"},{"name":"Public","enabled":true,"allow_local_rules":"True","default_inbound":"Block","default_outbound":"Allow"}],
 "rules":[],"firewall_filters":[],"domain_joined":false,"mdm_enrolled":false,"firewall_running":true
})).unwrap()
}
fn candidate(firewall: bool) -> Candidate {
    Candidate {
        interface: "Ethernet".into(),
        interface_index: Some(7),
        operation: if firewall {
            Operation::FirewallUdp {
                remote_address: "10.0.0.2".parse().unwrap(),
                local_address: "10.0.0.1".parse().unwrap(),
                port: 14550,
            }
        } else {
            Operation::HostRoute {
                target: "10.0.0.2".parse().unwrap(),
                local_address: "10.0.0.1".parse().unwrap(),
            }
        },
    }
}
fn plan(before: HostState, candidate: Candidate) -> Plan {
    Plan {
        plan_id: "a3b4029e70f24107a0629a51da359e33".into(),
        principal: "S-1-5-21-123".into(),
        candidate,
        before_fingerprint: fingerprint(&before),
        before,
        created_utc_ms: 1000,
        expires_utc_ms: 2000,
        rollback_after_ms: 30000,
        review_fingerprint: "unused_in_normalization".into(),
    }
}
fn rule(plan: &Plan) -> q22_network_helper::windows::NativeRule {
    serde_json::from_value(json!({"name":platform::object(plan).native_identity,"instance_id":platform::object(plan).native_identity,"association_verified":true,"enabled":true,"direction":"Inbound","action":"Allow","protocol":"UDP","local_address":["10.0.0.1"],"remote_address":["10.0.0.2"],"local_port":["14550"],"remote_port":["Any"],"interfaces":["Ethernet"],"profile":"Any","program":"Any","package":"Any","icmp_type":["Any"],"dynamic_target":"Any","service":"Any","interface_type":"Any","edge_traversal":"Block","policy_source_type":"Local","authentication":"NotRequired","encryption":"NotRequired","override_block_rules":false,"local_user":"Any","remote_user":"Any","remote_machine":"Any"})).unwrap()
}
fn add_rule(raw: &mut Snapshot, rule: q22_network_helper::windows::NativeRule) {
    for kind in [
        "address",
        "port",
        "interface",
        "application",
        "service",
        "interface_type",
        "security",
    ] {
        raw.firewall_filters
            .push(q22_network_helper::windows::NativeFilter {
                kind: kind.into(),
                instance_id: rule.instance_id.clone(),
                properties: json!({"synthetic_projection":"Any"}),
                association_rule: Some(rule.name.clone()),
            });
    }
    raw.rules.push(rule);
}
#[test]
fn native_order_is_canonical_and_multiroute_addition_matches() {
    let raw = snapshot();
    let c = candidate(false);
    let before = normalize_snapshot(raw.clone(), &c, &[]).unwrap();
    let plan = plan(before.clone(), c.clone());
    let mut changed = raw.clone();
    changed.routes.push(serde_json::from_value(json!({"destination":"10.0.0.2","prefix":32,"gateway":"0.0.0.0","index":7,"metric":platform::metric(&plan)})).unwrap());
    let after = normalize_snapshot(changed.clone(), &c, std::slice::from_ref(&plan)).unwrap();
    assert!(platform::exact_addition(&plan, &after));
    changed.routes.reverse();
    changed.addresses.reverse();
    changed.adapters.reverse();
    changed.dns.reverse();
    assert_eq!(
        after,
        normalize_snapshot(changed, &c, std::slice::from_ref(&plan)).unwrap()
    );
    assert!(
        normalize_snapshot(raw, &c, &[])
            .unwrap()
            .policy_allows_changes
    );
    assert_eq!(before.default_routes.len(), 1);
}
#[test]
fn only_witnessed_exact_rule_has_ownership_and_changed_security_is_foreign() {
    let raw = snapshot();
    let c = candidate(true);
    let before = normalize_snapshot(raw.clone(), &c, &[]).unwrap();
    let plan = plan(before, c.clone());
    let mut changed = raw;
    add_rule(&mut changed, rule(&plan));
    assert!(platform::exact_addition(
        &plan,
        &normalize_snapshot(changed.clone(), &c, std::slice::from_ref(&plan)).unwrap()
    ));
    let unverified = normalize_snapshot(changed.clone(), &c, &[]).unwrap();
    assert_eq!(unverified.owned[0].owner_id, "foreign_or_unverified");
    changed.rules[0].remote_user = "D:(A;;CC;;;WD)".into();
    let edited = normalize_snapshot(changed, &c, std::slice::from_ref(&plan)).unwrap();
    assert!(!platform::exact_addition(&plan, &edited));
    assert_eq!(
        edited.owned[0].native_identity,
        platform::object(&plan).native_identity
    );
    assert_eq!(edited.owned[0].owner_id, "foreign_or_unverified");
}
#[test]
fn unrelated_prefixed_rule_changes_are_part_of_cas_context() {
    let c = candidate(true);
    let raw = snapshot();
    let p = plan(normalize_snapshot(raw.clone(), &c, &[]).unwrap(), c.clone());
    let mut raw = raw;
    let mut foreign = rule(&p);
    foreign.name = "Q22-Helper-foreign".into();
    add_rule(&mut raw, foreign);
    let a = normalize_snapshot(raw.clone(), &c, &[]).unwrap();
    raw.rules[0].remote_address = vec!["10.1.2.3".into()];
    let b = normalize_snapshot(raw, &c, &[]).unwrap();
    assert_ne!(a.connection_id, b.connection_id);
}
#[test]
fn domain_mdm_disabled_firewall_unknown_dns_ambiguous_adapter_all_block() {
    let c = candidate(false);
    for condition in 0..4 {
        let mut raw = snapshot();
        match condition {
            0 => raw.domain_joined = true,
            1 => raw.mdm_enrolled = true,
            2 => raw.firewall_running = false,
            _ => raw.profiles[0].allow_local_rules = "False".into(),
        };
        assert!(
            !normalize_snapshot(raw, &c, &[])
                .unwrap()
                .policy_allows_changes
        )
    }
    let mut raw = snapshot();
    raw.dns.retain(|d| d.index != 7);
    assert!(normalize_snapshot(raw, &c, &[]).is_err());
    let mut raw = snapshot();
    raw.adapters.push(raw.adapters[0].clone());
    assert!(normalize_snapshot(raw, &c, &[]).is_err());
}
#[test]
fn foreign_matching_route_cannot_be_adopted_without_success_receipt() {
    let c = candidate(false);
    let mut raw = snapshot();
    let p = plan(normalize_snapshot(raw.clone(), &c, &[]).unwrap(), c.clone());
    raw.routes.push(serde_json::from_value(json!({"destination":"10.0.0.2","prefix":32,"gateway":"0.0.0.0","index":7,"metric":platform::metric(&p)})).unwrap());
    let actual = normalize_snapshot(raw, &c, &[]).unwrap();
    assert!(actual.owned.is_empty());
    assert!(!platform::exact_addition(&p, &actual));
}

#[test]
fn opaque_foreign_filter_swap_changes_context_even_without_rule_join() {
    let c = candidate(false);
    let mut raw = snapshot();
    for (id, port) in [("middle-a", "1200"), ("middle-b", "1300")] {
        raw.firewall_filters
            .push(q22_network_helper::windows::NativeFilter {
                kind: "port".into(),
                instance_id: id.into(),
                properties: json!({"LocalPort":[port]}),
                association_rule: None,
            });
    }
    let before = normalize_snapshot(raw.clone(), &c, &[]).unwrap();
    let first = raw.firewall_filters[0].properties.clone();
    raw.firewall_filters[0].properties = raw.firewall_filters[1].properties.clone();
    raw.firewall_filters[1].properties = first;
    let after = normalize_snapshot(raw, &c, &[]).unwrap();
    assert_ne!(before.connection_id, after.connection_id);
}
#[test]
fn missing_duplicate_or_contradictory_filter_evidence_never_proves_ownership() {
    let c = candidate(true);
    let empty = snapshot();
    let p = plan(
        normalize_snapshot(empty.clone(), &c, &[]).unwrap(),
        c.clone(),
    );
    let mut raw = empty;
    add_rule(&mut raw, rule(&p));
    let mut missing = raw.clone();
    missing.firewall_filters.pop();
    assert!(normalize_snapshot(missing, &c, std::slice::from_ref(&p)).is_err());
    let mut duplicate = raw.clone();
    duplicate
        .firewall_filters
        .push(duplicate.firewall_filters[0].clone());
    assert!(normalize_snapshot(duplicate, &c, std::slice::from_ref(&p)).is_err());
    let mut bad = raw.clone();
    bad.firewall_filters[0].association_rule = Some("another-rule".into());
    assert!(normalize_snapshot(bad, &c, std::slice::from_ref(&p)).is_err());
    raw.rules[0].association_verified = false;
    let actual = normalize_snapshot(raw, &c, std::slice::from_ref(&p)).unwrap();
    assert_eq!(actual.owned[0].owner_id, "foreign_or_unverified");
    assert!(!platform::exact_addition(&p, &actual));
}
#[test]
fn foreign_unselected_filter_rows_survive_helper_addition_and_rollback_guard() {
    let c = candidate(true);
    let mut raw = snapshot();
    raw.firewall_filters
        .push(q22_network_helper::windows::NativeFilter {
            kind: "application".into(),
            instance_id: "disabled-or-ipsec-rule".into(),
            properties: json!({"Program":"C:\\foreign.exe","Package":"Any"}),
            association_rule: None,
        });
    let p = plan(normalize_snapshot(raw.clone(), &c, &[]).unwrap(), c.clone());
    add_rule(&mut raw, rule(&p));
    assert!(platform::exact_addition(
        &p,
        &normalize_snapshot(raw.clone(), &c, std::slice::from_ref(&p)).unwrap()
    ));
    raw.firewall_filters[0].properties["Program"] = json!("C:\\changed.exe");
    assert!(!platform::exact_addition(
        &p,
        &normalize_snapshot(raw, &c, std::slice::from_ref(&p)).unwrap()
    ));
}

#[test]
fn case_variant_rule_names_with_distinct_instance_ids_are_ambiguous() {
    let c = candidate(true);
    let mut raw = snapshot();
    let p = plan(normalize_snapshot(raw.clone(), &c, &[]).unwrap(), c.clone());
    let original = rule(&p);
    let mut case_variant = original.clone();
    case_variant.name = original.name.to_uppercase();
    case_variant.instance_id = "different-native-instance".into();
    add_rule(&mut raw, original);
    add_rule(&mut raw, case_variant);
    let error = normalize_snapshot(raw, &c, std::slice::from_ref(&p)).unwrap_err();
    assert!(error.contains("duplicate native rule identity"));
}
