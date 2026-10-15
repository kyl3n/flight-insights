"""Immutable repeated-flight plans and descriptive, criterion-based evaluation.

No experiment authorizes aircraft changes. Verified flight records establish
separate recorded flights, not statistical independence or a causal effect.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math
import re
from statistics import median

from .insight_comparison import BASE_METRICS, _flight_context, _metric_sources, _observations, compare_reports
from .logs import check_cancel

VERSION = "q22-flight-experiments/1"
MAX_REPORTS = 20
MAX_CRITERIA = 5
METRIC = re.compile(r"(?:mean_power_w|energy_wh|peak_power_w|roll_tracking_rmse_deg|minimum_pack_voltage_v|command_headroom_fraction|"
                    r"vibration\.(?:reported|[0-9]{1,2})\.[xyz]\.(?:p95_mps2|maximum_mps2)|"
                    r"control\.(?:roll|pitch|yaw)\.tracking_rmse_deg_s|spectrum\.retained_band_power)\Z")
PARAMETER = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
LIMITATIONS = [
    "Results describe the selected repeated observations and operator-defined criteria; they do not prove causation, an optimum, airworthiness or statistical independence.",
    "Distinct verified flight records are counted separately. Unlinked reports and repeated intervals from one flight are never promoted into independent flights.",
    "A group median is robust to individual extremes but cannot remove changed weather, pilot demand, payload, battery condition or acquisition effects.",
    "All selected observations, failed criteria and counterexamples remain in the evaluation. No aircraft parameters are written.",
    "An observed parameter value or complete readback is not proof that reboot-dependent settings were effective during flight.",
]


def _text(value, label, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters.")
    return value.strip()


def _number(value, label, low=0, high=1e100):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be a finite number in {low}..{high}.")
    return value


def _identifier(value, label):
    return _text(value, label, 100)


def _numeric_value(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 128:
        raise ValueError(f"{label} requires a finite numeric parameter value.")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not math.isfinite(float(number)):
            raise InvalidOperation()
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError(f"{label} requires a finite numeric parameter value.") from error
    return str(value)


def _criteria(value, label, threshold):
    if not isinstance(value, list) or len(value) > MAX_CRITERIA or (label == "goals" and not value):
        raise ValueError(f"{label} must contain {'1' if label == 'goals' else '0'}..{MAX_CRITERIA} criteria.")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"metric", "unit", "direction", threshold}:
            raise ValueError(f"Each {label} criterion requires metric, unit, direction and {threshold}.")
        name = _text(item.get("metric"), "metric", 100)
        if not METRIC.fullmatch(name):
            raise ValueError("Unsupported experiment metric; select a reviewed report metric.")
        if item["direction"] not in {"lower", "higher"}:
            raise ValueError("Criterion direction must be lower or higher.")
        expected_unit = BASE_METRICS.get(name, "m/s²" if name.startswith("vibration.") else "deg/s" if name.startswith("control.") else "(rad/s)²")
        if item["unit"] != expected_unit:
            raise ValueError(f"{name} requires the recorded unit {expected_unit}; experiment thresholds do not convert units.")
        if any(row["metric"] == name for row in result):
            raise ValueError(f"Duplicate metric in {label}.")
        result.append({"metric": name, "unit": _text(item["unit"], "unit", 40), "direction": item["direction"],
                       threshold: _number(item[threshold], threshold)})
    return result


def _summary(values):
    return {"count": len(values), "median": median(values) if values else None,
            "minimum": min(values) if values else None, "maximum": max(values) if values else None}


def _finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e100 else None


def _coverage(metric, report):
    fraction = _finite(metric.get("coverage_fraction"))
    if fraction is not None:
        return fraction
    duration = _finite(metric.get("covered_duration_s"))
    scope = report.get("scope", {})
    start, end = _finite(scope.get("interval_start_s")), _finite(scope.get("interval_end_s"))
    if duration is not None and start is not None and end is not None and end > start:
        return min(1, duration / (end - start))
    return None


class ExperimentService:
    def __init__(self, store):
        self.store = store

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Experiment params must be an object.")
        if method == "experiments.create":
            return {"experiment": self.create(params)}
        if method == "experiments.list":
            limit, offset = params.get("limit", 20), params.get("offset", 0)
            if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
                raise ValueError("Experiment limit must be 1..50 and offset nonnegative.")
            rows = self.store.list_documents("experiment", limit + 1, offset)
            return {"experiments": [self.summary(row) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}
        if method not in {"experiments.detail", "experiments.evaluate"}:
            raise ValueError("Unsupported experiment method.")
        identifier = _identifier(params.get("experiment_id"), "experiment_id")
        experiment = self.store.get_document("experiment", identifier)
        if method == "experiments.detail":
            with self.store._connect() as db:
                rows = db.execute("SELECT data FROM documents WHERE kind=? AND json_extract(data, '$.experiment_id')=? ORDER BY created_at DESC, id DESC LIMIT 21",
                                  ("experiment_evaluation", identifier)).fetchall()
            retained, size = [], 0
            for row in rows[:20]:
                size += len(row[0].encode("utf-8"))
                if size > 2 * 1024 * 1024:
                    break
                retained.append(json.loads(row[0]))
            return {"experiment": experiment, "evaluations": retained,
                    "evaluations_truncated": len(rows) > len(retained)}
        evaluation = self.evaluate(experiment, cancel_check=cancel_check, progress=progress)
        check_cancel(cancel_check)
        return {"evaluation": self.store.add_document("experiment_evaluation", evaluation)}

    @staticmethod
    def summary(row):
        return {key: row.get(key) for key in ("id", "created_at", "name", "revision", "parent_experiment_id", "flight_phase")} | {
            "reference_count": len(row["reference_report_ids"]), "candidate_count": len(row["candidate_report_ids"]),
            "goal_count": len(row["goals"])}

    def create(self, params):
        allowed = {"name", "hypothesis", "parameter_hypothesis", "reference_report_ids", "candidate_report_ids", "pairs", "flight_phase",
                   "minimum_coverage", "minimum_flights_per_group", "goals", "regression_limits", "parent_experiment_id"}
        if set(params) - allowed:
            raise ValueError("Unknown experiment fields: " + ", ".join(sorted(set(params) - allowed)))
        phase = _text(params.get("flight_phase"), "flight_phase", 80)
        if phase.lower() == "unknown":
            raise ValueError("Choose a known flight phase to match repeated observations.")
        count = params.get("minimum_flights_per_group", 2)
        if type(count) is not int or not 2 <= count <= MAX_REPORTS:
            raise ValueError("minimum_flights_per_group must be an integer in 2..20.")
        plan = {"version": VERSION, "name": _text(params.get("name"), "name", 160),
                "hypothesis": _text(params.get("hypothesis"), "hypothesis"), "flight_phase": phase,
                "minimum_coverage": _number(params.get("minimum_coverage", .8), "minimum_coverage", 0, 1),
                "minimum_flights_per_group": count, "goals": _criteria(params.get("goals"), "goals", "minimum_change"),
                "regression_limits": _criteria(params.get("regression_limits", []), "regression_limits", "maximum_regression"),
                "parameter_hypothesis": [], "pairs": [], "parent_experiment_id": None, "revision": 1}
        for key in ("reference_report_ids", "candidate_report_ids"):
            identifiers = params.get(key, [])
            if not isinstance(identifiers, list) or len(identifiers) > MAX_REPORTS:
                raise ValueError(f"{key} must contain at most {MAX_REPORTS} saved report IDs.")
            identifiers = [_identifier(value, key) for value in identifiers]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"Duplicate report IDs in {key}.")
            for identifier in identifiers:
                self.store.get_document("insight_report", identifier)
            plan[key] = identifiers
        if set(plan["reference_report_ids"]) & set(plan["candidate_report_ids"]):
            raise ValueError("A report cannot belong to both experiment groups.")
        hypotheses = params.get("parameter_hypothesis", [])
        if not isinstance(hypotheses, list) or len(hypotheses) > 8:
            raise ValueError("parameter_hypothesis must contain at most 8 named parameter changes.")
        for item in hypotheses:
            if not isinstance(item, dict) or set(item) != {"name", "before", "after"}:
                raise ValueError("Each parameter hypothesis requires name, before and after.")
            name = _text(item["name"], "parameter name", 64)
            if not PARAMETER.fullmatch(name) or any(row["name"] == name for row in plan["parameter_hypothesis"]):
                raise ValueError("Parameter hypotheses require distinct uppercase parameter names.")
            plan["parameter_hypothesis"].append({"name": name, "before": _numeric_value(item["before"], "before"),
                                                  "after": _numeric_value(item["after"], "after")})
        pairs = params.get("pairs", [])
        if not isinstance(pairs, list) or len(pairs) > MAX_REPORTS:
            raise ValueError("pairs must contain at most 20 explicit matched report pairs.")
        for pair in pairs:
            if not isinstance(pair, dict) or set(pair) != {"reference_report_id", "candidate_report_id"}:
                raise ValueError("Each pair requires reference_report_id and candidate_report_id.")
            if pair["reference_report_id"] not in plan["reference_report_ids"] or pair["candidate_report_id"] not in plan["candidate_report_ids"]:
                raise ValueError("Each paired report must belong to its declared group.")
            plan["pairs"].append(deepcopy(pair))
        if pairs and (len(pairs) != len(plan["reference_report_ids"]) or len(pairs) != len(plan["candidate_report_ids"])
                      or len({pair["reference_report_id"] for pair in pairs}) != len(pairs)
                      or len({pair["candidate_report_id"] for pair in pairs}) != len(pairs)):
            raise ValueError("Explicit pairs must cover every selected report exactly once; unmatched observations cannot be discarded.")
        parent = params.get("parent_experiment_id")
        if parent is not None:
            previous = self.store.get_document("experiment", _identifier(parent, "parent_experiment_id"))
            plan.update(parent_experiment_id=parent, revision=previous["revision"] + 1)
        plan["analysis_mode"] = "explicit_paired" if pairs else "group_medians"
        plan["limitations"] = LIMITATIONS
        return self.store.add_document("experiment", plan)

    def evaluate(self, experiment, cancel_check=None, progress=None):
        groups = {name: [self.store.get_document("insight_report", identifier) for identifier in experiment[name + "_report_ids"]]
                  for name in ("reference", "candidate")}
        all_reports = groups["reference"] + groups["candidate"]
        confounders, blockers = {}, []
        def issue(code, detail, report_id=None, blocking=False):
            item = confounders.setdefault(code, {"code": code, "detail": detail, "report_ids": [], "blocks_decision": False})
            if report_id is not None and report_id not in item["report_ids"]:
                item["report_ids"].append(report_id)
            item["blocks_decision"] |= blocking
            if blocking and code not in blockers:
                blockers.append(code)
        anchor = groups["reference"][0] if groups["reference"] else None
        comparisons = {}
        if anchor:
            for index, report in enumerate(all_reports):
                check_cancel(cancel_check)
                if progress:
                    progress("comparing_repeated_flights", index, len(all_reports))
                comparison = compare_reports(anchor, report, store=self.store, cancel_check=cancel_check, progress=progress)
                comparisons[report["id"]] = {row["name"]: row for row in comparison["metrics"]}
                if report["id"] != anchor["id"]:
                    for item in comparison["confounders"]:
                        if item["code"] not in {"same_artifact", "overlapping_interval", "same_flight"}:
                            issue(item["code"], item["detail"], report["id"])
        summaries, verified_ids, hardware_ids = {}, {}, set()
        for name, reports in groups.items():
            identities, unresolved = [], []
            for report in reports:
                context = _flight_context(report)
                if not context or not context.get("hardware_uid"):
                    unresolved.append(report["id"])
                    issue("flight_identity_unknown", "Separate verified flight records are required to count repeated flights.", report["id"], True)
                else:
                    identities.append(context["flight_record_id"])
                    hardware_ids.add(context["hardware_uid"])
                phase = report.get("operating_profile", {}).get("flight_phase", report.get("scope", {}).get("flight_phase"))
                if phase != experiment["flight_phase"]:
                    issue("phase_mismatch", "Every selected interval must match the planned flight phase.", report["id"], True)
            if len(set(identities)) != len(identities):
                issue("repeated_intervals_one_flight", "Multiple reports from one verified flight do not provide separate repeats; retain them for review and select one matched interval per flight in a revision.", blocking=True)
            verified_ids[name] = set(identities)
            count = len(set(identities))
            if count < experiment["minimum_flights_per_group"]:
                issue("insufficient_repeated_flights", "Each group needs the planned minimum number of distinct verified flights.", blocking=True)
            summaries[name] = {"report_count": len(reports), "distinct_verified_flight_count": count,
                               "independent_flight_count": None, "unlinked_report_ids": unresolved,
                               "report_ids": [report["id"] for report in reports]}
        if verified_ids["reference"] & verified_ids["candidate"]:
            issue("flight_in_both_groups", "A verified flight appears in both groups; the groups do not represent separate before/after flights.", blocking=True)
        if len(hardware_ids) > 1:
            issue("different_aircraft", "The experiment includes different verified aircraft hardware identities.", blocking=True)
        synthetic_states = {report.get("scope", {}).get("synthetic") for report in all_reports}
        if len(synthetic_states) > 1 or None in synthetic_states:
            issue("synthetic_evidence_mismatch", "Synthetic and real evidence must not be pooled, and unknown source status requires review.", blocking=True)

        parameter_evidence = []
        for name, reports in groups.items():
            for report in reports:
                check_cancel(cancel_check)
                observed = report.get("parameter_evidence", {})
                values, invalid = _observations(observed)
                eligible = observed.get("state") == "observed_only" and observed.get("observations_eligible_for_review") is True and not invalid and not observed.get("observations_truncated")
                basis, snapshot_id, configuration_reason = "observed_parameter_subset", None, None
                if report.get("configuration", {}).get("status") == "flight_bound_readbacks":
                    from .flight_configuration import configuration_parameters
                    try:
                        full, reference = configuration_parameters(self.store, report, cancel_check=cancel_check, progress=progress)
                        values = {parameter: {"value": row["value"], "number": Decimal(row["value"]),
                                              "mav_param_type": row["mav_param_type"], "canonical_token": row["canonical_token"]}
                                  for parameter, row in full.items()}
                        basis, snapshot_id, eligible = "complete_typed_preflight_readback", reference["snapshot_id"], True
                    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as error:
                        configuration_reason = str(error)
                        issue("full_readback_unavailable", "A preserved full readback failed current validation; only separately supported logged observations remain eligible.", report["id"])
                checks = []
                for hypothesis in experiment["parameter_hypothesis"]:
                    expected = hypothesis["before" if name == "reference" else "after"]
                    found = values.get(hypothesis["name"])
                    expected_canonical = expected
                    if found is not None and "mav_param_type" in found:
                        from .parameter_capture import _decode
                        decoded, _ = _decode(expected, found["mav_param_type"], "typed_numeric")
                        expected_canonical = decoded["canonical_value"] if decoded else None
                        matches = bool(eligible and decoded and decoded["canonical_token"] == found["canonical_token"])
                    else:
                        matches = bool(eligible and found is not None and found["number"] == Decimal(expected))
                    checks.append({"parameter": hypothesis["name"], "expected": expected,
                                   "expected_canonical": expected_canonical,
                                   "observed": found["value"] if found else None, "status": "matched_observation" if matches else "unverified_or_mismatched"})
                    if not matches:
                        issue("planned_parameters_unverified", "At least one selected report does not contain a timely matching observation of the planned parameter value.", report["id"], True)
                parameter_evidence.append({"report_id": report["id"], "group": name, "state": "complete_preflight_readback" if snapshot_id else observed.get("state", "missing"),
                                           "basis": basis, "snapshot_id": snapshot_id, "configuration_reason": configuration_reason,
                                           "effective_configuration": "unknown", "checks": checks})

        metrics = sorted({criterion["metric"] for criterion in experiment["goals"] + experiment["regression_limits"]})
        summaries_by_metric, criteria, counterexamples = [], [], []
        for metric in metrics:
            check_cancel(cancel_check)
            metric_criteria = [("goal", row) for row in experiment["goals"] if row["metric"] == metric]
            metric_criteria += [("regression_limit", row) for row in experiment["regression_limits"] if row["metric"] == metric]
            units = {row["unit"] for _, row in metric_criteria}
            values, observations = {"reference": [], "candidate": []}, []
            for name, reports in groups.items():
                for report in reports:
                    datum = _metric_sources(report).get(metric, {})
                    value, unit = _finite(datum.get("value")), datum.get("unit")
                    coverage = _coverage(datum, report)
                    semantic = comparisons.get(report["id"], {}).get(metric, {})
                    reasons = []
                    if value is None or unit not in units or len(units) != 1:
                        reasons.append("Metric value or expected unit is unavailable or inconsistent.")
                    if semantic.get("status") != "descriptive":
                        reasons.append("Metric semantics differ from the reference acquisition/calibration/algorithm.")
                    if coverage is None or coverage < experiment["minimum_coverage"]:
                        reasons.append("Known interval coverage does not meet the planned threshold.")
                    if _finite(datum.get("sample_count")) is None or datum.get("sample_count", 0) <= 0:
                        reasons.append("Positive sample counts are required.")
                    if reasons:
                        issue("metric_evidence_insufficient", "Every selected observation must meet the planned metric units, comparability and coverage; exclusions remain visible.", report["id"], True)
                    else:
                        values[name].append(value)
                    observations.append({"report_id": report["id"], "group": name, "value": value, "unit": unit,
                                         "coverage_fraction": coverage, "eligible": not reasons, "limitations": reasons})
            reference, candidate = _summary(values["reference"]), _summary(values["candidate"])
            observed_by_id = {item["report_id"]: item for item in observations}
            pair_deltas = []
            for pair in experiment["pairs"]:
                left, right = observed_by_id[pair["reference_report_id"]], observed_by_id[pair["candidate_report_id"]]
                pair_deltas.append({**pair, "delta": _finite(right["value"] - left["value"]) if left["eligible"] and right["eligible"] else None})
            raw_delta = (_finite(candidate["median"] - reference["median"])
                         if reference["median"] is not None and candidate["median"] is not None else None)
            delta = median([pair["delta"] for pair in pair_deltas]) if pair_deltas and all(pair["delta"] is not None for pair in pair_deltas) else None if pair_deltas else raw_delta
            metric_row = {"metric": metric, "unit": next(iter(units)) if len(units) == 1 else None,
                          "reference": reference, "candidate": candidate, "delta": delta,
                          "analysis_mode": experiment["analysis_mode"], "observations": observations, "pair_deltas": pair_deltas}
            summaries_by_metric.append(metric_row)
            for kind, criterion in metric_criteria:
                improvement = delta * (1 if criterion["direction"] == "higher" else -1) if delta is not None else None
                enough = not blockers and improvement is not None and all(len(values[name]) >= experiment["minimum_flights_per_group"] for name in values)
                passed = (improvement > 0 and improvement >= criterion["minimum_change"] if kind == "goal" and improvement is not None
                          else improvement >= -criterion["maximum_regression"] if improvement is not None else False)
                criteria.append({"kind": kind, **criterion, "observed_change": delta, "signed_improvement": improvement,
                                 "status": "insufficient_evidence" if not enough else "passed" if passed else "failed"})
                for observation in observations:
                    if observation["group"] != "candidate" or not observation["eligible"]:
                        continue
                    reference_value = reference["median"]
                    if pair_deltas:
                        pair = next(pair for pair in experiment["pairs"] if pair["candidate_report_id"] == observation["report_id"])
                        matched = observed_by_id[pair["reference_report_id"]]
                        reference_value = matched["value"] if matched["eligible"] else None
                    if reference_value is None:
                        continue
                    change = (observation["value"] - reference_value) * (1 if criterion["direction"] == "higher" else -1)
                    threshold = criterion.get("minimum_change", -criterion.get("maximum_regression", 0))
                    if change < threshold:
                        counterexamples.append({"report_id": observation["report_id"], "metric": metric, "kind": kind,
                            "reference_value": reference_value, "candidate_value": observation["value"], "unit": criterion["unit"],
                            "signed_improvement": change, "required_improvement": threshold,
                            "basis": "explicit_matched_reference" if pair_deltas else "reference_group_median"})
        if blockers or not criteria or any(item["status"] == "insufficient_evidence" for item in criteria):
            status = "insufficient_evidence"
        elif any(item["status"] == "failed" for item in criteria) or counterexamples:
            status = "mixed_results"
        else:
            status = "observed_improvement"
        return {"version": VERSION, "experiment_id": experiment["id"], "experiment_revision": experiment["revision"],
                "status": status, "analysis_mode": experiment["analysis_mode"], "causal_attribution": False,
                "synthetic": synthetic_states == {True}, "groups": summaries, "metric_summaries": summaries_by_metric,
                "criteria": criteria, "parameter_evidence": parameter_evidence, "counterexamples": counterexamples,
                "confounders": list(confounders.values()), "decision_blockers": blockers,
                "decision_basis": "All planned group/paired criteria and every retained candidate observation must meet the explicit thresholds, and each goal must show a positive change in its requested direction; numeric trends alone do not establish causes.",
                "limitations": LIMITATIONS}
