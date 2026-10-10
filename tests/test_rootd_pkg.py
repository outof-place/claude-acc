"""scripts/rootd-pkg.sh: Pod's root helper as an installer package (docs/pod-rootd.md, "The package").

Builds an unsigned package from a fake binary and reads it back with pkgutil and lsbom: the program
in /Library/PrivilegedHelperTools and the job in /Library/LaunchDaemons, root:wheel, the receipt id,
and scripts that boot the job out and in. Nothing is installed; the scripts are never run.

Run: /usr/bin/python3 -m unittest discover -s tests
"""

import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "rootd-pkg.sh")
PLIST = os.path.join(ROOT, "launchd", "codes.pod.app.rootd.plist")


@unittest.skipUnless(os.path.exists("/usr/bin/pkgbuild") and os.path.exists("/usr/bin/productbuild"), "no pkgbuild")
class RootdPackageTest(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="rootd-pkg-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.binary = os.path.join(self.dir, "pod-rootd")
        with open(self.binary, "w") as f:
            f.write("fake helper\n")

    def build(self, *extra, version="56"):
        out = os.path.join(self.dir, "pod-rootd.pkg")
        done = subprocess.run(["/bin/bash", SCRIPT, "--binary", self.binary, "--version", version, "--out", out, *extra],
                              capture_output=True, text=True, timeout=120)
        return done, out

    def test_the_package_puts_the_helper_and_its_job_in_root_only_places(self):
        done, out = self.build()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("codes.pod.rootd.pkg 56 (codes.pod.app.rootd)", done.stdout)
        expanded = os.path.join(self.dir, "expanded")
        subprocess.run(["/usr/sbin/pkgutil", "--expand-full", out, expanded], check=True, capture_output=True)
        component = os.path.join(expanded, next(n for n in os.listdir(expanded) if n.endswith(".pkg")))
        payload = os.path.join(component, "Payload")
        files = sorted(os.path.relpath(os.path.join(d, n), payload) for d, _, names in os.walk(payload) for n in names)
        self.assertEqual(files, ["Library/LaunchDaemons/codes.pod.app.rootd.plist",
                                 "Library/PrivilegedHelperTools/codes.pod.app.rootd"])
        with open(os.path.join(payload, "Library/PrivilegedHelperTools/codes.pod.app.rootd")) as f:
            self.assertEqual(f.read(), "fake helper\n")
        with open(os.path.join(payload, "Library/LaunchDaemons/codes.pod.app.rootd.plist"), "rb") as f, \
                open(PLIST, "rb") as g:
            self.assertEqual(plistlib.load(f), plistlib.load(g))

        # every entry root:wheel; the program 0755, the job 0644, PrivilegedHelperTools keeps its sticky bit
        bom = subprocess.run(["/usr/bin/lsbom", "-p", "fMUG", os.path.join(component, "Bom")], check=True,
                             capture_output=True, text=True).stdout
        entries = {}
        for line in bom.splitlines():
            path, mode, user, group = line.split("\t")
            if not os.path.basename(path).startswith("._"):  # extended attributes, folded back by Installer
                entries[path] = (mode.strip(), user, group)
        self.assertEqual(entries["./Library/PrivilegedHelperTools/codes.pod.app.rootd"], ("-rwxr-xr-x", "root", "wheel"))
        self.assertEqual(entries["./Library/LaunchDaemons/codes.pod.app.rootd.plist"], ("-rw-r--r--", "root", "wheel"))
        self.assertEqual(entries["./Library/PrivilegedHelperTools"], ("drwxr-xr-t", "root", "wheel"))
        self.assertEqual(entries["./Library/LaunchDaemons"], ("drwxr-xr-x", "root", "wheel"))
        self.assertTrue(all(user == "root" and group == "wheel" for _, user, group in entries.values()))

        with open(os.path.join(component, "PackageInfo")) as f:
            info = f.read()
        self.assertRegex(info, r'identifier="codes\.pod\.rootd\.pkg"')
        self.assertRegex(info, r'version="56"')
        self.assertRegex(info, r'install-location="/"')
        self.assertRegex(info, r'relocatable="false"')
        self.assertRegex(info, r'auth="root"')
        # Installer applies the payload's directory modes to /Library/... (overwrite-permissions), which
        # is why the script gives them macOS's own: 0755, and 1755 for PrivilegedHelperTools
        self.assertRegex(info, r'overwrite-permissions="true"')

        scripts = os.path.join(component, "Scripts")
        with open(os.path.join(scripts, "preinstall")) as f:
            pre = f.read()
        with open(os.path.join(scripts, "postinstall")) as f:
            post = f.read()
        self.assertIn("/bin/launchctl bootout system/codes.pod.app.rootd", pre)
        self.assertIn("/bin/launchctl bootstrap system /Library/LaunchDaemons/codes.pod.app.rootd.plist", post)
        # the Pod.app the package came from: only a package inside an app's claude-acc folder counts
        self.assertIn("*/*.app/Contents/Resources/claude-acc/pod-rootd.pkg)", post)
        self.assertIn("state=/var/db/codes.pod.app.rootd", post)
        for name in ("preinstall", "postinstall"):
            self.assertTrue(os.access(os.path.join(scripts, name), os.X_OK))
            self.assertEqual(subprocess.run(["/bin/sh", "-n", os.path.join(scripts, name)]).returncode, 0)
        # unsigned here: Pod's release.sh signs it with the Developer ID Installer identity
        check = subprocess.run(["/usr/sbin/pkgutil", "--check-signature", out], capture_output=True, text=True)
        self.assertIn("no signature", check.stdout)

    def test_bad_input_builds_nothing(self):
        for version in ("", "56a", ".56", "56.", "5 6"):
            done, out = self.build(version=version)
            self.assertEqual(done.returncode, 2, version)
            self.assertFalse(os.path.exists(out))
        odd = os.path.join(self.dir, "odd.plist")
        with open(PLIST, "rb") as f:
            plist = plistlib.load(f)
        plist["Program"] = "/Users/x/pod-rootd"  # the job must point into PrivilegedHelperTools
        with open(odd, "wb") as f:
            plistlib.dump(plist, f)
        done, out = self.build("--plist", odd)
        self.assertEqual(done.returncode, 2)
        self.assertIn("is not /Library/PrivilegedHelperTools/codes.pod.app.rootd", done.stderr)
        self.assertFalse(os.path.exists(out))
        done, _ = self.build("--nonsense")
        self.assertEqual(done.returncode, 2)

    def test_the_script_never_installs(self):
        with open(SCRIPT) as f:
            text = f.read()
        self.assertNotRegex(text, r"(^|[;&|]\s*)(sudo|/usr/sbin/installer|installer)\s")
        self.assertIsNone(re.search(r"launchctl\s+(bootstrap|bootout)", text.split("cat > ")[0]))


if __name__ == "__main__":
    unittest.main()
