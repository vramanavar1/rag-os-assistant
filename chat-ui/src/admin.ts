// Admin console (/admin): ingestion & governance. Hash routing: #/<view>[/<param>][?query].
import { ApiClient, isAbortError, type TokenProvider } from './api';
import { SessionTokenStore, fetchPublicConfig } from './auth';
import { setUpEntra, type EntraTokenProvider } from './entra';
import { byId, h, mount, show } from './dom';
import { renderSignIn } from './signin';
import { openAccountDialog } from './account';
import { identityChip, isAdmin, problemBox, toast, toastError } from './ui';
import type { Me, PublicConfig } from './types';
import type { View, ViewContext } from './admin/common';
import { configView } from './admin/config';
import { controlsView } from './admin/controls';
import { dashboardView } from './admin/dashboard';
import { dlqView } from './admin/dlq';
import { documentsView } from './admin/documents';
import { reviewView } from './admin/review';
import { runsView } from './admin/runs';
import { sourcesView } from './admin/sources';
import { uploadsView } from './admin/uploads';

const ROUTES: { id: string; label: string; view: View }[] = [
  { id: 'dashboard', label: 'Dashboard', view: dashboardView },
  { id: 'runs', label: 'Runs', view: runsView },
  { id: 'documents', label: 'Documents', view: documentsView },
  { id: 'uploads', label: 'Uploads', view: uploadsView },
  { id: 'dlq', label: 'Dead letters', view: dlqView },
  { id: 'sources', label: 'Sources', view: sourcesView },
  { id: 'review', label: 'Review queue', view: reviewView },
  { id: 'controls', label: 'Controls', view: controlsView },
  { id: 'config', label: 'Config', view: configView },
];

const store = new SessionTokenStore('rag-os:token:admin');
// Entra is resolved in main(); until then (and when it is not configured) the stored/pasted token is used.
let entra: EntraTokenProvider | null = null;
const tokens: TokenProvider = {
  getToken: () => entra?.getToken() ?? store.getToken(),
  onUnauthorized: async () => (entra ? await entra.onUnauthorized() : false) || store.onUnauthorized(),
};
const api = new ApiClient(tokens);
let config: PublicConfig;
let me: Me | null = null;

// ------------------------------------------------------------------ layout

const appName = h('span', { class: 'brand-name' }, 'RAG-OS');
const identitySlot = h('div', { class: 'identity-slot' });
const nav = h('nav', { class: 'sidenav', 'aria-label': 'Admin sections' });
const viewRoot = h('main', { id: 'view', class: 'view', tabindex: '-1' });
const banner = h('div', { class: 'banner', hidden: true, role: 'status' });
const signinSlot = h('div', { class: 'signin-wrap', hidden: true });
const layout = h('div', { class: 'admin-layout', hidden: true }, nav, h('div', { class: 'view-wrap' }, banner, viewRoot));

function renderShell(): void {
  mount(
    byId('app'),
    h(
      'header',
      { class: 'topbar' },
      h('div', { class: 'brand' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), appName, h('span', { class: 'brand-sub' }, 'Admin console')),
      identitySlot,
      h('div', { class: 'toolbar-actions' }, h('a', { href: '/', class: 'btn btn-ghost' }, 'Open chat')),
    ),
    signinSlot,
    layout,
  );
  mount(
    nav,
    h(
      'ul',
      null,
      ROUTES.map((r) => h('li', null, h('a', { href: `#/${r.id}`, 'data-route': r.id }, r.label))),
    ),
  );
}

// ------------------------------------------------------------------ router

interface Active {
  ctrl: AbortController;
  cleanups: (() => void)[];
  guard: (() => boolean) | null;
}
let active: Active | null = null;
let currentHash = '';

function parseHash(): { view: string; params: string[]; query: URLSearchParams } {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const qi = raw.indexOf('?');
  const pathPart = qi >= 0 ? raw.slice(0, qi) : raw;
  const queryPart = qi >= 0 ? raw.slice(qi + 1) : '';
  const segs = pathPart
    .split('/')
    .filter(Boolean)
    .map((s) => {
      try {
        return decodeURIComponent(s);
      } catch {
        return s;
      }
    });
  return { view: segs[0] ?? 'dashboard', params: segs.slice(1), query: new URLSearchParams(queryPart) };
}

function teardown(): void {
  if (!active) return;
  active.ctrl.abort();
  active.cleanups.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
  active = null;
}

async function route(): Promise<void> {
  if (active?.guard && window.location.hash !== currentHash && !active.guard()) {
    history.replaceState(null, '', currentHash || '#/dashboard'); // stay (no hashchange fired)
    return;
  }
  teardown();
  currentHash = window.location.hash;
  const { view, params, query } = parseHash();
  const routeDef = ROUTES.find((r) => r.id === view);
  if (!routeDef) {
    window.location.replace('#/dashboard');
    return;
  }
  nav.querySelectorAll<HTMLAnchorElement>('a[data-route]').forEach((a) => {
    if (a.dataset.route === view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  document.title = `${routeDef.label} · ${config.app_name} admin`;

  const current: Active = { ctrl: new AbortController(), cleanups: [], guard: null };
  active = current;
  const ctx: ViewContext = {
    api,
    root: viewRoot,
    params,
    query,
    signal: current.ctrl.signal,
    onCleanup: (fn) => current.cleanups.push(fn),
    setLeaveGuard: (g) => {
      current.guard = g;
    },
    navigate: (hash) => {
      if (window.location.hash === hash) void route();
      else window.location.hash = hash;
    },
    me,
    config,
  };
  mount(viewRoot, h('p', { class: 'muted loading' }, 'Loading…'));
  try {
    await routeDef.view(ctx);
    if (!current.ctrl.signal.aborted) viewRoot.querySelector<HTMLElement>('.view-title')?.focus({ preventScroll: true });
  } catch (err) {
    if (!isAbortError(err) && !current.ctrl.signal.aborted) mount(viewRoot, problemBox(err));
  }
}

window.addEventListener('hashchange', () => void route());

// ------------------------------------------------------------------ auth / start-up

async function loadIdentity(): Promise<void> {
  try {
    me = await api.get<Me>('/api/me');
    // The console deliberately passes no link to itself, but the account panel belongs on both pages.
    mount(identitySlot, identityChip(me, signOut, { onOpenAccount: () => openAccountDialog(api) }));
    const admin = isAdmin(me);
    mount(banner, admin ? null : h('p', null, h('strong', null, 'This principal has no admin role. '), 'Admin requests will be refused (403). Sign out and choose an administrator.'));
    show(banner, !admin);
  } catch (err) {
    toastError(err, 'Could not load your profile');
  }
}

async function signIn(): Promise<void> {
  teardown();
  show(layout, false);
  show(signinSlot, true);
  mount(identitySlot);
  await renderSignIn(signinSlot, {
    config,
    store,
    preferRole: 'admin',
    heading: `Sign in to ${config.app_name} admin`,
    ...(entra ? { onEntraSignIn: () => entra!.signIn() } : {}),
  });
  show(signinSlot, false);
  show(layout, true);
  await loadIdentity();
  currentHash = '';
  await route();
}

function signOut(): void {
  if (active?.guard && !active.guard()) return;
  store.clear();
  me = null;
  if (entra?.account) {
    void entra.signOut(); // ends the Entra session too, not just this tab's token
    return;
  }
  void signIn();
}

store.onExpired(() => {
  toast('Your session has expired. Please sign in again.');
  void signIn();
});

async function main(): Promise<void> {
  renderShell();
  try {
    config = await fetchPublicConfig();
  } catch (err) {
    mount(byId('app'), h('main', { class: 'signin-wrap' }, h('section', { class: 'card' }, h('h1', null, 'Admin console unavailable'), problemBox(err))));
    return;
  }
  appName.textContent = config.app_name;
  entra = await setUpEntra(config);
  entra?.onExpired(() => {
    toast('Your session has expired. Please sign in again.');
    void signIn();
  });
  if (!window.location.hash) history.replaceState(null, '', '#/dashboard');
  const restored = entra ? await entra.restore() : false;
  if (!restored && !store.getToken()) {
    await signIn();
    return;
  }
  show(layout, true);
  await loadIdentity();
  await route();
}

void main();
