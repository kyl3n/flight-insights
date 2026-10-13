"""Independent transfer oracles, source-generated numbers and numerical limits.

SOURCE_CASES were generated from the unmodified pinned upstream .cpp files
whose SHA256 hashes are in the model references. An isolated /tmp harness
supplied only class declarations/math/vector stubs and exposed coefficients;
g++ 14.2.0 -std=c++17 -O2 on Linux x86_64 compiled the original routines.
No upstream implementation is vendored. Coefficient tolerance allows float32
rounding, and intentionally does not assert bit-for-bit firmware parity.

SciPy 1.17.1 is a development oracle, never a runtime dependency. The frozen
source and analytic tests run without it; the independent live oracle tests
skip explicitly if SciPy is absent.
"""
import cmath
import json
import math

import pytest

from q22_engine.filter_models import filter_model


def low(cutoff=30):
    return {"kind": "butterworth_lowpass", "cutoff_hz": cutoff}


def notch(center=50, bandwidth=20, attenuation=40):
    return {"kind": "ardupilot_static_notch", "center_hz": center,
            "bandwidth_hz": bandwidth, "attenuation_db": attenuation}


def model(*stages, rate=400, frequencies=None):
    config = {"sample_rate_hz": rate, "stages": list(stages)}
    if frequencies is not None:
        config["frequencies_hz"] = frequencies
    return filter_model(config)


SOURCE_CASES = [
    (400, notch(50, 20, 40), [0.86411166191101074, -1.2200973033905029, 0.86136651039123535, 1, -1.2200973033905029, 0.72547817230224609]),
    (1000, notch(80, 40, 30), [0.88069558143615723, -1.5366905927658081, 0.87290364503860474, 1, -1.5366905927658081, 0.75359916687011719]),
    (8000, notch(180, 25, 50), [0.98998385667800903, -1.9601520299911499, 0.98992031812667847, 1, -1.9601520299911499, 0.9799041748046875]),
    (1000, notch(400, 300, 10), [0.84770715236663818, 1.2576582431793213, 0.70684373378753662, 1, 1.2576582431793213, 0.55455082654953003]),
    (1000, notch(10, 19, 80), [0.6149178147315979, -1.2273319959640503, 0.6148408055305481, 1, -1.2273319959640503, 0.22975863516330719]),
    (1000, notch(200, .2, 80), [0.99952453374862671, -0.61774009466171265, 0.99952447414398193, 1, -0.61774009466171265, 0.99904906749725342]),
    (1000, notch(480, 100, 5), [0.99403631687164307, 1.9571917057037354, 0.97871100902557373, 1, 1.9571917057037354, 0.9727473258972168]),
    (400, low(30), [0.041253536939620972, 0.082507073879241943, 0.041253536939620972, 1, -1.3489677906036377, 0.51398193836212158]),
    (1000, low(200), [0.20657208561897278, 0.41314417123794556, 0.20657208561897278, 1, -0.36952739953994751, 0.19581569731235504]),
    (8000, low(5), [3.8446341932285577e-06, 7.6892683864571154e-06, 3.8446341932285577e-06, 1, -1.9944463968276978, 0.9944617748260498]),
    (1000, low(500), [0.63894557952880859, 1.2778911590576172, 0.63894557952880859, 1, 1.142980694770813, 0.41280165314674377]),
]

# Frozen SciPy 1.17.1 bilinear/freqz/group_delay output, generated with the
# dimensional analog design in the independent live oracle below. These
# retain independent response/delay regression coverage in installations
# without SciPy; center attenuation and delay are deliberately included.
SCIPY_RESPONSE_CASES = [
    (400, notch(50, 20, 40), [
        [10, .9922711820642524, -.08713090800460023, .0014966349662540624],
        [45, .21806079089770958, -.4033495881735248, .0129566297968411],
        [50, .010000000000000132, -4.992066614539254e-16, -1.5556349186078413],
        [55, .19324969299256706, .3844954435110656, .011328944151186732],
        [150, .9937745516652433, .07825878637214041, .00029345169201878464],
    ]),
    (1000, notch(80, 40, 30), [
        [20, .9781125680737264, -.14393134085305275, .0012941265770969332],
        [70, .2109419074544851, -.3761558338609568, .005498147664479607],
        [80, .031622776601683576, -8.532109086899457e-16, -.2179378851978183],
        [90, .17962348316024374, .3484484239257433, .004338218995300877],
        [400, .9976799506384098, .04734237351341436, 8.166624503861586e-05],
    ]),
]


@pytest.mark.parametrize("rate,stage,expected", SCIPY_RESPONSE_CASES)
def test_frozen_independent_scipy_transfer_and_delay_oracle(rate, stage, expected):
    result = model(stage, rate=rate, frequencies=[row[0] for row in expected])
    for row, (_, real, imaginary, delay) in zip(result["response"], expected):
        actual = row["gain"] * cmath.exp(1j * math.radians(row["phase_deg"]))
        assert actual == pytest.approx(complex(real, imaginary), abs=1e-12)
        assert row["group_delay_s"] == pytest.approx(delay, rel=2e-7, abs=1e-10)


@pytest.mark.parametrize("rate,stage,coefficients", SOURCE_CASES)
def test_pinned_original_float32_source_coefficients(rate, stage, coefficients):
    value = model(stage, rate=rate)["stages"][0]
    assert value["numerator"] + value["denominator"] == pytest.approx(coefficients, abs=4e-7, rel=4e-7)
    assert value["stable"] and max(value["pole_radii"]) < 1


@pytest.mark.parametrize("rate,cutoff", [(400, 30), (1000, 200), (8000, 5), (1000, 500), (64000, 6.4)])
def test_butterworth_against_independent_scipy_design_and_response(rate, cutoff):
    signal = pytest.importorskip("scipy.signal")
    np = pytest.importorskip("numpy")
    frequencies = [0, min(cutoff, rate * .4), rate * .23, rate * .49]
    frequencies = sorted(set(frequencies))
    actual = model(low(cutoff), rate=rate, frequencies=frequencies)
    b, a = signal.butter(2, min(cutoff, rate * .4), fs=rate)
    assert actual["stages"][0]["numerator"] == pytest.approx(b, rel=1e-12, abs=1e-14)
    assert actual["stages"][0]["denominator"] == pytest.approx(a, rel=1e-12, abs=1e-14)
    _, response = signal.freqz(b, a, worN=frequencies, fs=rate)
    _, delay = signal.group_delay((b, a), w=frequencies, fs=rate)
    for row, h, d in zip(actual["response"], response, delay):
        assert row["gain"] == pytest.approx(abs(h), rel=2e-9, abs=1e-13)
        assert row["phase_deg"] == pytest.approx(np.angle(h, deg=True), abs=2e-8)
        assert row["group_delay_s"] == pytest.approx(d / rate, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("rate,center,bandwidth,depth", [(400, 50, 20, 40), (1000, 80, 40, 30), (8000, 180, 25, 50), (1000, 400, 300, 10)])
def test_finite_depth_notch_against_scipy_analog_bilinear_response_and_delay(rate, center, bandwidth, depth):
    signal = pytest.importorskip("scipy.signal")
    np = pytest.importorskip("numpy")
    # Independent analog oracle: dimensional rad/s, then SciPy's bilinear
    # transform. Q here uses octave/log form rather than production algebra.
    octaves = 2 * math.log2(center / (center - bandwidth / 2))
    quality = math.sqrt(2 ** octaves) / (2 ** octaves - 1)
    omega = 2 * rate * math.tan(math.pi * center / rate)
    damping = omega / quality
    b, a = signal.bilinear([1, damping * 10 ** (-depth / 20), omega ** 2], [1, damping, omega ** 2], fs=rate)
    frequencies = sorted({0, center * .7, center, center * 1.05, min(center * 1.4, rate * .49), rate / 2})
    actual = model(notch(center, bandwidth, depth), rate=rate, frequencies=frequencies)
    assert actual["stages"][0]["numerator"] == pytest.approx(b, abs=1e-14)
    assert actual["stages"][0]["denominator"] == pytest.approx(a, abs=1e-14)
    _, response = signal.freqz(b, a, worN=frequencies, fs=rate)
    _, delays = signal.group_delay((b, a), w=frequencies, fs=rate)
    for row, h, delay in zip(actual["response"], response, delays):
        assert row["gain"] == pytest.approx(abs(h), rel=1e-10, abs=1e-13)
        assert row["phase_deg"] == pytest.approx(np.angle(h, deg=True), abs=1e-9)
        assert row["group_delay_s"] == pytest.approx(delay / rate, rel=2e-7, abs=1e-10)


def test_analytic_limits_at_dc_cutoff_center_and_nyquist():
    lp = model(low(30), frequencies=[0, 30, 200])["response"]
    assert lp[0]["gain"] == 1 and lp[0]["phase_deg"] == 0
    assert lp[1]["gain"] == pytest.approx(1 / math.sqrt(2))
    assert lp[1]["attenuation_db"] == pytest.approx(10 * math.log10(2))
    assert lp[1]["phase_deg"] == -90
    assert lp[2]["gain"] == 0 and lp[2]["phase_deg"] is None
    assert lp[2]["attenuation_db"] is None and lp[2]["group_delay_s"] is None
    nt = model(notch(50, 20, 40), frequencies=[0, 50, 200])["response"]
    assert nt[0]["gain"] == nt[2]["gain"] == 1
    assert nt[1]["gain"] == pytest.approx(.01)
    assert nt[1]["attenuation_db"] == pytest.approx(40)
    assert nt[1]["phase_deg"] == 0
    assert nt[1]["group_delay_s"] < 0  # Local dispersive slope, not latency.
    assert nt[2]["group_delay_s"] > 0


def test_transfer_polynomials_match_response_and_analytic_delay_matches_local_derivative():
    # This catches a disagreement between the emitted coefficients and the
    # prototype used for phase/delay without relying on shared model helpers.
    for stage in (low(), notch()):
        frequencies = [5, 25, 49, 50, 51, 75, 150]
        report = model(stage, frequencies=frequencies)
        coeff = report["stages"][0]

        def transfer(f):
            z = cmath.exp(-2j * math.pi * f / 400)
            return sum(c * z ** k for k, c in enumerate(coeff["numerator"])) / sum(c * z ** k for k, c in enumerate(coeff["denominator"]))

        for row in report["response"]:
            h = transfer(row["frequency_hz"])
            assert row["gain"] == pytest.approx(abs(h), rel=1e-11)
            assert cmath.exp(1j * math.radians(row["phase_deg"])) == pytest.approx(h / abs(h), abs=1e-11)
            delta = 1e-5
            derivative = cmath.phase(transfer(row["frequency_hz"] + delta) / transfer(row["frequency_hz"] - delta)) / (4 * math.pi * delta)
            assert row["group_delay_s"] == pytest.approx(-derivative, rel=1e-6, abs=1e-9)


def test_cascade_phase_never_aliases_on_sparse_grid_and_delay_is_additive():
    frequencies = [0, 199]
    single = model(low(), frequencies=frequencies)["response"][1]
    cascade = model(*([low()] * 8), frequencies=frequencies)["response"][1]
    assert cascade["phase_deg"] < -1400
    assert cascade["phase_deg"] == pytest.approx(8 * single["phase_deg"])
    assert cascade["attenuation_db"] == pytest.approx(8 * single["attenuation_db"])
    assert cascade["gain"] == pytest.approx(single["gain"] ** 8)
    assert cascade["group_delay_s"] == pytest.approx(8 * single["group_delay_s"])


def test_bypass_clamp_and_default_anchor_grid():
    bypass = model(low(0), notch(50, 20, 0))
    assert all(row["gain"] == 1 and row["phase_deg"] == row["group_delay_s"] == 0 for row in bypass["response"])
    assert {0, 50, 200} <= {row["frequency_hz"] for row in bypass["response"]}
    clamped = model(low(200))
    assert clamped["stages"][0]["settings"] == {"cutoff_hz": 160, "requested_cutoff_hz": 200, "cutoff_clamped": True, "bypass": False}


@pytest.mark.parametrize("bad", [
    None, {}, {"sample_rate_hz": True, "stages": [low()]},
    {"sample_rate_hz": 400, "stages": []}, {"sample_rate_hz": 400, "stages": [low()] * 17},
    {"sample_rate_hz": float("nan"), "stages": [low()]},
    {"sample_rate_hz": 10 ** 1000, "stages": [low()]},
    {"sample_rate_hz": 400, "stages": [low()], "surprise": 1},
    {"sample_rate_hz": 400, "stages": [{"kind": "dynamic_notch"}]},
    {"sample_rate_hz": 400, "stages": [low(-1)]},
    {"sample_rate_hz": 400, "stages": [low(.001)]},
    {"sample_rate_hz": 400, "stages": [notch(200)]},
    {"sample_rate_hz": 400, "stages": [notch(50, 100)]},
    {"sample_rate_hz": 400, "stages": [notch(50, 0)]},
    {"sample_rate_hz": 400, "stages": [notch(50, 20, 81)]},
    {"sample_rate_hz": 400, "stages": [{**notch(), "cutoff_hz": 30}]},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": []},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": [0, 0]},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": [2, 1]},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": [201]},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": [float("inf")]},
    {"sample_rate_hz": 400, "stages": [low()], "frequencies_hz": [0] * 1026},
])
def test_rejects_unbounded_unsupported_or_nonfinite_input(bad):
    with pytest.raises(ValueError):
        filter_model(bad)


def test_report_is_bounded_strict_json_and_never_claims_complete_firmware_parity():
    report = model(*([notch()] * 16), frequencies=[200 * i / 1024 for i in range(1025)])
    encoded = json.dumps(report, allow_nan=False)
    assert len(encoded) < 400_000
    assert report["qualification"]["static_equations_numerically_validated"] is True
    for key in ("exact_firmware_parity", "dynamic_bank_parity", "historical_configuration_verified", "tuning_prescription", "parameter_application_allowed"):
        assert report["qualification"][key] is False
    report["references"][0]["sha256"] = "modified"
    assert model(low())["references"][0]["sha256"] != "modified"


def test_underflow_does_not_masquerade_as_an_exact_transmission_zero():
    value = model(*([low(.04)] * 16), frequencies=[math.nextafter(200, 0)])["response"][0]
    assert value["gain"] is None and "underflows" in value["undefined_reason"]
    assert math.isfinite(value["attenuation_db"])
    assert math.isfinite(value["phase_deg"]) and math.isfinite(value["group_delay_s"])
