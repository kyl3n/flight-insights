"""Revision 2 reference evidence and fail-closed interlocks; no host mutations."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from q22_engine.network import NetworkService
from test_network import FakeHost, SOURCE, UDP, safe_observer, qualify_fake


class RevisionTwoNetworkTests(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.state = safe_observer()
        self.service = self.host.service()
        self.service.safety_state = lambda: copy.deepcopy(self.state)

    def plan(self, service=None):
        return (service or self.service).plan({"kind": "host_route", "source": SOURCE, "interface": "eth0"})

    @staticmethod
    def confirm(plan):
        return {"plan_id": plan["plan_id"], "confirm": True}

    def test_production_has_no_automatic_backend_even_with_admin_disarmed(self):
        service = NetworkService(system="Linux", runner=self.host.run, interface_provider=lambda: self.host.interfaces,
                                 elevated=lambda: True, safety_state=safe_observer)
        plan = self.plan(service)
        self.assertEqual(plan["state"], "ready")  # Reviewable preview, never execution authorization.
        self.assertFalse(plan["automatic_apply"])
        self.assertEqual(service.apply(self.confirm(plan))["state"], "diagnosis_only")
        self.assertEqual(len(self.host.routes), 2)
        self.assertFalse(any("add" in call or "del" in call for call in self.host.calls))
        service.plans[plan["plan_id"]]["applied"] = True
        self.assertEqual(service.rollback(self.confirm(plan))["state"], "diagnosis_only")

    def test_all_three_production_platforms_report_diagnosis_only(self):
        for system in ("Linux", "Windows", "Darwin"):
            with self.subTest(system=system):
                result = NetworkService(system=system).dispatch("network.capabilities", {})
                self.assertEqual(set(result["platforms"]), {"Linux", "Windows", "Darwin"})
                self.assertEqual(result["platforms"][system]["repair"]["state"], "diagnosis_only")
                self.assertFalse(result["platforms"][system]["repair"]["automatic_apply"])
                self.assertFalse(result["repair_safety"]["allowed"])
                self.assertIsNone(result["repair_safety"]["recording_active"])
                self.assertFalse(result["repair_safety"]["atomic_revocation"])

    def test_renderer_cannot_supply_qualification_or_disarmed_state(self):
        with self.assertRaises(ValueError):
            self.service.capabilities({"qualified": True})
        plan = self.plan()
        for method in (self.service.apply, self.service.rollback):
            with self.assertRaises(ValueError):
                method({**self.confirm(plan), "flight_state": "disarmed", "qualified": True})

    def test_partly_qualified_backend_is_not_executable(self):
        self.service.repair_backend.capabilities = lambda: {
            "state": "supported_automatic", "platform": "Linux", "qualified": True, "signed_helper": True,
            "manager_aware": False, "supervised_rollback": True}
        self.assertEqual(self.service.apply(self.confirm(self.plan()))["state"], "diagnosis_only")
        self.assertEqual(self.service.repair_backend.operations, [])

    def test_armed_stale_unknown_unverified_recording_all_block_both_directions(self):
        cases = [{"flight_state": "armed"}, {"flight_state": "unknown"}, {"flight_state": []}, {"heartbeat_age_s": 10 ** 1000}, {"heartbeat_age_s": 3.01},
                 {"heartbeat_age_s": None}, {"heartbeat_age_s": float("nan")}, {"heartbeat_age_s": -1},
                 {"heartbeat_age_s": True}, {"identity_verified": False}, {"recording_active": True},
                 {"recording_active": None}]
        for delta in cases:
            with self.subTest(delta=delta):
                self.state = {**safe_observer(), **delta}
                plan = self.plan()
                self.assertEqual(self.service.apply(self.confirm(plan))["state"], "safety_blocked")
                self.service.plans[plan["plan_id"]]["applied"] = True
                self.assertEqual(self.service.rollback(self.confirm(plan))["state"], "safety_blocked")
        self.assertEqual(self.service.repair_backend.operations, [])

    def test_observer_exception_or_missing_callback_blocks_mutation(self):
        for callback in (None, lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("offline"))):
            self.service.safety_state = callback
            result = self.service.apply(self.confirm(self.plan()))
            self.assertEqual(result["state"], "safety_blocked")
        self.assertEqual(self.service.repair_backend.operations, [])

    def test_arm_between_preview_and_apply_blocks(self):
        plan = self.plan()
        self.state["flight_state"] = "armed"
        self.assertEqual(self.service.apply(self.confirm(plan))["state"], "safety_blocked")
        self.assertEqual(self.service.repair_backend.operations, [])

    def test_stale_during_host_revalidation_blocks_before_backend(self):
        plan = self.plan()
        original = self.service.inspect
        def inspect(params):
            result = original(params)
            self.state["heartbeat_age_s"] = 30
            return result
        self.service.inspect = inspect
        result = self.service.apply(self.confirm(plan))
        self.assertEqual(result["state"], "safety_blocked")
        self.assertEqual(self.service.repair_backend.operations, [])
        self.assertEqual(len(self.host.routes), 2)

    def test_arm_during_execution_preserves_receipt_without_automatic_undo(self):
        plan = self.plan()
        original = self.service.repair_backend.execute
        def execute(*args):
            original(*args)
            self.state["flight_state"] = "armed"
        self.service.repair_backend.execute = execute
        result = self.service.apply(self.confirm(plan))
        self.assertEqual(result["state"], "safety_changed")
        self.assertTrue(result["may_have_applied"])
        self.assertTrue(self.service.plans[plan["plan_id"]]["applied"])
        self.assertEqual(len(self.service.repair_backend.operations), 1)
        self.assertEqual(self.service.rollback(self.confirm(plan))["state"], "safety_blocked")
        self.assertEqual(len(self.host.routes), 3)

    def test_arm_during_rollback_ownership_check_blocks_backend(self):
        plan = self.plan()
        self.assertEqual(self.service.apply(self.confirm(plan))["state"], "applied")
        original = self.service._owned
        def owned(value):
            result = original(value)
            self.state["flight_state"] = "armed"
            return result
        self.service._owned = owned
        self.assertEqual(self.service.rollback(self.confirm(plan))["state"], "safety_blocked")
        self.assertEqual(len(self.service.repair_backend.operations), 1)
        self.assertEqual(len(self.host.routes), 3)

    def test_arm_after_rollback_reports_observed_absence_and_blocks_further_work(self):
        plan = self.plan()
        self.service.apply(self.confirm(plan))
        original = self.service.repair_backend.execute
        def execute(*args):
            original(*args)
            self.state["flight_state"] = "armed"
        self.service.repair_backend.execute = execute
        result = self.service.rollback(self.confirm(plan))
        self.assertEqual(result["state"], "rolled_back")
        self.assertTrue(result["safety_changed"])
        self.assertFalse(result["may_have_applied"])
        self.assertEqual(len(self.host.routes), 2)

    def test_recovery_observes_without_auto_replay_and_cannot_bypass_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.host.service(data_dir=Path(directory))
            plan = self.plan(service)
            service.apply(self.confirm(plan))
            restarted = NetworkService(Path(directory), system="Linux", runner=self.host.run,
                                       interface_provider=lambda: self.host.interfaces, elevated=lambda: True)
            recovered = restarted.recovery({})["repairs"][0]
            self.assertEqual(recovered["ownership"], "owned")
            self.assertFalse(recovered["automatic_apply"])
            self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "diagnosis_only")
            qualify_fake(restarted)
            restarted.safety_state = lambda: {**safe_observer(), "heartbeat_age_s": 100}
            self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "safety_blocked")
            self.assertEqual(restarted.repair_backend.operations, [])

    def test_recorder_active_or_pending_overlap_uses_status_without_socket(self):
        for active in (True, False):
            for address in (UDP["local_address"], "0.0.0.0", "::"):
                self.state = {**safe_observer(), "recording_active": active,
                              "owned_udp": [{"local_address": address, "port": UDP["port"]}]}
                with patch.object(self.service, "_udp_probe") as probe:
                    result = self.service.probe({"sources": [UDP]})
                    self.assertEqual(result["probes"][0]["state"], "recorder_owned")
                    probe.assert_not_called()

    def test_unknown_recorder_state_defers_udp_and_distinct_bind_remains_available(self):
        for callback in (None, lambda: {}, lambda: {"owned_udp": [None]}, lambda: {"owned_udp": [{"local_address": "bad", "port": 14551}]}):
            self.service.safety_state = callback
            with patch.object(self.service, "_udp_probe") as probe:
                self.assertEqual(self.service.probe({"sources": [UDP]})["probes"][0]["state"], "recorder_state_unknown")
                probe.assert_not_called()
        self.service.safety_state = lambda: {**safe_observer(), "owned_udp": [{"local_address": UDP["local_address"], "port": 14552}]}
        with patch.object(self.service, "_udp_probe", return_value={"state": "no_packets", "checks": []}) as probe:
            self.service.probe({"sources": [UDP]})
            probe.assert_called_once()

    def test_q22_profile_declared_evidence_preserves_actual_handoff_decisions(self):
        with patch("q22_engine.network.socket.socket") as sock:
            reference = self.service.dispatch("network.profile", {"profile_id": "q22"})
            sock.assert_not_called()
        self.assertEqual(reference["status"], "reference_not_enrolled")
        self.assertEqual(reference["host"]["drone_ipv4"], "10.0.0.5")
        self.assertEqual(reference["ground_radio"]["ipv4"], "10.0.0.2")
        self.assertIsNone(reference["camera"]["ipv4"])
        self.assertEqual(reference["camera"]["model"], "SIYI A8 Mini")
        firewall = reference["windows_firewall_profile"]
        self.assertEqual((firewall["local_addresses"], firewall["profiles"], firewall["interface_type"]), ("Any", "Any", "wired"))
        self.assertEqual(firewall["remote_addresses"], ["10.0.0.2"])
        self.assertEqual(firewall["local_ports"], [5600, 14550])
        video, gcs, observer = reference["streams"]
        self.assertEqual(video["codec_evidence"], "handoff_declared")
        self.assertIsNone(video["observed_codec"])
        self.assertFalse(gcs["app_may_bind_direct_port"])
        self.assertIsNone(observer["destination_port"])
        qgc = reference["gcs_profiles"]["qgroundcontrol"]
        self.assertEqual(qgc["forwarded_parameter_requests"], "unsupported")
        self.assertEqual(qgc["forwarded_log_requests"], "unsupported")
        self.assertFalse(qgc["inherits_mission_planner_runtime"])
        reference["host"]["drone_ipv4"] = "1.2.3.4"
        self.assertEqual(self.service.profile({})["host"]["drone_ipv4"], "10.0.0.5")

    def test_q22_inspection_compares_evidence_without_enrolling_or_probing(self):
        with patch("q22_engine.network.socket.create_connection") as connect:
            evidence = self.service.inspect({"profile_id": "q22"})["profile_evidence"]
            connect.assert_not_called()
        self.assertEqual(evidence["observations"]["address_state"], "not_observed")
        self.assertEqual(evidence["observations"]["video_decode"], "not_observed")
        self.assertFalse(evidence["profile_changed"])
        self.host.interfaces[1]["ipv4"][0]["address"] = "10.0.0.5"
        result = self.service.inspect({"profile_id": "q22"})["profile_evidence"]
        self.assertEqual(result["observations"]["address_state"], "observed")
        self.assertEqual(result["observations"]["packet_source"], "not_observed")
        with self.assertRaises(ValueError):
            self.service.profile({"profile_id": "another-aircraft"})
        with self.assertRaises(ValueError):
            self.service.profile({"profile_id": "q22", "enroll": True})


if __name__ == "__main__":
    unittest.main()
