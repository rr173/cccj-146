"""Query-tree protocol with a declared variable scope.

A query node is a JSON object with one of the keys ``triple``, ``path``,
``join``, ``union_all``, ``optional``, ``filter``, ``project``, ``distinct``
or ``values``.

    {"triple": {"graph": GRAPH, "s": T, "p": U, "o": T}}
    {"path": {"graph": GRAPH, "start": T, "path": PATH, "end": T}}
    {"join": {"left": Q, "right": Q}}
    {"union_all": {"left": Q, "right": Q}}
    {"optional": {"left": Q, "right": Q, "condition": EXPR?}}
    {"filter": {"input": Q, "expr": EXPR}}
    {"project": {"input": Q, "vars": ["x", ...]}}
    {"distinct": {"input": Q}}
    {"values": {"vars": ["x", ...], "rows": [{"x": T} | {}, ...]}}

``GRAPH`` is ``"default"`` or an IRI term.  ``T`` is ``{"var": "x"}`` or a
constant term from :mod:`rdfq.terms`.  Variables are structurally disjoint
from terms: an object is a variable iff its only key is ``"var"``.

PATH is one of::

    {"predicate": IRI_TERM}
    {"inverse": PATH}
    {"sequence": [PATH, ...]}          (at least two members)
    {"alternative": [PATH, ...]}       (at least two members)
    {"zero_or_more": PATH}
    {"one_or_more": PATH}

An expression is ``{"same_term": [T, T]}``, ``{"bound": "x"}``,
``{"not": EXPR}``, ``{"and": [EXPR, ...]}`` or ``{"or": [EXPR, ...]}``.

Every node has an *output scope*: the variables it may bind.  ``project``
narrows the scope, ``filter``/``optional``/``distinct`` keep it, and the
other operators union (or intersect for join) scopes.  Any variable used but
not in scope is rejected before execution.
"""

from __future__ import annotations

from . import terms

VAR_KEYS = {"var"}

NODE_KINDS = {
    "triple": {"graph", "s", "p", "o"},
    "path": {"graph", "start", "path", "end"},
    "join": {"left", "right"},
    "union_all": {"left", "right"},
    "optional": {"left", "right", "condition"},
    "filter": {"input", "expr"},
    "project": {"input", "vars"},
    "distinct": {"input"},
    "values": {"vars", "rows"},
}

PATH_KINDS = {
    "predicate": ("term",),
    "inverse": ("path",),
    "sequence": ("list",),
    "alternative": ("list",),
    "zero_or_more": ("path",),
    "one_or_more": ("path",),
}

EXPR_KINDS = {
    "same_term": ("terms",),
    "bound": ("var",),
    "not": ("expr",),
    "and": ("list",),
    "or": ("list",),
}


class QueryError(ValueError):
    """Raised for an invalid query tree, variable or expression."""


def is_var(raw) -> bool:
    return isinstance(raw, dict) and set(raw) == VAR_KEYS


def parse_var(raw, where: str) -> str:
    if not is_var(raw):
        raise QueryError(f"{where}: expected a variable object {{\"var\": \"name\"}}")
    name = raw["var"]
    if not isinstance(name, str) or not name:
        raise QueryError(f"{where}: variable name must be a non-empty string")
    return name


def parse_var_list(raw, where: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(raw, list):
        raise QueryError(f"{where}: expected a list of variable names")
    out = []
    seen = set()
    for i, name in enumerate(raw):
        if not isinstance(name, str) or not name:
            raise QueryError(f"{where}[{i}]: variable name must be a non-empty string")
        if name in seen:
            raise QueryError(f"{where}[{i}]: duplicate variable {name!r}")
        seen.add(name)
        out.append(name)
    if not allow_empty and not out:
        raise QueryError(f"{where}: at least one variable is required")
    return out


def parse_graph(raw, where: str) -> str | None:
    if raw == "default":
        return None
    term = terms.parse_term(raw, where)
    if term["type"] != terms.IRI:
        raise QueryError(f"{where}: graph must be \"default\" or an IRI term")
    return term["value"]


def parse_endpoint(raw, where: str) -> tuple[str, object]:
    """Return ('var', name) or ('const', term)."""
    if isinstance(raw, dict) and "var" in raw:
        return ("var", parse_var(raw, where))
    return ("const", terms.parse_term(raw, where))


def parse_path(raw, where: str) -> dict:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise QueryError(f"{where}: a path must be an object with exactly one operator key")
    (kind, body), = raw.items()
    if kind not in PATH_KINDS:
        raise QueryError(f"{where}: unknown path operator {kind!r}")
    if kind == "predicate":
        term = terms.parse_term(body, where + ".predicate")
        if term["type"] != terms.IRI:
            raise QueryError(f"{where}.predicate: must be an IRI term")
        return {"predicate": term}
    if kind in ("inverse", "zero_or_more", "one_or_more"):
        return {kind: parse_path(body, where + "." + kind)}
    if kind in ("sequence", "alternative"):
        if not isinstance(body, list) or len(body) < 2:
            raise QueryError(f"{where}.{kind}: expected a list of at least two paths")
        return {kind: [parse_path(p, f"{where}.{kind}[{i}]") for i, p in enumerate(body)]}
    raise QueryError(f"{where}: unknown path operator {kind!r}")


def parse_expr(raw, where: str) -> dict:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise QueryError(f"{where}: an expression must be an object with exactly one operator key")
    (kind, body), = raw.items()
    if kind not in EXPR_KINDS:
        raise QueryError(f"{where}: unknown expression operator {kind!r}")
    if kind == "same_term":
        if not isinstance(body, list) or len(body) != 2:
            raise QueryError(f"{where}.same_term: expected a list of two endpoints")
        return {"same_term": [parse_endpoint(body[0], f"{where}.same_term[0]"),
                              parse_endpoint(body[1], f"{where}.same_term[1]")]}
    if kind == "bound":
        return {"bound": parse_var(body, where + ".bound")}
    if kind == "not":
        return {"not": parse_expr(body, where + ".not")}
    if kind in ("and", "or"):
        if not isinstance(body, list) or not body:
            raise QueryError(f"{where}.{kind}: expected a non-empty list of expressions")
        return {kind: [parse_expr(e, f"{where}.{kind}[{i}]") for i, e in enumerate(body)]}
    raise QueryError(f"{where}: unknown expression operator {kind!r}")


def parse_node(raw, where: str = "query") -> dict:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise QueryError(f"{where}: a query node must be an object with exactly one operator key")
    (kind, body), = raw.items()
    if kind not in NODE_KINDS:
        raise QueryError(f"{where}: unknown query operator {kind!r}")
    if not isinstance(body, dict):
        raise QueryError(f"{where}.{kind}: body must be an object")
    allowed = NODE_KINDS[kind] if kind != "optional" else (
        {"left", "right"} if "condition" not in body else {"left", "right", "condition"}
    )
    missing = {"left", "right"} - set(body) if kind in ("join", "union_all", "optional") else set()
    if missing:
        raise QueryError(f"{where}.{kind}: missing fields {sorted(missing)}")
    if kind in ("join", "union_all", "optional"):
        extra = set(body) - allowed
    else:
        extra = None
    if extra:
        raise QueryError(f"{where}.{kind}: unknown fields {sorted(extra)}")
    if kind in ("triple",):
        if set(body) != {"graph", "s", "p", "o"}:
            raise QueryError(f"{where}.triple: expected exactly graph, s, p, o")
        graph = parse_graph(body["graph"], where + ".graph")
        s = parse_endpoint(body["s"], where + ".s")
        if s[0] == "const" and s[1]["type"] not in (terms.IRI, terms.BNODE):
            raise QueryError(f"{where}.s: subject constant must be an IRI or blank node")
        p = parse_endpoint(body["p"], where + ".p")
        if p[0] == "const" and p[1]["type"] != terms.IRI:
            raise QueryError(f"{where}.p: predicate constant must be an IRI")
        o = parse_endpoint(body["o"], where + ".o")
        return {"triple": {"graph": graph, "s": s, "p": p, "o": o}}
    if kind == "path":
        if set(body) != {"graph", "start", "path", "end"}:
            raise QueryError(f"{where}.path: expected exactly graph, start, path, end")
        return {"path": {
            "graph": parse_graph(body["graph"], where + ".graph"),
            "start": parse_endpoint(body["start"], where + ".start"),
            "path": parse_path(body["path"], where + ".path"),
            "end": parse_endpoint(body["end"], where + ".end"),
        }}
    if kind in ("join", "union_all", "optional"):
        node = {kind: {
            "left": parse_node(body["left"], where + ".left"),
            "right": parse_node(body["right"], where + ".right"),
        }}
        if kind == "optional" and "condition" in body:
            node[kind]["condition"] = parse_expr(body["condition"], where + ".condition")
        return node
    if kind == "filter":
        if set(body) != {"input", "expr"}:
            raise QueryError(f"{where}.filter: expected exactly input, expr")
        return {"filter": {
            "input": parse_node(body["input"], where + ".input"),
            "expr": parse_expr(body["expr"], where + ".expr"),
        }}
    if kind == "project":
        if set(body) != {"input", "vars"}:
            raise QueryError(f"{where}.project: expected exactly input, vars")
        return {"project": {
            "input": parse_node(body["input"], where + ".input"),
            "vars": parse_var_list(body["vars"], where + ".vars", allow_empty=True),
        }}
    if kind == "distinct":
        if set(body) != {"input"}:
            raise QueryError(f"{where}.distinct: expected exactly input")
        return {"distinct": {"input": parse_node(body["input"], where + ".input")}}
    if kind == "values":
        if set(body) != {"vars", "rows"}:
            raise QueryError(f"{where}.values: expected exactly vars, rows")
        vars_ = parse_var_list(body["vars"], where + ".vars", allow_empty=True)
        if not isinstance(body["rows"], list):
            raise QueryError(f"{where}.rows: expected a list")
        rows = []
        seen_vars = set(vars_)
        for i, row in enumerate(body["rows"]):
            rwhere = f"{where}.rows[{i}]"
            if not isinstance(row, dict):
                raise QueryError(f"{rwhere}: each row must be an object")
            parsed = {}
            for name, raw_term in row.items():
                if name not in seen_vars:
                    raise QueryError(f"{rwhere}: variable {name!r} is not declared in vars")
                parsed[name] = terms.parse_term(raw_term, f"{rwhere}.{name}")
            rows.append(parsed)
        return {"values": {"vars": vars_, "rows": rows}}
    raise QueryError(f"{where}: unknown query operator {kind!r}")


def free_vars(node: dict) -> set[str]:
    """Variables in the output scope of a parsed node."""
    (kind, body), = node.items()
    if kind == "values":
        return set(body["vars"])
    if kind == "triple":
        out = set()
        for key in ("s", "p", "o"):
            if body[key][0] == "var":
                out.add(body[key][1])
        return out
    if kind == "path":
        out = set()
        for key in ("start", "end"):
            if body[key][0] == "var":
                out.add(body[key][1])
        return out
    if kind in ("join", "union_all"):
        return free_vars(body["left"]) | free_vars(body["right"])
    if kind in ("optional", "distinct"):
        return free_vars(body["left"] if kind == "optional" else body["input"]) | (
            free_vars(body["right"]) if kind == "optional" else set())
    if kind == "filter":
        return free_vars(body["input"])
    if kind == "project":
        return set(body["vars"])
    raise QueryError(f"unknown query operator {kind!r}")


def expr_vars(expr: dict) -> set[str]:
    (kind, body), = expr.items()
    if kind == "same_term":
        return {x[1] for x in body if x[0] == "var"}
    if kind == "bound":
        return {body}
    if kind == "not":
        return expr_vars(body)
    if kind in ("and", "or"):
        out = set()
        for e in body:
            out |= expr_vars(e)
        return out
    raise QueryError(f"unknown expression operator {kind!r}")


def validate_scopes(node: dict) -> None:
    """Reject use of variables outside the declared scope of their node.

    * filter / optional conditions may only mention variables produced by the
      tree they are attached to (for optional: left join right);
    * join is unrestricted in the protocol but repeated variables are fine;
    * project may only project variables produced by its input.
    """
    (kind, body), = node.items()
    if kind in ("join", "union_all"):
        validate_scopes(body["left"])
        validate_scopes(body["right"])
        return
    if kind == "optional":
        validate_scopes(body["left"])
        validate_scopes(body["right"])
        if "condition" in body:
            scope = free_vars(body["left"]) | free_vars(body["right"])
            missing = expr_vars(body["condition"]) - scope
            if missing:
                raise QueryError(
                    f"optional condition references variables outside its scope: {sorted(missing)}")
        return
    if kind == "filter":
        validate_scopes(body["input"])
        missing = expr_vars(body["expr"]) - free_vars(body["input"])
        if missing:
            raise QueryError(f"filter references variables outside its scope: {sorted(missing)}")
        return
    if kind == "distinct":
        validate_scopes(body["input"])
        return
    if kind == "project":
        validate_scopes(body["input"])
        inner = free_vars(body["input"])
        missing = set(body["vars"]) - inner
        if missing:
            raise QueryError(
                f"project references variables not bound by its input: {sorted(missing)}")
        return
    if kind in ("triple", "path", "values"):
        return
    raise QueryError(f"unknown query operator {kind!r}")
