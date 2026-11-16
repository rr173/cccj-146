"""Independent oracle used by the differential tests.

This module deliberately does NOT import anything from ``rdfq``.  It parses
the same public protocol from scratch, keeps everything in memory and answers
queries by direct enumeration of tiny graphs.

Paths are computed as explicit route sets with a visited guard, which is only
valid for the small acyclic-or-cyclic test graphs used here; it is a slower,
independent implementation intended to catch mistakes in the optimised
frontier-set production code.

Bags are lists of plain mappings; multiplicities are compared as full
multisets of mappings.
"""

from __future__ import annotations

XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"


# ---------------------------------------------------------------------------
# terms


def norm_term(raw):
    t = raw["type"]
    if t == "iri":
        return ("iri", raw["value"])
    if t == "bnode":
        return ("bnode", raw["label"])
    if t == "literal":
        if "language" in raw:
            return ("literal", raw["value"], "", raw["language"].lower())
        return ("literal", raw["value"], raw.get("datatype", XSD_STRING), "")
    raise ValueError("bad term")


def endpoint(raw):
    if isinstance(raw, dict) and "var" in raw:
        return ("var", raw["var"])
    return ("const", norm_term(raw))


def graph_id(raw):
    if raw == "default":
        return ("default",)
    return ("iri", raw["value"])


# ---------------------------------------------------------------------------
# structures


class Graph:
    def __init__(self):
        self.quads = []          # (s, p, o, g) with g = ("default",)/("iri",v)
        self.all_quads = set()
        self.nodes = {}          # g -> set of nodes appearing in s or o

    def add(self, s, p, o, g):
        self.all_quads.add((s, p, o, g))

    def finish(self):
        self.quads = sorted(self.all_quads)
        self.nodes = {}
        for s, p, o, g in self.quads:
            bucket = self.nodes.setdefault(g, set())
            bucket.add(s)
            bucket.add(o)

    def relation(self, g, path):
        """Direct enumeration of the path's endpoint relation in one graph.

        Implemented independently of the production traversal: build the base
        edge relation from the quadruple list, then combine relations with
        Python set algebra, and compute transitive closure as a pair-set
        fixpoint.  This terminates on cycles because only pairs are tracked.
        """
        kind, body = next(iter(path.items()))
        if kind == "predicate":
            pred = norm_term(body)
            return {(s, o) for (s, p, o, qg) in self.quads
                    if qg == g and p == pred}
        if kind == "inverse":
            return {(o, s) for (s, o) in self.relation(g, body)}
        if kind == "alternative":
            out = set()
            for part in body:
                out |= self.relation(g, part)
            return out
        if kind == "sequence":
            current = self.relation(g, body[0])
            for part in body[1:]:
                nxt = self.relation(g, part)
                current = {(a, d) for (a, b) in current for (c, d) in nxt if b == c}
            return current
        if kind in ("zero_or_more", "one_or_more"):
            base = self.relation(g, body)
            result = set(base) if kind == "one_or_more" else set()
            if kind == "zero_or_more":
                result |= {(n, n) for n in self.nodes.get(g, set())}
            while True:
                grown = {(a, c) for (a, b) in result for (x, c) in base if b == x}
                new = grown - result
                if not new:
                    break
                result |= new
            return result
        raise ValueError("bad path")

    def path_pairs(self, g, path, start=None, end=None):
        rel = self.relation(g, path)
        if start is not None:
            rel = {(s, e) for (s, e) in rel if s == start}
        if end is not None:
            rel = {(s, e) for (s, e) in rel if e == end}
        return rel

# ---------------------------------------------------------------------------
# evaluation


def compatible(a, b):
    merged = dict(a)
    for k, v in b.items():
        if k in merged:
            if merged[k] != v:
                return None
        else:
            merged[k] = v
    return merged


class Eval:
    def __init__(self, graph: Graph):
        self.g = graph

    def run(self, node):
        kind, body = next(iter(node.items()))
        if kind == "values":
            vars_ = body["vars"]
            if not vars_:
                return [{}] if body["rows"] else []
            return [{k: norm_term(v) for k, v in row.items()} for row in body["rows"]]
        if kind == "triple":
            g = graph_id(body["graph"])
            s_side = endpoint(body["s"])
            p_side = endpoint(body["p"])
            o_side = endpoint(body["o"])
            out = []
            for qs, qp, qo, qg in self.g.quads:
                if qg != g:
                    continue
                ok = True
                m = {}
                for side, val in ((s_side, qs), (p_side, qp), (o_side, qo)):
                    if side[0] == "const":
                        if side[1] != val:
                            ok = False
                            break
                    else:
                        if side[1] in m and m[side[1]] != val:
                            ok = False
                            break
                        m[side[1]] = val
                if ok:
                    out.append(m)
            return out
        if kind == "path":
            g = graph_id(body["graph"])
            sk, sval = endpoint(body["start"])
            ek, eval_ = endpoint(body["end"])
            start = sval if sk == "const" else None
            end = eval_ if ek == "const" else None
            out = []
            for s, e in sorted(self.g.path_pairs(g, body["path"], start, end)):
                m = {}
                ok = True
                for isvar, name, same in ((sk == "var", sval, s), (ek == "var", eval_, e)):
                    if isvar:
                        if name in m and m[name] != same:
                            ok = False
                            break
                        m[name] = same
                if ok:
                    out.append(m)
            return out
        if kind == "join":
            left = self.run(body["left"])
            right = self.run(body["right"])
            out = []
            for a in left:
                for b in right:
                    m = compatible(a, b)
                    if m is not None:
                        out.append(m)
            return out
        if kind == "union_all":
            return self.run(body["left"]) + self.run(body["right"])
        if kind == "optional":
            left = self.run(body["left"])
            right = self.run(body["right"])
            cond = body.get("condition")
            out = []
            for a in left:
                matched = False
                for b in right:
                    m = compatible(a, b)
                    if m is None:
                        continue
                    if cond is not None and truth(self.expr(cond, m)) is not True:
                        continue
                    out.append(m)
                    matched = True
                if not matched:
                    out.append(dict(a))
            return out
        if kind == "filter":
            src = self.run(body["input"])
            return [m for m in src if truth(self.expr(body["expr"], m)) is True]
        if kind == "project":
            vars_ = body["vars"]
            return [{k: m[k] for k in vars_ if k in m} for m in self.run(body["input"])]
        if kind == "distinct":
            out = []
            seen = set()
            for m in self.run(body["input"]):
                fk = tuple(sorted(m.items()))
                if fk not in seen:
                    seen.add(fk)
                    out.append(m)
            return out
        raise ValueError("bad node " + kind)

    def expr(self, e, mapping):
        kind, body = next(iter(e.items()))
        if kind == "bound":
            name = body["var"] if isinstance(body, dict) else body
            return name in mapping
        if kind == "same_term":
            def side(raw):
                if "var" in raw:
                    return mapping.get(raw["var"])
                return norm_term(raw)
            left = side(body[0])
            right = side(body[1])
            if left is None or right is None:
                return None
            return left == right
        if kind == "not":
            v = self.expr(body, mapping)
            return None if v is None else not v
        if kind == "and":
            vals = [self.expr(x, mapping) for x in body]
            if False in vals:
                return False
            if None in vals:
                return None
            return True
        if kind == "or":
            vals = [self.expr(x, mapping) for x in body]
            if True in vals:
                return True
            if None in vals:
                return None
            return False
        raise ValueError("bad expr " + kind)


def truth(v):
    return v


# ---------------------------------------------------------------------------
# public helpers for tests


def load(file_pairs, directed_paths=None):
    """``file_pairs`` is a list of (file_id, [quad dicts])."""
    graph = Graph()
    for file_id, quads in file_pairs:
        for quad in quads:
            s = norm_term(quad["subject"])
            p = norm_term(quad["predicate"])
            o = norm_term(quad["object"])
            if s[0] == "bnode":
                s = ("bnode", file_id + ":" + s[1])
            if o[0] == "bnode":
                o = ("bnode", file_id + ":" + o[1])
            graph.add(s, p, o, graph_id(quad["graph"]))
    graph.finish()
    return graph


def multiset(maps, vars_=None):
    if vars_ is None:
        vars_ = sorted({k for m in maps for k in m})
    out = {}
    for m in maps:
        filt = tuple(sorted((k, v) for k, v in m.items() if k in vars_))
        out[filt] = out.get(filt, 0) + 1
    return out


def bag_from_cells(rows, columns):
    """Convert CLI result rows into the oracle's mapping multiset."""
    from_cells = {}
    for r in rows:
        mapping = {}
        for name, cell in zip(columns, r["cells"]):
            if cell["bound"]:
                t = cell["term"]
                if t["type"] == "literal":
                    lang = t.get("language", "")
                    dt = "" if lang else t.get("datatype", XSD_STRING)
                    mapping[name] = ("literal", t["value"], dt, lang)
                else:
                    mapping[name] = (t["type"], t["value"] if t["type"] == "iri" else t["label"])
        from_cells[tuple(sorted(mapping.items()))] = (
            from_cells.get(tuple(sorted(mapping.items())), 0) + r["multiplicity"])
    return from_cells
