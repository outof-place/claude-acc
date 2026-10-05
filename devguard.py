#!/usr/bin/env python3
"""Strażnik dev serwerów: agenci w Orce nie zajadą Maca serwerami `next dev`.

Skąd problem (zmierzone 2026-10-04 na 48 GB RAM):
- `next dev` z Turbopackiem trzyma graf modułów w natywnej pamięci Rusta i po godzinie
  pracy agenta dobija do 7-8 GB footprintu. Wbudowany watchdog Next.js patrzy tylko na
  stertę V8 (restart przy 80% heap), więc tych gigabajtów nie widzi;
- otwarty podgląd (karta w Orce, przeglądarka) trzyma websocket HMR: każdy plik zapisany
  przez agenta to rekompilacja i przeładowanie strony (jedna zmiana tokens.css = 10 s CPU),
  a serwer bez podglądu na tę samą zmianę prawie nie reaguje;
- kilka worktree, w każdym agent ze swoim serwerem i kartą, i swap rośnie do pełna, a jądro
  do końca zgłasza presję "normalną". Potem jetsam ubija procesy z powodem low-swap.

Co robi strażnik:
- mierzy to, co mierzy jądro: phys_footprint z proc_pid_rusage (ten sam licznik, którym
  jetsam wybiera ofiary), czas CPU i zapisy na dysk, bez forkowania niczego na proces;
- wie, kto ogląda serwer: klientów TCP z gniazd procesów (Orca, przeglądarka, headless)
  i karty podglądu z Orki, a także to, czy patrzysz na nią w tej chwili;
- presję liczy ze swapu i z licznika swapoutów kompresora; poziom presji jądra jest
  tylko dodatkiem, bo przy pełnym swapie potrafi dalej mówić "normal";
- serwer, na który nie patrzysz, dostaje QoS tła Darwina (PRIO_DARWIN_BG: tylko rdzenie
  energooszczędne, dławione IO), więc burze rekompilacji po edycjach agentów nie lagują
  interfejsu; gdy przełączysz się na jego podgląd, wraca do zwykłego priorytetu;
- spuchnięty serwer restartuje w tym samym terminalu Orki: Turbopack wstaje z cache
  na dysku w kilka sekund, a karta podglądu sama się podłącza;
- zatrzymuje duplikaty tej samej aplikacji, sieroty po zamkniętych agentach i serwery,
  których nikt nie ogląda; serwera, na który patrzysz, nigdy nie zatrzymuje, najwyżej
  restartuje przy krytycznej presji;
- `admit` to hook PreToolUse dla Claude Code: agent nie postawi drugiego serwera tej
  samej aplikacji, tylko dostanie adres tego, który już działa.

Komendy:
  run                   pętla dla launchd: pomiar co kilka sekund, najwyżej jedna akcja naraz
  status [--json]       serwery, pamięć, kto je ogląda i co strażnik z nimi zrobi
  once [--dry-run]      jeden pomiar i co najwyżej jedna akcja
  stop <pid|:port>      zatrzymaj serwer tak, jak robi to strażnik
  recycle <pid|:port>   restart w tym samym terminalu Orki
  pin <:port|katalog> [--for 12h | --forever] [--reason TEKST] [--no-restart]
                        wyjątek na czas: strażnik go nie zatrzyma (bezczynność, duplikat,
                        sierota, budżet); spuchnięty dostaje restart, z --no-restart nawet
                        nie to, poza krytyczną presją
  unpin <:port|katalog|all>   zdejmij przypięcie
  pins                  przypięcia, ich powody i terminy
  admit                 hook PreToolUse (Bash) dla Claude Code; zdarzenie czyta z stdin
  words                 słowa, od których komenda idzie do `admit` dalej niż szybka ścieżka,
                        jako JSON {"dev": [...], "go": [...]} dla natywnego claude-acc-hook
"""

# Ten plik to tylko wejście. Hook `admit` idzie przy każdym poleceniu Bash każdego agenta, a
# skrypt podany Pythonowi wprost kompiluje się przy każdym starcie (bajtkod z __pycache__
# dostają tylko importowane moduły): przy 1900 liniach to było 7-10 ms na każdego Basha.
# Strażnik (pomiar, decyzje, akcje) leży więc w devguard_core.py i ładuje się z cache, a tu
# zostaje tylko to, czego hook potrzebuje dla komendy, która dev serwera nie stawia.

import os
import sys

DEV_WORDS = ("dev", "vite", "expo", "serve")
GO_WORDS = ("go ", "golangci-lint", "make", "govulncheck")

# komenda stawiająca dev serwer, po zdjęciu opakowań (zmienne, rtk proxy, npx, pnpm exec).
# Wzorce to tekst: `re` kompiluje je przy pierwszym użyciu, więc komenda bez słowa od dev
# serwera nie płaci ani za import `re`, ani za kompilację
START = (
    r"^(?:next\s+dev|vite(?:\s+(?:dev|serve))?(?=\s+-|\s*$)|expo\s+start"
    r"|webpack(?:-cli)?\s+serve|astro\s+dev|nuxi?\s+dev"
    r"|(?:pnpm|yarn)\s+(?:--filter|-F)[\s=](?P<pkg>\S+)\s+(?:run\s+)?dev(?::\S+)?"
    r"|(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev(?::\S+)?|turbo\s+(?:run\s+)?dev)(?=\s|$)"
)
WRAPPERS = (
    r"^(?:\w+=\S*\s+|(?:rtk\s+proxy|nohup|exec|time|env|caffeinate(?:\s+-\w+)*)\s+"
    r"|(?:npx|bunx|(?:pnpm|yarn|npm)(?:\s+(?:-C|--dir|--prefix|--cwd)[\s=]\S+)*\s+(?:exec|dlx))"
    r"\s+(?:--\S+\s+)*)+"
)
DIR_FLAG = r"\s(-C|--dir|--prefix|--cwd)[\s=](\S+)"
# komenda w cudzysłowie, którą uruchomi ktoś inny: `orca terminal create --command`, `sh -c`
NESTED = r"""(?:--command|\b(?:ba|z)?sh\s+-c)[\s=](?:"((?:[^"\\]|\\.)*)"|'([^']*)')"""


def sched_rewrite(event):
    """Komenda Go agenta owinięta w scheduler (sched.py obok tego pliku): wyjście hooka z
    updatedInput albo None. Jedyny hook, który przepisuje komendy Bash; nigdy nie blokuje."""
    try:
        from importlib.machinery import SourceFileLoader

        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "sched.py")
        if not os.path.exists(path):
            return None
        # loader wprost, bez importlib.util, który na 3.9 ciągnie typing (4 ms); bajtkod
        # sched.py i tak przychodzi z cache
        module = type(sys)("acc_sched")
        module.__file__ = path
        SourceFileLoader("acc_sched", path).exec_module(module)
        return module.hook_rewrite(event)
    except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
        return None


def dev_starts(command, cwd):
    """[(katalog, filtr pakietu albo None, cały stos?)] dla każdej komendy stawiającej dev serwer."""
    import re

    found = []
    for inner in re.findall(NESTED, command):
        found += dev_starts(inner[0] or inner[1], cwd)
    for segment in re.split(r"&&|\|\||[;|\n&]", command):
        segment = segment.strip().strip("()").strip()
        cd = re.match(r"^cd\s+(\S+)$", segment)
        if cd:
            cwd = os.path.normpath(
                os.path.join(cwd, os.path.expanduser(cd.group(1).strip("'\"")))
            )
            continue
        bare = re.sub(WRAPPERS, "", segment)
        match = re.match(START, bare)
        if not match:
            continue
        target = cwd
        flag = re.search(DIR_FLAG, " " + segment)
        if flag:
            target = os.path.normpath(
                os.path.join(cwd, os.path.expanduser(flag.group(2).strip("'\"")))
            )
        package = match.group("pkg")
        # skrypt `dev` z package.json albo turbo: stawia to, co zdefiniował projekt
        stack = not package and bool(re.match(r"^(pnpm|npm|yarn|bun|turbo)\s", bare))
        found.append((target, package, stack))
    return found


def admit(raw):
    """Hook PreToolUse dla Basha; `raw` to zdarzenie z stdin.

    Idzie przy każdym poleceniu każdego agenta, więc kończy się jak najwcześniej. Słowa to
    minimum, które ma każda komenda pasująca do START (dev, vite, expo, webpack serve), i
    każda praca Go dla schedulera. Najpierw szuka ich w surowym JSON-ie: kodowanie JSON
    zmienia tylko znaki z odwrotnym ukośnikiem, a żadne z tych słów go nie ma, więc słowo
    nieobecne w tekście zdarzenia (bez ucieczek \\u) nie ma się skąd wziąć w komendzie, a
    `json` z `re` w ogóle się nie ładują. Fałszywy alarm (słowo w ścieżce, w /dev/null) idzie
    dalej i odpada na wzorcach; do strażnika (devguard_core) trafia tylko komenda, która
    naprawdę stawia dev serwer.
    """
    if "\\u" not in raw and not any(w in raw for w in DEV_WORDS + GO_WORDS):
        return 0
    import json

    try:
        event = json.loads(raw)
        command = (event.get("tool_input") or {}).get("command") or ""
    except (ValueError, AttributeError):
        return 0
    if any(word in command for word in DEV_WORDS):
        if event.get("tool_name") != "Bash":
            return 0
        cwd = event.get("cwd") or os.getcwd()
        if "DEVGUARD_ALLOW=1" not in command and dev_starts(command, cwd):
            import io

            sys.stdin = io.StringIO(raw)
            return core().main(["admit"])
    elif not any(word in command for word in GO_WORDS):
        return 0
    # bez dev serwera komenda idzie do schedulera, bez strażnika; ta ze słowem od dev serwera
    # także bez słowa od Go, jak dotąd po pełnej ścieżce
    out = sched_rewrite(event)
    if out:
        print(json.dumps(out))
    return 0


def core():
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import devguard_core

    return devguard_core


def main(argv):
    if argv[:1] == ["admit"]:
        try:
            return admit(sys.stdin.read())
        except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
            return 0
    if argv[:1] == ["words"]:
        # jedno źródło list dla obu frontów: setup.sh zapisuje to do hook-words.json
        import json

        print(json.dumps({"dev": list(DEV_WORDS), "go": list(GO_WORDS)}))
        return 0
    return core().main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
