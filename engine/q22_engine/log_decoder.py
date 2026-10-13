"""Bounded, read-only DataFlash BIN/LOG and MAVLink TLOG decoding.

The iterator never accumulates a flight or claims startup PARM records are a
complete configuration. DataFlash schemas and scaling are taken from the log.
MAVLink packets are CRC-checked by the pinned pymavlink ArduPilot dialect.
TLOG receive time and flight-controller boot time remain separate clocks.
"""
from __future__ import annotations

import math
from concurrent.futures import CancelledError
from pathlib import Path
import re
import struct
from typing import Any, Iterator


PARSER_VERSION = "q22-streaming-flight-log/2"
MAX_LINE_BYTES = 64 * 1024
MAX_RESYNC_BYTES = 1024 * 1024
FORMATS = {"b": "b", "B": "B", "g": "e", "h": "h", "H": "H", "i": "i",
           "I": "I", "f": "f", "d": "d", "n": "4s", "N": "16s", "Z": "64s",
           "c": "h", "C": "H", "e": "i", "E": "I", "L": "i", "M": "b",
           "q": "q", "Q": "Q", "a": "64s"}
INTRINSIC_SCALE = {"c": .01, "C": .01, "e": .01, "E": .01, "L": 1e-7}
PARAMETER_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def _text(value):
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return str(value).split("\0", 1)[0]


def _finite(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _json_value(value):
    if isinstance(value, bytes):
        return _text(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class _Schema:
    def __init__(self, type_id, length, name, codes, columns):
        self.type_id, self.length, self.name = int(type_id), int(length), _text(name)
        self.codes, self.columns = _text(codes), _text(columns).split(",")
        if not 0 <= self.type_id <= 255 or not 3 <= self.length <= 255:
            raise ValueError("Invalid DataFlash FMT type/length.")
        if (not self.name or len(self.name) > 4 or len(_text(columns)) > 64
                or len(self.codes) != len(self.columns)
                or len(set(self.columns)) != len(self.columns) or len(self.codes) > 16):
            raise ValueError("Invalid DataFlash FMT columns.")
        try:
            self.unpack = struct.Struct("<" + "".join(FORMATS[code] for code in self.codes))
        except KeyError as exc:
            raise ValueError(f"Unsupported DataFlash field format: {exc.args[0]}") from exc
        if self.unpack.size != self.length - 3:
            raise ValueError("DataFlash FMT length does not match field definitions.")
        self.unit_ids = self.mult_ids = ""


class _Clock:
    def __init__(self):
        self.states = {}

    def observe(self, key, boot_time):
        # Bounded by the MAVLink address space; fail explicitly before that bound
        # becomes an unreasonable amount of state for an untrusted file.
        if key not in self.states and len(self.states) >= 4096:
            raise ValueError("Log exceeds 4096 source-clock identities.")
        epoch, previous = self.states.get(key, (0, None))
        flags = []
        if boot_time is not None:
            if previous is not None and boot_time < previous:
                epoch += 1
                flags.append("boot_time_discontinuity_unclassified")
            self.states[key] = (epoch, boot_time)
        return epoch, flags


def sniff_log_format(path: Path | str) -> str:
    """Identify bytes, including content-addressed archive files without suffixes."""
    with Path(path).open("rb") as handle:
        head = handle.read(4096)
    if head.startswith(b"\xa3\x95"):
        return "bin"
    if re.search(rb"(?:^|\n)\s*FMT\s*,", head):
        return "log"
    if len(head) >= 9 and head[8] in (0xFE, 0xFD):
        return "tlog"
    raise ValueError("Unrecognized log content; expected DataFlash BIN/LOG or timestamped MAVLink TLOG.")


sniff_format = sniff_log_format


def iter_log_events(path: Path | str, *, format: str | None = None,
                    cancel_check=None) -> Iterator[dict[str, Any]]:
    """Yield normalized records with raw fields, source offsets and unit lineage.

    Every timestamp is an integer microsecond count or None. Consumers crossing
    JavaScript JSON boundaries must serialize these counts as decimal strings.
    `time_us` is boot time for DataFlash and receive time for TLOG, as identified
    by `time_domain`. Unmapped messages remain evidence events. Truncated final
    packets produce a `decode_issue` event; invalid schemas fail explicitly.
    """
    path = Path(path)
    actual = sniff_log_format(path)
    if format is not None and format.lower().lstrip(".") != actual:
        raise ValueError("Requested format disagrees with file contents.")
    if actual == "tlog":
        yield from _tlog_events(path, cancel_check)
    else:
        from .imu_batches import BatchAssembler
        batches = BatchAssembler()
        for event in _dataflash_events(path, actual, cancel_check):
            yield event
            yield from batches.accept(event)
        yield from batches.finish()


def _issue(source_format, offset, index, reason, **details):
    return {"kind": "diagnostic", "name": "decode_issue", "message_type": None,
            "time_us": None, "boot_time_us": None, "receive_time_us": None,
            "time_domain": "unavailable", "boot_epoch": 0, "system_id": None,
            "component_id": None, "record_index": index, "byte_offset": offset,
            "source_format": source_format, "parser_version": PARSER_VERSION,
            "quality_flags": [reason], "reason": reason, "source_fields": [],
            "raw_fields": {}, "field_schema": {}, "details": details,
            "provenance": {"parser_version": PARSER_VERSION, "format": source_format}}


def _records(path, source_format, schemas, cancel_check):
    with path.open("rb") as handle:
        index = 0
        while True:
            if cancel_check is not None and cancel_check():
                raise CancelledError("Log import cancelled.")
            offset = handle.tell()
            if source_format == "bin":
                header = handle.read(3)
                if not header:
                    return
                if len(header) < 3:
                    yield None, {}, offset, index, "truncated_header"
                    return
                if header[:2] != b"\xa3\x95" or header[2] not in schemas:
                    # DataFlash lacks a per-message CRC. Resynchronization is
                    # explicitly a corruption boundary, never invisible repair.
                    skipped = 0
                    while header[:2] != b"\xa3\x95" or header[2] not in schemas:
                        byte = handle.read(1)
                        if not byte:
                            yield None, {}, offset, index, "unrecoverable_trailing_bytes"
                            return
                        header = header[1:] + byte
                        skipped += 1
                        if skipped % 4096 == 0 and cancel_check is not None and cancel_check():
                            raise CancelledError("Log import cancelled.")
                        if skipped >= MAX_RESYNC_BYTES:
                            raise ValueError("DataFlash corruption exceeds 1 MiB resynchronization limit.")
                    yield None, {"skipped_bytes": skipped}, offset, index, "resynchronized_after_corruption"
                    index += 1
                    offset = handle.tell() - 3
                schema = schemas[header[2]]
                payload = handle.read(schema.length - 3)
                if len(payload) != schema.length - 3:
                    yield None, {}, offset, index, "truncated_record"
                    return
                raw = dict(zip(schema.columns, schema.unpack.unpack(payload)))
                for column, code in zip(schema.columns, schema.codes):
                    if code == "a":
                        raw[column] = list(struct.unpack("<32h", raw[column]))
                    elif isinstance(raw[column], bytes):
                        raw[column] = _text(raw[column])
            else:
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError("DataFlash LOG line exceeds 64 KiB limit.")
                line = line.decode("utf-8-sig", errors="strict").strip()
                if not line or line.startswith("#"):
                    continue
                name, separator, rest = line.partition(",")
                schema = next((item for item in schemas.values() if item.name == name.strip()), None)
                if not separator or schema is None:
                    yield None, {}, offset, index, "unknown_text_record"
                    index += 1
                    continue
                # A final Z/N field can contain commas (FMT.Columns, MSG.Message).
                if "a" in schema.codes:
                    # Array commas are internal field separators. Keep the
                    # fixed 32-element arrays intact, with bounded input above.
                    parts, mark, depth = [], 0, 0
                    for position, character in enumerate(rest):
                        if character == "[":
                            depth += 1
                        elif character == "]":
                            depth -= 1
                        elif character == "," and depth == 0:
                            parts.append(rest[mark:position].strip())
                            mark = position + 1
                        if depth not in (0, 1):
                            break
                    parts.append(rest[mark:].strip())
                    if depth != 0:
                        parts = []
                else:
                    parts = [item.strip() for item in rest.split(",", len(schema.columns) - 1)]
                if len(parts) != len(schema.columns):
                    yield None, {}, offset, index, "malformed_text_record"
                    index += 1
                    continue
                raw = {}
                try:
                    for column, code, item in zip(schema.columns, schema.codes, parts):
                        if code in "nNZ":
                            raw[column] = item
                        elif code == "a":
                            raw[column] = [int(x) for x in re.split(r"[\s,]+", item.strip("[]")) if x]
                            if len(raw[column]) != 32:
                                raise ValueError("Expected 32 array elements")
                        elif code in "fgdcCeEL":
                            raw[column] = float(item)
                        elif code == "M":
                            try:
                                raw[column] = int(item)
                            except ValueError:
                                raw[column] = item
                        else:
                            raw[column] = int(item)
                except ValueError:
                    yield None, {}, offset, index, "invalid_text_field"
                    index += 1
                    continue
            yield schema, raw, offset, index, None
            index += 1


def _dataflash_events(path, source_format, cancel_check):
    schemas = {128: _Schema(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")}
    units, multipliers, pending_fmtu = {}, {}, {}
    clocks = _Clock()
    for schema, raw, offset, index, issue in _records(path, source_format, schemas, cancel_check):
        if issue:
            yield _issue(source_format, offset, index, issue, **raw)
            continue
        name = schema.name
        if name == "FMT":
            new = _Schema(raw["Type"], raw["Length"], raw["Name"], raw["Format"], raw["Columns"])
            if new.type_id in pending_fmtu:
                new.unit_ids, new.mult_ids = pending_fmtu[new.type_id]
            schemas[new.type_id] = new
        elif name == "UNIT":
            if not 0 <= int(raw["Id"]) <= 255:
                raise ValueError("UNIT identifier exceeds DataFlash address space.")
            units[chr(int(raw["Id"]))] = _text(raw["Label"])
        elif name == "MULT":
            if not 0 <= int(raw["Id"]) <= 255:
                raise ValueError("MULT identifier exceeds DataFlash address space.")
            multiplier = raw["Mult"]
            if _finite(multiplier):
                multipliers[chr(int(raw["Id"]))] = float(multiplier)
        elif name == "FMTU":
            type_id = int(raw["FmtType"])
            if not 0 <= type_id <= 255:
                raise ValueError("FMTU type exceeds DataFlash address space.")
            ids = (_text(raw["UnitIds"]), _text(raw["MultIds"]))
            pending_fmtu[type_id] = ids
            if type_id in schemas:
                schemas[type_id].unit_ids, schemas[type_id].mult_ids = ids

        field_schema, values, flags = {}, {}, []
        for position, (field, code) in enumerate(zip(schema.columns, schema.codes)):
            unit_id = schema.unit_ids[position:position + 1] or None
            mult_id = schema.mult_ids[position:position + 1] or None
            intrinsic = INTRINSIC_SCALE.get(code)
            declared = multipliers.get(mult_id)
            # DataFlash text contains already-decoded intrinsic c/C/e/E/L values.
            # MULT specifies the raw unit scale for other formats. Never double
            # apply the intrinsic scale when FMTU declares the same multiplier.
            # MULT zero is the DataFlash no-multiplier marker, not zero-valued
            # physical data (the reference reader leaves the base unit intact).
            scale = (intrinsic if source_format == "bin" else 1.0) if intrinsic is not None else (1.0 if declared == 0 else declared)
            value = raw[field]
            values[field] = value * scale if _finite(value) and scale is not None else None
            if values[field] is not None and not _finite(values[field]):
                values[field] = None
                flags.append(f"normalization_overflow:{field}")
            if isinstance(value, float) and not math.isfinite(value):
                flags.append(f"nonfinite:{field}")
            field_schema[field] = {"source_field": f"{name}.{field}", "format_code": code,
                                   "unit_id": unit_id, "unit": units.get(unit_id),
                                   "multiplier_id": mult_id, "declared_multiplier": declared,
                                   "intrinsic_multiplier": intrinsic, "applied_multiplier": scale,
                                   "zero_multiplier_is_identity": declared == 0,
                                   "text_intrinsic_scaling_already_applied": source_format == "log" and intrinsic is not None,
                                   "unit_basis": "in_log_FMTU_UNIT_MULT" if unit_id in units else "unknown"}

        # The timestamp field name is a documented DataFlash wire convention.
        boot = None
        if _finite(raw.get("TimeUS")) and raw["TimeUS"] >= 0:
            boot = int(raw["TimeUS"])
        elif _finite(raw.get("TimeMS")) and raw["TimeMS"] >= 0:
            boot = int(raw["TimeMS"]) * 1000
        epoch, clock_flags = clocks.observe((None, None), boot)
        event = {"kind": "event", "name": name, "message_type": name,
                 "time_us": boot, "boot_time_us": boot, "receive_time_us": None,
                 "time_domain": "flight_controller_boot", "boot_epoch": epoch,
                 "boot_epoch_basis": "conservative clock segment; reset, wrap and delayed/out-of-order records are not distinguished",
                 "system_id": None, "component_id": None, "record_index": index,
                 "capture_link_id": None,
                 "byte_offset": offset, "source_format": source_format,
                 "parser_version": PARSER_VERSION, "raw_fields": {k: _json_value(v) for k, v in raw.items()},
                 "source_fields": [f"{name}.{field}" for field in raw],
                 "field_schema": field_schema, "quality_flags": flags + clock_flags,
                 "provenance": {"parser_version": PARSER_VERSION, "format": source_format,
                                "scaling": "format intrinsic scale once; otherwise in-log MULT", "field_schema": field_schema},
                 "time_basis": "DataFlash TimeUS/TimeMS boot-time wire convention" if boot is not None else "unavailable"}

        def quantity(field, acceptable_units):
            descriptor = field_schema.get(field, {})
            if descriptor.get("unit") not in acceptable_units or values.get(field) is None:
                event["quality_flags"].append(f"unvalidated_units_or_value:{field}")
                return None
            return values[field]

        if name in {"FMT", "FMTU", "UNIT", "MULT"}:
            event["kind"] = "schema"
        elif name == "PARM":
            parameter_name, value = _text(raw.get("Name", "")), raw.get("Value")
            if PARAMETER_NAME.fullmatch(parameter_name) and _finite(value):
                event.update(kind="parameter", name=parameter_name, value=value,
                             parameter_index=None, parameter_count=None,
                             completeness="observed_only", observation_kind="logged_parameter")
            else:
                event["quality_flags"].append("invalid_parameter_observation")
        elif name in {"BAT", "BATT"}:
            instance_fields = [field for field, descriptor in field_schema.items() if descriptor["unit_id"] == "#"]
            if not instance_fields:
                instance_fields = [field for field in ("Inst", "Instance") if field in raw]
            instance_field = instance_fields[0] if len(instance_fields) == 1 else None
            battery_id = str(raw[instance_field]) if instance_field is not None else None
            if battery_id is None:
                event["quality_flags"].append("battery_instance_unavailable_or_ambiguous")
            event.update(kind="power", battery_id=battery_id, battery_instance_field=instance_field,
                         voltage_v=quantity("Volt", {"V", "v", "volt", "volts"}),
                         current_a=quantity("Curr", {"A", "amp", "amps"}))
            if event["voltage_v"] is not None and event["voltage_v"] < 0:
                event["voltage_v"] = None
                event["quality_flags"].append("invalid_negative_voltage")
            # Preserve battery sentinel evidence without asserting temperature
            # support or undocumented firmware-specific consumption units.
            event["remaining_pct"] = raw.get("RemPct") if _finite(raw.get("RemPct")) and 0 <= raw["RemPct"] <= 100 else None
            if raw.get("RemPct") == 255:
                event["quality_flags"].append("remaining_unknown_255")
            if raw.get("SH") == 0:
                event["quality_flags"].append("battery_state_of_health_unknown_0")
            if raw.get("Temp") == 0:
                event["quality_flags"].append("zero_temperature_support_unknown")
        elif name == "ATT":
            event.update(kind="attitude", roll_deg=quantity("Roll", {"deg", "degrees"}),
                         desired_roll_deg=quantity("DesRoll", {"deg", "degrees"}),
                         pitch_deg=quantity("Pitch", {"deg", "degrees"}),
                         yaw_deg=quantity("Yaw", {"deg", "degrees"}))
            event["actual_roll_deg"] = event["roll_deg"]
        elif name in {"VER", "MSG"}:
            event.update(kind="identity", identity_basis="logged_text_or_version_record",
                         firmware=None, board=None, text=_text(raw.get("Message", "")))
        yield event
        if event["quality_flags"]:
            yield {**event, "kind": "diagnostic", "name": "record_quality",
                   "reason": ";".join(event["quality_flags"])}


def _tlog_events(path, cancel_check):
    try:
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
    except ImportError as exc:
        raise ValueError("TLOG decoding requires the packaged pymavlink dependency.") from exc
    # pymavlink exposes an environment-based CRC bypass for debugging. Imported
    # evidence always requires checksum validation, even if that flag was set.
    mavlink.MAVLINK_IGNORE_CRC = False
    parser = mavlink.MAVLink(None)
    clocks, last_receive, autopilots = _Clock(), None, {}
    with path.open("rb") as handle:
        index = 0
        while True:
            if cancel_check is not None and cancel_check():
                raise CancelledError("Log import cancelled.")
            offset = handle.tell()
            stamp = handle.read(8)
            if not stamp:
                return
            if len(stamp) != 8:
                yield _issue("tlog", offset, index, "truncated_receive_timestamp")
                return
            raw_receive = struct.unpack(">Q", stamp)[0]
            capture_link = raw_receive & 3
            receive = raw_receive & ~3
            magic = handle.read(1)
            if magic not in (b"\xfe", b"\xfd"):
                yield _issue("tlog", offset, index, "invalid_mavlink_frame_magic")
                return
            header = magic + handle.read(9 if magic == b"\xfd" else 5)
            if len(header) != (10 if magic == b"\xfd" else 6):
                yield _issue("tlog", offset, index, "truncated_mavlink_header")
                return
            remaining = header[1] + 2 + (13 if magic == b"\xfd" and header[2] & 1 else 0)
            payload = handle.read(remaining)
            if len(payload) != remaining:
                yield _issue("tlog", offset, index, "truncated_mavlink_packet")
                return
            try:
                message = parser.parse_char(header + payload)
            except Exception as exc:
                # Reset parser after a malformed/checksum-invalid packet. We know
                # the next timestamp boundary from the frame header length.
                parser = mavlink.MAVLink(None)
                yield _issue("tlog", offset, index, "invalid_mavlink_packet", error=type(exc).__name__)
                index += 1
                continue
            if message is None or message.get_type() == "BAD_DATA":
                yield _issue("tlog", offset, index, "undecodable_mavlink_packet")
                index += 1
                continue
            if message.get_type().startswith("UNKNOWN"):
                yield _issue("tlog", offset, index, "unknown_mavlink_message_crc_unverified")
                index += 1
                continue
            raw = {k: _json_value(v) for k, v in message.to_dict().items() if k != "mavpackettype"}
            name, system, component = message.get_type(), message.get_srcSystem(), message.get_srcComponent()
            boot = int(raw["time_boot_ms"]) * 1000 if _finite(raw.get("time_boot_ms")) else None
            # time_usec can mean boot time or Unix epoch, depending on message.
            # Retain it raw instead of guessing alignment from its magnitude.
            epoch, flags = clocks.observe((system, component, capture_link), boot)
            if last_receive is not None and receive < last_receive:
                flags.append("receive_clock_regression")
            last_receive = receive
            units = getattr(message, "fieldunits_by_name", {})
            event = {"kind": "event", "name": name, "message_type": name,
                     "time_us": receive, "receive_time_us": receive, "boot_time_us": boot,
                     "raw_receive_time_us": raw_receive, "capture_link_id": capture_link,
                     "capture_link_id_basis": "pymavlink low-two-timestamp-bit convention; exporter convention unverified",
                     "time_domain": "utc_receive_us", "boot_epoch": epoch,
                     "boot_epoch_basis": "conservative clock segment; reset, wrap and delayed/out-of-order records are not distinguished",
                     "system_id": system, "component_id": component, "record_index": index,
                     "byte_offset": offset, "source_format": "tlog", "parser_version": PARSER_VERSION,
                     "raw_fields": raw, "source_fields": [f"{name}.{key}" for key in raw],
                     "field_schema": {key: {"source_field": f"{name}.{key}", "unit": units.get(key),
                                            "unit_basis": "pymavlink_ardupilotmega_v20_wire_schema"} for key in raw},
                     "quality_flags": flags, "mavlink_sequence": message.get_seq(),
                     "provenance": {"parser_version": PARSER_VERSION, "format": "tlog",
                                    "dialect": "pymavlink-2.4.49-ardupilotmega-v20", "CRC_validated": True,
                                    "capture_link_convention": "pymavlink low two timestamp bits; assumption, not verified physical channel"},
                     "signature_verified": False,
                     "receive_time_validation": "recorded_time_not_independently_verified",
                     "time_basis": "TLOG big-endian uint64 ground receive microseconds, low two capture-link bits cleared"}
            if name == "PARAM_VALUE":
                parameter_name, value = _text(raw.get("param_id", "")), raw.get("param_value")
                # ArduPilot uses C-cast PARAM_VALUE encoding. Generic MAVLink
                # bytewise integer parameters must not silently be interpreted.
                encoding_known = raw.get("param_type") == 9 or autopilots.get((system, component, capture_link)) == 3
                if PARAMETER_NAME.fullmatch(parameter_name) and _finite(value) and encoding_known:
                    event.update(kind="parameter", name=parameter_name, value=value,
                                 parameter_index=raw.get("param_index"), parameter_count=raw.get("param_count"),
                                 mav_param_type=raw.get("param_type"), completeness="observed_only",
                                 observation_kind="mavlink_parameter_readback",
                                 value_encoding="REAL32" if raw.get("param_type") == 9 else "ArduPilot C-cast, heartbeat-identified")
                else:
                    event.update(kind="diagnostic", name="unvalidated_parameter_observation")
                    event["quality_flags"].append("invalid_parameter_or_unverified_integer_encoding")
            elif name in {"SYS_STATUS", "BATTERY_STATUS"}:
                voltage, current = None, raw.get("current_battery")
                if current == -1:
                    current = None
                    event["quality_flags"].append("current_unknown_minus_one")
                current = current * .01 if _finite(current) else None
                if name == "SYS_STATUS":
                    value = raw.get("voltage_battery")
                    voltage = value * .001 if _finite(value) and value != 65535 else None
                else:
                    cells = raw.get("voltages", [])
                    # First field may hold pack voltage when cells are unavailable.
                    available = [cell for cell in cells if cell != 65535]
                    extension = [cell for cell in raw.get("voltages_ext", []) if cell != 0]
                    voltage = (sum(available) + sum(extension)) * .001 if available else None
                if voltage is None:
                    event["quality_flags"].append("voltage_unknown_65535")
                event.update(kind="power", battery_id=f"battery_status:{raw.get('id', 0)}" if name == "BATTERY_STATUS" else "sys_status",
                             voltage_v=voltage, current_a=current,
                             remaining_pct=raw.get("battery_remaining") if 0 <= raw.get("battery_remaining", -1) <= 100 else None,
                             voltage_interpretation="sum reported cells or split pack voltage; schema cannot distinguish those encodings" if name == "BATTERY_STATUS" else "system aggregate pack voltage")
                if name == "BATTERY_STATUS":
                    event["temperature_c"] = raw["temperature"] * .01 if _finite(raw.get("temperature")) and raw["temperature"] != 32767 else None
                for field in ("voltage_battery", "voltages", "voltages_ext", "current_battery"):
                    if field in event["field_schema"]:
                        event["field_schema"][field]["applied_multiplier"] = .01 if field == "current_battery" else .001
            elif name == "ATTITUDE":
                event.update(kind="attitude", **{f"{axis}_deg": math.degrees(raw[axis]) if _finite(raw.get(axis)) else None for axis in ("roll", "pitch", "yaw")})
                event["desired_roll_deg"] = None
                event["actual_roll_deg"] = event["roll_deg"]
                for axis in ("roll", "pitch", "yaw"):
                    event["field_schema"][axis].update(applied_multiplier=180 / math.pi, normalized_unit="deg")
            elif name in {"AUTOPILOT_VERSION", "HEARTBEAT"}:
                event.update(kind="identity", identity_basis="mavlink_version_or_heartbeat", firmware=None, board=None)
                if name == "HEARTBEAT":
                    if (system, component, capture_link) not in autopilots and len(autopilots) >= 4096:
                        raise ValueError("Log exceeds 4096 autopilot identities.")
                    autopilots[system, component, capture_link] = raw.get("autopilot")
            yield event
            if event["quality_flags"] and event["kind"] != "diagnostic":
                yield {**event, "kind": "diagnostic", "name": "record_quality",
                       "reason": ";".join(event["quality_flags"])}
            index += 1
