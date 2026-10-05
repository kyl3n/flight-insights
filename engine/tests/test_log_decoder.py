"""Real wire formats, explicitly synthetic observations; no hardware claims."""
from concurrent.futures import CancelledError
from pathlib import Path
import struct
import tracemalloc

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from q22_engine.log_decoder import iter_log_events, sniff_format


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "logs"


def events(extension, kind=None):
    result = list(iter_log_events(FIXTURES / f"synthetic-flight.{extension}"))
    return result if kind is None else [item for item in result if item["kind"] == kind]


def tlog_file(tmp_path, messages, *, system=1):
    sender = mavlink.MAVLink(None, srcSystem=system, srcComponent=1)
    path = tmp_path / "test.tlog"
    with path.open("wb") as handle:
        for index, message in enumerate(messages):
            handle.write(struct.pack(">Q", 1_760_000_000_000_001 + index * 1_000_000))
            handle.write(message.pack(sender))
    return path


@pytest.mark.parametrize("extension", ["bin", "log", "tlog"])
def test_content_sniffing_without_extension(tmp_path, extension):
    path = tmp_path / "content-addressed-original"
    path.write_bytes((FIXTURES / f"synthetic-flight.{extension}").read_bytes())
    assert sniff_format(path) == extension
    assert list(iter_log_events(path))
    with pytest.raises(ValueError, match="disagrees"):
        list(iter_log_events(path, format="incorrect"))


@pytest.mark.parametrize("extension", ["bin", "log"])
def test_dataflash_schema_scaling_parameter_timeline_and_boot_reset(extension):
    power = events(extension, "power")
    assert [(p["voltage_v"], p["current_a"]) for p in power[:5]] == [(24, 10)] * 5
    assert power[-1]["boot_epoch"] == 1
    assert power[-1]["time_us"] == 1000
    assert power[-1]["remaining_pct"] is None
    assert "remaining_unknown_255" in power[-1]["quality_flags"]
    assert "battery_state_of_health_unknown_0" in power[-1]["quality_flags"]
    assert "zero_temperature_support_unknown" in power[-1]["quality_flags"]
    assert power[0]["source_fields"][2] == "BAT.Volt"
    assert power[0]["field_schema"]["Volt"]["unit"] == "V"
    assert power[0]["field_schema"]["Volt"]["applied_multiplier"] == 1
    attitude = events(extension, "attitude")
    assert attitude[0]["desired_roll_deg"] == 3
    assert attitude[0]["actual_roll_deg"] == 1
    assert attitude[0]["field_schema"]["Roll"]["intrinsic_multiplier"] == .01
    assert attitude[0]["field_schema"]["Roll"]["text_intrinsic_scaling_already_applied"] == (extension == "log")
    parameters = events(extension, "parameter")
    assert len(parameters) == 3
    assert [p["time_us"] for p in parameters] == [0, 3_000_000, 1000]
    assert parameters[0]["value"] == pytest.approx(.1)
    assert parameters[1]["value"] == pytest.approx(.12)
    assert all(p["completeness"] == "observed_only" for p in parameters)


def test_missing_metadata_abstains_instead_of_guessing_units(tmp_path):
    text = (FIXTURES / "synthetic-flight.log").read_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith("FMTU,"))
    path = tmp_path / "unknown.log"
    path.write_text(text)
    decoded = list(iter_log_events(path))
    power = [p for p in decoded if p["kind"] == "power"]
    assert power[0]["raw_fields"]["Volt"] == 24
    assert power[0]["voltage_v"] is None
    assert power[0]["current_a"] is None
    assert any(p["kind"] == "diagnostic" and "unvalidated_units" in p["reason"] for p in decoded)


def test_fmtu_multiplier_applied_to_float_once(tmp_path):
    text = (FIXTURES / "synthetic-flight.log").read_text().replace("MULT, 0, 48, 1", "MULT, 0, 48, 0.001")
    path = tmp_path / "milliunits.log"
    path.write_text(text)
    first = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert first["voltage_v"] == pytest.approx(.024)
    assert first["current_a"] == pytest.approx(.01)


def test_fmtu_declared_instance_field_overrides_name_guess(tmp_path):
    text = (FIXTURES / "synthetic-flight.log").read_text().replace("TimeUS,Inst,Volt", "TimeUS,Port,Volt")
    path = tmp_path / "instance.log"
    path.write_text(text)
    first = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert first["battery_id"] == "0"
    assert first["battery_instance_field"] == "Port"
    path.write_text(text.replace("s#vAO--", "s-vAO--"))
    unknown = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert unknown["battery_id"] is None
    assert "battery_instance_unavailable_or_ambiguous" in unknown["quality_flags"]


def test_zero_multiplier_marker_and_overflow_are_explicit(tmp_path):
    text = (FIXTURES / "synthetic-flight.log").read_text()
    path = tmp_path / "scale.log"
    path.write_text(text.replace("MULT, 0, 48, 1", "MULT, 0, 48, 0"))
    first = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert first["voltage_v"] == 24
    assert first["field_schema"]["Volt"]["zero_multiplier_is_identity"] is True
    path.write_text(text.replace("MULT, 0, 48, 1", "MULT, 0, 48, 1e308"))
    first = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert first["voltage_v"] is None
    assert "normalization_overflow:Volt" in first["quality_flags"]


def test_tlog_source_ids_receive_precision_and_boot_epochs():
    parameters = events("tlog", "parameter")
    assert {p["system_id"] for p in parameters} == {1, 2}
    assert all(p["component_id"] == 1 for p in parameters)
    assert all(p["completeness"] == "observed_only" for p in parameters)
    assert all(p["boot_time_us"] is None for p in parameters)
    assert all(p["time_domain"] == "utc_receive_us" for p in parameters)
    assert all(isinstance(p["receive_time_us"], int) for p in parameters)
    power = events("tlog", "power")
    assert power[0]["voltage_v"] == 24
    assert power[0]["current_a"] == 10
    assert power[0]["battery_id"] == "battery_status:0"
    assert power[-1]["boot_epoch"] == 1
    assert power[-1]["voltage_v"] is None and power[-1]["current_a"] is None
    attitudes = events("tlog", "attitude")
    assert attitudes[0]["desired_roll_deg"] is None
    assert attitudes[0]["actual_roll_deg"] == pytest.approx(5.72957795)


def test_tlog_capture_links_preserve_raw_stamp_and_isolate_clocks(tmp_path):
    sender = mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
    path = tmp_path / "links.tlog"
    with path.open("wb") as handle:
        for link, boot_ms in [(0, 1000), (1, 10), (0, 1100), (1, 20)]:
            raw_receive = 1_760_000_000_000_000 + boot_ms * 1000 + link
            handle.write(struct.pack(">Q", raw_receive))
            handle.write(mavlink.MAVLink_attitude_message(boot_ms, 0., 0., 0., 0., 0., 0.).pack(sender))
    attitudes = [p for p in iter_log_events(path) if p["kind"] == "attitude"]
    assert [p["capture_link_id"] for p in attitudes] == [0, 1, 0, 1]
    assert [p["boot_epoch"] for p in attitudes] == [0, 0, 0, 0]
    assert attitudes[1]["raw_receive_time_us"] == 1_760_000_000_010_001
    assert attitudes[1]["receive_time_us"] == 1_760_000_000_010_000


def test_tlog_split_pack_voltage_extended_cells_and_signed_current(tmp_path):
    # MAVLink uses 65534-mV chunks when total pack voltage exceeds uint16.
    messages = [mavlink.MAVLink_battery_status_message(2, 0, 0, 32767,
                [65534, 4466] + [65535] * 8, -200, -1, -1, -1,
                voltages_ext=[1000, 0, 0, 0])]
    path = tlog_file(tmp_path, messages)
    power = next(p for p in iter_log_events(path) if p["kind"] == "power")
    assert power["voltage_v"] == 71
    assert power["current_a"] == -2
    assert power["battery_id"] == "battery_status:2"
    assert "split pack voltage" in power["voltage_interpretation"]


def test_sys_status_is_separate_monitor_and_uses_centiamps(tmp_path):
    message = mavlink.MAVLink_sys_status_message(0, 0, 0, 0, 24000, 150, 80, 0, 0, 0, 0, 0, 0)
    power = next(p for p in iter_log_events(tlog_file(tmp_path, [message])) if p["kind"] == "power")
    assert power["voltage_v"] == 24
    assert power["current_a"] == 1.5
    assert power["battery_id"] == "sys_status"


def test_crc_validation_cannot_be_disabled_by_environment_or_global(tmp_path, monkeypatch):
    path = tlog_file(tmp_path, [mavlink.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3)])
    bad = bytearray(path.read_bytes())
    bad[-1] ^= 1
    path.write_bytes(bad)
    monkeypatch.setenv("MAV_IGNORE_CRC", "1")
    monkeypatch.setattr(mavlink, "MAVLINK_IGNORE_CRC", True)
    result = list(iter_log_events(path))
    assert len(result) == 1
    assert result[0]["kind"] == "diagnostic"
    assert result[0]["reason"] == "invalid_mavlink_packet"


def test_unidentified_integer_parameter_encoding_abstains(tmp_path):
    path = tlog_file(tmp_path, [mavlink.MAVLink_param_value_message(b"TEST_INT", 3., 6, 1, 0)])
    result = list(iter_log_events(path))
    assert not any(p["kind"] == "parameter" for p in result)
    assert result[0]["name"] == "unvalidated_parameter_observation"


@pytest.mark.parametrize("extension", ["bin", "log", "tlog"])
def test_cancellation_is_checked_between_records(extension):
    with pytest.raises(CancelledError):
        list(iter_log_events(FIXTURES / f"synthetic-flight.{extension}", cancel_check=lambda: True))


@pytest.mark.parametrize("extension", ["bin", "tlog"])
def test_truncated_final_packet_has_explicit_diagnostic(tmp_path, extension):
    path = tmp_path / "truncated"
    path.write_bytes((FIXTURES / f"synthetic-flight.{extension}").read_bytes()[:-2])
    decoded = list(iter_log_events(path))
    assert decoded[-1]["kind"] == "diagnostic"
    assert "truncated" in decoded[-1]["reason"]


def test_binary_corruption_resynchronization_is_reported(tmp_path):
    data = (FIXTURES / "synthetic-flight.bin").read_bytes()
    path = tmp_path / "corrupt.bin"
    # Insert bytes between the first and second fixed-length FMT records.
    path.write_bytes(data[:89] + b"corruption" + data[89:])
    decoded = list(iter_log_events(path))
    assert any(p.get("reason") == "resynchronized_after_corruption" for p in decoded)
    assert len([p for p in decoded if p["kind"] == "power"]) == 6


def test_log_line_limit_and_schema_length_validation(tmp_path):
    path = tmp_path / "bad.log"
    path.write_text("FMT, 129, 20, PARM, QNf, TimeUS,Name,Value\n")
    with pytest.raises(ValueError, match="length"):
        list(iter_log_events(path))
    path.write_text((FIXTURES / "synthetic-flight.log").read_text() + "X" * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        list(iter_log_events(path))


def test_streaming_memory_does_not_grow_with_records(tmp_path):
    prefix = (FIXTURES / "synthetic-flight.log").read_text().split("\nMSG,")[0] + "\n"
    path = tmp_path / "streaming.log"
    with path.open("w") as handle:
        handle.write(prefix)
        for index in range(10000):
            handle.write(f"BAT, {index * 1000000}, 0, 24, 10, 20, 80, 100\n")
    tracemalloc.start()
    count = sum(1 for p in iter_log_events(path) if p["kind"] == "power")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 10000
    assert peak < 2 * 1024 * 1024
