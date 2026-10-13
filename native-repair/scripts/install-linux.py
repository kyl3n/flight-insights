#!/usr/bin/env python3
"""Review, then explicitly install a Linux private-bench helper; never run by the app.

Default operation reads local inputs and prints a hash-bound plan. --apply needs
root and --approve-plan-sha256 from that reviewed plan. This script never writes
routes/firewalls or enrolls an interpreter. Existing installation/history blocks
replacement; journal/configuration evidence is never silently removed.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

HELPER = Path("/usr/libexec/q22-network-helper")
CONFIG = Path("/etc/q22-network-helper/enrollment.json")
STATE = Path("/var/lib/q22-network-helper")
UNIT = Path("/etc/systemd/system/q22-network-helper.service")
UNIT_SOURCE = Path(__file__).with_name("q22-network-helper.service")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, maximum, *, protected=False, private=False):
    """Read the exact no-follow inode; reject mutation during the bounded read."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Use absolute paths without parent traversal")
    if protected:
        protected_ancestry(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > maximum or (private and before.st_mode & 0o077)):
            raise ValueError("Input must be a bounded ordinary single-link file with the required privacy")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Input changed while it was read")
    return data


def protected_ancestry(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Root ownership, no symlinks and no group/world writes required: {part}")


def unicast(value, *, allow_loopback=False):
    address = ipaddress.IPv4Address(value)
    if (str(address) != value or address.is_unspecified or address.is_multicast
            or (address.is_loopback and not allow_loopback) or int(address) == 0xffffffff):
        raise ValueError("Explicit canonical unicast IPv4 required; loopback is allowed only for configured observer forwarding")


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("Explicit TCP/UDP port must be 1..65535")


def service_visible(path, roots):
    if (not path.is_absolute() or ".." in path.parts
            or not any(path.is_relative_to(root) for root in roots)):
        raise ValueError("Service-visible paths required: installed engine under /usr or /opt; private key under /etc")


def engine_access(path):
    # A documented conservative installation mode avoids inferring effective
    # per-user ACL/group access while this review might be running as root.
    if path.lstat().st_mode & 0o555 != 0o555:
        raise ValueError("Installed frozen engine needs public read/execute access (normally mode0755)")
    for part in (path, *path.parents):
        if part != path and part.lstat().st_mode & 0o001 == 0:
            raise ValueError("Installed engine ancestors must allow desktop-user traversal")
        if "system.posix_acl_access" in os.listxattr(part, follow_symlinks=False):
            raise ValueError("Custom access ACLs require separate principal-access qualification; use the normal installed image")


def enrollment(data, engine, helper_hash):
    policy = json.loads(data)
    keys = {"version", "client_principal", "client_image", "client_sha256", "helper_sha256",
            "allowed_interfaces", "devices", "internet_probe", "observer", "bench_enrollment"}
    if (not isinstance(policy, dict) or set(policy) != keys or policy["version"] != 1
            or policy["bench_enrollment"] is not True):
        raise ValueError("Use the exact version 1 private-bench enrollment schema")
    principal = policy["client_principal"]
    if (not isinstance(principal, str) or not principal.isascii() or not principal.isdecimal()
            or str(int(principal)) != principal or int(principal) == 0):
        raise ValueError("Enroll an explicit canonical non-root desktop UID")
    pwd.getpwuid(int(principal))
    interfaces = policy["allowed_interfaces"]
    if (not isinstance(interfaces, list) or not 1 <= len(interfaces) <= 8
            or any(not isinstance(name, str) or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name) for name in interfaces)):
        raise ValueError("Supply bounded exact interface names")
    devices = policy["devices"]
    if not isinstance(devices, list) or not 1 <= len(devices) <= 8:
        raise ValueError("Supply one to eight explicitly reviewed devices")
    for device in devices:
        if set(device) != {"host", "verification_port"}: raise ValueError("Unexpected device policy fields")
        unicast(device["host"]); port(device["verification_port"])
    internet = policy["internet_probe"]
    if set(internet) != {"host", "port"}: raise ValueError("Unexpected Internet probe fields")
    unicast(internet["host"]); port(internet["port"])
    observer = policy["observer"]
    if set(observer) != {"local_address", "port", "sender", "system_id", "component_id", "expected_uid", "require_signed", "signing_key_path"}:
        raise ValueError("Unexpected observer policy fields")
    unicast(observer["local_address"], allow_loopback=True)
    unicast(observer["sender"], allow_loopback=True)
    port(observer["port"])
    if observer["port"] in (14550, 5600) or observer["port"] < 1024:
        raise ValueError("Use a dedicated unprivileged observer port, separate from GCS/video")
    for name in ("system_id", "component_id"):
        if type(observer[name]) is not int or not 1 <= observer[name] <= 255: raise ValueError("Explicit MAVLink identities required")
    uid = observer["expected_uid"]
    if uid is not None and (type(uid) is not int or not 1 <= uid < 2**64): raise ValueError("Invalid expected vehicle UID")
    if type(observer["require_signed"]) is not bool: raise ValueError("Signing requirement must be boolean")
    key_path = observer["signing_key_path"]
    if observer["require_signed"] and not key_path: raise ValueError("Signed observer requires a protected key file")
    if key_path is not None:
        key = Path(key_path)
        service_visible(key, (Path("/etc"),))
        protected_ancestry(key)
        info = key.lstat()
        if not key.is_absolute() or ".." in key.parts or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size != 32:
            raise ValueError("Provision a root-owned mode-600 32-byte observer key file out of band; it is not copied or printed")
    # Exact reviewed hashes are inputs, not silently substituted defaults.
    service_visible(engine, (Path("/usr"), Path("/opt")))
    engine_hash = digest(regular(engine, 512 * 1024**2, protected=True))
    engine_access(engine)
    if engine.name != "q22-engine" or policy["client_image"] != str(engine):
        raise ValueError("Enrollment must name the exact already-installed protected frozen q22-engine")
    if policy["client_sha256"] != engine_hash or policy["helper_sha256"] != helper_hash:
        raise ValueError("Enrollment hashes must match the exact reviewed frozen engine and helper")
    return policy


def new_file(path, data, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def systemctl(*arguments):
    # The installer never uses shell interpolation or invokes its source binary.
    return subprocess.run(["/usr/bin/systemctl", *arguments], check=True, timeout=130,
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--enrollment", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-plan-sha256")
    parser.add_argument("--start", action="store_true", help="Explicitly start the installed supervisor (included in review hash)")
    parser.add_argument("--enable", action="store_true", help="Explicitly enable supervision at boot (included in review hash)")
    args = parser.parse_args()
    if sys.platform != "linux": parser.error("Linux systemd/NetworkManager private bench installer only")
    if args.enable and not args.start: parser.error("--enable also requires --start")
    helper = regular(args.helper, 512 * 1024**2)
    service_visible(args.engine, (Path("/usr"), Path("/opt")))
    engine_magic = regular(args.engine, 512 * 1024**2, protected=True)[:4]
    if helper[:4] != b"\x7fELF" or engine_magic != b"\x7fELF": raise ValueError("Only native ELF helper/frozen engine images can be enrolled")
    policy = enrollment(regular(args.enrollment, 65536), args.engine, digest(helper))
    unit = regular(UNIT_SOURCE.absolute(), 65536)
    config = (json.dumps(policy, indent=2) + "\n").encode()
    plan = {"version": 1, "mode": "experimental_private_bench", "helper_sha256": digest(helper),
        "enrollment": policy, "enrollment_sha256": digest(config), "unit_sha256": digest(unit),
        "destinations": [str(HELPER), str(CONFIG), str(UNIT), str(STATE)],
        "start_service": args.start, "enable_at_boot": args.enable,
        "no_route_or_firewall_write_by_installer": True,
        "limits": ["Native hardware qualification and publisher signing are not established.",
            "Service may make explicit TCP verification probes after an approved repair; dedicated telemetry observation is passive.",
            "User app opt-in and per-plan approval remain mandatory; installer approval is not repair approval.",
            "Existing installation or journal blocks replacement; this installer has no destructive uninstall mode."]}
    fingerprint = digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
    print(json.dumps({"plan_sha256": fingerprint, "plan": plan, "apply_requested": args.apply}, indent=2), flush=True)
    if not args.apply: return
    if os.geteuid() != 0 or args.approve_plan_sha256 != fingerprint:
        raise ValueError("Installation requires root and the exact --approve-plan-sha256 from the reviewed plan")
    for target in (HELPER, CONFIG.parent, UNIT, STATE):
        if target.exists() or target.is_symlink(): raise ValueError(f"Existing installation/state must be reviewed and migrated, never overwritten: {target}")
    for parent in (Path("/usr"), Path("/etc"), UNIT.parent, STATE.parent): protected_ancestry(parent)
    if not Path("/run/systemd/system").is_dir() or not Path("/usr/bin/busctl").is_file():
        raise ValueError("A normal systemd host with NetworkManager D-Bus support is required")
    systemctl("is-active", "NetworkManager.service")
    if not HELPER.parent.exists(): HELPER.parent.mkdir(mode=0o755)
    protected_ancestry(HELPER.parent)
    CONFIG.parent.mkdir(mode=0o755)
    STATE.mkdir(mode=0o700)
    new_file(HELPER, helper, 0o755)
    new_file(CONFIG, config, 0o644)
    new_file(UNIT, unit, 0o644)
    new_file(CONFIG.parent / "installation-review.json", (json.dumps({"plan_sha256": fingerprint, "plan": plan}, indent=2) + "\n").encode(), 0o644)
    for path in (HELPER, CONFIG, UNIT): protected_ancestry(path)
    systemctl("daemon-reload")
    if args.enable: systemctl("enable", "q22-network-helper.service")
    if args.start: systemctl("start", "q22-network-helper.service")
    print("Experimental service installed. No repair was approved; review app opt-in, safety evidence and each plan separately.")


if __name__ == "__main__":
    main()
