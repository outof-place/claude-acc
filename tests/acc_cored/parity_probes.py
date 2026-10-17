#!/usr/bin/env python3
"""acc-cored's kernel readings against devguard_core's, on the live Mac, read back to back.

    python3 -I tests/acc_cored/parity_probes.py <acc-cored binary> [--rounds N]

Each round reads Python, native, Python. A row counts only when both Python reads agree on it
(the process didn't start, exit or exec in between); then the native row must equal it, and it
can't be missing natively (it lived through the native read). Both ways: what the native read has
and neither Python read has may only be a short-lived process or socket, a few per round. Sockets
the same way, per listening port and per link, links in lsof's order. Prints the counts; exit 1
on any difference, on rows missing natively, on too many native extras, or when nothing compared.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import devguard_core as dg  # noqa: E402


def native(binary, what):
    out = subprocess.run([binary, what], capture_output=True, text=True, check=True).stdout
    return out


def lines(text):
    # not splitlines(): U+0085 and U+2028 in a command line are no line breaks for the native side
    return [line for line in text.split("\n") if line]


def main(argv):
    binary = argv[0]
    rounds = int(argv[argv.index("--rounds") + 1]) if "--rounds" in argv else 5
    same = diff = skipped = extra = 0
    port_same = port_diff = port_extra = 0
    examples = []
    for _ in range(rounds):
        a = {pid: (ppid, cmd) for pid, ppid, cmd in dg.processes()}
        rows = [json.loads(line) for line in lines(native(binary, "scan"))]
        b = {pid: (ppid, cmd) for pid, ppid, cmd in dg.processes()}
        order_a = [pid for pid in a if pid in b and a[pid] == b[pid]]
        mine = {r["pid"]: (r["ppid"], r["command"]) for r in rows}
        for pid in order_a:
            if pid not in mine:
                skipped += 1
                if len(examples) < 5:
                    examples.append({"pid": pid, "python": a[pid], "native": None})
                continue
            if mine[pid] == a[pid]:
                same += 1
            else:
                diff += 1
                if len(examples) < 5:
                    examples.append({"pid": pid, "python": a[pid], "native": mine[pid]})
        # the order ps sorts by (terminal, pid) must hold too, over the stable rows
        stable = [r["pid"] for r in rows if r["pid"] in a and r["pid"] in b and a[r["pid"]] == b[r["pid"]]]
        if stable != [pid for pid in order_a if pid in mine]:
            diff += 1
            examples.append({"order": "differs"})
        extra += sum(1 for r in rows if r["pid"] not in a and r["pid"] not in b)
        la, links_a = dg.sockets()
        sock = json.loads(native(binary, "sockets"))
        lb, links_b = dg.sockets()
        for port, pids in la.items():
            if lb.get(port) != pids:
                continue
            if set(sock["listen"].get(str(port), [])) == pids:
                port_same += 1
            else:
                port_diff += 1
                examples.append({"port": port, "python": sorted(pids), "native": sock["listen"].get(str(port))})
        port_extra += sum(1 for port in sock["listen"] if int(port) not in la and int(port) not in lb)
        stable_links = [link for link in links_a if link in links_b]
        mine_links = [tuple(link) for link in sock["links"]]
        for link in stable_links:
            if link in mine_links:
                port_same += 1
            else:
                port_diff += 1
                examples.append({"link": link})
        common = set(stable_links) & set(mine_links)
        if [link for link in mine_links if link in common] != [link for link in stable_links if link in common]:
            port_diff += 1
            examples.append({"links": "order differs"})
        port_extra += sum(1 for link in mine_links if link not in links_a and link not in links_b)
    print(json.dumps({"rows_same": same, "rows_diff": diff, "rows_gone_native": skipped, "rows_extra_native": extra,
                      "sockets_same": port_same, "sockets_diff": port_diff, "sockets_extra_native": port_extra,
                      "examples": examples[:8]}, indent=1))  # fmt: skip
    # a process that started and ended between the reads, a socket opened and closed: a few a round
    too_many = extra > 20 * rounds or port_extra > 5 * rounds
    return 1 if diff or port_diff or skipped or too_many or same == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
