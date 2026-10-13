"""Synthetic real BIN/LOG batches, checked against pinned wire/reference semantics."""
import json
from pathlib import Path
import struct

import pytest

from q22_engine.imu_batches import MAX_BATCH_SAMPLES
from q22_engine.log_decoder import iter_log_events
from q22_engine.logs import LogService
from q22_engine.insights import InsightService
from q22_engine.store import Store


HEADER = [
    "FMT, 129, 76, UNIT, QBZ, TimeUS,Id,Label", "FMT, 130, 20, MULT, QBd, TimeUS,Id,Mult",
    "FMT, 131, 44, FMTU, QBNN, TimeUS,FmtType,UnitIds,MultIds",
    "FMT, 132, 35, PARM, QNff, TimeUS,Name,Value,Default",
    "FMT, 140, 31, ISBH, QHBBHHQf, TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate",
    "FMT, 141, 207, ISBD, QHHaaa, TimeUS,N,seqno,x,y,z",
    "UNIT, 0, 115, s", "UNIT, 0, 122, Hz", "MULT, 0, 70, 0.000001", "MULT, 0, 48, 1",
    "FMTU, 0, 140, s-----sz, F-----F0", "FMTU, 0, 141, s--ooo, F--???",
]


def log_file(tmp_path, *, count=256, mul=1000, sequence=None, sensor=1, options=0, samples=None):
    samples = samples or [1000 if index % 8 < 4 else -1000 for index in range(count)]
    rows = list(HEADER) + [f"PARM, 0, INS_LOG_BAT_OPT, {options}, 0", f"ISBH, 2000000, 7, {sensor}, 0, {mul}, {count}, 1000000, 400"]
    for index in range(count // 32):
        if sequence is not None and index not in sequence:
            continue
        block = str(samples[index*32:(index+1)*32])
        rows.append(f"ISBD, {2001000 + index*1000}, 7, {index}, {block}, {str([0]*32)}, {str([0]*32)}")
    path = tmp_path / "batch.log"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_complete_text_arrays_reconstruct_sample_scale_timing_and_provenance(tmp_path):
    path = log_file(tmp_path)
    events = list(iter_log_events(path))
    chunks = [item for item in events if item["kind"] == "imu_batch"]
    assert [item["sample_offset"] for item in chunks] == [0, 128]
    assert chunks[0]["samples"]["x"][:8] == [1]*4 + [-1]*4
    assert chunks[0]["sample_start_us"] == 1_000_000
    assert chunks[0]["sample_rate_hz"] == 400
    assert chunks[0]["filter_stage"] == "pre" and chunks[0]["sensor_type"] == "gyro"
    assert all(item["time_us"] >= 2_000_000 for item in chunks)  # measurement clock never rewinds log clock
    assert len(chunks[0]["sample_block_references"]) == 4
    assert all(len(json.dumps(item)) < 128 * 1024 for item in chunks)
    assert not [item for item in events if item["kind"] == "diagnostic"]


def test_frozen_independent_filterreview_batch_oracle(tmp_path):
    # Executed the original load_from_batch function under Node 24.19.0 from
    # WebTools 5b7fe705f6516c0d60830449d14630135cd23ed2, FilterReview.js SHA256
    # ef5a9708a173726b81668b9c42d29f5b8792983be6400992b1c2a3d82feaa439.
    # Input: batch7, type1, instance0, mul1000,256samples,400Hz,SampleUS1e6,
    # each 32-vector repeats [1000]*4+[-1000]*4, y/z0, BAT_OPT0,num_gyro1.
    # A sentinel following batch was supplied because upstream's EOF path
    # discards its last batch. Numeric outputs only are retained here; no GPL
    # implementation is distributed or imported by this application.
    chunks = [item for item in iter_log_events(log_file(tmp_path)) if item["kind"] == "imu_batch"]
    x = [value for item in chunks for value in item["samples"]["x"]]
    assert len(x) == 256 and x[:16] == [1,1,1,1,-1,-1,-1,-1,1,1,1,1,-1,-1,-1,-1]
    assert sum(x) == 0
    assert chunks[0]["sample_start_us"] / 1e6 == 1
    assert chunks[0]["sample_rate_hz"] == 400 and chunks[0]["filter_stage"] == "pre"


def test_real_binary_batch_arrays_reconstruct_identically(tmp_path):
    path = tmp_path / "batch.bin"
    def fmt(kind, length, name, codes, columns):
        return b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", kind, length, name.encode(), codes.encode(), columns.encode())
    data = fmt(140,31,"ISBH","QHBBHHQf","TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate")
    data += fmt(141,207,"ISBD","QHHaaa","TimeUS,N,seqno,x,y,z")
    data += b"\xa3\x95\x8c" + struct.pack("<QHBBHHQf", 2_000_000, 7, 1, 0, 1000, 64, 1_000_000, 400)
    for index in range(2):
        data += b"\xa3\x95\x8d" + struct.pack("<QHH96h", 2_001_000+index*1000,7,index,*([1000]*32+[-2000]*32+[3000]*32))
    path.write_bytes(data)
    chunks = [item for item in iter_log_events(path) if item["kind"] == "imu_batch"]
    assert len(chunks) == 1 and chunks[0]["samples"] == {"x": [1]*64, "y": [-2]*64, "z": [3]*64}
    assert chunks[0]["filter_stage"] == "unknown"  # absent options, no guessed pre/post


@pytest.mark.parametrize("mode", ["missing", "duplicate", "truncated", "zero_multiplier", "oversized", "clock", "corruption", "overflow"])
def test_invalid_batch_never_emits_partial_numerical_samples(tmp_path, mode):
    path = log_file(tmp_path, sequence={0, 1, 3, 4, 5, 6, 7} if mode == "missing" else None)
    content = path.read_text()
    if mode == "duplicate": content = content.replace("7, 1, [", "7, 0, [")
    if mode == "truncated": content = "\n".join(content.splitlines()[:-1]) + "\n"
    if mode == "zero_multiplier": content = content.replace("0, 1000, 256,", "0, 0, 256,")
    if mode == "oversized": content = content.replace("1000, 256,", f"1000, {MAX_BATCH_SAMPLES+32},")
    if mode == "clock": content = content.replace("1000000, 400", "1900000, 400")
    if mode == "corruption": content = content.replace("ISBD, 2004000", "UNRECOGNIZED BYTES\nISBD, 2004000")
    if mode == "overflow": content = content.replace("[1000,", "[32768,", 1)
    path.write_text(content)
    decoded = list(iter_log_events(path))
    assert not any(item["kind"] == "imu_batch" for item in decoded)
    assert any(item.get("name") == "imu_batch_issue" and item["metric_barrier"] for item in decoded)


@pytest.mark.parametrize("options,stage", [(0,"pre"),(1,"pre"),(2,"post"),(3,"unknown"),(4,"unknown"),(6,"unknown")])
def test_filter_stage_never_guesses_offset_sensor_count(tmp_path, options, stage):
    chunks = [item for item in iter_log_events(log_file(tmp_path, options=options)) if item["kind"] == "imu_batch"]
    assert chunks[0]["filter_stage"] == stage


def test_options_observed_after_measurement_start_cannot_label_historical_filter_stage(tmp_path):
    path = log_file(tmp_path)
    path.write_text(path.read_text().replace("PARM, 0, INS_LOG_BAT_OPT", "PARM, 1500000, INS_LOG_BAT_OPT"))
    chunks = [row for row in iter_log_events(path) if row["kind"] == "imu_batch"]
    assert chunks and chunks[0]["filter_stage"] == "unknown"


def test_batch_import_to_real_scoped_psd_uses_measurement_not_delayed_log_clock(tmp_path):
    store = Store(tmp_path / "store")
    log = LogService(store).import_log({"path": str(log_file(tmp_path)), "label": "SYNTHETIC batch oracle", "synthetic": True})["log"]
    result = InsightService(store).report({"log_id": log["id"], "source_key": "dataflash", "boot_epoch": 0,
        "interval_start_s": 1, "interval_end_s": 1.8, "spectrum": {"source": "batch_imu", "sensor_instance": "0", "axis": "x",
        "sample_rate_hz": 400, "window_samples": 256, "anti_alias_cutoff_hz": 100,
        "anti_alias_evidence": "SYNTHETIC ideal anti-alias fixture; no hardware assertion"}})["report"]
    spectrum = result["spectrum"]
    assert spectrum["status"] == "conditional" and spectrum["source"] == "batch_imu"
    assert spectrum["measurement_interval_end_s"] == pytest.approx(1+255/400)
    assert spectrum["peaks"][0]["frequency_hz"] == pytest.approx(50)
    assert spectrum["filter_stage"] == "pre"
    assert len(spectrum["spectrogram"]["windows"]) == 1
    for path in tmp_path.rglob("*"):
        if path.is_file(): path.chmod(0o600)
