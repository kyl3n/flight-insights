"""All host mutations and remote devices are mocked; no hardware claims."""
import copy
import base64
import json
import socket
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from q22_engine.network import NetworkService, _executable, _mac_destination, _ps, _run, _source


INTERFACES = [
    {"name": "wifi0", "index": 1, "up": True, "ipv4": [{"address": "192.168.0.20", "prefix": 24, "network": "192.168.0.0/24"}]},
    {"name": "eth0", "index": 2, "up": True, "ipv4": [{"address": "10.0.0.10", "prefix": 24, "network": "10.0.0.0/24"}]},
]
SOURCE = {"id": "ground", "kind": "skypulse", "host": "10.0.0.2", "interface": "eth0"}
UDP = {"id": "telemetry", "kind": "udp", "host": "10.0.0.2", "port": 14551, "local_address": "10.0.0.10", "expected_format": "mavlink"}


def safe_observer():
    return {"flight_state": "disarmed", "heartbeat_age_s": 0.1, "identity_verified": True,
            "recording_active": False, "owned_udp": []}


class FakeQualifiedBackend:
    """Explicit qualification is test-only; never invokes the real OS runner."""
    def __init__(self, service):
        self.service = service
        self.operations = []

    def capabilities(self):
        return {"state": "supported_automatic", "qualified": True, "platform": self.service.system,
                "signed_helper": True, "manager_aware": True, "supervised_rollback": True}

    def execute(self, operation, plan_id, descriptor):
        assert self.service.run is not _run, "Test backend must not execute real host commands"
        self.operations.append((operation, plan_id, copy.deepcopy(descriptor)))
        # Emulate a typed native compiler, never trust plan/journal command arrays.
        plan = {"plan_id": plan_id, "descriptor": copy.deepcopy(descriptor)}
        self.service._compile_descriptor(plan)
        for command in plan["commands" if operation == "apply" else "rollback_commands"]:
            self.service.run(command)


def qualify_fake(service):
    service.repair_backend = FakeQualifiedBackend(service)
    service.safety_state = safe_observer
    return service


class FakeHost:
    def __init__(self):
        self.interfaces = copy.deepcopy(INTERFACES)
        self.routes = [{"dst": "default", "dev": "wifi0", "gateway": "192.168.0.1", "metric": 100}, {"dst": "10.0.0.0/24", "dev": "eth0", "prefsrc": "10.0.0.10", "protocol": "kernel"}]
        self.selected = "eth0"
        self.calls = []
        self.addresses = []
        self.rules = []
        self.zone_members = ["eth0"]

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[:6] == ["ip", "-j", "-4", "-N", "route", "show"]:
            return json.dumps(self.routes)
        if argv[:5] == ["ip", "-j", "-4", "route", "get"]:
            return json.dumps([{"dev": self.selected, "prefsrc": "10.0.0.10" if self.selected == "eth0" else "192.168.0.20"}])
        if argv[:4] == ["ip", "-4", "route", "add"]:
            self.routes.append({"dst": argv[4], "dev": argv[6], "prefsrc": argv[8], "protocol": argv[10], "metric": int(argv[12])})
            return ""
        if argv[:4] == ["ip", "-4", "route", "del"]:
            self.routes = [r for r in self.routes if not (r.get("dst") == argv[4] and r.get("metric") == int(argv[12]))]
            return ""
        if argv[:4] == ["ip", "-4", "address", "add"]:
            address, prefix = argv[4].split("/")
            self.addresses.append({"local": address, "prefixlen": int(prefix), "label": argv[8]})
            return ""
        if argv[:4] == ["ip", "-4", "address", "del"]:
            self.addresses = []
            return ""
        if argv[:5] == ["ip", "-j", "-4", "address", "show"]:
            return json.dumps([{"addr_info": self.addresses}])
        if argv[0] == "firewall-cmd":
            command = argv[-1]
            if command.startswith("--get-zone-of-interface="):
                return "device"
            if command == "--list-interfaces":
                return " ".join(self.zone_members)
            if command == "--list-sources":
                return ""
            if command == "--list-rich-rules":
                return "\n".join(self.rules)
            if command.startswith("--add-rich-rule="):
                self.rules.append(command.split("=", 1)[1])
                return "success"
            if command.startswith("--remove-rich-rule="):
                self.rules.remove(command.split("=", 1)[1])
                return "success"
        raise AssertionError("Unexpected host command: " + repr(argv))

    def service(self, **kwargs):
        return qualify_fake(NetworkService(system="Linux", runner=self.run, interface_provider=lambda: self.interfaces, elevated=lambda: True, **kwargs))


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.service = self.host.service()

    def plan_route(self):
        return self.service.dispatch("network.plan", {"kind": "host_route", "source": SOURCE, "interface": "eth0"})

    def test_dual_nic_uses_device_subnet_preserves_internet(self):
        result = self.service.dispatch("network.inspect", {"sources": [SOURCE]})
        self.assertEqual(result["targets"][0]["state"], "route_available")
        self.assertEqual(result["targets"][0]["selected_interface"], "eth0")
        self.assertEqual(result["default_routes"][0]["gateway"], "192.168.0.1")
        self.assertEqual(result["firewall"]["state"], "unknown")
        self.assertFalse(any("add" in call for call in self.host.calls))

    def test_wrong_route_identified_without_attributing_firewall(self):
        self.host.selected = "wifi0"
        result = self.service.inspect({"sources": [SOURCE]})
        self.assertEqual(result["targets"][0]["state"], "attention")
        self.assertTrue(any("directly connected" in issue for issue in result["targets"][0]["issues"]))
        self.assertEqual(result["firewall"]["state"], "unknown")

    def test_missing_local_subnet_blocks_route_only_fix(self):
        self.host.interfaces[1]["ipv4"] = []
        plan = self.plan_route()
        self.assertEqual(plan["state"], "blocked")
        self.assertEqual(plan["commands"], [])

    def test_partial_inspection_blocks_all_repairs(self):
        with patch.object(self.service, "_routes", side_effect=OSError("ip unavailable")):
            self.assertEqual(self.plan_route()["state"], "blocked")

    def test_no_endpoint_probe_on_inspect(self):
        with patch("q22_engine.network.socket.create_connection") as connect:
            self.service.inspect({"sources": [SOURCE]})
            connect.assert_not_called()

    def test_route_apply_and_exact_rollback(self):
        before = copy.deepcopy(self.host.routes)
        plan = self.plan_route()
        self.assertEqual(plan["state"], "ready")
        self.assertNotIn("default", plan["commands"][0])
        result = self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.host.routes[0], before[0])
        result = self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(self.host.routes, before)

    def test_rollback_refuses_replaced_route(self):
        plan = self.plan_route()
        self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.host.routes[-1]["metric"] += 1
        result = self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "ownership_lost")
        self.assertFalse(any(call[:4] == ["ip", "-4", "route", "del"] for call in self.host.calls))

    def test_rollback_refuses_route_with_changed_gateway(self):
        plan = self.plan_route()
        self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.host.routes[-1]["gateway"] = "10.0.0.1"
        self.assertEqual(self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})["state"], "ownership_lost")

    def test_route_numeric_table_and_protocol_normalization(self):
        self.host.routes[1].update(protocol=4, table=254)
        route = self.service.inspect({})["routes"][1]
        self.assertEqual(route["protocol"], "4")
        self.assertEqual(route["table"], "main")

    def test_existing_host_route_is_never_adopted(self):
        self.host.routes.append({"dst": "10.0.0.2", "dev": "eth0", "metric": 42})
        self.assertEqual(self.plan_route()["state"], "not_needed")

    def test_stale_plan_does_not_change_host(self):
        plan = self.plan_route()
        self.host.routes[0]["metric"] = 1
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "stale")
        self.assertEqual(len(self.host.routes), 2)

    def test_inspection_failure_after_review_is_stale(self):
        plan = self.plan_route()
        with patch.object(self.service, "_routes", side_effect=OSError("lost route access")):
            self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "stale")

    def test_missing_elevation_returns_actionable_status(self):
        plan = self.plan_route()
        self.service.elevated = lambda: False
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "permission_required")
        self.assertEqual(len(self.host.routes), 2)

    def test_confirmation_and_command_injection_rejected(self):
        plan = self.plan_route()
        for params in ({"plan_id": plan["plan_id"]}, {"plan_id": plan["plan_id"], "confirm": "true"}, {"plan_id": "made-up", "confirm": True}, {"plan_id": plan["plan_id"], "confirm": True, "commands": [["sh", "-c", "anything"]]}):
            with self.assertRaises(ValueError):
                self.service.apply(params)
        for host in ("10.0.0.2;echo bad", "$(id)", "10.0.0.0/24", "device.local", "255.255.255.255"):
            with self.assertRaises(ValueError):
                self.service.inspect({"sources": [{**SOURCE, "host": host}]})
        with self.assertRaises(ValueError):
            _source({**SOURCE, "port": True})

    def test_expired_plan_cannot_apply(self):
        plan = self.plan_route()
        self.service.plans[plan["plan_id"]]["created"] -= 601
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "expired")

    def test_command_timeout_keeps_possible_rollback(self):
        plan = self.plan_route()
        original = self.service.run
        def uncertain(argv):
            if argv[:4] == ["ip", "-4", "route", "add"]:
                original(argv)
                raise subprocess.TimeoutExpired(argv, 8)
            return original(argv)
        self.service.run = uncertain
        result = self.service.apply({"plan_id": plan["plan_id"], "confirm": True})
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["may_have_applied"])
        self.assertEqual(self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})["state"], "rolled_back")

    def test_public_plan_cannot_mutate_server_plan(self):
        plan = self.plan_route()
        plan["commands"][0][0] = "sh"
        self.assertEqual(self.service.plans[plan["plan_id"]]["commands"][0][0], "ip")

    def test_alias_owns_label_and_does_not_add_subnet_route(self):
        self.host.interfaces[1]["ipv4"] = []
        plan = self.service.plan({"kind": "address_alias", "source": SOURCE, "interface": "eth0", "local_address": "10.0.0.10", "prefix": 24})
        self.assertIn("noprefixroute", plan["commands"][0])
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "applied")
        self.host.addresses[0]["label"] = "eth0:other"
        self.assertEqual(self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})["state"], "ownership_lost")

    def test_alias_never_changes_internet_adapter(self):
        plan = self.service.plan({"kind": "address_alias", "source": SOURCE, "interface": "wifi0", "local_address": "10.0.0.50", "prefix": 24})
        self.assertEqual(plan["state"], "blocked")

    def test_alias_rollback_removes_exact_labelled_addition(self):
        self.host.interfaces[1]["ipv4"] = []
        plan = self.service.plan({"kind": "address_alias", "source": SOURCE, "interface": "eth0", "local_address": "10.0.0.10"})
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "applied")
        self.assertEqual(self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})["state"], "rolled_back")

    def test_firewalld_scope_and_owned_rollback(self):
        with patch("q22_engine.network.shutil.which", return_value="/usr/bin/firewall-cmd"):
            plan = self.service.plan({"kind": "firewall_udp", "source": UDP, "interface": "eth0", "local_address": "10.0.0.10"})
        self.assertEqual(plan["state"], "ready")
        rule = plan["commands"][0][-1]
        for scope in ('source address="10.0.0.2/32"', 'destination address="10.0.0.10/32"', 'port="14551"'):
            self.assertIn(scope, rule)
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "applied")
        self.assertEqual(self.service.rollback({"plan_id": plan["plan_id"], "confirm": True})["state"], "rolled_back")

    def test_firewalld_shared_zone_blocks_repair(self):
        self.host.zone_members.append("wifi0")
        with patch("q22_engine.network.shutil.which", return_value="/usr/bin/firewall-cmd"):
            plan = self.service.plan({"kind": "firewall_udp", "source": UDP, "interface": "eth0", "local_address": "10.0.0.10"})
        self.assertEqual(plan["state"], "blocked")

    def test_firewalld_query_denial_is_not_rule_absence(self):
        original = self.service.run
        def denied(argv):
            if argv[-1] == "--list-rich-rules":
                raise PermissionError("Not authorized to inspect the zone")
            return original(argv)
        self.service.run = denied
        with patch("q22_engine.network.shutil.which", return_value="/usr/bin/firewall-cmd"):
            plan = self.service.plan({"kind": "firewall_udp", "source": UDP, "interface": "eth0", "local_address": "10.0.0.10"})
        self.assertEqual(plan["state"], "blocked")

    def test_firewalld_changed_zone_membership_blocks_apply(self):
        with patch("q22_engine.network.shutil.which", return_value="/usr/bin/firewall-cmd"):
            plan = self.service.plan({"kind": "firewall_udp", "source": UDP, "interface": "eth0", "local_address": "10.0.0.10"})
        self.host.zone_members.append("wifi0")
        self.assertEqual(self.service.apply({"plan_id": plan["plan_id"], "confirm": True})["state"], "stale")
        self.assertEqual(self.host.rules, [])

    def test_udp_busy_port_is_not_stolen(self):
        connection = MagicMock()
        connection.bind.side_effect = OSError("Address already in use")
        with patch("q22_engine.network.socket.socket") as factory:
            factory.return_value.__enter__.return_value = connection
            result = self.service._udp_probe(_source(UDP), "10.0.0.10", 0.1)
        self.assertEqual(result["state"], "bind_failed")
        connection.setsockopt.assert_not_called()
        connection.sendto.assert_not_called()

    def test_udp_timeout_does_not_claim_firewall_block(self):
        connection = MagicMock()
        connection.recvfrom.side_effect = socket.timeout()
        with patch("q22_engine.network.socket.socket") as factory:
            factory.return_value.__enter__.return_value = connection
            result = self.service._udp_probe(_source(UDP), "10.0.0.10", 0.1)
        self.assertEqual(result["state"], "no_packets")
        self.assertIn("does not identify firewall", result["details"])

    def test_udp_generic_data_is_not_validated_telemetry(self):
        connection = MagicMock()
        connection.recvfrom.side_effect = [(b"random", ("10.0.0.2", 2345)), (b"other", ("10.0.0.3", 2345)), socket.timeout()]
        with patch("q22_engine.network.socket.socket") as factory:
            factory.return_value.__enter__.return_value = connection
            result = self.service._udp_probe(_source(UDP), "10.0.0.10", 0.1)
        self.assertEqual(result["state"], "packets_received")
        self.assertEqual(result["checks"][0]["other_packets"], 1)
        self.assertFalse(result["checks"][0]["video_decoded"])

    def test_heartbeat_crc_distinguishes_corruption(self):
        # Labeled synthetic common HEARTBEAT. Fixed checksum dd7d independently
        # checked with bit-at-a-time reflected polynomial 0x8408 + extra 50.
        packet = bytes.fromhex("fe09000101000000000002035104037ddd")
        self.assertTrue(self.service._heartbeat_valid(packet))
        self.assertFalse(self.service._heartbeat_valid(packet[:-1] + bytes([packet[-1] ^ 1])))

    def test_http_only_gets_documented_endpoints_and_does_not_validate_schema(self):
        connections = []
        for _ in range(2):
            connection = MagicMock()
            connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}', b""]
            wrapper = MagicMock()
            wrapper.__enter__.return_value = connection
            connections.append(wrapper)
        with patch("q22_engine.network.socket.create_connection", side_effect=connections):
            result = self.service._http_probe(_source(SOURCE), 0.1)
        self.assertEqual(result["state"], "reachable")
        self.assertFalse(result["checks"][0]["schema_validated"])
        requests = [c.__enter__.return_value.sendall.call_args.args[0] for c in connections]
        self.assertTrue(requests[0].startswith(b"GET /scatter_data.json "))
        self.assertTrue(requests[1].startswith(b"GET /events.json "))

    def test_http_header_deadline_is_not_reset_by_drip(self):
        connection = MagicMock()
        with patch("q22_engine.network.socket.create_connection") as connect, patch("q22_engine.network.time.monotonic", side_effect=[0, 0, 0.2, 1, 1, 1.2]):
            connect.return_value.__enter__.return_value = connection
            result = self.service._http_probe(_source(SOURCE), 0.1)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(all("deadline" in check["error"] for check in result["checks"]))
        connection.recv.assert_not_called()

    def test_http_truncated_content_length_is_not_accessible_json(self):
        connection = MagicMock()
        connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{}', b"", b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{}', b""]
        with patch("q22_engine.network.socket.create_connection") as connect:
            connect.return_value.__enter__.return_value = connection
            result = self.service._http_probe(_source(SOURCE), 0.1)
        self.assertEqual(result["state"], "failed")

    def test_windows_guard_property_order_does_not_lose_ownership(self):
        values = iter(['{"rule":{"Action":2,"Name":"Q22-x"},"port":{"LocalPort":14551}}', '{"port":{"LocalPort":14551},"rule":{"Name":"Q22-x","Action":2}}'])
        service = NetworkService(system="Windows", runner=lambda _: next(values))
        self.assertEqual(service._guard_snapshot({"guard": []}), service._guard_snapshot({"guard": []}))

    def test_rtsp_advertisement_not_playback(self):
        body = b"v=0\r\no=- 1 1 IN IP4 10.0.0.2\r\ns=Camera\r\nt=0 0\r\nm=video 5004 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
        reply = b"RTSP/1.0 200 OK\r\nContent-Type: application/sdp\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        connection = MagicMock()
        connection.recv.side_effect = [reply]
        with patch("q22_engine.network.socket.create_connection") as connect:
            connect.return_value.__enter__.return_value = connection
            result = self.service._rtsp_probe(_source({**SOURCE, "kind": "rtsp", "path": "/video"}), 0.1)
        self.assertEqual(result["state"], "capabilities_available")
        self.assertEqual(result["checks"][0]["advertised_codecs"], ["H264/90000"])
        self.assertFalse(result["checks"][0]["media_received"])
        self.assertNotIn(b"PLAY", connection.sendall.call_args.args[0])

    def test_rtsp_incomplete_sdp_not_capabilities(self):
        connection = MagicMock()
        connection.recv.side_effect = [b"RTSP/1.0 200 OK\r\nContent-Type: application/sdp\r\nContent-Length: 5\r\n\r\nv=0\r\n"]
        with patch("q22_engine.network.socket.create_connection") as connect:
            connect.return_value.__enter__.return_value = connection
            result = self.service._rtsp_probe(_source({**SOURCE, "kind": "rtsp"}), 0.1)
        self.assertEqual(result["state"], "unexpected_response")

    def test_windows_inspection_and_scoped_route_plan(self):
        def windows_runner(argv):
            script = argv[-1]
            if "Find-NetRoute" in script:
                return json.dumps({"InterfaceAlias": "eth0", "IPAddress": "10.0.0.10"})
            if "Get-NetRoute" in script:
                return json.dumps({"DestinationPrefix": "0.0.0.0/0", "InterfaceAlias": "wifi0", "InterfaceIndex": 1, "NextHop": "192.168.0.1", "RouteMetric": 100})
            raise AssertionError(script)
        service = NetworkService(system="Windows", runner=windows_runner, interface_provider=lambda: INTERFACES)
        result = service.inspect({"sources": [SOURCE]})
        self.assertEqual(result["targets"][0]["state"], "route_available")
        plan = service.plan({"kind": "host_route", "source": SOURCE, "interface": "eth0"})
        self.assertIn("-InterfaceIndex 2", plan["commands"][0][-1])
        self.assertIn("'10.0.0.2/32'", plan["commands"][0][-1])
        self.assertNotIn("192.168.0.1", plan["commands"][0][-1])

    def test_macos_inspection_admits_automatic_repair_limit(self):
        def mac_runner(argv):
            if argv[0] == "netstat":
                return "Destination Gateway Flags Netif Expire\ndefault 192.168.0.1 UGScg wifi0\n10.0.0/24 link#2 UCS eth0\n"
            if argv[0] == "route":
                return "route to: 10.0.0.2\ninterface: eth0\ngateway: 10.0.0.2\n"
            raise AssertionError(argv)
        service = NetworkService(system="Darwin", runner=mac_runner, interface_provider=lambda: INTERFACES)
        with patch("q22_engine.network.socket.socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.getsockname.return_value = ("10.0.0.10", 49123)
            result = service.inspect({"sources": [SOURCE]})
            self.assertEqual(result["targets"][0]["local_address"], "10.0.0.10")
            self.assertEqual(result["targets"][0]["local_address_state"], "os_selected")
            self.assertEqual(result["routes"][1]["destination"], "10.0.0.0/24")
            self.assertEqual(service.plan({"kind": "host_route", "source": SOURCE, "interface": "eth0"})["state"], "ready")
            connection.send.assert_not_called()
            connection.sendto.assert_not_called()
            connection.bind.assert_not_called()

    def test_macos_kernel_selects_second_alias_without_guessing(self):
        interfaces = copy.deepcopy(INTERFACES)
        interfaces[1]["ipv4"].append({"address": "10.0.0.20", "prefix": 24, "network": "10.0.0.0/24"})
        def runner(argv):
            if argv[0] == "netstat":
                return "Destination Gateway Flags Netif Expire\ndefault 192.168.0.1 UGScg wifi0\n10.0.0/24 link#2 UCS eth0\n"
            return "route to: 10.0.0.2\ninterface: eth0\ngateway: 10.0.0.2\n"
        service = NetworkService(system="Darwin", runner=runner, interface_provider=lambda: interfaces)
        with patch("q22_engine.network.socket.socket") as factory:
            factory.return_value.__enter__.return_value.getsockname.return_value = ("10.0.0.20", 49123)
            target = service.inspect({"sources": [SOURCE]})["targets"][0]
        self.assertEqual(target["local_address"], "10.0.0.20")
        self.assertEqual(target["state"], "route_available")

    def test_macos_unknown_kernel_source_does_not_guess_an_alias(self):
        def runner(argv):
            if argv[0] == "netstat":
                return "Destination Gateway Flags Netif Expire\ndefault 192.168.0.1 UGScg wifi0\n10.0.0/24 link#2 UCS eth0\n"
            return "route to: 10.0.0.2\ninterface: eth0\ngateway: 10.0.0.2\n"
        service = NetworkService(system="Darwin", runner=runner, interface_provider=lambda: INTERFACES)
        with patch("q22_engine.network.socket.socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError("Source selection unavailable")
            target = service.inspect({"sources": [SOURCE]})["targets"][0]
        self.assertIsNone(target["local_address"])
        self.assertEqual(target["local_address_state"], "unknown")
        self.assertEqual(target["state"], "attention")

    def test_macos_routed_subnet_uses_os_source_not_device_subnet_guess(self):
        service = NetworkService(system="Darwin", runner=lambda _: "route to: 10.20.0.2\ninterface: wifi0\ngateway: 192.168.0.1\n")
        with patch("q22_engine.network.socket.socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.getsockname.return_value = ("192.168.0.20", 49123)
            route = service._route_to("10.20.0.2")
            connection.connect.assert_called_once_with(("10.20.0.2", 9))
            connection.send.assert_not_called()
            connection.sendto.assert_not_called()
        self.assertEqual(route["local_address"], "192.168.0.20")

    def test_macos_route_columns_and_abbreviated_prefixes(self):
        reply = "Destination Gateway Flags Refs Use Netif Expire\ndefault 192.168.0.1 UGSc 4 1 en0\n10/8 link#7 UCS 0 0 en7\n10.0.0.2 00:00:00:00:00:00 UHLWI 0 0 en7 1200\n"
        service = NetworkService(system="Darwin", runner=lambda _: reply)
        routes = service._routes()
        self.assertEqual([r["interface"] for r in routes], ["en0", "en7", "en7"])
        self.assertEqual([r["destination"] for r in routes], ["0.0.0.0/0", "10.0.0.0/8", "10.0.0.2/32"])
        self.assertEqual(_mac_destination("169.254"), "169.254.0.0/16")
        self.assertEqual(_mac_destination("192.168.0/24"), "192.168.0.0/24")

    def test_windows_unicode_interface_and_bom_json_round_trip(self):
        alias = "Ethernet – 通信"
        addresses = {alias: [SimpleNamespace(family=socket.AF_INET, address="10.0.0.10", netmask="255.255.255.0")]}
        reply = "\ufeff" + json.dumps({"InterfaceAlias": alias, "InterfaceIndex": 12, "Dhcp": 1}, ensure_ascii=False)
        service = NetworkService(system="Windows", runner=lambda _: reply)
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value={alias: SimpleNamespace(isup=True)}), patch("q22_engine.network.socket.if_nametoindex", side_effect=OSError("friendly name")):
            interfaces = service._interfaces()
        self.assertEqual(interfaces[0]["name"], alias)
        self.assertEqual(interfaces[0]["index"], 12)
        self.assertEqual(interfaces[0]["ipv4"][0]["network"], "10.0.0.0/24")

    def test_windows_localized_firewall_alias_is_quoted_as_literal(self):
        alias = "通信’; $(Write-Host $x); ‘'"
        interfaces = copy.deepcopy(INTERFACES)
        interfaces[1]["name"] = alias
        def runner(argv):
            if "Find-NetRoute" in argv[-1]:
                return json.dumps({"InterfaceAlias": alias, "IPAddress": "10.0.0.10"})
            return json.dumps({"DestinationPrefix": "0.0.0.0/0", "InterfaceAlias": "wifi0", "InterfaceIndex": 1, "NextHop": "192.168.0.1", "RouteMetric": 100})
        service = NetworkService(system="Windows", runner=runner, interface_provider=lambda: interfaces)
        plan = service.plan({"kind": "firewall_udp", "source": UDP, "interface": alias, "local_address": "10.0.0.10"})
        self.assertEqual(plan["state"], "ready")
        encoded = base64.b64encode(alias.encode("utf-8")).decode("ascii")
        self.assertIn(f"-InterfaceAlias ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))) -Profile", plan["commands"][0][-1])
        self.assertNotIn("Write-Host", plan["commands"][0][-1])
        self.assertNotIn("通信", plan["commands"][0][-1])

    def test_windows_failed_supplemental_query_keeps_addresses_but_blocks_repairs(self):
        def runner(argv):
            if "Get-NetIPInterface" in argv[-1]:
                raise OSError("NetTCPIP query unavailable")
            return json.dumps({"DestinationPrefix": "0.0.0.0/0", "InterfaceAlias": "eth0", "InterfaceIndex": 12, "NextHop": "10.0.0.1", "RouteMetric": 100})
        service = NetworkService(system="Windows", runner=runner)
        addresses = {"eth0": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.10", netmask="255.255.255.0")]}
        with patch("psutil.net_if_addrs", return_value=addresses), patch("psutil.net_if_stats", return_value={"eth0": SimpleNamespace(isup=True)}):
            result = service.inspect({})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["interfaces"][0]["ipv4"][0]["address"], "10.0.0.10")
        self.assertTrue(any("NetTCPIP" in item for item in result["limitations"]))

    def test_windows_find_netroute_separate_address_and_route_json(self):
        reply = "\ufeff" + json.dumps([{"InterfaceAlias": "eth0", "IPAddress": "10.0.0.10", "NextHop": None}, {"InterfaceAlias": "eth0", "IPAddress": None, "NextHop": "0.0.0.0"}])
        service = NetworkService(system="Windows", runner=lambda _: reply)
        self.assertEqual(service._route_to("10.0.0.2"), {"interface": "eth0", "local_address": "10.0.0.10", "gateway": "0.0.0.0"})

    def test_windows_powershell_fixed_encoding_and_startup_budget(self):
        command = _ps("Get-NetRoute | ConvertTo-Json -Compress")
        self.assertIn("[Console]::OutputEncoding=$utf8", command[-1])
        self.assertIn("$OutputEncoding=$utf8", command[-1])
        with patch("q22_engine.network._executable", return_value="powershell.exe"), patch("q22_engine.network.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"InterfaceAlias":"通信"}', stderr="")) as run:
            self.assertIn("通信", _run(command))
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["timeout"], 6)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_sparse_desktop_path_uses_known_os_tool_locations(self):
        with patch("q22_engine.network.shutil.which", return_value=None), patch("q22_engine.network.os.path.isfile", side_effect=lambda p: p in {"/usr/sbin/netstat", "/sbin/route", "/usr/sbin/ip"}), patch("q22_engine.network.os.access", return_value=True):
            self.assertEqual(_executable("netstat"), "/usr/sbin/netstat")
            self.assertEqual(_executable("route"), "/sbin/route")
            self.assertEqual(_executable("ip"), "/usr/sbin/ip")
            self.assertIsNone(_executable("invented-network-tool"))


if __name__ == "__main__":
    unittest.main()
