"""Bounded static filter models, with no parameter selection or device writes.

The implementation uses independently expressed analog prototypes and a
bilinear substitution. Upstream source is referenced for the bandwidth
convention and supported static equation scope; no upstream code is embedded.
Python double precision is intentional and is not firmware float32 emulation.
"""
from __future__ import annotations

import cmath
from copy import deepcopy
import math


MODEL_VERSION = "q22-static-filter/1"
REVISION = "dbe792162d06cab66c3475fd5556bf7a120f119e"
MAX_STAGES = 16
MAX_FREQUENCIES = 1025


def _reference(path: str, digest: str, first: int, last: int) -> dict:
    return {"repository": "ArduPilot/ardupilot", "revision": REVISION,
            "path": path, "sha256": digest, "line_start": first, "line_end": last,
            "url": f"https://github.com/ArduPilot/ardupilot/blob/{REVISION}/{path}#L{first}-L{last}"}


REFERENCES = [
    _reference("libraries/Filter/LowPassFilter2p.cpp",
               "305fef4115925d28947496c4148db0b6745e33ac94f581fa28a1e781b4dfaac3", 49, 66),
    _reference("libraries/Filter/NotchFilter.cpp",
               "06e391457b50d92716b24309214ac3c83cc96ee03c724c7c810c9529ce2d6860", 31, 101),
    _reference("libraries/Filter/HarmonicNotchFilter.cpp",
               "d787a67791546e093be87a35be240f5448db627977492f55880184b25346ea87", 166, 212),
]


def _number(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return result


def _keys(value: object, allowed: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{name} must be an object with only supported keys")
    return value


def _bilinear(polynomial: tuple[float, float, float]) -> list[float]:
    """Coefficients of (1+z^-1)^2 P((1-z^-1)/(1+z^-1))."""
    constant, linear, quadratic = polynomial
    return [constant + linear + quadratic, 2 * (constant - quadratic),
            constant - linear + quadratic]


def _stage(config: object, rate: float) -> dict:
    row = _keys(config, {"kind", "cutoff_hz", "center_hz", "bandwidth_hz", "attenuation_db"}, "stage")
    kind = row.get("kind")
    if kind == "butterworth_lowpass":
        _keys(row, {"kind", "cutoff_hz"}, "low-pass stage")
        requested = _number(row.get("cutoff_hz"), "cutoff_hz", 0, rate / 2)
        cutoff = min(requested, rate * 0.4)
        if cutoff and cutoff < rate * 0.0001:
            raise ValueError("positive cutoff_hz must be at least 0.0001 * sample_rate_hz")
        effective = {"cutoff_hz": cutoff, "requested_cutoff_hz": requested,
                     "cutoff_clamped": cutoff != requested, "bypass": cutoff == 0}
        if cutoff == 0:
            numerator = denominator = [1.0, 0.0, 0.0]
            prototype = None
        else:
            center = math.tan(math.pi * cutoff / rate)
            prototype = (center * center, math.sqrt(2) * center, 0.0)
            numerator = _bilinear((center * center, 0, 0))
            denominator = _bilinear((center * center, math.sqrt(2) * center, 1))
    elif kind == "ardupilot_static_notch":
        _keys(row, {"kind", "center_hz", "bandwidth_hz", "attenuation_db"}, "notch stage")
        frequency = _number(row.get("center_hz"), "center_hz", rate * 0.0001, rate * 0.48)
        bandwidth = _number(row.get("bandwidth_hz"), "bandwidth_hz", frequency * 0.001, frequency * 1.9)
        depth = _number(row.get("attenuation_db"), "attenuation_db", 0, 80)
        # If u is lower-edge/center frequency, AP's octave convention is
        # Q = u/(1-u^2). This is not the generic center/bandwidth definition.
        u = 1 - bandwidth / (2 * frequency)
        quality = u / ((1 - u) * (1 + u))
        center = math.tan(math.pi * frequency / rate)
        damping = center / quality
        gain = 10 ** (-depth / 20)
        prototype = (center * center, damping, gain)
        numerator = _bilinear((center * center, damping * gain, 1))
        denominator = _bilinear((center * center, damping, 1))
        effective = {"center_hz": frequency, "bandwidth_hz": bandwidth,
                     "attenuation_db": depth, "quality_factor": quality,
                     "bandwidth_convention": "ArduPilot octave-derived Q; not a digital -3 dB bandwidth",
                     "bypass": depth == 0}
    else:
        raise ValueError("stage kind must be butterworth_lowpass or ardupilot_static_notch")
    scale = denominator[0]
    numerator = [value / scale for value in numerator]
    denominator = [value / scale for value in denominator]
    root = cmath.sqrt(denominator[1] ** 2 - 4 * denominator[2])
    radii = [abs((-denominator[1] + sign * root) / 2) for sign in (1, -1)]
    if max(radii) >= 1 or any(not math.isfinite(value) for value in numerator + denominator):
        raise ValueError("filter is outside the stable numerical model domain")
    return {"kind": kind, "settings": effective, "numerator": numerator,
            "denominator": denominator, "pole_radii": radii,
            "stable": True, "_prototype": prototype}


def _response(stage: dict, frequency: float, rate: float) -> tuple[float, float, float] | None:
    """Return amplitude, continuous phase radians, and delay seconds.

    Phase follows each analog prototype's continuous branch, independent of
    response-grid spacing. Delay is the analytic logarithmic derivative of
    the same transfer function; no finite-difference phase unwrapping is used.
    """
    if stage["settings"]["bypass"]:
        return 1.0, 0.0, 0.0
    lowpass = stage["kind"] == "butterworth_lowpass"
    if frequency == rate / 2 and lowpass:
        return None  # Double zero at z=-1: its phase and delay are undefined.
    constant, damping, depth = stage["_prototype"]
    if frequency == rate / 2:
        # Limiting value avoids tan(pi/2) overflow and polynomial cancellation.
        return 1.0, 0.0, damping * (1 - depth) / (2 * rate)
    t = math.tan(math.pi * frequency / rate)
    real = constant - t * t
    denominator = complex(real, damping * t)
    derivative = complex(-2 * t, damping)
    if lowpass:
        numerator = complex(constant, 0)
        numerator_derivative = 0j
        phase = -math.atan2(damping * t, real)
    else:
        numerator = complex(real, depth * damping * t)
        numerator_derivative = complex(-2 * t, depth * damping)
        phase = math.atan2(depth * damping * t, real) - math.atan2(damping * t, real)
    amplitude = abs(numerator) / abs(denominator)
    delay = -(numerator_derivative / numerator - derivative / denominator).imag * (1 + t * t) / (2 * rate)
    return amplitude, phase, delay


def filter_model(config: object) -> dict:
    """Evaluate an operator-defined static cascade. Never derive a tuning value."""
    value = _keys(config, {"sample_rate_hz", "stages", "frequencies_hz"}, "filter model")
    rate = _number(value.get("sample_rate_hz"), "sample_rate_hz", 100, 64000)
    stage_configs = value.get("stages")
    if not isinstance(stage_configs, list) or not 1 <= len(stage_configs) <= MAX_STAGES:
        raise ValueError(f"stages must contain 1 to {MAX_STAGES} filter stages")
    stages = [_stage(stage, rate) for stage in stage_configs]
    frequencies = value.get("frequencies_hz")
    if frequencies is None:
        anchors = {row["settings"].get("center_hz", row["settings"].get("cutoff_hz")) for row in stages}
        frequencies = sorted({rate * i / 512 for i in range(257)} | anchors)
    else:
        if not isinstance(frequencies, list) or not 1 <= len(frequencies) <= MAX_FREQUENCIES:
            raise ValueError(f"frequencies_hz must contain 1 to {MAX_FREQUENCIES} frequencies")
        frequencies = [_number(f, "frequency_hz", 0, rate / 2) for f in frequencies]
        if any(left >= right for left, right in zip(frequencies, frequencies[1:])):
            raise ValueError("frequencies_hz must be strictly increasing")
    response = []
    for frequency in frequencies:
        parts = [_response(stage, frequency, rate) for stage in stages]
        if any(part is None for part in parts):
            row = {"frequency_hz": frequency, "gain": 0.0, "attenuation_db": None,
                   "phase_deg": None, "group_delay_s": None,
                   "undefined_reason": "Transmission zero: infinite attenuation; phase and group delay undefined."}
        else:
            amplitude = math.prod(part[0] for part in parts)
            # Sum logarithms instead of logging a product that may underflow.
            attenuation = -20 * sum(math.log10(part[0]) for part in parts)
            row = {"frequency_hz": frequency, "gain": amplitude if amplitude else None,
                   "attenuation_db": attenuation, "phase_deg": math.degrees(sum(part[1] for part in parts)),
                   "group_delay_s": sum(part[2] for part in parts),
                   "undefined_reason": None if amplitude else "Amplitude product underflows float64; attenuation, phase and delay remain defined."}
        response.append(row)
    return {"model_version": MODEL_VERSION, "status": "modeled_static", "sample_rate_hz": rate,
            "stages": [{key: item for key, item in stage.items() if not key.startswith("_")} for stage in stages],
            "response": response,
            "qualification": {"input_basis": "operator_defined_hypothetical_static_cascade",
                "static_equation_scope": "Pinned LowPassFilter2p coefficient design and initialized NotchFilter single-stage design",
                "static_equations_numerically_validated": True,
                "exact_firmware_parity": False, "dynamic_bank_parity": False,
                "historical_configuration_verified": False, "tuning_prescription": False,
                "parameter_application_allowed": False, "reviewed_release": "Copter-4.7.1",
                "reviewed_revision": REVISION},
            "references": deepcopy(REFERENCES),
            "validation": {"suite": "engine/tests/test_filter_models.py",
                "independent_oracle": "SciPy 1.17.1 signal.butter, bilinear, freqz and group_delay; development-only",
                "source_oracle": "Pinned unmodified C++ coefficient routines compiled with g++ 14.2.0, float32; 11 frozen coefficient cases",
                "checks": ["Analytic DC, cutoff, notch-center and Nyquist limits",
                    "Independent coefficient, transfer-function and delay comparisons",
                    "Sparse-grid continuous phase and additive cascade delay",
                    "Stable poles and strict finite numerical input bounds"],
                "scope": "Numerical model validation; not target-board, flight or dynamic-bank qualification"},
            "limitations": [
                "Curves are predicted linear steady-state responses, not measured attenuation or flight performance.",
                "Stages are explicitly supplied; no firmware parameter timeline or active pipeline is inferred.",
                "Dynamic tracking, harmonic allocation, composite notches, bank options, center-frequency slew, minimum-frequency behavior and scheduler load are not simulated.",
                "Python double precision does not reproduce firmware float32 rounding, sample transients, reset behavior or board-specific sample pipelines.",
                "Negative group delay near a notch is a local phase slope, not negative transport latency or an improvement in control stability.",
                "Bandwidth uses the pinned ArduPilot Q convention and is not a measured digital -3 dB width.",
                "The plotting grid can miss narrow features; center/cutoff anchors are included in the default grid.",
                "No cutoff, notch frequency, bandwidth, attenuation or parameter write is recommended.",
            ]}
