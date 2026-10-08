"""Ostatnia linia strażnika: przy krytycznej presji gasi ciężkie procesy pomocnicze.

Strażnik (devguard_core) umie zatrzymywać tylko dev serwery. 2026-10-08 Mac padł przy swapie
17,9 GB, bo pamięć zjadło tło, którego nie widział: 55 procesów node (14,7 GB, serwery MCP,
vitest, tsc), cztery gopls (2,3 GB), headless Chrome agentów. Ten moduł wchodzi dopiero wtedy,
gdy presja jest krytyczna (Pressure.level == 2), a strażnik nie miał już czego zatrzymać, i
gasi JEDNO drzewo na przebieg, w kolejności od najmniej bolesnego:

  1. sieroty: proces node/bun/deno/gopls przepięty na launchd, którego właściciel już nie
     żyje, starszy niż 10 min. Sam rodzic 1 nie wystarcza: agent puszcza zadanie przez `&`
     albo `nohup`, jego powłoka wychodzi, a on dalej czeka na wynik. Właściciel to agent z
     CLAUDE_PID w środowisku procesu, a bez tej zmiennej lider grupy procesów;
  2. headless przeglądarki agentów (Chrome for Testing, --headless, playwright, puppeteer);
  3. gopls: diagnostyka Go w tej sesji znika, dopóki klient LSP go nie podniesie;
  4. przebiegi testów i kompilacji agentów (vitest, jest, tsc, tsgo, playwright, binarka
     `go test`, kompilator Go, golangci-lint): agent dostaje błąd i powtarza przez scheduler.

Nigdy: claude, codex, Orca, powłoki, launchd, przeglądarka użytkownika, aplikacje z
/Applications i /System, demony claude-acc. Dev serwery zostają strażnikowi.

`choose` jest czysta (tabela procesów, rozmiary i wiek wchodzą z zewnątrz), więc testuje się
ją na atrapach: tests/test_lastresort.py.
"""
import os
import re

MB = 1024**2
GB = 1024**3
MINUTE = 60

# komendy, które nigdy nie giną (to samo co SACRED strażnika plus aplikacje i nasze demony)
NEVER = re.compile(
    r"(^|/)(claude|codex|login|launchd)(\s|$)|Orca\.app|^-?(\S*/)?(zsh|bash|fish|sh)(\s|$)"
    r"|^/Applications/(?!.*(Chrome for Testing|--headless))|^/System/|^/usr/(libexec|sbin|bin)/"
    r"|^/Library/|\.local/share/claude-acc/|memory-guard\.py|com\.docker|Docker\.app"
)
AGENT = re.compile(r"(^|/)(claude|codex)(\s|$)")
RUNTIME = re.compile(r"^(\S*/)?(node|bun|deno|gopls)(\s|$)")
HEADLESS = re.compile(
    r"Chrome for Testing|HeadlessChrome|headless_shell|ms-playwright|puppeteer|--headless"
)
GOPLS = re.compile(r"^(\S*/)?gopls(\s|$)(?!.*\*\* telemetry)")
RUNNER = re.compile(
    r"(^|/|\s)(vitest|jest|tsc|tsgo|playwright|golangci-lint|tsgolint)(\.js|\.mjs|\.cjs)?(\s|$)"
    r"|/go-build\d+/\S+\.test(\s|$)|\.test\s+-test\.|/pkg/tool/\S+/(compile|link)(\s|$)"
)

# progi: drobiazgi nie ratują Maca, a każde zabicie coś komuś psuje
CLASSES = (
    # (kod, opis, min. rozmiar drzewa, min. wiek w sekundach)
    ("orphan", "sierota po martwym agencie albo terminalu", 100 * MB, 10 * MINUTE),
    ("headless", "headless przeglądarka agenta", 300 * MB, 5 * MINUTE),
    ("gopls", "gopls (serwer LSP Go)", 800 * MB, 2 * MINUTE),
    ("runner", "przebieg testów albo kompilacji agenta", 1 * GB, 2 * MINUTE),
)


def _children(table):
    out = {}
    for pid, (ppid, _command) in table.items():
        out.setdefault(ppid, []).append(pid)
    return out


def _tree(pid, children):
    out, stack = [pid], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            out.append(child)
            stack.append(child)
    return out


def classify(pid, table, owned=lambda pid: False):
    """Kod klasy procesu albo None. Sierota: rodzic 1 (launchd przejął dziecko) i nikt żywy
    się do niej nie przyznaje (`owned`)."""
    ppid, command = table.get(pid, (0, ""))
    if not command or NEVER.search(command):
        return None
    if HEADLESS.search(command):
        return "headless"
    if ppid == 1 and RUNTIME.search(command) and not owned(pid):
        return "orphan"
    if GOPLS.search(command):
        return "gopls"
    if RUNNER.search(command):
        return "runner"
    return None


def choose(table, size_of, age_of, skip=(), owned=lambda pid: False):
    """Najlepszy kandydat albo None: (kod, opis, pid korzenia, pidy drzewa, rozmiar drzewa).

    table: {pid: (ppid, komenda)}; size_of(pid) -> bajty footprintu albo None;
    age_of(pid) -> sekundy życia albo None; skip: pidy, których nie wolno ruszać (dev serwery
    strażnika i jego własny proces); owned(pid) -> czy proces z rodzicem 1 ma żywego
    właściciela."""
    children = _children(table)
    skip = set(skip)
    found = {}
    for pid in table:
        code = classify(pid, table, owned)
        if code is None or pid in skip:
            continue
        # korzeń drzewa: rodzic tej samej klasy przejmuje dziecko (vitest i jego wątki,
        # `go test` i binarka testu, Chrome i jego renderery)
        parent = table[pid][0]
        if parent in table and classify(parent, table, owned) == code and parent not in skip:
            continue
        tree = [p for p in _tree(pid, children) if p not in skip]
        if any(NEVER.search(table.get(p, (0, ""))[1]) for p in tree):
            # drzewo z czymś świętym w środku (np. agent pod node) nie jest pomocnikiem
            continue
        size = sum(size_of(p) or 0 for p in tree)
        age = age_of(pid)
        found.setdefault(code, []).append((size, age, pid, tree))
    for code, label, min_size, min_age in CLASSES:
        ready = [
            (size, pid, tree)
            for size, age, pid, tree in found.get(code, [])
            if size >= min_size and age is not None and age >= min_age
        ]
        if ready:
            size, pid, tree = max(ready)
            return code, label, pid, tree, size
    return None


def owner_alive(pid, table, env, pgid):
    """Czy proces przepięty na launchd ma żywego właściciela.

    env: środowisko procesu; Claude Code daje każdej komendzie CLAUDE_PID, więc zadanie
    puszczone w tło przez agenta wskazuje swojego agenta nawet po śmierci powłoki. Bez tej
    zmiennej właścicielem jest lider grupy procesów (serwer MCP dzieli grupę z agentem, który
    go postawił); pgid: lider grupy albo None."""
    agent = env.get("CLAUDE_PID", "")
    if agent.isdigit():
        return bool(AGENT.search(table.get(int(agent), (0, ""))[1]))
    return pgid is not None and pgid != pid and pgid in table


def reap(world, state, core):
    """Jedno drzewo mniej, gdy presja krytyczna; zwraca opis akcji albo None.

    core: moduł devguard_core (usage, terminate, log, janitor, mach time)."""
    table = world.table
    skip = {os.getpid()}
    for unit in world.units:
        skip.update(unit.pids)
    cache = {}

    def info(pid):
        if pid not in cache:
            cache[pid] = core.usage(pid)
        return cache[pid]

    def size_of(pid):
        got = info(pid)
        return got["footprint"] if got else None

    now_abs = core._libc.mach_absolute_time()

    def age_of(pid):
        got = info(pid)
        if not got or not got["start"]:
            return None
        return (now_abs - got["start"]) * core.TICK_NS / 1e9

    def owned(pid):
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        return owner_alive(pid, table, core.proc_env(pid), pgid)

    pick = choose(table, size_of, age_of, skip, owned)
    if pick is None:
        return None
    code, label, pid, tree, size = pick

    class _Tree:  # terminate() strażnika chce obiektu z .pids
        pids = tree

    left = core.terminate(_Tree, table)
    command = table.get(pid, (0, ""))[1]
    result = "zatrzymany" if not left else f"nie chcą zginąć: {left}"
    line = (
        f"ostatnia linia: {label} pid {pid} {core.janitor.human(size)} "
        f"({len(tree)} proc.): {command[:160]} -> {result}"
    )
    core.log(line)
    events = state.setdefault("lastresort", [])
    events.append({"at": world.now, "code": code, "pid": pid, "size": size, "result": result})
    del events[:-50]
    try:
        core.janitor.notify(
            "Strażnik: ostatnia linia", f"{label}: {core.janitor.human(size)} zwolnione"
        )
    except Exception:
        pass
    return line
