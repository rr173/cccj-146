"""Small hand-built fixtures with directly enumerated expectations.

None of these tests import the production path evaluator to compute an
expected answer: every expectation below is written out from the fixture.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import harness as H

EX = H.EX
G = H.G
iri = H.iri
lit = H.lit
bnode = H.bnode
quad = H.quad


def part(s, o, graph=None):
    return quad(iri(s), iri("part"), iri(o), G() if graph is None else graph)


def make_store(tmpdir, quads, name="store.sqlite"):
    return H.build_store(tmpdir, [quads], store_name=name)


def endpoints(result, x="x", y="y"):
    """Bag of (x_label, y_label) extracted from a two-column projection."""
    out = {}
    ix, iy = result["columns"].index(x), result["columns"].index(y)
    for row in result["rows"]:
        left = row["cells"][ix]
        right = row["cells"][iy]
        key = (None if not left["bound"] else H.unwrap(left["term"]["value"]),
               None if not right["bound"] else H.unwrap(right["term"]["value"]))
        out[key] = out.get(key, 0) + row["multiplicity"]
    return out


def all_columns(result):
    """Bag keyed by every column, with None for unbound."""
    out = {}
    for row in result["rows"]:
        cells = []
        for cell in row["cells"]:
            if not cell["bound"]:
                cells.append(None)
            else:
                term = cell["term"]
                if term["type"] == "iri":
                    cells.append(H.unwrap(term["value"]))
                elif term["type"] == "bnode":
                    cells.append(("bnode", term["label"]))
                elif "language" in term:
                    cells.append(("lit", term["value"], term["language"]))
                elif term.get("datatype", "").endswith("#string"):
                    cells.append(("lit", term["value"]))
                else:
                    cells.append(("lit", term["value"], term["datatype"]))
        key = tuple(cells)
        out[key] = out.get(key, 0) + row["multiplicity"]
    return out


class PathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rdfq-small-")
        self.dir = pathlib.Path(self.tmp.name)
        self.quads = [
            part("a", "b"), part("b", "d"),
            part("a", "c"), part("c", "d"),
            part("d", "a"),
        ]
        self.store = make_store(self.dir, self.quads)

    def tearDown(self):
        self.tmp.cleanup()

    def path_query(self, start, path, end, graph=None, project=("x", "y")):
        return {"project": {"input": {
            "path": {
                "graph": G() if graph is None else graph,
                "start": start, "path": path, "end": end,
            }
        }, "vars": list(project)}}

    def test_one_or_more_endpoints_once(self):
        q = self.path_query({"var": "x"}, {"one_or_more": {"predicate": iri("part")}},
                            {"var": "y"})
        result = H.run_in_process(self.store, q)
        got = endpoints(result)
        # direct enumeration: a->{a,b,c,d}, b->{a,b,c,d}, c->{a,b,c,d}, d->{a,b,c,d}
        expected = {(s, t): 1 for s in "abcd" for t in "abcd"}
        self.assertEqual(got, expected)

    def test_zero_or_more_includes_self_endpoint(self):
        q = self.path_query({"var": "x"}, {"zero_or_more": {"predicate": iri("part")}},
                            {"var": "y"})
        result = H.run_in_process(self.store, q)
        got = endpoints(result)
        self.assertEqual(got, {(s, t): 1 for s in "abcd" for t in "abcd"})

    def test_constant_start_and_two_variable_constant_end(self):
        q = self.path_query(iri("a"), {"one_or_more": {"predicate": iri("part")}},
                            {"var": "y"}, project=("y",))
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["columns"], ["y"])
        values = {}
        for row in result["rows"]:
            cell = row["cells"][0]
            values[H.unwrap(cell["term"]["value"])] = values.get(
                H.unwrap(cell["term"]["value"]), 0) + row["multiplicity"]
        self.assertEqual(values, {"a": 1, "b": 1, "c": 1, "d": 1})

    def test_same_variable_both_ends_only_cycles(self):
        # Only nodes lying on a cycle have a non-empty one-or-more route to
        # themselves: a->b->d->a, b->d->a->b, c->d->a->...->c, d->a->...->d.
        q = {
            "project": {"input": {"path": {
                "graph": G(),
                "start": {"var": "x"},
                "path": {"one_or_more": {"predicate": iri("part")}},
                "end": {"var": "x"},
            }}, "vars": ["x"]},
        }
        result = H.run_in_process(self.store, q)
        got = {}
        for row in result["rows"]:
            cell = row["cells"][0]
            key = H.unwrap(cell["term"]["value"])
            got[key] = got.get(key, 0) + row["multiplicity"]
        self.assertEqual(got, {"a": 1, "b": 1, "c": 1, "d": 1})
        self.assertEqual(result["row_count"], 4)

    def test_constant_start_zero_or_more_absent_constant(self):
        q = self.path_query(H.iri("absent"),
                            {"zero_or_more": {"predicate": iri("part")}},
                            {"var": "y"}, project=("y",))
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["row_count"], 0)

    def test_zero_or_more_constant_present_only_zero(self):
        # a present literal node with no outgoing part edge
        quads = self.quads + [quad(iri("a"), iri("label"), lit("x"), G())]
        store = make_store(self.dir, quads, name="store2.sqlite")
        q = {"project": {"input": {"path": {
            "graph": G(), "start": lit("x"),
            "path": {"zero_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(result["row_count"], 1)
        cell = result["rows"][0]["cells"][0]
        self.assertEqual(cell["term"], {"type": "literal", "value": "x",
                                        "datatype": "http://www.w3.org/2001/XMLSchema#string"})

    def test_literal_node_zero_length(self):
        quads = [quad(iri("a"), iri("label"), lit("tag"), G()),
                 quad(iri("a"), iri("part"), iri("b"), G())]
        store = make_store(self.dir, quads, name="lit.sqlite")
        # zero-length from a literal endpoint to itself
        q = {"project": {"input": {"path": {
            "graph": G(), "start": lit("tag"),
            "path": {"zero_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(result["row_count"], 1)

    def test_default_graph_not_merged_with_named(self):
        quads = [
            quad(iri("a"), iri("part"), iri("b"), None),
            quad(iri("b"), iri("part"), iri("c"), G()),
        ]
        store = make_store(self.dir, quads, name="graphs.sqlite")
        q = {"project": {"input": {"path": {
            "graph": "default", "start": iri("a"),
            "path": {"one_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["cells"][0]["term"]["value"], EX + "b")

    def test_two_graphs_halves_not_joined(self):
        quads = [
            quad(iri("a"), iri("part"), iri("b"), H.G("g1")),
            quad(iri("b"), iri("part"), iri("c"), H.G("g2")),
        ]
        store = make_store(self.dir, quads, name="halves.sqlite")
        # Start a is only in g1; a's route inside g2 does not exist.
        for graph, start, expected in (
                (H.G("g1"), "a", {"b"}),
                (H.G("g2"), "a", set()),
                (H.G("g2"), "b", {"c"}),
                (H.G("g1"), "b", set())):
            q = {"project": {"input": {"path": {
                "graph": graph, "start": iri(start),
                "path": {"one_or_more": {"predicate": iri("part")}},
                "end": {"var": "y"}}}, "vars": ["y"]}}
            result = H.run_in_process(store, q)
            got = {H.unwrap(r["cells"][0]["term"]["value"]) for r in result["rows"]}
            self.assertEqual(got, expected, f"graph {graph} start {start}")

    def test_both_endpoints_constant(self):
        inside = {"project": {"input": {"path": {
            "graph": G(), "start": iri("a"),
            "path": {"one_or_more": {"predicate": iri("part")}},
            "end": iri("d")}}, "vars": []}}
        result = H.run_in_process(self.store, inside)
        # a reaches d, so one row with an empty mapping
        self.assertEqual(result["row_count"], 1)
        outside = {"project": {"input": {"path": {
            "graph": G(), "start": iri("d"),
            "path": {"one_or_more": {"predicate": iri("other")}},
            "end": iri("a")}}, "vars": []}}
        self.assertEqual(H.run_in_process(self.store, outside)["row_count"], 0)
        # zero_or_more from a constant to an unrelated present constant: no row
        unrelated = {"project": {"input": {"path": {
            "graph": G(), "start": iri("a"),
            "path": {"zero_or_more": {"predicate": iri("other")}},
            "end": iri("b")}}, "vars": []}}
        self.assertEqual(H.run_in_process(self.store, unrelated)["row_count"], 0)

    def test_inverse_sequence_nested(self):
        quads = [
            part("a", "b"), part("b", "c"), part("c", "d"),
            quad(iri("c"), iri("other"), iri("e"), G()),
        ]
        store = make_store(self.dir, quads, name="inv.sqlite")
        # sequence(inverse(part), other): start d -> c via inverse(part), then c->e
        q = {"project": {"input": {"path": {
            "graph": G(), "start": iri("d"),
            "path": {"sequence": [
                {"inverse": {"predicate": iri("part")}},
                {"predicate": iri("other")},
            ]},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q)
        self.assertEqual([H.unwrap(r["cells"][0]["term"]["value"]) for r in result["rows"]],
                         ["e"])
        # inverse(sequence(part, part)) from d: d <-part- c <-part- b, so y = b
        q2 = {"project": {"input": {"path": {
            "graph": G(), "start": iri("d"),
            "path": {"inverse": {"sequence": [
                {"predicate": iri("part")},
                {"predicate": iri("part")},
            ]}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result2 = H.run_in_process(store, q2)
        self.assertEqual([H.unwrap(r["cells"][0]["term"]["value"]) for r in result2["rows"]],
                         ["b"])

    def test_alternative_zero_or_more_self_loop(self):
        quads = [
            part("a", "a"),
            quad(iri("a"), iri("alt"), iri("b"), G()),
        ]
        store = make_store(self.dir, quads, name="alt.sqlite")
        q = {"project": {"input": {"path": {
            "graph": G(), "start": iri("a"),
            "path": {"zero_or_more": {"alternative": [
                {"predicate": iri("part")}, {"predicate": iri("alt")},
            ]}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q)
        got = {H.unwrap(r["cells"][0]["term"]["value"]) for r in result["rows"]}
        self.assertEqual(got, {"a", "b"})
        self.assertEqual(result["row_count"], 2)


class BagSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rdfq-bag-")
        self.dir = pathlib.Path(self.tmp.name)
        self.quads = [
            part("a", "b"), part("b", "d"), part("a", "c"), part("c", "d"),
            part("d", "a"),
            quad(iri("b"), iri("label"), lit("保留"), G()),
            quad(iri("c"), iri("label"), lit("其他"), G()),
        ]
        self.store = make_store(self.dir, self.quads)

    def tearDown(self):
        self.tmp.cleanup()

    def reach(self, rows):
        return {
            "project": {"input": {"join": {
                "left": {"values": {"vars": ["x"], "rows": rows}},
                "right": {"path": {
                    "graph": G(), "start": {"var": "x"},
                    "path": {"one_or_more": {"predicate": iri("part")}},
                    "end": {"var": "y"}}},
            }}, "vars": ["x", "y"]},
        }

    def test_values_repeated_start_multiplies_endpoints(self):
        result = H.run_in_process(self.store, self.reach([{"x": iri("a")}, {"x": iri("a")}]))
        got = endpoints(result)
        self.assertEqual(got, {("a", y): 2 for y in "abcd"})

    def test_union_all_adds_multiplicity_then_project_keeps(self):
        q = {"project": {"input": {"union_all": {
            "left": {"path": {"graph": G(), "start": iri("a"),
                              "path": {"predicate": iri("part")}, "end": {"var": "y"}}},
            "right": {"path": {"graph": G(), "start": iri("a"),
                               "path": {"predicate": iri("part")}, "end": {"var": "y"}}},
        }}, "vars": ["y"]}}
        result = H.run_in_process(self.store, q)
        got = {H.unwrap(r["cells"][0]["term"]["value"]): r["multiplicity"]
               for r in result["rows"]}
        self.assertEqual(got, {"b": 2, "c": 2})

    def test_distinct_collapses_after_projection(self):
        q = {"distinct": {"input": {"project": {
            "input": {"values": {"vars": ["x", "y"], "rows": [
                {"x": iri("a"), "y": iri("b")},
                {"x": iri("a"), "y": iri("c")},
            ]}},
            "vars": ["x"],
        }}}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["multiplicity"], 1)

    def test_values_unbound_cells_and_empty_rows(self):
        tree = {"union_all": {
            "left": {"values": {"vars": ["x", "y"], "rows": [
                {"x": iri("a")},
                {"x": iri("a"), "y": iri("b")},
                {"y": iri("b")},
            ]}},
            "right": {"values": {"vars": ["x", "y"], "rows": []}},
        }}
        result = H.run_in_process(self.store, {"project": {"input": tree, "vars": ["x", "y"]}})
        rows = sorted(((tuple(None if not c["bound"] else H.unwrap(c["term"]["value"])
                              for c in r["cells"]), r["multiplicity"])
                       for r in result["rows"]), key=repr)
        self.assertEqual(rows, [(('a', 'b'), 1), (('a', None), 1), ((None, 'b'), 1)])

    def test_empty_variable_values(self):
        q = {"project": {"input": {"values": {"vars": [], "rows": [{}]}}, "vars": []}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["cells"], [])

    def test_join_bag_multiplicity(self):
        # values with two identical rows joined with a two-row values table
        q = {"project": {"input": {"join": {
            "left": {"values": {"vars": ["x"], "rows": [{"x": iri("a")}, {"x": iri("a")}]}},
            "right": {"values": {"vars": ["y"], "rows": [{"y": iri("b")}, {"y": iri("b")}]}},
        }}, "vars": ["x", "y"]}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["row_count"], 4)

    def test_optional_multiple_qualifying_labels(self):
        quads = self.quads + [
            quad(iri("b"), iri("label"), lit("保留"), G()),
            quad(iri("b"), iri("label"), lit("保留"), G()),  # duplicate quad collapses
        ]
        store = make_store(self.dir, quads, name="opt2.sqlite")
        # b has one stored label quad, but join with a 2-row values table doubles it
        q = {"project": {"input": {"join": {
            "left": {"values": {"vars": ["y"], "rows": [{"y": iri("b")}, {"y": iri("b")}]}},
            "right": {"optional": {
                "left": {"triple": {"graph": G(), "s": {"var": "y"},
                                    "p": iri("label"), "o": {"var": "l"}}},
                "right": {"values": {"vars": [], "rows": [{}]}},
            }},
        }}, "vars": ["y", "l"]}}
        result = H.run_in_process(store, q)
        got = all_columns(result)
        self.assertEqual(got, {("b", ("lit", "保留")): 2})

    def test_optional_multiple_qualifying_right_rows_multiply(self):
        # b has two distinct labels; left has two copies of b.  Every left row
        # can match both right rows (no condition), so 2 x 2 = 4 rows.
        quads = self.quads + [
            quad(iri("b"), iri("label"), lit("保留"), G()),
            quad(iri("b"), iri("label"), lit("另一个"), G()),
        ]
        store = make_store(self.dir, quads, name="opt3.sqlite")
        q = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["y"], "rows": [{"y": iri("b")},
                                                        {"y": iri("b")}]}},
            "right": {"triple": {"graph": G(), "s": {"var": "y"},
                                 "p": iri("label"), "o": {"var": "l"}}},
        }}, "vars": ["y", "l"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(all_columns(result), {
            ("b", ("lit", "保留")): 2,
            ("b", ("lit", "另一个")): 2,
        })

    def test_optional_condition_selects_one_of_two_labels(self):
        quads = self.quads + [
            quad(iri("b"), iri("label"), lit("保留"), G()),
            quad(iri("b"), iri("label"), lit("另一个"), G()),
        ]
        store = make_store(self.dir, quads, name="opt4.sqlite")
        q = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["y"], "rows": [{"y": iri("b")},
                                                        {"y": iri("b")}]}},
            "right": {"triple": {"graph": G(), "s": {"var": "y"},
                                 "p": iri("label"), "o": {"var": "l"}}},
            "condition": {"same_term": [{"var": "l"}, lit("保留")]},
        }}, "vars": ["y", "l"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(all_columns(result), {("b", ("lit", "保留")): 2})

    def test_optional_unmatched_keeps_multiplicity(self):
        # d has no label: both d rows survive the optional with l unbound.
        # The two-row values table must be inside the optional's left branch,
        # because the optional's right tree is evaluated independently.
        q = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["y"], "rows": [{"y": iri("d")},
                                                        {"y": iri("d")}]}},
            "right": {"triple": {"graph": G(), "s": {"var": "y"},
                                 "p": iri("label"), "o": {"var": "l"}}},
        }}, "vars": ["y", "l"]}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(all_columns(result), {("d", None): 2})

    def test_same_label_different_datatype_and_language(self):
        quads = [
            quad(iri("s"), iri("p"), lit("01", "http://www.w3.org/2001/XMLSchema#integer"), G()),
            quad(iri("s"), iri("p"), lit("1", "http://www.w3.org/2001/XMLSchema#integer"), G()),
            quad(iri("s"), iri("p"), lit("Chat", language="FR"), G()),
            quad(iri("s"), iri("p"), lit("chat", language="fr"), G()),
        ]
        store = make_store(self.dir, quads, name="terms.sqlite")
        q = {"project": {"input": {"triple": {
            "graph": G(), "s": iri("s"), "p": iri("p"), "o": {"var": "o"},
        }}, "vars": ["o"]}}
        result = H.run_in_process(store, q)
        keys = sorted(
            (cells[0][1], cells[0][2] if len(cells[0]) > 2 else "")
            for cells in all_columns(result))
        # lexical forms are never normalised: 01 and 1 stay distinct, and the
        # two language literals differ only by lexical form (language tag is
        # lower-cased, so "FR" and "fr" share one tag but not one term).
        self.assertEqual(len(keys), 4)
        self.assertIn(("01", "http://www.w3.org/2001/XMLSchema#integer"), keys)
        self.assertIn(("1", "http://www.w3.org/2001/XMLSchema#integer"), keys)
        self.assertIn(("Chat", "fr"), keys)
        self.assertIn(("chat", "fr"), keys)

    def test_repeated_quad_deduplicated(self):
        quads = [part("a", "b"), part("a", "b"), part("a", "b")]
        store = make_store(self.dir, quads, name="dup.sqlite")
        q = {"project": {"input": {"triple": {
            "graph": G(), "s": iri("a"), "p": iri("part"), "o": {"var": "o"},
        }}, "vars": ["o"]}}
        result = H.run_in_process(store, q)
        self.assertEqual(result["row_count"], 1)


class ExprTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rdfq-expr-")
        self.dir = pathlib.Path(self.tmp.name)
        self.quads = [part("a", "b"), part("a", "c"),
                      quad(iri("a"), iri("label"), lit("x"), G())]
        self.store = make_store(self.dir, self.quads)

    def tearDown(self):
        self.tmp.cleanup()

    def filt(self, expr, rows=(("x", iri("a")),)):
        values = {"vars": ["x", "missing"], "rows": [{k: v} for k, v in rows]}
        return {"project": {"input": {"filter": {"input": {"values": values},
                                                 "expr": expr}},
                            "vars": ["x"]}}

    def test_error_combinations(self):
        # same_term(x, unbound y) is an error; NOT(error) stays error and drops.
        tree = self.filt({"not": {"same_term": [{"var": "x"}, {"var": "missing"}]}})
        result = H.run_in_process(self.store, tree)
        self.assertEqual(result["row_count"], 0)
        # false AND error = false
        tree = self.filt({"and": [
            {"same_term": [{"var": "x"}, iri("zzz")]},
            {"same_term": [{"var": "x"}, {"var": "missing"}]},
        ]})
        self.assertEqual(H.run_in_process(self.store, tree)["row_count"], 0)
        # true AND error = error -> drop
        tree = self.filt({"and": [
            {"bound": {"var": "x"}},
            {"same_term": [{"var": "x"}, {"var": "missing"}]},
        ]})
        probe = H.run_in_process(self.store, tree)
        self.assertEqual(probe["row_count"], 0)
        # true OR error = true -> keep
        tree = self.filt({"or": [
            {"bound": {"var": "x"}},
            {"same_term": [{"var": "x"}, {"var": "missing"}]},
        ]})
        self.assertEqual(H.run_in_process(self.store, tree)["row_count"], 1)
        # false OR error = error -> drop
        tree = self.filt({"or": [
            {"same_term": [{"var": "x"}, iri("zzz")]},
            {"same_term": [{"var": "x"}, {"var": "missing"}]},
        ]})
        self.assertEqual(H.run_in_process(self.store, tree)["row_count"], 0)

    def test_bound_uses_presence_only(self):
        # x is bound but the extra variable y is not
        tree = {"project": {"input": {"filter": {
            "input": {"values": {"vars": ["x", "y"], "rows": [{"x": iri("a")}]}},
            "expr": {"and": [{"bound": {"var": "x"}}, {"not": {"bound": {"var": "y"}}}]}}},
            "vars": ["x"]}}
        self.assertEqual(H.run_in_process(self.store, tree)["row_count"], 1)

    def test_optional_condition_error_keeps_left_row(self):
        # condition compares an unbound variable -> error, not true: the left
        # row is kept once with the right variables unbound.
        q = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["x"], "rows": [{"x": iri("a")},
                                                        {"x": iri("a")}]}},
            "right": {"values": {"vars": ["z"], "rows": [{"z": iri("b")}]}},
            "condition": {"same_term": [{"var": "z"}, {"var": "x"}]},
        }}, "vars": ["x", "z"]}}
        result = H.run_in_process(self.store, q)
        got = all_columns(result)
        # right binds z=b, x=a so the condition is false, not an error
        self.assertEqual(got, {("a", None): 2})
        # force a real error: compare bound z with an unbound variable y
        q2 = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["x"], "rows": [{"x": iri("a")}]}},
            "right": {"values": {"vars": ["z", "w"], "rows": [{"z": iri("b")}]}},
            "condition": {"same_term": [{"var": "z"}, {"var": "w"}]},
        }}, "vars": ["x", "z", "w"]}}
        result2 = H.run_in_process(self.store, q2)
        self.assertEqual(all_columns(result2), {("a", None, None): 1})

    def test_optional_condition_true_and_false_mix(self):
        q = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["x"], "rows": [
                {"x": iri("a")}, {"x": iri("b")}]}},
            "right": {"values": {"vars": ["z"], "rows": [
                {"z": iri("a")}, {"z": iri("b")}]}},
            "condition": {"same_term": [{"var": "x"}, {"var": "z"}]},
        }}, "vars": ["x", "z"]}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(all_columns(result), {("a", "a"): 1, ("b", "b"): 1})

    def test_filter_can_remove_unbound_after_optional(self):
        q = {"project": {"input": {"filter": {
            "input": {"optional": {
                "left": {"values": {"vars": ["x"], "rows": [
                    {"x": iri("a")}, {"x": iri("a")}]}},
                "right": {"values": {"vars": ["z"], "rows": []}},
            }},
            "expr": {"not": {"bound": {"var": "z"}}},
        }}, "vars": ["x"]}}
        result = H.run_in_process(self.store, q)
        self.assertEqual(result["row_count"], 2)


class ScaleTest(unittest.TestCase):
    """Path evaluation must not enumerate routes.

    The fixture has k parallel two-edge routes per start/diamond and back
    edges; the number of walks is exponential while the endpoint relation is
    small.  A route-enumerating evaluator would not finish this.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rdfq-scale-")
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_diamond_chain_no_walk_explosion(self):
        quads = []
        layers = 60
        for i in range(layers):
            quads.append(part(f"n{i}", f"a{i}"))
            quads.append(part(f"n{i}", f"b{i}"))
            quads.append(part(f"a{i}", f"n{i+1}"))
            quads.append(part(f"b{i}", f"n{i+1}"))
        # back edges from the far end into the first layer create many cycles
        quads.append(part(f"n{layers}", "n0"))
        quads.append(part(f"n{layers}", "a0"))
        store = H.build_store(self.dir, [quads], store_name="scale.sqlite")
        q = {"project": {"input": {"path": {
            "graph": G(), "start": iri("n0"),
            "path": {"one_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"}}}, "vars": ["y"]}}
        result = H.run_in_process(store, q, explain=True)
        # Reachable nodes: n0..n60 plus a0..a59 and b0..b59 (all on routes).
        expected = {f"n{i}" for i in range(layers + 1)}
        expected |= {f"a{i}" for i in range(layers)}
        expected |= {f"b{i}" for i in range(layers)}
        got = {H.unwrap(r["cells"][0]["term"]["value"]) for r in result["rows"]}
        self.assertEqual(got, expected)
        self.assertEqual(result["row_count"], len(expected))
        path_step = next(s for s in result["plan"] if s["operator"] == "path")
        self.assertLess(path_step["endpoint_pairs"], 2 * len(expected))
        stats = result["statistics"]["path"]
        # states are (node, frontier) entries, bounded by nodes x rounds,
        # not by the exponentially many walks through the diamond chain
        self.assertLess(stats["states_expanded"], 20 * len(expected))
        self.assertLess(stats["frontier_rounds"], 5 * len(expected))

    def test_zero_or_more_terminates_on_self_loop(self):
        quads = [part("a", "a"), part("a", "b"), part("b", "a")]
        store = H.build_store(self.dir, [quads], store_name="loop.sqlite")
        q = {"project": {"input": {"path": {
            "graph": G(), "start": {"var": "x"},
            "path": {"zero_or_more": {"sequence": [
                {"predicate": iri("part")}, {"predicate": iri("part")}]}},
            "end": {"var": "y"}}}, "vars": ["x", "y"]}}
        result = H.run_in_process(store, q)
        got = endpoints(result)
        self.assertEqual(got, {("a", "a"): 1, ("a", "b"): 1,
                               ("b", "a"): 1, ("b", "b"): 1})


if __name__ == "__main__":
    unittest.main()
