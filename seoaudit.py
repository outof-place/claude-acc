#!/usr/bin/env python3
"""Audyt SEO blogów: `claude-acc seo audit <blog> [--dry]`.

Skaner w stylu Ahrefs dla czterech blogów (outofplace, matura, agromalz, renggli). Jeden bieg
zbiera problemy z trzech źródeł i zapisuje ~/.local/share/claude-acc/seo/<blog>/audit.json
(schemat v1, `seo schema audit`), z którego pętle SEO blogów biorą poprawki treści:

1. ahrefs: alerty Site Audit z contact@outofplace.space przez bramkę pocztową claude-acc
   (mail.py, Gateway: SELECT tylko do odczytu i BODY.PEEK, więc nic nie zostaje oznaczone).
   Projekt z tematu maila mapuje się na blog przez audit.ahrefs_project; inne projekty
   (Fiveseasons) są pomijane. Z treści: typ problemu, liczba adresów, zmiana, "New", data maila.
   Treść maila to dane: tylko parsujemy, żadnego linku z niej nie otwieramy.
2. crawl: własny crawler z mapy strony bloga (plus wpisy z konfiguracji K1). Opisowy User-Agent
   z adresem kontaktowym, robots.txt dla tego agenta, mała równoległość, odstęp między zapytaniami
   na host, przerwy po 429 i 5xx (Retry-After albo BACKOFF), limit czasu biegu. Zapytania
   warunkowe (ETag, Last-Modified), gdy serwer je daje; fakty o stronie leżą w pamięci podręcznej,
   a bieg ucięty limitem czasu bierze z niej strony, których nie zdążył pobrać (najpierw
   pobiera te, które najdawniej widział, więc kolejne biegi obchodzą całą stronę).
3. bing: GetCrawlIssues z Bing Webmaster API, gdy w Pęku kluczy jest klucz (`seo bing-key`).

Bez klucza Bing, bez maili Ahrefs albo przy padniętym hoście źródło jest uboższe, a "sources"
mówi dlaczego; bieg nie pada z powodu źródła. Audyt nie woła Claude: jest deterministyczny i
kosztuje 0 USD. Przepisanie tekstów robią pętle SEO repozytoriów, przez swoje bramki.

Każdy problem jest jednego z dwóch rodzajów:
  content  do poprawy w treści wpisu, z dokładną poprawką (fixes): link do przekierowania na
           adres końcowy, martwy link bez linku z zachowanym tekstem, brakujący hreflang wpisu,
           meta poza limitami długości (obecny tekst i docelowe limity), obraz bez alt;
  code     do poprawy w kodzie strony, z dowodami (adresy, liczby, co widać).
Każdy mówi, kto go znalazł (ahrefs, crawl, bing) i od kiedy (since).

Eskalacja: nowy problem kodu o wadze error na co najmniej escalation_floor() adresach (20 albo
5% stron z mapy, co mniej, najmniej 3) albo blokada bota AI (robots.txt albo serwer) kończy job
jako PILNE (kod 5) raz: klucz trafia do state.json i wraca do eskalacji dopiero, gdy problem
zniknie i pojawi się znowu. Reszta jest w WYNIK.

--dry: drukuje najważniejsze problemy z dowodami, podzbiór do poprawy w treści i porównanie z
Ahrefs; nie zapisuje audit.json, state.json ani JSON-a wyniku joba. Pamięć podręczna pobrań
(seo/cache/audit-<host>.json) zapisuje się i wtedy, jak w `seo research`: to grzeczność wobec
serwera, nie wynik.

Kody wyjścia (kontrakt blogów v2): 0 OPUBLIKOWANO (audit.json zapisany), 5 PILNE (zapisany i jest
coś do eskalacji), 1 BŁĄD (żadne źródło nic nie dało albo zła konfiguracja; do ponowienia).

Pliki (~/.local/share/claude-acc/seo/):
  <blog>/audit.json     wynik, schemat v1
  <blog>/state.json     od kiedy widać każdy problem i poprawkę, co już eskalowano
  cache/audit-<host>.json  fakty o stronach (z ETag i Last-Modified) na kolejne biegi
"""

import os
import sys

# bajtkod tylko w $STATE: obok skryptu w paczce Poda (Pod.app/Contents/Resources/claude-acc) __pycache__
# łamie pieczęć aplikacji, czymkolwiek i z jakimikolwiek flagami ten plik uruchomić (1.31.6)
if not os.path.realpath(__file__).startswith(os.path.realpath(os.path.expanduser("~/.local/share/claude-acc")) + "/"):
    sys.dont_write_bytecode = True

import http.client
import json
import math
import re
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, OrderedDict, defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import seo

UA_TOKEN = "claude-acc-seo-audit"
UA = f"{UA_TOKEN}/1 (+https://www.outofplace.space/; contact@outofplace.space)"
DAY = 86400
TIMEOUT = 20
MAX_PAGE = 8 * 1024 * 1024
MAX_SITEMAP = 40 * 1024 * 1024
MAX_HOPS = 10
BACKOFF = (5, 20, 60)  # s po kolejnych 429 albo 5xx, gdy serwer nie podał Retry-After
MAX_PAUSE = 120
MAX_FAILS = 15  # tyle błędów sieci albo 5xx z rzędu i host uznajemy za niedostępny w tym biegu
PAGE_TTL = 60 * DAY  # tak stare fakty o stronie jeszcze zastępują stronę, której bieg nie zdążył pobrać
RESERVE_S = 150  # czas na boty AI, llms.txt, IndexNow, Bing i pocztę po crawlu
MAX_TARGETS = 6000
AHREFS = {"mailbox": "contact@outofplace.space", "from": "sa@ahrefs.com", "max_age_days": 45, "max_mails": 6}
DEFAULT_AUDIT = {
    "time_cap_min": 30,
    "concurrency": 3,
    "interval_s": 0.15,
    "title": [30, 60],
    "description": [70, 160],
    "ai_slow_ms": 2000,
    "title_suffix": [],
}
# boty AI: token z robots.txt i User-Agent, którym pobierają strony (None: sam token, bez pobierania).
# Pełne napisy podają OpenAI, Perplexity i Microsoft; Anthropic publikuje tylko tokeny, więc dla
# ClaudeBot i Claude-SearchBot napis ma postać z logów serwerów. Zapory dopasowują token.
AI_BOTS = (
    ("GPTBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.4; +https://openai.com/gptbot"),
    ("OAI-SearchBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"),
    ("ChatGPT-User", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("ClaudeBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Claude-SearchBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-SearchBot/1.0; +searchbot@anthropic.com)"),
    ("PerplexityBot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    # Google-Extended to tylko znacznik w robots.txt (Google: "doesn't have a separate HTTP request user agent")
    ("Google-Extended", None),
    ("Bingbot", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/116.0.1938.76 Safari/537.36"),
)  # fmt: skip
BLOCKED = {401, 403, 406, 429, 451, 503}
ARTICLE_TYPES = {
    "Article", "BlogPosting", "NewsArticle", "TechArticle", "ScholarlyArticle", "Report", "SocialMediaPosting",
    "LiveBlogPosting", "OpinionNewsArticle", "AnalysisNewsArticle", "ReportageNewsArticle", "BackgroundNewsArticle",
}  # fmt: skip

# typ: (rodzaj, waga, opis po polsku). Rodzaj content ma poprawki we wpisie; _template to ten sam
# problem poza treścią wpisu (szablon, strony spoza bloga), czyli kod.
TYPES = {
    "robots_unreachable": ("code", "error", "robots.txt nie odpowiada (5xx albo sieć): roboty traktują to jak zakaz"),
    "sitemap_unreachable": ("code", "error", "mapa strony nie odpowiada albo nie jest poprawnym XML-em"),
    "sitemap_redirect": ("code", "error", "mapa strony podaje adresy z przekierowaniem"),
    "sitemap_broken": ("code", "error", "mapa strony podaje adresy 4xx albo 5xx"),
    "sitemap_lastmod_missing": ("code", "notice", "adresy w mapie bez lastmod"),
    "sitemap_lastmod_future": ("code", "warning", "lastmod w mapie z przyszłości"),
    "sitemap_lastmod_stale": ("code", "warning", "lastmod w mapie starszy niż dateModified wpisu"),
    "post_not_in_sitemap": ("code", "warning", "wpisy, których nie ma w mapie strony"),
    "redirect": ("code", "warning", "wewnętrzne adresy z przekierowaniem 3xx"),
    "redirect_chain": ("code", "warning", "łańcuchy przekierowań (więcej niż jeden skok)"),
    "redirect_loop": ("code", "error", "pętle przekierowań"),
    "broken": ("code", "error", "wewnętrzne adresy 4xx"),
    "server_error": ("code", "error", "wewnętrzne adresy 5xx"),
    "link_to_redirect": ("content", "warning", "linki w treści wpisów do adresów z przekierowaniem"),
    "link_to_redirect_template": ("code", "warning", "linki w szablonie albo poza wpisami do adresów z przekierowaniem"),
    "link_to_redirect_temporary": ("code", "notice", "linki do tymczasowych przekierowań (302, 303, 307): adres końcowy może się zmieniać (np. losowanie), więc to nie poprawka treści"),
    "link_to_broken": ("content", "error", "martwe linki w treści wpisów"),
    "link_to_broken_template": ("code", "error", "martwe linki w szablonie albo poza wpisami"),
    "canonical_missing": ("code", "error", "strony bez link rel=canonical"),
    "canonical_multiple": ("code", "error", "strony z kilkoma różnymi canonical"),
    "canonical_host": ("code", "error", "canonical na innym hoście niż kanoniczny"),
    "canonical_other": ("code", "warning", "adresy z mapy, które wskazują inny canonical"),
    "canonical_broken": ("code", "error", "canonical wskazuje przekierowanie albo błąd"),
    "hreflang_return": ("content", "warning", "wpisy bez zwrotnego hreflang (no return-tag)"),
    "hreflang_return_template": ("code", "warning", "strony bez zwrotnego hreflang (no return-tag)"),
    "hreflang_self": ("code", "warning", "hreflang bez odnośnika do samej strony"),
    "hreflang_x_default": ("code", "warning", "hreflang bez x-default"),
    "hreflang_target": ("code", "error", "hreflang wskazuje przekierowanie albo błąd"),
    "title_length": ("content", "warning", "tytuły wpisów poza limitem długości"),
    "title_length_template": ("code", "warning", "tytuły stron poza limitem długości"),
    "description_length": ("content", "warning", "opisy meta wpisów poza limitem długości"),
    "description_length_template": ("code", "warning", "opisy meta stron poza limitem długości"),
    "title_duplicate": ("content", "warning", "wpisy z takim samym tytułem jak inna strona"),
    "title_duplicate_template": ("code", "warning", "strony z identycznym tytułem"),
    "description_duplicate": ("content", "warning", "wpisy z takim samym opisem meta jak inna strona"),
    "description_duplicate_template": ("code", "warning", "strony z identycznym opisem meta"),
    "h1": ("code", "warning", "strony bez h1 albo z kilkoma h1"),
    "jsonld_invalid": ("code", "error", "JSON-LD, który się nie parsuje"),
    "jsonld_article": ("code", "warning", "wpisy bez JSON-LD typu Article (BlogPosting)"),
    "jsonld_incomplete": ("code", "warning", "JSON-LD bez wymaganych pól (Article, FAQPage, HowTo, BreadcrumbList)"),
    "jsonld_breadcrumb": ("code", "notice", "wpisy bez BreadcrumbList, choć reszta wpisów go ma"),
    "noindex": ("code", "error", "noindex na stronie z mapy albo na wpisie"),
    "orphan": ("code", "warning", "wpisy bez żadnego linku wewnętrznego (sieroty)"),
    "image_alt": ("content", "notice", "obrazy w treści wpisów bez atrybutu alt"),
    "image_alt_template": ("code", "notice", "obrazy w treści stron bez atrybutu alt"),
    "llms_unreachable": ("code", "warning", "llms.txt nie odpowiada"),
    "llms_missing": ("code", "warning", "wpisy, których nie ma w llms.txt"),
    "indexnow_key": ("code", "error", "plik klucza IndexNow nie odpowiada kluczem"),
    "ai_robots_blocked": ("code", "error", "robots.txt blokuje bota AI"),
    "ai_fetch_blocked": ("code", "error", "serwer odmawia botowi AI"),
    "ai_slow": ("code", "warning", "wolna odpowiedź dla bota AI"),
    "bing_robots_blocked": ("code", "warning", "Bing: adresy zablokowane w robots.txt"),
    "bing_important_blocked": ("code", "error", "Bing: ważne adresy zablokowane w robots.txt"),
    "bing_malware": ("code", "error", "Bing: adresy oznaczone jako złośliwe"),
    "bing_fetch_errors": ("code", "warning", "Bing: błędy DNS albo przekroczony czas"),
    "ahrefs": ("code", "warning", "problem z alertu Ahrefs bez odpowiednika w crawlu"),
}
SEVERITY_ORDER = {"error": 0, "warning": 1, "notice": 2}
# nazwy problemów Ahrefs (małymi literami) na typy crawla o tym samym znaczeniu; reszta zostaje
# osobnym problemem "ahrefs" (np. Noindex page: Ahrefs liczy każdą stronę noindex, my tylko
# zabłąkane, czyli w mapie albo na wpisie)
AHREFS_TYPES = {
    "3xx redirect": "redirect",
    "3xx page receives organic traffic": "redirect",
    "redirect chain": "redirect_chain",
    "redirect loop": "redirect_loop",
    "page has links to redirect": "link_to_redirect",
    "page has links to broken page": "link_to_broken",
    "4xx page": "broken",
    "404 page": "broken",
    "5xx page": "server_error",
    "3xx redirect in sitemap": "sitemap_redirect",
    "4xx page in sitemap": "sitemap_broken",
    "5xx page in sitemap": "sitemap_broken",
    "noindex page in sitemap": "noindex",
    "title too long": ("title_length", "za długi"),
    "title too short": ("title_length", "za krótki"),
    "title tag missing or empty": ("title_length", "brak"),
    "meta description too long": ("description_length", "za długi"),
    "meta description too short": ("description_length", "za krótki"),
    "meta description tag missing or empty": ("description_length", "brak"),
    "h1 tag missing or empty": "h1",
    "multiple h1 tags": "h1",
    "missing reciprocal hreflang (no return-tag)": "hreflang_return",
    "hreflang to redirect or broken page": "hreflang_target",
    "self-reference hreflang annotation missing": "hreflang_self",
    "x-default hreflang annotation missing": "hreflang_x_default",
    "orphan page (has no incoming internal links)": "orphan",
    "canonical points to redirect": "canonical_broken",
    "canonical points to 4xx": "canonical_broken",
    "canonical points to 5xx": "canonical_broken",
    "non-canonical page in sitemap": "canonical_other",
    "canonical url has no incoming internal links": None,
    "slow server response for ai crawlers": "ai_slow",
    "missing alt text": "image_alt",
    "structured data has schema.org validation error": "jsonld_incomplete",
}
# flagi CrawlIssues (UrlWithCrawlIssues.CrawlIssues, Bing Webmaster API) na typy
BING_FLAGS = (
    (1, "redirect"), (2, "redirect"), (4, "broken"), (8, "server_error"), (16, "bing_robots_blocked"),
    (32, "bing_malware"), (64, "bing_important_blocked"), (128, "bing_fetch_errors"), (256, "bing_fetch_errors"),
)  # fmt: skip

Hit = namedtuple("Hit", "status body headers ms")


class HostDown(seo.FetchError):
    pass


def say(text):
    seo.say(text)


def source(name, ok, detail, count=0, skipped=False, degraded=False):
    return seo.source(name, ok, seo.no_dashes(detail), count, skipped, degraded)


# ---------- adresy ----------

SAFE_PATH = "/%:@!$&'()*+,;=-._~"
SAFE_QUERY = SAFE_PATH + "?"


def norm_url(url, base=None):
    """Adres do porównań i zapytań: względny złożony z base, schemat i host małymi literami, bez
    fragmentu i domyślnego portu, znaki spoza ASCII zakodowane procentami (wielkie litery).
    None dla adresów, które nie są http(s)."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if base:
        try:
            url = urllib.parse.urljoin(base, url)
        except ValueError:
            return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    if port and port not in (80, 443):
        host = f"{host}:{port}"

    def quote(text, safe):
        text = urllib.parse.quote(text, safe=safe)
        return re.sub(r"%[0-9a-f]{2}", lambda m: m.group(0).upper(), text)

    return urllib.parse.urlunsplit((parts.scheme.lower(), host, quote(parts.path or "/", SAFE_PATH), quote(parts.query, SAFE_QUERY), ""))


def path_of(url):
    parts = urllib.parse.urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def internal(url, domain):
    return seo.registrable(seo.host_of(url)) == domain


# ---------- robots.txt (RFC 9309) ----------


class Robots:
    """robots.txt według RFC 9309: grupa po dokładnym tokenie agenta (bez wielkości liter), grupy
    tego samego agenta łączone, bez niej grupa *; najdłuższa pasująca reguła wygrywa, przy remisie
    Allow. Python urllib.robotparser bierze pierwszą pasującą regułę i dopasowuje podnapisy, więc
    "Allow: /" przed "Disallow: /api/" puściłby /api/."""

    def __init__(self, text):
        self.groups = []
        agents, rules = [], []
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif key in ("allow", "disallow") and agents:
                rules.append((key == "allow", value))
        if agents:
            self.groups.append((agents, rules))

    def rules_for(self, token):
        token = token.lower()
        mine = [rule for agents, rules in self.groups if token in agents for rule in rules]
        if mine or any(token in agents for agents, _ in self.groups):
            return mine
        return [rule for agents, rules in self.groups if "*" in agents for rule in rules]

    def named(self, token):
        """Czy robots.txt ma grupę z tym tokenem (inaczej obowiązuje go grupa *)."""
        return any(token.lower() in agents for agents, _ in self.groups)

    def allowed(self, token, path):
        best = None
        for allow, pattern in self.rules_for(token):
            if not pattern:
                continue  # "Disallow:" bez ścieżki niczego nie zabrania
            if robots_match(pattern, path):
                rank = (len(pattern), allow)
                if best is None or rank > best:
                    best = rank
        return True if best is None else best[1]


def robots_match(pattern, path):
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if c == "*" else re.escape(c) for c in body) + ("$" if anchored else "")
    return re.match(regex, path) is not None


# ---------- HTML ----------

CHROME = ("nav", "header", "footer", "aside")
REGIONS = CHROME + ("main", "article")


class PageParser(HTMLParser):
    """Z jednej strony to, co widzi wyszukiwarka bez JavaScriptu: tytuł, opis, robots, lang,
    canonical, hreflang, h1, linki (w treści albo w szablonie), obrazy, bloki JSON-LD."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.svg = [], 0
        self.title, self._title = None, None
        self.description, self.robots, self.lang = None, [], None
        self.canonicals, self.alternates = [], []
        self.h1, self._h1 = [], None
        self.links, self._a = [], None
        self.images, self.ld, self._ld = [], [], None
        self.has_main = False

    def where(self):
        return any(t in ("main", "article") for t in self.stack), any(t in CHROME for t in self.stack)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "svg":
            self.svg += 1
        elif tag == "html" and self.lang is None:
            self.lang = a.get("lang", "").strip() or None
        elif tag == "title" and not self.svg and self.title is None and self._title is None:
            self._title = []
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description" and self.description is None:
                self.description = a.get("content", "")
            elif name in ("robots", "googlebot", "bingbot"):
                self.robots.append(a.get("content", "").lower())
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.canonicals.append(a["href"])
            if "alternate" in rel and a.get("hreflang") and a.get("href"):
                self.alternates.append((a["hreflang"].strip().lower(), a["href"]))
        elif tag in REGIONS:
            self.stack.append(tag)
            if tag in ("main", "article"):
                self.has_main = True
        elif tag == "h1":
            self._h1 = []
        elif tag == "a" and a.get("href") is not None:
            in_main, in_chrome = self.where()
            self._a = [a["href"], [], in_main, in_chrome]
        elif tag == "img":
            if self._a is not None and a.get("alt"):
                self._a[1].append(a["alt"])
            in_main, in_chrome = self.where()
            self.images.append((a.get("src", ""), "alt" in a, in_main, in_chrome))
        elif tag == "script" and a.get("type", "").lower().startswith("application/ld+json"):
            self._ld = []

    def handle_endtag(self, tag):
        if tag == "svg" and self.svg:
            self.svg -= 1
        elif tag == "title" and self._title is not None:
            self.title = " ".join("".join(self._title).split())
            self._title = None
        elif tag in REGIONS and tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass
        elif tag == "h1" and self._h1 is not None:
            self.h1.append(" ".join("".join(self._h1).split())[:120])
            self._h1 = None
        elif tag == "a" and self._a is not None:
            href, text, in_main, in_chrome = self._a
            self.links.append((href, " ".join("".join(text).split())[:100], in_main, in_chrome))
            self._a = None
        elif tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
        if self._h1 is not None:
            self._h1.append(data)
        if self._a is not None:
            self._a[1].append(data)
        if self._ld is not None:
            self._ld.append(data)


def ld_nodes(data):
    """Każdy węzeł JSON-LD (dict) na każdej głębokości, także w @graph."""
    out, queue = [], [data]
    while queue:
        item = queue.pop()
        if isinstance(item, dict):
            out.append(item)
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return out


def ld_types(node):
    t = node.get("@type")
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)] if isinstance(t, list) else []


def text_of(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return text_of(value.get("text") or value.get("name"))
    return ""


def as_list(value):
    return value if isinstance(value, list) else [value] if value is not None else []


def ld_summary(blocks):
    """{types, err, prob, mod}: typy z każdej głębokości, błędy parsowania, brakujące pola
    wymagane przez Google i dateModified artykułu."""
    types, errors, problems, modified = set(), [], [], None
    for raw in blocks:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            errors.append(str(exc)[:100])
            continue
        for node in ld_nodes(data):
            kinds = ld_types(node)
            types.update(kinds)
            if ARTICLE_TYPES & set(kinds):
                if not text_of(node.get("headline")):
                    problems.append("Article bez headline")
                modified = modified or (node.get("dateModified") if isinstance(node.get("dateModified"), str) else None)
            if "FAQPage" in kinds:
                questions = [q for q in as_list(node.get("mainEntity")) if isinstance(q, dict)]
                if not questions:
                    problems.append("FAQPage bez mainEntity")
                for q in questions:
                    answer = q.get("acceptedAnswer")
                    if not text_of(q.get("name")) or not text_of(answer[0] if isinstance(answer, list) and answer else answer):
                        problems.append("FAQPage: pytanie bez name albo acceptedAnswer.text")
                        break
            if "HowTo" in kinds and not as_list(node.get("step")):
                problems.append("HowTo bez step")
            if "BreadcrumbList" in kinds:
                items = [i for i in as_list(node.get("itemListElement")) if isinstance(i, dict)]
                if not items:
                    problems.append("BreadcrumbList bez itemListElement")
                elif any(not isinstance(i.get("position"), (int, str)) or not (text_of(i.get("name")) or text_of(i.get("item"))) for i in items):
                    problems.append("BreadcrumbList: element bez position albo name")
    return {"types": sorted(types), "err": errors[:5], "prob": sorted(set(problems))[:8], "mod": modified}


def page_facts(body, url, headers, domain):
    """Fakty o stronie HTML do pamięci podręcznej (krótkie klucze: 3500 stron Matury to kilka MB)."""
    parser = PageParser()
    try:
        parser.feed(body.decode("utf-8", "replace") if isinstance(body, bytes) else body)
        parser.close()
    except Exception as exc:  # HTMLParser bywa kapryśny na zepsutym HTML-u
        say(f"HTML {url}: {exc!r}"[:160])
    content, template, seen_c, seen_t = [], [], set(), set()
    for href, anchor, in_main, in_chrome in parser.links:
        if href.strip().startswith("#"):
            continue
        target = norm_url(href, url)
        if not target or not internal(target, domain) or target == url:
            continue
        in_content = not in_chrome and (in_main or not parser.has_main)
        if in_content:
            if target not in seen_c:
                seen_c.add(target)
                content.append([target, anchor])
        elif target not in seen_t:
            seen_t.add(target)
            template.append(target)
    images = []
    for src, has_alt, in_main, in_chrome in parser.images:
        if not has_alt and not in_chrome and (in_main or not parser.has_main) and src and not src.startswith("data:"):
            images.append(norm_url(src, url) or src[:200])
    robots = [r for r in parser.robots if r] + [v.lower() for v in [headers.get("x-robots-tag", "")] if v]
    return {
        "title": parser.title, "desc": parser.description if parser.description is None else " ".join(parser.description.split()),
        "robots": ", ".join(robots), "lang": parser.lang,
        "canon": [c for c in (norm_url(h, url) for h in parser.canonicals) if c],
        "alt": [[code, h] for code, h in ((c, norm_url(h, url)) for c, h in parser.alternates) if h],
        "h1": parser.h1[:5], "h1n": len(parser.h1), "lc": content, "lt": template, "img": images[:20],
        "ld": ld_summary(parser.ld),
    }  # fmt: skip


def noindex(facts):
    tokens = set(re.split(r"[\s,]+", facts.get("robots") or ""))
    return bool(tokens & {"noindex", "none"})


# ---------- mapa strony ----------


def parse_sitemap(body):
    """(wpisy [(loc, lastmod)], mapy podrzędne) z XML-a mapy; bez DTD (encje, XXE)."""
    if body[:2] == b"\x1f\x8b":
        body = unzip(body, MAX_SITEMAP)
    if re.search(rb"<!(doctype|entity)", body, re.IGNORECASE):
        raise ValueError("mapa z DTD")
    root = ET.fromstring(body)
    kind = root.tag.rsplit("}", 1)[-1]
    entries, children = [], []
    for entry in root:
        loc = lastmod = None
        for child in entry:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if kind == "sitemapindex" or entry.tag.rsplit("}", 1)[-1] == "sitemap":
            children.append(loc)
        else:
            entries.append((loc, lastmod))
    return entries, children


def parse_time(text):
    """Data W3C (RRRR-MM-DD albo pełny ISO) jako epoch s albo None."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d"):
        try:
            when = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text[:10], fmt)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    return None


# ---------- sieć ----------


def lower_headers(pairs):
    out = {}
    for key, value in pairs:
        key = key.lower()
        out[key] = f"{out[key]}, {value}" if key in out else str(value)
    return out


def unzip(raw, limit):
    out = zlib.decompressobj(wbits=47)  # gzip albo zlib, nagłówek wykrywany sam
    try:
        return out.decompress(raw, limit)
    except zlib.error as exc:
        raise seo.FetchError(f"zła kompresja: {exc}")


_LOCAL = threading.local()
_TLS = ssl.create_default_context()
# serwer zamknął bezczynne połączenie keep-alive: zapytanie idzie jeszcze raz nowym połączeniem
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest, http.client.BadStatusLine, ConnectionResetError,
          BrokenPipeError)  # fmt: skip


def connection(scheme, host, port, timeout):
    """Połączenie keep-alive tego wątku do hosta: 3500 stron Matury to jedno uzgodnienie TLS na
    wątek zamiast 3500 (szybciej i lżej dla serwera)."""
    pool = getattr(_LOCAL, "pool", None)
    if pool is None:
        pool = _LOCAL.pool = {}
    key = (scheme, host, port)
    conn = pool.get(key)
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_TLS)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.used = False
        pool[key] = conn
    return conn


def drop(scheme, host, port):
    conn = (getattr(_LOCAL, "pool", None) or {}).pop((scheme, host, port), None)
    if conn is not None:
        conn.close()


def http_transport(method, url, headers, timeout, limit):
    """Jedno zapytanie bez podążania za przekierowaniem: Hit(status, treść, nagłówki małymi
    literami, ms). 3xx, 4xx i 5xx to Hit; sieć, TLS i czas to FetchError (bez adresu w opisie)."""
    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme, parts.hostname
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise seo.FetchError(str(exc)[:80])
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    for attempt in (0, 1):
        conn = connection(scheme, host, port, timeout)
        reused = conn.used
        start = time.monotonic()
        try:
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(limit + 1)  # także HEAD: dopiero przeczytana odpowiedź zwalnia połączenie
            if len(raw) > limit:
                drop(scheme, host, port)  # reszty nie czytamy, więc połączenie nie nadaje się do ponownego użycia
            elif resp.will_close:
                drop(scheme, host, port)
            else:
                conn.used = True
            status, hdrs = resp.status, lower_headers(resp.getheaders())
        except _STALE as exc:
            drop(scheme, host, port)
            if reused and attempt == 0:
                continue
            raise seo.FetchError(str(exc)[:120] or exc.__class__.__name__)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            drop(scheme, host, port)
            raise seo.FetchError(str(exc)[:120] or exc.__class__.__name__)
        ms = int((time.monotonic() - start) * 1000)
        if "gzip" in hdrs.get("content-encoding", "") and raw:
            raw = unzip(raw, limit)
        return Hit(status, raw[:limit], hdrs, ms)
    raise seo.FetchError("połączenie zamknięte")


def retry_after(hit, now):
    value = (hit.headers or {}).get("retry-after", "").strip()
    if value.isdigit():
        return int(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, IndexError):
        return None


class Ctx(seo.Ctx):
    """seo.Ctx (zegar, sen, Pęk kluczy, pamięć podręczna) plus transport HTTP bez podążania za
    przekierowaniami i bramka pocztowa. Testy podstawiają atrapy."""

    def __init__(self, transport=None, gateway=None, **kw):
        super().__init__(**kw)
        self.transport = transport or http_transport
        self.gateway = gateway


class Fetcher:
    """Uprzejme zapytania do jednego bloga: odstęp interval między startami zapytań (wspólny dla
    wszystkich wątków), przerwa dla wszystkich po 429 i 5xx, po MAX_FAILS błędach z rzędu host jest
    niedostępny do końca biegu (HostDown)."""

    def __init__(self, ctx, interval):
        self.ctx, self.interval = ctx, interval
        self.lock = threading.Lock()
        self.next_at = self.pause_until = 0.0
        self.requests = self.fails = self.pauses = 0
        self.down = None

    def _slot(self):
        with self.lock:
            now = self.ctx.now()
            start = max(now, self.next_at, self.pause_until)
            self.next_at = start + self.interval
            self.requests += 1
        if start > now:
            self.ctx.sleep(start - now)

    def _pause(self, seconds, why):
        with self.lock:
            self.pauses += 1
            self.pause_until = max(self.pause_until, self.ctx.now() + min(max(seconds, 1), MAX_PAUSE))
        self.ctx.log(f"crawl: {why}, przerwa {min(max(seconds, 1), MAX_PAUSE):.0f} s")

    def _failed(self, why):
        with self.lock:
            self.fails += 1
            if self.fails >= MAX_FAILS and not self.down:
                self.down = f"{MAX_FAILS} błędów z rzędu, ostatni: {why}"
            return bool(self.down)

    def request(self, url, method="GET", ua=UA, headers=None, limit=MAX_PAGE):
        if self.down:
            raise HostDown(self.down)
        hdrs = {"User-Agent": ua, "Accept-Encoding": "gzip",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5"}  # fmt: skip
        hdrs.update(headers or {})
        for attempt in range(len(BACKOFF) + 1):
            self._slot()
            try:
                hit = self.ctx.transport(method, url, hdrs, TIMEOUT, limit)
            except seo.FetchError as exc:
                if self._failed(str(exc)) or attempt >= 1:
                    raise
                self._pause(BACKOFF[0], f"sieć ({exc})")
                continue
            if hit.status == 429 or hit.status >= 500:
                if attempt < len(BACKOFF):
                    wait = retry_after(hit, self.ctx.now())
                    self._pause(wait if wait is not None else BACKOFF[attempt], f"HTTP {hit.status}")
                    continue
                self._failed(f"HTTP {hit.status}")
                return hit
            with self.lock:
                self.fails = 0
            return hit
        return hit


# ---------- crawl ----------


class Crawl:
    """Jeden obchód bloga: robots.txt, mapy, strony z mapy i wpisy, cele linków, łańcuchy
    przekierowań. statuses: {adres: {s, loc, err}} dla każdego sprawdzonego adresu; pages: {adres:
    fakty} dla stron HTML 200 (świeże albo z pamięci podręcznej)."""

    def __init__(self, ctx, blog, cfg):
        self.ctx, self.blog, self.cfg = ctx, blog, cfg
        self.domain = blog["domain"]
        self.host = cfg["canonical_host"]
        self.origin = f"https://{self.host}"
        self.fetcher = Fetcher(ctx, cfg["interval_s"])
        self.cache = ctx.cache(f"audit-{self.host}")
        self.lock = threading.Lock()
        self.statuses, self.pages = {}, {}
        self.fresh, self.from_cache, self.not_modified, self.robots_skipped = set(), set(), set(), set()
        self.sitemap = OrderedDict()
        self.sitemap_ok = False
        self.robots, self.robots_status = None, None
        self.problems, self.issues = [], []
        self.started = ctx.now()
        self.cap = cfg["time_cap_min"] * 60
        self.list = []
        self.done_pages = self.done_targets = True
        self.targets = 0

    def deadline(self, share):
        return self.started + max(30, self.cap - RESERVE_S) * share

    def add_issue(self, type, url=None, detail=None, subject=None):
        self.issues.append((type, url, detail, subject))

    # --- robots i mapy

    def follow(self, url, ua=UA, method="GET", limit=MAX_PAGE, hops=3):
        """GET z podążaniem za przekierowaniem (robots.txt, mapy, llms.txt): (Hit, adres końcowy, skoki)."""
        jumps = []
        for _ in range(hops + 1):
            hit = self.fetcher.request(url, method=method, ua=ua, limit=limit)
            location = hit.headers.get("location")
            if 300 <= hit.status < 400 and location:
                nxt = norm_url(location, url)
                if not nxt:
                    return hit, url, jumps
                jumps.append((url, hit.status))
                url = nxt
                continue
            return hit, url, jumps
        return hit, url, jumps

    def load_robots(self):
        url = self.origin + "/robots.txt"
        try:
            hit, _, _ = self.follow(url, limit=512 * 1024)
        except seo.FetchError as exc:
            self.robots_status = f"sieć: {exc}"
            self.add_issue("robots_unreachable", url, f"robots.txt: {exc}")
            return False
        self.robots_status = hit.status
        if hit.status >= 500:
            self.add_issue("robots_unreachable", url, f"robots.txt: HTTP {hit.status}")
            return False
        self.robots = Robots(hit.body.decode("utf-8", "replace") if hit.status == 200 else "")
        return True

    def allowed(self, url):
        return self.robots is None or self.robots.allowed(UA_TOKEN, path_of(url))

    def load_sitemaps(self):
        queue, seen = [norm_url(self.cfg["sitemap"])], set()
        root = queue[0]
        while queue and len(seen) < 300:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if not self.allowed(url):
                self.problems.append(f"robots.txt zabrania mapy {path_of(url)}")
                continue
            try:
                hit, final, jumps = self.follow(url, limit=MAX_SITEMAP)
            except seo.FetchError as exc:
                self.sitemap_problem(url, root, f"sieć: {exc}")
                continue
            if hit.status != 200:
                self.sitemap_problem(url, root, f"HTTP {hit.status}")
                continue
            try:
                entries, children = parse_sitemap(hit.body)
            except (ValueError, ET.ParseError, seo.FetchError) as exc:
                self.sitemap_problem(url, root, f"nie XML: {str(exc)[:80]}")
                continue
            self.sitemap_ok = True
            for loc, lastmod in entries:
                key = norm_url(loc)
                if key and internal(key, self.domain) and len(self.sitemap) < 50000:
                    self.sitemap.setdefault(key, lastmod)
            queue.extend(c for c in (norm_url(c) for c in children) if c)

    def sitemap_problem(self, url, root, why):
        self.problems.append(f"mapa {path_of(url)}: {why}")
        if url == root:
            self.add_issue("sitemap_unreachable", url, f"{url}: {why}")

    # --- strony

    def fetch_page(self, url):
        if self.ctx.now() >= self.deadline(0.7):
            return "time"
        if not self.allowed(url):
            with self.lock:
                self.robots_skipped.add(url)
            return "robots"
        cached = self.cache.get(url, PAGE_TTL)
        headers = {}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached and cached.get("lm"):
            headers["If-Modified-Since"] = cached["lm"]
        try:
            hit = self.fetcher.request(url, headers=headers)
        except seo.FetchError as exc:
            with self.lock:
                self.statuses[url] = {"s": None, "loc": None, "err": str(exc)[:80]}
            return "error"
        location = norm_url(hit.headers.get("location", ""), url) if 300 <= hit.status < 400 else None
        with self.lock:
            self.statuses[url] = {"s": 200 if hit.status == 304 else hit.status, "loc": location, "err": None, "ms": hit.ms}
        if hit.status == 304 and cached and cached.get("f"):
            with self.lock:
                self.pages[url] = cached["f"]
                self.fresh.add(url)
                self.not_modified.add(url)
                self.cache.put(url, cached)
            return "304"
        ctype = hit.headers.get("content-type", "")
        if hit.status == 200 and ("html" in ctype or not ctype):
            facts = page_facts(hit.body, url, hit.headers, self.domain)
            entry = {"f": facts, "etag": hit.headers.get("etag"), "lm": hit.headers.get("last-modified")}
            with self.lock:
                self.pages[url] = facts
                self.fresh.add(url)
                self.cache.put(url, entry)
        return "ok"

    def crawl_pages(self, posts):
        urls = list(OrderedDict.fromkeys([p["url"] for p in posts] + list(self.sitemap)))
        post_set = {p["url"] for p in posts}

        def age(url):
            entry = self.cache.data.get(f"{url}")
            return entry.get("at", 0) if isinstance(entry, dict) else 0

        # najpierw strony bez faktów w pamięci (nowe albo starsze niż PAGE_TTL), potem wpisy, potem
        # reszta od najdawniej widzianej: gdy same wpisy nie mieszczą się w limicie czasu (wolny
        # serwer), bieg ucięty limitem i tak obejdzie całą stronę w kilku biegach
        def order(url):
            return (self.cache.get(url, PAGE_TTL) is not None, url not in post_set, age(url))

        self.list = sorted(urls, key=order)
        total = len(self.list)
        counter = {"n": 0}

        def work(url):
            result = self.fetch_page(url)
            with self.lock:
                counter["n"] += 1
                n = counter["n"]
            if n % 500 == 0:
                self.ctx.log(f"crawl: {n}/{total} stron")
            return result

        with ThreadPoolExecutor(max_workers=self.cfg["concurrency"]) as pool:
            results = list(pool.map(work, self.list))
        if "time" in results:
            self.done_pages = False
        # strony, których bieg nie zdążył pobrać (albo host padł): fakty z poprzednich biegów
        for url in self.list:
            if url in self.pages or url in self.statuses:
                continue
            cached = self.cache.get(url, PAGE_TTL)
            if cached and cached.get("f"):
                self.pages[url] = cached["f"]
                self.from_cache.add(url)
                self.statuses.setdefault(url, {"s": 200, "loc": None, "err": None, "cached": True})

    # --- cele linków i łańcuchy przekierowań

    def check(self, url):
        """HEAD bez podążania (GET, gdy serwer nie zna HEAD); status do statuses."""
        if self.ctx.now() >= self.deadline(1.0):
            return False
        if not self.allowed(url):
            with self.lock:
                self.robots_skipped.add(url)
            return True
        try:
            hit = self.fetcher.request(url, method="HEAD")
            if hit.status in (405, 501):
                hit = self.fetcher.request(url, limit=64 * 1024)
        except seo.FetchError as exc:
            with self.lock:
                self.statuses[url] = {"s": None, "loc": None, "err": str(exc)[:80]}
            return True
        location = norm_url(hit.headers.get("location", ""), url) if 300 <= hit.status < 400 else None
        with self.lock:
            self.statuses[url] = {"s": hit.status, "loc": location, "err": None, "ms": hit.ms}
        return True

    def check_targets(self):
        wanted = Counter()
        for facts in self.pages.values():
            for target, _ in facts.get("lc", []):
                wanted[target] += 1
            for target in facts.get("lt", []):
                wanted[target] += 1
            for target in facts.get("canon", [])[:1]:
                wanted[target] += 1
            for _, target in facts.get("alt", []):
                wanted[target] += 1
        todo = [u for u, _ in wanted.most_common() if u not in self.statuses and internal(u, self.domain)][:MAX_TARGETS]
        self.targets = len(todo)
        self.run_checks(todo)
        # łańcuchy: każdy skok, którego jeszcze nie znamy, aż do końca albo MAX_HOPS
        for _ in range(MAX_HOPS):
            todo = sorted({s["loc"] for s in self.statuses.values() if s.get("loc") and s["loc"] not in self.statuses
                           and internal(s["loc"], self.domain)})  # fmt: skip
            if not todo:
                break
            self.run_checks(todo)

    def run_checks(self, urls):
        if not urls:
            return
        with ThreadPoolExecutor(max_workers=self.cfg["concurrency"]) as pool:
            results = list(pool.map(self.check, urls))
        if not all(results):
            self.done_targets = False

    def resolve(self, url):
        """(adres końcowy, jego status, skoki [(adres, status)], pętla) z tego, co crawl widział;
        status None, gdy nie wiadomo (nie sprawdzony, sieć)."""
        hops, seen = [], set()
        while True:
            state = self.statuses.get(url)
            if state is None or state.get("s") is None:
                return url, None, hops, False
            status = state["s"]
            if 300 <= status < 400 and state.get("loc"):
                if url in seen or len(hops) >= MAX_HOPS:
                    return url, status, hops, True
                seen.add(url)
                hops.append((url, status))
                url = state["loc"]
                if not internal(url, self.domain) and url not in self.statuses:
                    return url, 200, hops, False  # przekierowanie poza bloga: dalej nie sprawdzamy
                continue
            return url, status, hops, False

    def run(self, posts_for):
        """Cały crawl; posts_for(adresy z mapy) daje wpisy bloga (konfiguracja K1)."""
        if not self.load_robots():
            return posts_for([])
        self.load_sitemaps()
        posts = posts_for(list(self.sitemap))
        try:
            self.crawl_pages(posts)
            self.check_targets()
        except HostDown as exc:
            self.problems.append(f"host przestał odpowiadać: {exc}")
        self.ctx.save_caches()
        return posts

    # --- pomiar

    def coverage(self):
        return len(self.pages) / len(self.list) if self.list else 0.0


# ---------- analiza ----------


class Acc:
    """Problemy zbierane z crawla, Bing i Ahrefs: klucz to typ albo typ|przedmiot (np. bot)."""

    def __init__(self):
        self.items = OrderedDict()

    def add(self, type, url=None, detail=None, fix=None, subject=None, found_by="crawl"):
        key = f"{type}|{subject}" if subject else type
        item = self.items.get(key)
        if item is None:
            item = self.items[key] = {"type": type, "subject": subject, "urls": OrderedDict(), "details": {},
                                      "fixes": OrderedDict(), "found_by": OrderedDict(), "ext": {}}  # fmt: skip
        item["found_by"][found_by] = True
        if url:
            item["urls"][url] = True
        lines = item["details"].setdefault(found_by, [])
        if detail and len(lines) < 30 and seo.no_dashes(detail)[:300] not in lines:
            lines.append(seo.no_dashes(detail)[:300])
        if fix:
            key = "|".join(str(fix.get(k, "")) for k in ("page", "action", "from", "href", "field", "src"))
            item["fixes"].setdefault(key, fix)
        return item


def base_type(type):
    """Typ bez wariantu: link_to_redirect_template i link_to_redirect_temporary to link_to_redirect."""
    for tail in ("_template", "_temporary"):
        if type.endswith(tail):
            return type[: -len(tail)]
    return type


def chain_text(hops, final, status):
    return " -> ".join(f"{seo.url_path(u)} ({s})" for u, s in hops) + f" -> {final} ({status})"


def within(n, limits):
    return limits[0] <= n <= limits[1]


def analyse(cr, blog, cfg, posts, acc, now):
    """Wszystkie sprawdzenia na tym, co crawl zebrał; poprawki treści tylko dla wpisów."""
    post_set = {p["url"] for p in posts}
    pages = cr.pages
    for type, url, detail, subject in cr.issues:
        acc.add(type, url, detail, subject=subject)

    # mapa strony: adresy, lastmod
    for url, lastmod in cr.sitemap.items():
        final, status, hops, loop = cr.resolve(url)
        if status is None:
            continue
        if loop:
            acc.add("redirect_loop", url, f"{url}: pętla")
        elif hops:
            acc.add("sitemap_redirect", url, f"{url} {chain_text(hops, final, status)}")
        elif status >= 400:
            acc.add("sitemap_broken", url, f"{url}: HTTP {status}")
        when = parse_time(lastmod)
        if lastmod is None:
            acc.add("sitemap_lastmod_missing", url)
        elif when is None or when > now + DAY:
            acc.add("sitemap_lastmod_future", url, f"{url}: lastmod {lastmod}")
        elif url in post_set and url in pages:
            modified = parse_time((pages[url].get("ld") or {}).get("mod"))
            if modified and modified > when + DAY:
                acc.add("sitemap_lastmod_stale", url, f"{url}: lastmod {lastmod[:10]}, dateModified {pages[url]['ld']['mod'][:10]}")
    if cr.sitemap_ok:
        for url in post_set:
            if url not in cr.sitemap:
                acc.add("post_not_in_sitemap", url)

    # przekierowania i błędy wszystkich sprawdzonych adresów wewnętrznych
    for url, state in list(cr.statuses.items()):
        status = state.get("s")
        if status is None or state.get("cached"):
            continue
        if 300 <= status < 400:
            final, fstatus, hops, loop = cr.resolve(url)
            if loop:
                acc.add("redirect_loop", url, f"{url}: pętla przekierowań")
                continue
            acc.add("redirect", url, f"{url} {chain_text(hops, final, fstatus)}")
            if len(hops) > 1:
                acc.add("redirect_chain", url, f"{url}: {len(hops)} skoki")
        elif 400 <= status < 500:
            acc.add("broken", url, f"{url}: HTTP {status}")
        elif status >= 500:
            acc.add("server_error", url, f"{url}: HTTP {status}")

    # linki: w treści wpisu to poprawka treści, reszta to kod. Ta sama para (cel, tekst) na wielu
    # wpisach to element szablonu (np. CTA), nawet gdy stoi poza nav, header i footer
    posts_with_facts = [u for u in post_set if u in pages]
    pairs = Counter((t, a) for u in posts_with_facts for t, a in pages[u].get("lc", []))
    boiler = max(5, math.ceil(0.3 * len(posts_with_facts)))
    inlinks = defaultdict(set)
    for url, facts in pages.items():
        is_post = url in post_set
        links = [(t, a, True) for t, a in facts.get("lc", [])] + [(t, None, False) for t in facts.get("lt", [])]
        for target, anchor, in_content in links:
            inlinks[target].add(url)
            final, status, hops, loop = cr.resolve(target)
            if status is None:
                continue
            body = is_post and in_content and pairs[(target, anchor)] < boiler
            permanent = all(code in (301, 308) for _, code in hops)
            if hops and not loop and status < 400 and not permanent:
                codes = ", ".join(str(code) for _, code in hops)
                acc.add("link_to_redirect_temporary", url, f"{url}: link do {target} ({codes}) -> {final}")
            elif hops and not loop and status < 400:
                if body:
                    acc.add("link_to_redirect", url, fix={"page": url, "action": "replace_link", "from": target, "to": final,
                                                         "anchor": anchor or "", "chain": [s for _, s in hops]})  # fmt: skip
                else:
                    acc.add("link_to_redirect_template", url, f"{url}: link do {target} -> {final}")
            elif status >= 400 or loop:
                code = status if not loop else "pętla"
                if body:
                    acc.add("link_to_broken", url, fix={"page": url, "action": "unlink_keep_anchor", "href": target,
                                                       "anchor": anchor or "", "status": status})  # fmt: skip
                else:
                    acc.add("link_to_broken_template", url, f"{url}: link do {target} ({code})")
            # cel z przekierowaniem prowadzi też do strony końcowej
            if hops:
                inlinks[final].add(url)

    indexable = {u for u, f in pages.items() if not noindex(f)}

    # noindex na stronach z mapy i na wpisach
    for url, facts in pages.items():
        if (url in cr.sitemap or url in post_set) and noindex(facts):
            acc.add("noindex", url, f"{url}: {facts.get('robots')}")

    # canonical
    host = cfg["canonical_host"]
    for url in indexable:
        canons = pages[url].get("canon") or []
        if not canons:
            acc.add("canonical_missing", url)
            continue
        if len(set(canons)) > 1:
            acc.add("canonical_multiple", url, f"{url}: {', '.join(sorted(set(canons))[:3])}")
        canon = canons[0]
        if seo.host_of(canon) != host:
            acc.add("canonical_host", url, f"{url}: canonical {canon}")
        if canon != url:
            if url in cr.sitemap:
                acc.add("canonical_other", url, f"{url} -> {canon}")
            final, status, hops, loop = cr.resolve(canon)
            if status is not None and (hops or status >= 400):
                acc.add("canonical_broken", url, f"{url}: canonical {canon} ({hops[0][1] if hops else status})")

    # hreflang: odnośnik do siebie, x-default, cele, zwrotne odnośniki (no return-tag)
    for url in indexable:
        alts = pages[url].get("alt") or []
        if not alts:
            continue
        hrefs = {h for _, h in alts}
        codes = {c for c, _ in alts}
        if url not in hrefs:
            acc.add("hreflang_self", url)
        if "x-default" not in codes:
            acc.add("hreflang_x_default", url)
        own = next((c for c, h in alts if h == url and c != "x-default"), None) or (pages[url].get("lang") or "").lower() or None
        for code, href in alts:
            if code == "x-default" or href == url:
                continue
            final, status, hops, loop = cr.resolve(href)
            if status is None:
                continue
            if hops or status >= 400:
                acc.add("hreflang_target", url, f"{url}: {code} -> {href} ({hops[0][1] if hops else status})")
                continue
            other = pages.get(href)
            if other is None or not own or url in {h for _, h in other.get("alt") or []}:
                continue
            if href in post_set:
                acc.add("hreflang_return", href, fix={"page": href, "action": "add_hreflang", "hreflang": own, "href": url})
            else:
                acc.add("hreflang_return_template", href, f"{href}: brak {own} -> {url}")

    # tytuły i opisy: długość i duplikaty na stronach do indeksu, które same są kanoniczne
    canonical_pages = [u for u in indexable if not pages[u].get("canon") or pages[u]["canon"][0] == u]
    for field, key in (("title", "title"), ("description", "desc")):
        limits = cfg[field]
        groups = defaultdict(list)
        for url in canonical_pages:
            text = pages[url].get(key) or ""
            suffix = next((x for x in cfg["title_suffix"] if field == "title" and text.endswith(x)), "")
            if not within(len(text), limits):
                way = "brak" if not text else "za długi" if len(text) > limits[1] else "za krótki"
                fix = {"page": url, "action": "rewrite_meta", "field": field, "current": text, "length": len(text),
                       "min": limits[0], "max": limits[1], "suffix": suffix}  # fmt: skip
                if url in post_set:
                    acc.add(f"{field}_length", url, fix=fix, subject=way)
                else:
                    acc.add(f"{field}_length_template", url, f"{len(text)} znaków: {url}: {text[:90]}", subject=way)
            if text:
                groups[" ".join(text.lower().split())].append(url)
        for same in groups.values():
            if len(same) < 2:
                continue
            same.sort()
            for url in same:
                others = [u for u in same if u != url][:5]
                text = pages[url].get(key) or ""
                if url in post_set:
                    suffix = next((x for x in cfg["title_suffix"] if field == "title" and text.endswith(x)), "")
                    acc.add(f"{field}_duplicate", url, fix={"page": url, "action": "rewrite_meta", "field": field, "current": text,
                                                             "length": len(text), "min": limits[0], "max": limits[1],
                                                             "suffix": suffix, "duplicate_of": others})  # fmt: skip
                else:
                    alts = {h for _, h in pages[url].get("alt") or []}
                    note = " (wersje językowe tej samej strony, hreflang)" if alts and set(others) <= alts else ""
                    acc.add(f"{field}_duplicate_template", url, f"\"{text[:80]}\": {', '.join([url] + others)}{note}")

    # h1, JSON-LD, obrazy
    with_breadcrumb = [u for u in posts_with_facts if "BreadcrumbList" in (pages[u].get("ld") or {}).get("types", [])]
    breadcrumbs_used = posts_with_facts and len(with_breadcrumb) >= 0.8 * len(posts_with_facts)
    for url in indexable:
        facts = pages[url]
        is_post = url in post_set
        n = facts.get("h1n", 0)
        if n != 1:
            acc.add("h1", url, f"{url}: " + ("brak h1" if n == 0 else f"{n} × h1: " + " | ".join(facts.get("h1", [])[:3])))
        ld = facts.get("ld") or {}
        for err in ld.get("err", []):
            acc.add("jsonld_invalid", url, f"{url}: {err}")
        for problem in ld.get("prob", []):
            acc.add("jsonld_incomplete", url, f"{url}: {problem}")
        if is_post and not ARTICLE_TYPES & set(ld.get("types", [])):
            acc.add("jsonld_article", url, f"{url}: typy {', '.join(ld.get('types', [])[:6]) or 'brak'}")
        if is_post and breadcrumbs_used and url not in with_breadcrumb:
            acc.add("jsonld_breadcrumb", url)
        for src in facts.get("img", []):
            if is_post:
                acc.add("image_alt", url, fix={"page": url, "action": "add_alt", "src": src})
            else:
                acc.add("image_alt_template", url, f"{url}: {src}")

    # sieroty: wpisy bez linku z innej strony; tylko przy pełnym obrazie linków
    if cr.coverage() >= 0.95:
        for url in posts_with_facts:
            if url in indexable and not (inlinks.get(url, set()) - {url}):
                acc.add("orphan", url)
        return True
    return False


# ---------- llms.txt, IndexNow, boty AI ----------


LINK_RE = re.compile(r"\]\(\s*([^)\s]+)\s*\)|(https?://[^\s<>()\[\]\"']+)")


def check_llms(cr, posts, acc):
    """Indeks llms.txt: plik z konfiguracji plus llms.txt z tego samego hosta, do których linkuje
    (Renggli ma /pl/llms.txt i /pl/blog/llms.txt). Wpis jest w indeksie, gdy jest tam jego adres,
    ścieżka albo bliźniak .md, jego albo dowolnej wersji językowej (hreflang)."""
    start = norm_url(cr.cfg["llms"])
    if not start:
        return "pominięte: brak adresu llms.txt"
    queue, seen, found = [start], set(), set()
    while queue and len(seen) < 25:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            hit, final, _ = cr.follow(url, limit=6 * 1024 * 1024)
        except seo.FetchError as exc:
            if url == start:
                acc.add("llms_unreachable", url, f"{url}: {exc}")
                return f"{url}: {exc}"
            continue
        if hit.status != 200:
            if url == start:
                acc.add("llms_unreachable", url, f"{url}: HTTP {hit.status}")
                return f"HTTP {hit.status}"
            continue
        text = hit.body.decode("utf-8", "replace")
        for m in LINK_RE.finditer(text):
            link = norm_url(m.group(1) or m.group(2), final)
            if not link:
                continue
            found.add(re.sub(r"\.md$", "", link))
            if internal(link, cr.domain) and link.endswith("llms.txt") and link not in seen:
                queue.append(link)
    groups = {}
    for p in posts:
        facts = cr.pages.get(p["url"]) or {}
        groups[p["url"]] = {p["url"]} | {h for c, h in facts.get("alt", []) if c != "x-default"}
    missing = [u for u, group in groups.items() if not group & found]
    for url in missing:
        acc.add("llms_missing", url)
    return f"{len(seen)} plików llms.txt, brakuje {len(missing)} z {len(posts)} wpisów"


def check_indexnow(cr, acc):
    url = norm_url(cr.cfg.get("indexnow_key_url") or "")
    if not url:
        return "pominięte: brak indexnow_key_url w konfiguracji"
    key = re.sub(r"\.txt$", "", seo.url_path(url).rsplit("/", 1)[-1])
    try:
        hit = cr.fetcher.request(url, limit=4096)
    except seo.FetchError as exc:
        acc.add("indexnow_key", url, f"{url}: {exc}")
        return f"sieć: {exc}"
    body = hit.body.decode("utf-8", "replace").strip()
    if hit.status != 200:
        acc.add("indexnow_key", url, f"{url}: HTTP {hit.status}" + (f" -> {hit.headers.get('location')}" if hit.headers.get("location") else ""))
        return f"HTTP {hit.status}"
    if body != key:
        acc.add("indexnow_key", url, f"{url}: treść {body[:40]!r} zamiast klucza z nazwy pliku")
        return "zła treść"
    return "klucz odpowiada"


def bot_fetch(cr, url, ua):
    """GET z User-Agentem bota i podążaniem za przekierowaniami: (status, ms łącznie) albo (None, opis)."""
    total = 0
    for _ in range(6):
        try:
            hit = cr.fetcher.request(url, ua=ua, limit=MAX_PAGE)
        except seo.FetchError as exc:
            return None, str(exc)[:80]
        total += hit.ms
        location = hit.headers.get("location")
        if 300 <= hit.status < 400 and location and norm_url(location, url):
            url = norm_url(location, url)
            continue
        return hit.status, total
    return hit.status, total


def check_ai_bots(cr, posts, cfg, acc):
    """Dla każdego bota: reguły robots.txt dla jego tokenu na stronie głównej i na wpisie, a dla
    botów, które pobierają strony, status i czas odpowiedzi z ich User-Agentem (porównane z naszym)."""
    table = []
    if cr.robots is None:
        return table
    home = cr.origin + "/"
    sample = posts[0]["url"] if posts else None
    targets = [u for u in (home, sample) if u]
    ours = {}
    for url in targets:
        final, status, _, _ = cr.resolve(url)
        if status is None:
            status, _ = bot_fetch(cr, url, UA)
        ours[url] = status
    robots_url = cr.origin + "/robots.txt"
    for token, ua in AI_BOTS:
        row = {"bot": token, "robots_group": "własna" if cr.robots.named(token) else "*", "robots": "allow", "fetch": None}
        blocked_paths = [path_of(u) for u in targets if not cr.robots.allowed(token, path_of(u))]
        if blocked_paths:
            row["robots"] = "disallow " + ", ".join(blocked_paths)
            acc.add("ai_robots_blocked", robots_url, f"{token}: robots.txt zabrania {', '.join(blocked_paths)}", subject=token)
        if ua:
            fetched = []
            for url in targets:
                status, ms = bot_fetch(cr, url, ua)
                fetched.append({"url": url, "status": status, "ms": ms if isinstance(ms, int) else None})
                if status is None:
                    continue
                if (status in BLOCKED or status >= 500) and ours.get(url) == 200:
                    acc.add("ai_fetch_blocked", url, f"{token}: HTTP {status} na {url}, nasz User-Agent dostaje 200", subject=token)
                elif isinstance(ms, int) and ms > cfg["ai_slow_ms"]:
                    acc.add("ai_slow", url, f"{token}: {ms} ms na {url}", subject=token)
            row["fetch"] = fetched
        table.append(row)
    return table


# ---------- Bing ----------


def bing_source(ctx, cfg):
    """GetCrawlIssues dla bing_site: [(typ, adres, opis)] i status. Adres z kluczem nigdy nie trafia
    do logu ani do opisu błędu."""
    key = ctx.secret(seo.BING_ACCOUNT)
    if not key:
        return [], source("bing", False, "brak klucza w Pęku kluczy (claude-acc seo bing-key)", skipped=True)
    site = cfg.get("bing_site")
    if not site:
        return [], source("bing", False, "brak audit.bing_site w konfiguracji", skipped=True)
    url = f"{seo.BING_URL}/GetCrawlIssues?" + urllib.parse.urlencode({"siteUrl": site, "apikey": key})
    hit, problem = None, None
    for attempt in range(2):
        if attempt:
            ctx.sleep(5)
        try:
            hit = ctx.transport("GET", url, {"User-Agent": UA, "Accept": "application/json"}, 30, 30 * 1024 * 1024)
            problem = None if hit.status == 200 else f"HTTP {hit.status}"
        except seo.FetchError as exc:
            hit, problem = None, f"sieć: {exc}"
        if not problem:
            break
    if problem:
        return [], source("bing", False, f"GetCrawlIssues: {problem}", degraded=True)
    try:
        data = json.loads(hit.body.decode("utf-8", "replace"))
        rows = data.get("d") if isinstance(data, dict) else data
        rows = [r for r in rows or [] if isinstance(r, dict)]
    except (ValueError, AttributeError, TypeError):
        return [], source("bing", False, "GetCrawlIssues: odpowiedź nie jest JSON-em", degraded=True)
    found = []
    for row in rows:
        page = norm_url(str(row.get("Url") or ""))
        flags = row.get("Issues")
        if not page or not isinstance(flags, int):
            continue
        for bit, type in BING_FLAGS:
            if flags & bit:
                found.append((type, page, f"{page}: Bing HTTP {row.get('HttpCode')}, {row.get('InLinks', 0)} linków"))
    return found, source("bing", True, f"GetCrawlIssues: {seo.plural(len(rows), 'adres', 'adresy', 'adresów')} z problemami", len(rows))


# ---------- Ahrefs (poczta) ----------

SUBJECT_RE = re.compile(r"^\((?P<project>[^()]{1,80})\)\s+(?P<rest>.+)$")
SUBJECT_ISSUE_RE = re.compile(r"^(?P<name>.+?)(?:\s+\[New\])?:\s+(?P<count>\d[\d,]*)\s+URLs?$", re.IGNORECASE)
ISSUE_RE = re.compile(r"^(?P<name>.*?\S)\s*(?P<new>New)?(?P<count>\d[\d,]*)(?P<delta>[+\-−]\d[\d,]*)?$")
SECTION_START = ("what's new", "what’s new", "top issues")


def number_of(text):
    return int(re.sub(r"[^\d]", "", text)) if text and re.search(r"\d", text) else 0


def parse_ahrefs(subject, body, links, domain):
    """Jeden mail Site Audit jako dane: {project, kind (crawl|crawl_error|alert), issues [{name,
    count, delta, new, view}], samples [adresy bloga z treści]} albo None, gdy to nie Site Audit."""
    m = SUBJECT_RE.match(seo.no_dashes(subject or "").strip())
    if not m:
        return None
    project, rest = m.group("project").strip(), m.group("rest").strip()
    out = {"project": project, "kind": "crawl", "subject_issue": None, "issues": [], "samples": []}
    if re.search(r"crawl (error|failed)", rest, re.IGNORECASE):
        out["kind"] = "crawl_error"
        return out
    hit = SUBJECT_ISSUE_RE.match(rest)
    if hit and seo.clean(hit.group("name"), 120):
        out["subject_issue"] = {"name": seo.clean(hit.group("name"), 120), "count": number_of(hit.group("count"))}
    lines = [line.strip() for line in (body or "").splitlines()]
    inside = False
    for line in lines:
        low = line.lower()
        if low in SECTION_START:
            inside = True
            continue
        if low.startswith("view all issues"):
            inside = False
            continue
        if not inside or not line or low == "view":
            continue
        got = ISSUE_RE.match(line)
        name = seo.clean(got.group("name"), 120) if got else ""
        if name and len(out["issues"]) < 40:
            delta = got.group("delta")
            out["issues"].append({
                "name": name, "count": number_of(got.group("count")),
                "delta": (-1 if delta and delta[0] in "-−" else 1) * number_of(delta) if delta else None,
                "new": bool(got.group("new")), "view": None,
            })  # fmt: skip
    views = [l for l in links or [] if isinstance(l, str) and l.startswith("https://app.ahrefs.com/") and "/data-explorer" in l]
    if len(views) == len(out["issues"]):
        for issue, view in zip(out["issues"], views):
            issue["view"] = view[:600]
    if not out["issues"] and out["subject_issue"]:
        out["issues"].append(dict(out["subject_issue"], delta=None, new="[new]" in rest.lower(), view=None))
    samples = []
    for candidate in re.findall(r"https?://[^\s<>()\"']+", body or "") + list(links or []):
        url = norm_url(candidate)
        if url and internal(url, domain) and url not in samples:
            samples.append(url)
    out["samples"] = samples[:20]
    return out


def project_blog(config, project):
    """Nazwa bloga dla projektu Ahrefs z konfiguracji (audit.ahrefs_project) albo None."""
    for name, raw in (config.get("blogs") or {}).items():
        audit = raw.get("audit") if isinstance(raw, dict) else None
        if isinstance(audit, dict) and str(audit.get("ahrefs_project") or "").lower() == project.lower():
            return name
    return None


def mail_date(text):
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def gateway(ctx):
    if ctx.gateway is not None:
        return ctx.gateway
    import mail  # bramka claude-acc: SELECT tylko do odczytu, BODY.PEEK; nic nie zostaje oznaczone

    return mail.Gateway(mail.load_config(), client="seo-audit")


def ahrefs_source(ctx, blog, config, settings):
    """Alerty Site Audit tego bloga z ostatnich max_age_days: (problemy {nazwa: {...}}, status).
    Najnowszy mail z przeglądem daje liczby; starsze tylko datę, od kiedy problem widać."""
    project = blog["audit"].get("ahrefs_project")
    if not project:
        return {}, source("ahrefs", False, "blog nie ma projektu w Ahrefs (audit.ahrefs_project)", skipped=True)
    try:
        gw = gateway(ctx)
        query = f"from:{settings['from']} subject:{project} newer_than:{int(settings['max_age_days'])}d"
        found = gw.run("mail_search", {"mailbox": settings["mailbox"], "query": query, "max_results": 30})
    except Exception as exc:  # bramka, IMAP, konfiguracja: źródło jest uboższe, bieg idzie dalej
        return {}, source("ahrefs", False, f"poczta: {str(exc)[:140]}", degraded=True)
    mails = []
    for msg in found.get("messages") or []:
        sender = str(msg.get("from") or "").lower()
        m = SUBJECT_RE.match(str(msg.get("subject") or ""))
        if settings["from"].lower() not in sender or not m:
            continue
        if project_blog(config, m.group("project").strip()) != blog["name"]:
            continue  # inny projekt (np. Fiveseasons): nie nasz blog
        mails.append(msg)
    mails.sort(key=lambda msg: mail_date(msg.get("date")) or 0, reverse=True)
    issues, errors, read, problems = {}, [], 0, []
    latest = None
    for msg in mails[: settings["max_mails"]]:
        try:
            full = gw.run("mail_read", {"mailbox": settings["mailbox"], "message_id": msg["id"]})
        except Exception as exc:
            problems.append(str(exc)[:80])
            continue
        read += 1
        parsed = parse_ahrefs(full.get("subject") or msg.get("subject"), full.get("body") or "", full.get("links") or [], blog["domain"])
        if not parsed:
            continue
        when = mail_date(msg.get("date"))
        if parsed["kind"] == "crawl_error":
            errors.append(seo.iso(when)[:10] if when else "?")
            continue
        if latest is None:
            latest = (when, parsed)
        for issue in parsed["issues"]:
            entry = issues.get(issue["name"].lower())
            if entry is None:
                if parsed is not latest[1]:
                    continue  # ze starszego maila bierzemy tylko datę problemu, który wciąż jest
                entry = issues[issue["name"].lower()] = dict(issue, mail_date=seo.iso(when) if when else None, since=when,
                                                              samples=parsed["samples"], message_id=msg["id"])  # fmt: skip
            elif when and (entry["since"] is None or when < entry["since"]):
                entry["since"] = when
    if latest is None:
        detail = f"brak maili z przeglądem crawla projektu {project} z {settings['max_age_days']} dni"
        if errors:
            detail += f"; błędy crawla Ahrefs: {', '.join(errors[:3])}"
        if problems:
            detail += f"; poczta: {problems[0]}"
        return {}, source("ahrefs", False, detail, degraded=bool(problems))
    when = seo.iso(latest[0])[:10] if latest[0] else "?"
    detail = f"mail z {when}: {seo.plural(len(issues), 'problem', 'problemy', 'problemów')} (przeczytane {read})"
    if errors:
        detail += f"; błędy crawla Ahrefs: {', '.join(errors[:3])}"
    return issues, source("ahrefs", True, detail, len(issues), degraded=bool(problems))


# ---------- audit.json, schemat v1 ----------

_SOURCE = seo.SCHEMA_V1["properties"]["sources"]["items"]
_FIX = {
    "type": "object", "required": ["page", "action", "since"],
    "properties": {"page": {"type": "string", "pattern": "^https?://"},
                   "action": {"enum": ["replace_link", "unlink_keep_anchor", "add_hreflang", "rewrite_meta", "add_alt"]},
                   "since": {"type": "string"}},
}  # fmt: skip
_ISSUE = {
    "type": "object",
    "required": ["id", "type", "subject", "kind", "severity", "title", "found_by", "since", "first_seen", "new", "count",
                 "urls", "evidence", "fixes", "escalate"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "type": {"enum": list(TYPES)},
        "subject": {"type": ["string", "null"]},
        "kind": {"enum": ["content", "code"]},
        "severity": {"enum": list(SEVERITY_ORDER)},
        "title": {"type": "string", "minLength": 1},
        "found_by": {"type": "array", "minItems": 1, "items": {"enum": ["crawl", "ahrefs", "bing"]}},
        "since": {"type": "object"},
        "first_seen": {"type": "string"},
        "new": {"type": "boolean"},
        "count": {"type": "integer", "minimum": 0},
        "urls": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "object"},
        "fixes": {"type": "array", "items": _FIX},
        "escalate": {"type": "boolean"},
    },
}  # fmt: skip
SCHEMA_V1 = {
    "$id": "claude-acc/seo/audit/v1",
    "type": "object",
    "required": ["schema", "v", "blog", "domain", "site", "generated_at", "sources", "crawl", "ai_bots", "ahrefs",
                 "escalation", "summary", "issues"],
    "properties": {
        "schema": {"const": "claude-acc.seo.audit"},
        "v": {"const": 1},
        "blog": {"type": "string"},
        "domain": {"type": "string"},
        "site": {"type": "string"},
        "generated_at": {"type": "string"},
        "sources": {"type": "array", "items": _SOURCE},
        "crawl": {
            "type": "object",
            "required": ["urls", "fetched", "not_modified", "from_cache", "missing", "targets", "requests", "seconds",
                         "complete", "time_cap_s", "checks"],
            "properties": {"urls": {"type": "integer", "minimum": 0}, "fetched": {"type": "integer", "minimum": 0},
                           "seconds": {"type": "number", "minimum": 0}, "complete": {"type": "boolean"}},
        },
        "ai_bots": {"type": "array", "items": {"type": "object", "required": ["bot", "robots", "robots_group", "fetch"]}},
        "ahrefs": {"type": "array", "items": {"type": "object", "required": ["name", "count", "crawl_type", "crawl_count"]}},
        "escalation": {"type": "object", "required": ["floor", "escalate"]},
        "summary": {"type": "object", "required": ["issues", "content", "code", "fixes", "new"]},
        "issues": {"type": "array", "items": _ISSUE},
    },
}  # fmt: skip


def validate(doc):
    return seo.validate(doc, SCHEMA_V1)


# ---------- składanie ----------


def escalation_floor(pages):
    """Ile adresów to "wiele" dla eskalacji: 20 albo 5% stron z mapy, co mniej, najmniej 3."""
    return max(3, min(20, math.ceil(0.05 * pages)))


def load_state(path):
    state = seo.read_json(path, {}) or {}
    if not isinstance(state, dict):
        state = {}
    for key in ("issues", "fixes", "escalated"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    return state


def fix_key(fix):
    return "|".join(str(fix.get(k, "")) for k in ("page", "action", "from", "href", "field", "src"))


def assemble(acc, ahrefs, bing_found, state, now, floor, checked):
    """Lista problemów audit.json i nowy stan (since, eskalacje). checked: typy, których sprawdzenie
    odbyło się w tym biegu w pełni; tylko one mogą zniknąć ze stanu."""
    now_iso = seo.iso(now)
    for type, url, detail in bing_found:
        acc.add(type, url, detail, found_by="bing")
    comparison = []
    for name, issue in ahrefs.items():
        ctype, subject = (AHREFS_TYPES.get(name), None) if not isinstance(AHREFS_TYPES.get(name), tuple) else AHREFS_TYPES[name]
        matched = [item for item in acc.items.values() if ctype and base_type(item["type"]) == ctype and "crawl" in item["found_by"]
                   and (subject is None or item["subject"] == subject)]  # fmt: skip
        ext = {"issue": issue["name"], "count": issue["count"], "delta": issue["delta"], "new": issue["new"],
               "mail_date": issue["mail_date"], "view": issue["view"], "samples": issue["samples"][:10]}  # fmt: skip
        crawl_count = len({u for item in matched for u in item["urls"]})  # strona z linkiem w treści i w menu to jedna strona
        comparison.append({"name": issue["name"], "count": issue["count"], "delta": issue["delta"],
                           "crawl_type": f"{ctype}|{subject}" if ctype and subject else ctype,
                           "crawl_count": crawl_count if ctype else None, "mail_date": issue["mail_date"]})  # fmt: skip
        if matched:
            for item in matched:
                item["found_by"]["ahrefs"] = True
                item["ext"]["ahrefs"] = ext
                item.setdefault("ahrefs_since", issue["since"])
        else:
            item = acc.add("ahrefs", subject=issue["name"], found_by="ahrefs")
            item["ext"]["ahrefs"] = ext
            item["ahrefs_count"] = issue["count"]
            item["ahrefs_since"] = issue["since"]
            for sample in issue["samples"]:
                item["urls"][sample] = True
    issues, new_state = [], {"v": 1, "issues": {}, "fixes": {}, "escalated": dict(state["escalated"])}
    for key, item in acc.items.items():
        kind, severity, title = TYPES[item["type"]]
        if item["type"] == "ahrefs":
            title = f"Ahrefs: {item['subject']}"
        elif item["subject"]:
            title = f"{title}: {item['subject']}"
        old = state["issues"].get(key) or {}
        since = dict(old.get("since") or {})
        for src in item["found_by"]:
            if src not in since:
                when = item.get("ahrefs_since") if src == "ahrefs" else None
                since[src] = seo.iso(when) if when else now_iso
        first_seen = min(since.values())
        fixes = []
        for fix in item["fixes"].values():
            fk = fix_key(fix)
            fix_since = state["fixes"].get(fk) or now_iso
            new_state["fixes"][fk] = fix_since
            fixes.append(dict(fix, since=fix_since))
        count = item.get("ahrefs_count") or len(item["urls"])
        urls = list(item["urls"])
        is_block = item["type"] in ("ai_robots_blocked", "ai_fetch_blocked")
        escalate = kind == "code" and item["type"] != "ahrefs" and key not in state["escalated"] and (
            is_block or (severity == "error" and count >= floor))
        if escalate:
            new_state["escalated"][key] = now_iso
        new_state["issues"][key] = {"since": since}
        evidence = {src: lines[:20] for src, lines in item["details"].items() if lines}
        evidence.update(item["ext"])
        issues.append({
            "id": key, "type": item["type"], "subject": item["subject"], "kind": kind, "severity": severity,
            "title": title, "found_by": list(item["found_by"]), "since": since, "first_seen": first_seen,
            "new": not old, "count": count, "urls": urls[:50], "evidence": evidence, "fixes": fixes[:3000],
            "escalate": escalate,
        })  # fmt: skip
    # problem, którego sprawdzenie przeszło w całości i go nie znalazło, zniknął: następne pojawienie się
    # znowu jest nowe (i może znowu eskalować)
    for key in list(new_state["escalated"]):
        if key not in acc.items and key.split("|", 1)[0] in checked:
            del new_state["escalated"][key]
    for key, value in state["issues"].items():
        if key not in new_state["issues"] and key.split("|", 1)[0] not in checked:
            new_state["issues"][key] = value  # nie sprawdzone w tym biegu: zostaje, jak było
    issues.sort(key=lambda i: (not i["escalate"], SEVERITY_ORDER[i["severity"]], i["kind"] != "code", -i["count"], i["id"]))
    return issues, new_state, comparison


def checked_types(cr, analysed_links, llms_ran, indexnow_ran, bots_ran, ahrefs_ok, bing_ok):
    """Typy, których sprawdzenie odbyło się w tym biegu w pełni (problem mógł więc zniknąć)."""
    done = set()
    full = cr.robots is not None and cr.sitemap_ok and cr.done_pages and cr.done_targets and not cr.fetcher.down
    if cr.robots is not None:
        done.add("robots_unreachable")
    if cr.sitemap_ok:
        done |= {"sitemap_unreachable"}
    if full:
        done |= {t for t in TYPES if t not in ("ahrefs",) and not t.startswith(("bing_", "ai_", "llms_", "indexnow"))}
        if not analysed_links:
            done.discard("orphan")
    if llms_ran:
        done |= {"llms_unreachable", "llms_missing"}
    if indexnow_ran:
        done.add("indexnow_key")
    if bots_ran:
        done |= {"ai_robots_blocked", "ai_fetch_blocked", "ai_slow"}
    if ahrefs_ok:
        done.add("ahrefs")
    if bing_ok:
        done |= {t for t in TYPES if t.startswith("bing_")}
    return done


def audit_posts(ctx, blog, sitemap_urls):
    """Wpisy bloga z konfiguracji K1: posts.kind sitemap filtruje adresy z mapy, którą crawl już
    pobrał (include), json czyta listę jak `seo research`."""
    spec = blog.get("posts") or {}
    if spec.get("kind") == "json":
        posts, status = seo.load_posts(ctx, blog)
        out = []
        for post in posts:
            url = norm_url(post["url"])
            if url:
                out.append({"url": url, "lang": post["lang"]})
        return out, status
    include = re.compile(spec.get("include") or r"/blog/[^/]+$")
    out = [{"url": u, "lang": seo.lang_of_path(blog, seo.url_path(u))} for u in sitemap_urls
           if internal(u, blog["domain"]) and include.search(seo.url_path(u))]  # fmt: skip
    return out, source("posts", bool(out), f"{seo.plural(len(out), 'wpis', 'wpisy', 'wpisów')} z mapy strony", len(out))


# ---------- konfiguracja ----------


def audit_config(blog):
    """Pola audytu bloga (blog["audit"]) z domyślnymi wartościami; UsageError mówi, co poprawić."""
    raw = blog.get("audit")
    if not isinstance(raw, dict):
        raise seo.UsageError(f"{blog['name']}.audit: brak sekcji audit w konfiguracji (wzór w README, SEO audit)")
    cfg = dict(DEFAULT_AUDIT)
    cfg.update(raw)
    cfg["canonical_host"] = str(cfg.get("canonical_host") or seo.host_of(blog["site"])).lower()
    origin = f"https://{cfg['canonical_host']}"
    cfg["sitemap"] = cfg.get("sitemap") or origin + "/sitemap.xml"
    cfg["llms"] = cfg.get("llms") or origin + "/llms.txt"
    for key in ("title", "description"):
        value = cfg[key]
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value) and 0 <= value[0] <= value[1]):
            raise seo.UsageError(f"{blog['name']}.audit.{key}: [min, max] w znakach, np. {DEFAULT_AUDIT[key]}")
    try:
        cfg["time_cap_min"] = float(cfg["time_cap_min"])
        cfg["concurrency"] = int(cfg["concurrency"])
        cfg["interval_s"] = float(cfg["interval_s"])
        cfg["ai_slow_ms"] = int(cfg["ai_slow_ms"])
    except (TypeError, ValueError):
        raise seo.UsageError(f"{blog['name']}.audit: time_cap_min, concurrency, interval_s i ai_slow_ms to liczby")
    if not 1 <= cfg["time_cap_min"] <= 240:
        raise seo.UsageError(f"{blog['name']}.audit.time_cap_min: od 1 do 240 minut")
    if not 1 <= cfg["concurrency"] <= 4:
        raise seo.UsageError(f"{blog['name']}.audit.concurrency: od 1 do 4 zapytań naraz (to nasze serwery)")
    if cfg["interval_s"] < 0.05 and not os.environ.get("CLAUDE_ACC_SEO_INTERVAL"):
        raise seo.UsageError(f"{blog['name']}.audit.interval_s: najmniej 0.05 s między zapytaniami")
    if not isinstance(cfg["title_suffix"], list) or not all(isinstance(x, str) and x for x in cfg["title_suffix"]):
        raise seo.UsageError(f"{blog['name']}.audit.title_suffix: lista końcówek tytułu dokładanych przez szablon, np. [\" | Marka\"]")
    for key in ("sitemap", "llms", "indexnow_key_url", "bing_site"):
        if cfg.get(key) and not re.match(r"^https?://", str(cfg[key])):
            raise seo.UsageError(f"{blog['name']}.audit.{key}: pełny adres https://...")
    return cfg


def ahrefs_settings(config):
    out = dict(AHREFS)
    raw = config.get("ahrefs")
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in AHREFS})
    return out


# ---------- wyjście dla człowieka ----------


def duration(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def fix_line(fix):
    action = fix["action"]
    if action == "replace_link":
        return f"{seo.url_path(fix['page'])}: link \"{seo.fit_text(fix['anchor'], 40)}\" {fix['from']} -> {fix['to']}"
    if action == "unlink_keep_anchor":
        return f"{seo.url_path(fix['page'])}: usuń link do {fix['href']} ({fix['status']}), zostaw tekst \"{seo.fit_text(fix['anchor'], 40)}\""
    if action == "add_hreflang":
        return f"{seo.url_path(fix['page'])}: dodaj hreflang {fix['hreflang']} -> {fix['href']}"
    if action == "rewrite_meta":
        dup = f", taki sam jak {seo.url_path(fix['duplicate_of'][0])}" if fix.get("duplicate_of") else ""
        return (f"{seo.url_path(fix['page'])}: {fix['field']} {fix['length']} znaków (cel {fix['min']}-{fix['max']}){dup}: "
                f"\"{seo.fit_text(fix['current'], 70)}\"")  # fmt: skip
    return f"{seo.url_path(fix['page'])}: alt dla {fix['src']}"


def ahrefs_line(row):
    delta = f" ({row['delta']:+d})" if row.get("delta") else ""
    if row["crawl_type"] is None:
        return f"{row['name']}: Ahrefs {row['count']}{delta}; crawl tego nie liczy"
    return f"{row['name']}: Ahrefs {row['count']}{delta}, crawl {row['crawl_count']} ({row['crawl_type']})"


def crawl_line(crawl):
    line = (f"{crawl['urls']} adresów z mapy i wpisów, pobrane {crawl['fetched']} (304: {crawl['not_modified']}), "
            f"z pamięci {crawl['from_cache']}, bez danych {crawl['missing']}; cele linków {crawl['targets']}; "
            f"{crawl['requests']} zapytań w {duration(crawl['seconds'])}")  # fmt: skip
    return line + ("" if crawl["complete"] else ", NIEPEŁNY (limit czasu albo host)")


def report(doc, n=15):
    """Najważniejsze problemy z dowodami, podzbiór do poprawy w treści, Ahrefs obok crawla."""
    lines = [f"Audyt SEO {doc['blog']} ({doc['site']}): {doc['summary']['issues']} problemów, "
             f"{doc['summary']['content']} w treści ({doc['summary']['fixes']} poprawek), {doc['summary']['code']} w kodzie, "
             f"nowe: {doc['summary']['new']}"]  # fmt: skip
    lines.append("Crawl: " + crawl_line(doc["crawl"]))
    escalate = [i for i in doc["issues"] if i["escalate"]]
    if escalate:
        lines.append("Do eskalacji (PILNE): " + "; ".join(f"{i['title']} ({i['count']})" for i in escalate))
    lines.append("")
    lines.append(f"Najważniejsze problemy (z {len(doc['issues'])}):")
    for i, issue in enumerate(doc["issues"][:n], 1):
        since = min(issue["since"].values())[:10]
        lines.append(f"{i:>2}. [{issue['kind']}/{issue['severity']}] {issue['title']}: {issue['count']} "
                     f"(źródła: {', '.join(issue['found_by'])}, od {since}{', nowy' if issue['new'] else ''})")  # fmt: skip
        if issue["fixes"]:
            for fix in issue["fixes"][:3]:
                lines.append(f"      poprawka: {fix_line(fix)}")
        else:
            for detail in issue["evidence"].get("crawl", [])[:3]:
                lines.append(f"      dowód: {seo.fit_text(detail, 150)}")
        if "ahrefs" in issue["evidence"]:
            a = issue["evidence"]["ahrefs"]
            lines.append(f"      Ahrefs ({(a['mail_date'] or '?')[:10]}): {a['issue']} {a['count']}" + (f" ({a['delta']:+d})" if a.get("delta") else ""))
        if "bing" in issue["found_by"]:
            lines.append("      Bing: zgłasza też te adresy (GetCrawlIssues)")
    content = [i for i in doc["issues"] if i["kind"] == "content"]
    lines.append("")
    lines.append(f"Do poprawy w treści: {sum(len(i['fixes']) for i in content)} poprawek w {len({f['page'] for i in content for f in i['fixes']})} wpisach")
    for issue in content:
        lines.append(f"  {issue['title']}: {len(issue['fixes'])}")
        for fix in issue["fixes"][:12]:
            lines.append(f"    {fix_line(fix)}")
        if len(issue["fixes"]) > 12:
            lines.append(f"    ... i {len(issue['fixes']) - 12} więcej")
    if doc["ahrefs"]:
        lines.append("")
        lines.append("Ahrefs obok crawla:")
        for row in doc["ahrefs"]:
            lines.append(f"  {ahrefs_line(row)}")
    if doc["ai_bots"]:
        lines.append("")
        lines.append("Boty AI (robots.txt, status i czas z ich User-Agentem):")
        for row in doc["ai_bots"]:
            fetched = ", ".join(f"{seo.url_path(f['url'])} {f['status']} {f['ms']} ms" for f in row["fetch"] or []) or "tylko robots.txt"
            lines.append(f"  {row['bot']}: robots {row['robots']} (grupa {row['robots_group']}); {fetched}")
    lines.append("")
    for s in doc["sources"]:
        lines.append(f"  {s['name']}: {'ok' if s['ok'] else 'pominięte' if s['skipped'] else 'brak'}{', uboższe' if s['degraded'] else ''}: {s['detail']}")
    return "\n".join(seo.no_dashes(line) for line in lines)


def wynik_lines(doc, outcome, path):
    s = doc["summary"]
    lines = [f"WYNIK: {outcome}",
             f"Audyt SEO {doc['blog']}: {s['issues']} problemów ({s['content']} w treści, {s['fixes']} poprawek; "
             f"{s['code']} w kodzie), nowe: {s['new']}."]  # fmt: skip
    escalate = [i for i in doc["issues"] if i["escalate"]]
    if escalate:
        lines.append("PILNE: " + "; ".join(f"{i['title']} ({i['count']})" for i in escalate[:4]))
    code = [i for i in doc["issues"] if i["kind"] == "code" and not i["escalate"]][:3]
    if code:
        lines.append("Kod: " + "; ".join(f"{i['title']} ({i['count']})" for i in code))
    if doc["ahrefs"]:
        lines.append("Ahrefs: " + "; ".join(ahrefs_line(r) for r in doc["ahrefs"][:4]))
    lines.append("Crawl: " + crawl_line(doc["crawl"]))
    lines.append("Źródła: " + "; ".join(f"{x['name']} {'ok' if x['ok'] else 'pominięte' if x['skipped'] else 'brak'}: {x['detail']}" for x in doc["sources"]))
    if path:
        lines.append(f"Plik: {path}")
    return [seo.no_dashes(line) for line in lines]


# ---------- bieg ----------


def audit(ctx, blog, config, dry=False, out_dir=None):
    """Cały audyt jednego bloga; zwraca kod wyjścia (0, 5 albo 1)."""
    cfg = audit_config(blog)
    blog = dict(blog, audit=cfg)
    out_dir = out_dir or os.path.join(seo.SEO_DIR, blog["name"])
    state_path = os.path.join(out_dir, "state.json")
    started = ctx.now()
    acc = Acc()
    sources = []

    cr = Crawl(ctx, blog, cfg)
    post_status = {}

    def posts_for(urls):
        posts, status = audit_posts(ctx, blog, urls)
        post_status["s"] = status
        return posts

    posts = cr.run(posts_for)
    links_full = analyse(cr, blog, cfg, posts, acc, ctx.now())
    llms = indexnow = None
    table = []
    if cr.robots is not None and not cr.fetcher.down:
        try:
            llms = check_llms(cr, posts, acc)
            indexnow = check_indexnow(cr, acc)
            table = check_ai_bots(cr, posts, cfg, acc)
        except HostDown as exc:
            cr.problems.append(f"host przestał odpowiadać: {exc}")
    ctx.save_caches()
    seconds = ctx.now() - started
    missing = sum(1 for u in cr.list if u not in cr.pages and (cr.statuses.get(u) or {}).get("s") is None)
    complete = cr.robots is not None and cr.sitemap_ok and cr.done_pages and cr.done_targets and not cr.fetcher.down
    crawl = {
        "urls": len(cr.list), "fetched": len(cr.fresh), "not_modified": len(cr.not_modified), "from_cache": len(cr.from_cache),
        "missing": missing, "targets": cr.targets, "requests": cr.fetcher.requests, "seconds": round(seconds, 1),
        "complete": complete, "time_cap_s": int(cfg["time_cap_min"] * 60), "robots_status": cr.robots_status,
        "sitemap_urls": len(cr.sitemap), "posts": len(posts), "pauses": cr.fetcher.pauses,
        "checks": {"llms": llms, "indexnow": indexnow, "orphans": "tak" if links_full else "pominięte: niepełny obraz linków",
                   "robots_skipped": len(cr.robots_skipped)},
        "problems": [seo.no_dashes(p)[:200] for p in cr.problems[:10]],
    }  # fmt: skip
    crawl_detail = crawl_line(crawl)
    if post_status.get("s") and not post_status["s"]["ok"]:
        crawl_detail += f"; wpisy: {post_status['s']['detail']}"
    if cr.problems:
        crawl_detail += f"; {cr.problems[0]}"
    sources.append(source("crawl", bool(cr.pages), crawl_detail, len(cr.pages), degraded=not complete))
    ahrefs, ahrefs_status = ahrefs_source(ctx, blog, config, ahrefs_settings(config))
    sources.insert(0, ahrefs_status)
    bing_found, bing_status = bing_source(ctx, cfg)
    sources.append(bing_status)
    if not cr.pages and not ahrefs and not bing_found:
        # sam robots.txt albo mapa bez odpowiedzi to zwykle chwilowa awaria hosta: poprzedni audit.json
        # zostaje, a ponowienie w slocie sprawdzi jeszcze raz
        found = "; ".join(d for _, _, d, _ in cr.issues if d)
        reason = "żadne źródło nic nie dało (" + "; ".join(f"{s['name']}: {s['detail']}" for s in sources) + ")" + (f"; {found}" if found else "")
        if not dry:
            seo.write_result({"outcome": "BŁĄD", "reason": seo.no_dashes(reason)[:400], "title": None, "urls": [], "pr": None,
                              "retryable": True, "notes": "poprzedni audit.json zostaje"})  # fmt: skip
        print(seo.no_dashes(f"\nWYNIK: BŁĄD\nAudyt SEO {blog['name']}: {reason}"))
        return 1
    state = load_state(state_path)
    floor = escalation_floor(len(cr.sitemap) or len(cr.list))
    checked = checked_types(cr, links_full, llms is not None and not str(llms).startswith("pominięte"),
                            indexnow is not None and not str(indexnow).startswith("pominięte"), bool(table),
                            ahrefs_status["ok"], bing_status["ok"])  # fmt: skip
    issues, new_state, comparison = assemble(acc, ahrefs, bing_found, state, ctx.now(), floor, checked)
    content = [i for i in issues if i["kind"] == "content"]
    doc = {
        "schema": "claude-acc.seo.audit", "v": 1, "blog": blog["name"], "domain": blog["domain"], "site": f"https://{cfg['canonical_host']}",
        "generated_at": seo.iso(ctx.now()), "sources": sources, "crawl": crawl, "ai_bots": table, "ahrefs": comparison,
        "escalation": {"floor": floor, "rule": "nowy błąd kodu (waga error) na co najmniej floor adresach albo blokada bota AI; raz",
                       "escalate": [i["id"] for i in issues if i["escalate"]]},
        "summary": {"issues": len(issues), "content": len(content), "code": len(issues) - len(content),
                    "fixes": sum(len(i["fixes"]) for i in content), "new": sum(1 for i in issues if i["new"])},
        "issues": issues,
    }  # fmt: skip
    problems = validate(doc)
    if problems:
        raise RuntimeError("audit.json nie przechodzi schematu v1: " + "; ".join(problems[:5]))
    if dry:
        print(report(doc))
        print(seo.no_dashes(f"\nNa sucho: nic nie zapisano (audit.json, state.json, wynik joba). Próg eskalacji: {floor} adresów."))
        return 0
    path = os.path.join(out_dir, "audit.json")
    escalate = [i for i in issues if i["escalate"]]
    outcome = "PILNE" if escalate else "OPUBLIKOWANO"
    seo.write_result({"phase": "side-effect"})
    seo.atomic_write(path, json.dumps(doc, indent=1, ensure_ascii=False) + "\n", 0o644)
    seo.atomic_write(state_path, json.dumps(new_state, ensure_ascii=False) + "\n")
    degraded = [f"{s['name']}: {s['detail']}" for s in sources if (not s["ok"] and not s["skipped"]) or s["degraded"]]
    s = doc["summary"]
    reason = (f"audit.json: {s['issues']} problemów, {s['fixes']} poprawek treści, {s['code']} w kodzie, nowe {s['new']}"
              + ("; PILNE: " + "; ".join(f"{i['title']} ({i['count']})" for i in escalate[:3]) if escalate else ""))  # fmt: skip
    urls = [u for i in escalate for u in i["urls"][:3]][:10]
    seo.write_result({
        "outcome": outcome, "title": f"Audyt SEO {blog['name']}: {s['issues']} problemów", "reason": seo.no_dashes(reason)[:400],
        "urls": urls, "pr": None, "retryable": False, "notes": seo.no_dashes("; ".join(degraded))[:600],
    })  # fmt: skip
    print(report(doc))
    print("\n" + "\n".join(wynik_lines(doc, outcome, path)))
    return 5 if escalate else 0


def cmd_audit(args):
    if not args or args[0].startswith("-"):
        raise seo.UsageError("claude-acc seo audit BLOG [--dry]")
    name, flags = args[0], args[1:]
    unknown = [f for f in flags if f != "--dry"]
    if unknown:
        raise seo.UsageError(f"nieznana opcja {unknown[0]}; jest --dry")
    dry = "--dry" in flags
    config = seo.load_config()
    blog = seo.blog_config(config, name)
    try:
        return audit(Ctx(), blog, config, dry=dry)
    except seo.UsageError:
        raise
    except Exception as exc:
        if not dry:
            seo.write_result({"outcome": "BŁĄD", "reason": seo.no_dashes(f"audyt {name}: {exc!r}")[:400], "title": None,
                              "urls": [], "pr": None, "retryable": True, "notes": ""})  # fmt: skip
        raise


if __name__ == "__main__":
    sys.modules.setdefault("seoaudit", sys.modules[__name__])
    try:
        sys.exit(cmd_audit(sys.argv[1:]))
    except seo.UsageError as exc:
        print(f"błąd: {exc}", file=sys.stderr)
        sys.exit(2)
