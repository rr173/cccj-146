"""Differential tests on random small graphs against the independent oracle.

The oracle (``tests/oracle.py``) shares no code with ``rdfq``.  It parses the
same public protocol, stores quads in plain Python, computes path endpoint
relations by set algebra / fixpoint, and evaluates bags by direct row mapping.

Each random case compares the complete multiset of projected mappings for
several query shapes, including paths with constants, inverses, sequences,
alternatives and closures.
"""

from __future__ import annotations

import random
import tempfile
import unittest
import pathlib

import harness as H
import oracle as O
from rdfq.query import QueryError

EX = H.EX
PREDS = ["p", "q"]
NODES = ["n0", "n1", "n2", "n3"]


def iri(name):
    return {"type": "iri", "value": EX + name}


def scoped_blank_files(rng, files=2):
    """Random quads split across files, some with blank nodes."""
    out = []
    for f in range(files):
        quads = []
        for _ in range(rng.randint(2, 6)):
            s = rng.choice(NODES + [None])
            o = rng.choice(NODES + [None, "lit"])
            subj = iri(s) if s is not None else {"type": "bnode", "label": rng.choice(["b0", "b1"])}
            if o == "lit":
                obj = {"type": "literal", "value": rng.choice(["x", "y"])}
            elif o is None:
                obj = {"type": "bnode", "label": rng.choice(["b0", "b1"])}
            else:
                obj = iri(o)
            graph = rng.choice([None, H.G("g1"), H.G("g2")])
            quads.append({
                "subject": subj,
                "predicate": iri(rng.choice(PREDS)),
                "object": obj,
                "graph": "default" if graph is None else graph,
            })
        out.append(quads)
    return out


def random_path(rng, depth=0):
    choices = ["predicate"]
    if depth < 2:
        choices += ["inverse", "sequence", "alternative", "zero_or_more", "one_or_more"]
    kind = rng.choice(choices)
    if kind == "predicate":
        return {"predicate": iri(rng.choice(PREDS))}
    if kind == "inverse":
        return {"inverse": random_path(rng, depth + 1)}
    if kind in ("sequence", "alternative"):
        return {kind: [random_path(rng, depth + 1) for _ in range(rng.randint(2, 3))]}
    return {kind: random_path(rng, depth + 1)}


def endpoint_vars(*endpoints):
    return {e["var"] for e in endpoints if "var" in e}


def random_query(rng):
    constants = NODES + ["absent"]
    graph = rng.choice([None, H.G("g1"), H.G("g2")])
    graph_value = "default" if graph is None else graph
    var_names = ["x", "y"]
    # A path node with at most two variables (the protocol endpoint contract).
    start_var = rng.random() < 0.75
    end_var = rng.random() < 0.75
    start = {"var": "x"} if start_var else iri(rng.choice(constants))
    if rng.random() < 0.2:
        end = {"var": "x"} if start_var else iri(rng.choice(constants))
    else:
        end = {"var": "y"} if end_var else iri(rng.choice(constants))
    path_node = {"path": {"graph": graph_value, "start": start,
                          "path": random_path(rng), "end": end}}
    shape = rng.choice(["bare", "values_join", "union", "optional", "filter", "triple_join"])
    scope = sorted(endpoint_vars(start, end))
    if shape == "bare":
        if not scope:
            return None
        return {"project": {"input": path_node, "vars": scope}}
    if shape == "values_join":
        if "x" not in scope:
            return None
        rows = [{"x": iri(rng.choice(NODES))} for _ in range(rng.randint(0, 3))]
        return {"project": {"input": {"join": {
            "left": {"values": {"vars": ["x"], "rows": rows}},
            "right": path_node}}, "vars": scope}}
    if shape == "union":
        if not scope:
            return None
        return {"project": {"input": {"union_all": {
            "left": path_node,
            "right": {"values": {"vars": scope, "rows": [
                {v: iri(rng.choice(constants)) for v in scope}] * rng.randint(0, 2)}},
        }}, "vars": scope}}
    if shape == "optional":
        if "x" not in scope:
            return None
        right = {"values": {"vars": ["y", "z"], "rows": [
            {"y": iri(rng.choice(NODES + ["absent"]))} for _ in range(rng.randint(0, 2))]}}
        node = {"optional": {"left": path_node, "right": right}}
        if rng.random() < 0.5:
            node["optional"]["condition"] = {"same_term": [{"var": "y"}, {"var": "z"}]}
        return {"project": {"input": node, "vars": scope + ["z"]}}
    if shape == "filter":
        if "y" not in scope:
            return None
        other = "x" if "x" in scope else None
        options = [
            {"bound": {"var": "y"}},
            {"same_term": [{"var": "y"}, iri(rng.choice(constants))]},
            {"not": {"same_term": [{"var": "y"}, iri(rng.choice(constants))]}},
        ]
        if other:
            options.append({"or": [
                {"same_term": [{"var": "y"}, iri(rng.choice(constants))]},
                {"bound": {"var": other}}]})
            options.append({"and": [
                {"bound": {"var": other}},
                {"not": {"same_term": [{"var": "y"}, iri(rng.choice(constants))]}}]})
        expr = rng.choice(options)
        return {"project": {"input": {"filter": {"input": path_node, "expr": expr}},
                            "vars": scope}}
    # triple_join
    if "y" not in scope:
        return None
    triple = {"triple": {"graph": graph_value,
                         "s": {"var": "y"},
                         "p": iri(rng.choice(PREDS)),
                         "o": {"var": "o"}}}
    return {"project": {"input": {"join": {"left": path_node, "right": triple}},
                        "vars": scope + ["o"]}}


class RandomDifferentialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rdfq-rand-")
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_random_queries_match_oracle(self):
        rng = random.Random(20261116)
        checked = 0
        for case in range(70):
            files = scoped_blank_files(rng)
            name = f"store{case}.sqlite"
            store = H.build_store(self.dir, files, store_name=name)
            graph = O.load([(f"f{i}", quads) for i, quads in enumerate(files)])
            evaluator = O.Eval(graph)
            for _ in range(8):
                tree = random_query(rng)
                if tree is None:
                    continue
                try:
                    expected_maps = evaluator.run(tree)
                except Exception as exc:
                    raise AssertionError(
                        f"oracle failed on generated tree: {exc!r}\ntree={tree}") from exc
                expected = O.multiset(expected_maps)
                result = H.run_in_process(store, tree)
                got = H.cells_to_oracle_bag(result, store)
                # Production may keep a mapping with an unbound variable that
                # the oracle's projected dict simply omits; oracle.multiset
                # already restricts to keys, so normalise both.
                self.assertEqual(got, expected,
                                 f"case {case}\nfiles={files}\ntree={tree}\n"
                                 f"got={got}\nexpected={expected}")
                checked += 1
        self.assertGreater(checked, 150)

    def test_random_invalid_trees_do_not_run(self):
        # Scope validation must reject references to variables the tree never binds.
        store = H.build_store(self.dir, [[H.quad(H.iri("a"), H.iri("p"), H.iri("b"), H.G())]],
                              store_name="scope.sqlite")
        bad = [
            {"project": {"input": {"values": {"vars": ["x"], "rows": [{"x": H.iri("a")}]}},
                         "vars": ["y"]}},
            {"filter": {"input": {"values": {"vars": ["x"], "rows": [{"x": H.iri("a")}]}},
                        "expr": {"bound": {"var": "nope"}}}},
            {"project": {"input": {"path": {
                "graph": H.G(), "start": {"var": "x"},
                "path": {"predicate": H.iri("p")}, "end": {"var": "y"}}},
                "vars": ["x", "z"]}},
            {"optional": {"left": {"values": {"vars": ["x"], "rows": [{"x": H.iri("a")}]}},
                          "right": {"values": {"vars": [], "rows": [{}]}},
                          "condition": {"bound": {"var": "nope"}}}},
        ]
        for tree in bad:
            with self.assertRaises(QueryError):
                H.run_in_process(store, tree)


if __name__ == "__main__":
    unittest.main()
