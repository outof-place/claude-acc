#!/usr/bin/env python3
"""Radar tematów dla blogów: `claude-acc seo radar <blog> [--dry] [--no-web]`.

Blog o programowaniu żyje z tego, że pisze o nowościach, zanim zrobią to inni. Radar zbiera, co
w ostatnich dniach wydali Anthropic, Vercel i zespoły Next.js, React i spółki, o czym mówią
programiści, i zapisuje ~/.local/share/claude-acc/seo/<blog>/radar.json (schemat v1,
`seo schema radar`), z którego planer bloga wybiera temat dnia. Jeden bieg:

1. Źródła z konfiguracji bloga (seo/config.json, sekcja radar.sources): kanały RSS i Atom, wydania
   GitHub (releases.atom), mapa strony z lastmod (Anthropic /news i /engineering, tytuł i data ze
   strony), strony z notatkami o wydaniach (docs Claude w Markdown, Help Center w HTML) i API
   Hacker News (Algolia). Każde źródło może zawieść osobno: wynik jest wtedy uboższy, a "sources"
   mówi, czego brakło i dlaczego. Bieg nie pada z powodu źródła.
2. Zdarzenia: pozycje z różnych źródeł o tym samym (ten sam adres albo to samo wydanie: byt i
   wersja) to jedno zdarzenie. Wersje wstępne (canary, beta, rc) nie są zdarzeniami, ciche wydania
   poprawkowe tego samego bytu zwija się w jedno zdarzenie. Rodzaj: release, announcement,
   article albo discussion.
3. Popularność: punkty i komentarze HN (sumy ze wszystkich wątków zdarzenia), liczba niezależnych
   źródeł, pobrania npm z tygodnia i popyt na frazę zdarzenia z K1: autocomplete Google (w
   językach bloga), GSC i Bing, gdy blog je ma.
4. Pilność (breaking, high, normal), według reguły z README, i to, czy blog już pisał o zdarzeniu
   (jego wpisy z mapy strony i llms.txt).
5. Rozmowy na X, pośrednio i za darmo: jedna sesja Sonnet z WebSearch i WebFetch przegląda
   najważniejsze zdarzenia, zapisuje, co mówią programiści, jakie posty z X cytują inni i kto już
   opisał zdarzenie dobrze (luka). Sesja dostaje te dwa narzędzia i jeden plik wyjścia (jak sesje
   K1 w seo.py); to, co wraca z sieci, jest danymi: kod tnie linie z poleceniami i znaki
   sterujące, a posty X przyjmuje tylko z adresów x.com i twitter.com.
6. Propozycja wpisu dla każdego zdarzenia: typ (release, tutorial, digest), kąt, słowa kluczowe EN
   i PL, pomysł na demo. Kąt i demo najważniejszych zdarzeń poprawia sesja z punktu 5.

Uprzejme pobieranie: opisowy User-Agent z contact@outofplace.space, zapytania warunkowe (ETag i
Last-Modified) z pamięcią podręczną w seo/cache, odstęp co najmniej HOST_INTERVAL s na host
(wspólny z K1 i audytem), przerwa po 429 i 503 (Retry-After albo BACKOFF), host, który nie ustąpił,
jest zamknięty do końca biegu. robots.txt obowiązuje przy stronach HTML (Anthropic). Tylko darmowe
źródła: bez API X, bez Reddita, bez płatnych usług.

Claude woła tylko ta jedna sesja, i tylko przez `claude` biegu (job albo `credits run`): bez
CLAUDE_ACC_RUN komenda sama woła `credits run --purpose seo-radar-<blog>`. `--no-web` pomija sesję
(0 USD, bez credits run). `--dry` drukuje zdarzenia z dowodami i nie zapisuje niczego w katalogu
bloga (radar.json, radar-state.json, JSON wyniku joba); pamięć podręczna pobrań zapisuje się i
wtedy, jak w `seo research`.

W jobie (CLAUDE_ACC_JOB_RESULT) wynik idzie według kontraktu blogów: znacznik side-effect przed
zapisem radar.json, JSON wyniku, blok WYNIK; kod 0 OPUBLIKOWANO (radar.json zapisany), 1 BŁĄD
(zła konfiguracja albo żadne źródło nic nie dało: do ponowienia, poprzedni radar.json zostaje).

Pliki (~/.local/share/claude-acc/seo/):
  <blog>/radar.json        wynik, schemat v1
  <blog>/radar-state.json  od kiedy radar widzi każde zdarzenie (first_seen)
  cache/radar-*.json       pobrania (z ETag i Last-Modified), strony Anthropic, HN, npm, robots.txt
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import hashlib
import html
import json
import math
import re
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import seo
import seoaudit

UA = "claude-acc-seo-radar/1 (+https://www.outofplace.space; contact@outofplace.space)"
DAY = 86400
HN_API = "https://hn.algolia.com/api/v1"
NPM_API = "https://api.npmjs.org/downloads/point/last-week"
TIMEOUT = 25
RECENT = 600  # s: pobranie młodsze niż to nie idzie znowu do sieci (suchy bieg tuż po prawdziwym)
BACKOFF = (10, 30)  # s przerwy po kolejnych 429 i 503, gdy serwer nie podał Retry-After
MAX_PAUSE = 60
NET_RETRY_S = 3  # przerwa przed jedynym ponowieniem po błędzie sieci (DNS bywa kapryśny)
KEEP_DAYS = 30  # tyle dni pozycji kanału leży w pamięci podręcznej (na odpowiedź 304 i na awarię)
HN_TTL = 3 * 3600
NPM_TTL = DAY
PAGE_TTL = 90 * DAY
ROBOTS_TTL = 7 * DAY
DEMAND_TTL = 36 * 3600
PAGE_BYTES = 400 * 1024
MAX_TITLE = 200
SOURCE_KINDS = ("feed", "github", "sitemap", "notes", "hn")
ITEM_KINDS = ("release", "announcement", "article", "discussion")
KIND_ORDER = {"announcement": 0, "article": 1, "release": 2, "discussion": 3}  # który wpis nazywa zdarzenie
URGENCIES = ("breaking", "high", "normal")
LEVELS = ("major", "minor", "patch")
PROPOSALS = ("release", "tutorial", "digest")
NOTE_FORMATS = ("md-headings", "html-dates")
DEFAULT_LIMITS = {
    "events": 80,  # zdarzeń w radar.json
    "x_events": 8,  # tyle najważniejszych zdarzeń przegląda sesja z WebSearch
    "demand_events": 16,  # tyle najważniejszych zdarzeń dostaje popyt z autocomplete
    "hn_lookups": 20,  # zapytań HN o adres zdarzenia, którego nie znalazły zapytania słów kluczowych
    "items_per_event": 8,
}

# reguły pilności (README, SEO radar); progi są stałymi, bo to polityka, nie konfiguracja
BREAKING_HOURS = 48
HIGH_DAYS = 7
BREAKING_RELEASE_HN = 50  # wydanie major albo minor bytu pierwszej rangi: 2 źródła albo tyle punktów HN
BREAKING_TALK_HN = 150  # ogłoszenie albo artykuł o bycie pierwszej rangi: tyle punktów HN albo 3 źródła
HIGH_HN = 100
QUIET_HN = 30  # wydanie poprawkowe z mniejszą liczbą punktów HN i jednym źródłem idzie do zwiniętej sumy

PRERELEASE_RE = re.compile(r"(?i)[-.\s@](?:canary|alpha|beta|rc|preview|nightly|insiders?|dev|experimental|pre)(?:[-.\d\s]|$)")
SECURITY_RE = re.compile(r"\b(security|vulnerabilit\w*|CVE-\d{4}-\d+|advisory|exploit\w*)\b", re.IGNORECASE)
ANY_VERSION_RE = re.compile(r"(?<![\w.])v?(\d{1,2}(?:\.\d{1,3}){1,2}|\d{1,2})(?![\w]|\.\d)")
AFTER_VERSION_RE = re.compile(r"[\s:@\-]{0,3}v?(\d{1,2}(?:\.\d{1,3}){0,2})(?![\w])")
MD_DATE_RE = re.compile(r"^#{2,4}\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\s*$", re.MULTILINE)
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
STOP_TOPIC = set(("a an and are as at be by for from how in is it of on or the to what with your you new now available introducing "
                  "may can could will been being our we us its their this that these those why when where which who using use used "
                  "show hn launch ask i my me").split())


def source(name, ok, detail, count=0, skipped=False, degraded=False):
    return seo.source(name, ok, seo.no_dashes(detail), count, skipped, degraded)


# ---------- konfiguracja ----------


def radar_config(blog):
    """Sekcja radar bloga (blog["radar"]) z domyślnymi wartościami; UsageError mówi, co poprawić."""
    name = blog["name"]
    raw = blog.get("radar")
    if not isinstance(raw, dict):
        raise seo.UsageError(f"{name}.radar: brak sekcji radar w konfiguracji (wzór w README, SEO radar)")
    cfg = {"window_days": 7, "npm_api": NPM_API}
    try:
        cfg["window_days"] = int(raw.get("window_days", 7))
    except (TypeError, ValueError):
        raise seo.UsageError(f"{name}.radar.window_days: liczba dni")
    if not 1 <= cfg["window_days"] <= 30:
        raise seo.UsageError(f"{name}.radar.window_days: od 1 do 30 dni")
    if raw.get("npm_api"):
        cfg["npm_api"] = str(raw["npm_api"]).rstrip("/")
    limits = dict(DEFAULT_LIMITS)
    for key, value in (raw.get("limits") or {}).items():
        if key in limits and isinstance(value, int) and value >= 0:
            limits[key] = value
    cfg["limits"] = limits
    entities = []
    for e in raw.get("entities") or []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
            raise seo.UsageError(f"{name}.radar.entities: każdy byt ma name (np. \"Next.js\"), aliases, tier (1 albo 2), npm, category")
        aliases = [a.strip().lower() for a in e.get("aliases") or [e["name"]] if isinstance(a, str) and a.strip()]
        tier = e.get("tier", 2)
        if tier not in (1, 2):
            raise seo.UsageError(f"{name}.radar.entities.{e['name']}.tier: 1 (może być breaking) albo 2")
        entities.append({"name": e["name"].strip(), "aliases": aliases, "tier": tier, "npm": e.get("npm") or None,
                         "category": e.get("category") or None, "phrase": (e.get("phrase") or e["name"]).strip().lower()})  # fmt: skip
    names = [e["name"] for e in entities]
    if len(set(names)) != len(names):
        raise seo.UsageError(f"{name}.radar.entities: nazwy bytów się powtarzają")
    cfg["entities"] = entities
    sources, seen = [], set()
    for s in raw.get("sources") or []:
        sources.append(source_config(name, s, names, seen))
    if not sources:
        raise seo.UsageError(f"{name}.radar.sources: lista źródeł (feed, github, sitemap, notes, hn)")
    cfg["sources"] = sources
    return cfg


def source_config(blog_name, raw, entity_names, seen):
    where = f"{blog_name}.radar.sources"
    if not isinstance(raw, dict) or not SOURCE_ID_RE.match(str(raw.get("id", ""))):
        raise seo.UsageError(f"{where}: każde źródło ma id (małe litery, cyfry i myślniki) i kind")
    sid = raw["id"]
    if sid in seen:
        raise seo.UsageError(f"{where}: id {sid} się powtarza")
    seen.add(sid)
    kind = raw.get("kind")
    if kind not in SOURCE_KINDS:
        raise seo.UsageError(f"{where}.{sid}.kind: jeden z {', '.join(SOURCE_KINDS)}")
    out = {"id": sid, "kind": kind, "label": str(raw.get("label") or sid), "entity": raw.get("entity") or None}
    if out["entity"] and out["entity"] not in entity_names:
        raise seo.UsageError(f"{where}.{sid}.entity: {out['entity']} nie ma w entities")
    for key in ("include", "exclude", "release_title"):
        if raw.get(key):
            try:
                re.compile(raw[key])
            except re.error:
                raise seo.UsageError(f"{where}.{sid}.{key}: to nie jest wyrażenie regularne")
        out[key] = raw.get(key) or None
    if kind == "github":
        if not REPO_RE.match(str(raw.get("repo", ""))):
            raise seo.UsageError(f"{where}.{sid}.repo: właściciel/repozytorium, np. vercel/next.js")
        out.update(repo=raw["repo"], url=f"https://github.com/{raw['repo']}/releases.atom", event="release",
                   releases=bool(raw.get("releases", True)), keep_prerelease=bool(raw.get("keep_prerelease", False)))  # fmt: skip
    elif kind == "hn":
        queries = []
        for q in raw.get("queries") or []:
            q = {"q": q} if isinstance(q, str) else q
            if not isinstance(q, dict) or not isinstance(q.get("q"), str) or not q["q"].strip():
                raise seo.UsageError(f"{where}.{sid}.queries: lista fraz albo obiektów {{q, url, entity, min_points}}")
            if q.get("entity") and q["entity"] not in entity_names:
                raise seo.UsageError(f"{where}.{sid}.queries: byt {q['entity']} nie ma w entities")
            queries.append({"q": q["q"].strip(), "url": bool(q.get("url", False)), "entity": q.get("entity") or None,
                            "min_points": int(q.get("min_points", raw.get("min_points", 5 if q.get("url") else 20)))})  # fmt: skip
        if not queries:
            raise seo.UsageError(f"{where}.{sid}.queries: co najmniej jedna fraza")
        out.update(queries=queries, api=str(raw.get("api") or HN_API).rstrip("/"), event="discussion", releases=False)
    else:
        url = str(raw.get("url", ""))
        if not re.match(r"^https?://", url):
            raise seo.UsageError(f"{where}.{sid}.url: pełny adres https://...")
        event = raw.get("event", "announcement")
        if event not in ("announcement", "article"):
            raise seo.UsageError(f"{where}.{sid}.event: announcement albo article")
        out.update(url=url, event=event, releases=bool(raw.get("releases", kind == "notes")))
        if kind == "notes":
            if raw.get("format") not in NOTE_FORMATS:
                raise seo.UsageError(f"{where}.{sid}.format: {' albo '.join(NOTE_FORMATS)}")
            out.update(format=raw["format"], page_url=str(raw.get("page_url") or re.sub(r"\.md$", "", url)))
        if kind == "sitemap" and not out["include"]:
            out["include"] = "."
    return out


# ---------- czas, adresy, tekst ----------


def parse_when(text):
    """Epoch z daty ISO, RFC 822 (RSS) albo "October 9, 2026"; None, gdy to nie data."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    got = seoaudit.parse_time(text)
    if got is not None:
        return got
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    match = re.match(r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", text)
    if match and match.group(1).lower() in MONTHS:
        try:
            return datetime(int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2)), tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def canon(url):
    """Adres do porównań między źródłami: https, bez www, bez fragmentu, ukośnika na końcu i
    znaczników utm; None dla czegoś, co nie jest adresem http(s)."""
    got = seoaudit.norm_url(url)
    if not got:
        return None
    parts = urllib.parse.urlsplit(got)
    netloc = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    query = "&".join(p for p in parts.query.split("&") if p and not p.lower().startswith(("utm_", "fbclid", "ref=")))
    return urllib.parse.urlunsplit(("https", netloc, parts.path.rstrip("/") or "/", query, ""))


def plain(text):
    """Tekst z fragmentu HTML albo Markdowna: bez znaczników, linków Markdown i kodu, jedna linia."""
    text = str(text or "")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text, limit=160):
    text = plain(text)
    match = re.search(r"(?<=[.!?])\s+(?=[A-Z])", text)
    head = text[: match.start()] if match else text
    return head[:limit].rstrip(" .,;:") if len(head) > limit else head.rstrip(" .")


def header(resp, name):
    for key, value in (resp.headers or {}).items():
        if key.lower() == name:
            return value
    return ""


def retry_after(resp, now):
    value = header(resp, "retry-after").strip()
    if value.isdigit():
        return int(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, IndexError):
        return None


# ---------- uprzejme pobieranie ----------


class Polite:
    """Zapytania radaru: User-Agent z adresem kontaktowym, odstęp na host (ctx.pace, ten sam zamek
    co K1 i audyt), przerwa po 429 i 503 (Retry-After albo BACKOFF), jedno ponowienie po błędzie
    sieci. Host, który po przerwach dalej odmawia, jest zamknięty do końca biegu: jego pozostałe
    źródła degradują od razu zamiast czekać."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.limited = {}
        self.requests = 0

    def get(self, url, headers=None, limit=seo.MAX_BODY, partial=False):
        host = seo.host_of(url)
        if host in self.limited:
            raise seo.FetchError(f"{host} ogranicza zapytania ({self.limited[host]}), do końca biegu pomijam")
        hdrs = dict({"User-Agent": UA, "Accept": "*/*"}, **(headers or {}))
        pauses = net_retries = 0
        while True:
            self.ctx.pace(f"host-{host}", self.ctx.host_interval)
            self.requests += 1
            try:
                resp = self.ctx.fetch(url, timeout=TIMEOUT, limit=limit, headers=hdrs, partial=partial)
            except seo.FetchError as exc:
                if net_retries >= 1:
                    raise
                net_retries += 1
                self.ctx.log(f"{host}: {exc}, ponawiam za {NET_RETRY_S} s")
                self.ctx.sleep(NET_RETRY_S)
                continue
            if resp.status not in (429, 503):
                return resp
            if pauses >= len(BACKOFF):
                self.limited[host] = f"HTTP {resp.status}"
                return resp
            wait = retry_after(resp, self.ctx.now())
            wait = min(wait if wait is not None else BACKOFF[pauses], MAX_PAUSE)
            self.ctx.log(f"{host}: HTTP {resp.status}, czekam {wait:.0f} s")
            self.ctx.sleep(wait)
            pauses += 1

    def robots_allow(self, url):
        """True, gdy robots.txt hosta pozwala nam na adres; robots.txt, który nie odpowiada (sieć,
        5xx), znaczy "dziś nie czytamy" i nie trafia do pamięci podręcznej."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cache = self.ctx.cache("radar-robots")
        got = cache.get(origin, ROBOTS_TTL)
        if got is None:
            try:
                resp = self.get(origin + "/robots.txt", limit=512 * 1024)
            except seo.FetchError:
                return False
            if resp.status >= 500 or resp.status == 429:
                return False
            got = {"status": resp.status, "text": resp.body.decode("utf-8", "replace") if resp.status == 200 else ""}
            cache.put(origin, got)
        parser = urllib.robotparser.RobotFileParser()
        if got["status"] in (401, 403):
            parser.disallow_all = True
        elif got["status"] == 200:
            parser.parse(got["text"].splitlines())
        else:
            parser.allow_all = True
        return parser.can_fetch(UA, url)


def conditional(polite, url, parse):
    """Pozycje spod adresu: zapytanie warunkowe (ETag, Last-Modified) z pamięci podręcznej; 304
    oddaje zapamiętane pozycje, awaria serwera oddaje je jako nieświeże. (pozycje, uwaga, nieświeże)."""
    ctx = polite.ctx
    cache = ctx.cache("radar-http")
    entry = cache.data.get(url)
    prev = entry["v"] if isinstance(entry, dict) and isinstance(entry.get("v"), dict) else None
    if prev and ctx.now() - entry.get("at", 0) < RECENT:
        return prev["items"], "z pamięci podręcznej (świeża)", False
    headers = {}
    if prev and prev.get("etag"):
        headers["If-None-Match"] = prev["etag"]
    if prev and prev.get("lm"):
        headers["If-Modified-Since"] = prev["lm"]
    try:
        resp = polite.get(url, headers)
    except seo.FetchError as exc:
        if prev:
            return prev["items"], f"{exc}; użyte pozycje z cache", True
        raise
    if resp.status == 304 and prev:
        cache.put(url, prev)
        return prev["items"], "nie zmieniony (304)", False
    if resp.status != 200:
        if prev:
            return prev["items"], f"HTTP {resp.status}; użyte pozycje z cache", True
        raise seo.FetchError(f"HTTP {resp.status}")
    items = parse(resp.body)
    cache.put(url, {"etag": header(resp, "etag") or None, "lm": header(resp, "last-modified") or None, "items": items})
    return items, "", False


# ---------- parsery ----------


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_feed(body, keep_since):
    """Pozycje RSS albo Atom jako [{title, url, published, summary}] nie starsze niż keep_since;
    bez DTD (encje, XXE)."""
    if re.search(rb"<!(doctype|entity)", body, re.IGNORECASE):
        raise ValueError("kanał z DTD")
    root = ET.fromstring(body)
    out = []
    for entry in root.iter():
        if local(entry.tag) not in ("entry", "item"):
            continue
        fields, link = {}, None
        for child in entry:
            name = local(child.tag)
            if name == "link":
                href = child.get("href") or (child.text or "").strip()
                if href and (link is None or child.get("rel") in (None, "alternate")):
                    link = href
            elif name not in fields:
                fields[name] = "".join(child.itertext())
        when = next((parse_when(fields[k]) for k in ("published", "pubDate", "date", "updated") if fields.get(k) and parse_when(fields[k])), None)
        if when is None or when < keep_since:
            continue
        link = link or (fields.get("id") if str(fields.get("id", "")).startswith("http") else None)
        summary = next((fields[k] for k in ("summary", "description", "content", "encoded") if fields.get(k)), "")
        out.append({"title": plain(fields.get("title")), "url": link, "published": when, "summary": plain(summary)[:500]})
    return out


def parse_notes_md(body, page_url, keep_since):
    """Notatki o wydaniach w Markdownie (docs Claude): nagłówek z datą, pod nim punkty; każdy punkt to pozycja."""
    text = body.decode("utf-8", "replace")
    marks = list(MD_DATE_RE.finditer(text))
    out = []
    for i, mark in enumerate(marks):
        when = parse_when(mark.group(1))
        if when is None or when < keep_since:
            continue
        block = text[mark.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        bullets = [b for b in re.split(r"\n(?=[*-]\s)", block.strip()) if b.strip()]
        for n, bullet in enumerate(bullets, 1):
            body_text = re.sub(r"^[*-]\s+", "", bullet.strip())
            title = first_sentence(body_text)
            if title:
                anchor = re.sub(r"[^a-z0-9]+", "-", mark.group(1).lower()).strip("-")
                out.append({"title": title, "url": f"{page_url}#{anchor}-{n}", "published": when, "summary": plain(body_text)[:500]})
    return out


def parse_notes_html(body, page_url, keep_since):
    """Notatki o wydaniach w HTML (Help Center): <h3>data</h3>, potem akapity; akapit tylko z <b>
    to tytuł pozycji, następne akapity do kolejnego tytułu to jej treść."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", body.decode("utf-8", "replace"))
    heads = list(re.finditer(r"(?is)<h3[^>]*?(?:id=\"([^\"]*)\")?[^>]*>(.*?)</h3>", text))
    out = []
    for i, head in enumerate(heads):
        when = parse_when(plain(head.group(2)))
        if when is None or when < keep_since:
            continue
        block = text[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        block = re.split(r"(?is)<h2\b", block)[0]  # nagłówek miesiąca kończy blok dnia
        current, n = None, 0
        for para in re.findall(r"(?is)<p[^>]*>(.*?)</p>", block):
            line = plain(para)
            if not line:
                continue
            if re.fullmatch(r"(?is)\s*<b>.*?</b>\s*", para.strip()):
                n += 1
                current = {"title": line, "url": f"{page_url}#{head.group(1) or 'wpis'}-{n}", "published": when, "summary": ""}
                out.append(current)
            elif current is not None:
                current["summary"] = (current["summary"] + " " + line).strip()[:500]
    return out


def meta_tag(text, *names):
    for name in names:
        for pattern in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']',
                        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']'):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return html.unescape(match.group(1))
    return ""


def page_meta(body):
    """Tytuł, opis i data publikacji ze strony: og:title albo <title>, og:description, datePublished."""
    text = body.decode("utf-8", "replace")
    title = meta_tag(text, "og:title", "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        title = match.group(1) if match else ""
    title = re.split(r"\s[|\\·]\s", plain(title))[0]
    published = meta_tag(text, "article:published_time", "og:article:published_time")
    if not published:
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', text)
        published = match.group(1) if match else ""
    return {"title": title, "description": meta_tag(text, "og:description", "description"), "published": parse_when(published)}


# ---------- źródła ----------


def feed_items(spec, items):
    out = []
    for raw in items:
        out.append(make_item(spec, raw["title"], raw["url"], raw["published"], raw.get("summary", "")))
    return [i for i in out if i]


def make_item(spec, title, url, published, summary="", kind=None, **extra):
    """Pozycja źródła z tekstem potraktowanym jak dane (clean: linia z poleceniem znika cała);
    None, gdy brakuje tytułu, adresu albo daty."""
    title = seo.clean(title, MAX_TITLE)
    key = canon(url) if isinstance(url, str) else None
    if not title or not key or published is None:
        return None
    if "#" in url and spec["kind"] == "notes":
        key = key + "#" + url.split("#", 1)[1]
    item = {"source": spec["id"], "kind": kind or spec["event"], "title": title, "url": url, "key": key,
            "published": float(published), "summary": seo.clean(summary, 400)}  # fmt: skip
    item.update(extra)
    return item


def wanted(spec, title):
    if spec.get("include") and not re.search(spec["include"], title):
        return False
    if spec.get("exclude") and re.search(spec["exclude"], title):
        return False
    return True


def load_feed(polite, spec, now, keep_since):
    """Kanał RSS, Atom albo wydania GitHub: (pozycje, status)."""
    try:
        raw, note, stale = conditional(polite, spec["url"], lambda body: parse_feed(body, now - KEEP_DAYS * DAY))
    except (seo.FetchError, ValueError, ET.ParseError) as exc:
        return [], source(spec["id"], False, f"{spec['label']}: {str(exc)[:120]}")
    items, skipped = [], 0
    for r in raw:
        if not wanted(spec, r["title"]):
            continue
        if spec["kind"] == "github" and not spec["keep_prerelease"] and PRERELEASE_RE.search(r["title"] + " "):
            skipped += 1
            continue
        item = make_item(spec, r["title"], r["url"], r["published"], r.get("summary", ""))
        if item:
            items.append(item)
    recent = [i for i in items if i["published"] >= keep_since]
    detail = f"{spec['label']}: {seo.plural(len(recent), 'pozycja', 'pozycje', 'pozycji')} z okna"
    if skipped:
        detail += f", {skipped} wersji wstępnych pominięto"
    if note:
        detail += f"; {note}"
    return items, source(spec["id"], True, detail, len(recent), degraded=stale)


def load_notes(polite, spec, now, keep_since):
    parser = parse_notes_md if spec["format"] == "md-headings" else parse_notes_html
    try:
        raw, note, stale = conditional(polite, spec["url"], lambda body: parser(body, spec["page_url"], now - KEEP_DAYS * DAY))
    except (seo.FetchError, ValueError) as exc:
        return [], source(spec["id"], False, f"{spec['label']}: {str(exc)[:120]}")
    items = [i for i in (make_item(spec, r["title"], r["url"], r["published"], r.get("summary", "")) for r in raw if wanted(spec, r["title"])) if i]
    recent = [i for i in items if i["published"] >= keep_since]
    detail = f"{spec['label']}: {seo.plural(len(recent), 'wpis', 'wpisy', 'wpisów')} z okna" + (f"; {note}" if note else "")
    return items, source(spec["id"], True, detail, len(recent), degraded=stale)


def load_sitemap(polite, spec, now, keep_since):
    """Mapa strony z lastmod (Anthropic /news i /engineering): adresy z okna dostają tytuł, opis i
    datę publikacji ze strony (robots.txt, pamięć podręczna). Strona, której data publikacji jest
    sprzed okna, była tylko poprawiona i nie jest zdarzeniem."""
    try:
        entries, _note, stale = conditional(polite, spec["url"], lambda body: [list(e) for e in seoaudit.parse_sitemap(body)[0]])
    except (seo.FetchError, ValueError, ET.ParseError, OSError, EOFError) as exc:
        return [], source(spec["id"], False, f"{spec['label']}: {str(exc)[:120]}")
    include = re.compile(spec["include"])
    cache = polite.ctx.cache("radar-pages")
    items, edited, blocked, failed = [], 0, 0, 0
    for loc, lastmod in entries:
        when = seoaudit.parse_time(lastmod)
        if when is None or when < keep_since or not include.search(seo.url_path(loc)):
            continue
        meta = cache.get(loc, PAGE_TTL)
        if meta is None:
            if not polite.robots_allow(loc):
                blocked += 1
                meta = {"title": "", "description": "", "published": None}
            else:
                try:
                    page = polite.get(loc, limit=PAGE_BYTES, partial=True)
                    meta = page_meta(page.body) if page.status == 200 else None
                except seo.FetchError:
                    meta = None
                if meta is None:
                    failed += 1
                    meta = {"title": "", "description": "", "published": None}
                else:
                    cache.put(loc, meta)
        published = meta.get("published") or when
        if published < keep_since:
            edited += 1
            continue
        title = meta.get("title") or seo.slug_words(loc)
        item = make_item(spec, title, loc, published, meta.get("description") or "")
        if item:
            items.append(item)
    detail = f"{spec['label']}: {seo.plural(len(items), 'strona', 'strony', 'stron')} z okna"
    if edited:
        detail += f", {edited} poprawionych starych stron pominięto"
    if blocked or failed:
        detail += f"; tytuł ze slugu: {blocked} (robots.txt), {failed} (błąd strony)"
    return items, source(spec["id"], True, detail, len(items), degraded=bool(failed) or stale)


def hn_url(spec, query, since):
    since = int(since // DAY * DAY)  # do doby: adres (klucz pamięci podręcznej) ma się powtórzyć następnego biegu
    params = {"query": query["q"], "tags": "story", "hitsPerPage": 30,
              "numericFilters": f"created_at_i>{since},points>={query['min_points']}"}  # fmt: skip
    if query["url"]:
        params["restrictSearchableAttributes"] = "url"
    return f"{spec['api']}/search?" + urllib.parse.urlencode(params)


def hn_hits(polite, spec, url):
    """Wątki HN spod adresu zapytania (pamięć podręczna HN_TTL): lista [{id, title, url, points, comments, created}]."""
    cache = polite.ctx.cache("radar-hn")
    got = cache.get(url, HN_TTL)
    if got is not None:
        return got, True
    resp = polite.get(url)
    if resp.status != 200:
        raise seo.FetchError(f"HTTP {resp.status}")
    try:
        data = json.loads(seo.decode(resp))
        rows = data["hits"]
    except (ValueError, KeyError, TypeError):
        raise seo.FetchError("odpowiedź nie jest JSON-em HN")
    out = []
    for h in rows if isinstance(rows, list) else []:
        if isinstance(h, dict) and h.get("objectID") and isinstance(h.get("created_at_i"), int):
            out.append({"id": str(h["objectID"]), "title": str(h.get("title") or "")[:300], "url": h.get("url") if isinstance(h.get("url"), str) else None,
                        "points": int(h.get("points") or 0), "comments": int(h.get("num_comments") or 0), "created": h["created_at_i"]})  # fmt: skip
    cache.put(url, out)
    return out, False


def hn_item(spec, hit, entity=None, loose=False):
    """Wątek HN jako pozycja. loose: trafienie zapytania po słowie (Algolia szuka też w treści i
    komentarzach), więc byt zapytania nie przechodzi na wątek; resolve odrzuca wątek, którego tytuł
    ani adres nie wspomina żadnego bytu."""
    discussion = f"https://news.ycombinator.com/item?id={hit['id']}"
    url = hit["url"] if hit["url"] and canon(hit["url"]) else discussion
    item = make_item(spec, hit["title"], url, hit["created"], "", kind="discussion",
                     hn={"id": hit["id"], "points": hit["points"], "comments": hit["comments"], "discussion": discussion})  # fmt: skip
    if item is not None:
        item["default_entity"] = None if loose else entity
        item["loose"] = loose
    return item


def load_hn(polite, spec, now, keep_since):
    """Zapytania HN z konfiguracji (słowa kluczowe i adresy): wątki z okna z punktami."""
    items, seen, errors, cached = [], set(), [], 0
    for query in spec["queries"]:
        try:
            hits, was_cached = hn_hits(polite, spec, hn_url(spec, query, keep_since))
        except seo.FetchError as exc:
            errors.append(f"{query['q']}: {str(exc)[:60]}")
            if len(errors) >= 3:
                break
            continue
        cached += was_cached
        for hit in hits:
            if hit["id"] in seen or hit["created"] < keep_since:
                continue
            item = hn_item(spec, hit, query["entity"], loose=not query["url"])
            if item:
                seen.add(hit["id"])
                items.append(item)
    detail = f"{seo.plural(len(items), 'wątek', 'wątki', 'wątków')} z {len(spec['queries'])} zapytań ({cached} z cache)"
    if errors:
        detail += "; błędy: " + "; ".join(errors[:3])
    return items, source(spec["id"], bool(items) or not errors, detail, len(items), degraded=bool(errors))


def collect(polite, cfg, now, keep_since):
    items, statuses = [], []
    for spec in cfg["sources"]:
        loader = {"feed": load_feed, "github": load_feed, "sitemap": load_sitemap, "notes": load_notes, "hn": load_hn}[spec["kind"]]
        try:
            got, status = loader(polite, spec, now, keep_since)
        except Exception as exc:  # źródło nie przewraca biegu
            got, status = [], source(spec["id"], False, f"{spec['label']}: błąd {exc!r}"[:200])
        items += [i for i in got if i["published"] >= keep_since]
        statuses.append(status)
        polite.ctx.save_caches()
    return items, statuses


# ---------- byty i wersje ----------


def norm_version(text):
    """"16.4.0" i "16.4" to 16.4; "6.0" to 6; "7.0.2" zostaje: zera na końcu odpadają."""
    parts = [int(p) for p in str(text).split(".") if p.isdigit()]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(p) for p in parts) if parts else None


def level_of(version):
    return None if not version else LEVELS[min(len(version.split(".")), 3) - 1]


def version_key(version):
    return tuple(int(p) for p in version.split("."))


class Entities:
    """Byty z konfiguracji (Next.js, React, Claude Code...): znajdowanie po aliasach w tekście."""

    def __init__(self, cfgs):
        self.by_name = {e["name"]: e for e in cfgs}
        pairs = sorted(((a, e) for e in cfgs for a in e["aliases"]), key=lambda p: -len(p[0]))
        self.by_alias = {}
        for alias, ent in pairs:
            self.by_alias.setdefault(alias, ent)
        body = "|".join(re.escape(a).replace(r"\ ", r"[ \-]") for a, _ in pairs)
        self.rx = re.compile(rf"(?<![A-Za-z0-9])({body})(?![A-Za-z0-9\-])", re.IGNORECASE) if pairs else None

    def mentions(self, text):
        """[(początek, koniec, byt)] w kolejności w tekście; najdłuższy alias wygrywa na tym samym miejscu."""
        if not self.rx:
            return []
        out = []
        for match in self.rx.finditer(text):
            alias = re.sub(r"[ \-]", " ", match.group(1).lower())
            ent = self.by_alias.get(alias) or self.by_alias.get(match.group(1).lower())
            if ent:
                out.append((match.start(), match.end(), ent))
        return out

    @staticmethod
    def version_after(text, end):
        match = AFTER_VERSION_RE.match(text, end)
        return norm_version(match.group(1)) if match else None


def resolve(items, entities, specs):
    """Każda pozycja dostaje byt, wersję i znacznik wydania. Byt z tytułu wygrywa z domyślnym bytem
    źródła; wersja liczy się jako wydanie tylko ze źródła, które ogłasza własne wydania (releases)."""
    for item in items:
        spec = specs[item["source"]]
        text = item["title"]
        found = entities.mentions(text)
        if not found and spec["kind"] == "hn":
            found = entities.mentions(seo.slug_words(item["url"]))
            text = seo.slug_words(item["url"]) if found else text
        item["off_topic"] = bool(item.get("loose") and not found)
        ent, version = None, None
        if found:
            ent = found[0][2]
            version = entities.version_after(text, found[0][1])
        else:
            default = spec.get("entity") or item.get("default_entity")
            ent = entities.by_name.get(default) if default else None
        if version is None and spec["kind"] in ("github", "notes") and ent is not None:
            match = ANY_VERSION_RE.search(text)
            version = norm_version(match.group(1)) if match else None
        item["entity"] = ent["name"] if ent else None
        item["version"] = version if ent else None
        item["release"] = bool(spec.get("releases") and ent and version and not PRERELEASE_RE.search(text + " ")
                               and (not spec.get("release_title") or re.search(spec["release_title"], text)))  # fmt: skip
        # wydanie GitHub ma w tytule tylko numer; o bezpieczeństwie mówi jego treść
        item["security"] = bool(SECURITY_RE.search(item["title"]) or (spec["kind"] == "github" and SECURITY_RE.search(item["summary"])))
        item["official"] = spec["kind"] != "hn"


class Union:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def group(items):
    """Pozycje w zdarzenia: ten sam adres albo to samo wydanie (byt i wersja z własnego źródła
    wydań) to jedno zdarzenie; pozycja o wersji spoza źródeł wydań (HN, changelog Vercela) dołącza
    do wydania, jeśli takie jest, a inaczej zostaje przy swoim adresie."""
    union = Union(len(items))
    by_key, by_release = {}, {}
    for i, item in enumerate(items):
        if item["key"] in by_key:
            union.join(i, by_key[item["key"]])
        by_key.setdefault(item["key"], i)
        if item["release"]:
            rk = (item["entity"], item["version"])
            if rk in by_release:
                union.join(i, by_release[rk])
            by_release.setdefault(rk, i)
    for i, item in enumerate(items):
        if not item["release"] and item["entity"] and item["version"]:
            rk = (item["entity"], item["version"])
            if rk in by_release:
                union.join(i, by_release[rk])
    # oficjalny komunikat o bezpieczeństwie bytu i wydania tego bytu z poprawkami bezpieczeństwa z tych
    # samych dwóch dób to jedno zdarzenie (Next.js Security Update, v16.4.1 i v15.5.28)
    notices = [i for i, it in enumerate(items) if it["security"] and it["official"] and not it["release"] and it["entity"]]
    for i, item in enumerate(items):
        if item["release"] and item["security"]:
            for n in notices:
                if items[n]["entity"] == item["entity"] and abs(items[n]["published"] - item["published"]) <= BREAKING_HOURS * 3600:
                    union.join(i, n)
    groups = {}
    for i, item in enumerate(items):
        groups.setdefault(union.find(i), []).append(item)
    return list(groups.values())


# ---------- zdarzenia ----------


def event_id(key):
    return "e" + hashlib.sha1(key.encode()).hexdigest()[:8]


def hn_totals(items):
    stories = [dict(i["hn"], title=i["title"], url=i["url"]) for i in items if i.get("hn")]
    if not stories:
        return None
    return {"points": sum(s["points"] for s in stories), "comments": sum(s["comments"] for s in stories), "stories": stories[:5]}


def make_event(items, entities):
    """Jedno zdarzenie z grupy pozycji: rodzaj, byt i wersja z własnego źródła wydań, tytuł, adres."""
    official = [i for i in items if i["official"]] or items
    # najwyższa wersja nazywa zdarzenie (poprawki bezpieczeństwa wychodzą naraz dla kilku linii)
    releases = sorted((i for i in items if i["release"]), key=lambda i: (tuple(-n for n in version_key(i["version"])), i["published"]))
    ordered = sorted(official, key=lambda i: (KIND_ORDER[i["kind"]], i["published"]))
    primary = ordered[0]
    ent_name = (releases[0]["entity"] if releases else None) or next((i["entity"] for i in ordered if i["entity"]), None) or next(
        (i["entity"] for i in items if i["entity"]), None)  # fmt: skip
    ent = entities.by_name.get(ent_name) if ent_name else None
    # zdarzenie bez wydania też może być o wersji ("Next.js 16.4" na HN): do popytu i pokrycia, bez poziomu
    versions = sorted({i["version"] for i in releases}, key=version_key, reverse=True)
    version = releases[0]["version"] if releases else next((i["version"] for i in ordered if i["entity"] == ent_name and i["version"]), None)
    kinds = [i["kind"] for i in official]
    kind = "release" if releases or "release" in kinds else "announcement" if "announcement" in kinds else "article" if "article" in kinds else "discussion"
    if not any(i["official"] for i in items):
        kind = "discussion"
    title = primary["title"]
    if ent and version and not any(a in title.lower() for a in ent["aliases"]):
        title = f"{ent['name']} {version}"
    hn = hn_totals(items)
    if releases:
        key = f"r|{ent_name}|{version}"
    elif any(i["official"] for i in items):
        key = "u|" + min(i["key"] for i in official)
    else:
        key = "h|" + min(i["key"] for i in items)
    published = min(i["published"] for i in official)
    return {
        "key": key, "id": event_id(key), "kind": kind, "entity": ent, "version": version, "level": level_of(version) if releases else None,
        "title": title, "url": primary["url"], "published": published, "last_activity": max(i["published"] for i in items),
        "security": any(i["security"] for i in items if i["official"]), "hn": hn, "items": sorted(items, key=lambda i: i["published"]),
        "sources": sorted({i["source"] for i in items}), "summary": next((i["summary"] for i in ordered if i["summary"]), ""),
        "rollup": None, "versions": versions,
    }  # fmt: skip


def roll_up_patches(events):
    """Ciche wydania poprawkowe tego samego bytu (jedno źródło, mało punktów HN, bez bezpieczeństwa)
    zwijają się w jedno zdarzenie, żeby jeden bieg nie niósł tuzina "Claude Code 2.1.x"."""
    quiet = {}
    for ev in events:
        if ev["level"] == "patch" and len(ev["sources"]) == 1 and not ev["security"] and (ev["hn"] or {"points": 0})["points"] < QUIET_HN:
            quiet.setdefault(ev["entity"]["name"], []).append(ev)
    folded = {id(ev) for group_ in quiet.values() if len(group_) >= 2 for ev in group_}
    out = [ev for ev in events if id(ev) not in folded]
    for name, group_ in quiet.items():
        if len(group_) < 2:
            continue
        group_.sort(key=lambda e: version_key(e["version"]))
        items = [i for e in group_ for i in e["items"]]
        versions = [e["version"] for e in group_]
        key = f"p|{name}"
        out.append({
            "key": key, "id": event_id(key), "kind": "release", "entity": group_[0]["entity"], "version": None, "level": "patch",
            "title": f"{name} {versions[0]} do {versions[-1]}: {len(versions)} wydań poprawkowych", "url": group_[-1]["url"],
            "published": min(e["published"] for e in group_), "last_activity": max(e["last_activity"] for e in group_),
            "security": False, "hn": None, "items": sorted(items, key=lambda i: i["published"]), "sources": group_[0]["sources"],
            "summary": group_[-1]["summary"], "rollup": versions,
        })  # fmt: skip
    return out


def mentioned(ev, entities):
    """Byty i wersje, o których jest zdarzenie: główny pierwszy, potem do dwóch wspomnianych w tytułach i opisach."""
    out = []
    if ev["entity"]:
        out.append({"name": ev["entity"]["name"], "version": ev["version"], "level": ev["level"] if ev["version"] else None, "primary": True})
    seen = {e["name"] for e in out}
    for item in ev["items"]:
        if not item["official"]:
            continue
        text = item["title"] + ". " + item["summary"][:200]
        for _start, end, ent in entities.mentions(text):
            if ent["name"] in seen or len(out) >= 3:
                continue
            seen.add(ent["name"])
            version = entities.version_after(text, end)
            out.append({"name": ent["name"], "version": version, "level": level_of(version), "primary": False})
    return out


# ---------- popularność i popyt ----------


def hn_lookup(polite, specs_hn, events, since, limit):
    """Wątki HN o adresie zdarzenia, którego zapytania słów kluczowych nie znalazły (najważniejsze
    najpierw): dopisują punkty do zdarzenia. (liczba zapytań, błędy)."""
    if not specs_hn or limit <= 0:
        return 0, []
    spec, asked, errors = specs_hn[0], 0, []
    for ev in events:
        if asked >= limit or len(errors) >= 3:
            break
        # wpis notatek o wydaniach ma adres z kotwicą: wątek HN o całej stronie nie jest o tym wpisie
        if ev["hn"] or ev["kind"] == "discussion" or ev["rollup"] or "#" in ev["url"]:
            continue
        target = canon(ev["url"])
        if not target:
            continue
        host_path = target.split("://", 1)[1]
        query = {"q": host_path, "url": True, "min_points": 1, "entity": None}
        asked += 1
        try:
            hits, _ = hn_hits(polite, spec, hn_url(spec, query, 0))
        except seo.FetchError as exc:
            errors.append(str(exc)[:60])
            continue
        stories = [hn_item(spec, h, None) for h in hits if h["url"] and canon(h["url"]) == target]
        stories = [s for s in stories if s]
        if stories:
            for s in stories:
                s.update(entity=None, version=None, release=False, security=False, official=False)
            ev["items"] += stories
            ev["sources"] = sorted(set(ev["sources"]) | {spec["id"]})
            ev["hn"] = hn_totals(ev["items"])
    return asked, errors


def npm_weekly(polite, cfg, package):
    """Pobrania pakietu npm z ostatniego tygodnia (pamięć podręczna na dobę); None, gdy nie ma danych."""
    cache = polite.ctx.cache("radar-npm")
    got = cache.get(package, NPM_TTL)
    if got is None:
        resp = polite.get(f"{cfg['npm_api']}/{urllib.parse.quote(package, safe='@/')}")
        if resp.status != 200:
            raise seo.FetchError(f"HTTP {resp.status}")
        try:
            got = {"downloads": int(json.loads(seo.decode(resp))["downloads"])}
        except (ValueError, KeyError, TypeError):
            raise seo.FetchError("odpowiedź nie jest JSON-em npm")
        cache.put(package, got)
    return got["downloads"]


def attach_npm(polite, cfg, events):
    packages = sorted({e["entity"]["npm"] for e in events if e["entity"] and e["entity"]["npm"]})
    if not packages:
        return source("npm", False, "żaden byt nie ma pakietu npm w konfiguracji", skipped=True)
    got, errors = {}, []
    for package in packages:
        try:
            got[package] = npm_weekly(polite, cfg, package)
        except seo.FetchError as exc:
            errors.append(f"{package}: {str(exc)[:60]}")
            if len(errors) >= 3:
                break
    for ev in events:
        package = ev["entity"]["npm"] if ev["entity"] else None
        ev["npm"] = {"package": package, "downloads": got[package]} if package in got else None
    detail = f"{seo.plural(len(got), 'pakiet', 'pakiety', 'pakietów')} z {len(packages)}" + ("; błędy: " + "; ".join(errors) if errors else "")
    polite.ctx.save_caches()
    return source("npm", bool(got), detail, len(got), degraded=bool(errors))


def event_phrase(ev, entities):
    """Fraza do popytu: "next.js 16.4" dla wydania, inaczej byt i pierwsze słowa tytułu bez nazwy bytu."""
    ent = ev["entity"]
    if ev["version"] and ent:
        return f"{ent['phrase']} {ev['version']}"
    title = re.sub(r"['’]\w*", "", ev["title"].lower())  # we've, it's: bez końcówek ściągnięć
    for start, end, _ in reversed(entities.mentions(title)):
        title = title[:start] + title[end:]
    topic = [w for w in re.findall(r"[\w.+#]+", title) if w not in STOP_TOPIC and not w.isdigit()][:3]
    return " ".join(([ent["phrase"]] if ent else []) + topic).strip()


class TtlCache(seo.JsonCache):
    """JsonCache z własnym czasem życia wpisów: seo.suggest pyta z TTL K1 (28 dni), a nowe wydanie
    musi być sprawdzone znowu następnego dnia."""

    def __init__(self, path, now, ttl):
        super().__init__(path, now)
        self.ttl = ttl

    def get(self, key, ttl):
        return super().get(key, self.ttl)


def ac_score(phrase, found):
    words = phrase.split()
    for rank, text in enumerate(found):
        t = seo.norm(text)
        if t.startswith(phrase) or all(w in t for w in words):
            return round(max(0.1, 1 - rank / 10), 2)
    return 0.0


def attach_demand(ctx, blog, events, entities, limit):
    """Popyt na frazę zdarzenia z K1: autocomplete Google w językach bloga, GSC i Bing (gdy blog je ma).
    Tylko `limit` najważniejszych zdarzeń. Zwraca statusy źródeł."""
    chosen = events[:limit]
    for ev in events:
        ev["demand"] = {"phrase": event_phrase(ev, entities), "score": 0.0, "autocomplete": {}, "gsc": None, "bing": None}
    statuses = []
    asked = errors = 0
    stopped = None
    for loc in blog["locales"]:
        name = f"radar-suggest-{loc['lang']}-{loc['country']}"
        ctx.caches[name] = cache = TtlCache(os.path.join(ctx.cache_dir, seo.safe_name(name) + ".json"), ctx.now, DEMAND_TTL)
        state = {"fetched": 0, "cached": 0, "over": 0, "errors": 0, "budget": 60}
        for ev in chosen:
            phrase = ev["demand"]["phrase"]
            if not phrase or stopped:
                continue
            try:
                found = seo.suggest(ctx, loc, phrase, cache, state)
            except seo.Stop as exc:
                stopped = f"{loc['lang']}-{loc['country']}: {exc}"
                continue
            ev["demand"]["autocomplete"][f"{loc['lang']}-{loc['country']}"] = ac_score(phrase, found)
        asked += state["fetched"] + state["cached"]
        errors += state["errors"]
        ctx.save_caches()
    hits = sum(1 for ev in chosen if any(ev["demand"]["autocomplete"].values()))
    detail = f"{seo.plural(len(chosen), 'fraza', 'frazy', 'fraz')} w autocomplete, {hits} z podpowiedzią" + (f"; przerwane: {stopped}" if stopped else "")
    statuses.append(source("demand", bool(asked) and not (stopped and not hits), detail, hits, degraded=bool(stopped)))
    gsc, status = seo.gsc_rows(ctx, blog)
    statuses.append(status)
    for ev in chosen:
        words = ev["demand"]["phrase"].split()
        total = sum(q["impressions"] for q in gsc.values() if words and all(w in q["query"] for w in words))
        ev["demand"]["gsc"] = int(total) if total else None
    pool = {}
    for loc in blog["locales"]:
        for ev in chosen:
            if ev["demand"]["phrase"]:
                pool[(loc["lang"], id(ev))] = {"text": ev["demand"]["phrase"], "lang": loc["lang"], "demand": 0.0, "ev": ev}
    bing = seo.bing_stats(ctx, blog, pool) if pool else seo.source("bing", False, "brak fraz", skipped=True)
    statuses.append(bing)
    for p in pool.values():
        if p.get("bing"):
            p["ev"]["demand"]["bing"] = int(max(p["bing"], p["ev"]["demand"]["bing"] or 0))
    for ev in chosen:
        d = ev["demand"]
        parts = [0.0] + list(d["autocomplete"].values())
        if d["gsc"]:
            parts.append(min(1.0, math.log1p(d["gsc"]) / math.log1p(1000)))
        if d["bing"]:
            parts.append(min(1.0, math.log1p(d["bing"]) / math.log1p(500)))
        d["score"] = round(max(parts), 3)
    return statuses


# ---------- pilność i kolejność ----------


def age_hours(ev, now):
    return max(0.0, (now - ev["published"]) / 3600)


def urgency(ev, now):
    """(pilność, powód) według reguły z README, SEO radar."""
    age = age_hours(ev, now)
    hn = (ev["hn"] or {"points": 0})["points"]
    sources = len(ev["sources"])
    tier = ev["entity"]["tier"] if ev["entity"] else 3
    major_minor = ev["level"] in ("major", "minor") and not ev["rollup"]
    if tier == 1 and age <= BREAKING_HOURS:
        if ev["kind"] == "release" and major_minor and (sources >= 2 or hn >= BREAKING_RELEASE_HN):
            return "breaking", f"wydanie {ev['level']} bytu pierwszej rangi sprzed {age:.0f} h, {sources} źródeł, HN {hn} pkt"
        if ev["security"]:
            return "breaking", f"wydanie lub komunikat o bezpieczeństwie bytu pierwszej rangi sprzed {age:.0f} h"
        if ev["kind"] in ("announcement", "article") and (hn >= BREAKING_TALK_HN or sources >= 3):
            return "breaking", f"ogłoszenie bytu pierwszej rangi sprzed {age:.0f} h, {sources} źródeł, HN {hn} pkt"
    if age <= HIGH_DAYS * 24:
        if ev["kind"] == "release" and major_minor and tier <= 2:
            return "high", f"wydanie {ev['level']} bytu rangi {tier}, {age / 24:.1f} dnia temu"
        if ev["security"] and tier <= 2:
            return "high", "komunikat o bezpieczeństwie śledzonego bytu"
        if hn >= HIGH_HN:
            return "high", f"HN {hn} pkt"
        if tier == 1 and ev["kind"] != "discussion" and (sources >= 2 or hn >= 30):
            return "high", f"byt pierwszej rangi, {sources} źródeł, HN {hn} pkt"
    return "normal", "poza regułami breaking i high"


def score(ev, now):
    """Liczba 0..100 do kolejności w obrębie pilności: HN, niezależne źródła, pobrania npm, popyt, świeżość."""
    hn = ev["hn"]
    hn_part = min(1.0, math.log1p(hn["points"] + 2 * hn["comments"]) / math.log1p(1500)) if hn else 0.0
    src_part = min(1.0, (len(ev["sources"]) - 1) / 3)
    npm = (ev.get("npm") or {}).get("downloads") or 0
    npm_part = min(1.0, math.log10(npm) / 9) if npm > 1 else 0.0
    fresh = max(0.0, 1 - age_hours(ev, now) / (HIGH_DAYS * 24))
    tier = {1: 1.0, 2: 0.7}.get(ev["entity"]["tier"] if ev["entity"] else 3, 0.4)
    return round(100 * tier * (0.30 * hn_part + 0.20 * src_part + 0.10 * npm_part + 0.20 * ev["demand"]["score"] + 0.20 * fresh), 1)


def rank_events(events, now):
    for ev in events:
        ev["urgency"], ev["urgency_reason"] = urgency(ev, now)
        ev["score"] = score(ev, now)
    return sorted(events, key=lambda e: (URGENCIES.index(e["urgency"]), -e["score"], -e["published"]))


# ---------- wpisy bloga ----------


def llms_titles(polite, blog, include):
    """{adres: tytuł} z llms.txt bloga (linki Markdown do wpisów); puste, gdy pliku nie ma."""
    audit = blog.get("audit") or {}
    url = audit.get("llms") or blog["site"] + "/llms.txt"
    resp = polite.get(url, limit=2 * 1024 * 1024)
    if resp.status != 200:
        raise seo.FetchError(f"HTTP {resp.status}")
    out = {}
    for title, link in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", seo.decode(resp)):
        if include.search(seo.url_path(link)):
            out[canon(link)] = seo.clean(title, 140)
    return out


def blog_posts(ctx, polite, blog):
    """Wpisy bloga z mapy strony (K1: seo.load_posts) z tytułami z llms.txt; każde źródło osobno. (wpisy, statusy)."""
    posts, status = seo.load_posts(ctx, blog)
    statuses = [status]
    spec = blog.get("posts") or {}
    include = re.compile(spec.get("include") or r"/blog/[^/]+$")
    try:
        titles = llms_titles(polite, blog, include)
        statuses.append(source("llms", True, f"{seo.plural(len(titles), 'wpis', 'wpisy', 'wpisów')} w llms.txt", len(titles)))
    except (seo.FetchError, ValueError) as exc:
        titles = {}
        statuses.append(source("llms", False, f"llms.txt: {str(exc)[:100]}"))
    by_key = {canon(p["url"]): p for p in posts}
    for key, title in titles.items():
        if key in by_key:
            by_key[key]["title"] = title or by_key[key]["title"]
        else:
            path = seo.url_path(key)
            by_key[key] = {"url": key, "path": path, "lang": seo.lang_of_path(blog, path), "title": title}
    return list(by_key.values()), statuses


def attach_coverage(events, posts, entities):
    """Czy blog już pisał o zdarzeniu: wpis z nazwą bytu i wersją zdarzenia (wydanie), albo wpis o
    prawie tych samych słowach co tytuł; wpisy o tym samym bycie bez wersji to related."""
    prepared = []
    for p in posts:
        hay = seo.fold(p["title"] + " " + seo.slug_words(p["url"]))
        prepared.append((p, hay, seo.keyset(p["title"]) | seo.keyset(seo.slug_words(p["url"]))))
    for ev in events:
        covered, related = [], []
        ks = seo.keyset(ev["title"])
        ent = ev["entity"]
        version_rx = None
        if ent and ev["version"]:
            dotted = re.escape(ev["version"]).replace("\\.", "[. -]")
            version_rx = re.compile(r"(?<!\d)" + dotted + r"(?!\d)")
        for p, hay, pk in prepared:
            about = bool(ent) and any(seo.fold(a).replace(".", "") in hay.replace(".", "") for a in ent["aliases"])
            if version_rx and about and version_rx.search(hay):
                covered.append({"url": p["url"], "title": p["title"], "match": "byt i wersja"})
            elif not version_rx and len(ks) >= 2 and seo.containment(ks, pk) >= 0.6:
                covered.append({"url": p["url"], "title": p["title"], "match": "tytuł"})
            elif about:
                related.append({"url": p["url"], "title": p["title"], "match": "ten sam byt"})
        ev["covered"] = {"covered": bool(covered), "posts": covered[:3], "related": related[:3]}


# ---------- propozycja wpisu ----------


def propose(ev, entities):
    """Typ, kąt, słowa kluczowe EN i PL oraz pomysł na demo, z reguł; sesja z WebSearch poprawia kąt i demo najważniejszych."""
    ent = ev["entity"]
    name = ent["name"] if ent else None
    hn = (ev["hn"] or {"points": 0})["points"]
    tier = ent["tier"] if ent else 3
    if ev["urgency"] in ("breaking", "high") and ev["kind"] in ("release", "announcement") and tier <= 2 and not ev["rollup"]:
        ptype = "release"
    elif ent and ev["kind"] in ("article", "announcement") and (hn >= 30 or len(ev["sources"]) >= 2):
        ptype = "tutorial"
    elif ent and ev["kind"] == "discussion" and hn >= HIGH_HN:
        ptype = "tutorial"  # wątek, o którym mówi cały HN: poradnik o tym, czego dotyczy
    else:
        ptype = "digest"
    phrase = ev["demand"]["phrase"] or (name or "").lower()
    topic = ev["title"]
    if ptype == "release" and ev["security"] and name:
        # poprawka bezpieczeństwa: kogo dotyczy i jak się zaktualizować, bez "nowych funkcji" i bez kodu exploita
        fixed = ", ".join(ev.get("versions") or ([ev["version"]] if ev["version"] else []))
        angle = f"What the {name} security update{f' ({fixed})' if fixed else ''} fixes, which apps are affected, and how to upgrade and verify it."
        demo = f"A lab check that pins the patched {name} version and a Playwright test of the fixed behaviour, with no exploit code."
        base = name.lower()
        en = [phrase, f"{base} security update", f"{base} vulnerability", f"{phrase} upgrade"]
        pl = [phrase, f"{base} aktualizacja bezpieczeństwa", f"{base} luka", f"{phrase} aktualizacja"]
    elif ptype == "release" and ev["version"]:
        label = f"{name} {ev['version']}"
        angle = f"What {label} changes in practice: its headline features tried in a working demo, with versions, what changed and what broke."
        demo = f"One lab demo per headline feature of {label}, each with a Playwright test that asserts what the post claims."
        en = [phrase, f"what's new in {phrase}", f"{phrase} new features", f"{phrase} migration"]
        pl = [phrase, f"{phrase} nowości", f"co nowego w {phrase}", f"{phrase} migracja"]
    elif ptype == "release":
        angle = f"What \"{topic}\" means for developers: tried in a working demo, with versions and what broke."
        demo = f"A lab demo of the headline change in \"{topic}\" with a Playwright test."
        en = [phrase, f"{phrase} explained", f"{phrase} what changed"]
        pl = [phrase, f"{phrase} co nowego", f"{phrase} jak działa"]
    elif ptype == "tutorial":
        angle = f"A hands-on take on \"{topic}\": the idea reproduced step by step in a demo whose code is the code shown in the post."
        demo = f"A minimal lab route that reproduces \"{topic}\" with a Playwright test asserting the claim of the post."
        en = [phrase, f"{phrase} tutorial", f"{phrase} example"]
        pl = [phrase, f"{phrase} poradnik", f"{phrase} przykład"]
    else:
        angle = f"One-line take on \"{topic}\" with a link to the primary source, for the weekly digest."
        demo = None
        en = [phrase] if phrase else []
        pl = [phrase] if phrase else []
    return {"type": ptype, "category": ent["category"] if ent else None, "angle": seo.no_dashes(angle), "demo": demo and seo.no_dashes(demo),
            "keywords": {"en": [seo.no_dashes(k) for k in dict.fromkeys(en) if k], "pl": [seo.no_dashes(k) for k in dict.fromkeys(pl) if k]}}  # fmt: skip


# ---------- X pośrednio: sesja z WebSearch ----------


def x_prompt(blog, events):
    rows = [{"id": ev["id"], "title": ev["title"], "entity": ev["entity"]["name"] if ev["entity"] else None, "version": ev["version"],
             "published": seo.iso(ev["published"])[:10], "urls": [i["url"] for i in ev["items"] if i["official"]][:3],
             "hn": ev["hn"] and {"points": ev["hn"]["points"], "comments": ev["hn"]["comments"]}} for ev in events]  # fmt: skip
    return f"""You research what developers say about the newest tech events, for the blog {blog['domain']}.
Blog: {blog['about']}

Events (JSON, newest news about frameworks, platforms and AI tools):
{json.dumps(rows, ensure_ascii=False)}

For each event run at most 2 WebSearch queries in English, in batches of at most 4 searches per turn. Open a
page with WebFetch only when a search result is not enough.
- X (Twitter) cannot be searched directly. Look for links to posts on x.com or twitter.com that news sites,
  Hacker News threads, blogs and search results cite. List only links you actually saw; never invent one.
- "discussion": one or two sentences on what developers say (reactions, complaints, praise).
- "covered_by": up to 3 existing posts or tutorials on this event that are not the official announcement,
  with a short note on how good they are and what they lack.
- "gap": one sentence on what a new post could add that these lack.
- "angle": one sentence, the best angle for a post on this blog (tutorials with a working demo, and
  what's-new posts with versions, what changed and what broke). "demo": one sentence, a demo idea.
{seo.UNTRUSTED}

After every batch, before the next one, write the file {{OUT}} again with everything found so far, so the file
is complete even if you are stopped. It holds JSON and nothing else:
{{"events": [{{"id": "<event id>", "discussion": "...", "x_posts": [{{"url": "https://x.com/...", "summary": "..."}}],
"covered_by": [{{"url": "https://...", "title": "...", "note": "..."}}], "gap": "...", "angle": "...", "demo": "..."}}]}}
Write no other file."""


def read_x(data, events, domain):
    """Odpowiedź sesji na zdarzenia: tylko znane id, adresy http(s), posty X tylko z x.com i twitter.com,
    każdy tekst przez clean (linia z poleceniem znika). {id zdarzenia: wynik}."""
    ids = {ev["id"] for ev in events}
    out = {}
    for row in (data or {}).get("events") or []:
        if not isinstance(row, dict) or row.get("id") not in ids or row["id"] in out:
            continue
        posts = []
        for p in row.get("x_posts") or []:
            url = p.get("url") if isinstance(p, dict) else None
            if isinstance(url, str) and len(url) <= 300 and seo.registrable(seo.host_of(url)) in ("x.com", "twitter.com") and canon(url):
                posts.append({"url": url, "summary": seo.clean(p.get("summary"), 200)})
        covered = []
        for c in row.get("covered_by") or []:
            url = c.get("url") if isinstance(c, dict) else None
            if isinstance(url, str) and len(url) <= 300 and canon(url) and not seo.on_domain(url, domain):
                covered.append({"url": url, "title": seo.clean(c.get("title"), 140), "note": seo.clean(c.get("note"), 200)})
        out[row["id"]] = {"discussion": seo.clean(row.get("discussion"), 400), "x_posts": posts[:5], "covered_by": covered[:4],
                          "gap": seo.clean(row.get("gap"), 300), "angle": seo.clean(row.get("angle"), 300), "demo": seo.clean(row.get("demo"), 300)}  # fmt: skip
    return out


def x_session(ctx, blog, events):
    """Jedna sesja Sonnet z WebSearch o najważniejszych zdarzeniach; (wyniki, status źródła)."""
    if not events:
        return {}, source("x", False, "brak zdarzeń do przejrzenia", skipped=True)
    ctx.log(f"X pośrednio: sesja Sonnet z WebSearch, {seo.plural(len(events), 'zdarzenie', 'zdarzenia', 'zdarzeń')}")
    data, problem = seo.session(ctx, blog, "radar", x_prompt(blog, events))
    if problem:
        return {}, source("x", False, problem)
    found = read_x(data, events, blog["domain"])
    if not found:
        return {}, source("x", False, "sesja nie oddała rozmów o zdarzeniach")
    posts = sum(len(v["x_posts"]) for v in found.values())
    return found, source("x", True, f"{seo.plural(len(found), 'zdarzenie', 'zdarzenia', 'zdarzeń')} z {len(events)}, {seo.plural(posts, 'post X', 'posty X', 'postów X')} cytowanych (przybliżone)", len(found))


def merge_x(events, found):
    for ev in events:
        row = found.get(ev["id"])
        ev["x"] = None if row is None else {k: row[k] for k in ("discussion", "x_posts", "covered_by", "gap")}
        if row:
            # "Skip.", "None." i podobne to brak odpowiedzi: zostaje propozycja z reguł
            if meaningful(row["angle"]):
                ev["proposal"]["angle"] = row["angle"]
            if meaningful(row["demo"]):
                ev["proposal"]["demo"] = row["demo"]


def meaningful(text):
    return bool(text) and not re.fullmatch(r"(?i)\W*(none|skip|n/?a|null|-|unknown|not applicable)\W*", text)


# ---------- radar.json, schemat v1 ----------

_SOURCE = seo.SCHEMA_V1["properties"]["sources"]["items"]
_ITEM = {
    "type": "object", "required": ["source", "kind", "title", "url", "published_at"],
    "properties": {"source": {"type": "string"}, "kind": {"enum": list(ITEM_KINDS)}, "title": {"type": "string", "minLength": 1},
                   "url": {"type": "string", "minLength": 1}, "published_at": {"type": "string"}},
}  # fmt: skip
_EVENT = {
    "type": "object",
    "required": ["id", "kind", "title", "url", "entities", "version", "level", "published_at", "first_seen_at", "new", "age_hours", "urgency",
                 "urgency_reason", "score", "popularity", "items", "summary", "covered", "x", "proposal"],
    "properties": {
        "id": {"type": "string", "pattern": "^e[0-9a-f]{8}$"},
        "kind": {"enum": list(ITEM_KINDS)},
        "title": {"type": "string", "minLength": 1},
        "url": {"type": "string", "minLength": 1},
        "entities": {"type": "array", "items": {"type": "object", "required": ["name", "version", "level", "primary"],
                                                "properties": {"name": {"type": "string"}, "version": {"type": ["string", "null"]},
                                                               "level": {"enum": list(LEVELS) + [None]}, "primary": {"type": "boolean"}}}},
        "version": {"type": ["string", "null"]},
        "level": {"enum": list(LEVELS) + [None]},
        "rollup": {"type": ["array", "null"], "items": {"type": "string"}},
        "published_at": {"type": "string"},
        "first_seen_at": {"type": "string"},
        "new": {"type": "boolean"},
        "age_hours": {"type": "number", "minimum": 0},
        "urgency": {"enum": list(URGENCIES)},
        "urgency_reason": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "popularity": {
            "type": "object", "required": ["hn", "sources", "source_ids", "npm_weekly", "demand"],
            "properties": {
                "hn": {"type": ["object", "null"], "required": ["points", "comments", "stories"],
                       "properties": {"points": {"type": "integer", "minimum": 0}, "comments": {"type": "integer", "minimum": 0},
                                      "stories": {"type": "array", "items": {"type": "object", "required": ["id", "points", "comments", "discussion"]}}}},
                "sources": {"type": "integer", "minimum": 1},
                "source_ids": {"type": "array", "items": {"type": "string"}},
                "npm_weekly": {"type": ["object", "null"], "required": ["package", "downloads"]},
                "demand": {"type": "object", "required": ["phrase", "score", "autocomplete", "gsc", "bing"],
                           "properties": {"phrase": {"type": "string"}, "score": {"type": "number", "minimum": 0, "maximum": 1}}},
            },
        },
        "items": {"type": "array", "minItems": 1, "items": _ITEM},
        "summary": {"type": "string"},
        "covered": {"type": "object", "required": ["covered", "posts", "related"], "properties": {"covered": {"type": "boolean"}}},
        "x": {"type": ["object", "null"], "required": ["discussion", "x_posts", "covered_by", "gap"]},
        "proposal": {
            "type": "object", "required": ["type", "category", "angle", "keywords", "demo"],
            "properties": {"type": {"enum": list(PROPOSALS)}, "angle": {"type": "string", "minLength": 1},
                           "keywords": {"type": "object", "required": ["en", "pl"],
                                        "properties": {"en": {"type": "array", "items": {"type": "string"}},
                                                       "pl": {"type": "array", "items": {"type": "string"}}}}},
        },
    },
}  # fmt: skip
SCHEMA_V1 = {
    "$id": "claude-acc/seo/radar/v1",
    "type": "object",
    "required": ["schema", "v", "blog", "domain", "generated_at", "window_days", "sources", "x", "summary", "events"],
    "properties": {
        "schema": {"const": "claude-acc.seo.radar"},
        "v": {"const": 1},
        "blog": {"type": "string"},
        "domain": {"type": "string"},
        "generated_at": {"type": "string"},
        "window_days": {"type": "integer", "minimum": 1},
        "sources": {"type": "array", "items": _SOURCE},
        "x": {"type": ["object", "null"], "required": ["approx", "checked", "with_x_posts"], "properties": {"approx": {"const": True}}},
        "summary": {"type": "object", "required": ["events", "breaking", "high", "normal", "covered", "new"]},
        "events": {"type": "array", "items": _EVENT},
    },
}  # fmt: skip


def validate(doc):
    return seo.validate(doc, SCHEMA_V1)


def event_doc(ev, now, state):
    seen = state["events"].get(ev["id"])
    first = min(seen["first_seen"], now) if seen else now
    popularity = {
        "hn": ev["hn"], "sources": len(ev["sources"]), "source_ids": ev["sources"], "npm_weekly": ev.get("npm"), "demand": ev["demand"],
    }  # fmt: skip
    return {
        "id": ev["id"], "kind": ev["kind"], "title": ev["title"], "url": ev["url"], "entities": ev["entities"], "version": ev["version"],
        "level": ev["level"], "rollup": ev["rollup"], "published_at": seo.iso(ev["published"]), "first_seen_at": seo.iso(first),
        "new": seen is None, "age_hours": round(age_hours(ev, now), 1), "urgency": ev["urgency"], "urgency_reason": seo.no_dashes(ev["urgency_reason"]),
        "score": ev["score"], "popularity": popularity,
        "items": [{"source": i["source"], "kind": i["kind"], "title": i["title"], "url": i["url"], "published_at": seo.iso(i["published"])}
                  for i in ev["items"][:DEFAULT_LIMITS["items_per_event"]]],
        "summary": ev["summary"], "covered": ev["covered"], "x": ev["x"], "proposal": ev["proposal"],
    }  # fmt: skip


def load_state(path):
    data = seo.read_json(path, {})
    if not isinstance(data, dict) or not isinstance(data.get("events"), dict):
        return {"v": 1, "events": {}}
    return data


def next_state(state, events, now):
    keep = {k: v for k, v in state["events"].items() if isinstance(v, dict) and now - v.get("last_seen", 0) < 45 * DAY}
    for ev in events:
        old = keep.get(ev["id"])
        keep[ev["id"]] = {"first_seen": old["first_seen"] if old else now, "last_seen": now, "title": ev["title"][:120]}
    return {"v": 1, "events": keep}


# ---------- wyjście dla człowieka ----------

UR_PL = {"breaking": "BREAKING", "high": "high", "normal": "normal"}
KIND_PL = {"release": "wydanie", "announcement": "ogłoszenie", "article": "artykuł", "discussion": "dyskusja"}
TYPE_PL = {"release": "wpis o wydaniu", "tutorial": "poradnik", "digest": "pozycja do digestu"}


def event_lines(i, ev):
    p = ev["popularity"]
    hn = f"HN {p['hn']['points']} pkt/{p['hn']['comments']} kom." if p["hn"] else "bez HN"
    npm = f"npm {p['npm_weekly']['downloads'] / 1e6:.1f} mln/tydz." if p["npm_weekly"] else "bez npm"
    d = p["demand"]
    demand = f"popyt {d['score']:.2f}" + ("" if d["score"] else " (brak podpowiedzi)")
    age = f"{ev['age_hours']:.0f} h" if ev["age_hours"] < 72 else f"{ev['age_hours'] / 24:.1f} d"
    head = f"{i:>2}  {UR_PL[ev['urgency']]:<8} {ev['score']:>5.1f}  {seo.fit_text(ev['title'], 58):<58} {KIND_PL[ev['kind']]:<10} {age:>6}"
    lines = [head,
             f"{'':>6}źródła ({p['sources']}): {', '.join(p['source_ids'])} | {hn} | {npm} | {demand}",
             f"{'':>6}pilność: {ev['urgency_reason']}"]  # fmt: skip
    if ev["covered"]["covered"]:
        lines.append(f"{'':>6}blog już pisał: " + ", ".join(c["url"] for c in ev["covered"]["posts"]))
    else:
        lines.append(f"{'':>6}blog nie pisał o tym" + (f" (podobne: {len(ev['covered']['related'])})" if ev["covered"]["related"] else ""))
    pr = ev["proposal"]
    lines.append(f"{'':>6}wpis: {TYPE_PL[pr['type']]}" + (f" [{pr['category']}]" if pr["category"] else "") + f" | {seo.fit_text(pr['angle'], 120)}")
    lines.append(f"{'':>6}EN: {', '.join(pr['keywords']['en'][:3]) or '-'} | PL: {', '.join(pr['keywords']['pl'][:3]) or '-'}" + (f" | demo: {seo.fit_text(pr['demo'], 90)}" if pr["demo"] else ""))
    if ev["x"]:
        if ev["x"]["discussion"]:
            lines.append(f"{'':>6}X/dyskusje: {seo.fit_text(ev['x']['discussion'], 140)}")
        for post in ev["x"]["x_posts"][:2]:
            lines.append(f"{'':>6}post X: {post['url']}")
        if ev["x"]["gap"]:
            lines.append(f"{'':>6}luka: {seo.fit_text(ev['x']['gap'], 140)}")
    lines.append(f"{'':>6}{ev['url']}")
    return lines


def report(doc, n=15):
    lines = [f"{'#':>2}  {'pilność':<8} {'wynik':>5}  {'zdarzenie':<58} {'rodzaj':<10} {'wiek':>6}"]
    for i, ev in enumerate(doc["events"][:n], 1):
        lines += event_lines(i, ev)
    return "\n".join(seo.no_dashes(line) for line in lines)


def wynik_lines(blog, doc, outcome, path):
    s = doc["summary"]
    lines = [f"WYNIK: {outcome}",
             f"Radar tematów {blog['name']}: {seo.plural(s['events'], 'zdarzenie', 'zdarzenia', 'zdarzeń')} z {doc['window_days']} dni "
             f"({s['breaking']} breaking, {s['high']} high, {s['new']} nowych, {s['covered']} już opisanych)."]  # fmt: skip
    top = [e for e in doc["events"] if e["urgency"] != "normal"][:3] or doc["events"][:3]
    if top:
        lines.append("Najważniejsze: " + "; ".join(f"\"{e['title']}\" ({e['urgency']}, {e['proposal']['type']})" for e in top))
    lines.append("Źródła: " + "; ".join(f"{s_['name']} {'ok' if s_['ok'] else 'pominięte' if s_['skipped'] else 'brak'}: {s_['detail']}" for s_ in doc["sources"]))
    if path:
        lines.append(f"Plik: {path}")
    return [seo.no_dashes(line) for line in lines]


# ---------- bieg ----------


def radar(ctx, blog, dry=False, web=True, out_dir=None):
    """Cały bieg dla jednego bloga; zwraca kod wyjścia (0 albo 1)."""
    cfg = radar_config(blog)
    out_dir = out_dir or os.path.join(seo.SEO_DIR, blog["name"])
    now = ctx.now()
    keep_since = now - cfg["window_days"] * DAY
    polite = Polite(ctx)
    entities = Entities(cfg["entities"])
    specs = {s["id"]: s for s in cfg["sources"]}
    items, sources = collect(polite, cfg, now, keep_since)
    delivered = [s for s in sources if s["ok"]]
    if not delivered:
        reason = "żadne źródło nic nie dało (" + "; ".join(f"{s['name']}: {s['detail']}" for s in sources) + ")"
        if not dry:
            seo.write_result({"outcome": "BŁĄD", "reason": seo.no_dashes(reason)[:400], "title": None, "urls": [], "pr": None,
                              "retryable": True, "notes": "poprzedni radar.json zostaje"})  # fmt: skip
        print(seo.no_dashes(f"\nWYNIK: BŁĄD\nRadar tematów {blog['name']}: {reason}"))
        return 1
    resolve(items, entities, specs)
    off_topic = [i for i in items if i["off_topic"]]
    if off_topic:
        items = [i for i in items if not i["off_topic"]]
        for status in sources:
            dropped = sum(1 for i in off_topic if i["source"] == status["name"])
            if dropped:
                status["detail"] += seo.no_dashes(f", {dropped} bez śledzonego bytu w tytule odrzucono")
                status["count"] = max(0, status["count"] - dropped)
    events = [make_event(g, entities) for g in group(items)]
    events = roll_up_patches(events)
    for ev in events:
        ev["entities"] = mentioned(ev, entities)
        ev["demand"] = {"phrase": "", "score": 0.0, "autocomplete": {}, "gsc": None, "bing": None}
        ev["covered"] = {"covered": False, "posts": [], "related": []}
        ev["x"] = None
    events = rank_events(events, now)
    hn_specs = [s for s in cfg["sources"] if s["kind"] == "hn" and next((x for x in sources if x["name"] == s["id"]), {}).get("ok")]
    asked, hn_errors = hn_lookup(polite, hn_specs, events, keep_since, cfg["limits"]["hn_lookups"])
    if asked:
        sources.append(source("hn-adresy", not hn_errors or asked > len(hn_errors), f"{seo.plural(asked, 'zapytanie', 'zapytania', 'zapytań')} o adres zdarzenia"
                              + ("; błędy: " + "; ".join(hn_errors) if hn_errors else ""), asked, degraded=bool(hn_errors)))  # fmt: skip
    sources.append(attach_npm(polite, cfg, events))
    events = rank_events(events, now)
    try:
        sources += attach_demand(ctx, blog, events, entities, cfg["limits"]["demand_events"])
    except Exception as exc:  # popyt nie przewraca biegu
        sources.append(source("demand", False, f"błąd: {exc!r}"[:160]))
    try:
        posts, statuses = blog_posts(ctx, polite, blog)
        sources += statuses
        attach_coverage(events, posts, entities)
    except Exception as exc:
        sources.append(source("posts", False, f"błąd: {exc!r}"[:160]))
    events = rank_events(events, now)[: cfg["limits"]["events"]]
    for ev in events:
        ev["proposal"] = propose(ev, entities)
    x_summary = None
    if web:
        top = events[: cfg["limits"]["x_events"]]
        found, status = x_session(ctx, blog, top)
        sources.append(status)
        merge_x(top, found)
        x_summary = {"approx": True, "checked": len(top), "with_x_posts": sum(1 for e in top if e["x"] and e["x"]["x_posts"]), "checked_at": seo.iso(ctx.now())}
    else:
        sources.append(source("x", False, "pominięte (--no-web)", skipped=True))
    ctx.save_caches()
    state = load_state(os.path.join(out_dir, "radar-state.json"))
    docs = [event_doc(ev, now, state) for ev in events]
    count = lambda u: sum(1 for e in docs if e["urgency"] == u)  # noqa: E731
    doc = {
        "schema": "claude-acc.seo.radar", "v": 1, "blog": blog["name"], "domain": blog["domain"], "generated_at": seo.iso(now),
        "window_days": cfg["window_days"], "sources": sources, "x": x_summary,
        "summary": {"events": len(docs), "breaking": count("breaking"), "high": count("high"), "normal": count("normal"),
                    "covered": sum(1 for e in docs if e["covered"]["covered"]), "new": sum(1 for e in docs if e["new"])},
        "events": docs,
    }  # fmt: skip
    problems = validate(doc)
    if problems:
        raise RuntimeError("radar.json nie przechodzi schematu v1: " + "; ".join(problems[:5]))
    print(report(doc))
    if dry:
        print(seo.no_dashes(f"\nNa sucho: nic nie zapisano (radar.json, radar-state.json, wynik joba). Koszt sesji: {ctx.spent:.2f} USD."))
        for s in sources:
            print(seo.no_dashes(f"  {s['name']}: {'ok' if s['ok'] else 'pominięte' if s['skipped'] else 'brak'}, {s['detail']}"))
        return 0
    path = os.path.join(out_dir, "radar.json")
    seo.write_result({"phase": "side-effect"})
    seo.atomic_write(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n", 0o644)
    seo.atomic_write(os.path.join(out_dir, "radar-state.json"), json.dumps(next_state(state, events, now), ensure_ascii=False) + "\n")
    degraded = [f"{s['name']}: {s['detail']}" for s in sources if (not s["ok"] and not s["skipped"]) or s["degraded"]]
    s = doc["summary"]
    top = [e["title"] for e in docs if e["urgency"] == "breaking"][:2]
    seo.write_result({
        "outcome": "OPUBLIKOWANO", "title": f"Radar tematów {blog['name']}: {seo.plural(s['events'], 'zdarzenie', 'zdarzenia', 'zdarzeń')}",
        "reason": seo.no_dashes(f"radar.json: {s['events']} zdarzeń ({s['breaking']} breaking, {s['high']} high, {s['new']} nowych)"
                                + ("; breaking: " + "; ".join(top) if top else "")),
        "urls": [], "pr": None, "retryable": False, "notes": seo.no_dashes("; ".join(degraded))[:600],
    })  # fmt: skip
    print("\n" + "\n".join(wynik_lines(blog, doc, "OPUBLIKOWANO", path)))
    return 0


def no_claude(argv, prompt, cwd, timeout):
    raise RuntimeError("--no-web: sesja Claude nie powinna się zacząć")


def cmd_radar(args):
    if not args or args[0].startswith("-"):
        raise seo.UsageError("claude-acc seo radar BLOG [--dry] [--no-web]")
    name, flags = args[0], args[1:]
    unknown = [f for f in flags if f not in ("--dry", "--no-web")]
    if unknown:
        raise seo.UsageError(f"nieznana opcja {unknown[0]}; są --dry i --no-web")
    dry, web = "--dry" in flags, "--no-web" not in flags
    config = seo.load_config()
    blog = seo.blog_config(config, name)
    radar_config(blog)
    if web and not os.environ.get("CLAUDE_ACC_RUN"):
        return seo.wrap(name, blog, flags, sub="radar")
    ctx = seo.Ctx(claude=None if web else no_claude)
    try:
        return radar(ctx, blog, dry=dry, web=web)
    except Exception as exc:
        if not dry:
            seo.write_result({"outcome": "BŁĄD", "reason": seo.no_dashes(f"radar {name}: {exc!r}")[:400], "title": None,
                              "urls": [], "pr": None, "retryable": True, "notes": ""})  # fmt: skip
        raise


if __name__ == "__main__":
    sys.modules.setdefault("seoradar", sys.modules[__name__])
    try:
        sys.exit(cmd_radar(sys.argv[1:]))
    except seo.UsageError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        sys.exit(2)
