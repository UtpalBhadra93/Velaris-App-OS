/**
 * Calendar — people time.
 *
 * `business_calendars` describes the ORGANISATION (what the SLA clock counts as
 * a working hour). This screen adds the half that never existed: when a PERSON
 * is away, and who covers for them. Assignment and escalation read it, which is
 * where it pays — before this, both picked people who were on holiday.
 *
 * Two things this UI is careful about:
 *
 *   1. **A colleague sees busy, never why.** The server strips `kind` and `note`
 *      from anybody who is not the person, their manager or an admin, so a row
 *      without those fields is not a bug — it is the privacy filter working.
 *   2. **Leave is a fact; working hours are an assumption.** The Team tab shows
 *      leave. Working patterns live under Business Calendars, where they read as
 *      configuration rather than as somebody's whereabouts.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  approveAbsence,
  cancelAbsence,
  cancelEvent,
  updateAbsence,
  createAbsence,
  createCalendar,
  createEvent,
  createMeetingRoom,
  getMeetSessionToken,
  inviteGuests,
  getWorkPattern,
  listAbsences,
  listCalendars,
  listEvents,
  proposeSlots,
  skipOccurrence,
  importCalendarIcs,
  listCalendarSubscriptions,
  createCalendarSubscription,
  updateCalendarSubscription,
  deleteCalendarSubscription,
  refreshCalendarSubscription,
  exportCalendarIcs,
  type CalendarSubscription,
  updateEvent,
  saveWorkPattern,
  updateCalendar,
  type Absence,
  type CalendarEvent,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner, Tabs } from "@shared/components";
import MeetRoom from "@shared/components/MeetRoom";
import { ALL_DAYS, DAY_NAMES, TZ_GROUPS } from "@shared/constants/calendar";

const S: Record<string, React.CSSProperties> = {
  page:   { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  body:   { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
          // mixes the two for one value, and the warning is right — the
          // shorthand below would win on a rerender.
  label:  { fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", textTransform: "uppercase" as const, letterSpacing: "0.06em", marginBottom: 4, display: "block" },
  input:  { width: "100%", padding: "8px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  badge:  { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const, fontFamily: "var(--font-mono)" },
  grid2:  { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 },
  note:   { fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 },
};

const STATUS_COLOUR: Record<string, string> = {
  requested: "#f59e0b", approved: "#22c55e", cancelled: "#64748b",
};

function day(value: string): string {
  try { return new Date(value).toLocaleDateString(); } catch { return value; }
}

function AbsenceRow({ a, onApprove, onCancel, onEdit, busy }: {
  a: Absence;
  onApprove?: (id: string) => void;
  onCancel?: (id: string) => void;
  onEdit?: (a: Absence) => void;
  busy: boolean;
}) {
  return (
    <Card style={{ marginBottom: "var(--space-sm)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
            {a.user_id}
            <span style={{ ...S.badge, marginLeft: 10, background: (STATUS_COLOUR[a.status] ?? "#64748b") + "22", color: STATUS_COLOUR[a.status] ?? "#64748b" }}>
              {a.status}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
            {day(a.starts_at)} → {day(a.ends_at)}
            {a.all_day ? " · all day" : ""}
            {a.delegate_user_id ? ` · covered by ${a.delegate_user_id}` : ""}
          </div>
          {/* Absent for a colleague — the server strips it. That is the privacy
              filter, not a missing field. */}
          {a.kind && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
              {a.kind}{a.note ? ` — ${a.note}` : ""}
            </div>
          )}
          {!a.kind && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4, fontStyle: "italic" }}>
              busy — the reason is private
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {onEdit && a.status !== "cancelled" && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onEdit(a)}>Edit</Button>
          )}
          {onApprove && a.status === "requested" && (
            <Button size="sm" disabled={busy} onClick={() => onApprove(a.id)}>Approve</Button>
          )}
          {onCancel && a.status !== "cancelled" && (
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => onCancel(a.id)}>Cancel</Button>
          )}
        </div>
      </div>
    </Card>
  );
}

/* ── One meeting, wherever it is shown ──────────────────────────── */
//
// Meetings live on the **calendar**, not in a list under the form that made
// them — the same way Teams or Outlook works, and for the same reason: you look
// for a meeting on the day it happens, not in the order it was created. So this
// is the day panel's row, and it carries everything you would want at the
// moment you find it: join, edit, invite, skip, cancel.

function MeetingEntry({ ev, onChanged }: { ev: CalendarEvent; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [edit, setEdit] = useState<CalendarEvent | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [room, setRoom] = useState<{ url: string; token: string; title?: string } | null>(null);

  const past = new Date(ev.ends_at) < new Date();
  const time = `${new Date(ev.starts_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    + ` – ${new Date(ev.ends_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

  const act = async (fn: () => Promise<unknown>, said?: string) => {
    setBusy(true); setMsg(null);
    try {
      const out: any = await fn();
      if (out?.join_url) setMsg(`Room ready — ${out.join_url}`);
      else if (out?.sent) setMsg(`Invited ${out.sent.length}${out.failed?.length ? `, ${out.failed.length} failed` : ""}.`);
      else if (said) setMsg(said);
      onChanged();
    } catch (e: any) { setMsg(e?.message ?? "That did not work"); }
    finally { setBusy(false); }
  };

  /** Join: an external link opens where it lives; a room this platform created
   *  opens **in place**, because sending somebody to another screen to join a
   *  meeting they can already see is the thing calendars are supposed to fix. */
  const join = async () => {
    if (ev.meeting_url) { window.open(ev.meeting_url, "_blank", "noreferrer"); return; }
    setBusy(true); setMsg(null);
    try {
      // LiveKit closes a room when the last person leaves. That is right for a
      // case session — a thing that happened — and wrong for a meeting, which
      // recurs: "Session is not active" is not an answer anybody wants when they
      // step back into their own stand-up. So joining a room that has finished
      // starts a fresh one, and the server reuses a live one rather than
      // refusing.
      let sessionId = ev.meet_session_id;
      if (!sessionId || ev.room_status !== "active") {
        const refreshed = await createMeetingRoom(ev.id);
        sessionId = refreshed.meet_session_id;
        if (refreshed.join_url) {
          window.open(refreshed.join_url, "_blank", "noreferrer");
          onChanged();
          return;
        }
      }
      if (!sessionId) { setMsg("No room for this meeting."); return; }
      const t = await getMeetSessionToken(sessionId);
      setRoom({ url: (t as any).url, token: (t as any).token, title: ev.title });
      onChanged();
    } catch (e: any) { setMsg(e?.message ?? "Could not join"); }
    finally { setBusy(false); }
  };

  // The room is a **modal**, not a panel under the grid. A meeting takes over
  // the screen everywhere else, and rendering it below the calendar meant
  // scrolling away from the thing you were joining to see the people in it.
  const roomModal = room ? (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Meeting room: ${ev.title}`}
      onKeyDown={e => { if (e.key === "Escape") setRoom(null); }}
      style={{
        position: "fixed", inset: 0, zIndex: 1200,
        background: "rgba(0,0,0,0.72)", display: "flex",
        alignItems: "center", justifyContent: "center", padding: 24,
      }}
    >
      <div style={{
        background: "var(--bg-panel)", border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)", width: "min(1100px, 100%)",
        maxHeight: "92vh", overflow: "auto", padding: 16,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>{ev.title}</div>
          {/* Closing is not leaving: the room stays live for whoever else is in
              it, exactly as it does in the meeting itself. */}
          <button onClick={() => setRoom(null)} aria-label="Close"
            style={{ background: "none", border: "none", cursor: "pointer", fontSize: 20, color: "var(--text-muted)" }}>
            ✕
          </button>
        </div>
        {/* No `title` here: the modal header already carries it, and MeetRoom
            renders its own — two copies of the same words. */}
        <MeetRoom url={room.url} token={room.token}
                  onLeave={() => setRoom(null)} />
      </div>
    </div>
  ) : null;

  return (
    <>
    {roomModal}
    <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "8px 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div style={{ cursor: "pointer" }} onClick={() => setOpen(o => !o)}>
          <div style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 600 }}>
            {ev.title}
            {ev.is_recurring && (
              <span style={{ ...S.badge, marginLeft: 8, background: "var(--accent-dim)", color: "var(--accent)" }}>
                repeats {ev.recurrence?.freq}
              </span>
            )}
            {ev.visibility === "private" && (
              <span style={{ ...S.badge, marginLeft: 6, background: "#64748b22", color: "#94a3b8" }}>private</span>
            )}
            {past && <span style={{ ...S.badge, marginLeft: 6, background: "#64748b22", color: "#94a3b8" }}>past</span>}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 2 }}>
            {time}
            {ev.attendee_user_ids.length ? ` · ${ev.attendee_user_ids.join(", ")}` : ""}
            {ev.external_emails?.length ? ` · ${ev.external_emails.length} guest(s)` : ""}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {(ev.meeting_url || ev.meet_session_id) && !past && (
            <Button size="sm" disabled={busy} onClick={join}>
              {ev.meeting_url || ev.room_status === "active" ? "Join" : "Start"}
            </Button>
          )}
          <Button size="sm" variant="secondary" onClick={() => setOpen(o => !o)}>
            {open ? "Close" : "Details"}
          </Button>
        </div>
      </div>

      {msg && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6, wordBreak: "break-all" as const }}>{msg}</div>}

      {open && !edit && (
        <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" as const }}>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => setEdit(ev)}>Edit</Button>
          {!ev.meeting_url && !ev.meet_session_id && (
            <Button size="sm" variant="secondary" disabled={busy}
              onClick={() => act(
                () => createMeetingRoom(ev.id),
                ev.external_emails?.length
                  // Their invitation went out before this room existed, so it
                  // named no way in.
                  ? "Room ready. Your guests were invited before it existed — press Invite guests to send them the link."
                  : undefined,
              )}>Add room</Button>
          )}
          {ev.external_emails?.length > 0 && (
            <Button size="sm" variant="secondary" disabled={busy}
              onClick={() => act(() => inviteGuests(ev.id))}>Invite guests</Button>
          )}
          {ev.is_recurring && ev.occurrence_date && (
            <Button size="sm" variant="secondary" disabled={busy}
              onClick={() => act(() => skipOccurrence(ev.id, ev.occurrence_date!), "That date is skipped.")}>
              Skip this date
            </Button>
          )}
          <Button size="sm" variant="danger" disabled={busy}
            onClick={() => act(() => cancelEvent(ev.id),
              ev.is_recurring ? "The whole series is cancelled." : "Cancelled.")}>
            {/* "Cancel meeting", not "Cancel": the edit form below has a Cancel
                that discards a change, and the two must not read the same when
                one of them calls the meeting off. */}
            Cancel {ev.is_recurring ? "series" : "meeting"}
          </Button>
        </div>
      )}

      {edit && (
        <div style={{ marginTop: 10 }}>
          <span style={S.label}>Title</span>
          <input style={S.input} value={edit.title}
            onChange={e => setEdit({ ...edit, title: e.target.value })} />
          <div style={S.grid2}>
            <div>
              <span style={S.label}>Starts</span>
              <input type="datetime-local" style={S.input} value={edit.starts_at.slice(0, 16)}
                onChange={e => setEdit({ ...edit, starts_at: new Date(e.target.value).toISOString() })} />
            </div>
            <div>
              <span style={S.label}>Ends</span>
              <input type="datetime-local" style={S.input} value={edit.ends_at.slice(0, 16)}
                onChange={e => setEdit({ ...edit, ends_at: new Date(e.target.value).toISOString() })} />
            </div>
          </div>
          <span style={S.label}>Meeting link</span>
          <input style={S.input} value={edit.meeting_url ?? ""}
            onChange={e => setEdit({ ...edit, meeting_url: e.target.value })} />

          <span style={S.label}>Attendees (comma separated user ids)</span>
          <input style={S.input} value={(edit.attendee_user_ids ?? []).join(", ")}
            onChange={e => setEdit({
              ...edit,
              attendee_user_ids: e.target.value.split(",").map(x => x.trim()).filter(Boolean),
            })} />

          <span style={S.label}>Guests (external email addresses)</span>
          <input style={S.input} value={(edit.external_emails ?? []).join(", ")}
            onChange={e => setEdit({
              ...edit,
              external_emails: e.target.value.split(",").map(x => x.trim()).filter(Boolean),
            })} />
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -6, marginBottom: 8 }}>
            Changing the list does not send anything. Press <b>Invite guests</b>
            afterwards — a save that mailed people would fire every time somebody
            fixed a typo in the title.
          </div>
          {ev.is_recurring && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
              This is a series — the change moves <b>every</b> occurrence. To drop one
              date, use <b>Skip this date</b>.
            </div>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" disabled={busy} onClick={async () => {
              await act(() => updateEvent(edit.id, {
                title: edit.title, starts_at: edit.starts_at,
                ends_at: edit.ends_at, meeting_url: edit.meeting_url || null,
                attendee_user_ids: edit.attendee_user_ids ?? [],
                external_emails: edit.external_emails ?? [],
              }), "Saved.");
              setEdit(null);
            }}>Save</Button>
            <Button size="sm" variant="secondary" onClick={() => setEdit(null)}>Cancel</Button>
          </div>
        </div>
      )}
    </div>
    </>
  );
}


/* ── Month grid ────────────────────────────────────────────────── */
//
// The view people mean when they say "a calendar": a month at a glance, with
// leave and meetings on the same squares. Both are the same fact to whoever is
// looking — that person cannot take a third thing that day — so showing them in
// two separate places would make the reader do the merge in their head.

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function startOfMonth(d: Date): Date { return new Date(d.getFullYear(), d.getMonth(), 1); }
function addMonths(d: Date, n: number): Date { return new Date(d.getFullYear(), d.getMonth() + n, 1); }
function ymd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** The 42 cells of a Monday-first month grid, including the neighbouring days
 *  that fill the first and last weeks. Six rows always, so the grid does not
 *  change height as you page through the year. */
function monthCells(month: Date): Date[] {
  const first = startOfMonth(month);
  const lead = (first.getDay() + 6) % 7;            // Monday = 0
  const start = new Date(first);
  start.setDate(first.getDate() - lead);
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return d;
  });
}

/** Does [start, end) touch this calendar day, in the reader's own timezone? */
function touchesDay(startISO: string, endISO: string, day: Date): boolean {
  const dayStart = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  const dayEnd = new Date(dayStart.getTime() + 24 * 3600 * 1000);
  return new Date(startISO) < dayEnd && new Date(endISO) > dayStart;
}

function MonthTab() {
  const [month, setMonth] = useState(() => startOfMonth(new Date()));
  const [absences, setAbsences] = useState<Absence[]>([]);
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    const cells = monthCells(month);
    const from = cells[0].toISOString();
    const to = new Date(cells[41].getTime() + 24 * 3600 * 1000).toISOString();
    try {
      const [a, e] = await Promise.all([
        listAbsences({ start: from, end: to }),
        listEvents({ start: from, end: to }),
      ]);
      setAbsences(a.filter(x => x.status !== "cancelled"));
      setEvents(e);
    } catch (ex: any) {
      setErr(ex?.message ?? "Could not load the month");
    } finally { setLoading(false); }
  }, [month]);
  useEffect(() => { load(); }, [load]);

  const todayKey = ymd(new Date());
  const monthLabel = month.toLocaleDateString(undefined, { month: "long", year: "numeric" });

  const forDay = (d: Date) => ({
    away: absences.filter(a => touchesDay(a.starts_at, a.ends_at, d)),
    meetings: events.filter(e => touchesDay(e.starts_at, e.ends_at, d)),
  });

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <Button size="sm" variant="secondary" onClick={() => setMonth(m => addMonths(m, -1))}>‹</Button>
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)", minWidth: 190 }}>{monthLabel}</div>
        <Button size="sm" variant="secondary" onClick={() => setMonth(m => addMonths(m, 1))}>›</Button>
        <Button size="sm" variant="secondary" onClick={() => setMonth(startOfMonth(new Date()))}>Today</Button>
        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)", display: "flex", gap: 12 }}>
          <span><span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: "#22c55e", marginRight: 4 }} />away (approved)</span>
          <span><span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: "#f59e0b", marginRight: 4 }} />requested</span>
          <span><span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: "var(--accent)", marginRight: 4 }} />meeting</span>
        </span>
      </div>

      {err && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "#ef444422", color: "#ef4444" }}>{err}</div>}
      {loading && <Spinner size={28} />}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 1, background: "var(--border-subtle)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
        {WEEKDAYS.map(w => (
          <div key={w} style={{ background: "var(--bg-elevated)", padding: "6px 8px", fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.06em" }}>{w}</div>
        ))}
        {monthCells(month).map(d => {
          const key = ymd(d);
          const { away, meetings } = forDay(d);
          const outside = d.getMonth() !== month.getMonth();
          return (
            <div key={key} onClick={() => setPicked(key === picked ? null : key)} style={{
              background: key === picked ? "var(--accent-dim)" : "var(--bg-card)",
              minHeight: 92, padding: 6, cursor: "pointer",
              opacity: outside ? 0.45 : 1,
              outline: key === todayKey ? "2px solid var(--accent)" : "none",
              outlineOffset: -2,
            }}>
              <div style={{ fontSize: 11, fontWeight: key === todayKey ? 700 : 500, color: "var(--text-secondary)", marginBottom: 4 }}>
                {d.getDate()}
              </div>
              {away.slice(0, 2).map(a => (
                <div key={a.id} title={a.user_id} style={{
                  fontSize: 10, padding: "1px 4px", borderRadius: 3, marginBottom: 2,
                  background: (a.status === "approved" ? "#22c55e" : "#f59e0b") + "22",
                  color: a.status === "approved" ? "#22c55e" : "#f59e0b",
                  whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis",
                }}>{a.user_id}</div>
              ))}
              {meetings.slice(0, 2).map(e => (
                <div key={e.id} title={e.title} style={{
                  fontSize: 10, padding: "1px 4px", borderRadius: 3, marginBottom: 2,
                  background: "var(--accent-dim)", color: "var(--accent)",
                  whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis",
                }}>{e.title}</div>
              ))}
              {(away.length + meetings.length) > 4 && (
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>+{away.length + meetings.length - 4} more</div>
              )}
            </div>
          );
        })}
      </div>

      {picked && (() => {
        const d = new Date(picked + "T00:00:00");
        const { away, meetings } = forDay(d);
        return (
          <Card style={{ marginTop: "var(--space-lg)" }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 10 }}>
              {d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}
            </div>
            {away.length === 0 && meetings.length === 0 && (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Nothing that day.</div>
            )}
            {away.map(a => (
              <div key={a.id} style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
                <strong style={{ color: "var(--text-primary)" }}>{a.user_id}</strong> — away ({a.status})
                {a.delegate_user_id ? `, covered by ${a.delegate_user_id}` : ""}
                {/* Absent for a colleague: the server strips it. */}
                {a.kind ? ` · ${a.kind}` : ""}
              </div>
            ))}
            {meetings.map(e => (
              <MeetingEntry key={`${e.id}-${e.occurrence_date ?? ""}`} ev={e} onChanged={load} />
            ))}
          </Card>
        );
      })()}
    </div>
  );
}


/* ── Team ──────────────────────────────────────────────────────── */

function TeamTab() {
  const [rows, setRows] = useState<Absence[]>([]);
  const [editing, setEditing] = useState<Absence | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try { setRows(await listAbsences()); }
    catch (e: any) { setErr(e?.message ?? "Could not load absences"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (fn: (id: string) => Promise<unknown>, id: string) => {
    setBusy(true); setErr(null);
    try { await fn(id); await load(); }
    catch (e: any) { setErr(e?.message ?? "That did not work"); }
    finally { setBusy(false); }
  };

  const upcoming = rows.filter(r => r.status !== "cancelled");

  return (
    <div>
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={S.note}>
          Who is away over the next 90 days. <strong style={{ color: "var(--text-primary)" }}>Only approved leave moves work</strong> —
          a request changes nothing until somebody approves it, so asking for leave can never
          quietly empty your own queue. Assignment skips people on approved leave, and hands a
          named target over to their nominated cover.
        </div>
      </Card>

      {err && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "#ef444422", color: "#ef4444" }}>{err}</div>}
      {loading && <Spinner size={28} />}
      {!loading && upcoming.length === 0 && (
        <EmptyState title="Nobody is away" description="Absences recorded here are read by assignment and escalation." />
      )}
      {editing && (
        <Card style={{ marginBottom: "var(--space-md)", border: "1px solid var(--accent)" }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>
            Editing {editing.user_id}
          </div>
          <div style={{ fontSize: 11, color: "#f59e0b", marginBottom: 10 }}>
            Any change sends it back for approval. Approved leave moves other people's
            work, so changing its dates needs the same second look the approval was for.
          </div>
          <div style={S.grid2}>
            <div>
              <span style={S.label}>First day away</span>
              <input type="date" style={S.input} value={editing.starts_at.slice(0, 10)}
                onChange={e => setEditing({ ...editing, starts_at: new Date(`${e.target.value}T00:00:00Z`).toISOString() })} />
            </div>
            <div>
              <span style={S.label}>Last day away</span>
              <input type="date" style={S.input} value={editing.ends_at.slice(0, 10)}
                onChange={e => setEditing({ ...editing, ends_at: new Date(`${e.target.value}T23:59:00Z`).toISOString() })} />
            </div>
          </div>
          <span style={S.label}>Cover</span>
          <input style={S.input} value={editing.delegate_user_id ?? ""} placeholder="who picks up their work"
            onChange={e => setEditing({ ...editing, delegate_user_id: e.target.value })} />
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" disabled={busy} onClick={async () => {
              setBusy(true); setErr(null);
              try {
                await updateAbsence(editing.id, {
                  starts_at: editing.starts_at, ends_at: editing.ends_at,
                  delegate_user_id: editing.delegate_user_id || null,
                });
                setEditing(null); await load();
              } catch (e: any) { setErr(e?.message ?? "Could not save"); }
              finally { setBusy(false); }
            }}>Save</Button>
            <Button size="sm" variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
          </div>
        </Card>
      )}

      {upcoming.map(a => (
        <AbsenceRow key={a.id} a={a} busy={busy}
          onEdit={setEditing}
          onApprove={id => act(approveAbsence, id)}
          onCancel={id => act(cancelAbsence, id)} />
      ))}
    </div>
  );
}

/* ── Record leave ──────────────────────────────────────────────── */

function RecordTab({ onDone }: { onDone: () => void }) {
  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({
    user_id: "", kind: "holiday", starts_on: today, ends_on: today,
    delegate_user_id: "", note: "",
  });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const submit = async () => {
    setBusy(true); setMsg(null);
    try {
      // A day is inclusive: "off on the 3rd" ends at the end of the 3rd, not at
      // midnight when it began.
      await createAbsence({
        user_id: form.user_id || undefined,
        kind: form.kind,
        starts_at: new Date(`${form.starts_on}T00:00:00Z`).toISOString(),
        ends_at: new Date(`${form.ends_on}T23:59:00Z`).toISOString(),
        all_day: true,
        delegate_user_id: form.delegate_user_id || null,
        note: form.note,
      });
      setMsg({ text: "Recorded. It stays requested until somebody else approves it.", ok: true });
      onDone();
    } catch (e: any) {
      setMsg({ text: e?.message ?? "Could not record that", ok: false });
    } finally { setBusy(false); }
  };

  return (
    <div style={{ maxWidth: 620 }}>
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={S.note}>
          Leave for yourself, or for somebody you manage. It is <strong style={{ color: "var(--text-primary)" }}>never
          approved by whoever recorded it</strong> — approved leave reroutes other people's work,
          so a second person signs it off.
        </div>
      </Card>

      <span style={S.label}>Person (blank = you)</span>
      <input style={S.input} value={form.user_id} placeholder="user id"
        onChange={e => setForm(f => ({ ...f, user_id: e.target.value }))} />

      <span style={S.label}>Kind</span>
      <select style={S.input} value={form.kind}
        onChange={e => setForm(f => ({ ...f, kind: e.target.value }))}>
        {["holiday", "sick", "training", "other"].map(k => <option key={k} value={k}>{k}</option>)}
      </select>

      <div style={S.grid2}>
        <div>
          <span style={S.label}>First day away</span>
          <input type="date" style={S.input} value={form.starts_on}
            onChange={e => setForm(f => ({ ...f, starts_on: e.target.value }))} />
        </div>
        <div>
          <span style={S.label}>Last day away</span>
          <input type="date" style={S.input} value={form.ends_on}
            onChange={e => setForm(f => ({ ...f, ends_on: e.target.value }))} />
        </div>
      </div>

      <span style={S.label}>Cover (optional)</span>
      <input style={S.input} value={form.delegate_user_id} placeholder="who picks up their work"
        onChange={e => setForm(f => ({ ...f, delegate_user_id: e.target.value }))} />
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -6, marginBottom: 10 }}>
        Named here rather than on the person: cover belongs to this absence, and December's
        deputy need not be July's.
      </div>

      <span style={S.label}>Note (optional — private)</span>
      <input style={S.input} value={form.note}
        onChange={e => setForm(f => ({ ...f, note: e.target.value }))} />
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -6, marginBottom: 14 }}>
        Visible to this person, their manager and an admin. Colleagues see only that they are busy.
      </div>

      {msg && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: msg.ok ? "#22c55e22" : "#ef444422", color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.text}</div>}
      <Button disabled={busy} onClick={submit}>{busy ? "Recording…" : "Record leave"}</Button>
    </div>
  );
}

/* ── Business calendars + working patterns ─────────────────────── */

function CalendarsTab() {
  const [calendars, setCalendars] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<any | null>(null);
  const [form, setForm] = useState({ name: "", timezone: "UTC", work_days: [1, 2, 3, 4, 5], work_start_hour: 9, work_end_hour: 17, description: "" });
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Working pattern for one person — the org calendar's counterpart.
  const [patternUser, setPatternUser] = useState("");
  const [pattern, setPattern] = useState<any | null>(null);
  const [patternMsg, setPatternMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try { setCalendars(await listCalendars()); }
    catch (e: any) { setErr(e?.message ?? "Could not load calendars"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const openNew = () => {
    setForm({ name: "", timezone: "UTC", work_days: [1, 2, 3, 4, 5], work_start_hour: 9, work_end_hour: 17, description: "" });
    setEditing({});
  };
  const openEdit = (c: any) => {
    setForm({
      name: c.name, timezone: c.timezone, work_days: c.work_days || [1, 2, 3, 4, 5],
      work_start_hour: c.work_start_hour ?? 9, work_end_hour: c.work_end_hour ?? 17,
      description: c.description || "",
    });
    setEditing(c);
  };
  const toggleDay = (d: number) => setForm(f => ({
    ...f, work_days: f.work_days.includes(d) ? f.work_days.filter(x => x !== d) : [...f.work_days, d].sort(),
  }));

  const save = async () => {
    setSaving(true); setErr(null);
    try {
      if (editing?.id) await updateCalendar(editing.id, form);
      else await createCalendar(form as any);
      setEditing(null); await load();
    } catch (e: any) { setErr(e?.message ?? "Could not save"); }
    finally { setSaving(false); }
  };

  const loadPattern = async () => {
    setPatternMsg(null);
    try { setPattern(await getWorkPattern(patternUser)); }
    catch (e: any) { setPatternMsg(e?.message ?? "Could not read that pattern"); }
  };
  const savePattern = async () => {
    if (!pattern) return;
    setPatternMsg(null);
    try {
      await saveWorkPattern({
        user_id: patternUser, work_days: pattern.work_days,
        work_start_hour: pattern.work_start_hour, work_end_hour: pattern.work_end_hour,
        timezone: pattern.timezone,
      });
      setPatternMsg("Saved.");
    } catch (e: any) { setPatternMsg(e?.message ?? "Could not save"); }
  };

  return (
    <div>
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={S.note}>
          <strong style={{ color: "var(--text-primary)" }}>Business calendars</strong> define what counts as a
          working hour for SLA calculations: "resolve within 8 business hours" counts only hours
          inside the window, and weekends and holidays do not count. Set the timezone explicitly —
          create one calendar per region if the team spans several.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Working hours are not availability.</strong> They are an
          assumption about a normal week; an absence is a fact somebody approved. Assignment reads
          the absence and never these hours, or a case arriving at 22:00 would be assigned to nobody.
        </div>
      </Card>

      {err && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "#ef444422", color: "#ef4444" }}>{err}</div>}

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-lg)" }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{calendars.length} calendar{calendars.length !== 1 ? "s" : ""}</span>
        <Button size="sm" onClick={openNew}>+ New calendar</Button>
      </div>

      {loading && <Spinner size={28} />}
      {!loading && calendars.length === 0 && (
        <EmptyState title="No calendars" description="Create at least one so SLAs can calculate business hours correctly." />
      )}

      {calendars.map((c: any) => (
        <Card key={c.id} style={{ marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{c.name}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {c.timezone} · {c.work_start_hour}:00–{c.work_end_hour}:00
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4, display: "flex", gap: 4 }}>
                {ALL_DAYS.map(d => (
                  <span key={d} style={{ padding: "1px 5px", borderRadius: 3, background: (c.work_days || []).includes(d) ? "var(--accent-dim)" : "var(--bg-elevated)", color: (c.work_days || []).includes(d) ? "var(--accent)" : "var(--text-muted)" }}>
                    {DAY_NAMES[d]}
                  </span>
                ))}
              </div>
              {c.description && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{c.description}</div>}
            </div>
            <Button size="sm" variant="secondary" onClick={() => openEdit(c)}>Edit</Button>
          </div>
        </Card>
      ))}

      {/* Per-person pattern */}
      <Card style={{ marginTop: "var(--space-xl)" }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>
          One person's hours
        </div>
        <div style={{ ...S.note, marginBottom: 12 }}>
          Only for somebody whose week differs from the organisation's. No pattern means they
          follow the tenant's calendar, which is what almost everybody should be.
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <input style={{ ...S.input, marginBottom: 0 }} value={patternUser} placeholder="user id"
            onChange={e => setPatternUser(e.target.value)} />
          <Button size="sm" variant="secondary" disabled={!patternUser} onClick={loadPattern}>Load</Button>
        </div>

        {pattern && (
          <div style={{ marginTop: 14 }}>
            <span style={S.label}>Working days</span>
            <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
              {ALL_DAYS.map(d => (
                <button key={d} onClick={() => setPattern((p: any) => ({
                  ...p, work_days: (p.work_days || []).includes(d)
                    ? p.work_days.filter((x: number) => x !== d)
                    : [...(p.work_days || []), d].sort(),
                }))} style={{
                  width: 36, height: 32, borderRadius: 4, border: "1px solid var(--border-default)",
                  cursor: "pointer", fontSize: 11, fontFamily: "var(--font-mono)", fontWeight: 600,
                  background: (pattern.work_days || []).includes(d) ? "var(--accent)" : "transparent",
                  color: (pattern.work_days || []).includes(d) ? "#fff" : "var(--text-secondary)",
                }}>{DAY_NAMES[d]}</button>
              ))}
            </div>
            <div style={S.grid2}>
              <div>
                <span style={S.label}>Start hour</span>
                <input type="number" min={0} max={23} style={S.input} value={pattern.work_start_hour ?? 9}
                  onChange={e => setPattern((p: any) => ({ ...p, work_start_hour: parseInt(e.target.value) || 0 }))} />
              </div>
              <div>
                <span style={S.label}>End hour</span>
                <input type="number" min={1} max={24} style={S.input} value={pattern.work_end_hour ?? 17}
                  onChange={e => setPattern((p: any) => ({ ...p, work_end_hour: parseInt(e.target.value) || 0 }))} />
              </div>
            </div>
            <span style={S.label}>Timezone</span>
            <select style={S.input} value={pattern.timezone ?? "UTC"}
              onChange={e => setPattern((p: any) => ({ ...p, timezone: e.target.value }))}>
              {TZ_GROUPS.map(g => (
                <optgroup key={g.label} label={g.label}>
                  {g.zones.map(tz => <option key={tz} value={tz}>{tz}</option>)}
                </optgroup>
              ))}
            </select>
            {patternMsg && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>{patternMsg}</div>}
            <Button size="sm" onClick={savePattern}>Save pattern</Button>
          </div>
        )}
        {!pattern && patternMsg && <div style={{ fontSize: 12, color: "#ef4444", marginTop: 8 }}>{patternMsg}</div>}
      </Card>

      {/* Create/edit modal */}
      {editing !== null && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 480, maxHeight: "80vh", overflow: "auto" }}>
            <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between" }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{editing?.id ? "Edit calendar" : "New calendar"}</div>
              <button onClick={() => setEditing(null)} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 18, color: "var(--text-muted)" }}>✕</button>
            </div>
            <div style={{ padding: "var(--space-lg)" }}>
              <span style={S.label}>Name</span>
              <input style={S.input} value={form.name} placeholder="e.g. India Office"
                onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />

              <span style={S.label}>Timezone</span>
              <select style={S.input} value={form.timezone}
                onChange={e => setForm(f => ({ ...f, timezone: e.target.value }))}>
                {TZ_GROUPS.map(g => (
                  <optgroup key={g.label} label={g.label}>
                    {g.zones.map(tz => <option key={tz} value={tz}>{tz}</option>)}
                  </optgroup>
                ))}
              </select>

              <span style={S.label}>Working days</span>
              <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
                {ALL_DAYS.map(d => (
                  <button key={d} onClick={() => toggleDay(d)} style={{
                    width: 36, height: 32, borderRadius: 4, border: "1px solid var(--border-default)",
                    cursor: "pointer", fontSize: 11, fontFamily: "var(--font-mono)", fontWeight: 600,
                    background: form.work_days.includes(d) ? "var(--accent)" : "transparent",
                    color: form.work_days.includes(d) ? "#fff" : "var(--text-secondary)",
                  }}>{DAY_NAMES[d]}</button>
                ))}
              </div>

              <div style={S.grid2}>
                <div>
                  <span style={S.label}>Work start hour</span>
                  <input type="number" min={0} max={23} style={S.input} value={form.work_start_hour}
                    onChange={e => setForm(f => ({ ...f, work_start_hour: parseInt(e.target.value) || 9 }))} />
                </div>
                <div>
                  <span style={S.label}>Work end hour</span>
                  <input type="number" min={1} max={24} style={S.input} value={form.work_end_hour}
                    onChange={e => setForm(f => ({ ...f, work_end_hour: parseInt(e.target.value) || 17 }))} />
                </div>
              </div>

              <span style={S.label}>Description (optional)</span>
              <input style={S.input} value={form.description}
                onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />

              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
                <Button onClick={save} disabled={saving || !form.name}>
                  {saving ? "Saving…" : editing?.id ? "Save changes" : "Create calendar"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Meetings ──────────────────────────────────────────────────── */

function MeetingsTab() {
  const [rows, setRows] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({
    title: "", on: today, from: "10:00", minutes: 60,
    attendees: "", guests: "", link: "", room: false, allowPast: false,
    repeat: "none" as "none" | "daily" | "weekly" | "monthly",
    interval: 1, until: "",
    visibility: "team" as "team" | "private",
  });
  const [note, setNote] = useState<string | null>(null);
  const [slots, setSlots] = useState<{ starts_at: string; ends_at: string }[] | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try { setRows(await listEvents()); }
    catch (e: any) { setErr(e?.message ?? "Could not load events"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const attendees = () => form.attendees.split(",").map(a => a.trim()).filter(Boolean);
  const startsAt = () => new Date(`${form.on}T${form.from}:00`).toISOString();
  const endsAt = () => new Date(new Date(`${form.on}T${form.from}:00`).getTime() + form.minutes * 60000).toISOString();

  const findATime = async () => {
    setBusy(true); setErr(null); setSlots(null);
    try {
      const day = new Date(`${form.on}T00:00:00`);
      const r = await proposeSlots({
        user_ids: attendees(),
        start: day.toISOString(),
        end: new Date(day.getTime() + 24 * 3600 * 1000).toISOString(),
        minutes: form.minutes, limit: 6,
      });
      setSlots(r.slots);
    } catch (e: any) { setErr(e?.message ?? "Could not search"); }
    finally { setBusy(false); }
  };

  const guests = () => form.guests.split(",").map(a => a.trim()).filter(Boolean);
  const recurrence = () => form.repeat === "none" ? null : {
    freq: form.repeat, interval: form.interval || 1,
    ...(form.until ? { until: new Date(`${form.until}T23:59:00`).toISOString() } : {}),
  };

  const create = async () => {
    setBusy(true); setErr(null); setNote(null);
    try {
      // One call: the server schedules, asks HxMeet for a room, then sends the
      // invitations — in that order, so the guests' mail carries the link.
      // Doing it here in three round trips left a window where the meeting
      // existed and nobody had been told.
      const ev = await createEvent({
        title: form.title, starts_at: startsAt(), ends_at: endsAt(),
        attendee_user_ids: attendees(), external_emails: guests(),
        meeting_url: form.link || null, recurrence: recurrence() as any,
        visibility: form.visibility, allow_past: form.allowPast,
        create_room: form.room && !form.link,
      });

      const said: string[] = ["Scheduled."];
      if (ev.room_error) said.push(`The room could not be created: ${ev.room_error}`);
      else if (ev.join_url) said.push(`Room ready — ${ev.join_url}`);
      else if (form.room && !form.link) said.push("Room created in HxMeet.");
      if (ev.invitations) {
        const { sent, failed } = ev.invitations;
        if (sent.length) said.push(`Invited ${sent.join(", ")}.`);
        for (const f of failed ?? []) said.push(`${f.email} was not invited: ${f.error}`);
      }
      setNote(said.join(" "));
      setForm(f => ({ ...f, title: "", guests: "", link: "", room: false, allowPast: false }));
      setSlots(null);
      await load();
    } catch (e: any) { setErr(e?.message ?? "Could not schedule that"); }
    finally { setBusy(false); }
  };

  const act = async (fn: () => Promise<unknown>, said?: string) => {
    setBusy(true); setErr(null); setNote(null);
    try {
      const out: any = await fn();
      if (said) setNote(said);
      if (out?.join_url) setNote(`Room ready — ${out.join_url}`);
      if (out?.sent) {
        setNote(`Invited ${out.sent.length}${out.failed?.length ? `, ${out.failed.length} failed` : ""}.`);
      }
      await load();
    } catch (e: any) { setErr(e?.message ?? "That did not work"); }
    finally { setBusy(false); }
  };



  return (
    <div>
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={S.note}>
          Team meetings — the ones that belong to no case. <strong style={{ color: "var(--text-primary)" }}>Calendar
          owns when and who; HxMeet owns the room</strong>, so nothing here starts video. <em>Find a time</em>
          searches for a slot when everybody named is inside their working hours, not on leave,
          and not already in a meeting.
        </div>
      </Card>

      {err && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "#ef444422", color: "#ef4444" }}>{err}</div>}

      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <span style={S.label}>Title</span>
        <input style={S.input} value={form.title} placeholder="Stand-up"
          onChange={e => setForm(f => ({ ...f, title: e.target.value }))} />

        <span style={S.label}>Attendees (comma separated user ids)</span>
        <input style={S.input} value={form.attendees} placeholder="bob, carol"
          onChange={e => setForm(f => ({ ...f, attendees: e.target.value }))} />

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 12 }}>
          <div>
            <span style={S.label}>Day</span>
            <input type="date" style={S.input} value={form.on}
              onChange={e => setForm(f => ({ ...f, on: e.target.value }))} />
          </div>
          <div>
            <span style={S.label}>Start</span>
            <input type="time" style={S.input} value={form.from}
              onChange={e => setForm(f => ({ ...f, from: e.target.value }))} />
          </div>
          <div>
            <span style={S.label}>Minutes</span>
            <input type="number" min={5} max={480} style={S.input} value={form.minutes}
              onChange={e => setForm(f => ({ ...f, minutes: parseInt(e.target.value) || 30 }))} />
          </div>
          <div>
            <span style={S.label}>Visibility</span>
            <select style={S.input} value={form.visibility}
              onChange={e => setForm(f => ({ ...f, visibility: e.target.value as "team" | "private" }))}>
              <option value="team">team</option>
              <option value="private">private</option>
            </select>
          </div>
        </div>

        {new Date(startsAt()) < new Date() && (
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, marginBottom: 10, color: "#f59e0b" }}>
            <input type="checkbox" checked={form.allowPast}
              onChange={e => setForm(f => ({ ...f, allowPast: e.target.checked }))}
              style={{ accentColor: "#f59e0b" }} />
            <span>That time has already passed — record it anyway (a meeting that happened)</span>
          </label>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <div>
            <span style={S.label}>Guests (external email addresses)</span>
            <input style={S.input} value={form.guests} placeholder="counsel@client.example"
              onChange={e => setForm(f => ({ ...f, guests: e.target.value }))} />
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -6, marginBottom: 10 }}>
              People with no account here. Kept apart from attendees — an availability
              lookup cannot find a working pattern for an email address.
              <b> They are emailed the invitation when you schedule.</b>
            </div>
          </div>
          <div>
            <span style={S.label}>Meeting link (optional)</span>
            <input style={S.input} value={form.link} placeholder="paste a Teams/Zoom link"
              onChange={e => setForm(f => ({ ...f, link: e.target.value }))} />
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, marginTop: -4, marginBottom: 6 }}>
              <input type="checkbox" checked={form.room} disabled={!!form.link}
                onChange={e => setForm(f => ({ ...f, room: e.target.checked }))}
                style={{ accentColor: "var(--accent)" }} />
              <span style={{ color: "var(--text-primary)" }}>Create a meeting link with HxMeet</span>
            </label>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
              A link <i>or</i> a room, never both. HxMeet's own screens are per case;
              this asks it for a room for a meeting that has no case, which is what
              a stand-up is.
            </div>
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
          <div>
            <span style={S.label}>Repeats</span>
            <select style={S.input} value={form.repeat}
              onChange={e => setForm(f => ({ ...f, repeat: e.target.value as any }))}>
              <option value="none">does not repeat</option>
              <option value="daily">daily</option>
              <option value="weekly">weekly</option>
              <option value="monthly">monthly</option>
            </select>
          </div>
          <div>
            <span style={S.label}>Every</span>
            <input type="number" min={1} max={52} style={S.input} value={form.interval}
              disabled={form.repeat === "none"}
              onChange={e => setForm(f => ({ ...f, interval: parseInt(e.target.value) || 1 }))} />
          </div>
          <div>
            <span style={S.label}>Until (optional)</span>
            <input type="date" style={S.input} value={form.until}
              disabled={form.repeat === "none"}
              onChange={e => setForm(f => ({ ...f, until: e.target.value }))} />
          </div>
        </div>

        <div style={{ display: "flex", gap: 8 }}>
          <Button size="sm" disabled={busy || !form.title} onClick={create}>Schedule</Button>
          <Button size="sm" variant="secondary" disabled={busy || attendees().length === 0} onClick={findATime}>
            Find a time
          </Button>
        </div>
        {note && <div style={{ fontSize: 12, marginTop: 10, color: "#22c55e", wordBreak: "break-all" as const }}>{note}</div>}

        {slots && (
          <div style={{ marginTop: 12 }}>
            {slots.length === 0
              ? <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  No slot that day when everybody is free. That is an answer, not an error — try another day.
                </div>
              : <div style={{ display: "flex", gap: 8, flexWrap: "wrap" as const }}>
                  {slots.map(sl => (
                    <button key={sl.starts_at} onClick={() => setForm(f => ({
                      ...f, from: new Date(sl.starts_at).toTimeString().slice(0, 5),
                    }))} style={{
                      padding: "4px 10px", borderRadius: 4, cursor: "pointer", fontSize: 12,
                      border: "1px solid var(--accent)", background: "var(--accent-dim)",
                      color: "var(--accent)", fontFamily: "var(--font-mono)",
                    }}>
                      {new Date(sl.starts_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                    </button>
                  ))}
                </div>}
          </div>
        )}
      </Card>

      {/* No list here on purpose. A meeting lives on the **calendar**, not under
          the form that made it — you look for a meeting on the day it happens,
          not in the order it was created. Opening, joining, editing and
          cancelling all happen on the Calendar tab. */}
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 14, lineHeight: 1.7 }}>
        Scheduled meetings appear on the <b>Calendar</b> tab, on their day. Open one
        there to <b>join</b>, edit, invite guests or cancel it.
      </div>
    </div>
  );
}


/* ── Sync tab ──────────────────────────────────────────────────────
 *
 * Three things that are one idea: get a calendar in, keep it coming in, get it
 * out. They share a tab because a user asking "can I use my Outlook calendar
 * here" does not know which of the three they want.
 */

const FEED_STATUS: Record<string, { colour: string; label: string }> = {
  ok:        { colour: "#22c55e", label: "OK" },
  unchanged: { colour: "#22c55e", label: "Up to date" },
  error:     { colour: "#ef4444", label: "Failing" },
  never:     { colour: "#64748b", label: "Not read yet" },
};

function when(value: string | null): string {
  if (!value) return "never";
  try { return new Date(value).toLocaleString(); } catch { return value; }
}

function SyncTab() {
  const [subs, setSubs] = useState<CalendarSubscription[]>([]);
  const [hosts, setHosts] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [form, setForm] = useState({ name: "", url: "", interval_minutes: 60 });
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  // Import. `preview` holds the dry run, so the second click confirms something
  // the person has actually read rather than something they hope is true.
  const [ics, setIcs] = useState("");
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState<any | null>(null);
  const [importing, setImporting] = useState(false);

  const [range, setRange] = useState(() => {
    const from = new Date();
    const to = new Date(); to.setMonth(to.getMonth() + 3);
    return { start: from.toISOString().slice(0, 10), end: to.toISOString().slice(0, 10) };
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listCalendarSubscriptions();
      setSubs(data.subscriptions);
      setHosts(data.allowed_hosts || []);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message || "Could not load subscriptions");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function subscribe() {
    if (!form.name.trim() || !form.url.trim()) {
      setMsg({ text: "A name and a URL, please.", ok: false });
      return;
    }
    setBusy("new");
    try {
      await createCalendarSubscription(form);
      setForm({ name: "", url: "", interval_minutes: 60 });
      setMsg({ text: "Subscribed. Refresh it to read it now.", ok: true });
      await load();
    } catch (e: any) {
      setMsg({ text: e?.message || "Could not subscribe", ok: false });
    } finally { setBusy(null); }
  }

  async function refresh(id: string) {
    setBusy(id);
    try {
      // A feed-side failure comes back as a 200 with status "error" — the
      // request worked, somebody else's server did not. Report it as it is.
      const out = await refreshCalendarSubscription(id);
      setMsg(out.status === "error"
        ? { text: `That calendar could not be read: ${out.error}`, ok: false }
        : { text: out.status === "unchanged" ? "Already up to date."
              : `Read ${out.created} new, ${out.updated} updated${out.withdrawn ? `, ${out.withdrawn} withdrawn` : ""}.`,
            ok: true });
      await load();
    } catch (e: any) {
      setMsg({ text: e?.message || "Refresh failed", ok: false });
    } finally { setBusy(null); }
  }

  async function toggle(row: CalendarSubscription) {
    setBusy(row.id);
    try { await updateCalendarSubscription(row.id, { enabled: !row.enabled }); await load(); }
    catch (e: any) { setMsg({ text: e?.message || "Could not save", ok: false }); }
    finally { setBusy(null); }
  }

  async function remove(row: CalendarSubscription) {
    setBusy(row.id);
    try {
      // Entries are kept: unsubscribing means "stop refreshing this", not
      // "erase the last year of my diary".
      await deleteCalendarSubscription(row.id, true);
      setMsg({ text: `Unsubscribed from "${row.name}". Its entries were kept.`, ok: true });
      await load();
    } catch (e: any) {
      setMsg({ text: e?.message || "Could not unsubscribe", ok: false });
    } finally { setBusy(null); }
  }

  function readFile(file: File) {
    const reader = new FileReader();
    reader.onload = () => {
      setIcs(String(reader.result || ""));
      setFileName(file.name);
      setPreview(null);
    };
    reader.readAsText(file);
  }

  async function runImport(dryRun: boolean) {
    if (!ics.trim()) { setMsg({ text: "Choose a .ics file first.", ok: false }); return; }
    setImporting(true);
    try {
      const out = await importCalendarIcs({ ics, dry_run: dryRun });
      if (dryRun) {
        setPreview(out);
        setMsg(null);
      } else {
        setPreview(null); setIcs(""); setFileName("");
        setMsg({ text: `Imported ${out.created} new and ${out.updated} updated entries.`, ok: true });
      }
    } catch (e: any) {
      setMsg({ text: e?.message || "Import failed", ok: false });
      setPreview(null);
    } finally { setImporting(false); }
  }

  async function download() {
    try {
      const text = await exportCalendarIcs({
        start: new Date(range.start).toISOString(),
        end: new Date(range.end).toISOString(),
      });
      // Built in the browser, not followed as a link: a plain <a href> carries
      // no Authorization header and would come back 401.
      const url = URL.createObjectURL(new Blob([text], { type: "text/calendar" }));
      const a = document.createElement("a");
      a.href = url; a.download = "velaris-calendar.ics";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setMsg({ text: e?.message || "Export failed", ok: false });
    }
  }

  if (loading) return <Spinner />;

  return (
    <div>
      {msg && (
        <div style={{ marginBottom: 14, padding: "10px 12px", borderRadius: "var(--radius-sm)", fontSize: 13,
                      background: msg.ok ? "rgba(34,197,94,0.10)" : "rgba(239,68,68,0.10)",
                      color: msg.ok ? "#16a34a" : "#dc2626" }}>
          {msg.text}
        </div>
      )}

      <Card>
        <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>Subscribe to a calendar</h3>
        <div style={{ ...S.note, marginBottom: 14 }}>
          Paste the address of a calendar published elsewhere &mdash; Outlook, Google
          or Apple all give you one. Velaris <b>reads</b> it on a schedule and never
          writes back to it.
        </div>

        {hosts.length === 0 ? (
          <EmptyState
            title="No calendar services are allowed yet"
            description="An administrator lists the calendar hosts this server may fetch from before a subscription can be added. This server only fetches addresses approved in advance."
          />
        ) : (
          <>
            <div style={S.grid2}>
              <div>
                <label style={S.label}>Name</label>
                <input style={S.input} value={form.name} placeholder="My Outlook calendar"
                       onChange={e => setForm({ ...form, name: e.target.value })} />
              </div>
              <div>
                <label style={S.label}>Refresh every</label>
                <select style={S.input} value={form.interval_minutes}
                        onChange={e => setForm({ ...form, interval_minutes: Number(e.target.value) })}>
                  <option value={15}>15 minutes</option>
                  <option value={60}>hour</option>
                  <option value={360}>6 hours</option>
                  <option value={1440}>day</option>
                </select>
              </div>
            </div>
            <label style={S.label}>Calendar address</label>
            <input style={S.input} value={form.url} placeholder="https://outlook.office365.com/owa/calendar/…/calendar.ics"
                   onChange={e => setForm({ ...form, url: e.target.value })} />
            <div style={{ ...S.note, marginBottom: 12 }}>
              <b>Treat this address like a password.</b> A private calendar link has a
              token in it, so anyone holding the link can read that calendar. It is
              shown back only to you, your manager and an administrator.
              <br />
              Allowed here: {hosts.map(h => <code key={h} style={{ marginRight: 8 }}>{h}</code>)}
            </div>
            <Button onClick={subscribe} disabled={busy === "new"}>
              {busy === "new" ? "Subscribing…" : "Subscribe"}
            </Button>
          </>
        )}
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ margin: "0 0 12px", fontSize: 15 }}>Subscribed calendars</h3>
        {err && <div style={{ color: "#dc2626", fontSize: 13, marginBottom: 10 }}>{err}</div>}
        {subs.length === 0 ? (
          <EmptyState title="No subscriptions" description="Calendars you subscribe to appear here, with when they were last read." />
        ) : subs.map(row => {
          const state = FEED_STATUS[row.last_status] || FEED_STATUS.never;
          return (
            <div key={row.id} style={{ display: "flex", alignItems: "center", gap: 12, padding: "12px 0",
                                       borderBottom: "1px solid var(--border-subtle)" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>
                  {row.name}{" "}
                  <span style={{ ...S.badge, background: `${state.colour}22`, color: state.colour }}>
                    {state.label}
                  </span>
                  {!row.enabled && <span style={{ ...S.badge, marginLeft: 6, background: "#64748b22", color: "#64748b" }}>PAUSED</span>}
                </div>
                <div style={{ ...S.note, fontSize: 12 }}>
                  {row.last_event_count} entries &middot; last read {when(row.last_fetched_at)}
                  {row.last_status === "error" && row.last_error && (
                    // A calendar failing for a fortnight must SAY so. A sync that
                    // silently stops is worse than one that never started.
                    <div style={{ color: "#dc2626", marginTop: 4 }}>
                      {row.last_error}
                      {row.consecutive_errors >= 20 &&
                        " — stopped refreshing after repeated failures. Fix the address and refresh by hand."}
                    </div>
                  )}
                </div>
              </div>
              <Button variant="secondary" onClick={() => refresh(row.id)} disabled={busy === row.id}>
                {busy === row.id ? "…" : "Refresh"}
              </Button>
              <Button variant="secondary" onClick={() => toggle(row)} disabled={busy === row.id}>
                {row.enabled ? "Pause" : "Resume"}
              </Button>
              <Button variant="secondary" onClick={() => remove(row)} disabled={busy === row.id}>
                Unsubscribe
              </Button>
            </div>
          );
        })}
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>Import a file once</h3>
        <div style={{ ...S.note, marginBottom: 14 }}>
          Export your calendar from Outlook, Google or Apple as an <code>.ics</code>
          file and upload it. A file is a snapshot &mdash; it will not update itself.
          Subscribe above if you want it to stay current.
        </div>
        <input type="file" accept=".ics,text/calendar" style={{ marginBottom: 12 }}
               onChange={e => { const f = e.target.files?.[0]; if (f) readFile(f); }} />
        {fileName && <div style={{ ...S.note, marginBottom: 12 }}>Selected: <b>{fileName}</b></div>}

        <div style={{ display: "flex", gap: 8 }}>
          <Button variant="secondary" onClick={() => runImport(true)} disabled={!ics || importing}>
            {importing ? "Reading…" : "Preview"}
          </Button>
          <Button onClick={() => runImport(false)} disabled={!preview || importing}>
            Import {preview ? `${preview.created + preview.updated} entries` : ""}
          </Button>
        </div>

        {preview && (
          // The preview is the point: the second click confirms something the
          // person has read, not something they hope is true.
          <div style={{ marginTop: 14, padding: 12, borderRadius: "var(--radius-sm)", background: "var(--bg-subtle)" }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
              {preview.created} new &middot; {preview.updated} updated
              {preview.absences_requested > 0 && ` · ${preview.absences_requested} leave request(s)`}
            </div>
            {preview.absences_requested > 0 && (
              <div style={{ ...S.note, fontSize: 12 }}>
                Out-of-office entries arrive as leave <b>requests</b> &mdash; they change
                nothing until somebody approves them.
              </div>
            )}
            {preview.skipped?.length > 0 && (
              <div style={{ ...S.note, fontSize: 12, marginTop: 8 }}>
                <b>{preview.skipped.length} entr{preview.skipped.length === 1 ? "y" : "ies"} skipped:</b>
                <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                  {preview.skipped.slice(0, 8).map((s: any, i: number) => (
                    <li key={i}>{s.event} &mdash; {s.reason}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>Export your calendar</h3>
        <div style={{ ...S.note, marginBottom: 14 }}>
          Download meetings and approved leave as an <code>.ics</code> file, to open
          in Outlook, Google or Apple Calendar.
        </div>
        <div style={S.grid2}>
          <div>
            <label style={S.label}>From</label>
            <input type="date" style={S.input} value={range.start}
                   onChange={e => setRange({ ...range, start: e.target.value })} />
          </div>
          <div>
            <label style={S.label}>To</label>
            <input type="date" style={S.input} value={range.end}
                   onChange={e => setRange({ ...range, end: e.target.value })} />
          </div>
        </div>
        <Button variant="secondary" onClick={download}>Download .ics</Button>
      </Card>
    </div>
  );
}

/* ── Root ──────────────────────────────────────────────────────── */

type Tab = "month" | "team" | "record" | "meetings" | "calendars" | "sync";

const TABS: { key: Tab; label: string }[] = [
  { key: "month",     label: "Calendar" },
  { key: "team",      label: "Who is away" },
  { key: "record",    label: "Record leave" },
  { key: "meetings",  label: "New meeting" },
  { key: "calendars", label: "Business Calendars" },
  { key: "sync",      label: "Import / Sync" },
];

export default function Calendar() {
  const [tab, setTab] = useState<Tab>("month");
  const [reload, setReload] = useState(0);

  return (
    <div style={S.page}>
      <Tabs
          ariaLabel="Calendar"
          tabs={TABS}
          value={tab}
          onChange={setTab}
        />
      <div style={S.body}>
        {tab === "month"     && <MonthTab key={reload} />}
        {tab === "team"      && <TeamTab key={reload} />}
        {tab === "record"    && <RecordTab onDone={() => setReload(n => n + 1)} />}
        {tab === "meetings"  && <MeetingsTab />}
        {tab === "calendars" && <CalendarsTab />}
        {tab === "sync"      && <SyncTab />}
      </div>
    </div>
  );
}
