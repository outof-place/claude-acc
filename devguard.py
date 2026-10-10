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
  admit [--codex]       hook PreToolUse (Bash) dla Claude Code, z --codex dla Codeksa; zdarzenie
                        czyta z stdin
  admitd                ciepły `admit` na gnieździe $STATE/admit.sock (admitd.py): pętla dla
                        agenta Pod codes.pod.app.acc.admit, z którą claude-acc-hook nie startuje Pythona
  room [katalog]        kod 0, gdy pamięć wpuści nowy dev serwer (w tym katalogu), 1 z powodem;
                        warunek dla `claude-acc sched wait`, który odmowa podaje agentowi
  brake [--stage N] [--within PID] [--json]
                        hamulec pamięci: stopień teraz i kogo zgasiłby (bez sygnałów)
  words                 słowa, od których komenda idzie do `admit` dalej niż szybka ścieżka,
                        i bramka całych słów, jako JSON {"dev": [...], "sched": [...],
                        "gate": [wzorzec]} dla natywnego claude-acc-hook
"""

# Ten plik to tylko wejście. Hook `admit` idzie przy każdym poleceniu Bash każdego agenta, a
# skrypt podany Pythonowi wprost kompiluje się przy każdym starcie (bajtkod z __pycache__
# dostają tylko importowane moduły): przy 1900 liniach to było 7-10 ms na każdego Basha.
# Strażnik (pomiar, decyzje, akcje) leży więc w devguard_core.py i ładuje się z cache, a tu
# zostaje tylko to, czego hook potrzebuje dla komendy, która dev serwera nie stawia.

import os
import sys

DEV_WORDS = ("dev", "vite", "expo", "serve", "react-native")
# słowa, bez których komenda nie ma pracy dla schedulera (Go, JS i natywne buildy z symulatorami);
# fałszywy alarm to tylko klasyfikacja w sched.py, która odpowie None
SCHED_WORDS = (
    "go ", "golangci-lint", "make", "govulncheck", "vitest", "jest", "playwright", "next ",
    "tsc", "eslint", "turbo", "pnpm", "npm ", "npx ", "yarn", "bun ", "bunx", "node_modules/.bin/",
    "xcodebuild", "simctl", "pod", "eas", "gradle", "portivo-mobile", "Simulator",
    # reszta ciężkiej pracy (sched.py, GENERIC_TOOLS i interpretery) i skrypty po ścieżce
    "cargo", "swift", "docker", "pytest", "py.test", "tox", "nox", "mypy", "pyright",
    "deno", "bazel", "mvn", "dotnet", "nx ", "lerna", "webpack", "rollup", "parcel",
    "tsup", "astro", "nuxt", "nuxi", "svelte-check", "cypress", "mocha", "ava ", "lighthouse",
    "expo", "react-native", "storybook", "just ", "task ", "python", "node ", "tsx", "ts-node",
    "uv ", "bash ", "sh ", "zsh ", ".sh", "/", "grep",
)  # fmt: skip
# Bramka natywnego frontu (claude-acc-hook): komenda idzie do Pythona tylko wtedy, gdy słowo
# stoi w niej jako całe słowo, a nie kawałek ścieżki albo innego słowa. Scheduler rozpoznaje
# program po nazwie tokenu (shlex, basename pierwszego słowa członu), a START widzi dev, serve
# i resztę tylko za białym znakiem, więc `2>/dev/null`, `export`, `main_test.go` czy
# `tsconfig.json` Pythona już nie budzą (6.10, doba komend: 16% do Pythona zamiast 45%, żadna
# z komend, przy których Python coś robi, nie odpadła). Ten sam wzorzec czyta `re` i ICU
# (NSRegularExpression w Swifcie), więc tylko klasy znaków, \w i lookaroundy. Program dodany
# do schedulera (NODE_TOOLS, go, make...) albo do START musi trafić i tutaj; pilnują tego testy.
GATE_PROGRAMS = (
    "go", "golangci-lint", "make", "govulncheck", "npx", "bunx", "pnpm", "yarn", "npm", "bun",
    "vitest", "jest", "playwright", "next", "tsc", "vue-tsc", "eslint", "turbo", "vite", "expo",
    "react-native", "xcodebuild", "pod", "pod-install", "eas", "eas-cli", "gradle", "gradlew",
    "portivo-mobile",
    # ciężka praca spoza Go i JS (sched.GENERIC_TOOLS); swift, docker, uv i interpretery mają
    # w HOOK_GATE własne kształty, bo jako samo słowo stoją w co trzeciej komendzie agenta
    "cargo", "pytest", "py.test", "tox", "nox", "mypy",
    "pyright", "deno", "bazel", "bazelisk", "mvn", "mvnw", "dotnet", "nx",
    "lerna", "webpack", "rollup", "parcel", "tsup", "astro", "nuxt", "nuxi", "svelte-check",
    "cypress", "mocha", "ava", "lighthouse", "unlighthouse", "storybook", "just",
    "task",
)  # fmt: skip
# natywne komendy, których nie poznać po samym programie: xcrun i open robią też wiele lekkich
# rzeczy, a do schedulera idzie tylko start symulatora
GATE_NATIVE = r"(?<![\w.-])simctl\s+boot|(?<![\w.-])-a\s+[\"']?Simulator(?![\w-])"
# Sekrety bramki pocztowej (klucz konta serwisowego Google, hasła IMAP) leżą w Pęku kluczy pod
# usługą claude-acc-mail. Agent korzysta z poczty przez narzędzia mail, a komendy, która je
# wyciąga (`security find-generic-password ... -w`, `dump-keychain`), strażnik nie przepuszcza.
# Tak samo z przeglądarkami: klucz "Chrome/Brave Safe Storage" odszyfrowuje ciasteczka i hasła,
# a pliki Cookies, Login Data i Web Data w profilu to sesje, hasła i karty. Agent wchodzi na
# strony z Twoimi loginami przez bramkę przeglądarki (browser.py), nigdy przez te pliki.
# Klucze API organizacji z kredytami (claude-acc-credits) dostaje tylko dziecko `credits exec`
# albo Claude Code przez apiKeyHelper (`credits helper`), który nie idzie przez narzędzie Bash.
SECRET_WORDS = ("claude-acc-mail", "claude-acc-browser", "claude-acc-credits", "credits helper", "google-service-account", "dump-keychain", "Safe Storage", "BraveSoftware", "Google/Chrome")
# przed programem na początku członu: zmienne (także w cudzysłowie) i programy-opakowania
COMMAND_PREFIX = (
    r"""(?:[A-Za-z_]\w*=(?:"[^"]*"|'[^']*'|\S*)\s+|(?:rtk\s+proxy|time|nice|env|caffeinate|command|exec|nohup)\s+)*"""
)
HOOK_GATE = (
    r"(?<![\w./-])(?:dev|serve)(?![\w.-])"
    r"|(?<![\w.-])(?:" + "|".join(p.replace(".", r"\.") for p in GATE_PROGRAMS) + r")(?![\w.-])"
    # interpretery tylko ze skryptem z pliku albo modułem: `python3 -c`, heredoc, `node -e` i
    # `--version` nie budzą Pythona (doba komend 08.10: samo słowo python, node i bash to 4400
    # z 35 tys. komend, a scheduler i tak by je puścił)
    r"|(?<![\w.-])python[0-9.]*(?![\w.-])[^;&|\n]*?(?:\.py(?![\w.-])|\s-m\s)"
    r"|(?<![\w.-])(?:node|tsx|ts-node)(?![\w.-])[^;&|\n]*?(?:\.[cm]?[jt]sx?(?![\w.-])|\s--test(?![\w-]))"
    r"|(?<![\w.-])(?:ba|z)?sh(?![\w.-])(?:\s+-\w+)*\s+['\"]?" + COMMAND_PREFIX + r"[~.\w-]*(?:/[\w.-]|\.sh(?![\w.-]))"
    r"|(?<![\w.-])docker(?![\w.-])[^;&|\n]*?\sbuild(?![\w.-])"
    r"|(?<![\w.-])uv\s+run(?![\w.-])"
    r"|(?<![\w.-])swift\s+(?:build|test|run)(?![\w.-])"
    # skrypt po ścieżce albo *.sh na początku członu (./scripts/e2e.sh, bin/verify, e2e.sh);
    # program systemowy po pełnej ścieżce (/usr/bin/git) nie
    r"|(?:^|[;&|(\n])\s*" + COMMAND_PREFIX + r"(?!/(?:usr|bin|sbin|opt/homebrew|System|Library|Applications)/)[~.\w-]*/[\w.-]"
    r"|(?:^|[;&|(\n])\s*" + COMMAND_PREFIX + r"[\w.-]+\.sh(?![\w.-])"
    r"|(?<![\w.-])git(?:\s+-[Cc]\s+\S+)*\s+grep(?![\w.-])"
    r"|" + GATE_NATIVE +
    r"|(?<![\w.-])(?:" + "|".join(SECRET_WORDS) + r")(?![\w.-])"
)
SECRET_DENY = (
    "Sekrety bramki pocztowej zostają w Pęku kluczy. Do poczty użyj narzędzi mail "
    "(skill `mail`, `claude-acc mail ...`); diagnoza: `claude-acc mail doctor`."
)
CREDITS_SECRET_DENY = (
    "Klucze API kredytów zostają w Pęku kluczy, a `credits helper` woła tylko Claude Code jako "
    "apiKeyHelper. Komendę, która ma płacić kredytem, uruchom przez `claude-acc credits exec "
    "--purpose <nazwa> -- <komenda>`; saldo: `claude-acc credits status`."
)
BROWSER_SECRET_DENY = (
    "Ciasteczka, hasła i karty z Chrome i Brave zostają w przeglądarce. Na strony z Twoimi "
    "loginami agent wchodzi bramką przeglądarki (skill `browser`, `claude-acc browser ...`)."
)


def secret_read(command):
    """Powód odmowy dla komendy, która wyciąga z Pęku kluczy sekret bramki pocztowej albo klucz
    API kredytów, albo ciasteczka, hasła i karty z profilu Chrome lub Brave; None dla każdej innej."""
    import re

    if re.search(r"(?<![\w-])dump-keychain(?![\w-])", command):
        return SECRET_DENY
    reads = r"find-(?:generic|internet)-password|export(?![\w-])|\s-[a-zA-Z]*[wg]\b"
    if re.search(r"(?<![\w.-])credits\s+helper(?![\w.-])", command):
        return CREDITS_SECRET_DENY
    if (
        re.search(r"(?<![\w-])security(?![\w-])", command)
        and re.search(r"(?<![\w-])claude-acc-credits(?![\w-])", command)
        and re.search(reads, command)
    ):
        return CREDITS_SECRET_DENY
    if (
        re.search(r"(?<![\w-])security(?![\w-])", command)
        and re.search(r"(?<![\w-])(?:claude-acc-mail|claude-acc-browser|google-service-account)(?![\w-])", command)
        and re.search(reads, command)
    ):
        return SECRET_DENY
    if re.search(r"(?:Chrome|Brave|Chromium)\s+Safe\s+Storage", command, re.IGNORECASE):
        return BROWSER_SECRET_DENY
    flat = command.replace("\\ ", " ")
    if re.search(r"Google/Chrome|BraveSoftware/Brave-Browser", flat) and re.search(
        r"(?<![\w-])(?:Cookies|Login Data(?: For Account)?|Web Data)(?![\w-])", flat
    ):
        return BROWSER_SECRET_DENY
    return None

# komenda stawiająca dev serwer, po zdjęciu opakowań (zmienne, rtk proxy, npx, pnpm exec) i
# ścieżki programu. Metro stawia `expo start`, `react-native start` i `expo run:<platforma>`
# (po buildzie, chyba że --no-bundler; Metro, które już serwuje tę aplikację, expo bierze zamiast
# nowego, więc dla run nie ma odmowy „drugi serwer”, zostaje tylko pamięć). Wzorce to tekst: `re`
# kompiluje je przy pierwszym użyciu, więc komenda bez słowa od dev serwera nie płaci ani za
# import `re`, ani za kompilację
METRO = r"(?:expo(?:@\S+)?\s+(?:start|(?P<run>run:\w+))|react-native(?:@\S+)?\s+start)"
START = (
    r"^(?:next\s+dev|vite(?:\s+(?:dev|serve))?(?=\s+-|\s*$)|(?:(?:pnpm|yarn|bun)\s+)?" + METRO +
    r"|webpack(?:-cli)?\s+serve|astro\s+dev|nuxi?\s+dev"
    r"|(?:pnpm|yarn)\s+(?:--filter|-F)[\s=](?P<pkg>\S+)\s+(?:run\s+)?dev(?::\S+)?"
    r"|(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev(?::\S+)?|turbo\s+(?:run\s+)?dev)(?=\s|$)"
)
WRAPPERS = (
    r"^(?:\w+=\S*\s+|(?:rtk\s+proxy|nohup|exec|time|env|caffeinate(?:\s+-\w+)*)\s+"
    r"|(?:npx|bunx|(?:pnpm|yarn|npm)(?:\s+(?:-C|--dir|--prefix|--cwd|--filter|-F)[\s=]\S+)*"
    r"\s+(?:exec|dlx))\s+(?:--\S+\s+)*)+"
)
FILTER_FLAG = r"\s(?:--filter|-F)[\s=](\S+)"
# skrypt CLI pod node (`node …/expo/bin/cli start`, `node /opt/homebrew/bin/pnpm dev`): takie
# komendy wznowienia strażnik sam pisze do logu, a agent je kopiuje
NODE_SCRIPT = r"^node\s+(?:-\S+\s+)*(?=\S*/)"
# pytanie o pomoc albo wersję niczego nie stawia
NOT_A_START = r"\s(?:--help|-h|--version)(?=\s|$)"
DIR_FLAG = r"\s(-C|--dir|--prefix|--cwd)[\s=](\S+)"
# komenda w cudzysłowie, którą uruchomi ktoś inny: `orca terminal create --command`, `sh -c`
NESTED = r"""(?:--command|\b(?:ba|z)?sh\s+-c)[\s=](?:"((?:[^"\\]|\\.)*)"|'([^']*)')"""


def sched_rewrite(event):
    """Komenda Go albo JS agenta owinięta w scheduler (sched.py obok tego pliku): wyjście hooka z
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


def program(path):
    """Nazwa programu z jego ścieżki: `./node_modules/.bin/expo` i `/opt/homebrew/bin/pnpm` to
    expo i pnpm, a skrypt CLI paczki (`…/expo/bin/cli`, `…/next/dist/bin/next`,
    `…/vite/bin/vite.js`, `…/react-native/cli.js`) to nazwa paczki."""
    parts = path.split("/")
    name = parts[-1]
    for ext in (".js", ".cjs", ".mjs"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    if len(parts) > 3 and parts[-2] == "bin":
        package = parts[-4] if parts[-3] == "dist" else parts[-3]
        if name == "cli" or package == name or parts[-3] == "dist":
            return package
    if name == "cli" and len(parts) > 1:
        return parts[-2]
    return name


def dev_starts(command, cwd):
    """[(katalog, filtr pakietu albo None, cały stos?, bierze działające Metro?)] dla każdej
    komendy stawiającej dev serwer. Ostatnie pole mówi, że komenda (`expo run:ios`) sama użyje
    Metro, które już serwuje tę aplikację, więc drugi serwer nie powstanie."""
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
        bare = re.sub(NODE_SCRIPT, "", bare)
        first, _, rest = bare.partition(" ")
        if "/" in first:
            bare = re.sub(WRAPPERS, "", program(first) + " " + rest).rstrip()
        match = re.match(START, bare)
        if not match or re.search(NOT_A_START, segment):
            continue
        run = match.group("run")
        if run and re.search(r"\s--no-bundler(?=\s|$)", segment):
            continue  # sam build: czeka w schedulerze, Metro nie stawia
        target = cwd
        flag = re.search(DIR_FLAG, " " + segment)
        if flag:
            target = os.path.normpath(
                os.path.join(cwd, os.path.expanduser(flag.group(2).strip("'\"")))
            )
        package = match.group("pkg")
        if not package and bare != segment:
            # `pnpm --filter mobile exec expo start`: filtr zdjęty razem z exec
            picked = re.search(FILTER_FLAG, " " + segment)
            package = picked.group(1) if picked else None
        # skrypt `dev` z package.json albo turbo: stawia to, co zdefiniował projekt
        stack = not package and bool(
            re.match(r"^(?:(?:pnpm|npm|yarn|bun)\s+(?:run\s+)?dev|turbo)(?:\s|:|$)", bare)
        )
        found.append((target, package, stack, bool(run)))
    return found


def command_text(raw):
    """Wartość "command" z surowego JSON-u zdarzenia, bez dekodowania (ucieczki zostają), bez
    importów; "" bez komendy. Słowo, którego nie ma w tym tekście, nie ma się skąd wziąć w
    komendzie, a cwd i ścieżka transkryptu (zawsze ze „/”) zostają poza nim."""
    i = raw.find('"command"')
    if i < 0:
        return ""
    j = raw.find('"', raw.find(":", i) + 1)
    if j < 0:
        return raw[i:]
    k = j + 1
    while True:
        k = raw.find('"', k)
        if k < 0:
            return raw[j + 1 :]
        slashes = 0
        while raw[k - 1 - slashes] == "\\":
            slashes += 1
        if slashes % 2 == 0:
            return raw[j + 1 : k]
        k += 1


def admit(raw):
    """Hook PreToolUse dla Basha; `raw` to zdarzenie z stdin.

    Idzie przy każdym poleceniu każdego agenta, więc kończy się jak najwcześniej. Słowa to
    minimum, które ma każda komenda pasująca do START (dev, vite, expo, webpack serve), i
    każda praca Go i JS dla schedulera. Najpierw szuka ich w surowym JSON-ie: kodowanie JSON
    zmienia tylko znaki z odwrotnym ukośnikiem, a żadne z tych słów go nie ma, więc słowo
    nieobecne w tekście zdarzenia (bez ucieczek \\u) nie ma się skąd wziąć w komendzie, a
    `json` z `re` w ogóle się nie ładują. Fałszywy alarm (słowo w ścieżce, w /dev/null) idzie
    dalej i odpada na wzorcach; do strażnika (devguard_core) trafia tylko komenda, która
    naprawdę stawia dev serwer.
    """
    text = command_text(raw)
    if "\\u" not in text and not any(w in text for w in DEV_WORDS + SCHED_WORDS + SECRET_WORDS):
        return 0
    import json

    try:
        event = json.loads(raw)
        command = (event.get("tool_input") or {}).get("command") or ""
    except (ValueError, AttributeError):
        return 0
    reason = event.get("tool_name") == "Bash" and any(w in command for w in SECRET_WORDS) and secret_read(command)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
        return 0
    if any(word in command for word in DEV_WORDS):
        if event.get("tool_name") != "Bash":
            return 0
        cwd = event.get("cwd") or os.getcwd()
        if "DEVGUARD_ALLOW=1" not in command and dev_starts(command, cwd):
            import io

            sys.stdin = io.StringIO(raw)
            return core().main(["admit"])
    elif not any(word in command for word in SCHED_WORDS):
        return 0
    # bez dev serwera komenda idzie do schedulera, bez strażnika; ta ze słowem od dev serwera
    # także bez słowa od schedulera, jak dotąd po pełnej ścieżce
    out = sched_rewrite(event)
    if out:
        print(json.dumps(out))
    return 0


# Codex (od 0.15x) ma hooki PreToolUse jak Claude Code, z dwiema różnicami: komenda powłoki
# przychodzi pod kilkoma nazwami narzędzia (matcher Bash obejmuje też exec_command), a
# updatedInput działa tylko razem z permissionDecision "allow". Codex z Orki biegnie i tak z
# --dangerously-bypass-approvals-and-sandbox, więc "allow" niczego tam nie zmienia; przy Codeksie
# z pytaniem o zgodę owinięta komenda przechodzi bez pytania (README, Scheduler i Codex).
CODEX_SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "container.exec", "unified_exec")


def admit_codex(raw):
    """`admit --codex`: zdarzenie Codeksa na kształt Claude Code, a wyjście z updatedInput
    dostaje permissionDecision "allow", bez którego Codex przepisania nie przyjmie."""
    import contextlib
    import io
    import json

    try:
        event = json.loads(raw)
        tool_input = event.get("tool_input") or {}
        command = tool_input.get("command", tool_input.get("cmd"))
    except (ValueError, AttributeError):
        return 0
    if event.get("tool_name") not in CODEX_SHELL_TOOLS or not command:
        return 0
    if isinstance(command, list):
        import shlex

        # ["bash", "-lc", "komenda"] albo argv
        command = command[2] if len(command) == 3 and command[1] in ("-c", "-lc") else shlex.join(command)
    event["tool_name"] = "Bash"
    event["tool_input"] = dict(tool_input, command=command)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        admit(json.dumps(event))
    out = buf.getvalue().strip()
    if not out:
        return 0
    data = json.loads(out)
    spec = data.get("hookSpecificOutput") or {}
    if "updatedInput" in spec and "permissionDecision" not in spec:
        spec["permissionDecision"] = "allow"
        spec["permissionDecisionReason"] = "claude-acc: memory scheduler"
    print(json.dumps(data))
    return 0


def core():
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import devguard_core

    return devguard_core


def main(argv):
    if argv[:1] == ["admit"]:
        try:
            raw = sys.stdin.read()
            return admit_codex(raw) if "--codex" in argv else admit(raw)
        except Exception:  # hook nigdy nie blokuje agenta przez własny błąd
            return 0
    if argv[:1] == ["admitd"]:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import admitd

        return admitd.serve()
    if argv[:1] == ["words"]:
        # jedno źródło list dla obu frontów: setup.sh zapisuje to do hook-words.json; bramka
        # jako lista, bo starszy claude-acc-hook czyta plik jako {klucz: [tekst]}
        import json

        print(
            json.dumps(
                {"dev": list(DEV_WORDS), "sched": list(SCHED_WORDS), "gate": [HOOK_GATE]}
            )
        )
        return 0
    return core().main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
