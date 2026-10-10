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
        # nieznany klucz wypada przy zapisie: z nim demon odrzuciłby cały plik
        self.write('{"max_mbps": 40, "iface": "en0; reboot"}')
        hotspot.set_enabled(True, self.path)
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"enabled": True, "max_mbps": 40})

    def test_malformed_config_means_safe_defaults_and_a_reason(self):
        """Plik pisze użytkownik, czyta root: wszystko spoza trzech kluczy i ich typów to domyślne."""
        defaults = (False, hotspot.MIN_KBPS, hotspot.MAX_KBPS)
        for text in ('{"enabled": true, "iface": "en0"}', '{"enabled": 1}', '{"enabled": true, "max_mbps": "40"}',
                     '{"enabled": true, "min_mbps": NaN}', '{"enabled": true, "max_mbps": Infinity}',
                     '{"enabled": true, "max_mbps": true}', "[1]", "{nie json"):
            with self.subTest(text=text):
                self.write(text)
                cfg = hotspot.read_config(self.path)
                self.assertEqual((cfg["enabled"], cfg["min_kbps"], cfg["max_kbps"]), defaults)
                self.assertTrue(cfg.get("problem"), text)
        # brak pliku to zwykłe "wyłączony", nie problem
        self.assertNotIn("problem", hotspot.read_config(os.path.join(self.dir, "nie-ma.json")))
        self.write('{"enabled": true, "min_mbps": 10, "max_mbps": 40}')
        self.assertNotIn("problem", hotspot.read_config(self.path))

    def test_tbr_only_on_a_real_interface_with_a_plain_name(self):
        names = {name for _, name in hotspot.socket.if_nameindex()}
        real = "lo0" if "lo0" in names else sorted(names)[0]
        self.assertTrue(hotspot.valid_iface(real))
        for bad in ("en99999", "-x", "en0 tbr 0", "lo0;id", "", None, 7, "../lo0"):
            self.assertFalse(hotspot.valid_iface(bad), bad)
        with mock.patch.object(hotspot.subprocess, "run") as run, mock.patch("sys.stderr"):
            hotspot.set_tbr("-tbr", 5000)
            run.assert_not_called()
            hotspot.set_tbr(real, 10 ** 12)
            self.assertEqual(run.call_args.args[0], ["/sbin/ifconfig", real, "tbr", "%dKbps" % hotspot.TBR_MAX_KBPS])
            hotspot.set_tbr(real, 0)
            self.assertEqual(run.call_args.args[0][-1], "0")

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

    def test_interpreter_check_at_run_time(self):
        """Demon sprawdza przy starcie interpreter jeszcze raz: ścieżka użytkownika to odmowa."""
        self.assertEqual(hotspot.interpreter_problem(["/usr/bin/true"]), None)
        mine = tempfile.mkdtemp(prefix="hotspot-py-")
        self.assertEqual(hotspot.interpreter_problem(["/usr/bin/true", os.path.join(mine, "python3")]),
                         os.path.join(mine, "python3"))  # plik, którego nie ma, też się nie liczy
        open(os.path.join(mine, "python3"), "w").close()
        self.assertEqual(hotspot.interpreter_problem([os.path.join(mine, "python3")]), os.path.join(mine, "python3"))

    def test_daemon_refuses_a_user_owned_interpreter(self):
        with mock.patch.object(hotspot.os, "geteuid", return_value=0), \
             mock.patch.object(hotspot, "interpreter_problem", return_value="/Applications/Xcode.app"), \
             mock.patch.object(hotspot.time, "sleep") as sleep, mock.patch("sys.stderr"):
            self.assertEqual(hotspot.daemon("/nie/ma.json", "/nie/ma-state.json"), 78)
        sleep.assert_called_once_with(300)

    def test_install_refuses_without_a_root_owned_interpreter(self):
        args = argparse.Namespace(dry_run=True)
        with mock.patch.object(hotspot, "root_python", return_value=(None, "atrapa: nie root")):
            with mock.patch("sys.stderr"):
                self.assertEqual(hotspot.cmd_install(args), 1)
        # rootpy.py leży obok hotspot.py, więc na tym Macu interpreter się znajduje
        self.assertEqual(hotspot.root_python()[0], R.find()[0])


FAKE_FOLLOW = """#!/bin/sh
# pod-rootctl stand-in: its arguments, then every line it gets, into the log named next to it
log="$(dirname "$0")/follow.log"
echo "args $*" >> "$log"
while IFS= read -r line; do echo "line $line" >> "$log"; done
echo "eof" >> "$log"
"""


class FollowShaperTest(unittest.TestCase):
    """In Pod (`daemon --rootd`) the limit goes through `pod-rootctl shaper follow`, as the user."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hotspot-follow-")
        self.rootctl = os.path.join(self.dir, "pod-rootctl")
        with open(self.rootctl, "w") as f:
            f.write(FAKE_FOLLOW)
        os.chmod(self.rootctl, 0o755)
        self.log = os.path.join(self.dir, "follow.log")

    def lines(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_a_line_per_change_never_below_the_floor_off_and_eof_at_the_end(self):
        shaper = hotspot.FollowShaper(self.rootctl)
        shaper.set("en8", 30000)
        shaper.set("en8", 3000)  # under the helper's tier A floor: 6 Mb/s
        shaper.set("en8", 0)
        shaper.close()
        self.assertEqual(self.lines(), ["args shaper follow en8", "line 30000", "line 6000", "line off", "eof"])

    def test_set_tbr_goes_to_the_follow_while_the_daemon_runs_with_rootd(self):
        recorded = []

        class Recorder:
            def set(self, iface, kbps):
                recorded.append((iface, kbps))

        real = [i for _, i in __import__("socket").if_nameindex() if i.startswith("lo")][0]
        with mock.patch.object(hotspot, "SHAPER", Recorder()), mock.patch.object(hotspot.subprocess, "run") as run:
            hotspot.set_tbr(real, 27000)
            hotspot.set_tbr(real, 10 ** 12)
        self.assertEqual(recorded, [(real, 27000), (real, hotspot.TBR_MAX_KBPS)])
        run.assert_not_called()  # no ifconfig

    def test_a_follow_that_dies_is_not_started_again_at_once(self):
        with open(self.rootctl, "w") as f:
            f.write('#!/bin/sh\necho "args $*" >> "$(dirname "$0")/follow.log"\nexit 69\n')
        clock = [100.0]
        shaper = hotspot.FollowShaper(self.rootctl, now=lambda: clock[0])
        shaper.set("en8", 30000)
        shaper.procs["en8"].wait(timeout=5)
        for _ in range(5):
            shaper.set("en8", 31000)
        self.assertEqual(self.lines().count("args shaper follow en8"), 1)
        clock[0] += hotspot.ROOTD_RETRY_S + 1
        shaper.set("en8", 31000)
        shaper.procs["en8"].wait(timeout=5)
        self.assertEqual(self.lines().count("args shaper follow en8"), 2)

    def test_without_the_helper_the_user_agent_exits_at_once(self):
        with mock.patch.object(hotspot, "ROOTCTL", os.path.join(self.dir, "missing")):
            self.assertEqual(hotspot.daemon(os.path.join(self.dir, "c.json"), os.path.join(self.dir, "s.json"),
                                            rootd=True), 0)

    def test_status_in_pod_counts_the_helper_as_installed(self):
        cfg = os.path.join(self.dir, "hotspot.json")
        hotspot.write_json(cfg, {"enabled": True})
        with mock.patch.object(hotspot, "ROOTCTL", self.rootctl), mock.patch.object(hotspot, "installed", lambda: False):
            s = hotspot.status(cfg, os.path.join(self.dir, "none.json"))
        self.assertTrue(s["installed"] and s["helper"])
        with mock.patch.object(hotspot, "ROOTCTL", self.rootctl):
            self.assertEqual(hotspot.cmd_install(argparse.Namespace(dry_run=False)), 0)

    def test_the_agent_plist_runs_daemon_rootd_and_stays_down_on_exit_0(self):
        with open(os.path.join(os.path.dirname(HERE), "launchd", "com.filip.claude-acc.hotspot-user.plist.template"), "rb") as f:
            plist = plistlib.loads(f.read().replace(b"__HOME__", b"/Users/x"))
        self.assertEqual(plist["ProgramArguments"][2:], ["hotspot", "daemon", "--rootd"])
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})


class AgentLifecycleTest(unittest.TestCase):
    """The agent is in JOBS (every install), the mode is opt-in: off means down, on starts it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hotspot-agent-")
        self.rootctl = os.path.join(self.dir, "pod-rootctl")
        with open(self.rootctl, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.rootctl, 0o755)
        self.config = os.path.join(self.dir, "hotspot.json")
        self.state = os.path.join(self.dir, "state.json")

    def quiet(self):
        return mock.patch.object(hotspot, "log", lambda line: None)

    def test_the_agent_exits_0_while_the_mode_is_off(self):
        hotspot.write_json(self.config, {"enabled": False})
        with mock.patch.object(hotspot, "ROOTCTL", self.rootctl), mock.patch.object(hotspot, "installed", lambda: False), \
                mock.patch.object(hotspot, "detect") as detect, self.quiet():
            self.assertEqual(hotspot.daemon(self.config, self.state, rootd=True), 0)
        detect.assert_not_called()

    def test_waiting_for_migrate_looks_once_a_minute_and_detects_nothing(self):
        hotspot.write_json(self.config, {"enabled": True})
        clock = [1000.0]
        looks = []

        def installed():
            looks.append(clock[0])
            if len(looks) == 3:
                hotspot.write_json(self.config, {"enabled": False})  # `hotspot off`: the agent then exits
            return True

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(hotspot, "ROOTCTL", self.rootctl), mock.patch.object(hotspot, "installed", installed), \
                mock.patch.object(hotspot, "detect") as detect, mock.patch.object(hotspot, "iphone_ports", lambda: set()), \
                mock.patch.object(hotspot.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(hotspot.time, "sleep", sleep), self.quiet():
            self.assertEqual(hotspot.daemon(self.config, self.state, rootd=True), 0)
        self.assertEqual(len(looks), 3)
        self.assertEqual([round(b - a) for a, b in zip(looks, looks[1:])], [60, 60])
        detect.assert_not_called()

    def pod_owner(self, app_id="codes.pod.app", agents=True):
        """owner.json as setup.sh writes it: "menu" only with --pod-agents (layout 2)."""
        app = os.path.join(self.dir, "Pod.app")
        os.makedirs(os.path.join(app, "Contents"), exist_ok=True)
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": app_id}, f)
        owner = {"owner": "pod", "app": app}
        if agents:
            owner["menu"] = os.path.join(app, "Contents/Library/LoginItems/Pod Menu.app")
        with open(os.path.join(self.dir, "owner.json"), "w") as f:
            json.dump(owner, f)

    def fake_launchctl(self):
        """`print` answers 0 for the labels in $KNOWN (all when unset); the rest is logged."""
        self.calls = os.path.join(self.dir, "launchctl.log")
        launchctl = os.path.join(self.dir, "launchctl")
        with open(launchctl, "w") as f:
            f.write('#!/bin/sh\n'
                    'if [ "$1" = print ]; then\n'
                    '  [ -z "${KNOWN+x}" ] && exit 0\n'
                    '  for l in $KNOWN; do [ "$2" = "gui/$(id -u)/$l" ] && exit 0; done\n'
                    '  exit 113\n'
                    'fi\n'
                    'echo "$*" >> "%s"\nexit "${FAIL:-0}"\n' % self.calls)
        os.chmod(launchctl, 0o755)
        return mock.patch.object(hotspot, "LAUNCHCTL", launchctl)

    def test_the_agent_label_follows_the_layout(self):
        pod = "codes.pod.app.acc.hotspot-user"
        legacy = "com.filip.claude-acc.hotspot-user"
        with mock.patch.object(hotspot, "STATE_DIR", self.dir), self.fake_launchctl():
            self.assertEqual(hotspot.agent_label(), legacy)  # Homebrew, the source checkout
            self.pod_owner(agents=True)  # Pod with its own agents (layout 2)
            self.assertEqual(hotspot.agent_label(), pod)
            # Pod v1 (owner pod, no --pod-agents): setup.sh's LaunchAgents, as on this Mac today
            self.pod_owner(agents=False)
            self.assertEqual(hotspot.agent_label(), legacy)
            with mock.patch.dict(os.environ, {"KNOWN": pod}):
                self.assertEqual(hotspot.agent_label(), pod)  # launchd knows only Pod's
            self.pod_owner(agents=True)
            with mock.patch.dict(os.environ, {"KNOWN": legacy}):
                self.assertEqual(hotspot.agent_label(), legacy)  # launchd knows only setup.sh's
            with mock.patch.dict(os.environ, {"KNOWN": ""}):
                self.assertEqual(hotspot.agent_label(), pod)  # neither: the layout decides
            with open(os.path.join(self.dir, "Pod.app", "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"CFBundleIdentifier": "bad id; rm"}, f)
            self.assertEqual(hotspot.agent_label(), pod)

    def test_hotspot_on_in_pod_kickstarts_the_agent(self):
        self.pod_owner()
        args = argparse.Namespace(no_install=True, dry_run=False)
        with mock.patch.object(hotspot, "STATE_DIR", self.dir), mock.patch.object(hotspot, "ROOTCTL", self.rootctl), \
                self.fake_launchctl(), mock.patch.object(hotspot, "set_enabled") as enabled, mock.patch("sys.stdout"):
            self.assertEqual(hotspot.cmd_on(args), 0)
            enabled.assert_called_once_with(True)
            with mock.patch.dict(os.environ, {"FAIL": "113"}), mock.patch("sys.stderr"):
                self.assertEqual(hotspot.cmd_on(args), 1)
            # Pod v1: the job that exists is setup.sh's
            self.pod_owner(agents=False)
            self.assertEqual(hotspot.cmd_on(args), 0)
        with open(self.calls) as f:
            self.assertEqual(f.read().splitlines(), ["kickstart gui/%d/codes.pod.app.acc.hotspot-user" % os.getuid()] * 2
                             + ["kickstart gui/%d/com.filip.claude-acc.hotspot-user" % os.getuid()])


if __name__ == "__main__":
    unittest.main()
