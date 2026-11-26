#!/usr/bin/env python3
"""Map functions of one C/C++ file: definition range + which of the listed names / regexes each body uses.

usage:
  fnmap.py FILE name1 [name2 ...] [-- regex1 regex2 ...]

  name   : function name as written at the definition, e.g. NatApp::AddEntry or ipa_nl_recv_msg
  regex  : extra patterns to look for in each body (e.g. 'ioctl\\(' 'PostEvt' 'cache\\[')

Output per name:  name: start-end (N lines[, K defs]) -> [called names] [matched regexes]
Uses the same locating rule as build_arch.py (definition line, '{' alone on a following line,
body ends at the first '}' in column 0), so "NOT FOUND" here means build_arch.py will fail too.
"""
import re
import sys


def locate(lines, name, nth=1):
    pat = re.compile(r"^(?:[A-Za-z_~].*[\s*&])?" + re.escape(name) + r"\s*(\(|$)")
    hits = []

    for i, line in enumerate(lines):
        if not pat.match(line) or line.rstrip().endswith(";"):
            continue

        for j in range(i, min(i + 12, len(lines))):
            t = lines[j].rstrip()

            if t == "{":
                hits.append(i)
                break

            if t.endswith(";"):
                break

    if len(hits) < nth:
        return None

    s = hits[nth - 1]

    for e in range(s + 1, len(lines)):
        if lines[e].rstrip() == "}":
            return s + 1, e + 1, len(hits)

    return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2

    path = argv[0]
    rest = argv[1:]
    names = rest[:rest.index("--")] if "--" in rest else rest
    extra = rest[rest.index("--") + 1:] if "--" in rest else []
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")

    for n in names:
        r = locate(lines, n)

        if not r:
            print(f"{n}: NOT FOUND")
            continue

        s, e, k = r
        body = "\n".join(lines[s:e - 1])
        calls = [m.split("::")[-1] for m in names if m != n and re.search(r"\b" + re.escape(m.split("::")[-1]) + r"\s*\(", body)]
        ex = [x for x in extra if re.search(x, body)]
        more = f", {k} defs" if k > 1 else ""

        print(f"{n}: {s}-{e} ({e - s + 1} lines{more}) -> {calls} {ex}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
