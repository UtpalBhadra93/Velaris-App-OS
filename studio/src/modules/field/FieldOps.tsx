/**
 * Field operations (F-10) — the conflict queue, and the fleet.
 *
 * The queue is the reason this screen exists. Deciding between a field value
 * and the hub's is CASE work: the person who understands the case chooses, and
 * they need no authority over the node fleet to do it. Nothing in this list
 * decided itself — a disagreement on personal data always lands here, and the
 * only thing that clears one is somebody choosing.
 *
 * Two things the screen is careful about:
 *
 *   1. **Both values are shown, side by side, with which side is which.** A
 *      queue that showed only the incoming value would be asking somebody to
 *      approve a change they cannot see the alternative to.
 *   2. **A stale node is called stale on the fleet list.** A node past its
 *      snapshot age has stopped being allowed to act, and an operator reading
 *      "last seen 3 days ago" without that word would draw the wrong conclusion.
 *
 * Layout follows the platform shell (`/help`, `/work-center`).
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  listCaseTypes,
  listFieldConflicts,
  listFieldNodes,
  registerFieldNode,
  resolveFieldConflict,
  type FieldConflict,
  type FieldNode,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, MONO, Pill, SectionTitle, Select,
  Spinner, TABLE, Tabs, TextInput, type PillTone,
} from "@shared/components";

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };

/** The incoming value, as rows. A `JSON.stringify` blob is the thing somebody
 *  has to read against the hub's, and braces and quotes are in the way of it. */
function FieldValue({ value }: { value: unknown }) {
  const entries =
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value as Record<string, unknown>)
      : null;
  if (!entries || !entries.length) {
    return (
      <div style={{ ...MONO, marginTop: 6, color: "var(--text-primary)", wordBreak: "break-all" }}>
        {value == null ? "—" : JSON.stringify(value)}
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 14px",
                  marginTop: 6 }}>
      {entries.map(([k, v]) => (
        <React.Fragment key={k}>
          <span style={{ ...MONO, color: "var(--text-muted)" }}>{k}</span>
          <span style={{ ...MONO, color: "var(--text-primary)", wordBreak: "break-word" }}>
            {typeof v === "object" ? JSON.stringify(v) : String(v)}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
}

function ConflictCard({ conflict, onResolved }: {
  conflict: FieldConflict; onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const decide = async (resolution: "keep_field" | "keep_hub") => {
    setBusy(true); setError(null);
    try { await resolveFieldConflict(conflict.id, resolution); onResolved(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card style={{ borderLeft: "3px solid var(--status-running)" }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone="warn">needs a person</Pill>
        <strong style={{ ...MONO, fontSize: 13, color: "var(--text-primary)" }}>
          {conflict.target ?? conflict.kind}
        </strong>
        <span style={muted}>
          case {(conflict.case_id ?? "—").slice(0, 8)} · seq {conflict.seq}
        </span>
      </div>
      <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "8px 0 0", lineHeight: 1.55 }}>
        {conflict.reason}
      </p>

      {/* Both values, side by side. A queue showing only the incoming value asks
          somebody to approve a change they cannot see the alternative to. */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
                    gap: "var(--space-md)", margin: "var(--space-md) 0 0" }}>
        <div style={{ border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
                      padding: "var(--space-md)", background: "var(--bg-elevated)" }}>
          <div style={{ ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                        letterSpacing: "0.06em", fontSize: 11 }}>
            from the field node
          </div>
          <FieldValue value={conflict.field_value} />
          {conflict.node_time && (
            <div style={{ ...muted, marginTop: 8 }}>
              {/* The node's own clock. Never used for ordering — but it is what
                  the field worker will describe. */}
              node clock: {new Date(conflict.node_time).toLocaleString()}
            </div>
          )}
        </div>
        <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)",
                      padding: "var(--space-md)", background: "var(--bg-elevated)" }}>
          <div style={{ ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                        letterSpacing: "0.06em", fontSize: 11 }}>
            on the hub
          </div>
          {/* The hub's actual value, not a description of it. This panel used to
              read "the value currently on the case", which is the very thing the
              header comment says a conflict queue must not do — it asked
              somebody to choose against a value they could not see. */}
          {conflict.hub_value_known
            ? <FieldValue value={conflict.hub_value} />
            : <div style={{ ...MONO, marginTop: 6, color: "var(--text-muted)" }}>
                {/* Absent is not empty, and they are drawn differently. */}
                the hub holds no value for this field
              </div>}
          <div style={{ ...muted, marginTop: 8 }}>
            stands unless you choose the field's
          </div>
        </div>
      </div>

      <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)",
                    paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
        <Button disabled={busy} onClick={() => decide("keep_field")}>
          Take the field's value
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => decide("keep_hub")}>
          Keep the hub's
        </Button>
      </div>
      {error && <ErrorNote style={{ marginTop: "var(--space-md)" }}>{error}</ErrorNote>}
    </Card>
  );
}

function NodeRow({ node }: { node: FieldNode }) {
  const stale = node.staleness?.stale;
  const tone: PillTone = node.status === "shredded" ? "bad"
    : node.status === "shred_ordered" ? "bad"
    : stale ? "warn" : "ok";
  return (
    <tr style={TABLE.tr}>
      <td style={{ ...TABLE.td, ...MONO }}>{node.slug}</td>
      <td style={TABLE.td}>{node.display_name}</td>
      <td style={TABLE.td}><Pill tone={tone}>{node.status}</Pill></td>
      <td style={{ ...TABLE.td, ...MONO }}>{node.ceiling}</td>
      <td style={{ ...TABLE.td, ...MONO, color: "var(--text-muted)" }}>
        key {node.key_id.slice(0, 10)}…
      </td>
      <td style={TABLE.td}>
        {/* A node past its snapshot age has STOPPED being allowed to act.
            "Last seen 3 days ago" without that word invites the wrong
            conclusion. */}
        {stale
          ? <Pill tone="warn">locked out — {node.staleness?.reason}</Pill>
          : <span style={muted}>
              {node.last_contact_at
                ? new Date(node.last_contact_at).toLocaleString()
                : "never in contact"}
            </span>}
      </td>
    </tr>
  );
}


/** Enrol a node.
 *
 * **Studio never generates the keypair.** It would be one line, and it would
 * destroy the thing the key is for: a hub that has once held a node's private
 * key has pinned nothing, because it can forge that node's whole sub-chain
 * afterwards. The key is generated on the node and its PUBLIC half is pasted
 * here — so this form asks for something the operator has to go and fetch, on
 * purpose.
 *
 * The charter is required rather than defaulted to "everything". It is checked
 * again at SYNC, so a node dispatched nothing can still be told to hand work
 * back — an empty charter would accept all of it.
 */
function EnrolNode({ onEnrolled }: { onEnrolled: (notice: string) => void }) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [ceiling, setCeiling] = useState("internal");
  const [maxAge, setMaxAge] = useState("48");
  const [types, setTypes] = useState<{ id: string; name: string }[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listCaseTypes(1)
      .then((r) => setTypes(((r as { items?: { id: string; name: string }[] }).items ?? [])
        .map((c) => ({ id: c.id, name: c.name }))))
      .catch(() => {});
  }, []);

  // The server decodes this and insists on 32 bytes; saying so before the round
  // trip is cheaper than a 422 that reads like a server fault.
  const keyBytes = (() => {
    const t = key.trim();
    if (!t) return null;
    try { return atob(t).length; } catch { return -1; }
  })();
  const keyProblem = keyBytes === null ? null
    : keyBytes === -1 ? "That is not valid base64."
    : keyBytes !== 32 ? `An Ed25519 public key is 32 bytes; this decodes to ${keyBytes}. `
                      + "A key of the wrong length is a key from the wrong algorithm."
    : null;

  const ready = slug.trim() && name.trim() && !keyProblem && keyBytes === 32
                && chosen.length > 0 && Number(maxAge) > 0;

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const node = await registerFieldNode({
        slug: slug.trim(), display_name: name.trim(), public_key: key.trim(),
        charter: { case_type_ids: chosen },
        ceiling, snapshot_max_age_hours: Number(maxAge),
      });
      // Defensive on `key_id`: the write has already happened by the time this
      // line runs, so a missing field must not surface as a failed enrolment.
      const pinned = node?.key_id ? `${node.key_id.slice(0, 10)}…` : "shown in the fleet above";
      setSlug(""); setName(""); setKey(""); setChosen([]);
      // Handed UP rather than held here. Reloading the fleet remounts this
      // component, and a confirmation that dies with it takes the instruction
      // to check the fingerprint with it — which is the one thing enrolment
      // asks a person to actually do.
      onEnrolled(`${node?.display_name || slug.trim()} is enrolled. Its key is `
                 + `pinned as ${pinned} — check that against what the node reports.`);
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <SectionTitle>Enrol a node</SectionTitle>
      <p style={{ ...muted, margin: "0 0 var(--space-md)", lineHeight: 1.55 }}>
        Generate the keypair <strong>on the node</strong> and paste its public
        half here. Velaris never generates it: a hub that has held a node&rsquo;s
        private key can forge that node&rsquo;s whole log, so it would be pinning
        nothing.
      </p>

      <div style={{ display: "grid", gap: "var(--space-md)",
                    gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
        <label style={{ display: "grid", gap: 4 }}>
          <span style={muted}>Slug</span>
          <TextInput value={slug} onChange={(e) => setSlug(e.target.value)}
                     placeholder="van-201" />
        </label>
        <label style={{ display: "grid", gap: 4 }}>
          <span style={muted}>Display name</span>
          <TextInput value={name} onChange={(e) => setName(e.target.value)}
                     placeholder="Van 201 — North engineer" />
        </label>
        <label style={{ display: "grid", gap: 4 }}>
          <span style={muted}>Data ceiling</span>
          <Select value={ceiling} onChange={(e) => setCeiling(e.target.value)}>
            <option value="none">none — no case data leaves the hub</option>
            <option value="internal">internal</option>
            <option value="personal">personal</option>
          </Select>
        </label>
        <label style={{ display: "grid", gap: 4 }}>
          <span style={muted}>Snapshot max age (hours)</span>
          <TextInput type="number" min={1} value={maxAge}
                     onChange={(e) => setMaxAge(e.target.value)} />
        </label>
      </div>

      <label style={{ display: "grid", gap: 4, marginTop: "var(--space-md)" }}>
        <span style={muted}>Node public key — raw base64, not PEM</span>
        <TextInput value={key} onChange={(e) => setKey(e.target.value)}
                   placeholder="MCowBQYDK2VwAyEA…"
                   style={{ ...MONO, width: "100%" }} />
      </label>
      {keyProblem && (
        <div style={{ ...muted, color: "var(--status-failed)", marginTop: 6 }}>
          {keyProblem}
        </div>
      )}

      <div style={{ marginTop: "var(--space-md)" }}>
        <div style={muted}>
          Charter — the case types this node may hold. Checked again at sync, so
          a node that sends work about anything else is refused.
        </div>
        {types.length === 0 ? (
          <div style={{ ...muted, marginTop: 6 }}>No case types to choose from.</div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
            {types.map((t) => {
              const on = chosen.includes(t.id);
              return (
                <button key={t.id} type="button"
                  onClick={() => setChosen(on ? chosen.filter((x) => x !== t.id)
                                              : [...chosen, t.id])}
                  style={{
                    padding: "4px 12px", borderRadius: 20, cursor: "pointer",
                    fontSize: 12, border: "1px solid var(--border-default)",
                    background: on ? "var(--accent)" : "var(--bg-elevated)",
                    color: on ? "var(--text-inverse)" : "var(--text-secondary)",
                    fontWeight: on ? 600 : 400,
                  }}>{t.name}</button>
              );
            })}
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                    marginTop: "var(--space-md)", paddingTop: "var(--space-md)",
                    borderTop: "1px solid var(--border-subtle)" }}>
        <Button disabled={busy || !ready} onClick={submit}>Enrol the node</Button>
        {!ready && !keyProblem && (
          <span style={muted}>
            A slug, a name, a 32-byte key and at least one case type.
          </span>
        )}
      </div>

      {error && <ErrorNote style={{ marginTop: "var(--space-md)" }}>{error}</ErrorNote>}
    </Card>
  );
}

export default function FieldOps() {
  const [tab, setTab] = useState<"conflicts" | "nodes">("conflicts");
  const [conflicts, setConflicts] = useState<FieldConflict[]>([]);
  const [nodes, setNodes] = useState<FieldNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Lives above the tab body, so a reload cannot take it with it.
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [c, n] = await Promise.all([listFieldConflicts(), listFieldNodes()]);
      setConflicts(c.conflicts); setNodes(n.nodes);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const stale = nodes.filter(n => n.staleness?.stale).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`. */}

      {/* The standing fact, on every tab: a node past its snapshot age has
          stopped being allowed to act, and its work is being refused right now.
          An operator who only ever opens the conflict queue would never learn
          that from a list of conflicts, because a locked-out node produces
          none. */}
      {!loading && stale > 0 && (
        <Banner
          tone="warn"
          title={`${stale} node${stale === 1 ? " is" : "s are"} locked out.`}
        >
          Past its snapshot age a node must fail closed, and the hub refuses work
          it recorded after that point. Nothing it does now will merge.
        </Banner>
      )}

      <Tabs
        ariaLabel="Field operations"
        tabs={[
          { key: "conflicts" as const, label: "Needs a decision",
            badge: conflicts.length || undefined },
          { key: "nodes" as const, label: "Nodes", badge: nodes.length || undefined },
        ]}
        value={tab}
        onChange={(k) => setTab(k as "conflicts" | "nodes")}
      />

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {error && <ErrorNote style={{ marginBottom: "var(--space-md)" }}>{error}</ErrorNote>}
        {notice && (
          <Card style={{ marginBottom: "var(--space-md)",
                         borderLeft: "3px solid var(--status-completed)",
                         display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
            <Pill tone="ok">enrolled</Pill>
            <span style={{ fontSize: 13, color: "var(--text-primary)", flex: 1 }}>{notice}</span>
            <Button size="sm" variant="ghost" onClick={() => setNotice(null)}>Dismiss</Button>
          </Card>
        )}

        {loading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
            <Spinner size={24} />
          </div>
        ) : (
          <>
            {tab === "conflicts" && (
              conflicts.length === 0
                ? <EmptyState
                    title="Nothing waiting"
                    description="Every field sync merged cleanly, or somebody has already decided." />
                : <div style={{ display: "grid", gap: "var(--space-md)" }}>
                    {conflicts.map((c) => (
                      <ConflictCard key={c.id} conflict={c} onResolved={load} />
                    ))}
                  </div>
            )}

            {tab === "nodes" && (
              <>
              <Card style={{ padding: 0, overflow: "hidden", marginBottom: "var(--space-md)" }}>
                <div style={{ padding: "var(--space-lg) var(--space-lg) 0" }}>
                  <SectionTitle
                    right={<span style={muted}>{nodes.length} enrolled</span>}
                  >
                    Node fleet
                  </SectionTitle>
                </div>
                {nodes.length === 0
                  ? <EmptyState title="No nodes registered"
                                description="A node is enrolled with a pinned key and a charter." />
                  : <div style={{ overflowX: "auto", padding: "0 var(--space-lg) var(--space-lg)" }}>
                      <table style={TABLE.table}>
                        <thead>
                          <tr>
                            <th style={TABLE.th}>Node</th>
                            <th style={TABLE.th}>Name</th>
                            <th style={TABLE.th}>Status</th>
                            <th style={TABLE.th}>Ceiling</th>
                            <th style={TABLE.th}>Key</th>
                            <th style={TABLE.th}>Last contact</th>
                          </tr>
                        </thead>
                        <tbody>{nodes.map((n) => <NodeRow key={n.id} node={n} />)}</tbody>
                      </table>
                    </div>}
              </Card>
              <EnrolNode onEnrolled={(n) => { setNotice(n); void load(); }} />
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
