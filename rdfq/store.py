"""SQLite storage: dataset loading, deduplication and read-only access.

The on-disk identity of a term is a canonical key (see :func:`rdfq.terms.term_key`):

* IRI                 ``I:<iri>``
* literal             ``L:<json [lexical, datatype, language]>``
* blank node          ``B:<integer id>``

Blank nodes get an integer id per (source file, label).  The id is persisted,
so identities are stable when the store is re-opened, and labels from two
different source files never collapse into one node.

Quads are unique on the full tuple ``(subject, predicate, object, graph)``.
The default graph is stored as graph key ``"D"`` and each named graph as
``"G:<iri>"``; they are never implicitly merged.

Loading is atomic: files are parsed and validated completely in memory, then
one SQLite transaction inserts everything.  Any failure rolls back, leaving an
existing store untouched.  Loading over an existing store additionally builds
a fresh temporary database and atomically replaces the old file only after a
successful commit and integrity check.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import urllib.parse

from . import terms

SCHEMA = """
CREATE TABLE dataset (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE TABLE source_file (
    file_id      TEXT PRIMARY KEY,
    source_name  TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    line_count   INTEGER NOT NULL,
    quad_count   INTEGER NOT NULL
);
CREATE TABLE bnode (
    bid       INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_file(file_id),
    label     TEXT NOT NULL,
    UNIQUE (source_id, label)
);
CREATE TABLE quad (
    qid        INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    predicate  TEXT NOT NULL,
    object     TEXT NOT NULL,
    graph_key  TEXT NOT NULL,
    graph_label TEXT NOT NULL,
    source_id  TEXT NOT NULL REFERENCES source_file(file_id),
    UNIQUE (subject, predicate, object, graph_key)
);
CREATE INDEX quad_spo ON quad (graph_key, predicate, subject, object);
CREATE INDEX quad_pos ON quad (graph_key, predicate, object, subject);
CREATE INDEX quad_s ON quad (graph_key, subject);
CREATE INDEX quad_o ON quad (graph_key, object);
"""


class StoreError(RuntimeError):
    """Raised for store-level failures (missing store, bad manifest, ...)."""


class ManifestError(StoreError):
    pass


DEFAULT_GRAPH = "default"


def graph_storage(graph: dict | None) -> tuple[str, str]:
    if graph is None:
        return "D", DEFAULT_GRAPH
    return "G:" + graph["value"], graph["value"]


def read_manifest(manifest_path) -> dict:
    path = pathlib.Path(manifest_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    extra = set(raw) - {"dataset", "files"}
    if extra:
        raise ManifestError(f"manifest has unknown fields {sorted(extra)}")
    name = raw.get("dataset", path.stem)
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("manifest 'dataset' must be a non-empty string")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("manifest 'files' must be a non-empty list")
    resolved = []
    seen = set()
    base = path.resolve().parent
    for i, entry in enumerate(files):
        if not isinstance(entry, str) or not entry:
            raise ManifestError(f"files[{i}]: expected a non-empty path string")
        p = pathlib.Path(entry)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        if not p.is_file():
            raise ManifestError(f"files[{i}]: file not found: {p}")
        if p in seen:
            raise ManifestError(f"files[{i}]: duplicate file path {p}")
        seen.add(p)
        resolved.append(p)
    return {"dataset": name, "files": resolved, "manifest_path": str(path.resolve())}


def parse_sources(files: list[pathlib.Path]) -> dict:
    """Parse and validate every source file, without touching the database."""
    parsed_files = []
    for file_index, path in enumerate(files):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoreError(f"{path}: not valid UTF-8: {exc}")
        sha = hashlib.sha256(data).hexdigest()
        file_id = f"f{file_index}"
        bnode_ids: dict[str, int] = {}
        records = []
        line_count = 0
        if text and not text.endswith("\n"):
            text += "\n"
        for line_no, line in enumerate(text.splitlines(), start=1):
            line_count = line_no
            if not line.strip():
                raise StoreError(f"{path}:{line_no}: empty line in JSONL input")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path}:{line_no}: invalid JSON: {exc}")
            try:
                quad = terms.parse_quad(raw, line_no)
            except terms.TermError as exc:
                raise StoreError(f"{path}:{exc}")
            keys = {}
            for role in ("subject", "predicate", "object"):
                term = quad[role]
                if term["type"] == terms.BNODE:
                    label = term["label"]
                    if label not in bnode_ids:
                        bnode_ids[label] = len(bnode_ids)
                    key = "B:" + str(file_index) + "\x1f" + label
                else:
                    key = terms.term_key(term, scope=file_id)
                keys[role] = key
            gkey, glabel = graph_storage(quad["graph"])
            records.append((keys["subject"], keys["predicate"], keys["object"],
                            gkey, glabel, file_id))
        parsed_files.append({
            "path": path,
            "file_id": file_id,
            "sha256": sha,
            "line_count": line_count,
            "bnode_labels": list(bnode_ids),
            "records": records,
        })
    return {"files": parsed_files}


def load_store(store_path, manifest_path, *, replace: bool = False) -> dict:
    """Load the manifest's files into ``store_path`` atomically."""
    store_path = pathlib.Path(store_path)
    if store_path.exists() and not replace:
        raise StoreError(f"store already exists: {store_path} (use --replace to rebuild)")
    manifest = read_manifest(manifest_path)
    parsed = parse_sources(manifest["files"])

    store_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=store_path.name + ".", suffix=".tmp",
                                    dir=str(store_path.parent))
    os.close(fd)
    tmp_path = pathlib.Path(tmp_name)
    # mkstemp creates 0600; give the published store normal file permissions.
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp_path, 0o666 & ~umask)

    try:
        conn = sqlite3.connect(tmp_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO dataset (id, name, created_utc) VALUES (1, ?, ?)",
                (manifest["dataset"], _utc_now()))
            # Blank nodes need their persisted ids while records are inserted;
            # ids are assigned in manifest/file/label order.
            bnode_lookup: dict[tuple[int, str], int] = {}
            next_bid = 1
            inserted = 0
            duplicates = 0
            total_quads = 0
            for file_index, pf in enumerate(parsed["files"]):
                conn.execute(
                    "INSERT INTO source_file (file_id, source_name, sha256, line_count, quad_count)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (pf["file_id"], str(pf["path"]), pf["sha256"],
                     pf["line_count"], len(pf["records"])))
                for label in pf["bnode_labels"]:
                    conn.execute(
                        "INSERT INTO bnode (bid, source_id, label) VALUES (?, ?, ?)",
                        (next_bid, pf["file_id"], label))
                    bnode_lookup[(file_index, label)] = next_bid
                    next_bid += 1
                for rec in pf["records"]:
                    subj, pred, obj, gkey, glabel, source_id = rec
                    total_quads += 1
                    subj = _physical_bnode_key(subj, bnode_lookup)
                    obj = _physical_bnode_key(obj, bnode_lookup)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO quad"
                        " (subject, predicate, object, graph_key, graph_label, source_id)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (subj, pred, obj, gkey, glabel, source_id))
                    if cur.rowcount:
                        inserted += 1
                    else:
                        duplicates += 1
            conn.execute("COMMIT")
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise StoreError(f"SQLite integrity check failed: {check}")
        finally:
            conn.close()
        # Atomic replacement: the previous store survives any failure above.
        os.replace(tmp_path, store_path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    return {
        "store": str(store_path),
        "dataset": manifest["dataset"],
        "files": len(parsed["files"]),
        "input_quads": total_quads,
        "stored_quads": inserted,
        "duplicates": duplicates,
    }


def _physical_bnode_key(key: str, lookup: dict[tuple[int, str], int]) -> str:
    if not key.startswith("B:"):
        return key
    file_index_text, label = key[2:].split("\x1f", 1)
    bid = lookup[(int(file_index_text), label)]
    return "B:b" + str(bid)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_readonly(store_path):
    """Open an existing store read-only with a consistent read snapshot."""
    path = pathlib.Path(store_path)
    if not path.is_file():
        raise StoreError(f"store not found: {path}")
    # immutable=1 lets readers skip locking; the loader only ever publishes a
    # fully committed store via atomic rename, so readers never see a
    # half-written file and need no live lock coordination.
    uri = "file:" + urllib.parse.quote(str(path.resolve())) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise StoreError(f"cannot open store {path}: {exc}")
    conn.execute("PRAGMA query_only = ON")
    try:
        # A single read transaction pins the view for the whole query.
        conn.execute("BEGIN")
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dataset'").fetchone()
        conn.execute("SELECT id FROM dataset").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise StoreError(f"not a queryable rdfq store: {path}: {exc}")
    return conn


def dataset_info(conn) -> dict:
    row = conn.execute("SELECT name, created_utc FROM dataset").fetchone()
    quads = conn.execute("SELECT COUNT(*) FROM quad").fetchone()[0]
    files = conn.execute(
        "SELECT source_name, line_count, quad_count FROM source_file ORDER BY file_id").fetchall()
    graphs = conn.execute(
        "SELECT graph_label, COUNT(*) FROM quad GROUP BY graph_key ORDER BY graph_label").fetchall()
    return {
        "dataset": row[0],
        "created_utc": row[1],
        "stored_quads": quads,
        "source_files": [{"path": f[0], "lines": f[1], "quads": f[2]} for f in files],
        "graphs": [{"graph": g[0], "quads": g[1]} for g in graphs],
    }
