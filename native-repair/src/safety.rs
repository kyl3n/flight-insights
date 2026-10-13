//! Independent native MAVLink 2 signed-heartbeat observer. This never transmits
//! packets, negotiates streams or shares a GCS/recorder socket. It consumes an
//! explicitly configured dedicated forwarded stream with original signatures.
use crate::{FlightState, Result, Safety, SafetyObserver};
use sha2::{Digest, Sha256};
use std::net::Ipv4Addr;
#[cfg(unix)]
use std::net::{SocketAddr, UdpSocket};
#[cfg(unix)]
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
#[cfg(unix)]
use std::time::{SystemTime, UNIX_EPOCH};

const MAVLINK_EPOCH_UNIX: u64 = 1_420_070_400;
pub struct SignedHeartbeat {
    key: [u8; 32],
    sender: Ipv4Addr,
    system_id: u8,
    component_id: u8,
    last_timestamp: std::collections::HashMap<u8, u64>,
}
impl SignedHeartbeat {
    pub fn new(key: [u8; 32], sender: Ipv4Addr, system_id: u8, component_id: u8) -> Result<Self> {
        if key.iter().all(|b| *b == 0)
            || system_id == 0
            || component_id == 0
            || sender.is_unspecified()
            || sender.is_multicast()
        {
            return Err(
                "An installed signing key, exact sender and aircraft identity are required".into(),
            );
        }
        Ok(Self {
            key,
            sender,
            system_id,
            component_id,
            last_timestamp: Default::default(),
        })
    }
    /// `now_unix_us` bounds first-packet replay. Timestamp units are 10 usec
    /// since 2015-01-01; each signing link must strictly advance thereafter.
    pub fn observe(
        &mut self,
        packet: &[u8],
        sender: Ipv4Addr,
        now_unix_us: u64,
    ) -> Option<FlightState> {
        if sender != self.sender {
            return None;
        }
        let mut result = None;
        for offset in 0..packet.len() {
            if packet[offset] != 0xfd || packet.len() - offset < 34 {
                continue;
            }
            let frame = &packet[offset..];
            if frame[1] != 9
                || frame[2] != 1
                || frame[5] != self.system_id
                || frame[6] != self.component_id
                || frame[7..10] != [0, 0, 0]
            {
                continue;
            }
            let length = 12 + usize::from(frame[1]);
            if frame.len() < length + 13 {
                continue;
            }
            let mut crc = 0xffffu16;
            for byte in frame[1..length - 2].iter().chain(std::iter::once(&50u8)) {
                crc_accumulate(*byte, &mut crc);
            }
            if frame[length - 2..length] != crc.to_le_bytes() {
                continue;
            }
            let signature = &frame[length..length + 13];
            let link_id = signature[0];
            let mut timestamp_bytes = [0u8; 8];
            timestamp_bytes[..6].copy_from_slice(&signature[1..7]);
            let timestamp = u64::from_le_bytes(timestamp_bytes);
            let packet_unix_us = MAVLINK_EPOCH_UNIX * 1_000_000 + timestamp * 10;
            if packet_unix_us.abs_diff(now_unix_us) > 60_000_000
                || self
                    .last_timestamp
                    .get(&link_id)
                    .is_some_and(|old| timestamp <= *old)
            {
                continue;
            }
            let mut hash = Sha256::new();
            hash.update(self.key);
            hash.update(&frame[..length + 7]);
            let digest = hash.finalize();
            let mut different = 0u8;
            for i in 0..6 {
                different |= digest[i] ^ signature[7 + i];
            }
            if different != 0 {
                continue;
            }
            let payload = &frame[10..19];
            // GCS or invalid autopilot heartbeats never establish disarmed state.
            if payload[4] == 6 || payload[5] == 8 || payload[8] != 3 {
                continue;
            }
            self.last_timestamp.insert(link_id, timestamp);
            result = Some(if payload[6] & 128 != 0 {
                FlightState::Armed
            } else {
                FlightState::Disarmed
            });
        }
        result
    }
}
fn crc_accumulate(data: u8, crc: &mut u16) {
    let mut tmp = data ^ (*crc as u8);
    tmp ^= tmp << 4;
    *crc = (*crc >> 8) ^ ((tmp as u16) << 8) ^ ((tmp as u16) << 3) ^ ((tmp as u16) >> 4);
}

/// CRC-validated source/system/component evidence is not cryptographic identity.
/// Signing is enforced only when the installation policy explicitly requests it.
pub struct PassiveHeartbeat {
    config: crate::enrollment::ObserverConfig,
    key: Option<[u8; 32]>,
    timestamps: std::collections::HashMap<u8, u64>,
    uid: Option<u64>,
    boot: Option<(u32, u64)>,
}
impl PassiveHeartbeat {
    pub fn new(config: crate::enrollment::ObserverConfig, key: Option<[u8; 32]>) -> Result<Self> {
        if config.require_signed && key.is_none() {
            return Err("Installed policy requires a signing key".into());
        }
        Ok(Self {
            config,
            key,
            timestamps: Default::default(),
            uid: None,
            boot: None,
        })
    }
    pub fn observe_all(
        &mut self,
        packet: &[u8],
        sender: Ipv4Addr,
        now_unix_us: u64,
    ) -> Vec<FlightState> {
        if sender != self.config.sender {
            return vec![];
        }
        let mut observed = Vec::new();
        for offset in 0..packet.len() {
            let frame = &packet[offset..];
            if frame.len() < 8 || !matches!(frame[0], 0xfe | 0xfd) {
                continue;
            }
            let v2 = frame[0] == 0xfd;
            let header = if v2 { 10 } else { 6 };
            if frame.len() < header {
                continue;
            }
            let size = header + usize::from(frame[1]) + 2;
            let signed = v2 && frame[2] & 1 != 0;
            if frame.len() < size + if signed { 13 } else { 0 } || (v2 && frame[2] & !1 != 0) {
                continue;
            }
            let (system, component, message) = if v2 {
                (
                    frame[5],
                    frame[6],
                    u32::from(frame[7]) | (u32::from(frame[8]) << 8) | (u32::from(frame[9]) << 16),
                )
            } else {
                (frame[3], frame[4], u32::from(frame[5]))
            };
            let extra = match message {
                0 => 50,
                2 => 137,
                148 => 178,
                _ => continue,
            };
            let mut crc = 0xffff;
            for byte in frame[1..size - 2].iter().chain(std::iter::once(&extra)) {
                crc_accumulate(*byte, &mut crc)
            }
            if frame[size - 2..size] != crc.to_le_bytes() {
                continue;
            }
            if self.config.require_signed && !signed {
                continue;
            }
            if signed {
                if let Some(key) = self.key {
                    let sig = &frame[size..size + 13];
                    let mut bytes = [0; 8];
                    bytes[..6].copy_from_slice(&sig[1..7]);
                    let timestamp = u64::from_le_bytes(bytes);
                    if self
                        .timestamps
                        .get(&sig[0])
                        .is_some_and(|previous| timestamp <= *previous)
                        || (MAVLINK_EPOCH_UNIX * 1_000_000 + timestamp * 10).abs_diff(now_unix_us)
                            > 60_000_000
                    {
                        continue;
                    }
                    let mut hash = Sha256::new();
                    hash.update(key);
                    hash.update(&frame[..size + 7]);
                    let digest = hash.finalize();
                    let mut difference = 0;
                    for i in 0..6 {
                        difference |= digest[i] ^ sig[7 + i]
                    }
                    if difference != 0 {
                        continue;
                    }
                    self.timestamps.insert(sig[0], timestamp);
                }
            }
            if system != self.config.system_id || component != self.config.component_id {
                self.uid = None;
                self.boot = None;
                observed.push(FlightState::Unknown);
                continue;
            }
            let payload = &frame[header..size - 2];
            if message == 148 {
                if payload.len() < 16 {
                    continue;
                }
                let uid = u64::from_le_bytes(payload[8..16].try_into().unwrap());
                if uid == 0
                    || self
                        .config
                        .expected_uid
                        .is_some_and(|expected| expected != uid)
                    || self.uid.is_some_and(|previous| previous != uid)
                {
                    self.uid = None;
                    self.boot = None;
                    observed.push(FlightState::Unknown);
                } else {
                    self.uid = Some(uid)
                }
                continue;
            }
            if message == 2 {
                // MAVLink 2 may truncate trailing zero boot bytes; zero pad the
                // fixed SYSTEM_TIME payload after validating its frame CRC.
                if payload.len() > 12 {
                    continue;
                }
                let mut padded = [0u8; 12];
                padded[..payload.len()].copy_from_slice(payload);
                let boot = u32::from_le_bytes(padded[8..12].try_into().unwrap());
                let discontinuity = boot == 0
                    || self.boot.is_some_and(|(old, seen)| {
                        let elapsed = now_unix_us.checked_sub(seen).map(|n| n / 1000);
                        boot < old
                            || elapsed.is_none()
                            || u64::from(boot).abs_diff(u64::from(old) + elapsed.unwrap_or(0))
                                > 5000
                    });
                if discontinuity {
                    self.uid = None;
                    self.boot = None;
                    observed.push(FlightState::Unknown)
                } else {
                    self.boot = Some((boot, now_unix_us))
                }
                continue;
            }
            if payload.len() != 9 || payload[4] == 6 || payload[5] == 8 || payload[8] != 3 {
                continue;
            }
            let boot_fresh = self
                .boot
                .is_some_and(|(_, seen)| now_unix_us >= seen && now_unix_us - seen <= 3_000_000);
            if self.uid.is_none() || !boot_fresh {
                observed.push(FlightState::Unknown);
                continue;
            }
            observed.push(if payload[6] & 128 != 0 {
                FlightState::Armed
            } else {
                FlightState::Disarmed
            });
        }
        observed
    }
}

struct Evidence {
    state: FlightState,
    received: Option<Instant>,
    generation: u64,
    recording: Option<(bool, Instant)>,
}
#[derive(Clone)]
pub struct NativeObserver {
    start: Instant,
    evidence: Arc<Mutex<Evidence>>,
    recording_guard: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
    signature_required: bool,
}
impl NativeObserver {
    pub fn unavailable(start: Instant) -> Self {
        Self {
            start,
            evidence: Arc::new(Mutex::new(Evidence {
                state: FlightState::Unknown,
                received: None,
                generation: 0,
                recording: None,
            })),
            recording_guard: None,
            signature_required: false,
        }
    }
    pub fn set_recording_guard(&mut self, guard: Arc<dyn Fn() -> bool + Send + Sync>) {
        self.recording_guard = Some(guard);
    }
    pub fn set_signature_policy(&mut self, required: bool) {
        self.signature_required = required;
    }
    pub fn public_safety(&self) -> serde_json::Value {
        let mut observer = self.clone();
        let value = observer.observe();
        match value {
            Ok(value) => {
                let age = self
                    .monotonic_ms()
                    .saturating_sub(value.observed_monotonic_ms) as f64
                    / 1000.0;
                let mut reasons = Vec::new();
                if value.flight_state != FlightState::Disarmed {
                    reasons
                        .push("The native observer requires current disarmed aircraft evidence.");
                }
                if !value.identity_verified || age > 3.0 {
                    reasons.push("Native UID/boot/source/heartbeat identity evidence is missing, contradictory or stale.");
                }
                if value.recording_active {
                    reasons.push("The managed recording lease or native worker-process census is active or unknown.");
                }
                serde_json::json!({"allowed":reasons.is_empty(),"state":if reasons.is_empty(){"allowed"}else{"blocked"},"reasons":reasons,"flight_state":value.flight_state,"identity_verified":value.identity_verified,"aircraft_identity_assurance":if !value.identity_verified{"unverified"}else if self.signature_required{"mavlink2_signature"}else{"observed_unauthenticated"},"heartbeat_age_s":age,"recording_active":value.recording_active,"max_heartbeat_age_s":3.0,"authority":"native_observer","atomic_revocation":false})
            }
            Err(_) => {
                serde_json::json!({"allowed":false,"state":"blocked","reasons":["Native safety observer unavailable"],"flight_state":"unknown","identity_verified":false,"aircraft_identity_assurance":"unverified","heartbeat_age_s":null,"recording_active":null,"authority":"native_observer","atomic_revocation":false})
            }
        }
    }
    /// Only the authenticated frozen-engine peer may renew this lease. It is
    /// not part of renderer RPC. Missing/disconnected/expired lease blocks.
    pub fn recording_lease(&self, active: bool) -> Result<()> {
        let mut e = self
            .evidence
            .lock()
            .map_err(|_| "Safety observer poisoned")?;
        if e.recording
            .as_ref()
            .is_none_or(|(old, at)| *old != active || at.elapsed() > Duration::from_secs(3))
        {
            e.generation += 1;
        }
        e.recording = Some((active, Instant::now()));
        Ok(())
    }
    pub fn accept(&self, state: FlightState) -> Result<()> {
        let mut e = self
            .evidence
            .lock()
            .map_err(|_| "Safety observer poisoned")?;
        if e.state != state
            || e.received
                .is_none_or(|at| at.elapsed() > Duration::from_secs(3))
        {
            e.generation += 1;
        }
        e.state = state;
        e.received = Some(Instant::now());
        Ok(())
    }
    pub fn monotonic_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }
    /// Socket binding is explicit enrollment only. No SO_REUSE* and no send.
    #[cfg(unix)]
    pub fn listen(
        &self,
        local: SocketAddr,
        mut validator: PassiveHeartbeat,
        stop: Arc<AtomicBool>,
    ) -> Result<std::thread::JoinHandle<()>> {
        if !local.is_ipv4()
            || local.ip().is_unspecified()
            || matches!(local.port(), 0 | 14550 | 5600)
        {
            return Err(
                "Select a dedicated explicit IPv4 observer endpoint, not a GCS/video port".into(),
            );
        }
        let socket = UdpSocket::bind(local).map_err(|e| e.to_string())?;
        socket
            .set_read_timeout(Some(Duration::from_millis(250)))
            .map_err(|e| e.to_string())?;
        let observer = self.clone();
        Ok(std::thread::spawn(move || {
            let mut buffer = [0u8; 65507];
            while !stop.load(Ordering::Acquire) {
                if let Ok((count, SocketAddr::V4(sender))) = socket.recv_from(&mut buffer) {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .map(|d| d.as_micros() as u64)
                        .unwrap_or(0);
                    for state in validator.observe_all(&buffer[..count], *sender.ip(), now) {
                        let _ = observer.accept(state);
                    }
                }
            }
        }))
    }
}
impl SafetyObserver for NativeObserver {
    fn observe(&mut self) -> Result<Safety> {
        let census_clear = self.recording_guard.as_ref().is_none_or(|guard| guard());
        let e = self
            .evidence
            .lock()
            .map_err(|_| "Safety observer poisoned")?;
        let fresh = e
            .received
            .is_some_and(|at| at.elapsed() <= Duration::from_secs(3));
        let recording = !census_clear
            || e.recording
                .as_ref()
                .is_none_or(|(active, at)| *active || at.elapsed() > Duration::from_secs(3));
        Ok(Safety {
            flight_state: if fresh {
                e.state.clone()
            } else {
                FlightState::Unknown
            },
            identity_verified: fresh && e.state != FlightState::Unknown,
            recording_active: recording,
            observed_monotonic_ms: e
                .received
                .map(|at| at.duration_since(self.start).as_millis() as u64)
                .unwrap_or(0),
            generation: e.generation,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn packet(key: [u8; 32], time: u64, armed: bool) -> Vec<u8> {
        let mut p = vec![
            0xfd,
            9,
            1,
            0,
            44,
            1,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            2,
            3,
            if armed { 128 } else { 0 },
            3,
            3,
        ];
        let mut crc = 0xffff;
        for b in p[1..].iter().chain(std::iter::once(&50)) {
            crc_accumulate(*b, &mut crc);
        }
        p.extend(crc.to_le_bytes());
        p.push(1);
        p.extend(&time.to_le_bytes()[..6]);
        let mut hash = Sha256::new();
        hash.update(key);
        hash.update(&p);
        p.extend(&hash.finalize()[..6]);
        p
    }
    #[test]
    fn only_signed_expected_recent_aircraft_can_establish_state() {
        let key = [7; 32];
        let sender = "10.0.0.2".parse().unwrap();
        let timestamp = 100_000;
        let now = MAVLINK_EPOCH_UNIX * 1_000_000 + timestamp * 10;
        let mut v = SignedHeartbeat::new(key, sender, 1, 1).unwrap();
        assert_eq!(
            v.observe(&packet(key, timestamp, false), sender, now),
            Some(FlightState::Disarmed)
        );
        assert_eq!(v.observe(&packet(key, timestamp, false), sender, now), None);
        assert_eq!(
            v.observe(&packet([8; 32], timestamp + 1, true), sender, now),
            None
        );
        assert_eq!(
            v.observe(
                &packet(key, timestamp + 1, true),
                "10.0.0.3".parse().unwrap(),
                now
            ),
            None
        );
        assert_eq!(
            v.observe(&packet(key, timestamp + 1, true), sender, now),
            Some(FlightState::Armed)
        );
        assert_eq!(
            v.observe(&packet(key, timestamp + 2, false), sender, now + 61_000_000),
            None
        );
    }
    #[test]
    fn unsigned_bad_crc_wrong_identity_and_gcs_are_ignored() {
        let key = [7; 32];
        let sender = "10.0.0.2".parse().unwrap();
        let now = MAVLINK_EPOCH_UNIX * 1_000_000 + 1_000_000;
        for index in [2, 5, 6, 10, 18, 19, 27] {
            let mut p = packet(key, 100_000, false);
            p[index] ^= 1;
            let mut v = SignedHeartbeat::new(key, sender, 1, 1).unwrap();
            assert_eq!(v.observe(&p, sender, now), None);
        }
    }
    #[test]
    fn recorder_lease_required_separately_from_native_heartbeat() {
        let mut observer = NativeObserver::unavailable(Instant::now());
        assert_eq!(
            observer.observe().unwrap().flight_state,
            FlightState::Unknown
        );
        observer.accept(FlightState::Disarmed).unwrap();
        assert!(observer.observe().unwrap().recording_active);
        observer.recording_lease(false).unwrap();
        assert!(!observer.observe().unwrap().recording_active);
        observer.recording_lease(true).unwrap();
        assert!(observer.observe().unwrap().recording_active);
    }
    fn unsigned(message: u8, payload: &[u8], system: u8) -> Vec<u8> {
        let mut bytes = vec![0xfe, payload.len() as u8, 0, system, 1, message];
        bytes.extend(payload);
        let extra = match message {
            0 => 50,
            2 => 137,
            148 => 178,
            _ => panic!(),
        };
        let mut crc = 0xffff;
        for b in bytes[1..].iter().chain(std::iter::once(&extra)) {
            crc_accumulate(*b, &mut crc)
        }
        bytes.extend(crc.to_le_bytes());
        bytes
    }
    fn heartbeat(armed: bool) -> Vec<u8> {
        unsigned(0, &[0, 0, 0, 0, 2, 3, if armed { 128 } else { 0 }, 3, 3], 1)
    }
    fn uid(value: u64) -> Vec<u8> {
        let mut payload = vec![0; 60];
        payload[8..16].copy_from_slice(&value.to_le_bytes());
        unsigned(148, &payload, 1)
    }
    fn boot(value: u32) -> Vec<u8> {
        let mut payload = vec![0; 12];
        payload[8..12].copy_from_slice(&value.to_le_bytes());
        unsigned(2, &payload, 1)
    }
    fn passive() -> PassiveHeartbeat {
        PassiveHeartbeat::new(
            crate::enrollment::ObserverConfig {
                local_address: "127.0.0.1".parse().unwrap(),
                port: 14552,
                sender: "10.0.0.2".parse().unwrap(),
                system_id: 1,
                component_id: 1,
                expected_uid: Some(42),
                require_signed: false,
                signing_key_path: None,
            },
            None,
        )
        .unwrap()
    }
    fn feed(parser: &mut PassiveHeartbeat, bytes: &[u8], now: u64) -> Vec<FlightState> {
        parser.observe_all(bytes, "10.0.0.2".parse().unwrap(), now)
    }
    fn established(parser: &mut PassiveHeartbeat) {
        assert_eq!(
            feed(
                parser,
                &[uid(42), boot(1000), heartbeat(false)].concat(),
                1_000_000
            ),
            vec![FlightState::Disarmed]
        );
    }
    #[test]
    fn heartbeat_alone_never_claims_verified_aircraft() {
        let mut p = passive();
        assert_eq!(
            feed(&mut p, &heartbeat(false), 1_000_000),
            vec![FlightState::Unknown]
        );
        assert_eq!(
            feed(&mut p, &[uid(42), heartbeat(false)].concat(), 1_000_000),
            vec![FlightState::Unknown]
        );
    }
    #[test]
    fn crc_valid_uid_system_and_reboot_contradictions_revoke_immediately() {
        for contradiction in [
            uid(43),
            boot(1),
            unsigned(0, &[0, 0, 0, 0, 2, 3, 0, 3, 3], 2),
        ] {
            let mut p = passive();
            established(&mut p);
            let result = feed(
                &mut p,
                &[contradiction, heartbeat(false)].concat(),
                1_100_000,
            );
            assert!(!result.is_empty());
            assert!(result.iter().all(|v| *v == FlightState::Unknown));
        }
    }
    #[test]
    fn every_arm_transition_in_one_datagram_advances_generation() {
        let mut p = passive();
        established(&mut p);
        let states = feed(
            &mut p,
            &[heartbeat(true), heartbeat(false)].concat(),
            1_100_000,
        );
        assert_eq!(states, vec![FlightState::Armed, FlightState::Disarmed]);
        let mut observer = NativeObserver::unavailable(Instant::now());
        observer.accept(FlightState::Disarmed).unwrap();
        let before = observer.observe().unwrap().generation;
        for state in states {
            observer.accept(state).unwrap()
        }
        assert_eq!(observer.observe().unwrap().generation, before + 2);
    }
    #[test]
    fn stale_boot_evidence_and_unknown_worker_census_block() {
        let mut p = passive();
        established(&mut p);
        assert_eq!(
            feed(&mut p, &heartbeat(false), 4_000_001),
            vec![FlightState::Unknown]
        );
        let mut observer = NativeObserver::unavailable(Instant::now());
        observer.accept(FlightState::Disarmed).unwrap();
        observer.recording_lease(false).unwrap();
        observer.set_recording_guard(Arc::new(|| false));
        assert!(observer.observe().unwrap().recording_active);
        assert_eq!(observer.public_safety()["allowed"], false);
    }
}
