#!/usr/bin/env python3
"""Miesięczne kredyty API z planów Max i Team: pula kluczy z wielu organizacji Console.

    claude-acc credits status [--json]
    claude-acc credits add <email> --scope own|KLIENT [--org ID] [--granted-usd 200] [--resets-at DATA] [--new-key]
    claude-acc credits pending <email> [--scope own|KLIENT]
    claude-acc credits remove <email>
    claude-acc credits balance <email> --remaining-usd KWOTA [--expires-at DATA]
    claude-acc credits record --org ID --usd KWOTA --purpose NAZWA
    claude-acc credits exec --purpose NAZWA [--scope own|KLIENT] [--org ID] [--min-remaining-usd 5] [--no-env] -- <komenda...>
    claude-acc credits helper --purpose NAZWA [--scope own|KLIENT] [--org ID]
    claude-acc credits key --purpose NAZWA [--scope own|KLIENT] [--org ID] [--min-remaining-usd 5] [--json]

Plan Max daje co cykl rozliczeniowy kredyt w jednej, powiązanej organizacji Console ($200 dla
Max 20x, $100 dla Max 5x). Kredyt nie przechodzi na kolejny cykl, więc `exec` bierze organizację,
której kredyt wygasa najwcześniej i ma jeszcze zapas (albo tę z `--org`, bez zastępstwa), i
uruchamia komendę na jej koszt. Zakresy się nie mieszają: kredyt planu oznaczonego zakresem
klienta (`--scope` z jego nazwą) płaci tylko za pracę dla tego klienta, nigdy za prywatną, i
odwrotnie; zakres bez zapasu to kod 75, nie zastępstwo z innego. Kody wyjścia `exec`: kod komendy; 75, gdy przed startem nikt nie ma
zapasu (wywołujący czeka albo wraca na subskrypcję); 76, gdy API w trakcie odpowiedziało
"credit balance is too low" (przerwane, mogły zostać efekty uboczne, nie ponawiać automatycznie).

Klucz dla komendy: `exec` wstawia go do ANTHROPIC_API_KEY dziecka (proste skrypty), a
`exec --no-env` nie wstawia go nigdzie: dziecko (Claude Code, Agent SDK) bierze go przez
apiKeyHelper = `claude-acc credits helper --purpose NAZWA`, który oddaje klucz organizacji
wybranej przez exec (CLAUDE_ACC_CREDITS_ORG) na stdout procesu, który go woła. Agent z Bash w
takim biegu nie ma klucza w środowisku, a strażnik (devguard) nie przepuszcza mu `credits helper`.

Klucze leżą wyłącznie w Pęku kluczy (usługa "claude-acc-credits", konto = e-mail). `add` pyta o
klucz oknem systemowym z ukrytym polem, zapisuje go przez stdin `security -i` i sprawdza w API
(GET /v1/models, nagłówek anthropic-organization-id), że należy do podanej organizacji. Klucz
nie trafia do argumentów procesu, na wyjście (poza helperem), do logu ani do plików, a `key`
oddaje referencję do Pęku kluczy, nie klucz.

Saldo: API nie podaje salda kredytów promocyjnych (Admin API nie działa dla kont indywidualnych
i nie ma endpointu salda), więc prawdą jest Console: Settings > Billing > Promotional credits.
Odczyt z Console wpisuje `balance`; między odczytami zostało = odczyt minus wydatki zgłoszone
przez `record` po odczycie (a bez odczytu w tym cyklu: przyznane minus wydatki od początku cyklu).
Wyczerpanie zauważone przez `exec` zapisuje się jak odczyt 0. Cykl to miesięczna rocznica daty
z `--resets-at`/`--expires-at`, a bez niej startu subskrypcji konta z `claude-acc status`.
"""

import calendar
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
CREDITS_DIR = os.path.join(STATE_DIR, "credits")
REGISTRY_PATH = os.path.join(CREDITS_DIR, "accounts.json")
LOCK_PATH = os.path.join(CREDITS_DIR, ".lock")
LOG_PATH = os.path.join(CREDITS_DIR, "credits.log")
# konta z `claude-acc status`: tożsamość z API profilu z datą startu subskrypcji
ACCOUNTS_STATE = os.path.join(STATE_DIR, "state.json")

KEYCHAIN_SERVICE = "claude-acc-credits"
API = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
# zakres: "own" albo nazwa klienta, którego kredyt płaci tylko za jego pracę
SCOPE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
STATES = ("linked", "pending", "error")
# przydział planu z tieru konta (profil API); bez tieru zakładamy Max 20x
GRANT_BY_TIER = {"default_claude_max_20x": 200.0, "default_claude_max_5x": 100.0}
DEFAULT_GRANT = 200.0
DEFAULT_MIN_REMAINING = 5.0
NO_CREDIT = 75  # EX_TEMPFAIL: brak zapasu przed startem, wywołujący może spróbować później
EXHAUSTED = 76  # kredyt skończył się w trakcie: przerwane, możliwe efekty uboczne, bez ponawiania
LOG_MAX_BYTES = 256 * 1024
# zmienne, które w Claude Code wygrywają z ANTHROPIC_API_KEY albo wysłałyby klucz gdzie indziej:
# dziecko `exec` ma płacić kredytem tej organizacji i tylko do api.anthropic.com
OVERRIDING_ENV = (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)  # fmt: skip
PURPOSE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
API_KEY_RE = re.compile(r"sk-ant-api[0-9A-Za-z_-]+")
# komunikat API ("Your credit balance is too low...") i Claude Code ("Credit balance is too low")
EXHAUSTED_RE = re.compile(rb"credit balance (?:is )?too low", re.IGNORECASE)


class CreditsError(Exception):
    """Błąd, który użytkownik ma zobaczyć jako jedną linię, bez śladu stosu (kod 1)."""


class UsageError(CreditsError):
    """Źle wywołana komenda (kod 2)."""


# ---------- pliki ----------


def log(line):
    os.makedirs(CREDITS_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
        with open(LOG_PATH) as f:
            tail = f.readlines()[-500:]
        with open(LOG_PATH, "w") as f:
            f.writelines(tail)
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


class Locked:
    """Blokada katalogu kredytów: zapis rejestru i dopisanie do dziennika wydatków naraz z wielu procesów."""

    def __enter__(self):
        os.makedirs(CREDITS_DIR, mode=0o700, exist_ok=True)
        self.fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_registry():
    try:
        with open(REGISTRY_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"accounts": {}}
    except ValueError as exc:
        raise CreditsError(f"zepsuty {REGISTRY_PATH}: {exc}")
    data.setdefault("accounts", {})
    return data


def save_registry(registry):
    write_json(REGISTRY_PATH, registry)


def ledger_path(ts):
    """Dziennik wydatków dzielony na miesiące: odczyt cyklu czyta najwyżej dwa pliki."""
    return os.path.join(CREDITS_DIR, f"ledger-{datetime.fromtimestamp(ts):%Y-%m}.jsonl")


def append_ledger(entry):
    """Jedna linia JSON pod blokadą: równoległe `record` nie przeplatają ani nie gubią wpisów."""
    path = ledger_path(entry["at"])
    with Locked():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_spend(since):
    """Suma zgłoszonych wydatków na organizację od chwili `since` (czas unixowy, None = od zawsze)."""
    if not os.path.isdir(CREDITS_DIR):
        return {}
    first = None if since is None else datetime.fromtimestamp(since).strftime("%Y-%m")
    totals = {}
    for name in sorted(os.listdir(CREDITS_DIR)):
        m = re.fullmatch(r"ledger-(\d{4}-\d{2})\.jsonl", name)
        if not m or (first and m.group(1) < first):
            continue
        with open(os.path.join(CREDITS_DIR, name)) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # urwana linia po przerwanym zapisie nie psuje sumy
                if since is None or entry.get("at", 0) >= since:
                    org = entry.get("org_id")
                    totals[org] = totals.get(org, 0.0) + float(entry.get("usd") or 0)
    return totals


# ---------- cykl rozliczeniowy ----------


def parse_when(text):
    """Data (2026-10-15) albo czas ISO na naiwny czas lokalny; kalendarz liczymy bez stref,
    żeby przejście na czas zimowy nie przesuwało rocznicy na poprzedni dzień."""
    text = (text or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.strptime(text, "%Y-%m-%d")
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise UsageError(f"zła data {text!r}: np. 2026-10-15")
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def shift_month(anchor, months):
    """Ten sam dzień i godzina `months` miesięcy dalej; 31. w krótszym miesiącu to jego ostatni dzień."""
    total = anchor.year * 12 + anchor.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    return anchor.replace(year=year, month=month, day=min(anchor.day, calendar.monthrange(year, month)[1]))


def cycle_around(anchor, now):
    """(początek, koniec) miesięcznego cyklu z rocznicą w dniu `anchor`, w którym leży `now`."""
    k = (now.year - anchor.year) * 12 + now.month - anchor.month
    while shift_month(anchor, k) <= now:
        k += 1
    while shift_month(anchor, k - 1) > now:
        k -= 1
    return shift_month(anchor, k - 1), shift_month(anchor, k)


def known_accounts():
    """Konta z `claude-acc status` po e-mailu: tier i start subskrypcji z API profilu."""
    try:
        with open(ACCOUNTS_STATE) as f:
            identity = json.load(f).get("identity") or {}
    except (OSError, ValueError):
        return {}
    return {(i.get("email") or "").lower(): i for i in identity.values() if i.get("email")}


def anchor_of(email, spec, accounts):
    if spec.get("resets_at"):
        return parse_when(spec["resets_at"])
    since = (accounts.get(email) or {}).get("subscription_since")
    return parse_when(since) if since else None


def iso(dt):
    return dt.astimezone().isoformat(timespec="seconds") if dt else None


def iso_ts(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


# ---------- saldo ----------


def view(email, spec, accounts, spend_since, now):
    """Jedno konto w kształcie kontraktu dla Jarvisa plus pola wewnętrzne z podkreślnikiem."""
    anchor = anchor_of(email, spec, accounts)
    start, end = cycle_around(anchor, now) if anchor else (None, None)
    row = {
        "email": email,
        "org_id": spec.get("org_id"),
        "scope": spec.get("scope", "own"),
        "granted_usd": 0.0,
        "spent_usd": 0.0,
        "remaining_usd": 0.0,
        "cycle_resets_at": iso(end),
        "checked_at": None,
        "state": spec.get("state", "linked"),
        "_ends": end.timestamp() if end else None,
    }
    if row["state"] == "pending":
        row["checked_at"] = iso_ts(spec.get("checked_at"))
        return row
    granted = float(spec.get("granted_usd") or DEFAULT_GRANT)
    reading = spec.get("balance") or {}
    cycle_start = start.timestamp() if start else spec.get("added_at")
    if reading.get("at") and (cycle_start is None or reading["at"] >= cycle_start):
        # odczyt z Console w tym cyklu: od niego odejmujemy tylko to, co zgłoszono później
        base, since = float(reading["remaining_usd"]), reading["at"]
        row["checked_at"] = iso_ts(reading["at"])
        granted = max(granted, base)
    else:
        base, since = granted, cycle_start
    remaining = max(base - spend_since(since).get(spec.get("org_id"), 0.0), 0.0)
    row.update(
        granted_usd=round(granted, 4),
        spent_usd=round(granted - remaining, 4),
        remaining_usd=round(remaining, 4),
    )
    return row


def overview(registry=None, now=None):
    """Wszystkie konta i suma pozostałego kredytu połączonych kont: wynik `status --json`."""
    registry = registry or load_registry()
    now = now or datetime.now()
    accounts = known_accounts()
    cache = {}

    def spend_since(since):
        if since not in cache:
            cache[since] = ledger_spend(since)
        return cache[since]

    rows = [view(email, spec, accounts, spend_since, now) for email, spec in sorted(registry["accounts"].items())]
    total = sum(r["remaining_usd"] for r in rows if r["state"] == "linked")
    return {"total_remaining_usd": round(total, 4), "accounts": rows}


def public(data):
    """Kontrakt bez pól wewnętrznych."""
    return {
        "total_remaining_usd": data["total_remaining_usd"],
        "accounts": [{k: v for k, v in r.items() if not k.startswith("_")} for r in data["accounts"]],
    }


def targets(data, scope, org, min_remaining):
    """Konta, z których można płacić, w kolejności prób; pusta lista to brak zapasu (kod 75).

    Przypięta organizacja (`--org`) to jedyny kandydat: bez wyboru i bez zastępstwa, a gdy
    podano też zakres, musi się z nim zgadzać (kredyt klienta nigdy nie płaci za prywatne
    i odwrotnie). Bez przypięcia: konta z zakresu, najpierw kredyt, który wygasa najwcześniej
    (i tak przepadnie), przy tej samej dacie mniejszy zapas, żeby dopalić jedno konto do końca.
    Zakresy się nie mieszają: brak zapasu w zakresie klienta nie sięga po own."""
    if org:
        row = next((r for r in data["accounts"] if r["org_id"] == org), None)
        if row is None:
            raise CreditsError(f"nieznana organizacja {org}: claude-acc credits status")
        if scope and row["scope"] != scope:
            raise CreditsError(f"organizacja {org} ma zakres {row['scope']}, nie {scope}; nic nie uruchomiono")
        usable = row["state"] == "linked" and row["remaining_usd"] >= min_remaining
        return [row] if usable else []
    rows = [
        r for r in data["accounts"]
        if r["state"] == "linked" and r["scope"] == (scope or "own") and r["org_id"]
        and r["remaining_usd"] >= min_remaining
    ]  # fmt: skip
    return sorted(rows, key=lambda r: (r["_ends"] or float("inf"), r["remaining_usd"], r["email"]))


def panel(registry=None, now=None):
    """Skrót dla `claude-acc status --json` i aplikacji w pasku menu; None bez żadnego konta."""
    registry = registry or load_registry()
    if not registry["accounts"]:
        return None
    data = overview(registry, now)
    linked = [r for r in data["accounts"] if r["state"] == "linked"]
    by_scope = {}
    for r in linked:
        by_scope[r["scope"]] = round(by_scope.get(r["scope"], 0.0) + r["remaining_usd"], 4)
    # najbliższy dzień, w którym przepada niezerowy kredyt, i ile wtedy przepada
    expiring = sorted((r["_ends"], r["remaining_usd"]) for r in linked if r["_ends"] and r["remaining_usd"] > 0)
    first_day = datetime.fromtimestamp(expiring[0][0]).date() if expiring else None
    readings = [r["checked_at"] for r in linked if r["checked_at"]]
    return {
        "total_remaining_usd": data["total_remaining_usd"],
        "granted_usd": round(sum(r["granted_usd"] for r in linked), 4),
        "linked": len(linked),
        "pending": sum(r["state"] == "pending" for r in data["accounts"]),
        "error": sum(r["state"] == "error" for r in data["accounts"]),
        "by_scope": by_scope,
        "next_expiry_at": expiring[0][0] if expiring else None,
        "next_expiry_usd": round(sum(usd for ends, usd in expiring if datetime.fromtimestamp(ends).date() == first_day), 4)
        if expiring else None,
        # najstarszy odczyt z Console w tym cyklu: tyle ma najmniej świeża liczba w sumie
        "checked_at": min(parse_when(c).timestamp() for c in readings) if readings else None,
    }


def money(value):
    return f"${value:,.2f}"


def summary_line(summary=None):
    """Jedna linia dla `claude-acc status`; None bez żadnego konta."""
    p = panel() if summary is None else summary
    if not p:
        return None
    scopes = ", ".join(f"{k} {money(v)}" for k, v in sorted(p["by_scope"].items()))
    text = f"kredyty API: zostało {money(p['total_remaining_usd'])} z {money(p['granted_usd'])}"
    if scopes:
        text += f" ({scopes})"
    if p["pending"]:
        text += f", czeka {p['pending']}"
    if p["error"]:
        text += f", błąd {p['error']}"
    if p["next_expiry_at"]:
        text += f"; {money(p['next_expiry_usd'])} wygasa {datetime.fromtimestamp(p['next_expiry_at']):%d.%m}"
    return text


# ---------- Pęk kluczy i okno na klucz ----------


def security(args, stdin=None):
    return subprocess.run(
        [shutil.which("security") or "/usr/bin/security"] + args,
        input=stdin, capture_output=True, text=True, timeout=30,
    )  # fmt: skip


def key_exists(email):
    """Czy wpis jest, bez czytania sekretu (bez -w `security` wypisuje tylko atrybuty)."""
    return security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email]).returncode == 0


def key_read(email):
    out = security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email, "-w"])
    value = out.stdout.strip() if out.returncode == 0 else ""
    return value or None


def key_store(email, key):
    """Zapis przez `security -i`: klucz idzie stdin-em, nie w argumentach procesu. Klucz przeszedł
    API_KEY_RE i adres jest bez cudzysłowów, więc linia polecenia nie wymaga ucieczek."""
    if not API_KEY_RE.fullmatch(key) or '"' in email or "\\" in email:
        raise CreditsError("klucz albo adres w złym formacie")
    cmd = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a "{email}" -l "claude-acc credits {email}" -w "{key}"\n'
    out = security(["-i"], stdin=cmd)
    if out.returncode != 0 or "error" in out.stderr.lower():
        raise CreditsError(f"zapis do Pęku kluczy nieudany ({email}): {out.stderr.strip()[:200]}")


def key_delete(email):
    security(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email])


def ask_key(email):
    """Klucz przez okno systemowe z ukrytym polem: nie przechodzi przez terminal ani agenta."""
    prompt = (
        f"Paste the API key of the Claude Console organization linked to {email}. "
        "It goes straight to your Keychain."
    )
    script = (
        f'display dialog {json.dumps(prompt)} default answer "" with hidden answer '
        'with title "claude-acc credits" buttons {"Cancel", "Save"} default button "Save"'
    )
    out = subprocess.run(
        [shutil.which("osascript") or "/usr/bin/osascript", "-e", script, "-e", "text returned of result"],
        capture_output=True, text=True,
    )  # fmt: skip
    if out.returncode != 0:
        raise CreditsError("anulowane")
    return out.stdout.strip()


def check_key_shape(key):
    if key.startswith("sk-ant-admin"):
        raise CreditsError("to klucz Admin API; potrzebny zwykły klucz API organizacji (sk-ant-api...)")
    if not API_KEY_RE.fullmatch(key):
        raise CreditsError("to nie wygląda na klucz API (zaczyna się od sk-ant-api); nic nie zapisano")


def verify_key(key):
    """Organizacja klucza z API: GET /v1/models nic nie kosztuje, a nagłówek
    anthropic-organization-id mówi, do której organizacji należy klucz.
    Zwraca (org_id albo None, opis problemu albo None, czy API odpowiedziało); klucz idzie
    do curl stdin-em."""
    lines = [
        f'url = "{API}/v1/models"',
        "silent",
        "max-time = 20",
        'write-out = "\\n%{http_code}\\n%header{anthropic-organization-id}"',
        f'header = "x-api-key: {key}"',
        f'header = "anthropic-version: {API_VERSION}"',
    ]
    try:
        out = subprocess.run(
            [shutil.which("curl") or "/usr/bin/curl", "-K", "-"],
            input="\n".join(lines), capture_output=True, text=True, timeout=40,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return None, "API nie odpowiedziało", False
    head, _, org = out.stdout.rpartition("\n")
    body, _, code = head.rpartition("\n")
    if not code.isdigit() or code == "000":
        return None, "brak połączenia z API", False
    if code == "200" and org.strip():
        return org.strip(), None, True
    try:
        message = ((json.loads(body) or {}).get("error") or {}).get("message") or ""
    except ValueError:
        message = ""
    if "anthropic-workspace-id" in message:
        return None, "klucz nie jest przypisany do workspace: utwórz klucz z Workspace = Default", True
    if code in ("401", "403"):
        return None, f"API odrzuciło klucz (HTTP {code}): nieprawidłowy, wyłączony albo wygasły", True
    return None, f"API odpowiedziało HTTP {code} {message[:120]}".strip(), True


# ---------- komendy ----------


def flag(args, name):
    """Wartość opcji `name` (ostatnia, gdy podana kilka razy); usuwa ją z args."""
    values, rest, i = [], [], 0
    while i < len(args):
        if args[i] == name:
            if i + 1 >= len(args):
                raise UsageError(f"{name} wymaga wartości")
            values.append(args[i + 1])
            i += 2
        else:
            rest.append(args[i])
            i += 1
    args[:] = rest
    return values[-1] if values else None


def switch(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def usd(text, name):
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise UsageError(f"{name} wymaga kwoty w USD, np. 0.42")
    if not (value >= 0 and value < 1e7):
        raise UsageError(f"{name}: kwota poza zakresem")
    return value


def scope_of(text, default=None):
    value = text or default
    if not value or not SCOPE_RE.fullmatch(value):
        raise UsageError("--scope: own albo nazwa klienta (małe litery, cyfry, -)")
    return value


def email_of(args):
    if not args or "@" not in args[0]:
        raise UsageError("podaj e-mail konta Claude, np. a@example.com")
    return args.pop(0).strip().lower()


def no_leftovers(args):
    if args:
        raise UsageError(f"nieznane argumenty: {' '.join(args)}")


def cmd_status(args):
    as_json = switch(args, "--json")
    no_leftovers(args)
    data = overview()
    if as_json:
        print(json.dumps(public(data), ensure_ascii=False))
        return 0
    if not data["accounts"]:
        print("brak kont z kredytami API: claude-acc credits add <email> --scope own|KLIENT")
        return 0
    print(summary_line() or "")
    print(f"\n{'konto':<32}{'zakres':<9}{'przyznane':>10}{'wydane':>10}{'zostało':>10}  {'odnowienie':<12}{'odczyt Console':<16}stan")
    for r in data["accounts"]:
        reset = f"{parse_when(r['cycle_resets_at']):%d.%m}" if r["cycle_resets_at"] else "-"
        checked = f"{parse_when(r['checked_at']):%d.%m %H:%M}" if r["checked_at"] else "-"
        if r["state"] == "pending":
            print(f"{r['email']:<32}{r['scope']:<9}{'-':>10}{'-':>10}{'-':>10}  {reset:<12}{'-':<16}pending (sprawdzone {checked})")
            continue
        print(
            f"{r['email']:<32}{r['scope']:<9}{money(r['granted_usd']):>10}{money(r['spent_usd']):>10}"
            f"{money(r['remaining_usd']):>10}  {reset:<12}{checked:<16}{r['state']}"
        )
    print(
        "\nzostało = odczyt z Console minus wydatki zgłoszone później (bez odczytu: przyznane minus wydatki w cyklu)."
        "\nnowy odczyt: claude-acc credits balance <email> --remaining-usd KWOTA [--expires-at DATA]"
    )
    return 0


def cmd_add(args):
    scope = scope_of(flag(args, "--scope"))
    org = flag(args, "--org")
    granted = flag(args, "--granted-usd")
    resets = flag(args, "--resets-at")
    new_key = switch(args, "--new-key")
    email = email_of(args)
    no_leftovers(args)
    if resets:
        parse_when(resets)
    stored = key_exists(email)
    fresh = new_key or not stored
    key = ask_key(email) if fresh else key_read(email)
    if fresh:
        check_key_shape(key)
    if not key:
        raise CreditsError(f"brak klucza w Pęku kluczy; wklej go jeszcze raz: claude-acc credits add {email} --scope {scope} --new-key")
    found, problem, reachable = verify_key(key)
    if reachable and not found:
        hint = "" if fresh else " (nowy klucz: --new-key)"
        raise CreditsError(f"{problem}; nic nie zapisano{hint}")
    if found and org and found != org:
        raise CreditsError(f"klucz należy do organizacji {found}, nie {org}; nic nie zapisano")
    if not found and not org:
        raise CreditsError(f"{problem}: bez sieci podaj --org z Console (Settings > Organization); nic nie zapisano")
    if fresh:
        key_store(email, key)
    key = None
    tier = (known_accounts().get(email) or {}).get("tier")
    with Locked():
        registry = load_registry()
        spec = registry["accounts"].get(email) or {"added_at": time.time()}
        spec.update(
            org_id=found or org,
            scope=scope,
            state="linked",
            granted_usd=usd(granted, "--granted-usd") if granted else spec.get("granted_usd") or GRANT_BY_TIER.get(tier, DEFAULT_GRANT),
            verified_at=time.time() if found else spec.get("verified_at"),
            error=None,
        )
        if resets:
            spec["resets_at"] = resets
        spec.pop("checked_at", None)
        registry["accounts"][email] = spec
        save_registry(registry)
    log(f"add: {email} -> {spec['org_id']} ({scope}, ${spec['granted_usd']:.0f})")
    note = "" if found else f" (bez sprawdzenia w API: {problem})"
    print(f"połączone: {email}, organizacja {spec['org_id']}, {scope}, {money(spec['granted_usd'])} na cykl{note}")
    if not spec.get("resets_at") and email not in known_accounts():
        print("nie znam cyklu tego konta: podaj datę z Console, claude-acc credits balance <email> --remaining-usd KWOTA --expires-at DATA")
    return 0


def cmd_pending(args):
    scope = flag(args, "--scope")
    email = email_of(args)
    no_leftovers(args)
    with Locked():
        registry = load_registry()
        spec = registry["accounts"].get(email)
        if spec and spec.get("state") != "pending":
            raise CreditsError(f"{email} jest już połączone ({spec.get('state')}); zdejmij je najpierw: claude-acc credits remove {email}")
        spec = spec or {"added_at": time.time(), "state": "pending", "org_id": None}
        spec["scope"] = scope_of(scope, spec.get("scope") or "own")
        spec["checked_at"] = time.time()
        registry["accounts"][email] = spec
        save_registry(registry)
    log(f"pending: {email}")
    print(f"czeka: {email} ({spec['scope']}); sprawdź przycisk API credits w claude.ai jutro")
    return 0


def cmd_remove(args):
    email = email_of(args)
    no_leftovers(args)
    with Locked():
        registry = load_registry()
        if email not in registry["accounts"]:
            raise CreditsError(f"nie ma konta {email}")
        del registry["accounts"][email]
        save_registry(registry)
    key_delete(email)
    log(f"remove: {email}")
    print(f"usunięte: {email} (klucz z Pęku kluczy też; dziennik wydatków zostaje)")
    return 0


def cmd_balance(args):
    remaining = flag(args, "--remaining-usd")
    expires = flag(args, "--expires-at")
    email = email_of(args)
    no_leftovers(args)
    value = usd(remaining, "--remaining-usd")
    if expires:
        parse_when(expires)
    with Locked():
        registry = load_registry()
        spec = registry["accounts"].get(email)
        if not spec or spec.get("state") == "pending":
            raise CreditsError(f"{email} nie jest połączone: claude-acc credits add {email} --scope own|KLIENT")
        spec["balance"] = {"at": time.time(), "remaining_usd": value}
        if expires:
            spec["resets_at"] = expires
        save_registry(registry)
    log(f"balance: {email} {money(value)}")
    print(f"odczyt zapisany: {email} ma {money(value)}")
    return 0


def cmd_record(args):
    org = flag(args, "--org")
    amount = flag(args, "--usd")
    purpose = flag(args, "--purpose")
    no_leftovers(args)
    value = usd(amount, "--usd")
    if not purpose or not PURPOSE_RE.fullmatch(purpose):
        raise UsageError("--purpose: litery, cyfry i . _ : - (do 64 znaków)")
    owners = [e for e, s in load_registry()["accounts"].items() if org and s.get("org_id") == org]
    if not owners:
        raise CreditsError(f"nieznana organizacja {org!r}: claude-acc credits status")
    append_ledger({"at": time.time(), "org_id": org, "email": owners[0], "usd": value, "purpose": purpose})
    print(f"zapisane: {money(value)} z {owners[0]} ({purpose})")
    return 0


def pick_options(args):
    """--purpose, --scope (None: own, a przy --org zakres tej organizacji), --org i
    --min-remaining-usd (None: próg domyślny dla danej komendy)."""
    purpose = flag(args, "--purpose")
    scope = flag(args, "--scope")
    if scope is not None:
        scope_of(scope)
    org = flag(args, "--org")
    minimum = flag(args, "--min-remaining-usd")
    minimum = None if minimum is None else usd(minimum, "--min-remaining-usd")
    if not purpose or not PURPOSE_RE.fullmatch(purpose):
        raise UsageError("--purpose: litery, cyfry i . _ : - (do 64 znaków), np. polid-digest")
    return purpose, scope, org, minimum


def mark_error(email, reason):
    with Locked():
        registry = load_registry()
        spec = registry["accounts"].get(email)
        if spec:
            spec.update(state="error", error=reason)
            save_registry(registry)
    log(f"błąd: {email} {reason}")


def mark_exhausted(email):
    """API odmówiło, bo kredyt się skończył: zostało 0 do końca cyklu albo do odczytu `balance`."""
    with Locked():
        registry = load_registry()
        spec = registry["accounts"].get(email)
        if spec:
            spec["balance"] = {"at": time.time(), "remaining_usd": 0.0, "source": "exhausted"}
            save_registry(registry)
    log(f"wyczerpane w trakcie: {email}")


def no_credit(data, scope, org, minimum):
    """Komunikat dla wywołującego przy kodzie 75: kiedy wróci kredyt i co robić do tego czasu."""
    scope = scope or "own"
    mine = [r for r in data["accounts"] if r["state"] == "linked" and (r["org_id"] == org if org else r["scope"] == scope)]
    ends = sorted(r["_ends"] for r in mine if r["_ends"])
    when = f"; najbliższe odnowienie {datetime.fromtimestamp(ends[0]):%d.%m %H:%M}" if ends else ""
    who = f"organizacja {org}" if org else f"żadne konto w zakresie {scope}"
    print(
        f"brak kredytu API: {who} nie ma {money(minimum)} zapasu{when}. Poczekaj albo uruchom na subskrypcji.",
        file=sys.stderr,
    )
    return NO_CREDIT


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def run_watched(command, env):
    """Dziecko z tym samym stdin; jego stdout i stderr idą bajt w bajt na nasze, a po drodze
    wypatrujemy komunikatu API o wyczerpanym kredycie (`claude -p` pisze go na stdout, SDK w
    wyjątku na stderr). Wyjścia nie zapisujemy ani nie logujemy, a klucz nie bierze w tym udziału.
    Zwraca (kod wyjścia jak w powłoce, czy padł komunikat)."""
    import selectors
    import signal

    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: proc.send_signal(signum))
    sel = selectors.DefaultSelector()
    tails, gone, seen = {}, set(), False
    for pipe, target in ((proc.stdout, 1), (proc.stderr, 2)):
        sel.register(pipe, selectors.EVENT_READ, target)
        tails[target] = b""
    while sel.get_map():
        for item, _ in sel.select():
            chunk = os.read(item.fileobj.fileno(), 65536)
            if not chunk:
                sel.unregister(item.fileobj)
                continue
            window = tails[item.data] + chunk
            seen = seen or bool(EXHAUSTED_RE.search(window))
            tails[item.data] = window[-64:]  # zdanie rozcięte między dwa kawałki też się znajdzie
            if item.data not in gone:
                try:
                    write_all(item.data, chunk)
                except BrokenPipeError:
                    gone.add(item.data)  # czytelnik odszedł; dziecko pisze dalej, my tylko patrzymy
    code = proc.wait()
    return (128 - code if code < 0 else code), seen


def runs_dir():
    return os.path.join(CREDITS_DIR, "runs")


def cmd_exec(args):
    if "--" not in args:
        raise UsageError("usage: claude-acc credits exec --purpose NAZWA [--scope own|KLIENT] [--org ID] [--no-env] -- <komenda...>")
    split = args.index("--")
    options, command = args[:split], args[split + 1:]
    no_env = switch(options, "--no-env")
    purpose, scope, org, minimum = pick_options(options)
    no_leftovers(options)
    if not command:
        raise UsageError("brak komendy po --")
    minimum = DEFAULT_MIN_REMAINING if minimum is None else minimum
    data = overview()
    for row in targets(data, scope, org, minimum):
        email = row["email"]
        env = {k: v for k, v in os.environ.items() if k not in OVERRIDING_ENV and k != "ANTHROPIC_API_KEY"}
        env.update(
            CLAUDE_ACC_CREDITS_ORG=row["org_id"],
            CLAUDE_ACC_CREDITS_EMAIL=email,
            CLAUDE_ACC_CREDITS_SCOPE=row["scope"],
            CLAUDE_ACC_CREDITS_PURPOSE=purpose,
        )
        run_id = None
        if no_env:
            # klucz poda helper (apiKeyHelper) organizacji z CLAUDE_ACC_CREDITS_ORG; tu go nie czytamy
            if not key_exists(email):
                mark_error(email, "brak klucza w Pęku kluczy: claude-acc credits add <email> --scope ... --new-key")
                continue
            run_id = os.urandom(8).hex()
            env["CLAUDE_ACC_CREDITS_RUN"] = run_id
        else:
            key = key_read(email)
            if not key:
                mark_error(email, "brak klucza w Pęku kluczy: claude-acc credits add <email> --scope ... --new-key")
                continue
            env["ANTHROPIC_API_KEY"] = key
        mode = "helper" if no_env else "env"
        log(f"exec {purpose} ({mode}): {email} ({row['org_id']}, zostało {money(row['remaining_usd'])}) {os.path.basename(command[0])}")
        try:
            code, exhausted = run_watched(command, env)
        except FileNotFoundError:
            print(f"nie ma polecenia: {command[0]}", file=sys.stderr)
            return 127
        except PermissionError:
            print(f"nie można uruchomić: {command[0]}", file=sys.stderr)
            return 126
        finally:
            env = None
        if run_id:
            marker = os.path.join(runs_dir(), run_id)
            if os.path.exists(marker):
                os.unlink(marker)
            else:
                # bez apiKeyHelper komenda płaciła czymś innym (subskrypcją), nie kredytem
                print(
                    "uwaga: helper kredytów nie został wywołany; ustaw apiKeyHelper na "
                    f"`claude-acc credits helper --purpose {purpose}` (README, sekcja API credits)",
                    file=sys.stderr,
                )
                log(f"exec {purpose}: helper nie był wywołany")
        if exhausted and code != 0:
            mark_exhausted(email)
            print(
                f"kredyt organizacji {row['org_id']} skończył się w trakcie (credit balance is too low): "
                "komenda przerwana, mogła zostawić efekty uboczne; nie ponawiaj automatycznie",
                file=sys.stderr,
            )
            return EXHAUSTED
        return code
    return no_credit(data, scope, org, minimum)


def cmd_helper(args):
    """apiKeyHelper dla Claude Code i Agent SDK: klucz na stdout, tylko dla procesu, który go woła."""
    purpose, scope, org, minimum = pick_options(args)
    no_leftovers(args)
    if sys.stdout.isatty():
        raise UsageError("helper oddaje klucz tylko jako apiKeyHelper (Claude Code, Agent SDK), nie do terminala")
    pinned = org or os.environ.get("CLAUDE_ACC_CREDITS_ORG")
    if minimum is None:
        # przypięta organizacja: Claude Code woła helper także w trakcie biegu (po TTL i po 401),
        # więc wystarczy, że kredyt się nie skończył; bez przypięcia ten sam próg co exec
        minimum = 0.01 if pinned else DEFAULT_MIN_REMAINING
    data = overview()
    for row in targets(data, scope, pinned, minimum):
        key = key_read(row["email"])
        if not key:
            mark_error(row["email"], "brak klucza w Pęku kluczy: claude-acc credits add <email> --scope ... --new-key")
            continue
        run_id = os.environ.get("CLAUDE_ACC_CREDITS_RUN") or ""
        if re.fullmatch(r"[0-9a-f]{16}", run_id):
            os.makedirs(runs_dir(), mode=0o700, exist_ok=True)
            os.close(os.open(os.path.join(runs_dir(), run_id), os.O_CREAT | os.O_WRONLY, 0o600))
        log(f"helper {purpose}: {row['email']} ({row['org_id']})")
        sys.stdout.write(key)
        sys.stdout.flush()
        return 0
    return no_credit(data, scope, pinned, minimum)


def cmd_key(args):
    as_json = switch(args, "--json")
    purpose, scope, org, minimum = pick_options(args)
    no_leftovers(args)
    minimum = DEFAULT_MIN_REMAINING if minimum is None else minimum
    data = overview()
    for row in targets(data, scope, org, minimum):
        if not key_exists(row["email"]):
            mark_error(row["email"], "brak klucza w Pęku kluczy: claude-acc credits add <email> --scope ... --new-key")
            continue
        ref = {
            "service": KEYCHAIN_SERVICE,
            "account": row["email"],
            "org_id": row["org_id"],
            "scope": row["scope"],
            "remaining_usd": row["remaining_usd"],
            "cycle_resets_at": row["cycle_resets_at"],
        }
        log(f"key {purpose}: {row['email']} ({row['org_id']})")
        if as_json:
            print(json.dumps(ref, ensure_ascii=False))
        else:
            print(f"Pęk kluczy: usługa {KEYCHAIN_SERVICE}, konto {row['email']} (organizacja {row['org_id']}, zostało {money(row['remaining_usd'])})")
        return 0
    return no_credit(data, scope, org, minimum)


COMMANDS = {
    "status": cmd_status, "add": cmd_add, "pending": cmd_pending, "remove": cmd_remove, "balance": cmd_balance,
    "record": cmd_record, "exec": cmd_exec, "helper": cmd_helper, "key": cmd_key,
}  # fmt: skip
USAGE = "\n".join(line for line in __doc__.splitlines() if line.startswith("    claude-acc credits"))


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] not in COMMANDS:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return COMMANDS[argv[0]](list(argv[1:]))
    except UsageError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        return 2
    except CreditsError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
