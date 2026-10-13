//! Actual NetworkManager D-Bus adapter. Temporary changes use the active
//! connection's versioned Reapply transaction, never transient `ip` writes or
//! whole persistent-profile replacement. `ip` is used for read-only observation.
use crate::enrollment::Enrollment;
use crate::*;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::Read,
    net::{SocketAddr, TcpStream},
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tool {
    Busctl,
    Ip,
    Firewalld,
}
pub trait Runner {
    fn run(&mut self, tool: Tool, args: &[String]) -> Result<String>;
}
pub struct SystemRunner;
impl Runner for SystemRunner {
    fn run(&mut self, tool: Tool, args: &[String]) -> Result<String> {
        use std::os::unix::{fs::MetadataExt, process::CommandExt};
        let alternatives: &[&str] = match tool {
            Tool::Busctl => &["/usr/bin/busctl"],
            Tool::Ip => &["/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip"],
            Tool::Firewalld => &["/usr/bin/firewall-cmd"],
        };
        let executable = alternatives
            .iter()
            .find(|p| std::path::Path::new(p).is_file())
            .ok_or("Required native manager tool is unavailable")?;
        let canonical = std::fs::canonicalize(executable).map_err(|e| e.to_string())?;
        let metadata = std::fs::metadata(&canonical).map_err(|e| e.to_string())?;
        if metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
            return Err("Native OS tool is not root-owned and protected".into());
        }
        let mut child = std::process::Command::new(canonical)
            .args(args)
            .env_clear()
            .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
            .env("LC_ALL", "C")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .process_group(0)
            .spawn()
            .map_err(|e| e.to_string())?;
        let stdout = child.stdout.take().ok_or("No native stdout")?;
        let stderr = child.stderr.take().ok_or("No native stderr")?;
        let read = |stream: Box<dyn Read + Send>| {
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let result = stream.take(1_048_577).read_to_end(&mut bytes);
                (result, bytes)
            })
        };
        let out = read(Box::new(stdout));
        let err = read(Box::new(stderr));
        let start = Instant::now();
        let mut timed_out = false;
        let status = loop {
            match child.try_wait().map_err(|e| e.to_string())? {
                Some(status) => break status,
                None if start.elapsed() > Duration::from_secs(8) => {
                    timed_out = true;
                    unsafe {
                        libc::kill(-(child.id() as i32), libc::SIGKILL);
                    }
                    break child.wait().map_err(|e| e.to_string())?;
                }
                None => std::thread::sleep(Duration::from_millis(20)),
            }
        };
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        let (out_result, out) = out.join().map_err(|_| "Native reader failed")?;
        let (err_result, err) = err.join().map_err(|_| "Native reader failed")?;
        out_result.map_err(|e| e.to_string())?;
        err_result.map_err(|e| e.to_string())?;
        if timed_out {
            return Err("Native operation timed out; outcome may be uncertain".into());
        }
        if out.len() > 1_048_576 || err.len() > 1_048_576 {
            return Err("Native reply exceeds limit".into());
        }
        if !status.success() {
            return Err(format!(
                "Native operation failed: {}",
                String::from_utf8_lossy(&err)
                    .chars()
                    .take(512)
                    .collect::<String>()
            ));
        }
        String::from_utf8(out).map_err(|_| "Native reply is not UTF-8".into())
    }
}
fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(|s| s.to_string()).collect()
}
fn data(value: &Value) -> Result<&Vec<Value>> {
    value
        .get("data")
        .and_then(Value::as_array)
        .ok_or("Malformed busctl response".into())
}
fn variant<'a>(settings: &'a Value, section: &str, key: &str) -> Option<&'a Value> {
    settings.get(section)?.get(key)?.get("data")
}
fn canonical<T: Serialize>(values: &mut [T]) {
    values.sort_by_key(|v| serde_json::to_string(v).unwrap_or_default());
}
pub struct LinuxHost<R> {
    pub runner: R,
    pub policy: Enrollment,
    pub known: BTreeMap<String, Plan>,
    pub observer: Option<crate::safety::NativeObserver>,
}
impl<R: Runner> LinuxHost<R> {
    fn bus(&mut self, args: Vec<String>) -> Result<Value> {
        let mut command = strings(&["--system", "--timeout=3", "--json=short"]);
        command.extend(args);
        let result = self.runner.run(Tool::Busctl, &command)?;
        if result.trim().is_empty() {
            return Ok(json!({"type":"","data":[]}));
        }
        serde_json::from_str(&result).map_err(|_| "Malformed native D-Bus JSON".into())
    }
    fn device(&mut self, candidate: &Candidate) -> Result<String> {
        let mut args = strings(&[
            "call",
            "org.freedesktop.NetworkManager",
            "/org/freedesktop/NetworkManager",
            "org.freedesktop.NetworkManager",
            "GetDeviceByIpIface",
            "s",
        ]);
        args.push(candidate.interface.clone());
        let reply = self.bus(args)?;
        let path = data(&reply)?
            .first()
            .and_then(Value::as_str)
            .ok_or("NetworkManager device not found")?;
        if !path.starts_with("/org/freedesktop/NetworkManager/Devices/")
            || !path.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'/')
        {
            return Err("Unexpected native device path".into());
        }
        Ok(path.into())
    }
    fn property(&mut self, path: &str, interface: &str, name: &str) -> Result<Value> {
        let reply = self.bus(strings(&[
            "get-property",
            "org.freedesktop.NetworkManager",
            path,
            interface,
            name,
        ]))?;
        reply
            .get("data")
            .cloned()
            .ok_or("Missing native property".into())
    }
    fn applied(&mut self, path: &str) -> Result<(Value, u64)> {
        let reply = self.bus(strings(&[
            "call",
            "org.freedesktop.NetworkManager",
            path,
            "org.freedesktop.NetworkManager.Device",
            "GetAppliedConnection",
            "u",
            "0",
        ]))?;
        let values = data(&reply)?;
        if values.len() != 2 {
            return Err("Incomplete applied-connection response".into());
        }
        let settings = values[0].clone();
        if !settings.is_object() {
            return Err("Invalid applied-connection settings".into());
        }
        let version = values[1]
            .as_u64()
            .filter(|v| *v != 0)
            .ok_or("Missing or zero NetworkManager CAS version ID")?;
        Ok((settings, version))
    }
    fn native_snapshot(
        &mut self,
        candidate: &Candidate,
    ) -> Result<(HostState, String, Value, u64)> {
        self.policy.permits(candidate)?;
        let manager_version = self.property(
            "/org/freedesktop/NetworkManager",
            "org.freedesktop.NetworkManager",
            "Version",
        )?;
        let pieces: Vec<_> = manager_version
            .as_str()
            .ok_or("Missing NetworkManager version")?
            .split('.')
            .collect();
        let major = pieces
            .first()
            .and_then(|s| s.parse::<u32>().ok())
            .ok_or("Invalid NetworkManager version")?;
        let minor = pieces
            .get(1)
            .and_then(|s| s.parse::<u32>().ok())
            .ok_or("Invalid NetworkManager version")?;
        if major < 1 || (major == 1 && minor < 42) {
            return Err("NetworkManager 1.42 or newer is required to preserve externally configured addresses/routes".into());
        }
        let device = self.device(candidate)?;
        if self.property(&device, "org.freedesktop.NetworkManager.Device", "Managed")?
            != json!(true)
        {
            return Err("NetworkManager does not own this adapter".into());
        }
        let (settings, version) = self.applied(&device)?;
        let uuid = variant(&settings, "connection", "uuid")
            .and_then(Value::as_str)
            .ok_or("Applied connection UUID is unavailable")?
            .to_owned();
        if variant(&settings, "ipv4", "route-table")
            .and_then(Value::as_u64)
            .is_some_and(|t| t != 0 && t != 254)
        {
            return Err(
                "Non-main NetworkManager routing tables require separate qualification".into(),
            );
        }
        let addresses: Value = serde_json::from_str(
            &self
                .runner
                .run(Tool::Ip, &strings(&["-N", "-j", "-4", "address", "show"]))?,
        )
        .map_err(|_| "Invalid native addresses")?;
        let interfaces = addresses
            .as_array()
            .ok_or("Incomplete native address list")?;
        let adapter = interfaces
            .iter()
            .find(|i| i["ifname"].as_str() == Some(candidate.interface.as_str()))
            .ok_or("Selected adapter is absent")?;
        let index = adapter["ifindex"]
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .ok_or("Missing native interface index")?;
        let mac = adapter["address"]
            .as_str()
            .ok_or("Missing native hardware address")?;
        let adapter_id = format!("{device}@{mac}");
        let parse_addresses = |i: &Value| -> Result<Vec<Address>> {
            let values = i["addr_info"]
                .as_array()
                .ok_or("Missing native IPv4 addresses")?;
            values
                .iter()
                .filter(|a| a["family"] == "inet")
                .map(|a| {
                    Ok(Address {
                        address: a["local"]
                            .as_str()
                            .ok_or("Missing local address")?
                            .parse()
                            .map_err(|_| "Invalid local address")?,
                        prefix: a["prefixlen"]
                            .as_u64()
                            .and_then(|p| u8::try_from(p).ok())
                            .ok_or("Invalid prefix")?,
                    })
                })
                .collect()
        };
        let mut local = parse_addresses(adapter)?;
        let mut others = Vec::new();
        for other in interfaces
            .iter()
            .filter(|i| i["ifname"] != adapter["ifname"])
        {
            others.extend(parse_addresses(other)?)
        }
        canonical(&mut local);
        canonical(&mut others);
        let routes: Value = serde_json::from_str(&self.runner.run(
            Tool::Ip,
            &strings(&["-N", "-j", "-4", "route", "show", "table", "all"]),
        )?)
        .map_err(|_| "Invalid native routes")?;
        let mut regular = Vec::new();
        let mut defaults = Vec::new();
        for r in routes.as_array().ok_or("Incomplete native route list")? {
            if r.get("type").is_some_and(|t| t != "unicast") {
                continue;
            }
            let dest = r["dst"].as_str().unwrap_or("default");
            let (address, prefix) = if dest == "default" {
                ("0.0.0.0", 0)
            } else {
                let mut parts = dest.split('/');
                let address = parts.next().ok_or("Missing route destination")?;
                let prefix = parts
                    .next()
                    .map(|p| p.parse::<u8>())
                    .transpose()
                    .map_err(|_| "Invalid route prefix")?
                    .unwrap_or(32);
                (address, prefix)
            };
            let dev = r["dev"].as_str().ok_or("Native route has no interface")?;
            let route = Route {
                destination: address.parse().map_err(|_| "Invalid route address")?,
                prefix,
                gateway: r["gateway"]
                    .as_str()
                    .map(|s| s.parse())
                    .transpose()
                    .map_err(|_| "Invalid gateway")?,
                interface_id: if dev == candidate.interface {
                    adapter_id.clone()
                } else {
                    dev.into()
                },
                metric: r["metric"]
                    .as_u64()
                    .unwrap_or(0)
                    .try_into()
                    .map_err(|_| "Invalid route metric")?,
                table: match r.get("table") {
                    Some(Value::Number(n)) => n
                        .as_u64()
                        .and_then(|n| u32::try_from(n).ok())
                        .ok_or("Invalid route table")?,
                    Some(Value::String(s)) if s == "main" => 254,
                    None => 254,
                    _ => return Err("Unknown native route table".into()),
                },
            };
            if prefix == 0 {
                defaults.push(route)
            } else {
                regular.push(route)
            }
        }
        canonical(&mut regular);
        canonical(&mut defaults);
        let ip_config = self
            .property(
                &device,
                "org.freedesktop.NetworkManager.Device",
                "Ip4Config",
            )?
            .as_str()
            .ok_or("Missing native IP configuration")?
            .to_string();
        let dns_property = self.property(
            &ip_config,
            "org.freedesktop.NetworkManager.IP4Config",
            "NameserverData",
        )?;
        let mut dns = Vec::new();
        for entry in dns_property
            .as_array()
            .ok_or("Incomplete native DNS evidence")?
        {
            let address = entry["address"]["data"]
                .as_str()
                .ok_or("Missing native DNS address")?;
            dns.push(
                address
                    .parse()
                    .map_err(|_| "Invalid native DNS IPv4 address")?,
            )
        }
        canonical(&mut dns);
        let mut state = HostState {
            platform: Platform::Linux,
            adapter_id,
            interface: candidate.interface.clone(),
            interface_index: index,
            manager: "NetworkManager".into(),
            connection_id: uuid,
            revision: fingerprint(&(settings.clone(), version)),
            up: adapter["flags"]
                .as_array()
                .is_some_and(|flags| flags.iter().any(|f| f == "UP")),
            policy_allows_changes: true,
            dhcp: variant(&settings, "ipv4", "method").and_then(Value::as_str) == Some("auto"),
            addresses: local,
            routes: regular,
            default_routes: defaults,
            dns,
            other_subnets: others,
            firewall_zone: None,
            firewall_zone_interfaces: vec![],
            firewall_zone_sources: vec![],
            owned: vec![],
        };
        if matches!(candidate.operation, Operation::FirewallUdp { .. }) {
            let zone = self
                .runner
                .run(
                    Tool::Firewalld,
                    &[format!("--get-zone-of-interface={}", candidate.interface)],
                )?
                .trim()
                .to_owned();
            if zone.is_empty()
                || !zone
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
            {
                return Err("Unsupported firewall zone".into());
            }
            state.firewall_zone_interfaces = self
                .runner
                .run(
                    Tool::Firewalld,
                    &[format!("--zone={zone}"), "--list-interfaces".into()],
                )?
                .split_whitespace()
                .map(str::to_string)
                .collect();
            state.firewall_zone_sources = self
                .runner
                .run(
                    Tool::Firewalld,
                    &[format!("--zone={zone}"), "--list-sources".into()],
                )?
                .split_whitespace()
                .map(str::to_string)
                .collect();
            state.firewall_zone = Some(zone);
        }
        for plan in self
            .known
            .values()
            .filter(|p| p.candidate.interface == candidate.interface)
        {
            let expected = platform::object(plan);
            let present = match plan.candidate.operation {
                Operation::HostRoute { target, .. } => state.routes.iter().any(|r| {
                    r.destination == target
                        && r.prefix == 32
                        && r.gateway.is_none()
                        && r.interface_id == state.adapter_id
                        && r.metric == platform::metric(plan)
                        && r.table == 254
                }),
                Operation::AddressAlias { address, prefix } => {
                    state.addresses.contains(&Address { address, prefix })
                }
                Operation::FirewallUdp { .. } => {
                    if let Some(zone) = &state.firewall_zone {
                        let rules = self.runner.run(
                            Tool::Firewalld,
                            &[format!("--zone={zone}"), "--list-rich-rules".into()],
                        )?;
                        rules
                            .lines()
                            .any(|rule| rule.trim() == expected.native_identity)
                    } else {
                        false
                    }
                }
            };
            if present {
                state.owned.push(expected)
            }
        }
        canonical(&mut state.owned);
        Ok((state, device, settings, version))
    }
}

/// Encode only native D-Bus variant types supported by NetworkManager settings.
/// Arbitrary signatures, object paths, methods and executables are never inputs.
fn encode_variant(signature: &str, value: &Value, out: &mut Vec<String>) -> Result<()> {
    match signature {
        "s" => out.push(
            value
                .as_str()
                .ok_or("Invalid native string variant")?
                .to_string(),
        ),
        "b" => out.push(
            value
                .as_bool()
                .ok_or("Invalid native boolean variant")?
                .to_string(),
        ),
        "u" | "q" | "y" | "t" => {
            let n = value.as_u64().ok_or("Invalid native unsigned variant")?;
            let max = match signature {
                "y" => 255,
                "q" => 65535,
                "u" => u32::MAX as u64,
                _ => u64::MAX,
            };
            if n > max {
                return Err("Native integer variant overflow".into());
            }
            out.push(n.to_string());
        }
        "i" | "n" | "x" => out.push(
            value
                .as_i64()
                .ok_or("Invalid native signed variant")?
                .to_string(),
        ),
        "as" | "au" | "ay" | "aau" | "aa{sv}" => {
            let values = value.as_array().ok_or("Invalid native array variant")?;
            if values.len() > 4096 {
                return Err("Native array limit".into());
            }
            out.push(values.len().to_string());
            for v in values {
                encode_variant(&signature[1..], v, out)?
            }
        }
        "a{sv}" => {
            let map = value
                .as_object()
                .ok_or("Invalid native variant dictionary")?;
            if map.len() > 256 {
                return Err("Native dictionary limit".into());
            }
            out.push(map.len().to_string());
            for (k, v) in map {
                out.push(k.clone());
                let signature = v["type"].as_str().ok_or("Missing native variant type")?;
                out.push(signature.into());
                encode_variant(signature, &v["data"], out)?
            }
        }
        "a{ss}" => {
            let map = value
                .as_object()
                .ok_or("Invalid native string dictionary")?;
            out.push(map.len().to_string());
            for (k, v) in map {
                out.push(k.clone());
                out.push(
                    v.as_str()
                        .ok_or("Invalid native string dictionary value")?
                        .into(),
                );
            }
        }
        _ => {
            return Err(format!(
                "Unsupported native connection variant: {signature}"
            ))
        }
    }
    Ok(())
}
pub fn reapply_args(device: &str, settings: &Value, version: u64) -> Result<Vec<String>> {
    if version == 0 {
        return Err("An unversioned native write is forbidden".into());
    }
    let mut args = strings(&[
        "call",
        "org.freedesktop.NetworkManager",
        device,
        "org.freedesktop.NetworkManager.Device",
        "Reapply",
        "a{sa{sv}}tu",
    ]);
    let map = settings.as_object().ok_or("Invalid native connection")?;
    if map.len() > 64 {
        return Err("Native settings limit".into());
    }
    args.push(map.len().to_string());
    for (section, values) in map {
        args.push(section.clone());
        encode_variant("a{sv}", values, &mut args)?
    }
    args.push(version.to_string());
    args.push("1".into());
    Ok(args)
}
fn patch_settings(settings: &mut Value, plan: &Plan, direction: Direction) -> Result<()> {
    let (key, addition) = match plan.candidate.operation {
        Operation::HostRoute { target, .. } => (
            "route-data",
            json!({"dest":{"type":"s","data":target.to_string()},"prefix":{"type":"u","data":32},"metric":{"type":"u","data":platform::metric(plan)},"table":{"type":"u","data":254}}),
        ),
        Operation::AddressAlias { address, prefix } => (
            "address-data",
            json!({"address":{"type":"s","data":address.to_string()},"prefix":{"type":"u","data":prefix}}),
        ),
        _ => return Err("Firewall changes are not NetworkManager settings".into()),
    };
    let ipv4 = settings
        .get_mut("ipv4")
        .and_then(Value::as_object_mut)
        .ok_or("No applied IPv4 setting")?;
    let data = ipv4
        .entry(key)
        .or_insert_with(|| json!({"type":"aa{sv}","data":[]}));
    if data["type"] != "aa{sv}" {
        return Err("Unsupported native route/address encoding".into());
    }
    let entries = data["data"]
        .as_array_mut()
        .ok_or("Invalid native route/address array")?;
    if direction == Direction::Apply {
        if entries.contains(&addition) {
            return Err("Native addition already exists".into());
        }
        entries.push(addition)
    } else {
        let position = entries
            .iter()
            .position(|v| v == &addition)
            .ok_or("Owned native tuple changed")?;
        entries.remove(position);
    }
    // Legacy arrays conflict with modern data fields. An active connection
    // exporting both requires explicit reconciliation, never silent deletion.
    let legacy = if key == "route-data" {
        "routes"
    } else {
        "addresses"
    };
    if ipv4
        .get(legacy)
        .and_then(|v| v["data"].as_array())
        .is_some_and(|v| !v.is_empty())
    {
        return Err("Legacy and modern native address/route fields need qualification".into());
    }
    Ok(())
}
impl<R: Runner> Host for LinuxHost<R> {
    fn inspect(&mut self, candidate: &Candidate) -> Result<HostState> {
        Ok(self.native_snapshot(candidate)?.0)
    }
    fn change(&mut self, plan: &Plan, direction: Direction) -> Result<()> {
        let mut observer = self
            .observer
            .clone()
            .ok_or("Native safety observer unavailable")?;
        let safety = observer.observe()?;
        if safety.flight_state != FlightState::Disarmed
            || !safety.identity_verified
            || safety.recording_active
        {
            return Err("Native pre-write safety gate blocked".into());
        }
        self.policy.permits(&plan.candidate)?;
        let (current, device, mut settings, version) = self.native_snapshot(&plan.candidate)?;
        if direction == Direction::Apply {
            if fingerprint(&current) != plan.before_fingerprint {
                return Err("Native adapter changed before transaction".into());
            }
        } else if !platform::exact_addition(plan, &current) {
            return Err("Exact native owned state changed".into());
        }
        if matches!(plan.candidate.operation, Operation::FirewallUdp { .. }) {
            // Firewalld has no versioned compare-and-change for rich rules. The
            // implemented exact template remains diagnosis-only until a native
            // manager policy can close the race with zone/interface changes.
            return Err(
                "Automatic firewalld writer is not CAS-qualified; exact manual preview only".into(),
            );
        }
        patch_settings(&mut settings, plan, direction)?;
        let latest = observer.observe()?;
        if latest.flight_state != FlightState::Disarmed
            || !latest.identity_verified
            || latest.recording_active
            || latest.generation != safety.generation
        {
            return Err("Native safety changed during manager preflight".into());
        }
        self.bus(reapply_args(&device, &settings, version)?)?;
        if direction == Direction::Apply {
            self.known.insert(plan.plan_id.clone(), plan.clone());
        }
        Ok(())
    }
    fn verify_paths(&mut self, plan: &Plan) -> Result<bool> {
        let target = match plan.candidate.operation {
            Operation::HostRoute { target, .. } => target,
            Operation::FirewallUdp { remote_address, .. } => remote_address,
            Operation::AddressAlias { .. } => return Ok(false),
        };
        let local_address = match plan.candidate.operation {
            Operation::HostRoute { local_address, .. }
            | Operation::FirewallUdp { local_address, .. } => local_address,
            _ => return Ok(false),
        };
        let selected: Value = serde_json::from_str(&self.runner.run(
            Tool::Ip,
            &[
                "-N".into(),
                "-j".into(),
                "-4".into(),
                "route".into(),
                "get".into(),
                target.to_string(),
            ],
        )?)
        .map_err(|_| "Invalid native selected route")?;
        if !selected_route_matches(&selected, &plan.candidate.interface, local_address) {
            return Ok(false);
        }
        let endpoint = self
            .policy
            .devices
            .iter()
            .find(|device| device.host == target)
            .ok_or("No enrolled target verification endpoint")?;
        let target = SocketAddr::new(target.into(), endpoint.verification_port);
        let internet = SocketAddr::new(
            self.policy.internet_probe.host.into(),
            self.policy.internet_probe.port,
        );
        Ok(
            TcpStream::connect_timeout(&target, Duration::from_secs(1)).is_ok()
                && TcpStream::connect_timeout(&internet, Duration::from_secs(1)).is_ok(),
        )
    }
}

fn selected_route_matches(reply: &Value, interface: &str, local: Ipv4Addr) -> bool {
    let Some(routes) = reply.as_array().filter(|r| r.len() == 1) else {
        return false;
    };
    let route = &routes[0];
    route["dev"].as_str() == Some(interface)
        && route.get("type").is_none_or(|v| v == "unicast")
        && route
            .get("prefsrc")
            .or_else(|| route.get("src"))
            .and_then(Value::as_str)
            .and_then(|s| s.parse::<Ipv4Addr>().ok())
            == Some(local)
}
#[cfg(test)]
mod tests {
    #[test]
    fn selected_route_requires_exact_device_and_kernel_source() {
        let local = "10.0.0.5".parse().unwrap();
        let good = serde_json::json!([{"dev":"device0","prefsrc":"10.0.0.5"}]);
        assert!(super::selected_route_matches(&good, "device0", local));
        for bad in [
            serde_json::json!([]),
            serde_json::json!([{"dev":"internet0","prefsrc":"10.0.0.5"}]),
            serde_json::json!([{"dev":"device0","prefsrc":"192.168.0.3"}]),
            serde_json::json!([{"dev":"device0"}]),
        ] {
            assert!(!super::selected_route_matches(&bad, "device0", local));
        }
    }

    use super::*;
    #[test]
    fn dbus_reapply_is_versioned_preserves_external_ip_and_argv_only() {
        let settings = json!({"connection":{"id":{"type":"s","data":"literal; $(touch /tmp/no)"},"uuid":{"type":"s","data":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}},"ipv4":{"method":{"type":"s","data":"manual"},"route-data":{"type":"aa{sv}","data":[]}}});
        let args =
            reapply_args("/org/freedesktop/NetworkManager/Devices/2", &settings, 77).unwrap();
        assert!(args.contains(&"literal; $(touch /tmp/no)".to_string()));
        assert_eq!(&args[args.len() - 2..], &["77", "1"]);
        assert_eq!(args[5], "a{sa{sv}}tu");
    }
    #[test]
    fn unknown_dbus_signature_and_overflow_fail_closed() {
        let mut out = vec![];
        assert!(encode_variant("h", &json!(7), &mut out).is_err());
        assert!(encode_variant("u", &json!(u64::MAX), &mut out).is_err());
        assert!(encode_variant("s", &json!(42), &mut out).is_err());
    }
    struct Fixture {
        version: u64,
        nm_version: String,
        route: bool,
        metric: u32,
        writes: usize,
        fail_cas: bool,
    }
    impl Runner for Fixture {
        fn run(&mut self, tool: Tool, args: &[String]) -> Result<String> {
            let has = |s: &str| args.iter().any(|v| v == s);
            let value = match tool {
                Tool::Busctl if has("Version") => json!({"type":"s","data":self.nm_version}),
                Tool::Busctl if has("GetDeviceByIpIface") => {
                    json!({"type":"o","data":["/org/freedesktop/NetworkManager/Devices/8"]})
                }
                Tool::Busctl if has("Managed") => json!({"type":"b","data":true}),
                Tool::Busctl if has("Ip4Config") => {
                    json!({"type":"o","data":"/org/freedesktop/NetworkManager/IP4Config/8"})
                }
                Tool::Busctl if has("NameserverData") => {
                    json!({"type":"aa{sv}","data":[{"address":{"type":"s","data":"192.168.0.1"}}]})
                }
                Tool::Busctl if has("GetAppliedConnection") => {
                    let routes = if self.route {
                        vec![
                            json!({"dest":{"type":"s","data":"10.0.0.2"},"prefix":{"type":"u","data":32},"metric":{"type":"u","data":self.metric},"table":{"type":"u","data":254}}),
                        ]
                    } else {
                        vec![]
                    };
                    json!({"type":"a{sa{sv}}t","data":[{"connection":{"id":{"type":"s","data":"Ground device"},"uuid":{"type":"s","data":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}},"ipv4":{"method":{"type":"s","data":"manual"},"route-data":{"type":"aa{sv}","data":routes}}},self.version]})
                }
                Tool::Busctl if has("Reapply") => {
                    assert_eq!(args.last().map(String::as_str), Some("1"));
                    if self.fail_cas || args[args.len() - 2] != self.version.to_string() {
                        return Err("Native D-Bus version conflict".into());
                    }
                    self.metric = args
                        .windows(3)
                        .find(|v| v[0] == "metric" && v[1] == "u")
                        .map(|v| v[2].parse().unwrap())
                        .unwrap_or(0);
                    self.route = has("10.0.0.2");
                    self.writes += 1;
                    self.version += 1;
                    return Ok(String::new());
                }
                Tool::Ip if has("address") => {
                    json!([{"ifname":"device0","ifindex":8,"address":"aa:bb:cc:dd:ee:ff","flags":["UP"],"addr_info":[{"family":"inet","local":"10.0.0.5","prefixlen":24}]},{"ifname":"eth0","ifindex":2,"address":"00:11:22:33:44:55","flags":["UP"],"addr_info":[{"family":"inet","local":"192.168.0.4","prefixlen":24}]}])
                }
                Tool::Ip if has("route") => {
                    let mut routes = vec![
                        json!({"dst":"default","gateway":"192.168.0.1","dev":"eth0","metric":100,"table":254}),
                        json!({"dst":"10.8.0.0/24","dev":"eth0","metric":88,"table":254}),
                    ];
                    if self.route {
                        routes.push(json!({"dst":"10.0.0.2/32","dev":"device0","metric":self.metric,"table":254}));
                    }
                    json!(routes)
                }
                _ => return Err(format!("Unexpected fixture query {tool:?} {args:?}")),
            };
            Ok(value.to_string())
        }
    }
    fn fixture() -> LinuxHost<Fixture> {
        let policy:Enrollment=serde_json::from_value(json!({"version":1,"client_principal":"1000","client_image":"/opt/q22/q22-engine","client_sha256":"a".repeat(64),"helper_sha256":"b".repeat(64),"allowed_interfaces":["device0"],"devices":[{"host":"10.0.0.2","verification_port":80}],"internet_probe":{"host":"1.1.1.1","port":443},"observer":{"local_address":"127.0.0.1","port":14552,"sender":"10.0.0.2","system_id":1,"component_id":1,"expected_uid":42,"require_signed":false,"signing_key_path":null},"bench_enrollment":true})).unwrap();
        let observer = crate::safety::NativeObserver::unavailable(Instant::now());
        observer.accept(FlightState::Disarmed).unwrap();
        observer.recording_lease(false).unwrap();
        LinuxHost {
            runner: Fixture {
                version: 42,
                nm_version: "1.42.0".into(),
                route: false,
                metric: 0,
                writes: 0,
                fail_cas: false,
            },
            policy,
            known: Default::default(),
            observer: Some(observer),
        }
    }
    fn candidate() -> Candidate {
        Candidate {
            interface: "device0".into(),
            interface_index: Some(8),
            operation: Operation::HostRoute {
                target: "10.0.0.2".parse().unwrap(),
                local_address: "10.0.0.5".parse().unwrap(),
            },
        }
    }
    fn plan(host: &mut LinuxHost<Fixture>) -> Plan {
        let candidate = candidate();
        let before = host.inspect(&candidate).unwrap();
        Plan {
            plan_id: "1234567890abcdef1234567890abcdef".into(),
            principal: "1000".into(),
            candidate,
            before_fingerprint: fingerprint(&before),
            before,
            created_utc_ms: 1000,
            expires_utc_ms: 121000,
            rollback_after_ms: 30000,
            review_fingerprint: String::new(),
        }
    }
    #[test]
    fn real_busctl_property_framing_and_call_framing_are_distinct() {
        let mut host = fixture();
        let state = host.inspect(&candidate()).unwrap();
        assert_eq!(state.interface_index, 8);
        assert_eq!(state.dns, vec!["192.168.0.1".parse::<Ipv4Addr>().unwrap()]);
        assert_eq!(state.connection_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
        assert_eq!(state.default_routes[0].interface_id, "eth0");
    }
    #[test]
    fn versioned_apply_and_rollback_preserve_external_route_and_internet() {
        let mut host = fixture();
        let plan = plan(&mut host);
        host.change(&plan, Direction::Apply).unwrap();
        let after = host.inspect(&plan.candidate).unwrap();
        assert!(platform::exact_addition(&plan, &after));
        assert_eq!(after.default_routes, plan.before.default_routes);
        assert_eq!(after.dns, plan.before.dns);
        assert!(after.routes.contains(&plan.before.routes[0]));
        host.change(&plan, Direction::Rollback).unwrap();
        let rolled = host.inspect(&plan.candidate).unwrap();
        assert_eq!(rolled.routes, plan.before.routes);
        assert_eq!(host.runner.writes, 2);
    }
    #[test]
    fn native_cas_failure_is_not_adopted_or_replayed() {
        let mut host = fixture();
        let plan = plan(&mut host);
        host.runner.fail_cas = true;
        assert!(host.change(&plan, Direction::Apply).is_err());
        assert_eq!(host.runner.writes, 0);
        assert!(host.known.is_empty());
    }
    #[test]
    fn old_manager_and_zero_version_never_write() {
        let mut host = fixture();
        host.runner.nm_version = "1.36.6".into();
        assert!(host.inspect(&candidate()).is_err());
        host.runner.nm_version = "1.42.0".into();
        host.runner.version = 0;
        assert!(host.inspect(&candidate()).is_err());
        assert!(reapply_args("/org/freedesktop/NetworkManager/Devices/8", &json!({}), 0).is_err());
        assert_eq!(host.runner.writes, 0);
    }
}
