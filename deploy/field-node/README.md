# Field node — running Velaris offline on a device (F-10, Oct item 8)

A field node is a laptop or small box that takes a slice of the caseload into a
place with no line — a ship, a clinic, an inspection team — is worked on for
days, and hands the work back when it reconnects. The hub side is in
`docs/components/field-operations.md`; this directory is the device side.

The node is **one CLI and one SQLite file**:
`python -m velaris_shared.field_node` (package `services/_shared/velaris_shared/field_node/`).
It needs no server, no container and no network while it works.

## Before you install: encrypt the disk

The node's state file holds the dispatched case data and the node's private key.
It is created `0600` inside a `0700` directory, and the node refuses to open it if
anyone else can read it — but a lost device is read with a screwdriver, not a
login. **Put the node on a LUKS/FileVault/BitLocker-encrypted disk.** The wipe
overwrites and deletes the file; on flash storage an overwrite is not a
guarantee, and disk encryption is what makes it one.

## Install

```bash
python3 -m venv /opt/velaris-field && . /opt/velaris-field/bin/activate
pip install ./services/_shared            # the shared package; the node uses only
                                           # the standard library and `cryptography`
```

Optional, for `assist`: [Ollama](https://ollama.com) on the device (or on its
private network) with a model pulled, e.g. `ollama pull llama3.2`.

## Enrol it (once, while connected)

```bash
velaris() { python -m velaris_shared.field_node "$@"; }
velaris init --slug ship-a --hub https://velaris.example.com
# → prints public_key and key_id
```

Register that public key on the hub — Studio **Field Ops → Enrol a node**, or
`POST /api/v1/field/nodes` — with a charter naming the case types it may hold.
**Read the `key_id` back to the person doing it** and check it matches: that is
the whole of key pinning.

## Work

```bash
export VELARIS_FIELD_TOKEN=…          # a dispatcher's token (field.nodes.dispatch);
                                      # read per command, never stored by the node
velaris pull                          # check in, take a bundle
velaris cases                         # — offline from here —
velaris show <case_id>
velaris note <case_id> "boarded at 09:40"
velaris set <case_id> inspection_code '"OK-7"'
velaris complete <case_id> s1
velaris assist <case_id> "what is still missing?"   # a LOCAL model only
velaris sync                          # — back online: check in, hand it back —
```

No network? `velaris import bundle.json` takes a bundle carried in on media.

## What the node refuses, on the device

| | |
|---|---|
| Acting past `stale_after` (72 h by default) | `cases`, `show`, `note`, `set`, `complete`, `assist` stop. `sync`, `contact` and `wipe` still work — they are how a stale node gets home. |
| A clock set back | The node remembers the latest time it has seen; a clock more than 5 minutes earlier is refused. |
| Anything in the dispatch's `offline_deny` | `export` is denied by default. |
| A new bundle over unsynced work | A new dispatch supersedes the old one on the hub, and that work could never be handed back. Sync first. |
| A model that is not local | `assist` resolves the host and accepts only loopback or private addresses (never public, never link-local), connects to the address it checked, and follows no redirects. Its answer is printed, never recorded. |
| A hub over plain http | https only, except to `127.0.0.1`/`localhost`. An internal CA goes in `VELARIS_FIELD_CA_FILE`. |

After a successful sync the node **drops the cases and events** — a dispatch is
synced once, and data it can no longer act on is data it should not carry.

**What it cannot stop:** someone with root on the device can read the file and
edit the node's clock record. The defences there are the encrypted disk, the hub
refusing any work dated after `stale_after`, and the shred order.

## Losing a device

Order a shred on the hub (Studio **Field Ops**, or `POST /field/nodes/{slug}/shred`).
Every command that reaches the hub **checks in first** (`POST /field/nodes/{slug}/contact`),
so the order is obeyed on the next connection — even one whose sync would have
been refused. The node wipes its data and key and exits with code 3. A node that
never reconnects is locked out by its own `stale_after`.

## Signatures

The node **always** signs its check-ins and its sync with its key. The hub
checks a signature whenever one is sent. Requiring one is the operator's choice:
set `VELARIS_FIELD_REQUIRE_SIGNATURE=true` on case-service once every node runs
this CLI, and a staff token alone can no longer check in or hand work back for a
node. **Unset, nothing changes** — a node that never signed keeps working.

## Optional: check in on a timer

A node that is often online can check in every 15 minutes, so a shred order
lands without waiting for someone to run a command. This needs the token on the
device, which the plain CLI avoids — decide per fleet.

```ini
# /etc/systemd/system/velaris-field-contact.service
[Service]
Type=oneshot
User=velaris-field
Environment=VELARIS_FIELD_NODE_DB=/var/lib/velaris-field/node.db
ExecStart=/opt/velaris-field/bin/python -m velaris_shared.field_node \
          --token-file /etc/velaris-field/token contact

# /etc/systemd/system/velaris-field-contact.timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
```

## Exit codes

`0` done · `2` the node refused · `3` the hub ordered a wipe and it is done ·
`4` the hub refused or could not be reached.
