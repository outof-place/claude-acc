# Wydajność Maca: co zmierzone, co działa, co nie

Badanie z 2026-10-04 na MacBooku Pro 16" M4 Max (Mac16,5, 12 rdzeni P + 4 E, 48 GB RAM),
macOS 27.0 (26A428). Miejsce: TKB, kabel USB LAN (RTL8153) do Archera BE230. W tle przez
cały czas pracowało kilka sesji Claude Code w Orce, dev serwery, maszyna Dockera i rendery
Blendera innego agenta, więc każdą liczbę podaję jako zakres z kilku przebiegów.

Najpierw praca agentów (Orca, Claude Code, dev serwery, Docker) i przełącznik Ultra, potem
ogólne pomiary sieci, CPU i GPU, ślepe zaułki i rekomendacje.

Wcześniej zrobione i tu nie powtarzane: unbound z blocklistą, strażnik AWDL, watchdog Wi-Fi,
browser-turbo, prywatność Spotlight, Reduce transparency/motion, High Power na zasilaczu,
HTTP/2 w kliencie Bun. Strojenie sysctl TCP, dummynet, LEDBAT, pacing i QoS na BE230 to
znane ślepe zaułki z poprzednich sesji.

## Jak mierzę

Wszystko z `perf.py bench`, żeby dało się powtórzyć:

- **Sieć:** `networkQuality -s -c` (najpierw pobieranie, potem wysyłanie), ping do bramy i
  1.1.1.1, czasy curl do `api.anthropic.com`, a na początku 3 s `nettop`. Ten ostatni
  pokazuje, kto w tle zajmuje łącze.
  - Liczba „responsiveness” z networkQuality miesza dwie rzeczy. Pierwsza to opóźnienie w
    sieci, mierzone na osobnych połączeniach (`lud_foreign_*`). Druga to kolejka w buforach
    gniazd TCP na samym obciążonym połączeniu (`lud_self_*`). `perf.py` raportuje je osobno,
    bo leczy się je czym innym.
  - Do porównań A/B przypinałem serwer plikiem konfiguracji (`-C file://...` z
    `test_endpoint`). Bez tego `-f L4S` i domyślny tryb trafiają na różne serwery, Mediolan
    i Berlin, i różnica w pomiarze bierze się z innej trasy.
- **CPU:** 1 GB przez SHA-256 w jednym procesie i w 16 naraz.
  - Z `proc_pid_rusage` (`rusage_info_v6`, bez roota) biorę udział czasu na rdzeniach P
    (`ri_user_ptime`), czekanie w kolejce (`ri_runnable_time` minus czas pracy) i energię
    (`ri_energy_nj`).
  - Dodatkowo spóźnienie wybudzenia wątku po `sleep(1 ms)` i 10 s spisu własnych procesów,
    które palą rdzenie P i energię.
- **GPU:**
  - Czas GPU każdego klienta Metalu: `accumulatedGPUTime` z `AGXDeviceUserClient` w ioreg,
    to samo źródło co kolumna GPU w Monitorze aktywności.
  - Obciążenie urządzenia i CPU WindowServera.
  - Sonda Metalu przez ctypes: 200 malutkich zleceń blit, czas tam i z powrotem. Tyle samo
    czeka klatka WindowServera, gdy GPU mieli coś w tle.
- **System plików** (`bench fs`): lstat dużego drzewa node_modules dwa razy pod rząd i
  liczba vnode, które jądro przy tym odzyskało (`kern.num_recycledvnodes`).
- **Hooki Claude Code:** z prawdziwych transkryptów (`~/.claude/projects/*/*.jsonl`), gdzie
  każdy hook ma czas trwania; `perf.py` liczy z nich czas czekania sesji na zdarzenie.

## Praca agentów: Orca, Claude Code, dev serwery, Docker

Ta część jest ważniejsza od ogólnych pomiarów niżej. Na tym Macu praca wygląda tak:

- 9 sesji Claude Code w terminalach Orki;
- każda sesja ma swoje serwery MCP i hooki;
- agenci stawiają dev serwery Next.js i odpalają rg, git, tsc i eslint w monorepo portivo (14 tys. plików, 9 worktree);
- obok działa Docker z 21 kontenerami.

Wszystko mierzone przy działającej Orce i sesjach (2026-10-04, wieczór).

### Limity systemu kontra realne zużycie

| limit | wartość | zużycie teraz | wniosek |
|---|---|---|---|
| `kern.maxfiles` / `maxfilesperproc` | 368640 / 184320 | `kern.num_files` 17352 (5%) | zapas |
| `launchctl limit maxfiles` | 256 miękki, bez twardego | powłoki agentów w Orce mają `ulimit -n` 1048576 (Orca podnosi) | nie ruszać |
| `kern.maxproc` / `maxprocperuid` | 12000 / 8000 | 919 procesów, 661 moich | zapas |
| `kern.tty.ptmx_max` (PTY) | 511 | 41 `/dev/ttys*` | zapas |
| `kern.ipc.somaxconn` | 128 | dev serwery i HMR nie zbliżają się | nie ruszać |
| `kern.maxvnodes` | 263168 | **pełne (263168), 28 mln odzysków w 5 h, 250-1500/s** | za mało, patrz niżej |

Limity plików, procesów i PTY mają kilkunastokrotny zapas, więc ich podnoszenie niczego
nie przyspieszy. Wąskim gardłem jest za to cache vnode.

**Cache vnode.** Jedno drzewo `node_modules/.pnpm` portivo ma 358 tys. wpisów, a cache vnode mieści 263 tys.:

- lstat całego drzewa trwa 7,8 s za pierwszym razem i 3,6 s przy każdym kolejnym;
- w każdym przebiegu jądro odzyskuje około 250 tys. vnode, więc drzewo nigdy nie zostaje w cache;
- 9 worktree portivo ma osobne kopie node_modules (APFS clone, osobne inody), więc zbiór
  plików, po którym chodzą tsc, eslint, Turbopack i `git status -uall`, jest kilka razy większy niż cache.

`kern.maxvnodes` zmienia tylko root. Jeden vnode kosztuje około 1,1 KB pamięci jądra (vnode 264 B, inode APFS 424 B, namecache 104 B, ubc_info 96 B, obiekt VM),
więc 786432 to około +0,6 GB.

`sudo ./perf-root.sh vnodes trial` mierzy to samo przed i po zmianie i cofa ją
(`--keep` zostawia). Ten test **nie był jeszcze uruchomiony**, bo wymaga sudo.

### Hooki Claude Code: koszt każdego wywołania narzędzia

Hooki zmierzyłem na prawdziwych transkryptach, nie na syntetycznym teście. Każdy hook
ma tam wpis `hook_success` z `durationMs`. Wziąłem 24 godziny, czyli 12 tys. wywołań
narzędzi w 20 sesjach. Hooki jednego zdarzenia działają równolegle, więc sesja czeka na
najdłuższy z nich.

| zdarzenie | hook | p50 | p90 | p99 |
|---|---|---|---|---|
| PostToolUse (każde narzędzie) | cavemem `post-tool-use` (node) | 54 ms | 99 ms | 382 ms |
| PostToolUse | hook Orki (sh + curl) | 26 ms | 71 ms | 264 ms |
| Stop (koniec tury) | cavemem `stop` | 73 ms | 164 ms | 921 ms |
| Stop | hook Orki | 45 ms | 111 ms | 823 ms |
| PreToolUse | hook Orki | 35 ms | 88 ms | 605 ms |
| SessionStart | cavemem `session-start` | 147 ms | 223 ms | 400 ms |
| PostToolUse Write/Edit | `ts-typecheck.sh` | 787 ms | 1805 ms | 5271 ms |

Z tej tabeli wynikają trzy rzeczy:

- **cavemem jest najdłuższym hookiem przy każdym narzędziu.** Sam cavemem raportuje 18-19
  ms pracy, reszta to start node i ładowanie modułów. Wynik hooka nie jest sesji do
  niczego potrzebny, więc może iść w tle: `"async": true` (opcja z dokumentacji hooków,
  obecna w binarce 2.1.289).
  - Wtedy PostToolUse czeka tylko na hook Orki: około 54 -> 26 ms p50 i 100 -> 71 ms p90 na
    każde narzędzie.
  - Przy 12 tys. wywołań dziennie to około 6 minut czekania agentów mniej na dobę, a na
    końcu tury Stop szybciej o kolejne ~30 ms.
  - `UserPromptSubmit` i `SessionStart` zostają synchroniczne, bo wstrzykują pamięć do
    kontekstu.
- **`devguard.py admit`** (PreToolUse dla Bash) trwa 44 ms p50 w syntetycznym pomiarze i
  przy Bashu jest najdłuższym hookiem. rtk-enforce trwa 16 ms, a hook Orki bez Orki 8 ms.
  - Sam start `/usr/bin/python3` to 19 ms. Import `ctypes.util` to 7 ms, `janitor` 3 ms,
    kompilacja regexów 5 ms.
  - Szybka ścieżka może wychodzić przed importami, gdy komenda nie zawiera słów od dev
    serwera, i oszczędzać około 20 ms na każdym Bashu. To jednak plik głównej sesji
    (devguard.py), więc go nie ruszam.
- **`ts-typecheck.sh`** po każdym Write/Edit pliku TS trwa medianowo 0,8 s, a w ogonie 5 s.
  Na wynik tego hooka sesja naprawdę czeka (sprawdza typy), więc nie przenoszę go w tło.

### Serwery MCP i procesy na sesję

| na każdą sesję (8-9 kopii) | footprint jednej kopii | razem |
|---|---|---|
| cavemem `mcp` (node) | 29-101 MB | 405 MB |
| MCP Magic `mcp-stdio-server.cjs` (node) | 41 MB | 327 MB |
| e2e portivo `bin.js mcp` (node, z `.mcp.json` projektu) | 43 MB | 344 MB |
| hunspell (sprawdzanie pisowni z ustawień Claude) | 27 MB | 218 MB |
| chrome-devtools-mcp (w jednej sesji, razem z Chrome) | 990 MB | 990 MB |

Dzieci 9 sesji zajmują razem 2,3 GB, a same procesy `claude` 0,2-0,7 GB każdy.

- Serwery stdio startują osobno w każdej sesji. Dokumentacja nie przewiduje dzielenia
  jednego serwera stdio między sesje; dałoby się to zrobić tylko przez serwer HTTP, gdyby
  te serwery go miały.
- Te kopie to funkcje, z których korzystasz, więc Ultra ich nie wyłącza.

### Sieć do API Claude

- **DNS:** unbound, 0,2 ms przy trafieniu w cache.
- **Nowe połączenie po kablu:** TCP 10-12 ms, TLS 20-24 ms, pierwszy bajt 30-36 ms.
- **Połączenia są używane ponownie:** każda sesja trzyma 1-3 stałe połączenia TLS (do
  160.79.104.10 i dwóch innych hostów), więc koszt nowego połączenia płaci raz.
  - Jedna sesja trzyma połączenie wychodzące z adresu Wi-Fi (192.168.0.235), bo powstało,
    zanim kabel był pierwszy. To kosztuje około 1,5 ms więcej na pakiet, aż połączenie się
    odnowi.
- **IPv6:** `api.anthropic.com` ma też adres IPv6 (AAAA), ale przy BE230 Mac ma tylko adres
  link-local, więc zostaje IPv4 bez opóźnień Happy Eyeballs.
- **Zmienne środowiskowe:** wszystkie 9 sesji mają już `DISABLE_TELEMETRY`,
  `DISABLE_ERROR_REPORTING` i flagę HTTP/2 Buna (sprawdzone w środowisku działających
  procesów).

Wniosek: nie ma tu nic do poprawienia.

### Orca

Tryb App Nap (usypianie aplikacji w tle) Orki nie dotyczy:

- **Priorytet się nie zmienia:** przez 10 minut z Brave na pierwszym planie główny proces
  Orki miał priorytet 46, a host terminali 31. Ani razu nie spadły do 4.
- **Wybudzenia też:** główny proces miał 24-31 wybudzeń/s, host terminali 33-40/s, tyle samo
  co przy Terminalu na pierwszym planie.
- **Wniosek:** `NSAppSleepDisabled` dla `com.stablyai.orca` niczego by tu nie zmieniło.

Orca sama sporo kosztuje:

- **Lista procesów:** `ps -axo pid,ppid,pgid,tpgid,stat,tty,lstart,command` odpala 41 razy
  na 30 s, po około 70 ms (60 ms w jądrze). To mniej więcej 10% jednego rdzenia
  przez cały czas.
- **Ustawienia:** jedyne związane z wydajnością to `terminalGpuAcceleration: on` i
  `windowBackgroundBlur: false`; jest dobrze. Pokrętła do częstotliwości odpytywania nie
  ma, więc to temat dla twórców Orki.
- **Dysk:** `~/Library/Application Support/orca` ma 5,2 GB, w tym 3,4 GB `Partitions`
  (podglądy przeglądarki). To miejsce na dysku, nie wydajność.
- **CLI z devguard:** strażnik woła `orca` 19 razy na minutę. Jedno wywołanie trwa 70-130 ms,
  w tym 60 ms CPU, a każde budzi też główny proces Orki.

### Git w worktree

Orca prowadzi worktree jednego repozytorium: portivo (`~/Documents/portivo-app/Untitled`,
14 025 plików, pack 627 MB, git 2.54).

- **Prawdziwe repo:** `git status --porcelain` trwa 72-87 ms (p50 76), czytane bez blokad
  (`GIT_OPTIONAL_LOCKS=0`).
- **Ustawienia sprawdziłem na klonie** (`git clone --local`, obiekty jako twarde dowiązania),
  bo konfigurację portivo zmienia tylko jego właściciel:

| ustawienie | git status p50 |
|---|---|
| nic (index v2) | 71 ms |
| `core.untrackedCache=true` | 31 ms |
| + `feature.manyFiles` (index v4, skipHash) | 34 ms, bez zysku |
| + `core.fsmonitor=true` | 26 ms |

- **Ultra** ma do tego poprawkę `git-speed` (untrackedCache + fsmonitor, z dokładnym
  cofnięciem), ale działa tylko na repozytoriach wpisanych do `git_repos` w
  `~/.local/share/claude-acc/perf.json`. Lista jest domyślnie pusta; `"git_repos": "orca"`
  bierze wszystkie repozytoria z worktree Orki.
- **Dla portivo** można to włączyć wpisem albo ręcznie:
  `git -C ~/Documents/portivo-app/Untitled config core.untrackedCache true && git -C
  ~/Documents/portivo-app/Untitled config core.fsmonitor true && git -C
  ~/Documents/portivo-app/Untitled update-index --untracked-cache`.
  - Cofnięcie: `config --unset` obu kluczy, `fsmonitor--daemon stop` i `update-index
    --no-untracked-cache`.
  - fsmonitor stawia jeden mały demon na każdy worktree, w którym ktoś woła gita.
- **`feature.manyFiles` i `index.skipHash` pomijam:** nic nie dały, a skipHash potrafi
  zmylić narzędzia na libgit2.

### Node i reszta pętli dev

- **`NODE_COMPILE_CACHE`** (V8 code cache, Node 22.1+, tu Node 24.12). Pomiar czasu
  `require`, mediana z 7 przebiegów:

  | co wczytuje node | bez cache | z cache |
  |---|---|---|
  | typescript | 87 ms | 40 ms |
  | eslint | 77 ms | 67 ms |
  | next/dist/server | 48 ms | 48 ms |
  | moduły cavemem | 28 ms | 28 ms |

  - Zysk widać przy tsc, eslint z typescript-eslint, vitest i ts-node. Cache po pomiarze
    ma 3,4 MB.
  - Działa przez `env` w `~/.claude/settings.json`, więc obejmuje wszystko, co uruchamiają
    sesje Claude: polecenia Bash, serwery MCP i hooki.
  - Sam `claude` to binarka Buna i z tego nie korzysta. `claude --version` i tak trwa 10 ms.
- **pnpm:**
  - Magazyn leży w `~/Library/pnpm/store/v11` (14 GB) na tym samym wolumenie APFS co
    projekty. `package-import-method` jest domyślny, więc pakiety trafiają do projektów
    przez clone/hardlink bez kopiowania. Nie ma tu nic do zmiany.
  - **Spotlight indeksuje ten magazyn (468 066 plików)**, a obok też `~/go` (41 759 plików,
    moduły Go).
  - Wykluczenie przez ustawienia jest tylko w GUI: Ustawienia > Spotlight > Prywatność
    wyszukiwania. Przeniesienie magazynu do katalogu z końcówką `.noindex` zmieniłoby
    ścieżkę magazynu zapisaną w `node_modules/.modules.yaml` każdego projektu i pnpm
    zażądałby reinstalacji, więc tego nie robię.
- **Go:** `GOCACHE` to `~/Library/Caches/go-build` (Spotlight go nie indeksuje), a
  `GOFLAGS=-p=4` ogranicza równoległość buildów (ktoś ustawił to celowo). Nie ruszam.
- **Time Machine:** nie ma skonfigurowanego dysku (`tmutil destinationinfo`), więc
  wykluczenia nic nie dadzą.
- **Indeksery:** mdworker, suggestd, bird, siriknowledged i backupd już działają z
  priorytetem 4 (QoS tła). Spotlight (mds_stores) to 26 minut CPU w 5 h, ale należy do
  roota; ulżyć mu mogą tylko wykluczenia.

### Docker

| pomiar | wynik |
|---|---|
| przydział VM (domyślny) | 8 GB, 16 CPU, swap domyślny; containerd (overlayfs), Docker Desktop 4.90 |
| footprint VM na Macu | 8,0 GB; 21 kontenerów używa 3,7 GB (największe: otel-lgtm 1 GB, supabase analytics 0,7 GB) |
| start kontenera (`docker run --rm`, obraz lokalny) | 241-310 ms |
| 3000 małych plików, zapis | 0,61 s na montowaniu z Maca (VirtioFS), 0,05 s na dysku kontenera |
| zapis 256 MB | 0,34 s VirtioFS, 0,81 s dysk kontenera |
| `find` po 310 tys. plików node_modules | 19,6 s przez VirtioFS, 2,7 s na Macu |
| dysk | obrazy 17 GB (4,2 GB do odzyskania), cache buildów 566 MB |

- **VirtioFS:** przy operacjach na metadanych jest 7-12 razy wolniejszy niż natywnie.
  Kontenery portivo i supabase to jednak usługi z wolumenami, bez montowanego kodu, więc
  tu nie płacisz za to nic.
- **Odpytywanie z dashboardu:** Docker Desktop z otwartym oknem odpala `docker stats --all
  --no-stream` 18 razy na minutę. Zamknięcie okna (nie aplikacji) to wyłącza.
- **Limit pamięci VM:** tylko zalecenie, poza Ultrą. `perf.py apply docker-vm` zapisuje
  `MemoryMiB` = 6144 do `settings-store.json`, ale wyłącznie wtedy, gdy Docker nie działa,
  bo działający Docker trzyma ustawienia w pamięci i nadpisuje plik. Nowa wartość działa
  od następnego startu Dockera. Nikt go nie restartuje.
- **Docker VMM** (od 4.86) według dokumentacji oddaje wolną pamięć hostowi. Przejście
  wymaga restartu i nie obsługuje Rosetty; nie mierzyłem, zostawiam to do decyzji.

## Ultra: jeden przełącznik

`perf.py ultra on|off|status [--json]` włącza naraz poprawki dla pracy agentów i zapisuje
w `perf-state.json`, co było przed nimi. `off` przywraca dokładnie poprzednie wartości.

- **Sprawdzone na żywym systemie:** po `ultra off` pliki `~/.claude/settings.json` i
  `devguard.json` były bajt w bajt takie jak przed `on`. Testy w osobnym `$HOME` sprawdzają
  to samo: `on`, drugi raz `on` bez żadnej zmiany w plikach, `off`.
- **Cudze zmiany zostają:** gdy ktoś zmieni wartość po włączeniu Ultry, `off` jej nie
  rusza.
- **Ręcznie włączone poprawki zostają:** to, co włączono przed Ultrą przez `perf.py apply`,
  Ultra omija i `off` tego nie cofa.
- **Drugie `on` niczego nie psuje** i nie mierzy „przed” od nowa, więc może iść przy
  logowaniu.
- **Pilnowanie:** po restarcie wystarczy `perf.py keep` (RunAtLoad i co 5 minut). Nakłada
  QoS tła na nowy pid workera, przywraca wpisy, gdy ktoś nadpisze settings.json, i
  uzupełnia wyniki, które przychodzą później.
- **Zmiany składu:** składnik usunięty z Ultry, jak `docker-vm` z pierwszej wersji, następne
  `ultra on` cofa sam.

### Co Ultra włącza i co dało

| poprawka | grupa | co zmienia | zmierzone przed -> po |
|---|---|---|---|
| `bg-helpers` | cpu | `PRIO_DARWIN_BG` dla cavemem worker | rdzeń P 32,1% -> 0,1%, energia ~1 W -> 0,1 W |
| `claude-hooks-async` | claude | `"async": true` dla cavemem `post-tool-use` i `stop` w `~/.claude/settings.json` | sesja czeka na hooki PostToolUse: **53 -> 24 ms p50, 73 -> 30 ms p90** na każde narzędzie (107 wywołań po włączeniu kontra 3304 w dobie przed); Stop 70 -> 16 ms p50 (tylko 2 tury po) |
| `node-compile-cache` | claude | `env.NODE_COMPILE_CACHE` w `~/.claude/settings.json` | `require('typescript')` 96 -> 49 ms |
| `devguard-budget` | dev | `budget_percent` 25 w `devguard.json` | 35% RAM (16,8 GB) -> 25% (12 GB); polityka |
| `devguard-max-server` | dev | `max_server_gb` 4 w `devguard.json` | 5 -> 4 GB na serwer; polityka (Turbopack dobija do 7-9 GB) |
| `git-speed` | dev | `core.untrackedCache` + `core.fsmonitor` w repozytoriach z `git_repos` | klon portivo 71 -> 26 ms; domyślnie pusta lista |
| `fast-npx-hooks` | claude | hooki formatowania owinięte `npx-fast-wrap.sh` (faza 3) | hook na plikach portivo 3,3-6,6 s -> 39-82 ms; edycja pliku formatowanego 3,25 s p50 przed |
| `claude-limits` | claude | `env.BASH_MAX_TIMEOUT_MS` 3600000 (faza 3) | ~20 poleceń na dobę ściętych do 10 min wbrew timeoutowi agenta przed |

Hooki zmieniają się na żywo, bez restartu sesji. Po włączeniu wpisy cavemem PostToolUse
zniknęły z transkryptów także sesji otwartych wcześniej (było 2389 na godzinę, przyszło 6).
Z kolei PreToolUse, którego zmiana nie dotyczy, przesunęło się w tym samym oknie o 4 ms
(29 -> 25 ms p50), więc tyle spadku to luźniejsza pora, a reszta (~25 ms na każde
narzędzie) to hook cavemem.

**Czeka na roota** (`pending_root`, robi to perf-root.sh):

- `vnodes`: `kern.maxvnodes` 786432, przez `sudo ./perf-root.sh vnodes trial`.
- `shaper`: ogranicznik wysyłania. Pojawia się tylko wtedy, gdy ostatni pomiar sieci
  pokazał, że łącze puchnie przy wysyłaniu (więcej niż 50 ms ponad opóźnienie bez
  obciążenia).

**Czeka na człowieka** (`pending_manual`): `spotlight-privacy`. Spotlight trzyma 509 825
plików z `~/Library/pnpm` i `~/go`, a wykluczyć je można tylko w Ustawieniach. Gdy liczba
spadnie, wynik sam dostanie „po”; sprawdzanie najwyżej raz na 10 minut, bo mdfind trwa
około sekundy.

**Tylko na życzenie, poza Ultrą** (`perf.py apply ...`):

- `docker-vm`: `MemoryMiB` 6144, zapis tylko przy zamkniętym Dockerze, działa od jego
  następnego startu. Dockera nikt nie restartuje.
- `git-speed` dla portivo: `"git_repos": "orca"` albo `["~/Documents/portivo-app/Untitled"]`
  w `perf.json`. Lista jest domyślnie pusta, bo konfigurację portivo zmienia tylko jego
  właściciel.

### Kształt JSON dla panelu

`perf-state.json` pod kluczem `ultra`, także `perf.py ultra status --json`. Na żywo, 2026-10-04:

```json
{
  "on": true,
  "since": 1791137059.26,
  "applied": ["bg-helpers", "claude-hooks-async", "node-compile-cache", "devguard-budget", "git-speed", "devguard-max-server"],
  "pending_root": ["vnodes"],
  "pending_manual": ["spotlight-privacy"],
  "results": {
    "bg-helpers": {"before": 32.1, "after": 0.1, "unit": "% rdzenia P"},
    "claude-hooks-async": {"before": 53.0, "after": 24.0, "unit": "ms hooków na narzędzie (p50)"},
    "node-compile-cache": {"before": 96.0, "after": 49.0, "unit": "ms require('typescript')"},
    "devguard-budget": {"before": 35, "after": 25, "unit": "% RAM na dev serwery"},
    "devguard-max-server": {"before": 5, "after": 4, "unit": "GB na jeden dev serwer"},
    "spotlight-privacy": {"before": 509825, "after": null, "unit": "plików w indeksie Spotlight", "note": "dodaj w Ustawienia > Spotlight > Prywatność: ~/Library/pnpm, ~/go"}
  }
}
```

- **Klucze:** `on`, `since`, `applied`, `pending_root` i `results` (z `before`, `after`,
  `unit` i opcjonalnym `note`) są takie, jak ustaliliśmy. Doszła lista `pending_manual`:
  `spotlight-privacy`, a przy ręcznie włączonym `docker-vm` także `docker-quit` i
  `docker-restart`.
- **`after: null`:** pomiar „po” jeszcze nie przyszedł.
- **Wynik bez wpisu w `applied`:** `spotlight-privacy` dotyczy kroku do kliknięcia, nie
  poprawki.

### Sprawdzone i niewłączone

| kandydat | pomiar | decyzja |
|---|---|---|
| inne pomocniki systemu w bg-helpers | 60 s: photoanalysisd, mediaanalysisd, cloudd, fileproviderd 0,00-0,04% CPU; corespotlightd 0,17%; mdworker, bird, suggestd, siriknowledged, duetexpertd już z priorytetem 4; mds_stores należy do `_mds_stores` (bez roota setpriority odmawia) | nie: nie ma czego zabrać |
| Passwords w menu bar (`com.apple.Passwords.MenuBarExtra`) | 3,7% CPU, 1,4% rdzenia P, 50 mW bez przerwy | nie w tło (to UI, menu otwierałoby się wolniej); lepiej wyłączyć ikonę w ustawieniach aplikacji Hasła |
| limity (`maxfiles`, `maxproc`, `ptmx_max`) | 5-8% zużycia; agenci mają `ulimit -n` 1048576 | nie: zapas jest |
| `feature.manyFiles`, `index.skipHash` | klon portivo: 34 ms kontra 31 ms z samym untrackedCache | nie: bez zysku, skipHash myli narzędzia na libgit2 |
| `git maintenance register` | portivo ma już łańcuch commit-graph z automatycznego gc; na klonie commit-graph z filtrami ścieżek dał `git log -- plik` 31 -> 15 ms, `git status` i całą historię bez zmian | nie domyślnie; opcja dla portivo: `git -C <repo> maintenance register` (cofnięcie: `maintenance unregister`) |
| wykluczenia Time Machine (`tmutil addexclusion`) | `tmutil destinationinfo`: brak dysku | nie: nic nie da, dopóki nie ma kopii |
| Spotlight na worktree, node_modules, .next | worktree portivo i projekty: 0 plików w indeksie (prywatność już ustawiona, `.next` to katalog z kropką) | zostają tylko pnpm i go (patrz wyżej) |
| App Nap i timery Orki | priorytet 46 i 24-31 wybudzeń/s także pod spodem | nie: nie ma czego wyłączać |
| `NODE_COMPILE_CACHE` dla samego `claude` | `claude --version` 10 ms; to binarka Buna | nie dotyczy; cache włączony dla tego, co sesje uruchamiają |
| Docker VM w Ultra | 8 GB VM, 3,7 GB kontenerów | tylko zalecenie (`perf.py apply docker-vm`) |

Gdzie ustawić `NODE_COMPILE_CACHE` dla terminali Orki:

- **`launchctl setenv`:** działa tylko dla aplikacji uruchomionych później. Działająca Orca
  i jej terminale nie dostaną zmiennej do restartu Orki, a ustawienie znika przy restarcie
  Maca.
- **Plik startowy powłoki (`~/.zshrc`):** zmienia twój dotfile, działa w każdej powłoce,
  także poza agentami, a cofnięcie to edycja pliku.
- **`env` w `~/.claude/settings.json` (wybrane):** jeden klucz z zapisaną poprzednią
  wartością. Obejmuje dokładnie to, co uruchamiają sesje Claude: Bash, serwery MCP i hooki.

### Zmienne Claude Code (sprawdzone w dokumentacji, Context7 `/websites/code_claude`)

| zmienna | co robi według dokumentacji | decyzja |
|---|---|---|
| `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING` | wyłączają metryki i raporty błędów; `DISABLE_TELEMETRY` wyłącza też pobieranie flag funkcji, przez co Remote Control może być niedostępny | już ustawione we wszystkich 9 sesjach (`~/.zshrc`); jeśli używasz Remote Control, to jest jego możliwa przyczyna |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | wyłącza autoaktualizacje, telemetrię, raporty, `/feedback`, informacje o wydaniach, odznaki statusu PR, sprawdzanie dostępności, pobieranie flag funkcji (Remote Control) i działanie wtyczek `command` w tle; `0` też wyłącza | opt-in, nie w Ultra: wyłącza autoaktualizacje i funkcje |
| `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY` | wyłącza ankiety jakości sesji | opt-in; narzutu nie da się zmierzyć |
| `skipWebFetchPreflight` (ustawienie) | pomija sprawdzanie bezpieczeństwa domeny przy WebFetch | nie: to zabezpieczenie |
| `USE_BUILTIN_RIPGREP=0` | używa systemowego rg zamiast dołączonego | nie: dokumentacja podaje to jako obejście zgodności |
| `async: true` w hooku | „hook runs in background without blocking” (schemat w binarce 2.1.289) | tak, dla cavemem PostToolUse i Stop |

## Faza 3: obrót agentów i limity Claude Code

Cel użytkownika: agenci mają mieć do dyspozycji całą wydajność Maca (szybkie odpowiedzi,
czytanie plików, edycje, testy Go), a Claude Code ma działać bez sztucznych sufitów.

Wszystko zmierzone 2026-10-04 wieczorem, po stanie z main c75f6d8. Na Macu działały już
wtedy:
- Spotlight tylko dla aplikacji,
- `kern.maxvnodes` 786432,
- git-speed dla portivo,
- Docker z 6 GB pamięci,
- wyłączone Hasła w menu bar,
- szybka ścieżka hooka admit.

### Skąd liczby

Obrót narzędzia liczę od wpisu asystenta z `tool_use` do wpisu z `tool_result` w
transkryptach Claude Code, łącznie z subagentami (`~/.claude/projects/**/*.jsonl`). To
czas, który sesja naprawdę czeka: wykonanie narzędzia razem z hookami Pre i Post.

`perf.py bench agents` liczy to samo dla ostatniej doby i zapisuje w `perf-state.json`.

Ostatnia doba, 109 transkryptów (bez tej sesji badawczej):

| narzędzie | wywołań | p50 | p90 | suma |
|---|---|---|---|---|
| Bash: rg/grep/find | 4867 | 238 ms | 722 ms | 261 min |
| Bash: go test | 280 | 10,6 s | 68 s | 126 min |
| Bash: pnpm/npm/npx | 722 | 2,1 s | 19,7 s | 111 min |
| Bash: node | 442 | 2,1 s | 28 s | 111 min |
| Bash: pliki (ls/cat/sed) | 2460 | 215 ms | 787 ms | 75 min |
| Bash: git | 1811 | 290 ms | 1,6 s | 46 min |
| Edit pliku formatowanego (.ts/.tsx/.md/.json/.yml/.css/.mjs) | 362 | **3,25 s** | **9,4 s** | |
| Write pliku formatowanego | 179 | 805 ms | 7,0 s | |
| Edit/Write innych plików (.go/.py/.sh/.sql) | 1372 | 102 ms | 160 ms | |
| Bash: go build/vet/run | 69 | 10,5 s | 62 s | 26 min |
| Read | 1522 | 93 ms | 268 ms | 9 min |
| Bash: make | 10 | 87 s | 90 s | 9 min |
| Bash: golangci-lint | 16 | 679 ms | 27 s | 10 min |

Edycje według rozszerzenia (p50 / p90):

| rozszerzenie | p50 | p90 |
|---|---|---|
| .ts | 5,4 s | 11,8 s |
| .tsx | 7,0 s | 20,1 s |
| .json | 3,5 s | 5,2 s |
| .yml | 4,3 s | 7,1 s |
| .css | 3,7 s | 4,5 s |
| .mjs | 2,9 s | 9,1 s |
| .md | 537 ms | 4,6 s |
| .go, .sh, .py, .sql | 100-126 ms | 158-198 ms |

### Trzy największe wąskie gardła

**1. Hooki formatowania wołały `npx`, który w monorepo pnpm szuka narzędzia sekundami.
Naprawione.**

- `~/.claude/hooks/auto-format.sh` po każdym Write/Edit woła po kolei `npx --no-install
  eslint --fix` i `npx --no-install prettier --write`. `ts-typecheck.sh` woła `npx
  --no-install tsc`.
- W portivo eslinta i prettiera nie ma w żadnym `node_modules/.bin`. Mimo to npx czyta
  najpierw całe drzewo modułów i dopiero potem się poddaje:
  - w `apps/landing-page` trwa to 3,3-8,8 s (kod 127);
  - w katalogu bez node_modules trwa 0,25 s.
- Edycja .ts płaciła to dwa razy z rzędu, za nic: obie komendy kończą się błędem i plik
  zostaje, jaki był.

Poprawka `fast-npx-hooks` nie rusza skryptów hooków użytkownika:

- **Owinięcie:** komenda hooka w `~/.claude/settings.json` dostaje przed sobą
  `npx-fast-wrap.sh`, który kładzie na początek PATH atrapę `npx` (`hooks/npx-fast/npx`).
- **Atrapa:** `npx --no-install [--quiet] <narzędzie>` szuka narzędzia w
  `node_modules/.bin` od bieżącego katalogu w górę. Znalezione uruchamia od razu, a brak
  kończy kodem 127, jak npx. Każde inne wywołanie npx przekazuje prawdziwemu npx.
- **Cofnięcie:** oryginalna komenda jest zapisana w `perf-state.json` i wraca bajt w bajt.

Zmierzone na prawdziwych plikach portivo. Formatterów tam nie ma, więc obie wersje niczego
nie zmieniają; sprawdziłem, że mtime plików jest ten sam:

| | npx | szybki npx |
|---|---|---|
| auto-format.sh na `package.json` | 3297 ms | 39 ms |
| auto-format.sh na `src/i18n.ts` | 6643 ms | 82 ms |
| `npx --no-install tsc --version` | 236 ms | 54 ms |
| `npx --version` (przekazane dalej) | 82 ms | 88 ms |

Na żywo, od 21:32, pierwsze 17 minut (włączone przez `perf.py apply`): edycje plików formatowanych p50 78 ms (n=4; Write .md/.json 67-83 ms, Edit .ts z tsc 1,9 s), przed 2,1 s p50 w dobie (n=541). Próbka jest mała, kierunek zgodny z pomiarem wyżej; `perf.py bench agents` pokaże pełną dobę.

**2. Testy Go czekają na zamek i na sufit 10 minut. Zamek zostaje, sufit podniesiony.**

- **Zamek plock (decyzja właściciela, nie ruszam):** testy i buildy charter-service idą
  przez wspólny zamek `~/.claude/portivo-locks/plock.py go`. Do tego dochodzi `go env
  GOFLAGS=-p=4`, którego wymaga hook `depot-heavy-go.sh` portivo: jedna kompilacja
  charter-service to 4-6 GB, dwie naraz 10-12 GB.
  - Z 422 poleceń pod zamkiem w ostatniej dobie 27 czekało, razem 18 minut, mediana 24 s,
    najdłużej 153 s.
- **Sufit 10 minut:** w dwie doby 59 poleceń Bash zatrzymało się na 600 s. Około 40 z nich
  miało od agenta timeout dłuższy niż 10 minut (15-62 min: testy Go pod zamkiem, czekanie
  na buildy na Depot, pętle `until ...`). Claude Code ściął je do 10 minut i przeniósł w
  tło, więc agent musiał potem odpytywać plik z wyjściem.
- **Poprawka `claude-limits`:** `BASH_MAX_TIMEOUT_MS` = 3600000 (1 h).
  - Według dokumentacji to tylko sufit, o który model może poprosić. Domyślny limit 2
    minut się nie zmienia, a limity zadań w tle też nie, bo te zmieniają się dopiero
    powyżej 2 godzin.
  - Na żywo: w pierwszych 17 minutach żadne polecenie nie zostało ścięte. Pełny obraz da dopiero doba, bo przed zmianą było około 20 takich poleceń na dobę.

**3. Stały koszt każdego polecenia Bash: około 200 ms. Nic więcej do wyciągnięcia.**

- Nawet `ls` czy `echo` mają p50 215 ms. Przed poleceniem hooki działają równolegle, więc
  liczy się najdłuższy z nich:

  | hook przed poleceniem Bash | p50 |
  |---|---|
  | devguard admit (z szybką ścieżką) | 36 ms |
  | rg-rewrite.sh (portivo) | 26 ms |
  | depot-heavy-go.sh (portivo) | 22 ms |
  | rtk-enforce | 21 ms |

- Po poleceniu czeka już tylko hook Orki (24 ms), bo cavemem chodzi w tle od fazy 2.
- Snapshot powłoki Claude (147 KB) źródłuje się w 20 ms.
- Resztę stanowią sam Claude Code i zapis transkryptu.

### Limity Claude Code (2.1.289)

Sprawdzone trzema drogami:
- dokumentacja (Context7, `/websites/code_claude`: env-vars, workflows, tools-reference);
- wartości domyślne wprost w binarce;
- środowisko 9 działających sesji (żadna nie ma ustawionego żadnego z tych limitów).

| pokrętło | teraz | domyślnie | sygnały w transkryptach (48 h) | decyzja |
|---|---|---|---|---|
| `BASH_MAX_TIMEOUT_MS` | 3600000 (Ultra) | 600000 | ~40 poleceń z dłuższym timeoutem ściętych do 10 min | **podniesione do 1 h** (`claude-limits`) |
| `BASH_DEFAULT_TIMEOUT_MS` | brak | 120000 | 24 polecenia bez własnego timeoutu ucięte na 2 min; to głównie `sleep 240`, zawieszone curl i docker | bez zmian; dłuższy domyślny limit przedłużyłby czekanie na zawieszone polecenia; agenci i tak podają timeout (319 razy 300 s) |
| `BASH_MAX_OUTPUT_LENGTH` | brak | 30000 znaków | 161 razy „Output too large” (wyjście trafia do pliku) | bez zmian; więcej to więcej kontekstu w każdej rozmowie |
| `MAX_MCP_OUTPUT_TOKENS` | brak | 25000 | 3 przekroczenia | zalecenie: 50000, jeśli te serwery MCP są ważne |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | brak | zależne od modelu | 0 odpowiedzi ucięte na `max_tokens` | bez zmian |
| `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` | brak | 10 | najwięcej 3 narzędzia w jednej wiadomości | bez zmian |
| `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | brak | 20 | 0 razy „Concurrent subagent limit reached” | bez zmian |
| `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` | brak | 3 (albo z flag funkcji, ale `DISABLE_TELEMETRY` je wyłącza) | 0 razy „nesting limit reached” | bez zmian |
| `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` | brak | liczba rdzeni, tu 16 | brak śladu w transkryptach | bez zmian; podnieść tylko pod konkretny workflow |
| `workflowSizeGuideline` | brak, czyli `medium` (<10 agentów) | `medium` | to podpowiedź dla modelu, a nie limit | zalecenie na życzenie: `large` (<50) albo `unrestricted`; więcej agentów to więcej tokenów i RAM |
| `CLAUDE_CODE_SUBAGENT_MODEL` | brak, czyli model główny | `inherit` | | bez zmian; szybszy model przyspieszy subagentów kosztem jakości |
| myślenie / wysiłek | `effortLevel: xhigh`, myślenie adaptacyjne | | | bez zmian |
| `MCP_TIMEOUT` / `MCP_TOOL_TIMEOUT` | brak | 30 s / ~28 h | | bez zmian |
| timeouty hooków | 10-30 s na hook | 600 s | | bez zmian |
| autocompact | `autoCompactWindow` 1000000, włączony | | | bez zmian |
| ustawienia agentów w Orce | `claudeAgentTeamsMode: off`, `keepComputerAwakeWhileAgentsRun: true`, hibernacja wyłączona | | | nic nie ogranicza współbieżności |

### Reszta z listy

- **Priorytet drzew agentów:** wszystko pod hostem terminali Orki działa z pri 31 i nice 0
  (claude, node, pnpm, next-server, air, git, go).
  - claude liczy w 80% na rdzeniach P, next-server w 90%.
  - node ma 44%, ale to głównie cavemem worker i dev serwery, które celowo są w tle.
  - Nic nie działa poniżej domyślnego priorytetu, więc nie ma czego poprawiać.
- **golangci-lint to agenci, nie hook:** 16 uruchomień na dobę przez plock z
  `--concurrency 4`, p50 0,7 s (z cache), p90 27 s. Jedno uruchomienie rodzi setki
  procesów potomnych przez loader pakietów; stąd 254 starty w 30 s. Cache (137 MB) zostaje.
- **Go:**
  - `GOCACHE` ma teraz 8,2 GB, `GOMAXPROCS` jest domyślny (16), a `GOFLAGS=-p=4` to
    wymaganie RAM guarda portivo.
  - `-buildvcs`: `go test` nie stempluje VCS, a przy `go build` git status trwa po
    git-speed 28 ms, więc nie ma czego oszczędzać.
  - Janitor kasuje cały `GOCACHE` po przekroczeniu 20 GB (3 października było to 87,5 GB),
    więc następne buildy są zimne. Usuwanie tylko najstarszych wpisów byłoby łagodniejsze.
    Nie zmierzyłem tego, bo w transkryptach różnicy nie widać na tle zmian obciążenia; to
    temat dla `janitor.py`.
- **Orca i `ps`:**
  - Orca odpala `ps -axo pid,ppid,pgid,tpgid,stat,tty,lstart,command` około 1,4 razy na
    sekundę (70 ms, w tym 60 ms w jądrze); kolumna `tty` każe `ps` przeszukać `/dev`.
  - Ani w `orca --help`, ani w ustawieniach (`orca-data.json`) nie ma pokrętła do
    odpytywania. Binarki nie zmieniam, więc to temat dla twórców Orki: mogliby czytać
    `libproc` albo pominąć `tty`.
- **WindowServer** (`sudo powermetrics --samplers tasks,gpu_power` i `sudo sample`):
  - 429 ms CPU na sekundę, 658 wybudzeń/s, 215 terminów poniżej 2 ms na sekundę.
  - W `sample` aktywna część to `CA::Render::Updater::prepare_layer`, czyli przeliczanie
    drzewa warstw przy każdej klatce. Koszt rośnie z liczbą warstw na ekranie, a nie z
    jedną aplikacją.
  - Bez chowania okien jedno po drugim nie da się wskazać winnego. Narzędzie do tego to
    `perf.py bench gpu` przed i po.
- **secd:** w pierwszym `powermetrics` palił 40% CPU, a ctkd 10%. To było uwierzytelnianie
  Touch ID tego samego `sudo`. Pół minuty później secd miał 0,1%, a nikt nie wołał
  `security`.

### Gatekeeper: każda nowa binarka z Orki czeka na ocenę

Każdy `go test` buduje świeżą binarkę testu dla każdego pakietu, a `go run` i natywne
moduły node robią to samo. macOS przy pierwszym uruchomieniu takiej binarki pyta, czy
proces odpowiedzialny za nią (ten, który `responsibility_get_pid_responsible_for_pid`
wskazuje dla powłoki) był na liście Narzędzi deweloperskich (Prywatność i ochrona,
`kTCCServiceDeveloperTool` w systemowym TCC.db), kiedy startował. Jeśli nie, syspolicyd
ocenia binarkę:
skan XProtect i zapytanie HTTPS do Apple o bilet notaryzacji (około 150 ms, limit 3 s,
więc na słabej sieci dużo gorzej). Widać to w `log stream` syspolicyd przy każdym
pierwszym exec. Drugie uruchomienie tej samej binarki idzie już z pamięci podręcznej.

Za każdą powłoką agenta stoi Orca (`com.stablyai.orca`), a na liście był tylko Terminal.

Pierwsze uruchomienie świeżo zbudowanej binarki Go (każda z inną stałą, więc inny hash;
mediana z 8-10, `perf.py bench gatekeeper` mierzy to samo tam, gdzie go uruchomisz):

| skąd | pierwsze | drugie |
|---|---|---|
| Terminal (Narzędzie deweloperskie) | 4,3 ms | 3,3 ms |
| proces z odrzuconą odpowiedzialnością (`posix_spawn` z disclaim) | 208,5 ms | |
| terminal w Orce, przed | 196,2 ms | 3,7 ms |
| terminal w Orce, kopia tej samej binarki (ten sam hash, nowy plik) | 76,8 ms | |
| terminal w Orce, po dodaniu Orki, ta sama Orca | 196,5 ms | 3,6 ms |
| terminal w Orce, po restarcie Orki | 4,1-4,4 ms | 3,4 ms |
| terminal w Orce po restarcie, kopia tej samej binarki | 4,0 ms | |

Prawdziwe repo, kopia portivo `apps/auth-service` (8 pakietów), `go test -count=1`:

| | Terminal | Orca przed | Orca po dodaniu, bez restartu | Orca po restarcie |
|---|---|---|---|---|
| `-run ^$ ./...` (sam build i start binarek, 3 razy) | 2,30-2,39 s | 2,67-2,83 s | 2,64-2,84 s | 2,26-2,46 s |
| pełne `./...` | 23-32 s | 26-33 s | 21,8 s | nie mierzone |

- Po restarcie Orki pierwszy exec spada ze 196 do 4,1 ms (około 48 razy), czyli tyle, ile
  w Terminalu. Sam build i start binarek auth-service skraca się o około 0,4 s na przebieg,
  czyli około 50 ms na pakiet.
- Na pełnych testach różnica ginie w szumie samych testów.
- Kara rośnie jednak z liczbą pakietów: charter-service ma 272 pakiety testowe, czyli do
  około 53 s oceny na pełny przebieg modułu, jeśli syspolicyd ocenia je po kolei.
- Każde `go test` jednego pakietu, które agent puszcza po edycji, płaci 0,2 s, a bez sieci
  do Apple nawet do 3 s.

Naprawa to Orca na liście Narzędzi deweloperskich:

- **Skryptem się nie da.** Na macOS 27 SIP chroni systemowy TCC.db także przed rootem:
  `sqlite3` pod sudo dostaje „attempt to write a readonly database”, a `test -w` jako
  root zwraca fałsz. `tccutil` umie tylko kasować. Profil PPPC nie ma usługi
  DeveloperTool i wymaga MDM.
- **Ręcznie:** Ustawienia systemowe > Prywatność i ochrona > Narzędzia deweloperskie >
  „+” > `/Applications/Orca.app` > Otwórz, potwierdzić Touch ID.
- `claude-acc perf-root devtools add` (bez sudo, w Terminalu) otwiera ten panel
  (`x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_DevTools`),
  czeka do 5 minut, aż wpis Orki pojawi się w TCC.db, i zapisuje go w stanie perf.py.
  Czytanie bazy wymaga tylko Pełnego dostępu do dysku, który Terminal ma.
- **Cofnięcie:** `claude-acc perf-root devtools undo` otwiera ten sam panel. Zaznacz Orkę
  i kliknij „-” (albo wyłącz przełącznik, jeśli przed zmianą był wyłączony). Skrypt czeka,
  aż wpis wróci do stanu sprzed zmiany, i usuwa zapis.
- `claude-acc perf-root devtools status` pokazuje listę.
- Ultra nie zrobi tego sama, więc dopóki wpisu nie ma, pokazuje `devtools` w
  `pending_manual`.
- `claude-acc perf bench gatekeeper` uruchomione z terminala Orki, która wystartowała po
  zapisie devtools, samo zapisuje swój pierwszy exec jako wynik „po” (przed: 196,2 ms),
  jeśli devtools nie ma jeszcze wyniku.
- **Działa dopiero po restarcie Orki.** Po dodaniu wpisu ta sama Orca (uruchomiona
  wcześniej) dalej płaci 196 ms, nawet w nowym terminalu. W logu syspolicyd binarki z jej
  terminali dalej dostają `GK performScan` i `evaluateScanResult: 2`, a pytanie do TCC
  przychodzi dopiero po skanie. Binarki z Terminala w ogóle nie docierają do syspolicyd.
  Zwolnienie jest więc przypięte do procesu odpowiedzialnego w chwili jego startu (Apple
  przy Terminalu też każe go zrestartować). Restart Orki zamyka sesje w jej terminalach,
  więc perf.py tylko o niego prosi: `devtools-restart` w `pending_manual`, dopóki działająca
  Orca wystartowała przed zapisem.
- Ryzyko: aplikacja na tej liście może uruchamiać programy, które nie spełniają zasad
  Gatekeepera. To ten sam poziom zaufania, jaki Terminal ma od początku, a Orca i tak
  uruchamia dowolne polecenia agentów.

## Faza 4: pętla agenta od modelu do wyniku (2026-10-08)

Pytanie: gdzie Claude naprawdę traci czas i co jeszcze da się przyspieszyć natywnie. Pod
uwagę wchodzi model, sieć, Bash, hooki, Python, build, Orca i sprzęt. Wszystko jest
zmierzone na tym Macu: MacBook Pro M4 Max (12 rdzeni P + 4 E, 48 GB), zasilanie z
sieci, `pmset powermode 2`, brak ostrzeżeń termicznych.

### Skąd liczby

Źródło to 7 dni transkryptów Claude Code: 712 plików, 5 GB, także subagenci.

- **Zapytanie do modelu** zaczyna się wpisem tuż przed jego pierwszym blokiem (prompt
  albo wynik narzędzia). Kończy się ostatnim blokiem z tym samym `requestId`.
- **Narzędzie** liczę od `tool_use` do `tool_result`, jak w fazie 3.
- **Hooki** biorę z `durationMs` we wpisach `hook_success`.

`perf.py bench agents [--hours N]` liczy dziś to wszystko sam: opóźnienie modelu według
kontekstu, podłogę Bash, zimny cache i narzędzia według łącznego czasu.

| gdzie | wynik | uwaga |
|---|---|---|
| zapytania do modelu | 145 tys. na tydzień, 79% od subagentów | suma 372 h po równoległych agentach |
| narzędzia | 327 h | z tego `sleep`/`until` 53 h, to celowe czekanie |
| Bash | 122 tys. wywołań | polecenia krótsze niż 5 s: p10 120 ms, p50 260 ms przy 5-20 ms samej pracy |
| hooki blokujące Bash | ~5 ms | fasthooks rtk-enforce (Go) równolegle z claude-acc-hook (Swift); ~40 ms przy go/npm/dev |

**Model zwalnia z kontekstem.** p50 / p90 całego zapytania, pierwszy blok p50:

| kontekst | zapytań | p50 | p90 | pierwszy blok |
|---|---|---|---|---|
| < 30 tys. | 475 | 2,6 s | 6,1 s | 2,0 s |
| 30-100 tys. | 11 706 | 3,9 s | 11,3 s | 3,4 s |
| 100-200 tys. | 27 593 | 5,1 s | 16,5 s | 4,5 s |
| 200-400 tys. | 45 617 | 5,9 s | 20,0 s | 4,9 s |
| 400-700 tys. | 41 995 | 6,8 s | 19,5 s | 5,5 s |
| 700 tys.+ | 13 467 | 6,8 s | 19,3 s | 5,4 s |

70% zapytań idzie z kontekstem ponad 200 tys. Generowanie stoi na 250-280 tokenach/s
niezależnie od kontekstu, więc różnicę robi czas do pierwszego bloku.

Subagenci startują z małym kontekstem (16-56 tys. tokenów), ale rosną:
- członkowie zespołu do p50 444 tys. i p90 965 tys.,
- workflow-subagent do p50 363 tys.

Nie ma ustawienia kompaktacji tylko dla subagentów. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` i
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` działają też na główną sesję.

### Cache subagentów: godzina zamiast 5 minut

Subagenci domyślnie piszą cache na 5 minut. Przerwa dłuższa niż 5 minut, na przykład
członek zespołu czekający na innego, oznacza zapis całego kontekstu od nowa.

Od 1 do 3.10, z cache 5-minutowym:
- 300-500 mln tokenów zapisu na dobę,
- 47% zapisów to kontekst zapisywany od nowa po 5-60 min ciszy,
- 678 takich zapytań w tygodniu, p50 8,1 s.

Od 4.10 `~/.claude/settings.json` ma `subagentPromptCacheTtl: "1h"`. Od 5.10 subagenci
piszą tylko do kubełka 1 h, 45-115 mln tokenów na dobę. W ostatniej dobie nie było ani
jednego zapisu od nowa po przerwie 5-60 min. Ultra pilnuje teraz tego ustawienia jako
`subagent-cache-1h`.

### Sieć: telefon zamiast kabla

Trasa domyślna szła przez iPhone USB (`en8`) z flagą `constrained`, czyli Low Data Mode.
Adapter USB LAN był odłączony, a Wi-Fi wyłączone.

| pomiar | wynik |
|---|---|
| ping do 1.1.1.1 | 10-248 ms (σ 75 ms) |
| TCP do api.anthropic.com | p50 25 ms |
| TLS | p50 51 ms |
| upload | 16-21 Mb/s |
| treść 2 MB (surowy kontekst ~400 tys. tokenów) | +0,45-0,8 s wobec 40 KB |

Claude Code domyślnie kompresuje duże zapytania (`CLAUDE_CODE_GZIP_REQUEST_BODIES`, tu
`1`), więc realny koszt uploadu jest mniejszy. Nie da się go zmniejszyć inaczej niż
mniejszym kontekstem.

Poprawka **`tether-profile`** sprawdza w `perf keep` co 5 min, którędy idzie trasa
domyślna. Tethering to port `iPhone USB` albo `Bluetooth PAN`, albo brama `172.20.10.1`.
Na takim łączu poprawka ustawia w env Claude Code:
- `DISABLE_AUTOUPDATER=1`, bo natywny updater ściąga ~236 MB na każde wydanie,
- `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false`,
- `CLAUDE_CODE_ENABLE_AWAY_SUMMARY=0`.

Na kablu albo zwykłym Wi-Fi zdejmuje je co do bajtu. Ten sam przebieg zapisuje `link` w
`perf-state.json`. Według niego `updates.py` wstrzymuje zaplanowane aktualizacje na
tetheringu. `claude-acc update` działa zawsze.

MPTCP (telefon plus Wi-Fi naraz) odpada: `URLSession` na macOS go nie ma, a serwer
musiałby go obsługiwać.

### Bash: 17 ms z ~/.zshenv w każdym wywołaniu

Claude Code uruchamia każde polecenie przez `/bin/zsh -c "source <snapshot> … eval"`.
Snapshot zastępuje `.zshrc`, ale `~/.zshenv` i tak wykonuje się przy każdym poleceniu. Tam
dwa razy wołany był `security find-generic-password`. Dodatkowo jeden klucz leżał w
pliku jawnym tekstem.

| spawn powłoki jak w Claude Code | czas |
|---|---|
| przed | 21,1 ms |
| po (guard `[ -n "$VAR" ] \|\|`, zmienne dziedziczone z procesu `claude`) | **4,2 ms** |
| bez dziedziczonych zmiennych (pierwsza powłoka sesji) | 36,8 ms |

Klucze przeszły do Pęku kluczy przez `security -i` na stdin, więc wartość nie trafiła do
`ps`. Konfiguracja Claude Platform on AWS i Bedrock zniknęła z `~/.zshrc`, z env w
settings.json i z 156 snapshotów powłoki.

`PYTHON_JIT=1` nie kosztuje startu: 12,6 ms wobec 12,8 ms dla `-c pass`, więc zostaje.

### rg: 16 wątków walczy o blokady jądra

To samo wyszukiwanie w portivo (17,7 tys. plików), przy obciążeniu 5-6:

| wątki | czas | czas jądra |
|---|---|---|
| domyślnie (16) | 428 ms | 3,9 s |
| -j2 | 242 ms | |
| -j4 | **179 ms** | 0,7 s |
| -j6 | 272 ms | |
| -j12 | 434 ms | |

W claude-acc: 6,5 ms przy domyślnych wątkach, 5,8 ms z `-j4`. Agenci wołają rg/grep
~4300 razy na dobę.

Poprawka **`rg-threads`** zapisuje `ripgreprc` z `--threads=4` i ustawia
`RIPGREP_CONFIG_PATH` w env Claude Code. Jawne `-j` w poleceniu dalej wygrywa.

### cavemem worker: 228% CPU bez przerwy

Worker cavemem (node, onnxruntime-node 1.21, MiniLM-L6 q8) palił 140-228% CPU przez cały
dzień. Średnie obciążenie Maca stało przez to na 14-15.

`sample` pokazał dwa źródła:
- `observationsMissingEmbeddings` co 2 s robiło `SCAN` wszystkich 628 tys. wierszy bazy
  2,2 GB. W stanie ustalonym brakowało mniej niż 16 wierszy, więc zapytanie z
  `ORDER BY id DESC LIMIT 16` przechodziło całą tabelę.
- 7 wątków puli ORT kręciło się na pusto (`RunQueue::PopFront`). Inferencja to ~1%
  pracy, więc CoreML ani ANE nic by tu nie dały.

bg-helpers trzymał worker w tle, ale ten tracił `PRIO_DARWIN_BG` co kilkadziesiąt minut.
Nawet na rdzeniach E zajmował 2,3 z 4.

Naprawa poszła w repo cavemem (sweep z kursorem `afterId`, build z 18:16). Nowy worker
pali 0-2% CPU. Do zrobienia tam jeszcze: `session_options` z `intraOpNumThreads: 1` i
`allow_spinning: "0"`.

### Build Swift

| pomiar | wynik |
|---|---|
| czysty release (swiftbuild) | 12-17 s |
| czysty release `--build-system native` | 12,2 s, ale native jest oznaczony jako przestarzały |
| czysty debug | 7 s |
| no-op | ~1 s |
| frontend ClaudeAcc | 14,1 s na jednym wątku, 8,7 s na 8 wątkach; optymalizacja SIL 4,1 s, sprawdzanie typów 1,8 s |
| najwolniejsze ciało widoku | ≤106 ms, nie jest wąskim gardłem |

`-Osize` daje ten sam czas buildu (11,5-12,1 s wobec 12,2-15,3 s) i 12% mniejszy
`__TEXT` (1,10 -> 0,97 MB). Dla aplikacji w pasku menu to bez znaczenia, więc go nie
włączam. Caching kompilacji przez CAS (SE-0547) przyjęto dziś, ale w CLI 6.4 go nie ma.

### Sprawdzone i niewłączone

| pomysł | dlaczego nie |
|---|---|
| CoreML/ANE dla embeddingów cavemem | inferencja to ~1% pracy workera |
| MPTCP telefon + Wi-Fi | brak w `URLSession` na macOS, serwer musiałby go obsługiwać |
| RAM disk na `.build`, `noatime` | APFS i SSD M4 Max: zysk w szumie |
| `-mcpu=apple-m4` | 0-3% dla małych CLI, SIGILL na M1 z butelki Homebrew |
| Python free-threaded, mypyc | wolniejszy pojedynczy wątek; XProtect skanuje każdy `.so` |
| git z Homebrew zamiast shimu xcrun | +4,3 ms na proces × ~3 tys. wywołań na tydzień |
| `USE_BUILTIN_RIPGREP=0` | narzędzie Grep 0 razy na tydzień; agenci wołają rg przez Bash |
| typecheck TS w tle (`asyncRewake`) | 4 edycje formatowane na dobę, ~15 min na tydzień |
| podział ClaudeAcc na moduły | release 12 s, budowany przy instalacji |
| Orca | git co ~6 s (p50 18 ms, 916 s na 34 h), trace 10 MB na dobę: mało |

### GPU, termika, debloat

**GPU.** Zajęty w 13% (max 34%), z czego WindowServer to 14% czasu GPU, a Orca 1%. Małe
zlecenie wraca w 463 µs p50. W pętli agenta nic na GPU nie czeka: model liczy się w
chmurze, a rg, git, kompilacja i testy to CPU i IO.

Lokalny model na GPU (MLX) mógłby robić drobne rzeczy, ale Claude Code nie wyprowadza
swoich zapytań w tle do lokalnego modelu bez proxy na cały ruch. Pomijam.

**Termika.** Wiatraki już stoją na maksimum: fanctl trzyma 5777 rpm w trybie manual. Test
z 16 procesami SHA-256 przez 90 s, przy obciążeniu Maca 15:

| sekunda | przepustowość | CPU |
|---|---|---|
| 10 | 27,9 GB/s | 84 °C |
| 40 | 34,8 GB/s | 85 °C |
| 80 | 30,5 GB/s | 87 °C |

Przepustowość nie ma trendu spadkowego, a wahania to inni agenci. M4 Max zbija zegary
dopiero przy ~100 °C, więc throttlingu nie ma i nic więcej z wentylacji się nie wyciśnie.

**Prawdziwy hamulec to CPU zajęte przez tło.** `bench cpu` przy 16 procesach: 58%
czasu w kolejce.

| proces | RAM | CPU |
|---|---|---|
| VM Dockera | 8,6 GB | 55% |
| WindowServer | | 45% |
| Brave | ~4 GB | ~70% razem |
| Canary Mail | | 25% |

Reduce Transparency jest już włączone, a ekran tylko wewnętrzny, więc WindowServer nie
ma łatwej dźwigni.

**Docker.** 26 kontenerów w trzech projektach:

| projekt | kontenery | stan |
|---|---|---|
| portivo (`untitled`) | 15 | stack dev od 8 h; z Maca tylko 2 połączenia do mailpit |
| klastry testpg | 6 | wspólne, do ponownego użycia; harness ma limit 6 |
| Supabase `outofplace-finance` | 11 | |

Wszystkie 17 zdarzeń `docker exec` z ostatniej godziny to healthchecki.

Harness testpg w portivo sam zatrzymuje swoje klastry po 2 h bez podłączenia i usuwa je
po 24 h, ale tylko gdy akurat startuje test. Restart po zatrzymaniu to ~1 s plus 10-40 s
budowy szablonu.

Poprawka **`docker-idle`** (poza Ultra, `perf.py apply docker-idle`) zatrzymuje projekt
compose albo kontener, gdy przez `docker_idle_hours` (2 h) nikt z Maca się z nim nie
łączył i nikt nie zrobił `docker exec` poza healthcheckiem. Zegar rusza od pierwszego
spojrzenia, a nie od startu kontenera. Resource Saver Dockera usypia potem VM, gdy nic
już nie działa. `undo` uruchamia z powrotem dokładnie to, co poprawka zatrzymała. Projekt
przywraca `docker compose up -d` w repo albo `docker start <id>`.

**Updatery.** Pliki Google Keystone (`com.google.keystone.*`) to celowe zaślepki: pusty
`<dict/>` należący do roota, blokujący ponowną instalację Keystone. Zostają. Reszta jest
albo potrzebna, albo nic nie kosztuje:
- Microsoft AutoUpdate to jedyne łatki Office,
- GoogleUpdater łata Chrome, bo `updates.py` aktualizuje caski bez `--greedy`,
- Adobe ARMDC i XQuartz startują tylko na żądanie.

### claude-acc: mniej procesów, szybszy interpreter

| zmiana | przed | po |
|---|---|---|
| `claude-acc-hook pause` w trakcie pauzy (fronty przez `acc.py` na `$STATE/python` zamiast `/usr/bin/python3` 3.9) | 34,6 ms | 22,9 ms |
| `claude-acc-pause` w trakcie pauzy | 30,0 ms | 20,4 ms |
| procesy na minutę przy zamkniętym panelu: tick zapisuje `status.json`, aplikacja go czyta zamiast co minutę uruchamiać `status --json` z ~20 odczytami Pęku kluczy | ~32 | ~12 |
| `mail --help`, Python 3.15 z `__lazy_modules__` (PEP 810) | 39 ms | 24 ms |
| `updates --help`, Python 3.15 z `__lazy_modules__` | 26 ms | 16 ms |
| import `mcpbase`, Python 3.15 z `__lazy_modules__` | 15 ms (3.14) | 7 ms |

Hand-over do `devguard admit` przez `acc.py` zmienia się w granicach szumu (16,7 -> 17,1 ms).

`setup.sh` przejdzie na uv-owego 3.15, gdy będzie wydaniem końcowym (premiera 2026-10-09).
Do tego czasu zostaje 3.14.8, a `__lazy_modules__` na 3.14 i 3.9 nic nie zmienia.

Token z `/login` aplikacja przejmuje teraz najpóźniej po jednym ticku (do 120 s), a przy
otwarciu panelu od razu.

`PYTHON_JIT=1` zostaje: start trwa 12,6 ms z JIT i 12,8 ms bez.

### rg pod skrajnym obciążeniem

Przy obciążeniu 152 (inny agent puścił test wyścigów z 32 procesami `yes`) `-j4` było o
10% wolniejsze od 16 wątków: 576 wobec 518 ms. Przy zwykłym obciążeniu tego Maca (5-15) jest
2,3-2,4x szybsze. Zostaje 4.

## Wyniki ogólne: sieć, CPU, GPU

### Sieć (TKB, kabel)

Punkt wyjścia: 441-452 Mb/s w dół i 136-220 Mb/s w górę. Ping do bramy 0,8 ms (max
1,1 ms), do 1.1.1.1 9,9 ms. Do api.anthropic.com: TCP 11-12 ms, TLS 22-24 ms, pierwszy
bajt 34-36 ms.

Przypięte serwery, 3 przebiegi na wariant (2 dla h3 w Berlinie), mediana z zakresem:

| serwer / wariant | w dół Mb/s | w górę Mb/s | bez obciążenia ms | sieć przy wysyłaniu p90 ms | kolejka w połączeniu przy wysyłaniu p50 ms | responsiveness przy wysyłaniu ms |
|---|---|---|---|---|---|---|
| Mediolan, TCP | 447 (438-453) | 218 (145-261) | 42 | 45 | 364 (328-465) | 298 (214-333) |
| Mediolan, TCP + L4S | 442 | **97 (94-109)** | 42 | 45 | 462 | **1032 (790-1536)** |
| Mediolan, h3 (QUIC) | 210 | 334 | 43 | 46 | 138 | 80 (72-130) |
| Berlin, TCP | 224 | 256 | 28 | 34 | 318 | 197 (189-221) |
| Berlin, TCP + L4S | 193 | 284 | 28 | 32 | 193 | 275 (173-377) |
| Berlin, h3 (QUIC) | 203 | 204 | 35 | 83 | 56 | 51 |

Wnioski:

- **Przy BE230 sieć nie puchnie.** Opóźnienie na osobnych połączeniach rośnie pod
  obciążeniem o 1-4 ms w obie strony. Cała „responsiveness” rzędu 200-330 ms przy
  wysyłaniu to kolejka w buforze gniazda samego obciążonego połączenia (TCP autotuning do
  4 MB). Router i ogranicznik nie mają tu nic do roboty.
- **Wymuszone L4S szkodzi.** Mediolan nie zna AccECN, połączenia spadają do klasycznego ECN,
  a upload leci z 218 do 97 Mb/s przy responsiveness ponad sekundę. W Berlinie (AccECN) jest
  remis w granicach szumu. Na trasie nic nie znakuje L4S (ani BE230, ani TKB), więc nie ma z
  czego zyskać. `net.inet.tcp.l4s` zostaje 0.
- **QUIC trzyma kolejkę krótko**: kolejka w połączeniu 56-138 ms zamiast 318-364 ms. Za to
  pobiera tylko 200-230 Mb/s, bo jego stos działa w przestrzeni użytkownika. Brave używa
  QUIC tam, gdzie serwer go zna, i to jest dobre ustawienie. Claude Code (Bun) chodzi po TCP,
  a pojedyncze zapytania API nie są na tyle duże, żeby ta kolejka miała znaczenie.
- **Kabel kontra Wi-Fi do tej samej bramy:** ping 0,77 ms (max 1,1-1,3) przez kabel i
  2,29 ms (max 3,3) przez Wi-Fi. Przepustowość w tym oknie była zaszumiona. Kolejność
  usług z kablem na pierwszym miejscu zostaje.
- **Sterownik adaptera:** RTL8153 (0bda:8153) obsługuje tylko CDC-ECM, więc działa na
  AppleUserECM i nie da się go przełączyć na NCM. Przy 448 Mb/s ten sterownik zajmuje 19%
  jednego rdzenia. To mało, ale adapter na RTL8156B (NCM, 2,5 GbE) obniżyłby ten koszt
  mniej więcej o połowę (dane z raportów, nie z pomiaru).

### CPU

Punkt wyjścia (load 7-8, swap 3,6 GB):

| pomiar | wynik |
|---|---|
| 1 proces SHA-256 | 2968-2972 MB/s, 100% na rdzeniach P, kolejka 0-1,3% |
| 16 procesów naraz | 25,4-29,0 GB/s razem, 84-88% na rdzeniach P, kolejka 15-23% |
| wybudzenie po 1 ms | p50 259-262 µs, p99 393-421 µs, max 2,4-6,5 ms spóźnienia |

Planista nie dusi pierwszego planu. 16 równoległych zadań traci 15-23% czasu w kolejce, a
to są cztery wolne rdzenie E plus to, co i tak liczy się w tle.

Kto pali rdzenie P i energię wśród moich procesów (okno 20 s):

| proces | CPU | rdzeń P | energia |
|---|---|---|---|
| cavemem worker (`node .../cavemem/dist/index.js worker run`) | 23% | 23% | 1,07 W |
| maszyna Dockera (Virtualization.framework) | 38% | 25% | 0,59 W |
| Orca renderer | 8% | 8% | 0,20 W |
| ClaudeAcc (panel) | 27% | 2,7% | 0,15 W |

Worker cavemem działa od startu systemu: 54 minuty CPU w 3,5 godziny. W `sample` widać
timer, który co chwilę robi zapytanie SQLite. Zapytanie skanuje bazę
`~/.cavemem/data.db` (1,9 GB) przez `pread`.

Według samego cavemem worker robi tylko dwie rzeczy: liczy embeddingi i serwuje podgląd.
Hooki cavemem w Claude Code to osobne procesy node. Przez 60 s obserwacji żaden z nich
nie żył dłużej niż odstęp próbkowania (około 50 ms). Na worker nie czeka więc żadna
sesja.

**Poprawka `bg-helpers`** daje procesom z listy `background` QoS tła Darwina
(`PRIO_DARWIN_BG`, to samo co `taskpolicy -b`). Na cavemem worker, A/B po 45 s, dwa razy:

| tryb | CPU | na rdzeniach P | energia | czekanie w kolejce |
|---|---|---|---|---|
| normalnie | 24-32% | 99% | 0,90-1,25 W | 0,1-0,4% |
| w tle | 43-72% | 0,2-0,5% | 0,10-0,19 W | 41-104% |
| po `perf.py apply` (30 s) | 49% | 0,1% | 0,09 W | 33% |

Rdzeń P zwalnia się o 24-32 punkty procentowe, a energia spada o 85-90%. Ta sama praca
trwa na rdzeniach E około dwa razy dłużej i czeka tam w kolejce: zabiera czas tłu
systemu, a nie pierwszemu planowi. Na pracy w pierwszym planie zysk widać dopiero przy
pełnych rdzeniach P, czyli przy buildach i wielu agentach naraz. Przy dzisiejszym loadzie
7-8 to różnica rzędu ćwierci rdzenia z dwunastu.

### GPU

Punkt wyjścia:

- Bez renderu w tle są dwa pomiary:
  - pierwszy: GPU zajęte 45% (max 88%), WindowServer 47% CPU i 42% czasu GPU; sonda
    Metalu p50 373 µs, p99 1,3 ms;
  - drugi, pół godziny później: GPU 0% (max 26%), WindowServer 43% CPU i 4,6% czasu GPU.
- Z renderem Blendera innego agenta (`Blender -b ... hero-film`): GPU 97-99%, Blender
  41-64% czasu GPU. Czas GPU WindowServera rośnie z 8% do 35-45%, bo jego klatki dłużej
  czekają.

| eksperyment (3 rundy) | sonda p99 tam i z powrotem | wniosek |
|---|---|---|
| Blender normalnie | 1,7-3,3 ms | |
| Blender wstrzymany (SIGSTOP na 5 s) | 1,1-2,5 ms | p99 lepsze o 0,6-2,2 ms w 2 z 3 rund |
| Blender w QoS tła (`PRIO_DARWIN_BG`) | 2,5-5,6 ms | GPU się tym nie przejmuje: Blender trafia na rdzenie E (rdzeń P 58% -> 1%), ale udział w GPU zostaje |

Rozkład czasu GPU mówi dwie rzeczy. QoS tła nie obniża priorytetu na GPU, a nawet
wstrzymanie renderu poprawia ogon małych zleceń tylko o 1-2 ms przy budżecie klatki 8,3 ms
(120 Hz). Wywłaszczanie na GPU M4 Max działa, więc nie ma tu poprawki do zrobienia.

Sonda mierzy przy okazji wybudzanie GPU ze stanu oszczędzania. Przy bezczynnym GPU
(0%, max 26%) wyszło p50 0,7 ms i p99 3,8 ms, czyli więcej niż pod obciążeniem. Jej
wynik porównuję więc tylko między pomiarami przy podobnym obciążeniu GPU.

WindowServer:

- Od startu systemu zużył 88 minut CPU w 3,6 godziny, czyli średnio 41% rdzenia.
- Przez 10 minut próbkowałem go co 5 s razem z aplikacją na pierwszym planie (mediana):

  | na pierwszym planie | próbki | WindowServer CPU | WindowServer GPU |
  |---|---|---|---|
  | Brave | 57 | 41% | 4% |
  | Terminal | 7 | 39% | 1% |
  | QuickTime Player | 7 | 39% | 3% |
  | Discord | 7 | 38% | 6% |
  | Orca | 2 | 37% | 3% |

- To nie jest koszt składania klatek konkretnej aplikacji, bo GPU WindowServera ledwo
  pracuje. Te 40% CPU to stały koszt, który nie zależy od aplikacji na pierwszym planie.
  Mogą to być programy, które bez przerwy pytają WindowServer o okna (podglądy okien w
  DockDoor, Mos, nakładki), albo coś, co animuje się bez przerwy przy 120 Hz (ekran ma
  ProMotion 1728x1117 przy 120 Hz).
- Bez roota nie da się podejrzeć, kto wysyła te zapytania (`sample WindowServer` wymaga
  roota). Sprawdzić to można, zamykając podejrzanych po kolei na minutę i patrząc na
  `perf.py bench gpu`.
- Panel ClaudeAcc nie jest przyczyną. W oknach po 5 s skacze do 55% CPU (na rdzeniach E),
  a WindowServer w tych samych oknach stoi na 38-47%.
- Wszystkie uruchomione aplikacje na Electronie mają wersję 42 lub nowszą, więc błąd
  `_cornerMask` z Tahoe ich nie dotyczy (poprawka weszła w 36.9.2, 37.6.0 i 38.2.0). Na
  dysku są dwie stare aplikacje, MyMonero (Electron 9) i Upscayl (27). Nie były
  uruchomione, ale po starcie trzymałyby WindowServer.

### RAM (kontekst dla CPU)

Footprint moich procesów to 34,2 GB. Największe pozycje:

| co | footprint |
|---|---|
| maszyna Dockera | 8,0 GB |
| Brave | 4,8 GB |
| 9 procesów Claude Code | 3,3 GB |
| dev serwery | 3,2 GB |
| Orca | 2,5 GB |

21 kontenerów używa razem 3,7 GB (`docker stats`). VM ma przydział 8 GB i trzyma
całe 8 GB. Virtualization.framework nie oddaje pamięci, którą raz dotknął gość.

## Ślepe zaułki na tym Macu

| pomysł | dlaczego nie |
|---|---|
| L4S na siłę (`net.inet.tcp.l4s_developer=1`, `defaults write -g network_enable_l4s`) | zmierzone wyżej: z serwerem bez AccECN upload 218 -> 97 Mb/s, responsiveness 298 -> 1032 ms; na trasie nikt nie znakuje L4S (Orange Polska też nie ma wdrożenia) |
| ogranicznik wysyłania przy BE230 | sieć dokłada pod obciążeniem 1-4 ms, nie ma czego ratować; ma sens tylko przy Zyxelu (patrz rekomendacje) |
| `ifconfig netem output bandwidth` | to kolejka FIFO przed fq_codel, czyli zły bufor; do SQM nadaje się tylko `tbr` |
| `ifconfig throttle` | zawiesza wyłącznie klasę BK_SYS, nie jest limitem pasma |
| QoS tła dla renderów GPU | zmierzone wyżej: CPU na rdzenie E, udział w GPU bez zmian |
| wstrzymywanie renderów GPU, kiedy pracujesz | p99 małych zleceń lepsze tylko o 1-2 ms; nie warte zatrzymywania pracy innego agenta |
| Multipath TCP (Wi-Fi + hotspot) | tryb aggregate jest tylko dla aplikacji Apple (`net.inet.mptcp.allow_aggregate`), a api.anthropic.com i tak nie mówi MPTCP |
| Wi-Fi 6E | ani Zyxel (Wi-Fi 5), ani BE230 (2,4 + 5 GHz) nie mają 6 GHz |
| NCM zamiast ECM | RTL8153 nie ma trybu NCM; boot-arg wracający do starego kexta wymaga obniżenia zabezpieczeń |
| flagi GPU w Brave | ANGLE Metal jest domyślne od M129, a Skia Graphite od około M138; Brave 1.96 jest na Chromium 154 |
| Game Mode dla aplikacji, które nie są grami | działa tylko dla gry na pełnym ekranie, a przy tym spycha procesy w tle, czyli agentów |
| `NSAppSleepDisabled` dla Orki | gdy na pierwszym planie był Brave albo Discord, Orca i jej główny renderer miały priorytet 46-47, a nie 4 (App Nap); nie ma czego wyłączać |
| `debug.lowpri_throttle_enabled=0` | przyspiesza tylko IO w tle (Time Machine) kosztem pierwszego planu |
| sysctl TCP, dummynet, LEDBAT, pacing | zmierzone w poprzednich sesjach, bez zysku |
| podnoszenie `maxfiles`, `maxproc`, `ptmx_max`, `somaxconn` | zużycie to 5-8% limitów; agenci mają `ulimit -n` 1048576 |
| App Nap i koalescencja timerów dla Orki | zmierzone: priorytet i wybudzenia Orki bez zmian, gdy jest pod spodem |
| wyższy QoS dla `claude` i node agentów | już mają zwykły priorytet (31); podnieść ponad zwykły może tylko root, a w tle i tak siedzą ci, którym to nie szkodzi |
| `NODE_COMPILE_CACHE` dla samego `claude` | to binarka Buna, cache Node jej nie dotyczy; `claude --version` trwa 10 ms |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | według dokumentacji wyłącza też autoaktualizacje i Remote Control, czyli funkcje, z których korzystasz; telemetria i raporty błędów są już wyłączone osobno |
| `USE_BUILTIN_RIPGREP=0` | dokumentacja podaje to jako obejście zgodności, nie przyspieszenie |
| async dla hooków Orki | Orca zarządza tymi wpisami sama i pokazuje z nich stan agentów; to nie nasze |
| `feature.manyFiles` / `index.skipHash` | na klonie portivo bez zysku względem samego untrackedCache, a skipHash myli narzędzia na libgit2 |
| Orca w Narzędziach deweloperskich skryptem (`sqlite3` do TCC.db pod sudo) | SIP chroni systemowy TCC.db także przed rootem („attempt to write a readonly database”); `tccutil` tylko kasuje, profil PPPC nie zna DeveloperTool i wymaga MDM; zostaje „+” w Ustawieniach |
| `spctl --master-disable` zamiast listy Narzędzi | wyłącza Gatekeepera dla wszystkiego, co przychodzi z sieci, nie tylko dla binarek agentów; Narzędzia deweloperskie dają ten sam zysk tylko dla Orki |
| wykluczenia Time Machine | Time Machine nie ma tu skonfigurowanego dysku |
| przeniesienie magazynu pnpm do `.noindex` | pnpm zapisuje ścieżkę magazynu w każdym projekcie i po zmianie każe reinstalować |

## Rekomendacje, od największego efektu

Najpierw praca agentów, potem ogólne.

- **Orca w Narzędziach deweloperskich** (`claude-acc perf-root devtools add` w Terminalu
  i jedno „+” w Ustawieniach): pierwsze uruchomienie każdej nowej binarki testu Go w
  terminalu agenta spada ze 196 do 4,1 ms (około 48 razy), a `go test -run ^$` w
  auth-service z 2,67-2,83 do 2,26-2,46 s. Działa po restarcie Orki. Włączone
  2026-10-04: wpis o 22:13, restart Orki o 22:16. Szczegóły w sekcji Gatekeeper.

0. **Faza 3, na żywo od 2026-10-04 21:32** (włączone przez `perf.py apply`, jeszcze nie
   przez Ultrę):
   - **`fast-npx-hooks`:** hooki formatowania bez wolnego npx.
   - **`claude-limits`:** sufit Bash 1 h.

   Po scaleniu i instalacji `hooks/` musi trafić obok `perf.py`. Potem `claude-acc perf
   undo fast-npx-hooks claude-limits && claude-acc perf ultra on` przenosi obie poprawki
   pod Ultrę. Działają tak samo, ale panel pokaże je w karcie Ultry z wynikami „przed” i
   „po”.

1. **`perf.py ultra on`** (włączone 2026-10-04 wieczorem). Robi jednocześnie:
   - **cavemem worker w tle:** rdzeń P 32% -> 0,1%;
   - **hooki cavemem w tle:** PostToolUse 53 -> 24 ms p50 na każde narzędzie, działa od
     razu także w otwartych sesjach;
   - **`NODE_COMPILE_CACHE`:** `require('typescript')` 96 -> 49 ms;
   - **ciaśniejsze limity strażnika:** 25% RAM na wszystkie dev serwery, 4 GB na jeden.

   Szczegóły i kształt JSON są w sekcji Ultra.
   - Żeby Ultra pilnowała nowych pidów po restarcie, trzeba zainstalować szablon
     `launchd/com.filip.claude-acc.perf.plist.template` (`perf.py keep` przy starcie i co 5
     minut). Tego jeszcze nie zrobiłem.
   - Lepsza poprawka cavemem jest u źródła: worker nie powinien skanować 1,9 GB bazy bez
     końca (cavemem 0.3.0, `idleShutdownMs` nie działa, gdy sesje bez przerwy zapisują).
2. **`sudo ./perf-root.sh vnodes trial`:**
   - cache vnode jest pełny i rotuje 250-1500 vnode/s, a drzewa node_modules worktree nie
     mieszczą się w nim nawet pojedynczo;
   - trial mierzy lstat przed i po zmianie i wszystko cofa; jeśli drugi przebieg spadnie
     wyraźnie poniżej 3,6 s, warto zostawić (`--keep`, do restartu).
3. **`git-speed` dla portivo:**
   - dopisz `"git_repos": "orca"` (albo `["~/Documents/portivo-app/Untitled"]`) do
     `~/.local/share/claude-acc/perf.json` i puść `perf.py ultra on` jeszcze raz;
   - `git status` spadnie z ~76 do ~26-31 ms we wszystkich worktree.
4. **Spotlight:** Ustawienia > Spotlight > Prywatność wyszukiwania, dodaj `~/Library/pnpm`
   i `~/go` (razem 509 825 plików w indeksie). Opcjonalnie też
   `~/Documents/test-router/lms` (23 902 pliki, katalog `vendor` PHP). Każda instalacja
   pnpm z nowymi pakietami przestanie dokładać pracy mds; Ultra pokazuje to jako
   `spotlight-privacy` i sama zauważy, gdy liczba spadnie.
5. **devguard** (dla głównej sesji): szybka ścieżka w `admit` przed importami oszczędzi
   około 20 ms na każdym Bashu (44 ms teraz). Polling Orki przez CLI (19 razy na minutę,
   70-130 ms każde) warto zrzadzić albo przenieść na jedno wywołanie.
6. **Docker:**
   - zamykaj okno Docker Desktop, gdy go nie oglądasz: z otwartym oknem Docker odpala
     `docker stats` 18 razy na minutę;
   - `perf.py apply docker-vm` i restart Dockera, kiedy Ci pasuje, dadzą 2 GB RAM na stałe;
   - Docker VMM to osobna decyzja.
7. **Ogranicznik wysyłania przy Zyxelu** (`sudo ./perf-root.sh trial`).
   - Przy Orange najpierw `perf.py bench network`. Jeśli w linii „z tego sieć” opóźnienie
     przy wysyłaniu jest wyraźnie wyższe niż bez obciążenia (np. +100 ms i więcej), bufor
     Zyxela puchnie. Wtedy Ultra sama dopisze `shaper` do `pending_root`.
   - Wcześniejsze 0,9-2,2 s „responsiveness” przy wysyłaniu z tamtego łącza to suma sieci
     i kolejki w gnieździe. Przy BE230 ta suma wynosi 200-450 ms, a sama sieć tylko +1-4
     ms. Bez rozbicia nie wiadomo, ile z tamtych sekund siedziało w routerze.
   - `trial` mierzy, ustawia `ifconfig <if> tbr` na 90% zmierzonego uploadu, mierzy znowu
     i cofa (z `--keep` zostawia). Limit nie przetrwa restartu ani odłączenia adaptera.
8. **Memory Saver w Brave na „Maximum”** (Ustawienia, Wydajność):
   - teraz jest włączony, ale z `aggressiveness: 0`, a Brave ma 4,8 GB w 28 procesach;
   - zmiana w interfejsie działa od razu i jest odwracalna;
   - nie mierzyłem, bo zmiana pliku `Local State` wymaga zamknięcia Brave.
9. **WindowServer na 37-41% CPU niezależnie od aplikacji na pierwszym planie:**
   - sprawdź `perf.py bench gpu` przed i po zamknięciu na minutę podejrzanych: DockDoor,
     Mos, Music z animowanymi okładkami, animowane ikony w menu bar;
   - przełączenie ekranu na 60 Hz obetnie składanie klatek o połowę kosztem płynności,
     więc to wymiana, a nie przyspieszenie.

## Jak tego używać

```
perf.py ultra on|off|status [--json]                         # wszystko dla agentów naraz
perf.py bench [network|cpu|gpu|fs|agents|gatekeeper|all] [--runs N] [--json]   # pomiar, zapis w perf-state.json
perf.py status [--json]                                      # poprawki i ostatnie pomiary
perf.py list                                                 # poprawki z grupą i efektem
perf.py apply <nazwa> | --all [--dry-run]
perf.py undo <nazwa> | --all
perf.py keep                                                 # pilnowanie poprawek (launchd)
# pod rootem tylko z kopii roota (claude-acc root install, raz): claude-acc perf-root woła
# sudo /usr/local/libexec/claude-acc-root/root-run.sh perf-root ...
claude-acc perf-root vnodes trial [--value 786432] [--keep]   # cache vnode: pomiar przed i po
claude-acc perf-root vnodes apply|undo
claude-acc perf-root trial [--rate 27Mbps] [--keep]           # ogranicznik: pomiar przed i po
claude-acc perf-root shaper apply|undo                        # ./perf-root.sh shaper status bez sudo
./perf-root.sh devtools add|undo|status [--app <ścieżka do .app>; domyślnie host z orcahost.py]   # bez sudo, w Terminalu; "+" w Ustawieniach
```

Stan dla panelu jest w `~/.local/share/claude-acc/perf-state.json`:

- `applied`: włączone poprawki razem z tym, co trzeba cofnąć (pid i chwila startu procesu,
  więc ponownie użyty pid nie zostanie ruszony).
- `bench`: ostatni pomiar każdego rodzaju (`at`, `load` sprzed pomiaru, `result`).
- `history`: 30 ostatnich pomiarów każdego rodzaju. Wpis z `shaper` oznacza pomiar z
  włączonym ogranicznikiem i nie liczy się do jego limitu.
- `ultra`: przełącznik i jego wyniki (kształt w sekcji Ultra).
- `deferred`: cofnięcia, które czekają, aż Docker będzie zamknięty.

## Źródła

- Faza 4 (2026-10-08): ustawienia i zmienne Claude Code
  https://code.claude.com/docs/en/env-vars , https://code.claude.com/docs/en/settings-reference ,
  https://code.claude.com/docs/en/prompt-caching , https://code.claude.com/docs/en/sub-agents
- PEP 810 i Python 3.15: https://peps.python.org/pep-0810/ ,
  https://docs.python.org/3.15/whatsnew/3.15.html
- ONNX Runtime, wątki i spinning: https://onnxruntime.ai/docs/performance/tune-performance/threading.html
- SwiftPM, zmiana domyślnego build systemu:
  https://forums.swift.org/t/swiftpm-development-update-default-build-system-change/85548 ;
  SE-0547 (caching kompilacji): https://forums.swift.org/t/se-0547-swiftpm-support-for-compilation-caching/89191
- QoS i rdzenie E (`taskpolicy -b`): https://eclecticlight.co/2022/10/20/making-the-most-of-apple-silicon-power-5-user-control/
- MPTCP na macOS: https://www.mptcp.dev/macOS.html
- Low Data Mode a ruch na pierwszym planie:
  https://developer.apple.com/documentation/foundation/urlsessionconfiguration/allowsconstrainednetworkaccess

- Apple, Testing and debugging L4S in your app:
  https://developer.apple.com/documentation/network/testing-and-debugging-l4s-in-your-app
- XNU (tcp_prague.c, tcp_output.c, classq_subr.c, nx_netif.c) i network_cmds (ifconfig.c):
  https://github.com/apple-oss-distributions/xnu ,
  https://github.com/apple-oss-distributions/network_cmds
- IETF 123 CCWG, L4S impact on measured latency:
  https://datatracker.ietf.org/meeting/123/materials/slides-123-ccwg-l4s-impact-on-measured-latency-00
- RFC 9330 i testy L4S z fq_codel: https://github.com/heistp/l4s-tests
- Orange Polska i L4S (wątek klienta, brak wdrożenia):
  https://nasz.orange.pl/t5/Internet-domowy/L4S-w-Orange-%C5%9Awiat%C5%82ow%C3%B3d/td-p/422384
- networkQuality(8): https://keith.github.io/xcode-man-pages/networkQuality.8.html
- Chromium, preferencje Memory Saver:
  https://github.com/chromium/chromium/blob/main/components/performance_manager/public/user_tuning/prefs.h
- Brave, Memory Saver za słaby: https://github.com/brave/brave-browser/issues/47599
- Skia Graphite w Chrome: https://blog.chromium.org/2025/07/introducing-skia-graphite-chromes.html
- Electron, poprawka `_cornerMask`: https://github.com/electron/electron/pull/48376
- Rdzenie P i E a QoS: https://eclecticlight.co/2024/12/17/tune-for-performance-core-types/
- Docker Resource Saver: https://docs.docker.com/desktop/use-desktop/resource-saver/ ;
  Docker VMM: https://docs.docker.com/desktop/features/vmm/ ;
  pamięć VM na macOS: https://arcbox.dev/blog/macos-vm-memory-ratchet
- Claude Code: zmienne środowiskowe https://code.claude.com/docs/en/env-vars.md , hooki
  (`async`, czas, nowy proces na każde wywołanie) https://code.claude.com/docs/en/hooks.md ,
  MCP https://code.claude.com/docs/en/mcp.md
- Node.js, module compile cache (`NODE_COMPILE_CACHE`):
  https://nodejs.org/api/module.html#module-compile-cache
- git: `core.untrackedCache`, `core.fsmonitor`, `feature.manyFiles`:
  https://git-scm.com/docs/git-config
- RTL8153 bez NCM: https://lkml.kernel.org/netdev/20230106160739.100708-3-bjorn@mork.no/T/ ;
  ECM a NCM w praktyce: https://gist.github.com/MadLittleMods/3005bb13f7e7178e1eaa9f054cc547b0
