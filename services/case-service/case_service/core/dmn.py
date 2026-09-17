"""DMN 1.x decision-table interchange — import and export (market gap B5).

**Why this exists.** The engine has run decision tables since long before this
file: ``rules_evaluator.evaluate_decision_table`` matches a row and returns its
outcomes, and that semantic is correct. What was missing was the *interchange*
format. Camunda and Flowable are DMN-native, so a customer migrating from
either arrives holding ``.dmn`` files, and had no way in.

**Where it lives, and why not in `bpm_importer`.** `market-gaps-secondary.md`
proposed a ninth parser beside the eight in `hxmigrate_service`. Those parsers
produce *case types, forms, SLAs and access groups* — a whole process — and a
decision table is none of those: it is a **rule**, and rules are case-service's.
Putting it there would have meant teaching the five-pass migration pipeline a
result type it has no mapper for, and teaching hxmigrate-service to write rules
it does not own. This sits beside the evaluator that runs the output instead.

**What is deliberately NOT here.**

* **FEEL.** DMN input entries are FEEL expressions, and the platform's
  ``safe_expression`` is not a FEEL engine — it must not be made into one to
  read a spreadsheet. What is supported is the *unary-test subset* real
  decision tables are written in (see ``_parse_unary_test``), and anything
  outside it is **refused by name**, never approximated. A decision table that
  silently answers a slightly different question than the one it was written to
  answer is worse than one that will not import.
* **Three hit policies.** COLLECT, RULE ORDER and OUTPUT ORDER all return
  *several* rows; this engine returns the first match, and there is nowhere in
  the rule shape to put a list. They are refused, not mapped onto FIRST — which
  would produce a table that runs and is wrong.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as _ET_SERIALISE  # export only — never parses input
from dataclasses import dataclass, field
from typing import Any

import defusedxml.ElementTree as ET

#: Same ceiling the BPM importer applies to a single XML file. A decision table
#: is small; anything approaching this is not one.
MAX_DMN_BYTES = 10 * 1024 * 1024

#: Hit policies this platform can honour, and what each means here.
#:
#: * ``UNIQUE``  — at most one row may match. The engine returns the first, and
#:   an overlap is a modelling error DMN says is undefined behaviour; import
#:   keeps the policy on the rule so a linter can say so later.
#: * ``FIRST``   — first matching row in document order. The evaluator sorts by
#:   priority descending, and Python's sort is stable, so equal priorities
#:   preserve document order exactly.
#: * ``ANY``     — every matching row must produce the same output, so the first
#:   match IS the answer.
#: * ``PRIORITY``— the matching row whose output ranks highest. Expressible only
#:   when the ranking is actually written down; see ``_priority_ranking``.
SUPPORTED_HIT_POLICIES = ("UNIQUE", "FIRST", "ANY", "PRIORITY")

#: Refused, with the reason each is refused for.
UNSUPPORTED_HIT_POLICIES = {
    "COLLECT": "COLLECT returns every matching row (optionally aggregated); "
               "this engine returns one row's outcomes and the rule shape has "
               "nowhere to hold a list.",
    "RULE ORDER": "RULE ORDER returns every matching row in order; this engine "
                  "returns one.",
    "OUTPUT ORDER": "OUTPUT ORDER returns every matching row ranked by output; "
                    "this engine returns one.",
}


class DmnError(ValueError):
    """A DMN document this platform will not pretend to understand.

    ``code`` is the platform error code the API surfaces, so the caller gets
    something they can act on rather than "import failed".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ImportedDecision:
    """One ``<decision>`` mapped to this platform's rule shape."""

    name: str
    rule_type: str = "decision_table"
    definition_json: dict[str, Any] = field(default_factory=dict)
    #: Everything the file said that this platform does not store, kept so the
    #: preview can show it rather than losing it silently.
    notes: list[str] = field(default_factory=list)


# ─── XML helpers ──────────────────────────────────────────────────
#
# DMN in the wild carries at least four namespaces (OMG 1.1/1.2/1.3, plus
# Camunda's and Trisotech's extensions) and exporters disagree about prefixes.
# `bpmn2.py` settled this years ago by matching on the LOCAL name, and the same
# reasoning holds here: the element is `decisionTable` in every one of them.


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _child(el, name: str):
    found = _children(el, name)
    return found[0] if found else None


def _text_of(el, name: str) -> str:
    """The `<text>` inside a wrapper element, whitespace-normalised.

    Returns "" when absent — an empty cell is DMN's "any value", the same as
    ``-``, and both must reach ``_parse_unary_test`` rather than being treated
    as a missing element.
    """
    wrapper = _child(el, name)
    if wrapper is None:
        return ""
    text = _child(wrapper, "text")
    raw = (text.text if text is not None else wrapper.text) or ""
    return " ".join(raw.split())


# ─── FEEL unary tests — the supported subset ──────────────────────

_NUM = r"-?\d+(?:\.\d+)?"
_RANGE = re.compile(
    rf"^(?P<lo_b>[\[\]\(])\s*(?P<lo>{_NUM})\s*\.\.\s*(?P<hi>{_NUM})\s*(?P<hi_b>[\[\]\)])$"
)
_COMPARISON = re.compile(rf"^(?P<op><=|>=|<|>|!=|=)\s*(?P<val>.+)$")
_NOT = re.compile(r"^not\s*\((?P<inner>.*)\)$", re.IGNORECASE)

_COMPARISON_OPS = {"<": "$lt", "<=": "$lte", ">": "$gt", ">=": "$gte", "!=": "$neq"}


def _literal(raw: str) -> Any:
    """A FEEL literal → a Python value.

    Strings are quoted in FEEL, so an unquoted bareword is NOT a string: it is a
    variable reference or a function call, neither of which this subset covers.
    Guessing that `approved` means `"approved"` is exactly the silent
    mistranslation this module exists to avoid.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    if re.fullmatch(_NUM, text):
        return float(text) if "." in text else int(text)
    raise DmnError(
        "VEL-RULE-DMN-EXPR-422",
        f"'{raw}' is not a literal this platform reads. A cell may hold a "
        "number, a quoted string, true/false/null, a comparison, a range, or a "
        "comma-separated list of those. A bare word is a FEEL variable or "
        "function call, and this is deliberately not a FEEL engine.",
    )


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside quotes or brackets."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current = ""
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            current += ch
        elif ch in "[(":
            depth += 1
            current += ch
        elif ch in "])":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _parse_unary_test(cell: str) -> Any | None:
    """A DMN input entry → this platform's condition value.

    ``None`` means "no condition" — DMN's ``-`` and the empty cell, which match
    anything. The evaluator omits absent columns, so returning None and leaving
    the key out is the faithful mapping.
    """
    text = cell.strip()
    if text in ("", "-"):
        return None

    negated = _NOT.match(text)
    if negated:
        inner = _split_top_level(negated.group("inner"))
        if not inner:
            raise DmnError("VEL-RULE-DMN-EXPR-422", "not() with nothing in it")
        return {"$not_in": [_literal(part) for part in inner]}

    parts = _split_top_level(text)
    if len(parts) > 1:
        # A disjunction: "a","b" means a OR b.
        return {"$in": [_literal(part) for part in parts]}

    range_match = _RANGE.match(text)
    if range_match:
        lo = _literal(range_match.group("lo"))
        hi = _literal(range_match.group("hi"))
        # FEEL writes an exclusive bound as ( or ], and both forms appear.
        lo_exclusive = range_match.group("lo_b") in "(]"
        hi_exclusive = range_match.group("hi_b") in ")["
        return {
            "$gt" if lo_exclusive else "$gte": lo,
            "$lt" if hi_exclusive else "$lte": hi,
        }

    comparison = _COMPARISON.match(text)
    if comparison:
        op = comparison.group("op")
        value = _literal(comparison.group("val"))
        if op == "=":
            return value
        return {_COMPARISON_OPS[op]: value}

    return _literal(text)


# ─── Import ───────────────────────────────────────────────────────


def _priority_ranking(table, outputs: list[dict]) -> dict[str, int]:
    """Row priority for hitPolicy PRIORITY, or a refusal.

    DMN ranks a PRIORITY table by the ORDER OF THE OUTPUT VALUES, declared on
    the output as ``<outputValues>``. That ordering is the only thing that makes
    the policy meaningful, and a table that omits it is under-specified in DMN
    itself — so rather than invent a ranking, this refuses and says which
    element is missing.
    """
    if len(outputs) != 1:
        raise DmnError(
            "VEL-RULE-DMN-HITPOLICY-422",
            "hitPolicy PRIORITY ranks rows by one output's declared value "
            f"order, and this table has {len(outputs)} outputs. Rank it on a "
            "single output, or model it as FIRST with the rows in the order "
            "you want them tried.",
        )
    output_el = outputs[0]["element"]
    values_el = _child(output_el, "outputValues")
    values_text = _text_of(output_el, "outputValues") if values_el is not None else ""
    ordered = [str(_literal(p)) for p in _split_top_level(values_text)] if values_text else []
    if not ordered:
        raise DmnError(
            "VEL-RULE-DMN-HITPOLICY-422",
            "hitPolicy PRIORITY needs <outputValues> on the output to say which "
            "result outranks which. Without it the policy has no meaning, and "
            "guessing an order would silently change the decision.",
        )
    # First listed outranks the rest, so it gets the highest number.
    return {value: len(ordered) - index for index, value in enumerate(ordered)}


def parse_dmn(xml: str) -> list[ImportedDecision]:
    """Every ``<decision>`` holding a decision table, as platform rules.

    Raises ``DmnError`` — never returns a partial answer. An import that drops
    the two tables it could not read and creates the three it could leaves an
    operator believing they migrated a decision they did not.
    """
    if len(xml.encode("utf-8", errors="replace")) > MAX_DMN_BYTES:
        raise DmnError(
            "VEL-RULE-DMN-XML-400",
            f"DMN file exceeds {MAX_DMN_BYTES // (1024 * 1024)} MB.",
        )
    try:
        # defusedxml: XXE and entity expansion are refused before we see a tag.
        root = ET.fromstring(xml)
    except Exception as exc:  # ParseError, EntitiesForbidden, …
        raise DmnError(
            "VEL-RULE-DMN-XML-400",
            f"Not parseable as XML ({type(exc).__name__}).",
        ) from exc

    decisions = [el for el in root.iter() if _local(el.tag) == "decision"]
    if not decisions and _local(root.tag) == "decision":
        decisions = [root]

    results: list[ImportedDecision] = []
    for decision in decisions:
        table = _child(decision, "decisionTable")
        if table is None:
            # A decision with no table is a literal expression or a BKM
            # invocation — a different DMN feature, not a table we skipped.
            continue
        results.append(_parse_table(decision, table))

    if not results:
        raise DmnError(
            "VEL-RULE-DMN-EMPTY-422",
            "The file parsed, but holds no <decisionTable>. A DRD of literal "
            "expressions or business knowledge models is a different DMN "
            "feature; only decision tables are read here.",
        )
    return results


def _parse_table(decision, table) -> ImportedDecision:
    name = (decision.get("name") or decision.get("id") or "Imported decision").strip()
    hit_policy = (table.get("hitPolicy") or "UNIQUE").strip().upper()
    if hit_policy in UNSUPPORTED_HIT_POLICIES:
        raise DmnError(
            "VEL-RULE-DMN-HITPOLICY-422",
            f"'{name}': {UNSUPPORTED_HIT_POLICIES[hit_policy]}",
        )
    if hit_policy not in SUPPORTED_HIT_POLICIES:
        raise DmnError(
            "VEL-RULE-DMN-HITPOLICY-422",
            f"'{name}': unknown hitPolicy '{hit_policy}'. Known: "
            f"{', '.join(SUPPORTED_HIT_POLICIES)}.",
        )
    if table.get("aggregation"):
        # aggregation is only meaningful with COLLECT, which is already refused
        # above — but an exporter that writes it on another policy is telling us
        # the model means something we are not about to reproduce.
        raise DmnError(
            "VEL-RULE-DMN-HITPOLICY-422",
            f"'{name}': aggregation='{table.get('aggregation')}' aggregates "
            "several matching rows, which this engine does not do.",
        )

    columns: list[dict] = []
    notes: list[str] = []

    inputs = []
    for index, el in enumerate(_children(table, "input")):
        expression = _text_of(el, "inputExpression")
        if not expression:
            raise DmnError(
                "VEL-RULE-DMN-EXPR-422",
                f"'{name}': input {index + 1} has no <inputExpression>, so "
                "there is no field for its column to test.",
            )
        if not re.fullmatch(r"[A-Za-z_][\w.]*", expression):
            raise DmnError(
                "VEL-RULE-DMN-EXPR-422",
                f"'{name}': input expression '{expression}' is FEEL, not a "
                "field path. This platform resolves a dotted path against the "
                "case context; computing a value first is a rule of its own.",
            )
        col_id = el.get("id") or f"in{index + 1}"
        columns.append({
            "id": col_id,
            "label": el.get("label") or expression,
            "field_path": expression,
            "is_condition": True,
            "type_ref": _child(el, "inputExpression").get("typeRef") if _child(el, "inputExpression") is not None else None,
        })
        inputs.append({"id": col_id, "element": el})

    outputs = []
    for index, el in enumerate(_children(table, "output")):
        # `name` is the variable the output writes; DMN allows it to be absent
        # on a single-output table, where the decision's own name is the target.
        target = el.get("name") or (decision.get("name") or "").strip() or el.get("id")
        if not target:
            raise DmnError(
                "VEL-RULE-DMN-EXPR-422",
                f"'{name}': output {index + 1} has no name to write to.",
            )
        col_id = el.get("id") or f"out{index + 1}"
        columns.append({
            "id": col_id,
            "label": el.get("label") or target,
            "field_path": target,
            "is_condition": False,
            "type_ref": el.get("typeRef"),
        })
        outputs.append({"id": col_id, "element": el, "field_path": target})

    if not outputs:
        raise DmnError(
            "VEL-RULE-DMN-EXPR-422",
            f"'{name}': a decision table with no output decides nothing.",
        )

    ranking = _priority_ranking(table, outputs) if hit_policy == "PRIORITY" else {}

    rows: list[dict] = []
    for row_index, rule_el in enumerate(_children(table, "rule")):
        input_cells = _children(rule_el, "inputEntry")
        output_cells = _children(rule_el, "outputEntry")
        if len(input_cells) != len(inputs) or len(output_cells) != len(outputs):
            raise DmnError(
                "VEL-RULE-DMN-EXPR-422",
                f"'{name}': row {row_index + 1} has {len(input_cells)} input "
                f"and {len(output_cells)} output cells, but the table declares "
                f"{len(inputs)} and {len(outputs)}. A row read against the "
                "wrong column is a wrong decision, so this is refused rather "
                "than aligned by position.",
            )

        conditions: dict[str, Any] = {}
        for cell_el, column in zip(input_cells, inputs):
            parsed = _parse_unary_test(_cell_text(cell_el))
            if parsed is not None:
                conditions[column["id"]] = parsed

        outcomes: dict[str, Any] = {}
        for cell_el, column in zip(output_cells, outputs):
            text = _cell_text(cell_el)
            if text in ("", "-"):
                # An empty OUTPUT is not "any value" — it is no value. Writing
                # nothing is different from writing null, so the key is omitted.
                continue
            outcomes[column["id"]] = _literal(text)

        row: dict[str, Any] = {
            "conditions": conditions,
            "outcomes": outcomes,
            # Equal priorities + a stable sort = document order, which is what
            # FIRST/UNIQUE/ANY all need.
            "priority": 0,
        }
        if hit_policy == "PRIORITY":
            value = outcomes.get(outputs[0]["id"])
            if str(value) not in ranking:
                raise DmnError(
                    "VEL-RULE-DMN-HITPOLICY-422",
                    f"'{name}': row {row_index + 1} outputs '{value}', which is "
                    "not in the output's declared value order, so its rank is "
                    "undefined.",
                )
            row["priority"] = ranking[str(value)]
        description = rule_el.get("label") or _text_of(rule_el, "description")
        if description:
            row["description"] = description
        rows.append(row)

    if not rows:
        notes.append("The table has no rules, so it decides nothing until rows are added.")

    return ImportedDecision(
        name=name,
        definition_json={
            "table_columns": columns,
            "table_rows": rows,
            # Kept so an export can put back what the file said, and so a
            # UNIQUE table stays labelled UNIQUE for anything that later checks
            # rows do not overlap.
            "hit_policy": hit_policy,
            "source": "dmn",
        },
        notes=notes,
    )


def _cell_text(cell_el) -> str:
    text = _child(cell_el, "text")
    raw = (text.text if text is not None else cell_el.text) or ""
    return " ".join(raw.split())


# ─── Export ───────────────────────────────────────────────────────

_DMN_NS = "https://www.omg.org/spec/DMN/20191111/MODEL/"


def _unary_test_text(value: Any) -> str:
    """A condition value → the FEEL unary test it came from.

    The inverse of ``_parse_unary_test`` for everything that module produces,
    so import→export→import is the identity. A condition the internal shape
    allows but DMN's unary tests cannot express is refused here rather than
    written as something that reads back differently.
    """
    if value is None:
        return "-"
    if isinstance(value, dict):
        parts: list[str] = []
        keys = set(value)
        lower = {"$gt": "(", "$gte": "["}
        upper = {"$lt": ")", "$lte": "]"}
        if keys & set(lower) and keys & set(upper):
            lo_key = "$gt" if "$gt" in value else "$gte"
            hi_key = "$lt" if "$lt" in value else "$lte"
            return (f"{lower[lo_key]}{_feel_literal(value[lo_key])}.."
                    f"{_feel_literal(value[hi_key])}{upper[hi_key]}")
        for op, value_ in value.items():
            if op == "$in":
                parts.append(", ".join(_feel_literal(v) for v in value_))
            elif op == "$not_in":
                parts.append("not(" + ", ".join(_feel_literal(v) for v in value_) + ")")
            elif op in ("$lt", "$lte", "$gt", "$gte", "$neq"):
                symbol = {v: k for k, v in _COMPARISON_OPS.items()}[op]
                parts.append(f"{symbol}{_feel_literal(value_)}")
            else:
                raise DmnError(
                    "VEL-RULE-DMN-EXPORT-409",
                    f"Condition operator '{op}' has no DMN unary test. This "
                    "table can be evaluated here but not written as DMN.",
                )
        return ", ".join(parts)
    return _feel_literal(value)


def _feel_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return '"%s"' % str(value).replace('"', '\\"')


def to_dmn(rule_name: str, definition: dict[str, Any]) -> str:
    """A platform decision-table rule → a DMN 1.3 document.

    Camunda and Flowable both read this back, and the round trip is pinned by
    test: import → export → import returns the same rule.

    The one thing that can move is **row order**, and only for PRIORITY. Rows
    are written in the order the evaluator would try them (priority descending),
    because a rule whose priorities were edited by hand evaluates in an order
    its stored list no longer shows — exporting the list order would produce a
    DMN table that decides differently from the one that was exported. DMN
    PRIORITY rules are explicitly unordered, so re-ordering them is faithful;
    for FIRST, UNIQUE and ANY every priority is 0 and a stable sort changes
    nothing.
    """
    columns = definition.get("table_columns") or []
    rows = definition.get("table_rows") or []
    inputs = [c for c in columns if c.get("is_condition", True)]
    outputs = [c for c in columns if not c.get("is_condition", True)]
    if not outputs:
        raise DmnError(
            "VEL-RULE-DMN-EXPORT-409",
            "This rule declares no outcome column, so there is no DMN output "
            "to write.",
        )

    hit_policy = str(definition.get("hit_policy") or "UNIQUE").upper()
    if hit_policy not in SUPPORTED_HIT_POLICIES:
        # A table stored with a policy we cannot express is not exported as a
        # different one.
        raise DmnError(
            "VEL-RULE-DMN-EXPORT-409",
            f"hit policy '{hit_policy}' is not a DMN policy this platform "
            "round-trips.",
        )
    # PRIORITY is only meaningful with the value order that produced it, and the
    # rule shape stores the resolved ranks, not the order. Export the ranking
    # back as <outputValues> so the file means what the table means.
    definitions = _ET_SERIALISE.Element(
        "definitions",
        {
            "xmlns": _DMN_NS,
            "id": f"velaris_{re.sub(r'[^A-Za-z0-9_]', '_', rule_name)[:60] or 'decision'}",
            "name": rule_name,
            "namespace": "https://velaris.app/dmn",
            "exporter": "Velaris",
        },
    )
    # A DIGEST, not `hash()`: Python randomises string hashing per process, so
    # the same rule exported twice — from two workers, or after a restart —
    # would carry two different decision ids. An export that is not
    # byte-stable is one nobody can diff or check into a repository.
    decision_id = hashlib.sha256(rule_name.encode("utf-8")).hexdigest()[:12]
    decision = _ET_SERIALISE.SubElement(
        definitions, "decision",
        {"id": f"decision_{decision_id}", "name": rule_name},
    )
    table = _ET_SERIALISE.SubElement(
        decision, "decisionTable",
        {"id": "decisionTable_1", "hitPolicy": hit_policy},
    )

    for index, column in enumerate(inputs):
        input_el = _ET_SERIALISE.SubElement(
            table, "input",
            {"id": str(column.get("id") or f"in{index + 1}"),
             "label": str(column.get("label") or column.get("field_path") or "")},
        )
        expression_attrs = {"id": f"inputExpression_{index + 1}"}
        if column.get("type_ref"):
            expression_attrs["typeRef"] = str(column["type_ref"])
        expression = _ET_SERIALISE.SubElement(input_el, "inputExpression", expression_attrs)
        _ET_SERIALISE.SubElement(expression, "text").text = str(column.get("field_path") or "")

    for index, column in enumerate(outputs):
        attrs = {
            "id": str(column.get("id") or f"out{index + 1}"),
            "label": str(column.get("label") or column.get("field_path") or ""),
            "name": str(column.get("field_path") or ""),
        }
        if column.get("type_ref"):
            attrs["typeRef"] = str(column["type_ref"])
        output_el = _ET_SERIALISE.SubElement(table, "output", attrs)
        if hit_policy == "PRIORITY" and index == 0:
            ranked = sorted(
                {str(r.get("outcomes", {}).get(column["id"])): r.get("priority", 0)
                 for r in rows}.items(),
                key=lambda pair: pair[1], reverse=True,
            )
            values = ", ".join(_feel_literal(value) for value, _ in ranked)
            _ET_SERIALISE.SubElement(output_el, "outputValues").append(
                _text_element(values))

    # FIRST/UNIQUE/ANY carry no priority, so document order is the meaning:
    # write the rows in the order the evaluator would try them.
    ordered_rows = sorted(rows, key=lambda r: r.get("priority", 0), reverse=True)
    for index, row in enumerate(ordered_rows):
        rule_el = _ET_SERIALISE.SubElement(table, "rule", {"id": f"row_{index + 1}"})
        if row.get("description"):
            _ET_SERIALISE.SubElement(rule_el, "description").text = str(row["description"])
        for column in inputs:
            entry = _ET_SERIALISE.SubElement(rule_el, "inputEntry",
                                             {"id": f"in_{index + 1}_{column['id']}"})
            _ET_SERIALISE.SubElement(entry, "text").text = _unary_test_text(
                row.get("conditions", {}).get(column["id"]))
        for column in outputs:
            entry = _ET_SERIALISE.SubElement(rule_el, "outputEntry",
                                             {"id": f"out_{index + 1}_{column['id']}"})
            value = row.get("outcomes", {}).get(column["id"])
            _ET_SERIALISE.SubElement(entry, "text").text = (
                "" if value is None and column["id"] not in row.get("outcomes", {})
                else _feel_literal(value))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + _ET_SERIALISE.tostring(definitions, encoding="unicode")
    )


def _text_element(value: str):
    el = _ET_SERIALISE.Element("text")
    el.text = value
    return el
