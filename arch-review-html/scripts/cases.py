#!/usr/bin/env python3
"""List the case labels of a switch inside a line range, with what each case calls and which events it posts.

usage:
  cases.py FILE START END [call-regex]

  call-regex : identifiers to report as calls (default: handle*/Handle*/Trigger*/Process*/post*/config*/
               install*/del*/init*/create*/Update*/Add*/Del*/Reset* followed by '(')

Output per case:  start-end NAME: [calls] [events assigned to evt_data.event]
Case ranges use the same rule as build_arch.py: a case ends right before the next case label whose
indentation is less than 4 columns deeper; empty fall-through cases are reported as their own 1-line
range here (build_arch.py merges them with the following case).
"""
import re
import sys

DEFAULT_CALLS = r"\b((?:handle|Handle|Trigger|Process|post|config|install|del|init|create|Update|eth_bridge|query|Add|Del|Reset)[A-Za-z_0-9]*)\s*\("


def width(ws):
    return len(ws.replace("\t", "    "))


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2

    path, a, b = argv[0], int(argv[1]), int(argv[2])
    call_re = re.compile(argv[3] if len(argv) > 3 else DEFAULT_CALLS)
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    cases = []

    for i in range(a - 1, min(b, len(lines))):
        m = re.match(r"^(\s*)(?:case\s+([A-Za-z_0-9:]+)\s*:|(default)\s*:)", lines[i])

        if m:
            cases.append((i, width(m.group(1)), m.group(2) or m.group(3)))

    for k, (i, w, name) in enumerate(cases):
        end = b - 1

        for (i2, w2, _) in cases[k + 1:]:
            if w2 < w + 4:
                end = i2 - 1
                break

        body = "\n".join(lines[i + 1:end + 1])
        calls = sorted(set(m.group(1) for m in call_re.finditer(body)))
        events = sorted(set(re.findall(r"\.event\s*=\s*([A-Z_][A-Z_0-9]+)", body)))

        print(f"{i + 1}-{end + 1} {name}: {calls} {events}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
