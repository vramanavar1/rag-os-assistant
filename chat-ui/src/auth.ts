// Token handling.
//  * Standalone pages (/ and /admin): EntraTokenProvider (see entra.ts) when auth_mode is 'entra',
//    otherwise SessionTokenStore — token in sessionStorage (dev principal picker or a pasted token),
//    cleared on 401 so the page shows the sign-in panel again.
//  * Embedded chat (/embed): EmbedTokenBridge — an Entra access token pushed by the host page via
//    postMessage, origin-checked against /api/public-config embed_origins, kept in memory only. On 401
//    it asks the parent for a fresh token and the request is retried once.
import { ApiClient, type TokenProvider } from './api';
import type { PublicConfig } from './types';

export const MSG_TOKEN = 'rag-os:token';
export const MSG_TOKEN_REQUEST = 'rag-os:token-request';

/** Client for unauthenticated endpoints (public config, dev principals, dev token). */
export const publicApi = new ApiClient(null);

export async function fetchPublicConfig(): Promise<PublicConfig> {
  const cfg = await publicApi.get<Partial<PublicConfig>>('/api/public-config');
  const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null);
  const mode = cfg.auth_mode;
  return {
    auth_mode: mode === 'entra' || mode === 'dev' || mode === 'none' ? mode : 'none',
    embed_origins: Array.isArray(cfg.embed_origins) ? cfg.embed_origins.filter((o) => typeof o === 'string') : [],
    dev_auth_enabled: cfg.dev_auth_enabled === true,
    app_name: str(cfg.app_name) ?? 'RAG-OS',
    entra_tenant_id: str(cfg.entra_tenant_id),
    entra_client_id: str(cfg.entra_client_id),
    entra_api_scope: str(cfg.entra_api_scope),
  };
}

/** Read `exp` from a JWT payload for display/expiry bookkeeping only (never for trust decisions). */
export function jwtExpiry(token: string): number | null {
  const part = token.split('.')[1];
  if (!part) return null;
  try {
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '=');
    const payload: unknown = JSON.parse(atob(b64));
    const exp = (payload as { exp?: unknown }).exp;
    return typeof exp === 'number' ? exp * 1000 : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ standalone

interface StoredToken {
  token: string;
  expiresAt: number | null;
  label?: string;
}

function safeSession(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // storage disabled (privacy mode / sandbox)
  }
}

export class SessionTokenStore implements TokenProvider {
  private current: StoredToken | null = null;
  private expiredHandler: (() => void) | null = null;

  constructor(private readonly key: string) {
    try {
      const raw = safeSession()?.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw) as StoredToken;
        if (typeof parsed.token === 'string' && parsed.token) this.current = parsed;
      }
    } catch {
      this.current = null;
    }
  }

  onExpired(handler: () => void): void {
    this.expiredHandler = handler;
  }

  get label(): string | undefined {
    return this.current?.label;
  }

  getToken(): string | null {
    const c = this.current;
    if (!c) return null;
    if (c.expiresAt !== null && Date.now() > c.expiresAt - 5_000) {
      this.clear();
      return null;
    }
    return c.token;
  }

  set(token: string, expiresAt: number | null, label?: string): void {
    this.current = { token, expiresAt, ...(label ? { label } : {}) };
    try {
      safeSession()?.setItem(this.key, JSON.stringify(this.current));
    } catch {
      /* memory only */
    }
  }

  clear(): void {
    this.current = null;
    try {
      safeSession()?.removeItem(this.key);
    } catch {
      /* ignore */
    }
  }

  async onUnauthorized(): Promise<boolean> {
    this.clear();
    this.expiredHandler?.();
    return false;
  }
}

// ------------------------------------------------------------------ embedded

/** Allow-list entry: exact origin, or a wildcard host like https://*.contoso.com[:port]. */
export function originMatcher(patterns: string[]): (origin: string) => boolean {
  const exact = new Set<string>();
  const wild: { scheme: string; suffix: string; port: string }[] = [];
  for (const raw of patterns) {
    const p = raw.trim().replace(/\/+$/, '');
    const m = /^(https?):\/\/\*\.([a-z0-9.-]+)(?::(\d{1,5}))?$/i.exec(p);
    if (m) {
      wild.push({ scheme: m[1]!.toLowerCase(), suffix: `.${m[2]!.toLowerCase()}`, port: m[3] ?? '' });
      continue;
    }
    try {
      const u = new URL(p);
      if (u.protocol === 'https:' || u.protocol === 'http:') exact.add(u.origin);
    } catch {
      /* ignore invalid entries */
    }
  }
  return (origin: string) => {
    if (exact.has(origin)) return true;
    let u: URL;
    try {
      u = new URL(origin);
    } catch {
      return false;
    }
    return wild.some((w) => `${w.scheme}:` === u.protocol && u.hostname.endsWith(w.suffix) && u.port === w.port);
  };
}

interface TokenMessage {
  type: typeof MSG_TOKEN;
  token: string;
  expiresAt?: number;
}

function isTokenMessage(data: unknown): data is TokenMessage {
  if (!data || typeof data !== 'object') return false;
  const d = data as Record<string, unknown>;
  return d.type === MSG_TOKEN && typeof d.token === 'string' && d.token.length > 0 && d.token.length < 16_384;
}

export class EmbedTokenBridge implements TokenProvider {
  private token: string | null = null;
  private expiresAt = 0;
  private allowed: ((origin: string) => boolean) | null = null;
  private allowList: string[] = [];
  private parentOrigin: string | null = null;
  private buffered: MessageEvent[] = [];
  private waiters: (() => void)[] = [];
  private tokenListeners: (() => void)[] = [];
  private refreshing: Promise<boolean> | null = null;

  constructor() {
    // Register immediately: the host page may post the first token before /api/public-config returns.
    window.addEventListener('message', (ev) => this.onMessage(ev));
  }

  get isFramed(): boolean {
    return window.parent !== window;
  }

  /** Install the origin allow-list and process any messages that arrived earlier. */
  configure(embedOrigins: string[]): void {
    // A same-origin parent (e.g. /dev/embed-host) is already fully trusted by the browser's own rules.
    this.allowList = [...embedOrigins.filter((o) => !o.includes('*')), window.location.origin];
    this.allowed = originMatcher([...embedOrigins, window.location.origin]);
    const pending = this.buffered;
    this.buffered = [];
    pending.forEach((ev) => this.handle(ev));
  }

  hasToken(): boolean {
    return this.getToken() !== null;
  }

  getToken(): string | null {
    if (!this.token || Date.now() >= this.expiresAt) return null;
    return this.token;
  }

  onToken(listener: () => void): void {
    this.tokenListeners.push(listener);
  }

  /** Ask the embedding host page for a (fresh) token. */
  requestToken(): void {
    if (!this.isFramed) return;
    const msg = { type: MSG_TOKEN_REQUEST };
    if (this.parentOrigin) {
      window.parent.postMessage(msg, this.parentOrigin);
      return;
    }
    // Parent origin not known yet: target each allowed origin; mismatches are dropped by the browser.
    for (const origin of this.allowList) window.parent.postMessage(msg, origin);
  }

  waitForToken(timeoutMs: number): Promise<boolean> {
    if (this.hasToken()) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== done);
        resolve(false);
      }, timeoutMs);
      const done = () => {
        window.clearTimeout(timer);
        resolve(true);
      };
      this.waiters.push(done);
    });
  }

  onUnauthorized(): Promise<boolean> {
    if (!this.refreshing) {
      this.token = null;
      this.requestToken();
      this.refreshing = this.waitForToken(10_000).finally(() => {
        this.refreshing = null;
      });
    }
    return this.refreshing;
  }

  private onMessage(ev: MessageEvent): void {
    if (!this.isFramed || ev.source !== window.parent) return;
    if (!this.allowed) {
      if (this.buffered.length < 10) this.buffered.push(ev);
      return;
    }
    this.handle(ev);
  }

  private handle(ev: MessageEvent): void {
    if (!isTokenMessage(ev.data)) return;
    if (!this.allowed?.(ev.origin)) {
      console.warn(`[rag-os] ignored token message from non-allow-listed origin ${ev.origin}`);
      return;
    }
    const d = ev.data;
    const exp =
      typeof d.expiresAt === 'number' && Number.isFinite(d.expiresAt) ? d.expiresAt : (jwtExpiry(d.token) ?? Date.now() + 5 * 60_000);
    this.token = d.token;
    this.expiresAt = exp;
    this.parentOrigin = ev.origin;
    const waiters = this.waiters;
    this.waiters = [];
    waiters.forEach((w) => w());
    this.tokenListeners.forEach((l) => l());
  }
}
