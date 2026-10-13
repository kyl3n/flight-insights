"""Native repair client boundaries using an explicitly fake enrolled service."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from q22_engine.network_helper import NativeRepairBackend, LinuxTransport
from q22_engine.network import NetworkService
from test_network import FakeHost, SOURCE, safe_observer


class FakeNativeTransport:
    peer_authenticated = True

    def __init__(self):
        self.calls = []
        self.plan = None
        self.records = []
        self.failure = False

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if self.failure:
            raise OSError("native service unavailable")
        if method == "capabilities":
            return {"protocol_version": 1, "platform": "Linux", "state": "bench_enrolled", "qualified": False,
                    "signed_helper": False, "admin_enrolled": True, "manager_aware": True,
                    "supervised_rollback": True, "supported_operations": ["host_route"]}
        if method == "recording_lease":
            return {"state": "renewed", "expires_in_ms": 3000}
        if method == "prepare":
            self.plan = {"plan_id": "a" * 32, "review_fingerprint": "b" * 64, "before_fingerprint": "c" * 64,
                         "candidate": copy.deepcopy(params["candidate"]), "expires_utc_ms": 9_999_999_999_999,
                         "rollback_after_ms": 30_000, "before": {"native": "fixture"}, "principal": "fixture-engine",
                         "created_utc_ms": 0}
            return copy.deepcopy(self.plan)
        if method in {"apply", "rollback"}:
            assert set(params) == {"plan_id", "review_fingerprint", "confirm"}
            assert params["plan_id"] == self.plan["plan_id"]
            record = {"plan": copy.deepcopy(self.plan), "state": "applied_temporary" if method == "apply" else "rolled_back",
                      "rollback_deadline_utc_ms": 9_999_999_999_999, "post_state": {"native": "fixture"}}
            self.records = [{"record": record, "ownership": "owned" if method == "apply" else "absent"}]
            return record
        if method == "recovery":
            return copy.deepcopy(self.records)
        raise AssertionError(method)


class NativeHelperTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeNativeTransport()
        self.backend = NativeRepairBackend(transport=self.transport, system="Linux")
        self.host = FakeHost()
        self.service = NetworkService(system="Linux", runner=self.host.run, interface_provider=lambda: self.host.interfaces,
                                      elevated=lambda: False, safety_state=safe_observer, repair_backend=self.backend)

    def tearDown(self):
        self.service.close()

    def plan(self):
        return self.service.plan({"kind": "host_route", "source": SOURCE, "interface": "eth0"})

    def test_constructor_is_passive_and_default_requires_opt_in(self):
        self.assertEqual(self.transport.calls, [])
        caps = self.service.capabilities({})["platforms"]["Linux"]["repair"]
        self.assertEqual(caps["state"], "experimental_available")
        self.assertFalse(caps["automatic_apply"])
        self.assertFalse(caps["qualified"])
        self.assertFalse(caps["signed_helper"])
        plan = self.plan()
        self.assertNotIn("native_review", plan)
        result = self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "native_review_required")

    def test_os_peer_authentication_cannot_be_declared_in_json(self):
        self.transport.peer_authenticated = False
        self.assertFalse(self.backend.capabilities()["bench_available"])
        with self.assertRaises(ValueError):
            self.service.bench_opt_in({"enabled": True})

    def test_opt_in_native_review_exact_confirmation_and_unprivileged_engine(self):
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        self.assertFalse(plan["requires_elevation"])
        self.assertEqual(plan["commands"], [])
        self.assertEqual(plan["rollback_commands"], [])
        self.assertEqual(plan["native_review"]["authority"], "admin_enrolled_helper")
        confirmed = {"plan_id": plan["plan_id"], "confirm": True}
        result = self.service.apply(confirmed)
        self.assertEqual(result["state"], "applied")
        self.assertIn("rollback_deadline_utc_ms", result)
        self.assertEqual(self.service.rollback(confirmed)["state"], "rolled_back")
        self.assertFalse(any("add" in argv or "del" in argv for argv in self.host.calls))
        operations = [(method, params) for method, params in self.transport.calls if method in {"apply", "rollback"}]
        self.assertEqual([method for method, _ in operations], ["apply", "rollback"])
        self.assertTrue(all(params["plan_id"] == "a" * 32 for _, params in operations))

    def test_renderer_cannot_supply_authority_or_fingerprint(self):
        for extra in ({"qualified": True}, {"flight_state": "disarmed"}, {"journal_path": "/tmp/forged"}):
            with self.assertRaises(ValueError):
                self.service.bench_opt_in({"enabled": True, **extra})
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        with self.assertRaises(ValueError):
            self.service.apply({"plan_id": plan["plan_id"], "confirm": True, "review_fingerprint": "b" * 64})

    def test_opt_out_stops_explicit_changes_and_restarts_reset_opt_in(self):
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        self.service.bench_opt_in({"enabled": False})
        result = self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "diagnosis_only")
        fresh = NativeRepairBackend(transport=self.transport, system="Linux")
        self.assertFalse(fresh.capabilities()["automatic_apply"])

    def test_native_safety_remains_independent_and_engine_blocks_armed(self):
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        self.service.safety_state = lambda: {**safe_observer(), "flight_state": "armed"}
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "safety_blocked")
        self.assertFalse(any(method == "apply" for method, _ in self.transport.calls))

    def test_native_recovery_uses_only_authenticated_helper_records(self):
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        restarted = NetworkService(system="Linux", safety_state=safe_observer,
                                   repair_backend=NativeRepairBackend(transport=self.transport, system="Linux"))
        result = restarted.recovery({})
        recovered = result["repairs"][0]
        self.assertEqual(recovered["ownership"], "owned")
        self.assertFalse(recovered["automatic_apply"])
        self.assertEqual(recovered["rollback_deadline_utc_ms"], 9_999_999_999_999)
        self.assertEqual(restarted.apply({"plan_id": recovered["plan_id"], "confirm": True})["state"], "recovered_receipt")
        restarted.bench_opt_in({"enabled": True})
        self.assertEqual(restarted.rollback({"plan_id": recovered["plan_id"], "confirm": True})["state"], "rolled_back")
        restarted.close()

    def test_mismatched_native_plan_or_receipt_is_not_adopted(self):
        self.service.bench_opt_in({"enabled": True})
        plan = self.plan()
        self.transport.plan["candidate"]["interface"] = "foreign"
        result = self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["may_have_applied"])

    def test_missing_service_does_not_create_or_elevate_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sock"
            with patch("q22_engine.network_helper.SOCKET_PATH", missing):
                backend = NativeRepairBackend(transport=LinuxTransport(), system="Linux")
                self.assertEqual(backend.capabilities()["state"], "diagnosis_only")
                self.assertFalse(missing.exists())

    def test_native_unknown_recording_callback_renews_blocked_lease(self):
        self.backend.safety_state = lambda: {}
        self.backend.opt_in(True)
        leases = [params for method, params in self.transport.calls if method == "recording_lease"]
        self.assertTrue(leases[-1]["active"])

    def test_recording_reservation_survives_opt_out_and_failed_after_ack(self):
        self.backend.safety_state = safe_observer
        self.backend.opt_in(True)
        self.backend.opt_in(False)
        self.backend.before_recording()
        self.backend._renew_lease()
        self.assertTrue([p for m, p in self.transport.calls if m == "recording_lease"][-1]["active"])
        self.transport.failure = True
        self.backend.after_recording()  # Does not mask a successful worker start.
        self.assertFalse(self.backend._recording_reserved)
        with self.assertRaises(OSError):
            self.backend.before_recording()

    def test_authenticated_native_safety_replaces_stopped_collection_heartbeat(self):
        original = self.transport.request
        def request(method, params):
            result = original(method, params)
            if method == "capabilities":
                result["repair_safety"] = {"flight_state": "disarmed", "heartbeat_age_s": 0.1,
                                           "identity_verified": True, "recording_active": False}
            return result
        self.transport.request = request
        self.service.safety_state = lambda: {**safe_observer(), "flight_state": "unknown", "heartbeat_age_s": 100}
        safety = self.service._repair_safety()
        self.assertTrue(safety["allowed"])
        self.assertEqual(safety["authority"], "native_observer_and_engine_recording")
        self.service.safety_state = lambda: {**safe_observer(), "recording_active": True}
        self.assertFalse(self.service._repair_safety()["allowed"])


if __name__ == "__main__":
    unittest.main()
