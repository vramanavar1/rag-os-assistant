// Documents: filters (status/source/facet/text), keyset paging, detail dialog, bulk retry,
// CSV export and the upload widget. Deep link: #/documents/<doc_id>.
import { isAbortError } from '../api';
import { fmtAgo, fmtBytes, fmtNum, h, mount, show } from '../dom';
import { copyTag, problemBox, statusBadge, toast, toastError } from '../ui';
import { createUploadWidget } from '../upload';
import { PIPELINE_STATUSES, type DocumentRecord, type Page } from '../types';
import { accessSummary, dataTable, getFacets, getRequiredAccess, openDocumentDialog, pageHeader, retryDocs, sourceSelect, type View, type ViewContext } from './common';

const PAGE_SIZE = 50;

function listHash(q: URLSearchParams): string {
  const s = q.toString();
  return `#/documents${s ? `?${s}` : ''}`;
}

async function facetSelect(ctx: ViewContext, selected: string): Promise<HTMLSelectElement> {
  const select = h('select', { id: 'doc-facet', name: 'facet' }, h('option', { value: '' }, 'Any facet'));
  try {
    const { facets } = await getFacets(ctx.api);
    for (const [name, facet] of Object.entries(facets ?? {})) {
      select.appendChild(
        h(
          'optgroup',
          { label: facet.label || name },
          facet.values.map((v) => h('option', { value: `${name}:${v.id}` }, `${v.label || v.id}${typeof v.count === 'number' ? ` (${fmtNum(v.count)})` : ''}`)),
        ),
      );
    }
  } catch {
    /* facet list unavailable: keep the current value only */
  }
  if (selected && ![...select.options].some((o) => o.value === selected)) select.appendChild(h('option', { value: selected }, selected));
  select.value = selected;
  return select;
}

async function exportCsv(ctx: ViewContext, status: string, sourceId: string, btn: HTMLButtonElement): Promise<void> {
  btn.disabled = true;
  try {
    const res = await ctx.api.post<{ name: string; url: string }>('/api/admin/ingestion/export', {
      ...(status ? { status } : {}),
      ...(sourceId ? { source_id: sourceId } : {}),
    });
    const target = new URL(res.url, window.location.href);
    if (target.origin !== window.location.origin) {
      // Never send the bearer token to another origin (e.g. a pre-signed storage URL).
      window.open(target.href, '_blank', 'noopener');
      return;
    }
    const blob = await ctx.api.request<Blob>('GET', target.pathname + target.search, { responseType: 'blob', headers: { Accept: 'text/csv, */*' } });
    const href = URL.createObjectURL(blob);
    const a = h('a', { href, download: res.name || 'documents.csv', class: 'visually-hidden' });
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(href), 10_000);
    toast(`Exported ${res.name || 'CSV'}.`, { kind: 'success' });
  } catch (err) {
    toastError(err, 'Export failed');
  } finally {
    btn.disabled = false;
  }
}

export const documentsView: View = async (ctx: ViewContext) => {
  const q = ctx.query;
  const status = q.get('status') ?? '';
  const sourceId = q.get('source_id') ?? '';
  const facet = q.get('facet') ?? '';
  const text = q.get('q') ?? '';

  // ---- filter form
  const statusSel = h('select', { id: 'doc-status', name: 'status' }, h('option', { value: '' }, 'Any status'), PIPELINE_STATUSES.map((s) => h('option', { value: s }, s)));
  statusSel.value = status;
  const [sourceSel, facetSel, required] = await Promise.all([sourceSelect(ctx.api, 'doc-source', sourceId), facetSelect(ctx, facet), getRequiredAccess(ctx.api)]);
  if (ctx.signal.aborted) return;
  const textInput = h('input', { id: 'doc-q', name: 'q', type: 'search', value: text, placeholder: 'Title or path contains…', maxlength: 200 });
  const filterForm = h(
    'form',
    {
      class: 'filters',
      onsubmit: (ev: SubmitEvent) => {
        ev.preventDefault();
        const next = new URLSearchParams();
        if (statusSel.value) next.set('status', statusSel.value);
        if (sourceSel.value) next.set('source_id', sourceSel.value);
        if (facetSel.value) next.set('facet', facetSel.value);
        if (textInput.value.trim()) next.set('q', textInput.value.trim());
        ctx.navigate(listHash(next));
      },
    },
    h('div', { class: 'field' }, h('label', { for: 'doc-status' }, 'Status'), statusSel),
    h('div', { class: 'field' }, h('label', { for: 'doc-source' }, 'Source'), sourceSel),
    h('div', { class: 'field' }, h('label', { for: 'doc-facet' }, 'Facet'), facetSel),
    h('div', { class: 'field grow' }, h('label', { for: 'doc-q' }, 'Search'), textInput),
    h('div', { class: 'field actions' }, h('button', { type: 'submit', class: 'btn btn-primary' }, 'Apply'), h('a', { href: '#/documents', class: 'btn btn-ghost' }, 'Reset')),
  );

  // ---- bulk actions
  const selected = new Set<string>();
  const retrySelBtn = h('button', { type: 'button', class: 'btn', disabled: true }, 'Retry selected');
  const retryAllBtn = h('button', { type: 'button', class: 'btn' }, sourceId ? `Retry all failed in ${sourceId}` : 'Retry all failed');
  const exportBtn = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Export CSV');
  const uploadBtn = h('button', { type: 'button', class: 'btn btn-ghost', 'aria-expanded': 'false', 'aria-controls': 'doc-upload' }, 'Upload');
  const uploadPanel = h('section', { id: 'doc-upload', class: 'card', hidden: true });
  const updateSelection = () => {
    retrySelBtn.disabled = selected.size === 0;
    retrySelBtn.textContent = selected.size ? `Retry selected (${selected.size})` : 'Retry selected';
  };

  const rows: DocumentRecord[] = [];
  let next: string | null = null;
  const tableSlot = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  const moreBtn = h('button', { type: 'button', class: 'btn', hidden: true }, 'Load more');
  const countLabel = h('span', { class: 'muted small', 'aria-live': 'polite' });

  const selectAll = h('input', { type: 'checkbox', 'aria-label': 'Select all loaded documents' });
  selectAll.addEventListener('change', () => {
    for (const r of rows) {
      if (selectAll.checked) selected.add(r.doc_id);
      else selected.delete(r.doc_id);
    }
    tableSlot.querySelectorAll<HTMLInputElement>('input[data-doc]').forEach((cb) => (cb.checked = selectAll.checked));
    updateSelection();
  });

  const openDoc = (docId: string, focusAccess = false) => {
    history.replaceState(null, '', `#/documents/${encodeURIComponent(docId)}${q.toString() ? `?${q.toString()}` : ''}`);
    openDocumentDialog(ctx.api, docId, () => history.replaceState(null, '', listHash(q)), { focusAccess, onChanged: () => void reload() });
  };

  const renderTable = () => {
    const table = dataTable<DocumentRecord>(
      [
        {
          label: '',
          class: 'select',
          render: (r) => {
            const cb = h('input', { type: 'checkbox', 'data-doc': r.doc_id, checked: selected.has(r.doc_id), 'aria-label': `Select ${r.title || r.path}` });
            cb.addEventListener('change', () => {
              if (cb.checked) selected.add(r.doc_id);
              else selected.delete(r.doc_id);
              updateSelection();
            });
            return cb;
          },
        },
        {
          label: 'Document',
          render: (r) => h('div', { class: 'doc-cell' }, h('strong', { class: 'truncate', title: r.title || r.path }, r.title || r.path.split('/').pop() || r.path), h('span', { class: 'mono muted truncate', title: r.path }, r.path)),
        },
        { label: 'Doc ID', render: (r) => copyTag(r.doc_id, null, 'Copy document ID', 12) },
        { label: 'Access', render: (r) => accessSummary(r, required, () => openDoc(r.doc_id, true)) },
        { label: 'Source', render: (r) => r.source_id },
        { label: 'Status', render: (r) => statusBadge(r.status) },
        { label: 'Stage', render: (r) => r.stage || '—' },
        { label: 'Tries', class: 'num', render: (r) => fmtNum(r.attempts ?? 0) },
        { label: 'Size', class: 'num', render: (r) => fmtBytes(r.size) },
        { label: 'Error', render: (r) => (r.error_type ? h('code', { class: 'text-critical', title: r.error_message ?? '' }, r.error_type) : '') },
        { label: 'Updated', render: (r) => h('span', { title: r.updated_at ?? '' }, fmtAgo(r.updated_at)) },
      ],
      rows,
      { empty: 'No documents match these filters.', caption: 'Documents', onRowClick: (r) => openDoc(r.doc_id), rowLabel: (r) => `Open ${r.title || r.path}` },
    );
    const headCell = table.querySelector('thead th');
    if (headCell && rows.length) headCell.replaceChildren(selectAll);
    mount(tableSlot, table);
    show(moreBtn, !!next);
    countLabel.textContent = `${fmtNum(rows.length)} shown${next ? ' (more available)' : ''}`;
  };

  const load = async (after?: string) => {
    moreBtn.disabled = true;
    try {
      const page = await ctx.api.get<Page<DocumentRecord>>(
        '/api/admin/ingestion/documents',
        { status: status || undefined, source_id: sourceId || undefined, facet: facet || undefined, q: text || undefined, after, limit: PAGE_SIZE },
        { signal: ctx.signal },
      );
      if (ctx.signal.aborted) return;
      rows.push(...(page.items ?? []));
      next = page.next ?? null;
      renderTable();
    } catch (err) {
      if (isAbortError(err)) return;
      if (after) toastError(err, 'Could not load more');
      else mount(tableSlot, problemBox(err));
    } finally {
      moreBtn.disabled = false;
    }
  };

  const reload = async () => {
    rows.length = 0;
    next = null;
    selected.clear();
    selectAll.checked = false;
    updateSelection();
    await load();
  };

  moreBtn.addEventListener('click', () => void (next && load(next)));
  retrySelBtn.addEventListener('click', async () => {
    retrySelBtn.disabled = true;
    if ((await retryDocs(ctx.api, { doc_ids: [...selected] })) !== null) await reload();
    updateSelection();
  });
  retryAllBtn.addEventListener('click', async () => {
    if (!window.confirm(`Requeue every FAILED document${sourceId ? ` in ${sourceId}` : ''}?`)) return;
    retryAllBtn.disabled = true;
    if ((await retryDocs(ctx.api, { status: 'FAILED', ...(sourceId ? { source_id: sourceId } : {}) })) !== null) await reload();
    retryAllBtn.disabled = false;
  });
  exportBtn.addEventListener('click', () => void exportCsv(ctx, status, sourceId, exportBtn));
  uploadBtn.addEventListener('click', () => {
    const open = uploadPanel.hidden !== false;
    if (open && !uploadPanel.childElementCount) {
      mount(uploadPanel, h('h2', null, 'Upload documents'), createUploadWidget(ctx.api, {
        documentHref: (docId) => `#/documents/${encodeURIComponent(docId)}`,
        facetPickers: true,
        onFinished: () => void reload(),
      }));
    }
    show(uploadPanel, open);
    uploadBtn.setAttribute('aria-expanded', String(open));
  });

  mount(
    ctx.root,
    pageHeader('Documents', uploadBtn, exportBtn),
    filterForm,
    uploadPanel,
    h('div', { class: 'toolbar' }, retrySelBtn, retryAllBtn, h('span', { class: 'spacer' }), countLabel),
    tableSlot,
    h('div', { class: 'load-more' }, moreBtn),
  );
  await load();
  if (ctx.params[0] && !ctx.signal.aborted) openDoc(ctx.params[0]);
};

