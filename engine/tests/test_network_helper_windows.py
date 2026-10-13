"""Platform boundary checks; no service installation or network mutations."""
import os
from pathlib import Path
import pytest
from q22_engine.network_helper_windows import WindowsTransport


def test_transport_is_import_safe_and_cannot_activate_on_other_platforms():
    if os.name != "nt":
        with pytest.raises(RuntimeError, match="requires Windows"):
            WindowsTransport()


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows ACL APIs")
def test_windows_known_folder_and_readonly_system_acl():
    transport = WindowsTransport()
    program_files = transport._folder("905e63b6-c1bf-494e-b29c-65b732d3d21a")
    assert program_files.is_absolute() and program_files.is_dir()
    system = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # Windows servicing legitimately hard-links system executables into WinSxS.
    # Exercise actual ACL APIs independently of the stricter enrollment rule,
    # which still requires a single-link helper image.
    for index, part in enumerate([system, *system.parents]):
        assert not part.lstat().st_file_attributes & 0x400
        transport._acl(part, leaf=index == 0)


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows file link metadata")
def test_hardlinked_image_is_rejected_before_acl_enrollment(tmp_path):
    original = tmp_path / "original.exe"
    original.write_bytes(b"MZsynthetic")
    linked = tmp_path / "q22-network-helper.exe"
    os.link(original, linked)
    assert linked.stat().st_nlink == 2
    with pytest.raises(OSError, match="ordinary and have one link"):
        WindowsTransport()._protected(linked)


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows ACL APIs")
def test_user_writable_image_cannot_be_enrolled(tmp_path):
    path = tmp_path / "q22-network-helper.exe"
    path.write_bytes(b"MZsynthetic")
    with pytest.raises(OSError):
        WindowsTransport()._protected(path)
