#!/usr/bin/env python3
"""The guard's patterns in acc-cored (PyRegex on ICU) against Python's re, on a corpus.

    python3 -I tests/acc_cored/parity_regex.py <acc-cored binary> [--fuzz N]

The corpus is every command line on this Mac (devguard_core.processes()) plus fuzzed lines built
from the patterns' own words with the characters where ICU and Python differ (\\v, \\x1c-\\x1f,
\\x85, NBSP, U+2028, combining marks, non-ASCII digits and letters). For each pattern and line the
answers of re.search, re.match and search().groups() must agree. Exit 1 on any difference.
"""

import json
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import devguard_core as dg  # noqa: E402

WORDS = ["next", "dev", "vite", "serve", "expo", "start", "webpack", "astro", "storybook", "nuxi", "nuxt", "pnpm", "npm",
         "npx", "yarn", "bun", "rtk", "turbo", "node", "sh", "zsh", "bash", "fish", "-c", "claude", "codex", "Safari",
         "Google Chrome", "Chrome for Testing", "launchd_sim", "serve-sim", "simctl", "booted", "--watch", "-w", "gopls",
         "git", "Pod.app", "Orca.app", "login", "launchd", "/", "--", "metro", "react-native", "vitest", "watch",
         "CoreSimulator/Devices/0A1B2C3D-0000-4000-8000-000000000001/", "Docker.app", "tsserver.js", "-zsh", "-il"]  # fmt: skip
ODD = ["\v", "\x1c", "\x1f", "\x85", "\xa0", "\u2028", "\u0301", "\u0663", "é", "ß", "_", "-", " ", "\t", "\n", "x"]


def fuzz(rng, n):
    out = []
    for _ in range(n):
        parts = []
        for _ in range(rng.randint(1, 6)):
            w = rng.choice(WORDS)
            if rng.random() < 0.3:
                w = rng.choice(["/usr/bin/", "/x/node_modules/.bin/", "/a/b/", ""]) + w
            parts.append(w)
            parts.append(rng.choice([" ", " ", " ", rng.choice(ODD)]))
        if rng.random() < 0.3:
            parts.insert(rng.randrange(len(parts) + 1), rng.choice(ODD))
        out.append("".join(parts))
    return out


def main(argv):
    binary = argv[0]
    n = int(argv[argv.index("--fuzz") + 1]) if "--fuzz" in argv else 3000
    patterns = subprocess.run([binary, "regex-check", "--patterns"], capture_output=True, text=True, check=True).stdout.splitlines()
    corpus = [cmd for _pid, _ppid, cmd in dg.processes()] + fuzz(random.Random(7), n)
    corpus = [c for c in corpus if "\t" not in c and "\n" not in c] + [c for c in corpus if "\t" in c or "\n" in c]
    lines, expect = [], []
    for i, pat in enumerate(patterns):
        rx = re.compile(pat)
        for text in corpus:
            enc = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t") if False else text.replace("\n", "\\n").replace("\t", "\\t")
            if "\\n" in text or "\\t" in text:
                continue  # the line protocol can't carry a literal backslash-n
            lines.append(f"{i}\t{enc}")
            m = rx.search(text)
            expect.append((i, text, int(bool(m)), int(bool(rx.match(text))), list(m.groups()) if m else None))
    got = subprocess.run([binary, "regex-check"], input="\n".join(lines) + "\n", capture_output=True, text=True, check=True).stdout.splitlines()
    diff = 0
    for (i, text, s, m, g), line in zip(expect, got):
        a, b, groups = line.split(" ", 2)
        if (int(a), int(b), json.loads(groups)) != (s, m, g):
            diff += 1
            if diff <= 8:
                print(f"pattern {i} {patterns[i][:60]!r} on {text[:80]!r}: python {s} {m} {g}, native {line}")
    print(f"patterns {len(patterns)}, lines {len(corpus)}, checks {len(expect)}, different {diff}")
    return 1 if diff or len(got) != len(expect) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
