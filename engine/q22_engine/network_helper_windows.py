"""Fixed Windows pipe transport for the administrator-enrolled bench helper.

Import-safe on other platforms. Trust is established from the SCM service PID,
protected fixed-path enrollment/executable, and pipe-server PID before any request.
No service installation, elevation, arbitrary pipe, or command API is exposed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

PIPE = r"\\.\pipe\Q22NetworkRepair-v1"
SERVICE = "Q22NetworkRepair"
_TRUSTED = {"S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
_METHODS = {"capabilities", "prepare", "apply", "rollback", "recovery", "recording_lease"}


class WindowsTransport:
    peer_authenticated = False

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("The native Windows helper transport requires Windows.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "WaitNamedPipeW": ([w.LPCWSTR, w.DWORD], w.BOOL),
            "GetNamedPipeServerProcessId": ([w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
            "SetNamedPipeHandleState": ([w.HANDLE, ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
            "ReadFile": ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
            "WriteFile": ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
            "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        signatures = {
            "OpenSCManagerW": ([w.LPCWSTR, w.LPCWSTR, w.DWORD], w.HANDLE),
            "OpenServiceW": ([w.HANDLE, w.LPCWSTR, w.DWORD], w.HANDLE),
            "CloseServiceHandle": ([w.HANDLE], w.BOOL),
            "QueryServiceStatusEx": ([w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            "QueryServiceConfigW": ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            "GetNamedSecurityInfoW": ([w.LPWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], w.DWORD),
            "GetSecurityInfo": ([w.HANDLE, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], w.DWORD),
            "GetAce": ([ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
            "ConvertSidToStringSidW": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.advapi, name)
            function.argtypes, function.restype = args, result
        self.shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, w.DWORD, w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHGetKnownFolderPath.restype = ctypes.c_long
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    def _folder(self, identity: str) -> Path:
        guid = ctypes.create_string_buffer(uuid.UUID(identity).bytes_le)
        pointer = ctypes.c_void_p()
        if self.shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(pointer)) < 0:
            raise OSError("Windows known folder unavailable.")
        try:
            return Path(ctypes.wstring_at(pointer))
        finally:
            self.ole.CoTaskMemFree(pointer)

    def _sid(self, pointer: ctypes.c_void_p) -> str:
        text = ctypes.c_void_p()
        if not self.advapi.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            raise OSError("Native security identifier unavailable.")
        try:
            return ctypes.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    def _acl(self, target: Path | int, *, handle: bool = False, leaf: bool = True) -> None:
        owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        getter = self.advapi.GetSecurityInfo if handle else self.advapi.GetNamedSecurityInfoW
        code = getter(target if handle else str(target), 2 if handle else 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        if code:
            raise OSError(f"Installed security descriptor unavailable ({code}).")
        try:
            if not owner.value or self._sid(owner) not in _TRUSTED or not acl.value:
                raise OSError("Installed service/file is not administrator-controlled.")
            count = ctypes.c_ushort.from_address(acl.value + 4).value
            if count > 4096:
                raise OSError("Installed ACL exceeds bounds.")
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.advapi.GetAce(acl, index, ctypes.byref(ace)):
                    raise OSError("Installed ACL entry unavailable.")
                kind, flags = ctypes.string_at(ace, 2)
                if flags & 8:  # INHERIT_ONLY_ACE
                    continue
                if kind == 0:
                    mask = ctypes.c_uint32.from_address(ace.value + 4).value
                    sid = self._sid(ctypes.c_void_p(ace.value + 8))
                    forbidden = 0x500D0002 if handle else (0x500D0156 if leaf else 0x500D0150)
                    if sid not in _TRUSTED and mask & forbidden:
                        raise OSError("Untrusted principal can change installed service/file.")
                elif kind != 1:
                    raise OSError("Unsupported installed ACL; cannot establish trust.")
        finally:
            self.kernel.LocalFree(descriptor)

    def _protected(self, path: Path) -> None:
        for index, part in enumerate([path, *path.parents]):
            metadata = part.lstat()
            if metadata.st_file_attributes & 0x400:
                raise OSError("Installed path contains a reparse point.")
            if index == 0 and (not part.is_file() or metadata.st_nlink != 1):
                raise OSError("Installed file must be ordinary and have one link.")
            self._acl(part, leaf=index == 0)

    def _authenticate(self, pipe: int) -> None:
        root = self._folder("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97") / "Q22" / "NetworkRepair"
        config_path = root / "enrollment.json"
        self._protected(config_path)
        with config_path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise OSError("Enrollment exceeds bounds.")
        policy = json.loads(raw)
        expected_hash = policy.get("helper_sha256", "")
        if policy.get("version") != 1 or policy.get("bench_enrollment") is not True or len(expected_hash) != 64:
            raise OSError("Unsupported native enrollment.")
        expected_image = self._folder("905e63b6-c1bf-494e-b29c-65b732d3d21a") / "Q22" / "NetworkRepair" / "q22-network-helper.exe"
        pid = w.ULONG()
        if not self.kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(pid)) or not pid.value:
            raise OSError("Native pipe server identity unavailable.")
        manager = self.advapi.OpenSCManagerW(None, None, 1)
        if not manager:
            raise OSError("Windows service manager unavailable.")
        service = None
        try:
            service = self.advapi.OpenServiceW(manager, SERVICE, 0x20005)  # READ_CONTROL, QUERY_CONFIG, QUERY_STATUS
            if not service:
                raise OSError("Installed helper service is unavailable.")
            self._acl(service, handle=True)
            status = (w.DWORD * 9)()
            needed = w.DWORD()
            if not self.advapi.QueryServiceStatusEx(service, 0, status, ctypes.sizeof(status), ctypes.byref(needed)) or status[1] != 4 or status[7] != pid.value:
                raise OSError("Pipe server is not the running installed helper service.")
            class Config(ctypes.Structure):
                _fields_ = [("service_type", w.DWORD), ("start_type", w.DWORD), ("error_control", w.DWORD), ("binary", w.LPWSTR), ("group", w.LPWSTR), ("tag", w.DWORD), ("dependencies", w.LPWSTR), ("account", w.LPWSTR), ("display", w.LPWSTR)]
            buffer = ctypes.create_string_buffer(65536)
            if not self.advapi.QueryServiceConfigW(service, buffer, len(buffer), ctypes.byref(needed)):
                raise OSError("Installed service configuration unavailable.")
            config = ctypes.cast(buffer, ctypes.POINTER(Config)).contents
            if config.service_type != 0x10 or config.account not in {"LocalSystem", r"NT AUTHORITY\SYSTEM"} or config.binary.casefold() != f'"{expected_image}" --service'.casefold():
                raise OSError("Unexpected helper service account or command.")
        finally:
            if service:
                self.advapi.CloseServiceHandle(service)
            self.advapi.CloseServiceHandle(manager)
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise OSError("Helper process image unavailable.")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = w.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)) or Path(buffer.value).resolve() != expected_image.resolve():
                raise OSError("Helper process image differs from installed path.")
        finally:
            self.kernel.CloseHandle(process)
        self._protected(expected_image)
        digest = hashlib.sha256()
        with expected_image.open("rb") as image:
            if os.fstat(image.fileno()).st_size > 512 * 1024 * 1024:
                raise OSError("Installed helper image exceeds bounds.")
            while block := image.read(65536):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise OSError("Helper image hash differs from administrator enrollment.")

    def request(self, method: str, params: dict) -> object:
        if method not in _METHODS or not isinstance(params, dict):
            raise ValueError("Unsupported native helper request.")
        frame = json.dumps({"method": method, "params": params}, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(frame) > 65536:
            raise ValueError("Native request exceeds bounds.")
        # Separate connection for every call keeps lease renewal independent of
        # the helper's serialized native write/checkpoint operations.
        self.kernel.WaitNamedPipeW(PIPE, 3000)
        pipe = self.kernel.CreateFileW(PIPE, 0x0012018B, 0, None, 3, 0x00110000, None)  # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
        if pipe == ctypes.c_void_p(-1).value:
            self.peer_authenticated = False
            raise OSError("Administrator-enrolled native helper pipe unavailable.")
        try:
            self._authenticate(pipe)
            mode = w.DWORD(1)  # nonblocking byte pipe; bound reads ourselves
            if not self.kernel.SetNamedPipeHandleState(pipe, ctypes.byref(mode), None, None):
                raise OSError("Native pipe mode unavailable.")
            # Identification-level impersonation is required by server SID check.
            written = w.DWORD()
            if not self.kernel.WriteFile(pipe, frame, len(frame), ctypes.byref(written), None) or written.value != len(frame):
                raise OSError("Native request write failed.")
            deadline = time.monotonic() + (3 if method in {"capabilities", "recording_lease"} else 60)
            response = bytearray()
            buffer = ctypes.create_string_buffer(16384)
            while time.monotonic() < deadline:
                count = w.DWORD()
                ok = self.kernel.ReadFile(pipe, buffer, len(buffer), ctypes.byref(count), None)
                if count.value:
                    response.extend(buffer.raw[:count.value])
                    if len(response) > 1_048_576:
                        raise OSError("Native response exceeds bounds.")
                    if b"\n" in response:
                        break
                elif not ok and ctypes.get_last_error() not in {232, 233, 109}:
                    raise OSError("Native response read failed.")
                elif not ok and ctypes.get_last_error() == 109:
                    raise OSError("Native helper closed without a response.")
                time.sleep(0.01)
            if not response.endswith(b"\n") or response.count(b"\n") != 1:
                raise OSError("Native response timeout or invalid framing.")
            result = json.loads(response)
            self.peer_authenticated = True
            if not isinstance(result, dict) or set(result) not in ({"result"}, {"error"}):
                raise OSError("Invalid native response envelope.")
            if "error" in result:
                raise OSError(str(result["error"])[:1000])
            if set(result) != {"result"}:
                raise OSError("Invalid native response envelope.")
            return result["result"]
        except (OSError, ValueError):
            self.peer_authenticated = False
            raise
        finally:
            self.kernel.CloseHandle(pipe)
