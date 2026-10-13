"""Reviewed offline references, never a flight controller or tuning prescription.

The source pins and hashes identify upstream material read by the reviewer. They
are provenance, not signatures. No network, dynamic imports, file execution,
parameter writes, or model API calls occur in this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone


REGISTRY_VERSION = "q22-ardupilot-guidance/2026-10-13.1"
RETRIEVED_AT = "2026-10-13T04:40:55Z"
REVIEW_AFTER_DAYS = 90  # Q22 editorial review policy, not an ArduPilot expiry.
WIKI_REVISION = "21432301f2d504e29940a86473e74259b871d936"
FIRMWARE_REVISION = "dbe792162d06cab66c3475fd5556bf7a120f119e"
FIRMWARE_RELEASE = "Copter-4.7.1"


def _source(repo: str, revision: str, path: str, sha256: str) -> dict:
    return {"repository": f"ArduPilot/{repo}", "revision": revision, "path": path,
            "url": f"https://github.com/ArduPilot/{repo}/blob/{revision}/{path}",
            "sha256": sha256, "retrieved_at": RETRIEVED_AT}


_SOURCES = {
    "wiki_vibration": _source("ardupilot_wiki", WIKI_REVISION,
        "common/source/docs/common-measuring-vibration.rst",
        "c4eb532d6c6b16f04d43c13ca2d0801a8c17606e6258d5c7989524eb0fa4c9e0"),
    "wiki_notch": _source("ardupilot_wiki", WIKI_REVISION,
        "common/source/docs/common-imu-notch-filtering.rst",
        "5346ae7f0b55ba9f78ed5b875f07cb6e1ea8449bc21466acb58800130c726926"),
    "wiki_tuning": _source("ardupilot_wiki", WIKI_REVISION,
        "copter/source/docs/tuning-process-instructions.rst",
        "1f7a48e27dac8d44e782217893b9a6b913aa3d4e1f3f10aa59ae96c2c4f4b323"),
    "wiki_initial": _source("ardupilot_wiki", WIKI_REVISION,
        "copter/source/docs/setting-up-for-tuning.rst",
        "d6410d391fda6b68fd1d8b9d0f5a7225901b037bfc2cb02ee562ca4aeac0bec1"),
    "imu": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/AP_InertialSensor/AP_InertialSensor.cpp",
        "abf460ad1827918da131fb3276232da1e58552d415aab2ca85f05d950af24c54"),
    "imu_log": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/AP_InertialSensor/LogStructure.h",
        "e5227640d36c01da8b579f167d314c30a629ec879edf4949354ba35b96d2fb28"),
    "notch": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/Filter/HarmonicNotchFilter.cpp",
        "d787a67791546e093be87a35be240f5448db627977492f55880184b25346ea87"),
    "attitude": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/AC_AttitudeControl/AC_AttitudeControl.cpp",
        "7eb50332f0fceec323ba0d2088c7a3271e7675a52ebd406c0f99e3051690e674"),
    "rate_log": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/AC_AttitudeControl/LogStructure.h",
        "d7f8c62c9abad8a74ec7cde95a7b045ce1fdfb1f3e5b1ad58b355ecbbefda943"),
    "mixer": _source("ardupilot", FIRMWARE_REVISION,
        "libraries/AP_Motors/AP_MotorsMatrix.cpp",
        "54118f5782b699b890da825ac5bfe44bf11aa0d2db8a279fdd8786dbaf6f92b2"),
}


def _reference(source_id: str, first: int, last: int) -> dict:
    return {"source_id": source_id, **_SOURCES[source_id],
            "line_start": first, "line_end": last,
            "url": f"{_SOURCES[source_id]['url']}#L{first}-L{last}"}


_ENTRIES = {
    "vibration": {
        "title": "Interpret filtered accelerometer vibration",
        "summary": "ArduPilot describes VIBE as filtered accelerometer variation in m/s². Its screening guidance says below 30 is normally acceptable, above 30 may cause problems, and above 60 nearly always causes position or altitude hold problems.",
        "references": [_reference("wiki_vibration", 28, 48), _reference("imu_log", 116, 140)],
        "evidence_requirements": ["Validated VIBE field units and IMU identity", "Selected flight interval, sample coverage and gaps"],
        "limitations": ["Screening guidance is not an airworthiness limit or proof of a particular mechanical cause.",
                        "A VIBE envelope is not raw high-rate IMU data and cannot establish a vibration spectrum or notch frequency.",
                        "Copter 4.7.1 uses IMU and Clip; older examples use Clip0/Clip1/Clip2. Decode the actual log schema."],
        "parameter_families": ["INS_"],
    },
    "clipping": {
        "title": "Interpret accelerometer clipping within a continuous interval",
        "summary": "Clipping counters increase when measured acceleration exceeds the sensor backend's clipping limit. Repeated increases warrant investigating mechanical vibration and mounting before tuning.",
        "references": [_reference("imu", 2216, 2246), _reference("wiki_vibration", 43, 48)],
        "evidence_requirements": ["Same IMU and uninterrupted boot/clock segment", "At least two valid cumulative counter observations"],
        "limitations": ["Count only observed positive deltas; resets, wraps and gaps must be disclosed rather than joined.",
                        "Do not count a nonzero first sample as clipping during the selected flight.",
                        "The wiki's example 16G limit is not universal; firmware delegates the threshold to each backend.",
                        "Zero observed increases does not prove absence of clipping during missing data."],
        "parameter_families": ["INS_"],
    },
    "harmonic_notch": {
        "title": "Review harmonic notch evidence and tracking source",
        "summary": "Notch filtering targets motor-related gyro noise. Review measured noise, filter effectiveness, tracking-source reliability and processor capacity. Throttle tracking can change with payload; reliable ESC RPM telemetry can follow changing motor speed.",
        "references": [_reference("wiki_notch", 38, 110), _reference("notch", 58, 137)],
        "evidence_requirements": ["Validated high-rate pre/post-filter IMU evidence with sample rate and anti-alias context", "RPM/ESC/throttle tracking evidence where relevant", "Firmware, enabled features and processor context"],
        "limitations": ["No center frequency, bandwidth, harmonic count or filter value is inferred from low-rate VIBE envelopes.",
                        "More notches consume processor capacity; an octocopter or heavy aircraft does not imply a preset.",
                        "A parameter present in a file does not prove the feature is compiled in or active."],
        "parameter_families": ["INS_HNTCH_", "INS_HNTC2_", "FFT_"],
    },
    "gyro_filter": {
        "title": "Review the noise and phase-delay tradeoff",
        "summary": "Gyro low-pass filtering trades noise reduction against phase delay. Aliased noise requires addressing its source; changing software filters is not a general cure for mechanical vibration.",
        "references": [_reference("wiki_notch", 20, 34), _reference("imu", 342, 356)],
        "evidence_requirements": ["High-rate gyro evidence and known filtering stage", "Time-aligned desired/actual rate response"],
        "limitations": ["No cutoff frequency or PID value is recommended automatically.", "Parameter units here are reviewed against Copter 4.7.1 source, not all firmware."],
        "parameter_families": ["INS_GYRO_FILTER", "INS_ACCEL_FILTER", "ATC_RAT_"],
    },
    "heavy_lift": {
        "title": "Account for payload, flexibility and actuator limits",
        "summary": "ArduPilot's tuning sequence establishes a stable aircraft and evaluates noise before optimizing response. Its advanced considerations include flexible airframes, large payloads on flexible mounts, and rate-limited or nonlinear actuators.",
        "references": [_reference("wiki_tuning", 7, 33), _reference("wiki_initial", 67, 85)],
        "evidence_requirements": ["Operator-declared mass, payload, propeller/rotor and hardware configuration", "Comparable battery, flight regime and wind context"],
        "limitations": ["The documentation's size examples are not a certified heavy-lift tuning recipe.",
                        "Mass, payload or rotor count alone cannot identify inertia, control authority, resonance or safe gains.",
                        "A current parameter readback is not proof of the configuration used by a historical flight."],
        "parameter_families": ["ATC_", "MOT_", "INS_"],
    },
    "rate_limits": {
        "title": "Separate rate and acceleration constraints from tracking error",
        "summary": "Attitude input shaping and axis rate/acceleration constraints affect demanded response. Copter 4.7.1 uses ATC_ACC_R_MAX, ATC_ACC_P_MAX and ATC_ACC_Y_MAX in deg/s²; the source marks older ATC_ACCEL names as centidegree-based predecessors.",
        "references": [_reference("attitude", 34, 51), _reference("attitude", 96, 134), _reference("attitude", 175, 203)],
        "evidence_requirements": ["Firmware identity and exact parameter names/units", "Flight-bound parameter snapshot and time-aligned demand/response"],
        "limitations": ["Do not copy values or units across ATC_ACCEL_* and ATC_ACC_* names.",
                        "Reference metadata does not establish the exact firmware build, board features or a safe setting.",
                        "Tracking error does not uniquely diagnose poor gains; limits, disturbances and actuator authority can contribute."],
        "parameter_families": ["ATC_ACC_", "ATC_ACCEL_", "ATC_RATE_", "ATC_INPUT_TC"],
    },
    "tracking": {
        "title": "Compare compatible desired and achieved response",
        "summary": "RATE documents desired and achieved roll, pitch and yaw rates separately from normalized controller outputs. Compare aligned matching quantities with validated units, coverage and flight regime.",
        "references": [_reference("rate_log", 122, 150), _reference("rate_log", 201, 204)],
        "evidence_requirements": ["Matching desired/actual axis, units and timebase", "No interpolation across boot changes or unacceptable gaps"],
        "limitations": ["Attitude angle error is not rate-controller error.", "Observed improvement alongside changed parameters is descriptive association, not proof of causation.", "No universal good/bad tracking-error threshold is supplied by these references."],
        "parameter_families": ["ATC_"],
    },
    "actuator_limits": {
        "title": "Distinguish output level from actuator saturation",
        "summary": "The multicopter mixer sets limit flags when combined demands cannot fit available outputs. A normalized controller output or high PWM alone does not establish motor saturation.",
        "references": [_reference("mixer", 359, 394), _reference("rate_log", 122, 137)],
        "evidence_requirements": ["Validated limit flags or mapped actuator outputs with configuration", "Vehicle/mixer type, output endpoints and flight interval"],
        "limitations": ["Reviewed mixer code is AP_MotorsMatrix in Copter 4.7.1; other mixers can differ.", "Do not infer available thrust or saturation from a generic percentage threshold."],
        "parameter_families": ["MOT_", "SERVO_"],
    },
}


def citation(identifier: str) -> dict:
    """Return an isolated JSON-safe citation; unknown IDs fail closed."""
    if identifier not in _ENTRIES:
        raise ValueError(f"Unknown guidance citation: {identifier}")
    entry = deepcopy(_ENTRIES[identifier])
    return {"id": identifier, "registry_version": REGISTRY_VERSION, **entry,
            "url": entry["references"][0]["url"], "retrieved_at": RETRIEVED_AT,
            "firmware_scope": "General explanatory guidance; source metadata reviewed only for Copter-4.7.1; exact build/board/features unverified."}


def registry(firmware: str | None = None, *, now: datetime | None = None) -> dict:
    """Describe the offline snapshot and freshness without claiming live currency.

    A release label is declared context, not proof that metadata matches a
    vehicle. Unknown, custom, prerelease and other-version builds stay unmatched.
    """
    if firmware is not None and not isinstance(firmware, str):
        raise ValueError("firmware must be a string or null")
    checked = now if now is not None else datetime.now(timezone.utc)
    if checked.tzinfo is None or checked.utcoffset() is None:
        raise ValueError("now must include a timezone")
    retrieved = datetime.fromisoformat(RETRIEVED_AT.replace("Z", "+00:00"))
    review_after = retrieved + timedelta(days=REVIEW_AFTER_DAYS)
    state = "clock_before_retrieval" if checked < retrieved else "review_due" if checked >= review_after else "within_review_window"
    label_match = firmware in {FIRMWARE_RELEASE, "ArduCopter 4.7.1", "ArduCopter V4.7.1"}
    return {
        "version": REGISTRY_VERSION, "mode": "bundled_offline", "retrieved_at": RETRIEVED_AT,
        "freshness": {"status": state, "checked_at": checked.astimezone(timezone.utc).isoformat(),
                      "review_after": review_after.isoformat(), "policy": "Q22 90-day editorial review; no live freshness verification"},
        "firmware_compatibility": {"declared_firmware": firmware, "reviewed_release": FIRMWARE_RELEASE,
            "reviewed_revision": FIRMWARE_REVISION, "release_label_match": label_match,
            "exact_build_validated": False, "parameter_application_allowed": False,
            "status": "declared_release_matches_build_unverified" if label_match else "unverified_or_unmatched"},
        "sources": deepcopy(_SOURCES), "entries": [citation(key) for key in _ENTRIES],
        "limitations": ["These pinned references do not claim to be the latest guidance after retrieval.",
            "Firmware labels and operator profiles do not prove historical flight configuration.",
            "Review-due references remain available as historical evidence; they do not authorize changes.",
            "No live update, remote model request, upload or parameter command is implemented."],
    }
