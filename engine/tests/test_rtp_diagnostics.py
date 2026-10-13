"""Synthetic RTP packets test framing, not a qualified camera/video decoder."""
import random
import socket
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

from q22_engine.network import NetworkService, _source
from q22_engine.rtp_diagnostics import MAX_PACKETS, MAX_STREAMS, RtpDiagnostics, hevc_framing, rtp_packet


def packet(sequence=1, *, payload=b'\x02\x01\x11', pt=96, ssrc=0x1234, timestamp=9000, marker=False, flags=0x80):
    return struct.pack('!BBHII', flags, pt | (128 if marker else 0), sequence, timestamp, ssrc) + payload


def observe(sequences, **options):
    diagnostics = RtpDiagnostics(**options)
    for index, sequence in enumerate(sequences):
        diagnostics.observe(packet(sequence), 12345, index * .02)
    return diagnostics.result()


def test_rtp_header_csrc_extension_padding_preserves_only_payload():
    csrc = struct.pack('!I', 555)
    extension = struct.pack('!HHI', 0xBEDE, 1, 0x01020304)
    data = packet(payload=csrc + extension + b'payload' + b'\0\0\0\4', flags=0xB1, marker=True)
    parsed = rtp_packet(data)
    assert parsed['payload'].tobytes() == b'payload'
    assert parsed['ssrc'] == 0x1234
    assert parsed['marker'] is True
    assert parsed['payload_type'] == 96


@pytest.mark.parametrize('data,error', [
    (b'', 'short_header'), (packet(flags=0x40), 'version'),
    (packet(payload=b'', flags=0x8F), 'csrc'),
    (packet(payload=b'', flags=0x90), 'extension_header'),
    (packet(payload=b'\x00\x00\xff\xff', flags=0x90), 'extension_data'),
    (packet(payload=b'abc\x00', flags=0xA0), 'padding'),
    (packet(payload=b'abc\xff', flags=0xA0), 'padding'),
])
def test_malformed_rtp_is_rejected_without_claiming_firewall_or_codec(data, error):
    with pytest.raises(ValueError, match=error):
        rtp_packet(data)


def test_header_only_rtp_does_not_claim_video_payload():
    diagnostics = RtpDiagnostics()
    diagnostics.observe(packet(payload=b''), 3333, 1.0)
    result = diagnostics.result()
    assert result['rtp_packets'] == 1
    assert result['streams'][0]['payload_bytes'] == 0
    assert result['video_decoded'] is False
    assert result['streams'][0]['h265'] is None


def test_rtp_wrap_duplicates_and_reorder_reconcile_missing_slots():
    result = observe([65534, 0, 65535, 0, 1])
    stream = result['streams'][0]
    assert stream['sequence'] == {'duplicates': 1, 'reordered': 1, 'missing_sequence_slots': 0,
                                  'discontinuities': 0, 'reconciliation_window': 4096}
    assert stream['cadence']['mean_arrival_gap_ms'] == 20
    assert stream['cadence']['observation_span_ms'] == 80
    assert stream['h265'] is None


def test_observed_gap_and_restart_are_distinct_not_rf_loss():
    assert observe([10, 13])['streams'][0]['sequence']['missing_sequence_slots'] == 2
    reset = observe([100, 101, 9000, 9001])['streams'][0]['sequence']
    assert reset['discontinuities'] == 1
    assert reset['missing_sequence_slots'] == 0


def test_sequence_counters_do_not_cross_sender_ports_or_ssrc_and_pt_change_is_not_gap():
    diagnostics = RtpDiagnostics(h265_payload_type=96)
    diagnostics.observe(packet(10, pt=96), 3333, 0)
    diagnostics.observe(packet(11, pt=97), 3333, .01)
    diagnostics.observe(packet(8000, pt=96), 3334, .02)
    diagnostics.observe(packet(16000, ssrc=55), 3333, .03)
    result = diagnostics.result()
    assert len(result['streams']) == 3
    assert result['streams'][0]['payload_types'] == [96, 97]
    assert result['streams'][0]['h265']['packet_types'] == {'single_nal': 1}
    assert all(stream['sequence']['missing_sequence_slots'] == 0 for stream in result['streams'])


def test_rtcp_header_shape_is_counted_separately_and_not_decoded():
    diagnostics = RtpDiagnostics()
    diagnostics.observe(struct.pack('!BBHI', 0x80, 201, 1, 123), 3333, 0)
    diagnostics.observe(b'\x80\xc9\xff\xff', 3333, .1)
    result = diagnostics.result()
    assert result['rtcp_packets'] == 1
    assert result['rtcp_header_only'] is True
    assert result['rtp_packets'] == 0
    assert result['invalid_packets'] == 1
    assert result['streams'] == []


def nal(kind=1, data=b'example'):
    return bytes([kind << 1, 1]) + data


def test_hevc_single_aggregation_fragments_and_unsupported_paci_are_separate():
    one, two = nal(), nal(32, b'parameter-set-fixture')
    aggregation = nal(48, struct.pack('!H', len(one)) + one + struct.pack('!H', len(two)) + two)
    assert hevc_framing(one) == 'single_nal'
    assert hevc_framing(nal(36, b'')) == 'single_nal'  # An EOS header need not contain slice payload.
    assert hevc_framing(aggregation) == 'aggregation_packet'
    assert hevc_framing(nal(49, b'\x81abc')) == 'fragment_start'
    assert hevc_framing(nal(49, b'\x01abc')) == 'fragment_middle'
    assert hevc_framing(nal(49, b'\x41abc')) == 'fragment_end'
    assert hevc_framing(nal(50)) == 'unsupported_packetization'


@pytest.mark.parametrize('payload', [b'', b'\x02', b'\x82\x01x', b'\x02\x00x',
    nal(48, b''), nal(48, b'\0\5abc'), nal(48, b'\0\1a'), nal(48, b'\0\3\x02\x01x'),
    nal(48, b'\0\3\x60\x01x\0\3\x02\x01x'), nal(49, b'\x81'), nal(49, b'\xc1x'), nal(49, b'\xb0x')])
def test_invalid_hevc_payload_framing_is_not_reported_as_decode(payload):
    with pytest.raises(ValueError):
        hevc_framing(payload)


def test_configured_hevc_packet_errors_are_reported_but_never_identify_codec():
    diagnostics = RtpDiagnostics(h265_payload_type=96)
    diagnostics.observe(packet(payload=nal()), 5000, 1)
    diagnostics.observe(packet(2, payload=b'bad'), 5000, 1.1)
    hevc = diagnostics.result()['streams'][0]['h265']
    assert hevc['packet_types'] == {'single_nal': 1, 'invalid_payload': 1}
    assert hevc['codec_identified'] is False
    assert hevc['fragment_reassembly'] is False
    assert hevc['video_decoded'] is False


def test_parser_state_has_explicit_stream_and_packet_bounds():
    diagnostics = RtpDiagnostics()
    for index in range(MAX_PACKETS + 1):
        diagnostics.observe(packet(index % 65536, ssrc=index), 3333, index * .01)
    result = diagnostics.result()
    assert len(result['streams']) == MAX_STREAMS
    assert result['rtp_packets'] == MAX_PACKETS
    assert result['streams_truncated'] is True
    assert result['budget_exhausted'] is True


def test_bounded_random_datagrams_cannot_escape_parser_validation():
    random_source = random.Random(20261013)
    diagnostics = RtpDiagnostics(h265_payload_type=96)
    for index in range(200):
        data = random_source.randbytes(random_source.randrange(0, 256))
        diagnostics.observe(data, 3333, index * .01)
    assert diagnostics.observed == 200


BASE = {'kind': 'udp', 'host': '10.0.0.2', 'port': 5601, 'expected_format': 'rtp'}
MAPPING = {'rtp_codec': 'h265', 'rtp_payload_type': 96, 'h265_donl': False}


def test_h265_mapping_requires_explicit_nonambiguous_payload_type_and_no_donl():
    assert _source({**BASE, **MAPPING})['rtp_payload_type'] == 96
    for delta in ({'rtp_payload_type': None}, {'rtp_payload_type': True}, {'rtp_payload_type': 200},
                  {'rtp_payload_type': 72}, {'h265_donl': None}, {'h265_donl': True},
                  {'rtp_codec': 'h264'}, {'expected_format': 'unknown'}, {'kind': 'skypulse'}):
        with pytest.raises(ValueError):
            _source({**BASE, **MAPPING, **delta})
    with pytest.raises(ValueError):
        _source({**BASE, 'rtp_payload_type': 96})


def test_udp_integration_ignores_other_senders_and_never_sends_or_reuses_socket():
    connection = MagicMock()
    connection.recvfrom.side_effect = [(packet(), ('10.0.0.2', 3333)),
                                      (packet(2), ('10.0.0.3', 3333)), socket.timeout()]
    with patch('q22_engine.network.socket.socket') as factory:
        factory.return_value.__enter__.return_value = connection
        result = NetworkService(system='Linux')._udp_probe(_source({**BASE, **MAPPING}), '10.0.0.5', .1)
    check = result['checks'][0]
    assert check['rtp']['rtp_packets'] == 1
    assert check['other_packets'] == 1
    assert check['video_decoded'] is False
    assert result['state'] == 'packets_received'
    connection.setsockopt.assert_not_called()
    connection.send.assert_not_called()
    connection.sendto.assert_not_called()


def test_real_loopback_rtp_probe_reports_transport_without_video_decode():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    stop = threading.Event()
    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sequence = 1
            while not stop.wait(.01):
                sender.sendto(packet(sequence), ('127.0.0.1', port))
                sequence += 1
    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        result = NetworkService()._udp_probe(_source({**BASE, **MAPPING, 'host': '127.0.0.1', 'port': port}), '127.0.0.1', .15)
        rtp = result['checks'][0]['rtp']
        assert rtp['rtp_packets'] > 0
        assert rtp['streams'][0]['h265']['packet_types']['single_nal'] > 0
        assert rtp['video_decoded'] is False
        assert rtp['streams'][0]['h265']['codec_identified'] is False
    finally:
        stop.set()
        thread.join(timeout=2)
