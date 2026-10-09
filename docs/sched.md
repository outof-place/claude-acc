# Scheduler Go, JS i natywnych buildów: stan dla panelu i historia biegów

`sched.py` wpuszcza ciężkie komendy agentów po pamięci zamiast jednego zamka na wszystko: Go
(build, vet, test, lint, cele make), JS (testy, e2e, buildy, typecheck, lint, w każdym projekcie z
`package.json`), natywne buildy iOS i Androida razem ze startem symulatora (sekcja „Natywne”) i
resztę ciężkiej pracy po kształcie komendy (sekcja „Reszta ciężkiej pracy”). Każdy bieg przechodzi
przez `sched.py run`; hook PreToolUse (`devguard.py admit`) sam owija komendy agentów Claude Code i,
po `sched.py codex install`, Codeksa; `plock.py go` przekazuje do niego swoje, a terminal, skrypty i
automatyzacje Orki wołają `claude-acc run -- KOMENDA`. Nikomu nie odmawia: najwyżej każe czekać. Pomiary, z których wzięły się
liczby niżej: `docs/perf-research.md`, sekcja o schedulerze.

Presja pamięci jądra (`kern.memorystatus_vm_pressure_level`): przy `critical` nic nie startuje,
przy `warn` startuje to, co mieści się w wolnej pamięci (także kilka jobów naraz), ale bez furtki
„sam na Macu po 30 s ponad pamięć”. macOS potrafi trzymać `warn` godzinami przy połowie wolnej
pamięci, a jeden job naraz robił z kolejki stary zamek: czekanie dłuższe niż 5 minut kasuje też
cache promptu subagenta.

Zakres joba: `tree` to cały moduł (`./...` w jego katalogu), `subtree` to wzorce z `...` na części
modułu (`./internal/push/...` albo `./...` w podkatalogu), `pkg` i `handlers` to jeden pakiet, a `pkgs`
kilka. Przewidywanie dla `subtree` leży między jednym pakietem a całym modułem, w proporcji do liczby
pakietów (bez `testdata`, `vendor` i zagnieżdżonych modułów); poddrzewo z `internal/handlers` waży co
najmniej tyle co on. Na Depot `subtree` idzie przez `depot-exec` z komendą agenta, bo joby CI
(`full`, `handlers`) testują stały zestaw pakietów.

Mały job (`small`, niżej) startuje, gdy mieści się w pamięci dostępnej teraz, nawet jeśli
`free_for_admission_gb` jest zjedzone przez rezerwy długich jobów na wzrost, którego jeszcze nie ma.
Od tej pamięci odejmują się tylko prognozy świeżo wpuszczonych małych jobów. Głowa kolejki czekająca
dłużej niż `starve_s` zostawia sobie miejsce i tutaj, a po `2 × starve_s` jej rezerwacja jest twarda:
żaden mały job jej już nie wyprzedza, więc strumień krótkich testów nie zagłodzi dużego.

Rezerwacja nie trzyma jednak pamięci, której głowa jeszcze nie użyje (backfill w stylu EASY). Job za
zablokowaną głową, także nie mały, startuje, gdy mieści się w `free_for_admission_gb`, ma prognozę z
własnych biegów (`predicted_from: history:N`) i nie opóźni startu głowy:

- start głowy da się przewidzieć (każdy job, na którego koniec czeka, ma prognozę z historii i jeszcze
  jej nie przekroczył): job skończy się przed nim z zapasem (`2 × predicted_wall_s + 10 s`), albo w
  chwili jej startu zmieści się obok niej (wolne wtedy minus to, czego potrzebuje głowa);
- nie da się (czeka na job ze zgadniętą prognozą albo taki, który biegnie dłużej, niż miał): tylko
  lekki job (`small_gb`), którego `2 × predicted_wall_s + 10 s` mieści się w `head_delay_s`, i tylko
  póki głowa nie zmieściłaby się nawet bez jobów, które ją już wyprzedziły (`passed` w `running[]`).
  Opóźni ją najwyżej o swój czas; gdy to wyprzedzający trzymają jej pamięć, nikt więcej nie wchodzi
  i głowa startuje, gdy się skończą.

Lekki job (`small_gb`), który skończy się przed głową albo wchodzi jako krótki, wystarczy, że zmieści
się w pamięci dostępnej teraz, jak mały job szybką ścieżką, także po `2 × starve_s`: rezerwy na
wzrost długich jobów i natywnego buildu spoza schedulera potrafią zepchnąć `free_for_admission_gb`
poniżej zera przy kilkunastu GB dostępnych (2026-10-09 18:20, 1.25.1: ruff i pytest po 0,1 GB stały
wtedy za `next build`). Miejsce obok głowy liczy się zawsze w pamięci po rezerwach.

Natywne buildy i symulatory nie wchodzą przez backfill, a za natywną głową wstrzymaną przez strażnika
backfillu nie ma. 2026-10-09 głowa `next build` (11,7 GB przy 5,9 wolnych) trzymała tak po
`2 × starve_s` 25 krótkich jobów (ruff, `go vet` jednego pakietu, skrypty Pythona) do 25 minut.

Pliki w `~/.local/share/claude-acc/sched/`:

| plik | kto pisze | po co |
|---|---|---|
| `state.json` | każdy proces `sched.py run`, pod `flock` na `lock` | stan dla panelu: co biegnie, kto czeka i dlaczego, pamięć, bilans dnia |
| `history.jsonl` | `sched.py run` na końcu biegu; `scripts/depot-cost.py history` w portivo dopisuje biegi Depot | jeden wiersz na skończony bieg, z wejściami decyzji o trasie |
| `lock` | wszyscy | `fcntl.flock` na czas czytania i zapisu stanu |
| `depot.json` | `sched.py depot`, pod `flock` na `depot.lock` | biegi Depot CI całej organizacji dla karty (niżej) |
| `config.json` | człowiek | nadpisania domyślnych ustawień (niżej) |

## Kiedy `state.json` się zmienia

- Od razu przy każdej zmianie stanu: zgłoszenie joba, wpuszczenie, wyprzedzenie, decyzja o Depot,
  start i koniec biegu Depot, pauza i wznowienie, koniec joba.
- Co 1 s, dopóki coś biegnie albo czeka: pamięć jobów, czas, ETA, pamięć systemu. Pisze proces,
  który w tej sekundzie dostał `flock`; reszta tę sekundę pomija.
- Bez pracy nikt nie pisze. `idle_since` mówi, od kiedy. Karta liczy wtedy miernik sama z
  `kern.memorystatus_level` (sysctl) i bierze z pliku tylko `today` i `recent`.
- Zapis atomowy: `state.json.tmp`, potem `rename`. Karta czyta przy zmianie pliku (FSEvents) i co
  1 s, gdy `running` albo `queue` nie są puste.
- `today` zeruje się przy pierwszym zapisie po północy czasu lokalnego.

Wszystkie czasy to epoch w sekundach (float), pamięć w GB (1 GB = 2^30 bajtów), pieniądze w USD.
Pole, którego nie ma, ma wartość `null`. Klucze są stabilne; nowe mogą dojść, istniejące nie
zmienią znaczenia bez podbicia `version`.

## `state.json`, wersja 1

```
version            1
updated_at         epoch ostatniego zapisu
idle_since         epoch, gdy running i queue są puste; inaczej null
host               ram_gb, cores_p, cores_e
config             headroom_gb (4), lambda_s_per_unit (6), unit_usd (0.006),
                   small_gb (4.5), small_wall_s (120)
memory             patrz niżej
running[]          joby, które biegną (lokalnie albo na Depot)
queue[]            joby, które czekają, w kolejności wpuszczania
overtakes[]        10 ostatnich wyprzedzeń
recent[]           8 ostatnich skończonych jobów
today              bilans dnia
```

### `memory`

Pasek w karcie to suma `others_gb + jobs_now_gb + reserved_gb + devserver_reserve_gb +
free_for_admission_gb + headroom_gb = host.ram_gb` (po zaokrągleniu).

| pole | znaczenie |
|---|---|
| `level_pct` | `kern.memorystatus_level`: procent pamięci, który jądro uważa za dostępny |
| `available_gb` | `level_pct / 100 × ram_gb` |
| `headroom_gb` | zapas, którego scheduler nie rusza (`config.headroom_gb`) |
| `devserver_reserve_gb` | miejsce na dev serwery, z `devguard-state.json`: `min(max_server_gb, budżet devguarda - dev serwery teraz)`, ale nie mniej niż powrót chronionych i przypiętych serwerów (których strażnik nie zatrzyma) do ich zmierzonego szczytu: pole `regrow` strażnika, czyli suma `max(0, szczyt procesu - procesu teraz)` po procesach serwera, najwyżej `max_server_gb` na serwer |
| `jobs_now_gb` | suma `mem_now_gb` lokalnych jobów |
| `reserved_gb` | o ile lokalne joby jeszcze urosną: suma `max(0, mem_predicted_gb - mem_now_gb)`, plus `native.reserve_gb` |
| `others_gb` | `ram_gb - available_gb - jobs_now_gb`: inne aplikacje i system |
| `free_for_admission_gb` | `available_gb - headroom_gb - devserver_reserve_gb - reserved_gb` |
| `idle_max_gb` | ile zmieściłoby się na pustym Macu: najwyższy `available_gb` z ostatnich 7 dni minus `headroom_gb`; większe joby idą zawsze na Depot |
| `swap_used_gb`, `swap_growth_2m_gb` | swap teraz i jego przyrost w 2 minuty |
| `pressure` | `normal`, `warn`, `critical` z poziomu jądra (`kern.memorystatus_vm_pressure_level`) |
| `guard_level` | presja wg strażnika dev serwerów (0, 1, 2) z jego pomiaru młodszego niż minuta, albo `null`; liczy też rosnący swap, więc jej `2` wstrzymuje natywne joby |
| `native` | `{build_gb, active, outside, reserve_gb, owner, owner_label}`: natywny build na Macu teraz. `active`: xcodebuild z akcją, która kompiluje, albo usługa buildów Xcode, pod którą biegnie kompilator (`swift-frontend`, `clang`, `ld`, `actool`…); sama otwarta usługa z pomocnikami buildem nie jest; `outside`: build spoza schedulera (nikt z `running[]` nie trzyma miejsca); `reserve_gb`: dla buildu z zewnątrz `max(0, przewidywany szczyt buildu - build_gb)`; `owner`: id joba, który trzyma miejsce |
| `simulators` | `{booted, in_use, cap, gb, holders[]}` z pomiaru devguarda albo `null` bez świeżego pomiaru; `in_use`: żywa dzierżawa portivo-mobile, widz albo symulator człowieka |
| `brake` | stopień hamulca strażnika: `normal`, `tight`, `brake`, `emergency` (niżej) |
| `long_lived_gb`, `long_lived[]` | pamięć długo żyjących procesów ze stanu strażnika: dev serwery, symulatory, expo/metro, watchery, headless przeglądarki, LSP, Docker (`{family, gb, count}`); jest już w `others_gb`, tu z nazwy; `null` bez świeżego stanu strażnika |

### Job w `running[]` i `queue[]`

| pole | znaczenie |
|---|---|
| `id` | `j-<epoch>-<4 hex>` |
| `class` | Go: `<moduł>:<czasownik>:<zakres>[:race][:compile]`, np. `charter-service:vet:tree`, `charter-service:test:pkg:internal/handlers:compile`; JS i natywne: patrz ich sekcje |
| `lang` | `go`, `node` albo `native` |
| `kind` | `build`, `vet`, `test`, `lint`, `make`, `generate`, `run`, `other`; JS i natywne: patrz ich sekcje |
| `label` | komenda Go bez otoczki (`cd`, `rtk proxy`, potoki), do wyświetlenia |
| `module` | katalog modułu Go względem repo, np. `apps/charter-service` |
| `repo` | nazwa repo (katalog z `.git`), np. `Untitled` |
| `cmd` | pełna komenda agenta |
| `agent` | `{session, name, worktree, pane}`: sesja Claude, nazwa podagenta, jeśli jest, worktree, panel Orki (`ORCA_PANE_KEY`) |
| `where` | `local` albo `depot` (w kolejce: trasa, jeśli już zapadła, inaczej `null`) |
| `route` | decyzja o trasie, patrz niżej |
| `p`, `p_by` | `-p` biegu i kto je wybrał: `scheduler`, `agent`, `default`, `depot` (runner Depot ustawia `-p` = rdzenie) |
| `mem_predicted_gb` | przewidywany szczyt pamięci |
| `predicted_wall_s` | przewidywany czas biegu lokalnie |
| `small` | czy job jest mały (`mem_predicted_gb ≤ small_gb` i `predicted_wall_s ≤ small_wall_s`): może wyprzedzać |
| `count1_dropped` | scheduler zdjął `-count=1` (wynik może przyjść z cache testów), patrz niżej |
| `native_tool`, `exclusive` | natywny job: narzędzie (`xcodebuild`, `portivo-mobile`, `simulator`…) i czy zajmuje jedyne miejsce na natywny build |

Tylko w `running[]`:

| pole | znaczenie |
|---|---|
| `started_at`, `elapsed_s` | start biegu (po wpuszczeniu) i czas od startu |
| `eta_s`, `progress` | `max(0, predicted_wall_s - elapsed_s)` i `min(0.99, elapsed_s / predicted_wall_s)` |
| `mem_now_gb`, `mem_peak_gb` | suma `phys_footprint` procesów joba (drzewo i grupa procesów) teraz i najwięcej do tej pory; 0 dla Depot |
| `cpu_cores` | średnio zajęte rdzenie od startu (CPU / czas) |
| `paused`, `pause_reason` | SIGSTOP przy rosnącym swapie; tekst, np. `swap +0,6 GB in 2 min` |
| `passed` | id joba, który ten job wyprzedził (backfill sprawdza, czy to wyprzedzający trzymają pamięć głowy) |
| `depot` | dla `where=depot`: `{target, job, cores, run_id, url, units, cost_usd}`; `target` to `depot-exec` albo `depot-ci`; `units` i `cost_usd` rosną w trakcie |

Tylko w `queue[]`:

| pole | znaczenie |
|---|---|
| `position` | 1 = następny do wpuszczenia |
| `enqueued_at`, `waited_s` | kiedy się zgłosił i ile już czeka |
| `eta_start_s` | przewidywane sekundy do startu |
| `reason` | `{code, need_gb, free_gb, after[], text}`; `code`: `memory` (czeka na pamięć), `head` (mieści się, ale mógłby opóźnić start joba, który czeka najdłużej, patrz backfill wyżej), `pressure` (devguard: presja), `deciding` (scheduler jeszcze liczy trasę), `native` (inny natywny build trzyma miejsce), `simulators` (`portivo-mobile up` czeka na wolny symulator); `after` to id jobów, na których koniec czeka |

### `route`

Trasa minimalizuje `czas do wyniku + λ × jednostki Depot`:

1. Mieści się teraz (`mem_predicted_gb ≤ free_for_admission_gb`): lokalnie, bez czekania, chyba
   że ciężki job (ponad `small_gb`) jest na Depot o więcej szybszy, niż kosztuje:
   `predicted_wall_s - depot_eta_s > lambda × units` (cały `internal/handlers`: ~25 min lokalnie,
   8 min na Depot za $0,19).
2. Nie zmieści się nawet na pustym Macu (`mem_predicted_gb > idle_max_gb`): zawsze Depot.
3. Musi czekać: lokalnie, chyba że `local_eta_s - depot_eta_s > lambda × units`.
   `local_eta_s` to czekanie (aż przewidywany koniec blokujących jobów zwolni pamięć) plus bieg
   lokalnie; `depot_eta_s` to p50 z `scripts/depot-cost.py eta --json` dla tej klasy Depot razem
   z przygotowaniem; `units = cores / 2 × minuty Depot`.

Na Depot nigdy nie idzie komenda, która tam dałaby inny wynik niż tutaj. Depot dostaje drzewo repo
(pliki śledzone i nieśledzone bez `.gitignore`) pod inną ścieżką, bez zmiennych z komendy, i odsyła
tylko wyjście. Lokalnie zostaje więc komenda ze ścieżką bezwzględną, z `~` albo `$`, ze ścieżką
względną poza repo albo ignorowaną, z flagą piszącą plik (`-o`, `-c`, `-coverprofile` i inne
profile, `-trace`, `-outputdir`), ze zmienną w prefiksie albo z `GOFLAGS` innym niż `-p`/`-count`.
Trasa mówi wtedy `local only (<powód>)`, także gdy job nie zmieści się na pustym Macu.

| pole | znaczenie |
|---|---|
| `choice` | `local` albo `depot` |
| `why` | `fits`, `waits`, `cost`, `cannot_fit`, `no_depot` (brak trasy Depot dla tej komendy), `agent` (agent sam wskazał Depot) |
| `local_eta_s`, `depot_eta_s`, `units`, `cost_usd`, `lambda`, `saves_s` | wejścia decyzji; `saves_s = local_eta_s - depot_eta_s` |
| `text` | gotowe zdanie do karty, po angielsku, np. `local, fits`, `local: waits 40s, Depot would cost $0.40`, `Depot: saves 3m40s for $0.01`, `Depot: needs 34 GB, Mac max ~27` |

### `overtakes[]`, `recent[]`, `today`

| pole | znaczenie |
|---|---|
| `overtakes[]` | `{at, id, label, passed, passed_label, mem_gb, wall_s}`: mały job wpuszczony przed `passed` |
| `recent[]` | `{id, label, where, rc, finished_at, wall_s, peak_gb, predicted_gb, waited_s, cost_usd, route_text, depot_run_id}` |
| `today.jobs_local`, `today.jobs_depot` | liczba skończonych jobów |
| `today.wait_s` | suma prawdziwego czekania |
| `today.old_lock_wait_s` | czekanie jobów Go przy starym zamku `plock go` (jeden job Go naraz): wirtualny zamek w kolejności przyjścia (start = max(przyjście, zwolnienie poprzedniego), z prawdziwymi czasami biegów). Tylko joby Go, bo tylko one szły przez ten zamek; job skończony wcześniej niż starszy od niego czeka w `_internal.old_lock.pending`, aż ten się skończy |
| `today.go_wait_s` | prawdziwe czekanie tych samych jobów Go |
| `today.wait_saved_s` | `old_lock_wait_s - go_wait_s` |
| `today.depot_units`, `today.depot_cost_usd` | zużycie Depot przez scheduler |
| `today.local_kept_usd` | koszt Depot, którego uniknęły lokalne biegi klas, które stary hak `depot-heavy-go.sh` wysyłał na Depot |
| `today.overtakes`, `today.pauses`, `today.peak_concurrency`, `today.max_reserved_gb` | liczniki dnia |

## `depot.json`: biegi Depot CI spoza schedulera

Scheduler zna tylko joby, które sam wysłał na Depot. Bramka pushu (`.husky/pre-push`, workflow
`gates`) i `scripts/depot-ci.sh` albo `depot-exec.sh` odpalone wprost przez agenta omijają go
(`SKIP_MARKERS`), więc karta Builds bierze je z API Depot. `sched.py depot [--max-age S] [--json]`
pyta `depot ci run list` o 15 ostatnich biegów, bieg biegnący przy każdym odczycie o
`depot ci status`, a skończony raz o `ci status` i `ci metrics --run` (czasy); potem leży w pliku.
Aplikacja woła komendę, dopóki panel jest otwarty: co 15 s, gdy coś biegnie, inaczej co minutę;
z `--max-age` komenda nie idzie do sieci, gdy plik jest młodszy.

```
version, checked_at   epoch ostatniego odczytu
error                 ostatnia linia błędu CLI (brak `depot`, brak logowania) albo null
running[]             biegi queued/running w kształcie joba z `running[]`: where=depot, kind=ci,
                      label "workflow · joby", depot {target: ci, run_id, url}, elapsed_s, eta_s
                      i progress z mediany zielonych biegów tej samej etykiety
recent[]              do 6 skończonych w kształcie `recent[]`: rc 0 (finished), 1 (failed),
                      130 (cancelled), url do joba na depot.dev (czerwony pierwszy)
_runs                 szczegóły biegów po run_id; skończone (`final`) nie są pytane drugi raz
```

Biegi, które scheduler sam wysłał (ich `run_id` w `running[].depot` albo `recent[].depot_run_id`
w `state.json`), w `depot.json` się nie powtarzają. Karta pokazuje biegnące razem z jobami
schedulera, a skończone w sekcji „Depot CI”; plik starszy niż 2 min nie daje wierszy biegnących.
Organizację wybiera `depot_org` w `config.json` (pusta: domyślna organizacja CLI).

## Kiedy scheduler zdejmuje `-count=1`

Tylko w iteracji agenta (komenda owinięta przez hook), tylko dla `go test` jednego albo kilku
pakietów bez `-race` i flag spoza zestawu cache'owalnego, nigdy dla celów make. Cache testów Go
śledzi pliki i zmienne środowiska, które czyta sam proces testu; nie widzi tego, co czyta
uruchomiony przez niego program (git, node, `atlas migrate lint`). Dlatego `-count=1` zostaje,
gdy `exec.Command`, `exec.CommandContext` albo `os.StartProcess` (także przez alias importu
`os/exec`) stoi w pakiecie, w jego testach albo w pakiecie modułu ściąganym tylko przez testy
(pomocniki testów i ich zależności). Pakiety z bazą przechodzą: szablon testpg czyta migracje,
Dockerfile Postgresa i atlasa w procesie testu, więc jego pliki (`count1_trusted_exec`) się nie
liczą. Wynik jest w cache po podpisie plików; `go list` biegnie dopiero po zmianie `go.sum` albo
plików pakietu. Ta sama analiza mówi, czy testy sięgają po Postgresa: wtedy `depot-exec` dostaje
`--with pg`.

## `history.jsonl`

Jeden wiersz JSON na skończony bieg, dopisywany na końcu pliku. Wiersze `where=depot` dopisuje też
`scripts/depot-cost.py history --out …` (portivo), z deduplikacją po id próby.

```
ts, id, where (local|depot), class, module, repo, label, p, peak_gb, sys_drop_gb, wall_s, cpu_s,
wait_s, rc, agent, worktree, depot_run_id, runner, units, cost_usd,
choice, why, local_eta_s, depot_eta_s, lambda, predicted_gb, predicted_wall_s, count1_dropped,
native_built
```

`native_built` (tylko joby natywne): czy w trakcie biegu naprawdę ruszyła kompilacja; `portivo-mobile
up` z gotowym klientem w cache jej nie ma.

`class` dla wierszy z Depot CI to `go-heavy/<job>`, dla `depot-exec` to `depot-exec-<cores>/exec`.
Z tych wierszy scheduler uczy się przewidywań (p90 szczytu i mediana czasu na klasę i `-p`) i
można z nich stroić λ.

## `config.json`

Inny plik konfiguracji (np. na jedną próbę) wskazuje zmienna `SCHED_CONFIG`.

```
headroom_gb          4      zapas pamięci, którego scheduler nie rusza
lambda_s_per_unit    6      ile sekund czekania jest warta jedna jednostka Depot ($0,006)
small_gb             4.5    mały job: tyle GB lub mniej...
small_wall_s         120    ...i tyle sekund lub mniej; mały może wyprzedzać
starve_s             120    po tylu sekundach czekania job rezerwuje pamięć; wyprzedza go już tylko backfill
head_delay_s         60     gdy startu głowy nie da się przewidzieć, wyprzedza ją tylko lekki job,
                            którego 2 × prognoza + 10 s mieści się w tylu sekundach
drop_count1          true   zdejmuj -count=1 w iteracji agenta dla pakietów bez bazy
pause_swap_gb        0.5    przyrost swapu w 2 min, przy którym najmłodszy ciężki job dostaje SIGSTOP
depot_eta_since      "2026-10-05"   od kiedy brać czasy z `depot-cost.py eta` (rozmiary maszyn)
count1_trusted_exec  ["internal/testhelpers/testpg"]   pliki pomocników, których exec nie psuje cache
depot_org            ""     organizacja Depot dla `sched.py depot`; pusta: domyślna organizacja CLI
node                 true   testy, buildy i typecheck JS w kolejce
native               true   natywne buildy, pody i start symulatora w kolejce
generic              true   reszta ciężkiej pracy po kształcie komendy (niżej)
```

## JS

Hook owija komendy, które kończą się same i zjadają pamięć: `vitest`, `jest`, `playwright test`,
`next build`, `vite build`, `tsc`, `vue-tsc`, `eslint`, `turbo run`, także przez `npx`, `bunx`,
`pnpm|yarn|bun exec|dlx`, `npm exec` i `node_modules/.bin/`, oraz skrypty menedżera pakietów
(`pnpm test`, `npm run test:unit`, `yarn build`, `bun run typecheck`, `pnpm -r --filter web test`),
których nazwa pasuje do rodzaju: `test`, `e2e`, `build`, `typecheck`, `lint`, `check`. Nigdy:
skrypty i flagi, które się nie kończą albo czekają na człowieka (`dev`, `watch`, `serve`, `start`,
`preview`, `storybook`, `--ui`, `-w` w `tsc` i `vitest`), instalacje i wszystko poza katalogiem z
`package.json` (szukanym w górę, do `.git`).

Klasa: `<repo>:<rodzaj>:<pakiet albo filter=X>:<narzędzie>[:all][:filtered]`, np.
`shop:test:apps/web:vitest`. `<repo>` to nazwa głównego repo także w jego worktree (z pliku `.git`
worktree), więc każde drzewo uczy się z tej samej historii. `all` to bieg na całym monorepo (`-r`, `--workspaces`, każde
`turbo run`), `filtered` to wybrane testy (`-t`, `--grep`, `--project`, pliki w argumentach). Do pierwszych
biegów klasy przewidywanie bierze się z tabeli (GB, sekundy): `test` 3,0/90, `e2e` 3,5/180, `build`
4,0/180, `typecheck` 2,5/60, `lint` 2,0/60, `check` 3,0/120; `all` razy 1,5 GB i 2 czasu, `filtered`
połowa GB (najmniej 1) i 0,4 czasu. Potem p90 z historii, jak w Go. Joby JS biegną tylko lokalnie:
nie idą na Depot i nie dostają Postgresa. `"node": false` w `config.json` wyłącza całą tę część.

## Natywne

2026-10-08 Mac zamarzł o 18:57: build iOS w Release (18:12-18:30) i build klienta deweloperskiego
przez `portivo-mobile up` (od 18:48, przy swapie 11 GB i rosnącym) nie przeszły przez żadną bramkę
pamięci. Build aplikacji Expo xcodebuildem z pełną równoległością to 6-12 GB. Hook owija więc
także:

| narzędzie | komendy | rodzaj | GB, s na start | pamięć poza drzewem |
|---|---|---|---|---|
| `xcodebuild` | build, test, archive, analyze, build-for-testing, test-without-building, także bez akcji i przez `xcrun` | build | 10, 600 | nie |
| `expo-run` | `expo run:ios`, `expo run:android` (każda droga do CLI expo) | build | 10, 900 | tak: symulator, demon Gradle |
| `react-native-run` | `react-native run-ios`, `run-android`, `build-ios`, `build-android` | build | 10, 900 | tak |
| `eas-local` | `eas build --local` (bez `--local` build idzie w chmurze) | build | 10, 1500 | nie |
| `gradle` | `./gradlew` i `gradle` z zadaniem assemble*, bundle*, install*, build | build | 6, 600 | tak: demon Gradle |
| `portivo-mobile` | `portivo-mobile up <app>`: dzierżawa i start symulatora, czasem 10-20 min buildu w odczepionym procesie | build | 10, 900 | tak |
| `pod` | `pod install`, `pod update`, `pod-install`, także z `arch` i `bundle exec` | pods | 1,5, 180 | nie |
| `expo-prebuild` | `expo prebuild` (z `pod install` w środku) | pods | 2, 180 | nie |
| `simulator` | `xcrun simctl boot`, `xcrun simctl bootstatus … -b`, `open -a Simulator` | simulator | 2,5, 30 | tak: launchd_sim |

Klasa: `<repo>:native:<narzędzie>[:<akcja, platforma albo aplikacja>]`, np.
`portivo:native:xcodebuild:build`, `portivo:native:portivo-mobile:storefront-mobile`; symulator to
zawsze `mac:native:simulator`. Nigdy: informacje, eksport i pobieranie w xcodebuild (`-version`,
`-list`, `-showBuildSettings`, `-exportArchive`, `-downloadPlatform`…), sam `clean`, pomoc i wersje,
`simctl` bez startu, `eas build` w chmurze, inne zadania Gradle, `portivo-mobile status|release`.

Zasady wpuszczania różnią się od Go i JS w trzech miejscach:

- start tylko w pamięci wolnej po rezerwach biegnących jobów (sam na Macu: dostępna minus zapas),
  nigdy „po 30 s ponad pamięć”, jak samotny job Go: SIGSTOP, który go wtedy pilnuje, nie sięga
  symulatora ani demona Gradle;
- mały natywny job (start symulatora) nie korzysta z pamięci „dostępnej teraz” mimo rezerw długich
  jobów: build, który właśnie wystartował, rośnie do swojej prognozy;
- `guard_level` 2 wstrzymuje natywne joby, także gdy jądro mówi `normal` (przy pełnym i rosnącym
  swapie potrafi tak mówić do samego końca). Powód w kolejce: `pressure`.

Prognoza z historii jak w Go (p90 szczytu × 1,15 i mediana czasu), ale dla narzędzi z pamięcią poza
drzewem procesów historia może ją tylko podnieść: scheduler mierzy `xcrun`, a nie symulator, więc
trzy szybkie starty nauczyłyby go zera. Natywne joby biegną tylko lokalnie. Agent, którego komenda
czeka, dostaje na stderr powód i zdanie, że build wystartuje sam.

Jeden natywny build iOS naraz na całym Macu: `xcodebuild` z akcją, która kompiluje, `expo run:ios`,
`react-native run-ios|build-ios`, `eas build --local` poza Androidem i `portivo-mobile up`. Pody,
start symulatora i buildy Androida (Gradle, `expo run:android`) biegną obok, dalej tylko przy wolnej
pamięci: koniec fazy kompilacji scheduler widzi po kompilatorach Xcode, więc build Androida trzymałby
miejsce do upływu czasu, nic nie budując. Miejsce trzyma job z `running[]` tylko w fazie kompilacji:
30 s po ostatnim kompilatorze (albo po 2 × przewidywany czas bez kompilacji, nie mniej niż czas z
tabeli, bo zimny build z prebuildem i podami długo nie rusza kompilatorów) zwalnia je i swoją
rezerwację, więc `expo run:ios`, który
dalej trzyma Metro, nie blokuje następnego. Build spoza schedulera (xcodebuild z akcją, która
kompiluje, albo usługa buildów Xcode z dziećmi: odczepiony builder portivo-mobile, Xcode) też
zajmuje miejsce, a jego wzrost do przewidywanego szczytu buildu (p90 × 1,15 ostatnich zmierzonych
buildów, na start 10 GB) idzie do `reserved_gb`. `xcodebuild test-without-building`, który maestro
trzyma przez całą sesję, buildem nie jest. Pomiar joba w fazie buildu obejmuje drzewa xcodebuild
poza jego drzewem procesów, a dla `portivo-mobile up` także symulator, który włączył; czekający
na swoją kolej job (`reason.code = native`) nie blokuje jobów za sobą.

Start symulatora (`xcrun simctl boot`, `open -a Simulator`) i `portivo-mobile up` sesji, która nie
ma jeszcze dzierżawy, czekają (`reason.code = simulators`), gdy symulatory agentów (z puli
`simulator_pool_prefix`, `Portivo-*`) w użyciu są na limicie devguarda (`max_booted_simulators`).
Twoje symulatory spoza puli i chronione (`simulator_protect`, domyślnie `Portivo-Perf-*`) liczą
się do pamięci, ale nie do tego limitu: strażnik ich nie wyłącza, więc Twój otwarty iPhone albo
symulator sesji pomiarów wydajności nie może na stałe zablokować agentom startu. Nieużywane symulatory wyłącza strażnik (README, sekcja o
strażniku dev serwerów); bez jego świeżego pomiaru limitu nie ma.

## Reszta ciężkiej pracy: po kształcie komendy

Hook owija też komendy, których scheduler nie zna z projektu, ale które z samego kształtu są
pracą, kończą się same i potrafią zjeść gigabajty (`GENERIC_TOOLS` w `sched.py`):

- narzędzia: `cargo` (build, test, check, clippy, run, bench, doc, nextest, install), `swift`
  (build, test, run), `docker build` (i `buildx build`, `compose build`), `pytest`,
  `python -m pytest|unittest|mypy`, `mypy`, `pyright`, `tox`, `nox`, `deno`, `bazel`, `mvn`,
  `dotnet`, `nx`, `lerna`, `webpack`, `rollup`, `parcel`, `tsup`, `astro build`, `nuxt build`,
  `svelte-check`, `cypress run`, `mocha`, `ava`, `lighthouse`, `storybook build`, przepisy `just`,
  `task` i `make` poza modułem Go, także przez `npx`, `pnpm exec` i `uv run`. xcodebuild, Gradle,
  expo, react-native i start symulatora zna część natywna (sekcja wyżej). `swift build` jest tu, ale jego `swift-build`
  zajmuje miejsce na natywny build jak każdy build w drzewie joba schedulera (niżej);
- skrypty: plik uruchamiany po ścieżce (`./scripts/e2e.sh`, `bin/verify`, `e2e.sh`), przez
  interpreter (`python x.py`, `node x.js`, `tsx`, `ts-node`, `bun x.ts`, `bash x.sh`) i skrypty z
  `package.json` o dowolnej nazwie (`pnpm sm capture`, `npm run e2e:ci`), także `sh -c '...'`.

Nigdy: serwery, watchery i REPL-e (nazwa skryptu, podkomenda albo treść skryptu z `dev`, `serve`,
`server`, `start`, `watch`, `preview`, `runserver`..., `--watch`), `python -c`, `node -e`, heredoc
(`python3 -`), flagi informacyjne (`--version`, `--help`), skrypt, którego pliku nie ma, lekkie
skrypty (`format`, `fmt`, `clean`) i komendy menedżera pakietów (`install`, `add`, `why`...).

Klasa: `<repo>:<rodzaj>:<podpis>`, np. `site:script:pnpm-script:sm:capture`,
`app:test:cargo:test`, `site:script:python:scripts/capture.py`; skrypt spoza repo (tmp, scratchpad)
podpisuje się samą nazwą pliku. Pierwszy bieg nieznanego podpisu dostaje ostrożne przewidywanie
(GB, s): `build` 6/300, `test` 4/180, `e2e` 4/240, `typecheck` 3/90, `script` 4/300. Gdy rodzina
(repo, rodzaj, narzędzie: wszystkie skrypty Pythona w tym repo) ma co najmniej 5 biegów, nowy podpis
startuje od jej p90 × 1,5 (najmniej 1 GB). Potem p90 z historii tej klasy, jak w Go. `docker build`
nie schodzi poniżej 4 GB: praca dzieje się w maszynie wirtualnej Dockera, nie w drzewie komendy.
Te joby biegną tylko lokalnie, a `run` czyta z historii tylko wiersze ich rodziny i nie ładuje
cache Go, więc krótka komenda płaci mało: owinięty skrypt, który kończy się od razu, oddaje
wynik po kilkudziesięciu ms (pętla `run` patrzy co 10 ms przez pierwsze pół sekundy).

Rezerwa na wzrost (`reserved_gb`) znika dla joba, który biegnie dłużej niż 3 × przewidywany czas
i dłużej niż 10 minut: to zwykle serwer albo watcher, który skrypt zostawił na pierwszym planie,
a jego pamięć jest już w tym, co widzi jądro.

Natywny build w drzewie innego joba (`make ios`, skrypt z xcodebuild, `swift build`) należy do
tego joba: zajmuje miejsce na natywny build (`native.owner`), ale nie dokłada rezerwy jak build
spoza schedulera, bo wzrost tego joba jest już w jego przewidywaniu.

### Job w jobie

`run` daje dziecku `CLAUDE_ACC_SCHED_JOB=<id joba>`. `sched.py run` z tą zmienną (skrypt, który
sam woła scheduler, jak `sm-heavy.sh` albo `plock.py`) uruchamia komendę od razu: jej pamięć liczy
się w drzewie zewnętrznego joba, a drugie czekanie na tę samą pamięć mogłoby czekać na siebie.

### `git grep`

Hook dokłada `-I` (pomiń pliki binarne) do `git grep` agenta, który nie ma `-I`, `-a`, `--text`
ani `--binary-files`. 2026-10-08 pętla agenta z `git grep -nE ... <commit>` w repo z 368 MB filmów
i obrazów w historii urosła do 10 GB w 2 sekundy: wyrażenie `-E` idzie przez regex macOS, a plik
binarny to dla niego jedna linia długości megabajtów. Z `-I` ta sama komenda ma szczyt ~270 MB.

### Codex

`sched.py codex install` dopisuje do `~/.codex/hooks.json` (albo `$CODEX_HOME/hooks.json`) hook
PreToolUse z matcherem `Bash`: `claude-acc-hook codex`, a bez natywnego frontu `devguard admit
--codex`. Cudze wpisy (rtk, Orca) zostają, drugi `install` niczego nie dubluje, `uninstall` zdejmuje
tylko nasz. Codex uruchamia nowy hook dopiero po zaufaniu mu w `/hooks`. Przepisaną komendę przyjmuje
tylko z `permissionDecision: "allow"`, więc owinięte komendy nie pytają o zgodę (Orka i tak
uruchamia Codeksa z `--dangerously-bypass-approvals-and-sandbox`).

## Hamulec strażnika

Strażnik (`devguard.py`, `lastresort.py`) liczy co przebieg stopień hamulca: `tight` (ciasno),
`brake` (hamulec), `emergency` (awaria); szczegóły i progi w README, sekcja Dev server guard.
Scheduler czyta go ze świeżego (do 30 s) stanu strażnika: przy `tight` i `brake` startuje tylko to,
co się mieści, bez furtki „sam na Macu” (przy `brake` także bez rezerwy na samotny job ponad
dostępną pamięć), a przy `emergency` nie startuje nic; joby w kolejce czekają (`reason.code`
`pressure`), nikt nie dostaje odmowy. Hamulec może zgasić biegnący job schedulera: agent dostaje
kod wyjścia sygnału, a log strażnika komendę do wznowienia.

## `sched.py cancel`

`claude-acc sched cancel <job-id|pane>` zaznacza job w `state.json` (`cancelled`, czas). Czekający wrapper widzi to w następnym obrocie pętli (co 0,5 s), zdejmuje się z kolejki i kończy kodem 130 z linią `[sched] anulowane …`; biegnący dostaje SIGTERM do wrappera, który jak przy Ctrl-C przekazuje SIGCONT i SIGTERM grupie procesów komendy i zapisuje bieg do historii z `"cancelled": true`. Sygnał idzie tylko do pidu, który wciąż jest wrapperem `sched.py run` (argv z KERN_PROCARGS2), a `--kill` (SIGKILL po 5 s) tylko do grupy, której lider jest dzieckiem tego wrappera. Klucz panelu Orki (`ORCA_PANE_KEY`, w `agent.pane`) wybiera wszystkie joby panelu, `--queued` tylko czekające; `--json` wypisuje `{"target", "jobs": [{"id", "label", "state", "result"}]}` z `result` `dequeued`, `ended`, `terminated`, `killed`, `still running` albo `gone`.

## `sched.py wait`

`sched.py wait [--max S] [--every S] -- 'WARUNEK'` sprawdza WARUNEK (komendę powłoki) co `--every`
sekund (5) i kończy się kodem 0, gdy WARUNEK zwróci 0, albo kodem 75 po `--max` sekundach (270).
Domyślny limit jest krótszy niż 5-minutowy cache promptu subagenta Claude Code: agent, który czeka
w pętli wywołań `wait`, odświeża cache przy każdym, zamiast pisać cały kontekst od nowa po jednym
długim `sleep`. Agent z cache godzinnym podaje większe `--max`. Zły argument: kod 64.

## rtk

Hook rtk (`rtk-rewrite.sh`) też przepisuje komendy, które owija scheduler. Dwa hooki PreToolUse z
`updatedInput` na tej samej komendzie dają losowy wynik, więc na Macu z rtk te komendy idą w jego
wyjątki, w `~/Library/Application Support/rtk/config.toml`, w sekcji `[hooks]`. Linię
`exclude_commands` drukuje `sched.py rtk-excludes`, a `sched.py rtk-excludes --write` wpisuje ją w
ten plik (setup.sh woła to przy każdej instalacji na Macu z rtk). Twoje wpisy w `exclude_commands`
zostają za naszymi, komentarze i reszta pliku zostają znak w znak, kopia sprzed pierwszej zmiany to
`config.toml.bak-claude-acc`. Wartości, której nie umie przeczytać (nie tablica, tablica nie samych
stringów), nie rusza: kończy się kodem 1 i plik zostaje, jaki był. Test pilnuje, żeby wyjątki obejmowały wszystko,
co scheduler owija, i nic więcej (rtk dalej skraca `pnpm install` czy `git`). Jeden wyjątek od
„nic więcej”: rtk czyta wzorce crate'em `regex` bez lookaroundów, więc `xcodebuild` idzie w wyjątki
cały, także `-version` i `-list`, których scheduler nie owija.
Test pyta też samo rtk: każda komenda, którą scheduler owija, zostaje przez rtk nieprzepisana, a
te, których nie owija (`git status`, `cargo fmt`, `docker ps`, `pnpm install`), rtk dalej skraca.
rtk porównuje wyjątki z komendą po swojej normalizacji (`uv run pytest` i `python -m pytest` to dla
niego `pytest`) i ignoruje cały plik z niepełną sekcją. Komenda złożona z członu, który rtk
przepisuje, i członu, który owija scheduler (`git status && cargo test`), dalej daje dwa
przepisania naraz; wtedy wygrywa jedno z nich.

W środku opakowania `with_rtk` pyta `rtk rewrite` o tę samą komendę z pustym `HOME`, czyli bez
naszych wyjątków, więc reguły rtk mają jedno źródło i agent dalej dostaje krótkie wyjście. Gdy rtk
nie ma albo nic nie przepisuje, komenda zostaje bez zmian. Na Depot idzie komenda bez opakowań,
czyli bez rtk.
