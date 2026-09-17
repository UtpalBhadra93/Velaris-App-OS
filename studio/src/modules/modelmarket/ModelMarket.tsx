/**
 * Model Marketplace (F-07) — the screen for deciding what leaves, and what runs.
 *
 * Four things are invisible without it, and each is a panel:
 *
 *   1. **What a published model actually discloses.** The weights are not the
 *      disclosure; the per-feature MEAN and STANDARD DEVIATION are, because
 *      they are summary statistics of real cases. Publish shows that in words
 *      before it asks anybody to sign, and the checkbox is disabled until the
 *      canary has been run and read.
 *   2. **Whether a stranger's model is any good HERE.** The publisher's AUC is
 *      arithmetic about the publisher's data. The evaluation panel prints the
 *      measured figure first and the claim beside it, labelled — never one in
 *      place of the other.
 *   3. **That "signed" is not "trusted".** The public key travels inside the
 *      file, so a signature proves the bytes match a key and nothing about
 *      whose key it is. The key id is shown for pinning; the word trusted
 *      appears nowhere.
 *   4. **That nothing imported is running.** An installed package is staged,
 *      not active — a risk model lands pending and a pack lands disabled — and
 *      the row says so rather than leaving "installed" to be read as "on".
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  downloadModelPackage,
  evaluateModelPackage,
  getModelPackage,
  importModelPackage,
  installModelPackage,
  listModelPackages,
  listModelSources,
  prepareModelPackage,
  publishModelPackage,
  uninstallModelPackage,
  type ModelCanaryCheck,
  type ModelEvaluation,
  type ModelInstall,
  type ModelPackage,
  type ModelPrepared,
  type ModelSources,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill, Select,
  Spinner, TABLE, Tabs, TextInput,
} from "@shared/components";

type TabKey = "publish" | "published" | "imported";

/** A number the publisher claimed, never printed as if it were measured here. */
function Metric({ label, value, muted }: {
  label: string; value: unknown; muted?: boolean;
}) {
  const text = value === null || value === undefined
    ? "not stated"
    : typeof value === "number" ? value.toFixed(3) : String(value);
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
      <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{label}</span>
      <span style={{ ...MONO, opacity: muted ? 0.7 : 1 }}>{text}</span>
    </div>
  );
}

function CheckList({ checks }: { checks: ModelCanaryCheck[] }) {
  if (!checks.length) return null;
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0", display: "grid",
                 gap: 6 }}>
      {checks.map((c) => (
        <li key={c.check} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <Pill tone={c.ok ? "ok" : c.blocking ? "bad" : "warn"}>
            {c.ok ? "pass" : c.blocking ? "blocks" : "note"}
          </Pill>
          <div>
            <div style={{ ...MONO }}>{c.check}</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {c.detail}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** What leaves with the file, said in words before anybody signs for it. */
function Disclosure({ manifest }: { manifest: Record<string, any> }) {
  const d = manifest?.disclosure;
  if (!d) return null;
  return (
    <Card>
      <h4 style={{ margin: "0 0 8px" }}>What this file carries</h4>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
        {(d.contains || []).map((line: string) => <li key={line}>{line}</li>)}
      </ul>
      {d.contains_no?.length ? (
        <p style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 0 }}>
          It carries no {(d.contains_no as string[]).join(", ")}.
        </p>
      ) : null}
      {d.cohort_size !== null && d.cohort_size !== undefined ? (
        <p style={{ fontSize: 13, marginBottom: 0 }}>
          Learned from <strong>{d.cohort_size}</strong> cases
          {d.cohort_floor ? ` (the floor for publishing is ${d.cohort_floor})` : ""}.
        </p>
      ) : null}
    </Card>
  );
}

// ── publish ──────────────────────────────────────────────────────────────────

function PublishPanel({ onPublished }: { onPublished: () => void }) {
  const [sources, setSources] = useState<ModelSources | null>(null);
  const [kind, setKind] = useState("risk_model");
  const [source, setSource] = useState("");
  const [packageId, setPackageId] = useState("");
  const [prepared, setPrepared] = useState<ModelPrepared | null>(null);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ModelPackage | null>(null);

  useEffect(() => {
    listModelSources().then(setSources).catch((e) => setError(String(e)));
  }, []);

  // The acknowledgement is reset by every change to what is being published.
  // A tick that survived a change of source would be a signature on something
  // the person never read.
  useEffect(() => { setPrepared(null); setAck(false); setResult(null); },
            [kind, source]);

  const doPrepare = async () => {
    setBusy(true); setError(null);
    try { setPrepared(await prepareModelPackage({ kind, source })); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const doPublish = async () => {
    setBusy(true); setError(null);
    try {
      const r = await publishModelPackage({ kind, source, package_id: packageId,
                                            acknowledged: ack });
      setResult(r.package);
      onPublished();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const options = kind === "risk_model"
    ? (sources?.risk_models || []).map((m) => ({
        value: m.source,
        label: `v${m.version} — ${m.status}, ${m.trained_on} cases` }))
    : (sources?.signal_packs || []).map((p) => ({
        value: p.source, label: `${p.name} — ${p.rules} rules` }));

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Card>
        <h4 style={{ margin: "0 0 12px" }}>Share a model</h4>
        <div style={{ display: "grid", gap: 10, maxWidth: 620 }}>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12 }}>Kind</span>
            <Select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="risk_model">Risk model (learned weights)</option>
              <option value="signal_pack">Signal pack (declarative rules)</option>
            </Select>
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12 }}>Source</span>
            <Select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="">Choose…</option>
              {options.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </Select>
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12 }}>Package name</span>
            <TextInput value={packageId} placeholder="acme/triage-risk"
                       onChange={(e) => setPackageId(e.target.value)} />
          </label>
          <div>
            <Button onClick={doPrepare} disabled={!source || busy}>
              Check what it discloses
            </Button>
          </div>
        </div>
      </Card>

      {busy && <Spinner />}
      {error && <ErrorNote>{error}</ErrorNote>}

      {prepared && (
        <>
          <Disclosure manifest={prepared.manifest} />
          <Card>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <h4 style={{ margin: 0 }}>Disclosure checks</h4>
              <Pill tone={prepared.canary.verdict === "pass" ? "ok" : "bad"}>
                {prepared.canary.verdict}
              </Pill>
            </div>
            <CheckList checks={prepared.canary.checks} />
          </Card>
          <Card>
            <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" checked={ack}
                     onChange={(e) => setAck(e.target.checked)} />
              <span style={{ fontSize: 13 }}>
                I have read what this file carries and accept the residual
                disclosure. My name is recorded against this package.
              </span>
            </label>
            <div style={{ marginTop: 10 }}>
              <Button onClick={doPublish} disabled={!ack || !packageId || busy}>
                Publish
              </Button>
            </div>
          </Card>
        </>
      )}

      {result && (
        <Card>
          {result.status === "published" ? (
            <div>
              <Pill tone="ok">published</Pill>
              <span style={{ marginLeft: 8, ...MONO }}>{result.package_id} v{result.version}</span>
            </div>
          ) : (
            <div>
              <Pill tone="bad">refused</Pill>
              {/* The refusal is a kept record, not a toast that vanishes. */}
              <p style={{ fontSize: 13, marginBottom: 0 }}>{result.status_reason}</p>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

// ── the two lists ────────────────────────────────────────────────────────────

function PackageRow({ pkg, onOpen }: { pkg: ModelPackage; onOpen: () => void }) {
  const tone = pkg.status === "published" ? "ok"
    : pkg.status === "quarantined" ? "bad" : "muted";
  return (
    <tr>
      <td style={{ ...MONO }}>{pkg.package_id}</td>
      <td>v{pkg.version}</td>
      <td>{pkg.kind}</td>
      <td><Pill tone={tone}>{pkg.status}</Pill></td>
      <td>
        {/* Signed says the bytes match a key. Whose key is a separate question
            and the screen does not answer it for the operator. */}
        {pkg.signed
          ? <span style={MONO} title="Pin this key id out of band">
              key {pkg.signing_key_id?.slice(0, 12)}…
            </span>
          : <span style={{ color: "var(--text-muted)" }}>unsigned</span>}
      </td>
      <td style={{ ...MONO }}>{pkg.content_sha256.slice(0, 12)}…</td>
      <td><Button onClick={onOpen}>Open</Button></td>
    </tr>
  );
}

function PackageTable({ rows, onOpen }: {
  rows: ModelPackage[]; onOpen: (id: string) => void;
}) {
  if (!rows.length) {
    return <EmptyState title="Nothing here yet"
                       description="Packages appear once one is published or imported." />;
  }
  return (
    <table style={TABLE.table}>
      <thead>
        <tr>
          <th style={TABLE.th}>Package</th><th style={TABLE.th}>Version</th>
          <th style={TABLE.th}>Kind</th><th style={TABLE.th}>Status</th>
          <th style={TABLE.th}>Signature</th><th style={TABLE.th}>Content</th>
          <th style={TABLE.th} />
        </tr>
      </thead>
      <tbody>
        {rows.map((p) => (
          <PackageRow key={p.id} pkg={p} onOpen={() => onOpen(p.id)} />
        ))}
      </tbody>
    </table>
  );
}

function EvaluationCard({ ev }: { ev: ModelEvaluation }) {
  const tone = ev.verdict === "pass" ? "ok"
    : ev.verdict === "fail" ? "bad" : "warn";
  const measured = (ev.measured || {}) as Record<string, any>;
  const declared = (ev.declared || {}) as Record<string, any>;
  return (
    <Card>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <Pill tone={tone}>{ev.verdict}</Pill>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {ev.holdout_size} of this tenant's cases · {ev.positives} bad / {ev.negatives} clean
        </span>
      </div>
      <p style={{ fontSize: 13 }}>{ev.reason}</p>
      {/* Measured first, and labelled. The claim sits beside it, dimmed, and
          never in its place. */}
      <div style={{ display: "grid", gap: 4 }}>
        <Metric label="measured here" value={measured.holdout_auc} />
        <Metric label="claimed by the publisher" value={declared.holdout_auc} muted />
        {measured.fire_rates ? (
          <Metric label="rules that fire on almost everything"
                  value={(measured.noisy || []).join(", ") || "none"} />
        ) : null}
      </div>
    </Card>
  );
}

function PackageDetail({ id, onChanged, onClose }: {
  id: string; onChanged: () => void; onClose: () => void;
}) {
  const [data, setData] = useState<{
    package: ModelPackage; evaluations: ModelEvaluation[]; install: ModelInstall | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setData(await getModelPackage(id)); }
    catch (e) { setError(String(e)); }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); await load(); onChanged(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  if (!data) return <Spinner />;
  const { package: pkg, evaluations, install } = data;
  const latest = evaluations[0];
  const canInstall = latest?.verdict === "pass"
    && (!install || install.status === "uninstalled" || install.status === "shredded");

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <Button onClick={onClose}>Back</Button>
        <strong style={MONO}>{pkg.package_id} v{pkg.version}</strong>
        <Pill tone={pkg.status === "published" ? "ok" : "bad"}>{pkg.status}</Pill>
      </div>
      {error && <ErrorNote>{error}</ErrorNote>}

      <Disclosure manifest={pkg.manifest as Record<string, any>} />

      <Card>
        <h4 style={{ margin: "0 0 8px" }}>The card</h4>
        <div style={{ display: "grid", gap: 4 }}>
          <Metric label="task" value={(pkg.manifest as any)?.task} />
          <Metric label="intended use" value={(pkg.manifest as any)?.intended_use} />
          <Metric label="limitations" value={(pkg.manifest as any)?.limitations} />
          <Metric label="data classes" value={(pkg.data_classes || []).join(", ") || "none"} />
          <Metric label="publisher's cohort" value={pkg.trained_on} muted />
          <Metric label="publisher's holdout AUC"
                  value={(pkg.declared_metrics as any)?.holdout_auc} muted />
        </div>
      </Card>

      {pkg.canary.detail?.checks?.length ? (
        <Card>
          <h4 style={{ margin: 0 }}>Disclosure checks at publish</h4>
          <CheckList checks={pkg.canary.detail.checks} />
        </Card>
      ) : null}

      {pkg.direction === "published" ? (
        <Card>
          <Button disabled={busy || pkg.status !== "published"} onClick={() => act(async () => {
            const f = await downloadModelPackage(pkg.id);
            // The viewer's browser saves it; nothing about the file leaves
            // this install except through this deliberate act.
            const blob = new Blob([JSON.stringify(f.content, null, 2)],
                                  { type: "application/json" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = f.filename;
            a.click();
            URL.revokeObjectURL(a.href);
          })}>Download the file</Button>
        </Card>
      ) : (
        <>
          <Card>
            <h4 style={{ margin: "0 0 8px" }}>Evaluation on this tenant's data</h4>
            <Button disabled={busy}
                    onClick={() => act(() => evaluateModelPackage(pkg.id))}>
              {evaluations.length ? "Re-evaluate" : "Evaluate"}
            </Button>
            <p style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Scored against this tenant's own labelled cases, none of which the
              model has seen.
            </p>
          </Card>
          {evaluations.map((e) => <EvaluationCard key={e.id} ev={e} />)}
          <Card>
            <h4 style={{ margin: "0 0 8px" }}>Install</h4>
            {install && install.status !== "uninstalled" && install.status !== "shredded" ? (
              <div style={{ display: "grid", gap: 8 }}>
                <div>
                  <Pill tone={install.status === "activated" ? "ok" : "warn"}>
                    {install.status}
                  </Pill>
                  {install.status === "staged" && (
                    // "installed" must never be read as "on".
                    <span style={{ marginLeft: 8, fontSize: 13 }}>
                      Installed but not active — switch it on in HxSentinel.
                    </span>
                  )}
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <Button disabled={busy}
                          onClick={() => act(() => uninstallModelPackage(install.id, false))}>
                    Uninstall
                  </Button>
                  <Button disabled={busy}
                          onClick={() => act(() => uninstallModelPackage(install.id, true))}>
                    Uninstall and delete the weights
                  </Button>
                </div>
              </div>
            ) : (
              <>
                <Button disabled={busy || !canInstall}
                        onClick={() => act(() => installModelPackage(pkg.id))}>
                  Install
                </Button>
                {!canInstall && (
                  <p style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    An evaluation must pass on this tenant's data first.
                  </p>
                )}
              </>
            )}
          </Card>
        </>
      )}
    </div>
  );
}

function ImportPanel({ onImported }: { onImported: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Card>
      <h4 style={{ margin: "0 0 8px" }}>Take a package in</h4>
      <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 0 }}>
        Storing it installs nothing and runs nothing. The hash and the signature
        are checked before the contents are read at all.
      </p>
      <textarea value={text} onChange={(e) => setText(e.target.value)}
                rows={6} placeholder="Paste a .hxmodel file"
                style={{ ...FIELD_STYLE, ...MONO, width: "100%", resize: "vertical" }} />
      <div style={{ marginTop: 8 }}>
        <Button disabled={!text.trim() || busy} onClick={async () => {
          setBusy(true); setError(null);
          try { await importModelPackage(text); setText(""); onImported(); }
          catch (e) { setError(String(e)); }
          finally { setBusy(false); }
        }}>Import</Button>
      </div>
      {error && <ErrorNote style={{ marginTop: "var(--space-sm)" }}>{error}</ErrorNote>}
    </Card>
  );
}

export default function ModelMarket() {
  const [tab, setTab] = useState<TabKey>("publish");
  const [rows, setRows] = useState<ModelPackage[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const direction = tab === "published" ? "published" : "imported";
  const load = useCallback(async () => {
    if (tab === "publish") return;
    setLoading(true);
    try { setRows((await listModelPackages(direction)).packages); }
    finally { setLoading(false); }
  }, [tab, direction]);
  useEffect(() => { setOpenId(null); void load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%",
                  overflow: "hidden", boxSizing: "border-box" }}>
      {/* No page title here: `AppLayout` already renders one from `nav-data`,
          so a module adding its own gets two stacked title bars. */}
      <Tabs
        ariaLabel="Model marketplace"
        tabs={[
          { key: "publish", label: "Publish" },
          { key: "published", label: "Published from here" },
          { key: "imported", label: "Imported" },
        ]}
        value={tab}
        onChange={(k) => setTab(k as TabKey)}
      />
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {tab === "publish" && <PublishPanel onPublished={() => setTab("published")} />}
        {tab !== "publish" && (
          openId
            ? <PackageDetail id={openId} onChanged={load} onClose={() => setOpenId(null)} />
            : (
              <div style={{ display: "grid", gap: "var(--space-md)" }}>
                {tab === "imported" && <ImportPanel onImported={load} />}
                <Card>
                  {loading ? <Spinner /> : <PackageTable rows={rows} onOpen={setOpenId} />}
                </Card>
              </div>
            )
        )}
      </div>
    </div>
  );
}
