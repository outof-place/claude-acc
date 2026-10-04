# Wydajność Maca: co zmierzone, co działa, co nie

Badanie z 2026-10-04 na MacBooku Pro 16" M4 Max (Mac16,5, 12 rdzeni P + 4 E, 48 GB RAM),
macOS 27.0 (26A428). Miejsce: TKB, kabel USB LAN (RTL8153) do Archera BE230. W tle przez
cały czas pracowało kilka sesji Claude Code w Orce, dev serwery, maszyna Dockera i rendery
Blendera innego agenta, więc każdą liczbę podaję jako zakres z kilku przebiegów.

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

## Wyniki

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

## Rekomendacje, od największego efektu

1. **`perf.py apply bg-helpers`** (włączone 2026-10-04): cavemem worker przestaje zajmować
   rdzeń P, około 1 W mniej.
   - Lista `background` w `~/.local/share/claude-acc/perf.json` przyjmuje kolejne wzorce.
     Kandydatów pokazuje `perf.py bench cpu` w linii „najwięcej energii”.
   - Gdy worker wstanie z nowym pid, trzeba go znowu dodać przez `perf.py keep`. Szablon
     `launchd/com.filip.claude-acc.perf.plist.template` robi to co 5 minut (jeszcze nie
     zainstalowany).
   - Lepsza poprawka jest po stronie cavemem: worker nie powinien skanować bazy bez końca
     (cavemem 0.3.0, `idleShutdownMs` nie działa, gdy sesje bez przerwy zapisują).
2. **Pamięć Dockera.** Przydział VM z 8 GB na 6 GB (Settings, Resources) zwalnia około 2 GB
   na stałe. Kontenery używają razem 3,7 GB, a największy z nich (otel-lgtm) 1 GB. Druga
   droga: przejście na Docker VMM (od 4.86), który według dokumentacji oddaje wolną
   pamięć hostowi. Obie zmiany wymagają restartu Dockera, więc zostawiam je Tobie.
3. **Ogranicznik wysyłania przy Zyxelu** (`sudo ./perf-root.sh trial`).
   - Przy Orange najpierw `perf.py bench network`. Jeśli w linii „z tego sieć” opóźnienie
     przy wysyłaniu jest wyraźnie wyższe niż bez obciążenia (np. +100 ms i więcej), bufor
     Zyxela puchnie.
   - Wcześniejsze 0,9-2,2 s „responsiveness” przy wysyłaniu z tamtego łącza to suma sieci
     i kolejki w gnieździe. Przy BE230 ta suma wynosi 200-450 ms, a sama sieć tylko +1-4
     ms. Bez rozbicia nie wiadomo, ile z tamtych sekund siedziało w routerze.
   - Wtedy `trial` mierzy, ustawia `ifconfig <if> tbr` na 90% zmierzonego uploadu,
     mierzy znowu i cofa (z `--keep` zostawia). Kolejka przechodzi wtedy do fq_codel
     Maca, gdzie jądro dławi gniazda agentów zamiast zapychać router.
   - Nie zmierzone, bo dziś Mac był przy BE230, gdzie nie ma czego ratować. Limit nie
     przetrwa restartu ani odłączenia adaptera. Jeśli test wypadnie dobrze, następny krok
     to demon roota, który nakłada limit przy zmianie sieci (osobno dla każdej bramy).
4. **Memory Saver w Brave na „Maximum”** (Ustawienia, Wydajność). Teraz jest włączony, ale
   z `aggressiveness: 0`. Brave ma 4,8 GB w 28 procesach; ustawienie w interfejsie działa
   od razu i jest odwracalne. Nie mierzyłem, bo zmiana pliku `Local State` wymaga
   zamknięcia Brave.
5. **WindowServer na 37-41% CPU niezależnie od aplikacji na pierwszym planie.** Warto
   sprawdzić `perf.py bench gpu` przed i po zamknięciu na minutę podejrzanych: DockDoor,
   Mos, Music z animowanymi okładkami, ikony w menu bar, które się animują.
   Przełączenie ekranu na 60 Hz (Ustawienia, Monitory, Odświeżanie) obetnie składanie
   klatek o połowę kosztem płynności, więc to wymiana, a nie przyspieszenie.

## Jak tego używać

```
perf.py bench [network|cpu|gpu|all] [--runs N] [--json]   # pomiar, zapis w perf-state.json
perf.py status [--json]                                    # poprawki i ostatnie pomiary
perf.py list                                               # poprawki z efektem
perf.py apply bg-helpers | --all [--dry-run]
perf.py undo bg-helpers | --all
perf.py keep                                               # ponowne nałożenie (launchd)
sudo ./perf-root.sh trial [--rate 27Mbps] [--keep]         # ogranicznik: pomiar przed i po
sudo ./perf-root.sh shaper apply|undo                      # ./perf-root.sh shaper status bez sudo
```

Stan dla panelu jest w `~/.local/share/claude-acc/perf-state.json`:

- `applied`: włączone poprawki razem z tym, co trzeba cofnąć (pid i chwila startu procesu,
  więc ponownie użyty pid nie zostanie ruszony).
- `bench`: ostatni pomiar każdego rodzaju (`at`, `load` sprzed pomiaru, `result`).
- `history`: 30 ostatnich pomiarów każdego rodzaju. Wpis z `shaper` oznacza pomiar z
  włączonym ogranicznikiem i nie liczy się do jego limitu.

## Źródła

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
- RTL8153 bez NCM: https://lkml.kernel.org/netdev/20230106160739.100708-3-bjorn@mork.no/T/ ;
  ECM a NCM w praktyce: https://gist.github.com/MadLittleMods/3005bb13f7e7178e1eaa9f054cc547b0
