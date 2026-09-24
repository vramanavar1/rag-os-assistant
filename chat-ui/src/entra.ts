// Microsoft Entra ID sign-in for the standalone pages (/ and /admin).
//
// Authorization code + PKCE via @azure/msal-browser, redirecting to /auth/callback. The access token is
// acquired for the API scope the server advertises in /api/public-config, so the browser never needs to know
// the audience or tenant at build time.
//
// ApiClient asks for the token synchronously (see api.ts), while MSAL is async. So this provider keeps the
// current access token in a field, renews it a minute before it expires, and uses the async onUnauthorized()
// hook as the retry seam — the same shape EmbedTokenBridge already uses.
import {
  BrowserAuthError,
  InteractionRequiredAuthError,
  PublicClientApplication,
  type AccountInfo,
  type AuthenticationResult,
  type Configuration,
} from '@azure/msal-browser';

import type { TokenProvider } from './api';
import type { PublicConfig } from './types';

const RENEW_MARGIN_MS = 60_000;
const REDIRECT_PATH = '/auth/callback';
/** Where to return after the redirect dance; read back by the callback page. */
const RETURN_KEY = 'rag-os:return-to';

export interface EntraConfig {
  tenantId: string;
  clientId: string;
  scope: string;
}

/** Pull the Entra settings out of the public config, or null when this deployment is not Entra-backed. */
export function entraConfig(cfg: PublicConfig): EntraConfig | null {
  if (cfg.auth_mode !== 'entra' || !cfg.entra_tenant_id || !cfg.entra_client_id || !cfg.entra_api_scope) {
    return null;
  }
  return { tenantId: cfg.entra_tenant_id, clientId: cfg.entra_client_id, scope: cfg.entra_api_scope };
}

export function msalConfig(cfg: EntraConfig): Configuration {
  return {
    auth: {
      clientId: cfg.clientId,
      authority: `https://login.microsoftonline.com/${cfg.tenantId}`,
      redirectUri: new URL(REDIRECT_PATH, window.location.origin).toString(),
      postLogoutRedirectUri: window.location.origin,
    },
    // sessionStorage keeps the cache to this tab, matching how SessionTokenStore already behaves.
    cache: { cacheLocation: 'sessionStorage' },
  };
}

export function rememberReturnTo(url: string): void {
  try {
    window.sessionStorage.setItem(RETURN_KEY, url);
  } catch {
    /* storage disabled: the callback falls back to '/' */
  }
}

export function takeReturnTo(): string {
  try {
    const v = window.sessionStorage.getItem(RETURN_KEY);
    window.sessionStorage.removeItem(RETURN_KEY);
    if (v && v.startsWith('/') && !v.startsWith('//')) return v; // same-origin paths only
  } catch {
    /* ignore */
  }
  return '/';
}

export async function createMsal(cfg: EntraConfig): Promise<PublicClientApplication> {
  const app = new PublicClientApplication(msalConfig(cfg));
  await app.initialize();
  return app;
}

/**
 * Build a provider from the public config, or null when this deployment is not Entra-backed (or MSAL cannot
 * start). Never throws: a broken MSAL config must still leave the paste-a-token path usable for an admin.
 */
export async function setUpEntra(cfg: PublicConfig): Promise<EntraTokenProvider | null> {
  const conf = entraConfig(cfg);
  if (!conf) return null;
  try {
    return new EntraTokenProvider(await createMsal(conf), conf.scope);
  } catch (e) {
    console.error('Microsoft sign-in could not be initialised', e);
    return null;
  }
}

/** Prefer the Entra token; fall back to a stored/pasted one so an admin can still get in. */
export function combineProviders(primary: TokenProvider, fallback: TokenProvider): TokenProvider {
  return {
    getToken: () => primary.getToken() ?? fallback.getToken(),
    onUnauthorized: async () => (await primary.onUnauthorized()) || fallback.onUnauthorized(),
  };
}

export class EntraTokenProvider implements TokenProvider {
  private token: string | null = null;
  private expiresAt = 0;
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private renewing: Promise<boolean> | null = null;
  private expiredHandler: (() => void) | null = null;

  constructor(
    private readonly msal: PublicClientApplication,
    private readonly scope: string,
  ) {}

  onExpired(handler: () => void): void {
    this.expiredHandler = handler;
  }

  get account(): AccountInfo | null {
    return this.msal.getActiveAccount() ?? this.msal.getAllAccounts()[0] ?? null;
  }

  /** True once an account is present and a token has been acquired silently. */
  async restore(): Promise<boolean> {
    const acct = this.account;
    if (!acct) return false;
    this.msal.setActiveAccount(acct);
    return this.acquire(acct);
  }

  /** Start an interactive sign-in. The page is replaced, so this does not return. */
  async signIn(): Promise<void> {
    rememberReturnTo(window.location.pathname + window.location.search + window.location.hash);
    await this.msal.loginRedirect({ scopes: [this.scope] });
  }

  async signOut(): Promise<void> {
    this.stopRenewTimer();
    this.token = null;
    const account = this.account;
    await this.msal.logoutRedirect(account ? { account } : {});
  }

  getToken(): string | null {
    if (!this.token) return null;
    if (Date.now() > this.expiresAt - 5_000) {
      this.token = null;
      void this.renew(); // fire-and-forget; this request will 401 and retry through onUnauthorized
      return null;
    }
    return this.token;
  }

  /** ApiClient's 401 hook: try a silent renew once, and report whether the request may be retried. */
  async onUnauthorized(): Promise<boolean> {
    return this.renew();
  }

  private renew(): Promise<boolean> {
    this.renewing ??= (async () => {
      try {
        const acct = this.account;
        return acct ? await this.acquire(acct) : false;
      } finally {
        this.renewing = null;
      }
    })();
    return this.renewing;
  }

  private async acquire(account: AccountInfo): Promise<boolean> {
    try {
      const res = await this.msal.acquireTokenSilent({ scopes: [this.scope], account });
      this.adopt(res);
      return true;
    } catch (e) {
      if (e instanceof InteractionRequiredAuthError || e instanceof BrowserAuthError) {
        // The session is gone (or a hidden-iframe renewal was blocked): fall back to the sign-in panel.
        this.token = null;
        this.stopRenewTimer();
        this.expiredHandler?.();
        return false;
      }
      throw e;
    }
  }

  private adopt(res: AuthenticationResult): void {
    this.token = res.accessToken;
    this.expiresAt = res.expiresOn ? res.expiresOn.getTime() : Date.now() + 5 * 60_000;
    if (res.account) this.msal.setActiveAccount(res.account);
    this.scheduleRenew();
  }

  private scheduleRenew(): void {
    this.stopRenewTimer();
    const delay = Math.max(10_000, this.expiresAt - Date.now() - RENEW_MARGIN_MS);
    this.renewTimer = setTimeout(() => void this.renew(), delay);
  }

  private stopRenewTimer(): void {
    if (this.renewTimer !== null) {
      clearTimeout(this.renewTimer);
      this.renewTimer = null;
    }
  }
}
