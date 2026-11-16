"""Test harness utilities (plain unittest, no third-party dependencies).

Small in-memory oracle tests live in ``test_small.py`` and never import the
production evaluator; CLI/integration tests build a real store under the
system temp directory.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

EX = "http://example.org/"


def iri(path: str) -> dict:
    return {"type": "iri", "value": EX + path}


def lit(value: str, datatype=None, language=None) -> dict:
    if language is not None:
        return {"type": "literal", "value": value, "language": language}
    if datatype is not None:
        return {"type": "literal", "value": value, "datatype": datatype}
    return {"type": "literal", "value": value}


def bnode(label: str) -> dict:
    return {"type": "bnode", "label": label}


def G(name: str = "g") -> dict:
    return iri(name)


def quad(s, p, o, graph=None) -> dict:
    return {
        "subject": s,
        "predicate": p,
        "object": o,
        "graph": "default" if graph is None else graph,
    }


def write_jsonl(path: pathlib.Path, quads) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for item in quads:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_manifest(path: pathlib.Path, files, dataset="test") -> None:
    payload = {"dataset": dataset, "files": [str(f) for f in files]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


class CliRunner:
    """Runs the real terminal entry point in a fresh subprocess."""

    def __init__(self, root: pathlib.Path | None = None):
        self.root = root or ROOT
        self.tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="rdfq-test-"))

    def close(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def path(self, name: str) -> pathlib.Path:
        return self.tmpdir / name

    def raw(self, *args, cwd=None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.root)
        env["PYTHONHASHSEED"] = "0"
        err_path = self.tmpdir / "stderr.txt"
        with open(err_path, "w", encoding="utf-8") as err:
            proc = subprocess.run(
                [sys.executable, "-m", "rdfq", *args],
                cwd=str(cwd or self.root), env=env,
                stdout=subprocess.PIPE, stderr=err, text=True)
        proc.stderr = err_path.read_text(encoding="utf-8")
        return proc

    def json(self, *args, cwd=None) -> dict:
        proc = self.raw(*args, cwd=cwd)
        if proc.returncode != 0:
            raise AssertionError(
                f"command exited {proc.returncode}: {' '.join(args)}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return json.loads(proc.stdout)

    def load(self, data_dir: pathlib.Path, store: str = "store.sqlite", *extra) -> dict:
        return self.json("load", "--manifest", str(data_dir / "manifest.json"),
                         "--store", str(self.path(store)), *extra)

    def query(self, query, store: str = "store.sqlite", *extra) -> dict:
        if not isinstance(query, (str, pathlib.Path)):
            qpath = self.path("query.json")
            with open(qpath, "w", encoding="utf-8") as handle:
                json.dump(query, handle, ensure_ascii=False)
            query = qpath
        return self.json("query", "--store", str(self.path(store)),
                         "--query", str(query), "--quiet", *extra)

    def explain(self, query, store: str = "store.sqlite") -> dict:
        if not isinstance(query, (str, pathlib.Path)):
            qpath = self.path("query.json")
            with open(qpath, "w", encoding="utf-8") as handle:
                json.dump(query, handle, ensure_ascii=False)
            query = qpath
        return self.json("explain", "--store", str(self.path(store)),
                         "--query", str(query), "--quiet")


def unwrap(value: str) -> str:
    m = re.match(r"^http://example\.org/(.*)$", value)
    return m.group(1) if m else value


def cells_to_labels(result: dict) -> list[list]:
    """Turn result cells into python-ish labels for easy assertions.

    IRI http://example.org/x -> "x"; literal -> ("lit", lexical[, lang]).
    """
    rows = []
    for row in result["rows"]:
        cells = []
        for cell in row["cells"]:
            if not cell["bound"]:
                cells.append(None)
                continue
            term = cell["term"]
            if term["type"] == "iri":
                cells.append(unwrap(term["value"]))
            elif term["type"] == "bnode":
                cells.append(("bnode", term["label"]))
            else:
                if "language" in term:
                    cells.append(("lit", term["value"], term["language"]))
                else:
                    cells.append(("lit", term["value"]))
        rows.extend([cells] * row["multiplicity"])
    return rows


def bag_of(result: dict) -> dict:
    """Bag keyed by tuples of cells (bound-only, order of columns preserved)."""
    out = {}
    for row in result["rows"]:
        cells = []
        for cell in row["cells"]:
            if not cell["bound"]:
                cells.append(None)
                continue
            term = cell["term"]
            if term["type"] == "iri":
                cells.append(("iri", unwrap(term["value"])))
            elif term["type"] == "bnode":
                cells.append(("bnode", term["label"]))
            elif "language" in term:
                cells.append(("lit", term["value"], "", term["language"]))
            else:
                cells.append(("lit", term["value"], term.get("datatype", ""), ""))
        key = tuple(cells)
        out[key] = out.get(key, 0) + row["multiplicity"]
    return out


def groups(result: dict, key_column: str, value_columns: list[str]) -> dict:
    """Group multiplicities of value columns by one key column."""
    index = result["columns"].index(key_column)
    out = {}
    for row in result["rows"]:
        key = row["cells"][index]
        key_value = None if not key["bound"] else key["term"].get("value", key["term"].get("label"))
        cell_key = tuple(
            None if not row["cells"][result["columns"].index(name)]["bound"]
            else json.dumps(row["cells"][result["columns"].index(name)]["term"], sort_keys=True)
            for name in value_columns)
        bucket = out.setdefault(key_value, {})
        bucket[cell_key] = bucket.get(cell_key, 0) + row["multiplicity"]
    return out


# ---------------------------------------------------------------------------
# in-process helpers for small, oracle-driven tests


def build_store(tmpdir, files, store_name="store.sqlite", dataset="test"):
    """Write JSONL files plus manifest and load a real store.

    ``files`` is a list of quad lists; file order defines blank-node scopes.
    Returns the store path.
    """
    from rdfq.store import load_store

    tmpdir = pathlib.Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    if files and isinstance(files[0], dict):
        files = [files]
    names = []
    for i, quads in enumerate(files):
        path = tmpdir / f"source{i}.jsonl"
        write_jsonl(path, quads)
        names.append(path)
    manifest = tmpdir / "manifest.json"
    write_manifest(manifest, names, dataset=dataset)
    store = tmpdir / store_name
    load_store(store, manifest)
    return store


def run_in_process(store, tree, explain=False):
    from rdfq.store import open_readonly
    from rdfq.eval import run_query

    conn = open_readonly(store)
    try:
        return run_query(conn, tree, explain=explain)
    finally:
        conn.close()


def bnode_id_map(store) -> dict:
    """Map opaque stored blank-node labels (``bN``) to oracle identities."""
    from rdfq.store import open_readonly

    conn = open_readonly(store)
    try:
        rows = conn.execute("SELECT bid, source_id, label FROM bnode").fetchall()
    finally:
        conn.close()
    return {f"b{bid}": f"{source}:{label}" for bid, source, label in rows}


def cells_to_oracle_bag(result: dict, store=None) -> dict:
    """Mapping multiset for the oracle, from a production result dict."""
    import oracle

    mapping = bnode_id_map(store) if store is not None else None
    if mapping is not None:
        for row in result["rows"]:
            for cell in row["cells"]:
                if cell["bound"] and cell["term"]["type"] == "bnode":
                    cell["term"]["label"] = mapping.get(cell["term"]["label"],
                                                        cell["term"]["label"])
    return oracle.bag_from_cells(result["rows"], result["columns"])


def query_terms(tree, store):
    return run_in_process(store, tree)


class TempDir:
    def __init__(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="rdfq-small-"))

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
