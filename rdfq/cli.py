"""Terminal entry point for the local RDF query layer.

    python3 -m rdfq load   --manifest data/manifest.json --store build/demo.sqlite [--replace]
    python3 -m rdfq info   --store build/demo.sqlite
    python3 -m rdfq explain --store build/demo.sqlite --query example.json [--skip-validation]
    python3 -m rdfq query  --store build/demo.sqlite --query example.json [--explain]
    python3 -m rdfq dump   --store build/demo.sqlite --graph default

Exit codes: 0 success, 2 usage/protocol error, 3 invalid data or query,
4 evaluation or store failure.  A failed command never prints a success
marker, and a failed load never replaces an existing store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from . import __version__
from . import query as q
from . import store as st
from .eval import EvaluationError, run_query
from .query import QueryError
from .store import StoreError
from .terms import TermError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID = 3
EXIT_FAILURE = 4


class UsageError(Exception):
    pass


def _read_json(path: str, what: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise UsageError(f"{what} file not found: {path}")
    except OSError as exc:
        raise UsageError(f"cannot read {what} file {path}: {exc}")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{what} file {path} is not valid UTF-8: {exc}")
    except json.JSONDecodeError as exc:
        raise UsageError(f"{what} file {path} is not valid JSON: {exc}")


def _write_json_atomic(path: str, payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _result_summary(result: dict) -> dict:
    return {
        "columns": result["columns"],
        "columns_source": result["columns_source"],
        "row_count": result["row_count"],
        "distinct_rows": result["distinct_rows"],
    }


def cmd_load(args) -> int:
    info = st.load_store(args.store, args.manifest, replace=args.replace)
    result = {"ok": True, "command": "load", "store": info}
    _emit(args, result)
    if not args.quiet:
        print(f"loaded {info['stored_quads']} quads ({info['duplicates']} duplicate rows ignored) "
              f"from {info['files']} file(s) into {info['store']}", file=sys.stderr)
        print("END load status=ok", file=sys.stderr)
    return EXIT_OK


def cmd_info(args) -> int:
    conn = st.open_readonly(args.store)
    try:
        info = st.dataset_info(conn)
    finally:
        conn.close()
    result = {"ok": True, "command": "info", "dataset": info}
    _emit(args, result)
    return EXIT_OK


def cmd_query(args, *, explain: bool) -> int:
    tree = _read_json(args.query, "query")
    # Validate before touching the store so bad trees are uniform protocol errors.
    node = q.parse_node(tree)
    q.validate_scopes(node)
    with_explain = bool(getattr(args, "explain", False) or explain)
    conn = st.open_readonly(args.store)
    try:
        result = run_query(conn, tree, explain=with_explain)
    finally:
        conn.close()
    command = "explain" if with_explain else "query"
    payload = {"ok": True, "command": command}
    payload.update(result)
    _emit(args, payload)
    if not args.quiet:
        print(f"END {command} status=ok", file=sys.stderr)
    return EXIT_OK


def cmd_dump(args) -> int:
    conn = st.open_readonly(args.store)
    try:
        if args.graph == "default":
            gkey = "D"
        else:
            gkey = "G:" + args.graph
        rows = conn.execute(
            "SELECT subject, predicate, object FROM quad WHERE graph_key = ?"
            " ORDER BY subject, predicate, object", (gkey,)).fetchall()
        existing = conn.execute(
            "SELECT 1 FROM quad WHERE graph_key = ? LIMIT 1", (gkey,)).fetchone()
    finally:
        conn.close()
    if existing is None:
        raise UsageError(f"graph not found in store: {args.graph}")
    from .terms import key_to_term
    quads = [{"subject": key_to_term(r[0]), "predicate": key_to_term(r[1]),
              "object": key_to_term(r[2]), "graph": "default" if args.graph == "default" else {"type": "iri", "value": args.graph}}
             for r in rows]
    result = {"ok": True, "command": "dump", "graph": args.graph, "quad_count": len(quads),
              "quads": quads}
    _emit(args, result)
    return EXIT_OK


def _emit(args, payload) -> None:
    # Machine output goes to stdout (or --output); human notes go to stderr.
    text = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2)
    if args.output:
        _write_json_atomic(args.output, payload)
        summary = _result_summary(payload) if "rows" in payload else payload
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m rdfq",
        description="Local RDF relation query layer (JSON terms, JSONL quads, SQLite).")
    parser.add_argument("--version", action="version", version="rdfq " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load JSONL files listed in a manifest")
    p_load.add_argument("--manifest", required=True)
    p_load.add_argument("--store", required=True)
    p_load.add_argument("--replace", action="store_true", help="atomically rebuild an existing store")
    p_load.add_argument("--quiet", action="store_true", help="suppress the load summary lines")
    _output_args(p_load)
    p_load.set_defaults(func=cmd_load)

    p_info = sub.add_parser("info", help="show dataset provenance and graph sizes")
    p_info.add_argument("--store", required=True)
    _output_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_query = sub.add_parser("query", help="execute a JSON query tree")
    p_query.add_argument("--store", required=True)
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--explain", action="store_true", help="include plan and path statistics")
    p_query.add_argument("--quiet", action="store_true", help="suppress end-of-query status lines")
    _output_args(p_query)
    p_query.set_defaults(func=lambda a: cmd_query(a, explain=False))

    p_explain = sub.add_parser("explain", help="validate and plan a query without printing answers")
    p_explain.add_argument("--store", required=True)
    p_explain.add_argument("--query", required=True)
    p_explain.add_argument("--quiet", action="store_true", help="suppress end-of-query status lines")
    _output_args(p_explain)
    p_explain.set_defaults(func=lambda a: cmd_query(a, explain=True))

    p_dump = sub.add_parser("dump", help="dump one graph's stored quads")
    p_dump.add_argument("--store", required=True)
    p_dump.add_argument("--graph", default="default")
    _output_args(p_dump)
    p_dump.set_defaults(func=cmd_dump)
    return parser


def _output_args(parser) -> None:
    parser.add_argument("--output", help="write JSON result to this file instead of stdout")
    parser.add_argument("--compact", action="store_true", help="single-line JSON on stdout")


def _error(command: str, kind: str, message: str, code: int) -> int:
    payload = {"ok": False, "command": command, "error": {"kind": kind, "message": message}}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    print(f"END {command} status=error", file=sys.stderr)
    return code


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage/version; keep its exit status.
        return int(exc.code or 0)
    command = args.command
    started = time.monotonic()
    try:
        code = args.func(args)
        if code == EXIT_OK and command in ("query", "explain") and not args.quiet:
            elapsed = time.monotonic() - started
            print(f"query completed in {elapsed:.3f}s", file=sys.stderr)
        return code
    except UsageError as exc:
        return _error(command, "usage", str(exc), EXIT_USAGE)
    except OSError as exc:
        return _error(command, "io", str(exc), EXIT_FAILURE)
    except QueryError as exc:
        return _error(command, "query", str(exc), EXIT_INVALID)
    except TermError as exc:
        return _error(command, "term", str(exc), EXIT_INVALID)
    except st.ManifestError as exc:
        return _error(command, "manifest", str(exc), EXIT_INVALID)
    except StoreError as exc:
        return _error(command, "store", str(exc), EXIT_INVALID)
    except EvaluationError as exc:
        return _error(command, "evaluation", str(exc), EXIT_FAILURE)
    except BrokenPipeError:
        return EXIT_FAILURE
    except Exception as exc:  # never report success on unexpected failure
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"END {command} status=error", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
