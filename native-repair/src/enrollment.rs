//! Installation-owned private bench policy. Public release signing is separate.
use crate::*;
use std::path::Path;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Enrollment {
    pub version: u32,
    pub client_principal: String,
    pub client_image: String,
    pub client_sha256: String,
    pub helper_sha256: String,
    pub allowed_interfaces: Vec<String>,
    pub devices: Vec<Device>,
    pub internet_probe: Probe,
    pub observer: ObserverConfig,
    pub bench_enrollment: bool,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub host: Ipv4Addr,
    pub verification_port: u16,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Probe {
    pub host: Ipv4Addr,
    pub port: u16,
}
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObserverConfig {
    pub local_address: Ipv4Addr,
    pub port: u16,
    pub sender: Ipv4Addr,
    pub system_id: u8,
    pub component_id: u8,
    pub expected_uid: Option<u64>,
    pub require_signed: bool,
    pub signing_key_path: Option<String>,
}
impl Enrollment {
    pub fn validate(&self) -> Result<()> {
        let digest = |value: &str| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        };
        let filename = Path::new(&self.client_image)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        if self.version != 1
            || !self.bench_enrollment
            || self.client_principal.is_empty()
            || self.client_principal.len() > 256
            || !Path::new(&self.client_image).is_absolute()
            || !matches!(filename, "q22-engine" | "q22-engine.exe")
            || !digest(&self.client_sha256)
            || !digest(&self.helper_sha256)
            || self.allowed_interfaces.is_empty()
            || self.allowed_interfaces.len() > 8
            || self
                .allowed_interfaces
                .iter()
                .any(|s| s.is_empty() || s.len() > 256 || s.chars().any(char::is_control))
            || self.devices.is_empty()
            || self.devices.len() > 8
            || self
                .devices
                .iter()
                .any(|d| !unicast(d.host) || d.verification_port == 0)
            || !unicast(self.internet_probe.host)
            || self.internet_probe.port == 0
        {
            return Err(
                "Invalid bounded private bench enrollment; a frozen engine image is required"
                    .into(),
            );
        }
        let o = &self.observer;
        if o.local_address.is_unspecified()
            || o.local_address.is_multicast()
            || matches!(o.port, 0 | 14550 | 5600)
            || o.sender.is_unspecified()
            || o.sender.is_multicast()
            || o.system_id == 0
            || o.component_id == 0
            || o.expected_uid == Some(0)
            || (o.require_signed && o.signing_key_path.is_none())
            || o.signing_key_path
                .as_ref()
                .is_some_and(|p| !Path::new(p).is_absolute())
        {
            return Err("Invalid dedicated native observer configuration".into());
        }
        Ok(())
    }
    pub fn permits(&self, candidate: &Candidate) -> Result<()> {
        candidate.validate()?;
        if !self.allowed_interfaces.contains(&candidate.interface) {
            return Err("Adapter is outside installed bench policy".into());
        }
        let target = match candidate.operation {
            Operation::HostRoute { target, .. } => Some(target),
            Operation::FirewallUdp { remote_address, .. } => Some(remote_address),
            Operation::AddressAlias { .. } => None,
        };
        if target.is_some_and(|host| !self.devices.iter().any(|device| device.host == host)) {
            return Err("Device is outside installed bench policy".into());
        }
        Ok(())
    }
}
