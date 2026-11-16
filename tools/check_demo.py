#!/usr/bin/env python3
"""Check the demo's stated expectations against the real CLI.

The expected answers live in ``examples/demo/expected.json`` and were written
by hand from the fixture; no production path or evaluator code is imported
here.  Every step shells out to ``python3 -m rdfq`` and only consumes JSON.

Run from the repository root:

    python3 tools/check_demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE = os.path.join(ROOT, "build", "demo-check.sqlite")
QUERIES = os.path.join(ROOT, "examples", "queries")
MANIFEST = os.path.join(ROOT, "examples", "demo", "data", "manifest.json")
EXPECTED = os.path.join(ROOT, "examples", "demo", "expected.json")


def run(*args: str) -> dict:
    proc = subprocess.run([sys.executable, "-m", "rdfq", *args],
                          cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{proc.stderr}")
    return json.loads(proc.stdout)


def cell_label(cell: dict):
    if not cell["bound"]:
        return None
    term = cell["term"]
    if term["type"] == "literal":
        return term["value"]
    return term["value"]


def endpoint_groups(result: dict, x: str, y: str, label: str | None) -> dict:
    """{x: {endpoint: {label_or_unbound: multiplicity}}} from one result."""
    ix, iy = result["columns"].index(x), result["columns"].index(y)
    il = result["columns"].index(label) if label else None
    out: dict = {}
    for row in result["rows"]:
        key_x = cell_label(row["cells"][ix])
        key_y = cell_label(row["cells"][iy])
        if il is not None:
            cell = row["cells"][il]
            key_l = "unbound" if not cell["bound"] else cell_label(cell)
        else:
            key_l = None
        bucket = out.setdefault(key_x, {}).setdefault(key_y, {})
        bucket[key_l] = bucket.get(key_l, 0) + row["multiplicity"]
    return out


def expect(condition: bool, message: str, failures: list) -> None:
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        failures.append(message)


def main() -> int:
    with open(EXPECTED, encoding="utf-8") as handle:
        spec = json.load(handle)
    failures: list[str] = []
    # Only remove this checker's own store, never the README's build/demo.sqlite.
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(STORE + suffix)
        except FileNotFoundError:
            pass
    run("load", "--manifest", MANIFEST, "--store", STORE)

    for check in spec["checks"]:
        result = run("query", "--store", STORE,
                     "--query", os.path.join(QUERIES, check["query"]))
        fact = check["expect"]
        x = fact["x"]
        if "endpoint_multiplicities" in fact:
            groups = endpoint_groups(result, "x", "y", None)
            got = {endpoint: counts[None] for endpoint, counts in groups.get(x, {}).items()}
            expect(got == fact["endpoint_multiplicities"], check["name"], failures)
            if got != fact["endpoint_multiplicities"]:
                print(f"  expected {fact['endpoint_multiplicities']}\n  got      {got}")
        else:
            groups = endpoint_groups(result, "x", "y", "l")
            got = groups.get(x, {})
            expect(got == fact["labels_by_endpoint"], check["name"], failures)
            if got != fact["labels_by_endpoint"]:
                print(f"  expected {fact['labels_by_endpoint']}\n  got      {got}")

    explain_spec = spec["explain_expect"]
    result = run("explain", "--store", STORE,
                 "--query", os.path.join(QUERIES, explain_spec["query"]))
    path_steps = [s for s in result["plan"] if s["operator"] == "path"]
    ok = (len(path_steps) == 1
          and path_steps[0]["graph"] == explain_spec["graph"]
          and path_steps[0]["endpoint_pairs"] == explain_spec["endpoint_pairs"]
          and path_steps[0]["route_enumeration"] is False)
    expect(ok, "explain reports graph scope, endpoint boundary and no route enumeration",
           failures)
    if not ok:
        print(f"  path step: {path_steps}")

    steps = [s["operator"] for s in result["plan"]]
    expect("path" in steps and "project" in steps,
           "plan shows the path and projection steps", failures)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all demo checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
