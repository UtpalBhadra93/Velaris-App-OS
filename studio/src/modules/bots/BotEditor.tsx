/* ═══════════════════════════════════════════════════════════════════
   Bot Editor — writing the container that will run on this host

   The form exists because bot definitions could previously only be created
   through the API, but it is written around one rule the API enforces and the
   UI has to make impossible to be surprised by:

   **Saving an approved bot returns it to draft.** Not a subset of "risky"
   fields — every field. A classification is a thing to get wrong, and getting it
   wrong here means an image, an egress list or a credential set changing under
   an approval that was granted for something else. The banner says so before
   the first keystroke, not in an error afterwards.

   The other two are the server's refusals, shown as guidance rather than
   discovered as 400s: an image must be pinned by digest, and credentials plus
   wildcard egress are refused together.

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useEffect, useState } from "react";

import {
  createBot,
  listBotCredentials,
  updateBot,
  type BotCredential,
  type BotDefinition,
} from "@shared/api/client";
import { Button, Card } from "@shared/components";

const label: React.CSSProperties = {
  display: "block", fontSize: "0.75rem", color: "var(--text-muted)",
  marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.04em",
};
const field: React.CSSProperties = {
  width: "100%", padding: "var(--space-sm)", boxSizing: "border-box",
  background: "var(--surface)", color: "var(--text-primary)",
  border: "1px solid var(--border)", borderRadius: "var(--radius-sm)",
  fontSize: "0.875rem", fontFamily: "inherit",
};
const mono: React.CSSProperties = { ...field, fontFamily: "var(--font-mono, monospace)" };
const row: React.CSSProperties = { marginBottom: "var(--space-md)" };

type Draft = {
  name: string; description: string; image: string; digest: string;
  granted_domains: string; credential_refs: string[];
  timeout_seconds: number; max_concurrency: number; resource_profile: string;
  fanout_input_key: string; fanout_max: number;
  input_schema: string; output_schema: string;
  declared_env: string; declared_command: string;
};

function toDraft(bot: BotDefinition | null): Draft {
  return {
    name: bot?.name ?? "",
    description: bot?.description ?? "",
    image: bot?.image ?? "",
    digest: bot?.digest ?? "",
    granted_domains: (bot?.granted_domains ?? []).join(", "),
    credential_refs: bot?.credential_refs ?? [],
    timeout_seconds: bot?.timeout_seconds ?? 300,
    max_concurrency: bot?.max_concurrency ?? 1,
    resource_profile: bot?.resource_profile ?? "light",
    fanout_input_key: bot?.fanout_input_key ?? "",
    fanout_max: bot?.fanout_max ?? 25,
    input_schema: JSON.stringify(bot?.input_schema ?? {}, null, 2),
    output_schema: JSON.stringify(bot?.output_schema ?? {}, null, 2),
    declared_env: JSON.stringify(bot?.declared_env ?? {}, null, 2),
    declared_command: (bot?.declared_command ?? []).join(" "),
  };
}

function parseJson(raw: string, what: string): Record<string, unknown> {
  const text = raw.trim();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("not an object");
    }
    return parsed as Record<string, unknown>;
  } catch {
    throw new Error(`${what} must be a JSON object.`);
  }
}

export default function BotEditor({ bot, onDone, onCancel }: {
  bot: BotDefinition | null;
  onDone: (saved: BotDefinition) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(bot));
  const [credentials, setCredentials] = useState<BotCredential[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { setDraft(toDraft(bot)); }, [bot]);
  useEffect(() => {
    listBotCredentials().then(setCredentials).catch(() => setCredentials([]));
  }, []);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft(d => ({ ...d, [key]: value }));

  const domains = draft.granted_domains.split(",").map(d => d.trim()).filter(Boolean);
  const wildcard = domains.some(d => d === "*" || d.startsWith("*"));
  const credentialsAndWildcard = wildcard && draft.credential_refs.length > 0;
  const unpinned = !draft.digest.trim().startsWith("sha256:");

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const body = {
        name: draft.name.trim(),
        description: draft.description,
        image: draft.image.trim(),
        digest: draft.digest.trim(),
        granted_domains: domains,
        credential_refs: draft.credential_refs,
        timeout_seconds: Number(draft.timeout_seconds) || 300,
        max_concurrency: Number(draft.max_concurrency) || 1,
        resource_profile: draft.resource_profile,
        fanout_input_key: draft.fanout_input_key.trim() || null,
        fanout_max: Number(draft.fanout_max) || 25,
        input_schema: parseJson(draft.input_schema, "The input schema"),
        output_schema: parseJson(draft.output_schema, "The output schema"),
        declared_env: parseJson(draft.declared_env, "Environment"),
        declared_command: draft.declared_command.trim()
          ? draft.declared_command.trim().split(/\s+/) : [],
      };
      const saved = bot ? await updateBot(bot.id, body) : await createBot(body);
      onDone(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card style={{ marginBottom: "var(--space-xl)" }}>
      <h3 style={{ marginTop: 0 }}>{bot ? `Edit ${bot.name}` : "Register a bot"}</h3>

      {bot?.status === "active" && (
        <p role="note" style={{
          fontSize: "0.8125rem", padding: "var(--space-sm)",
          border: "1px solid var(--warning, var(--border))",
          borderRadius: "var(--radius-sm)", marginTop: 0,
        }}>
          <strong>Saving returns this bot to draft.</strong> It was approved by{" "}
          {bot.approved_by} for the definition as it stands; any change means it
          has to be approved again, by somebody who is not you. Jobs already
          running are left alone — use Disable to stop those.
        </p>
      )}
      {error && <p role="alert" style={{ color: "var(--danger)" }}>{error}</p>}

      <div style={row}>
        <label style={label} htmlFor="bot-name">Name</label>
        <input id="bot-name" style={field} value={draft.name}
               onChange={e => set("name", e.target.value)} />
      </div>
      <div style={row}>
        <label style={label} htmlFor="bot-desc">What it does</label>
        <input id="bot-desc" style={field} value={draft.description}
               onChange={e => set("description", e.target.value)} />
      </div>

      <div style={{ display: "flex", gap: "var(--space-md)", ...row }}>
        <div style={{ flex: 2 }}>
          <label style={label} htmlFor="bot-image">Image</label>
          <input id="bot-image" style={mono} placeholder="registry.example.com/team/bot"
                 value={draft.image} onChange={e => set("image", e.target.value)} />
        </div>
        <div style={{ flex: 3 }}>
          <label style={label} htmlFor="bot-digest">Digest</label>
          <input id="bot-digest" style={mono} placeholder="sha256:…"
                 value={draft.digest} onChange={e => set("digest", e.target.value)} />
          {unpinned && (
            <small style={{ color: "var(--text-muted)" }}>
              Pinned by digest, never by tag — a tag can be repointed at
              different content after the review, which would make the approval
              meaningless.
            </small>
          )}
        </div>
      </div>

      <div style={row}>
        <label style={label} htmlFor="bot-egress">Egress allowlist</label>
        <input id="bot-egress" style={field} placeholder="supplier.example.com, api.example.net"
               value={draft.granted_domains}
               onChange={e => set("granted_domains", e.target.value)} />
        <small style={{ color: "var(--text-muted)" }}>
          Empty means internal only. The container has no route out except these.
        </small>
      </div>

      <div style={row}>
        <span style={label}>Credentials leased per job</span>
        {credentials.length === 0 ? (
          <small style={{ color: "var(--text-muted)" }}>
            None stored yet. Add one below the bot list — the value is encrypted
            under this tenant's key and is never shown again, to anyone.
          </small>
        ) : credentials.map(c => (
          <label key={c.id} style={{ display: "block", fontSize: "0.875rem" }}>
            <input
              type="checkbox"
              checked={draft.credential_refs.includes(c.ref)}
              onChange={e => set("credential_refs", e.target.checked
                ? [...draft.credential_refs, c.ref]
                : draft.credential_refs.filter(r => r !== c.ref))}
            />{" "}
            {c.ref} <span style={{ color: "var(--text-muted)" }}>→ ${c.env_name}</span>
          </label>
        ))}
        {credentialsAndWildcard && (
          <p role="alert" style={{ color: "var(--danger)", fontSize: "0.8125rem" }}>
            A bot holding credentials cannot also have wildcard egress: with a
            live password and permission to reach anywhere, it is an
            exfiltration tool. Name the hosts it needs. Approval will refuse
            this combination.
          </p>
        )}
      </div>

      <div style={{ display: "flex", gap: "var(--space-md)", ...row }}>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-timeout">Timeout (seconds)</label>
          <input id="bot-timeout" style={field} type="number" min={1} max={3600}
                 value={draft.timeout_seconds}
                 onChange={e => set("timeout_seconds", Number(e.target.value))} />
        </div>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-conc">Concurrent jobs</label>
          <input id="bot-conc" style={field} type="number" min={1} max={32}
                 value={draft.max_concurrency}
                 onChange={e => set("max_concurrency", Number(e.target.value))} />
        </div>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-profile">Resource profile</label>
          <select id="bot-profile" style={field} value={draft.resource_profile}
                  onChange={e => set("resource_profile", e.target.value)}>
            <option value="light">light — a script</option>
            <option value="browser">browser — Playwright</option>
          </select>
        </div>
      </div>

      <div style={{ display: "flex", gap: "var(--space-md)", ...row }}>
        <div style={{ flex: 2 }}>
          <label style={label} htmlFor="bot-fanout">Fan out over (input key)</label>
          <input id="bot-fanout" style={field} placeholder="rows"
                 value={draft.fanout_input_key}
                 onChange={e => set("fanout_input_key", e.target.value)} />
          <small style={{ color: "var(--text-muted)" }}>
            Empty means one job per run. Named, one run starts a job per item in
            that list — which is why it is part of what gets approved.
          </small>
        </div>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-fanout-max">Fan-out limit</label>
          <input id="bot-fanout-max" style={field} type="number" min={1} max={500}
                 value={draft.fanout_max}
                 onChange={e => set("fanout_max", Number(e.target.value))} />
          <small style={{ color: "var(--text-muted)" }}>
            A larger batch is refused, not trimmed.
          </small>
        </div>
      </div>

      <div style={{ display: "flex", gap: "var(--space-md)", ...row }}>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-in">Input schema (JSON)</label>
          <textarea id="bot-in" style={{ ...mono, minHeight: 110 }}
                    value={draft.input_schema}
                    onChange={e => set("input_schema", e.target.value)} />
        </div>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-out">Output schema (JSON)</label>
          <textarea id="bot-out" style={{ ...mono, minHeight: 110 }}
                    value={draft.output_schema}
                    onChange={e => set("output_schema", e.target.value)} />
          <small style={{ color: "var(--text-muted)" }}>
            The result is checked against this before it touches a case.
          </small>
        </div>
      </div>

      <div style={{ display: "flex", gap: "var(--space-md)", ...row }}>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-env">Environment (JSON, non-secret)</label>
          <textarea id="bot-env" style={{ ...mono, minHeight: 80 }}
                    value={draft.declared_env}
                    onChange={e => set("declared_env", e.target.value)} />
          <small style={{ color: "var(--text-muted)" }}>
            Never a password: this is stored in the clear on the definition and
            is identical for every job. Passwords are credentials, leased per job.
          </small>
        </div>
        <div style={{ flex: 1 }}>
          <label style={label} htmlFor="bot-cmd">Command (optional)</label>
          <input id="bot-cmd" style={mono} value={draft.declared_command}
                 onChange={e => set("declared_command", e.target.value)} />
        </div>
      </div>

      <div style={{ display: "flex", gap: "var(--space-sm)" }}>
        <Button disabled={busy || !draft.name.trim() || !draft.image.trim()}
                onClick={save}>
          {bot ? "Save (returns to draft)" : "Create draft"}
        </Button>
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
      </div>
    </Card>
  );
}
