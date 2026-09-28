// Shared API client: bearer auth, per-request correlation id, RFC 7807 problem+json errors,
// one transparent retry after a 401 when the token provider can obtain a fresh token.

export interface Problem {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  correlation_id?: string;
  errors?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail: string | undefined;
  readonly correlationId: string | undefined;
  readonly errors: unknown;
  readonly type: string | undefined;

  constructor(status: number, problem: Problem, correlationId?: string) {
    const title = problem.title || (status ? `HTTP ${status}` : 'Request failed');
    super(problem.detail ? `${title}: ${problem.detail}` : title);
    this.name = 'ApiError';
    this.status = status;
    this.title = title;
    this.detail = problem.detail;
    this.correlationId = problem.correlation_id || correlationId;
    this.errors = problem.errors;
    this.type = problem.type;
  }
}

export interface TokenProvider {
  /** Current bearer token, or null when anonymous. */
  getToken(): string | null;
  /** Called at most once per request after a 401. Resolve true when a fresh token is ready (request is retried). */
  onUnauthorized(): Promise<boolean>;
}

export interface RequestOptions {
  /** An array value repeats the key (`?status=A&status=B`), which is how FastAPI reads a list. */
  query?: Record<string, string | number | boolean | null | undefined | readonly string[]>;
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Send the Authorization header (default true). */
  auth?: boolean;
  timeoutMs?: number;
  responseType?: 'json' | 'blob' | 'text';
}

const DEFAULT_TIMEOUT_MS = 130_000; // nginx proxy_read_timeout is 120s

export function newCorrelationId(): string {
  // crypto.randomUUID only exists in secure contexts (https / localhost).
  if (typeof crypto.randomUUID === 'function' && window.isSecureContext) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6]! & 0x0f) | 0x40;
  b[8] = (b[8]! & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

export function buildUrl(path: string, query?: RequestOptions['query']): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    // append, not set: repeating a key is the wire form for a list, and set() would keep only the last one.
    if (Array.isArray(v)) for (const one of v) params.append(k, String(one));
    else params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}${path.includes('?') ? '&' : '?'}${qs}` : path;
}

function withTimeout(signal: AbortSignal | undefined, ms: number): AbortSignal | undefined {
  const anyFn = (AbortSignal as unknown as { any?: (s: AbortSignal[]) => AbortSignal }).any;
  if (typeof AbortSignal.timeout !== 'function') return signal;
  const timeout = AbortSignal.timeout(ms);
  if (!signal) return timeout;
  return typeof anyFn === 'function' ? anyFn([signal, timeout]) : signal;
}

async function toApiError(res: Response, correlationId: string): Promise<ApiError> {
  let problem: Problem = {};
  const ct = res.headers.get('Content-Type') ?? '';
  try {
    if (ct.includes('json')) {
      const body: unknown = await res.json();
      if (body && typeof body === 'object') {
        const b = body as Record<string, unknown>;
        problem = { ...(b as Problem) };
        // FastAPI-style {"detail": [...]} validation errors
        if (Array.isArray(b.detail)) {
          problem.errors = problem.errors ?? b.detail;
          problem.detail = 'The request was not valid.';
        } else if (b.detail !== undefined && typeof b.detail !== 'string') {
          problem.detail = JSON.stringify(b.detail);
        }
      }
    } else {
      const text = (await res.text()).trim();
      if (text && !text.startsWith('<')) problem.detail = text.slice(0, 300);
    }
  } catch {
    /* body unreadable: fall back to status text */
  }
  if (!problem.title) problem.title = res.statusText || `HTTP ${res.status}`;
  return new ApiError(res.status, problem, correlationId);
}

export class ApiClient {
  constructor(private readonly tokens: TokenProvider | null) {}

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const url = buildUrl(path, opts.query);
    const useAuth = opts.auth !== false && this.tokens !== null;
    let retried = false;
    for (;;) {
      const correlationId = newCorrelationId();
      const headers = new Headers(opts.headers);
      headers.set('X-Correlation-ID', correlationId);
      if (!headers.has('Accept')) headers.set('Accept', 'application/json, application/problem+json');
      let body = opts.body;
      if (opts.json !== undefined) {
        headers.set('Content-Type', 'application/json');
        body = JSON.stringify(opts.json);
      }
      const token = useAuth ? this.tokens!.getToken() : null;
      if (token) headers.set('Authorization', `Bearer ${token}`);

      let res: Response;
      try {
        res = await fetch(url, {
          method,
          headers,
          body: body ?? null,
          signal: withTimeout(opts.signal, opts.timeoutMs ?? DEFAULT_TIMEOUT_MS) ?? null,
          credentials: 'same-origin',
          cache: 'no-store',
        });
      } catch (err) {
        if (opts.signal?.aborted) throw err; // cancelled by the caller
        const timedOut = err instanceof DOMException && (err.name === 'TimeoutError' || err.name === 'AbortError');
        throw new ApiError(
          0,
          {
            title: timedOut ? 'Request timed out' : 'Network error',
            detail: timedOut ? 'The server took too long to respond.' : 'Could not reach the server. Check your connection.',
          },
          correlationId,
        );
      }

      const cid = res.headers.get('X-Correlation-ID') || correlationId;
      if (res.status === 401 && useAuth && !retried) {
        retried = true;
        if (await this.tokens!.onUnauthorized()) continue;
      }
      if (!res.ok) throw await toApiError(res, cid);
      if (opts.responseType === 'blob') return (await res.blob()) as T;
      if (opts.responseType === 'text') return (await res.text()) as T;
      if (res.status === 204 || res.headers.get('Content-Length') === '0') return undefined as T;
      const ct = res.headers.get('Content-Type') ?? '';
      if (!ct.includes('json')) {
        throw new ApiError(res.status, { title: 'Unexpected response', detail: `Expected JSON, got ${ct || 'no content type'}.` }, cid);
      }
      return (await res.json()) as T;
    }
  }

  get<T>(path: string, query?: RequestOptions['query'], opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('GET', path, { ...opts, query });
  }

  post<T>(path: string, json?: unknown, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('POST', path, { ...opts, json: json ?? {} });
  }

  put<T>(path: string, json?: unknown, opts: RequestOptions = {}): Promise<T> {
    return this.request<T>('PUT', path, { ...opts, json: json ?? {} });
  }
}

/** Flatten problem.errors (list of strings / {loc,msg} objects / {field: msg} map) into display lines. */
export function problemErrorLines(errors: unknown): string[] {
  if (!errors) return [];
  if (Array.isArray(errors)) {
    return errors.map((e) => {
      if (typeof e === 'string') return e;
      if (e && typeof e === 'object') {
        const o = e as Record<string, unknown>;
        const loc = Array.isArray(o.loc) ? o.loc.join('.') : (o.loc ?? o.field ?? o.path ?? o.pointer);
        const msg = o.msg ?? o.message ?? o.detail ?? JSON.stringify(o);
        return loc ? `${String(loc)}: ${String(msg)}` : String(msg);
      }
      return String(e);
    });
  }
  if (typeof errors === 'object') {
    return Object.entries(errors as Record<string, unknown>).map(
      ([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : typeof v === 'string' ? v : JSON.stringify(v)}`,
    );
  }
  return [String(errors)];
}

export function describeError(err: unknown): { title: string; detail?: string; correlationId?: string; lines: string[] } {
  if (err instanceof ApiError) {
    return {
      title: err.title,
      ...(err.detail ? { detail: err.detail } : {}),
      ...(err.correlationId ? { correlationId: err.correlationId } : {}),
      lines: problemErrorLines(err.errors),
    };
  }
  return { title: 'Something went wrong', detail: err instanceof Error ? err.message : String(err), lines: [] };
}
