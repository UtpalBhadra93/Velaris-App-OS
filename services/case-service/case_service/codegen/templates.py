"""React Native code templates.

Each template is a plain string with ``__TOKEN__`` placeholders that
:func:`case_service.codegen.generator.render` substitutes.

**Braces are literal.** An earlier scheme doubled every ``{`` and un-escaped
once at render time, which is how ``App.tsx`` shipped for months with
``headerStyle: {{ ... }}`` in it — a syntax error, so the generated app had
never compiled. Nothing is escaped here: write the JSX you want to read.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

PACKAGE_JSON = '''{
  "name": "__APP_SLUG__",
  "version": "__APP_VERSION__",
  "main": "node_modules/expo/AppEntry.js",
  "scripts": {
    "start": "expo start",
    "android": "expo start --android",
    "ios": "expo start --ios",
    "web": "expo start --web",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "expo": "~57.0.0",
    "expo-status-bar": "~57.0.1",
    "expo-secure-store": "~57.0.1",
    "expo-splash-screen": "~57.0.6",
    "react": "19.2.3",
    "react-dom": "19.2.3",
    "react-native": "0.86.2",
    "react-native-web": "^0.21.2",
    "@expo/metro-runtime": "~57.0.11",
    "@react-navigation/native": "^7.0.0",
    "@react-navigation/stack": "^7.0.0",
    "@react-navigation/bottom-tabs": "^7.0.0",
    "react-native-screens": "~4.26.0",
    "react-native-safe-area-context": "~5.7.0",
    "react-native-gesture-handler": "~2.32.0",
    "@react-native-async-storage/async-storage": "2.2.0"
  },
  "devDependencies": {
    "@babel/core": "^7.20.0",
    "typescript": "~6.0.3",
    "@types/react": "~19.2.0"
  },
  "private": true
}
'''

#: Targets the **current Expo SDK**. Expo Go only ever supports the newest SDK,
#: so a scaffold pinned to an old one cannot be opened on a phone at all — it
#: answers *"Project is incompatible with this version of Expo Go"*. Bump this
#: with each SDK release; `npx expo install --fix` resolves the rest.
#:
#: `splash` is a **plugin** from SDK 52 onward, not a top-level key — as a
#: top-level key it fails the config schema check outright.
#:
#: Deliberately carries **no `updates` block and no `extra.eas.projectId`**.
#: Both used to be emitted with placeholder values, and Expo CLI reads them: it
#: tries to resolve the EAS project and then refuses to serve the app until you
#: sign in to an Expo account. `expo start` answered *"An Expo user account is
#: required to proceed"* before anyone could open the scaffold in Expo Go, so
#: the export could not be run at all without an account on an unrelated
#: service. `eas init` writes both correctly when a team actually sets EAS up;
#: an unconfigured placeholder is worse than an absent key.
APP_JSON = '''{
  "expo": {
    "name": "__APP_NAME__",
    "slug": "__APP_SLUG__",
    "version": "__APP_VERSION__",
    "description": "__APP_DESCRIPTION__",
    "orientation": "portrait",
    "icon": "./assets/icon.png",
    "userInterfaceStyle": "automatic",
    "assetBundlePatterns": ["**/*"],
    "plugins": [
      ["expo-splash-screen", {
        "image": "./assets/splash.png",
        "resizeMode": "contain",
        "backgroundColor": "__PRIMARY_COLOR__"
      }]
    ],
    "ios": {
      "supportsTablet": true,
      "bundleIdentifier": "__IOS_BUNDLE_ID__",
      "buildNumber": "1"
    },
    "android": {
      "package": "__ANDROID_PACKAGE__",
      "versionCode": 1,
      "adaptiveIcon": {
        "foregroundImage": "./assets/adaptive-icon.png",
        "backgroundColor": "__PRIMARY_COLOR__"
      }
    },
    "web": { "favicon": "./assets/favicon.png" },
    "runtimeVersion": {
      "policy": "appVersion"
    }
  }
}
'''

EAS_JSON = '''{
  "cli": {
    "version": ">= 7.0.0"
  },
  "build": {
    "development": {
      "developmentClient": true,
      "distribution": "internal"
    },
    "preview": {
      "distribution": "internal",
      "android": { "buildType": "apk" }
    },
    "production": {
      "autoIncrement": true
    }
  },
  "submit": {
    "production": {
      "ios": {
        "appleId": "your@apple.id",
        "ascAppId": "your-app-store-connect-app-id",
        "appleTeamId": "YOUR_TEAM_ID"
      },
      "android": {
        "serviceAccountKeyPath": "./path/to/google-service-account.json",
        "track": "production"
      }
    }
  }
}
'''

# ---------------------------------------------------------------- app config

CONFIG_TS = '''// Values baked in by Velaris App Builder at generation time.
export const APP_NAME = '__APP_NAME__';
export const APP_VERSION = '__APP_VERSION__';
export const PRIMARY_COLOR = '__PRIMARY_COLOR__';
export const DEFAULT_API_URL = '__DEFAULT_API_URL__';
export const DEFAULT_TENANT = '__DEFAULT_TENANT__';

// Customer sign-in is portal-scoped — `/portal/{slug}/…` — and the token the
// server mints is bound to this one slug, so it is baked in rather than typed.
export const PORTAL_SLUG = '__PORTAL_SLUG__';

// Who this build is for. 'staff' and 'customer' builds omit the other side's
// screens entirely — a company shipping to its customers should not have a
// Resolve button in the binary at all, not merely hidden behind a tab.
export const AUDIENCE: 'both' | 'staff' | 'customer' = '__AUDIENCE__';

// The case types the designer selected in App Builder. Empty means "all of
// them" — the server is still the authority on what this user may see; this
// only narrows what the app offers.
export const ALLOWED_CASE_TYPE_IDS: string[] = __CASE_TYPE_IDS_JSON__;

export function caseTypeAllowed(id: string): boolean {
  return ALLOWED_CASE_TYPE_IDS.length === 0 || ALLOWED_CASE_TYPE_IDS.indexOf(id) !== -1;
}
'''

# --------------------------------------------------------------- server URLs

URL_TS = r'''// Where the app is allowed to send a bearer token.
//
// The base URL is typed by a person, so it is the one place a valid session
// token could be aimed at a host that should never see it. Everything here
// names the shape that is ALLOWED rather than listing characters to refuse —
// an allowlist cannot be walked around with an encoding trick.

// `$` alone also matches just before a trailing newline, so the end is
// anchored against any remaining character at all.
const BASE_URL_SHAPE =
  /^(https?):\/\/([a-z0-9][a-z0-9.-]*|\[[0-9a-f:]+\])(?::(\d{1,5}))?((?:\/[A-Za-z0-9._~-]+)*)\/?$(?![\s\S])/i;

const LOOPBACK = ['localhost', '127.0.0.1', '[::1]'];

/** Hosts where plain http is a development convenience rather than a mistake. */
function isLocalHost(host: string): boolean {
  if (LOOPBACK.indexOf(host) !== -1) return true;
  if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
  return /\.local$/.test(host);
}

export type UrlCheck =
  | { ok: true; baseUrl: string; origin: string }
  | { ok: false; reason: string };

/** Validate and canonicalise a server URL. */
export function checkBaseUrl(raw: string): UrlCheck {
  const trimmed = (raw || '').trim();
  if (!trimmed) return { ok: false, reason: 'Enter your Velaris server URL.' };

  const m = BASE_URL_SHAPE.exec(trimmed);
  if (!m) {
    return {
      ok: false,
      reason: 'That is not a server URL. Expected something like https://velaris.example.com',
    };
  }

  const scheme = m[1].toLowerCase();
  const host = m[2].toLowerCase();
  const port = m[3];
  const path = (m[4] || '').replace(/\/$/, '');

  if (port && Number(port) > 65535) return { ok: false, reason: 'That port number does not exist.' };

  if (scheme === 'http' && !isLocalHost(host)) {
    return {
      ok: false,
      reason:
        'Plain http would send your sign-in over the network in the clear. ' +
        'Use https:// (http is accepted only for a local development server).',
    };
  }

  const origin = scheme + '://' + host + (port ? ':' + port : '');
  return { ok: true, baseUrl: origin + path, origin };
}

/** The scheme+host+port a base URL points at, or '' if it is not a valid one. */
export function originOf(raw: string): string {
  const checked = checkBaseUrl(raw);
  return checked.ok ? checked.origin : '';
}

// API paths are built by this app, never by a server response, but a screen
// bug should not be able to turn one into a different host or a new query.
const SAFE_PATH = /^\/[A-Za-z0-9._~\-\/]*(\?[A-Za-z0-9._~\-=&%]*)?$/;

export function pathIsSafe(path: string): boolean {
  return SAFE_PATH.test(path) && path.indexOf('//') === -1 && path.indexOf('..') === -1;
}

// Two kinds of caller reach this server, and both present their token as
// `Authorization: Bearer` — the server tells them apart only by which router
// the request lands on. Nothing in the transport distinguishes them, so the app
// has to, or a customer's token eventually goes to a staff route.
export type RouteFamily = 'staff' | 'customer';

/** Which half of the API a path belongs to. */
export function routeFamily(path: string): RouteFamily {
  return /^\/portal(\/|$)/.test(path) ? 'customer' : 'staff';
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID.test(value);
}
'''

# ------------------------------------------------------------------- storage

STORAGE_TS = '''// Two stores, because two kinds of value.
//
// The access and refresh tokens go to the platform keystore (Keychain on iOS,
// Android Keystore behind EncryptedSharedPreferences). AsyncStorage is an
// unencrypted SQLite file inside the app sandbox and lands in device backups,
// so it holds only the things that are not secret: which server, which tenant,
// who was signed in.
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

// expo-secure-store has no web implementation. Web is a development target for
// this scaffold, not a shipping one — see the README.
const HAS_KEYSTORE = Platform.OS === 'ios' || Platform.OS === 'android';

export const K = {
  baseUrl: 'velaris_base_url',
  tenant: 'velaris_tenant',
  origin: 'velaris_session_origin',
  kind: 'velaris_session_kind',
  user: 'velaris_user',
  token: 'velaris_token',
  refresh: 'velaris_refresh_token',
};

// Keys earlier builds of this app wrote, in the order they were used. Each is
// read once, moved to its new home, and deleted — a stale copy of a token must
// never outlive the sign-out that cleared the current one.
const LEGACY: Record<string, string[]> = {
  velaris_base_url: ['helix_base_url'],
  velaris_tenant: ['helix_tenant'],
  velaris_token: ['helix_token'],
};

export async function getPlain(key: string): Promise<string | null> {
  try {
    const current = await AsyncStorage.getItem(key);
    if (current !== null) return current;
    for (const old of LEGACY[key] || []) {
      const value = await AsyncStorage.getItem(old);
      await AsyncStorage.removeItem(old);
      if (value !== null) {
        await AsyncStorage.setItem(key, value);
        return value;
      }
    }
    return null;
  } catch {
    return null;
  }
}

export async function setPlain(key: string, value: string): Promise<void> {
  try {
    await AsyncStorage.setItem(key, value);
  } catch {
    /* storage unavailable — the value is still live in memory for this session */
  }
}

export async function getSecret(key: string): Promise<string | null> {
  try {
    if (HAS_KEYSTORE) {
      const fromKeystore = await SecureStore.getItemAsync(key);
      if (fromKeystore !== null) return fromKeystore;
    }
    // An app installed before this build kept its token in AsyncStorage in the
    // clear. Carry it over into the keystore, then wipe the plaintext copy.
    const plain = await getPlain(key);
    if (plain === null) return null;
    await AsyncStorage.removeItem(key);
    if (HAS_KEYSTORE) await SecureStore.setItemAsync(key, plain);
    return plain;
  } catch {
    return null;
  }
}

export async function setSecret(key: string, value: string): Promise<void> {
  try {
    if (HAS_KEYSTORE) await SecureStore.setItemAsync(key, value);
    else await AsyncStorage.setItem(key, value);
  } catch {
    /* keystore unavailable — the session stays in memory only */
  }
}

export async function deleteSecret(key: string): Promise<void> {
  try {
    if (HAS_KEYSTORE) await SecureStore.deleteItemAsync(key);
    // Always clear the AsyncStorage copy too: an upgraded install may still
    // have one, and sign-out has to remove every copy, not the newest one.
    await AsyncStorage.removeItem(key);
  } catch {
    /* nothing further we can do */
  }
}

export async function deletePlain(key: string): Promise<void> {
  try {
    await AsyncStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}
'''

# ----------------------------------------------------------------- endpoints

ENDPOINTS_TS = '''// Every server path this app knows, in one place.
//
// A generated app that calls a route the server does not have fails at the
// worst possible moment — on a customer's phone. The build checks each string
// below against the live route table, which only works while they all live
// here. Add a call by adding it here.
export const EP = {
  login: '/auth/real/login',
  refresh: '/auth/real/refresh',
  logout: '/auth/real/logout',
  cases: '/cases',
  caseTypes: '/case-types',
  myAssignments: '/my/assignments',
  case: (id: string) => `/cases/${id}`,
  resolve: (id: string) => `/cases/${id}/resolve`,
};

/** The customer half. Every path is scoped to one portal slug. */
export const PORTAL = {
  register: (slug: string) => `/portal/${slug}/auth/register`,
  requestOtp: (slug: string) => `/portal/${slug}/auth/request-otp`,
  verifyOtp: (slug: string) => `/portal/${slug}/auth/verify-otp`,
  refresh: (slug: string) => `/portal/${slug}/auth/refresh`,
  account: (slug: string) => `/portal/${slug}/account`,
  config: (slug: string) => `/portal/${slug}`,
  form: (slug: string, caseTypeId: string) => `/portal/${slug}/case-types/${caseTypeId}/form`,
  submit: (slug: string) => `/portal/${slug}/submit`,
  cases: (slug: string) => `/portal/${slug}/account/cases`,
  case: (slug: string, id: string) => `/portal/${slug}/account/cases/${id}`,
  timeline: (slug: string, id: string) => `/portal/${slug}/account/cases/${id}/timeline`,
  messages: (slug: string, id: string) => `/portal/${slug}/account/cases/${id}/messages`,
  documents: (slug: string, id: string) => `/portal/${slug}/account/cases/${id}/documents`,
};
'''

# ----------------------------------------------------------------- api layer

API_CONTEXT = '''import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { DEFAULT_API_URL, DEFAULT_TENANT, PORTAL_SLUG } from './config';
import { EP, PORTAL } from './endpoints';
import { checkBaseUrl, originOf, pathIsSafe, routeFamily } from './url';
import type { RouteFamily } from './url';
import { K, deletePlain, deleteSecret, getPlain, getSecret, setPlain, setSecret } from './storage';

export interface User {
  user_id: string;
  username: string;
  display_name?: string;
  email?: string;
  roles?: string[];
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/** Staff sign in against the platform; customers against a portal. */
export type SessionKind = RouteFamily;

const SESSION_KINDS: SessionKind[] = ['staff', 'customer'];

interface Session {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;   // epoch ms
  origin: string;      // the server this session was issued by
  kind: SessionKind;   // which half of the API this token is for
  user: User | null;
}

// A tagged union, so a caller cannot read `.error` off a variant that has
// none. Optional discriminants (`mfaRequired?: false`) do not narrow.
export type SignInResult =
  | { ok: true }
  | { ok: false; reason: 'mfa' }
  | { ok: false; reason: 'sso'; error: string }
  | { ok: false; reason: 'failed'; error: string };

export type OtpResult = { ok: true } | { ok: false; error: string };

//: The server's stable code for "this account exists but has no password —
//: it signs in through an identity provider". Matched on the code rather than
//: the message, which is prose and may be reworded.
const SSO_REQUIRED_CODE = 'VEL-AUTH-SSO-REQUIRED-400';

//: Every customer route answers 404 when the Customer Accounts feature is off
//: for the deployment. Without this the app would report "not found", which
//: reads as a broken app rather than a feature nobody switched on.
const CUSTOMER_ACCOUNTS_OFF =
  'This Velaris server does not have customer accounts enabled. Ask your administrator, or sign in with a staff account.';

interface ApiState {
  ready: boolean;
  baseUrl: string;
  tenantSlug: string;
  user: User | null;
  signedIn: boolean;
  sessionKind: SessionKind | null;
  setServer: (baseUrl: string, tenant: string) => Promise<string | null>;
  signIn: (username: string, password: string, mfaToken?: string) => Promise<SignInResult>;
  /** Null until the portal has been asked, so the UI can wait rather than guess. */
  selfRegistration: boolean | null;
  requestOtp: (email: string) => Promise<OtpResult>;
  registerCustomer: (email: string, displayName: string) => Promise<OtpResult>;
  // Narrower than `signIn` on purpose: a one-time code has no second
  // factor and no federated branch, so those variants cannot occur.
  signInWithOtp: (email: string, otp: string) => Promise<OtpResult>;
  signOut: () => Promise<void>;
  request: (path: string, opts?: RequestInit) => Promise<any>;
}

const ApiCtx = createContext<ApiState | null>(null);

export function useApi() {
  const ctx = useContext(ApiCtx);
  if (!ctx) throw new Error('useApi must be inside ApiProvider');
  return ctx;
}

const EXPIRY_SKEW_MS = 60_000;   // refresh a minute early rather than fail a call

export function ApiProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [baseUrl, setBaseUrlState] = useState(DEFAULT_API_URL);
  const [tenantSlug, setTenantSlugState] = useState(DEFAULT_TENANT);
  const [user, setUser] = useState<User | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [sessionKind, setSessionKind] = useState<SessionKind | null>(null);
  const [selfRegistration, setSelfRegistration] = useState<boolean | null>(null);

  // `request` must never be rebuilt, because screens hold on to it: an earlier
  // version of this app captured the base URL and token from first mount, so
  // every list screen kept calling the build-time default with no credentials
  // no matter what Settings said. Everything it reads lives in a ref.
  const baseRef = useRef(DEFAULT_API_URL);
  const tenantRef = useRef(DEFAULT_TENANT);
  const sessionRef = useRef<Session | null>(null);
  const refreshInFlight = useRef<Promise<boolean> | null>(null);

  const applySession = useCallback(async (s: Session | null) => {
    sessionRef.current = s;
    setSignedIn(!!s);
    setUser(s ? s.user : null);
    setSessionKind(s ? s.kind : null);
    if (s) {
      await setSecret(K.token, s.accessToken);
      await setSecret(K.refresh, s.refreshToken);
      await setPlain(K.origin, s.origin);
      await setPlain(K.kind, s.kind);
      await setPlain(K.user, JSON.stringify({ user: s.user, expiresAt: s.expiresAt }));
    } else {
      await deleteSecret(K.token);
      await deleteSecret(K.refresh);
      await deletePlain(K.origin);
      await deletePlain(K.kind);
      await deletePlain(K.user);
    }
  }, []);

  // Load stored settings before anything renders. Screens used to mount and
  // fire their first request in the same tick as this, i.e. always against the
  // defaults; `ready` is what stops that.
  useEffect(() => {
    (async () => {
      const storedBase = await getPlain(K.baseUrl);
      const storedTenant = await getPlain(K.tenant);
      if (storedBase) {
        const checked = checkBaseUrl(storedBase);
        if (checked.ok) {
          baseRef.current = checked.baseUrl;
          setBaseUrlState(checked.baseUrl);
        }
      }
      if (storedTenant) {
        tenantRef.current = storedTenant;
        setTenantSlugState(storedTenant);
      }

      const token = await getSecret(K.token);
      const refresh = await getSecret(K.refresh);
      const origin = await getPlain(K.origin);
      const meta = await getPlain(K.user);
      // A session written before this app knew about kinds can only be a staff
      // one — that was the only kind that existed. An unrecognised value is not
      // guessed at: an unusable session is better than a misrouted token.
      const storedKind = await getPlain(K.kind);
      const kind: SessionKind | null =
        storedKind === null ? 'staff'
        : (SESSION_KINDS as string[]).indexOf(storedKind) !== -1 ? (storedKind as SessionKind)
        : null;
      // A stored session belongs to the server that issued it. If the app now
      // points somewhere else, that token is not this server's to receive.
      if (token && refresh && origin && kind && origin === originOf(baseRef.current)) {
        let parsed: any = {};
        try { parsed = meta ? JSON.parse(meta) : {}; } catch { parsed = {}; }
        sessionRef.current = {
          accessToken: token,
          refreshToken: refresh,
          expiresAt: Number(parsed.expiresAt) || 0,
          origin,
          kind,
          user: parsed.user || null,
        };
        setUser(parsed.user || null);
        setSignedIn(true);
        setSessionKind(kind);
      } else if (token || refresh) {
        await deleteSecret(K.token);
        await deleteSecret(K.refresh);
        await deletePlain(K.origin);
        await deletePlain(K.kind);
        await deletePlain(K.user);
      }
      setReady(true);
    })();
  }, []);

  // Ask the portal whether it accepts self-registration, so the sign-in screen
  // does not offer an option the server will refuse. Anonymous, and a failure
  // leaves it null — unknown, rather than a guess in either direction.
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(url(PORTAL.config(PORTAL_SLUG)), {
          headers: { 'X-Tenant-Slug': tenantRef.current },
        });
        if (!resp.ok) return;
        const body = await resp.json();
        if (!cancelled && typeof body.self_registration === 'boolean') {
          setSelfRegistration(body.self_registration);
        }
      } catch {
        /* offline, or no portal here — the screen simply does not offer it */
      }
    })();
    return () => { cancelled = true; };
  }, [ready, baseUrl, tenantSlug]);

  const url = (path: string) => baseRef.current + '/api/v1' + path;

  const sessionFrom = (
    body: any, origin: string, kind: SessionKind, fallbackUser: User | null,
  ): Session => ({
    accessToken: body.access_token,
    refreshToken: body.refresh_token || '',
    expiresAt: Date.now() + (Number(body.expires_in) || 900) * 1000,
    origin,
    kind,
    user: body.user || fallbackUser,
  });

  const doRefresh = useCallback(async (): Promise<boolean> => {
    const current = sessionRef.current;
    if (!current) return false;
    // A customer session has no refresh token by design — the server mints no
    // long-lived credential. A still-valid token buys another 24h; an expired
    // one buys nothing, which is why the renewal happens before the deadline.
    if (current.kind === 'staff' && !current.refreshToken) return false;
    if (refreshInFlight.current) return refreshInFlight.current;

    refreshInFlight.current = (async () => {
      try {
        const isCustomer = current.kind === 'customer';
        const headers: Record<string, string> = {
          'Content-Type': 'application/json',
          'X-Tenant-Slug': tenantRef.current,
        };
        if (isCustomer) headers['Authorization'] = 'Bearer ' + current.accessToken;

        const resp = await fetch(url(isCustomer ? PORTAL.refresh(PORTAL_SLUG) : EP.refresh), {
          method: 'POST',
          headers,
          body: isCustomer ? '{}' : JSON.stringify({ refresh_token: current.refreshToken }),
        });
        if (!resp.ok) return false;
        const body = await resp.json();

        if (isCustomer) {
          if (!body || !body.customer_token) return false;
          await applySession({
            ...current,
            accessToken: body.customer_token,
            expiresAt: Date.now() + 24 * 60 * 60 * 1000,
          });
          return true;
        }

        if (!body || !body.access_token) return false;
        await applySession(sessionFrom(body, current.origin, current.kind, current.user));
        return true;
      } catch {
        return false;   // offline: keep the session, let the caller surface it
      } finally {
        refreshInFlight.current = null;
      }
    })();
    return refreshInFlight.current;
  }, [applySession]);

  const request = useCallback(async (path: string, opts: RequestInit = {}): Promise<any> => {
    if (!pathIsSafe(path)) throw new ApiError('Blocked a malformed request.', 0);

    // A session is for one half of the API. Both halves read the same
    // `Authorization: Bearer` header, so nothing downstream would notice a
    // customer token arriving at a staff route — this is the only place that
    // can. Refused rather than sent unauthenticated: a screen calling the wrong
    // family is a bug, and a 401 would hide it behind a plausible-looking
    // "please sign in".
    const family = routeFamily(path);
    const current = sessionRef.current;
    if (current && current.kind !== family) {
      throw new ApiError('Blocked a request meant for a different sign-in.', 0);
    }

    const send = async (): Promise<Response> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        'X-Tenant-Slug': tenantRef.current,
        ...((opts.headers as Record<string, string>) || {}),
      };
      const s = sessionRef.current;
      // The token is the server's, not the app's: it goes to the origin that
      // issued it and to no other, however the base URL has been edited since —
      // and only to the half of the API it was issued for.
      if (s && s.origin === originOf(baseRef.current) && s.kind === family) {
        headers['Authorization'] = 'Bearer ' + s.accessToken;
      }
      return fetch(url(path), { ...opts, headers });
    };

    if (current && current.expiresAt && current.expiresAt - Date.now() < EXPIRY_SKEW_MS) {
      await doRefresh();
    }

    let resp: Response;
    try {
      resp = await send();
    } catch {
      // fetch only rejects when the request never completed.
      throw new ApiError('Cannot reach the server. Check the URL in Settings and your connection.', 0);
    }

    if (resp.status === 401 && sessionRef.current) {
      if (await doRefresh()) {
        resp = await send();
      }
      if (resp.status === 401) {
        await applySession(null);
        throw new ApiError('Your session has ended. Please sign in again.', 401);
      }
    }

    if (!resp.ok) {
      let detail = '';
      try {
        const body = await resp.json();
        if (body && typeof body.detail === 'string') detail = body.detail;
      } catch {
        /* not JSON — the status is all we have */
      }
      throw new ApiError(detail || 'The server refused that request (' + resp.status + ').', resp.status);
    }

    const txt = await resp.text();
    return txt ? JSON.parse(txt) : null;
  }, [applySession, doRefresh]);

  const setServer = useCallback(async (nextBase: string, nextTenant: string): Promise<string | null> => {
    const checked = checkBaseUrl(nextBase);
    if (!checked.ok) return checked.reason;
    const tenant = (nextTenant || '').trim();
    if (!/^[a-z0-9][a-z0-9-]*$/.test(tenant)) {
      return 'Tenant slug may use lower-case letters, numbers and hyphens.';
    }

    // Pointing the app at a different server ends the session here rather than
    // carrying a token towards a host that never issued it.
    if (sessionRef.current && sessionRef.current.origin !== checked.origin) {
      await applySession(null);
    }
    baseRef.current = checked.baseUrl;
    tenantRef.current = tenant;
    setBaseUrlState(checked.baseUrl);
    setTenantSlugState(tenant);
    await setPlain(K.baseUrl, checked.baseUrl);
    await setPlain(K.tenant, tenant);
    return null;
  }, [applySession]);

  const signIn = useCallback(async (
    username: string,
    password: string,
    mfaToken?: string,
  ): Promise<SignInResult> => {
    const origin = originOf(baseRef.current);
    if (!origin) return { ok: false, reason: 'failed', error: 'Set a valid server URL first.' };

    let resp: Response;
    try {
      resp = await fetch(url(EP.login), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Tenant-Slug': tenantRef.current },
        body: JSON.stringify(
          mfaToken ? { username, password, mfa_token: mfaToken } : { username, password },
        ),
      });
    } catch {
      return { ok: false, reason: 'failed', error: 'Cannot reach ' + origin + '. Check the server URL and your connection.' };
    }

    if (!resp.ok) {
      let detail = '';
      let code = '';
      try {
        const body = await resp.json();
        if (body && typeof body.detail === 'string') detail = body.detail;
        if (body && body.error && typeof body.error.code === 'string') code = body.error.code;
      } catch {
        /* fall through to the status-only message */
      }
      // The account exists and signs in through an identity provider. The
      // server already distinguishes this from a wrong password, so the app
      // does not need to ask anything before the attempt.
      if (code === SSO_REQUIRED_CODE) {
        return {
          ok: false, reason: 'sso',
          error: 'This account signs in through an identity provider at your organisation. Sign in on the web for now.',
        };
      }
      return { ok: false, reason: 'failed', error: detail || 'Sign-in failed (' + resp.status + ').' };
    }

    const body = await resp.json();
    if (body.mfa_required && !body.access_token) return { ok: false, reason: 'mfa' };
    if (!body.access_token) return { ok: false, reason: 'failed', error: 'The server did not return a session.' };

    await applySession(sessionFrom(body, origin, 'staff', body.user || null));
    return { ok: true };
  }, [applySession]);

  /** POST to a portal auth route. These are anonymous — no session yet. */
  const portalPost = useCallback(async (path: string, payload: any): Promise<
    { ok: true; body: any } | { ok: false; error: string }
  > => {
    const origin = originOf(baseRef.current);
    if (!origin) return { ok: false, error: 'Set a valid server URL first.' };
    let resp: Response;
    try {
      resp = await fetch(url(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Tenant-Slug': tenantRef.current },
        body: JSON.stringify(payload),
      });
    } catch {
      return { ok: false, error: 'Cannot reach ' + origin + '. Check the server URL and your connection.' };
    }
    if (resp.status === 404) return { ok: false, error: CUSTOMER_ACCOUNTS_OFF };
    if (!resp.ok) {
      let detail = '';
      try {
        const body = await resp.json();
        if (body && typeof body.detail === 'string') detail = body.detail;
      } catch {
        /* status only */
      }
      return { ok: false, error: detail || 'That did not work (' + resp.status + ').' };
    }
    return { ok: true, body: await resp.json() };
  }, []);

  const requestOtp = useCallback(async (email: string): Promise<OtpResult> => {
    // The server answers the same way whether or not the address has an
    // account, so the app must not add a difference of its own.
    const r = await portalPost(PORTAL.requestOtp(PORTAL_SLUG), { email: email.trim() });
    return r.ok ? { ok: true } : { ok: false, error: r.error };
  }, [portalPost]);

  const registerCustomer = useCallback(async (
    email: string, displayName: string,
  ): Promise<OtpResult> => {
    const r = await portalPost(PORTAL.register(PORTAL_SLUG), {
      email: email.trim(), display_name: displayName.trim(),
    });
    return r.ok ? { ok: true } : { ok: false, error: r.error };
  }, [portalPost]);

  const signInWithOtp = useCallback(async (
    email: string, otp: string,
  ): Promise<OtpResult> => {
    const origin = originOf(baseRef.current);
    const r = await portalPost(PORTAL.verifyOtp(PORTAL_SLUG), {
      email: email.trim(), otp: otp.trim(),
    });
    if (!r.ok) return { ok: false, error: r.error };
    const token = r.body && r.body.customer_token;
    if (!token) return { ok: false, error: 'The server did not return a session.' };

    const c = r.body.customer || {};
    // The customer token is a flat 24h JWT with no refresh token — a still
    // valid one buys another 24h, an expired one buys nothing. `expiresAt` is
    // what makes the app renew before that deadline rather than after it.
    await applySession({
      accessToken: token,
      refreshToken: '',
      expiresAt: Date.now() + 24 * 60 * 60 * 1000,
      origin,
      kind: 'customer',
      user: {
        user_id: String(c.id || ''),
        username: String(c.primary_email || email),
        display_name: c.display_name || undefined,
        email: c.primary_email || undefined,
      },
    });
    return { ok: true };
  }, [applySession, portalPost]);

  const signOut = useCallback(async () => {
    const s = sessionRef.current;
    // A customer session has nothing to revoke server-side — there is no
    // long-lived credential to retire, and the token dies on its own within
    // 24h. Clearing this device is the whole of it.
    if (s && s.kind === 'staff' && s.refreshToken) {
      try {
        // Best effort: the local session is cleared either way, but telling the
        // server lets it retire the refresh token instead of leaving it live.
        await fetch(url(EP.logout), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Tenant-Slug': tenantRef.current,
            Authorization: 'Bearer ' + s.accessToken,
          },
          body: JSON.stringify({ refresh_token: s.refreshToken }),
        });
      } catch {
        /* offline sign-out still clears this device */
      }
    }
    await applySession(null);
  }, [applySession]);

  return (
    <ApiCtx.Provider
      value={{ ready, baseUrl, tenantSlug, user, signedIn, sessionKind,
               selfRegistration, setServer, signIn, requestOtp, registerCustomer,
               signInWithOtp, signOut, request }}>
      {children}
    </ApiCtx.Provider>
  );
}
'''

# -------------------------------------------------------------------- App.tsx

APP_TSX = '''import React from 'react';
import { ActivityIndicator, View } from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { createStackNavigator } from '@react-navigation/stack';
import { StatusBar } from 'expo-status-bar';
import { ApiProvider, useApi } from './src/api/ApiContext';
import { APP_NAME, PRIMARY_COLOR } from './src/api/config';
import SettingsScreen from './src/screens/SettingsScreen';
import LoginScreen from './src/screens/LoginScreen';
import CaseListScreen from './src/screens/CaseListScreen';        // @staff
import CaseDetailScreen from './src/screens/CaseDetailScreen';    // @staff
import CreateCaseScreen from './src/screens/CreateCaseScreen';    // @staff
import MyWorkScreen from './src/screens/MyWorkScreen';            // @staff
import RequestsScreen from './src/screens/RequestsScreen';          // @customer
import NewRequestScreen from './src/screens/NewRequestScreen';      // @customer
import RequestDetailScreen from './src/screens/RequestDetailScreen'; // @customer

const Tab = createBottomTabNavigator();
const Stack = createStackNavigator();

// @staff-block-start
function CasesStack() {
  return (
    <Stack.Navigator
      screenOptions={{ headerStyle: { backgroundColor: PRIMARY_COLOR }, headerTintColor: '#fff' }}>
      <Stack.Screen name="Cases" component={CaseListScreen} options={{ title: APP_NAME }} />
      <Stack.Screen name="CaseDetail" component={CaseDetailScreen} options={{ title: 'Case Detail' }} />
      <Stack.Screen name="CreateCase" component={CreateCaseScreen} options={{ title: 'New Case' }} />
    </Stack.Navigator>
  );
}

function StaffTabs() {
  return (
    <Tab.Navigator screenOptions={{ tabBarActiveTintColor: PRIMARY_COLOR, headerShown: false }}>
      <Tab.Screen name="CasesTab" component={CasesStack} options={{ title: 'Cases' }} />
      <Tab.Screen name="MyWork" component={MyWorkScreen} options={{ title: 'My Work' }} />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ title: 'Settings' }} />
    </Tab.Navigator>
  );
}

// @staff-block-end

// @customer-block-start
function RequestsStack() {
  return (
    <Stack.Navigator
      screenOptions={{ headerStyle: { backgroundColor: PRIMARY_COLOR }, headerTintColor: '#fff' }}>
      <Stack.Screen name="Requests" component={RequestsScreen} options={{ title: 'My Requests' }} />
      <Stack.Screen name="NewRequest" component={NewRequestScreen} options={{ title: 'New Request' }} />
      <Stack.Screen name="RequestDetail" component={RequestDetailScreen} options={{ title: 'Request' }} />
    </Stack.Navigator>
  );
}

function CustomerTabs() {
  return (
    <Tab.Navigator screenOptions={{ tabBarActiveTintColor: PRIMARY_COLOR, headerShown: false }}>
      <Tab.Screen name="RequestsTab" component={RequestsStack} options={{ title: 'My Requests' }} />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ title: 'Settings' }} />
    </Tab.Navigator>
  );
}

// @customer-block-end

/** Nothing renders until the stored server and session are back from storage.
 *
 *  Which shell you get follows the **kind of session**, not a role name or a
 *  build flag: a customer token can only reach `/portal/…` routes, so showing
 *  a customer the staff shell would be a screen full of refusals. */
function Root() {
  const { ready, signedIn, sessionKind } = useApi();

  if (!ready) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: '#f5f5f7' }}>
        <ActivityIndicator size="large" color={PRIMARY_COLOR} />
      </View>
    );
  }
  if (!signedIn) return <LoginScreen />;
  return sessionKind === 'customer' ? <CustomerTabs /> : <StaffTabs />;   // @both
}

export default function App() {
  return (
    <ApiProvider>
      <NavigationContainer>
        <StatusBar style="light" />
        <Root />
      </NavigationContainer>
    </ApiProvider>
  );
}
'''

# -------------------------------------------------------------------- screens

LOGIN_SCREEN = '''import React, { useState } from 'react';
import {
  ActivityIndicator, KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, TextInput,
  TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { APP_NAME, APP_VERSION, PRIMARY_COLOR } from '../api/config';

// One screen, both audiences. Staff type a password; a customer taps "Email me
// a code" and never has one, because the server issues none — customer sign-in
// is a one-time code by design.
//
// Nothing here asks who you are before you try. Routing by identifier would
// mean telling any anonymous caller whether an address belongs to staff, and
// the server already separates a password account from a federated one *after*
// an attempt, which is a much smaller thing to disclose.
type Mode = 'password' | 'code';

export default function LoginScreen() {
  const api = useApi();
  const [mode, setMode] = useState<Mode>('password');

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [mfaToken, setMfaToken] = useState('');
  const [needsMfa, setNeedsMfa] = useState(false);

  const [code, setCode] = useState('');
  const [codeSent, setCodeSent] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [displayName, setDisplayName] = useState('');

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  // The server has to be settable before sign-in — there is nowhere else to do
  // it while the rest of the app is behind the login.
  const [showServer, setShowServer] = useState(false);
  const [baseUrl, setBaseUrl] = useState(api.baseUrl);
  const [tenant, setTenant] = useState(api.tenantSlug);

  const clear = () => { setError(''); setNotice(''); };

  const saveServer = async () => {
    const problem = await api.setServer(baseUrl, tenant);
    setError(problem || '');
    if (!problem) { setNotice('Server updated.'); setShowServer(false); }
  };

  const switchMode = (next: Mode) => {
    setMode(next);
    clear();
    setNeedsMfa(false);
    setCodeSent(false);
    setRegistering(false);
    setCode('');
  };

  const submitPassword = async () => {
    setBusy(true); clear();
    const result = await api.signIn(
      identifier.trim(), password, needsMfa ? mfaToken.trim() : undefined,
    );
    setBusy(false);
    if (result.ok) return;
    if (result.reason === 'mfa') {
      setNeedsMfa(true);
      setNotice('Enter the code from your authenticator app.');
      return;
    }
    setError(result.error);
  };

  const sendCode = async () => {
    setBusy(true); clear();
    const result = registering
      ? await api.registerCustomer(identifier, displayName)
      : await api.requestOtp(identifier);
    setBusy(false);
    if (!result.ok) { setError(result.error); return; }
    setCodeSent(true);
    setNotice('If that address has an account, a code is on its way. It expires in 10 minutes.');
  };

  const submitCode = async () => {
    setBusy(true); clear();
    const result = await api.signInWithOtp(identifier, code);
    setBusy(false);
    if (!result.ok) setError(result.error);
  };

  const canSubmit = mode === 'password'
    ? !!identifier.trim() && !!password
    : codeSent ? !!code.trim() : !!identifier.trim() && (!registering || !!displayName.trim());

  return (
    <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>{APP_NAME}</Text>
        <Text style={styles.subtitle}>Sign in to continue</Text>

        <Text style={styles.label}>{mode === 'password' ? 'Email or username' : 'Email'}</Text>
        <TextInput
          style={styles.input} value={identifier} onChangeText={setIdentifier}
          editable={!codeSent}
          autoCapitalize="none" autoCorrect={false}
          keyboardType={mode === 'code' ? 'email-address' : 'default'}
          textContentType="username" />

        {mode === 'password' && (
          <>
            <Text style={styles.label}>Password</Text>
            <TextInput
              style={styles.input} value={password} onChangeText={setPassword}
              autoCapitalize="none" autoCorrect={false} secureTextEntry textContentType="password" />

            {needsMfa && (
              <>
                <Text style={styles.label}>Authentication code</Text>
                <TextInput
                  style={styles.input} value={mfaToken} onChangeText={setMfaToken}
                  autoCapitalize="none" autoCorrect={false} keyboardType="number-pad" maxLength={6} />
              </>
            )}
          </>
        )}

        {mode === 'code' && registering && !codeSent && (
          <>
            <Text style={styles.label}>Your name</Text>
            <TextInput
              style={styles.input} value={displayName} onChangeText={setDisplayName}
              autoCorrect={false} textContentType="name" />
          </>
        )}

        {mode === 'code' && codeSent && (
          <>
            <Text style={styles.label}>Code from your email</Text>
            <TextInput
              style={styles.input} value={code} onChangeText={setCode}
              autoCapitalize="none" autoCorrect={false} keyboardType="number-pad" maxLength={8} />
          </>
        )}

        {!!error && <Text style={styles.error}>{error}</Text>}
        {!!notice && <Text style={styles.notice}>{notice}</Text>}

        <TouchableOpacity
          style={[styles.button, (!canSubmit || busy) && styles.buttonDisabled]}
          onPress={mode === 'password' ? submitPassword : codeSent ? submitCode : sendCode}
          disabled={!canSubmit || busy}>
          {busy
            ? <ActivityIndicator color="white" />
            : <Text style={styles.buttonText}>
                {mode === 'password' ? 'Sign In' : codeSent ? 'Sign In' : registering ? 'Create Account' : 'Email Me a Code'}
              </Text>}
        </TouchableOpacity>

        {mode === 'password' && (
          <TouchableOpacity onPress={() => switchMode('code')}>
            <Text style={styles.link}>Email me a code instead</Text>
          </TouchableOpacity>
        )}

        {mode === 'code' && (
          <>
            {codeSent && (
              <TouchableOpacity onPress={() => { setCodeSent(false); setCode(''); clear(); }}>
                <Text style={styles.link}>Use a different address</Text>
              </TouchableOpacity>
            )}
            {!codeSent && api.selfRegistration === true && (
              <TouchableOpacity onPress={() => { setRegistering(!registering); clear(); }}>
                <Text style={styles.link}>
                  {registering ? 'I already have an account' : 'I do not have an account yet'}
                </Text>
              </TouchableOpacity>
            )}
            {!codeSent && api.selfRegistration === false && (
              <Text style={styles.hint}>
                Accounts here are created by the team. Ask them to set one up,
                then sign in with a code.
              </Text>
            )}
            <TouchableOpacity onPress={() => switchMode('password')}>
              <Text style={styles.link}>Sign in with a password</Text>
            </TouchableOpacity>
          </>
        )}

        <TouchableOpacity onPress={() => setShowServer(!showServer)}>
          <Text style={styles.serverLine}>{api.baseUrl} · {api.tenantSlug}</Text>
        </TouchableOpacity>

        {showServer && (
          <View style={styles.serverBox}>
            <Text style={styles.label}>Server URL</Text>
            <TextInput
              style={styles.input} value={baseUrl} onChangeText={setBaseUrl}
              autoCapitalize="none" autoCorrect={false} keyboardType="url" />
            <Text style={styles.label}>Tenant</Text>
            <TextInput
              style={styles.input} value={tenant} onChangeText={setTenant}
              autoCapitalize="none" autoCorrect={false} />
            <TouchableOpacity style={[styles.button, styles.buttonSecondary]} onPress={saveServer}>
              <Text style={[styles.buttonText, styles.buttonSecondaryText]}>Use this server</Text>
            </TouchableOpacity>
          </View>
        )}

        <Text style={styles.version}>v{APP_VERSION}</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: '#f5f5f7' },
  container: { padding: 24, paddingTop: 72, paddingBottom: 40 },
  title: { fontSize: 28, fontWeight: '700', color: '#222' },
  subtitle: { fontSize: 14, color: '#888', marginTop: 4, marginBottom: 16 },
  label: { fontSize: 11, color: '#888', marginTop: 14, marginBottom: 4, textTransform: 'uppercase', letterSpacing: 0.5 },
  input: { backgroundColor: 'white', padding: 12, borderRadius: 6, fontSize: 14, borderWidth: 1, borderColor: '#e0e0e0' },
  error: { color: '#c0392b', fontSize: 13, marginTop: 14 },
  notice: { color: '#1e8449', fontSize: 13, marginTop: 14 },
  button: { backgroundColor: PRIMARY_COLOR, padding: 15, borderRadius: 8, alignItems: 'center', marginTop: 22 },
  buttonDisabled: { opacity: 0.5 },
  buttonSecondary: { backgroundColor: 'transparent', borderWidth: 1, borderColor: PRIMARY_COLOR },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  buttonSecondaryText: { color: PRIMARY_COLOR },
  link: { textAlign: 'center', color: PRIMARY_COLOR, fontSize: 13, fontWeight: '600', marginTop: 16 },
  hint: { textAlign: 'center', fontSize: 12, color: '#888', marginTop: 16, lineHeight: 17 },
  serverLine: { textAlign: 'center', fontSize: 11, color: '#888', marginTop: 22 },
  serverBox: { marginTop: 8, padding: 14, backgroundColor: '#ededf0', borderRadius: 8 },
  version: { textAlign: 'center', fontSize: 11, color: '#aaa', marginTop: 28 },
});
'''

REQUESTS_SCREEN = '''import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import { useApi } from '../api/ApiContext';
import { PORTAL } from '../api/endpoints';
import { PORTAL_SLUG, PRIMARY_COLOR } from '../api/config';

export default function RequestsScreen({ navigation }: any) {
  const { request } = useApi();
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await request(PORTAL.cases(PORTAL_SLUG));
      setItems((data && data.cases) || []);
      setError('');
    } catch (e: any) {
      setError(e.message || 'Could not load your requests.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [request]);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }

  return (
    <View style={styles.container}>
      {!!error && (
        <TouchableOpacity style={styles.errorBox} onPress={() => { setLoading(true); load(); }}>
          <Text style={styles.errorText}>{error}</Text>
          <Text style={styles.errorRetry}>Tap to try again</Text>
        </TouchableOpacity>
      )}
      <FlatList
        data={items}
        keyExtractor={(item, i) => item.case_id || String(i)}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} />
        }
        renderItem={({ item }) => (
          <TouchableOpacity
            style={styles.card}
            onPress={() => navigation.navigate('RequestDetail', { caseId: item.case_id })}>
            <Text style={styles.subject}>{item.subject || 'Your request'}</Text>
            <View style={styles.cardMeta}>
              <Text style={styles.ref}>{item.case_number || ''}</Text>
              <Text style={[styles.status, { color: statusColor(item.status) }]}>
                {String(item.status || '').replace(/_/g, ' ')}
              </Text>
            </View>
          </TouchableOpacity>
        )}
        ListEmptyComponent={
          error ? null : (
            <View style={styles.emptyBox}>
              <Text style={styles.empty}>You have not raised anything yet.</Text>
              <TouchableOpacity onPress={() => navigation.navigate('NewRequest')}>
                <Text style={styles.emptyLink}>Raise a request</Text>
              </TouchableOpacity>
            </View>
          )
        }
      />
      <TouchableOpacity style={styles.fab} onPress={() => navigation.navigate('NewRequest')}>
        <Text style={styles.fabText}>+</Text>
      </TouchableOpacity>
    </View>
  );
}

function statusColor(s: string): string {
  return ({ new: '#2d6cdf', open: '#2d6cdf', in_progress: '#f7b731',
            resolved: '#1e8449', closed: '#7f8c8d' } as any)[s] || '#555';
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  errorBox: { backgroundColor: '#fdecea', borderRadius: 8, padding: 12, margin: 12 },
  errorText: { color: '#c0392b', fontSize: 13 },
  errorRetry: { color: '#c0392b', fontSize: 11, marginTop: 4, opacity: 0.8 },
  card: { backgroundColor: 'white', padding: 16, marginHorizontal: 12, marginVertical: 4, borderRadius: 8 },
  subject: { fontSize: 15, fontWeight: '600', color: '#222' },
  cardMeta: { flexDirection: 'row', justifyContent: 'space-between', marginTop: 6 },
  ref: { fontSize: 11, color: '#888', fontFamily: 'monospace' },
  status: { fontSize: 12, fontWeight: '600', textTransform: 'capitalize' },
  emptyBox: { marginTop: 64, alignItems: 'center' },
  empty: { color: '#888', fontSize: 14 },
  emptyLink: { color: PRIMARY_COLOR, fontWeight: '600', marginTop: 10, fontSize: 14 },
  fab: { position: 'absolute', right: 20, bottom: 20, width: 56, height: 56,
         borderRadius: 28, backgroundColor: PRIMARY_COLOR, justifyContent: 'center',
         alignItems: 'center', shadowColor: '#000', shadowOpacity: 0.2, shadowRadius: 4, elevation: 5 },
  fabText: { color: 'white', fontSize: 28, fontWeight: '300' },
});
'''

NEW_REQUEST_SCREEN = '''import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, ScrollView, StyleSheet, Switch, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { PORTAL } from '../api/endpoints';
import { PORTAL_SLUG, PRIMARY_COLOR } from '../api/config';

// The form is whatever the case type says it is. Fields arrive flat from
// `/case-types/{id}/form` — key, label, type, required, placeholder, options —
// so editing a form in Form Builder changes this screen without a rebuild.
type Field = {
  key: string; label: string; type: string;
  required?: boolean; placeholder?: string; options?: any[];
};

export default function NewRequestScreen({ navigation }: any) {
  const { request, user } = useApi();
  const [caseTypes, setCaseTypes] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [fields, setFields] = useState<Field[]>([]);
  const [values, setValues] = useState<Record<string, any>>({});
  const [subject, setSubject] = useState('');
  const [description, setDescription] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadingForm, setLoadingForm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  // One id per attempt, reused across retries. The server treats a repeated
  // `client_ref` as the same submission — without it a flaky mobile connection
  // turns one tap into two cases.
  const clientRef = useRef(newId());

  const loadTypes = useCallback(async () => {
    try {
      const cfg = await request(PORTAL.config(PORTAL_SLUG));
      const list = (cfg && cfg.case_types) || [];
      setCaseTypes(list);
      if (list.length === 1) setSelected(list[0]);
      setError(list.length ? '' : 'This portal is not accepting requests right now.');
    } catch (e: any) {
      setError(e.message || 'Could not load the request types.');
    } finally {
      setLoading(false);
    }
  }, [request]);

  useEffect(() => { loadTypes(); }, [loadTypes]);

  useEffect(() => {
    if (!selected) { setFields([]); return; }
    let cancelled = false;
    setLoadingForm(true);
    (async () => {
      try {
        const data = await request(PORTAL.form(PORTAL_SLUG, selected.id));
        if (!cancelled) setFields(((data && data.fields) || []) as Field[]);
      } catch {
        if (!cancelled) setFields([]);   // no attached form is normal, not an error
      } finally {
        if (!cancelled) setLoadingForm(false);
      }
    })();
    return () => { cancelled = true; };
  }, [selected, request]);

  const setValue = (k: string, v: any) => setValues(prev => ({ ...prev, [k]: v }));

  const missing = fields.filter(f => f.required && !String(values[f.key] ?? '').trim());

  const submit = async () => {
    if (!selected) { setError('Choose what your request is about.'); return; }
    if (!subject.trim()) { setError('A short summary helps us route this.'); return; }
    if (missing.length) { setError('Please fill in: ' + missing.map(f => f.label).join(', ')); return; }

    setSubmitting(true);
    setError('');
    try {
      const result = await request(PORTAL.submit(PORTAL_SLUG), {
        method: 'POST',
        body: JSON.stringify({
          case_type_id: selected.id,
          submitter_name: (user && (user.display_name || user.username)) || '',
          submitter_email: (user && user.email) || '',
          subject: subject.trim(),
          description: description.trim(),
          priority: selected.default_priority || 'medium',
          extra_data: values,
          client_ref: clientRef.current,
        }),
      });
      const id = result && (result.case_id || result.id);
      if (id) navigation.replace('RequestDetail', { caseId: id });
      else navigation.goBack();
    } catch (e: any) {
      setError(e.message || 'Could not send your request.');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
                keyboardShouldPersistTaps="handled">
      {!!error && <Text style={styles.error}>{error}</Text>}

      <Text style={styles.label}>What is this about?</Text>
      <View style={styles.options}>
        {caseTypes.map(ct => (
          <TouchableOpacity
            key={ct.id} onPress={() => { setSelected(ct); setValues({}); }}
            style={[styles.option, selected && selected.id === ct.id && styles.optionSelected]}>
            <Text style={[styles.optionText, selected && selected.id === ct.id && styles.optionTextSelected]}>
              {ct.name}
            </Text>
          </TouchableOpacity>
        ))}
      </View>
      {!!(selected && selected.description) && (
        <Text style={styles.help}>{selected.description}</Text>
      )}

      <Text style={styles.label}>Summary</Text>
      <TextInput style={styles.input} value={subject} onChangeText={setSubject}
                 placeholder="One line — what do you need?" />

      <Text style={styles.label}>Details</Text>
      <TextInput style={[styles.input, styles.multiline]} value={description}
                 onChangeText={setDescription} multiline numberOfLines={4}
                 placeholder="Anything that would help us understand" />

      {loadingForm && <ActivityIndicator style={{ marginTop: 20 }} color={PRIMARY_COLOR} />}

      {fields.map(f => (
        <View key={f.key}>
          <Text style={styles.label}>{f.label}{f.required ? ' *' : ''}</Text>
          {renderField(f, values[f.key], v => setValue(f.key, v))}
        </View>
      ))}

      <TouchableOpacity
        style={[styles.button, (submitting || !selected) && styles.buttonDisabled]}
        onPress={submit} disabled={submitting || !selected}>
        {submitting
          ? <ActivityIndicator color="white" />
          : <Text style={styles.buttonText}>Send Request</Text>}
      </TouchableOpacity>
    </ScrollView>
  );
}

/** One control per declared type, with text as the fallback — an unknown type
 *  must still be answerable rather than silently missing from the form. */
function renderField(f: Field, value: any, onChange: (v: any) => void) {
  const type = (f.type || 'text').toLowerCase();

  if (type === 'boolean' || type === 'checkbox') {
    return (
      <View style={styles.switchRow}>
        <Switch value={!!value} onValueChange={onChange}
                trackColor={{ true: PRIMARY_COLOR, false: '#ccc' }} />
      </View>
    );
  }
  if ((type === 'select' || type === 'dropdown' || type === 'radio') && (f.options || []).length) {
    return (
      <View style={styles.options}>
        {(f.options || []).map((o: any, i: number) => {
          const val = typeof o === 'string' ? o : (o.value ?? o.label);
          const lbl = typeof o === 'string' ? o : (o.label ?? o.value);
          return (
            <TouchableOpacity key={String(val) + i} onPress={() => onChange(val)}
              style={[styles.option, value === val && styles.optionSelected]}>
              <Text style={[styles.optionText, value === val && styles.optionTextSelected]}>{lbl}</Text>
            </TouchableOpacity>
          );
        })}
      </View>
    );
  }
  return (
    <TextInput
      style={[styles.input, type === 'textarea' && styles.multiline]}
      value={value === undefined || value === null ? '' : String(value)}
      onChangeText={onChange}
      placeholder={f.placeholder || ''}
      multiline={type === 'textarea'}
      keyboardType={type === 'number' ? 'numeric' : type === 'email' ? 'email-address' : 'default'}
      autoCapitalize={type === 'email' ? 'none' : 'sentences'}
    />
  );
}

/** A UUID the server will accept as an idempotency key. */
function newId(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  label: { fontSize: 11, color: '#888', marginTop: 18, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 0.5 },
  help: { fontSize: 12, color: '#666', marginTop: 8, lineHeight: 17 },
  input: { backgroundColor: 'white', padding: 12, borderRadius: 6, fontSize: 14, borderWidth: 1, borderColor: '#e0e0e0' },
  multiline: { minHeight: 96, textAlignVertical: 'top' },
  options: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  option: { paddingHorizontal: 14, paddingVertical: 10, borderRadius: 6, backgroundColor: '#fff',
            borderWidth: 1, borderColor: '#e0e0e0' },
  optionSelected: { backgroundColor: PRIMARY_COLOR, borderColor: PRIMARY_COLOR },
  optionText: { fontSize: 13, color: '#444' },
  optionTextSelected: { color: 'white', fontWeight: '600' },
  switchRow: { flexDirection: 'row', alignItems: 'center', marginTop: 2 },
  button: { backgroundColor: PRIMARY_COLOR, padding: 16, borderRadius: 8, alignItems: 'center', marginTop: 28 },
  buttonDisabled: { opacity: 0.5 },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  error: { color: '#c0392b', fontSize: 13, marginBottom: 8 },
});
'''

REQUEST_DETAIL_SCREEN = '''import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator, ScrollView, StyleSheet, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { PORTAL } from '../api/endpoints';
import { PORTAL_SLUG, PRIMARY_COLOR } from '../api/config';
import { isUuid } from '../api/url';

export default function RequestDetailScreen({ route }: any) {
  const { request } = useApi();
  const caseId = route.params ? route.params.caseId : undefined;

  const [detail, setDetail] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [messages, setMessages] = useState<any[]>([]);
  const [documents, setDocuments] = useState<any[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    // The id comes from a navigation param, so it is checked before it goes
    // into a URL rather than trusted because our own screen passed it.
    if (!isUuid(caseId)) {
      setError('That request reference is not valid.');
      setLoading(false);
      return;
    }
    try {
      setDetail(await request(PORTAL.case(PORTAL_SLUG, caseId)));
      setError('');
      // The extras are each optional — a portal with messages switched off
      // should still show the request itself rather than an error page.
      const soft = async (path: string, pick: (d: any) => any[], set: (v: any[]) => void) => {
        try { set(pick(await request(path)) || []); } catch { set([]); }
      };
      await soft(PORTAL.timeline(PORTAL_SLUG, caseId), d => d.events || d.timeline, setTimeline);
      await soft(PORTAL.messages(PORTAL_SLUG, caseId), d => d.messages, setMessages);
      await soft(PORTAL.documents(PORTAL_SLUG, caseId), d => d.documents, setDocuments);
    } catch (e: any) {
      setError(e.message || 'Could not load this request.');
    } finally {
      setLoading(false);
    }
  }, [caseId, request]);

  useEffect(() => { load(); }, [load]);

  const send = async () => {
    const text = draft.trim();
    if (!text || !isUuid(caseId)) return;
    setSending(true);
    try {
      await request(PORTAL.messages(PORTAL_SLUG, caseId), {
        method: 'POST', body: JSON.stringify({ body: text }),
      });
      setDraft('');
      const d = await request(PORTAL.messages(PORTAL_SLUG, caseId));
      setMessages((d && d.messages) || []);
      setError('');
    } catch (e: any) {
      setError(e.message || 'Your message did not send.');
    } finally {
      setSending(false);
    }
  };

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }
  if (!detail) {
    return (
      <View style={styles.loading}>
        <Text style={styles.empty}>{error || 'Request not found'}</Text>
        <TouchableOpacity onPress={() => { setLoading(true); load(); }}>
          <Text style={styles.retry}>Try again</Text>
        </TouchableOpacity>
      </View>
    );
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}>
      {!!error && <Text style={styles.error}>{error}</Text>}

      <View style={styles.card}>
        <Text style={styles.subject}>{detail.subject || 'Your request'}</Text>
        <Text style={styles.ref}>{detail.case_number || ''}</Text>
        <View style={styles.row}>
          <Text style={styles.status}>{String(detail.status || '').replace(/_/g, ' ')}</Text>
          {!!detail.case_type_name && <Text style={styles.meta}>{detail.case_type_name}</Text>}
        </View>
        {!!detail.description && <Text style={styles.body}>{detail.description}</Text>}
      </View>

      {timeline.length > 0 && (
        <View style={styles.card}>
          <Text style={styles.heading}>Progress</Text>
          {timeline.map((e, i) => (
            <View key={e.id || i} style={styles.event}>
              <View style={styles.dot} />
              <View style={{ flex: 1 }}>
                <Text style={styles.eventLabel}>{e.label || e.action}</Text>
                {!!e.timestamp && <Text style={styles.eventTime}>{fmt(e.timestamp)}</Text>}
              </View>
            </View>
          ))}
        </View>
      )}

      {documents.length > 0 && (
        <View style={styles.card}>
          <Text style={styles.heading}>Documents</Text>
          {documents.map((d, i) => (
            <View key={d.id || i} style={styles.docRow}>
              <Text style={styles.docName} numberOfLines={1}>{d.filename}</Text>
              <Text style={styles.meta}>{d.source === 'customer' ? 'You' : 'Team'}</Text>
            </View>
          ))}
        </View>
      )}

      <View style={styles.card}>
        <Text style={styles.heading}>Messages</Text>
        {messages.length === 0 && <Text style={styles.meta}>No messages yet.</Text>}
        {messages.map((m, i) => (
          <View key={m.id || i} style={[styles.msg, m.mine && styles.msgMine]}>
            <Text style={styles.msgAuthor}>{m.author_name || (m.mine ? 'You' : 'Team')}</Text>
            <Text style={styles.msgBody}>{m.body}</Text>
            {!!m.created_at && <Text style={styles.eventTime}>{fmt(m.created_at)}</Text>}
          </View>
        ))}
        <TextInput
          style={styles.input} value={draft} onChangeText={setDraft}
          placeholder="Add a message" multiline />
        <TouchableOpacity
          style={[styles.button, (!draft.trim() || sending) && styles.buttonDisabled]}
          onPress={send} disabled={!draft.trim() || sending}>
          {sending ? <ActivityIndicator color="white" /> : <Text style={styles.buttonText}>Send</Text>}
        </TouchableOpacity>
      </View>
    </ScrollView>
  );
}

function fmt(iso: string): string {
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  card: { backgroundColor: 'white', padding: 16, borderRadius: 8, marginBottom: 12 },
  subject: { fontSize: 17, fontWeight: '700', color: '#222' },
  ref: { fontSize: 11, color: '#888', fontFamily: 'monospace', marginTop: 2 },
  row: { flexDirection: 'row', justifyContent: 'space-between', marginTop: 10 },
  status: { fontSize: 14, fontWeight: '600', color: PRIMARY_COLOR, textTransform: 'capitalize' },
  meta: { fontSize: 12, color: '#888' },
  body: { fontSize: 14, color: '#333', marginTop: 12, lineHeight: 20 },
  heading: { fontSize: 11, color: '#888', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 10 },
  event: { flexDirection: 'row', alignItems: 'flex-start', marginBottom: 12 },
  dot: { width: 8, height: 8, borderRadius: 4, backgroundColor: PRIMARY_COLOR, marginTop: 5, marginRight: 10 },
  eventLabel: { fontSize: 14, color: '#222' },
  eventTime: { fontSize: 11, color: '#999', marginTop: 2 },
  docRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12 },
  docName: { fontSize: 13, color: '#222', flex: 1 },
  msg: { backgroundColor: '#f2f2f4', borderRadius: 8, padding: 10, marginBottom: 8 },
  msgMine: { backgroundColor: '#eaf4ff' },
  msgAuthor: { fontSize: 11, color: '#666', fontWeight: '600' },
  msgBody: { fontSize: 14, color: '#222', marginTop: 3, lineHeight: 19 },
  input: { backgroundColor: '#fafafa', padding: 12, borderRadius: 6, fontSize: 14,
           borderWidth: 1, borderColor: '#e0e0e0', marginTop: 8, minHeight: 60, textAlignVertical: 'top' },
  button: { backgroundColor: PRIMARY_COLOR, padding: 13, borderRadius: 8, alignItems: 'center', marginTop: 10 },
  buttonDisabled: { opacity: 0.5 },
  buttonText: { color: 'white', fontSize: 14, fontWeight: '600' },
  error: { color: '#c0392b', fontSize: 13, marginBottom: 10 },
  empty: { textAlign: 'center', color: '#888' },
  retry: { textAlign: 'center', color: PRIMARY_COLOR, marginTop: 12, fontWeight: '600' },
});
'''

CASE_LIST_SCREEN = '''import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import { useApi } from '../api/ApiContext';
import { EP } from '../api/endpoints';
import { PRIMARY_COLOR } from '../api/config';

export default function CaseListScreen({ navigation }: any) {
  const { request } = useApi();
  const [cases, setCases] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await request(EP.cases + '?page=1&page_size=50');
      setCases((data && data.items) || []);
      setError('');
    } catch (e: any) {
      // An empty list and a failed call look identical unless we say so.
      setError(e.message || 'Could not load cases.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [request]);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }

  return (
    <View style={styles.container}>
      {!!error && (
        <TouchableOpacity style={styles.errorBox} onPress={() => { setLoading(true); load(); }}>
          <Text style={styles.errorText}>{error}</Text>
          <Text style={styles.errorRetry}>Tap to try again</Text>
        </TouchableOpacity>
      )}
      <FlatList
        data={cases}
        keyExtractor={(item, i) => item.id || String(i)}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} />
        }
        renderItem={({ item }) => (
          <TouchableOpacity
            style={styles.caseCard}
            onPress={() => navigation.navigate('CaseDetail', { caseId: item.id })}>
            <View style={styles.cardHeader}>
              <Text style={styles.caseId}>{item.case_number || String(item.id || '').slice(0, 8)}</Text>
              <Text style={[styles.priority, { backgroundColor: priorityColor(item.priority) }]}>
                {item.priority || 'none'}
              </Text>
            </View>
            <Text style={styles.status}>{String(item.status || 'unknown').replace(/_/g, ' ')}</Text>
            {!!item.current_stage_id && <Text style={styles.stage}>{item.current_stage_id}</Text>}
          </TouchableOpacity>
        )}
        ListEmptyComponent={
          error ? null : <Text style={styles.empty}>No cases yet. Create one!</Text>
        }
      />
      <TouchableOpacity style={styles.fab} onPress={() => navigation.navigate('CreateCase')}>
        <Text style={styles.fabText}>+</Text>
      </TouchableOpacity>
    </View>
  );
}

function priorityColor(p: string): string {
  return ({ blocker: '#fc5c65', critical: '#fc5c65', high: '#f7b731',
            medium: '#4ecdc4', low: '#95a5a6' } as any)[p] || '#95a5a6';
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  errorBox: { backgroundColor: '#fdecea', borderRadius: 8, padding: 12, margin: 12 },
  errorText: { color: '#c0392b', fontSize: 13 },
  errorRetry: { color: '#c0392b', fontSize: 11, marginTop: 4, opacity: 0.8 },
  caseCard: { backgroundColor: 'white', padding: 16, marginHorizontal: 12, marginVertical: 4,
              borderRadius: 8, shadowColor: '#000', shadowOpacity: 0.05, shadowRadius: 2 },
  cardHeader: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: 4 },
  caseId: { fontSize: 11, color: '#888', fontFamily: 'monospace' },
  priority: { fontSize: 10, color: 'white', paddingHorizontal: 8, paddingVertical: 2,
              borderRadius: 4, textTransform: 'uppercase', overflow: 'hidden' },
  status: { fontSize: 16, fontWeight: '600', color: '#222', textTransform: 'capitalize' },
  stage: { fontSize: 12, color: '#666', marginTop: 4 },
  empty: { textAlign: 'center', marginTop: 48, color: '#888', fontSize: 14 },
  fab: { position: 'absolute', right: 20, bottom: 20, width: 56, height: 56,
         borderRadius: 28, backgroundColor: PRIMARY_COLOR, justifyContent: 'center',
         alignItems: 'center', shadowColor: '#000', shadowOpacity: 0.2, shadowRadius: 4, elevation: 5 },
  fabText: { color: 'white', fontSize: 28, fontWeight: '300' },
});
'''

CASE_DETAIL_SCREEN = '''import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator, ScrollView, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { EP } from '../api/endpoints';
import { PRIMARY_COLOR } from '../api/config';
import { isUuid } from '../api/url';

export default function CaseDetailScreen({ route }: any) {
  const { request } = useApi();
  const caseId = route.params ? route.params.caseId : undefined;
  const [caseData, setCaseData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [resolving, setResolving] = useState(false);

  const load = useCallback(async () => {
    // The id arrives as a navigation param, so it is checked before it is put
    // into a URL rather than trusted because it came from our own screen.
    if (!isUuid(caseId)) {
      setError('That case reference is not valid.');
      setLoading(false);
      return;
    }
    try {
      setCaseData(await request(EP.case(caseId)));
      setError('');
    } catch (e: any) {
      setError(e.message || 'Could not load this case.');
    } finally {
      setLoading(false);
    }
  }, [caseId, request]);

  useEffect(() => { load(); }, [load]);

  const resolve = async () => {
    if (!isUuid(caseId)) return;
    setResolving(true);
    try {
      await request(EP.resolve(caseId), { method: 'POST', body: JSON.stringify({}) });
      setCaseData(await request(EP.case(caseId)));
      setError('');
    } catch (e: any) {
      setError(e.message || 'Could not resolve this case.');
    } finally {
      setResolving(false);
    }
  };

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }
  if (!caseData) {
    return (
      <View style={styles.loading}>
        <Text style={styles.empty}>{error || 'Case not found'}</Text>
        <TouchableOpacity onPress={() => { setLoading(true); load(); }}>
          <Text style={styles.retry}>Try again</Text>
        </TouchableOpacity>
      </View>
    );
  }

  const status = String(caseData.status || 'unknown');

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 16 }}>
      {!!error && <Text style={styles.errorText}>{error}</Text>}
      <View style={styles.card}>
        <Text style={styles.label}>Case</Text>
        <Text style={styles.value}>{caseData.case_number || caseData.id}</Text>

        <Text style={styles.label}>Status</Text>
        <Text style={styles.status}>{status.replace(/_/g, ' ')}</Text>

        <Text style={styles.label}>Priority</Text>
        <Text style={styles.value}>{caseData.priority || '—'}</Text>

        {!!caseData.current_stage_id && (
          <>
            <Text style={styles.label}>Current Stage</Text>
            <Text style={styles.value}>{caseData.current_stage_id}</Text>
          </>
        )}

        <Text style={styles.label}>Data</Text>
        <View style={styles.dataBox}>
          <Text style={styles.dataText}>{JSON.stringify(caseData.data || {}, null, 2)}</Text>
        </View>
      </View>

      {(status === 'new' || status === 'open') && (
        <TouchableOpacity style={styles.button} onPress={resolve} disabled={resolving}>
          {resolving
            ? <ActivityIndicator color="white" />
            : <Text style={styles.buttonText}>Resolve Case</Text>}
        </TouchableOpacity>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  card: { backgroundColor: 'white', padding: 16, borderRadius: 8, marginBottom: 16 },
  label: { fontSize: 10, color: '#888', marginTop: 12, textTransform: 'uppercase', letterSpacing: 0.5 },
  value: { fontSize: 15, color: '#222', marginTop: 2 },
  status: { fontSize: 18, fontWeight: '600', color: PRIMARY_COLOR, marginTop: 2, textTransform: 'capitalize' },
  dataBox: { backgroundColor: '#f0f0f0', padding: 10, borderRadius: 6, marginTop: 4 },
  dataText: { fontFamily: 'monospace', fontSize: 11, color: '#222' },
  button: { backgroundColor: PRIMARY_COLOR, padding: 14, borderRadius: 8, alignItems: 'center' },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  errorText: { color: '#c0392b', fontSize: 13, marginBottom: 12 },
  empty: { textAlign: 'center', color: '#888' },
  retry: { textAlign: 'center', color: PRIMARY_COLOR, marginTop: 12, fontWeight: '600' },
});
'''

CREATE_CASE_SCREEN = '''import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator, ScrollView, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { EP } from '../api/endpoints';
import { PRIMARY_COLOR, caseTypeAllowed } from '../api/config';

export default function CreateCaseScreen({ navigation }: any) {
  const { request } = useApi();
  const [caseTypes, setCaseTypes] = useState<any[]>([]);
  const [selectedType, setSelectedType] = useState('');
  const [priority, setPriority] = useState('medium');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await request(EP.caseTypes);
      // App Builder's case type selection is enforced here — without this the
      // app offered every case type on the server regardless of the choice.
      const allowed = (((data && data.items) || []) as any[]).filter(ct => caseTypeAllowed(ct.id));
      setCaseTypes(allowed);
      if (allowed.length) setSelectedType(allowed[0].id);
      setError(allowed.length ? '' : 'No case types are available to you.');
    } catch (e: any) {
      setError(e.message || 'Could not load case types.');
    } finally {
      setLoading(false);
    }
  }, [request]);

  useEffect(() => { load(); }, [load]);

  const submit = async () => {
    if (!selectedType) { setError('Select a case type'); return; }
    setSubmitting(true);
    setError('');
    try {
      const result = await request(EP.cases, {
        method: 'POST',
        body: JSON.stringify({ case_type_id: selectedType, priority, data: {} }),
      });
      if (result && result.id) navigation.replace('CaseDetail', { caseId: result.id });
      else setError('The case was created but the server did not return it.');
    } catch (e: any) {
      setError(e.message || 'Could not create the case.');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 16 }}>
      {!!error && <Text style={styles.errorText}>{error}</Text>}

      <Text style={styles.label}>Case Type</Text>
      <View style={styles.options}>
        {caseTypes.map(ct => (
          <TouchableOpacity
            key={ct.id}
            onPress={() => setSelectedType(ct.id)}
            style={[styles.option, selectedType === ct.id && styles.optionSelected]}>
            <Text style={[styles.optionText, selectedType === ct.id && styles.optionTextSelected]}>
              {ct.name}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      <Text style={styles.label}>Priority</Text>
      <View style={styles.options}>
        {['low', 'medium', 'high', 'critical'].map(p => (
          <TouchableOpacity
            key={p} onPress={() => setPriority(p)}
            style={[styles.option, priority === p && styles.optionSelected]}>
            <Text style={[styles.optionText, priority === p && styles.optionTextSelected]}>{p}</Text>
          </TouchableOpacity>
        ))}
      </View>

      <TouchableOpacity
        style={styles.button} onPress={submit} disabled={submitting || !selectedType}>
        {submitting ? <ActivityIndicator color="white" /> : <Text style={styles.buttonText}>Create Case</Text>}
      </TouchableOpacity>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#f5f5f7' },
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  label: { fontSize: 11, color: '#888', marginTop: 16, marginBottom: 6, textTransform: 'uppercase', letterSpacing: 0.5 },
  options: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  option: { paddingHorizontal: 14, paddingVertical: 10, borderRadius: 6, backgroundColor: '#fff',
            borderWidth: 1, borderColor: '#e0e0e0' },
  optionSelected: { backgroundColor: PRIMARY_COLOR, borderColor: PRIMARY_COLOR },
  optionText: { fontSize: 13, color: '#444', textTransform: 'capitalize' },
  optionTextSelected: { color: 'white', fontWeight: '600' },
  button: { backgroundColor: PRIMARY_COLOR, padding: 16, borderRadius: 8, alignItems: 'center', marginTop: 24 },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  errorText: { color: '#c0392b', fontSize: 13, marginBottom: 8 },
});
'''

MY_WORK_SCREEN = '''import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import { useApi } from '../api/ApiContext';
import { EP } from '../api/endpoints';
import { PRIMARY_COLOR } from '../api/config';

export default function MyWorkScreen() {
  const { request } = useApi();
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await request(EP.myAssignments);
      setItems(Array.isArray(data) ? data : (data && data.items) || []);
      setError('');
    } catch (e: any) {
      setError(e.message || 'Could not load your work.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [request]);

  useFocusEffect(useCallback(() => { load(); }, [load]));

  if (loading) {
    return <View style={styles.loading}><ActivityIndicator size="large" color={PRIMARY_COLOR} /></View>;
  }

  return (
    <FlatList
      data={items}
      keyExtractor={(item, i) => item.id || String(i)}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} />
      }
      ListHeaderComponent={
        <View>
          <Text style={styles.header}>My Work</Text>
          {!!error && (
            <TouchableOpacity style={styles.errorBox} onPress={() => { setLoading(true); load(); }}>
              <Text style={styles.errorText}>{error}</Text>
              <Text style={styles.errorRetry}>Tap to try again</Text>
            </TouchableOpacity>
          )}
        </View>
      }
      ListEmptyComponent={error ? null : <Text style={styles.empty}>No active assignments</Text>}
      renderItem={({ item }) => (
        <View style={styles.item}>
          <Text style={styles.itemTitle}>{item.step_id || 'Task'}</Text>
          <Text style={styles.itemMeta}>Case: {String(item.case_id || '').slice(0, 8)}</Text>
        </View>
      )}
    />
  );
}

const styles = StyleSheet.create({
  loading: { flex: 1, justifyContent: 'center', alignItems: 'center' },
  header: { fontSize: 24, fontWeight: '700', padding: 16, color: '#222' },
  errorBox: { backgroundColor: '#fdecea', borderRadius: 8, padding: 12, marginHorizontal: 12, marginBottom: 8 },
  errorText: { color: '#c0392b', fontSize: 13 },
  errorRetry: { color: '#c0392b', fontSize: 11, marginTop: 4, opacity: 0.8 },
  item: { backgroundColor: 'white', padding: 14, marginHorizontal: 12, marginVertical: 4, borderRadius: 8 },
  itemTitle: { fontSize: 15, fontWeight: '600', color: '#222', textTransform: 'capitalize' },
  itemMeta: { fontSize: 11, color: '#888', marginTop: 2, fontFamily: 'monospace' },
  empty: { textAlign: 'center', marginTop: 48, color: '#888' },
});
'''

SETTINGS_SCREEN = '''import React, { useState } from 'react';
import {
  ActivityIndicator, ScrollView, StyleSheet, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { useApi } from '../api/ApiContext';
import { APP_NAME, APP_VERSION, PRIMARY_COLOR } from '../api/config';
import { EP } from '../api/endpoints';

export default function SettingsScreen() {
  const api = useApi();
  const [baseUrl, setBaseUrl] = useState(api.baseUrl);
  const [tenant, setTenant] = useState(api.tenantSlug);
  const [message, setMessage] = useState('');
  const [problem, setProblem] = useState('');
  const [testing, setTesting] = useState(false);

  const save = async () => {
    setMessage('');
    const failure = await api.setServer(baseUrl, tenant);
    setProblem(failure || '');
    // Changing server ends the session, so say so rather than letting the app
    // drop back to the login screen with no explanation.
    if (!failure) setMessage('Saved. Signing in again may be required.');
  };

  const testConnection = async () => {
    setTesting(true);
    setMessage('');
    setProblem('');
    try {
      await api.request(EP.caseTypes);
      setMessage('Connection OK.');
    } catch (e: any) {
      setProblem(e.message || 'Connection failed.');
    } finally {
      setTesting(false);
    }
  };

  return (
    <ScrollView contentContainerStyle={styles.container}>
      <Text style={styles.title}>Settings</Text>

      <View style={styles.userBox}>
        <Text style={styles.userName}>
          {(api.user && (api.user.display_name || api.user.username)) || 'Signed in'}
        </Text>
        {!!(api.user && api.user.email) && <Text style={styles.userMeta}>{api.user!.email}</Text>}
        {!!(api.user && api.user.roles && api.user.roles.length) && (
          <Text style={styles.userMeta}>{api.user!.roles!.join(' · ')}</Text>
        )}
      </View>

      <Text style={styles.label}>API Base URL</Text>
      <TextInput
        style={styles.input} value={baseUrl} onChangeText={setBaseUrl}
        autoCapitalize="none" autoCorrect={false} keyboardType="url" />

      <Text style={styles.label}>Tenant Slug</Text>
      <TextInput
        style={styles.input} value={tenant} onChangeText={setTenant}
        autoCapitalize="none" autoCorrect={false} />

      {!!problem && <Text style={styles.error}>{problem}</Text>}
      {!!message && <Text style={styles.ok}>{message}</Text>}

      <TouchableOpacity style={styles.button} onPress={save}>
        <Text style={styles.buttonText}>Save</Text>
      </TouchableOpacity>

      <TouchableOpacity
        style={[styles.button, styles.buttonSecondary]} onPress={testConnection} disabled={testing}>
        {testing
          ? <ActivityIndicator color={PRIMARY_COLOR} />
          : <Text style={[styles.buttonText, styles.buttonSecondaryText]}>Test Connection</Text>}
      </TouchableOpacity>

      <TouchableOpacity style={[styles.button, styles.buttonDanger]} onPress={() => api.signOut()}>
        <Text style={styles.buttonText}>Sign Out</Text>
      </TouchableOpacity>

      <Text style={styles.version}>{APP_NAME} · v{APP_VERSION}</Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 20, paddingBottom: 40 },
  title: { fontSize: 24, fontWeight: '700', color: '#222', marginBottom: 16 },
  userBox: { backgroundColor: 'white', borderRadius: 8, padding: 14, marginBottom: 8 },
  userName: { fontSize: 16, fontWeight: '600', color: '#222' },
  userMeta: { fontSize: 12, color: '#888', marginTop: 2 },
  label: { fontSize: 11, color: '#888', marginTop: 12, marginBottom: 4, textTransform: 'uppercase', letterSpacing: 0.5 },
  input: { backgroundColor: 'white', padding: 12, borderRadius: 6, fontSize: 14,
           borderWidth: 1, borderColor: '#e0e0e0' },
  error: { color: '#c0392b', fontSize: 13, marginTop: 12 },
  ok: { color: '#1e8449', fontSize: 13, marginTop: 12 },
  button: { backgroundColor: PRIMARY_COLOR, padding: 14, borderRadius: 8,
            alignItems: 'center', marginTop: 16 },
  buttonSecondary: { backgroundColor: 'transparent', borderWidth: 1, borderColor: PRIMARY_COLOR },
  buttonDanger: { backgroundColor: '#c0392b', marginTop: 28 },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  buttonSecondaryText: { color: PRIMARY_COLOR },
  version: { textAlign: 'center', fontSize: 11, color: '#888', marginTop: 40 },
});
'''

BABEL_CONFIG = '''module.exports = function (api) {
  api.cache(true);
  return { presets: ['babel-preset-expo'] };
};
'''

TSCONFIG = '''{
  "extends": "expo/tsconfig.base",
  "compilerOptions": {
    "strict": true,
    "jsx": "react-native"
  }
}
'''

GITIGNORE = '''node_modules/
.expo/
dist/
npm-debug.*
*.jks
*.p8
*.p12
*.key
*.mobileprovision
*.orig.*
web-build/
.env
'''

README = '''# __APP_NAME__

Auto-generated Velaris mobile app — React Native + Expo.

## Setup

```bash
npm install
npm run typecheck
```

## Run

```bash
npm run ios       # iOS simulator
npm run android   # Android emulator
npm run web       # browser, for development only — see "Web" below
```

## Signing in

Open the app and sign in with your Velaris username and password. Tap the
server line under the Sign In button to point the app at a different Velaris
server or tenant before you sign in.

Multi-factor authentication is supported: if your account has it enabled the
app asks for the code after your password.

## How credentials are handled

- Tokens are kept in the device keystore (Keychain on iOS, Android Keystore
  via `expo-secure-store`), never in `AsyncStorage`. Only the server URL,
  tenant and your display name are stored unencrypted.
- The access token is short-lived and refreshed automatically; a refresh that
  fails returns you to the sign-in screen.
- **Signing out revokes the refresh token on the server** and clears every
  local copy.
- The server URL must be `https://`. Plain `http://` is accepted only for
  localhost and private LAN addresses so a development build still works.
- A session belongs to the server that issued it. Point the app at a different
  server and the session is cleared first — your token is never sent to a host
  that did not issue it.

## Web

`npm run web` is for development on your own machine. Browsers have no
keystore, so on web the token falls back to browser storage, and the Velaris
server must allow your dev origin via CORS. Ship the iOS and Android builds.

## Screens

- **Cases** — list, tap for detail, `+` to create
- **My Work** — your active assignments
- **Settings** — server, tenant, connection test, sign out

## Configured For

- **App Name**: __APP_NAME__
- **Version**: __APP_VERSION__
- **Primary Color**: __PRIMARY_COLOR__
- **Target Tenant**: __DEFAULT_TENANT__
- **API URL**: __DEFAULT_API_URL__

## Generated by Velaris

This app was generated from your case type definitions by Velaris App Builder.
Customize it as needed — it's yours to modify.
'''
