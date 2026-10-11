/**
 * The rep desk's action rail — Oct item 2.
 *
 * A rep on a call acts for the customer from here instead of switching
 * modules. Nothing is re-implemented: each form calls the API the action
 * already lives on, which enforces its own rules again. What this adds is the
 * answer to "may I, now?" before the rep promises anything.
 *
 * Two things it is careful about:
 *
 *   1. **A blocked action stays on screen, disabled, with its reason.** Hiding
 *      it would leave a rep unsure whether the platform can do it at all, and
 *      the reason's KIND is shown because the next step differs: verify the
 *      caller, ask an administrator, or tell the customer they opted out.
 *   2. **The rail is re-read after every action and every verification**, so a
 *      caller verified to Full, or a STOP that arrived mid-call, changes what
 *      is offered without a reload.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  createEvent,
  engageActionRail,
  engageCaptureConsent,
  initiatePaymentRequest,
  performCaseAction,
  replyToConversation,
  startCaseSession,
  type ActionRailView,
  type RailAction,
  type RailBlockKind,
  type RailCase,
} from "@shared/api/client";
import {
  Button, ErrorNote, FIELD_STYLE, Pill, Select, SubPanel, TextInput, type PillTone,
} from "@shared/components";

const KIND_LABEL: Record<RailBlockKind, string> = {
  tier: "verify the caller",
  consent: "no consent",
  privilege: "not your role",
  prerequisite: "not set up",
};
const KIND_TONE: Record<RailBlockKind, PillTone> = {
  tier: "warn", consent: "bad", privilege: "muted", prerequisite: "muted",
};

const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const row: React.CSSProperties = {
  display: "flex", gap: "var(--space-sm)", alignItems: "center", flexWrap: "wrap",
};

/** An industry rail's preset for one action's form (Oct item 5). */
function preset(rail: ActionRailView, key: RailAction["key"], field: string): string | undefined {
  const v = rail.actions.find((a) => a.key === key)?.defaults?.[field];
  return v === undefined ? undefined : String(v);
}

function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { style, ...rest } = props;
  return <textarea rows={3} {...rest}
                   style={{ ...FIELD_STYLE, width: "100%", resize: "vertical", ...style }} />;
}

/** Cases the action can be taken on, with the one chosen. */
function CasePick({ cases, value, onChange }: {
  cases: RailCase[]; value: string; onChange: (id: string) => void;
}) {
  return (
    <Select aria-label="Case" value={value} onChange={(e) => onChange(e.target.value)}>
      {cases.map((c) => (
        <option key={c.id} value={c.id}>{c.case_number} — {c.status}</option>
      ))}
    </Select>
  );
}

/** Reply and document request both send on a conversation. Opted-out ones are
 *  listed but not selectable, so the rep sees WHY a channel is missing. */
function SendForm({ rail, initial, onDone }: {
  rail: ActionRailView; initial: string; onDone: (msg: string) => void;
}) {
  const convs = rail.cases.flatMap((c) => c.conversations.map((v) => ({ ...v, case: c })));
  const first = convs.find((v) => !v.opted_out);
  const [conv, setConv] = useState(first?.id ?? "");
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const chosen = convs.find((v) => v.id === conv);
  return (
    <>
      <div style={row}>
        <Select aria-label="Conversation" value={conv} onChange={(e) => setConv(e.target.value)}>
          {convs.map((v) => (
            <option key={v.id} value={v.id} disabled={v.opted_out}>
              {v.case.case_number} · {v.channel} {v.peer_hint ?? ""}
              {v.opted_out ? " — opted out" : ""}
            </option>
          ))}
        </Select>
        {chosen?.template_only && <Pill tone="warn">template only</Pill>}
      </div>
      <TextArea aria-label="Message" value={text} onChange={(e) => setText(e.target.value)}
                style={{ marginTop: 8 }} />
      <div style={{ ...row, marginTop: 8 }}>
        <Button size="sm" disabled={!conv || !text.trim()} onClick={async () => {
          setError(null);
          try { await replyToConversation(conv, { body: text }); onDone("Sent."); }
          catch (e) { setError(String(e)); }
        }}>Send</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

function PaymentForm({ rail, onDone }: { rail: ActionRailView; onDone: (msg: string) => void }) {
  const payable = rail.cases.filter((c) => c.payment_steps.length);
  const [caseId, setCaseId] = useState(payable[0]?.id ?? "");
  const steps = payable.find((c) => c.id === caseId)?.payment_steps ?? [];
  const [step, setStep] = useState(steps[0]?.id ?? "");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState(preset(rail, "payment", "currency") ?? "gbp");
  const [description, setDescription] = useState(preset(rail, "payment", "description") ?? "Payment");
  const [link, setLink] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cents = Math.round(Number(amount) * 100);
  return (
    <>
      <div style={row}>
        <CasePick cases={payable} value={caseId} onChange={(id) => {
          setCaseId(id);
          setStep(payable.find((c) => c.id === id)?.payment_steps[0]?.id ?? "");
        }} />
        <Select aria-label="Payment step" value={step} onChange={(e) => setStep(e.target.value)}>
          {steps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
      </div>
      <div style={{ ...row, marginTop: 8 }}>
        <TextInput aria-label="Amount" inputMode="decimal" placeholder="Amount"
                   value={amount} onChange={(e) => setAmount(e.target.value)}
                   style={{ width: 120 }} />
        <Select aria-label="Currency" value={currency} onChange={(e) => setCurrency(e.target.value)}>
          {Array.from(new Set(["gbp", "eur", "usd", currency])).map((c) =>
            <option key={c} value={c}>{c.toUpperCase()}</option>)}
        </Select>
        <TextInput aria-label="Description" value={description}
                   onChange={(e) => setDescription(e.target.value)} style={{ flex: 1 }} />
        <Button size="sm" disabled={!step || !(cents > 0)} onClick={async () => {
          setError(null);
          try {
            const out = await initiatePaymentRequest(caseId, step, cents, currency, description);
            if (!out.checkout_url) throw new Error("The payment provider returned no link.");
            setLink(out.checkout_url);
          } catch (e) { setError(String(e)); }
        }}>Create link</Button>
      </div>
      {link && (
        <SubPanel tone="ok">
          <span style={{ fontSize: 13, wordBreak: "break-all" }}>{link}</span>
          {/* Creating the link is half the action; it reaches the customer on
              their channel, or the rep reads it out when they have none. */}
          {rail.cases.some((c) => c.conversations.some((v) => !v.opted_out)) ? (
            <div style={{ marginTop: 8 }}>
              <SendForm rail={rail} onDone={onDone}
                        initial={`Here is the secure link to make your payment: ${link}`} />
            </div>
          ) : (
            <p style={{ ...muted, margin: "8px 0 0" }}>
              No channel they can be messaged on — read the link out, or email it from the case.
            </p>
          )}
        </SubPanel>
      )}
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

function CallbackForm({ rail, onDone }: { rail: ActionRailView; onDone: (msg: string) => void }) {
  const [when, setWhen] = useState("");
  const [minutes, setMinutes] = useState(Number(preset(rail, "callback", "minutes") ?? 15));
  const [caseId, setCaseId] = useState(rail.cases[0]?.id ?? "");
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <div style={row}>
        <TextInput aria-label="Callback time" type="datetime-local" value={when}
                   onChange={(e) => setWhen(e.target.value)} />
        <Select aria-label="Length" value={minutes}
                onChange={(e) => setMinutes(Number(e.target.value))}>
          {[15, 30, 45, 60].map((m) => <option key={m} value={m}>{m} min</option>)}
        </Select>
        {rail.cases.length > 0 && (
          <CasePick cases={rail.cases} value={caseId} onChange={setCaseId} />
        )}
        <Button size="sm" disabled={!when} onClick={async () => {
          setError(null);
          const start = new Date(when);
          try {
            await createEvent({
              title: "Customer callback", starts_at: start.toISOString(),
              ends_at: new Date(start.getTime() + minutes * 60_000).toISOString(),
              case_id: caseId || null, visibility: "private",
            });
            onDone(`Callback booked for ${start.toLocaleString()}.`);
          } catch (e) { setError(String(e)); }
        }}>Book</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

function SessionForm({ rail, onDone }: { rail: ActionRailView; onDone: (msg: string) => void }) {
  const [caseId, setCaseId] = useState(rail.cases[0]?.id ?? "");
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <div style={row}>
        <CasePick cases={rail.cases} value={caseId} onChange={setCaseId} />
        <Button size="sm" disabled={!caseId} onClick={async () => {
          setError(null);
          try {
            await startCaseSession(caseId, {
              title: preset(rail, "live_session", "title") ?? "Live session with the customer" });
            onDone("Live session started — open it from the case to run a KYC check.");
          } catch (e) { setError(String(e)); }
        }}>Start</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

const STATE_TONE: Record<string, PillTone> = { granted: "ok", withdrawn: "bad", none: "muted" };

function ConsentForm({ rail, customerId, onDone }: {
  rail: ActionRailView; customerId: string; onDone: (msg: string) => void;
}) {
  const kinds = (["email", "phone"] as const).filter((k) => rail.consent[k]);
  const [kind, setKind] = useState<"email" | "phone">(kinds[0] ?? "email");
  const [purpose, setPurpose] = useState(
    preset(rail, "consent", "purpose") ?? rail.capturable_purposes[0] ?? "marketing");
  const [error, setError] = useState<string | null>(null);
  const view = rail.consent[kind];
  const record = async (action: "granted" | "withdrawn") => {
    setError(null);
    try {
      await engageCaptureConsent(customerId, {
        interaction_id: rail.interaction_id, purpose, action, subject_kind: kind });
      onDone(`Recorded: ${purpose} ${action}, on their word on this call.`);
    } catch (e) { setError(String(e)); }
  };
  return (
    <>
      {view?.purposes && (
        <div style={{ ...row, marginBottom: 8 }}>
          {Object.entries(view.purposes).map(([p, s]) => (
            <Pill key={p} tone={STATE_TONE[s]}>{p}: {s}</Pill>
          ))}
        </div>
      )}
      <div style={row}>
        <Select aria-label="Recorded against" value={kind}
                onChange={(e) => setKind(e.target.value as "email" | "phone")}>
          {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
        </Select>
        <Select aria-label="Purpose" value={purpose} onChange={(e) => setPurpose(e.target.value)}>
          {rail.capturable_purposes.map((p) => <option key={p} value={p}>{p}</option>)}
        </Select>
        <Button size="sm" onClick={() => record("granted")}>They agree</Button>
        <Button size="sm" variant="secondary" onClick={() => record("withdrawn")}>
          They withdraw
        </Button>
      </div>
      <p style={{ ...muted, margin: "8px 0 0" }}>
        Ask them on the call. The record names you, this call and its verification.
      </p>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

function FollowUpForm({ rail, onDone }: { rail: ActionRailView; onDone: (msg: string) => void }) {
  const [caseId, setCaseId] = useState(rail.cases[0]?.id ?? "");
  const [text, setText] = useState(preset(rail, "follow_up", "note") ?? "");
  const [when, setWhen] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <CasePick cases={rail.cases} value={caseId} onChange={setCaseId} />
      <TextArea aria-label="Follow-up" placeholder="What needs doing next"
                value={text} onChange={(e) => setText(e.target.value)} style={{ marginTop: 8 }} />
      <div style={{ ...row, marginTop: 8 }}>
        <TextInput aria-label="Remind me at" type="datetime-local" value={when}
                   onChange={(e) => setWhen(e.target.value)} />
        <Button size="sm" disabled={!caseId || !text.trim()} onClick={async () => {
          setError(null);
          try {
            // An internal note — `add_note` can never reach the customer.
            await performCaseAction(caseId, "add_note", { body: `Follow-up: ${text}` });
            if (when) {
              const start = new Date(when);
              await createEvent({
                title: "Follow-up", description: text, case_id: caseId,
                starts_at: start.toISOString(),
                ends_at: new Date(start.getTime() + 15 * 60_000).toISOString(),
                visibility: "private",
              });
            }
            onDone(when ? "Follow-up noted and reminder set." : "Follow-up noted on the case.");
          } catch (e) { setError(String(e)); }
        }}>Save</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: 8 }}>{error}</ErrorNote>}
    </>
  );
}

function Blocked({ action }: { action: RailAction }) {
  return (
    <div style={{ display: "grid", gap: 4, marginTop: 4 }}>
      {action.blocked_by.map((b, i) => (
        <div key={i} style={row}>
          <Pill tone={KIND_TONE[b.kind]}>{KIND_LABEL[b.kind]}</Pill>
          <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{b.reason}</span>
        </div>
      ))}
    </div>
  );
}

export default function ActionRail({ customerId, interactionId, tier }: {
  customerId: string; interactionId: string;
  /** Only to re-read the rail when verification moves. */
  tier: string;
}) {
  const [rail, setRail] = useState<ActionRailView | null>(null);
  // Oct item 5: which industry rail — "" lets the server choose by case type.
  const [railKey, setRailKey] = useState("");
  const [open, setOpen] = useState<RailAction["key"] | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setRail(await engageActionRail(customerId, interactionId, railKey || null)); setError(null); }
    catch (e) { setError(String(e)); }
  }, [customerId, interactionId, railKey]);
  useEffect(() => { void load(); }, [load, tier]);

  const finished = (msg: string) => { setDone(msg); setOpen(null); void load(); };

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!rail) return null;

  const form = (key: RailAction["key"]) => {
    switch (key) {
      case "reply":
        return <SendForm rail={rail} initial={preset(rail, "reply", "message") ?? ""} onDone={finished} />;
      case "document_request":
        // `{portal_link}` is the ONLY placeholder a rail may use; nothing about
        // the customer is ever filled into a message.
        return <SendForm rail={rail} onDone={finished}
                         initial={(preset(rail, "document_request", "message")
                                   ?? "Please upload the documents we discussed. Sign in here: {portal_link}")
                                  .split("{portal_link}").join(rail.portal_link ?? "")} />;
      case "payment": return <PaymentForm rail={rail} onDone={finished} />;
      case "callback": return <CallbackForm rail={rail} onDone={finished} />;
      case "live_session": return <SessionForm rail={rail} onDone={finished} />;
      case "consent": return <ConsentForm rail={rail} customerId={customerId} onDone={finished} />;
      case "follow_up": return <FollowUpForm rail={rail} onDone={finished} />;
    }
  };

  return (
    <div style={{ display: "grid", gap: 10 }}>
      {(rail.available_rails?.length ?? 0) > 0 && (
        <div style={row}>
          <Select aria-label="Industry rail" value={railKey}
                  onChange={(e) => { setOpen(null); setRailKey(e.target.value); }}>
            <option value="">{rail.rail ? `Automatic — ${rail.rail.name}` : "Standard rail"}</option>
            {rail.available_rails!.map((r) => (
              <option key={r.key} value={r.key}>{r.name} ({r.industry})</option>
            ))}
          </Select>
        </div>
      )}
      {rail.rail?.guidance && (
        <SubPanel tone="info" style={{ marginTop: 0 }}>
          <span style={{ fontSize: 13 }}>{rail.rail.guidance}</span>
        </SubPanel>
      )}
      {done && <SubPanel tone="ok" style={{ marginTop: 0 }}>
        <span style={{ fontSize: 13 }}>{done}</span>
      </SubPanel>}
      {rail.actions.map((a) => (
        <div key={a.key} style={{ paddingBottom: 10,
                                  borderBottom: "1px solid var(--border-subtle)" }}>
          <div style={row}>
            <Button size="sm" variant={open === a.key ? "primary" : "secondary"}
                    disabled={!a.enabled}
                    onClick={() => { setDone(null); setOpen(open === a.key ? null : a.key); }}>
              {a.label}
            </Button>
            {a.notes.map((n, i) => <span key={i} style={muted}>{n}</span>)}
          </div>
          {a.guidance && <div style={{ ...muted, marginTop: 4 }}>{a.guidance}</div>}
          {!a.enabled && <Blocked action={a} />}
          {a.enabled && open === a.key && (
            <div style={{ marginTop: 10 }}>{form(a.key)}</div>
          )}
        </div>
      ))}
    </div>
  );
}
