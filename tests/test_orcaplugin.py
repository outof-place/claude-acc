"""Testy orcaplugin.py (instalacja wtyczki w katalogu wtyczek Orki) i testy samej wtyczki (node).

Wszystko w katalogu testu: --user-data wskazuje tymczasowy userData, a podrobiona Orca.app to
katalog z app.asar. Prawdziwego ~/Library/Application Support/orca nic nie dotyka.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "orcaplugin.py")
PLUGIN = os.path.join(ROOT, "orca-plugin")

_spec = importlib.util.spec_from_file_location("acc_orcaplugin", SCRIPT)
O = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(O)

NODE = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)

# hashPluginTree z Orki (src/main/plugins/plugin-content-hash.ts) przepisany 1:1 na node
ORCA_HASH_JS = r"""
const { createHash } = require('node:crypto')
const { readdirSync, lstatSync, readFileSync } = require('node:fs')
const { join, relative } = require('node:path')
const root = process.argv[1]
const files = []
function collect(dir) {
  const entries = readdirSync(dir, { withFileTypes: true })
  entries.sort((l, r) => (l.name < r.name ? -1 : l.name > r.name ? 1 : 0))
  for (const e of entries) {
    if (dir === root && e.name === '.git') continue
    const full = join(dir, e.name)
    const st = lstatSync(full)
    if (st.isDirectory()) collect(full)
    else if (st.isFile()) files.push(full)
  }
}
collect(root)
const hash = createHash('sha256')
hash.update('orca-plugin-tree-v1\0')
const len = (n) => { const b = Buffer.allocUnsafe(8); b.writeBigUInt64BE(BigInt(n)); hash.update(b) }
for (const f of files) {
  const rel = relative(root, f).replaceAll('\\', '/')
  len(Buffer.byteLength(rel, 'utf8')); hash.update(rel, 'utf8')
  const data = readFileSync(f); len(data.length); hash.update(data)
}
console.log(hash.digest('hex'))
"""


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="orcaplugin-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.user_data = os.path.join(self.dir, "orca")
        self.app = os.path.join(self.dir, "Orca.app")
        os.makedirs(os.path.join(self.app, "Contents/Resources"))
        self.asar(b"... 'settings:own' ...")

    def asar(self, content):
        with open(os.path.join(self.app, "Contents/Resources/app.asar"), "wb") as f:
            f.write(b"\0" * 5_000_000 + content)  # znacznik za granicą pierwszego kawałka czytania

    def run_cli(self, *args):
        done = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr

    def pdir(self):
        return os.path.join(self.user_data, "plugins", O.PLUGIN_KEY)

    def installed_manifest(self):
        with open(os.path.join(self.pdir(), "current")) as f:
            digest = f.read().strip()
        with open(os.path.join(self.pdir(), digest, "orca-plugin.json")) as f:
            return digest, json.load(f)

    def test_install_lays_out_a_hash_addressed_tree(self):
        rc, out = self.run_cli("install", "--user-data", self.user_data, "--app", self.app)
        self.assertEqual(rc, 0, out)
        digest, manifest = self.installed_manifest()
        self.assertEqual(O.tree_hash(os.path.join(self.pdir(), digest)), digest)
        self.assertEqual(sorted(os.listdir(os.path.join(self.pdir(), digest))), ["lib", "orca-plugin.json", "panel", "worker.mjs"])
        # stara Orka: manifest bez kluczy, które jej schemat (strict) odrzuca
        self.assertNotIn("statusBarItems", manifest["contributes"])
        self.assertNotIn("statusBar", [c["kind"] for c in manifest["capabilities"]])
        self.assertIn("Settings › Plugins", out)
        # nic poza katalogiem wtyczki: ustawienia Orki zostają nietknięte
        self.assertEqual(os.listdir(self.user_data), ["plugins"])
        self.assertEqual(os.listdir(os.path.join(self.user_data, "plugins")), [O.PLUGIN_KEY])

    def test_live_orca_gets_status_bar_and_panel_messaging(self):
        self.asar(b'..."statusBar","panelMessaging"]...')
        self.assertEqual(self.run_cli("install", "--user-data", self.user_data, "--app", self.app)[0], 0)
        _, manifest = self.installed_manifest()
        self.assertEqual([i["id"] for i in manifest["contributes"]["statusBarItems"]], ["account", "memory", "awake"])
        self.assertIn("panelMessaging", [c["kind"] for c in manifest["capabilities"]])
        self.assertEqual(self.run_cli("install", "--user-data", self.user_data, "--live", "off")[0], 0)
        self.assertNotIn("statusBarItems", self.installed_manifest()[1]["contributes"])

    def test_reinstall_is_a_no_op_and_keeps_one_rollback(self):
        args = ("install", "--user-data", self.user_data, "--app", self.app)
        self.run_cli(*args)
        first, _ = self.installed_manifest()
        rc, out = self.run_cli(*args)
        self.assertIn("bez zmian", out)
        self.run_cli(*args, "--live", "on")
        second, _ = self.installed_manifest()
        self.run_cli(*args, "--live", "off")
        third, _ = self.installed_manifest()
        self.assertEqual(third, first)
        versions = sorted(e for e in os.listdir(self.pdir()) if O.is_hash(e))
        self.assertEqual(versions, sorted({first, second}))
        self.assertFalse([e for e in os.listdir(self.pdir()) if e.startswith(".staging")])

    def test_refresh_only_touches_an_earlier_install(self):
        rc, out = self.run_cli("install", "--refresh", "--user-data", self.user_data, "--app", self.app)
        self.assertEqual((rc, out), (0, ""))
        self.assertFalse(os.path.exists(self.user_data))
        self.run_cli("install", "--user-data", self.user_data, "--app", self.app)
        rc, out = self.run_cli("install", "--refresh", "--user-data", self.user_data, "--app", self.app)
        self.assertEqual(rc, 0)
        self.assertIn("bez zmian", out)

    def test_refresh_without_orca_does_nothing(self):
        with mock.patch.object(O, "APPS", (os.path.join(self.dir, "missing.app"),)):
            self.assertEqual(O.cmd_install(O.parse(["--refresh"])), 0)
            self.assertEqual(O.cmd_install(O.parse([])), 1)

    def test_status_and_uninstall(self):
        self.run_cli("install", "--user-data", self.user_data, "--app", self.app)
        rc, out = self.run_cli("status", "--json", "--user-data", self.user_data, "--app", self.app)
        state = json.loads(out)
        self.assertEqual((state["installed"], state["live"], state["orca_supports_live"]), (True, False, False))
        self.assertEqual(state["version"], state["source_version"])
        rc, out = self.run_cli("uninstall", "--user-data", self.user_data)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.pdir()))
        self.assertIn("nie ma", self.run_cli("uninstall", "--user-data", self.user_data)[1])

    @unittest.skipUnless(NODE, "node nie jest zainstalowany")
    def test_hash_matches_orcas_algorithm(self):
        self.run_cli("install", "--user-data", self.user_data, "--app", self.app)
        digest, _ = self.installed_manifest()
        tree = os.path.join(self.pdir(), digest)
        js = subprocess.run([NODE, "-e", ORCA_HASH_JS, tree], capture_output=True, text=True, timeout=60)
        self.assertEqual(js.stdout.strip(), digest, js.stderr)


@unittest.skipUnless(NODE, "node nie jest zainstalowany")
class PluginNodeTests(unittest.TestCase):
    """node --test orca-plugin/test: model, pasek statusu, karty, powiadomienia, worker z podróbką Orki."""

    def test_node_suite(self):
        tests = sorted(os.path.join("test", n) for n in os.listdir(os.path.join(PLUGIN, "test")) if n.endswith(".test.mjs"))
        done = subprocess.run([NODE, "--test", *tests], cwd=PLUGIN, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
