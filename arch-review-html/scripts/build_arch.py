#!/usr/bin/env python3
"""Build an interactive architecture review page from <name>.json + the C/C++ sources it maps.

The JSON spec is the single source of truth: blocks -> source line ranges, edges,
flows, pitfalls, nested sub-levels, and the top-level diagram layout ("view").
This script validates every anchor against the current sources, resolves
sub-levels by function / case name, derives call edges, and writes one
self-contained <name>.html next to the JSON (no network access needed).

usage:
  python3 build_arch.py SPEC.json [SPEC2.json ...]     # build pages
  python3 build_arch.py --dump SPEC.json               # also print resolved sub-levels + edges
  python3 build_arch.py --fill-anchors SPEC.json       # fill missing anchor "text" from sources
exit code 1 if any anchor / range / function / case cannot be resolved.

See references/spec-format.md in the arch-review-html skill for the JSON format.
"""
import json
import re
import sys
from pathlib import Path

VIEW_DEFAULTS = {"routes": {}, "bands": [], "cols": [], "nolabel": []}

def lineref(ref, default_file):
    """n -> [default_file, n];  "file:n" -> [file, n]."""
    if isinstance(ref, int):
        return [default_file, ref]

    f, n = ref.rsplit(":", 1)

    return [f, int(n)]


def normalize(spec, json_dir):
    """Return (normalized spec, {file key: absolute path})."""
    if "source" in spec:
        key = Path(spec["source"]).name
        paths = {key: (json_dir / spec["source"]).resolve()}
        root = None
        default = key
    else:
        root = (json_dir / spec["root"]).resolve()
        paths = {}
        default = None

    def use(f):
        if f not in paths:
            paths[f] = (root / f) if root else (json_dir / f).resolve()

        return f

    blk_file = {}

    for b in spec["blocks"]:
        bf = use(b.get("file", default))
        blk_file[b["id"]] = bf
        rs = []

        for r in b["ranges"]:
            rs.append([use(r[0]), r[1], r[2]] if len(r) == 3 else [bf, r[0], r[1]])

        b["ranges"] = rs
        b["file"] = bf

        if b.get("anchor"):
            b["anchor"]["file"] = use(b["anchor"].get("file", bf))

        b["highlight"] = [lineref(x, bf) for x in b.get("highlight", [])]

        for x in b["highlight"]:
            use(x[0])

    for f in spec["flows"]:
        for st in f["steps"]:
            st["lines"] = [lineref(x, blk_file.get(st["block"], default)) for x in st.get("lines", [])]

            for x in st["lines"]:
                use(x[0])

    for p in spec.get("pitfalls", []):
        pf = blk_file.get(p["blocks"][0], default)
        p["lines"] = [lineref(x, pf) for x in p.get("lines", [])]

        for x in p["lines"]:
            use(x[0])

    return spec, paths


def read_lines(path):
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")

    if lines and lines[-1] == "":
        lines.pop()

    return lines


def find_fn(lines, name, nth=1):
    """Locate the nth definition of function `name`; return ((start, end), n_defs), 1-based, end = closing '}' at column 0."""
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
        return None, len(hits)

    s = hits[nth - 1]

    for e in range(s + 1, len(lines)):
        if lines[e].rstrip() == "}":
            return (s + 1, e + 1), len(hits)

    return None, len(hits)


def indent_width(ws):
    return len(ws.replace("\t", "    "))


def find_case(lines, name, rng):
    """Locate `case name:` (or default) inside rng; empty fall-through cases are merged with the next one."""
    s, e = rng
    cases = []

    for i in range(s - 1, e):
        m = re.match(r"^(\s*)(?:case\s+([A-Za-z_0-9:]+)\s*:|(default)\s*:)", lines[i])

        if m:
            cases.append((i, indent_width(m.group(1)), m.group(2) or m.group(3)))

    for k, (i, w, n) in enumerate(cases):
        if n != name:
            continue

        cur = k
        end = e - 2

        while True:
            nxt = next((c for c in cases[cur + 1:] if c[1] < w + 4), None)

            if nxt is None:
                break

            body = [x for x in lines[cases[cur][0] + 1:nxt[0]] if x.strip() and not x.strip().startswith(("//", "/*", "*", "#"))]

            if body:
                end = nxt[0] - 1
                break

            cur = cases.index(nxt)

        return (i + 1, end + 1)

    return None


def short_name(fn):
    return fn.split("::")[-1]


def normalize_subs(spec, paths, get_lines, problems):
    """Resolve every nested "sub" (fn / case / ranges), assign full ids, derive call edges."""
    raw_index = {b["id"]: b for b in spec["blocks"]}

    def index(parent_id, sub):
        for n in sub["nodes"]:
            n["_full"] = f"{parent_id}.{n['id']}"
            raw_index[n["_full"]] = n

            if n.get("sub"):
                index(n["_full"], n["sub"])

    for b in spec["blocks"]:
        if b.get("sub"):
            index(b["id"], b["sub"])

    def target_patterns(n):
        if n.get("match"):
            return n["match"]

        if n.get("fn"):
            return [r"\b" + re.escape(short_name(n["fn"])) + r"\s*\("]

        if n.get("kind") == "ref":
            t = raw_index.get(n["ref"])

            if t is None:
                problems.append(f"[{n['_full']}] ref to unknown id {n['ref']}")
                return []

            if t.get("match"):
                return t["match"]

            if t.get("fn"):
                return [r"\b" + re.escape(short_name(t["fn"])) + r"\s*\("]

        return []

    def resolve(parent_id, parent_file, parent_rng, sub):
        out_nodes = []

        for raw in sub["nodes"]:
            nid = raw["_full"]
            kind = raw.get("kind", "fn")
            f = raw.get("file", parent_file)
            ranges = []
            lines = get_lines(f)

            if "fn" in raw:
                rng, cnt = find_fn(lines, raw["fn"], raw.get("nth", 1))

                if rng is None:
                    problems.append(f"[{nid}] function {raw['fn']} not found in {f} ({cnt} definitions)")
                else:
                    ranges = [[f, rng[0], rng[1]]]
            elif "case" in raw:
                within = parent_rng

                if raw.get("within"):
                    within, _ = find_fn(lines, raw["within"])

                rng = find_case(lines, raw["case"], within) if within else None

                if rng is None:
                    problems.append(f"[{nid}] case {raw['case']} not found in {f}")
                else:
                    ranges = [[f, rng[0], rng[1]]]
            elif "ranges" in raw:
                ranges = [[r[0], r[1], r[2]] if len(r) == 3 else [f, r[0], r[1]] for r in raw["ranges"]]

            if kind == "ref":
                t = raw_index.get(raw["ref"], {})
                title = raw.get("title") or t.get("title") or (short_name(t["fn"]) + "()" if t.get("fn") else raw["ref"])
            else:
                title = raw.get("title") or (short_name(raw["fn"]) + "()" if "fn" in raw else raw.get("case", raw["id"]))

            node = {
                "id": nid,
                "kind": kind,
                "title": title,
                "label": raw.get("label", ""),
                "file": f,
                "ranges": ranges,
                "highlight": [lineref(x, f) for x in raw.get("highlight", [])],
                "symbols": [raw["fn"]] if "fn" in raw else ([raw["case"]] if "case" in raw else []),
                "summary": raw.get("summary", ""),
                "details": raw.get("details", []),
                "callers": raw.get("callers", []),
            }

            if kind == "ref":
                node["ref"] = raw["ref"]

            if raw.get("sub"):
                node["sub"] = resolve(nid, f, ranges[0][1:] if ranges else None, raw["sub"])

            out_nodes.append(node)

        edges = []
        seen = set()

        for a_raw, a in zip(sub["nodes"], out_nodes):
            if a["kind"] in ("ref", "data") or not a["ranges"]:
                continue

            body = "\n".join("\n".join(get_lines(f)[s - 1:e]) for f, s, e in a["ranges"])

            for b_raw, b in zip(sub["nodes"], out_nodes):
                if b is a:
                    continue

                pats = target_patterns(b_raw)

                if pats and any(re.search(p, body) for p in pats) and (a["id"], b["id"]) not in seen:
                    seen.add((a["id"], b["id"]))
                    edges.append({"from": a["id"], "to": b["id"], "kind": "data" if b["kind"] == "data" else "call"})

        for e in sub.get("edges", []):
            fr = f"{parent_id}.{e['from']}"
            to = f"{parent_id}.{e['to']}"

            if (fr, to) not in seen:
                seen.add((fr, to))
                edges.append({"from": fr, "to": to, "kind": e.get("kind", "call"), "label": e.get("label", "")})

        return {"nodes": out_nodes, "edges": edges}

    for b in spec["blocks"]:
        if b.get("sub"):
            rng = next(([s, e] for f, s, e in b["ranges"] if f == b["file"]), None)
            b["sub"] = resolve(b["id"], b["file"], rng, b["sub"])


def check(spec, src, layout):
    """Return a list of human-readable problems; empty when everything matches."""
    problems = []
    ids = {b["id"] for b in spec["blocks"]}

    def in_file(f, n):
        return f in src and 1 <= n <= len(src[f])

    for b in spec["blocks"]:
        a = b.get("anchor")

        if a:
            f = a["file"]
            got = src[f][a["line"] - 1] if in_file(f, a["line"]) else None

            if "text" not in a:
                problems.append(f"[{b['id']}] anchor {f}:{a['line']} has no text (run --fill-anchors)")
            elif got != a["text"]:
                problems.append(f"[{b['id']}] anchor {f}:{a['line']}: expected {a['text']!r}, got {got!r}")

        for f, s, e in b["ranges"]:
            if not (in_file(f, s) and in_file(f, e) and s <= e):
                problems.append(f"[{b['id']}] range {f}:{s}-{e} out of bounds")

        for f, n in b["highlight"]:
            if not in_file(f, n):
                problems.append(f"[{b['id']}] highlight {f}:{n} out of bounds")

        if b["id"] not in layout:
            problems.append(f"[{b['id']}] has no layout entry")

    for e in spec["edges"]:
        if e["from"] not in ids or e["to"] not in ids:
            problems.append(f"edge {e['from']}->{e['to']} references unknown block")

    for fl in spec["flows"]:
        for st in fl["steps"]:
            if st["block"] not in ids:
                problems.append(f"flow {fl['id']} step references unknown block {st['block']}")

            for f, n in st["lines"]:
                if not in_file(f, n):
                    problems.append(f"flow {fl['id']} step line {f}:{n} out of bounds")

    return problems


def fill_anchors(json_path):
    """Add "text" to every anchor written as {"line": N} or {"file": F, "line": N}, keeping formatting."""
    raw = json_path.read_text(encoding="utf-8")
    spec, paths = normalize(json.loads(raw), json_path.parent)
    src = {f: read_lines(p) for f, p in paths.items()}
    todo = [b["anchor"] for b in spec["blocks"] if b.get("anchor") and "text" not in b["anchor"]]
    pat = re.compile(r'"anchor": \{(?:"file": "[^"]+", )?"line": (\d+)\}')
    it = iter(todo)

    def repl(m):
        a = next(it)
        text = src[a["file"]][a["line"] - 1]

        return m.group(0)[:-1] + ', "text": ' + json.dumps(text, ensure_ascii=False) + "}"

    out, n = pat.subn(repl, raw)
    json_path.write_text(out, encoding="utf-8")
    print(f"filled {n} anchors in {json_path.name}")


def js_embed(obj):
    """JSON for a <script> body: neutralise '</' so the source cannot close the tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(json_path):
    json_path = Path(json_path).resolve()
    name = json_path.stem
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    view = raw.pop("view", None)

    if not view or "layout" not in view:
        print(f"{name}: missing \"view\": {{\"w\", \"h\", \"layout\": {{block id: [x, y, w, h, [sub-lines]]}}}}", file=sys.stderr)
        return 1

    view = {**VIEW_DEFAULTS, **view}
    spec, paths = normalize(raw, json_path.parent)
    root = (json_path.parent / raw["root"]).resolve() if "root" in raw else json_path.parent
    src = {}

    def get_lines(f):
        if f not in src:
            paths.setdefault(f, root / f)
            src[f] = read_lines(paths[f])

        return src[f]

    for f in list(paths):
        get_lines(f)

    problems = []
    normalize_subs(spec, paths, get_lines, problems)
    problems += check(spec, src, view["layout"])

    if problems:
        print(f"{name}: anchor check FAILED (update {json_path.name}):", file=sys.stderr)

        for p in problems:
            print("  " + p, file=sys.stderr)

        return 1

    if DUMP:
        def dump(sub, depth):
            for n in sub["nodes"]:
                loc = ", ".join(f"{Path(f).name}:{s}-{e}" for f, s, e in n["ranges"])
                outs = [e["to"].split(".")[-1] for e in sub["edges"] if e["from"] == n["id"]]
                print(f"{'  ' * depth}{n['kind']:5} {n['id']:<45} {loc:<32} -> {outs}")

                if n.get("sub"):
                    dump(n["sub"], depth + 1)

        for b in spec["blocks"]:
            if b.get("sub"):
                print(f"## {b['id']}")
                dump(b["sub"], 1)

    html = (TEMPLATE
            .replace("__TITLE__", spec.get("title", name))
            .replace("__SPEC__", js_embed(spec))
            .replace("__VIEW__", js_embed(view))
            .replace("__SRC__", js_embed(src)))
    out = json_path.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    total = sum(len(v) for v in src.values())
    print(f"ok: {name}: {len(spec['blocks'])} blocks, {len(src)} files / {total} source lines -> {out.name}")

    return 0


DUMP = False


def main(argv):
    global DUMP

    if argv and argv[0] == "--dump":
        DUMP = True
        argv = argv[1:]

    if argv and argv[0] == "--fill-anchors":
        for p in argv[1:]:
            fill_anchors(Path(p).resolve())

        return 0

    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    rc = 0

    for p in argv:
        rc |= build(p)

    return rc


TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --fg: #1d2330; --muted: #677085; --line: #d9dde5;
  --band: #eef1f5; --code-bg: #fbfcfd; --hl: #fff4c2; --hl-strong: #ffe48a; --sel: #2563eb;
  --c0: #7c3aed; --c1: #2563eb; --c2: #0891b2; --c3: #059669; --c4: #d97706; --c5: #db2777;
  --c-ext: #64748b; --c-support: #a16207;
  --tk-kw: #8b2fc9; --tk-type: #0b6bcb; --tk-str: #b4461c; --tk-num: #0f7b6c;
  --tk-com: #8a93a3; --tk-pp: #b0277a; --tk-fn: #1f5fbf;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #12151b; --panel: #1a1e26; --fg: #e3e7ee; --muted: #949db0; --line: #2e3442;
    --band: #171b22; --code-bg: #151920; --hl: #3a3417; --hl-strong: #5a4b12; --sel: #60a5fa;
    --c0: #a78bfa; --c1: #60a5fa; --c2: #22d3ee; --c3: #34d399; --c4: #fbbf24; --c5: #f472b6;
    --c-ext: #94a3b8; --c-support: #eab308;
    --tk-kw: #c792ea; --tk-type: #82aaff; --tk-str: #f0a37a; --tk-num: #7fd1b9;
    --tk-com: #6b7385; --tk-pp: #f07ab8; --tk-fn: #82b1ff;
  }
}
:root[data-theme="dark"] {
  --bg: #12151b; --panel: #1a1e26; --fg: #e3e7ee; --muted: #949db0; --line: #2e3442;
  --band: #171b22; --code-bg: #151920; --hl: #3a3417; --hl-strong: #5a4b12; --sel: #60a5fa;
  --c0: #a78bfa; --c1: #60a5fa; --c2: #22d3ee; --c3: #34d399; --c4: #fbbf24; --c5: #f472b6;
  --c-ext: #94a3b8; --c-support: #eab308;
  --tk-kw: #c792ea; --tk-type: #82aaff; --tk-str: #f0a37a; --tk-num: #7fd1b9;
  --tk-com: #6b7385; --tk-pp: #f07ab8; --tk-fn: #82b1ff;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--fg); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "PingFang TC", sans-serif; }
code, .mono { font-family: ui-monospace, "JetBrains Mono", "Cascadia Code", Menlo, Consolas, monospace; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
header .sub { color: var(--muted); font-size: 12px; }
.spacer { flex: 1; }
button, select, input { font: inherit; color: var(--fg); background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; }
button { cursor: pointer; }
button:hover { border-color: var(--sel); }
button.on { background: var(--sel); color: #fff; border-color: var(--sel); }
main { display: grid; grid-template-columns: var(--leftw, 56%) 8px minmax(0, 1fr); height: calc(100% - 53px); }
#left { overflow: auto; padding: 12px 16px; min-width: 0; }
#split { cursor: col-resize; touch-action: none; background: linear-gradient(to right, transparent 3px, var(--line) 3px, var(--line) 5px, transparent 5px); }
#split:hover, #split:focus-visible, body.dragging #split { background: linear-gradient(to right, transparent 2px, var(--sel) 2px, var(--sel) 6px, transparent 6px); outline: none; }
body.dragging { cursor: col-resize; user-select: none; }
body.dragging iframe, body.dragging svg { pointer-events: none; }
#right { overflow: hidden; display: flex; flex-direction: column; background: var(--panel); }
.flowbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 8px; }
.flowbar .lbl { color: var(--muted); font-size: 12px; margin-right: 2px; }
#flowbtns { display: inline-flex; flex-wrap: wrap; gap: 6px; }
#stepinfo { min-height: 44px; margin: 0 0 8px; padding: 8px 12px; border-radius: 8px; background: var(--panel); border: 1px solid var(--line); font-size: 13px; }
#stepinfo .k { color: var(--muted); margin-right: 6px; }
svg#dg { width: 100%; height: auto; display: block; user-select: none; }
.band { fill: var(--band); }
.bandlbl, .collbl { fill: var(--muted); font-size: 11px; }
.blk rect { fill: var(--panel); stroke-width: 1.4; rx: 7; transition: opacity .15s; }
.blk { cursor: pointer; transition: opacity .15s; }
.blk .ttl { font-size: 12.5px; font-weight: 650; fill: var(--fg); }
.blk .sub { font-size: 10.5px; fill: var(--muted); }
.blk:hover rect { stroke-width: 2.4; }
.blk.sel rect { stroke-width: 3; filter: drop-shadow(0 0 4px var(--sel)); }
.blk.cur rect { stroke-width: 3.4; filter: drop-shadow(0 0 6px var(--sel)); }
.blk.ext rect:first-child { stroke-dasharray: 4 3; }
.edge path { fill: none; stroke: var(--muted); stroke-width: 1.3; opacity: .55; transition: opacity .15s, stroke .15s; }
.edge.data path { stroke-dasharray: 5 3; }
.edge.inherit path { stroke-dasharray: 1.5 3; stroke-width: 1.6; }
.edge text { font-size: 9.5px; fill: var(--muted); opacity: .8; }
.edge.on path { stroke: var(--sel); opacity: 1; stroke-width: 2.2; }
.edge.on text { fill: var(--sel); opacity: 1; }
svg.dim .blk:not(.lit) { opacity: .28; }
svg.dim .edge:not(.on) { opacity: .2; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 8px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.tabs { display: flex; gap: 4px; align-items: flex-end; padding: 8px 12px 0; border-bottom: 1px solid var(--line); }
.tabs button { border-bottom-left-radius: 0; border-bottom-right-radius: 0; border-bottom: none; }
.tabs select { margin-left: auto; margin-bottom: 4px; max-width: 55%; font-size: 12px; }
#info { padding: 12px 16px; overflow: auto; max-height: 45%; border-bottom: 1px solid var(--line); }
#info h2 { margin: 0 0 4px; font-size: 17px; }
#info h3 { margin: 10px 0 4px; font-size: 14px; }
#info .meta { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 8px; }
.chip { font-size: 11px; padding: 1px 8px; border-radius: 10px; border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.chip.sym { cursor: pointer; font-family: ui-monospace, Menlo, Consolas, monospace; }
.chip.sym:hover, .chip.rng:hover { border-color: var(--sel); color: var(--sel); }
.chip.rng { cursor: pointer; }
#info p { margin: 6px 0; }
#info ul { margin: 6px 0; padding-left: 20px; }
#info li { margin: 3px 0; }
.pit { margin-top: 8px; padding: 6px 10px; border-left: 3px solid var(--c4); background: var(--bg); border-radius: 0 6px 6px 0; font-size: 13px; }
.pit b { color: var(--c4); }
.pit .loc { color: var(--muted); font-size: 11px; }
.rel { font-size: 12.5px; color: var(--muted); margin-top: 6px; }
.rel a { color: var(--sel); cursor: pointer; text-decoration: none; margin-right: 8px; }
pre.cmd { background: var(--code-bg); border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; overflow-x: auto; font-size: 12px; }
#code { position: relative; flex: 1; overflow: auto; background: var(--code-bg); font: 12.5px/1.55 ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace; tab-size: 8; }
.ln { display: flex; white-space: pre; }
.ln .no { flex: 0 0 58px; text-align: right; padding-right: 10px; color: var(--muted); user-select: none; border-right: 3px solid transparent; opacity: .8; }
.ln .tx { padding-left: 10px; padding-right: 16px; }
.ln.inblk { background: color-mix(in srgb, var(--sel) 6%, transparent); }
.ln.hl { background: var(--hl); }
.ln.hl2 { background: var(--hl-strong); }
#code.full .ln { cursor: pointer; }
#code.full .ln:hover .no { color: var(--fg); }
.gap { padding: 2px 0 2px 68px; color: var(--muted); font-size: 11px; border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); background: var(--bg); }
.fhdr { position: sticky; top: 0; z-index: 1; padding: 3px 12px; font-size: 11.5px; font-weight: 600; color: var(--fg); background: var(--band); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.tk-kw { color: var(--tk-kw); } .tk-type { color: var(--tk-type); } .tk-str { color: var(--tk-str); }
.tk-num { color: var(--tk-num); } .tk-com { color: var(--tk-com); font-style: italic; } .tk-pp { color: var(--tk-pp); }
.tk-fn { color: var(--tk-fn); }
.hint { color: var(--muted); font-size: 13px; }
.crumb { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-height: 28px; margin-bottom: 6px; font-size: 13px; }
.crumb a { color: var(--sel); cursor: pointer; }
.crumb a:last-of-type { color: var(--fg); font-weight: 650; cursor: default; }
.crumb .sep { color: var(--muted); }
.crumb button { margin-left: 8px; font-size: 12px; padding: 2px 8px; }
.blk .badge { font-size: 12px; fill: var(--sel); font-weight: 700; }
button.drill, button.go { border-color: var(--sel); color: var(--sel); background: transparent; }
button.drill:hover, button.go:hover { background: var(--sel); color: #fff; }
@media (max-width: 900px) {
  main { grid-template-columns: 1fr; height: auto; }
  #left { border-bottom: 1px solid var(--line); }
  #split { display: none; }
  #right { height: 80vh; }
}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="sub" id="srcinfo"></span>
  <span class="spacer"></span>
  <input id="q" list="symlist" placeholder="搜尋函式 / 符號…" size="22">
  <datalist id="symlist"></datalist>
  <button id="theme" title="切換深淺色">◐</button>
</header>
<main>
  <section id="left">
    <div class="flowbar">
      <span class="lbl">流程：</span>
      <span id="flowbtns"></span>
      <button id="prev" title="上一步 (←)">◀</button>
      <button id="play" title="自動播放 (空白鍵)">▶ 播放</button>
      <button id="next" title="下一步 (→)">▶</button>
      <button id="clear" title="清除 (Esc)">清除</button>
    </div>
    <div id="stepinfo"></div>
    <div id="crumb" class="crumb"></div>
    <svg id="dg" role="img" aria-label="架構圖"></svg>
    <div class="legend" id="legend"></div>
  </section>
  <div id="split" role="separator" aria-orientation="vertical" tabindex="0" title="左右拖曳調整寬度；雙擊還原；聚焦後可用 ← → 微調"></div>
  <section id="right">
    <div id="info"></div>
    <div class="tabs">
      <button id="tabBlk" class="on">區塊源碼</button>
      <button id="tabFull">完整源碼</button>
      <select id="filesel" title="完整源碼要顯示的檔案"></select>
    </div>
    <div id="code"></div>
  </section>
</main>
<script>
const SPEC = __SPEC__;
const VIEW = __VIEW__;
const SRC = __SRC__;

const NS = "http://www.w3.org/2000/svg";
const FILES = Object.keys(SRC);
const HINT = "點選區塊看說明與對應源碼；有 ⊞ 的區塊可雙擊（或按資訊面板的「展開內部」）進入下一層。也可選一個流程逐步播放（← → 切換、空白鍵播放、Esc 清除）。";

/* Every node at every level: id -> { n, parent (container id or null), level } */
const NODE = {};

(function indexNodes(list, parent, level) {
  for (const n of list) {
    NODE[n.id] = { n, parent, level };

    if (n.sub) indexNodes(n.sub.nodes, n.id, level + 1);
  }
})(SPEC.blocks, null, 0);

const BLK = Object.fromEntries(Object.entries(NODE).map(([k, v]) => [k, v.n]));
const KIND_LABEL = { in: "對外介面", case: "事件分支", fn: "內部函式", data: "資料", ref: "外部 / 其他元件" };

const state = { sel: null, view: null, flow: null, step: -1, timer: null, tab: "blk", file: FILES[0], stepLines: new Set() };

function containerOf(id) {
  return NODE[id] ? NODE[id].parent : null;
}

function viewEdges() {
  return state.view ? BLK[state.view].sub.edges : SPEC.edges;
}

/* ---------- category colors: ext / support fixed, others by order ---------- */

const CAT_VAR = {};

(function assignColors() {
  let i = 0;

  for (const k of Object.keys(SPEC.categories)) {
    if (k === "ext") CAT_VAR[k] = "--c-ext";
    else if (k === "support") CAT_VAR[k] = "--c-support";
    else CAT_VAR[k] = `--c${i++ % 6}`;
  }
})();

function catColor(cat) {
  return `var(${CAT_VAR[cat] || "--c-ext"})`;
}

function nodeColor(n) {
  if (!n.kind) return catColor(n.category);
  if (n.kind === "ref") return BLK[n.ref] ? nodeColor(BLK[n.ref]) : "var(--c-ext)";
  if (n.kind === "data") return "var(--c-support)";
  if (n.kind === "case") return "var(--c0)";
  if (n.kind === "in") return "var(--c1)";

  return "var(--c3)";
}

function nodeCatLabel(n) {
  return n.kind ? KIND_LABEL[n.kind] : (SPEC.categories[n.category] || n.category);
}

/* ---------- syntax highlighting (per file, lazily, then split per line) ---------- */

const KW = new Set("if else for while do switch case default break continue return goto sizeof static const volatile extern struct enum union typedef inline class public private protected virtual new delete this template typename namespace using true false nullptr".split(" "));
const TY = new Set("void char short int long unsigned signed float double bool size_t ssize_t uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t u_int8_t sig_atomic_t va_list cpu_set_t pthread_t".split(" "));
const TOKRE = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(^[ \t]*#[ \t]*\w+)|(\b(?:0x[0-9a-fA-F]+|\d+)(?:ULL|UL|LL|L|U)?\b)|([A-Za-z_]\w*)(?=\s*\()|([A-Za-z_]\w*)/gm;
const HL_CACHE = {};

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function highlighted(file) {
  if (HL_CACHE[file]) return HL_CACHE[file];

  const text = SRC[file].join("\n");
  const toks = [];
  let last = 0;
  let m;

  TOKRE.lastIndex = 0;

  while ((m = TOKRE.exec(text))) {
    if (m.index > last)
      toks.push(["", text.slice(last, m.index)]);

    let cls = "";

    if (m[1]) cls = "tk-com";
    else if (m[2]) cls = "tk-str";
    else if (m[3]) cls = "tk-pp";
    else if (m[4]) cls = "tk-num";
    else if (m[5]) cls = KW.has(m[5]) ? "tk-kw" : "tk-fn";
    else if (m[6]) cls = KW.has(m[6]) ? "tk-kw" : TY.has(m[6]) ? "tk-type" : "";

    toks.push([cls, m[0]]);
    last = TOKRE.lastIndex;
  }

  toks.push(["", text.slice(last)]);

  /* Tokens may span newlines (block comments): close and reopen spans per line. */
  const out = [""];

  for (const [cls, s] of toks) {
    s.split("\n").forEach((p, i) => {
      if (i > 0) out.push("");
      if (p) out[out.length - 1] += cls ? `<span class="${cls}">${esc(p)}</span>` : esc(p);
    });
  }

  HL_CACHE[file] = out;

  return out;
}

/* Line -> owning block per file: the smallest non-external range wins. */
const OWNER = {};

(function buildOwner() {
  const size = {};

  for (const f of FILES) {
    OWNER[f] = new Array(SRC[f].length + 1).fill(null);
    size[f] = new Array(SRC[f].length + 1).fill(Infinity);
  }

  for (const { n: b } of Object.values(NODE)) {
    if (b.category === "ext" || b.kind === "ref") continue;

    for (const [f, s, e] of b.ranges) {
      for (let l = s; l <= e; l++) {
        if (e - s < size[f][l]) {
          size[f][l] = e - s;
          OWNER[f][l] = b.id;
        }
      }
    }
  }
})();

const key = (f, n) => `${f}:${n}`;
const base = f => f.split("/").pop();

/* ---------- diagram ---------- */

const svg = document.getElementById("dg");

function el(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);

  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);

  return n;
}

function autoRoute(a, b) {
  const [ax, ay, aw, ah] = VIEW.layout[a];
  const [bx, by, bw, bh] = VIEW.layout[b];
  const y0 = Math.max(ay, by);
  const y1 = Math.min(ay + ah, by + bh);
  const x0 = Math.max(ax, bx);
  const x1 = Math.min(ax + aw, bx + bw);

  if (y1 > y0) {
    const y = (y0 + y1) / 2;

    return bx > ax ? [[ax + aw, y], [bx, y]] : [[ax, y], [bx + bw, y]];
  }

  if (x1 > x0) {
    const x = (x0 + x1) / 2;

    return by > ay ? [[x, ay + ah], [x, by]] : [[x, ay], [x, by + bh]];
  }

  /* No overlap: elbow in the gap, then enter the facing side at mid-height.
     Last segment must be horizontal so the arrow meets the left or right edge
     at 90 degrees, not the corner. */
  const sy = ay + ah / 2;
  const ty = by + bh / 2;

  if (bx >= ax + aw) {
    const elbow = (ax + aw + bx) / 2;

    return [[ax + aw, sy], [elbow, sy], [elbow, ty], [bx, ty]];
  }

  const elbow = (bx + bw + ax) / 2;

  return [[ax, sy], [elbow, sy], [elbow, ty], [bx + bw, ty]];
}

const edgeEls = [];

function drawDefs() {
  const defs = el("defs", {}, svg);

  for (const [id, color] of [["arr", "var(--muted)"], ["arrOn", "var(--sel)"]]) {
    const mk = el("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);

    el("path", { d: "M0,0 L10,5 L0,10 z", fill: color }, mk);
  }
}

/* Title, ⊞ badge and mouse / keyboard handlers shared by every box at every level. */
function bindBox(g, b, x, y, w) {
  el("title", {}, g).textContent = b.summary + (b.sub ? "\n（雙擊展開內部）" : "");

  if (b.sub) {
    const badge = el("text", { class: "badge", x: x + w - 6, y: y + 15, "text-anchor": "end" }, g);

    badge.textContent = "⊞";
  }

  g.addEventListener("click", () => { stopFlow(true); select(b.id); });
  g.addEventListener("dblclick", () => { if (b.sub) drill(b.id); else if (b.kind === "ref") select(b.ref); });
  g.addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      stopFlow(true);
      if (b.sub && state.sel === b.id) drill(b.id);
      else select(b.id);
    }
  });
}

function renderView() {
  svg.innerHTML = "";
  edgeEls.length = 0;
  drawDefs();

  if (state.view) drawSub(BLK[state.view]);
  else {
    svg.style.width = "";
    svg.setAttribute("viewBox", `0 0 ${VIEW.w} ${VIEW.h}`);
    drawDiagram();
  }

  renderCrumb();
  renderLegend();
}

function renderCrumb() {
  const chain = [];

  for (let id = state.view; id; id = containerOf(id)) chain.unshift(id);

  let h = `<a data-view="">${esc(SPEC.title || "架構")}</a>`;

  for (const id of chain) h += ` <span class="sep">›</span> <a data-view="${esc(id)}">${esc(BLK[id].title)}</a>`;

  if (state.view) h += `<button id="up" title="回上一層 (Backspace)">⤴ 上一層</button>`;

  const c = document.getElementById("crumb");

  c.innerHTML = h;
  c.querySelectorAll("a[data-view]").forEach(a => a.addEventListener("click", () => goView(a.dataset.view || null)));

  const up = document.getElementById("up");

  if (up) up.onclick = () => goView(containerOf(state.view));
}

function goView(id) {
  if (id && state.flow) stopFlow(true);

  const from = state.view;

  state.view = id;
  renderView();

  /* Coming back up: keep the node we came out of selected so the user sees where they were. */
  if (from && containerOf(from) === id) select(from);
  else if (state.sel && containerOf(state.sel) === state.view) select(state.sel);
  else setLit([], () => false);
}

function drill(id) {
  if (!BLK[id] || !BLK[id].sub) return;

  stopFlow(true);
  state.view = id;
  renderView();
  state.sel = id;
  setLit([], () => false);
  renderInfo(BLK[id]);
  renderCode(true);
}

/* Layered layout for a sub-level: entry points left, internal functions by call depth, data, then references right. */
function layoutSub(sub) {
  const kindOf = n => n.kind || "fn";
  const rank = {};
  const fnIds = new Set(sub.nodes.filter(n => kindOf(n) === "fn").map(n => n.id));

  for (const n of sub.nodes) rank[n.id] = (kindOf(n) === "in" || kindOf(n) === "case") ? 0 : 1;

  for (let it = 0; it < fnIds.size + 1; it++) {
    let changed = false;

    for (const e of sub.edges) {
      if (!fnIds.has(e.to) || e.from === e.to || rank[e.from] === undefined) continue;

      const r = Math.min(rank[e.from] + 1, fnIds.size);

      if (r > rank[e.to]) {
        rank[e.to] = r;
        changed = true;
      }
    }

    if (!changed) break;
  }

  let maxFn = 0;

  for (const id of fnIds) maxFn = Math.max(maxFn, rank[id]);

  const hasData = sub.nodes.some(n => kindOf(n) === "data");
  const dataCol = maxFn + 1;
  const refCol = maxFn + (hasData ? 2 : 1);

  for (const n of sub.nodes) {
    if (kindOf(n) === "data") rank[n.id] = dataCol;
    if (kindOf(n) === "ref") rank[n.id] = refCol;
  }

  const W = 200;
  const H = 46;
  const GX = 64;
  const GY = 12;
  const TOP = 40;
  const cols = [];

  for (const n of sub.nodes) (cols[rank[n.id]] = cols[rank[n.id]] || []).push(n);

  const pos = {};
  let maxY = 0;

  cols.forEach((col, c) => {
    (col || []).forEach((n, i) => {
      const x = 16 + c * (W + GX);
      const y = TOP + i * (H + GY);

      pos[n.id] = [x, y, W, H];
      maxY = Math.max(maxY, y + H);
    });
  });

  const heads = cols.map((col, c) => {
    if (!col) return "";
    if (c === 0) return "對外介面 / 事件";
    if (c === dataCol && hasData) return "資料";
    if (c === refCol) return "外部 / 其他元件";
    if (c === 1) return "內部函式 →";

    return "";
  });

  return { pos, heads, w: Math.max(16 + cols.length * (W + GX), 900), h: maxY + 24, W, GX };
}

function drawSub(parent) {
  const sub = parent.sub;
  const L = layoutSub(sub);

  /* Sub-levels are drawn 1:1 (the left pane scrolls) so deep call chains stay readable. */
  svg.setAttribute("viewBox", `0 0 ${L.w} ${L.h}`);
  svg.style.width = `${L.w}px`;
  el("rect", { class: "band", x: 0, y: 0, width: L.w, height: L.h, rx: 10 }, svg);

  L.heads.forEach((t, c) => {
    if (t) el("text", { class: "collbl", x: 16 + c * (L.W + L.GX), y: 26 }, svg).textContent = t;
  });

  const eg = el("g", {}, svg);

  for (const e of sub.edges) {
    const a = L.pos[e.from];
    const b = L.pos[e.to];

    if (!a || !b) continue;

    const ay = a[1] + a[3] / 2;
    const by = b[1] + b[3] / 2;
    let d;

    if (b[0] > a[0]) {
      const ax = a[0] + a[2];
      const dx = Math.max(30, (b[0] - ax) / 2);

      d = `M${ax},${ay} C${ax + dx},${ay} ${b[0] - dx},${by} ${b[0]},${by}`;
    }
    else {
      /* Same column or backwards: loop out on the right side and come back in from the right. */
      const ax = a[0] + a[2];
      const bx = b[0] + b[2];
      const out = Math.max(ax, bx) + 36;

      d = `M${ax},${ay} C${out},${ay} ${out},${by} ${bx},${by}`;
    }

    const g = el("g", { class: `edge ${e.kind}` }, eg);
    const p = el("path", { d, "marker-end": "url(#arr)" }, g);

    if (e.label) el("text", { x: (a[0] + a[2] + b[0]) / 2, y: (ay + by) / 2 - 4, "text-anchor": "middle" }, g).textContent = e.label;

    edgeEls.push({ e, g, p });
  }

  const bg = el("g", {}, svg);

  for (const n of sub.nodes) {
    const [x, y, w, h] = L.pos[n.id];
    const col = nodeColor(n);
    const g = el("g", { class: "blk" + (n.kind === "ref" ? " ext" : ""), "data-id": n.id, tabindex: 0 }, bg);

    el("rect", { x, y, width: w, height: h, stroke: col }, g);
    el("rect", { x: x + 1, y: y + 1, width: 4, height: h - 2, rx: 2, style: `fill:${col}` }, g);
    el("text", { class: "ttl", x: x + 12, y: y + (n.label ? 19 : 28) }, g).textContent = clip(n.title, 26);

    if (n.label) el("text", { class: "sub", x: x + 12, y: y + 35 }, g).textContent = clip(n.label, 30);

    bindBox(g, n, x, y, w);
  }
}

function clip(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function renderLegend() {
  let h;

  if (state.view) {
    h = Object.entries(KIND_LABEL).map(([k, v]) => `<span><i style="background:${nodeColor({ kind: k, ref: "" })}"></i>${esc(v)}</span>`).join("");
    h += `<span>── 呼叫（由源碼掃描產生）</span><span>- - 讀寫資料</span><span>⊞ 可再展開</span>`;
  }
  else {
    const kinds = new Set(SPEC.edges.map(e => e.kind));

    h = Object.entries(SPEC.categories).map(([k, v]) => `<span><i style="background:${catColor(k)}"></i>${esc(v)}</span>`).join("");
    h += `<span>── 呼叫 / 控制</span><span>- - 資料</span>`;

    if (kinds.has("inherit")) h += `<span>··· 繼承</span>`;
    if (SPEC.blocks.some(b => b.sub)) h += `<span>⊞ 雙擊展開內部</span>`;
  }

  document.getElementById("legend").innerHTML = h;
}

function drawDiagram() {
  for (const b of VIEW.bands) {
    el("rect", { class: "band", x: b.x, y: b.y, width: b.w, height: b.h, rx: 10 }, svg);
    el("text", { class: "bandlbl", x: b.lx, y: b.ly, "text-anchor": b.anchor }, svg).textContent = b.label;
  }

  for (const c of VIEW.cols)
    el("text", { class: "collbl", x: c.x, y: c.y }, svg).textContent = c.text;

  const eg = el("g", {}, svg);
  const nolabel = new Set(VIEW.nolabel || []);

  for (const e of SPEC.edges) {
    const k = `${e.from}>${e.to}`;
    const pts = VIEW.routes[k] || autoRoute(e.from, e.to);
    const g = el("g", { class: `edge ${e.kind}` }, eg);
    const p = el("path", { d: "M" + pts.map(q => q.join(",")).join(" L"), "marker-end": "url(#arr)" }, g);

    if (e.both) p.setAttribute("marker-start", "url(#arr)");

    if (e.label && !nolabel.has(k)) {
      let best = 0;
      let bi = 0;

      for (let i = 0; i < pts.length - 1; i++) {
        const d = Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);

        if (d > best) {
          best = d;
          bi = i;
        }
      }

      const mx = (pts[bi][0] + pts[bi + 1][0]) / 2;
      const my = (pts[bi][1] + pts[bi + 1][1]) / 2;
      const vertical = pts[bi][0] === pts[bi + 1][0];

      el("text", { x: vertical ? mx + 4 : mx, y: vertical ? my : my - 4, "text-anchor": vertical ? "start" : "middle" }, g).textContent = e.label;
    }

    edgeEls.push({ e, g, p });
  }

  const bg = el("g", {}, svg);

  for (const b of SPEC.blocks) {
    const L = VIEW.layout[b.id];

    if (!L) continue;

    const [x, y, w, h, subs] = L;
    const g = el("g", { class: "blk" + (b.category === "ext" ? " ext" : ""), "data-id": b.id, tabindex: 0 }, bg);
    const col = catColor(b.category);

    el("rect", { x, y, width: w, height: h, stroke: col }, g);
    el("rect", { x: x + 1, y: y + 1, width: w - 2, height: 4, rx: 3, style: `fill:${col}` }, g);

    const lines = 1 + subs.length;
    const top = y + h / 2 - (lines - 1) * 7.5 + 4;

    el("text", { class: "ttl", x: x + w / 2, y: top, "text-anchor": "middle" }, g).textContent = b.title;

    subs.forEach((s, i) => {
      el("text", { class: "sub", x: x + w / 2, y: top + 16 + i * 14, "text-anchor": "middle" }, g).textContent = s;
    });

    bindBox(g, b, x, y, w);
  }
}

function setLit(ids, onEdge) {
  const lit = new Set(ids);

  svg.classList.toggle("dim", lit.size > 0);

  for (const g of svg.querySelectorAll(".blk")) {
    const id = g.dataset.id;

    g.classList.toggle("lit", lit.has(id));
    g.classList.toggle("sel", id === state.sel && !state.flow);
    g.classList.toggle("cur", !!state.flow && id === state.sel);
  }

  for (const x of edgeEls) {
    const on = onEdge(x.e);

    x.g.classList.toggle("on", on);
    x.p.setAttribute("marker-end", on ? "url(#arrOn)" : "url(#arr)");

    if (x.e.both) x.p.setAttribute("marker-start", on ? "url(#arrOn)" : "url(#arr)");
  }
}

/* ---------- selection / info panel ---------- */

function select(id) {
  const b = BLK[id];

  if (!b) return;

  const want = containerOf(id);

  if (state.view !== want) {
    if (want && state.flow) stopFlow(true);

    state.view = want;
    renderView();
  }

  state.sel = id;

  if (!state.flow) {
    const nb = new Set([id]);

    for (const e of viewEdges()) {
      if (e.from === id) nb.add(e.to);
      if (e.to === id) nb.add(e.from);
    }

    setLit([...nb], e => e.from === id || e.to === id);
  }

  renderInfo(b);
  renderCode(true);
}

function locText(lines) {
  return lines.map(([f, n]) => `${base(f)}:${n}`).join(", ");
}

function renderInfo(b, note) {
  const info = document.getElementById("info");
  const parent = containerOf(b.id);
  const edges = parent ? BLK[parent].sub.edges : SPEC.edges;
  const out = [...new Set(edges.filter(e => e.from === b.id).map(e => e.to))];
  const inc = [...new Set(edges.filter(e => e.to === b.id).map(e => e.from))];
  const inRange = ([f, n]) => b.ranges.some(([rf, s, e]) => rf === f && n >= s && n <= e);
  const pits = (SPEC.pitfalls || []).filter(p => p.blocks.includes(b.id) || (parent && p.lines.some(inRange)));
  const col = nodeColor(b);
  let h = "";

  h += `<h2>${esc(b.title)}</h2>`;
  h += `<div class="meta"><span class="chip" style="border-color:${col};color:${col}">${esc(nodeCatLabel(b))}</span>`;
  h += b.ranges.map(([f, s, e], i) => `<span class="chip rng mono" data-ri="${i}" title="${esc(f)}">${esc(base(f))}:${s === e ? s : s + "-" + e}</span>`).join("");
  h += `</div>`;

  if (b.external) h += `<div class="mono" style="font-size:12px;color:var(--muted)">${esc(b.external)}</div>`;
  if (note) h += `<div class="pit" style="border-left-color:var(--sel)"><b style="color:var(--sel)">此步驟：</b>${esc(note)}</div>`;

  if (b.kind === "ref" && BLK[b.ref]) {
    const t = BLK[b.ref];

    h += `<p>${esc(b.label ? b.label + "：" : "")}${esc(t.summary)}</p>`;
    h += `<p><button class="go" data-go="${esc(b.ref)}">前往「${esc(t.title)}」</button></p>`;
  }
  else {
    h += `<p>${esc(b.summary)}</p>`;
  }

  if (b.sub) {
    const cnt = b.sub.nodes.filter(n => n.kind !== "ref").length;

    h += `<p><button class="drill" data-drill="${esc(b.id)}">⊞ 展開內部（${cnt} 個介面 / 函式 / 分支）</button></p>`;
  }

  if (b.details.length) h += `<ul>${b.details.map(d => `<li>${esc(d)}</li>`).join("")}</ul>`;
  if (b.callers && b.callers.length) h += `<div class="rel">被呼叫自：${b.callers.map(i => BLK[i] ? `<a data-go="${i}">${esc(BLK[i].title)}</a>` : esc(i)).join("")}</div>`;
  if (b.symbols.length) h += `<div class="meta">${b.symbols.map(s => `<span class="chip sym" data-sym="${esc(s)}">${esc(s)}</span>`).join("")}</div>`;

  for (const p of pits)
    h += `<div class="pit"><b>坑：</b>${esc(p.text)} <span class="loc mono">(${esc(locText(p.lines))})</span></div>`;

  const link = ids => ids.map(i => `<a data-go="${i}">${esc(BLK[i].title)}</a>`).join("") || "—";

  h += `<div class="rel">來自：${link(inc)}</div><div class="rel">通往：${link(out)}</div>`;
  info.innerHTML = h;
  info.scrollTop = 0;

  info.querySelectorAll("[data-go]").forEach(a => a.addEventListener("click", () => { stopFlow(true); select(a.dataset.go); }));
  info.querySelectorAll("[data-drill]").forEach(a => a.addEventListener("click", () => drill(a.dataset.drill)));
  info.querySelectorAll(".chip.sym").forEach(c => c.addEventListener("click", () => jumpToSymbol(c.dataset.sym)));
  info.querySelectorAll(".chip.rng").forEach(c => c.addEventListener("click", () => {
    const [f, s] = b.ranges[+c.dataset.ri];

    scrollToLine(f, s);
  }));
}

function renderOverview() {
  const info = document.getElementById("info");
  let h = `<span class="hint">尚未選擇區塊。</span>`;

  if (SPEC.verify && SPEC.verify.length) {
    h += `<h3>板上驗證</h3>`;

    for (const v of SPEC.verify)
      h += `<p>${esc(v.text)}</p>` + (v.cmd ? `<pre class="cmd">${esc(v.cmd)}</pre>` : "");
  }

  if (SPEC.pitfalls && SPEC.pitfalls.length)
    h += `<p class="hint">共 ${SPEC.pitfalls.length} 個已知的坑，點區塊即可看到相關的那幾條。</p>`;

  info.innerHTML = h;
}

/* ---------- code panel ---------- */

const codeEl = document.getElementById("code");
const fileSel = document.getElementById("filesel");

function lineHtml(f, n, cls) {
  const owner = OWNER[f][n];
  const bar = owner ? `style="border-right-color:${nodeColor(BLK[owner])}"` : "";
  const html = highlighted(f)[n - 1];

  return `<div class="ln ${cls}" data-f="${esc(f)}" data-n="${n}"><span class="no" ${bar}>${n}</span><span class="tx">${html || " "}</span></div>`;
}

function lineClass(f, n, b) {
  const k = key(f, n);

  if (state.stepLines.has(k)) return "hl2";
  if (b && b._hl && b._hl.has(k)) return "hl";
  if (b && b.ranges.some(([rf, s, e]) => rf === f && n >= s && n <= e)) return "inblk";

  return "";
}

function renderCode(resetFile) {
  const b = state.sel ? BLK[state.sel] : null;
  let h = "";

  if (b && !b._hl) b._hl = new Set(b.highlight.map(([f, n]) => key(f, n)));

  codeEl.classList.toggle("full", state.tab === "full");
  fileSel.style.display = state.tab === "full" ? "" : "none";

  if (state.tab === "full") {
    if (resetFile && b) state.file = (b.ranges[0] || [state.file])[0];

    fileSel.value = state.file;

    const lines = SRC[state.file];

    for (let n = 1; n <= lines.length; n++) h += lineHtml(state.file, n, lineClass(state.file, n, b));
  }
  else if (b) {
    const order = f => FILES.indexOf(f);
    const rs = [...b.ranges].sort((p, q) => order(p[0]) - order(q[0]) || p[1] - q[1]);
    let prev = null;

    for (const [f, s, e] of rs) {
      if (!prev || prev[0] !== f) h += `<div class="fhdr mono">${esc(f)}</div>`;
      else if (s > prev[2] + 1) h += `<div class="gap">⋯ 第 ${prev[2] + 1}–${s - 1} 行省略 ⋯</div>`;

      for (let n = Math.max(s, prev && prev[0] === f ? prev[2] + 1 : s); n <= e; n++) h += lineHtml(f, n, lineClass(f, n, b));

      prev = [f, s, Math.max(e, prev && prev[0] === f ? prev[2] : e)];
    }
  }
  else {
    h = `<div class="hint" style="padding:16px">選一個區塊即可在這裡看到對應源碼；「完整源碼」分頁可選檔案，點任一行反查所屬區塊。</div>`;
  }

  codeEl.innerHTML = h;

  const firstStep = [...state.stepLines][0];

  if (firstStep) {
    const i = firstStep.lastIndexOf(":");

    scrollToLine(firstStep.slice(0, i), +firstStep.slice(i + 1), true);
  }
  else if (b && state.tab === "full") {
    const r = b.ranges.find(([f]) => f === state.file);

    if (r) scrollToLine(r[0], r[1], true);
  }
  else {
    codeEl.scrollTop = 0;
  }
}

function findRow(f, n) {
  for (const row of codeEl.querySelectorAll(`.ln[data-n="${n}"]`))
    if (row.dataset.f === f) return row;

  return null;
}

function scrollToLine(f, n, quiet) {
  let row = findRow(f, n);

  if (!row && !quiet) {
    state.file = f;
    setTab("full", false);
    row = findRow(f, n);
  }

  if (row) codeEl.scrollTop = row.offsetTop - 40;
}

codeEl.addEventListener("click", ev => {
  if (state.tab !== "full") return;

  const row = ev.target.closest(".ln");

  if (!row) return;

  const id = OWNER[row.dataset.f][+row.dataset.n];

  if (id && id !== state.sel) {
    const keep = codeEl.scrollTop;

    stopFlow(true);
    select(id);
    codeEl.scrollTop = keep;
  }
});

function setTab(t, resetFile = true) {
  state.tab = t;
  document.getElementById("tabBlk").classList.toggle("on", t === "blk");
  document.getElementById("tabFull").classList.toggle("on", t === "full");
  renderCode(resetFile);
}

document.getElementById("tabBlk").onclick = () => setTab("blk");
document.getElementById("tabFull").onclick = () => setTab("full");

fileSel.innerHTML = FILES.map(f => `<option value="${esc(f)}">${esc(f)}（${SRC[f].length} 行）</option>`).join("");
fileSel.onchange = () => {
  state.file = fileSel.value;
  renderCode(false);
};

/* ---------- flows ---------- */

const stepInfo = document.getElementById("stepinfo");

function flowBtns() {
  const box = document.getElementById("flowbtns");

  for (const f of SPEC.flows) {
    const btn = document.createElement("button");

    btn.textContent = f.title;
    btn.dataset.flow = f.id;
    btn.onclick = () => startFlow(f.id);
    box.appendChild(btn);
  }
}

function startFlow(id, step = 0, keepTimer = false) {
  if (!keepTimer) stopTimer();

  state.flow = SPEC.flows.find(f => f.id === id);
  document.querySelectorAll("#flowbtns button").forEach(b => b.classList.toggle("on", b.dataset.flow === id));
  gotoStep(step < 0 ? state.flow.steps.length - 1 : step);
}

/* Step forward/backward; at a flow boundary continue into the adjacent flow. */
function advance(delta, keepTimer = false) {
  if (!state.flow) {
    startFlow(SPEC.flows[0].id, 0, keepTimer);
    return true;
  }

  const fi = SPEC.flows.indexOf(state.flow);
  const next = state.step + delta;

  if (next >= 0 && next < state.flow.steps.length) {
    gotoStep(next);
    return true;
  }

  const nf = SPEC.flows[fi + delta];

  if (!nf) return false;

  startFlow(nf.id, delta > 0 ? 0 : -1, keepTimer);

  return true;
}

function gotoStep(i) {
  const f = state.flow;

  if (!f) return;

  i = Math.max(0, Math.min(f.steps.length - 1, i));
  state.step = i;

  /* Flows are defined on the top level. */
  if (state.view) {
    state.view = null;
    renderView();
  }

  const st = f.steps[i];
  const prev = i > 0 ? f.steps[i - 1].block : null;
  const visited = f.steps.slice(0, i + 1).map(s => s.block);
  const path = new Set();

  for (let k = 1; k <= i; k++) path.add(`${f.steps[k - 1].block}>${f.steps[k].block}`);

  state.stepLines = new Set(st.lines.map(([lf, n]) => key(lf, n)));
  state.sel = st.block;
  setLit(visited, e => path.has(`${e.from}>${e.to}`) || path.has(`${e.to}>${e.from}`) || (prev && ((e.from === prev && e.to === st.block) || (e.to === prev && e.from === st.block))));

  const nf = SPEC.flows[SPEC.flows.indexOf(f) + 1];
  const tail = i === f.steps.length - 1 ? `<br><span class="k">本流程結束 · ${nf ? `按 ▶ 進入「${esc(nf.title)}」` : "全部流程已播完"}</span>` : "";

  stepInfo.innerHTML = `<span class="k">${esc(f.title)} · 第 ${i + 1}/${f.steps.length} 步</span><b>${esc(BLK[st.block].title)}</b> — ${esc(st.note)}${tail}`;
  renderInfo(BLK[st.block], st.note);

  if (state.tab === "full" && st.lines.length) state.file = st.lines[0][0];

  renderCode(!st.lines.length);
}

function stopTimer() {
  if (state.timer) clearInterval(state.timer);

  state.timer = null;
  document.getElementById("play").textContent = "▶ 播放";
}

function stopFlow(keepSel) {
  stopTimer();
  state.flow = null;
  state.step = -1;
  state.stepLines = new Set();
  document.querySelectorAll("#flowbtns button").forEach(b => b.classList.remove("on"));
  stepInfo.innerHTML = `<span class="hint">${HINT}</span>`;

  if (!keepSel) {
    state.sel = null;
    setLit([], () => false);
    renderOverview();
    renderCode(false);
  }
}

document.getElementById("prev").onclick = () => { stopTimer(); advance(-1); };
document.getElementById("next").onclick = () => { stopTimer(); advance(1); };
document.getElementById("clear").onclick = () => stopFlow(false);
document.getElementById("play").onclick = togglePlay;

function togglePlay() {
  if (state.timer) {
    stopTimer();
    return;
  }

  if (!state.flow) startFlow(SPEC.flows[0].id);
  else if (state.flow === SPEC.flows[SPEC.flows.length - 1] && state.step >= state.flow.steps.length - 1) startFlow(SPEC.flows[0].id);

  document.getElementById("play").textContent = "❚❚ 暫停";
  state.timer = setInterval(() => {
    if (!advance(1, true)) stopTimer();
  }, 2200);
}

document.addEventListener("keydown", ev => {
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT" || ev.target.id === "split") return;

  if (ev.key === "ArrowRight") document.getElementById("next").click();
  else if (ev.key === "ArrowLeft") document.getElementById("prev").click();
  else if (ev.key === "Escape") stopFlow(false);
  else if (ev.key === "Backspace" && state.view) { ev.preventDefault(); goView(containerOf(state.view)); }
  else if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
});

/* ---------- search ---------- */

function jumpToSymbol(sym) {
  /* Prefer the deepest non-ref node that declares the symbol (e.g. the function node over its parent block). */
  const all = Object.values(NODE).filter(v => v.n.kind !== "ref").sort((a, b) => b.level - a.level).map(v => v.n);
  const b = all.find(x => x.symbols.includes(sym) || x.symbols.some(s => s.split("::").pop() === sym)) || all.find(x => x.title === sym || x.id === sym);

  if (b) {
    stopFlow(true);
    select(b.id);
    return;
  }

  const re = new RegExp("\\b" + sym.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");

  for (const f of FILES) {
    const n = SRC[f].findIndex(l => re.test(l)) + 1;

    if (n > 0 && OWNER[f][n]) {
      stopFlow(true);
      select(OWNER[f][n]);
      state.file = f;
      state.stepLines = new Set([key(f, n)]);
      setTab("full", false);
      state.stepLines = new Set();
      return;
    }
  }
}

(function initSearch() {
  const dl = document.getElementById("symlist");
  const all = new Set();

  Object.values(NODE).forEach(({ n }) => {
    if (n.kind === "ref") return;
    if (!n.kind) all.add(n.title);

    n.symbols.forEach(s => all.add(s));
  });
  [...all].sort().forEach(s => { const o = document.createElement("option"); o.value = s; dl.appendChild(o); });

  const q = document.getElementById("q");

  q.addEventListener("change", () => { if (q.value.trim()) jumpToSymbol(q.value.trim()); });
  q.addEventListener("keydown", ev => { if (ev.key === "Enter" && q.value.trim()) jumpToSymbol(q.value.trim()); });
})();

/* ---------- theme / legend / boot ---------- */

document.getElementById("theme").onclick = () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;

  root.dataset.theme = dark ? "light" : "dark";

  try { localStorage.setItem("arch_theme", root.dataset.theme); } catch (e) {}
};

try {
  const t = localStorage.getItem("arch_theme");

  if (t) document.documentElement.dataset.theme = t;
} catch (e) {}

document.getElementById("srcinfo").textContent = (SPEC.root || SPEC.source) + ` · ${FILES.length} 個檔案 · 產生於 ${SPEC.generated}`;
stepInfo.innerHTML = `<span class="hint">${HINT}</span>`;

/* Draggable splitter between diagram and code panes; width kept as a percentage so it follows window resizes. */
(function splitter() {
  const mainEl = document.querySelector("main");
  const sp = document.getElementById("split");
  const MIN_LEFT = 240;
  const MIN_RIGHT = 300;

  function setPct(pct, save) {
    mainEl.style.setProperty("--leftw", `${pct.toFixed(2)}%`);

    if (save) {
      try { localStorage.setItem("arch_split", String(pct)); } catch (e) {}
    }
  }

  function setPx(px, save) {
    const w = mainEl.clientWidth;

    if (!w) return;

    const lo = Math.min(MIN_LEFT, w / 2);
    const hi = Math.max(lo, w - MIN_RIGHT);

    setPct(Math.min(Math.max(px, lo), hi) / w * 100, save);
  }

  try {
    const s = parseFloat(localStorage.getItem("arch_split"));

    if (s > 0 && s < 100) setPct(s, false);
  } catch (e) {}

  sp.addEventListener("pointerdown", ev => {
    ev.preventDefault();
    sp.setPointerCapture(ev.pointerId);
    document.body.classList.add("dragging");

    const left = mainEl.getBoundingClientRect().left;
    const move = e => setPx(e.clientX - left, false);
    const up = e => {
      sp.releasePointerCapture(ev.pointerId);
      sp.removeEventListener("pointermove", move);
      sp.removeEventListener("pointerup", up);
      sp.removeEventListener("pointercancel", up);
      document.body.classList.remove("dragging");
      setPx(e.clientX - left, true);
    };

    sp.addEventListener("pointermove", move);
    sp.addEventListener("pointerup", up);
    sp.addEventListener("pointercancel", up);
  });

  sp.addEventListener("dblclick", () => {
    mainEl.style.removeProperty("--leftw");

    try { localStorage.removeItem("arch_split"); } catch (e) {}
  });

  sp.addEventListener("keydown", ev => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;

    ev.preventDefault();

    const cur = document.getElementById("left").getBoundingClientRect().width;

    setPx(cur + (ev.key === "ArrowLeft" ? -24 : 24), true);
  });
})();

renderView();
flowBtns();
renderOverview();
renderCode(false);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
