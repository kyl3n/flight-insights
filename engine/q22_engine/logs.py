"""Streaming log evidence import and source/boot-scoped session construction."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import CancelledError
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .log_decoder import iter_log_events
from .parameters import (CONDITIONS, MAX_PARAMETERS, MAX_TIME_SECONDS, NAME,
                         bounded_page, finite, identity, performance_metrics,
                         text_field)
from .store import Store


PARSER_VERSION = "q22-flight-log-ingestion/1"
SHARD_ROWS = 131072
SCHEMA = pa.schema([
    ("kind", pa.string()), ("source_key", pa.string()), ("boot_epoch", pa.int32()),
    ("time_us", pa.int64()), ("record_index", pa.int64()), ("battery_id", pa.string()),
    ("voltage_v", pa.float64()), ("current_a", pa.float64()),
    ("desired_roll_deg", pa.float64()), ("actual_roll_deg", pa.float64()),
    ("payload", pa.string()),
])
LIMITATIONS = [
    "Decoded parameter observations are not proof of a complete unchanged parameter snapshot for an entire flight.",
    "Aircraft identity, selected phase and flight interval need operator review; log coverage does not establish flight safety.",
    "TLOG receive timestamps describe reception, not synchronized vehicle measurement time; no cross-source interpolation is performed.",
    "Unrecognized schemas and unknown units remain explicit; no numerical firmware-specific tuning advice is produced.",
]


def check_cancel(callback):
    if callback and callback():
        raise CancelledError("Log operation cancelled.")


def safe_json(data):
    if isinstance(data, dict):
        return {key: safe_json(value) for key, value in data.items()}
    if isinstance(data, list):
        return [safe_json(value) for value in data]
    if isinstance(data, int) and abs(data) > 2 ** 53 - 1:
        return str(data)
    return data


def source_key(event):
    if event.get("system_id") is None:
        if event.get("source_format") not in ("bin", "log"):
            return "unscoped"
        return "dataflash"
    base = f"{int(event['system_id'])}:{int(event.get('component_id') or 0)}"
    link = event.get("capture_link_id", 0)
    return f"{base}@{link}" if link else base


def hash_file(path, cancel_check=None):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            check_cancel(cancel_check)
            result.update(chunk)
    return result.hexdigest()


def append_bounded(items, value, maximum=100):
    if value not in items and len(items) < maximum:
        items.append(value)


def decoded_events(path, cancel_check):
    try:
        yield from iter_log_events(path, cancel_check=cancel_check)
    except (ValueError, UnicodeDecodeError) as exc:
        # Completed prefix evidence remains usable and the whole immutable source
        # stays available for a future decoder; never turn a partial parse healthy.
        yield {"kind": "diagnostic", "reason": f"Decoder stopped: {exc}",
               "time_us": None, "boot_epoch": 0, "system_id": None, "component_id": None,
               "source_format": "unknown", "record_index": 0,
               "provenance": {"parser_version": PARSER_VERSION, "format": "unknown"}}


def conditions_from(params):
    given = params.get("conditions", {})
    if not isinstance(given, dict):
        raise ValueError("conditions must be an object.")
    result = {}
    for key in CONDITIONS:
        value = given.get(key)
        if value is None or value == "":
            result[key] = None
        elif key in ("battery_id", "hardware_revision", "props"):
            result[key] = text_field(given, key)
        else:
            value = finite(value, key)
            if key in ("mass_kg", "payload_kg", "wind_mps") and value < 0:
                raise ValueError(f"{key} cannot be negative.")
            if key == "battery_soc_pct" and not 0 <= value <= 100:
                raise ValueError("battery_soc_pct must be 0..100.")
            result[key] = value
    return result


class LogService:
    def __init__(self, store: Store):
        self.store = store
        self.derived = store.root / "derived" / "logs"
        self.derived.mkdir(parents=True, exist_ok=True)
        self._verified_artifacts = {}

    def dispatch(self, method, params, cancel_check=None, progress=None):
        if not isinstance(params, dict):
            raise ValueError("Method params must be an object.")
        if method == "logs.import":
            return self.import_log(params, cancel_check, progress)
        if method == "logs.session":
            return self.create_session(params, cancel_check, progress)
        if method == "logs.list":
            return self.list_logs(params)
        if method == "logs.detail":
            return self.detail(params)
        raise ValueError(f"Unsupported log method: {method}")

    def import_log(self, params, cancel_check=None, progress=None):
        label = text_field(params, "label")
        synthetic = params.get("synthetic", False)
        if not isinstance(synthetic, bool):
            raise ValueError("synthetic must be boolean.")
        source = self.store.archive_file(text_field(params, "path", 4096),
                                         cancel_check=cancel_check, progress=progress)
        original = self.store.raw_path(source["sha256"])
        token = str(uuid4())
        temporary, final = self.derived / f".{token}.tmp", self.derived / token
        temporary.mkdir()
        streams = {}
        warnings, schemas, batch = [], [], []
        event_counts = Counter()
        active_streams = {}
        count = 0
        format_name = None
        writer = None
        shard_rows = 0
        shards = []

        def close_shard():
            nonlocal writer, shard_rows
            if writer is None:
                return
            writer.close()
            writer = None
            shard_path = temporary / f"part-{len(shards):05d}.parquet"
            shards.append({"name": shard_path.name, "sha256": hash_file(shard_path, cancel_check),
                           "byte_size": shard_path.stat().st_size, "event_count": shard_rows})
            shard_rows = 0

        def flush_batch():
            nonlocal writer, shard_rows
            if not batch:
                return
            if writer is None:
                writer = pq.ParquetWriter(temporary / f"part-{len(shards):05d}.parquet", SCHEMA,
                                          compression="zstd", write_page_checksum=True)
            writer.write_table(pa.Table.from_pylist(batch, schema=SCHEMA))
            shard_rows += len(batch)
            batch.clear()
            # Arrow retains row-group footer metadata until the writer closes.
            # Sharding bounds that state independently of total log length.
            if shard_rows >= SHARD_ROWS:
                close_shard()

        try:
            for event in decoded_events(original, cancel_check):
                check_cancel(cancel_check)
                count += 1
                if count > 20_000_000:
                    raise ValueError("Log exceeds the supported 20 million normalized events.")
                key, epoch = source_key(event), int(event.get("boot_epoch", 0))
                if not 0 <= epoch < 2 ** 31:
                    raise ValueError("Unsupported boot epoch.")
                stream_key = (key, epoch)
                if stream_key not in streams:
                    if len(streams) >= 64:
                        raise ValueError("Log exceeds 64 independent source/boot streams; split input without altering originals.")
                    streams[stream_key] = {
                        "source_key": key, "system_id": event.get("system_id"),
                        "component_id": event.get("component_id"), "boot_epoch": epoch,
                        "capture_link_id": event.get("capture_link_id"),
                        "capture_link_id_basis": event.get("capture_link_id_basis"),
                        "boot_epoch_basis": event.get("boot_epoch_basis", "Conservative clock segment; reset/reordering/wrap not independently classified."),
                        "time_domain": event.get("time_domain", "boot_us" if key == "dataflash" else "utc_receive_us"),
                        "origin_time_us": None, "start_s": None, "end_s": None,
                        "record_count": 0, "parameter_observation_count": 0,
                        "power_sample_count": 0, "validated_power_sample_count": 0, "tracking_sample_count": 0,
                        "battery_ids": [], "identity_observations": [], "warnings": [],
                    }
                stream = streams[stream_key]
                stream["record_count"] += 1
                time_us = event.get("time_us")
                if time_us is not None:
                    active_streams[key] = stream
                    if not isinstance(time_us, int) or abs(time_us) >= 2 ** 63:
                        raise ValueError("Decoder timestamp exceeds signed 64-bit microseconds.")
                    if stream["origin_time_us"] is None:
                        stream["origin_time_us"] = "0" if key == "dataflash" else str(time_us)
                    relative = (time_us - int(stream["origin_time_us"])) / 1_000_000
                    stream["start_s"] = relative if stream["start_s"] is None else min(stream["start_s"], relative)
                    stream["end_s"] = relative if stream["end_s"] is None else max(stream["end_s"], relative)
                kind = event["kind"]
                event_counts[kind] += 1
                if kind == "parameter":
                    stream["parameter_observation_count"] += 1
                elif kind == "power":
                    stream["power_sample_count"] += 1
                    if event.get("battery_id") is not None:
                        append_bounded(stream["battery_ids"], str(event["battery_id"]), 64)
                        if event.get("voltage_v") is not None and event.get("current_a") is not None:
                            stream["validated_power_sample_count"] += 1
                elif kind == "attitude" and event.get("desired_roll_deg") is not None and event.get("actual_roll_deg") is not None:
                    stream["tracking_sample_count"] += 1
                elif kind == "identity":
                    append_bounded(stream["identity_observations"], safe_json(event), 12)
                elif kind == "diagnostic":
                    warning = str(event.get("message") or event.get("warning") or event.get("reason") or event.get("name") or "Decoder diagnostic")[:1000]
                    append_bounded(warnings, warning)
                    append_bounded(stream["warnings"], warning, 20)
                    # A corrupt packet may have lost its source or time fields.
                    # Insert an explicit barrier into each active source/boot,
                    # so a later sample never integrates across the byte gap.
                    if event.get("time_us") is None or "receive_clock_regression" in event.get("quality_flags", []):
                        affected = active_streams.values() if event.get("time_us") is None else [stream]
                        for affected_stream in affected:
                            guard = {"kind": "diagnostic", "metric_barrier": True, "reason": warning,
                                     "system_id": affected_stream["system_id"], "component_id": affected_stream["component_id"],
                                     "capture_link_id": affected_stream["capture_link_id"],
                                     "boot_epoch": affected_stream["boot_epoch"], "time_us": None,
                                     "source_format": event.get("source_format"), "record_index": event.get("record_index", count)}
                            batch.append({"kind": "diagnostic", "source_key": affected_stream["source_key"],
                                          "boot_epoch": affected_stream["boot_epoch"], "time_us": None,
                                          "record_index": int(event.get("record_index", count)), "payload": json.dumps(guard)})
                elif kind == "schema":
                    append_bounded(schemas, safe_json(event), 100)
                format_name = format_name or event.get("format") or event.get("provenance", {}).get("format")
                payload = json.dumps(event, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
                if len(payload) > 128 * 1024:
                    raise ValueError("Normalized event exceeds 128 KiB; unsupported schema data retained only in original.")
                batch.append({"kind": kind, "source_key": key, "boot_epoch": epoch,
                              "time_us": time_us, "record_index": int(event.get("record_index", count)),
                              "battery_id": str(event.get("battery_id", "unknown")) if kind == "power" else None,
                              "voltage_v": event.get("voltage_v"), "current_a": event.get("current_a"),
                              "desired_roll_deg": event.get("desired_roll_deg"), "actual_roll_deg": event.get("actual_roll_deg"),
                              "payload": payload})
                if len(batch) >= 1024:
                    flush_batch()
                    if progress:
                        progress("decoding", count, None)
            if batch:
                flush_batch()
            close_shard()
            check_cancel(cancel_check)
            if count == 0:
                raise ValueError("No recognized log records found; original remains archived.")
            digest = hashlib.sha256(json.dumps(shards, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            os.replace(temporary, final)
            meaningful = sum(event_counts[k] for k in ("parameter", "power", "attitude", "identity", "event"))
            if not meaningful:
                status = "unsupported"
            else:
                status = "partial" if warnings else "decoded"
            for stream in streams.values():
                if stream["origin_time_us"] is None:
                    stream["origin_time_us"] = "0"
                stream["parameter_completeness"] = "observed_only"
            document = {
                "label": label, "source": source, "synthetic": synthetic, "format": format_name or "log",
                "status": status, "record_count": count, "event_counts": dict(event_counts),
                "streams": list(streams.values()), "warnings": warnings, "schemas": schemas,
                "parser_version": PARSER_VERSION, "immutable": True,
                "derived_file": final.name, "derived_sha256": digest, "derived_shards": shards,
                "capabilities": {"parameter_timeline": bool(event_counts["parameter"]),
                                 "power": any(s["validated_power_sample_count"] for s in streams.values()), "tracking": any(s["tracking_sample_count"] for s in streams.values()),
                                 "dsp": False, "automatic_flight_segmentation": False},
                "limitations": LIMITATIONS,
            }
            check_cancel(cancel_check)
            log = self.store.add_document("log", document)
            if progress:
                progress("completed", count, count)
            return {"log": self.summary(log)}
        finally:
            if writer is not None:
                writer.close()
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def summary(log):
        return {key: value for key, value in log.items() if key not in ("derived_file", "derived_shards", "schemas")}

    def list_logs(self, params):
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        logs = self.store.list_documents("log", limit, offset)
        return bounded_page("logs", [self.summary(item) for item in logs], offset, limit)

    def artifact(self, log):
        name = log["derived_file"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid derived log reference.")
        return self.derived / name

    def events(self, log, selected=None, kinds=None, cancel_check=None):
        filter_expr = None
        if selected is not None:
            filter_expr = (ds.field("source_key") == selected["source_key"]) & (ds.field("boot_epoch") == selected["boot_epoch"])
        if kinds:
            kind_filter = ds.field("kind").isin(kinds)
            filter_expr = kind_filter if filter_expr is None else filter_expr & kind_filter
        artifact = self.artifact(log)
        paths = [artifact / shard["name"] for shard in log["derived_shards"]] if "derived_shards" in log else [artifact]
        # Open one bounded shard at a time, preserving source record order and
        # preventing dataset footer caches from growing with the complete log.
        for path in paths:
            scanner = ds.dataset(path, format="parquet").scanner(
                columns=["payload"], filter=filter_expr, batch_size=1024,
                batch_readahead=1, fragment_readahead=1, use_threads=False)
            for batch in scanner.to_batches():
                check_cancel(cancel_check)
                for payload in batch.column(0).to_pylist():
                    yield json.loads(payload)

    def verify_artifact(self, log, cancel_check=None):
        path = self.artifact(log)
        shards = log.get("derived_shards")
        if shards is not None:
            expected_names = {shard["name"] for shard in shards}
            if not path.is_dir() or {item.name for item in path.iterdir()} != expected_names:
                raise ValueError("Derived log shard set failed integrity verification; reimport original.")
            if any(Path(name).name != name for name in expected_names):
                raise ValueError("Invalid shard reference.")
            paths = [path / shard["name"] for shard in shards]
        else:
            paths = [path]  # Read older single-file derived catalogs safely.
        stamps = []
        for item in paths:
            stamp = item.stat()
            stamps.append((str(item), stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns))
        key = (tuple(stamps), log["derived_sha256"])
        if key in self._verified_artifacts:
            return
        if shards is None:
            valid = hash_file(path, cancel_check) == log["derived_sha256"]
        else:
            valid = hashlib.sha256(json.dumps(shards, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == log["derived_sha256"]
            for item, manifest in zip(paths, shards):
                valid = valid and hash_file(item, cancel_check) == manifest["sha256"]
                if not valid:
                    break
        if not valid:
            raise ValueError("Derived log failed integrity verification; reimport the immutable original.")
        if len(self._verified_artifacts) >= 128:
            self._verified_artifacts.clear()
        self._verified_artifacts[key] = True

    @staticmethod
    def select_stream(log, params, required=True):
        key = params.get("source_key")
        epoch = params.get("boot_epoch")
        if key is None and epoch is None and not required:
            return log["streams"][0]
        if not isinstance(key, str) or not isinstance(epoch, int) or isinstance(epoch, bool):
            raise ValueError("Select an explicit source_key and numeric boot_epoch.")
        for stream in log["streams"]:
            if stream["source_key"] == key and stream["boot_epoch"] == epoch:
                return stream
        raise ValueError("Source/boot stream does not exist in this log.")

    def detail(self, params):
        log = self.store.get_document("log", text_field(params, "log_id"))
        self.verify_artifact(log)
        selected = self.select_stream(log, params, required=False)
        offset, limit = int(params.get("timeline_offset", 0)), int(params.get("timeline_limit", 100))
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Timeline offset must be nonnegative and limit 1..200.")
        observations = []
        index = 0
        more = False
        for event in self.events(log, selected, ["parameter"]):
            if index < offset:
                index += 1
                continue
            if len(observations) >= limit:
                more = True
                break
            event = safe_json(event)
            timestamp = event.get("time_us")
            event["time_us"] = str(timestamp) if timestamp is not None else None
            event["time_s"] = (int(timestamp) - int(selected["origin_time_us"])) / 1_000_000 if timestamp is not None else None
            event["source_key"] = selected["source_key"]
            event["completeness"] = "observed_only"
            observations.append(event)
        return {"log": self.summary(log), "streams": log["streams"], "schemas": log.get("schemas", []),
                "parameter_observations": observations, "selected_stream": selected,
                "timeline_next_offset": offset + len(observations) if more else None}

    def create_session(self, params, cancel_check=None, progress=None):
        log = self.store.get_document("log", text_field(params, "log_id"))
        selected = self.select_stream(log, params)
        metadata = identity(params)
        label, phase = text_field(params, "label"), text_field(params, "phase")
        conditions = conditions_from(params)
        start = finite(params.get("interval_start_s"), "interval_start_s")
        end = finite(params.get("interval_end_s"), "interval_end_s")
        gap = finite(params.get("max_gap_s"), "max_gap_s")
        if start < 0 or end <= start or end > MAX_TIME_SECONDS or gap <= 0:
            raise ValueError("Require 0 <= interval_start_s < interval_end_s and max_gap_s > 0.")
        batteries = selected["battery_ids"]
        battery = params.get("battery_id")
        if battery is None and len(batteries) == 1:
            battery = batteries[0]
        if batteries and battery not in batteries:
            raise ValueError("Select one recorded battery monitor; aggregate and physical monitors must not be combined.")
        if not batteries and battery is not None:
            raise ValueError("Selected stream has no recorded battery monitor.")
        self.verify_artifact(log, cancel_check)
        parameter_values, current_values = {}, {}
        parameter_changes, warnings = [], list(selected["warnings"])
        parameter_change_count = 0
        unscoped_parameters = 0
        origin = int(selected["origin_time_us"])
        snapshot_at = None
        rows = {"power": 0, "tracking": 0}
        last_time = {"power": None, "tracking": None}
        break_next = {"power": False, "tracking": False}
        source_fields = {"power": [], "tracking": []}
        with tempfile.TemporaryDirectory(prefix="log-metrics-", dir=self.derived) as scratch:
            power_path, tracking_path = Path(scratch) / "power.csv", Path(scratch) / "tracking.csv"
            with power_path.open("w", newline="") as power_file, tracking_path.open("w", newline="") as tracking_file:
                writers = {"power": csv.writer(power_file), "tracking": csv.writer(tracking_file)}
                for writer in writers.values():
                    writer.writerow(["time_s", "voltage_v", "current_a", "desired_roll_deg", "actual_roll_deg", "quality_break_before"])
                for count, event in enumerate(self.events(log, selected, ["parameter", "power", "attitude", "diagnostic"], cancel_check), 1):
                    check_cancel(cancel_check)
                    timestamp = event.get("time_us")
                    time = (timestamp - origin) / 1_000_000 if timestamp is not None else None
                    kind = event["kind"]
                    if kind == "diagnostic":
                        if event.get("metric_barrier"):
                            break_next = {"power": True, "tracking": True}
                            append_bounded(warnings, "Corruption or clock boundary excludes adjacent integration pairs.")
                        continue
                    if kind == "parameter":
                        name, value = str(event.get("name", "")), str(event.get("value", ""))
                        try:
                            valid = NAME.fullmatch(name) and Decimal(value).is_finite() and math.isfinite(float(value))
                        except (InvalidOperation, ValueError, OverflowError):
                            valid = False
                        if not valid:
                            append_bounded(warnings, "Malformed/nonfinite parameter observation excluded.")
                            continue
                        if time is None:
                            unscoped_parameters += 1
                            continue
                        if name not in current_values and len(current_values) >= MAX_PARAMETERS:
                            raise ValueError("Observed parameter set exceeds 50,000 names.")
                        previous = current_values.get(name)
                        if time <= start:
                            parameter_values[name] = {"value": value, "observation_time_us": str(timestamp),
                                                      "record_index": event.get("record_index")}
                            snapshot_at = timestamp if snapshot_at is None else max(snapshot_at, timestamp)
                        elif time < end:
                            if previous is None or Decimal(previous) != Decimal(value):
                                parameter_change_count += 1
                                if len(parameter_changes) < 1000:
                                    parameter_changes.append({"name": name, "before": previous, "after": value,
                                                              "time_s": time, "time_us": str(timestamp),
                                                              "record_index": event.get("record_index"),
                                                              "kind": "newly_observed" if previous is None else "changed"})
                        current_values[name] = value
                        continue
                    if time is None:
                        continue
                    channel = "power" if kind == "power" else "tracking"
                    if channel == "power" and str(event.get("battery_id", "unknown")) != battery:
                        continue
                    if channel == "tracking" and "desired_roll_deg" not in event:
                        continue
                    if time < 0 or (last_time[channel] is not None and time <= last_time[channel]):
                        append_bounded(warnings, f"Duplicate/nonmonotonic {channel} timestamp excluded; review raw timing evidence.")
                        break_next[channel] = True
                        continue
                    last_time[channel] = time
                    rows[channel] += 1
                    for field in event.get("source_fields", []):
                        append_bounded(source_fields[channel], field, 20)
                    writers[channel].writerow([time, event.get("voltage_v") if channel == "power" else None,
                                               event.get("current_a") if channel == "power" else None,
                                               event.get("desired_roll_deg") if channel == "tracking" else None,
                                               event.get("actual_roll_deg") if channel == "tracking" else None,
                                               "1" if break_next[channel] else "0"])
                    break_next[channel] = False
                    if progress and count % 1024 == 0:
                        progress("scoping_metrics", count, None)
            # Empty channels are represented by blank boundaries, yielding explicit
            # unavailable metrics through the same tested integration core.
            for channel, path in (("power", power_path), ("tracking", tracking_path)):
                if rows[channel] < 2:
                    with path.open("w", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(["time_s", "voltage_v", "current_a", "desired_roll_deg", "actual_roll_deg", "quality_break_before"])
                        writer.writerow([start, "", "", "", "", "0"])
                        writer.writerow([end, "", "", "", "", "0"])
                    append_bounded(warnings, f"Insufficient {channel} samples in this source/boot; metric unavailable.")
            measured = performance_metrics(power_path, start, end, gap, break_before_column="quality_break_before", cancel_check=cancel_check)
            tracked = performance_metrics(tracking_path, start, end, gap, break_before_column="quality_break_before", cancel_check=cancel_check)
            measured["metrics"]["roll_tracking_rmse_deg"] = tracked["metrics"]["roll_tracking_rmse_deg"]
        check_cancel(cancel_check)
        for name, metric in measured["metrics"].items():
            channel = "tracking" if name == "roll_tracking_rmse_deg" else "power"
            metric.update({"source_fields": source_fields[channel], "source_sha256": log["source"]["sha256"],
                           "log_id": log["id"], "source_key": selected["source_key"], "boot_epoch": selected["boot_epoch"],
                           "time_domain": selected["time_domain"], "origin_time_us": selected["origin_time_us"],
                           "decoder_version": PARSER_VERSION, "primary_battery_monitor": battery if channel == "power" else None})
            if rows[channel] < 2:
                metric["sample_count"] = 0
                metric["source_row_count"] = rows[channel]
            if parameter_changes:
                metric["value"] = None
                metric["unavailable_reason"] = "Selected interval crosses parameter change/new-observation boundaries; select a separately scoped interval."
        snapshot = self.store.add_document("snapshot", {
            **metadata, "label": f"{label} — observed at interval start", "source_kind": "logged_interval",
            "observed_at": None, "observation_time_us": str(snapshot_at) if snapshot_at is not None else None,
            "parameters": parameter_values, "parameter_count": len(parameter_values), "source": log["source"],
            "synthetic": log["synthetic"], "immutable": True, "identity_verification": "operator_declared",
            "parser_version": PARSER_VERSION, "format": log["format"], "log_id": log["id"],
            "source_key": selected["source_key"], "boot_epoch": selected["boot_epoch"],
            "time_domain": selected["time_domain"], "completeness": "observed_only",
            "limitations": ["Only parameters observed at or before the selected start are included; completeness is unknown."]})
        changes_state = "present" if parameter_changes else "unknown"
        if rows["power"] < 2:
            measured["source_time_start_s"] = None
            measured["source_time_end_s"] = None
        warnings.extend(measured.get("warnings", []))
        if parameter_changes:
            append_bounded(warnings, "Parameter observations changed or first appeared inside the interval. Select sub-intervals at the reported boundaries before attributing performance.")
        if unscoped_parameters:
            append_bounded(warnings, "Untimed parameter observations were retained in the timeline but excluded from the interval-start snapshot.")
        session = self.store.add_document("session", {
            **metadata, **measured, "label": label, "phase": phase, "source": log["source"],
            "snapshot_id": snapshot["id"], "snapshot_source_sha256": log["source"]["sha256"],
            "snapshot_observed_at": None, "synthetic": log["synthetic"], "immutable": True,
            "interval_start_s": start, "interval_end_s": end, "max_gap_s": gap,
            "interval_start_us": str(round(start * 1_000_000)), "interval_end_us": str(round(end * 1_000_000)),
            "parameter_evidence": "logged_observations", "parameter_evidence_note": "Observed interval-start values; completeness and unobserved changes cannot be verified.",
            "parameter_changes": changes_state, "parameter_change_observations": parameter_changes,
            "parameter_change_count": parameter_change_count,
            "parameter_changes_truncated": parameter_change_count > len(parameter_changes),
            "parameter_split_points_s": sorted({item["time_s"] for item in parameter_changes}),
            "parameter_completeness": "observed_only", "conditions": conditions,
            "time_domain": selected["time_domain"], "origin_time_us": selected["origin_time_us"],
            "interval_basis": "operator_selected", "primary_battery_monitor": battery,
            "identity_verification": "operator_declared", "log_id": log["id"],
            "source_key": selected["source_key"], "boot_epoch": selected["boot_epoch"],
            "warnings": warnings, "limitations": LIMITATIONS,
        })
        return {"session": session}
