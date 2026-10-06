#!/usr/bin/env python3
"""Zarządzanie limitami kont Claude Code trzymanych przez Orca.

Konto Claude Code to po prostu zawartość wpisu w Pęku kluczy. Orca trzyma
kopię każdego konta pod usługą "Orca Claude Code Managed Credentials"
(nazwa konta = uuid konta w Orca), a każdy katalog konfiguracji ma swój wpis
runtime: "Claude Code-credentials-<sha256(katalog)[:8]>" plus wpis bazowy
"Claude Code-credentials". Przełączenie konta to podmiana wpisu runtime,
dokładnie to samo, co robi menu kont w Orca.

Warunek: w Orca musi być wybrany tryb "System default". Przy wybranym koncie
zarządzanym Orca wymusza swoje konto przy starcie terminala i co 15 minut.

Komendy:
  status [--json]   limity wszystkich kont, kolejka do palenia (--json dla aplikacji w pasku menu)
  who               konto aktywne w zarządzanym katalogu i prognoza
  plan              kolejność, w jakiej warto palić konta, z uzasadnieniem
  heal [--deep]     odzyskaj konta z martwym tokenem (--deep skanuje cały Pęk kluczy)
  switch <email>    przełącz na konkretne konto
  switch --auto     przełącz na następne z kolejki
  login <email>     zaloguj konto ponownie w przeglądarce, bez Orca i terminala
  tick              jeden przebieg pilnowania (uruchamiany przez launchd)
  resume            zdejmij pauzę limitów ręcznie (do czasu, aż limity wrócą)
  pause [on|off]    pauza limitów: stan, włącz albo wyłącz (domyślnie wyłączona)
  depot [--force]   token sandboxów `depot claude`: konto i ważność, --force wysyła od nowa
  depot --fallback  zapisz długi token z `claude setup-token` na wypadek braku konta z zapasem
  token [--json] [--min-minutes N]
                    token OAuth dla procesów spoza sesji (evale `claude -p`): konto z
                    największym zapasem poza lokalnym, w miarę możliwości inne niż Depot
  token --active    token konta aktywnego w zarządzanym katalogu (tylko odczyt, bez
                    odświeżania: token aktywnego rotują sesje); idzie za przełączeniem konta
  token --fallback  zapisz długi token z `claude setup-token` dla `token` bez konta z zapasem
  watch [sekundy]   pętla ticków na pierwszym planie
"""

import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
ORCA_DIR = os.path.join(HOME, "Library/Application Support/orca")
ACCOUNTS_DIR = os.path.join(ORCA_DIR, "claude-accounts")
STATE_DIR = os.path.join(HOME, ".local/share/claude-acc")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
HISTORY_PATH = os.path.join(STATE_DIR, "history.jsonl")
LOG_PATH = os.path.join(STATE_DIR, "switch.log")
LOCK_PATH = os.path.join(STATE_DIR, "lock")
LOGIN_LOCK_PATH = os.path.join(STATE_DIR, "login.lock")
USAGE_CACHE_PATH = os.path.join(STATE_DIR, "usage-cache.json")
# istnieje tylko w trakcie pauzy limitów; hook w sesjach Claude Code (hook.py) tylko go czyta
PAUSE_PATH = os.path.join(STATE_DIR, "pause.json")
# znaczniki hooka z bieżącego epizodu pauzy; kasowane razem z nią
PAUSE_MARKS_DIR = os.path.join(STATE_DIR, "pause-marks")
# ile token aktywnego konta musi być przeterminowany, zanim automat sam go odświeży:
# wcześniej robią to sesje Claude Code i drugi odświeżający zabija konto
IDLE_REFRESH_AFTER = 15 * 60
# kolejne przerwy po 429 z endpointu limitów; sukces zeruje licznik
BACKOFF_STEPS = [120, 240, 480, 900]

MANAGED_SERVICE = "Orca Claude Code Managed Credentials"
ACTIVE_SERVICE = "Claude Code-credentials"
KEYCHAIN_USER = os.environ.get("USER", "user")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-code/2.1.0"
LOG_MAX_BYTES = 512 * 1024
# o ile najstarsza próbka historii może wyjść poza history_keep_hours, zanim przytniemy plik
HISTORY_TRIM_SLACK = 3600

DEFAULT_CONFIG = {
    # katalog konfiguracji, którym zarządzamy (ten, w którym pracujesz na co dzień)
    "config_dir": os.path.join(HOME, ".claude"),
    # pozostałe katalogi, których wpisów NIE przełączamy, ale które trzeba
    # aktualizować przy odświeżeniu tokenu, żeby ich sesje nie padły
    "other_config_dirs": [],
    # przełączamy dopiero, gdy konto realnie padło
    "hard_session_left": 5,
    "hard_weekly_left": 3,
    # kandydat musi mieć przynajmniej tyle zapasu (nigdy ostrzej niż progi wyżej)
    "min_weekly_left": 8,
    "min_session_left": 15,
    # konta brane dopiero, gdy nie ma innego wyjścia (firmowe na końcu)
    "last_resort": [],
    # konta całkiem wyłączone z rotacji
    "never": [],
    "history_keep_hours": 48,
    # pauza limitów: gdy żadne konto nie ma zapasu, sesje kończą krok i czekają na budzik.
    # Opcjonalna (`claude-acc pause on|off`): bez niej sesje pracują do ściany limitu, Claude
    # Code wznawia je sam po resecie, a budzik watch-wall po przełączeniu konta
    "limit_pause": False,
    # sandboxy Claude Code w Depot (`depot claude`) biorą CLAUDE_CODE_OAUTH_TOKEN z sekretu
    # organizacji; tick trzyma tam konto z największym zapasem poza kontem lokalnym
    "depot_sync": True,
    # ile godzin ważności musi mieć token wysłany do Depot; krótszy wymieniamy
    "depot_min_valid_hours": 4,
    # ścieżka CLI Depot; pusta = szukaj na PATH i w Homebrew
    "depot_bin": "",
}

DEPOT_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
DEPOT_FALLBACK_SERVICE = "Claude Acc Depot fallback token"
TOKEN_FALLBACK_SERVICE = "Claude Acc token fallback"


# ---------- drobne narzędzia ----------

def log(line):
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
        tail = open(LOG_PATH).readlines()[-500:]
        open(LOG_PATH, "w").writelines(tail)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def notify(title, text):
    subprocess.run(["osascript", "-e",
                    f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
                   capture_output=True)


def parse_ts(value):
    """Czas z API na obiekt świadomy strefy. Python 3.9 nie łyka sufiksu Z."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    return datetime.fromisoformat(text)


def minutes_until(value):
    ts = parse_ts(value)
    return None if ts is None else (ts - datetime.now(timezone.utc)).total_seconds() / 60


def human_left(minutes):
    if minutes is None:
        return "-"
    h, m = divmod(int(max(minutes, 0)), 60)
    return f"{h // 24}d {h % 24}h" if h >= 24 else f"{h}h {m}m"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH)))
    # kandydat nigdy ostrzej niż moment porzucenia, inaczej powstaje martwa strefa
    cfg["min_weekly_left"] = max(cfg["min_weekly_left"], cfg["hard_weekly_left"] + 1)
    cfg["min_session_left"] = max(cfg["min_session_left"], cfg["hard_session_left"] + 1)
    return cfg


def load_state():
    return json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}


def write_json(path, data, **kwargs):
    """Zapis przez plik tymczasowy: przerwany proces nie zostawi uciętego JSON-a."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    # json.dumps i jeden zapis: te same bajty co json.dump, ale bez kodera w czystym Pythonie
    # (zmierzone na 27 KB stanu: 0,86 -> 0,24 ms na 3.9, 0,79 -> 0,26 ms na 3.14)
    text = json.dumps(data, **kwargs)
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def save_state(state):
    write_json(STATE_PATH, state, indent=1)


def update_state(**fields):
    """Zmiana kilku pól na świeżym odczycie stanu.

    Stan trzymany w pamięci przez cały przebieg i zapisany na końcu kasował to,
    co w międzyczasie zapisały inne funkcje: wycofanie po 429 i znaczniki kont
    do zalogowania, przez co powiadomienia wracały co dwie minuty.
    """
    state = load_state()
    state.update(fields)
    save_state(state)


def take_lock(wait=0):
    """Jeden przebieg naraz. Bez tego dwa procesy potrafią sobie nadpisać tokeny.

    Przy wait > 0 czekamy tyle sekund na zwolnienie blokady, zamiast od razu
    się poddawać: tak działają polecenia z aplikacji w pasku menu.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            time.sleep(0.5)


_TOOLS = {}


def tool(name):
    """Pełna ścieżka narzędzia z PATH, szukana raz na przebieg. Z pełną ścieżką i bez
    close_fds subprocess startuje proces przez posix_spawn zamiast forka interpretera:
    około 2 ms mniej na wywołanie (zmierzone 10,8 -> 8,9 ms). Deskryptorów Pythona dziecko
    i tak nie dziedziczy (PEP 446), więc blokada przebiegu zostaje w tym procesie."""
    if name not in _TOOLS:
        _TOOLS[name] = shutil.which(name) or name
    return _TOOLS[name]


def spawn(argv, **kwargs):
    return subprocess.run([tool(argv[0])] + argv[1:], capture_output=True, text=True, close_fds=False,
                          check=False, **kwargs)


# ---------- Pęk kluczy ----------

# Ostatnio widziana zawartość wpisów w tym przebiegu: (usługa, konto) -> blob albo None.
# Czyta z niej tylko kc_peek, czyli rozpoznawanie kont; każda decyzja, która coś zapisuje
# albo odświeża token, czyta przez kc_read na świeżo, tak jak przedtem.
_KC_SEEN = {}


def kc_read(service, account):
    r = spawn(["security", "find-generic-password", "-s", service, "-a", account, "-w"], timeout=30)
    value = r.stdout.strip() if r.returncode == 0 else None
    _KC_SEEN[(service, account)] = value
    return value


def kc_peek(service, account):
    """Wpis taki, jakim widział go ten przebieg, a gdy go jeszcze nie czytał, świeży odczyt.

    Panel czytał Pęk kluczy 32 razy na odświeżenie (po ~20 ms); 13 z tych odczytów to wpisy
    przeczytane chwilę wcześniej przez ten sam przebieg, potrzebne tylko do rozpoznania konta:
    które jest aktywne, czyj token trzymają sesje, które jest odstawione. Do tego wystarcza to,
    co już widzieliśmy; zapisy i odświeżenia tokenów dalej czytają przez kc_read.
    """
    key = (service, account)
    return _KC_SEEN[key] if key in _KC_SEEN else kc_read(service, account)


def kc_read_many(keys, batch=8):
    """Kilka wpisów naraz: `security` czeka głównie na securityd, więc 12 odczytów równolegle
    trwa ~100 ms zamiast ~250 ms po kolei. Wyniki lądują w _KC_SEEN tak jak z kc_read."""
    keys = list(keys)
    for start in range(0, len(keys), batch):
        procs = []
        try:
            for service, account in keys[start:start + batch]:
                argv = [tool("security"), "find-generic-password", "-s", service, "-a", account, "-w"]
                procs.append(((service, account), subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, close_fds=False)))
            deadline = time.time() + 30
            for key, proc in procs:
                out, _ = proc.communicate(timeout=max(deadline - time.time(), 0.1))
                _KC_SEEN[key] = out.strip() if proc.returncode == 0 else None
        finally:
            for _, proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def kc_write(service, account, contents):
    _KC_SEEN.pop((service, account), None)  # po nieudanym albo przerwanym zapisie nie wiadomo, co tam leży
    r = spawn(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", contents], timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"zapis do Keychain nieudany ({service}): {r.stderr.strip()}")
    _KC_SEEN[(service, account)] = contents


def kc_delete(service, account):
    _KC_SEEN.pop((service, account), None)
    spawn(["security", "delete-generic-password", "-s", service, "-a", account], timeout=30)


def scoped_service(config_dir):
    return f"{ACTIVE_SERVICE}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


def runtime_services(cfg):
    """Wszystkie wpisy runtime: zarządzany katalog, pozostałe katalogi i bazowy."""
    services = [scoped_service(cfg["config_dir"])]
    services += [scoped_service(d) for d in cfg["other_config_dirs"]]
    services.append(ACTIVE_SERVICE)
    return services


def orca_selected_id():
    """Konto wybrane w menu Orca (tryb kont zarządzanych) albo None przy "System default".

    Ta sama reguła co w Orca: activeClaudeManagedAccountIdsByRuntime.host, a gdy
    go brak, activeClaudeManagedAccountId; "System default" zapisuje tam null.
    Orca trzyma ustawienia per profil, więc czytamy najświeższy orca-data.json.
    """
    paths = glob.glob(os.path.join(ORCA_DIR, "profiles", "*", "orca-data.json"))
    paths += [p for p in [os.path.join(ORCA_DIR, "orca-data.json")] if os.path.exists(p)]
    if not paths:
        return None
    try:
        settings = json.load(open(max(paths, key=os.path.getmtime))).get("settings") or {}
    except (ValueError, OSError):
        return None
    host = (settings.get("activeClaudeManagedAccountIdsByRuntime") or {}).get("host")
    if host is None:
        host = settings.get("activeClaudeManagedAccountId")
    return host if isinstance(host, str) and host else None


def orca_selected(accounts):
    """E-mail konta wybranego w Orca albo None. W tym trybie Orca cofa nasze przełączenia
    i sama odświeża tokeny, więc automat, który by się z nią przepychał, wylogowuje konta."""
    selected = orca_selected_id()
    if not selected:
        return None
    return next((a.email for a in accounts if a.id == selected), selected)


def live_services(cfg):
    """Wpisy, z których mogą czytać sesje zarządzanego katalogu.

    Claude Code uruchomione bez CLAUDE_CONFIG_DIR używa wpisu bazowego, a z tą
    zmienną wpisu z hashem katalogu. Sesje odświeżają token tylko w swoim wpisie,
    więc patrzymy na oba i wierzymy temu, który odświeżono ostatnio.
    """
    return [ACTIVE_SERVICE, scoped_service(cfg["config_dir"])]


def freshest_runtime(cfg):
    """Blob z wpisu runtime z najpóźniejszą ważnością: tam leży ostatnie odświeżenie sesji."""
    blobs = [kc_read(s, KEYCHAIN_USER) for s in live_services(cfg)]
    blobs = [b for b in blobs if oauth_of(b).get("accessToken")]
    return max(blobs, key=lambda b: oauth_of(b).get("expiresAt", 0), default=None)


def oauth_of(creds_json):
    try:
        return json.loads(creds_json).get("claudeAiOauth", {})
    except (ValueError, AttributeError, TypeError):  # TypeError: brak wpisu w Pęku kluczy
        return {}


def with_oauth(creds_json, oauth):
    """Blob danych logowania z podmienionym wyłącznie kontem Claude.

    Obok `claudeAiOauth` leżą tokeny serwerów MCP (`mcpOAuth`), które należą do
    katalogu konfiguracji, a nie do konta. Nadpisanie całego blobu wylogowywało
    sesje ze wszystkich serwerów MCP, więc zawsze podmieniamy tylko konto.
    """
    try:
        blob = json.loads(creds_json) if creds_json else {}
    except ValueError:
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    blob["claudeAiOauth"] = oauth
    return json.dumps(blob, separators=(",", ":"))


def http(url, data=None, headers=None):
    """curl przez stdin, żeby token nie trafił do listy procesów."""
    lines = [f'url = "{url}"', "silent", "max-time = 20", 'write-out = "\\n%{http_code}"']
    lines += [f'header = "{k}: {v}"' for k, v in (headers or {}).items()]
    if data is not None:
        lines.append(f'data = "{data}"')
    r = spawn(["curl", "-K", "-"], input="\n".join(lines), timeout=40)
    body, _, code = r.stdout.rpartition("\n")
    try:
        return int(code), json.loads(body)
    except ValueError:
        return int(code or 0), None


# ---------- konta ----------

class Account:
    def __init__(self, acct_id, email):
        self.id = acct_id
        self.email = email

    @property
    def creds_json(self):
        """Zawsze świeży odczyt: inny proces mógł w międzyczasie obrócić token."""
        return kc_read(MANAGED_SERVICE, self.id)

    @property
    def oauth(self):
        return oauth_of(self.creds_json)

    @property
    def seen_oauth(self):
        """Konto z odczytu, który ten przebieg już zrobił: tylko do rozpoznawania, patrz kc_peek."""
        return oauth_of(kc_peek(MANAGED_SERVICE, self.id))

    def __repr__(self):
        return f"<{self.email}>"


def load_accounts():
    if not os.path.isdir(ACCOUNTS_DIR):
        return []
    found = [(acct_id, os.path.join(ACCOUNTS_DIR, acct_id, "auth", "oauth-account.json"))
             for acct_id in sorted(os.listdir(ACCOUNTS_DIR))]
    found = [(acct_id, info) for acct_id, info in found if os.path.exists(info)]
    # wpisy wszystkich kont naraz: i tak zaraz są potrzebne do ustalenia aktywnego konta
    kc_read_many((MANAGED_SERVICE, acct_id) for acct_id, _ in found)
    return [Account(acct_id, json.load(open(info)).get("emailAddress"))
            for acct_id, info in found if _KC_SEEN.get((MANAGED_SERVICE, acct_id))]


def propagate(old_refresh, new_creds_json, cfg):
    """Wstawia odświeżone dane wszędzie, gdzie leżał stary token tego konta.

    Odświeżenie wymienia refresh token i unieważnia stary. Gdyby zaktualizować
    tylko kopię w Orca, sesja czytająca wpis runtime zostałaby z martwym tokenem
    i przy najbliższym odświeżeniu poprosiłaby o ponowne logowanie. Dotyczy to
    także pozostałych katalogów konfiguracji, których wpisów nigdy nie przełączamy.
    """
    oauth = oauth_of(new_creds_json)
    for service in runtime_services(cfg):
        current = kc_read(service, KEYCHAIN_USER)
        if current and oauth_of(current).get("refreshToken") == old_refresh:
            kc_write(service, KEYCHAIN_USER, with_oauth(current, oauth))
            log(f"propagacja odświeżonego tokenu do wpisu {service}")


def token_mark(creds_json):
    """Skrót refresh tokenu: po nim poznajemy, że konto dostało nowe dane logowania."""
    refresh = oauth_of(creds_json or "").get("refreshToken") or ""
    return hashlib.sha256(refresh.encode()).hexdigest()[:12]


def mark_needs_login(account, failed_creds_json):
    """Martwy token odświeżający: konto odstawiamy i mówimy o tym raz.

    Bez tego tick dobijał się do takiego konta co dwie minuty i zapisywał setki
    identycznych linii błędu, a użytkownik i tak nie wiedział, że ma je zalogować.
    Zapamiętujemy też, który token umarł, żeby ponowne logowanie zdjęło blokadę
    od razu, a nie po kilku godzinach.
    """
    state = load_state()
    marks = state.setdefault("needs_login", {})
    if account.email not in marks:
        log(f"konto {account.email} wymaga ponownego logowania")
        notify("Claude: konto wypadło", f"{account.email} wymaga ponownego logowania (Claude Acc w pasku menu)")
    marks[account.email] = int(time.time())
    # hasz tokenu, który naprawdę padł: ponowny odczyt Pęku kluczy potrafił złapać
    # świeży token innego procesu i zablokować zdrowe konto na kilka godzin
    state.setdefault("needs_login_token", {})[account.email] = token_mark(failed_creds_json)
    save_state(state)


def clear_needs_login(account):
    state = load_state()
    token = state.get("needs_login_token", {}).pop(account.email, None)
    if state.get("needs_login", {}).pop(account.email, None) is not None:
        log(f"konto {account.email} znowu działa")
    elif token is None:
        return  # nie było czego zdejmować, oszczędzamy zapis
    save_state(state)


def needs_login(account, retry_after_hours=6, seen=False):
    """Czy konto jest odstawione. Nowe dane logowania (z Orca albo z `login`)
    zdejmują blokadę od razu, a co kilka godzin i tak dajemy mu jeszcze jedną szansę.

    seen=True porównuje z wpisem, który ten przebieg już czytał (kolejka i panel: najwyżej
    pominą konto zalogowane w trakcie przebiegu); tick przed przełączeniem czyta na świeżo.
    """
    state = load_state()
    stamp = state.get("needs_login", {}).get(account.email)
    if not stamp or time.time() - stamp >= retry_after_hours * 3600:
        return False
    dead = state.get("needs_login_token", {}).get(account.email)
    creds = kc_peek(MANAGED_SERVICE, account.id) if seen else account.creds_json
    return dead is not None and dead == token_mark(creds)


def ensure_fresh(account, cfg, force=False):
    """Odświeża token konta, gdy wygasa, i rozsyła go do wszystkich kopii."""
    creds_json = account.creds_json
    if not creds_json:
        return None, "brak danych w Keychain"
    oauth = oauth_of(creds_json)
    if not force and oauth.get("expiresAt", 0) > (time.time() + 300) * 1000:
        return creds_json, "token ważny"
    old_refresh = oauth.get("refreshToken")
    body = f"grant_type=refresh_token&refresh_token={old_refresh}&client_id={CLIENT_ID}"
    status, resp = http(TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    if status != 200 or not resp or not resp.get("access_token"):
        if status == 400:
            mark_needs_login(account, creds_json)
            return None, "wymaga ponownego logowania"
        return None, f"odświeżenie tokenu nieudane (HTTP {status})"
    clear_needs_login(account)
    creds = json.loads(creds_json)
    fresh = dict(creds["claudeAiOauth"])
    fresh["accessToken"] = resp["access_token"]
    if isinstance(resp.get("expires_in"), (int, float)):
        fresh["expiresAt"] = int(time.time() * 1000) + int(resp["expires_in"] * 1000)
    if resp.get("refresh_token"):
        fresh["refreshToken"] = resp["refresh_token"]
    if resp.get("scope"):
        fresh["scopes"] = resp["scope"].split(" ")
    creds["claudeAiOauth"] = fresh
    new_json = json.dumps(creds, separators=(",", ":"))
    kc_write(MANAGED_SERVICE, account.id, new_json)
    propagate(old_refresh, new_json, cfg)
    return new_json, "token odświeżony"


def fetch_usage(access_token):
    """Odpytanie API o limity, z hamulcem na 429.

    Endpoint limitów jest liczony na adres IP, a osiem kont razy kilka procesów
    potrafi go zdławić. Po 429 wstrzymujemy zapytania na kwadrans, zamiast
    dobijać się dalej i przedłużać blokadę.
    """
    until = load_state().get("api_backoff_until", 0)
    if time.time() < until:
        return 429, None
    status, data = http(USAGE_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USER_AGENT,
    })
    if status == 429:
        # narastające przerwy: stałe 15 minut oślepiało automat, a API wracało po paru
        step = min(load_state().get("api_backoff_step", 0), len(BACKOFF_STEPS) - 1)
        update_state(api_backoff_until=time.time() + BACKOFF_STEPS[step], api_backoff_step=step + 1)
        log(f"API limitów zwróciło 429, wstrzymuję odpytywanie na {BACKOFF_STEPS[step] // 60} min")
    elif status == 200:
        state = load_state()
        if "api_backoff_until" in state or "api_backoff_step" in state:
            state.pop("api_backoff_until", None)
            state.pop("api_backoff_step", None)
            save_state(state)
    return status, data


def settled(data):
    """Stare limity po resecie okna: zużycie wraca do zera, czas resetu przepada."""
    out = dict(data)
    for key in ("five_hour", "seven_day"):
        window = out.get(key) or {}
        reset = parse_ts(window.get("resets_at"))
        if reset and reset.timestamp() <= time.time():
            out[key] = dict(window, utilization=0.0, resets_at=None)
    return out


def cached_usage(account, cfg, max_age=90, refresh=True, stale_ok=None):
    """Limity z krótką pamięcią podręczną: kilka komend pod rząd nie mnoży zapytań.

    Bez odświeżania (panel w pasku menu) przy nieudanym odczycie oddajemy
    ostatnie znane liczby: lepsze stare dane z wiekiem niż pusty wiersz.
    """
    hit = load_json(USAGE_CACHE_PATH, {}).get(account.email)
    if hit and time.time() - hit["ts"] <= max_age:
        return settled(hit["data"]), "z pamięci podręcznej"
    data, note = usage(account, cfg, refresh)
    if data:
        cache = load_json(USAGE_CACHE_PATH, {})
        cache[account.email] = {"ts": time.time(), "data": data}
        write_json(USAGE_CACHE_PATH, cache)
        return data, note
    if hit and (not refresh if stale_ok is None else stale_ok):
        return settled(hit["data"]), f"dane z pamięci ({note})"
    return None, note


def usage(account, cfg, refresh=True):
    """Limity konta. Zwraca (dane, notatka) albo (None, powód błędu).

    Token bywa unieważniony przed czasem, gdy odświeżyła go sesja pracująca w
    innym katalogu konfiguracji. Wtedy data ważności kłamie, więc przy 401
    wymuszamy odświeżenie i próbujemy jeszcze raz.

    refresh=False nigdy nie odświeża tokenu. Tak czyta panel w pasku menu: pyta
    co minutę, a każde odświeżenie obraca refresh token, którego właścicielem
    bywa działająca sesja. Odświeżanie zostaje przy automacie i przełączaniu.
    """
    if refresh:
        creds_json, note = ensure_fresh(account, cfg)
        if not creds_json:
            return None, note
    else:
        creds_json, note = account.creds_json, "bez odświeżania"
        oauth = oauth_of(creds_json)
        if not oauth.get("accessToken") or oauth.get("expiresAt", 0) <= time.time() * 1000:
            return None, "token wygasł, odświeży się przy przełączeniu"
    status, data = fetch_usage(oauth_of(creds_json)["accessToken"])
    if status == 401 and refresh:
        creds_json, note = ensure_fresh(account, cfg, force=True)
        if not creds_json:
            return None, note
        status, data = fetch_usage(oauth_of(creds_json)["accessToken"])
    if status != 200 or not data:
        return None, f"odczyt limitów nieudany (HTTP {status})"
    clear_needs_login(account)  # token odpowiedział, więc stary znacznik martwego tokenu kłamie
    return data, note


def headroom(data):
    """Ile zostało: (sesja 5h, tydzień), w procentach."""
    return 100 - data["five_hour"]["utilization"], 100 - data["seven_day"]["utilization"]


def profile_request(access_token):
    return http("https://api.anthropic.com/api/oauth/profile", headers={
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USER_AGENT,
    })


def fetch_profile(access_token):
    status, profile = profile_request(access_token)
    return profile if status == 200 and profile else None


def identity(account, cfg, max_age=12 * 3600, refresh=True):
    """Kim naprawdę jest konto w tym wpisie, prosto z API.

    Nazwa katalogu w Orca to tylko etykieta i potrafi kłamać: przy krzyżowym
    nadpisaniu danych logowania wpis konta A trzymał konto B. Profil zwraca
    e-mail, plan i datę startu subskrypcji, więc bierzemy prawdę stamtąd.
    """
    hit = load_state().get("identity", {}).get(account.id)
    if hit and time.time() - hit.get("ts", 0) <= max_age:
        return hit
    if refresh:
        creds, _ = ensure_fresh(account, cfg)
    else:
        creds = account.creds_json
        if oauth_of(creds).get("expiresAt", 0) <= time.time() * 1000:
            return hit
    if not creds:
        return hit
    profile = fetch_profile(oauth_of(creds)["accessToken"])
    if not profile:
        return hit
    org = profile.get("organization") or {}
    info = {
        "ts": int(time.time()),
        "email": (profile.get("account") or {}).get("email"),
        "tier": org.get("rate_limit_tier"),
        "subscription_since": (org.get("subscription_created_at") or "")[:10],
        "status": org.get("subscription_status"),
    }
    state = load_state()  # ensure_fresh mógł w międzyczasie zapisać stan
    state.setdefault("identity", {})[account.id] = info
    save_state(state)
    if info["email"] and info["email"] != account.email:
        log(f"uwaga: wpis {account.email} zawiera konto {info['email']}")
    return info


def next_renewal(since):
    """Kolejna miesięczna rocznica startu subskrypcji."""
    if not since:
        return None
    start = datetime.strptime(since, "%Y-%m-%d")
    today = datetime.now()
    year, month = today.year, today.month
    if today.day >= start.day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    day = min(start.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return datetime(year, month, day)


# ---------- historia i tempo spalania ----------

def record_history(email, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps({
            "ts": int(time.time()),
            "email": email,
            "session_used": data["five_hour"]["utilization"],
            "weekly_used": data["seven_day"]["utilization"],
        }) + "\n")


def read_history(email, minutes, keep_hours):
    """Próbki konta z ostatnich minut, przy okazji przycina plik.

    Tick dopisuje próbki po kolei, więc ostatnie minuty leżą na końcu pliku: czytamy od
    końca do pierwszej próbki starszej od okna o godzinę (zapas na przestawiony zegar),
    zamiast parsować dwie doby próbek przy każdym odczycie panelu.
    """
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH) as f:
        lines = f.read().splitlines(keepends=True)
    now = time.time()
    since = now - minutes * 60
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row["ts"] < since - 3600:
            break
        if row["email"] == email and row["ts"] >= since:
            rows.append(row)
    rows.reverse()
    trim_history(lines, now - keep_hours * 3600)
    return rows


def trim_history(lines, cutoff):
    """Przycina plik, gdy najstarsza próbka wypadła z okna o ponad HISTORY_TRIM_SLACK.

    Przycinanie przy każdym odczycie przepisywało cały plik (~130 KB) co dwie minuty, bo
    tyle trwa, zanim kolejna próbka się zestarzeje: ~94 MB zapisów dziennie. Teraz raz na
    godzinę. Próbki starsze od okna, a jeszcze nieucięte, i tak nie wchodzą do wyników.
    """
    try:
        oldest = json.loads(lines[0])["ts"] if lines else cutoff
    except ValueError:
        oldest = 0  # uszkodzony początek: przycinamy od razu
    if oldest >= cutoff - HISTORY_TRIM_SLACK:
        return
    kept = []
    for line in lines:
        try:
            if json.loads(line)["ts"] >= cutoff:
                kept.append(line)
        except ValueError:
            continue
    with open(HISTORY_PATH, "w") as f:
        f.writelines(kept)


def burn_rate(rows, field):
    """Procenty na godzinę. Spadek wartości to reset okna, czyli brak danych."""
    if len(rows) < 2:
        return None
    hours = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    delta = rows[-1][field] - rows[0][field]
    if hours < 0.05 or delta <= 0:
        return None
    return delta / hours


def time_to_wall(data, rows):
    """Za ile minut konto przestanie odpowiadać, albo None gdy nie wiadomo.

    Okno, które odnowi się przed wypaleniem, nie jest wąskim gardłem, więc
    przy krótkiej sesji i zdrowym tygodniu narzędzie nie przełącza na zapas.
    """
    session_left, weekly_left = headroom(data)
    out = []
    for field, left, reset_at in (("session_used", session_left, data["five_hour"]["resets_at"]),
                                  ("weekly_used", weekly_left, data["seven_day"]["resets_at"])):
        rate = burn_rate(rows, field)
        if not rate:
            continue
        minutes = left / rate * 60
        to_reset = minutes_until(reset_at)
        if to_reset is not None and to_reset < minutes:
            continue
        out.append(minutes)
    return min(out) if out else None


# ---------- aktywne konto ----------

def find_active(accounts, cfg):
    """Które konto leży w zarządzanym wpisie runtime. None, gdy obce dane.

    Dopasowanie wyłącznie po tokenach. Zgadywanie po ostatnio zapisanym stanie
    prowadziło do wpisania danych jednego konta pod uuid drugiego.
    """
    for service in live_services(cfg):
        live = oauth_of(kc_peek(service, KEYCHAIN_USER))
        if not live.get("accessToken"):
            continue
        for a in accounts:
            stored = a.seen_oauth
            if stored.get("refreshToken") == live.get("refreshToken") or stored.get("accessToken") == live.get("accessToken"):
                return a
    # sesja odświeżyła token po ostatniej kopii: tokeny nie pasują do żadnej kopii,
    # więc właściciela mówi API profilu
    return owner_of(freshest_runtime(cfg), accounts)


def owner_of(creds_json, accounts):
    """Konto z Orca, do którego należy ten token, według API profilu. None, gdy obce albo nie wiadomo."""
    token = oauth_of(creds_json).get("accessToken")
    if not token:
        return None
    email = (((fetch_profile(token) or {}).get("account") or {}).get("email") or "").lower()
    if not email:
        return None
    # etykieta z Orca potrafi kłamać, więc liczy się też prawdziwy e-mail z pamięci profilu
    known = load_state().get("identity", {})
    return next((a for a in accounts
                 if email in (a.email.lower(), ((known.get(a.id) or {}).get("email") or "").lower())), None)


def works(creds_json):
    """Czy tym tokenem da się cokolwiek zrobić. Data ważności to za mało.

    Token bywa unieważniony przed czasem, gdy ktoś inny (sesja Claude Code albo
    Orca) odświeżył konto i dostał nową parę. Kopia z późniejszą datą ważności
    potrafiła więc być martwa, a mimo to wygrywała porównanie i kasowała żywy
    token. Dlatego przed każdym zapisem pytamy API.
    """
    return check(creds_json) == 200


def check(creds_json):
    """Odpowiedź API dla tego tokenu: 200 działa, 401 martwy, reszta to brak wiedzy
    (429, sieć), po której niczego nie wolno odświeżać ani oznaczać.

    Pytamy API profilu, a nie endpoint limitów: ten drugi potrafi odpowiadać 429
    przez godzinę i wtedy nie dało się nawet ręcznie przełączyć konta.
    """
    token = oauth_of(creds_json).get("accessToken")
    return profile_request(token)[0] if token else 401


def sync_back(account, cfg):
    """Przepisuje token z runtime do kopii w Orca. Nigdy w drugą stronę.

    Właścicielem żywego tokenu jest sesja Claude Code: to ona go odświeża w
    trakcie pracy. Nasza kopia ma za nią nadążać, a nie odwrotnie.
    """
    runtime_raw = freshest_runtime(cfg)
    managed_raw = account.creds_json
    live, stored = oauth_of(runtime_raw), oauth_of(managed_raw)
    # porównujemy samo konto: tokeny MCP w obu blobach różnią się z założenia
    if not live or live == stored:
        align_runtime(cfg, stored)
        return
    same_pair = live.get("refreshToken") == stored.get("refreshToken")
    if not same_pair and (owner_of(runtime_raw, [account]) is None):
        return  # obce konto albo API milczy: niczego nie kopiujemy w ciemno
    kc_write(MANAGED_SERVICE, account.id, with_oauth(managed_raw, live))
    align_runtime(cfg, live)
    log(f"read-back: token odświeżony przez sesję zapisany do konta {account.email}")


def align_runtime(cfg, oauth):
    """Oba wpisy runtime z tą samą, najnowszą parą: inaczej wpis, którego sesje nie
    odświeżają, trzyma zużyty refresh token i kusi do ponownego użycia."""
    if not oauth.get("accessToken"):
        return
    for service in live_services(cfg):
        current = kc_read(service, KEYCHAIN_USER)
        if current and oauth_of(current) != oauth:
            kc_write(service, KEYCHAIN_USER, with_oauth(current, oauth))


def adopt_runtime(accounts, cfg):
    """Runtime z tokenem, którego nie zna żadna kopia w Orca, zanim go nadpiszemy.

    Zwykle to nasze konto, któremu sesja obróciła token, a kopia została w tyle.
    Przełączenie bez tego kroku kasowało jedyny żywy token i konto wymagało
    ponownego logowania. Obce konto (spoza Orca) zostaje nadpisane, bo o to
    użytkownik prosi, przełączając.
    """
    raw = freshest_runtime(cfg)
    account = owner_of(raw, accounts)
    if account:
        kc_write(MANAGED_SERVICE, account.id, with_oauth(account.creds_json, oauth_of(raw)))
        clear_needs_login(account)
        log(f"żywy token {account.email} z runtime zapisany do jego kopii w Orca")


def switch_to(account, cfg, reason=""):
    creds_json, note = ensure_fresh(account, cfg)
    if not creds_json:
        raise RuntimeError(f"{account.email}: {note}")
    # nigdy nie wkładamy do runtime tokenu, którym nie udało się nic zrobić:
    # tak właśnie kasuje się działającą sesję
    status = check(creds_json)
    if status == 401:
        creds_json, note = ensure_fresh(account, cfg, force=True)
        if not creds_json:
            raise RuntimeError(f"{account.email}: {note}")
        status = check(creds_json)
        if status == 401:
            mark_needs_login(account, creds_json)
            raise RuntimeError(f"{account.email}: token nie działa, zaloguj konto ponownie")
    if status != 200:
        # 429 albo sieć: nie wiemy, czy token żyje, więc go nie ruszamy
        raise RuntimeError(f"API limitów chwilowo nie odpowiada (HTTP {status}), spróbuj za kilka minut")
    oauth = oauth_of(creds_json)
    for service in (scoped_service(cfg["config_dir"]), ACTIVE_SERVICE):
        kc_write(service, KEYCHAIN_USER, with_oauth(kc_read(service, KEYCHAIN_USER), oauth))
    state = load_state()
    state.update({"active_email": account.email, "switched_at": int(time.time()), "hands_off_notified": False})
    save_state(state)
    log(f"przełączono na {account.email} ({note}){'; ' + reason if reason else ''}")


# ---------- kolejka kont ----------

def rank(account, data, cfg):
    """Najwięcej zapasu wygrywa, konto firmowe zawsze na końcu.

    Wcześniejsza wersja ustawiała kolejkę po terminie resetu, żeby nie marnować
    limitu, który i tak przepadnie. W praktyce powodowało to skakanie między
    kontami w trakcie pracy, więc zasada wypadła: prościej i przewidywalniej.
    """
    session_left, weekly_left = headroom(data)
    return (1 if account.email in cfg["last_resort"] else 0, -weekly_left, -session_left)


def all_runtime_blobs():
    """Wszystkie wpisy runtime Claude Code w Pęku kluczy, nie tylko nasze katalogi.

    Sesje uruchamiane z innym CLAUDE_CONFIG_DIR mają własne wpisy i to właśnie
    tam potrafi wylądować świeży token konta, którego kopia w Orca już umarła.
    """
    dump = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True, timeout=60).stdout
    services = sorted({m for m in re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', dump)})
    blobs = {}
    for service in services:
        raw = kc_read(service, KEYCHAIN_USER)
        if raw and oauth_of(raw).get("accessToken"):
            blobs[service] = raw
    return blobs


def fingerprint(data):
    """Znacznik konta: godziny resetów są dla każdego konta inne."""
    return (data["seven_day"]["resets_at"] or "")[:16]


def cmd_heal(cfg, args):
    """Odzyskuje konta, którym token w kopii Orca umarł po rotacji gdzie indziej."""
    lock = take_lock()
    if not lock:
        print("inny przebieg właśnie trwa")
        return 1
    accounts = load_accounts()
    healthy, broken = {}, []
    for a in accounts:
        data, note = usage(a, cfg)
        if data:
            healthy[fingerprint(data)] = a.email
        else:
            broken.append((a, note))
    if not broken:
        print("wszystkie konta odpowiadają, nie ma czego ratować")
        return 0

    state = load_state()
    marks = state.get("fingerprints", {})
    print(f"konta bez odpowiedzi: {', '.join(a.email for a, _ in broken)}")
    orphans = []
    deep = bool(args) and args[0] == "--deep"
    pool = all_runtime_blobs() if deep else {s: kc_read(s, KEYCHAIN_USER) for s in runtime_services(cfg)}
    for service, raw in pool.items():
        if not raw:
            continue
        status, data = fetch_usage(oauth_of(raw)["accessToken"])
        if status != 200 or not data:
            continue
        mark = fingerprint(data)
        if mark in healthy:
            continue  # ten token należy do konta, które i tak działa
        orphans.append((service, raw, mark))

    fixed = 0
    for account, _ in broken:
        wanted = marks.get(account.email)
        match = next((o for o in orphans if o[2] == wanted), None)
        if not match and len(broken) == 1 and len(orphans) == 1:
            match = orphans[0]  # jedno chore konto, jeden bezpański token
        if not match:
            print(f"  {account.email}: nie znalazłem żywego tokenu, zaloguj konto w Orca")
            continue
        kc_write(MANAGED_SERVICE, account.id, with_oauth(account.creds_json, oauth_of(match[1])))
        orphans.remove(match)
        fixed += 1
        print(f"  {account.email}: odzyskany z wpisu {match[0]}")
        log(f"heal: konto {account.email} odzyskane z {match[0]}")
    return 0 if fixed else 1


def survey(accounts, cfg, exclude_id=None, max_age=90, refresh=True):
    """Limity wszystkich kont z oceną przydatności. Błąd odczytu to nie to samo,
    co wypalone konto, więc niesie osobną etykietę."""
    rows = []
    for a in accounts:
        if a.id == exclude_id or a.email in cfg["never"]:
            continue
        if needs_login(a, seen=True):
            rows.append({"account": a, "data": None, "why": "wymaga ponownego logowania",
                         "usable": False, "error": True})
            continue
        may_refresh = refresh(a) if callable(refresh) else refresh
        # Anulowane konto automat pomija. Limitów o nie nie pytamy: API odpowiada
        # 403, a panel pytał co minutę i przybliżał 429 dla reszty kont. Status
        # sprawdzamy w profilu co godzinę (ten endpoint nie dławi), więc po
        # odnowieniu konto szybko samo wraca do rotacji.
        status = (load_state().get("identity", {}).get(a.id) or {}).get("status")
        if status and status != "active":
            status = (identity(a, cfg, max_age=3600, refresh=may_refresh) or {}).get("status")
        if status and status != "active":
            rows.append({"account": a, "data": None, "why": f"subskrypcja: {status}, automat pomija",
                         "usable": False, "error": True, "skipped": True})
            continue
        data, note = cached_usage(a, cfg, max_age=max_age, refresh=may_refresh)
        if not data:
            rows.append({"account": a, "data": None, "why": note, "usable": False, "error": True})
            continue
        session_left, weekly_left = headroom(data)
        usable = weekly_left >= cfg["min_weekly_left"] and session_left >= cfg["min_session_left"]
        why = f"zostało {weekly_left:.0f}% tygodnia, {session_left:.0f}% sesji"
        remember_fingerprint(a.email, data)
        rows.append({
            "account": a, "data": data, "usable": usable, "error": False,
            "why": why, "rank": rank(a, data, cfg),
        })
    return rows


def remember_fingerprint(email, data):
    """Znacznik konta zapisany na czarną godzinę: po nim `heal` rozpozna token."""
    state = load_state()
    marks = state.setdefault("fingerprints", {})
    mark = fingerprint(data)
    if marks.get(email) != mark:
        marks[email] = mark
        save_state(state)


def queue(rows):
    """Kandydaci w kolejności palenia."""
    return sorted([r for r in rows if r["usable"]], key=lambda r: r["rank"])




# ---------- Depot: token sandboxów `depot claude` ----------

def depot_bin(cfg):
    """Ścieżka CLI Depot; launchd startuje z ubogim PATH, więc sprawdzamy też Homebrew."""
    if cfg.get("depot_bin"):
        return cfg["depot_bin"] if os.path.exists(cfg["depot_bin"]) else None
    found = shutil.which("depot")
    if found:
        return found
    return next((p for p in ("/opt/homebrew/bin/depot", "/usr/local/bin/depot") if os.path.exists(p)), None)


def depot_push(depot, token):
    r = subprocess.run([depot, "claude", "secrets", "add", DEPOT_SECRET, "--value", token],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:200]


def token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()[:12]


def depot_sync(accounts, cfg, active, force=False):
    """Trzyma w sekrecie Depot token konta z największym zapasem POZA kontem lokalnym.

    Sandbox `depot claude` czyta CLAUDE_CODE_OAUTH_TOKEN przy starcie sesji, więc token
    musi mieć zapas ważności (depot_min_valid_hours). Wymiana następuje, gdy konto
    sandboxów traci zapas, token dobiega końca albo konto stało się lokalnym: dwie
    strony palące jedno konto wyczerpują je dwa razy szybciej. Odświeżamy wyłącznie
    konta nieaktywne, bo token aktywnego rotują sesje Claude Code. Bez konta z zapasem
    idzie długi token z `depot --fallback`, jeśli jest. Zwraca e-mail konta sandboxów.
    """
    if not cfg.get("depot_sync"):
        return None
    depot = depot_bin(cfg)
    if not depot:
        return None
    state = load_state()
    now = time.time()
    min_valid = cfg["depot_min_valid_hours"] * 3600
    current = next((a for a in accounts if a.email == state.get("depot_email")), None)
    if current and not force and (not active or current.id != active.id) \
            and state.get("depot_expires_at", 0) - now > min_valid:
        data, _ = cached_usage(current, cfg, max_age=600, refresh=False)
        if data:
            session_left, weekly_left = headroom(data)
            if weekly_left >= cfg["min_weekly_left"] and session_left >= cfg["min_session_left"]:
                return current.email  # konto sandboxów niesie, token ważny: nic do roboty

    rows = queue(survey(accounts, cfg, exclude_id=active.id if active else None, max_age=600))
    for row in rows:
        target = row["account"]
        expires = oauth_of(target.creds_json or "").get("expiresAt", 0) / 1000
        creds_json, note = ensure_fresh(target, cfg, force=expires - now < min_valid)
        if not creds_json:
            log(f"depot: {target.email}: {note}")
            continue
        oauth = oauth_of(creds_json)
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt", 0) / 1000
        if not token or expires - now < min_valid:
            continue
        if token_hash(token) == state.get("depot_token_mark") and not force:
            return target.email
        ok, err = depot_push(depot, token)
        if not ok:
            log(f"depot: wysyłka tokenu nieudana: {err}")
            return None
        update_state(depot_email=target.email, depot_expires_at=int(expires),
                     depot_token_mark=token_hash(token), depot_synced_at=int(now))
        log(f"depot: sandboxy na {target.email}, token ważny do {datetime.fromtimestamp(expires):%H:%M}")
        return target.email

    fallback = kc_read(DEPOT_FALLBACK_SERVICE, KEYCHAIN_USER)
    if fallback and (force or state.get("depot_token_mark") != token_hash(fallback)):
        ok, err = depot_push(depot, fallback)
        if ok:
            update_state(depot_email="fallback", depot_expires_at=0,
                         depot_token_mark=token_hash(fallback), depot_synced_at=int(now))
            log("depot: brak konta z zapasem poza lokalnym, sandboxy na tokenie zapasowym")
            return "fallback"
        log(f"depot: wysyłka tokenu zapasowego nieudana: {err}")
    elif not rows:
        log("depot: brak konta z zapasem poza lokalnym, token sandboxów bez zmian")
    return None


def depot_sync_safe(accounts, cfg, active, force=False):
    """Depot to dodatek: jego błąd nie może zatrzymać pilnowania kont."""
    try:
        return depot_sync(accounts, cfg, active, force=force)
    except Exception as err:
        log(f"depot: błąd {err}")
        return None


# ---------- komendy ----------

def window_view(window):
    """Okno limitu dla aplikacji: procent zużycia i reset jako czas unixowy."""
    window = window or {}
    reset = parse_ts(window.get("resets_at"))
    return {"used": window.get("utilization"), "resets_at": reset.timestamp() if reset else None}


def forecast(data, rows, cfg):
    """Dokąd dojdzie aktywne konto w obecnym tempie: zużycie w chwili resetu
    albo godzina, o której automat przełączy konto. None, gdy za mało próbek."""
    out = {}
    now = time.time()
    for key, field, api_key, hard in (("session", "session_used", "five_hour", cfg["hard_session_left"]),
                                       ("weekly", "weekly_used", "seven_day", cfg["hard_weekly_left"])):
        rate = burn_rate(rows, field)
        used = (data.get(api_key) or {}).get("utilization")
        reset = parse_ts((data.get(api_key) or {}).get("resets_at"))
        if not rate or used is None or reset is None:
            out[key] = None
            continue
        at_reset = used + rate * max(reset.timestamp() - now, 0) / 3600
        switch_at = None
        if at_reset >= 100 - hard:
            switch_at = now + max(100 - hard - used, 0) / rate * 3600
        out[key] = {"rate": round(rate, 2), "at_reset": round(min(at_reset, 100), 1), "switch_at": switch_at}
    return out


def snapshot(cfg):
    """Stan wszystkich kont w jednym słowniku. To czyta aplikacja w pasku menu."""
    accounts = load_accounts()
    active = find_active(accounts, cfg)
    # aplikacja pyta co minutę, więc nie odświeża żadnych tokenów (to robią sesje
    # i automat), a świeże limity bierze tylko dla aktywnego konta; reszta z
    # pamięci do 10 minut, bo endpoint limitów dławi 429 i blokuje wtedy automat
    orca = orca_selected(accounts)
    if active and not orca:  # przy koncie wybranym w Orca niczego nie zapisujemy
        sync_back(active, cfg)
    if active:
        cached_usage(active, cfg, max_age=120, refresh=False)
    # Tokeny, które może trzymać jakaś sesja, zostają nietknięte. Konto, którego
    # token leży tylko w kopii Orca, odświeżamy: nikt inny go nie używa, a bez
    # tego panel pokazywał dane sprzed kilkunastu godzin.
    held = {oauth_of(kc_read(s, KEYCHAIN_USER)).get("refreshToken") for s in runtime_services(cfg)}
    idle = lambda a: not orca and a.seen_oauth.get("refreshToken") not in held
    rows = survey(accounts, cfg, max_age=1800, refresh=idle)
    if active and all(r["account"].id != active.id for r in rows):
        # konto z listy "never" też bywa aktywne (np. wybrane ręcznie): pokazujemy je,
        # tylko automat nigdy na nie nie przełącza
        rows += survey([active], dict(cfg, never=[]), max_age=120, refresh=False)
        rows[-1]["usable"] = False
    cache = load_json(USAGE_CACHE_PATH, {})
    order = {r["account"].id: i for i, r in enumerate(queue(rows), 1)}
    now = time.time()
    items = []
    for r in rows:
        a = r["account"]
        who = identity(a, cfg, refresh=False) or {}
        # pominięte konto to nie błąd: ma ostatnie znane limity, a panel mówi dlaczego stoi
        status = "needs_login" if needs_login(a, seen=True) else ("error" if r["error"] and not r.get("skipped") else "ok")
        # przy błędzie odczytu pokazujemy ostatnie znane limity z ich wiekiem
        hit = cache.get(a.email)
        data = r["data"] or (settled(hit["data"]) if hit else None)
        renewal = next_renewal(who.get("subscription_since"))
        items.append({
            "id": a.id,
            "email": a.email,
            "real_email": who.get("email"),
            "tier": (who.get("tier") or "").replace("default_claude_", "").replace("max_", "Max "),
            "active": bool(active and a.id == active.id),
            "last_resort": a.email in cfg["last_resort"],
            "status": status,
            "note": r["why"],
            "usable": r["usable"],
            "queue": order.get(a.id),
            "session": window_view(data.get("five_hour")) if data else None,
            "weekly": window_view(data.get("seven_day")) if data else None,
            "data_age": int(now - hit["ts"]) if hit and data else None,
            # API podaje tylko start subskrypcji, więc to miesięczna rocznica, nie data z rachunku
            "renews_at": renewal.timestamp() if renewal else None,
            "subscription_status": who.get("status"),
            "subscription_since": who.get("subscription_since") or None,
        })
    # najpierw kolejka automatu, potem wypalone od najbliższego resetu tygodnia
    items.sort(key=lambda i: (not i["active"], i["queue"] or 99, i["status"] != "ok",
                              (i["weekly"] or {}).get("resets_at") or 9e12))

    active_row = next((r for r in rows if active and r["account"].id == active.id and r["data"]), None)
    state = load_state()
    return {
        "generated_at": now,
        "active_email": active.email if active else None,
        "foreign_runtime": active is None,
        "thresholds": {"session_left": cfg["hard_session_left"], "weekly_left": cfg["hard_weekly_left"]},
        "forecast": forecast(active_row["data"], read_history(active.email, 60, cfg["history_keep_hours"]), cfg)
        if active_row else None,
        "api_backoff_until": state.get("api_backoff_until"),
        "last_tick": state.get("last_tick"),
        "switched_at": state.get("switched_at"),
        "orca_selected": orca,
        "pause": load_json(PAUSE_PATH, None),
        "accounts": items,
    }


def cmd_status(cfg, args):
    if args and args[0] == "--json":
        lock = take_lock(wait=25)
        if not lock:
            print("inny przebieg trwa zbyt długo, spróbuj za chwilę", file=sys.stderr)
            return 1
        print(json.dumps(snapshot(cfg), ensure_ascii=False))
        return 0
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg trwa zbyt długo, spróbuj za chwilę")
        return 1
    accounts = load_accounts()
    active = find_active(accounts, cfg)
    rows = survey(accounts, cfg)
    print(f"{'konto':<28}{'tydzień':>9}{'sesja 5h':>10}   {'reset tygodnia':<22}{'plan':<8}odnowienie")
    for r in sorted(rows, key=lambda r: r["rank"] if not r["error"] else (9, 9e9, 0, 0)):
        a = r["account"]
        mark = "→" if active and a.id == active.id else " "
        if r["error"]:
            print(f"{mark} {a.email:<26} {r['why']}")
            continue
        who = identity(a, cfg) or {}
        label = who.get("email") or a.email
        if who.get("email") and who["email"] != a.email:
            label += f" (wpis {a.email})"
        session_left, weekly_left = headroom(r["data"])
        resets = minutes_until(r["data"]["seven_day"]["resets_at"])
        tier = (who.get("tier") or "").replace("default_claude_", "").replace("max_", "max ")
        renewal = next_renewal(who.get("subscription_since"))
        flag = "" if r["usable"] else "  (za mało zapasu)"
        print(f"{mark} {label:<26}{weekly_left:>7.0f}% {session_left:>8.0f}%   "
              f"{(datetime.now() + timedelta(minutes=resets or 0)):%d.%m %H:%M} za {human_left(resets):<9}"
              f"{tier:<8}{renewal:%d.%m}{flag}" if renewal else
              f"{mark} {label:<26}{weekly_left:>7.0f}% {session_left:>8.0f}%   "
              f"{(datetime.now() + timedelta(minutes=resets or 0)):%d.%m %H:%M} za {human_left(resets):<9}{tier}{flag}")
    nxt = queue(rows)
    if nxt:
        print(f"\nnastępne w kolejce: {', '.join(r['account'].email for r in nxt[:3])}")
    return 0


def cmd_plan(cfg, _args):
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg trwa zbyt długo, spróbuj za chwilę")
        return 1
    rows = queue(survey(load_accounts(), cfg))
    if not rows:
        print("żadne konto nie ma sensownego zapasu")
        return 1
    print("kolejność palenia (najpierw to, czemu limit najszybciej przepadnie):")
    for i, r in enumerate(rows, 1):
        session_left, weekly_left = headroom(r["data"])
        resets = minutes_until(r["data"]["seven_day"]["resets_at"])
        extra = " [konto firmowe, ostatnia deska ratunku]" if r["account"].email in cfg["last_resort"] else ""
        print(f"{i}. {r['account'].email:<26} tydzień {weekly_left:>3.0f}%, sesja {session_left:>3.0f}%, "
              f"reset za {human_left(resets)}{extra}")
    return 0


def cmd_who(cfg, _args):
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg trwa zbyt długo, spróbuj za chwilę")
        return 1
    accounts = load_accounts()
    active = find_active(accounts, cfg)
    if not active:
        print(f"we wpisie {scoped_service(cfg['config_dir'])} leżą dane spoza Orca, nie ruszam ich")
        return 1
    data, note = cached_usage(active, cfg)
    if not data:
        print(f"{active.email} ({note})")
        return 1
    session_left, weekly_left = headroom(data)
    rows = read_history(active.email, 90, cfg["history_keep_hours"])
    wall = time_to_wall(data, rows)
    rate = burn_rate(rows, "weekly_used")
    print(f"{active.email}: zostało {weekly_left:.0f}% tygodnia, {session_left:.0f}% sesji 5h")
    print(f"  reset tygodnia za {human_left(minutes_until(data['seven_day']['resets_at']))}, "
          f"sesji za {human_left(minutes_until(data['five_hour']['resets_at']))}")
    if rate:
        print(f"  tempo: {rate:.1f}% tygodnia na godzinę")
    print(f"  prognoza ściany: {'za ' + human_left(wall) if wall else 'brak danych, za mało próbek'}")
    return 0


def cmd_switch(cfg, args):
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg właśnie trwa, spróbuj za chwilę")
        return 1
    accounts = load_accounts()
    if not accounts:
        print("brak kont zarządzanych przez Orca")
        return 1
    orca = orca_selected(accounts)
    if orca:
        print(f"Orca ma wybrane konto {orca} i cofnie każde przełączenie. W Orca wybierz System default")
        return 1
    active = find_active(accounts, cfg)
    if active:
        sync_back(active, cfg)
    else:
        adopt_runtime(accounts, cfg)
    if args and args[0] != "--auto":
        target = next((a for a in accounts if a.email == args[0]), None)
        if not target:
            print(f"nie znam konta {args[0]}")
            return 1
        switch_to(target, cfg, "ręcznie")
        depot_sync_safe(accounts, cfg, target)
        print(f"przełączono na {target.email}")
        return 0
    rows = queue(survey(accounts, cfg, exclude_id=active.id if active else None))
    if not rows:
        print("żadne konto nie ma zapasu, sprawdź claude-acc status")
        return 1
    switch_to(rows[0]["account"], cfg, "ręcznie --auto")
    depot_sync_safe(accounts, cfg, rows[0]["account"])
    print(f"przełączono na {rows[0]['account'].email}")
    return 0


def plain(text):
    """Tekst z terminala bez sekwencji ANSI i hiperłączy OSC 8."""
    text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text or "")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).strip()


def cmd_login(cfg, args):
    """Ponowne logowanie konta z Orca, bez terminala i bez klikania w Orca.

    Uruchamia zwykłe `claude auth login` w pustym katalogu konfiguracji, więc to
    Claude Code prowadzi logowanie w przeglądarce i zapisuje dane do własnego,
    tymczasowego wpisu w Pęku kluczy. Stamtąd, po sprawdzeniu w API, czyje to
    konto, przenosimy je do kopii w Orca. Tymczasowy wpis zawsze sprzątamy.
    """
    if not args:
        print("użycie: claude-acc login <email>")
        return 2
    email = args[0]
    target = next((a for a in load_accounts() if a.email == email), None)
    if not target:
        print(f"nie znam konta {email}")
        return 1
    # jedno logowanie naraz: dwa przebiegi dzieliłyby katalog i wpis tymczasowy
    login_lock = open(LOGIN_LOCK_PATH, "w")
    try:
        fcntl.flock(login_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("inne logowanie właśnie trwa, dokończ je albo anuluj")
        return 1
    workdir = os.path.join(STATE_DIR, "login")
    service = scoped_service(workdir)
    kc_delete(service, KEYCHAIN_USER)  # resztka po przerwanym przebiegu wygrałaby ze świeżym logowaniem
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=workdir)
    env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    claude = shutil.which("claude") or os.path.join(HOME, ".local/bin/claude")
    # Anuluj w aplikacji wysyła SIGTERM: wyjątek w run() zabija też proces claude
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        r = subprocess.run([claude, "auth", "login", "--claudeai", "--email", email], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
        raw = kc_read(service, KEYCHAIN_USER)
        creds_file = os.path.join(workdir, ".credentials.json")
        if not raw and os.path.exists(creds_file):
            raw = open(creds_file).read().strip()
    except subprocess.TimeoutExpired:
        print("logowanie przerwane: przez 10 minut nie wróciła odpowiedź z przeglądarki")
        return 1
    finally:
        # od powrotu z przeglądarki do końca zapisu Anuluj już nie przerywa:
        # urwany zapis zostawiłby kopię Orca i runtime w rozjeździe
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        kc_delete(service, KEYCHAIN_USER)
        shutil.rmtree(workdir, ignore_errors=True)

    fresh = oauth_of(raw)
    token = fresh.get("accessToken")
    if r.returncode != 0 or not token or not fresh.get("refreshToken"):
        lines = plain(r.stderr or r.stdout).splitlines()
        print(f"logowanie nieudane: {lines[-1] if lines else f'kod wyjścia {r.returncode}'}")
        return 1
    profile = fetch_profile(token)
    who = ((profile or {}).get("account") or {}).get("email")
    if not who:
        print("nie udało się sprawdzić, na jakie konto się zalogowano, spróbuj jeszcze raz")
        return 1
    if who.lower() != email.lower():
        # nigdy nie zapisujemy cudzego konta pod tym wpisem: tak etykiety w Orca zaczęły kłamać
        print(f"przeglądarka zalogowała {who}, a nie {email}. Wyloguj się z claude.ai albo użyj okna prywatnego")
        return 1

    lock = take_lock(wait=60)
    if not lock:
        print("inny przebieg trwa zbyt długo, zaloguj się jeszcze raz za chwilę")
        return 1
    old_refresh = target.oauth.get("refreshToken")
    kc_write(MANAGED_SERVICE, target.id, with_oauth(target.creds_json, fresh))
    # Wpisy runtime tego konta dostają nowy token, więc sesje wstają same. Które
    # to wpisy, rozstrzyga API profilu, a nie etykieta: wpis Orca potrafi trzymać
    # inne konto, a runtime nowszy token niż kopia.
    for runtime in runtime_services(cfg):
        current = kc_read(runtime, KEYCHAIN_USER)
        live = oauth_of(current)
        if not live.get("accessToken"):
            continue
        owner = ((fetch_profile(live["accessToken"]) or {}).get("account") or {}).get("email")
        if (owner or "").lower() == email.lower() or (owner is None and live.get("refreshToken") == old_refresh):
            kc_write(runtime, KEYCHAIN_USER, with_oauth(current, fresh))
            log(f"login: nowy token {email} wpisany do {runtime}")
    clear_needs_login(target)
    cache = load_json(USAGE_CACHE_PATH, {})
    if cache.pop(email, None) is not None:
        write_json(USAGE_CACHE_PATH, cache)
    log(f"login: konto {email} zalogowane ponownie")
    print(f"zalogowano {email}")
    return 0


def active_usage(account, cfg):
    """Limity aktywnego konta bez wchodzenia sesjom w drogę.

    Token aktywnego konta odświeżają sesje Claude Code, około 5 minut przed końcem
    ważności. Automat robił to samo z własnej kopii i jeden z dwóch zawsze używał
    zużytego refresh tokenu, a serwer unieważniał wtedy całe konto: stąd codzienne
    wylogowania. Teraz automat odświeża sam tylko token przeterminowany od kwadransa
    (sesje śpią), a odrzucony ważny token (401) oznacza konto do zalogowania.
    """
    oauth = account.oauth
    expired_for = time.time() - oauth.get("expiresAt", 0) / 1000
    if expired_for > IDLE_REFRESH_AFTER:
        return cached_usage(account, cfg, max_age=60)
    if expired_for > 0:
        return None, "token właśnie wygasł, czekam, aż odświeży go sesja"
    # stare liczby z pamięci nie mogą decydować o przełączeniu, więc stale_ok=False
    data, note = cached_usage(account, cfg, max_age=60, refresh=False, stale_ok=False)
    if data or check(account.creds_json) != 401:
        return data, note
    sync_back(account, cfg)  # sesja mogła odświeżyć token między odczytami
    if check(account.creds_json) == 401:
        mark_needs_login(account, account.creds_json)
        return None, "token odrzucony przez API"
    return cached_usage(account, cfg, max_age=0, refresh=False, stale_ok=False)


# ---------- pauza limitów ----------
#
# Gdy aktywne konto się kończy, a żadne inne nie ma zapasu, sesje Claude Code
# dostają przez hook czas na punkt kontrolny: kończą krok, zapisują stan i
# czekają, aż budzik je wznowi. Bez tego agenci padali w połowie pracy i trzeba
# ich było odpalać od nowa. Plik pauzy to jedyny kontrakt z hookiem (hook.py).

def clock(epoch):
    """Godzina, a gdy to nie dziś, także dzień: "14:30", "3.10 22:00"."""
    moment = datetime.fromtimestamp(epoch)
    return f"{moment:%H:%M}" if moment.date() == datetime.now().date() else f"{moment.day}.{moment:%m %H:%M}"


def usable_again_at(data, cfg):
    """Kiedy konto znów będzie miało zapas na pracę: najpóźniejszy z resetów
    okien, które je blokują. None, gdy API nie podało czasu resetu."""
    session_left, weekly_left = headroom(data)
    blocking = []
    if session_left < cfg["min_session_left"]:
        blocking.append((data.get("five_hour") or {}).get("resets_at"))
    if weekly_left < cfg["min_weekly_left"]:
        blocking.append((data.get("seven_day") or {}).get("resets_at"))
    resets = [parse_ts(r) for r in blocking]
    if any(r is None for r in resets):
        return None
    return max((r.timestamp() for r in resets), default=time.time())


def start_pause(active, reason, datas, cfg):
    """Ogłasza pauzę albo odświeża jej szacunek wznowienia. Pauza zdjęta ręcznie
    nie wraca, dopóki limity nie odżyją."""
    if load_state().get("pause_dismissed"):
        return
    times = [t for t in (usable_again_at(d, cfg) for d in datas) if t]
    resume_at = int(min(times)) if times else None
    pause = load_json(PAUSE_PATH, None)
    if pause:
        fresh = dict(pause, resume_at=resume_at, reason=reason, account=active.email)
        if fresh != pause:
            write_json(PAUSE_PATH, fresh)
        return
    now = int(time.time())
    write_json(PAUSE_PATH, {"episode": str(now), "since": now, "account": active.email,
                            "reason": reason, "resume_at": resume_at})
    when = f", wznowienie ok. {clock(resume_at)}" if resume_at else ""
    log(f"pauza limitów: {reason}, brak konta z zapasem{when}")
    notify("Claude: pauza limitów", f"Żadne konto nie ma zapasu. Sesje kończą bieżący krok i czekają{when}")


def drop_pause():
    """Kasuje plik pauzy i znaczniki hooka; wstrzymane sesje budzą się, gdy plik znika.
    True, gdy pauza była."""
    if not os.path.exists(PAUSE_PATH):
        return False
    os.remove(PAUSE_PATH)
    shutil.rmtree(PAUSE_MARKS_DIR, ignore_errors=True)
    return True


def end_pause(why):
    """Koniec epizodu: limity wróciły, automat przełączył konto albo przestał pilnować."""
    state = load_state()
    ended = [state.pop(key, None) for key in ("pause_dismissed", "out_of_headroom")]
    if any(ended):
        save_state(state)
    if drop_pause():
        log(f"koniec pauzy limitów: {why}")
        notify("Claude: limity wróciły", "Wstrzymane sesje wznawiają pracę")


def out_of_headroom(reason):
    """Bez pauzy limitów: jedno ostrzeżenie na epizod zamiast pliku pauzy. Sesje pracują
    dalej, a koniec epizodu (end_pause) kasuje znacznik."""
    if load_state().get("out_of_headroom"):
        return
    update_state(out_of_headroom=True)
    log(f"tick: {reason}, brak konta z zapasem (pauza limitów wyłączona)")
    notify("Claude: brak konta z zapasem",
           f"{reason}. Sesje pracują do limitu i wznowią się po resecie albo przełączeniu konta")


def cmd_pause(cfg, args):
    """Włącza albo wyłącza pauzę limitów: zapis `limit_pause` w config.json i od razu
    hooki w settings.json. Wyłączenie w trakcie pauzy budzi wstrzymane sesje."""
    if not args:
        print(f"pauza limitów {'włączona' if cfg['limit_pause'] else 'wyłączona'} (claude-acc pause on|off)")
        return 0
    if args[0] not in ("on", "off"):
        print("użycie: claude-acc pause [on|off]", file=sys.stderr)
        return 2
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg właśnie trwa, spróbuj za chwilę")
        return 1
    on = args[0] == "on"
    saved = load_json(CONFIG_PATH, {})
    saved["limit_pause"] = on
    write_json(CONFIG_PATH, saved)
    state = load_state()
    if state.pop("pause_dismissed", None):
        save_state(state)  # ręczne zdjęcie dotyczyło starego ustawienia
    if not on and drop_pause():
        log("pauza limitów wyłączona w trakcie pauzy, sesje wznawiają pracę")
        print("wstrzymane sesje wznawiają pracę")
    log(f"pauza limitów {'włączona' if on else 'wyłączona'}")
    hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook.py")
    settings = os.path.join(cfg["config_dir"], "settings.json")
    if subprocess.run([sys.executable, hook, "install", settings]).returncode:
        print(f"hooki w {settings} bez zmian, ustawienie zapisane", file=sys.stderr)
        return 1
    print(f"pauza limitów {'włączona' if on else 'wyłączona'}; hooki łapią nowe i wznowione sesje")
    return 0


def cmd_resume(cfg, _args):
    """Ręczne zdjęcie pauzy (przycisk w panelu). Trzyma do końca epizodu: automat
    nie ogłosi jej znowu, dopóki limity nie wrócą i nie skończą się od nowa."""
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg właśnie trwa, spróbuj za chwilę")
        return 1
    if not drop_pause():
        print("pauzy nie ma")
        return 0
    update_state(pause_dismissed=True)
    log("pauza limitów zdjęta ręcznie")
    print("pauza zdjęta, sesje wznawiają pracę")
    return 0


def cmd_tick(cfg, _args):
    """Jeden przebieg pilnowania. Uruchamiany przez launchd co 2 minuty."""
    lock = take_lock(wait=10)
    if not lock:
        return 0
    update_state(last_tick=int(time.time()))  # aplikacja w pasku menu po tym widzi, że automat żyje
    if not cfg["limit_pause"] and drop_pause():
        # pauza wyłączona w config.json w trakcie epizodu: sesje budzą się, gdy plik znika
        log("pauza limitów wyłączona w konfiguracji, sesje wznawiają pracę")
    accounts = load_accounts()
    if not accounts:
        return 1
    orca = orca_selected(accounts)
    if orca:
        # Orca sama pilnuje wybranego konta i odświeża jego token: drugi gracz
        # w tym samym miejscu to wyścig o refresh token i wylogowane konta.
        # Pauzy, której automat już nie zdejmie, nie trzymamy.
        end_pause(f"Orca ma wybrane konto {orca}, automat stoi")
        if load_state().get("orca_notified") != orca:
            log(f"tick: Orca ma wybrane konto {orca}, automat stoi, dopóki w Orca nie będzie System default")
            notify("Claude: automat wstrzymany", f"W Orca wybrane jest {orca}. Wybierz System default.")
            update_state(orca_notified=orca)
        return 0
    if load_state().get("orca_notified"):
        update_state(orca_notified=None)
    state = load_state()
    active = find_active(accounts, cfg)

    if not active:
        # ktoś zalogował się ręcznie albo trwa /login: ręce precz, tylko jedno ostrzeżenie
        end_pause("runtime ma konto spoza Orca")
        if not state.get("hands_off_notified"):
            log("tick: wpis runtime zawiera dane spoza Orca, nie przełączam")
            notify("Claude: nieznane konto", "Runtime ma dane spoza Orca. Automat nic nie zmienia.")
            update_state(hands_off_notified=True)
        return 0
    if state.get("hands_off_notified"):
        update_state(hands_off_notified=False)

    sync_back(active, cfg)
    depot_sync_safe(accounts, cfg, active)
    dead = needs_login(active)
    data, note = (None, "token nie działa") if dead else active_usage(active, cfg)
    if not data and not dead:
        dead = needs_login(active)  # odczyt właśnie trafił na martwy token
    if not data and not dead:
        # odświeżanie tokenu aktywnego konta zostawiamy sesji Claude Code:
        # dwa procesy rotujące ten sam token to pewna droga do wylogowania
        log(f"tick: {active.email}: {note}")  # błąd sieci lub API, nie przełączamy w ciemno
        return 1

    if dead:
        # refresh token padł (400), więc sesje i tak zaraz się wylogują: to jest
        # "konto realnie padło", przechodzimy na konto z zapasem
        reason = f"{active.email}: token nie działa"
        carries = roomy = False
    else:
        record_history(active.email, data)
        session_left, weekly_left = headroom(data)
        reason = f"{active.email}: tydzień {weekly_left:.0f}%, sesja {session_left:.0f}%"
        carries = session_left > cfg["hard_session_left"] and weekly_left > cfg["hard_weekly_left"]
        # pauzę zdejmujemy dopiero przy zapasie, z jakim automat bierze konto, a nie
        # tuż nad progiem porzucenia: inaczej sesje budziłyby się na minutę
        roomy = session_left >= cfg["min_session_left"] and weekly_left >= cfg["min_weekly_left"]
    if roomy:
        end_pause(f"{active.email} ma znowu zapas")
        return 0
    paused = os.path.exists(PAUSE_PATH)
    if carries and not paused:
        return 0  # konto jeszcze niesie, nie ruszamy go
    rows = survey(accounts, cfg, exclude_id=active.id)
    candidates = queue(rows)
    if candidates and carries:
        # trwa pauza, a inne konto odżyło: aktywne jeszcze niesie, więc zostaje,
        # sesje wracają do pracy, a przełączenie przyjdzie przy progu jak zwykle
        end_pause(f"{candidates[0]['account'].email} ma znowu zapas")
        return 0
    if not candidates:
        if not cfg["limit_pause"]:
            out_of_headroom(reason)
            return 1
        if not paused:
            log(f"tick: {reason}, brak konta z zapasem")
        # pauza ogłasza się raz na epizod (log i powiadomienie), kolejne przebiegi
        # tylko odświeżają szacunek wznowienia
        start_pause(active, reason, ([data] if data else []) + [r["data"] for r in rows if r["data"]], cfg)
        return 1

    target = candidates[0]["account"]
    switch_to(target, cfg, reason)
    end_pause(f"przełączono na {target.email}")
    depot_sync_safe(accounts, cfg, target)  # sandboxy nie mogą zostać na nowym koncie lokalnym
    left = headroom(candidates[0]["data"])[1]
    if target.email in cfg["last_resort"]:
        notify("Claude: wchodzę na konto firmowe",
               f"Prywatne konta bez zapasu, przełączam na {target.email}")
    else:
        notify("Claude: zmiana konta", f"{active.email} → {target.email} (zostało {left:.0f}% tygodnia)")
    return 0


def cmd_watch(cfg, args):
    interval = int(args[0]) if args else 120
    print(f"pilnuję limitów co {interval}s, Ctrl+C przerywa")
    while True:
        _KC_SEEN.clear()  # rozpoznanie kont w ticku opiera się na odczytach tego ticku, nie poprzedniego
        try:
            cmd_tick(cfg, [])
        except Exception as err:  # pętla ma przeżyć chwilowy błąd sieci
            log(f"tick: błąd {err}")
        time.sleep(interval)


def cmd_depot(cfg, args):
    """Stan i wymiana tokenu sandboxów `depot claude`."""
    if "--fallback" in args:
        import getpass
        token = getpass.getpass("token z `claude setup-token` (nie pokazuje się): ").strip()
        if not token.startswith("sk-ant-"):
            print("to nie wygląda na token Claude Code")
            return 1
        kc_write(DEPOT_FALLBACK_SERVICE, KEYCHAIN_USER, token)
        print("token zapasowy zapisany w Pęku kluczy")
        return 0
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg właśnie trwa, spróbuj za chwilę")
        return 1
    if not depot_bin(cfg):
        print("brak CLI depot (brew install depot/tap/depot)")
        return 1
    accounts = load_accounts()
    email = depot_sync(accounts, cfg, find_active(accounts, cfg), force="--force" in args)
    state = load_state()
    if not email:
        print("sandboxy Depot bez zmiany tokenu, szczegóły w switch.log")
        return 1
    until = state.get("depot_expires_at")
    print(f"sandboxy Depot: {email}" + (f", token ważny do {datetime.fromtimestamp(until):%Y-%m-%d %H:%M}" if until else ""))
    return 0


# ---------- token dla procesów spoza sesji ----------

def pick_token(accounts, cfg, active, min_valid, prefer=None, avoid=()):
    """Token konta z największym zapasem poza lokalnym, jak sandboxy Depot.

    Konto sandboxów Depot bierzemy dopiero, gdy innego nie ma: dwie strony palące
    jedno konto wyczerpują je dwa razy szybciej. Odświeżamy wyłącznie konta
    nieaktywne, bo token aktywnego rotują sesje Claude Code. Zwraca (email, token,
    expires_at) albo None.
    """
    now = time.time()
    rows = queue(survey(accounts, cfg, exclude_id=active.id if active else None, max_age=600))
    # konto, które właśnie odbiło proces limitem albo 401: limity w pamięci podręcznej
    # (do 10 min) jeszcze tego nie widzą
    rows = [r for r in rows if r["account"].email not in avoid]
    depot_email = load_state().get("depot_email")
    rows = [r for r in rows if r["account"].email != depot_email] + \
           [r for r in rows if r["account"].email == depot_email]
    # konto poprzedniego tokenu, póki ma zapas: seria procesów na jednym koncie dzieli
    # cache promptu, a przeskok na inne konto zaczyna go od zera
    rows = [r for r in rows if r["account"].email == prefer] + \
           [r for r in rows if r["account"].email != prefer]
    for row in rows:
        target = row["account"]
        expires = oauth_of(target.creds_json or "").get("expiresAt", 0) / 1000
        creds_json, note = ensure_fresh(target, cfg, force=expires - now < min_valid)
        if not creds_json:
            log(f"token: {target.email}: {note}")
            continue
        oauth = oauth_of(creds_json)
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt", 0) / 1000
        if token and expires - now >= min_valid:
            return target.email, token, int(expires)
    return None


def cmd_token(cfg, args):
    """Token OAuth dla procesu spoza sesji; bez konta z zapasem token zapasowy."""
    usage = ("użycie: claude-acc token [--json] [--min-minutes N] [--prefer EMAIL] [--avoid EMAIL]"
             " | token --active [--json] [--min-minutes N] | token --fallback")
    if "--help" in args or "-h" in args:
        print(usage)
        return 0
    rest = list(args)
    if "--min-minutes" in rest:
        i = rest.index("--min-minutes")
        if i + 1 >= len(rest) or not rest[i + 1].isdigit():
            print(usage, file=sys.stderr)
            return 2
        del rest[i:i + 2]
    picked_flags = {}
    for flag in ("--prefer", "--avoid"):
        while flag in rest:
            i = rest.index(flag)
            if i + 1 >= len(rest) or rest[i + 1].startswith("-"):
                print(usage, file=sys.stderr)
                return 2
            picked_flags.setdefault(flag, []).append(rest[i + 1])
            del rest[i:i + 2]
    prefer = (picked_flags.get("--prefer") or [None])[-1]
    avoid = tuple(picked_flags.get("--avoid", []))
    unknown = [a for a in rest if a not in ("--json", "--fallback", "--active")]
    if unknown:
        # nieznana flaga nigdy nie może skończyć się wypisaniem tokenu
        print(f"nieznany argument {' '.join(unknown)}; {usage}", file=sys.stderr)
        return 2
    if "--fallback" in args:
        import getpass
        token = getpass.getpass("token z `claude setup-token` (nie pokazuje się): ").strip()
        if not token.startswith("sk-ant-"):
            print("to nie wygląda na token Claude Code")
            return 1
        kc_write(TOKEN_FALLBACK_SERVICE, KEYCHAIN_USER, token)
        print("token zapasowy zapisany w Pęku kluczy")
        return 0
    minutes = 30
    if "--min-minutes" in args:
        minutes = int(args[args.index("--min-minutes") + 1])
    if "--active" in args:
        # Konto, na którym pracuje użytkownik: token z wpisu runtime, który sesje odświeżyły
        # ostatnio. Tylko odczyt: odświeżenie tokenu aktywnego konta zabija sesje, które go
        # trzymają, więc przy krótkiej ważności odmawiamy i zostawiamy to sesjom.
        accounts = load_accounts()
        active = find_active(accounts, cfg)
        oauth = oauth_of(freshest_runtime(cfg) or "")
        token, expires = oauth.get("accessToken"), oauth.get("expiresAt", 0) / 1000
        if not token:
            print("brak tokenu w zarządzanym katalogu", file=sys.stderr)
            return 1
        if expires - time.time() < minutes * 60:
            print(f"token aktywnego konta ważny krócej niż {minutes} min; odświeżą go sesje Claude Code",
                  file=sys.stderr)
            return 1
        email = active.email if active else "nieznane"
        log(f"token: wydany dla {email} (active)")
        if "--json" in args:
            print(json.dumps({"email": email, "token": token, "expiresAt": int(expires), "source": "active"}))
        else:
            print(token)
        return 0
    lock = take_lock(wait=25)
    if not lock:
        print("inny przebieg właśnie trwa, spróbuj za chwilę", file=sys.stderr)
        return 1
    accounts = load_accounts()
    picked = pick_token(accounts, cfg, find_active(accounts, cfg), minutes * 60, prefer=prefer, avoid=avoid)
    if picked:
        email, token, expires = picked
        source = "rotation"
    else:
        token = kc_read(TOKEN_FALLBACK_SERVICE, KEYCHAIN_USER)
        if not token:
            print("brak konta z zapasem poza lokalnym i brak tokenu zapasowego (`claude-acc token --fallback`)",
                  file=sys.stderr)
            return 1
        email, expires, source = "fallback", 0, "fallback"
    log(f"token: wydany dla {email} ({source})")
    if "--json" in args:
        print(json.dumps({"email": email, "token": token, "expiresAt": expires, "source": source}))
    else:
        print(token)
    return 0


COMMANDS = {"status": cmd_status, "who": cmd_who, "plan": cmd_plan, "heal": cmd_heal,
            "switch": cmd_switch, "login": cmd_login, "tick": cmd_tick, "resume": cmd_resume, "pause": cmd_pause,
            "watch": cmd_watch, "depot": cmd_depot, "token": cmd_token}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd not in COMMANDS:
        print(__doc__)
        return 2
    cfg = load_config()
    try:
        return COMMANDS[cmd](cfg, argv[1:])
    except Exception as err:
        log(f"{cmd}: błąd {err}")
        print(f"błąd: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
