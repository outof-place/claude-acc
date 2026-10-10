"""Tryb hotspot bez roota i bez sieci: sterownik dostaje sztuczne RTT i liczniki bajtów.

`set_tbr` i `if_bytes` są podmienione, więc żaden test nie dotyka ifconfig ani en8.
Wykrywanie hotspotu czyta wyjście `route` i `networksetup` z atrapy.

Uruchomienie: /usr/bin/python3 -m unittest tests.test_hotspot
"""

import argparse
import importlib.util
import json
import os
import plistlib
import tempfile
import unittest
from unittest import mock

# prawdziwy osascript pokazałby w testach prawdziwy baner: atrapa jest pierwsza na PATH
os.environ["PATH"] = os.pathsep.join(
    [os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes-osascript"), os.environ.get("PATH", "")]
)

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hotspot", os.path.join(os.path.dirname(HERE), "hotspot.py"))
hotspot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotspot)

_rspec = importlib.util.spec_from_file_location("acc_rootpy", os.path.join(os.path.dirname(HERE), "rootpy.py"))
R = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(R)

ROUTE_USB = """   route to: default
destination: default
    gateway: 172.20.10.1
  interface: en8
"""
ROUTE_WIFI_HOTSPOT = ROUTE_USB.replace("en8", "en0")
ROUTE_HOME = """   route to: default
    gateway: 192.168.0.1
  interface: en0
"""
PORTS = """
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: aa:bb

Hardware Port: iPhone USB
Device: en8
Ethernet Address: cc:dd
"""


class Link:
    """Liczniki interfejsu: wysyłanie z zadaną przepływnością, liczone od zegara testu."""

    def __init__(self):
        self.t = 0.0
        self.tx_kbps = 0.0
        self.obytes = 2 ** 32 - 50_000  # zaraz przekręci licznik 32-bitowy
        self.tbr = []

    def advance(self, dt):
        self.t += dt
        self.obytes = (self.obytes + int(self.tx_kbps * 1000 / 8 * dt)) % 2 ** 32

    def if_bytes(self, name):
        return (0, self.obytes)

    def set_tbr(self, iface, kbps):
        self.tbr.append(kbps)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.link = Link()
        patches = [mock.patch.object(hotspot, "if_bytes", self.link.if_bytes),
                   mock.patch.object(hotspot, "set_tbr", self.link.set_tbr)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctl = hotspot.Controller("en8", start_kbps=30000)

    def feed(self, seconds, rtt, tx_share):
        """Odpowiedzi sond co 50 ms; wysyłanie jako ułamek bieżącego limitu."""
        for i in range(int(seconds / hotspot.PROBE_EVERY)):
            self.link.tx_kbps = self.ctl.rate * tx_share
            self.link.advance(hotspot.PROBE_EVERY)
            value = rtt(i) if callable(rtt) else rtt
            self.ctl.on_rtt(hotspot.REFLECTORS[i % 6], value, self.link.t)

    def test_clean_load_raises_rate_and_safe(self):
        self.feed(1, 20, 0.0)
        self.feed(3, 20, 0.95)
        self.assertGreater(self.ctl.rate, 45000)
        self.assertGreater(self.ctl.safe, 35000)
        self.assertEqual(self.ctl.cuts, 0)
        self.assertTrue(self.link.tbr, "limit nie trafił do ifconfig")

    def test_standing_queue_under_own_load_cuts(self):
        self.feed(1, 20, 0.0)
        before = self.ctl.rate
        self.feed(1.0, 90, 0.95)
        self.assertEqual(self.ctl.cuts, 1, "jedno cięcie na sekundę, nie co sondę")
        self.assertAlmostEqual(self.ctl.rate, before * hotspot.DOWN_FACTOR, delta=1)
        self.feed(3.0, 90, 0.95)
        self.assertEqual(self.ctl.cuts, 4)
        self.assertLessEqual(self.ctl.safe, self.ctl.rate + 1)

    def test_single_radio_spikes_do_not_cut(self):
        self.feed(1, 20, 0.0)
        self.feed(5, lambda i: 250 if i % 3 == 0 else 20, 0.95)
        self.assertEqual(self.ctl.cuts, 0)

    def test_queue_without_own_upload_is_not_ours(self):
        self.feed(1, 20, 0.0)
        self.feed(5, 120, 0.1)
        self.assertEqual(self.ctl.cuts, 0)

    def test_rate_never_below_floor(self):
        self.feed(1, 20, 0.0)
        self.feed(60, 200, 1.0)
        self.assertEqual(self.ctl.rate, hotspot.MIN_KBPS)

    def test_idle_drifts_back_to_safe(self):
        self.feed(1, 20, 0.0)
        self.feed(4, 20, 0.95)
        safe = self.ctl.safe
        self.feed(5, 20, 0.0)
        self.assertAlmostEqual(self.ctl.rate, safe, delta=safe * 0.02)

    def test_baseline_does_not_chase_the_queue(self):
        self.feed(1, 20, 0.0)
        self.feed(20, 200, 0.1)
        self.assertLess(max(self.ctl.base.values()), 25)

    def test_counter_wrap(self):
        self.feed(1, 20, 0.5)
        self.assertGreater(self.ctl.tx_kbps, 10000)
        self.assertLess(self.ctl.tx_kbps, 20000)


class DetectTest(unittest.TestCase):
    def runner(self, route):
        return lambda cmd: route if cmd[0].endswith("route") else PORTS

    def test_iphone_usb(self):
        self.assertEqual(hotspot.detect(self.runner(ROUTE_USB)), ("en8", "USB"))

    def test_iphone_over_wifi(self):
        self.assertEqual(hotspot.detect(self.runner(ROUTE_WIFI_HOTSPOT)), ("en0", "Wi-Fi"))

    def test_home_network(self):
        self.assertIsNone(hotspot.detect(self.runner(ROUTE_HOME)))

    def test_no_route(self):
        self.assertIsNone(hotspot.detect(lambda cmd: ""))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hotspot-")
        self.path = os.path.join(self.dir, "hotspot.json")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_missing_or_broken_means_off(self):
        self.assertFalse(hotspot.read_config(self.path)["enabled"])
        self.write("{nie json")
        self.assertFalse(hotspot.read_config(self.path)["enabled"])
        self.write("[1, 2]")
        self.assertFalse(hotspot.read_config(self.path)["enabled"])

    def test_only_true_enables(self):
        self.write('{"enabled": "yes"}')
        self.assertFalse(hotspot.read_config(self.path)["enabled"])
        self.write('{"enabled": true}')
        self.assertTrue(hotspot.read_config(self.path)["enabled"])

    def test_limits_are_clamped(self):
        self.write('{"enabled": true, "min_mbps": 0, "max_mbps": 99999}')
        cfg = hotspot.read_config(self.path)
        self.assertEqual((cfg["min_kbps"], cfg["max_kbps"]), (1000, 2000000))
        self.write('{"enabled": true, "min_mbps": 50, "max_mbps": 20}')
        cfg = hotspot.read_config(self.path)
        self.assertEqual((cfg["min_kbps"], cfg["max_kbps"]), (20000, 20000))
        self.write('{"enabled": true, "max_mbps": true}')
        self.assertEqual(hotspot.read_config(self.path)["max_kbps"], hotspot.MAX_KBPS)

    def test_symlinked_config_is_ignored(self):
        target = os.path.join(self.dir, "elsewhere.json")
        with open(target, "w") as f:
            f.write('{"enabled": true}')
        os.symlink(target, self.path)
        self.assertFalse(hotspot.read_config(self.path)["enabled"])

    def test_set_enabled_keeps_other_keys(self):
        self.write('{"max_mbps": 40}')
        hotspot.set_enabled(True, self.path)
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"enabled": True, "max_mbps": 40})

    def test_write_json_replaces_a_planted_symlink(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.symlink(victim, self.path + ".tmp")
        hotspot.write_json(self.path, {"a": 1})
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"a": 1})


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hotspot-")
        self.cfg = os.path.join(self.dir, "hotspot.json")
        self.state = os.path.join(self.dir, "hotspot-state.json")
        hotspot.write_json(self.cfg, {"enabled": True})

    def test_fresh_active_state(self):
        hotspot.write_json(self.state, {"at": 1000, "active": True, "iface": "en8", "via": "USB",
                                        "rate_kbps": 41000, "shaping": True})
        s = hotspot.status(self.cfg, self.state, now=1005)
        self.assertTrue(s["running"] and s["active"])
        self.assertEqual((s["iface"], s["rate_kbps"]), ("en8", 41000))

    def test_stale_state_means_not_running(self):
        hotspot.write_json(self.state, {"at": 1000, "active": True, "iface": "en8"})
        s = hotspot.status(self.cfg, self.state, now=1100)
        self.assertFalse(s["running"])
        self.assertFalse(s["active"])
        self.assertNotIn("iface", s)


class RootLaunchTest(unittest.TestCase):
    """Demon roota startuje przez interpreter należący do roota, nie przez `#!/usr/bin/python3`."""

    def plist(self, args):
        """Plista demona z podanym ProgramArguments; status ma powiedzieć, czy start jest stary."""
        path = os.path.join(tempfile.mkdtemp(prefix="hotspot-plist-"), "daemon.plist")
        with open(path, "wb") as f:
            plistlib.dump({"Label": hotspot.LABEL, "ProgramArguments": args}, f)
        return path

    def test_the_installed_plist_starts_an_interpreter_with_I(self):
        body = hotspot.PLIST_BODY.format(label=hotspot.LABEL, python="/root/bin/python3", bin=hotspot.BIN,
                                         config="/c.json", state="/s.json", log="/l.log")
        args = plistlib.loads(body.encode("utf-8"))["ProgramArguments"]
        self.assertEqual(args[:4], ["/root/bin/python3", "-I", hotspot.BIN, "daemon"])

    def test_status_flags_a_daemon_installed_the_old_way(self):
        for args, legacy in (([hotspot.BIN, "daemon"], True),
                             (["/Library/Developer/CommandLineTools/usr/bin/python3", "-I", hotspot.BIN], False)):
            with self.subTest(args=args):
                path = self.plist(args)
                with mock.patch.object(hotspot, "PLIST", path), mock.patch.object(hotspot, "BIN", __file__):
                    s = hotspot.status(os.path.join(self.plist([]), "..", "none.json"), "/nie/ma/state.json")
                self.assertEqual(s["legacy_launch"], legacy)

    def test_install_refuses_without_a_root_owned_interpreter(self):
        args = argparse.Namespace(dry_run=True)
        with mock.patch.object(hotspot, "root_python", return_value=(None, "atrapa: nie root")):
            with mock.patch("sys.stderr"):
                self.assertEqual(hotspot.cmd_install(args), 1)
        # rootpy.py leży obok hotspot.py, więc na tym Macu interpreter się znajduje
        self.assertEqual(hotspot.root_python()[0], R.find()[0])


if __name__ == "__main__":
    unittest.main()
