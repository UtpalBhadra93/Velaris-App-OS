"""DMN interchange — what is read, what is REFUSED, and the round trip.

The refusals are the point of this file. The engine has run decision tables for
years; the risk in adding an importer is not that a table fails to load, it is
that one loads and quietly decides something slightly different from what the
modeller wrote. Every test below that asserts a refusal is a table this
platform could have half-understood.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.core.dmn import DmnError, parse_dmn, to_dmn
from case_service.core.rules_evaluator import evaluate_decision_table

NS = 'xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/"'


def _table(rules: str, *, hit_policy: str = "FIRST", inputs: str = "", outputs: str = "",
           table_attrs: str = "") -> str:
    inputs = inputs or (
        '<input id="i1" label="Amount">'
        '<inputExpression typeRef="number"><text>case.data.amount</text></inputExpression>'
        "</input>"
    )
    outputs = outputs or '<output id="o1" label="Discount" name="discount" typeRef="number"/>'
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><definitions {NS} id="defs" name="Defs">'
        f'<decision id="dec1" name="Discount">'
        f'<decisionTable id="t1" hitPolicy="{hit_policy}"{table_attrs}>'
        f"{inputs}{outputs}{rules}"
        "</decisionTable></decision></definitions>"
    )


def _row(*cells: str, outputs: tuple[str, ...] = ("0.1",)) -> str:
    # `<` is escaped because a real exporter escapes it — `&lt; 100` is what a
    # `< 100` cell looks like in every .dmn file on disk.
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    entries = "".join(f"<inputEntry><text>{esc(c)}</text></inputEntry>" for c in cells)
    entries += "".join(f"<outputEntry><text>{esc(o)}</text></outputEntry>" for o in outputs)
    return f"<rule>{entries}</rule>"


# ── What it reads ────────────────────────────────────────────────────────────


class TestUnaryTests:
    """The FEEL subset real decision tables are written in."""

    @pytest.mark.parametrize("cell,expected", [
        ("42", 42),
        ("4.5", 4.5),
        ('"gold"', "gold"),
        ("true", True),
        (">= 100", {"$gte": 100}),
        ("< 100", {"$lt": 100}),
        ("!= 3", {"$neq": 3}),
        ("= 3", 3),
        ("[100..1000]", {"$gte": 100, "$lte": 1000}),
        ("[100..1000)", {"$gte": 100, "$lt": 1000}),
        ("(100..1000]", {"$gt": 100, "$lte": 1000}),
        ('"a","b"', {"$in": ["a", "b"]}),
        ('not("blocked")', {"$not_in": ["blocked"]}),
    ])
    def test_a_cell_maps_to_a_condition(self, cell, expected):
        rule = parse_dmn(_table(_row(cell)))[0]
        assert rule.definition_json["table_rows"][0]["conditions"]["i1"] == expected

    @pytest.mark.parametrize("cell", ["-", "", "   "])
    def test_an_empty_cell_is_no_condition_at_all(self, cell):
        """DMN's `-` matches anything. The evaluator skips columns a row does
        not mention, so the faithful mapping is to leave the key out — not to
        store a condition that happens to always pass."""
        rule = parse_dmn(_table(_row(cell)))[0]
        assert rule.definition_json["table_rows"][0]["conditions"] == {}

    def test_a_bare_word_is_refused_not_treated_as_a_string(self):
        """`approved` in FEEL is a variable, not the string "approved".
        Guessing costs nothing until the day the guess is wrong."""
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("approved")))
        assert exc.value.code == "VEL-RULE-DMN-EXPR-422"


class TestImport:
    def test_columns_and_rows(self):
        rule = parse_dmn(_table(_row(">= 100")))[0]
        assert rule.name == "Discount"
        assert rule.rule_type == "decision_table"
        columns = rule.definition_json["table_columns"]
        assert [c["field_path"] for c in columns] == ["case.data.amount", "discount"]
        assert [c["is_condition"] for c in columns] == [True, False]

    def test_an_imported_table_evaluates(self):
        """The whole point: what comes out of the file runs on the engine that
        was already here, with no translation step in between."""
        rule = parse_dmn(_table(
            _row(">= 1000", outputs=("0.2",)) + _row("[100..1000)") + _row("-", outputs=("0",))
        ))[0]
        table = {"id": "r", **rule.definition_json}
        assert evaluate_decision_table(table, {"case": {"data": {"amount": 5000}}})["outcomes"] == {"discount": 0.2}
        assert evaluate_decision_table(table, {"case": {"data": {"amount": 500}}})["outcomes"] == {"discount": 0.1}
        assert evaluate_decision_table(table, {"case": {"data": {"amount": 5}}})["outcomes"] == {"discount": 0}

    def test_document_order_is_the_evaluation_order_for_first(self):
        """FIRST means the first matching ROW. Every priority is 0 and the
        evaluator's sort is stable, so the order in the file is the order
        tried — the narrower row must be allowed to win."""
        rule = parse_dmn(_table(
            _row(">= 100", outputs=("0.5",)) + _row("-", outputs=("0",))
        ))[0]
        table = {"id": "r", **rule.definition_json}
        assert evaluate_decision_table(table, {"case": {"data": {"amount": 100}}})["outcomes"] == {"discount": 0.5}

    def test_a_decision_without_a_table_is_not_silently_dropped(self):
        xml = (f'<definitions {NS}><decision id="d" name="D">'
               "<literalExpression><text>1</text></literalExpression>"
               "</decision></definitions>")
        with pytest.raises(DmnError) as exc:
            parse_dmn(xml)
        assert exc.value.code == "VEL-RULE-DMN-EMPTY-422"

    def test_an_empty_output_cell_writes_nothing_rather_than_null(self):
        """"No value" and "the value null" are different answers. The row keeps
        neither key rather than asserting one of them."""
        rule = parse_dmn(_table(_row(">= 100", outputs=("-",))))[0]
        assert rule.definition_json["table_rows"][0]["outcomes"] == {}


class TestHitPolicies:
    @pytest.mark.parametrize("policy", ["UNIQUE", "FIRST", "ANY"])
    def test_the_policies_this_engine_can_honour(self, policy):
        rule = parse_dmn(_table(_row("1"), hit_policy=policy))[0]
        assert rule.definition_json["hit_policy"] == policy

    @pytest.mark.parametrize("policy", ["COLLECT", "RULE ORDER", "OUTPUT ORDER"])
    def test_the_policies_that_return_several_rows_are_refused(self, policy):
        """This engine returns one row's outcomes. Mapping a multi-row policy
        onto FIRST produces a table that runs and is wrong, which is the one
        outcome worth refusing an import over."""
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1"), hit_policy=policy))
        assert exc.value.code == "VEL-RULE-DMN-HITPOLICY-422"
        assert policy.split()[0].lower() in str(exc.value).lower()

    def test_aggregation_is_refused_even_on_a_supported_policy(self):
        """An exporter that writes aggregation is telling us the model means
        something this engine does not do, whatever the policy says."""
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1"), table_attrs=' aggregation="SUM"'))
        assert exc.value.code == "VEL-RULE-DMN-HITPOLICY-422"

    def test_priority_needs_the_ranking_written_down(self):
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1", outputs=('"low"',)), hit_policy="PRIORITY",
                             outputs='<output id="o1" name="band"/>'))
        assert exc.value.code == "VEL-RULE-DMN-HITPOLICY-422"
        assert "outputValues" in str(exc.value)

    def test_priority_ranks_rows_by_the_declared_value_order(self):
        rule = parse_dmn(_table(
            _row("1", outputs=('"low"',)) + _row("2", outputs=('"high"',)),
            hit_policy="PRIORITY",
            outputs='<output id="o1" name="band"><outputValues>'
                    '<text>"high","low"</text></outputValues></output>',
        ))[0]
        rows = rule.definition_json["table_rows"]
        high = next(r for r in rows if r["outcomes"]["o1"] == "high")
        low = next(r for r in rows if r["outcomes"]["o1"] == "low")
        assert high["priority"] > low["priority"]

    def test_a_priority_row_outside_the_declared_order_is_refused(self):
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(
                _row("1", outputs=('"unknown"',)),
                hit_policy="PRIORITY",
                outputs='<output id="o1" name="band"><outputValues>'
                        '<text>"high","low"</text></outputValues></output>',
            ))
        assert exc.value.code == "VEL-RULE-DMN-HITPOLICY-422"

    def test_an_unknown_policy_is_not_assumed_to_be_unique(self):
        with pytest.raises(DmnError):
            parse_dmn(_table(_row("1"), hit_policy="SOMETIMES"))


class TestRefusals:
    def test_xxe_is_refused_by_the_parser_not_by_us(self):
        """defusedxml, the same choice `bpmn2.py` made. An importer that reads
        a file a stranger sent is the classic XXE surface."""
        xml = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               f'<definitions {NS}><decision name="&x;"/></definitions>')
        with pytest.raises(DmnError) as exc:
            parse_dmn(xml)
        assert exc.value.code == "VEL-RULE-DMN-XML-400"

    def test_a_file_over_the_ceiling_is_refused_before_it_is_parsed(self):
        from case_service.core import dmn
        with pytest.raises(DmnError) as exc:
            parse_dmn("x" * (dmn.MAX_DMN_BYTES + 1))
        assert exc.value.code == "VEL-RULE-DMN-XML-400"

    def test_not_xml_at_all(self):
        with pytest.raises(DmnError) as exc:
            parse_dmn("this is a spreadsheet, not a file")
        assert exc.value.code == "VEL-RULE-DMN-XML-400"

    def test_a_computed_input_expression_is_refused(self):
        """`sum(items)` is FEEL. This platform resolves a dotted path against
        the case context; computing the value first is a rule of its own."""
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1"), inputs='<input id="i1"><inputExpression>'
                                               "<text>sum(case.data.items)</text>"
                                               "</inputExpression></input>"))
        assert exc.value.code == "VEL-RULE-DMN-EXPR-422"

    def test_a_row_with_the_wrong_number_of_cells_is_refused(self):
        """Reading a row against the wrong column is a wrong decision, and
        alignment by position is how that happens quietly."""
        two_inputs = ('<input id="i1"><inputExpression><text>case.data.a</text>'
                      "</inputExpression></input>"
                      '<input id="i2"><inputExpression><text>case.data.b</text>'
                      "</inputExpression></input>")
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1"), inputs=two_inputs))
        assert exc.value.code == "VEL-RULE-DMN-EXPR-422"

    def test_a_table_with_no_output_decides_nothing(self):
        with pytest.raises(DmnError) as exc:
            parse_dmn(_table(_row("1", outputs=()), outputs=" "))
        assert exc.value.code == "VEL-RULE-DMN-EXPR-422"

    def test_one_unreadable_table_refuses_the_whole_file(self):
        """All or nothing. Creating the three tables that parsed leaves an
        operator believing they migrated a decision they did not."""
        good = ('<decision id="a" name="Good"><decisionTable hitPolicy="FIRST">'
                '<input id="i1"><inputExpression><text>case.data.a</text></inputExpression></input>'
                '<output id="o1" name="x"/>'
                "<rule><inputEntry><text>1</text></inputEntry>"
                "<outputEntry><text>2</text></outputEntry></rule></decisionTable></decision>")
        bad = good.replace('id="a" name="Good"', 'id="b" name="Bad"').replace(
            'hitPolicy="FIRST"', 'hitPolicy="COLLECT"')
        with pytest.raises(DmnError):
            parse_dmn(f"<definitions {NS}>{good}{bad}</definitions>")


# ── The round trip ───────────────────────────────────────────────────────────


class TestRoundTrip:
    @pytest.mark.parametrize("policy", ["UNIQUE", "FIRST", "ANY"])
    def test_import_export_import_is_the_identity(self, policy):
        source = _table(
            _row(">= 1000", outputs=("0.2",))
            + _row("[100..1000)", outputs=("0.1",))
            + _row("-", outputs=("0",)),
            hit_policy=policy,
        )
        first = parse_dmn(source)[0]
        second = parse_dmn(to_dmn(first.name, first.definition_json))[0]
        assert second.definition_json == first.definition_json

    def test_priority_survives_the_round_trip_as_the_declared_order(self):
        """PRIORITY rows are unordered in DMN — the ranking lives in
        <outputValues>, so that is what has to come back, not the row order."""
        source = _table(
            _row("1", outputs=('"low"',)) + _row("2", outputs=('"high"',)),
            hit_policy="PRIORITY",
            outputs='<output id="o1" name="band"><outputValues>'
                    '<text>"high","low"</text></outputValues></output>',
        )
        first = parse_dmn(source)[0]
        exported = to_dmn(first.name, first.definition_json)
        assert "outputValues" in exported
        second = parse_dmn(exported)[0]
        assert (sorted(second.definition_json["table_rows"], key=lambda r: r["priority"])
                == sorted(first.definition_json["table_rows"], key=lambda r: r["priority"]))

    def test_a_condition_dmn_cannot_express_is_refused_not_mangled(self):
        """The internal shape allows operators DMN's unary tests have no
        notation for. Writing one out as something that reads back differently
        would make the exported file a lie."""
        with pytest.raises(DmnError) as exc:
            to_dmn("Regex", {
                "table_columns": [
                    {"id": "i1", "field_path": "case.data.x", "is_condition": True},
                    {"id": "o1", "field_path": "y", "is_condition": False},
                ],
                "table_rows": [{"conditions": {"i1": {"$matches": "^a"}},
                                "outcomes": {"o1": 1}, "priority": 0}],
                "hit_policy": "FIRST",
            })
        assert exc.value.code == "VEL-RULE-DMN-EXPORT-409"

    def test_export_orders_rows_the_way_the_engine_tries_them(self):
        """A rule whose priorities were edited by hand evaluates in an order
        its stored list no longer shows. Exporting the list order would produce
        a DMN file that decides differently from the rule it came from."""
        xml = to_dmn("Hand edited", {
            "table_columns": [
                {"id": "i1", "field_path": "case.data.x", "is_condition": True},
                {"id": "o1", "field_path": "y", "is_condition": False},
            ],
            "table_rows": [
                {"conditions": {}, "outcomes": {"o1": "fallback"}, "priority": 0},
                {"conditions": {"i1": 1}, "outcomes": {"o1": "specific"}, "priority": 10},
            ],
            "hit_policy": "FIRST",
        })
        assert xml.index("specific") < xml.index("fallback")


# ── Through the API ──────────────────────────────────────────────────────────


class TestDmnApi:
    async def test_a_preview_creates_nothing(self, client):
        before = (await client.get("/api/v1/rules?rule_type=decision_table")).json()["total"]
        resp = await client.post("/api/v1/rules/dmn/import", json={
            "xml": _table(_row(">= 100")), "dry_run": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True and body["created"] == 0
        assert body["tables"][0]["name"] == "Discount"
        assert body["tables"][0]["row_count"] == 1
        assert body["tables"][0]["rule_id"] is None
        after = (await client.get("/api/v1/rules?rule_type=decision_table")).json()["total"]
        assert after == before

    async def test_import_creates_a_rule_that_is_off_until_someone_turns_it_on(self, client):
        resp = await client.post("/api/v1/rules/dmn/import", json={
            "xml": _table(_row(">= 100")), "dry_run": False,
        })
        assert resp.status_code == 200
        rule_id = resp.json()["tables"][0]["rule_id"]
        rule = (await client.get(f"/api/v1/rules/{rule_id}")).json()
        assert rule["rule_type"] == "decision_table"
        assert rule["enabled"] is False, (
            "an imported table that starts deciding the moment the file lands "
            "is an operator decision nobody made")

    async def test_a_refused_file_carries_the_code_that_says_why(self, client):
        resp = await client.post("/api/v1/rules/dmn/import", json={
            "xml": _table(_row("1"), hit_policy="COLLECT"), "dry_run": True,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VEL-RULE-DMN-HITPOLICY-422"

    async def test_export_returns_a_file_camunda_can_read(self, client):
        created = await client.post("/api/v1/rules/dmn/import", json={
            "xml": _table(_row(">= 100")), "dry_run": False,
        })
        rule_id = created.json()["tables"][0]["rule_id"]
        resp = await client.get(f"/api/v1/rules/{rule_id}/dmn")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/xml")
        assert "attachment" in resp.headers["content-disposition"]
        assert "<decisionTable" in resp.text
        # And what we emit, we read.
        assert parse_dmn(resp.text)[0].definition_json["table_rows"]

    async def test_exporting_a_rule_that_is_not_a_table_is_refused_by_name(self, client):
        created = await client.post("/api/v1/rules", json={
            "name": "Not a table", "version": "1.0.0", "rule_type": "when",
            "definition_json": {"conditions": [], "actions": []},
        })
        resp = await client.get(f"/api/v1/rules/{created.json()['id']}/dmn")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "VEL-RULE-DMN-EXPORT-409"


class TestTheAuditFoundThis:
    def test_an_export_is_byte_stable(self):
        """The decision id came from `hash(rule_name)`, and Python randomises
        string hashing per process — so the same rule exported from two workers,
        or after a restart, carried two different ids. An export nobody can diff
        or check into a repository is not an interchange format."""
        definition = {
            "table_columns": [
                {"id": "i1", "field_path": "case.data.x", "is_condition": True},
                {"id": "o1", "field_path": "y", "is_condition": False},
            ],
            "table_rows": [{"conditions": {"i1": 1}, "outcomes": {"o1": 2},
                            "priority": 0}],
            "hit_policy": "FIRST",
        }
        assert to_dmn("Discount", definition) == to_dmn("Discount", definition)

        # Across PROCESSES with different hash seeds, which is the only way to
        # catch this: within one process `hash()` is stable and the bug hides.
        import os
        import subprocess
        import sys

        script = (
            "import sys; sys.path[:0]=['.', '../_shared'];"
            "from case_service.core.dmn import to_dmn;"
            "print(to_dmn('Discount', %r).split('decision id=')[1][:20])" % definition
        )
        ids = set()
        for seed in ("1", "2", "99"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            run = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                 text=True, env=env, cwd=os.path.dirname(
                                     os.path.dirname(os.path.abspath(__file__))))
            assert run.returncode == 0, run.stderr[-400:]
            assert run.stdout.strip(), "the subprocess printed nothing to compare"
            ids.add(run.stdout.strip())
        assert len(ids) == 1, f"the decision id moves with PYTHONHASHSEED: {ids}"
