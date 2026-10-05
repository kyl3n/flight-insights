"""Real loopback transports, synthetic protocol payloads; no device claims."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from q22_engine.network import NetworkService


def test_http_endpoints_are_received_over_real_tcp():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            payload = json.dumps({"synthetic": True, "samples": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = NetworkService()._http_probe({"host": "127.0.0.1", "port": server.server_port}, 1)
        assert result['state'] == 'reachable'
        assert received == ['/scatter_data.json', '/events.json']
        assert all(check['schema_validated'] is False for check in result['checks'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_udp_receipt_is_not_reported_as_decoded_video():
    # The receiving port is selected dynamically; no existing GCS port is shared.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    stop = threading.Event()

    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            while not stop.wait(0.01):
                sender.sendto(b'SYNTHETIC test payload; not video', ('127.0.0.1', port))

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        result = NetworkService()._udp_probe({'host': '127.0.0.1', 'port': port, 'expected_format': 'rtp'}, '127.0.0.1', .2)
        assert result['state'] == 'packets_received'
        assert result['checks'][0]['packets_from_configured_host'] > 0
        assert result['checks'][0]['video_decoded'] is False
        assert result['checks'][0]['authentication_verified'] is False
    finally:
        stop.set()
        thread.join(timeout=2)


def test_udp_keeps_existing_receiver_exclusive():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as controlling_receiver:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            controlling_receiver.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        controlling_receiver.bind(('127.0.0.1', 0))
        port = controlling_receiver.getsockname()[1]
        result = NetworkService()._udp_probe({'host': '127.0.0.1', 'port': port}, '127.0.0.1', .1)
        assert result['state'] == 'bind_failed'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b'existing receiver stays usable', ('127.0.0.1', port))
        controlling_receiver.settimeout(1)
        assert controlling_receiver.recv(256) == b'existing receiver stays usable'


def test_rtsp_negotiation_uses_real_tcp_and_does_not_start_playback():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        requests = []

        def camera():
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                requests.append(connection.recv(8192))
                sdp = b'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Synthetic fixture\r\nt=0 0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n'
                reply = b'RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\nContent-Length: ' + str(len(sdp)).encode() + b'\r\n\r\n' + sdp
                connection.sendall(reply)

        thread = threading.Thread(target=camera, daemon=True)
        thread.start()
        result = NetworkService()._rtsp_probe({'host': '127.0.0.1', 'port': listener.getsockname()[1], 'path': '/synthetic'}, 1)
        thread.join(timeout=2)
        assert requests[0].startswith(b'DESCRIBE rtsp://127.0.0.1:')
        assert b'PLAY ' not in requests[0]
        assert result['checks'][0]['status'] == 200
        assert result['checks'][0]['sdp_structure_validated'] is True
        assert result['checks'][0]['decoded'] is False
        assert result['checks'][0]['media_received'] is False
