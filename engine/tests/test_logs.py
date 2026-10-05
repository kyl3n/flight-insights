"""Real-wire synthetic fixtures validate evidence flow, not Q22 hardware."""
from concurrent.futures import CancelledError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from q22_engine.logs import LogService
from q22_engine.parameters import ParameterService
from q22_engine.store import Store


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/logs"


class LogServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.service = LogService(self.store)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.temp.cleanup()

    def imported(self, format="bin", path=None):
        return self.service.dispatch("logs.import", {
            "path": str(path or FIXTURES / f"synthetic-flight.{format}"),
            "label": f"Synthetic {format}", "synthetic": True})["log"]

    def scoped(self, log, **changes):
        return self.service.dispatch("logs.session", {
            "log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
            "battery_id": "0", "aircraft_id": "synthetic-aircraft", "board": "synthetic-board",
            "firmware": "synthetic-exact-build", "label": "Synthetic selected interval",
            "phase": "operator-selected synthetic phase", "interval_start_s": 1,
            "interval_end_s": 2.5, "max_gap_s": 1.5, "conditions": {}, **changes})["session"]

    def test_bin_and_text_imports_match_scoped_metrics_and_reopen(self):
        sessions = []
        for format in ("bin", "log"):
            log = self.imported(format)
            self.assertEqual(log["format"], format)
            self.assertEqual([s["boot_epoch"] for s in log["streams"]], [0, 1])
            session = self.scoped(log)
            self.assertEqual(session["metrics"]["mean_power_w"]["value"], 240)
            self.assertAlmostEqual(session["metrics"]["energy_wh"]["value"], .1)
            self.assertEqual(session["metrics"]["roll_tracking_rmse_deg"]["value"], 2)
            self.assertEqual(session["source"]["sha256"], log["source"]["sha256"])
            self.assertEqual(session["parameter_completeness"], "observed_only")
            sessions.append(session)
        reopened = ParameterService(Store(self.store.root)).dispatch("sessions.list", {})["sessions"]
        self.assertEqual({s["id"] for s in reopened}, {s["id"] for s in sessions})

    def test_timeline_preserves_changes_and_snapshot_is_at_start_not_final(self):
        log = self.imported()
        detail = self.service.dispatch("logs.detail", {"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0})
        observations = detail["parameter_observations"]
        self.assertEqual([row["time_s"] for row in observations], [0, 3])
        self.assertTrue(all(isinstance(row["time_us"], str) for row in observations))
        before, after = self.scoped(log), self.scoped(log, interval_start_s=3, interval_end_s=5)
        a = self.store.get_document("snapshot", before["snapshot_id"])
        b = self.store.get_document("snapshot", after["snapshot_id"])
        self.assertAlmostEqual(float(a["parameters"]["ATC_RAT_RLL_P"]["value"]), .1)
        self.assertAlmostEqual(float(b["parameters"]["ATC_RAT_RLL_P"]["value"]), .12)
        result = ParameterService(self.store).dispatch("comparison.run", {
            "before_session_id": before["id"], "after_session_id": after["id"]})
        self.assertEqual(result["parameter_diff"]["counts"]["changed"], 1)
        self.assertEqual(result["status"], "confounded")
        self.assertIn("before.parameter_evidence", [c["field"] for c in result["confounders"]])

    def test_crossing_parameter_change_abstains_and_reports_split_boundary(self):
        session = self.scoped(self.imported(), interval_end_s=5)
        self.assertEqual(session["parameter_changes"], "present")
        self.assertEqual(session["parameter_split_points_s"], [3])
        for metric in session["metrics"].values():
            self.assertIsNone(metric["value"])
            self.assertIn("crosses parameter", metric["unavailable_reason"])

    def test_tlog_two_vehicles_and_clock_epochs_never_merge(self):
        log = self.imported("tlog")
        self.assertEqual([(s["source_key"], s["boot_epoch"]) for s in log["streams"]],
                         [("1:1", 0), ("2:1", 0), ("1:1", 1)])
        detail = self.service.dispatch("logs.detail", {"log_id": log["id"], "source_key": "2:1", "boot_epoch": 0})
        self.assertEqual(len(detail["parameter_observations"]), 1)
        self.assertAlmostEqual(detail["parameter_observations"][0]["value"], .2)
        first = self.scoped(log, source_key="1:1", battery_id="battery_status:0", interval_start_s=.75, interval_end_s=2.25)
        self.assertAlmostEqual(first["metrics"]["mean_power_w"]["value"], 240)
        self.assertIsNone(first["metrics"]["roll_tracking_rmse_deg"]["value"])
        reboot = self.scoped(log, source_key="1:1", boot_epoch=1, battery_id="battery_status:0", interval_start_s=0, interval_end_s=.25)
        self.assertIsNone(reboot["metrics"]["energy_wh"]["value"])
        snap = self.store.get_document("snapshot", reboot["snapshot_id"])
        self.assertEqual(snap["parameter_count"], 0)

    def test_crc_or_unknown_text_record_creates_integration_barrier(self):
        original = (FIXTURES / "synthetic-flight.log").read_text()
        damaged = self.root / "damaged.log"
        damaged.write_text(original.replace("PARM, 3000000,", "CORRUPTED_UNRECOGNIZED_RECORD\nPARM, 3000000,"))
        log = self.imported("log", damaged)
        self.assertEqual(log["status"], "partial")
        session = self.scoped(log)
        metric = session["metrics"]["energy_wh"]
        self.assertAlmostEqual(metric["value"], 240 / 3600)
        self.assertEqual(metric["covered_duration_s"], 1)
        self.assertIn("corruption_or_clock_boundary", [e["reason"] for e in metric["exclusions"]])

    def test_missing_units_do_not_turn_into_assumed_volts_amps(self):
        path = self.root / "no-units.log"
        path.write_text("\n".join(line for line in (FIXTURES / "synthetic-flight.log").read_text().splitlines()
                                   if not line.startswith("FMTU,")) + "\n")
        log = self.imported("log", path)
        self.assertFalse(log["capabilities"]["power"])
        session = self.scoped(log)
        self.assertIsNone(session["metrics"]["mean_power_w"]["value"])
        self.assertEqual(session["metrics"]["energy_wh"]["covered_duration_s"], 0)

    def test_unknown_file_is_archived_and_shows_unsupported(self):
        path = self.root / "unknown.vendor"
        path.write_bytes(b"unrecognized vendor data preserved exactly")
        log = self.imported(path=path)
        self.assertEqual(log["status"], "unsupported")
        self.assertFalse(log["capabilities"]["power"])
        self.assertEqual(self.store.raw_path(log["source"]["sha256"]).read_bytes(), path.read_bytes())

    def test_timeline_pagination_is_bounded(self):
        log = self.imported()
        page = self.service.dispatch("logs.detail", {"log_id": log["id"], "timeline_limit": 1})
        self.assertEqual(len(page["parameter_observations"]), 1)
        self.assertEqual(page["timeline_next_offset"], 1)
        second = self.service.dispatch("logs.detail", {"log_id": log["id"], "timeline_offset": 1, "timeline_limit": 1})
        self.assertEqual(second["parameter_observations"][0]["time_s"], 3)
        self.assertIsNone(second["timeline_next_offset"])

    def test_cancel_does_not_publish_half_decoded_log(self):
        ticks = 0
        def cancel():
            nonlocal ticks
            ticks += 1
            return ticks > 8
        with self.assertRaises(CancelledError):
            self.service.dispatch("logs.import", {"path": str(FIXTURES / "synthetic-flight.bin"),
                                                   "label": "cancel", "synthetic": True}, cancel_check=cancel)
        self.assertEqual(self.service.dispatch("logs.list", {})["logs"], [])
        self.assertFalse(list(self.service.derived.glob(".*.parquet")))

    def test_modified_derived_values_cannot_appear_as_trusted_timeline(self):
        log = self.imported()
        self.service.dispatch("logs.detail", {"log_id": log["id"]})  # populate integrity cache
        document = self.store.get_document("log", log["id"])
        path = self.service.artifact(document) / document["derived_shards"][0]["name"]
        with path.open("ab") as handle:
            handle.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.service.dispatch("logs.detail", {"log_id": log["id"]})

    def test_shards_preserve_record_order_and_snapshot_scope(self):
        with patch("q22_engine.logs.SHARD_ROWS", 1):
            # Force one flush group per shard without changing decoder semantics.
            path = self.root / "many.log"
            content = (FIXTURES / "synthetic-flight.log").read_text()
            header = "\n".join(line for line in content.splitlines() if not line.startswith(("BAT,", "ATT,", "PARM,")))
            with path.open("w") as output:
                output.write(header + "\nPARM, 0, ATC_RAT_RLL_P, 0.1, 0.1\n")
                for index in range(1, 2500):
                    output.write(f"BAT, {index * 1000000}, 0, 24, 10, 20, 80, 100\n")
                output.write("PARM, 2500000000, ATC_RAT_RLL_P, 0.12, 0.1\n")
            log = self.imported("log", path)
        document = self.store.get_document("log", log["id"])
        self.assertGreater(len(document["derived_shards"]), 1)
        events = list(self.service.events(document, document["streams"][0], ["parameter"]))
        self.assertEqual([event["time_us"] for event in events], [0, 2500000000])
        session = self.scoped(log)
        self.assertEqual(session["metrics"]["mean_power_w"]["value"], 240)

    def test_unknown_monitor_identity_never_becomes_selectable_none(self):
        path = self.root / "unknown-monitor.log"
        text = (FIXTURES / "synthetic-flight.log").read_text()
        # No FMTU # marker and no conventional instance field name.
        text = text.replace("TimeUS,Inst,Volt,Curr", "TimeUS,Unknown,Volt,Curr").replace("s#vAO--", "s-vAO--")
        path.write_text(text)
        log = self.imported("log", path)
        self.assertEqual(log["streams"][0]["battery_ids"], [])
        session = self.scoped(log, battery_id=None)
        self.assertIsNone(session["metrics"]["mean_power_w"]["value"])

    def test_requires_explicit_source_boot_and_available_monitor(self):
        log = self.imported()
        with self.assertRaisesRegex(ValueError, "source_key"):
            self.scoped(log, source_key=None)
        with self.assertRaisesRegex(ValueError, "battery monitor"):
            self.scoped(log, battery_id="not-a-monitor")


if __name__ == "__main__":
    unittest.main()
