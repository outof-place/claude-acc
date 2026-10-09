"""Hamulec pamięci strażnika: zanim kompresor i swap zamrożą Maca, gasi najmniej potrzebne drzewa.

Strażnik (devguard_core) umie zatrzymywać tylko dev serwery. 2026-10-08 Mac padł o 18:56 przy
swapie 17,9 GB, bo pamięć zjadło tło, którego nie widział: 55 procesów node (14,7 GB, serwery MCP,
vitest, tsc), cztery gopls (2,3 GB), headless Chrome agentów, git na 10 GB. Ostatnie linie jądra:
`memorystatus: killing due to "vm-compressor-space-shortage"`, kompresor ~34 GB z 48 GB RAM.

Stopień (`stage`) liczy się z tego, co tamtego dnia zapowiadało zamrożenie (README, Memory brake;
odtworzenie z historii strażnika w tests/test_lastresort.py): zajętość kompresora względem RAM, zajętość jego segmentów
względem limitu jądra (ten limit to "compressor space shortage"), swap i jego przyrost w 2 min,
poziom presji jądra.

  0  spokój
  1  ciasno: nikt nie ginie, scheduler wstrzymuje start nowych jobów (czekają, nie odpadają)
  2  hamulec: jedno drzewo co `brake_cooldown_seconds`, od najmniej bolesnego
  3  awaria: jedno drzewo co `emergency_cooldown_seconds`, krótka łaska przed SIGKILL, bez
     minimalnego wieku (świeży proces, który rośnie o gigabajty na minutę, to właśnie ten)

Kolejność ofiar (`CLASSES`), w klasie największa:
  1. runaway: jeden proces ponad `runaway_percent` RAM (dawny memory-guard), na każdym stopniu;
  2. sieroty: node/bun/deno/gopls przepięty na launchd, którego właściciel już nie żyje. Sam
     rodzic 1 nie wystarcza: agent puszcza zadanie przez `&` albo `nohup`, jego powłoka wychodzi,
     a on dalej czeka na wynik. Właściciel to agent z CLAUDE_PID w środowisku procesu, a bez tej
     zmiennej lider grupy procesów;
  3. headless przeglądarki agentów (Chrome for Testing, --headless, playwright, puppeteer);
  4. spuchnięty git (status w wielkim drzewie roboczym) i serwery LSP: klient podniesie je sam;
  5. joby schedulera (agent dostaje kod wyjścia, log mówi, jak wznowić) i przebiegi testów i
     kompilacji agentów spoza schedulera;
  6. tylko przy awarii: każde inne drzewo, które agent postawił komendą Bash, ponad 1 GB.

Nigdy: claude, codex, Orca, powłoki, launchd, przeglądarka użytkownika, aplikacje z
/Applications i /System, Docker Desktop, demony claude-acc. Dev serwery zostają strażnikowi.

`stage` i `choose` są czyste (sygnały, tabela procesów, rozmiary i wiek wchodzą z zewnątrz),
więc testują się na atrapach: tests/test_lastresort.py.
"""
import os
import re
import shlex

import orcahost

MB = 1024**2
GB = 1024**3
MINUTE = 60

# komendy, które nigdy nie giną (to samo co SACRED strażnika plus aplikacje i nasze demony);
# aplikacje hosta (Orca, Pod) daje orcahost
NEVER = re.compile(
    r"(^|/)(claude|codex|login|launchd)(\s|$)|" + orcahost.APPS + r"|^-?(\S*/)?(zsh|bash|fish|sh)(\s|$)"
    r"|^/Applications/(?!.*(Chrome for Testing|--headless))|^/System/|^/usr/(libexec|sbin|bin)/"
    r"|^/Library/|\.local/share/claude-acc/|memory-guard\.py|com\.docker|Docker\.app"
    r"|WindowServer|^\("
)
AGENT = re.compile(r"(^|/)(claude|codex)(\s|$)")
SHELL = re.compile(r"^-?(\S*/)?(zsh|bash|fish|sh)(\s|$)")
RUNTIME = re.compile(r"^(\S*/)?(node|bun|deno|gopls)(\s|$)")
HEADLESS = re.compile(
    r"Chrome for Testing|HeadlessChrome|headless_shell|chrome-headless-shell|ms-playwright|puppeteer|--headless"
)
GOPLS = re.compile(r"^(\S*/)?gopls(\s|$)(?!.*\*\* telemetry)")
LSP = re.compile(
    r"tsserver\.js|typescript-language-server|(^|/)(rust-analyzer|sourcekit-lsp|clangd|"
    r"pyright-langserver|basedpyright-langserver|vscode-eslint-language-server|tailwindcss-language-server)(\s|$)"
)
GIT = re.compile(r"^(\S*/)?git(\s|$)")
RUNNER = re.compile(
    r"(^|/|\s)(vitest|jest|tsc|tsgo|playwright|golangci-lint|tsgolint)(\.js|\.mjs|\.cjs)?(\s|$)"
    r"|/go-build\d+/\S+\.test(\s|$)|\.test\s+-test\.|/pkg/tool/\S+/(compile|link)(\s|$)"
    r"|(^|/)(cargo|rustc|swift-frontend|xcodebuild|pytest|mypy|webpack|esbuild)(\s|$)"
)

# progi: drobiazgi nie ratują Maca, a każde zabicie coś komuś psuje
CLASSES = (
    # (kod, opis, min. rozmiar drzewa, min. wiek w sekundach, od którego stopnia)
    ("runaway", "proces, który zjada pół RAM", None, 0, 0),
    ("orphan", "sierota po martwym agencie albo terminalu", 100 * MB, 10 * MINUTE, 2),
    ("headless", "headless przeglądarka agenta", 300 * MB, 5 * MINUTE, 2),
    ("git", "spuchnięty git", 1 * GB, 1 * MINUTE, 2),
    ("gopls", "gopls (serwer LSP Go)", 800 * MB, 2 * MINUTE, 2),
    ("lsp", "serwer LSP", 1536 * MB, 2 * MINUTE, 2),
    ("job", "job schedulera", 1 * GB, 1 * MINUTE, 2),
    ("runner", "przebieg testów albo kompilacji agenta", 1 * GB, 2 * MINUTE, 2),
    ("agent", "drzewo komendy agenta", 1 * GB, 0, 3),
)

STAGE_NAMES = ("spokój", "ciasno", "hamulec", "awaria")
DEFAULTS = {
    # kompresor (vm.compressor_bytes_used) jako procent RAM; 08.10 przez półtorej godziny
    # duszenia się trzymał 12-22 GB z 48, a w ostatnich minutach doszedł do 34
    "compressor_tight_percent": 40,
    "compressor_brake_percent": 50,
    "compressor_emergency_percent": 60,
    # segmenty kompresora względem vm.compressor.segment.limit: przy 98% jądro ogłasza
    # "compressor space shortage" i zaczyna jetsam
    "segments_brake_percent": 60,
    "segments_emergency_percent": 80,
    # swap jako procent RAM, liczony tylko wtedy, gdy rośnie (stojący swap to ślad po dawnej presji)
    "swap_tight_percent": 12,
    "swap_brake_percent": 20,
    "swap_emergency_percent": 30,
    # przyrost swapu w 2 minutach (GB)
    "swap_growth_tight_gb": 0.5,
    "swap_growth_brake_gb": 1.0,
    "swap_growth_emergency_gb": 2.0,
    # jeden proces ponad tyle procent RAM ginie na każdym stopniu (dawny memory-guard)
    "runaway_percent": 50,
    "brake_cooldown_seconds": 20,
    "emergency_cooldown_seconds": 5,
    # łaska między SIGTERM a SIGKILL
    "brake_grace_seconds": 8,
    "emergency_grace_seconds": 3,
}


def settings(cfg):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("brake") or {}).items() if k in DEFAULTS})
    return out


def stage(sig, cfg=None):
    """(stopień 0-3, powody) z sygnałów pamięci.

    sig: ram, compressed (bajty w kompresorze), segments, segments_limit, swap_used,
    swap_growth (bajty w 2 min), kernel (1, 2, 4), available (procent), guard_level (0-2,
    poziom strażnika: jego krytyczna presja to co najmniej hamulec)."""
    s = settings(cfg or {})
    ram = sig.get("ram") or 16 * GB
    comp = (sig.get("compressed") or 0) / ram * 100
    limit = sig.get("segments_limit") or 0
    segs = (sig.get("segments") or 0) / limit * 100 if limit else 0
    swap = (sig.get("swap_used") or 0) / ram * 100
    growth = (sig.get("swap_growth") or 0) / GB
    kernel = sig.get("kernel") or 1
    gb = lambda pct: f"{pct / 100 * ram / GB:.1f} GB"  # noqa: E731
    rules = (
        (3, comp >= s["compressor_emergency_percent"], f"kompresor {gb(comp)} ({comp:.0f}% RAM)"),
        (3, segs >= s["segments_emergency_percent"], f"segmenty kompresora {segs:.0f}% limitu"),
        (3, swap >= s["swap_emergency_percent"] and growth >= s["swap_growth_emergency_gb"],
         f"swap {gb(swap)}, +{growth:.1f} GB w 2 min"),
        (3, kernel >= 4 and growth >= s["swap_growth_emergency_gb"], f"jądro: presja krytyczna, swap +{growth:.1f} GB w 2 min"),
        (2, comp >= s["compressor_brake_percent"], f"kompresor {gb(comp)} ({comp:.0f}% RAM)"),
        (2, segs >= s["segments_brake_percent"], f"segmenty kompresora {segs:.0f}% limitu"),
        (2, swap >= s["swap_brake_percent"] and growth >= s["swap_growth_brake_gb"],
         f"swap {gb(swap)}, +{growth:.1f} GB w 2 min"),
        (2, kernel >= 4, "jądro: presja krytyczna"),
        (2, (sig.get("guard_level") or 0) >= 2, "strażnik: presja krytyczna"),
        (1, comp >= s["compressor_tight_percent"], f"kompresor {gb(comp)} ({comp:.0f}% RAM)"),
        (1, swap >= s["swap_tight_percent"] and growth >= s["swap_growth_tight_gb"],
         f"swap {gb(swap)}, +{growth:.1f} GB w 2 min"),
        (1, kernel >= 2 and growth >= s["swap_growth_tight_gb"], f"jądro: ostrzeżenie, swap +{growth:.1f} GB w 2 min"),
    )  # fmt: skip
    for level in (3, 2, 1):
        reasons = [text for lvl, hit, text in rules if lvl == level and hit]
        if reasons:
            return level, reasons
    return 0, []


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
    if LSP.search(command):
        return "lsp"
    if GIT.search(command):
        return "git"
    if RUNNER.search(command):
        return "runner"
    return None


def agent_command_roots(table):
    """Korzenie komend Bash agentów: dzieci powłoki, której rodzicem jest claude albo codex."""
    roots = []
    for pid, (ppid, command) in table.items():
        parent = table.get(ppid)
        if not parent or NEVER.search(command) or not SHELL.search(parent[1]):
            continue
        grand = table.get(parent[0])
        if grand and AGENT.search(grand[1]):
            roots.append(pid)
    return roots


def choose(table, size_of, age_of, skip=(), owned=lambda pid: False, level=2, jobs=(), ram=None, cfg=None):
    """Najlepszy kandydat albo None: (kod, opis, pid korzenia, pidy drzewa, rozmiar drzewa, job).

    table: {pid: (ppid, komenda)}; size_of(pid) -> bajty footprintu albo None;
    age_of(pid) -> sekundy życia albo None; skip: pidy, których nie wolno ruszać (dev serwery
    strażnika i jego własny proces); owned(pid) -> czy proces z rodzicem 1 ma żywego
    właściciela; level: stopień z `stage`; jobs: lokalne joby schedulera ({id, child_pgid, cmd,
    agent}); ram: bajty RAM (próg runaway)."""
    s = settings(cfg or {})
    children = _children(table)
    skip = set(skip)
    found = {}

    def tree_of(root):
        tree = [p for p in _tree(root, children) if p not in skip]
        if any(NEVER.search(table.get(p, (0, ""))[1]) for p in tree):
            return None  # drzewo z czymś świętym w środku (np. agent pod node) nie jest pomocnikiem
        return tree

    def add(code, root, tree, job=None):
        size = sum(size_of(p) or 0 for p in tree)
        found.setdefault(code, []).append((size, age_of(root), root, tree, job))

    taken = set()
    # joby schedulera: dzieci jego powłoki (sama powłoka jest święta), razem jako jedno drzewo
    for job in jobs:
        shell = job.get("child_pgid")
        if not shell or shell not in table:
            continue
        roots = [c for c in children.get(shell, []) if c not in skip]
        tree = []
        for root in roots:
            sub = tree_of(root)
            if sub is None:
                tree = []
                break
            tree += sub
        if tree:
            add("job", roots[0], tree, job)
            taken.update(tree)
    for pid in table:
        if pid in skip or pid in taken:
            continue
        code = classify(pid, table, owned)
        if code is None:
            continue
        # korzeń drzewa: rodzic tej samej klasy przejmuje dziecko (vitest i jego wątki,
        # `go test` i binarka testu, Chrome i jego renderery)
        parent = table[pid][0]
        if parent in table and classify(parent, table, owned) == code and parent not in skip:
            continue
        tree = tree_of(pid)
        if tree:
            add(code, pid, tree)
    # każde inne drzewo komendy agenta; CLASSES wpuszcza je dopiero przy awarii
    claimed = taken | {p for items in found.values() for _s, _a, _r, tree, _j in items for p in tree}
    for root in agent_command_roots(table):
        if root in skip or root in claimed:
            continue
        tree = tree_of(root)
        if tree:
            add("agent", root, tree)
    if ram:
        runaway = s["runaway_percent"] / 100 * ram
        for pid, (_ppid, command) in table.items():
            if pid in skip or not command or NEVER.search(command):
                continue
            size = size_of(pid) or 0
            if size >= runaway:
                found.setdefault("runaway", []).append((size, age_of(pid), pid, [pid], None))
    for code, label, min_size, min_age, min_level in CLASSES:
        if level < min_level or (code == "runaway" and not ram):
            continue
        if level >= 3:
            min_age = 0
        ready = [
            (size, pid, tree, job)
            for size, age, pid, tree, job in found.get(code, [])
            if (min_size is None or size >= min_size) and age is not None and age >= min_age
        ]
        if ready:
            size, pid, tree, job = max(ready, key=lambda r: r[0])
            return code, label, pid, tree, size, job
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


def resume_of(pid, job, core):
    """Komenda, którą da się postawić to, co zgasło: z joba schedulera albo z argv i cwd procesu."""
    if job:
        where = ((job.get("agent") or {}).get("worktree") or "").replace("~", os.path.expanduser("~"), 1)
        cmd = job.get("cmd") or job.get("label") or ""
        return f"cd {shlex.quote(where)} && {cmd}" if where else cmd
    argv = core.proc_argv(pid) if hasattr(core, "proc_argv") else None
    cwd = core.proc_cwd(pid) if hasattr(core, "proc_cwd") else None
    if not argv:
        return None
    return (f"cd {shlex.quote(cwd)} && " if cwd else "") + shlex.join(argv)


def sched_jobs(core):
    """Lokalne joby schedulera, które biegną (sched/state.json), albo []."""
    path = os.path.join(core.STATE_DIR, "sched", "state.json") if hasattr(core, "STATE_DIR") else None
    data = core.janitor.load_json(path, {}) if path and hasattr(core.janitor, "load_json") else {}
    return [j for j in (data or {}).get("running", []) if j.get("where") == "local" and j.get("child_pgid")]


def reap(world, state, core, level=2, cfg=None, dry_run=False):
    """Jedno drzewo mniej; zwraca opis akcji albo None.

    core: moduł devguard_core (usage, terminate, log, janitor, mach time); level: stopień z
    `stage`; dry_run: tylko wybór, bez sygnałów, wpisu do logu i powiadomienia."""
    s = settings(cfg or {})
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

    ram = getattr(getattr(world, "pressure", None), "ram", None)
    pick = choose(table, size_of, age_of, skip, owned, level=level, jobs=sched_jobs(core), ram=ram, cfg=cfg)
    if pick is None:
        return None
    code, label, pid, tree, size, job = pick
    command = (job or {}).get("label") or table.get(pid, (0, ""))[1]
    # przeglądarkę i serwer LSP stawia na nowo ich właściciel (test, edytor), więc bez wznowienia
    resume = None if code in ("headless", "gopls", "lsp", "git") else resume_of(pid, job, core)
    if resume and len(resume) > 400:
        resume = resume[:400] + "…"
    if dry_run:
        result = "próba, bez sygnałów"
    else:

        class _Tree:  # terminate() strażnika chce obiektu z .pids
            pids = tree

        grace = s["emergency_grace_seconds"] if level >= 3 else s["brake_grace_seconds"]
        left = core.terminate(_Tree, table, grace=grace)
        result = "zatrzymany" if not left else f"nie chcą zginąć: {left}"
    line = (
        f"ostatnia linia ({STAGE_NAMES[min(level, 3)]}): {label} pid {pid} {core.janitor.human(size)} "
        f"({len(tree)} proc.): {command[:160]} -> {result}"
    )
    if resume:
        line += f" | wznowienie: {resume}"
    if dry_run:
        return line
    core.log(line)
    events = state.setdefault("lastresort", [])
    events.append({"at": world.now, "code": code, "pid": pid, "size": size, "result": result,
                   "level": level, "resume": resume})  # fmt: skip
    del events[:-50]
    try:
        notify = getattr(core, "notify_quiet", None) or core.janitor.notify
        notify("Strażnik: ostatnia linia", f"{label}: {core.janitor.human(size)} zwolnione")
    except Exception:
        pass
    return line
