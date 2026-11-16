"""RDF terms and the public JSON term protocol.

A term is a plain dict::

    {"type": "iri", "value": "http://example.org/a"}
    {"type": "bnode", "label": "b0"}
    {"type": "literal", "value": "x", "datatype": "http://www.w3.org/2001/XMLSchema#string"}
    {"type": "literal", "value": "chat", "language": "fr"}

Rules enforced here (validated only, no RDF inference, no normalisation):

* IRIs must be absolute (a URI scheme plus a non-space remainder) and must
  contain only legal Unicode (no lone surrogates, no control characters).
* ``subject`` may be an IRI or a blank node, ``predicate`` an IRI, ``object``
  any term.
* A literal with ``language`` is normalised to lower case and carries the
  datatype ``rdf:langString``.  A literal with neither ``datatype`` nor
  ``language`` gets ``xsd:string``.  Lexical forms are stored verbatim:
  ``"01"^^xsd:integer`` and ``"1"^^xsd:integer`` are different terms.

The canonical JSON encoding (:func:`term_key`) is what is stored in SQLite
and is the identity used by ``same_term``.  Blank-node labels are only unique
inside one source file; :func:`term_key` takes a *scope* (the file id) for
them so that equal labels in different files never merge.
"""

from __future__ import annotations

import json
import re

XSD = "http://www.w3.org/2001/XMLSchema#"
XSD_STRING = XSD + "string"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_LANGSTRING = RDF + "langString"

IRI = "iri"
BNODE = "bnode"
LITERAL = "literal"

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_LANG_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)*$")


class TermError(ValueError):
    """Raised for a structurally invalid or illegal term."""


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _check_text(text, where: str) -> str:
    if not isinstance(text, str):
        raise TermError(f"{where}: expected a JSON string, got {type(text).__name__}")
    if _has_lone_surrogate(text):
        raise TermError(f"{where}: illegal Unicode (lone surrogate)")
    return text


def validate_iri(value, where: str = "iri") -> str:
    value = _check_text(value, where)
    if not _SCHEME_RE.match(value):
        raise TermError(f"{where}: not an absolute IRI: {value!r}")
    if _CTRL_RE.search(value) or any(ch.isspace() for ch in value):
        raise TermError(f"{where}: IRI contains whitespace or control characters: {value!r}")
    return value


def make_iri(value, where: str = "iri") -> dict:
    return {"type": IRI, "value": validate_iri(value, where)}


def make_bnode(label, where: str = "bnode") -> dict:
    label = _check_text(label, where)
    if not label:
        raise TermError(f"{where}: blank node label must not be empty")
    return {"type": BNODE, "label": label}


def make_literal(value, datatype=None, language=None, where: str = "literal") -> dict:
    value = _check_text(value, where)
    if datatype is not None and language is not None:
        raise TermError(f"{where}: literal cannot have both datatype and language")
    if language is not None:
        if not isinstance(language, str):
            raise TermError(f"{where}: language must be a string")
        lang = language.lower()
        if not _LANG_RE.match(lang):
            raise TermError(f"{where}: invalid language tag: {language!r}")
        return {"type": LITERAL, "value": value, "language": lang}
    if datatype is not None:
        validate_iri(datatype, where + ".datatype")
        return {"type": LITERAL, "value": value, "datatype": datatype}
    return {"type": LITERAL, "value": value, "datatype": XSD_STRING}


def term_key(term: dict, scope: str = "") -> str:
    """Canonical identity of a term.

    ``scope`` distinguishes source files for blank-node labels.  Stored
    quadruples always use the file id as scope, query terms use ``""``.
    """
    kind = term["type"]
    if kind == IRI:
        return "I:" + term["value"]
    if kind == BNODE:
        return "B:" + scope + "\x1f" + term["label"]
    if kind == LITERAL:
        # JSON array keeps lexical form, datatype and language unambiguous.
        lang = term.get("language", "")
        dtype = "" if lang else term.get("datatype", XSD_STRING)
        return "L:" + json.dumps([term["value"], dtype, lang], ensure_ascii=False)
    raise TermError(f"unknown term type: {kind!r}")


def key_to_term(key: str) -> dict:
    """Inverse of :func:`term_key` for query output.

    Blank nodes use their physical ``B:<id>`` identity, which is unique across
    files and stable for the lifetime of the store.
    """
    kind, rest = key[0], key[2:]
    if kind == "I":
        return {"type": IRI, "value": rest}
    if kind == "B":
        return {"type": BNODE, "label": rest}
    if kind == "L":
        value, dtype, lang = json.loads(rest)
        if lang:
            return {"type": LITERAL, "value": value, "language": lang}
        return {"type": LITERAL, "value": value, "datatype": dtype}
    raise TermError(f"bad stored term key: {key!r}")


def bnode_id(file_id: str, label: str) -> str:
    """Physical blank-node identity: stable across re-opens of the same store."""
    return file_id + "\x1f" + label


def parse_term(raw, where: str = "term", *, allow_variable: bool = False) -> dict:
    """Validate a public JSON term and return its normalised form."""
    if not isinstance(raw, dict):
        raise TermError(f"{where}: term must be a JSON object")
    kind = raw.get("type")
    if kind == IRI:
        if set(raw) - {"type", "value"}:
            raise TermError(f"{where}: unknown fields for iri term: {sorted(set(raw) - {'type', 'value'})}")
        return make_iri(raw.get("value"), where)
    if kind == BNODE:
        if set(raw) - {"type", "label"}:
            raise TermError(f"{where}: unknown fields for bnode term: {sorted(set(raw) - {'type', 'label'})}")
        return make_bnode(raw.get("label"), where)
    if kind == LITERAL:
        allowed = {"type", "value", "datatype", "language"}
        extra = set(raw) - allowed
        if extra:
            raise TermError(f"{where}: unknown fields for literal term: {sorted(extra)}")
        if "value" not in raw:
            raise TermError(f"{where}: literal is missing 'value'")
        return make_literal(raw.get("value"), raw.get("datatype"), raw.get("language"), where)
    raise TermError(f"{where}: unknown term type {kind!r} (expected iri, bnode or literal)")


def parse_quad(raw, line_no: int) -> dict:
    """Validate one JSONL quad. ``graph`` is ``"default"`` or an IRI term."""
    where = f"line {line_no}"
    if not isinstance(raw, dict):
        raise TermError(f"{where}: each line must be a JSON object")
    expected = {"subject", "predicate", "object", "graph"}
    missing = expected - set(raw)
    if missing:
        raise TermError(f"{where}: missing fields {sorted(missing)}")
    extra = set(raw) - expected
    if extra:
        raise TermError(f"{where}: unknown fields {sorted(extra)}")
    subj = parse_term(raw["subject"], where + ".subject")
    pred = parse_term(raw["predicate"], where + ".predicate")
    obj = parse_term(raw["object"], where + ".object")
    if subj["type"] not in (IRI, BNODE):
        raise TermError(f"{where}.subject: must be an IRI or blank node, not a literal")
    if pred["type"] != IRI:
        raise TermError(f"{where}.predicate: must be an IRI")
    graph = raw["graph"]
    if graph == "default":
        graph_term = None
    else:
        graph_term = parse_term(graph, where + ".graph")
        if graph_term["type"] != IRI:
            raise TermError(f"{where}.graph: must be \"default\" or an IRI")
    return {"subject": subj, "predicate": pred, "object": obj, "graph": graph_term}
