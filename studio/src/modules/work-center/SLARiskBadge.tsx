// Statistical SLA risk (#40 / report §4.5).
//
// Answers "how unusual is this case's current age, given how long cases like
// it have historically taken?" — percentile arithmetic, no model, no training.
// A case past the 85th percentile of its own history is very likely to breach
// and can be flagged before any SLA timer fires.
//
// The important behaviour is the SILENCE: below the minimum sample size the
// API refuses to answer, and this badge renders nothing rather than showing a
// confident-looking number derived from a handful of cases. Most case types
// will show nothing for a while, and that is correct.
import React, { useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface Grain {
  status: string;
  percentile: number | null;
  at_risk: boolean;
  samples: number;
  median_seconds?: number;
  p85_seconds?: number;
}

interface Prediction {
  at_risk: boolean;
  case: Grain;
  stage: Grain;
}

function human(seconds?: number): string {
  if (!seconds || seconds <= 0) return "—";
  const h = seconds / 3600;
  if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

export default function SLARiskBadge({ caseId }: { caseId: string }) {
  const [p, setP] = useState<Prediction | null>(null);

  useEffect(() => {
    let stop = false;
    (async () => {
      try {
        const r = await apiFetch(`/api/v1/cases/${caseId}/sla/prediction`);
        if (r.ok && !stop) setP(await r.json());
      } catch { /* prediction is advisory — never block the view */ }
    })();
    return () => { stop = true; };
  }, [caseId]);

  if (!p) return null;

  // Prefer the stage grain when it can answer — "late for this stage" is more
  // actionable than "late overall" — and fall back to the case grain.
  const grain = p.stage?.status === "ok" ? p.stage : p.case;
  if (grain?.status !== "ok" || grain.percentile === null) return null;

  const pct = grain.percentile;
  const colour = grain.at_risk
    ? "var(--status-failed)"
    : pct >= 60
      ? "var(--status-warning, #d98c00)"
      : "var(--text-muted)";

  return (
    <span
      title={
        `${pct.toFixed(0)}% of comparable cases finished faster than this one has been open. ` +
        `Median ${human(grain.median_seconds)}, 85th percentile ${human(grain.p85_seconds)}, ` +
        `from ${grain.samples} historical case(s).`
      }
      style={{
        fontSize: 10, fontFamily: "var(--font-mono)", padding: "1px 6px",
        borderRadius: 3, border: `1px solid ${colour}`, color: colour,
      }}
    >
      {grain.at_risk ? "⚠ " : ""}p{pct.toFixed(0)}
    </span>
  );
}
