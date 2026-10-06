# Scheduler Go i JS: stan dla panelu i historia biegów

`sched.py` wpuszcza ciężkie komendy agentów po pamięci zamiast jednego zamka na wszystko: Go
(build, vet, test, lint, cele make) i JS (testy, e2e, buildy, typecheck, lint, w każdym projekcie z
`package.json`). Każdy bieg przechodzi przez `sched.py run`; hook PreToolUse (`devguard.py admit`)
sam owija komendy agentów, a `plock.py go` przekazuje do niego swoje. Pomiary, z których wzięły się
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
| `devserver_reserve_gb` | miejsce na jeszcze jeden dev serwer: `min(max_server_gb, budżet devguarda - dev serwery teraz)`, z `devguard-state.json` |
| `jobs_now_gb` | suma `mem_now_gb` lokalnych jobów |
| `reserved_gb` | o ile lokalne joby jeszcze urosną: suma `max(0, mem_predicted_gb - mem_now_gb)` |
| `others_gb` | `ram_gb - available_gb - jobs_now_gb`: inne aplikacje i system |
| `free_for_admission_gb` | `available_gb - headroom_gb - devserver_reserve_gb - reserved_gb` |
| `idle_max_gb` | ile zmieściłoby się na pustym Macu: najwyższy `available_gb` z ostatnich 7 dni minus `headroom_gb`; większe joby idą zawsze na Depot |
| `swap_used_gb`, `swap_growth_2m_gb` | swap teraz i jego przyrost w 2 minuty |
| `pressure` | `normal`, `warn`, `critical` (devguard) |

### Job w `running[]` i `queue[]`

| pole | znaczenie |
|---|---|
| `id` | `j-<epoch>-<4 hex>` |
| `class` | Go: `<moduł>:<czasownik>:<zakres>[:race][:compile]`, np. `charter-service:vet:tree`, `charter-service:test:pkg:internal/handlers:compile`; JS: patrz sekcja JS |
| `kind` | `build`, `vet`, `test`, `lint`, `make`, `generate`, `run`, `other` |
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

Tylko w `running[]`:

| pole | znaczenie |
|---|---|
| `started_at`, `elapsed_s` | start biegu (po wpuszczeniu) i czas od startu |
| `eta_s`, `progress` | `max(0, predicted_wall_s - elapsed_s)` i `min(0.99, elapsed_s / predicted_wall_s)` |
| `mem_now_gb`, `mem_peak_gb` | suma `phys_footprint` procesów joba (drzewo i grupa procesów) teraz i najwięcej do tej pory; 0 dla Depot |
| `cpu_cores` | średnio zajęte rdzenie od startu (CPU / czas) |
| `paused`, `pause_reason` | SIGSTOP przy rosnącym swapie; tekst, np. `swap +0,6 GB in 2 min` |
| `depot` | dla `where=depot`: `{target, job, cores, run_id, url, units, cost_usd}`; `target` to `depot-exec` albo `depot-ci`; `units` i `cost_usd` rosną w trakcie |

Tylko w `queue[]`:

| pole | znaczenie |
|---|---|
| `position` | 1 = następny do wpuszczenia |
| `enqueued_at`, `waited_s` | kiedy się zgłosił i ile już czeka |
| `eta_start_s` | przewidywane sekundy do startu |
| `reason` | `{code, need_gb, free_gb, after[], text}`; `code`: `memory` (czeka na pamięć), `head` (pamięć zarezerwowana dla joba, który czeka najdłużej), `pressure` (devguard: presja), `deciding` (scheduler jeszcze liczy trasę); `after` to id jobów, na których koniec czeka |

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
| `today.old_lock_wait_s` | czekanie, które byłoby przy starym zamku: scheduler prowadzi wirtualny zamek FIFO (start = max(przyjście, zwolnienie poprzedniego), z prawdziwymi czasami biegów) |
| `today.wait_saved_s` | `old_lock_wait_s - wait_s` |
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
choice, why, local_eta_s, depot_eta_s, lambda, predicted_gb, predicted_wall_s, count1_dropped
```

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
starve_s             120    po tylu sekundach czekania job rezerwuje pamięć i nikt go nie wyprzedza
drop_count1          true   zdejmuj -count=1 w iteracji agenta dla pakietów bez bazy
pause_swap_gb        0.5    przyrost swapu w 2 min, przy którym najmłodszy ciężki job dostaje SIGSTOP
depot_eta_since      "2026-10-05"   od kiedy brać czasy z `depot-cost.py eta` (rozmiary maszyn)
count1_trusted_exec  ["internal/testhelpers/testpg"]   pliki pomocników, których exec nie psuje cache
depot_org            ""     organizacja Depot dla `sched.py depot`; pusta: domyślna organizacja CLI
node                 true   testy, buildy i typecheck JS w kolejce
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
`shop:test:apps/web:vitest`. `all` to bieg na całym monorepo (`-r`, `--workspaces`, każde
`turbo run`), `filtered` to wybrane testy (`-t`, `--grep`, `--project`, pliki w argumentach). Do pierwszych
biegów klasy przewidywanie bierze się z tabeli (GB, sekundy): `test` 3,0/90, `e2e` 3,5/180, `build`
4,0/180, `typecheck` 2,5/60, `lint` 2,0/60, `check` 3,0/120; `all` razy 1,5 GB i 2 czasu, `filtered`
połowa GB (najmniej 1) i 0,4 czasu. Potem p90 z historii, jak w Go. Joby JS biegną tylko lokalnie:
nie idą na Depot i nie dostają Postgresa. `"node": false` w `config.json` wyłącza całą tę część.

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
`exclude_commands` drukuje `sched.py rtk-excludes`; test pilnuje, żeby wyjątki obejmowały wszystko,
co scheduler owija, i nic więcej (rtk dalej skraca `pnpm install` czy `git`).

W środku opakowania `with_rtk` pyta `rtk rewrite` o tę samą komendę z pustym `HOME`, czyli bez
naszych wyjątków, więc reguły rtk mają jedno źródło i agent dalej dostaje krótkie wyjście. Gdy rtk
nie ma albo nic nie przepisuje, komenda zostaje bez zmian. Na Depot idzie komenda bez opakowań,
czyli bez rtk.
