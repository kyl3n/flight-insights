"""Versioned network reference profiles with separate observation evidence.

Revision 2 is user-supplied design evidence, not enrollment, device discovery,
permission to change the host, or proof of the current receiver configuration.
"""
from __future__ import annotations

import copy
import hashlib
import json


LAYERS = ("interface", "address_and_route", "service", "packet_arrival",
          "protocol_decode", "receiving_application", "freshness_and_quality")

_Q22 = {
    "profile_id": "q22",
    "profile_version": "q22.network.revision-2",
    "label": "Q22 network reference — revision 2",
    "status": "reference_not_enrolled",
    "origin": {"kind": "user_supplied_handoff", "revision": "2.0", "date": "2026-10-12",
               "contract": "Q22_Implementation_Contract.json", "section": "network_contract.q22_profile"},
    "evidence_state": "handoff_declared",
    "host": {"drone_ipv4": "10.0.0.5", "prefix_length": 24, "expected_interface_role": "dedicated_wired",
             "interface_stable_id": None, "interface_name": None, "default_gateway": None, "dns": None},
    "ground_radio": {"role": "skypulse_ground", "ipv4": "10.0.0.2", "firmware": None,
                     "read_only_paths": ["/scatter_data.json", "/events.json"],
                     "configured_stream_destination": "10.0.0.5"},
    "camera": {"model": "SIYI A8 Mini", "ipv4": None, "credentials_reference": None,
               "address_state": "unknown", "direct_host_reachability": "unknown"},
    "streams": [
        {"role": "video", "protocol": "udp", "destination_ipv4": "10.0.0.5", "destination_port": 5600,
         "expected_codec": "H265", "codec_evidence": "handoff_declared", "observed_codec": None,
         "expected_source_ipv4": "10.0.0.2", "observed_source_ipv4": None,
         "receiver": "selected_gcs", "app_may_bind_direct_port": False},
        {"role": "gcs_telemetry", "protocol": "udp", "destination_ipv4": "10.0.0.5", "destination_port": 14550,
         "expected_format": "mavlink", "expected_source_ipv4": "10.0.0.2", "observed_source_ipv4": None,
         "receiver": "selected_gcs", "app_may_bind_direct_port": False},
        {"role": "app_telemetry_observer", "protocol": "udp", "destination_ipv4": None, "destination_port": None,
         "expected_format": "mavlink", "expected_source_ipv4": None, "observed_source_ipv4": None,
         "receiver": "app_recorder", "state": "requires_enrolled_forwarding_endpoint"},
    ],
    "windows_firewall_profile": {
        "id": "q22-wired-skypulse-udp-any-profile", "state": "handoff_declared_not_observed",
        "direction": "inbound", "protocol": "udp", "local_ports": [5600, 14550],
        "interface_type": "wired", "profiles": "Any", "remote_addresses": ["10.0.0.2"],
        "local_addresses": "Any", "receiving_program": None,
        "replacement_policy": "Preserve the working rule. A narrower program/local-address proposal requires separate review and verification.",
    },
    "gcs_profiles": {
        "mission_planner": {
            "runtime_profile": "system-wide MinGW GStreamer", "runtime_evidence": "handoff_declared",
            "loaded_runtime_path": None, "loaded_runtime_version": None, "process_architecture": None,
            "video_mode": "H265 AutoConnect / HUD",
            "expected_pipeline": ["udpsrc", "rtpjitterbuffer", "rtph265depay", "h265parse", "d3d11h265dec",
                                  "d3d11download", "bounded leaky queue", "videoconvert", "BGRA", "appsink"],
            "historical_jitter_buffer_ms": [50, 60], "selected_jitter_buffer_ms": None,
            "forwarding_capability": "version_and_settings_specific_not_validated",
            "parameter_request_path": "not_validated", "log_download_path": "not_validated",
        },
        "qgroundcontrol": {
            "runtime_profile": "QGroundControl-owned video/runtime settings", "runtime_evidence": "not_observed",
            "inherits_mission_planner_runtime": False, "forwarding_capability": "one_way_observer_only",
            "forwarded_parameter_requests": "unsupported", "forwarded_log_requests": "unsupported",
            "native_tlog": "complementary_artifact_required_for_gcs_command_coverage",
        },
    },
    "policies": {
        "preserve_internet_default_route_and_dns": True, "add_drone_default_gateway": False,
        "bridge_or_nat": False, "send_connectivity_test_mavlink": False,
        "learned_endpoints_replace_profile": False, "gcs_choice_when_both_running": "requires_intended_connection_evidence",
    },
    "limitations": [
        "This reference preserves the supplied Q22 decisions; it is not approval to apply them to this computer.",
        "Inspect live source/destination and selected interface before enrolling the profile; a proxy/router can change source addresses.",
        "A radio management response does not prove video delivery or transparent IP access to the airborne camera.",
        "The radio may learn its destination from received MAVLink; diagnostics must not send test heartbeats.",
        "The application observer endpoint is unconfigured; do not take over the GCS ports 14550 or 5600.",
        "The expected codec and runtime are historical configuration evidence, not currently observed decode capability.",
    ],
}


def reference_profile(profile_id: str = "q22") -> dict:
    if profile_id != "q22":
        raise ValueError("No bundled reference profile exists for that ID. Other aircraft require an enrolled profile.")
    profile = copy.deepcopy(_Q22)
    profile["reference_fingerprint"] = hashlib.sha256(json.dumps(_Q22, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return profile


def capability_matrix(platform: str) -> dict:
    platforms = {}
    for system in ("Windows", "Linux", "Darwin"):
        reason = {
            "Windows": "A qualified signed repair broker and supervised rollback are not integrated; Python PowerShell previews are not a qualified privilege boundary.",
            "Linux": "NetworkManager checkpoints and manager-owned address/route updates are not integrated; transient ip commands can be overwritten by the active manager.",
            "Darwin": "Signed helper and SystemConfiguration network-service updates are not integrated; Local Network privacy is a separate OS permission.",
        }[system]
        platforms[system] = {
            "inspection": {"state": "supported_automatic", "scope": "implemented configured IPv4 interface/route inspection; no target packets"},
            "stream_probes": {"state": "supported_automatic", "scope": "bounded explicit HTTP, RTSP description and dedicated UDP receiver probes"},
            "repair": {"state": "diagnosis_only", "automatic_apply": False, "reason": reason},
            "qualification": "Implementation and fixture evidence do not establish installed hardware/topology qualification.",
        }
    return {"current_platform": platform, "platforms": platforms, "required_platforms": ["Windows", "Linux", "Darwin"],
            "layers": [
                {"layer": "interface", "state": "supported_automatic", "coverage": "Link state and IPv4 addresses; stable enrollment and manager ownership require additional evidence."},
                {"layer": "address_and_route", "state": "supported_automatic", "coverage": "OS route/source selection; return-path and DNS preservation are not independently verified."},
                {"layer": "service", "state": "supported_automatic", "coverage": "Explicit configured SkyPulse GET/RTSP DESCRIBE; no radio writes or guessed camera addresses."},
                {"layer": "packet_arrival", "state": "diagnosis_only", "coverage": "Exclusive dedicated observer socket only; use existing recorder counters when recording."},
                {"layer": "protocol_decode", "state": "diagnosis_only", "coverage": "Probe checks heartbeat CRC/SDP; it does not establish aircraft identity, signing authenticity or decoded video."},
                {"layer": "receiving_application", "state": "unknown", "coverage": "Actual GCS socket ownership, loaded decoder/runtime and frame consumption are not inspected yet."},
                {"layer": "freshness_and_quality", "state": "diagnosis_only", "coverage": "Bounded probes are not continuous stream/flight-state monitoring."},
            ], "limitations": ["Capabilities describe implemented operations, not airworthiness or permission to change this host.",
                                "QGC one-way forwarding supports observation, not parameter/log requests back to the aircraft."]}


def compare_reference(profile: dict, inspection: dict) -> dict:
    """Compare declared expectations to existing host observations, without probes."""
    host = profile["host"]
    matches = [(interface, address) for interface in inspection.get("interfaces", []) for address in interface.get("ipv4", [])
               if address.get("address") == host["drone_ipv4"]]
    observations = {"expected_drone_ipv4": host["drone_ipv4"], "expected_prefix": host["prefix_length"],
                    "matching_interfaces": [i["name"] for i, _ in matches],
                    "address_state": "unknown" if not inspection.get("interfaces") else "not_observed" if not matches else "observed" if any(a["prefix"] == host["prefix_length"] and i["up"] for i, a in matches) else "mismatch",
                    "configured_stream_destination": "not_observed", "packet_source": "not_observed",
                    "gcs_runtime": "not_observed", "firewall_policy": "unknown", "video_decode": "not_observed"}
    findings = []
    if observations["address_state"] in {"not_observed", "mismatch"}:
        findings.append({"code": "q22_reference_address_unconfirmed", "state": observations["address_state"],
                         "message": "The supplied Q22 reference expects an active drone adapter at 10.0.0.5/24. Inspect and enroll the actual topology before changing it."})
    if len(matches) > 1:
        findings.append({"code": "q22_duplicate_local_address", "state": "attention", "message": "More than one interface has the reference address; source/interface selection needs review."})
    defaults = {r.get("interface") for r in inspection.get("default_routes", [])}
    if any(i["name"] in defaults for i, _ in matches):
        findings.append({"code": "q22_drone_interface_default_route", "state": "attention", "message": "An interface with the reference drone address also carries a default route. Determine its role; do not remove an Internet route automatically."})
    return {"reference": profile, "observations": observations, "findings": findings,
            "observations_override_reference": True, "profile_changed": False}
