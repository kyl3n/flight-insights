"""Local installer input guards only; never install/start an elevated service."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux installer input guards")
ROOT = Path(__file__).resolve().parents[2]


def installer():
    spec = importlib.util.spec_from_file_location("q22_linux_installer", ROOT / "native-repair/scripts/install-linux.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_bounded_read_rejects_symlink_hardlink_and_size(tmp_path):
    install = installer()
    original = tmp_path / "input"
    original.write_bytes(b"fixed input")
    assert install.regular(original, 64) == b"fixed input"
    with pytest.raises(ValueError, match="bounded"):
        install.regular(original, 2)
    link = tmp_path / "symlink"
    link.symlink_to(original)
    with pytest.raises(OSError):
        install.regular(link, 64)
    hardlink = tmp_path / "hardlink"
    os.link(original, hardlink)
    with pytest.raises(ValueError, match="single-link"):
        install.regular(original, 64)
    fifo = tmp_path / "not-a-regular-input"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="ordinary"):
        install.regular(fifo, 64)


def test_installer_refuses_user_controlled_installed_engine_ancestry(tmp_path):
    install = installer()
    image = tmp_path / "q22-engine"
    image.write_bytes(b"synthetic ELF fixture only")
    with pytest.raises(ValueError, match="Root ownership"):
        install.regular(image, 1024, protected=True)


def test_installer_requires_exact_reviewed_hashes_and_dedicated_observer(tmp_path, monkeypatch):
    install = installer()
    engine = tmp_path / "q22-engine"
    engine.write_bytes(b"synthetic frozen-image bytes; no executable fixture")
    # Isolate policy validation from root filesystem ownership; no apply path or
    # subprocess is called by this test.
    monkeypatch.setattr(install, "protected_ancestry", lambda path: None)
    monkeypatch.setattr(install, "service_visible", lambda path, roots: None)
    monkeypatch.setattr(install, "engine_access", lambda path: None)
    monkeypatch.setattr(install.pwd, "getpwuid", lambda uid: object())
    policy = {"version": 1, "client_principal": "1000", "client_image": str(engine),
        "client_sha256": install.digest(engine.read_bytes()), "helper_sha256": "a" * 64,
        "allowed_interfaces": ["Ethernet"], "devices": [{"host": "10.0.0.2", "verification_port": 80}],
        "internet_probe": {"host": "192.0.2.1", "port": 443}, "bench_enrollment": True,
        "observer": {"local_address": "10.0.0.1", "port": 14660, "sender": "10.0.0.2",
            "system_id": 1, "component_id": 1, "expected_uid": None,
            "require_signed": False, "signing_key_path": None}}
    assert install.enrollment(json.dumps(policy), engine, "a" * 64) == policy
    policy["observer"].update(local_address="127.0.0.1", sender="127.0.0.1")
    assert install.enrollment(json.dumps(policy), engine, "a" * 64) == policy
    for target in (policy["devices"][0], policy["internet_probe"]):
        original = target["host"]
        target["host"] = "127.0.0.1"
        with pytest.raises(ValueError, match="only for configured observer"):
            install.enrollment(json.dumps(policy), engine, "a" * 64)
        target["host"] = original
    with pytest.raises(ValueError, match="hashes"):
        install.enrollment(json.dumps(policy), engine, "b" * 64)
    policy["observer"]["port"] = 14550
    with pytest.raises(ValueError, match="dedicated"):
        install.enrollment(json.dumps(policy), engine, "a" * 64)


def test_installer_exclusive_write_never_overwrites_prior_evidence(tmp_path):
    install = installer()
    target = tmp_path / "receipt"
    target.write_bytes(b"prior immutable review")
    with pytest.raises(FileExistsError):
        install.new_file(target, b"replacement", 0o600)
    assert target.read_bytes() == b"prior immutable review"


def test_installer_rejects_service_hidden_home_paths_even_if_root_protected():
    install = installer()
    for path, roots in ((Path("/root/bin/q22-engine"), (Path("/usr"), Path("/opt"))),
            (Path("/root/observer-signing.key"), (Path("/etc"),))):
        with pytest.raises(ValueError, match="Service-visible"):
            install.service_visible(path, roots)
    install.service_visible(Path("/usr/bin/q22-engine"), (Path("/usr"), Path("/opt")))
    install.service_visible(Path("/etc/q22-observer-signing.key"), (Path("/etc"),))


@pytest.mark.parametrize("mode", [0o644, 0o700])
def test_installer_rejects_images_desktop_principal_cannot_read_and_execute(tmp_path, mode):
    install = installer()
    image = tmp_path / "q22-engine"
    image.write_bytes(b"synthetic image metadata fixture")
    image.chmod(mode)
    with pytest.raises(ValueError, match="public read/execute"):
        install.engine_access(image)


def test_review_and_wrong_approval_never_mutate_host_and_bind_startup_flags(monkeypatch, capsys):
    install = installer()
    monkeypatch.setattr(install, "regular", lambda path, *args, **kwargs: b"\x7fELF synthetic review-only bytes")
    monkeypatch.setattr(install, "enrollment", lambda *args: {"synthetic_policy_fixture": True})
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)

    def mutation(*args, **kwargs):
        raise AssertionError("Review or incorrect approval attempted a host mutation")

    monkeypatch.setattr(install, "new_file", mutation)
    monkeypatch.setattr(install, "systemctl", mutation)
    monkeypatch.setattr(Path, "mkdir", mutation)
    arguments = ["install-linux.py", "--helper", "/synthetic/helper", "--engine",
        "/usr/bin/q22-engine", "--enrollment", "/synthetic/enrollment.json"]
    monkeypatch.setattr(sys, "argv", arguments)
    install.main()
    default = json.loads(capsys.readouterr().out)
    assert default["apply_requested"] is False
    assert default["plan"]["start_service"] is False
    monkeypatch.setattr(sys, "argv", arguments + ["--start", "--enable"])
    install.main()
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["plan_sha256"] != default["plan_sha256"]
    assert enabled["plan"]["enable_at_boot"] is True
    # Approval of the non-starting plan cannot authorize the changed service plan.
    monkeypatch.setattr(sys, "argv", arguments + ["--start", "--enable", "--apply",
        "--approve-plan-sha256", default["plan_sha256"]])
    with pytest.raises(ValueError, match="exact --approve-plan-sha256"):
        install.main()
