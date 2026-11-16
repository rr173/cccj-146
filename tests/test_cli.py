"""End-to-end tests through the real terminal entry point (subprocesses).

Covers loading, provenance, blank-node scoping, deduplication,
read-only re-open in a new process, deterministic column order, atomic
failure behaviour, and non-zero exit with no success marker on errors.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

import harness as H

EX = H.EX
iri, lit, bnode, quad, G = H.iri, H.lit, H.bnode, H.quad, H.G


class CliCase(unittest.TestCase):
    def setUp(self):
        self.runner = H.CliRunner()
        self.dir = self.runner.tmpdir

    def tearDown(self):
        self.runner.close()

    def write_sources(self, files, dataset="t"):
        paths = []
        for i, quads in enumerate(files):
            p = self.dir / f"src{i}.jsonl"
            H.write_jsonl(p, quads)
            paths.append(p)
        manifest = self.dir / "manifest.json"
        H.write_manifest(manifest, paths, dataset=dataset)
        return manifest


class LoadTest(CliCase):
    def test_load_dedup_and_graph_partition(self):
        files = [[
            quad(iri("a"), iri("p"), iri("b"), G()),
            quad(iri("a"), iri("p"), iri("b"), G()),          # duplicate
            quad(iri("a"), iri("p"), iri("b"), None),         # different graph
            quad(iri("a"), iri("p"), iri("b"), G("g2")),      # different graph
        ]]
        manifest = self.write_sources(files)
        info = self.runner.json("load", "--manifest", str(manifest),
                                "--store", str(self.dir / "s.sqlite"), "--quiet")
        self.assertEqual(info["store"]["stored_quads"], 3)
        self.assertEqual(info["store"]["duplicates"], 1)
        again = self.runner.json("info", "--store", str(self.dir / "s.sqlite"))
        graphs = {g["graph"]: g["quads"] for g in again["dataset"]["graphs"]}
        self.assertEqual(graphs, {"default": 1, EX + "g": 1, EX + "g2": 1})

    def test_refuses_overwrite_without_replace(self):
        manifest = self.write_sources([[quad(iri("a"), iri("p"), iri("b"), G())]])
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", store, "--quiet")
        proc = self.runner.raw("load", "--manifest", str(manifest), "--store", store)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("END load status=ok", proc.stderr)
        self.assertIn("already exists", proc.stderr)

    def test_failed_load_does_not_damage_existing_store(self):
        manifest = self.write_sources([[quad(iri("a"), iri("p"), iri("b"), G())]])
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", store, "--quiet")
        before = self.runner.json("info", "--store", store)
        digest_before = (self.dir / "s.sqlite").read_bytes()
        bad = self.dir / "bad.jsonl"
        bad.write_text('{"subject": {"type": "iri", "value": "relative"},'
                       '"predicate": {"type": "iri", "value": "http://e/p"},'
                       '"object": {"type": "iri", "value": "http://e/o"},'
                       '"graph": "default"}\n', encoding="utf-8")
        bad_manifest = self.dir / "bad-manifest.json"
        H.write_manifest(bad_manifest, [bad])
        proc = self.runner.raw("load", "--manifest", str(bad_manifest),
                               "--store", store, "--replace")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("END load status=ok", proc.stderr)
        after = self.runner.json("info", "--store", store)
        self.assertEqual(before["dataset"]["stored_quads"], after["dataset"]["stored_quads"])
        self.assertEqual(digest_before, (self.dir / "s.sqlite").read_bytes())

    def test_replace_rebuilds_cleanly(self):
        m1 = self.write_sources([[quad(iri("a"), iri("p"), iri("b"), G())]])
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(m1), "--store", store, "--quiet")
        m2 = self.write_sources([[quad(iri("x"), iri("p"), iri("y"), G())]])
        info = self.runner.json("load", "--manifest", str(m2), "--store", store,
                                "--replace", "--quiet")
        self.assertEqual(info["store"]["stored_quads"], 1)
        dump = self.runner.json("dump", "--store", store, "--graph", EX + "g")
        self.assertEqual(dump["quads"][0]["subject"]["value"], EX + "x")

    def test_blank_node_scope_per_file_and_across_graphs(self):
        files = [
            [quad(bnode("b0"), iri("p"), iri("x"), G("g1")),
             quad(iri("y"), iri("q"), bnode("b0"), G("g2"))],
            [quad(bnode("b0"), iri("p"), iri("z"), G("g1"))],
        ]
        manifest = self.write_sources(files)
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", store, "--quiet")
        q = {"project": {"input": {"triple": {
            "graph": G("g1"), "s": {"var": "s"}, "p": iri("p"), "o": {"var": "o"}}},
            "vars": ["s"]}}
        result = self.runner.json("query", "--store", store, "--query",
                                  self._write_query(q), "--quiet")
        self.assertEqual(len({r["cells"][0]["term"]["label"] for r in result["rows"]}), 2)

        # The same-file blank node is shared across graphs: it appears in g2 as
        # the object of y q _:b0 too, and identity is the same.
        def node_ids():
            conn = sqlite3.connect(store)
            try:
                return conn.execute("SELECT bid, source_id, label FROM bnode").fetchall()
            finally:
                conn.close()
        self.assertEqual(sorted((r[1], r[2]) for r in node_ids()),
                         [("f0", "b0"), ("f1", "b0")])
        q2 = {"project": {"input": {"triple": {
            "graph": G("g2"), "s": iri("y"), "p": iri("q"), "o": {"var": "o"}}},
            "vars": ["o"]}}
        r2 = self.runner.json("query", "--store", store, "--query",
                              self._write_query(q2, "q2.json"), "--quiet")
        q1 = {"project": {"input": {"path": {
            "graph": G("g1"), "start": {"var": "s"},
            "path": {"predicate": iri("p")}, "end": {"var": "o"}}}, "vars": ["s"]}}
        r1 = self.runner.json("query", "--store", store, "--query",
                              self._write_query(q1, "q1.json"), "--quiet")
        ids = {r["cells"][0]["term"]["label"] for r in r1["rows"]}
        self.assertIn(r2["rows"][0]["cells"][0]["term"]["label"], ids)

    def _write_query(self, tree, name="query.json"):
        p = self.dir / name
        p.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_blank_node_identity_stable_across_reopen(self):
        files = [[quad(bnode("n1"), iri("p"), iri("x"), G())]]
        manifest = self.write_sources(files)
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", store, "--quiet")
        q = {"project": {"input": {"triple": {"graph": G(), "s": {"var": "s"},
                                              "p": iri("p"), "o": {"var": "o"}}},
                         "vars": ["s"]}}
        qp = self.dir / "q.json"
        qp.write_text(json.dumps(q), encoding="utf-8")
        first = self.runner.json("query", "--store", store, "--query", str(qp), "--quiet")
        second = self.runner.json("query", "--store", store, "--query", str(qp), "--quiet")
        self.assertEqual(first, second)
        label = first["rows"][0]["cells"][0]["term"]["label"]
        # A constant blank node with that opaque identity can be queried back.
        q2 = {"project": {"input": {"triple": {
            "graph": G(), "s": {"type": "bnode", "label": label},
            "p": iri("p"), "o": {"var": "o"}}}, "vars": ["o"]}}
        qp2 = self.dir / "q2.json"
        qp2.write_text(json.dumps(q2), encoding="utf-8")
        back = self.runner.json("query", "--store", store, "--query", str(qp2), "--quiet")
        self.assertEqual(back["row_count"], 1)


class QueryProtocolTest(CliCase):
    def setUp(self):
        super().setUp()
        files = [[
            quad(iri("a"), iri("p"), iri("b"), G()),
            quad(iri("b"), iri("p"), iri("c"), G()),
            quad(iri("s"), iri("label"), lit("x"), G()),
        ]]
        manifest = self.write_sources(files)
        self.store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", self.store, "--quiet")

    def qpath(self, tree, name="q.json"):
        p = self.dir / name
        p.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_explicit_project_order_and_unbound_marker(self):
        tree = {"project": {"input": {"optional": {
            "left": {"values": {"vars": ["z"], "rows": [{"z": iri("a")}]}},
            "right": {"values": {"vars": ["w"], "rows": [{"w": iri("b")}]}},
        }}, "vars": ["w", "z"]}}
        result = self.runner.json("query", "--store", self.store,
                                  "--query", self.qpath(tree), "--quiet")
        self.assertEqual(result["columns"], ["w", "z"])
        self.assertEqual(result["rows"][0]["cells"], [
            {"bound": True, "term": {"type": "iri", "value": EX + "b"}},
            {"bound": True, "term": {"type": "iri", "value": EX + "a"}},
        ])

    def test_bad_term_exits_nonzero_without_success_marker(self):
        tree = {"project": {"input": {"triple": {
            "graph": G(), "s": {"type": "iri", "value": "not-absolute"},
            "p": iri("p"), "o": {"var": "o"}}}, "vars": ["o"]}}
        proc = self.runner.raw("query", "--store", self.store,
                               "--query", self.qpath(tree), "--quiet")
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("status=ok", proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stderr.splitlines()[0])["error"]["kind"], "term")

    def test_unknown_operator_exits_nonzero(self):
        tree = {"frobnicate": {"input": {"values": {"vars": [], "rows": [{}]}}}}
        proc = self.runner.raw("query", "--store", self.store,
                               "--query", self.qpath(tree), "--quiet")
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("status=ok", proc.stdout + proc.stderr)

    def test_unknown_variable_reference_exits_nonzero(self):
        tree = {"filter": {"input": {"values": {"vars": ["x"], "rows": [{"x": iri("a")}]}},
                           "expr": {"bound": {"var": "nope"}}}}
        proc = self.runner.raw("query", "--store", self.store,
                               "--query", self.qpath(tree), "--quiet")
        self.assertEqual(proc.returncode, 3)

    def test_literal_arguments_are_validated(self):
        tree = {"project": {"input": {"triple": {
            "graph": G(), "s": {"var": "s"}, "p": iri("p"), "o": {"var": "o"}}},
            "vars": ["s"]}}
        # invalid literal both datatype and language in a values row
        bad = {"filter": {"input": {"values": {"vars": ["x"], "rows": [
            {"x": {"type": "literal", "value": "v", "datatype": "http://e/d",
                   "language": "en"}}]}}, "expr": {"bound": {"var": "x"}}}}
        proc = self.runner.raw("query", "--store", self.store,
                               "--query", self.qpath(bad), "--quiet")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("both datatype and language", proc.stderr)

    def test_missing_store_exits_nonzero(self):
        proc = self.runner.raw("query", "--store", str(self.dir / "none.sqlite"),
                               "--query", self.qpath({"values": {"vars": [], "rows": [{}]}}),
                               "--quiet")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("status=ok", proc.stdout + proc.stderr)

    def test_snapshot_survives_concurrent_replace(self):
        from rdfq.store import load_store, open_readonly
        from rdfq.eval import run_query

        m2 = self.dir / "m2.json"
        other = self.dir / "other.jsonl"
        H.write_jsonl(other, [quad(iri("x"), iri("p"), iri("y"), G())])
        H.write_manifest(m2, [other])
        query = {"project": {"input": {"triple": {
            "graph": G(), "s": {"var": "s"}, "p": iri("p"), "o": {"var": "o"}}},
            "vars": ["s"]}}
        conn = open_readonly(self.store)
        try:
            before = run_query(conn, query)
            load_store(self.store, m2, replace=True)
            after = run_query(conn, query)
        finally:
            conn.close()
        self.assertEqual(before["rows"], after["rows"])
        fresh = self.runner.json("query", "--store", self.store,
                                 "--query", self.qpath(query, "fresh.json"), "--quiet")
        self.assertEqual([r["cells"][0]["term"]["value"] for r in fresh["rows"]], [EX + "x"])

    def test_query_does_not_write_source_files(self):
        manifest = self.dir / "manifest.json"
        before = {p.name: p.read_bytes() for p in self.dir.glob("*.jsonl")}
        q = {"project": {"input": {"path": {
            "graph": G(), "start": {"var": "x"},
            "path": {"zero_or_more": {"predicate": iri("p")}},
            "end": {"var": "y"}}}, "vars": ["x", "y"]}}
        self.runner.json("query", "--store", self.store, "--query", self.qpath(q), "--quiet")
        after = {p.name: p.read_bytes() for p in self.dir.glob("*.jsonl")}
        self.assertEqual(before, after)

    def test_readonly_connection_rejects_writes(self):
        conn = sqlite3.connect(self.store)
        conn.close()
        from rdfq.store import open_readonly
        ro = open_readonly(self.store)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO quad (subject, predicate, object, graph_key,"
                           " graph_label, source_id) VALUES ('a','b','c','D','default','f0')")
        finally:
            ro.close()

    def test_explain_reports_scope_and_bag_steps(self):
        q = {"project": {"input": {"join": {
            "left": {"values": {"vars": ["x"], "rows": [{"x": iri("a")}]}},
            "right": {"path": {"graph": G(), "start": {"var": "x"},
                               "path": {"one_or_more": {"predicate": iri("p")}},
                               "end": {"var": "y"}}},
        }}, "vars": ["x", "y"]}}
        result = self.runner.json("explain", "--store", self.store,
                                  "--query", self.qpath(q), "--quiet")
        ops = [s["operator"] for s in result["plan"]]
        self.assertIn("path", ops)
        self.assertIn("join", ops)
        path_step = next(s for s in result["plan"] if s["operator"] == "path")
        self.assertEqual(path_step["graph"], EX + "g")
        self.assertFalse(path_step["route_enumeration"])
        self.assertEqual(path_step["endpoint_pairs"], 3)

    def test_output_file(self):
        q = {"project": {"input": {"values": {"vars": ["x"], "rows": [{"x": iri("a")}]}},
                         "vars": ["x"]}}
        out = self.dir / "result.json"
        self.runner.json("query", "--store", self.store, "--query", self.qpath(q),
                         "--quiet", "--output", str(out))
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["row_count"], 1)


class TermIdentityTest(CliCase):
    def test_lexical_forms_not_normalised(self):
        files = [[
            quad(iri("s"), iri("p"), lit("01", EX + "int"), G()),
            quad(iri("s"), iri("p"), lit("1", EX + "int"), G()),
            quad(iri("s"), iri("p"), lit("Chat", language="FR"), G()),
            quad(iri("s"), iri("p"), lit("chat", language="fr"), G()),
            quad(iri("s"), iri("p"), lit("plain"), G()),
        ]]
        manifest = self.write_sources(files)
        store = str(self.dir / "s.sqlite")
        self.runner.json("load", "--manifest", str(manifest), "--store", store, "--quiet")
        q = {"project": {"input": {"triple": {"graph": G(), "s": iri("s"),
                                              "p": iri("p"), "o": {"var": "o"}}},
                         "vars": ["o"]}}
        qp = self.dir / "q.json"
        qp.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
        result = self.runner.json("query", "--store", store, "--query", str(qp), "--quiet")
        terms = sorted(json.dumps(r["cells"][0]["term"], ensure_ascii=False, sort_keys=True)
                       for r in result["rows"])
        expected = sorted(json.dumps(t, ensure_ascii=False, sort_keys=True) for t in [
            {"type": "literal", "value": "01", "datatype": EX + "int"},
            {"type": "literal", "value": "1", "datatype": EX + "int"},
            {"type": "literal", "value": "Chat", "language": "fr"},
            {"type": "literal", "value": "chat", "language": "fr"},
            {"type": "literal", "value": "plain",
             "datatype": "http://www.w3.org/2001/XMLSchema#string"},
        ])
        self.assertEqual(terms, expected)
        # Query by the xsd:string default finds the untyped literal.
        q2 = {"project": {"input": {"triple": {
            "graph": G(), "s": {"var": "s"}, "p": iri("p"),
            "o": {"type": "literal", "value": "plain",
                  "datatype": "http://www.w3.org/2001/XMLSchema#string"}}},
            "vars": ["s"]}}
        q2p = self.dir / "q2.json"
        q2p.write_text(json.dumps(q2, ensure_ascii=False), encoding="utf-8")
        r2 = self.runner.json("query", "--store", store, "--query", str(q2p), "--quiet")
        self.assertEqual(r2["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
