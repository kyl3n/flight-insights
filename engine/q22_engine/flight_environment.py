"""Bounded host observations retained with each actual armed interval.

Discovery matches a fixed application allowlist before inspecting executable
metadata. Paths stay transient; command lines, environments, documents and
unrelated application details are never read or retained. Discovered programs
are never run; Linux uses only bounded queries to trusted OS package utilities.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
from uuid import uuid4

import psutil

from . import __version__
from .software_versions import MAX_TARGETS, SoftwareVersionResolver


SCHEMA_VERSION = 1
MAX_PROCESS_NAMES = 256
PROCESS_SCAN_SECONDS = 0.025
RELEVANT_APPLICATIONS = {
    "missionplanner": ("Mission Planner", "gcs"),
    "mission planner": ("Mission Planner", "gcs"),
    "qgroundcontrol": ("QGroundControl", "gcs"),
    "mavlink-routerd": ("MAVLink Router", "router"),
    "mavlink-router": ("MAVLink Router", "router"),
    "mavproxy": ("MAVProxy", "router"),
    "mavproxy.py": ("MAVProxy", "router"),
    "vlc": ("VLC", "video"),
    "mpv": ("mpv", "video"),
    "ffplay": ("FFplay", "video"),
    "gst-launch-1.0": ("GStreamer launcher", "video"),
    "obs": ("OBS Studio", "video"),
    "obs64": ("OBS Studio", "video"),
}
LIMITATIONS = [
    "This is a host observation at the recorded time, not continuous flight-wide monitoring or a causal diagnosis.",
    "Only allowlisted GCS, telemetry-router and video process names are retained; names do not prove binary identity, usage, or a connection to this aircraft.",
    "Versions come from the recognized executable's Windows file-version resource or checksum-matched Linux package metadata. These sources do not authenticate software or identify which program controlled the aircraft.",
    "Linux versions are installed package versions and Windows versions are file-resource versions; they may differ from an application's About display. Portable or modified programs without supported exact metadata remain unknown.",
    "Version metadata runs in a bounded background worker. A lookup unfinished at the snapshot deadline stays pending in that immutable observation; later results never rewrite a flight.",
    "Operator declarations come from the reviewed capture profile; they are not automatically re-confirmed at each arm transition.",
    "Programs hosted under generic Python, Mono, Wine or other runtime names may not be detected without inspecting private command lines, which this detector does not do.",
    "Host load is acquisition context, not proof of packet loss or the cause of aircraft behavior. Unknown values are not zero.",
    "A content hash detects content changes; it does not authenticate the computer, software, or operator declarations.",
]


def _text(value, label, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label} must be text of at most {maximum} characters without control characters.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} must not be empty.")
    return value


def validate_context(value):
    """Normalize bounded declarations without treating them as detected facts."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"gcs_name", "gcs_version", "applications", "notes"}:
        raise ValueError("environment_context must contain only gcs_name, gcs_version, applications and notes.")
    applications = value.get("applications", [])
    if not isinstance(applications, list) or len(applications) > 8:
        raise ValueError("environment_context applications must contain at most eight declared programs.")
    declared = []
    for application in applications:
        if not isinstance(application, dict) or set(application) - {"name", "version"}:
            raise ValueError("Declared applications must contain only name and version.")
        name = _text(application.get("name", ""), "Application name", 160)
        version = _text(application.get("version", ""), "Application version", 160)
        if not name and not version:
            continue
        if not name:
            raise ValueError("A declared application version needs an application name.")
        declared.append({"name": name, "version": version})
    return {"gcs_name": _text(value.get("gcs_name", ""), "GCS name", 160),
            "gcs_version": _text(value.get("gcs_version", ""), "GCS version", 160),
            "applications": declared, "notes": _text(value.get("notes", ""), "Runtime notes", 2000)}


def _empty(scope, context):
    return {
        "schema_version": SCHEMA_VERSION, "id": None, "content_sha256": None,
        "observed_at": None, "status": "unavailable", "scope": scope,
        "host": {"os": None, "release": None, "version": None, "architecture": None,
                 "distribution": None, "distribution_version": None, "provenance": "unavailable"},
        "software": {
            "application": {"name": "Q22 Flight Lab", "version": None, "build": None, "provenance": "unavailable"},
            "engine": {"name": "q22-flight-engine", "version": None, "build": None, "provenance": "unavailable"},
        },
        "detected_applications": [],
        "declared_context": {**validate_context(context), "provenance": "operator_declared"},
        "load": {"memory_percent": None, "available_memory_bytes": None,
                 "load_average_1m_5m_15m": None, "engine_rss_bytes": None, "provenance": "unavailable"},
        "coverage": {"process_scan_status": "unavailable", "names_checked": 0,
                     "version_detection": "unavailable", "versions_observed": 0,
                     "versions_pending": 0, "versions_unavailable": 0},
        "limitations": list(LIMITATIONS), "errors": [],
    }


def _seal(snapshot):
    snapshot["id"] = str(uuid4())
    payload = {key: value for key, value in snapshot.items() if key not in {"id", "content_sha256"}}
    snapshot["content_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return snapshot


def unavailable_snapshot(reason="not_recorded", *, scope="armed_interval_start", context=None, observed=False):
    """Old records get no invented timestamp/hash; a failed current attempt does."""
    snapshot = _empty(scope, context)
    snapshot["errors"] = [reason]
    if observed:
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
        return _seal(snapshot)
    return snapshot


class EnvironmentDetector:
    def __init__(self):
        self._client_version = None
        self._versions = SoftwareVersionResolver()

    def close(self):
        self._versions.close()

    def note_client(self, params):
        # The desktop already reports its compiled package version in handshake.
        # Preserve the source as client-reported, never as independently verified.
        version = params.get("client_version")
        if (params.get("client") == "q22-desktop" and isinstance(version, str)
                and 0 < len(version) <= 160 and all(32 <= ord(char) < 127 for char in version)):
            self._client_version = version

    @staticmethod
    def _windows_architecture():
        import ctypes
        from ctypes import wintypes
        class SystemInfo(ctypes.Structure):
            _fields_ = [("architecture", wintypes.WORD), ("reserved", wintypes.WORD),
                        ("page_size", wintypes.DWORD), ("minimum_address", ctypes.c_void_p),
                        ("maximum_address", ctypes.c_void_p), ("active_mask", ctypes.c_size_t),
                        ("processor_count", wintypes.DWORD), ("processor_type", wintypes.DWORD),
                        ("allocation_granularity", wintypes.DWORD), ("processor_level", wintypes.WORD),
                        ("processor_revision", wintypes.WORD)]
        info = SystemInfo()
        function = ctypes.WinDLL("kernel32", use_last_error=True).GetNativeSystemInfo
        function.argtypes = [ctypes.POINTER(SystemInfo)]
        function.restype = None
        function(ctypes.byref(info))
        return {0: "x86", 5: "ARM", 6: "IA64", 9: "AMD64", 12: "ARM64"}.get(info.architecture)

    @staticmethod
    def _host():
        if os.name == "nt":
            # platform.uname() may fall back to executing `ver` on Windows.
            # Use the interpreter's OS API and a direct native architecture query.
            version = sys.getwindowsversion()
            major, minor, build = version.platform_version
            value = {"os": "Windows", "release": f"{major}.{minor}", "version": f"{major}.{minor}.{build}",
                     "architecture": EnvironmentDetector._windows_architecture(),
                     "provenance": "sys.getwindowsversion;GetNativeSystemInfo"}
        elif hasattr(os, "uname"):
            host = os.uname()
            value = {"os": host.sysname, "release": host.release, "version": host.version,
                     "architecture": host.machine, "provenance": "os.uname"}
        else:
            value = {"os": sys.platform, "release": None, "version": None,
                     "architecture": None, "provenance": "python.sys_platform"}
        value.update(distribution=None, distribution_version=None)
        if value["os"] == "Linux":
            # The standard platform helper caches os-release for the process
            # lifetime. Read the fixed OS config afresh so an in-session distro
            # upgrade cannot silently inherit an earlier flight's version.
            for location in ("/etc/os-release", "/usr/lib/os-release"):
                try:
                    with Path(location).open("r", encoding="utf-8") as stream:
                        text = stream.read(16_385)
                    if len(text) > 16_384:
                        continue
                    release = {}
                    for line in text.splitlines():
                        key, separator, raw = line.partition("=")
                        if separator and key in ("NAME", "VERSION_ID"):
                            parts = shlex.split(raw)
                            release[key] = parts[0][:160] if len(parts) == 1 else None
                    value["distribution"] = release.get("NAME")
                    value["distribution_version"] = release.get("VERSION_ID")
                    value["provenance"] += ";os_release_config"
                    break
                except (OSError, ValueError, UnicodeError):
                    continue
        elif value["os"] == "Darwin":
            try:
                import plistlib
                with open("/System/Library/CoreServices/SystemVersion.plist", "rb") as stream:
                    content = stream.read(65_537)
                if len(content) <= 65_536:
                    release = plistlib.loads(content)
                    product = release.get("ProductUserVisibleVersion", release.get("ProductVersion"))
                    if isinstance(product, str):
                        value.update(distribution="macOS", distribution_version=product[:160])
                        value["provenance"] += ";system_version_plist"
            except (OSError, ValueError, TypeError):
                pass
        return value

    @staticmethod
    def _applications(resolver=None, *, wait_budget=0.0):
        deadline = time.monotonic() + PROCESS_SCAN_SECONDS
        applications = {}
        checked = 0
        status = "complete"
        targets = []
        groups = {}
        truncated_groups = set()
        # attrs=['name'] deliberately excludes exe/cmdline/environ, user names,
        # working directories, open files, and network connection inventories.
        processes = iter(psutil.process_iter(attrs=["name"], ad_value=None))
        while checked < MAX_PROCESS_NAMES and time.monotonic() < deadline:
            try:
                process = next(processes)
            except StopIteration:
                break
            except (psutil.Error, OSError):
                status = "partial"
                break
            checked += 1
            name = process.info.get("name")
            if not isinstance(name, str):
                status = "partial"
                continue
            key = name.casefold()
            if key.endswith(".exe"):
                key = key[:-4]
            relevant = RELEVANT_APPLICATIONS.get(key)
            if relevant:
                display, category = relevant
                group = (display, category)
                applications.setdefault(group, {"name": display, "category": category, "version": None,
                    "process_name": name[:160], "provenance": "running_process_name", "version_status": "unavailable",
                    "version_provenance": None, "version_observed_at": None, "version_reason": "metadata_not_requested"})
                if resolver is not None:
                    if len(targets) < MAX_TARGETS:
                        groups.setdefault(group, []).append(len(targets))
                        targets.append((process, name))
                    else:
                        truncated_groups.add(group)
        else:
            status = "partial"
        if resolver is not None:
            results = resolver.inspect(targets, wait_seconds=wait_budget)
            for group, application in applications.items():
                values = [results[index] for index in groups.get(group, [])]
                versions = {(item["version"], item["version_provenance"]) for item in values if item["version_status"] == "observed"}
                if group in truncated_groups:
                    application["version_reason"] = "running_instance_limit_exceeded"
                elif len(versions) > 1:
                    application.update(version_status="ambiguous", version_reason="running_instances_have_different_versions")
                elif values and all(item["version_status"] == "observed" for item in values):
                    application.update(values[0])
                elif len(values) == 1:
                    application.update(values[0])
                elif values:
                    application.update(version_status="pending" if any(item["version_status"] == "pending" for item in values) else "unavailable",
                                       version_reason="multiple_running_instances_not_fully_resolved")
        observed = sum(item["version_status"] == "observed" for item in applications.values())
        version_status = "complete" if observed == len(applications) else "partial" if observed else "unavailable"
        return list(sorted(applications.values(), key=lambda item: (item["name"], item["process_name"]))), {
            "process_scan_status": status, "names_checked": checked,
            "version_detection": version_status, "versions_observed": observed,
            "versions_pending": sum(item["version_status"] == "pending" for item in applications.values()),
            "versions_unavailable": sum(item["version_status"] in ("unavailable", "ambiguous") for item in applications.values()),
        }

    def inspect(self, context=None, *, scope="current_host"):
        snapshot = _empty(scope, context)
        snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["software"]["engine"].update(version=__version__, provenance="engine_module_version")
        if self._client_version:
            snapshot["software"]["application"].update(version=self._client_version, provenance="desktop_handshake_client_reported")
        try:
            snapshot["host"] = self._host()
        except Exception as error:
            snapshot["errors"].append("host_detection_failed:" + type(error).__name__)
        try:
            budget = 0.01 if scope == "armed_interval_start" else 0.1 if scope == "capture_start" else 0.35
            snapshot["detected_applications"], snapshot["coverage"] = self._applications(self._versions, wait_budget=budget)
            if snapshot["coverage"]["process_scan_status"] != "complete":
                snapshot["errors"].append("process_scan_incomplete")
            if snapshot["detected_applications"] and snapshot["coverage"]["version_detection"] != "complete":
                snapshot["errors"].append("software_versions_incomplete")
        except Exception as error:
            snapshot["errors"].append("process_scan_failed:" + type(error).__name__)
        try:
            memory = psutil.virtual_memory()
            snapshot["load"].update(memory_percent=memory.percent, available_memory_bytes=memory.available, provenance="psutil_host_memory;os_loadavg;psutil_engine_rss")
        except Exception as error:
            snapshot["errors"].append("memory_observation_failed:" + type(error).__name__)
        try:
            snapshot["load"]["engine_rss_bytes"] = psutil.Process().memory_info().rss
        except Exception as error:
            snapshot["errors"].append("engine_memory_observation_failed:" + type(error).__name__)
        if hasattr(os, "getloadavg"):
            try:
                snapshot["load"]["load_average_1m_5m_15m"] = list(os.getloadavg())
            except OSError:
                snapshot["errors"].append("load_average_unavailable")
        else:
            snapshot["limitations"].append("Load averages are unavailable on this host; no blocking CPU sampling is performed.")
        snapshot["status"] = "partial" if snapshot["errors"] else "captured"
        return _seal(snapshot)


def diff_snapshots(left, right):
    """Compare preserved snapshots only; never substitute the current machine."""
    limitations = ["Environment differences are possible acquisition confounders, not evidence that they caused flight behavior.",
                   "Unknown software versions and incomplete process discovery do not establish equivalent environments.",
                   "Operator declarations are compared separately from automatically observed values."]
    if not isinstance(left, dict) or not isinstance(right, dict) or not left.get("id") or not right.get("id"):
        return {"status": "unavailable", "changes": [], "limitations": ["Both flights need preserved environment snapshots.", *limitations]}
    changes = []
    def changed(field, before, after, provenance):
        if before != after:
            changes.append({"field": field, "before": copy.deepcopy(before), "after": copy.deepcopy(after), "provenance": provenance})
    for field in ("os", "release", "version", "architecture", "distribution", "distribution_version"):
        changed("host." + field, left.get("host", {}).get(field), right.get("host", {}).get(field), "host_observation")
    for application in ("application", "engine"):
        for field in ("version", "build", "provenance"):
            changed(f"software.{application}.{field}", left.get("software", {}).get(application, {}).get(field),
                    right.get("software", {}).get(application, {}).get(field), "software_metadata")
    def detected(snapshot):
        return sorted({(item.get("name"), item.get("category"), item.get("version"), item.get("version_provenance"),
                        item.get("version_status", "unavailable")) for item in snapshot.get("detected_applications", [])}, key=repr)
    changed("detected_applications", detected(left), detected(right), "running_process_name;executable_version_metadata")
    for field in ("gcs_name", "gcs_version", "applications", "notes"):
        changed("declared_context." + field, left.get("declared_context", {}).get(field), right.get("declared_context", {}).get(field), "operator_declared")
    for field in ("memory_percent", "available_memory_bytes", "load_average_1m_5m_15m", "engine_rss_bytes"):
        changed("load." + field, left.get("load", {}).get(field), right.get("load", {}).get(field), "point_in_time_host_load")
    state = "compared" if left.get("status") == right.get("status") == "captured" else "partial"
    return {"status": state, "changes": changes, "limitations": limitations}
