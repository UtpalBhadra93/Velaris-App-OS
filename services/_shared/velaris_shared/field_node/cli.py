"""`python -m velaris_shared.field_node <command>` — the field node's CLI.

Exit codes: 0 done · 2 the node refused (stale, denied, not found…) · 3 the hub
ordered a wipe and the node has wiped itself · 4 the hub refused or was unreachable.

Every command that talks to the hub CHECKS IN FIRST, so a shred order is obeyed
even on a connection whose sync would have been refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from velaris_shared.field_node.assist import AssistError
from velaris_shared.field_node.hub import Hub, HubError
from velaris_shared.field_node.node import FieldNode, NodeError

DEFAULT_DB = Path(os.environ.get("VELARIS_FIELD_NODE_DB")
                  or Path.home() / ".velaris-field" / "node.db")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _token(args: argparse.Namespace) -> str:
    if args.token_file:
        return Path(args.token_file).read_text().strip()
    return os.environ.get("VELARIS_FIELD_TOKEN", "").strip()


class Wiped(Exception):
    pass


def _check_in(node: FieldNode, hub: Hub) -> dict[str, Any]:
    out = hub.contact(node.slug, node.signed_contact())
    if out.get("wipe"):
        node.wipe()
        raise Wiped()
    return out


def _value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="velaris-field-node",
                                description="A Velaris F-10 field node.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="the node's state file")
    p.add_argument("--token-file", help="a file holding the staff token (else VELARIS_FIELD_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create the node and its key")
    i.add_argument("--slug", required=True)
    i.add_argument("--hub", required=True, help="https://your-velaris")
    sub.add_parser("status", help="what the node is (never what it holds)")
    sub.add_parser("contact", help="check in with the hub")
    pl = sub.add_parser("pull", help="check in, then take a new bundle from the hub")
    pl.add_argument("--offline-deny", nargs="*")
    im = sub.add_parser("import", help="take a bundle from a file (carried in)")
    im.add_argument("file")
    sub.add_parser("cases", help="list the cases on the node")
    for name in ("show", "export"):
        s = sub.add_parser(name)
        s.add_argument("case_id")
    n = sub.add_parser("note")
    n.add_argument("case_id")
    n.add_argument("text")
    st = sub.add_parser("set", help="set a field (VALUE is JSON if it parses)")
    st.add_argument("case_id")
    st.add_argument("name")
    st.add_argument("value")
    c = sub.add_parser("complete")
    c.add_argument("case_id")
    c.add_argument("step")
    a = sub.add_parser("assist", help="ask a local model about a case")
    a.add_argument("case_id")
    a.add_argument("question")
    a.add_argument("--model", default=os.environ.get("VELARIS_FIELD_MODEL", "llama3.2"))
    a.add_argument("--url", default=os.environ.get("VELARIS_FIELD_MODEL_URL",
                                                   "http://127.0.0.1:11434"))
    sub.add_parser("sync", help="check in, then hand the recorded work back")
    w = sub.add_parser("wipe", help="destroy the node's data and key")
    w.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            node = FieldNode.init(args.db, slug=args.slug, hub_url=args.hub)
            _print({"slug": node.slug, "public_key": node.public_key, "key_id": node.key_id,
                    "next": "Register this public key on the hub (POST /api/v1/field/nodes) "
                            "and read the key_id back to whoever does it."})
            return 0
        node = FieldNode(args.db)

        def hub() -> Hub:
            return Hub(node.hub_url, _token(args), transport=transport)

        if args.cmd == "status":
            _print(node.status())
        elif args.cmd == "contact":
            _print(_check_in(node, hub()))
        elif args.cmd == "pull":
            h = hub()
            _check_in(node, h)
            if node.unsynced():
                raise NodeError("recorded work has not been synced; sync before pulling")
            out = h.dispatch(node.slug, args.offline_deny)
            _print(node.load_bundle(out["bundle"]))
        elif args.cmd == "import":
            _print(node.load_bundle(json.loads(Path(args.file).read_text())))
        elif args.cmd == "cases":
            _print(node.cases())
        elif args.cmd == "show":
            _print(node.show(args.case_id))
        elif args.cmd == "export":
            _print(node.export(args.case_id))
        elif args.cmd == "note":
            _print(node.note(args.case_id, args.text))
        elif args.cmd == "set":
            _print(node.set_field(args.case_id, args.name, _value(args.value)))
        elif args.cmd == "complete":
            _print(node.complete(args.case_id, args.step))
        elif args.cmd == "assist":
            print("ADVICE FROM A LOCAL MODEL — not recorded; write a note if you agree.\n")
            print(node.assist(args.case_id, args.question, url=args.url, model=args.model))
        elif args.cmd == "sync":
            h = hub()
            _check_in(node, h)
            out = h.sync(node.slug, node.sync_body())
            node.synced(out)
            if out.get("shred_ordered"):
                node.wipe()
                raise Wiped()
            _print(out)
        elif args.cmd == "wipe":
            if not args.yes:
                raise NodeError("wipe destroys the node's data and key; pass --yes")
            node.wipe()
            _print({"wiped": True})
        return 0
    except Wiped:
        _print({"wiped": True, "why": "the hub ordered this node shredded"})
        return 3
    except (NodeError, AssistError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except HubError as exc:
        print(f"hub: {exc}", file=sys.stderr)
        return 4
