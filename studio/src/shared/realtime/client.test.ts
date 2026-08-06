/**
 * Realtime WebSocket client — the shared live-update transport.
 *
 * It is a singleton every module reaches through, so its failure modes are
 * platform-wide rather than local: a handler leak here grows without bound
 * across a long session, and a reconnect loop without backoff turns one
 * server blip into a self-inflicted flood from every open tab.
 *
 * The behaviours pinned below are the ones that are easy to write, easy to
 * regress, and invisible until a session has been open for hours:
 *
 *  * unsubscribe actually removes the handler — otherwise a component that
 *    mounts and unmounts repeatedly accumulates dead callbacks that keep
 *    firing against unmounted state;
 *  * the last unsubscribe for a channel tells the server, rather than leaving
 *    a subscription nobody is listening to;
 *  * reconnect backs off exponentially and is capped, not retried tightly;
 *  * one throwing handler does not stop the others on the same channel;
 *  * a malformed frame is survived rather than killing the socket.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* A minimal WebSocket double. The real one cannot be used in jsdom, and the
   point here is the client's own bookkeeping, not the browser's transport. */
class FakeSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeSocket[] = [];

  readyState = FakeSocket.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) { this.sent.push(data); }
  close() { this.readyState = FakeSocket.CLOSED; this.onclose?.(); }

  parsed() { return this.sent.map(s => JSON.parse(s)); }
}

function latest() {
  return FakeSocket.instances[FakeSocket.instances.length - 1];
}

/* The client is a module-level singleton created on first use, so importing it
   normally would carry one instance — and its sockets — across every test in
   this file. Each test gets a fresh module registry instead. */
async function freshClient(userId = "u1") {
  const mod = await import("./client");
  return mod.getRealtimeClient(userId);
}

beforeEach(() => {
  vi.resetModules();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("subscription bookkeeping", () => {
  it("delivers events to a subscribed handler", async () => {
    const client = await freshClient();
    const seen: any[] = [];
    client.subscribe("cases.abc", e => seen.push(e));

    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.abc", v: 1 }) });

    expect(seen).toEqual([{ channel: "cases.abc", v: 1 }]);
  });

  it("unsubscribe stops delivery — the leak that grows over a long session", async () => {
    const client = await freshClient();
    const seen: any[] = [];
    const off = client.subscribe("cases.leak", e => seen.push(e));

    off();
    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.leak" }) });

    expect(seen).toEqual([]);
  });

  it("two handlers on one channel both fire, and removing one keeps the other", async () => {
    const client = await freshClient();
    const a: any[] = [], b: any[] = [];
    const offA = client.subscribe("cases.two", e => a.push(e));
    client.subscribe("cases.two", e => b.push(e));

    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.two" }) });
    offA();
    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.two" }) });

    expect(a).toHaveLength(1);
    expect(b).toHaveLength(2);
  });

  it("tells the server only when the LAST handler for a channel goes", async () => {
    // Unsubscribing on the first removal would silence the remaining handler.
    const client = await freshClient();
    const offA = client.subscribe("cases.last", () => {});
    const offB = client.subscribe("cases.last", () => {});

    offA();
    expect(latest().parsed().some(m => m.type === "unsubscribe")).toBe(false);

    offB();
    expect(latest().parsed().some(
      m => m.type === "unsubscribe" && m.channel === "cases.last")).toBe(true);
  });

  it("a throwing handler does not stop the others", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const client = await freshClient();
    const seen: any[] = [];
    client.subscribe("cases.boom", () => { throw new Error("bad handler"); });
    client.subscribe("cases.boom", e => seen.push(e));

    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.boom" }) });

    expect(seen).toHaveLength(1);
  });

  it("wildcard handlers receive a more specific channel's events", async () => {
    const client = await freshClient();
    const seen: any[] = [];
    client.subscribe("cases.*", e => seen.push(e));

    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.abc123" }) });

    expect(seen).toHaveLength(1);
  });
});

describe("resilience", () => {
  it("survives a malformed frame instead of tearing the socket down", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const client = await freshClient();
    const seen: any[] = [];
    client.subscribe("cases.ok", e => seen.push(e));

    latest().onmessage?.({ data: "<html>not json</html>" });
    latest().onmessage?.({ data: JSON.stringify({ channel: "cases.ok" }) });

    expect(seen).toHaveLength(1);
  });

  it("backs off exponentially rather than reconnecting tightly", async () => {
    // A tight loop turns one server restart into a flood from every open tab.
    await freshClient();
    const before = FakeSocket.instances.length;

    latest().close();
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances.length).toBe(before);   // not yet

    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances.length).toBe(before + 1);

    // Second failure must wait longer than the first.
    latest().close();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances.length).toBe(before + 1);
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances.length).toBe(before + 2);
  });

  it("re-subscribes every channel after a reconnect", async () => {
    // Otherwise the socket comes back and the UI silently stops updating —
    // the worst kind of failure, because it looks like nothing is happening.
    const client = await freshClient();
    client.subscribe("cases.keep", () => {});

    latest().close();
    vi.advanceTimersByTime(1000);
    latest().onopen?.();

    expect(latest().parsed().some(
      m => m.type === "subscribe" && m.channel === "cases.keep")).toBe(true);
  });

  it("caps the backoff so a long outage does not stall reconnection forever", async () => {
    await freshClient();
    for (let i = 0; i < 12; i++) {
      latest().close();
      vi.advanceTimersByTime(30_000);
    }
    const before = FakeSocket.instances.length;
    latest().close();
    vi.advanceTimersByTime(30_000);
    expect(FakeSocket.instances.length).toBe(before + 1);
  });
});
