"""Durable recovery and Darwin repair tests; all OS mutations are injected."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from q22_engine.network import NetworkService
from q22_engine.network_journal import RepairJournal
from test_network import FakeHost, SOURCE, UDP, INTERFACES, qualify_fake


class MacHost(FakeHost):
    def run(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "netstat":
            lines = ["Destination Gateway Flags Netif Expire"]
            for route in self.routes:
                lines.append(f"{route['dst']} {route.get('gateway', 'link#2')} {route.get('flags', 'UCS')} {route['dev']}")
            return "\n".join(lines)
        if argv[:3] == ["route", "-n", "get"]:
            route = next((r for r in self.routes if r["dst"] == argv[3] + "/32"), None)
            return "interface: eth0\nflags: <UP,HOST,STATIC,PROTO2>\n" if route else "interface: eth0\nflags: <UP>\n"
        if argv[:3] == ["route", "-n", "add"]:
            self.routes.append({"dst": argv[4] + "/32", "dev": argv[6], "gateway": "link#2", "flags": "UHS2"})
            return ""
        if argv[:3] == ["route", "-n", "delete"]:
            self.routes = [r for r in self.routes if r["dst"] != argv[4] + "/32"]
            return ""
        if argv[0] == "ifconfig":
            interface = next(i for i in self.interfaces if i["name"] == argv[1])
            if argv[-1] == "alias":
                interface["ipv4"].append({"address": argv[3], "prefix": 32, "network": argv[3] + "/32"})
            else:
                interface["ipv4"] = [a for a in interface["ipv4"] if a["address"] != argv[3]]
            return ""
        raise AssertionError(argv)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.host = FakeHost()
        self.service = self.new_service()

    def new_service(self, *, mac=False):
        service = NetworkService(self.directory, system="Darwin" if mac else "Linux", runner=self.host.run,
                                 interface_provider=lambda: self.host.interfaces, elevated=lambda: True)
        if mac:
            # The Darwin routing source socket is tested separately. Never use
            # this Linux machine's routing to model a different operating system.
            service._route_to = lambda _: {"interface": "eth0", "local_address": "10.0.0.10"}
        return qualify_fake(service)

    def route_plan(self, service=None, **extra):
        return (service or self.service).plan({"kind": "host_route", "source": SOURCE, "interface": "eth0", **extra})

    def confirm(self, plan):
        return {"plan_id": plan["plan_id"], "confirm": True}

    def test_intent_precedes_mutation_and_restart_rolls_back_exact_route(self):
        plan = self.route_plan()
        original = self.host.run
        def observe(argv):
            if argv[:4] == ["ip", "-4", "route", "add"]:
                records, warnings = self.service.journal.records()
                self.assertFalse(warnings)
                self.assertEqual(records[0]["events"][-1]["state"], "apply_intent")
            return original(argv)
        self.service.run = observe
        self.assertEqual(self.service.apply(self.confirm(plan))["state"], "applied")
        restarted = self.new_service()
        repairs = restarted.recovery({})["repairs"]
        self.assertEqual(repairs[0]["ownership"], "owned")
        self.assertEqual(restarted.apply(self.confirm(plan))["state"], "recovered_receipt")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "rolled_back")
        self.assertEqual(self.new_service().recovery({})["repairs"], [])
        records, _ = self.service.journal.records()
        self.assertEqual([e["state"] for e in records[0]["events"]], ["apply_intent", "applied", "rollback_intent", "rolled_back"])

    def test_restart_changed_route_never_deleted(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        self.host.routes[-1]["gateway"] = "10.0.0.1"
        restarted = self.new_service()
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "changed")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "ownership_lost")
        self.assertFalse(any(call[:4] == ["ip", "-4", "route", "del"] for call in self.host.calls))

    def test_absent_repair_is_not_reported_active(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        self.host.routes.pop()
        receipt = self.new_service().recovery({})["repairs"][0]
        self.assertEqual(receipt["ownership"], "absent")
        self.assertFalse(receipt["may_have_applied"])

    def test_crash_after_mutation_preserves_uncertain_intent(self):
        plan = self.route_plan()
        original = self.host.run
        def crash(argv):
            result = original(argv)
            if argv[:4] == ["ip", "-4", "route", "add"]:
                raise KeyboardInterrupt("simulated engine death")
            return result
        self.service.run = crash
        with self.assertRaises(KeyboardInterrupt):
            self.service.apply(self.confirm(plan))
        restarted = self.new_service()
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "owned")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "rolled_back")

    def test_journal_write_failure_prevents_mutation(self):
        plan = self.route_plan()
        with patch.object(self.service.journal, "save", side_effect=OSError("Disk full")):
            result = self.service.apply(self.confirm(plan))
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["may_have_applied"])
        self.assertEqual(len(self.host.routes), 2)

    @unittest.skipIf(os.name == "nt", "POSIX flock contention fixture")
    def test_busy_writer_lock_prevents_mutation(self):
        plan = self.route_plan()
        with self.service.journal._writer():
            result = self.service.apply(self.confirm(plan))
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["may_have_applied"])
        self.assertEqual(len(self.host.routes), 2)

    def test_rollback_changed_target_is_not_claimed_removed(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        original = self.host.run
        def changed(argv):
            if argv[:4] == ["ip", "-4", "route", "del"]:
                self.host.routes[-1]["metric"] += 1
                return ""
            return original(argv)
        self.service.run = changed
        result = self.service.rollback(self.confirm(plan))
        self.assertEqual(result["state"], "verification_failed")
        self.assertTrue(result["may_have_applied"])
        records, _ = self.service.journal.records()
        self.assertEqual(records[0]["events"][-1]["state"], "rollback_uncertain")

    def test_recovered_receipt_needs_fresh_confirmation(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        restarted = self.new_service()
        restarted.recovery({})
        with self.assertRaises(ValueError):
            restarted.rollback({"plan_id": plan["plan_id"]})

    def test_injected_journal_commands_are_rejected_never_executed(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        path = self.directory / "network-repairs" / (plan["plan_id"] + ".json")
        record = json.loads(path.read_text())
        record["commands"] = [["sh", "-c", "touch /tmp/not-allowed"]]
        path.write_text(json.dumps(record))
        result = self.new_service().recovery({})
        self.assertEqual(result["repairs"], [])
        self.assertTrue(result["warnings"])
        self.assertFalse(any(call[0] == "sh" for call in self.host.calls))

    def test_malicious_descriptor_cannot_compile_default_or_shell(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        path = self.directory / "network-repairs" / (plan["plan_id"] + ".json")
        record = json.loads(path.read_text())
        for value in ("0.0.0.0/0", "10.0.0.2; reboot"):
            modified = copy.deepcopy(record)
            modified["descriptor"]["source"]["host"] = value
            path.write_text(json.dumps(modified))
            result = self.new_service().recovery({})
            self.assertEqual(result["repairs"], [])
            self.assertTrue(result["warnings"])

    def test_atomic_replacement_failure_preserves_original_record(self):
        plan = self.route_plan()
        self.service.apply(self.confirm(plan))
        internal = self.service.plans[plan["plan_id"]]
        path = self.directory / "network-repairs" / (plan["plan_id"] + ".json")
        original = path.read_bytes()
        with patch("q22_engine.network_journal.os.replace", side_effect=OSError("Simulated rename failure")):
            with self.assertRaises(OSError):
                self.service.journal.save(plan["plan_id"], internal["descriptor"], internal["baseline"], "rollback_intent")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".receipt-*")), [])

    def test_symlink_journal_path_rejected(self):
        target = self.directory / "other"
        target.mkdir()
        root = self.directory / "second"
        root.mkdir()
        try:
            (root / "network-repairs").symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("OS account cannot create symbolic links")
        with self.assertRaises(OSError):
            RepairJournal(root)

    def test_mac_route_plan_and_verified_restart_rollback(self):
        self.host = MacHost()
        service = self.new_service(mac=True)
        plan = self.route_plan(service)
        self.assertEqual(plan["commands"][0], ["route", "-n", "add", "-host", "10.0.0.2", "-interface", "eth0", "-static", "-proto2"])
        self.assertEqual(service.apply(self.confirm(plan))["state"], "applied")
        restarted = self.new_service(mac=True)
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "owned")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "rolled_back")
        self.assertEqual(self.host.routes[0]["dst"], "default")

    def test_mac_alias_only32_and_explicit_host_route_prefix(self):
        self.host = MacHost()
        self.host.interfaces[1]["ipv4"] = []
        service = self.new_service(mac=True)
        plan = service.plan({"kind": "address_alias", "source": SOURCE, "interface": "eth0", "local_address": "10.0.0.10", "prefix": 24})
        self.assertEqual(plan["commands"][0], ["ifconfig", "eth0", "inet", "10.0.0.10", "netmask", "255.255.255.255", "alias"])
        self.assertEqual(service.apply(self.confirm(plan))["state"], "applied")
        self.assertEqual(self.route_plan(service)["state"], "blocked")
        self.assertEqual(self.route_plan(service, prefix=24)["state"], "ready")
        restarted = self.new_service(mac=True)
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "owned")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "rolled_back")

    def test_mac_alias_changed_interface_blocks_rollback(self):
        self.host = MacHost()
        self.host.interfaces[1]["ipv4"] = []
        service = self.new_service(mac=True)
        plan = service.plan({"kind": "address_alias", "source": SOURCE, "interface": "eth0", "local_address": "10.0.0.10", "prefix": 24})
        service.apply(self.confirm(plan))
        self.host.interfaces[1]["ipv4"].append({"address": "10.0.0.11", "prefix": 32, "network": "10.0.0.11/32"})
        restarted = self.new_service(mac=True)
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "changed")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "ownership_lost")

    def test_mac_dependent_route_must_be_removed_before_alias(self):
        self.host = MacHost()
        self.host.interfaces[1]["ipv4"] = []
        service = self.new_service(mac=True)
        alias = service.plan({"kind": "address_alias", "source": SOURCE, "interface": "eth0", "local_address": "10.0.0.10", "prefix": 24})
        service.apply(self.confirm(alias))
        route = self.route_plan(service, prefix=24)
        service.apply(self.confirm(route))
        self.assertEqual(service.rollback(self.confirm(alias))["state"], "ownership_lost")
        self.assertEqual(service.rollback(self.confirm(route))["state"], "rolled_back")
        self.assertEqual(service.rollback(self.confirm(alias))["state"], "rolled_back")

    def test_windows_firewall_filters_validated_before_receipt_adoption(self):
        active = [None]
        candidate = [None]
        def runner(argv):
            script = argv[-1]
            if "Find-NetRoute" in script:
                return json.dumps({"InterfaceAlias": "eth0", "IPAddress": "10.0.0.10"})
            if "Get-NetRoute" in script:
                return json.dumps({"DestinationPrefix": "0.0.0.0/0", "InterfaceAlias": "wifi0", "InterfaceIndex": 1, "NextHop": "192.168.0.1", "RouteMetric": 100})
            if "New-NetFirewallRule" in script:
                active[0] = copy.deepcopy(candidate[0])
                return ""
            if "Get-NetFirewallRule" in script:
                return json.dumps(active[0]) if active[0] else ""
            raise AssertionError(script)
        service = NetworkService(self.directory, system="Windows", runner=runner, interface_provider=lambda: INTERFACES, elevated=lambda: True)
        qualify_fake(service)
        plan = service.plan({"kind": "firewall_udp", "source": UDP, "interface": "eth0", "local_address": "10.0.0.10"})
        candidate[0] = {"rule": {"Name": "Q22-" + plan["plan_id"], "Direction": 1, "Action": 2, "Enabled": 1, "Profile": 0},
                        "port": {"Protocol": "UDP", "LocalPort": "14551", "RemotePort": "Any"},
                        "address": {"LocalAddress": ["10.0.0.10"], "RemoteAddress": ["10.0.0.2"]}, "interface": {"InterfaceAlias": ["eth0"]}}
        self.assertTrue(service._matches_addition(service.plans[plan["plan_id"]], candidate[0]))
        candidate[0]["port"]["LocalPort"] = "Any"
        result = service.apply(self.confirm(plan))
        self.assertEqual(result["state"], "verification_failed")
        restarted = NetworkService(self.directory, system="Windows", runner=runner, interface_provider=lambda: INTERFACES, elevated=lambda: True)
        qualify_fake(restarted)
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "changed")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "ownership_lost")

    def test_mac_uncertain_mutation_without_post_state_requires_manual_review(self):
        self.host = MacHost()
        service = self.new_service(mac=True)
        plan = self.route_plan(service)
        original = self.host.run
        def crash(argv):
            result = original(argv)
            if argv[:3] == ["route", "-n", "add"]:
                raise KeyboardInterrupt()
            return result
        service.run = crash
        with self.assertRaises(KeyboardInterrupt):
            service.apply(self.confirm(plan))
        restarted = self.new_service(mac=True)
        self.assertEqual(restarted.recovery({})["repairs"][0]["ownership"], "changed")
        self.assertEqual(restarted.rollback(self.confirm(plan))["state"], "ownership_lost")


if __name__ == "__main__":
    unittest.main()
