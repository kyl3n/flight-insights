"""Bounded, configured host diagnostics. No discovery or aircraft commands.

Typed recovery receipts preserve original baselines and uncertain outcomes.
Generating/recovering a plan never runs it; privileged actions need confirmation.
"""
from __future__ import annotations

import copy
import base64
import http.client
import ipaddress
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import time
import uuid
from typing import Any, Callable

from .network_journal import RepairJournal
from .rtp_diagnostics import RtpDiagnostics
from .network_profiles import capability_matrix, compare_reference, reference_profile


def _executable(name: str) -> str | None:
    """Resolve fixed OS tools even when a desktop launcher supplies a sparse PATH."""
    if name == "powershell.exe" and platform.system() == "Windows":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(candidate):
            return candidate
    resolved = shutil.which(name)
    if resolved:
        return resolved
    candidates = {"ip": ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip"),
                  "firewall-cmd": ("/usr/bin/firewall-cmd", "/bin/firewall-cmd"),
                  "netstat": ("/usr/sbin/netstat", "/usr/bin/netstat"),
                  "route": ("/sbin/route", "/usr/sbin/route"),
                  "ifconfig": ("/sbin/ifconfig", "/usr/sbin/ifconfig")}
    return next((path for path in candidates.get(name, ()) if os.path.isfile(path) and os.access(path, os.X_OK)), None)


def _run(argv: list[str]) -> str:
    # Queries should not accumulate unbounded latency across configured targets.
    mutation = any(token in {"add", "del", "delete", "alias", "-alias"} for token in argv) or any("New-Net" in token or "Remove-Net" in token or "--add-rich-rule=" in token or "--remove-rich-rule=" in token for token in argv)
    windows_shell = argv[0] == "powershell.exe"
    command = [_executable(argv[0]) or argv[0], *argv[1:]]
    # Windows PowerShell + NetTCPIP module cold startup often exceeds two seconds.
    # _ps explicitly selects UTF-8 so localized adapter aliases round-trip intact.
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=8 if mutation else 6 if windows_shell else 2, shell=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise OSError((result.stderr or result.stdout or "Command failed").strip()[:1000])
    return result.stdout


def _ip(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter a numeric IPv4 address.")
    address = ipaddress.IPv4Address(value)
    if address.is_multicast or address.is_unspecified or str(address) == "255.255.255.255":
        raise ValueError("Configure one unicast IPv4 address, not a subnet or broadcast.")
    return str(address)


def _integer(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return value


def _source(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("A configured source is required.")
    result = dict(value)
    result["host"] = _ip(value.get("host"))
    if value.get("kind") not in {"skypulse", "rtsp", "udp"}:
        raise ValueError("Source kind must be skypulse, rtsp or udp.")
    result["id"] = str(value.get("id", value["kind"]))[:120]
    result["port"] = _integer(value.get("port", {"skypulse": 80, "rtsp": 554, "udp": 14551}[value["kind"]]), 1, 65535, "Port")
    if value.get("local_address"):
        result["local_address"] = _ip(value["local_address"])
    path = value.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in path):
        raise ValueError("RTSP path must start with / and contain no whitespace or control characters.")
    result["path"] = path
    if value.get("expected_format", "unknown") not in {"unknown", "mavlink", "rtp"}:
        raise ValueError("Expected UDP format must be mavlink, rtp or unknown.")
    if value.get("rtp_codec", "unknown") not in {"unknown", "h265"}:
        raise ValueError("RTP codec must be unknown or explicitly configured h265.")
    if any(key in value for key in ("rtp_codec", "rtp_payload_type", "h265_donl")):
        if value["kind"] != "udp" or value.get("expected_format") != "rtp":
            raise ValueError("RTP mapping fields require a UDP source with expected_format rtp.")
        if value.get("rtp_codec", "unknown") == "h265":
            payload_type = _integer(value.get("rtp_payload_type"), 0, 127, "Configured RTP payload type")
            if 64 <= payload_type <= 95:
                raise ValueError("RTP payload types 64..95 are ambiguous with RTCP multiplexing; use the actual supported session mapping.")
            if value.get("h265_donl") is not False:
                raise ValueError("H.265 framing requires explicit h265_donl=false from the configured session; DONL-present payloads are not implemented.")
        elif any(key in value for key in ("rtp_payload_type", "h265_donl")):
            raise ValueError("Codec-specific mapping fields require rtp_codec h265.")
    return result


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _json_output(value: str) -> Any:
    return json.loads(value.lstrip("\ufeff \t\r\n").strip() or "[]")


def _ps(script: str) -> list[str]:
    setup = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
             "$WarningPreference='SilentlyContinue'; $InformationPreference='SilentlyContinue'; "
             "$utf8 = New-Object System.Text.UTF8Encoding($false); "
             "[Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; ")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", setup + script]


def _mac_destination(value: str) -> str:
    """netstat abbreviates both classful networks and CIDR prefixes on Darwin."""
    if value == "default":
        return "0.0.0.0/0"
    address, separator, prefix = value.partition("/")
    parts = address.split(".")
    full = ".".join(parts + ["0"] * (4 - len(parts)))
    return str(ipaddress.IPv4Network(f"{full}/{prefix if separator else len(parts) * 8}", strict=False))


class NetworkService:
    """One instance per engine session; injectable host boundaries aid testing."""

    def __init__(self, data_dir: Path | None = None, *, system: str | None = None, runner: Callable = _run,
                 interface_provider: Callable | None = None, elevated: Callable | None = None,
                 safety_state: Callable | None = None, repair_backend: Any = None):
        self.system = system or platform.system()
        self.run = runner
        self.interface_provider = interface_provider
        self.elevated = elevated or self._is_elevated
        # These boundaries are engine-owned and never accepted through RPC.
        self.safety_state = safety_state
        self.repair_backend = repair_backend
        self.max_heartbeat_age_s = 3.0
        self.plans: dict[str, dict] = {}
        self.journal = None
        self.journal_error = None
        if data_dir is not None:
            try:
                self.journal = RepairJournal(Path(data_dir))
            except (OSError, ValueError) as exc:
                self.journal_error = str(exc)

    def dispatch(self, method: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("Network parameters must be an object.")
        handler = {"network.inspect": self.inspect, "network.probe": self.probe,
                   "network.plan": self.plan, "network.apply": self.apply,
                   "network.rollback": self.rollback, "network.recovery": self.recovery,
                   "network.profile": self.profile, "network.capabilities": self.capabilities}.get(method)
        if handler is None:
            raise ValueError("Unknown network method.")
        return handler(params)

    def profile(self, params: dict) -> dict:
        if set(params) - {"profile_id"}:
            raise ValueError("Profile lookup only accepts profile_id; it does not enroll or change a profile.")
        return reference_profile(params.get("profile_id", "q22"))

    def _safety_snapshot(self) -> dict:
        if self.safety_state is None:
            return {"flight_state": "unknown", "heartbeat_age_s": None, "identity_verified": False,
                    "recording_active": None, "owned_udp": [], "ownership_available": False}
        try:
            value = self.safety_state()
            if not isinstance(value, dict):
                raise ValueError("Invalid safety observer response")
            value = copy.deepcopy(value)
            value["ownership_available"] = isinstance(value.get("owned_udp"), list)
            return value
        except Exception:
            # Observer failure cannot turn silence into disarmed or a free socket.
            return {"flight_state": "unknown", "heartbeat_age_s": None, "identity_verified": False,
                    "recording_active": None, "owned_udp": [], "ownership_available": False}

    def _repair_safety(self) -> dict:
        observed = self._safety_snapshot()
        flight_state = observed.get("flight_state", "unknown")
        if not isinstance(flight_state, str) or flight_state not in {"armed", "disarmed", "unknown"}:
            flight_state = "unknown"
        age = observed.get("heartbeat_age_s")
        try:
            age = age if type(age) in {int, float} and math.isfinite(age) else None
        except (OverflowError, ValueError):
            age = None
        fresh = age is not None and 0 <= age <= self.max_heartbeat_age_s
        reasons = []
        if flight_state != "disarmed":
            reasons.append("A current disarmed observation is required; armed or unknown state blocks host changes.")
        if not fresh:
            reasons.append("The selected aircraft heartbeat is missing or older than three seconds.")
        if observed.get("identity_verified") is not True:
            reasons.append("The engine has not verified the selected aircraft identity.")
        if observed.get("recording_active") is not False:
            reasons.append("Managed recording must be confirmed inactive before a disruptive host change.")
        return {"allowed": not reasons, "state": "allowed" if not reasons else "blocked", "reasons": reasons,
                "flight_state": flight_state, "heartbeat_age_s": age,
                "identity_verified": observed.get("identity_verified") is True,
                "recording_active": observed.get("recording_active") if type(observed.get("recording_active")) is bool else None,
                "max_heartbeat_age_s": self.max_heartbeat_age_s}

    def _execution_capability(self) -> dict:
        fallback = capability_matrix(self.system)["platforms"].get(self.system, {}).get("repair", {
            "state": "unsupported", "automatic_apply": False, "reason": "No repair implementation exists for this operating system."})
        backend = self.repair_backend
        if backend is None:
            return fallback
        try:
            flags = backend.capabilities()
            qualified = (isinstance(flags, dict) and flags.get("platform") == self.system
                         and flags.get("state") == "supported_automatic"
                         and all(flags.get(k) is True for k in ("qualified", "signed_helper", "manager_aware", "supervised_rollback"))
                         and callable(getattr(backend, "execute", None)))
            if qualified:
                return {"state": "supported_automatic", "automatic_apply": True,
                        "reason": "Engine-configured backend declares signed helper, manager-aware writes and supervised rollback; current safety and host-state checks still apply."}
        except Exception:
            pass
        return {**fallback, "reason": "The configured repair backend has not established all required native qualification capabilities."}

    def capabilities(self, params: dict) -> dict:
        if params:
            raise ValueError("Capability lookup accepts no parameters.")
        result = capability_matrix(self.system)
        if self.system in result["platforms"]:
            result["platforms"][self.system]["repair"] = self._execution_capability()
        result["repair_safety"] = self._repair_safety()
        result["repair_safety"]["atomic_revocation"] = False
        result["limitations"].append("Engine observations are rechecked around operations without locking the recorder. Python alone cannot atomically revoke an OS command after an aircraft-state transition; production automatic repair remains disabled without a qualified broker.")
        return result

    def _mutation_gate(self, plan: dict) -> dict | None:
        capability = self._execution_capability()
        safety = self._repair_safety()
        if not capability["automatic_apply"]:
            return {"plan_id": plan["plan_id"], "state": "diagnosis_only", "may_have_applied": plan["applied"],
                    "message": capability["reason"], "execution_capability": capability, "repair_safety": safety}
        if not safety["allowed"]:
            return {"plan_id": plan["plan_id"], "state": "safety_blocked", "may_have_applied": plan["applied"],
                    "message": " ".join(safety["reasons"]), "repair_safety": safety}
        return None

    def _udp_ownership(self, local: str, port: int) -> dict | None:
        observed = self._safety_snapshot()
        if not observed.get("ownership_available"):
            return {"state": "recorder_state_unknown", "checks": [],
                    "details": "Recorder socket ownership is unavailable; this UDP probe was deferred.",
                    "recovery_action": "Read session.status and restore recorder status before probing a dedicated observer endpoint."}
        for binding in observed["owned_udp"]:
            if (not isinstance(binding, dict) or type(binding.get("port")) is not int
                    or not 1 <= binding["port"] <= 65535 or not isinstance(binding.get("local_address"), str)):
                return {"state": "recorder_state_unknown", "checks": [], "details": "Recorder ownership information is incomplete; no diagnostic socket was opened."}
            try:
                address = ipaddress.ip_address(binding["local_address"])
                owned_address = str(getattr(address, "ipv4_mapped", None) or address)
            except ValueError:
                return {"state": "recorder_state_unknown", "checks": [], "details": "Recorder ownership information is incomplete; no diagnostic socket was opened."}
            if binding["port"] == port and (owned_address in {local, "0.0.0.0", "::"} or local in {"0.0.0.0", "::"}):
                return {"state": "recorder_owned", "checks": [],
                        "details": "The managed recorder owns or has reserved this UDP endpoint. Diagnostics use its counters and readiness instead of competing for its socket.",
                        "recovery_action": "Read session.status for recorder counters, heartbeat freshness and source readiness."}
        return None

    def _sources(self, params: dict) -> list[dict]:
        values = params.get("sources", [])
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError("Configure at most eight individual sources per check.")
        sources = [_source(value) for value in values]
        if len({s["id"] for s in sources}) != len(sources):
            raise ValueError("Source IDs must be unique.")
        return sources

    def _interfaces(self) -> list[dict]:
        if self.interface_provider:
            return copy.deepcopy(self.interface_provider())
        import psutil
        stats = psutil.net_if_stats()
        result = []
        for name, addresses in psutil.net_if_addrs().items():
            ipv4 = []
            for address in addresses:
                if address.family == socket.AF_INET and address.netmask:
                    subnet = ipaddress.IPv4Interface(f"{address.address}/{address.netmask}")
                    ipv4.append({"address": address.address, "prefix": subnet.network.prefixlen, "network": str(subnet.network)})
            try:
                index = socket.if_nametoindex(name)
            except OSError:
                index = None
            result.append({"name": name, "index": index, "up": bool(stats.get(name) and stats[name].isup), "ipv4": ipv4})
        if self.system == "Windows":
            # psutil uses friendly adapter names; get the Windows numeric indices.
            try:
                rows = _as_list(_json_output(self.run(_ps("Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias,InterfaceIndex,Dhcp | ConvertTo-Json -Compress"))))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # Keep useful addresses for probes, while blocking repairs that
                # require complete host visibility until supplemental data works.
                for interface in result:
                    interface["inspection_warning"] = "Windows interface-index/DHCP query unavailable: " + str(exc)
                return result
            for interface in result:
                match = next((r for r in rows if r["InterfaceAlias"] == interface["name"]), None)
                if match:
                    interface["index"] = int(match["InterfaceIndex"])
                    interface["dhcp"] = match.get("Dhcp")
        return result

    def _routes(self) -> list[dict]:
        if self.system == "Linux":
            rows = json.loads(self.run(["ip", "-j", "-4", "-N", "route", "show", "table", "all"]))
            return [{"destination": "0.0.0.0/0" if r.get("dst") == "default" else str(ipaddress.IPv4Network(r.get("dst", "0.0.0.0/0"), strict=False)),
                     "interface": r.get("dev"), "gateway": r.get("gateway"), "metric": r.get("metric", 0),
                     "source": r.get("prefsrc"), "protocol": str(r.get("protocol", "")), "table": "main" if str(r.get("table", "main")) in {"main", "254"} else str(r["table"]),
                     "type": r.get("type", "unicast")} for r in rows]
        if self.system == "Windows":
            rows = _as_list(_json_output(self.run(_ps("Get-NetRoute -AddressFamily IPv4 | Select-Object DestinationPrefix,InterfaceAlias,InterfaceIndex,NextHop,RouteMetric | ConvertTo-Json -Compress"))))
            return [{"destination": r["DestinationPrefix"], "interface": r["InterfaceAlias"], "index": r["InterfaceIndex"], "gateway": r["NextHop"], "metric": r["RouteMetric"]} for r in rows]
        if self.system == "Darwin":
            rows = []
            interface_column = 3
            for line in self.run(["netstat", "-rn", "-f", "inet"]).splitlines():
                fields = line.split()
                if fields and fields[0] == "Destination":
                    # macOS releases differ in whether Refs/Use/MTU columns are
                    # present. Locate Netif by its header, not a fixed position.
                    interface_column = next((i for i, field in enumerate(fields) if field in {"Netif", "Iface"}), 3)
                    continue
                if len(fields) <= interface_column or not (fields[0] == "default" or fields[0][0].isdigit()):
                    continue
                rows.append({"destination": _mac_destination(fields[0]), "gateway": fields[1], "interface": fields[interface_column], "flags": fields[2]})
            return rows
        raise OSError(f"Host inspection is unsupported on {self.system}.")

    def _route_to(self, host: str) -> dict:
        if self.system == "Linux":
            rows = json.loads(self.run(["ip", "-j", "-4", "route", "get", host]))
            row = rows[0]
            return {"interface": row.get("dev"), "local_address": row.get("prefsrc", row.get("src")), "gateway": row.get("gateway"), "type": row.get("type", "unicast")}
        if self.system == "Windows":
            # Only validated IPv4 text reaches this fixed PowerShell expression.
            rows = _as_list(_json_output(self.run(_ps(f"Find-NetRoute -RemoteIPAddress '{host}' | Select-Object InterfaceAlias,IPAddress,NextHop | ConvertTo-Json -Compress"))))
            return {"interface": next((r.get("InterfaceAlias") for r in rows if r.get("InterfaceAlias")), None),
                    "local_address": next((r.get("IPAddress") for r in rows if r.get("IPAddress")), None),
                    "gateway": next((r.get("NextHop") for r in rows if r.get("NextHop")), None)}
        if self.system == "Darwin":
            row = dict(line.strip().split(":", 1) for line in self.run(["route", "-n", "get", host]).splitlines() if ":" in line)
            result = {"interface": row.get("interface", "").strip(), "gateway": row.get("gateway", "").strip(), "local_address": None}
            # Darwin route(8) reports the interface but not the selected source.
            # Connecting UDP only performs local route/source selection. There
            # is intentionally no send/sendto, bind to a GCS port or remote probe.
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                    connection.settimeout(0.5)
                    connection.connect((host, 9))
                    result["local_address"] = _ip(connection.getsockname()[0])
            except (OSError, ValueError) as exc:
                result["source_warning"] = "Kernel source-address selection unavailable: " + str(exc)
            flags = row.get("flags", "")
            if re.search(r"\b(REJECT|BLACKHOLE)\b", flags):
                result["type"] = "unreachable"
            return result
        raise OSError("No route query is supported on this OS.")

    def inspect(self, params: dict) -> dict:
        sources = self._sources(params)
        limitations = ["Firewall permission is unknown; a failed probe does not prove a blocked port.",
                       "Host routing cannot establish the device's return route or sender destination."]
        try:
            interfaces = self._interfaces()
        except (OSError, ImportError, ValueError, subprocess.SubprocessError) as exc:
            interfaces = []
            limitations.append("Interface inspection unavailable: " + str(exc))
        interface_warnings = sorted({i["inspection_warning"] for i in interfaces if i.get("inspection_warning")})
        limitations.extend(interface_warnings)
        try:
            routes = self._routes()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            routes = []
            limitations.append("Route inspection unavailable: " + str(exc))
        targets = []
        for source in sources:
            target = {"source_id": source["id"], "host": source["host"], "state": "unknown", "selected_interface": None, "local_address": None, "local_address_state": "unknown", "issues": []}
            try:
                route = self._route_to(source["host"])
                target.update(selected_interface=route.get("interface"), local_address=route.get("local_address"), gateway=route.get("gateway"))
                if route.get("local_address"):
                    target["local_address_state"] = "os_selected"
                if route.get("source_warning"):
                    target["issues"].append(route["source_warning"])
                iface = next((i for i in interfaces if i["name"] == route.get("interface")), None)
                matching = [i for i in interfaces if i["up"] and any(ipaddress.IPv4Address(source["host"]) in ipaddress.IPv4Network(a["network"]) for a in i["ipv4"])]
                if route.get("type", "unicast") not in {"unicast", "local"} or not route.get("interface"):
                    target["issues"].append("No usable unicast route was selected.")
                if source.get("interface") and source["interface"] != route.get("interface"):
                    target["issues"].append("The OS route uses a different interface from the configured device interface.")
                if matching and not any(i["name"] == route.get("interface") for i in matching):
                    target["issues"].append("The route does not use the directly connected device subnet.")
                if iface and not iface["up"]:
                    target["issues"].append("The selected interface is down.")
                if not matching:
                    target["issues"].append("No active local IPv4 address shares the device subnet; verify the gateway and device return path, or configure a dedicated adapter address.")
                if iface and target["local_address"] and not any(a["address"] == target["local_address"] for a in iface["ipv4"]):
                    target["issues"].append("The selected source address does not belong to the reported interface; routing may have changed during inspection.")
                    target["local_address_state"] = "inconsistent"
                if not target["local_address"]:
                    target["issues"].append("The OS-selected source address is unknown; choose an existing local IPv4 receive address explicitly before probing UDP.")
                if source.get("local_address") and target.get("local_address") != source["local_address"]:
                    target["issues"].append("The configured receive address differs from the OS-selected source address; verify the sender's destination.")
                target["state"] = "attention" if target["issues"] else "route_available"
            except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
                target["issues"].append("Route lookup unavailable: " + str(exc))
            targets.append(target)
        result = {"status": "inspected" if interfaces and routes and not interface_warnings else "partial", "platform": self.system,
                "interfaces": interfaces, "routes": routes, "default_routes": [r for r in routes if r["destination"] == "0.0.0.0/0"],
                "targets": targets, "firewall": {"state": "unknown", "reason": "Effective firewall policy depends on OS, permissions, active profile and other security software."},
                "limitations": limitations}
        if "profile_id" in params:
            result["profile_evidence"] = compare_reference(reference_profile(params["profile_id"]), result)
        return result

    def probe(self, params: dict) -> dict:
        sources = self._sources(params)
        timeout = _integer(params.get("timeout_ms", 1500), 100, 5000, "Probe timeout") / 1000
        result = self.inspect({"sources": sources})
        probes = []
        deadline = time.monotonic() + 20
        for source in sources:
            target = next(t for t in result["targets"] if t["source_id"] == source["id"])
            try:
                remaining = deadline - time.monotonic()
                # Reserve one bounded timeout for each of SkyPulse's two GETs.
                source_timeout = min(timeout, remaining / (2 if source["kind"] == "skypulse" else 1))
                if source_timeout < 0.05:
                    probe = {"state": "budget_exhausted", "checks": [], "details": "The 20-second total probe budget is exhausted. Run fewer configured sources together."}
                elif source["kind"] == "skypulse":
                    probe = self._http_probe(source, source_timeout)
                elif source["kind"] == "rtsp":
                    probe = self._rtsp_probe(source, source_timeout)
                else:
                    local = source.get("local_address") or target.get("local_address")
                    if not local or not any(a["address"] == local for i in result["interfaces"] for a in i["ipv4"]):
                        probe = {"state": "needs_configuration", "checks": [], "details": "Select a local interface IPv4 address for the dedicated forwarded UDP port."}
                    else:
                        probe = self._udp_ownership(local, source["port"]) or self._udp_probe(source, local, source_timeout)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                probe = {"state": "failed", "checks": [], "details": str(exc)[:1000]}
            probe.update(source_id=source["id"], recovery_action=probe.get("recovery_action", "Check device power/address, cable, configured source and return destination; compare routes before proposing a scoped repair."))
            probes.append(probe)
        result["probes"] = probes
        return result

    def _http_probe(self, source: dict, timeout: float) -> dict:
        checks = []
        for path in ("/scatter_data.json", "/events.json"):
            started = time.monotonic()
            try:
                # Read under an overall deadline, including slow-drip headers.
                # Connection: close bounds unknown/chunked body reads. Parsing
                # happens afterward in memory and never follows redirects.
                with socket.create_connection((source["host"], source["port"]), timeout=timeout) as connection:
                    connection.settimeout(max(0.001, timeout - (time.monotonic() - started)))
                    connection.sendall(f"GET {path} HTTP/1.1\r\nHost: {source['host']}:{source['port']}\r\nAccept: application/json\r\nConnection: close\r\n\r\n".encode("ascii"))
                    raw = bytearray()
                    limit = 1024 * 1024 + 65536
                    while len(raw) <= limit:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError("HTTP response exceeded the probe deadline.")
                        connection.settimeout(remaining)
                        chunk = connection.recv(min(65536, limit + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError("HTTP response exceeded the 1 MiB probe body limit.")
                class MemorySocket:
                    def makefile(self, *_args: Any, **_kwargs: Any) -> io.BytesIO:
                        return io.BytesIO(raw)
                reply = http.client.HTTPResponse(MemorySocket())
                reply.begin()
                content = reply.read(1024 * 1024 + 1)
                valid = False
                if reply.status == 200 and len(content) <= 1024 * 1024 and not reply.length:
                    try:
                        json.loads(content)
                        valid = True
                    except (ValueError, UnicodeError):
                        pass
                checks.append({"endpoint": path, "http_status": reply.status, "bytes": len(content), "state": "json_accessible" if valid else "unexpected_response", "schema_validated": False})
            except (OSError, ValueError, http.client.HTTPException) as exc:
                checks.append({"endpoint": path, "state": "failed", "error": str(exc)[:500]})
        return {"state": "reachable" if all(c["state"] == "json_accessible" for c in checks) else "partial" if any(c["state"] == "json_accessible" for c in checks) else "failed", "checks": checks,
                "details": "Read-only SkyPulse endpoint check. JSON availability does not validate vendor schema, radio health or rolling-history timestamps."}

    def _rtsp_probe(self, source: dict, timeout: float) -> dict:
        url = f"rtsp://{source['host']}:{source['port']}{source['path']}"
        started = time.monotonic()
        with socket.create_connection((source["host"], source["port"]), timeout=timeout) as connection:
            connection.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: Q22-Flight-Lab\r\n\r\n".encode("ascii"))
            raw = bytearray()
            required = None
            while len(raw) < 65536:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("RTSP response exceeded the probe deadline.")
                connection.settimeout(remaining)
                data = connection.recv(min(4096, 65536 - len(raw)))
                if not data:
                    break
                raw.extend(data)
                if b"\r\n\r\n" in raw and required is None:
                    headers, _ = raw.split(b"\r\n\r\n", 1)
                    length = re.search(br"(?im)^content-length:\s*(\d+)\s*$", headers)
                    required = len(headers) + 4 + (int(length[1]) if length else 0)
                if required is not None and len(raw) >= required:
                    break
        text = raw.decode("utf-8", errors="replace")
        match = re.match(r"RTSP/1\.0 (\d{3})", text)
        status = int(match[1]) if match else None
        headers, _, body = text.partition("\r\n\r\n")
        complete = required is not None and len(raw) >= required
        sdp = status == 200 and complete and bool(re.search(r"(?im)^content-type:\s*application/sdp(?:\s|;|$)", headers)) and body.startswith("v=0\r\n") and all(re.search(r"(?m)^" + prefix + r"=.+", body) for prefix in ("o", "s", "t", "m"))
        codecs = re.findall(r"(?m)^a=rtpmap:\d+ ([^\r\n]+)", body) if sdp else []
        return {"state": "capabilities_available" if sdp else "authentication_required" if status in {401, 403} else "unexpected_response",
                "checks": [{"protocol": "rtsp", "status": status, "sdp_structure_validated": sdp, "advertised_codecs": codecs, "media_received": False, "decoded": False}],
                "details": "DESCRIBE checks the configured RTSP URL without starting playback. Codec names come only from SDP. Actual video receipt, decode, resolution and end-to-end latency remain unverified."}

    @staticmethod
    def _heartbeat_valid(data: bytes) -> bool:
        """Validate only MAVLink common HEARTBEAT framing and X25+extra CRC.

        Other messages remain unvalidated, not reported as malformed telemetry.
        Signed packets' authentication is deliberately not claimed here.
        """
        for offset, magic in enumerate(data):
            if magic not in {0xFE, 0xFD}:
                continue
            # A heartbeat is at most 34 bytes including the MAVLink2 signature.
            # Do not copy every full tail of a maximum-size UDP datagram.
            chunk = data[offset:offset + 34]
            if len(chunk) >= 17 and chunk[0] == 0xFE and chunk[1] == 9 and chunk[5] == 0:
                end = 15
            elif len(chunk) >= 13 and chunk[0] == 0xFD and 1 <= chunk[1] <= 9 and chunk[2] & ~1 == 0 and chunk[7:10] == b"\0\0\0":
                end = 10 + chunk[1]
                if len(chunk) < end + 2 + (13 if chunk[2] & 1 else 0):
                    continue
            else:
                continue
            crc = 0xFFFF
            for byte in chunk[1:end] + bytes([50]):
                tmp = byte ^ (crc & 0xFF)
                tmp ^= (tmp << 4) & 0xFF
                crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
            if int.from_bytes(chunk[end:end + 2], "little") == crc:
                return True
        return False

    def _udp_probe(self, source: dict, local: str, timeout: float) -> dict:
        received = accepted = heartbeats = bytes_received = 0
        rtp = RtpDiagnostics(h265_payload_type=source.get("rtp_payload_type") if source.get("rtp_codec") == "h265" else None) if source.get("expected_format") == "rtp" else None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            if self.system == "Windows" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            # No SO_REUSEADDR / SO_REUSEPORT: never steal an existing GCS binding.
            try:
                connection.bind((local, source["port"]))
            except OSError as exc:
                return {"state": "bind_failed", "checks": [{"local_address": local, "port": source["port"]}], "details": str(exc),
                        "recovery_action": "Use a dedicated GCS/router forwarding destination. Keep the GCS connection running; do not share or take over its port."}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and received < 4096:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                try:
                    data, remote = connection.recvfrom(65535)
                except socket.timeout:
                    break
                received += 1
                if remote[0] != source["host"]:
                    continue
                accepted += 1
                bytes_received += len(data)
                if source.get("expected_format") == "mavlink" and self._heartbeat_valid(data):
                    heartbeats += 1
                if rtp is not None:
                    rtp.observe(data, remote[1], time.monotonic())
        result = {"state": "telemetry_validated" if heartbeats else "packets_received" if accepted else "no_packets",
                "checks": [{"local_address": local, "port": source["port"], "packets_from_configured_host": accepted,
                            "other_packets": received - accepted, "bytes": bytes_received, "valid_heartbeat_datagrams": heartbeats,
                            "authentication_verified": False, "video_decoded": False}],
                "details": "A valid heartbeat CRC establishes a telemetry packet, not sender authentication or full-stream continuity. Generic/RTP packets do not establish playable video. A timeout does not identify firewall failure."}
        if rtp is not None:
            result["checks"][0]["rtp"] = rtp.result()
            result["details"] = "Passive RTP headers, configured payload framing and local arrival cadence are separate from video decode. Sequence gaps do not prove RF loss; a timeout does not identify firewall failure."
        return result

    def _is_elevated(self) -> bool:
        if self.system == "Windows":
            try:
                import ctypes
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            except (AttributeError, OSError):
                return False
        return hasattr(os, "geteuid") and os.geteuid() == 0

    @staticmethod
    def _route_signature(routes: list[dict]) -> list[str]:
        return sorted(json.dumps(r, sort_keys=True) for r in routes)

    def plan(self, params: dict) -> dict:
        source = _source(params.get("source"))
        kind = params.get("kind")
        if kind not in {"host_route", "address_alias", "firewall_udp"}:
            raise ValueError("Unknown repair kind.")
        snapshot = self.inspect({"sources": [source]})
        if snapshot["status"] != "inspected":
            return {"state": "blocked", "title": "Host inspection is incomplete", "message": "Complete interface and route inspection is required before any repair. Install/enable the OS route inspection tools and retry.", "commands": [], "rollback_commands": [], "requires_elevation": True, "warnings": snapshot["limitations"]}
        interface = next((i for i in snapshot["interfaces"] if i["name"] == params.get("interface")), None)
        if not interface or not interface["up"]:
            raise ValueError("Choose an existing, active network interface.")
        # Interface names are used as argv data, never interpolated into a shell.
        name_valid = isinstance(interface["name"], str) and 1 <= len(interface["name"]) <= 100 and not interface["name"].startswith("-")
        if self.system == "Windows":
            name_valid = name_valid and all(ord(c) >= 32 and ord(c) != 127 for c in interface["name"])
        else:
            name_valid = name_valid and bool(re.fullmatch(r"[A-Za-z0-9_.: -]{1,100}", interface["name"]))
        if not name_valid:
            raise ValueError("This interface name is not supported by the repair adapter.")
        plan_id = uuid.uuid4().hex
        plan = {"plan_id": plan_id, "kind": kind, "state": "ready", "title": "", "commands": [], "rollback_commands": [],
                "requires_elevation": True, "warnings": ["Review this exact change before applying. The existing Internet default route is preserved.",
                    "Repairs affect this computer only; configure the device/GCS sender's return destination separately.",
                    "A durable receipt preserves rollback across engine restarts; recovering it never replays the repair." if self.journal else "Durable recovery is unavailable in this engine session. Roll back before closing the app."],
                "source": source, "interface": interface["name"], "created": time.monotonic(), "snapshot": snapshot,
                "applied": False, "guard": None, "expected": None}
        host, name = source["host"], interface["name"]
        local = params.get("local_address") or source.get("local_address")
        addresses = interface["ipv4"]
        matching = [a for a in addresses if ipaddress.IPv4Address(host) in ipaddress.IPv4Network(a["network"])]
        if kind == "host_route":
            plan["title"] = f"Route only {host}/32 through {name}"
            if any(r["destination"] in {host, host + "/32"} for r in snapshot["routes"]):
                return self._unavailable(plan, "not_needed", "An explicit host route already exists; inspect it manually instead of replacing it.")
            if not matching:
                # macOS aliases use /32 to avoid an implicit whole-subnet route.
                # Require an explicit verified device prefix before routing its
                # single host via that address; never infer one from the IP.
                if self.system == "Darwin" and params.get("prefix") is not None:
                    device_prefix = _integer(params["prefix"], 1, 30, "Verified device prefix")
                    matching = [a for a in addresses if a["prefix"] == 32 and ipaddress.IPv4Address(host) in ipaddress.IPv4Interface(f"{a['address']}/{device_prefix}").network]
                if not matching:
                    return self._unavailable(plan, "blocked", "Add a verified unused address in the device subnet first. For a macOS /32 alias, supply the verified device prefix when planning its host route.")
            local = _ip(local) if local else matching[0]["address"]
            if local not in [a["address"] for a in matching]:
                raise ValueError("Route source must be an existing interface address in the device subnet.")
            metric = 10000 + int(plan_id[:4], 16)
            if self.system == "Linux":
                suffix = [host + "/32", "dev", name, "src", local, "proto", "4", "metric", str(metric)]
                plan["commands"] = [["ip", "-4", "route", "add"] + suffix]
                plan["rollback_commands"] = [["ip", "-4", "route", "del"] + suffix]
                plan["expected"] = {"destination": host + "/32", "interface": name, "metric": metric, "source": local, "protocol": "4", "gateway": None, "table": "main", "type": "unicast"}
            elif self.system == "Windows" and type(interface.get("index")) is int:
                index = interface["index"]
                plan["commands"] = [_ps(f"New-NetRoute -DestinationPrefix '{host}/32' -InterfaceIndex {index} -NextHop '0.0.0.0' -RouteMetric {metric} -PolicyStore ActiveStore | Out-Null")]
                plan["rollback_commands"] = [_ps(f"Get-NetRoute -DestinationPrefix '{host}/32' -InterfaceIndex {index} -NextHop '0.0.0.0' -PolicyStore ActiveStore | Where-Object {{$_.RouteMetric -eq {metric}}} | Remove-NetRoute -Confirm:$false")]
                plan["expected"] = {"destination": host + "/32", "interface": name, "index": index, "metric": metric, "gateway": "0.0.0.0"}
            elif self.system == "Darwin":
                plan["commands"] = [["route", "-n", "add", "-host", host, "-interface", name, "-static", "-proto2"]]
                plan["rollback_commands"] = [["route", "-n", "delete", "-host", host, "-interface", name]]
                plan["expected"] = {"destination": host + "/32", "interface": name}
                plan["warnings"].append("macOS uses a static, marked route only for this /32 host. Automatic rollback requires its exact observed receipt. Native macOS qualification is pending.")
            else:
                return self._unavailable(plan, "unsupported", "The selected platform/interface lacks a supported route adapter.")
        elif kind == "address_alias":
            local = _ip(local)
            prefix = _integer(params.get("prefix", 24), 1, 32, "Prefix length")
            network = ipaddress.IPv4Interface(f"{local}/{prefix}").network
            if ipaddress.IPv4Address(host) not in network or local == host:
                raise ValueError("Choose a distinct local address in the device's verified subnet.")
            if ipaddress.IPv4Address(local) in {network.network_address, network.broadcast_address} and prefix < 31:
                raise ValueError("The local address cannot be the subnet's network or broadcast address.")
            if any(a["address"] == local for i in snapshot["interfaces"] for a in i["ipv4"]):
                return self._unavailable(plan, "not_needed", "That address already exists on this computer.")
            if self.system == "Darwin" and any(r["destination"] == local + "/32" for r in snapshot["routes"]):
                return self._unavailable(plan, "blocked", "A route already exists for the proposed local address. Review it before adding an alias.")
            if self.system not in {"Linux", "Darwin"}:
                return self._unavailable(plan, "unsupported", "Windows address creation can disable DHCP. Use OS settings for a dedicated adapter.")
            if self.system == "Linux" and len(name) > 9:
                return self._unavailable(plan, "unsupported", "The interface name leaves insufficient room for a unique Linux address ownership label. Configure this adapter in OS settings.")
            if any(r["interface"] == name for r in snapshot["default_routes"]):
                return self._unavailable(plan, "blocked", "Choose a dedicated device adapter, not an adapter carrying the Internet default route.")
            if any(ipaddress.IPv4Network(a["network"]).overlaps(network) for i in snapshot["interfaces"] if i["name"] != name for a in i["ipv4"]):
                return self._unavailable(plan, "blocked", "The proposed subnet overlaps another adapter; resolve the overlap manually.")
            plan["title"] = f"Add {local}/{prefix} to {name} without changing existing addresses"
            plan["warnings"].append("Confirm this static address is unused using the device/network administrator's allocation. This app does not sweep or prove address availability.")
            # noprefixroute avoids changing the route table; route planning is a separate reviewed step.
            label = name + ":q" + plan_id[:15 - len(name) - 2]
            plan["commands"] = [["ip", "-4", "address", "add", f"{local}/{prefix}", "dev", name, "label", label, "noprefixroute"]]
            plan["rollback_commands"] = [["ip", "-4", "address", "del", f"{local}/{prefix}", "dev", name, "label", label]]
            plan["expected"] = {"local": local, "prefixlen": prefix, "label": label}
            if self.system == "Darwin":
                plan["title"] = f"Add only {local}/32 to {name}; verified device subnet is {network}"
                plan["commands"] = [["ifconfig", name, "inet", local, "netmask", "255.255.255.255", "alias"]]
                plan["rollback_commands"] = [["ifconfig", name, "inet", local, "-alias"]]
                plan["expected"] = {"local": local, "prefixlen": 32}
                plan["warnings"].append("The macOS alias uses /32 to avoid adding a route for the entire device subnet. Next, separately review a host route for this configured device, supplying its verified subnet prefix. Rollback requires unchanged adapter address state. Native macOS qualification is pending.")
        else:
            if source["kind"] != "udp":
                raise ValueError("An inbound firewall repair requires a configured UDP receiver.")
            local = _ip(local)
            if local not in [a["address"] for a in addresses]:
                raise ValueError("Firewall destination must be an existing address on the selected interface.")
            port = source["port"]
            plan["title"] = f"Allow UDP from {host} to {local}:{port} on {name}"
            plan["warnings"].append("Effective firewall policy is unknown. This scoped exception may not override an explicit deny or third-party firewall; re-probe after applying.")
            if self.system == "Windows":
                rule_name = "Q22-" + plan_id
                plan["warnings"].append("Windows stores this named firewall exception persistently until it is rolled back or removed in Windows Firewall. Record the exact rule name before closing the app.")
                # PowerShell treats some Unicode smart quotes as delimiters.
                # Encode only the alias data, not code, into an ASCII literal and
                # decode with a fixed expression so every localized name is safe.
                encoded_name = base64.b64encode(name.encode("utf-8")).decode("ascii")
                alias_expression = f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_name}')))"
                plan["commands"] = [_ps(f"New-NetFirewallRule -Name '{rule_name}' -DisplayName '{rule_name}' -Direction Inbound -Action Allow -Protocol UDP -LocalPort {port} -RemoteAddress '{host}' -LocalAddress '{local}' -InterfaceAlias {alias_expression} -Profile Any -PolicyStore PersistentStore | Out-Null")]
                plan["rollback_commands"] = [_ps(f"Remove-NetFirewallRule -Name '{rule_name}' -PolicyStore PersistentStore")]
                plan["guard"] = _ps(f"$r = Get-NetFirewallRule -PolicyStore PersistentStore | Where-Object {{$_.Name -eq '{rule_name}'}}; if ($r) {{ @{{rule=($r | Select-Object Name,Direction,Action,Enabled,Profile); port=($r | Get-NetFirewallPortFilter | Select-Object Protocol,LocalPort,RemotePort); address=($r | Get-NetFirewallAddressFilter | Select-Object LocalAddress,RemoteAddress); interface=($r | Get-NetFirewallInterfaceFilter | Select-Object InterfaceAlias)}} | ConvertTo-Json -Depth 5 -Compress }}")
            elif self.system == "Linux" and _executable("firewall-cmd"):
                try:
                    zone = self.run(["firewall-cmd", "--get-zone-of-interface=" + name]).strip()
                except OSError:
                    zone = ""
                if not re.fullmatch(r"[A-Za-z0-9_-]+", zone) or zone == "no":
                    return self._unavailable(plan, "blocked", "firewalld must report the configured interface's active zone.")
                try:
                    members = self.run(["firewall-cmd", "--zone=" + zone, "--list-interfaces"]).split()
                    zone_sources = self.run(["firewall-cmd", "--zone=" + zone, "--list-sources"]).split()
                except (OSError, subprocess.SubprocessError) as exc:
                    return self._unavailable(plan, "blocked", "Cannot establish firewalld zone scope: " + str(exc))
                if members != [name] or zone_sources:
                    return self._unavailable(plan, "blocked", "A scoped automatic rule requires a firewalld zone containing only the selected interface and no source-based memberships. Zone membership will not be changed.")
                plan["zone"] = zone
                rule = f'rule family="ipv4" source address="{host}/32" destination address="{local}/32" port port="{port}" protocol="udp" accept'
                plan["commands"] = [["firewall-cmd", "--zone=" + zone, "--add-rich-rule=" + rule]]
                plan["rollback_commands"] = [["firewall-cmd", "--zone=" + zone, "--remove-rich-rule=" + rule]]
                plan["guard"] = ["firewall-cmd", "--zone=" + zone, "--list-rich-rules"]
                plan["expected"] = {"rule": rule}
                try:
                    if self._guard_present(plan):
                        return self._unavailable(plan, "not_needed", "This exact firewalld exception already exists; it will not be adopted or removed.")
                except (OSError, subprocess.SubprocessError) as exc:
                    return self._unavailable(plan, "blocked", "Cannot inspect existing firewalld rules: " + str(exc))
            else:
                return self._unavailable(plan, "unsupported", "Automatic UDP exceptions support Windows Firewall and active Linux firewalld. Other firewall backends require manual OS-specific review; the firewall will never be disabled.")
        plan["descriptor"] = {"platform": self.system, "kind": kind, "source": {k: source[k] for k in ("id", "kind", "host", "port", "path")},
                              "interface": name, "index": interface.get("index"), "local_address": local,
                              "prefix": params.get("prefix", 24) if kind == "address_alias" else None, "zone": plan.get("zone")}
        plan["baseline"] = {"interfaces": snapshot["interfaces"], "routes": snapshot["routes"]}
        # Initial plans and recovered receipts share the same trusted compiler.
        self._compile_descriptor(plan)
        self.plans[plan_id] = plan
        return self._public_plan(plan)

    def _public_plan(self, plan: dict) -> dict:
        result = copy.deepcopy({k: v for k, v in plan.items() if k not in {"snapshot", "created", "guard", "expected", "applied", "post_guard", "post_routes", "post_interfaces", "descriptor", "baseline", "post_state"}})
        result["execution_capability"] = self._execution_capability()
        result["automatic_apply"] = result["execution_capability"]["automatic_apply"]
        result["repair_safety"] = self._repair_safety()
        result.setdefault("warnings", []).append("Command previews are for operator review, not automatic authorization. Re-establish current disarmed identity evidence and review the impact before any manual host change.")
        return result

    def _unavailable(self, plan: dict, state: str, message: str) -> dict:
        plan.update(state=state, message=message)
        return self._public_plan(plan)

    def _compile_descriptor(self, plan: dict) -> None:
        """The only compiler for journal recovery; stored argv is never loaded."""
        d = plan["descriptor"]
        fields = {"platform", "kind", "source", "interface", "index", "local_address", "prefix", "zone"}
        if not isinstance(d, dict) or set(d) != fields or d["platform"] != self.system or self.system not in {"Linux", "Windows", "Darwin"}:
            raise ValueError("Repair receipt platform/schema does not match this computer.")
        plan_id = plan["plan_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise ValueError("Invalid repair identity.")
        if not isinstance(d["source"], dict) or set(d["source"]) != {"id", "kind", "host", "port", "path"}:
            raise ValueError("Invalid typed repair source.")
        if d["index"] is not None:
            _integer(d["index"], 1, 2147483647, "Interface index")
        source = _source(d["source"])
        host, port = source["host"], source["port"]
        name, kind = d["interface"], d["kind"]
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or name.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("Invalid repair interface.")
        if self.system != "Windows" and not re.fullmatch(r"[A-Za-z0-9_.: -]+", name):
            raise ValueError("Unsupported repair interface.")
        local = _ip(d["local_address"])
        metric = 10000 + int(plan_id[:4], 16)
        plan.update(source=source, interface=name, kind=kind, guard=None, expected=None)
        if kind == "host_route":
            if d["prefix"] is not None or d["zone"] is not None:
                raise ValueError("Unexpected host-route descriptor fields.")
            if self.system == "Linux":
                suffix = [host + "/32", "dev", name, "src", local, "proto", "4", "metric", str(metric)]
                plan["commands"] = [["ip", "-4", "route", "add"] + suffix]
                plan["rollback_commands"] = [["ip", "-4", "route", "del"] + suffix]
                plan["expected"] = {"destination": host + "/32", "interface": name, "metric": metric, "source": local, "protocol": "4", "gateway": None, "table": "main", "type": "unicast"}
            elif self.system == "Windows":
                index = _integer(d["index"], 1, 2147483647, "Interface index")
                plan["commands"] = [_ps(f"New-NetRoute -DestinationPrefix '{host}/32' -InterfaceIndex {index} -NextHop '0.0.0.0' -RouteMetric {metric} -PolicyStore ActiveStore | Out-Null")]
                plan["rollback_commands"] = [_ps(f"Get-NetRoute -DestinationPrefix '{host}/32' -InterfaceIndex {index} -NextHop '0.0.0.0' -PolicyStore ActiveStore | Where-Object {{$_.RouteMetric -eq {metric}}} | Remove-NetRoute -Confirm:$false")]
                plan["expected"] = {"destination": host + "/32", "interface": name, "index": index, "metric": metric, "gateway": "0.0.0.0"}
            else:
                plan["commands"] = [["route", "-n", "add", "-host", host, "-interface", name, "-static", "-proto2"]]
                plan["rollback_commands"] = [["route", "-n", "delete", "-host", host, "-interface", name]]
                plan["expected"] = {"destination": host + "/32", "interface": name}
            plan["title"] = f"Route only {host}/32 through {name}"
        elif kind == "address_alias":
            prefix = _integer(d["prefix"], 1, 32, "Prefix")
            if d["zone"] is not None or self.system not in {"Linux", "Darwin"} or host == local or ipaddress.IPv4Address(host) not in ipaddress.IPv4Interface(f"{local}/{prefix}").network:
                raise ValueError("Invalid address-alias descriptor.")
            if self.system == "Linux":
                if len(name) > 9:
                    raise ValueError("Alias ownership label would be too long.")
                label = name + ":q" + plan_id[:15 - len(name) - 2]
                plan["commands"] = [["ip", "-4", "address", "add", f"{local}/{prefix}", "dev", name, "label", label, "noprefixroute"]]
                plan["rollback_commands"] = [["ip", "-4", "address", "del", f"{local}/{prefix}", "dev", name, "label", label]]
                plan["expected"] = {"local": local, "prefixlen": prefix, "label": label}
            else:
                plan["commands"] = [["ifconfig", name, "inet", local, "netmask", "255.255.255.255", "alias"]]
                plan["rollback_commands"] = [["ifconfig", name, "inet", local, "-alias"]]
                plan["expected"] = {"local": local, "prefixlen": 32}
            plan["title"] = f"Add {local}/{32 if self.system == 'Darwin' else prefix} to {name} (verified device prefix /{prefix})"
        elif kind == "firewall_udp" and source["kind"] == "udp" and d["prefix"] is None:
            if self.system == "Windows" and d["zone"] is None:
                rule = "Q22-" + plan_id
                encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
                alias = f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))"
                plan["commands"] = [_ps(f"New-NetFirewallRule -Name '{rule}' -DisplayName '{rule}' -Direction Inbound -Action Allow -Protocol UDP -LocalPort {port} -RemoteAddress '{host}' -LocalAddress '{local}' -InterfaceAlias {alias} -Profile Any -PolicyStore PersistentStore | Out-Null")]
                plan["rollback_commands"] = [_ps(f"Remove-NetFirewallRule -Name '{rule}' -PolicyStore PersistentStore")]
                plan["guard"] = _ps(f"$r = Get-NetFirewallRule -PolicyStore PersistentStore | Where-Object {{$_.Name -eq '{rule}'}}; if ($r) {{ @{{rule=($r | Select-Object Name,Direction,Action,Enabled,Profile); port=($r | Get-NetFirewallPortFilter | Select-Object Protocol,LocalPort,RemotePort); address=($r | Get-NetFirewallAddressFilter | Select-Object LocalAddress,RemoteAddress); interface=($r | Get-NetFirewallInterfaceFilter | Select-Object InterfaceAlias)}} | ConvertTo-Json -Depth 5 -Compress }}")
            elif self.system == "Linux" and isinstance(d["zone"], str) and re.fullmatch(r"[A-Za-z0-9_-]+", d["zone"]):
                zone = d["zone"]
                rule = f'rule family="ipv4" source address="{host}/32" destination address="{local}/32" port port="{port}" protocol="udp" accept'
                plan["zone"] = zone
                plan["commands"] = [["firewall-cmd", "--zone=" + zone, "--add-rich-rule=" + rule]]
                plan["rollback_commands"] = [["firewall-cmd", "--zone=" + zone, "--remove-rich-rule=" + rule]]
                plan["guard"] = ["firewall-cmd", "--zone=" + zone, "--list-rich-rules"]
                plan["expected"] = {"rule": rule}
            else:
                raise ValueError("Unsupported firewall receipt.")
            plan["title"] = f"Allow UDP from {host} to {local}:{port} on {name}"
        else:
            raise ValueError("Unsupported repair descriptor.")

    def _save(self, plan: dict, state: str) -> None:
        if self.journal_error:
            raise OSError("Durable repair recovery is unavailable: " + self.journal_error)
        if self.journal:
            if state == "apply_intent":
                records, warnings = self.journal.records()
                if warnings:
                    raise OSError("Repair journal contains unreadable receipts. Preserve and review them before another host change.")
                active = [r for r in records if r["events"][-1]["state"] != "rolled_back" and r["plan_id"] != plan["plan_id"]]
                if len(active) >= 16:
                    raise OSError("Sixteen unresolved repair receipts already exist. Resolve existing repairs before adding another.")
            self.journal.save(plan["plan_id"], plan["descriptor"], plan["baseline"], state, plan.get("post_state"))

    def _current_addition(self, plan: dict) -> object:
        """Return exact current data for the narrow target, without executing it."""
        if plan["kind"] == "host_route":
            destination = plan["expected"]["destination"]
            routes = [r for r in self._routes() if r["destination"] == destination]
            if self.system == "Darwin" and routes:
                fields = dict(line.strip().split(":", 1) for line in self.run(["route", "-n", "get", plan["source"]["host"]]).splitlines() if ":" in line)
                return {"routes": routes, "flags": sorted(re.findall(r"[A-Z][A-Z0-9_]*", fields.get("flags", ""))), "interface": fields.get("interface", "").strip()}
            return routes
        if plan["kind"] == "address_alias":
            if self.system == "Darwin":
                return [i for i in self._interfaces() if i["name"] == plan["interface"] and any(a["address"] == plan["expected"]["local"] for a in i["ipv4"])]
            rows = json.loads(self.run(["ip", "-j", "-4", "address", "show", "dev", plan["interface"]]))
            return [a for row in rows for a in row.get("addr_info", []) if a.get("local") == plan["expected"]["local"]]
        text = self.run(plan["guard"]).strip()
        if self.system == "Windows":
            return _json_output(text) if text else None
        return plan["expected"]["rule"] if plan["expected"]["rule"] in text.splitlines() else None

    def _matches_addition(self, plan: dict, value: object) -> bool:
        if not value:
            return False
        if plan["kind"] == "host_route":
            if self.system == "Darwin":
                return (isinstance(value, dict) and value["interface"] == plan["interface"] and {"HOST", "STATIC", "PROTO2"}.issubset(value["flags"])
                        and len(value["routes"]) == 1 and all(value["routes"][0].get(k) == v for k, v in plan["expected"].items()))
            return len(value) == 1 and all(value[0].get(k) == v for k, v in plan["expected"].items())
        if plan["kind"] == "address_alias":
            if self.system == "Darwin":
                if len(value) != 1:
                    return False
                current = copy.deepcopy(value[0])
                own = [a for a in current["ipv4"] if a["address"] == plan["expected"]["local"] and a["prefix"] == 32]
                current["ipv4"] = [a for a in current["ipv4"] if a["address"] != plan["expected"]["local"]]
                before = next((i for i in plan["baseline"]["interfaces"] if i["name"] == plan["interface"]), None)
                # A later device host route may depend on this alias. Remove
                # that route first. Ignore only the kernel's own local /32 route.
                remaining_routes = [r for r in self._routes() if r["destination"] != plan["expected"]["local"] + "/32"]
                return len(own) == 1 and current == before and self._route_signature(remaining_routes) == self._route_signature(plan["baseline"]["routes"])
            return len(value) == 1 and all(value[0].get(k) == v for k, v in plan["expected"].items())
        if self.system == "Windows":
            # A name/UUID alone is insufficient, particularly if the engine died
            # before saving its post-state. Validate every scoped filter against
            # typed intent even before adopting the first observation.
            def singleton(item):
                return item[0] if isinstance(item, list) and len(item) == 1 else item
            try:
                rule = singleton(value["rule"])
                port = singleton(value["port"])
                address = singleton(value["address"])
                interface = singleton(value["interface"])
                expected_local = ipaddress.IPv4Network(plan["descriptor"]["local_address"] + "/32")
                expected_remote = ipaddress.IPv4Network(plan["source"]["host"] + "/32")
                return (rule["Name"] == "Q22-" + plan["plan_id"] and rule["Direction"] in (1, "Inbound")
                        and rule["Action"] in (2, "Allow") and rule["Enabled"] in (1, True, "True", "Enabled")
                        and singleton(rule["Profile"]) in (0, "Any")
                        and str(singleton(port["Protocol"])).upper() in {"17", "UDP"}
                        and str(singleton(port["LocalPort"])) == str(plan["source"]["port"])
                        and singleton(port["RemotePort"]) == "Any"
                        and ipaddress.IPv4Network(singleton(address["LocalAddress"]), strict=False) == expected_local
                        and ipaddress.IPv4Network(singleton(address["RemoteAddress"]), strict=False) == expected_remote
                        and singleton(interface["InterfaceAlias"]) == plan["interface"])
            except (ValueError, TypeError, KeyError, AttributeError):
                return False
        return True

    def recovery(self, params: dict) -> dict:
        if params:
            raise ValueError("Recovery discovery accepts no parameters.")
        if not self.journal:
            return {"status": "unavailable", "repairs": [], "warnings": [self.journal_error or "This session has no durable repair data directory."]}
        try:
            records, warnings = self.journal.records()
        except (OSError, ValueError) as exc:
            return {"status": "unavailable", "repairs": [], "warnings": [str(exc)]}
        repairs = []
        deadline = time.monotonic() + 20
        for record in records:
            if record["events"][-1]["state"] == "rolled_back":
                continue
            if len(repairs) >= 16:
                warnings.append("Additional unresolved receipts remain on disk. Resolve the displayed receipts before inspecting the next batch.")
                break
            try:
                plan = {"plan_id": record["plan_id"], "descriptor": record["descriptor"], "baseline": record["baseline"], "state": "uncertain",
                        "applied": True, "recovered": True, "requires_elevation": True, "warnings": ["Recovered local receipt is a claim of origin, not independently authenticated provenance. Review the exact matching native state and re-authorize this narrow rollback. Nothing was replayed."],
                        "post_state": next((e["post_state"] for e in reversed(record["events"]) if e["post_state"] is not None), None)}
                self._compile_descriptor(plan)
                baseline = plan["baseline"]
                if not isinstance(baseline, dict) or set(baseline) != {"interfaces", "routes"} or not isinstance(baseline["interfaces"], list) or not isinstance(baseline["routes"], list):
                    raise ValueError("Invalid original repair baseline.")
                if plan["kind"] == "host_route" and any(r.get("destination") == plan["expected"]["destination"] for r in baseline["routes"]):
                    raise ValueError("The baseline already contained this host route; ownership is not established.")
                if plan["kind"] == "address_alias" and any(a.get("address") == plan["expected"]["local"] for i in baseline["interfaces"] for a in i.get("ipv4", [])):
                    raise ValueError("The baseline already contained this address; ownership is not established.")
                self.plans[plan["plan_id"]] = plan
                if time.monotonic() >= deadline:
                    ownership, message = "unknown", "Recovery inspection budget exhausted; retry recovery before rollback."
                else:
                    try:
                        current = self._current_addition(plan)
                        owned = self._matches_addition(plan, current) and (plan.get("post_state") is None or current == plan["post_state"])
                        if self.system == "Darwin" and plan.get("post_state") is None:
                            owned = False
                        ownership = "absent" if not current else "owned" if owned else "changed"
                        message = "Exact native state matches the typed receipt; review and re-authorize rollback." if ownership == "owned" else "The recorded addition is absent." if ownership == "absent" else "State differs or the original outcome is uncertain. Automatic rollback is blocked; review manually."
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        ownership, message = "unknown", str(exc)
                public = self._public_plan(plan)
                public.update(ownership=ownership, state={"owned": "applied", "absent": "absent", "changed": "ownership_lost", "unknown": "inspection_failed"}[ownership], may_have_applied=ownership != "absent", message=message)
                repairs.append(public)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"Receipt {record['plan_id']} is not recoverable: {exc}")
        return {"status": "ready", "repairs": repairs, "warnings": warnings}

    def _guard_present(self, plan: dict) -> bool:
        text = self.run(plan["guard"]).strip()
        if (plan.get("expected") or {}).get("rule"):
            return plan["expected"]["rule"] in text.splitlines()
        return bool(text)

    def _guard_snapshot(self, plan: dict) -> str:
        value = self.run(plan["guard"]).strip()
        if self.system == "Windows":
            return json.dumps(_json_output(value), sort_keys=True, separators=(",", ":"))
        return "\n".join(sorted(value.splitlines()))

    def _confirmed_plan(self, params: dict) -> dict:
        if params.get("confirm") is not True:
            raise ValueError("Explicit confirmation of the displayed plan is required.")
        if set(params) - {"plan_id", "confirm"}:
            raise ValueError("Apply/rollback accepts only a server-held plan ID and confirmation.")
        plan_id = params.get("plan_id")
        if not isinstance(plan_id, str) or plan_id not in self.plans:
            raise ValueError("Unknown plan ID; generate and review a new plan.")
        return self.plans[plan_id]

    def _owned(self, plan: dict) -> bool:
        current = self._current_addition(plan)
        if not self._matches_addition(plan, current):
            return False
        if self.system == "Darwin" and plan.get("post_state") is None:
            return False
        return plan.get("post_state") is None or current == plan["post_state"]

    def apply(self, params: dict) -> dict:
        plan = self._confirmed_plan(params)
        plan_id = plan["plan_id"]
        if plan.get("recovered"):
            return {"plan_id": plan_id, "state": "recovered_receipt", "message": "Recovered receipts only authorize reviewed rollback. Generate a fresh plan for a new repair; nothing was replayed."}
        if plan["applied"]:
            return {"plan_id": plan_id, "state": "already_applied", "message": "This plan was already applied; re-probe or roll it back."}
        if time.monotonic() - plan["created"] > 600:
            return {"plan_id": plan_id, "state": "expired", "message": "Host state may have changed. Generate and review a fresh plan."}
        blocked = self._mutation_gate(plan)
        if blocked:
            return blocked
        if not self.elevated():
            return {"plan_id": plan_id, "state": "permission_required", "message": "Administrator privileges are required. No command ran. Use the exact reviewed command in an elevated OS terminal, or restart an administrator session and create a fresh plan. External changes cannot be tracked by this session's rollback."}
        try:
            current = self.inspect({"sources": [plan["source"]]})
            if current["status"] != "inspected" or current["interfaces"] != plan["snapshot"]["interfaces"] or self._route_signature(current["routes"]) != self._route_signature(plan["snapshot"]["routes"]):
                return {"plan_id": plan_id, "state": "stale", "message": "Addresses or routes changed since review. Create a new plan."}
            if plan["kind"] == "firewall_udp" and self._guard_present(plan):
                return {"plan_id": plan_id, "state": "conflict", "message": "The firewall rule already exists; it will not be adopted."}
            if plan.get("zone"):
                zone = plan["zone"]
                if self.run(["firewall-cmd", "--zone=" + zone, "--list-interfaces"]).split() != [plan["interface"]] or self.run(["firewall-cmd", "--zone=" + zone, "--list-sources"]).split():
                    return {"plan_id": plan_id, "state": "stale", "message": "The firewall zone scope changed. Create a new plan."}
            # A timeout/error after dispatch leaves outcome uncertain. Retain
            # rollback access and verify exact ownership before any deletion.
            self._save(plan, "apply_intent")
            blocked = self._mutation_gate(plan)
            if blocked:
                return blocked
            plan["applied"] = True
            self.repair_backend.execute("apply", plan_id, copy.deepcopy(plan["descriptor"]))
            observed = self._current_addition(plan)
            if not self._matches_addition(plan, observed):
                self._save(plan, "uncertain")
                return {"plan_id": plan_id, "state": "verification_failed", "may_have_applied": True, "message": "The OS command returned success but the exact change was not observed. Inspect manually; success is not established."}
            plan["post_state"] = observed
            safety = self._repair_safety()
            if not safety["allowed"]:
                self._save(plan, "uncertain")
                return {"plan_id": plan_id, "state": "safety_changed", "may_have_applied": True,
                        "repair_safety": safety, "message": "The host addition was observed, but aircraft/recording safety changed during execution. No automatic rollback was attempted; retain this receipt for reviewed recovery."}
            self._save(plan, "applied")
            plan["state"] = "applied"
            return {"plan_id": plan_id, "state": "applied", "may_have_applied": True, "message": "The exact host change was observed. Re-run the configured probe to check connectivity; device return-path settings remain independent."}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if plan["applied"]:
                try:
                    self._save(plan, "uncertain")
                except (OSError, ValueError):
                    pass  # The earlier durable intent remains the recovery evidence.
            return {"plan_id": plan_id, "state": "failed", "may_have_applied": plan["applied"], "message": "Host change failed or its outcome is uncertain: " + str(exc)[:1000]}

    def rollback(self, params: dict) -> dict:
        plan = self._confirmed_plan(params)
        plan_id = plan["plan_id"]
        if not plan["applied"]:
            return {"plan_id": plan_id, "state": "not_applied", "message": "This session has no applied change to roll back."}
        blocked = self._mutation_gate(plan)
        if blocked:
            return blocked
        if not self.elevated():
            return {"plan_id": plan_id, "state": "permission_required", "message": "Administrator privileges are required; no rollback command ran."}
        try:
            if not self._owned(plan):
                return {"plan_id": plan_id, "state": "ownership_lost", "message": "The exact addition is absent or changed. Rollback refuses to remove a different route, address or rule."}
            self._save(plan, "rollback_intent")
            blocked = self._mutation_gate(plan)
            if blocked:
                return blocked
            self.repair_backend.execute("rollback", plan_id, copy.deepcopy(plan["descriptor"]))
            if self._current_addition(plan):
                self._save(plan, "rollback_uncertain")
                return {"plan_id": plan_id, "state": "verification_failed", "may_have_applied": True, "message": "Rollback command completed but the narrow target is still present or changed; inspect manually."}
            plan["applied"] = False
            plan["state"] = "rolled_back"
            self._save(plan, "rolled_back")
            safety = self._repair_safety()
            return {"plan_id": plan_id, "state": "rolled_back", "may_have_applied": False,
                    "repair_safety": safety, "safety_changed": not safety["allowed"],
                    "message": "Only the exact change made by this plan was removed." + (" Aircraft/recording safety changed during verification; further host changes are blocked." if not safety["allowed"] else "")}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            try:
                self._save(plan, "rollback_uncertain")
            except (OSError, ValueError):
                pass
            return {"plan_id": plan_id, "state": "failed", "message": "Rollback failed: " + str(exc)[:1000]}
