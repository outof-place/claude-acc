#!/usr/bin/env python3
"""Kompresja APFS (LZFSE) aplikacji należących do roota, ze sprawdzeniem podpisu.

Zadanie `compress` janitora działa bez roota, więc omija pakiety instalowane przez .pkg
(Office, Adobe, App Store, aplikacje z instalatorów): należą do roota i nie da się ich
zapisać. Ten skrypt robi je przez sudo, aplikacja po aplikacji:

1. pomija aplikacje, które działają (kompresja w miejscu podmienia zmapowane pliki
   wykonywalne), i te, w których prawie wszystkie pliki są już skompresowane;
2. sprawdza podpis `codesign --verify --deep --strict`; pakietu z już zepsutym podpisem
   nie dotyka;
3. kompresuje `afsctool -c -T LZFSE`; pliki czytają się jak zwykle, dekompresuje je jądro;
4. sprawdza podpis jeszcze raz; jeśli przed kompresją był dobry, a po niej nie jest,
   rozpakowuje pakiet (`afsctool -d`) i sprawdza znowu.

Aktualizacje Office (Microsoft AutoUpdate) i Creative Cloud wgrywają nowe, nieskompresowane
pakiety, więc komenda jest do powtarzania: drugi przebieg bierze tylko to, co doszło.

Wywołanie przez `claude-acc mac compress-apps [--dry-run] [--apps Word,Excel] [--threads N]`. Plan
liczy janitor bez roota; tryb `run` biegnie pod rootem tylko z kopii roota (`claude-acc root install`):
`sudo /usr/local/libexec/claude-acc-root/root-run.sh compress-apps ...` uruchamia tam ten plik
interpreterem z rootpy.py, z -I, i przypiętą tam kopią afsctool. Ten plik w $STATE i afsctool z
/opt/homebrew może zmienić każdy na tym koncie, więc pod sudo nie biegną. Skrypt nie importuje nic
z repo, żeby tryb -I (bez katalogu skryptu i PYTHONPATH w sys.path) działał pod rootem.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys

APPS_ROOT = "/Applications"
SEARCH = ("/opt/homebrew/bin", "/usr/local/bin")
# udział plików z flagą UF_COMPRESSED, od którego pakiet uchodzi za zrobiony (część plików
# afsctool zawsze odpuszcza: małe albo nieściśliwe)
DONE_RATIO = 0.9
TIMEOUT = 3 * 3600


def find_afsctool():
    found = shutil.which("afsctool")
    if found:
        return found
    for base in SEARCH:
        path = os.path.join(base, "afsctool")
        if os.access(path, os.X_OK):
            return path
    return None


def outer_bundle(path):
    """Najbardziej zewnętrzny katalog .app w ścieżce albo None (helper w Frameworks to ten sam pakiet)."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part.endswith(".app"):
            return "/".join(parts[: i + 1])
    return None


def app_bundles(root=APPS_ROOT):
    """Pakiety .app w /Applications i jeden poziom niżej (Adobe trzyma je w podkatalogach)."""
    found = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        return found
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.name.endswith(".app") and entry.is_dir():
            found.append(entry.path)
        elif entry.is_dir() and not entry.name.startswith("."):
            try:
                inner = sorted(os.scandir(entry.path), key=lambda e: e.name.lower())
            except OSError:
                continue
            for sub in inner:
                if sub.name.endswith(".app") and sub.is_dir() and not sub.is_symlink():
                    found.append(sub.path)
    return found


def running_bundles():
    """Pakiety aplikacji, z których coś teraz działa (`ps` daje pełne ścieżki programów)."""
    out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout
    return {b for b in (outer_bundle(line.strip()) for line in out.splitlines()) if b}


def bundle_stats(path):
    """Pliki, pliki skompresowane, rozmiar logiczny i zajęte bajty pakietu (i-węzeł liczony raz)."""
    files = compressed = logical = used = 0
    seen = set()
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            files += 1
            logical += st.st_size
            used += st.st_blocks * 512
            if st.st_flags & stat.UF_COMPRESSED:
                compressed += 1
    return {"files": files, "compressed": compressed, "logical": logical, "used": used}


def ratio(stats):
    return stats["compressed"] / stats["files"] if stats["files"] else 1.0


def savings(stats):
    """Oszczędność względem rozmiaru logicznego, w procentach."""
    if not stats["logical"]:
        return 0.0
    return max(0.0, 100.0 * (1 - stats["used"] / stats["logical"]))


def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class Tools:
    """Prawdziwe narzędzia systemu; testy podstawiają zamiast nich atrapy."""

    def __init__(self, afsctool, threads):
        self.afsctool = afsctool
        self.threads = threads

    def owner(self, bundle):
        return os.stat(bundle).st_uid

    def stats(self, bundle):
        return bundle_stats(bundle)

    def running(self):
        return running_bundles()

    def verify(self, bundle):
        """(podpis dobry?, pierwsza linia błędu)"""
        try:
            r = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", bundle], capture_output=True, text=True, timeout=TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return False, "codesign: przekroczony czas"
        lines = [line for line in r.stderr.splitlines() if line.strip()]
        return r.returncode == 0, (lines[0] if lines else "")

    def compress(self, bundle):
        try:
            r = subprocess.run(
                [self.afsctool, "-c", "-T", "LZFSE", f"-J{self.threads}", bundle],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0

    def decompress(self, bundle):
        try:
            r = subprocess.run([self.afsctool, "-d", bundle], capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


def matches(bundle, wanted):
    """`--apps Word,premiere` pasuje po nazwie bez .app, bez wielkości liter, albo po pełnej ścieżce."""
    if not wanted:
        return True
    name = os.path.basename(bundle)[:-4].lower()
    for w in wanted:
        w = w.strip().lower()
        if not w:
            continue
        if w == bundle.lower() or w == name or w in name:
            return True
    return False


def plan(bundles, tools, wanted=(), done_ratio=DONE_RATIO):
    """Dla każdego pakietu: (pakiet, stats, powód pominięcia albo None)."""
    running = tools.running()
    rows = []
    for bundle in bundles:
        if not matches(bundle, wanted):
            continue
        try:
            owner = tools.owner(bundle)
        except OSError:
            continue
        if owner != 0:
            # pakiety użytkownika robi zadanie `compress` janitora, bez roota
            if wanted:
                rows.append((bundle, None, "nie root (robi to janitor compress)"))
            continue
        st = tools.stats(bundle)
        if bundle in running:
            rows.append((bundle, st, "działa"))
        elif ratio(st) >= done_ratio:
            rows.append((bundle, st, "już skompresowana"))
        else:
            rows.append((bundle, st, None))
    return rows


def process(bundle, tools, before=None):
    """Kompresja jednego pakietu ze sprawdzeniem podpisu przed i po. Zwraca wiersz wyniku."""
    before = before or tools.stats(bundle)
    row = {"app": bundle, "before": savings(before), "after": savings(before), "freed": 0, "used": before["used"]}
    if bundle in tools.running():
        row.update(status="pominięta: uruchomiła się", signature="nie sprawdzany")
        return row
    ok, why = tools.verify(bundle)
    if not ok:
        row.update(status="pominięta: podpis już zepsuty", signature=f"zły przed: {why}"[:120])
        return row
    if not tools.compress(bundle):
        row["status"] = "afsctool zgłosił błąd"
    after = tools.stats(bundle)
    ok_after, why_after = tools.verify(bundle)
    if ok_after:
        row.update(status=row.get("status", "skompresowana"), signature="dobry")
    else:
        # kompresja nie zmienia treści plików, więc to nie powinno się zdarzyć; gdyby jednak,
        # pakiet wraca do postaci sprzed kompresji
        tools.decompress(bundle)
        after = tools.stats(bundle)
        ok_back, why_back = tools.verify(bundle)
        row.update(
            status="COFNIĘTA: podpis zepsuł się po kompresji",
            signature="dobry po cofnięciu" if ok_back else f"NADAL ZŁY: {why_back or why_after}"[:120],
            rolled_back=True,
        )
    row.update(after=savings(after), freed=max(0, before["used"] - after["used"]))
    return row


def table(rows, headers):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    out = []
    for r in [headers] + rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(out)


def print_plan(rows):
    data = []
    for bundle, st, reason in rows:
        if st is None:
            data.append([os.path.basename(bundle), "-", "-", "-", reason])
            continue
        data.append([
            os.path.basename(bundle), human(st["used"]), f"{100 * ratio(st):.0f}%", f"{savings(st):.1f}%",
            reason or "do kompresji",
        ])
    if not data:
        print("brak aplikacji roota w /Applications")
        return
    print(table(data, ["aplikacja", "zajmuje", "plików skompr.", "oszczędność", "akcja"]))


def print_results(results):
    data = [
        [os.path.basename(r["app"]), f"{r['before']:.1f}% → {r['after']:.1f}%", human(r["freed"]), r["signature"],
         r["status"]]
        for r in results
    ]
    if data:
        print(table(data, ["aplikacja", "oszczędność", "zwolnione", "podpis", "wynik"]))
    freed = sum(r["freed"] for r in results)
    print(f"razem zwolnione: {human(freed)}")
    if any(r.get("rolled_back") for r in results):
        print("UWAGA: przynajmniej jedna aplikacja wróciła do postaci nieskompresowanej, zobacz kolumnę wynik")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="claude-acc mac compress-apps")
    p.add_argument("mode", nargs="?", choices=["plan", "run"], default="plan")
    p.add_argument("--dry-run", action="store_true", help="tylko lista: rozmiary i obecna oszczędność")
    p.add_argument("--apps", default="", help="tylko te aplikacje, po przecinku (np. Word,Excel,Premiere)")
    p.add_argument("--threads", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    p.add_argument("--afsctool", default=None, help="pełna ścieżka do afsctool")
    p.add_argument("--done-ratio", type=float, default=DONE_RATIO)
    p.add_argument("--json-out", default=None, help="zapis wyników jako JSON (dla janitora)")
    args = p.parse_args(argv)
    if args.threads < 1:
        p.error("--threads musi być co najmniej 1")
    args.wanted = [w for w in args.apps.split(",") if w.strip()]
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    afsctool = args.afsctool or find_afsctool()
    if not afsctool or not os.access(afsctool, os.X_OK):
        print("brak afsctool: brew install afsctool", file=sys.stderr)
        return 1
    tools = Tools(afsctool, args.threads)
    rows = plan(app_bundles(), tools, args.wanted, args.done_ratio)
    if not rows and args.wanted:
        print(f"żadna aplikacja w /Applications nie pasuje do --apps {args.apps}")
        return 0
    if args.mode == "plan" or args.dry_run:
        print_plan(rows)
        return 0
    if os.geteuid() != 0:
        print("kompresja aplikacji roota wymaga sudo: claude-acc mac compress-apps", file=sys.stderr)
        return 1
    results = []
    for bundle, st, reason in rows:
        if reason:
            continue
        print(f"kompresuję {os.path.basename(bundle)}...", flush=True)
        results.append(process(bundle, tools, st))
    print_results(results)
    skipped = [(bundle, reason) for bundle, _, reason in rows if reason]
    if skipped:
        print("pominięte: " + ", ".join(f"{os.path.basename(b)} ({r})" for b, r in skipped))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, ensure_ascii=False)
    return 2 if any(r.get("rolled_back") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
