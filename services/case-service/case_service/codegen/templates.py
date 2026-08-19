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
    "expo": "~50.0.0",
    "expo-status-bar": "~1.11.1",
    "expo-secure-store": "~12.8.1",
    "react": "18.2.0",
    "react-dom": "18.2.0",
    "react-native": "0.73.6",
    "react-native-web": "~0.19.6",
    "@expo/metro-runtime": "~3.1.3",
    "@react-navigation/native": "^6.1.9",
    "@react-navigation/stack": "^6.3.20",
    "@react-navigation/bottom-tabs": "^6.5.11",
    "react-native-screens": "~3.29.0",
    "react-native-safe-area-context": "4.8.2",
    "react-native-gesture-handler": "~2.14.0",
    "@react-native-async-storage/async-storage": "1.21.0"
  },
  "devDependencies": {
    "@babel/core": "^7.20.0",
    "typescript": "^5.3.0",
    "@types/react": "~18.2.45"
  },
  "private": true
}
'''

APP_JSON = '''{
  "expo": {
    "name": "__APP_NAME__",
    "slug": "__APP_SLUG__",
    "version": "__APP_VERSION__",
    "description": "__APP_DESCRIPTION__",
    "orientation": "portrait",
    "icon": "./assets/icon.png",
    "userInterfaceStyle": "automatic",
    "splash": {
      "image": "./assets/splash.png",
      "resizeMode": "contain",
      "backgroundColor": "__PRIMARY_COLOR__"
    },
    "assetBundlePatterns": ["**/*"],
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
    "updates": {
      "enabled": true,
      "fallbackToCacheTimeout": 0,
      "url": "https://u.expo.dev/your-project-id"
    },
    "runtimeVersion": {
      "policy": "appVersion"
    },
    "extra": {
      "eas": {
        "projectId": "your-eas-project-id"
      }
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
'''

# ----------------------------------------------------------------- api layer

API_CONTEXT = '''import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { DEFAULT_API_URL, DEFAULT_TENANT } from './config';
import { EP } from './endpoints';
import { checkBaseUrl, originOf, pathIsSafe } from './url';
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

interface Session {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;   // epoch ms
  origin: string;      // the server this session was issued by
  user: User | null;
}

export type SignInResult =
  | { ok: true }
  | { ok: false; mfaRequired: true }
  | { ok: false; mfaRequired?: false; error: string };

interface ApiState {
  ready: boolean;
  baseUrl: string;
  tenantSlug: string;
  user: User | null;
  signedIn: boolean;
  setServer: (baseUrl: string, tenant: string) => Promise<string | null>;
  signIn: (username: string, password: string, mfaToken?: string) => Promise<SignInResult>;
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
    if (s) {
      await setSecret(K.token, s.accessToken);
      await setSecret(K.refresh, s.refreshToken);
      await setPlain(K.origin, s.origin);
      await setPlain(K.user, JSON.stringify({ user: s.user, expiresAt: s.expiresAt }));
    } else {
      await deleteSecret(K.token);
      await deleteSecret(K.refresh);
      await deletePlain(K.origin);
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
      // A stored session belongs to the server that issued it. If the app now
      // points somewhere else, that token is not this server's to receive.
      if (token && refresh && origin && origin === originOf(baseRef.current)) {
        let parsed: any = {};
        try { parsed = meta ? JSON.parse(meta) : {}; } catch { parsed = {}; }
        sessionRef.current = {
          accessToken: token,
          refreshToken: refresh,
          expiresAt: Number(parsed.expiresAt) || 0,
          origin,
          user: parsed.user || null,
        };
        setUser(parsed.user || null);
        setSignedIn(true);
      } else if (token || refresh) {
        await deleteSecret(K.token);
        await deleteSecret(K.refresh);
        await deletePlain(K.origin);
        await deletePlain(K.user);
      }
      setReady(true);
    })();
  }, []);

  const url = (path: string) => baseRef.current + '/api/v1' + path;

  const sessionFrom = (body: any, origin: string, fallbackUser: User | null): Session => ({
    accessToken: body.access_token,
    refreshToken: body.refresh_token || '',
    expiresAt: Date.now() + (Number(body.expires_in) || 900) * 1000,
    origin,
    user: body.user || fallbackUser,
  });

  const doRefresh = useCallback(async (): Promise<boolean> => {
    const current = sessionRef.current;
    if (!current || !current.refreshToken) return false;
    if (refreshInFlight.current) return refreshInFlight.current;

    refreshInFlight.current = (async () => {
      try {
        const resp = await fetch(url(EP.refresh), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Tenant-Slug': tenantRef.current },
          body: JSON.stringify({ refresh_token: current.refreshToken }),
        });
        if (!resp.ok) return false;
        const body = await resp.json();
        if (!body || !body.access_token) return false;
        await applySession(sessionFrom(body, current.origin, current.user));
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

    const send = async (): Promise<Response> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        'X-Tenant-Slug': tenantRef.current,
        ...((opts.headers as Record<string, string>) || {}),
      };
      const s = sessionRef.current;
      // The token is the server's, not the app's: it goes to the origin that
      // issued it and to no other, however the base URL has been edited since.
      if (s && s.origin === originOf(baseRef.current)) {
        headers['Authorization'] = 'Bearer ' + s.accessToken;
      }
      return fetch(url(path), { ...opts, headers });
    };

    const s = sessionRef.current;
    if (s && s.expiresAt && s.expiresAt - Date.now() < EXPIRY_SKEW_MS) {
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
    if (!origin) return { ok: false, error: 'Set a valid server URL first.' };

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
      return { ok: false, error: 'Cannot reach ' + origin + '. Check the server URL and your connection.' };
    }

    if (!resp.ok) {
      let detail = '';
      try {
        const body = await resp.json();
        if (body && typeof body.detail === 'string') detail = body.detail;
      } catch {
        /* fall through to the status-only message */
      }
      return { ok: false, error: detail || 'Sign-in failed (' + resp.status + ').' };
    }

    const body = await resp.json();
    if (body.mfa_required && !body.access_token) return { ok: false, mfaRequired: true };
    if (!body.access_token) return { ok: false, error: 'The server did not return a session.' };

    await applySession(sessionFrom(body, origin, body.user || null));
    return { ok: true };
  }, [applySession]);

  const signOut = useCallback(async () => {
    const s = sessionRef.current;
    if (s && s.refreshToken) {
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
      value={{ ready, baseUrl, tenantSlug, user, signedIn, setServer, signIn, signOut, request }}>
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
import CaseListScreen from './src/screens/CaseListScreen';
import CaseDetailScreen from './src/screens/CaseDetailScreen';
import CreateCaseScreen from './src/screens/CreateCaseScreen';
import MyWorkScreen from './src/screens/MyWorkScreen';
import SettingsScreen from './src/screens/SettingsScreen';
import LoginScreen from './src/screens/LoginScreen';

const Tab = createBottomTabNavigator();
const Stack = createStackNavigator();

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

function Tabs() {
  return (
    <Tab.Navigator screenOptions={{ tabBarActiveTintColor: PRIMARY_COLOR, headerShown: false }}>
      <Tab.Screen name="CasesTab" component={CasesStack} options={{ title: 'Cases' }} />
      <Tab.Screen name="MyWork" component={MyWorkScreen} options={{ title: 'My Work' }} />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ title: 'Settings' }} />
    </Tab.Navigator>
  );
}

/** Nothing renders until the stored server and session are back from storage. */
function Root() {
  const { ready, signedIn } = useApi();

  if (!ready) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: '#f5f5f7' }}>
        <ActivityIndicator size="large" color={PRIMARY_COLOR} />
      </View>
    );
  }
  if (!signedIn) return <LoginScreen />;
  return <Tabs />;
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

export default function LoginScreen() {
  const api = useApi();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [mfaToken, setMfaToken] = useState('');
  const [needsMfa, setNeedsMfa] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  // The server has to be settable before sign-in — there is nowhere else to do
  // it while the rest of the app is behind the login.
  const [showServer, setShowServer] = useState(false);
  const [baseUrl, setBaseUrl] = useState(api.baseUrl);
  const [tenant, setTenant] = useState(api.tenantSlug);

  const saveServer = async () => {
    const problem = await api.setServer(baseUrl, tenant);
    setError(problem || '');
    if (!problem) setShowServer(false);
  };

  const submit = async () => {
    setBusy(true);
    setError('');
    const result = await api.signIn(username.trim(), password, needsMfa ? mfaToken.trim() : undefined);
    setBusy(false);
    if (result.ok) return;
    if (result.mfaRequired) {
      setNeedsMfa(true);
      setError('Enter the code from your authenticator app.');
      return;
    }
    setError(result.error);
  };

  return (
    <KeyboardAvoidingView style={styles.flex} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.container}>
        <Text style={styles.title}>{APP_NAME}</Text>
        <Text style={styles.subtitle}>Sign in to continue</Text>

        <Text style={styles.label}>Username</Text>
        <TextInput
          style={styles.input} value={username} onChangeText={setUsername}
          autoCapitalize="none" autoCorrect={false} textContentType="username" />

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

        {!!error && <Text style={styles.error}>{error}</Text>}

        <TouchableOpacity style={styles.button} onPress={submit} disabled={busy}>
          {busy ? <ActivityIndicator color="white" /> : <Text style={styles.buttonText}>Sign In</Text>}
        </TouchableOpacity>

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
  button: { backgroundColor: PRIMARY_COLOR, padding: 15, borderRadius: 8, alignItems: 'center', marginTop: 22 },
  buttonSecondary: { backgroundColor: 'transparent', borderWidth: 1, borderColor: PRIMARY_COLOR },
  buttonText: { color: 'white', fontSize: 15, fontWeight: '600' },
  buttonSecondaryText: { color: PRIMARY_COLOR },
  serverLine: { textAlign: 'center', fontSize: 11, color: '#888', marginTop: 22 },
  serverBox: { marginTop: 8, padding: 14, backgroundColor: '#ededf0', borderRadius: 8 },
  version: { textAlign: 'center', fontSize: 11, color: '#aaa', marginTop: 28 },
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
