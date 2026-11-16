"""Bag-semantics evaluator for the RDF query tree.

Design notes
------------

* A *solution* is a partial mapping ``variable -> stored term key``.  Bags are
  represented as ``dict[frozen mapping, multiplicity]`` so multiplicities stay
  exact without materialising duplicate rows.
* ``join`` is a bag join on the term identity of common bound variables.
* ``union_all`` adds multiplicities; ``project`` keeps duplicates; only
  ``distinct`` collapses equal partial mappings.
* A path node is evaluated as an *endpoint relation*: every endpoint pair is
  produced once, no matter how many routes connect it, before joining with
  the rest of the query.  This is the project's stated endpoint-set contract.
  Zero-length paths anchor on terms that occur as subject or object in the
  selected graph (literals included).
* Path traversal is iterative over sets of path states (frontier sets), never
  over route sequences, so cycles terminate.  Graphs are never mixed: every
  lookup is constrained by the node's fixed graph key.
* Filters and optional conditions use explicit three-valued logic.  Only
  ``true`` passes; an error is treated like ``false`` for retention, and
  ``NOT``/``AND``/``OR`` follow the specified error propagation.
"""

from __future__ import annotations

from . import query as q
from . import terms

DEFAULT_GRAPH_KEY = "D"


class EvaluationError(RuntimeError):
    pass


def stored_key(term: dict) -> str:
    """Key for a constant term used inside a query.

    Blank-node constants are opaque dataset node identities as returned in
    results (labels such as ``b7``); they are not source-file labels.
    """
    if term["type"] == terms.BNODE:
        return "B:" + term["label"]
    return terms.term_key(term)


def freeze(mapping: dict) -> tuple:
    return tuple(sorted(mapping.items()))


def unfreeze(key: tuple) -> dict:
    return dict(key)


def bag_total(bag: dict) -> int:
    return sum(bag.values())


def bag_add(bag: dict, mapping: dict, mult: int = 1) -> None:
    if mult <= 0:
        return
    fk = freeze(mapping)
    bag[fk] = bag.get(fk, 0) + mult


def bag_union(left: dict, right: dict) -> dict:
    out = dict(left)
    for k, n in right.items():
        out[k] = out.get(k, 0) + n
    return out


def compatible_and_merge(left: dict, right: dict):
    merged = dict(left)
    for var, key in right.items():
        if var in merged:
            if merged[var] != key:
                return None
        else:
            merged[var] = key
    return merged


def bag_join(left: dict, right: dict) -> dict:
    """Bag join: compatible rows multiply; variables bound on one side only
    are carried over, variables unbound on both sides stay unbound."""
    out: dict = {}
    for lk, lm in left.items():
        ld = unfreeze(lk)
        for rk, rm in right.items():
            merged = compatible_and_merge(ld, unfreeze(rk))
            if merged is not None:
                bag_add(out, merged, lm * rm)
    return out


def truth_not(value):
    if value is None:
        return None
    return not value


def truth_and(values):
    saw_error = False
    for v in values:
        if v is False:
            return False
        if v is None:
            saw_error = True
    return None if saw_error else True


def truth_or(values):
    saw_error = False
    for v in values:
        if v is True:
            return True
        if v is None:
            saw_error = True
    return None if saw_error else False


class Evaluator:
    def __init__(self, conn, *, explain: bool = False):
        self.conn = conn
        self.explain = explain
        self.steps: list[dict] = []
        self.path_stats = {
            "path_nodes": 0,
            "endpoint_pairs": 0,
            # work is counted in path *states* (node identities in a frontier),
            # never in route sequences
            "frontier_rounds": 0,
            "states_expanded": 0,
            "advance_calls": 0,
            "node_checks": 0,
        }
        self._node_cache: dict[str, set[str]] = {}
        self._edge_cache: dict[tuple[str, str], tuple[dict, dict]] = {}
        self._constant_cache: dict[str, bool] = {}

    # -- helpers ---------------------------------------------------------
    def _step(self, operator: str, **fields) -> None:
        if self.explain:
            entry = {"step": len(self.steps) + 1, "operator": operator}
            entry.update(fields)
            self.steps.append(entry)

    def _rows(self, sql: str, params=()) -> list[tuple]:
        try:
            return self.conn.execute(sql, params).fetchall()
        except Exception as exc:  # sqlite3.Error and anything from a closed store
            raise EvaluationError(f"storage lookup failed: {exc}") from exc

    def _graph_key(self, graph_iri: str | None) -> str:
        return DEFAULT_GRAPH_KEY if graph_iri is None else "G:" + graph_iri

    def _graph_label(self, graph_iri: str | None) -> str:
        return "default" if graph_iri is None else graph_iri

    def _nodes_in_graph(self, gkey: str) -> set[str]:
        if gkey not in self._node_cache:
            rows = self._rows(
                "SELECT subject FROM quad WHERE graph_key = ?"
                " UNION SELECT object FROM quad WHERE graph_key = ?",
                (gkey, gkey))
            self._node_cache[gkey] = {r[0] for r in rows}
        return self._node_cache[gkey]

    def _adjacency(self, gkey: str, pred_key: str):
        """Forward and backward adjacency for one (graph, predicate).

        Fetched once per query; traversal then walks in-memory frontiers.  It
        never materialises paths, so cycles cannot blow up.
        """
        cache_key = (gkey, pred_key)
        if cache_key not in self._edge_cache:
            rows = self._rows(
                "SELECT DISTINCT subject, object FROM quad"
                " WHERE graph_key = ? AND predicate = ?", (gkey, pred_key))
            forward: dict = {}
            backward: dict = {}
            for subject, obj in rows:
                forward.setdefault(subject, set()).add(obj)
                backward.setdefault(obj, set()).add(subject)
            self._edge_cache[cache_key] = (forward, backward)
        return self._edge_cache[cache_key]

    def _is_node(self, gkey: str, key: str) -> bool:
        self.path_stats["node_checks"] += 1
        if key in self._nodes_in_graph(gkey):
            return True
        cache_key = gkey + "\x1f" + key
        if cache_key not in self._constant_cache:
            rows = self._rows(
                "SELECT 1 FROM quad WHERE graph_key = ? AND (subject = ? OR object = ?)"
                " LIMIT 1", (gkey, key, key))
            self._constant_cache[cache_key] = bool(rows)
        return self._constant_cache[cache_key]

    @staticmethod
    def _chunks(items, size=400):
        items = list(items)
        for i in range(0, len(items), size):
            yield items[i:i + size]

    # -- expression evaluation ------------------------------------------
    def eval_expr(self, expr: dict, mapping: dict) -> bool | None:
        (kind, body), = expr.items()
        if kind == "bound":
            return body in mapping
        if kind == "same_term":
            values = []
            for side_kind, value in body:
                if side_kind == "var":
                    values.append(mapping.get(value))
                else:
                    values.append(stored_key(value))
            if values[0] is None or values[1] is None:
                return None
            return values[0] == values[1]
        if kind == "not":
            return truth_not(self.eval_expr(body, mapping))
        if kind == "and":
            return truth_and([self.eval_expr(e, mapping) for e in body])
        if kind == "or":
            return truth_or([self.eval_expr(e, mapping) for e in body])
        raise EvaluationError(f"unknown expression operator {kind!r}")

    # -- path relations ---------------------------------------------------
    def eval_path_node(self, body: dict) -> dict:
        gkey = self._graph_key(body["graph"])
        path = body["path"]
        start_kind, start_val = body["start"]
        end_kind, end_val = body["end"]
        same_var = (start_kind == "var" and end_kind == "var" and start_val == end_val)

        start_key = stored_key(start_val) if start_kind == "const" else None
        end_key = stored_key(end_val) if end_kind == "const" else None

        pairs: set[tuple[str, str]]
        if start_key is not None:
            targets = self._follow(path, gkey, {start_key}, forward=True)
            if end_key is not None:
                # Both endpoints fixed: the relation is non-empty only when
                # start actually reaches end through this path in this graph.
                pairs = {(start_key, end_key)} if end_key in targets else set()
            else:
                pairs = {(start_key, t) for t in targets}
        elif end_key is not None:
            sources = self._follow(path, gkey, {end_key}, forward=False)
            pairs = {(s, end_key) for s in sources}
        else:
            # Both endpoints unbound: the endpoint relation is the complete
            # pair set.  Each source's frontier traversal is independent and
            # terminates on cycles because only node identities are tracked.
            nodes = self._nodes_in_graph(gkey)
            pairs = set()
            for node in nodes:
                for target in self._follow(path, gkey, {node}, forward=True):
                    pairs.add((node, target))

        if same_var:
            pairs = {(s, t) for (s, t) in pairs if s == t}

        bag: dict = {}
        for s, t in pairs:
            mapping = {}
            if start_kind == "var":
                mapping[start_val] = s
            if end_kind == "var" and not (same_var):
                mapping[end_val] = t
            bag_add(bag, mapping, 1)

        self.path_stats["path_nodes"] += 1
        self.path_stats["endpoint_pairs"] += len(pairs)
        self._step(
            "path",
            graph=self._graph_label(body["graph"]),
            graph_key=gkey,
            path=describe_path(path),
            start=describe_endpoint(body["start"]),
            end=describe_endpoint(body["end"]),
            endpoint_contract="each (start,end) pair emitted once before joining",
            route_enumeration=False,
            endpoint_pairs=len(pairs),
            output_distinct_mappings=len(bag),
            output_rows=bag_total(bag),
            multiplicity_preserved=True,
        )
        return bag

    def _follow(self, path: dict, gkey: str, starts: set[str],
                *, forward: bool) -> set[str]:
        """All endpoints reachable from any start by one path traversal mode.

        Operates on frontier sets, so no route sequences are enumerated.
        """
        self.path_stats["advance_calls"] += 1
        if not starts:
            return set()
        (kind, body), = path.items()
        if kind == "predicate":
            adjacency = self._adjacency(gkey, stored_key(body))
            index = 0 if forward else 1
            out = set()
            for node in starts:
                out.update(adjacency[index].get(node, ()))
            return out
        if kind == "inverse":
            return self._follow(body, gkey, starts, forward=not forward)
        if kind == "alternative":
            out = set()
            for part in body:
                out |= self._follow(part, gkey, starts, forward=forward)
            return out
        if kind == "sequence":
            frontier = set(starts)
            parts = body if forward else list(reversed(body))
            for part in parts:
                frontier = self._follow(part, gkey, frontier, forward=forward)
                if not frontier:
                    break
            return frontier
        if kind in ("zero_or_more", "one_or_more"):
            first = self._follow(body, gkey, set(starts), forward=forward)
            if kind == "zero_or_more":
                # Zero repetitions: only terms actually occurring as subject or
                # object in this graph.  A constant absent from the graph has
                # no zero-length result.
                result = {s for s in starts if self._is_node(gkey, s)}
                frontier = first - result
                result |= frontier
            else:
                result = set(first)
                frontier = set(first)
            while frontier:
                self.path_stats["frontier_rounds"] += 1
                self.path_stats["states_expanded"] += len(frontier)
                nxt = self._follow(body, gkey, frontier, forward=forward) - result
                result |= nxt
                frontier = nxt
            return result
        raise EvaluationError(f"unknown path operator {kind!r}")

    # -- query nodes ------------------------------------------------------
    def eval_node(self, node: dict) -> dict:
        (kind, body), = node.items()
        if kind == "triple":
            return self.eval_triple(body)
        if kind == "path":
            return self.eval_path_node(body)
        if kind == "values":
            return self.eval_values(body)
        if kind == "join":
            return self.eval_join(body)
        if kind == "union_all":
            return self.eval_union_all(body)
        if kind == "optional":
            return self.eval_optional(body)
        if kind == "filter":
            return self.eval_filter(body)
        if kind == "project":
            return self.eval_project(body)
        if kind == "distinct":
            return self.eval_distinct(body)
        raise EvaluationError(f"unknown query operator {kind!r}")

    def eval_triple(self, body: dict) -> dict:
        gkey = self._graph_key(body["graph"])
        cols = {"s": "subject", "p": "predicate", "o": "object"}
        clauses = ["graph_key = ?"]
        params: list = [gkey]
        for role in ("s", "p", "o"):
            side_kind, value = body[role]
            if side_kind == "const":
                clauses.append(f"{cols[role]} = ?")
                params.append(stored_key(value))
        sql = "SELECT subject, predicate, object FROM quad WHERE " + " AND ".join(clauses)
        rows = self._rows(sql, tuple(params))
        bag: dict = {}
        for subj, pred, obj in rows:
            values = {"s": subj, "p": pred, "o": obj}
            mapping = {}
            ok = True
            for role in ("s", "p", "o"):
                side_kind, value = body[role]
                if side_kind == "var":
                    cell = values[role]
                    if value in mapping:
                        if mapping[value] != cell:
                            ok = False
                            break
                    else:
                        mapping[value] = cell
            if ok:
                bag_add(bag, mapping, 1)
        self._step(
            "triple",
            graph=self._graph_label(body["graph"]),
            graph_key=gkey,
            matched_quads=len(rows),
            output_distinct_mappings=len(bag),
            output_rows=bag_total(bag),
            multiplicity_preserved=True,
        )
        return bag

    def eval_values(self, body: dict) -> dict:
        vars_ = body["vars"]
        rows = body["rows"]
        bag: dict = {}
        if not vars_:
            if rows:
                bag_add(bag, {}, 1)
        else:
            for row in rows:
                mapping = {name: stored_key(term) for name, term in row.items()}
                bag_add(bag, mapping, 1)
        self._step(
            "values",
            variables=list(vars_),
            input_rows=len(rows),
            empty_variable_table=not vars_,
            output_distinct_mappings=len(bag),
            output_rows=bag_total(bag),
            multiplicity_preserved=True,
        )
        return bag

    def eval_join(self, body: dict) -> dict:
        left = self.eval_node(body["left"])
        right = self.eval_node(body["right"])
        out = bag_join(left, right)
        self._step(
            "join",
            left_distinct=len(left), left_rows=bag_total(left),
            right_distinct=len(right), right_rows=bag_total(right),
            output_distinct_mappings=len(out), output_rows=bag_total(out),
            multiplicity_rule="compatible rows multiply (bag join)",
            multiplicity_preserved=True,
        )
        return out

    def eval_union_all(self, body: dict) -> dict:
        left = self.eval_node(body["left"])
        right = self.eval_node(body["right"])
        out = bag_union(left, right)
        self._step(
            "union_all",
            left_rows=bag_total(left), right_rows=bag_total(right),
            output_distinct_mappings=len(out), output_rows=bag_total(out),
            multiplicity_rule="multiplicities are added; no de-duplication",
            multiplicity_preserved=True,
        )
        return out

    def eval_optional(self, body: dict) -> dict:
        left = self.eval_node(body["left"])
        right = self.eval_node(body["right"])
        condition = body.get("condition")
        out: dict = {}
        for lk, lm in left.items():
            ld = unfreeze(lk)
            matched = False
            for rk, rm in right.items():
                merged = compatible_and_merge(ld, unfreeze(rk))
                if merged is None:
                    continue
                if condition is not None and self.eval_expr(condition, merged) is not True:
                    continue
                bag_add(out, merged, lm * rm)
                matched = True
            if not matched:
                bag_add(out, ld, lm)
        self._step(
            "optional",
            left_rows=bag_total(left), right_rows=bag_total(right),
            right_evaluated_independently=True,
            output_distinct_mappings=len(out), output_rows=bag_total(out),
            multiplicity_rule="left multiplicity x qualifying right multiplicity; "
                              "unmatched left rows keep their multiplicity",
            multiplicity_preserved=True,
        )
        return out

    def eval_filter(self, body: dict) -> dict:
        source = self.eval_node(body["input"])
        out: dict = {}
        errors = 0
        false_drops = 0
        for fk, count in source.items():
            value = self.eval_expr(body["expr"], unfreeze(fk))
            if value is True:
                bag_add(out, unfreeze(fk), count)
            else:
                if value is None:
                    errors += 1
                else:
                    false_drops += 1
        self._step(
            "filter",
            input_rows=bag_total(source),
            passed_distinct=len(out),
            passed_rows=bag_total(out),
            error_distinct=errors,
            false_distinct=false_drops,
            rule="only true passes; error and false are dropped",
            multiplicity_preserved=True,
        )
        return out

    def eval_project(self, body: dict) -> dict:
        source = self.eval_node(body["input"])
        wanted = body["vars"]
        out: dict = {}
        for fk, count in source.items():
            mapping = unfreeze(fk)
            projected = {name: mapping[name] for name in wanted if name in mapping}
            bag_add(out, projected, count)
        self._step(
            "project",
            variables=list(wanted),
            input_rows=bag_total(source),
            output_distinct_mappings=len(out),
            output_rows=bag_total(out),
            multiplicity_rule="duplicates preserved; no duplicate elimination",
            multiplicity_preserved=True,
        )
        return out

    def eval_distinct(self, body: dict) -> dict:
        source = self.eval_node(body["input"])
        out = {fk: 1 for fk in source}
        self._step(
            "distinct",
            input_rows=bag_total(source),
            input_distinct=len(source),
            output_distinct_mappings=len(out),
            output_rows=len(out),
            multiplicity_rule="identical partial mappings collapse to one row",
            multiplicity_preserved=False,
        )
        return out


def describe_endpoint(endpoint) -> dict:
    kind, value = endpoint
    if kind == "var":
        return {"variable": value}
    return {"constant": value}


def describe_path(path: dict) -> dict:
    (kind, body), = path.items()
    if kind == "predicate":
        return {"op": "predicate", "iri": body["value"]}
    if kind in ("inverse", "zero_or_more", "one_or_more"):
        return {"op": kind, "path": describe_path(body)}
    if kind in ("sequence", "alternative"):
        return {"op": kind, "paths": [describe_path(p) for p in body]}
    return {"op": kind}


def result_columns(node: dict) -> tuple[list[str], str]:
    if "project" in node:
        return list(node["project"]["vars"]), "project"
    return sorted(q.free_vars(node)), "sorted_scope"


def mapping_cells(mapping: dict, columns: list[str]) -> list[dict]:
    cells = []
    for name in columns:
        key = mapping.get(name)
        if key is None:
            cells.append({"bound": False})
        else:
            cells.append({"bound": True, "term": terms.key_to_term(key)})
    return cells


def serialize_result(node: dict, bag: dict, steps: list[dict] | None) -> dict:
    columns, source = result_columns(node)
    rows = []
    for fk in sorted(bag, key=lambda k: _sort_key(unfreeze(k), columns)):
        mapping = unfreeze(fk)
        rows.append({
            "cells": mapping_cells(mapping, columns),
            "multiplicity": bag[fk],
        })
    result = {
        "columns": columns,
        "columns_source": source,
        "rows": rows,
        "row_count": sum(bag.values()),
        "distinct_rows": len(bag),
    }
    if steps is not None:
        result["plan"] = steps
    return result


def _sort_key(mapping: dict, columns: list[str]) -> tuple:
    out = []
    for name in columns:
        key = mapping.get(name)
        out.append((0, "") if key is None else (1, key))
    return tuple(out)


def run_query(conn, tree, *, explain: bool = False) -> dict:
    """Validate and execute a query tree against an open store connection."""
    node = q.parse_node(tree)
    q.validate_scopes(node)
    evaluator = Evaluator(conn, explain=explain)
    bag = evaluator.eval_node(node)
    stats = {"path": evaluator.path_stats, "steps": len(evaluator.steps)}
    result = serialize_result(node, bag, evaluator.steps if explain else None)
    if explain:
        result["statistics"] = stats
    return result
