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
def test_windows_known_folder_and_protected_system_image():
    transport = WindowsTransport()
    program_files = transport._folder("905e63b6-c1bf-494e-b29c-65b732d3d21a")
    assert program_files.is_absolute() and program_files.is_dir()
    system = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    transport._protected(system)


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows ACL APIs")
def test_user_writable_image_cannot_be_enrolled(tmp_path):
    path = tmp_path / "q22-network-helper.exe"
    path.write_bytes(b"MZsynthetic")
    with pytest.raises(OSError):
        WindowsTransport()._protected(path)
