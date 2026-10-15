"""Read-only version metadata for already recognized running flight programs.

Executable paths are transient inputs only. No discovered program is launched;
Linux queries use fixed, trusted OS package utilities with bounded output/time.
The worker owns potentially slow file/package operations so recorder callbacks
can return an explicit pending result instead of waiting for metadata.
"""
from __future__ import annotations

import copy
import ctypes
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import queue
import re
import selectors
import stat
import subprocess
import sys
import threading
import time

import psutil


MAX_TARGETS = 16
CACHE_TTL_SECONDS = 5.0
LOOKUP_SECONDS = 0.45
BATCH_SECONDS = 1.5
MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
DPKG = "/usr/bin/dpkg-query"
RPM = "/usr/bin/rpm"


class MetadataUnavailable(Exception):
    """Only these bounded reason codes may cross the snapshot boundary."""


def _result(reason, *, status="unavailable", version=None, provenance=None, **fields):
    return {"version": version, "version_status": status, "version_provenance": provenance,
            "version_observed_at": datetime.now(timezone.utc).isoformat() if status != "pending" else None,
            "version_reason": reason, **fields}


def _signature(path):
    value = os.stat(path)
    if not stat.S_ISREG(value.st_mode):
        raise MetadataUnavailable("executable_is_not_a_regular_file")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _fresh_process(process):
    # process_iter reuses Process objects and Process.exe caches its result.
    # Re-read only an already recognized PID, retaining its creation identity so
    # a reused PID cannot inherit the allowlisted name from an earlier process.
    if not process.is_running():
        raise MetadataUnavailable("recognized_process_changed_or_exited")
    current = psutil.Process(process.pid)
    if current.create_time() != process.create_time():
        raise MetadataUnavailable("recognized_process_changed_or_exited")
    return current


def _local_linux_path(path):
    """Reject remote/unknown mounts before touching an executable or database."""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as stream:
            content = stream.read(262_145)
        if len(content) > 262_144:
            return False
        matches = []
        for line in content.splitlines():
            left, separator, right = line.partition(" - ")
            fields = left.split()
            if not separator or len(fields) < 5:
                continue
            mount = fields[4]
            for escaped, character in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                mount = mount.replace(escaped, character)
            if path == mount or path.startswith(mount.rstrip("/") + "/"):
                matches.append((len(mount), right.split()[0]))
        filesystem = max(matches)[1] if matches else None
        return filesystem in {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs",
                              "f2fs", "jfs", "reiserfs", "squashfs", "erofs", "ntfs3", "vfat", "exfat"}
    except (OSError, ValueError, IndexError):
        return False


def _trusted(path, *, directory=False):
    """Fixed OS tools/metadata must not be replaceable by an ordinary user."""
    try:
        if sys.platform.startswith("linux") and not _local_linux_path(str(path)):
            return False
        target = Path(path).resolve(strict=True)
        if sys.platform.startswith("linux") and not _local_linux_path(str(target)):
            return False
        for current in (target, *target.parents):
            value = current.stat()
            expected_directory = current != target or directory
            if value.st_uid != 0 or value.st_mode & 0o022:
                return False
            if expected_directory and not stat.S_ISDIR(value.st_mode):
                return False
            if not expected_directory and not stat.S_ISREG(value.st_mode):
                return False
        return True
    except OSError:
        return False


def _run(argv, deadline, maximum=32_768):
    """Linux-only fixed package queries; no shell, inherited secrets, or prompts."""
    if argv[0] not in {DPKG, RPM} or not _trusted(argv[0]):
        raise MetadataUnavailable("package_query_tool_untrusted_or_missing")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MetadataUnavailable("metadata_lookup_timed_out")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                               close_fds=True)
    output = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MetadataUnavailable("metadata_lookup_timed_out")
            if not selector.select(min(remaining, 0.05)):
                continue
            chunk = os.read(process.stdout.fileno(), min(4096, maximum + 1 - len(output)))
            if not chunk:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                return process.returncode, output.decode("utf-8")
            output.extend(chunk)
            if len(output) > maximum:
                raise MetadataUnavailable("package_metadata_exceeds_limit")
    except subprocess.TimeoutExpired:
        raise MetadataUnavailable("metadata_lookup_timed_out") from None
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        process.stdout.close()


def _read_metadata(path, deadline):
    if not _trusted(path):
        raise MetadataUnavailable("package_metadata_untrusted_or_missing")
    if time.monotonic() >= deadline:
        raise MetadataUnavailable("metadata_lookup_timed_out")
    with open(path, "rb") as stream:
        content = stream.read(MAX_METADATA_BYTES + 1)
    if len(content) > MAX_METADATA_BYTES:
        raise MetadataUnavailable("package_metadata_exceeds_limit")
    return content.decode("utf-8")


def _digest(path, algorithm, expected_identity, deadline):
    if expected_identity[2] > MAX_BINARY_BYTES:
        raise MetadataUnavailable("executable_exceeds_verification_limit")
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with open(path, "rb") as stream:
        opened = os.fstat(stream.fileno())
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if identity != expected_identity:
            raise MetadataUnavailable("executable_changed_during_lookup")
        count = 0
        while True:
            if time.monotonic() >= deadline:
                raise MetadataUnavailable("metadata_lookup_timed_out")
            chunk = stream.read(256 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_BINARY_BYTES:
                raise MetadataUnavailable("executable_exceeds_verification_limit")
            digest.update(chunk)
        if _signature(path) != expected_identity:
            raise MetadataUnavailable("executable_changed_during_lookup")
    return digest.hexdigest()


def _linux_candidates(path):
    candidates = [path]
    # Merged-/usr distributions may retain /bin in their package database.
    # An alias is accepted only when it resolves to this exact installed inode.
    for prefix in ("bin", "sbin", "lib", "lib64"):
        start = f"/usr/{prefix}/"
        if path.startswith(start):
            alias = f"/{prefix}/" + path[len(start):]
            try:
                if os.path.samefile(alias, path):
                    candidates.append(alias)
            except OSError:
                pass
    return candidates


def _dpkg_version(path, image_path, identity, deadline):
    owners = []
    for candidate in _linux_candidates(path):
        if any(character in candidate for character in "*?[]\\\n\r"):
            continue  # dpkg search accepts glob patterns; never broaden a target.
        code, output = _run([DPKG, "--search", "--", candidate], deadline)
        if code:
            continue
        for line in output.splitlines():
            owner, separator, owned_path = line.partition(": ")
            if separator and owned_path == candidate and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?", owner):
                owners.append((owner, candidate))
    owners = list(dict.fromkeys(owners))
    if not owners:
        raise MetadataUnavailable("executable_not_owned_by_installed_package")
    if len({owner for owner, _ in owners}) != 1:
        raise MetadataUnavailable("package_ownership_ambiguous")
    owner = owners[0][0]
    code, output = _run([DPKG, "--show", "--showformat=${binary:Package}\t${Version}\t${db:Status-Status}\n", "--", owner], deadline)
    rows = [line.split("\t") for line in output.splitlines()]
    if code or len(rows) != 1 or len(rows[0]) != 3 or rows[0][2] != "installed":
        raise MetadataUnavailable("installed_package_version_unavailable")
    package, version, _ = rows[0]
    if package != owner and package.split(":")[0] != owner:
        raise MetadataUnavailable("package_ownership_changed")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?", package):
        raise MetadataUnavailable("package_metadata_invalid")
    metadata = _read_metadata(f"/var/lib/dpkg/info/{package}.md5sums", deadline)
    expected = set()
    paths = {candidate.lstrip("/") for _, candidate in owners}
    for line in metadata.splitlines():
        checksum, separator, filename = line.partition("  ")
        if separator and filename in paths and re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
            expected.add(checksum.lower())
    if len(expected) != 1:
        raise MetadataUnavailable("package_file_checksum_unavailable")
    if _digest(image_path, "md5", identity, deadline) not in expected:
        raise MetadataUnavailable("running_image_differs_from_package_metadata")
    return _result("installed_package_checksum_matches_running_image", status="observed", version=version,
                   provenance="linux_dpkg_package", version_kind="package_version", package_name=package)


def _rpm_database():
    for directory in ("/usr/lib/sysimage/rpm", "/var/lib/rpm"):
        for filename in ("rpmdb.sqlite", "Packages"):
            path = f"{directory}/{filename}"
            if _trusted(path):
                return directory, path
    return None


def _rpm_version(path, image_path, identity, deadline, database):
    # Explicit trusted configuration excludes per-user macros and plugins.
    for config in ("/usr/lib/rpm/rpmrc", "/usr/lib/rpm/macros"):
        if not _trusted(config):
            raise MetadataUnavailable("package_query_configuration_untrusted_or_missing")
    format_string = "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{FILEDIGESTALGO}\n[%{FILENAMES}\t%{FILEDIGESTS}\n]"
    command = [RPM, "--noplugins", "--rcfile", "/usr/lib/rpm/rpmrc", "--macros", "/usr/lib/rpm/macros",
               "--dbpath", database, "--query", "--file", "--queryformat", format_string, "--", path]
    code, output = _run(command, deadline, MAX_METADATA_BYTES)
    rows = output.splitlines()
    header = rows[0].split("\t") if rows else []
    if code or len(header) != 3:
        raise MetadataUnavailable("executable_not_owned_by_installed_package")
    package, version, algorithm_id = header
    algorithm = {"1": "md5", "2": "sha1", "8": "sha256", "9": "sha384", "10": "sha512"}.get(algorithm_id)
    if not algorithm:
        raise MetadataUnavailable("package_file_checksum_unavailable")
    candidates = set(_linux_candidates(path))
    checksums = {checksum.lower() for line in rows[1:] if "\t" in line
                 for filename, checksum in [line.split("\t", 1)] if filename in candidates and re.fullmatch(r"[0-9a-fA-F]+", checksum)}
    if len(checksums) != 1:
        raise MetadataUnavailable("package_file_checksum_unavailable")
    if _digest(image_path, algorithm, identity, deadline) not in checksums:
        raise MetadataUnavailable("running_image_differs_from_package_metadata")
    return _result("installed_package_checksum_matches_running_image", status="observed", version=version.removeprefix("0:"),
                   provenance="linux_rpm_package", version_kind="package_version", package_name=package)


def _windows_local_path(path):
    from ctypes import wintypes
    import ntpath
    # A recognized program on a network drive must not trigger network access.
    if path.startswith(("\\\\", "//")):
        raise MetadataUnavailable("network_executable_metadata_not_inspected")
    drive, tail = ntpath.splitdrive(path)
    root = drive + "\\"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x00000800)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(root) not in (2, 3, 6):
        raise MetadataUnavailable("nonlocal_executable_metadata_not_inspected")
    # A local drive may contain a junction/symlink into a network share. Check
    # each component before traversing it; GetFileAttributes returns the link's
    # own attributes. Unresolved/reparse locations remain explicitly unknown.
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    current = root
    for component in tail.replace("/", "\\").split("\\"):
        if not component:
            continue
        if component in (".", "..") or ":" in component:
            raise MetadataUnavailable("executable_metadata_path_not_supported")
        current = ntpath.join(current, component)
        attributes = kernel.GetFileAttributesW(current)
        if attributes == 0xFFFFFFFF:
            raise MetadataUnavailable("executable_metadata_path_unavailable")
        if attributes & 0x400:
            raise MetadataUnavailable("reparse_executable_metadata_not_inspected")


def _windows_version(path):
    from ctypes import wintypes
    _windows_local_path(path)
    library = ctypes.WinDLL("version", use_last_error=True, winmode=0x00000800)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    size = library.GetFileVersionInfoSizeW(path, None)
    if not size:
        raise MetadataUnavailable("file_version_resource_missing")
    if size > MAX_METADATA_BYTES:
        raise MetadataUnavailable("file_version_resource_exceeds_limit")
    buffer = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(path, 0, size, buffer):
        raise MetadataUnavailable("file_version_resource_unreadable")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (not library.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or length.value < 52 or not pointer.value
            or pointer.value < ctypes.addressof(buffer) or pointer.value + 52 > ctypes.addressof(buffer) + size):
        raise MetadataUnavailable("file_version_resource_invalid")
    # VS_FIXEDFILEINFO is thirteen DWORDs, independent of host word size.
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    if words[0] != 0xFEEF04BD:
        raise MetadataUnavailable("file_version_resource_invalid")
    components = (words[2] >> 16, words[2] & 65535, words[3] >> 16, words[3] & 65535)
    if not any(components):
        raise MetadataUnavailable("file_version_resource_has_no_version")
    return _result("version_resource_of_recognized_running_executable", status="observed", version=".".join(map(str, components)),
                   provenance="windows_file_version_resource", version_kind="file_version")


class SoftwareVersionResolver:
    def __init__(self):
        self._cache = {}
        self._queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._thread = None
        self._closed = False

    @staticmethod
    def _database_identity():
        if _trusted(DPKG) and _trusted("/var/lib/dpkg/status"):
            return ("dpkg", _signature("/var/lib/dpkg/status"))
        database = _rpm_database() if _trusted(RPM) else None
        if database:
            return ("rpm", database[0], _signature(database[1]))
        raise MetadataUnavailable("supported_package_database_unavailable")

    def _resolve(self, process, expected_name, deadline):
        try:
            process = _fresh_process(process)
            if not process.is_running() or process.name().casefold() != expected_name.casefold():
                raise MetadataUnavailable("recognized_process_changed_or_exited")
            executable = process.exe()
            if not isinstance(executable, str) or not os.path.isabs(executable) or len(executable) > 4096:
                raise MetadataUnavailable("running_executable_path_unavailable")
            # A script/runtime process can advertise an app name while exe is
            # Python/Mono/Wine. Its interpreter version is not the app version.
            if os.path.basename(executable).casefold().removesuffix(".exe") != expected_name.casefold().removesuffix(".exe"):
                raise MetadataUnavailable("process_name_does_not_match_executable_name")
            if sys.platform == "win32":
                _windows_local_path(executable)
                identity = _signature(executable)
                database = None
                image = executable
            elif sys.platform.startswith("linux"):
                if not _local_linux_path(executable):
                    raise MetadataUnavailable("nonlocal_executable_metadata_not_inspected")
                image = f"/proc/{int(process.pid)}/exe"
                identity = _signature(image)
                if identity != _signature(executable):
                    raise MetadataUnavailable("running_image_differs_from_installed_file")
                database = self._database_identity()
            else:
                raise MetadataUnavailable("software_version_adapter_unavailable")
            key = (sys.platform, executable, identity, database)
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return {**copy.deepcopy(cached[1]), "version_cache_hit": True}
            if sys.platform == "win32":
                result = _windows_version(executable)
            elif database[0] == "dpkg":
                result = _dpkg_version(executable, image, identity, deadline)
            else:
                result = _rpm_version(executable, image, identity, deadline, database[1])
            if _signature(image) != identity or _signature(executable) != identity or not process.is_running():
                raise MetadataUnavailable("executable_changed_during_lookup")
            if database is not None and database != self._database_identity():
                raise MetadataUnavailable("package_metadata_changed_during_lookup")
            if not isinstance(result.get("version"), str) or not 0 < len(result["version"]) <= 160 or any(ord(c) < 32 for c in result["version"]):
                raise MetadataUnavailable("software_version_metadata_invalid")
            if result.get("package_name") is not None and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.:@-]{0,159}", result["package_name"]):
                raise MetadataUnavailable("software_version_metadata_invalid")
            result["version_cache_hit"] = False
            if len(self._cache) >= 128:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (now, copy.deepcopy(result))
            return result
        except MetadataUnavailable as error:
            return _result(str(error))
        except (OSError, ValueError, UnicodeError, AttributeError):
            return _result("software_version_metadata_unavailable")
        except Exception:
            # Access-denied/process-exit exceptions contain executable paths.
            return _result("recognized_process_metadata_unavailable")

    def _work(self):
        while True:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._thread = None
                        return
                continue
            if task is None:
                return
            targets, done, results, result_lock = task
            deadline = time.monotonic() + BATCH_SECONDS
            for index, (process, name) in enumerate(targets):
                if self._closed or time.monotonic() >= deadline:
                    value = _result("metadata_lookup_budget_exhausted")
                else:
                    value = self._resolve(process, name, min(deadline, time.monotonic() + LOOKUP_SECONDS))
                with result_lock:
                    results[index] = value
            done.set()

    def inspect(self, targets, *, wait_seconds=0.0):
        targets = list(targets)[:MAX_TARGETS]
        if not targets:
            return []
        done, results, result_lock = threading.Event(), {}, threading.Lock()
        with self._lock:
            if self._closed:
                return [_result("version_detector_closed") for _ in targets]
            try:
                self._queue.put_nowait((targets, done, results, result_lock))
            except queue.Full:
                return [_result("metadata_worker_busy", status="pending") for _ in targets]
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="q22-software-metadata", daemon=True)
                self._thread.start()
        done.wait(max(0.0, min(wait_seconds, 0.5)))
        with result_lock:
            return [copy.deepcopy(results.get(index, _result("metadata_lookup_pending", status="pending"))) for index in range(len(targets))]

    def close(self):
        with self._lock:
            self._closed = True
            thread = self._thread
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.1)
