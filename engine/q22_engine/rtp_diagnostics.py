"""Bounded passive RTP evidence, not a decoder or proof of RF/video health.

RTP headers follow RFC 3550; optional explicitly configured HEVC packetization
follows RFC 7798 with DONL disabled. No payload codec is inferred from a UDP port.
"""
from __future__ import annotations

from collections import Counter

MAX_PACKETS = 4096
MAX_STREAMS = 16
SEQUENCE_WINDOW = 4096
MAX_SEQUENCE_JUMP = 3000


def rtp_packet(data: bytes) -> dict:
    if len(data) < 12 or data[0] >> 6 != 2:
        raise ValueError("invalid_version_or_short_header")
    header_size = 12 + 4 * (data[0] & 15)
    if header_size > len(data):
        raise ValueError("truncated_csrc_list")
    if data[0] & 16:
        if header_size + 4 > len(data):
            raise ValueError("truncated_extension_header")
        words = int.from_bytes(data[header_size + 2:header_size + 4], "big")
        header_size += 4 + words * 4
        if header_size > len(data):
            raise ValueError("truncated_extension_data")
    end = len(data)
    if data[0] & 32:
        padding = data[-1]
        if padding == 0 or padding > end - header_size:
            raise ValueError("invalid_padding")
        end -= padding
    return {"sequence": int.from_bytes(data[2:4], "big"), "timestamp": int.from_bytes(data[4:8], "big"),
            "ssrc": int.from_bytes(data[8:12], "big"), "payload_type": data[1] & 127,
            "marker": bool(data[1] & 128), "payload": memoryview(data)[header_size:end]}


def rtcp_headers(data: bytes) -> bool:
    """Recognize bounded RTCP block headers only; do not interpret control reports."""
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data) or data[offset] >> 6 != 2 or not 192 <= data[offset + 1] <= 223:
            return False
        end = offset + (int.from_bytes(data[offset + 2:offset + 4], "big") + 1) * 4
        if end > len(data):
            return False
        if data[offset] & 32:
            if end != len(data) or data[end - 1] == 0 or data[end - 1] > end - offset - 4:
                return False
        offset = end
    return bool(data)


def _nal_type(payload) -> int:
    if len(payload) < 2 or payload[0] & 128 or payload[1] & 7 == 0:
        raise ValueError("invalid_hevc_nal_header")
    return (payload[0] >> 1) & 63


def hevc_framing(payload) -> str:
    """Inspect packet framing under an explicit no-DONL HEVC configuration.

    This does not parse slice data, parameter sets, FU continuity or frame decode.
    A structurally plausible payload can still be a different codec or noise.
    """
    nal_type = _nal_type(payload)
    if nal_type < 48:
        return "single_nal"
    if nal_type == 48:
        offset, count = 2, 0
        while offset < len(payload):
            if offset + 2 > len(payload):
                raise ValueError("truncated_aggregation_length")
            length = int.from_bytes(payload[offset:offset + 2], "big")
            offset += 2
            if length < 2 or offset + length > len(payload):
                raise ValueError("invalid_aggregation_length")
            if _nal_type(payload[offset:offset + length]) >= 48:
                raise ValueError("nested_packetization_header")
            offset += length
            count += 1
        if count < 2:
            raise ValueError("aggregation_requires_multiple_nals")
        return "aggregation_packet"
    if nal_type == 49:
        if len(payload) < 4:
            raise ValueError("empty_fragmentation_payload")
        header = payload[2]
        if header & 192 == 192 or header & 63 >= 48:
            raise ValueError("invalid_fragmentation_header")
        return "fragment_start" if header & 128 else "fragment_end" if header & 64 else "fragment_middle"
    # PACI (50) and reserved packetization modes need additional negotiated data.
    return "unsupported_packetization"


class _Stream:
    def __init__(self, packet, sender_port, arrival):
        self.ssrc, self.sender_port = packet["ssrc"], sender_port
        self.highest = self.first_sequence = packet["sequence"]
        self.seen, self.missing = set(), set()
        self.finalized_missing = self.duplicates = self.reordered = self.discontinuities = 0
        self.packets = self.markers = self.bytes = self.payload_bytes = 0
        self.payload_types, self.hevc, self.hevc_errors = Counter(), Counter(), Counter()
        self.first_arrival = self.last_arrival = arrival
        self.gap_count = self.gap_sum = 0
        self.min_gap = self.max_gap = None

    def add(self, packet, arrival, size, codec_payload_type):
        sequence = packet["sequence"]
        extended = self.highest + ((sequence - self.highest + 32768) % 65536 - 32768)
        delta = extended - self.highest
        if self.packets and abs(delta) > MAX_SEQUENCE_JUMP:
            self.finalized_missing += len(self.missing)
            self.missing.clear()
            self.seen.clear()
            self.discontinuities += 1
            self.highest = extended
        if extended in self.seen:
            self.duplicates += 1
        elif self.packets and delta < 0 and abs(delta) <= MAX_SEQUENCE_JUMP:
            self.reordered += 1
            self.missing.discard(extended)
        elif delta > 0 and delta <= MAX_SEQUENCE_JUMP:
            self.missing.update(range(self.highest + 1, extended))
            self.highest = extended
        self.seen.add(extended)
        threshold = self.highest - SEQUENCE_WINDOW
        if len(self.seen) > SEQUENCE_WINDOW:
            self.seen = {value for value in self.seen if value >= threshold}
        if self.missing and min(self.missing) < threshold:
            retained = {value for value in self.missing if value >= threshold}
            self.finalized_missing += len(self.missing) - len(retained)
            self.missing = retained
        if self.packets:
            gap = max(0.0, arrival - self.last_arrival)
            self.gap_count += 1
            self.gap_sum += gap
            self.min_gap = gap if self.min_gap is None else min(self.min_gap, gap)
            self.max_gap = gap if self.max_gap is None else max(self.max_gap, gap)
        self.last_arrival = arrival
        self.packets += 1
        self.bytes += size
        self.payload_bytes += len(packet["payload"])
        self.markers += packet["marker"]
        self.payload_types[packet["payload_type"]] += 1
        if codec_payload_type is not None and packet["payload_type"] == codec_payload_type:
            try:
                self.hevc[hevc_framing(packet["payload"])] += 1
            except ValueError as error:
                self.hevc["invalid_payload"] += 1
                self.hevc_errors[str(error)] += 1

    def result(self, codec_payload_type):
        return {"ssrc_hex": f"{self.ssrc:08x}", "sender_port": self.sender_port,
                "packets": self.packets, "datagram_bytes": self.bytes, "payload_bytes": self.payload_bytes, "marker_packets": self.markers,
                "payload_types": sorted(self.payload_types), "per_payload_type": dict(self.payload_types),
                "sequence": {"duplicates": self.duplicates, "reordered": self.reordered,
                             "missing_sequence_slots": self.finalized_missing + len(self.missing),
                             "discontinuities": self.discontinuities, "reconciliation_window": SEQUENCE_WINDOW},
                "cadence": {"observation_span_ms": round((self.last_arrival - self.first_arrival) * 1000, 3),
                            "mean_arrival_gap_ms": round(self.gap_sum / self.gap_count * 1000, 3) if self.gap_count else None,
                            "min_arrival_gap_ms": round(self.min_gap * 1000, 3) if self.min_gap is not None else None,
                            "max_arrival_gap_ms": round(self.max_gap * 1000, 3) if self.max_gap is not None else None},
                "h265": {"state": "configured_payload_framing", "configured_payload_type": codec_payload_type,
                         "donl_present": False, "packet_types": dict(self.hevc), "errors": dict(self.hevc_errors),
                         "codec_identified": False, "fragment_reassembly": False, "video_decoded": False}
                        if codec_payload_type is not None else None}


class RtpDiagnostics:
    def __init__(self, *, h265_payload_type=None):
        self.h265_payload_type = h265_payload_type
        self.rtp_packets = self.rtcp_packets = self.invalid_packets = self.observed = 0
        self.streams = {}
        self.streams_truncated = self.budget_exhausted = False
        self.errors = Counter()

    def observe(self, data: bytes, sender_port: int, arrival: float):
        if self.observed >= MAX_PACKETS:
            self.budget_exhausted = True
            return
        self.observed += 1
        # RTP/RTCP multiplexing reserves these payload octets for RTCP; ambiguous
        # RTP PT64..95 cannot be identified without the session's transport map.
        if len(data) >= 2 and 192 <= data[1] <= 223:
            if rtcp_headers(data):
                self.rtcp_packets += 1
            else:
                self.invalid_packets += 1
                self.errors["ambiguous_or_invalid_rtcp_header"] += 1
            return
        try:
            packet = rtp_packet(data)
        except ValueError as error:
            self.invalid_packets += 1
            self.errors[str(error)] += 1
            return
        self.rtp_packets += 1
        key = (sender_port, packet["ssrc"])
        if key not in self.streams:
            if len(self.streams) >= MAX_STREAMS:
                self.streams_truncated = True
                return
            self.streams[key] = _Stream(packet, sender_port, arrival)
        self.streams[key].add(packet, arrival, len(data), self.h265_payload_type)

    def result(self):
        return {"state": "rtp_headers_observed" if self.rtp_packets else "no_rtp_headers_observed",
                "rtp_packets": self.rtp_packets, "rtcp_packets": self.rtcp_packets, "rtcp_header_only": True, "invalid_packets": self.invalid_packets,
                "errors": dict(self.errors), "streams_truncated": self.streams_truncated,
                "budget_exhausted": self.budget_exhausted or self.observed >= MAX_PACKETS,
                "streams": [stream.result(self.h265_payload_type) for stream in self.streams.values()],
                "video_decoded": False,
                "limitations": ["RTP header/framing consistency is not codec identification, sender authentication or decoded video.",
                                "Sequence gaps are observed missing slots, not proven radio loss; duplicates, reordering, sender resets and packets outside the probe window remain distinct uncertainties.",
                                "Arrival gaps measure this short local observation, not frame rate, end-to-end latency or decoder performance.",
                                "RTCP recognition checks block headers only; RTP payload types 64..95 can be ambiguous without a session transport map."]}
