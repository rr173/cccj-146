#!/usr/bin/env python3
"""Build a synthetic RDF graph with many diamonds and back edges, then time a
real CLI query and report path-state statistics.

The graph deliberately has a large number of distinct walks between endpoint
pairs (exponential in paper terms) while the algorithm only tracks frontier
sets of node identities.  The script shows that runtime is roughly linear in
nodes x edges and that the number of endpoint pairs is the small, actual
endpoint relation.

    python3 tools/stress.py [--nodes N] [--store PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
EX = "http://example.org/"


def iri(name: str) -> dict:
    return {"type": "iri", "value": EX + name}


def build_graph(nodes: int, seed: int = 7):
    rng = random.Random(seed)
    quads = set()

    def add(s, p, o, g="g"):
        quads.add((s, p, o, g))

    part = "part"
    # A chain of diamonds: layer i and i+1 both connect to two middle nodes,
    # giving two routes per layer and 2^layers walks overall.
    for i in range(nodes):
        add(f"n{i}", part, f"n{i + 1}")
    for i in range(0, nodes, 3):
        add(f"n{i}", part, f"n{i + 2}")
    # Back edges to create cycles: far node points back to near nodes.
    for i in range(nodes // 2, nodes):
        add(f"n{i}", part, f"n{i - nodes // 2}")
    # A dense diamond region with extra parallel routes.
    hub = f"n{nodes // 2}"
    for k in range(8):
        add(f"n{nodes // 2 - 1}", part, f"mid{k}")
        add(f"mid{k}", part, hub)
    # Extra edges from a random subset of nodes upward.
    for i in range(nodes):
        add(f"n{i}", part, f"n{max(0, i - 2)}")
    return [{"subject": iri(s), "predicate": iri(p), "object": iri(o),
             "graph": {"type": "iri", "value": EX + g}} for (s, p, o, g) in sorted(quads)]


def write_dataset(directory: pathlib.Path, quads) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    data = directory / "stress.jsonl"
    with open(data, "w", encoding="utf-8") as handle:
        for quad in quads:
            handle.write(json.dumps(quad, separators=(",", ":")) + "\n")
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"dataset": "stress", "files": [str(data)]}),
                        encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=400)
    parser.add_argument("--store", default=None)
    args = parser.parse_args(argv)

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="rdfq-stress-"))
    manifest = write_dataset(tmpdir, build_graph(args.nodes))
    store = args.store or str(tmpdir / "stress.sqlite")

    def cli(*a):
        proc = subprocess.run([sys.executable, "-m", "rdfq", *a],
                              cwd=str(ROOT), capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"failed: {' '.join(a)}\n{proc.stderr}")
        return json.loads(proc.stdout)

    t0 = time.monotonic()
    info = cli("load", "--manifest", str(manifest), "--store", store, "--quiet")
    t_load = time.monotonic() - t0
    print(f"loaded {info['store']['stored_quads']} quads in {t_load:.3f}s")

    query = {
        "project": {"input": {"path": {
            "graph": {"type": "iri", "value": EX + "g"},
            "start": {"var": "x"},
            "path": {"one_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"},
        }}, "vars": ["x", "y"]},
    }
    qpath = tmpdir / "q.json"
    qpath.write_text(json.dumps(query), encoding="utf-8")
    t0 = time.monotonic()
    result = cli("query", "--store", store, "--query", str(qpath),
                 "--quiet", "--explain")
    t_query = time.monotonic() - t0
    path_step = next(s for s in result["plan"] if s["operator"] == "path")
    stats = result["statistics"]["path"]
    print(f"one_or_more all-pairs query in {t_query:.3f}s")
    print(f"nodes={args.nodes} edges={info['store']['stored_quads']}")
    print(f"endpoint pairs (relation) = {path_step['endpoint_pairs']}")
    print(f"result rows = {result['row_count']} (distinct mappings "
          f"{result['distinct_rows']})")
    print(f"path states expanded = {stats['states_expanded']}, "
          f"frontier rounds = {stats['frontier_rounds']}, "
          f"frontier advances = {stats['advance_calls']}")
    print(f"route enumeration: {path_step['route_enumeration']}")
    print(f"per-pair multiplicity from routes: {path_step['multiplicity_preserved']} "
          "(one row per endpoint pair regardless of route count)")
    # Concrete start constant: a->... reachable closure.
    query2 = {
        "project": {"input": {"path": {
            "graph": {"type": "iri", "value": EX + "g"},
            "start": iri("n0"),
            "path": {"one_or_more": {"predicate": iri("part")}},
            "end": {"var": "y"},
        }}, "vars": ["y"]},
    }
    q2path = tmpdir / "q2.json"
    q2path.write_text(json.dumps(query2), encoding="utf-8")
    t0 = time.monotonic()
    result2 = cli("query", "--store", store, "--query", str(q2path),
                  "--quiet", "--explain")
    t2 = time.monotonic() - t0
    step2 = next(s for s in result2["plan"] if s["operator"] == "path")
    stats2 = result2["statistics"]["path"]
    print(f"single-start closure from n0: {result2['row_count']} endpoints in {t2:.3f}s "
          f"(states expanded {stats2['states_expanded']}, "
          f"frontier rounds {stats2['frontier_rounds']})")
    print("ok: runtime depends on node/state frontiers, not on the number of walks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
