from pathlib import Path
import json
import math
import tempfile
import unittest

from q22_engine.parameters import ParameterService, parse_parameters, performance_metrics
from q22_engine.store import Store


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


class ParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store")
        self.service = ParameterService(self.store)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.tmp.cleanup()

    def snapshot(self, file="synthetic-before.param", **extra):
        args = {"path": str(FIXTURES / "parameters" / file), "label": "Synthetic fixture",
                "aircraft_id": "synthetic-aircraft", "board": "synthetic-board",
                "firmware": "synthetic-exact-build-1", "source_kind": "flight_readback",
                "observed_at": "2026-01-01T00:00:00Z", "synthetic": True, **extra}
        return self.service.dispatch("parameters.import", args)["snapshot"]

    def session(self, snapshot, file="synthetic-constant.csv", **extra):
        args = {"path": str(FIXTURES / "performance" / file), "label": "Synthetic flight",
                "aircraft_id": snapshot["aircraft_id"], "board": snapshot["board"],
                "firmware": snapshot["firmware"], "snapshot_id": snapshot["id"],
                "phase": "operator-confirmed hover", "interval_start_s": 0,
                "interval_end_s": 4, "max_gap_s": 1.5, "synthetic": True,
                "parameter_evidence": "operator_attested", "parameter_evidence_note": "Synthetic test assertion only",
                "parameter_changes": "none_reported", "conditions": {
                    "mass_kg": 10, "payload_kg": 0, "battery_id": "synthetic-pack",
                    "battery_soc_pct": 80, "battery_temperature_c": 20, "wind_mps": 0,
                    "hardware_revision": "synthetic-v1", "props": "synthetic-props"}, **extra}
        return self.service.dispatch("sessions.import", args)["session"]

    def compare(self, before, after):
        return self.service.dispatch("comparison.run", {"before_session_id": before["id"], "after_session_id": after["id"]})

    def test_mission_planner_and_qgc_real_formats(self):
        a = self.snapshot()
        b = self.snapshot("synthetic-qgc.params")
        self.assertEqual(a["parameter_count"], 4)
        self.assertEqual(b["mavlink_source"], [1, 1])
        self.assertNotIn("parameters", a)
        diff = self.service.dispatch("parameters.diff", {"before_id": a["id"], "after_id": b["id"]})
        self.assertEqual(diff["counts"], {"changed": 0, "added": 0, "removed": 0})

    def test_diff_added_removed_and_numeric_equivalence(self):
        a = self.snapshot()
        b = self.snapshot("synthetic-after.parm", firmware="synthetic-different-build")
        diff = self.service.dispatch("parameters.diff", {"before_id": a["id"], "after_id": b["id"]})
        self.assertEqual(diff["counts"], {"changed": 1, "added": 1, "removed": 1})
        self.assertEqual(diff["changed"][0]["name"], "ATC_RAT_RLL_P")
        self.assertIsNone(diff["changed"][0]["units"])
        self.assertFalse(diff["compatibility"]["compatible"])
        self.assertIn("firmware differs", diff["compatibility"]["issues"])

    def test_malformed_ambiguous_and_nonfinite_exports_rejected(self):
        for contents in ("A,1\nA,2\n", "A,nan\n", "A,Infinity\n", "A,1e999\n",
                         "A,1,2\n", "a,1\n", "1 1 A 1 9\n2 1 B 1 9\n",
                         "A,1\n1 1 B 1 9\n", "0 1 A 1 9\n", "A,1 trailing\n"):
            with self.subTest(contents=contents):
                path = self.root / "bad.param"
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    parse_parameters(path)

    def test_snapshot_requires_timezone_and_identity(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.snapshot(observed_at="2026-01-01T00:00:00")
        with self.assertRaisesRegex(ValueError, "aircraft_id"):
            self.snapshot(aircraft_id="")

    def test_constant_power_tracking_and_reopen(self):
        snapshot = self.snapshot()
        session = self.session(snapshot)
        metrics = session["metrics"]
        self.assertAlmostEqual(metrics["energy_wh"]["value"], 400 / 3600)
        self.assertEqual(metrics["mean_power_w"]["value"], 100)
        self.assertEqual(metrics["roll_tracking_rmse_deg"]["value"], 3)
        self.assertEqual(metrics["energy_wh"]["coverage_fraction"], 1)
        reopened = ParameterService(Store(self.store.root))
        self.assertEqual(reopened.dispatch("sessions.list", {})["sessions"][0], session)
        self.assertEqual(session["snapshot_source_sha256"], snapshot["source"]["sha256"])

    def test_long_gap_not_integrated(self):
        result = performance_metrics(FIXTURES / "performance/synthetic-gap.csv", 0, 10, 1.5)
        metric = result["metrics"]["energy_wh"]
        self.assertAlmostEqual(metric["value"], 200 / 3600)
        self.assertEqual(metric["covered_duration_s"], 2)
        self.assertEqual(metric["coverage_fraction"], 0.2)
        self.assertEqual(metric["exclusions"], [{"start_s": 1.0, "end_s": 9.0, "reason": "gap_exceeds_max_gap_s"}])

    def test_partial_window_ramp_power_and_error_integrals(self):
        path = self.root / "ramp.csv"
        path.write_text("time_s,voltage_v,current_a,desired_roll_deg,actual_roll_deg\n0,10,0,0,0\n2,10,2,2,0\n")
        metrics = performance_metrics(path, 0.5, 1.5, 2)["metrics"]
        self.assertAlmostEqual(metrics["energy_wh"]["value"], 10 / 3600)
        self.assertAlmostEqual(metrics["mean_power_w"]["value"], 10)
        self.assertAlmostEqual(metrics["roll_tracking_rmse_deg"]["value"], math.sqrt(13 / 12))
        self.assertEqual(metrics["energy_wh"]["sample_count"], 0)

    def test_negative_current_contributing_endpoints_are_retained(self):
        path = self.root / "negative.csv"
        path.write_text("time_s,voltage_v,current_a\n0,10,-1\n1,10,-1\n")
        result = performance_metrics(path, 0.2, 0.8, 1)
        self.assertAlmostEqual(result["metrics"]["energy_wh"]["value"], -6 / 3600)
        self.assertEqual(result["negative_current_sample_count"], 2)
        self.assertTrue(result["warnings"])

    def test_missing_values_exclude_pairs_without_zero_fill(self):
        path = self.root / "missing.csv"
        path.write_text("time_s,voltage_v,current_a,desired_roll_deg,actual_roll_deg\n0,10,1,0,0\n1,10,,0,\n2,10,1,0,0\n")
        metrics = performance_metrics(path, 0, 2, 1)["metrics"]
        self.assertIsNone(metrics["energy_wh"]["value"])
        self.assertIsNone(metrics["roll_tracking_rmse_deg"]["value"])
        self.assertEqual(metrics["roll_tracking_rmse_deg"]["exclusion_count"], 2)

    def test_unknown_units_clock_reset_nan_or_duplicate_header_rejected(self):
        for content in ("time,voltage,current\n0,10,1\n1,10,1\n",
                        "time_s,voltage_v,current_a\n1,10,1\n0,10,1\n",
                        "time_s,voltage_v,current_a\n0,10,nan\n1,10,1\n",
                        "time_s,voltage_v,current_a,current_a\n0,10,1,1\n1,10,1,1\n"):
            with self.subTest(content=content):
                path = self.root / "bad.csv"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    performance_metrics(path, 0, 1, 1)

    def test_matched_comparison_is_descriptive_and_never_causal(self):
        a = self.session(self.snapshot())
        b = self.session(self.snapshot("synthetic-after.parm"), "synthetic-lower-power.csv")
        result = self.compare(a, b)
        self.assertEqual(result["status"], "descriptive_only")
        power = next(m for m in result["metrics"] if m["name"] == "mean_power_w")
        self.assertEqual(power["absolute_change"], -20)
        self.assertEqual(power["percent_change"], -20)
        self.assertTrue(any("not causation" in item for item in result["limitations"]))

    def test_current_loaded_snapshot_cannot_prove_historical_settings(self):
        a = self.session(self.snapshot(source_kind="current_readback"))
        b = self.session(self.snapshot("synthetic-after.parm"), "synthetic-lower-power.csv")
        result = self.compare(a, b)
        self.assertEqual(result["status"], "confounded")
        self.assertIn("before.parameter_evidence", [item["field"] for item in result["confounders"]])

    def test_conditions_firmware_and_timeline_gate_comparison(self):
        a = self.session(self.snapshot())
        b = self.session(self.snapshot("synthetic-after.parm", firmware="other-build"),
                         "synthetic-lower-power.csv", parameter_changes="present",
                         conditions={"payload_kg": 2})
        result = self.compare(a, b)
        self.assertEqual(result["status"], "confounded")
        fields = [item["field"] for item in result["confounders"]]
        for expected in ("firmware", "payload_kg", "wind_mps", "battery_id", "after.parameter_changes"):
            self.assertIn(expected, fields)

    def test_wrong_aircraft_and_synthetic_mixing_rejected(self):
        snapshot = self.snapshot()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.session(snapshot, aircraft_id="other-aircraft")
        with self.assertRaisesRegex(ValueError, "Synthetic"):
            self.session(snapshot, synthetic=False)

    def test_finite_comparison_inputs_cannot_overflow_json_results(self):
        before = self.session(self.snapshot())
        after = self.session(self.snapshot("synthetic-after.parm"), "synthetic-lower-power.csv")
        # Synthetic stored metric fixtures isolate comparison arithmetic: the
        # observed values are finite, but their difference/ratio need not be.
        before["metrics"]["mean_power_w"]["value"] = 1e308
        after["metrics"]["mean_power_w"]["value"] = -1e308
        before["metrics"]["energy_wh"]["value"] = 1e-308
        after["metrics"]["energy_wh"]["value"] = 1e308
        before = self.store.add_document("session", before)
        after = self.store.add_document("session", after)
        comparison = self.compare(before, after)
        metrics = {item["name"]: item for item in comparison["metrics"]}
        self.assertIsNone(metrics["mean_power_w"]["absolute_change"])
        self.assertIsNone(metrics["mean_power_w"]["percent_change"])
        self.assertEqual(metrics["energy_wh"]["absolute_change"], 1e308)
        self.assertIsNone(metrics["energy_wh"]["percent_change"])
        json.dumps(comparison, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
