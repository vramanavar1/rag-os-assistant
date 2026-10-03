// Helpers shared by the admin console views.
import type { ApiClient } from '../api';
import { fmtDate, fmtNum, h, mount, shortId, type Child } from '../dom';
import { correlationTag, problemBox, statusBadge, toast, toastError } from '../ui';
import { clearanceSelect, vocabularySelect } from '../tag-controls';
import type { DocumentDetail, DocumentRecord, FacetsResponse, Me, PublicConfig, Source, UploadOptions } from '../types';

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

// ---------------------------------------------------------------- who can read a document

type Required = { name: string; label: string }[];
let optionsCache: Promise<UploadOptions | null> | null = null;
/** GET /api/uploads/options, cached: the required access attributes and the clearance ladder. */
function getUploadOptions(api: ApiClient): Promise<UploadOptions | null> {
  optionsCache ??= api.get<UploadOptions>('/api/uploads/options').catch(() => {
    optionsCache = null;
    return null;
  });
  return optionsCache;
}

/** The access attributes a document must carry for anyone (beyond individual shares) to read it. */
export async function getRequiredAccess(api: ApiClient): Promise<Required> {
  const o = await getUploadOptions(api);
  return o?.required ?? [{ name: 'department', label: 'Department' }, { name: 'region', label: 'Region' }];
}

/** Required access tags this document lacks - unless it was deliberately made private (Only me). */
export function missingAccess(rec: Pick<DocumentRecord, 'tags'>, required: Required): string[] {
  const acl = (rec.tags?.acl ?? {}) as Record<string, unknown>;
  if (rec.tags?.sources?.visibility === 'private') return [];
  return required.filter((r) => acl[r.name] === undefined || (Array.isArray(acl[r.name]) && !(acl[r.name] as unknown[]).length)).map((r) => r.label);
}

/** "HR · Global · 1", "Only me", or a red badge naming the missing tags (the document nobody can read). */
export function accessSummary(rec: Pick<DocumentRecord, 'tags'>, required: Required, onFix?: () => void): HTMLElement {
  const missing = missingAccess(rec, required);
  if (missing.length) {
    const badge = h(
      onFix ? 'button' : 'span',
      { class: 'badge tone-critical', title: 'No one but administrators and people it is shared with individually can read this document. Select to fix.' },
      `No ${missing.join(', ')}: invisible`,
    );
    if (onFix) badge.addEventListener('click', (ev) => (ev.stopPropagation(), onFix()));
    return badge;
  }
  if (rec.tags?.sources?.visibility === 'private') return h('span', { class: 'badge tone-neutral', title: 'Only its uploader can read it' }, 'Only me');
  const acl = (rec.tags?.acl ?? {}) as Record<string, string[] | number>;
  // Policy order (department · region · clearance), not storage order; `*` means open to everyone.
  const order = [...required.map((r) => r.name), ...Object.keys(acl)];
  const parts = [...new Set(order)]
    .filter((k) => k !== 'employee_id' && acl[k] !== undefined)
    .map((k) => {
      const v = acl[k]!;
      return Array.isArray(v) ? v.map((x) => (x === '*' ? 'All' : x)).join('/') : String(v);
    });
  return h('span', { class: 'small', title: JSON.stringify(acl) }, parts.join(' · ') || '—');
}

export function openDocumentDialog(api: ApiClient, docId: string, onClose?: () => void, opts: { focusAccess?: boolean; onChanged?: () => void } = {}): void {
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
      const [d, required, options, facets] = await Promise.all([
        api.get<DocumentDetail>(`/api/admin/ingestion/documents/${encodeURIComponent(docId)}`),
        getRequiredAccess(api),
        getUploadOptions(api),
        getFacets(api).then((f) => f.facets).catch(() => ({}) as FacetsResponse['facets']),
      ]);
      const r = d.record;
      const editor = accessEditor(api, r, () => { opts.onChanged?.(); void load(); }, required, facets, options);
      if (opts.focusAccess || missingAccess(r, required).length) editor.open = true;
      const retry = h('button', { type: 'button', class: 'btn' }, 'Retry this document');
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        if ((await retryDocs(api, { doc_ids: [r.doc_id] })) !== null) await load();
        retry.disabled = false;
      });
      mount(
        body,
        h('div', { class: 'doc-title' }, h('strong', null, r.title || r.path), statusBadge(r.status), accessSummary(r, required)),
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
        editor,
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
        h('div', { class: 'dialog-actions' }, deleteButton(api, r.doc_id, r.title || r.path, () => { opts.onChanged?.(); dialog.close(); }), retry),
      );
    } catch (err) {
      mount(body, problemBox(err));
    }
  };
  void load();
}

// ---------------------------------------------------------------- access tags + delete (administrators)

/** Edit who may read a document - with the upload form's own dropdowns (tag-controls.ts), never free text.
 *  Matching is exact, so a typed `hr` would silently never match `HR`. Saves through /tags, which re-tags the
 *  index in place (no re-embedding) and refuses any value outside the vocabulary. */
function accessEditor(
  api: ApiClient,
  r: DocumentDetail['record'],
  reload: () => void,
  required: Required,
  vocab: FacetsResponse['facets'],
  options: UploadOptions | null,
): HTMLDetailsElement {
  const acl = (r.tags?.acl ?? {}) as Record<string, string[] | number>;
  const clearance = options?.clearance ?? null;
  const listNames = [...new Set([...required.map((x) => x.name), ...Object.keys(acl)])].filter(
    (k) => k !== clearance?.name && vocab[k] !== undefined,
  );
  const labelOf = (k: string) => required.find((x) => x.name === k)?.label ?? vocab[k]?.label ?? k;

  // One dropdown per value; + Add another for the rare document shared with several departments or regions.
  const rows = new Map<string, HTMLElement>();
  const addRow = (name: string, value: string): void => {
    const list = rows.get(name)!;
    const n = list.childElementCount;
    const select = vocabularySelect({
      id: `acl-${name}-${n}`,
      name,
      facet: vocab[name]!,
      selected: value,
      leading: [{ value: '', label: '— choose —' }, { value: '*', label: 'Everyone (*)' }],
    });
    const remove = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': `Remove this ${labelOf(name)}` }, '×');
    const row = h('div', { class: 'acl-row' }, select, remove);
    remove.addEventListener('click', () => (list.childElementCount > 1 ? row.remove() : (select.value = '')));
    list.appendChild(row);
  };
  const listFields = listNames.map((name) => {
    const list = h('div', { class: 'acl-values', id: `acl-${name}` });
    rows.set(name, list);
    const current = acl[name];
    const values = Array.isArray(current) && current.length ? current : [''];
    for (const v of values) addRow(name, String(v));
    const add = h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, '+ Add another');
    add.addEventListener('click', () => addRow(name, ''));
    return h('div', { class: 'field' }, h('label', { for: `acl-${name}-0` }, labelOf(name)), list, add);
  });

  const clearanceField = clearance
    ? (() => {
        // Recorded tags keep what their layer wrote: a manifest cell arrives as ["1"], a rule as 1.
        const current = acl[clearance.name] as unknown;
        const raw = Array.isArray(current) ? current[0] : current;
        const level = raw === undefined || raw === null || raw === '' ? null : Number(raw);
        const sel = clearanceSelect({ id: `acl-${clearance.name}`, clearance, selected: Number.isFinite(level) ? level : null });
        return { sel, el: h('div', { class: 'field' }, h('label', { for: sel.id }, clearance.label), sel) };
      })()
    : null;

  // Individual shares (employee_id): shown and removable, never typed. Adding a person needs a directory lookup.
  const shareNames = Object.keys(acl).filter((k) => !listNames.includes(k) && k !== clearance?.name && Array.isArray(acl[k]));
  const shares = new Map<string, Set<string>>(shareNames.map((k) => [k, new Set((acl[k] as string[]).map(String))]));
  const shareFields = shareNames.map((k) => {
    const chips = h('div', { class: 'chips' });
    const draw = () =>
      chips.replaceChildren(
        ...[...shares.get(k)!].map((v) => {
          const x = h('button', { type: 'button', class: 'chip', title: `Stop sharing with ${v}` }, v, ' ×');
          x.addEventListener('click', () => (shares.get(k)!.delete(v), draw()));
          return x;
        }),
      );
    draw();
    return h('div', { class: 'field' }, h('span', { class: 'label' }, `Shared individually (${k})`), chips);
  });

  const visibility = r.tags?.sources?.visibility;
  const save = h('button', { type: 'submit', class: 'btn' }, 'Save access tags');
  const form = h(
    'form',
    {
      class: 'stack',
      onsubmit: async (ev: SubmitEvent) => {
        ev.preventDefault();
        const next: Record<string, string[] | number> = {};
        for (const [name, list] of rows) {
          const picked = [...list.querySelectorAll('select')].map((s) => s.value).filter(Boolean);
          if (list.querySelector('select.invalid')) {
            toastError(new Error(`Replace the ${labelOf(name)} value marked "not in vocabulary" first.`));
            return;
          }
          if (picked.length) next[name] = [...new Set(picked)];
        }
        if (clearanceField?.sel.value) next[clearance!.name] = Number(clearanceField.sel.value);
        for (const [k, set] of shares) if (set.size) next[k] = [...set];
        save.disabled = true;
        try {
          await api.post(`/api/admin/documents/${encodeURIComponent(r.doc_id)}/tags`, { acl: next, approve: false });
          toast('Access tags saved. The index is re-tagged in place within seconds.', { kind: 'success' });
          reload();
        } catch (err) {
          toastError(err, 'Could not save access tags');
        } finally {
          save.disabled = false;
        }
      },
    },
    h('p', { class: 'hint' }, 'Who may read this document — the same lists the upload form offers. Department and Region are required for anyone but the people it is shared with individually. Administrators only.'),
    h('div', { class: 'row' }, listFields, clearanceField?.el ?? null),
    shareFields,
    h('div', null, save),
  );
  return h(
    'details',
    { class: 'access-editor' },
    h('summary', null, 'Edit access tags', visibility ? h('span', { class: 'muted small' }, ` · currently ${visibility === 'private' ? 'private (Only me)' : 'shared by tags'}`) : null),
    form,
  );
}

export interface DeleteReport {
  documents: number;
  chunks: number;
  blobs: number;
  blobs_kept_shared: number;
  blobs_left_at_source: number;
  traces: number;
  expectations_updated: number;
  not_found: string[];
  errors: string[];
}

/** The one confirmation for a permanent delete, so the single and the bulk action say the same thing. */
export async function deletePermanently(api: ApiClient, docIds: string[], what: string): Promise<DeleteReport | null> {
  const ok = confirm(
    `Permanently delete ${what}?\n\n` +
      'Removed now: its index entries (no answer can cite it), its stored copy (unless another document has the same bytes), ' +
      'its status history, queued work, and query traces that mention it.\n\n' +
      '• An UPLOAD has no other copy — it cannot be recovered.\n' +
      '• A document from a crawled source (folder / blob) COMES BACK on that source\'s next sync if the file is still there — delete it at the source to keep it out.',
  );
  if (!ok) return null;
  try {
    const rep = await api.post<DeleteReport>('/api/admin/documents/delete', { doc_ids: docIds, permanent: true });
    toast(
      `Deleted ${fmtNum(rep.documents)} document(s): ${fmtNum(rep.chunks)} index entries, ${fmtNum(rep.blobs)} stored copies` +
        (rep.blobs_kept_shared ? ` (${rep.blobs_kept_shared} kept: shared)` : '') +
        (rep.traces ? `, ${fmtNum(rep.traces)} traces` : '') +
        (rep.errors.length ? ` — ${rep.errors.length} error(s)` : '') + '.',
      { kind: rep.errors.length ? 'error' : 'success' },
    );
    return rep;
  } catch (err) {
    toastError(err, 'Delete failed');
    return null;
  }
}

function deleteButton(api: ApiClient, docId: string, name: string, done: () => void): HTMLElement {
  const btn = h('button', { type: 'button', class: 'btn btn-danger' }, 'Delete permanently');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    if (await deletePermanently(api, [docId], `"${name}"`)) done();
    else btn.disabled = false;
  });
  return btn;
}
