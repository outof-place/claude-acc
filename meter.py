#!/usr/bin/env python3
"""Licznik kosztu jednego biegu: odbiornik OpenTelemetry (OTLP/HTTP JSON) i rachunek z niego.

Claude Code z CLAUDE_CODE_ENABLE_TELEMETRY i zmiennymi OTEL_* w `env` ustawień katalogu biegu
wysyła co sekundę (i przy wyjściu) zdarzenia `claude_code.api_request` (koszt w USD, model,
sesja, tożsamość płatnika) i `claude_code.api_error`, także z zagnieżdżonych `claude -p`, bo
każdy z nich czyta te same ustawienia (FACTS-A0, Q10). Meter:

- słucha na 127.0.0.1 na losowym porcie, tylko pętla zwrotna;
- bierze wyłącznie zdarzenia z atrybutem job.run swojego biegu (cudzy bieg na tym porcie się
  nie liczy);
- każde zapytanie liczy raz po request_id, więc paczka ponowiona przez eksporter (503, wolna
  odpowiedź) ani drugi `finish` niczego nie dublują;
- dopisuje zdarzenia do pliku (events.jsonl), zanim odpowie 200: plik przeżywa śmierć
  właściciela i z niego sprzątanie liczy koszt martwego biegu;
- od razu sprawdza płatnika każdego zapytania i błędy API (kredyt wyczerpany, 401, 429),
  żeby właściciel mógł przerwać bieg w trakcie.

    m = Meter(run_id, mode, payer_email, events_path, on_error=callback)
    port = m.start(); m.spent(); m.alarm(); m.alarms(); m.stop()
    report(load_events(path), mode, payer_email, started) -> koszt, sesje, modele, werdykty

Alarmy: pierwszy alarm każdego rodzaju zostaje; alarm() oddaje najważniejszy według ALARM_ORDER
(payer > leak > exhausted > auth > limit), alarms() wszystkie w tej kolejności. Wcześniejszy 429
nie zasłania więc późniejszego obcego płatnika.

Tryb "credits": zapytania z kluczem API nie niosą user.email ani organization.id, więc e-mail
na zapytaniu znaczy, że zapłaciła subskrypcja. Tryb "subscription": każde zapytanie niesie
user.email wybranego konta i organizację inną niż Blazity.
"""

import gzip
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# organizacja konta firmowego (Blazity): nigdy nie płaci za biegi
BLAZITY_ORG = "bb6c7f81-65b7-4ea3-90bc-3d0ae91eb837"
EXHAUSTED_RE = re.compile(r"credit balance (?:is )?too low", re.IGNORECASE)
# klucze i tokeny nie trafiają do pliku zdarzeń, nawet gdyby API zacytowało je w błędzie
SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]{20,}")
MAX_BODY = 32 * 1024 * 1024
# rodzaje alarmów od najważniejszego: obcy płatnik i wyciek logowania to błąd konfiguracji (zabić
# bieg), exhausted to koniec kredytu (zatrzymać), auth i limit to 401 i 429 (decyduje właściciel)
ALARM_ORDER = ("payer", "leak", "exhausted", "auth", "limit")


def value(v):
    """Wartość OTLP AnyValue; intValue przychodzi w JSON-ie jako liczba albo tekst."""
    if not isinstance(v, dict):
        return None
    for kind in ("stringValue", "doubleValue", "intValue", "boolValue"):
        if kind in v:
            x = v[kind]
            if kind == "intValue":
                try:
                    return int(x)
                except (TypeError, ValueError):
                    return None
            if kind == "doubleValue":
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None
            return x
    return None


def attributes(items):
    return {a.get("key"): value(a.get("value")) for a in items or [] if isinstance(a, dict)}


def records(body):
    """(atrybuty zasobu i rekordu razem, nazwa zdarzenia) dla każdego logRecord w paczce."""
    for rl in body.get("resourceLogs") or []:
        resource = attributes((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs") or []:
            for rec in sl.get("logRecords") or []:
                attrs = dict(resource, **attributes(rec.get("attributes")))
                name = attrs.get("event.name") or str(value(rec.get("body")) or "")
                yield attrs, name.replace("claude_code.", "")


def number(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize(attrs, name):
    """Zdarzenie w kształcie pliku events.jsonl albo None dla zdarzeń, które nas nie obchodzą."""
    session = attrs.get("session.id")
    ident = attrs.get("request_id") or attrs.get("client_request_id") or f"{session}:{attrs.get('event.sequence')}"
    common = {"session": session, "model": attrs.get("model"), "email": attrs.get("user.email"),
              "org": attrs.get("organization.id"), "t": time.time()}
    if name == "api_request":
        cost = number(attrs.get("cost_usd"))
        return dict(common, k="request", id=f"req:{ident}", cost=cost)
    if name == "api_error":
        status = attrs.get("status_code")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        message = SECRET_RE.sub("[ukryte]", str(attrs.get("error") or ""))[:300]
        return dict(common, k="error", id=f"err:{ident}:{attrs.get('attempt')}", status=status, message=message)
    return None


def error_kind(event):
    """billing (kredyt wyczerpany), auth (401), limit (429) albo None."""
    if EXHAUSTED_RE.search(event.get("message") or ""):
        return "billing"
    if event.get("status") == 401:
        return "auth"
    if event.get("status") == 429:
        return "limit"
    return None


def identity_problem(event, mode, payer_email):
    """Opis zapytania, które zapłacił ktoś inny niż wybrany płatnik, albo None."""
    email, org = event.get("email"), event.get("org")
    if mode == "credits":
        if email or org:
            return f"zapytanie z kontem {email or org} w biegu na kredycie: zapłaciła subskrypcja, nie klucz"
        return None
    if not email:
        return "zapytanie bez user.email w biegu na subskrypcji: zapłacił klucz API, nie wybrane konto"
    if email.lower() != (payer_email or "").lower():
        return f"zapytanie z konta {email}, a płacić miało {payer_email}"
    if org == BLAZITY_ORG:
        return f"zapytanie z organizacji Blazity ({org})"
    return None


def by_priority(alarms):
    """Alarmy ({"kind", "reason"}) po jednym na rodzaj, od najważniejszego według ALARM_ORDER."""
    first = {}
    for alarm in alarms:
        if alarm and alarm.get("kind") not in first:
            first[alarm.get("kind")] = dict(alarm)
    rank = {kind: n for n, kind in enumerate(ALARM_ORDER)}
    return sorted(first.values(), key=lambda a: rank.get(a["kind"], len(rank)))


def load_events(path):
    """Zdarzenia z pliku biegu, każde raz; urwana ostatnia linia po zabitym procesie nie psuje reszty."""
    events, seen = [], set()
    try:
        with open(path) as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("id") not in seen:
                    seen.add(event.get("id"))
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def report(events, mode, payer_email, started, payer_problems=(), metering_problems=()):
    """Rachunek biegu z jego zdarzeń.

    started: ile nowych sesji (`claude -p` bez --resume, -r, --continue i -c, albo z --fork-session)
    uruchomiono przez `claude` biegu. Wznowienie niesie session.id sesji, którą wznawia, więc nie
    jest nową sesją do zobaczenia; jego zapytania liczą się do kosztu jak każde inne. Licznik jest
    pełny, gdy zapytania przyszły z co najmniej tylu sesji, ile było nowych, i każde ma cost_usd.
    Wznowienie nie dowodzi, że runda przed nim wysłała zdarzenia (to ta sama sesja). Werdykt płatnika:
    "mismatch", gdy cokolwiek zapłacił ktoś inny; "unverified", gdy licznik jest niepełny albo
    nie było żadnego zapytania (brak dowodu to nie dowód); "ok" w pozostałych przypadkach.
    """
    requests = [e for e in events if e.get("k") == "request"]
    errors = [e for e in events if e.get("k") == "error"]
    sessions, models, missing = {}, {}, 0
    for e in requests:
        cost = e.get("cost")
        if cost is None:
            missing += 1
            cost = 0.0
        sid = e.get("session") or "?"
        sessions[sid] = sessions.get(sid, 0.0) + cost
        model = e.get("model") or "?"
        models[model] = models.get(model, 0.0) + cost
    metering = list(metering_problems)
    if started > len(sessions):
        metering.append(f"zapytania przyszły z {len(sessions)} z {started} nowych sesji (bez wznowień)")
    if missing:
        metering.append(f"{missing} zapytań bez cost_usd")
    payer = []
    for e in requests:
        problem = identity_problem(e, mode, payer_email)
        if problem and problem not in payer:
            payer.append(problem)
    for problem in payer_problems:
        if problem not in payer:
            payer.append(problem)
    if payer:
        verdict = "mismatch"
    elif metering or not requests:
        verdict = "unverified"
    else:
        verdict = "ok"
    kinds = {}
    for e in errors:
        kind = error_kind(e)
        if kind:
            kinds.setdefault(kind, e)
    return {
        "cost_usd": round(sum(sessions.values()), 8),
        "by_model": {k: round(v, 8) for k, v in sorted(models.items())},
        "sessions": {k: round(v, 8) for k, v in sorted(sessions.items())},
        "requests": len(requests),
        "sessions_seen": len(sessions),
        "sessions_started": started,
        "metering": {"complete": not metering, "problems": metering},
        "payer_check": {"verdict": verdict, "problems": payer},
        "errors": [{"kind": k, "status": e.get("status"), "message": e.get("message")} for k, e in sorted(kinds.items())],
        "exhausted": "billing" in kinds,
    }


class Meter:
    """Odbiornik OTLP jednego biegu z kosztem na żywo i alarmem płatnika.

    on_error(kind, event) woła się raz na rodzaj błędu (billing, auth, limit), poza blokadą,
    w wątku odbiornika: tam właściciel oznacza wyczerpaną organizację albo konto do ominięcia.
    """

    def __init__(self, run_id, mode, payer_email, path, on_error=None):
        self.run_id, self.mode, self.payer_email, self.path = run_id, mode, payer_email, path
        self.on_error = on_error
        self.lock = threading.Lock()
        self.seen, self.first, self.kinds = set(), {}, set()
        self.cost = 0.0
        self.foreign = 0
        self.server = None
        for event in load_events(path):  # wznowienie: to, co już leży na dysku, liczy się raz
            self._count(event)

    def _count(self, event):
        self.seen.add(event["id"])
        if event["k"] == "request":
            self.cost += event.get("cost") or 0.0
            problem = identity_problem(event, self.mode, self.payer_email)
            if problem:
                self.first.setdefault("payer", {"kind": "payer", "reason": problem})
            return None
        kind = error_kind(event)
        if kind and kind not in self.kinds:
            self.kinds.add(kind)
            alarm = {"billing": "exhausted"}.get(kind, kind)
            self.first.setdefault(alarm, {"kind": alarm, "reason": event.get("message") or kind})
            return kind
        return None

    def ingest(self, body):
        """Paczka OTLP: ile zdarzeń tego biegu przyjęto. Wyjątek OSError: zapis się nie udał
        (odbiornik odpowiada wtedy 503 i eksporter ponawia)."""
        fresh, fired = [], []
        with self.lock:
            for attrs, name in records(body):
                event = normalize(attrs, name)
                if event is None:
                    continue
                if attrs.get("job.run") != self.run_id:
                    self.foreign += 1
                    continue
                if event["id"] in self.seen:
                    continue
                fresh.append(event)
            if fresh:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in fresh))
                for event in fresh:
                    kind = self._count(event)
                    if kind:
                        fired.append((kind, event))
        for kind, event in fired:
            if self.on_error:
                try:
                    self.on_error(kind, event)
                except Exception:  # noqa: BLE001 - błąd właściciela nie może zgubić zdarzeń
                    pass
        return len(fresh)

    def spent(self):
        with self.lock:
            return round(self.cost, 8)

    def alarms(self):
        """Pierwszy alarm każdego rodzaju ({"kind", "reason"}), od najważniejszego (ALARM_ORDER)."""
        with self.lock:
            return by_priority(list(self.first.values()))

    def alarm(self):
        """Najważniejszy alarm biegu albo None: payer, exhausted, auth, limit (leak ma Watcher)."""
        found = self.alarms()
        return found[0] if found else None

    def start(self):
        meter = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                    if size > MAX_BODY:
                        return self.reply(413)
                    raw = self.rfile.read(size)
                    if self.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    body = json.loads(raw) if raw else {}
                except (ValueError, OSError, EOFError):
                    return self.reply(400)
                if self.path.rstrip("/").endswith("/v1/logs") and isinstance(body, dict):
                    try:
                        meter.ingest(body)
                    except OSError:
                        return self.reply(503)
                self.reply(200)

            def reply(self, code):
                payload = b"{}"
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = False
        server.block_on_close = True  # stop() czeka, aż każda przyjęta paczka trafi na dysk
        self.server = server
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        return server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
