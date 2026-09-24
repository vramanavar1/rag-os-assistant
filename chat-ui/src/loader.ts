// /embed/loader.js — runs inside the host page (third-party origin). Dependency-free.
//
//   <div id="assistant"></div>
//   <script src="https://chat.example.com/embed/loader.js"
//           data-target="#assistant"
//           data-token-endpoint="/api/rag-os-token"   <!-- host backend: returns {token, expires_in} -->
//           data-height="640"></script>
//
// Creates an iframe to <loader origin>/embed, fetches a token from the host backend
// (credentials: 'include') and hands it to the iframe with postMessage (exact targetOrigin).
// Re-fetches ~60s before expiry and whenever the iframe asks ({type:'rag-os:token-request'}).
// Programmatic use: window.RagOsEmbed.mount({target, tokenEndpoint, height?, title?}) -> {destroy, refresh}.

interface MountOptions {
  target: string | Element;
  tokenEndpoint: string;
  height?: string | number | undefined;
  title?: string | undefined;
}

interface EmbedInstance {
  destroy(): void;
  refresh(): Promise<void>;
}

const MSG_TOKEN = 'rag-os:token';
const MSG_TOKEN_REQUEST = 'rag-os:token-request';
const REFRESH_MARGIN_MS = 60_000;
const RETRY_MS = 30_000;
const INSTANCE_KEY = '__ragOsEmbed';

const script = document.currentScript as HTMLScriptElement | null;
const embedOrigin = script?.src ? new URL(script.src, document.baseURI).origin : null;

function cssHeight(v: string | number | undefined): string {
  const s = String(v ?? '').trim();
  if (/^\d{2,4}$/.test(s)) return `${s}px`;
  if (/^\d{1,4}(\.\d+)?(px|vh|dvh|svh|rem|em|%)$/.test(s)) return s;
  return '640px';
}

function log(...args: unknown[]): void {
  console.error('[rag-os embed]', ...args);
}

function mount(opts: MountOptions): EmbedInstance | null {
  if (!embedOrigin) {
    log('cannot determine the assistant origin; load loader.js with a <script src> tag');
    return null;
  }
  const origin = embedOrigin;
  const container = typeof opts.target === 'string' ? document.querySelector(opts.target) : opts.target;
  if (!container) {
    log(`target ${String(opts.target)} not found`);
    return null;
  }
  if (!opts.tokenEndpoint) {
    log('data-token-endpoint is required');
    return null;
  }
  const holder = container as Element & { [INSTANCE_KEY]?: EmbedInstance };
  holder[INSTANCE_KEY]?.destroy();

  const iframe = document.createElement('iframe');
  iframe.src = `${origin}/embed`;
  iframe.title = opts.title || 'Knowledge assistant';
  iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads');
  iframe.setAttribute('allow', 'clipboard-write');
  iframe.referrerPolicy = 'strict-origin';
  iframe.style.width = '100%';
  iframe.style.height = cssHeight(opts.height);
  iframe.style.border = '0';
  iframe.style.display = 'block';
  iframe.style.colorScheme = 'normal';

  let destroyed = false;
  let timer: number | undefined;
  let inflight: Promise<void> | null = null;
  let expiresAt = 0;

  const schedule = (at: number) => {
    window.clearTimeout(timer);
    if (destroyed) return;
    timer = window.setTimeout(() => void push(), Math.max(5_000, at - Date.now()));
  };

  async function fetchToken(): Promise<{ token: string; expiresAt: number }> {
    const res = await fetch(opts.tokenEndpoint, { credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`token endpoint returned HTTP ${res.status}`);
    const body = (await res.json()) as { token?: unknown; expires_in?: unknown };
    if (typeof body.token !== 'string' || !body.token) throw new Error('token endpoint response has no "token"');
    const ttl = Number(body.expires_in);
    return { token: body.token, expiresAt: Date.now() + (Number.isFinite(ttl) && ttl > 0 ? ttl : 300) * 1000 };
  }

  function push(): Promise<void> {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const t = await fetchToken();
        const win = iframe.contentWindow;
        if (destroyed || !win) return;
        // Exact targetOrigin: the token is only delivered if the frame is still on the assistant origin.
        win.postMessage({ type: MSG_TOKEN, token: t.token, expiresAt: t.expiresAt }, origin);
        expiresAt = t.expiresAt;
        const ttl = t.expiresAt - Date.now();
        schedule(t.expiresAt - Math.min(REFRESH_MARGIN_MS, ttl / 2));
      } catch (err) {
        log('could not obtain a token:', err);
        schedule(Date.now() + RETRY_MS);
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  const onMessage = (ev: MessageEvent) => {
    if (ev.source !== iframe.contentWindow || ev.origin !== origin) return;
    const data = ev.data as { type?: unknown } | null;
    if (data && typeof data === 'object' && data.type === MSG_TOKEN_REQUEST) void push();
  };
  const onVisible = () => {
    // Background tabs throttle timers: top up when the host page becomes visible again.
    if (!document.hidden && Date.now() > expiresAt - REFRESH_MARGIN_MS) void push();
  };

  window.addEventListener('message', onMessage);
  document.addEventListener('visibilitychange', onVisible);
  iframe.addEventListener('load', () => void push());
  container.replaceChildren(iframe);

  const instance: EmbedInstance = {
    destroy() {
      destroyed = true;
      window.clearTimeout(timer);
      window.removeEventListener('message', onMessage);
      document.removeEventListener('visibilitychange', onVisible);
      iframe.remove();
      if (holder[INSTANCE_KEY] === instance) delete holder[INSTANCE_KEY];
    },
    refresh: () => push(),
  };
  holder[INSTANCE_KEY] = instance;
  return instance;
}

const w = window as unknown as { RagOsEmbed?: { mount: typeof mount; version: string } };
w.RagOsEmbed ??= { mount, version: '1' };

if (script?.dataset.target) {
  const cfg: MountOptions = {
    target: script.dataset.target,
    tokenEndpoint: script.dataset.tokenEndpoint ?? '',
    height: script.dataset.height,
    title: script.dataset.title,
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => mount(cfg), { once: true });
  else mount(cfg);
}
