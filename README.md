# rdfq — local RDF relation query layer

`rdfq` loads RDF quadruples from JSONL into SQLite and answers relation
queries expressed as a public JSON query tree. It focuses on one hard part:
keeping **graph reachability** and **bag semantics of query rows** straight
when diamonds and cycles give the same component many routes, labels go
missing, and the same product arrives through more than one row.

Deliberately out of scope: RDF inference, canonicalisation / isomorphism
comparison, federated or remote queries, and generic SPARQL text parsing.

Everything is standard-library Python 3 (3.10+; tested on 3.11) and SQLite.
No SPARQL or graph engine is used for evaluation: JSON parsing/validation,
path closure and joins are implemented here; SQLite is the store.

---

## 1. Install and run

No dependencies. From the repository root:

```bash
python3 --version                       # 3.10 or newer
python3 -m rdfq --help
python3 -m unittest discover -s tests   # full suite (51 tests)
```

The package is importable as `rdfq` from the repository root.

## 2. Input format

### 2.1 Terms (public JSON term protocol)

| kind | JSON |
|---|---|
| IRI | `{"type": "iri", "value": "http://example.org/a"}` |
| blank node | `{"type": "bnode", "label": "b0"}` |
| literal with datatype | `{"type": "literal", "value": "01", "datatype": "http://www.w3.org/2001/XMLSchema#integer"}` |
| literal with language | `{"type": "literal", "value": "chat", "language": "fr"}` |
| plain literal | `{"type": "literal", "value": "text"}` → `xsd:string` |

Validation performed:

* IRIs must be absolute (a scheme such as `http:` followed by a non-empty
  remainder), must contain no whitespace/control characters, and must be valid
  Unicode (no lone surrogates).
* Blank node labels are non-empty strings that are valid Unicode.
* A literal has **either** `datatype` **or** `language`, never both.
* Language tags are normalised to lower case; the datatype of a language
  literal is `rdf:langString`.
* A literal with neither field gets `xsd:string`.
* **Lexical forms are never normalised.** `"01"^^xsd:integer` and
  `"1"^^xsd:integer` are different terms. No numeric conversion or Unicode
  normalisation/folding is applied.

### 2.2 Quad lines

One JSON object per line, fields exactly `subject`, `predicate`, `object`,
`graph`:

```json
{"subject":{"type":"iri","value":"http://example.org/a"},
 "predicate":{"type":"iri","value":"http://example.org/part"},
 "object":{"type":"iri","value":"http://example.org/b"},
 "graph":{"type":"iri","value":"http://example.org/g"}}
```

* `subject`: IRI or blank node.
* `predicate`: IRI.
* `object`: any term.
* `graph`: the string `"default"`, or an IRI term.

No N-Quads syntax is parsed; input is JSONL only. Blank lines are an error.

### 2.3 Manifest

```json
{"dataset": "demo", "files": ["graph.jsonl"]}
```

Relative paths resolve against the manifest's directory. Order matters: it
defines file ids and therefore blank-node scopes.

## 3. Loading

```bash
python3 -m rdfq load --manifest examples/demo/data/manifest.json \
                     --store build/demo.sqlite
python3 -m rdfq load ... --replace       # atomically rebuild an existing store
python3 -m rdfq info --store build/demo.sqlite
python3 -m rdfq dump --store build/demo.sqlite --graph default
```

Semantics:

* **Deduplication** is on the complete quadruple `(subject, predicate,
  object, graph)`. Terms compare by full RDF term identity.
* **Blank nodes** are scoped per source file: the same label in two files is
  two different nodes; in one file the label is shared across all graphs.
  Internally each `(file, label)` gets a persistent integer id, exposed in
  results as an opaque label such as `{"type":"bnode","label":"b3"}`. The
  identity is stable when the store is re-opened.
* **Graphs are independent.** The default graph and every named graph are
  stored separately and never implicitly merged. Every match node names its
  graph explicitly.
* A failed load **never damages an existing store**: input is validated fully
  in memory, and a replacement store is built in a temporary file and moved
  into place only after a successful commit and `integrity_check`.
* Stores are opened with `mode=ro&immutable=1` and `PRAGMA query_only=ON`; a
  query runs inside one read transaction, so it sees a single consistent view.
  Because the loader publishes via atomic rename, an already-open reader keeps
  seeing the old snapshot when another process rebuilds the store. Source
  JSONL files are never written to during queries.
* Duplicate quad lines are counted and reported: `loaded 7 quads (1 duplicate
  row ignored)`.

## 4. Query tree protocol

A query node is a JSON object with exactly one operator key.

```json
{"triple":  {"graph": GRAPH, "s": T, "p": U, "o": T}}
{"path":    {"graph": GRAPH, "start": T, "path": PATH, "end": T}}
{"join":    {"left": Q, "right": Q}}
{"union_all": {"left": Q, "right": Q}}
{"optional": {"left": Q, "right": Q, "condition": EXPR(optional)}}
{"filter":  {"input": Q, "expr": EXPR}}
{"project": {"input": Q, "vars": ["x", "..."]}}
{"distinct": {"input": Q}}
{"values":  {"vars": ["x", "..."], "rows": [{"x": T} | {}, ...]}}
```

* `GRAPH` is `"default"` or an IRI term. This is part of every match node, so
  a query can never accidentally read across graphs.
* `T` is a variable `{"var": "x"}` or a constant term from §2.1. Variables and
  terms are structurally disjoint: `{"var": ...}` is always a variable.
* Repeating a variable inside one match (e.g. `s` and `o` both `{"var":"x"}`)
  requires the same term.
* `project.vars` may be empty; the projection order is the output column order.
* `values.vars` may be empty (one empty mapping if `rows` is `[{}]`, none if
  `rows` is `[]`). A row may omit declared variables (they stay unbound).
  A row may not bind an undeclared variable.

### 4.1 Paths

```
{"predicate": IRI}
{"inverse": PATH}
{"sequence": [PATH, PATH, ...]}        two or more members
{"alternative": [PATH, PATH, ...]}     two or more members
{"zero_or_more": PATH}
{"one_or_more": PATH}
```

Paths nest arbitrarily. A path node relates its `start` and `end` endpoint
**within its chosen graph**; `start`/`end` can independently be variables or
constants, and may be the **same** variable.

**Endpoint-set contract.** A path node produces one solution per distinct
`(start, end)` pair with a route, regardless of how many routes connect them.
That set is formed first and only then joined with the rest of the query. This
is the project's stated contract and is not claimed to equal every SPARQL
composite-path multiplicity rule. In the demo, `a` `one_or_more` reaches `d`
twice via two different routes, and the result still has exactly one
`(a, d)` row.

* Zero-length (`zero_or_more`) yields `x → x` for every term that actually
  occurs in the selected graph as a subject or object, **including literals**.
  A constant absent from that graph has no zero-length result.
* Cycles and self-loops terminate: traversal is a fixpoint over sets of path
  states (node identities), never over route sequences. Inverse, empty paths
  and alternatives compose correctly because every frontier set is advanced
  through each path fragment; a node is not "used up" by one path state.
* Graph halves never join: each fragment lookup is restricted to the node's
  fixed graph.

### 4.2 Expressions

```json
{"same_term": [T, T]}
{"bound": {"var": "x"}}
{"not": EXPR}
{"and": [EXPR, ...]}       non-empty
{"or":  [EXPR, ...]}       non-empty
```

Three-valued logic:

* `same_term` is **error** if either side is unbound; otherwise it compares
  complete RDF term identity (IRI vs blank node vs literal, lexical form,
  datatype, language).
* `bound` checks binding only; it never errors.
* `not(error)` is error. `and` is false if any operand is false, else error if
  any operand is error, else true. `or` is true if any operand is true, else
  error if any operand is error, else false.
* Only **true** passes a `filter` or an `optional` condition. Error and false
  both fail to pass; they are not the same value, but neither retains a row.

### 4.3 Joins and bags

A solution is a partial mapping from variables to terms. Unbound is not an RDF
literal and is not equal to anything.

* `join`: rows are compatible when every variable bound on **both** sides has
  the same term. Variables bound on one side only are carried over; a variable
  unbound on both stays unbound. Compatible row counts multiply.
* `union_all`: multiplicities add; no de-duplication.
* `project`: drops the other variables, keeps duplicates.
* `distinct`: the only operator that collapses identical partial mappings.
* No intermediate step de-duplicates. `column order` is the explicit project
  order; output marks every cell as bound term or unbound.

### 4.4 optional

`optional` is left outer join. The right tree is evaluated **independently**
(not a correlated subquery). For each left row, every compatible right row is
considered; the added `condition`, if present, is evaluated on the **merged**
mapping and only `true` keeps it. If no right row qualifies, the original left
mapping is kept with its original multiplicity, and right-side variables stay
unbound.

Because the condition is part of the optional, it can only suppress right
matches. A later outer `filter` on a right-side variable can and does delete
the padded unbound rows. The two placements are different and the demo shows
both.

### 4.5 Variable scope and rejection rules

* A `filter` expression may only mention variables produced by its input.
* An `optional` condition may only mention left or right variables.
* `project` may only project variables its input produces.
* An undeclared variable in a `values` row is rejected.
* Unknown operators, malformed paths, and structural errors are rejected
  before evaluation.

Rejected queries exit non-zero and never print a success marker.

## 5. Output format

```json
{
  "ok": true,
  "command": "query",
  "columns": ["x", "y", "l"],
  "columns_source": "project",
  "rows": [
    {"cells": [{"bound": true, "term": {"type":"iri","value":"http://example.org/a"}},
               {"bound": true, "term": {"type":"iri","value":"http://example.org/b"}},
               {"bound": false}],
     "multiplicity": 2}
  ],
  "row_count": 2,
  "distinct_rows": 1
}
```

Each row carries an exact integer multiplicity; `row_count` is the sum. A
caller may also read rows as repeated lines; both views are equivalent.
`columns_source` is `"project"` when an explicit project set the order, else
`sorted_scope`.

`explain` (or `query --explain`) adds `"plan"` and `"statistics"`:

* `path` step: `graph`, `graph_key`, path description, endpoint contract note,
  `route_enumeration: false`, `endpoint_pairs`, output rows.
* `join`/`union_all`/`optional`/`filter`/`project`/`distinct` steps: input and
  output row counts and the multiplicity rule applied.
* `statistics.path`: path nodes, endpoint pairs, path states expanded,
  frontier advances, node membership checks.

## 6. Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 2 | usage error (bad arguments, unreadable query file) |
| 3 | invalid data or query protocol (bad term, unknown operator, scope violation, load failure) |
| 4 | evaluation or store I/O failure |

Errors print `{"ok": false, "command": ..., "error": {"kind": ..., "message": ...}}`
to stderr, followed by `END <command> status=error`. A successful query prints
`END query status=ok` to stderr (suppressible with `--quiet`). Nothing that
fails prints a success marker.

## 7. Demo walkthrough

Fixture: `examples/demo/data/graph.jsonl`.

```
g: a→b→d, a→c→d, d→a   (all part edges)
   b label "保留", c label "其他"
```

```bash
rm -rf build
python3 -m rdfq load --manifest examples/demo/data/manifest.json \
                     --store build/demo.sqlite

# 1) closure from any x: a reaches a,b,c,d exactly once each (d is not doubled)
python3 -m rdfq query --store build/demo.sqlite \
    --query examples/queries/reach_closure.json --quiet

# 2) put a in values twice: each endpoint now appears twice
python3 -m rdfq query --store build/demo.sqlite \
    --query examples/queries/reach_from_a_twice.json --quiet

# 3) optional with a condition on the label: a,c,d survive unlabelled, only b is 保留
python3 -m rdfq query --store build/demo.sqlite \
    --query examples/queries/optional_condition.json --quiet

# 4) same label test outside the optional: only b's rows remain
python3 -m rdfq query --store build/demo.sqlite \
    --query examples/queries/optional_then_outer_filter.json --quiet

# 5) plan: graph scope, endpoint de-duplication boundary, bag multiplicities
python3 -m rdfq explain --store build/demo.sqlite \
    --query examples/queries/reach_closure.json --quiet

# all hand-written expectations in one shot
python3 tools/check_demo.py
```

The hand-written answers are in `examples/demo/expected.json`. They were
derived from the fixture directly, not from the query engine.

Run the whole verification:

```bash
python3 -m unittest discover -s tests          # fixtures + differential + CLI
python3 tools/check_demo.py                    # demo expectations via the CLI
python3 tools/stress.py --nodes 250            # many diamonds/back edges
```

## 8. Test design

* `tests/test_small.py` builds tiny JSONL graphs and checks hand-enumerated
  answers for: path endpoints, zero-length literal nodes, absent constants,
  same-variable endpoints, inverse+sequence nesting, graph separation, bag
  joins, `union_all` + projection, `values` with unbound cells and empty
  tables, optional multiplicities, error/true/false combinations, and term
  identity. No production path or join function is used to compute expected
  answers.
* `tests/test_random.py` generates random quads (two files, blank nodes, two
  named graphs plus the default graph) and random query trees. It compares the
  **complete multiset** of mappings against `tests/oracle.py`, an independent
  implementation that shares no code with `rdfq`: stored quads are plain
  Python tuples, paths are pair sets built by relation algebra and a
  transitive-closure fixpoint, and bags are Python lists of dicts.
* `tests/test_cli.py` drives the real CLI in subprocesses: load/dedup, graph
  partition, blank-node scoping and stable identity, atomic failed loads,
  read-only re-open, explicit column order, unbound markers, and non-zero
  exits with no success marker for bad terms/operators/variables.
* `tools/stress.py` builds a chain of diamonds with back edges. It reports
  endpoint-pair counts against a much larger number of actual walks, plus
  `states_expanded`, `frontier_rounds` and `advance_calls`, showing traversal
  work tracks the endpoint relation and path states rather than route counts.
  A sample run (`--nodes 500`, 1433 edges) finishes the all-pairs closure in
  under 10 seconds with 258,572 endpoint pairs. The single-start closure from
  `n0` visits 509 endpoints in about 0.05 s and expands 509 states over 333
  frontier rounds — the number of walks through the diamonds is exponential in
  500 and is never enumerated.

## 9. Limits

* No RDF inference, no datatype arithmetic, no canonicalisation or
  isomorphism comparison between graphs.
* No remote/federated access and no SPARQL text syntax.
* Path nodes bind at most the two endpoint variables, as in the protocol.
* `same_term` compares term identity; it does not implement SPARQL `=` value
  equality or type promotion.
* Blank-node constants in queries refer to the opaque identities returned by a
  previous query (e.g. `b3`), not to source-file labels.
* The store is one SQLite file, read-only during queries; there is no
  concurrent writer support.
