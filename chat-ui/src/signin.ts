// Sign-in panel for the standalone chat (/) and the admin console (/admin).
//  * auth_mode 'entra' -> "Sign in with Microsoft" (MSAL redirect; see entra.ts)
//  * dev_auth_enabled  -> principal picker (GET /api/dev/principals, POST /api/dev/token)
//  * otherwise         -> paste-a-token field only
import { publicApi, jwtExpiry, type SessionTokenStore } from './auth';
import { h, mount } from './dom';
import { attributeSummary, problemBox } from './ui';
import type { Principal, PublicConfig, TokenResponse } from './types';

export interface SignInOptions {
  config: PublicConfig;
  store: SessionTokenStore;
  /** Starts the Entra redirect. Supplied by the page when auth_mode is 'entra'. */
  onEntraSignIn?: () => Promise<void>;
  /** Highlight / preselect principals holding this role (the admin console passes "admin"). */
  preferRole?: string;
  heading?: string;
}

export function principalLabel(p: Principal): string {
  const attrs = attributeSummary(p.attributes);
  const roles = p.roles?.length ? ` [${p.roles.join(', ')}]` : '';
  return `${p.display_name || p.id}${attrs ? ` — ${attrs}` : ''}${roles}`;
}

export async function fetchDevPrincipals(): Promise<Principal[]> {
  const list = await publicApi.get<Principal[]>('/api/dev/principals');
  return Array.isArray(list) ? list : [];
}

export async function devToken(principalId: string): Promise<TokenResponse> {
  return publicApi.post<TokenResponse>('/api/dev/token', { principal_id: principalId });
}

/** Renders the sign-in panel into `container`; resolves once a token is stored. */
export function renderSignIn(container: HTMLElement, opts: SignInOptions): Promise<void> {
  return new Promise((resolve) => {
    const status = h('div', { class: 'signin-status', 'aria-live': 'polite' });
    const done = () => {
      container.replaceChildren();
      resolve();
    };

    // ---- paste token (always available; primary path when dev auth is off)
    const tokenInput = h('textarea', {
      id: 'paste-token',
      rows: 3,
      spellcheck: 'false',
      autocomplete: 'off',
      placeholder: 'eyJhbGciOi…',
      class: 'mono',
    });
    const pasteForm = h(
      'form',
      {
        class: 'stack',
        onsubmit: (ev: SubmitEvent) => {
          ev.preventDefault();
          const token = tokenInput.value.trim().replace(/^Bearer\s+/i, '');
          if (!/^[A-Za-z0-9._~+/=-]{20,16384}$/.test(token)) {
            mount(status, problemBox(new Error('That does not look like a bearer token.')));
            return;
          }
          opts.store.set(token, jwtExpiry(token), 'Pasted token');
          done();
        },
      },
      h('label', { for: 'paste-token' }, 'Bearer token'),
      tokenInput,
      h('div', null, h('button', { type: 'submit', class: 'btn' }, 'Use token')),
    );

    const card = h('section', { class: 'card signin', 'aria-labelledby': 'signin-title' });
    const title = h('h2', { id: 'signin-title' }, opts.heading ?? 'Sign in');

    // ---- Microsoft Entra ID (the production path)
    if (opts.config.auth_mode === 'entra') {
      const signIn = h('button', { type: 'button', class: 'btn btn-primary' }, 'Sign in with Microsoft');
      signIn.addEventListener('click', () => {
        signIn.disabled = true;
        status.replaceChildren('Redirecting to Microsoft…');
        opts.onEntraSignIn?.().catch((err: unknown) => {
          mount(status, problemBox(err));
          signIn.disabled = false;
        });
      });
      mount(
        card,
        title,
        h(
          'p',
          { class: 'lead' },
          'Sign in with your work account. What you can retrieve is decided by the attributes your account '
            + 'carries — department, location, clearance — not by anything stored here.',
        ),
        h('div', null, signIn),
        h('details', null, h('summary', null, 'Administrators: use a bearer token'), pasteForm),
        status,
      );
      mount(container, card);
      return;
    }

    if (!opts.config.dev_auth_enabled) {
      mount(
        card,
        title,
        h(
          'p',
          { class: 'lead' },
          'This deployment has no interactive sign-in configured. Open the assistant from a page that supplies '
            + 'a token, or paste one below.',
        ),
        h('details', { open: true }, h('summary', null, 'Use a bearer token'), pasteForm),
        status,
      );
      mount(container, card);
      return;
    }

    // ---- dev principal picker
    const select = h('select', { id: 'principal', required: true, disabled: true }, h('option', { value: '' }, 'Loading principals…'));
    const submit = h('button', { type: 'submit', class: 'btn btn-primary', disabled: true }, 'Sign in');
    const form = h(
      'form',
      {
        class: 'stack',
        onsubmit: async (ev: SubmitEvent) => {
          ev.preventDefault();
          if (!select.value) return;
          submit.disabled = true;
          status.replaceChildren('Requesting token…');
          try {
            const res = await devToken(select.value);
            const label = select.selectedOptions[0]?.dataset.name ?? select.value;
            const expiresAt = Number.isFinite(res.expires_in) ? Date.now() + res.expires_in * 1000 : jwtExpiry(res.token);
            opts.store.set(res.token, expiresAt, label);
            done();
          } catch (err) {
            mount(status, problemBox(err));
            submit.disabled = false;
          }
        },
      },
      h('label', { for: 'principal' }, 'Principal'),
      select,
      h('div', null, submit),
    );
    mount(
      card,
      title,
      h(
        'p',
        { class: 'lead' },
        'Development sign-in: pick a test principal. Each principal carries attributes (department, region, …) that decide which documents it may retrieve.',
      ),
      h('p', { class: 'notice notice-warning' }, 'Dev authentication is enabled on this API. Disable it outside development.'),
      form,
      h('details', null, h('summary', null, 'Use a bearer token instead'), pasteForm),
      status,
    );
    mount(container, card);

    fetchDevPrincipals()
      .then((principals) => {
        const preferred = opts.preferRole ? principals.filter((p) => p.roles?.includes(opts.preferRole!)) : [];
        const ordered = [...preferred, ...principals.filter((p) => !preferred.includes(p))];
        mount(
          select,
          h('option', { value: '' }, principals.length ? 'Choose a principal…' : 'No principals configured'),
          ordered.map((p) => h('option', { value: p.id, 'data-name': p.display_name || p.id }, principalLabel(p))),
        );
        if (preferred[0]) select.value = preferred[0].id;
        select.disabled = principals.length === 0;
        submit.disabled = principals.length === 0;
        select.focus();
      })
      .catch((err: unknown) => mount(status, problemBox(err)));
  });
}
