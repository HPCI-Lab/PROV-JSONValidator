#!/usr/bin/env python3
"""
provjson_validator.py -- Validate W3C PROV-JSON documents.

Checks the structural rules of the W3C PROV-JSON submission
(https://www.w3.org/Submission/prov-json/) plus cross-reference,
prefix and datatype checks, and reports every problem it finds.

Both PROV-JSON naming conventions for time attributes are accepted:
  prov:startTime/prov:endTime (PROV-JSON note) and
  prov:startedAtTime/prov:endedAtTime (PROV-O property names), as well as
  prov:generatedAtTime/prov:invalidatedAtTime on entity records.
Unknown 'prov:*' attributes are reported as warnings by default
(use --strict to make them errors), since producers may emit other
legitimate PROV-O terms.

Usage:
    python provjson_validator.py FILE [FILE ...] [--json] [--no-warnings] [--strict]

Exit codes: 0 = all files valid, 1 = validation errors found,
            2 = could not read/parse input.
"""

import argparse
import json
import re
import sys
from datetime import datetime

SPEC_URL = "https://www.w3.org/Submission/prov-json/"

# ---------------------------------------------------------------------------
# PROV-JSON schema knowledge
# ---------------------------------------------------------------------------

NODE_KINDS = ("entity", "activity", "agent", "collection", "bundle")

# Reserved attributes allowed on node records.  Time attributes appear in
# both the PROV-JSON note spelling and the PROV-O property spelling.
NODE_ATTRS = {
    "entity":     {"prov:label", "prov:type", "prov:location", "prov:value",
                   "prov:role",
                   "prov:generatedAtTime", "prov:invalidatedAtTime"},
    "activity":   {"prov:label", "prov:type", "prov:location",
                   "prov:startTime", "prov:endTime",
                   "prov:startedAtTime", "prov:endedAtTime"},
    "agent":      {"prov:label", "prov:type", "prov:location", "prov:role"},
    "collection": {"prov:label", "prov:type", "prov:location", "prov:value",
                   "prov:role",
                   "prov:generatedAtTime", "prov:invalidatedAtTime"},
    "bundle":     set(),
}

DATETIME_ATTRS = {"prov:startTime", "prov:endTime",
                  "prov:startedAtTime", "prov:endedAtTime",
                  "prov:generatedAtTime", "prov:invalidatedAtTime",
                  "prov:time"}

# Attributes allowed on any relation record (per the note's examples).
COMMON_REL_ATTRS = {"prov:type", "prov:label", "prov:role"}

# relation name -> key field (subject role), required fields, optional fields.
# kind "any" means: any identifier, existence is not verifiable (e.g. fields
# referring to other relations, or blank-node-keyed relations).
RELATIONS = {
    "used":              {"key": ("prov:activity", "activity"),
                          "req": {"prov:entity": "entity"},
                          "opt": {"prov:time": "datetime"}},
    "wasGeneratedBy":    {"key": ("prov:entity", "entity"),
                          "req": {"prov:activity": "activity"},
                          "opt": {"prov:time": "datetime"}},
    "wasInvalidatedBy":  {"key": ("prov:entity", "entity"),
                          "req": {"prov:activity": "activity"},
                          "opt": {"prov:time": "datetime"}},
    "wasDerivedFrom":    {"key": ("prov:generatedEntity", "entity"),
                          "req": {"prov:usedEntity": "entity"},
                          "opt": {"prov:type": "qname",
                                  "prov:activity": "activity",
                                  "prov:generation": "any",
                                  "prov:usage": "any"}},
    "wasAttributedTo":   {"key": ("prov:entity", "entity"),
                          "req": {"prov:agent": "agent"},
                          "opt": {}},
    "wasAssociatedWith": {"key": ("prov:activity", "activity"),
                          "req": {},
                          "opt": {"prov:agent": "agent",
                                  "prov:plan": "entity"}},
    "actedOnBehalfOf":   {"key": ("prov:delegate", "agent"),
                          "req": {"prov:responsible": "agent"},
                          "opt": {"prov:activity": "activity"}},
    "wasInformedBy":     {"key": ("prov:informed", "activity"),
                          "req": {"prov:informant": "activity"},
                          "opt": {}},
    "wasInfluencedBy":   {"key": ("prov:influencee", "any"),
                          "req": {"prov:influencer": "any"},
                          "opt": {}},
    "wasStartedBy":      {"key": ("prov:activity", "activity"),
                          "req": {"prov:trigger": "entity"},
                          "opt": {"prov:starter": "activity",
                                  "prov:time": "datetime"}},
    "wasEndedBy":        {"key": ("prov:activity", "activity"),
                          "req": {"prov:trigger": "entity"},
                          "opt": {"prov:ender": "activity",
                                  "prov:time": "datetime"}},
    "specializationOf":  {"key": ("prov:specificEntity", "entity"),
                          "req": {"prov:generalEntity": "entity"},
                          "opt": {}},
    "alternateOf":       {"key": ("prov:alternate1", "entity"),
                          "req": {"prov:alternate2": "entity"},
                          "opt": {}},
    "hadMember":         {"key": ("prov:collection", "collection"),
                          "req": {"prov:entity": "entity"},
                          "opt": {}},
    "mentionOf":         {"key": ("prov:specificEntity", "entity"),
                          "req": {"prov:generalEntity": "entity"},
                          "opt": {"prov:bundle": "bundle"}},
    "wasQuotedFrom":     {"key": ("prov:generatedEntity", "entity"),
                          "req": {"prov:usedEntity": "entity"},
                          "opt": {}},
    "hadPrimarySource":  {"key": ("prov:generatedEntity", "entity"),
                          "req": {"prov:usedEntity": "entity"},
                          "opt": {}},
    "wasRevisionOf":     {"key": ("prov:generatedEntity", "entity"),
                          "req": {"prov:usedEntity": "entity"},
                          "opt": {}},
}

KNOWN_TOP_KEYS = {"prefix"} | set(NODE_KINDS) | set(RELATIONS)

BUILTIN_PREFIXES = {"prov", "xsd"}
QNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*:[A-Za-z_][A-Za-z0-9_.-]*$")
PREFIX_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

# In shorthand form ("used": {"ex:a1": "ex:e1"}) the bare identifier stands
# for the single required object field of the relation.
SHORTHAND_FIELD = {rel: next(iter(spec["req"])) for rel, spec in RELATIONS.items()
                   if spec["req"]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def render_path(segments):
    return "root" + "".join(f'["{s}"]' for s in segments)


def is_datetime(value):
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


class Validator:
    def __init__(self, strict=False):
        self.strict = strict
        self.errors = []    # list of (path, message)
        self.warnings = []

    def err(self, path, msg):
        self.errors.append((render_path(path), msg))

    def warn(self, path, msg):
        self.warnings.append((render_path(path), msg))

    def unknown_attr(self, path, msg):
        if self.strict:
            self.err(path, msg)
        else:
            self.warn(path, msg + " (not defined in the PROV-JSON note; "
                                  "use --strict to reject)")

    # -- small value checks ---------------------------------------------------

    def check_qname_prefix(self, value, prefixes, path):
        if not isinstance(value, str) or value.startswith("_:"):
            return  # blank node identifiers are not qualified names
        if QNAME_RE.match(value):
            prefix = value.split(":", 1)[0]
            if prefix not in prefixes and prefix not in BUILTIN_PREFIXES:
                self.warn(path, f"prefix '{prefix}' is not declared "
                                f"in the 'prefix' map")

    def check_id(self, value, prefixes, path):
        if not isinstance(value, str) or not value:
            self.err(path, f"identifier must be a non-empty string, "
                           f"got {value!r}")
            return
        self.check_qname_prefix(value, prefixes, path)

    def check_kind(self, ident, expected, index, path):
        if expected == "any":
            if ident and not ident.startswith("_:") and ident not in index:
                self.warn(path, f"identifier '{ident}' does not exist in "
                                f"this bundle")
            return
        actual = index.get(ident)
        if actual is None:
            self.err(path, f"referenced identifier '{ident}' does not exist "
                           f"in this bundle")
            return
        compatible = {
            "entity":     {"entity", "collection"},
            "collection": {"collection"},
            "activity":   {"activity"},
            "agent":      {"agent"},
            "bundle":     {"bundle"},
        }
        if actual not in compatible.get(expected, {expected}):
            self.err(path, f"'{ident}' is a {actual}, but a {expected} is "
                           f"required here")

    def check_datetime_value(self, value, path):
        if not is_datetime(value):
            self.err(path, f"expected an ISO-8601 datetime string, "
                           f"got {value!r}")

    def check_attr_value(self, attr, value, prefixes, path):
        if attr in DATETIME_ATTRS:
            self.check_datetime_value(value, path)
        elif attr == "prov:label":
            if not isinstance(value, str):
                self.err(path, f"prov:label must be a string, got {value!r}")
        elif attr == "prov:type":
            vals = value if isinstance(value, list) else [value]
            for v in vals:
                if isinstance(v, str):
                    self.check_qname_prefix(v, prefixes, path)
                elif not (isinstance(v, dict) and "$" in v):
                    self.err(path, "prov:type values must be QName strings "
                                   "or {'$': ..., 'type': ...} structures, "
                                   f"got {v!r}")
        elif attr == "prov:role":
            if isinstance(value, str):
                self.check_qname_prefix(value, prefixes, path)

    # -- node records ----------------------------------------------------------

    def validate_node_record(self, kind, rec_id, record, prefixes, path):
        if not isinstance(record, dict):
            self.err(path, f"record for '{rec_id}' must be a JSON object of "
                           f"attributes, got {type(record).__name__}")
            return
        allowed = NODE_ATTRS[kind]
        for attr, value in record.items():
            apath = path + [attr]
            if attr.startswith("prov:") and attr not in allowed:
                self.unknown_attr(apath, f"unknown PROV attribute '{attr}' "
                                         f"for a {kind} record")
                continue
            if attr.startswith("prov:"):
                self.check_attr_value(attr, value, prefixes, apath)

    # -- relations --------------------------------------------------------------

    def validate_relation_record(self, rel, record, prefixes, index, path):
        spec = RELATIONS[rel]
        key_field, _ = spec["key"]
        fields = dict(spec["req"])
        fields.update(spec["opt"])

        if isinstance(record, str):
            # shorthand: bare identifier for the object role
            obj_field = SHORTHAND_FIELD[rel]
            self.check_id(record, prefixes, path)
            self.check_kind(record, spec["req"][obj_field], index, path)
            return
        if not isinstance(record, dict):
            self.err(path, f"relation record must be an object or an "
                           f"identifier string, got {record!r}")
            return

        allowed = set(fields) | {key_field} | COMMON_REL_ATTRS
        for attr in record:
            if attr.startswith("prov:") and attr not in allowed:
                self.unknown_attr(path + [attr],
                                  f"unknown PROV attribute '{attr}' for "
                                  f"relation '{rel}'")

        for attr, expected in fields.items():
            apath = path + [attr]
            if attr in spec["req"] and attr not in record:
                self.err(path, f"missing required attribute '{attr}' for "
                               f"relation '{rel}'")
                continue
            if attr not in record:
                continue
            value = record[attr]
            if expected == "datetime":
                self.check_datetime_value(value, apath)
            else:
                self.check_id(value, prefixes, apath)
                self.check_kind(value, expected, index, apath)

        # validate common reserved attributes present
        for attr in COMMON_REL_ATTRS & set(record):
            self.check_attr_value(attr, record[attr], prefixes, path + [attr])

    # -- bundle -------------------------------------------------------------------

    def validate_bundle(self, bundle, path):
        if not isinstance(bundle, dict):
            self.err(path, "a bundle must be a JSON object")
            return

        for key in bundle:
            if key not in KNOWN_TOP_KEYS:
                self.err(path + [key],
                         f"unknown PROV-JSON key '{key}' (see {SPEC_URL})")

        # prefix map
        prefixes = {}
        if "prefix" in bundle:
            p = bundle["prefix"]
            if not isinstance(p, dict):
                self.err(path + ["prefix"], "'prefix' must be an object "
                                            "mapping prefix names to URIs")
            else:
                for name, uri in p.items():
                    if not PREFIX_NAME_RE.match(name):
                        self.err(path + ["prefix", name],
                                 f"invalid prefix name '{name}'")
                    if not isinstance(uri, str) or not uri:
                        self.err(path + ["prefix", name],
                                 f"namespace for prefix '{name}' must be a "
                                 f"non-empty string")
                    prefixes[name] = uri

        # node records + kind index
        index = {}
        for kind in NODE_KINDS:
            if kind not in bundle:
                continue
            records = bundle[kind]
            if not isinstance(records, dict):
                self.err(path + [kind], f"'{kind}' must be an object mapping "
                                        f"identifiers to records")
                continue
            for rec_id, record in records.items():
                ipath = path + [kind, rec_id]
                self.check_id(rec_id, prefixes, ipath)
                if rec_id in index:
                    self.err(ipath, f"identifier '{rec_id}' is also defined "
                                    f"as a {index[rec_id]}; identifiers must "
                                    f"be unique across node types")
                else:
                    index[rec_id] = kind
                if kind == "bundle":
                    self.validate_bundle(record, ipath)
                else:
                    self.validate_node_record(kind, rec_id, record,
                                              prefixes, ipath)

        # relations
        for rel, spec in RELATIONS.items():
            if rel not in bundle:
                continue
            records = bundle[rel]
            if not isinstance(records, dict):
                self.err(path + [rel], f"'{rel}' must be an object mapping "
                                       f"subject identifiers to records")
                continue
            key_field, key_kind = spec["key"]
            for key_id, value in records.items():
                rpath = path + [rel, key_id]
                self.check_id(key_id, prefixes, rpath)
                # relation keys may be blank node identifiers per the note;
                # only check subject kind for real identifiers
                if not key_id.startswith("_:"):
                    self.check_kind(key_id, key_kind, index, rpath)
                entries = value if isinstance(value, list) else [value]
                for i, entry in enumerate(entries):
                    epath = rpath + [str(i)] if isinstance(value, list) else rpath
                    self.validate_relation_record(rel, entry, prefixes,
                                                  index, epath)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def validate_file(path_str, strict=False):
    try:
        with open(path_str, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return None, None, f"cannot read/parse '{path_str}': {exc}"
    v = Validator(strict=strict)
    v.validate_bundle(doc, [])
    return v.errors, v.warnings, None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Validate W3C PROV-JSON files and report all errors.")
    ap.add_argument("files", nargs="+", help="PROV-JSON file(s) to validate")
    ap.add_argument("--json", action="store_true",
                    help="emit results as JSON instead of text")
    ap.add_argument("--no-warnings", action="store_true",
                    help="do not report warnings, only errors")
    ap.add_argument("--strict", action="store_true",
                    help="treat unknown 'prov:*' attributes as errors "
                         "instead of warnings")
    args = ap.parse_args(argv)

    results = []
    fatal = False
    for f in args.files:
        errors, warnings, read_err = validate_file(f, strict=args.strict)
        if read_err:
            fatal = True
            results.append({"file": f, "fatal": read_err,
                            "errors": [], "warnings": []})
            continue
        if args.no_warnings:
            warnings = []
        results.append({
            "file": f,
            "fatal": None,
            "errors": [{"path": p, "message": m} for p, m in errors],
            "warnings": [{"path": p, "message": m} for p, m in warnings],
        })

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"== {r['file']} ==")
            if r["fatal"]:
                print(f"  FATAL: {r['fatal']}")
                continue
            if not r["errors"] and not r["warnings"]:
                print("  OK - valid PROV-JSON")
                continue
            for e in r["errors"]:
                print(f"  ERROR   {e['path']}: {e['message']}")
            for w in r["warnings"]:
                print(f"  WARNING {w['path']}: {w['message']}")
            print(f"  -> {len(r['errors'])} error(s), "
                  f"{len(r['warnings'])} warning(s)")
        print()

    has_errors = fatal or any(r["errors"] or r["fatal"] for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
