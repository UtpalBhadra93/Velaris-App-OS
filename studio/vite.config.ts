import { defineConfig, Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

/**
 * Warn when a non-auth file calls raw fetch() instead of apiClient.
 *
 * Exempt files (raw fetch is intentional):
 *   src/auth/         — login, refresh, SSO flows exist before tokens
 *   src/shared/api/client.ts — the canonical wrapper that wraps fetch itself
 *
 * All other .ts/.tsx files that contain `fetch(` (word-boundary match to
 * exclude `prefetch(`, `fetchData(` etc.) emit a Vite warning at build/dev
 * transform time. This does NOT break the build — it guides developers to
 * use apiClient so auth headers are always included.
 */
function noRawFetchPlugin(): Plugin {
  const RAW_FETCH = /\bfetch\s*\(/;
  const EXEMPT = [
    `${path.sep}src${path.sep}auth${path.sep}`,
    `${path.sep}src${path.sep}shared${path.sep}api${path.sep}client.ts`,
  ];

  return {
    name: "no-raw-fetch",
    transform(code, id) {
      if (!id.endsWith(".ts") && !id.endsWith(".tsx")) return;
      if (EXEMPT.some((seg) => id.includes(seg))) return;
      if (RAW_FETCH.test(code)) {
        this.warn(
          `raw fetch() detected — use apiClient from @shared/api/client so auth headers are always included`
        );
      }
    },
  };
}

// Shared by the dev server and by `vite preview`. **`preview` does NOT inherit
// `server.proxy`** — a production unit running preview without this serves the
// SPA fine and 404s every single API call.
const apiProxy = {
    "/api/v1/testsuite": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxtest": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxdbmigrate": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxevolve": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxreplay": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/costing": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/cases": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/case-types": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/queues": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/assignments": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/my": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/rules":{ 
      target: "http://localhost:8200", 
      changeOrigin: true,
    },
   "/api/v1/data-models":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/enterprise":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/realtime":{
      target: "ws://localhost:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/orchestrator":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/sitemap":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/scout-ai":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/codegen":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/tenants":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/scout":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/nlp":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/process-mining":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/auth":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // Enterprise identity P2 — the Studio side of this is the token
    // mint/revoke screen under /api/v1/scim/tokens; the /v2 half is called by
    // the customer's IdP, not by the browser.
    "/api/v1/scim":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // Omnichannel — the Channel Inbox and the case Channels tab.
    "/api/v1/channels":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // RPA — the bot registry, job list and attended proposal diff.
    "/api/v1/bots":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // Timed actions — the schedule list, its review gate and run history.
    "/api/v1/scheduler":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/admin":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/webhooks":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/analytics":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/form-submissions":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/observability":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/documents":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/email":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/compliance":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/legal-holds":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/user-directory":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/escalation-trees":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/push":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxnexus":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/portal-admin":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/portal":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/portals":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/access-roles":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/access-groups":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/knowledge": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/graph": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/apps": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/importer": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/analytics": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxbridge": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/sync": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/global": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/shield": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // Audit streaming to a SIEM (mig 200). Missing until the Enterprise screen
    // was built on 2026-09-10 — the documented trap: without an entry here the
    // browser's calls fall through to the engine on 8100 and 404, while curl
    // against :8200 works perfectly.
    "/api/v1/audit-stream": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/fusion": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/payments": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/identity": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/esign": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/crm": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/invoices": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/comms": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/docintel": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/devconn": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxmigrate": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/arbiter": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // F-00 agent identity. No Studio screen yet — the entry is here because a
    // router without one is the trap this allowlist exists to prevent, and the
    // first UI to call it would otherwise hit the catch-all instead.
    "/api/v1/agents": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // F-06 ambient capture. Same reason as the line above: the router exists,
    // so the allowlist entry exists, whether or not a screen calls it yet.
    "/api/v1/capture": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // F-07 sovereign model marketplace.
    "/api/v1/model-market": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // F-08 … F-12 and the Customer Engagement rep desk. None of these has a
    // Studio screen yet, and the entries exist anyway for the same reason
    // F-00's does: a router without an allowlist entry is precisely the trap
    // this list prevents, and the first UI to call one would silently hit the
    // catch-all instead of the gateway.
    "/api/v1/healing": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/handoff": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/field": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/intent": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/negotiate": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/engage": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/calendar": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/deploy": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxwork": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxcanvas": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxdocs": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/intake": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/branches": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/commits": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxlogs": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/webhooks": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/hxstream/ws": {
      target: "ws://localhost:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/hxstream":{
      target: "http://localhost:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/hxdbmanager": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // 198 — case status set and action configuration. `/api/v1/cases` already
    // routes to 8200 above; this is the separate authoring router.
    "/api/v1/case-config": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // HxStatus (§8.8). Must sit BEFORE the "/api" catch-all below, or the
    // browser 404s while curl against :8200 works perfectly — the documented
    // trap for every new case-service router.
    "/api/v1/status": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/releases": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/marketplace": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/auth/me":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/auth/switch-context":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/sla":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/forms":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/admin":{
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/meet": {
      target: "http://localhost:8200",
      changeOrigin: true,
      ws: true,  // P4a-live caption stream
    },
    "/api/v1/stt": {   // Voice input — local speech-to-text
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/voicefill": {   // Voice input P3 — transcript to form-field candidates
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/variables": {
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/risk": {   // HxSentinel (findings, rule config, verify-gate holds)
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    "/api/v1/platform": {   // Platform update orchestrator (banner + rollout plans)
      target: "http://localhost:8200",
      changeOrigin: true,
    },
    // Catch-all LAST: anything not matched above goes to the engine (8100)
    // with /api stripped. A new case-service router MUST be allowlisted
    // above, or its browser calls silently 404 on the engine.
    "/api": {
      target: "http://localhost:8100",
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api/, ""),
    },
};

export default defineConfig({
  plugins: [react(), noRawFetchPlugin()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "@shared": path.resolve(__dirname, "src/shared"),
      "@modules": path.resolve(__dirname, "src/modules"),
    },
  },
  server: {
    port: 5173,
    allowedHosts: true,
    fs: {
      // Allow serving files from root node_modules (hoisted monorepo deps)
      allow: [path.resolve(__dirname, ".."), "/home/utpal-bhadra/helix/node_modules"],
    },
    proxy: apiProxy,
  },
  preview: {
    port: 5173,
    allowedHosts: true,
    proxy: apiProxy,
  },
});
