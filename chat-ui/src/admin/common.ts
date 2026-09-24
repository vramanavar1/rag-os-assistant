// Helpers shared by the admin console views.
import type { ApiClient } from '../api';
import { fmtDate, fmtNum, h, mount, shortId, type Child } from '../dom';
import { correlationTag, problemBox, statusBadge, toast, toastError } from '../ui';
import type { DocumentDetail, FacetsResponse, Me, PublicConfig, Source } from '../types';

export interface ViewContext {
  api: ApiClient;
  root: HTMLElement;
  /** Hash path segments after the view id, e.g. #/runs/abc -> ["abc"]. */
  params: string[];
  query: URLSearchParams;
  signal: AbortSignal;
  onCleanup(fn: () => void): void;
  /** Return false from the guard to keep the user on this view (e.g. unsaved edits). */
  setLeaveGuard(guard: (() => boolean) | null): void;
  navigate(hash: string): void;
  me: Me | null;
  config: PublicConfig;
}

export type View = (ctx: ViewContext) => Promise<void> | void;

export function pageHeader(title: string, ...actions: Child[]): HTMLElement {
  return h('div', { class: 'page-head' }, h('h1', { tabindex: '-1', class: 'view-title' }, title), h('div', { class: 'page-actions' }, actions));
}

export function loading(label = 'Loading…'): HTMLElement {
  return h('p', { class: 'muted loading', role: 'status' }, label);
}

export function isStale(ctx: ViewContext): boolean {
  return ctx.signal.aborted;
}

// ---------------------------------------------------------------- tables

export interface Column<T> {
  label: string;
  render: (row: T) => Child;
  class?: string;
}

export function dataTable<T>(
  columns: Column<T>[],
  rows: T[],
  opts: { onRowClick?: (row: T) => void; empty?: string; caption?: string; rowLabel?: (row: T) => string } = {},
): HTMLElement {
  if (!rows.length) return h('p', { class: 'empty' }, opts.empty ?? 'Nothing to show.');
  const body = h(
    'tbody',
    null,
    rows.map((row) => {
      const tr = h(
        'tr',
        opts.onRowClick ? { class: 'clickable', tabindex: '0', 'aria-label': opts.rowLabel?.(row) ?? null } : null,
        columns.map((c) => h('td', c.class ? { class: c.class } : null, c.render(row))),
      );
      if (opts.onRowClick) {
        const open = (ev: Event) => {
          const t = ev.target as HTMLElement;
          if (t.closest('input, button, a, label, select, textarea')) return; // let controls work
          opts.onRowClick!(row);
        };
        tr.addEventListener('click', open);
        tr.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' && ev.target === tr) open(ev);
        });
      }
      return tr;
    }),
  );
  return h(
    'div',
    { class: 'table-wrap' },
    h(
      'table',
      { class: 'data' },
      opts.caption ? h('caption', { class: 'visually-hidden' }, opts.caption) : null,
      h('thead', null, h('tr', null, columns.map((c) => h('th', { scope: 'col', class: c.class ?? null }, c.label)))),
      body,
    ),
  );
}

export function kv(pairs: [string, Child][]): HTMLElement {
  return h('dl', { class: 'kv' }, pairs.map(([k, v]) => [h('dt', null, k), h('dd', null, v ?? '—')]));
}

export function code(v: string | null | undefined, n?: number): HTMLElement {
  return h('code', { title: v ?? '' }, n ? shortId(v, n) : (v ?? '—'));
}

// ---------------------------------------------------------------- auto refresh

/** A "Auto-refresh" checkbox that re-runs `fn` every `ms` while the view is active and the tab is visible. */
export function autoRefresh(ctx: ViewContext, fn: () => Promise<void> | void, ms: number, initiallyOn = true): HTMLElement {
  const id = `auto-${Math.random().toString(36).slice(2)}`;
  const cb = h('input', { type: 'checkbox', id, checked: initiallyOn });
  let timer: number | undefined;
  let busy = false;
  const tick = async () => {
    if (busy || document.hidden || ctx.signal.aborted) return;
    busy = true;
    try {
      await fn();
    } finally {
      busy = false;
    }
  };
  const arm = () => {
    window.clearInterval(timer);
    timer = cb.checked ? window.setInterval(() => void tick(), ms) : undefined;
  };
  cb.addEventListener('change', arm);
  arm();
  ctx.onCleanup(() => window.clearInterval(timer));
  return h('div', { class: 'check inline' }, cb, h('label', { for: id }, `Auto-refresh (${Math.round(ms / 1000)}s)`));
}

// ---------------------------------------------------------------- cached lookups

let facetsCache: Promise<FacetsResponse> | null = null;
export function getFacets(api: ApiClient): Promise<FacetsResponse> {
  facetsCache ??= api.get<FacetsResponse>('/api/facets').catch((err: unknown) => {
    facetsCache = null;
    throw err;
  });
  return facetsCache;
}

let sourcesCache: Promise<Source[]> | null = null;
export function getSources(api: ApiClient, refresh = false): Promise<Source[]> {
  if (refresh) sourcesCache = null;
  sourcesCache ??= api.get<Source[]>('/api/admin/sources').catch((err: unknown) => {
    sourcesCache = null;
    throw err;
  });
  return sourcesCache;
}

export async function sourceSelect(api: ApiClient, id: string, selected: string, allLabel = 'All sources'): Promise<HTMLSelectElement> {
  const select = h('select', { id, name: 'source_id' }, h('option', { value: '' }, allLabel));
  try {
    const sources = await getSources(api);
    for (const s of sources) select.appendChild(h('option', { value: s.id }, `${s.id} (${s.type})`));
  } catch {
    /* fall back to whatever is selected */
  }
  if (selected && ![...select.options].some((o) => o.value === selected)) select.appendChild(h('option', { value: selected }, selected));
  select.value = selected;
  return select;
}

// ---------------------------------------------------------------- document detail dialog

export async function retryDocs(api: ApiClient, body: { doc_ids?: string[]; status?: string; source_id?: string }): Promise<number | null> {
  try {
    const res = await api.post<{ requeued: number }>('/api/admin/ingestion/retry', body);
    toast(`Requeued ${fmtNum(res.requeued)} document${res.requeued === 1 ? '' : 's'}.`, { kind: 'success' });
    return res.requeued;
  } catch (err) {
    toastError(err, 'Retry failed');
    return null;
  }
}

function tagTable(title: string, entries: Record<string, unknown> | undefined): HTMLElement | null {
  const rows = Object.entries(entries ?? {});
  if (!rows.length) return null;
  return h(
    'div',
    null,
    h('h3', null, title),
    kv(rows.map(([k, v]) => [k, Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v)])),
  );
}

export function openDocumentDialog(api: ApiClient, docId: string, onClose?: () => void): void {
  const body = h('div', { class: 'dialog-body' }, loading());
  const closeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Close' }, '×');
  const dialog = h(
    'dialog',
    { class: 'dialog', 'aria-labelledby': 'doc-dialog-title' },
    h('div', { class: 'panel-head' }, h('h2', { id: 'doc-dialog-title' }, 'Document'), closeBtn),
    body,
  );
  closeBtn.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    dialog.remove();
    onClose?.();
  });
  dialog.addEventListener('click', (ev) => {
    if (ev.target === dialog) dialog.close(); // backdrop click
  });
  document.body.appendChild(dialog);
  dialog.showModal();

  const load = async () => {
    try {
      const d = await api.get<DocumentDetail>(`/api/admin/ingestion/documents/${encodeURIComponent(docId)}`);
      const r = d.record;
      const retry = h('button', { type: 'button', class: 'btn' }, 'Retry this document');
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        if ((await retryDocs(api, { doc_ids: [r.doc_id] })) !== null) await load();
        retry.disabled = false;
      });
      mount(
        body,
        h('div', { class: 'doc-title' }, h('strong', null, r.title || r.path), statusBadge(r.status)),
        r.status === 'FAILED' || r.error_message
          ? h('div', { class: 'problem' }, h('strong', null, r.error_type || 'Error'), r.error_message ? h('pre', { class: 'pre-wrap' }, r.error_message) : null)
          : null,
        kv([
          ['Doc ID', code(r.doc_id)],
          ['Source', `${r.source_id}`],
          ['Item', code(r.item_id)],
          ['Path', h('span', { class: 'mono break' }, r.path)],
          ['Stage', r.stage || '—'],
          ['Attempts', fmtNum(r.attempts ?? 0)],
          ['Content type', r.content_type || '—'],
          ['Size', fmtNum(r.size ?? 0) + ' bytes'],
          ['Chunks', fmtNum(r.chunk_count ?? 0)],
          ['Review', r.review_status || '—'],
          ['Embedding', code(r.embedding_fp, 12)],
          ['Run', r.run_id ? h('a', { href: `#/runs/${encodeURIComponent(r.run_id)}` }, code(r.run_id, 12)) : '—'],
          ['Tracking ID', code(r.tracking_id)],
          ['Correlation ID', r.correlation_id ? correlationTag(r.correlation_id) : '—'],
          ['Discovered', fmtDate(r.discovered_at)],
          ['Updated', fmtDate(r.updated_at)],
          ['Indexed', fmtDate(r.indexed_at)],
        ]),
        tagTable('Facets', r.tags?.facets),
        tagTable('Access tags (ACL)', r.tags?.acl),
        tagTable('Tag provenance', r.tags?.sources),
        h('h3', null, 'Timeline'),
        d.events?.length
          ? h(
              'ol',
              { class: 'timeline' },
              d.events.map((e) =>
                h(
                  'li',
                  null,
                  h('div', { class: 'timeline-head' }, statusBadge(e.status), h('time', { datetime: e.at }, fmtDate(e.at)), e.stage ? h('span', { class: 'muted' }, e.stage) : null),
                  e.message ? h('p', { class: 'pre-wrap' }, e.message) : null,
                  e.correlation_id ? correlationTag(e.correlation_id) : null,
                ),
              ),
            )
          : h('p', { class: 'muted' }, 'No events recorded.'),
        h('div', { class: 'dialog-actions' }, retry),
      );
    } catch (err) {
      mount(body, problemBox(err));
    }
  };
  void load();
}
