// Recent documents, newest first: the answer to "did my upload actually work?".
//
// One component, two mount points - the chat page's upload panel and the admin console's Uploads view - both
// reading GET /api/uploads. The server decides scope from the caller's role (your own documents, or everyone's),
// so there is a single paging implementation and a single access rule rather than two that drift apart.
//
// Deliberately built from dom.ts/ui.ts primitives rather than admin/common.ts: chat.ts and admin.ts are separate
// esbuild entry points, and importing the admin module here would pull the whole console into the chat bundle.
import { isAbortError, type ApiClient } from './api';
import { fmtAgo, fmtBytes, h, mount, show } from './dom';
import { problemBox, statusBadge } from './ui';
import { IN_FLIGHT_STATUSES, type DocumentRecord, type UploadPage } from './types';

const PAGE_SIZE = 10;

interface Tab {
  id: string;
  label: string;
  statuses: readonly string[];
  /** Which statuses this tab's count adds up. Empty means "everything". */
  counted: readonly string[];
}

const ALL_TAB: Tab = { id: 'all', label: 'All', statuses: [], counted: [] };
const TABS: readonly Tab[] = [
  ALL_TAB,
  { id: 'failed', label: 'Failed', statuses: ['FAILED'], counted: ['FAILED'] },
  { id: 'active', label: 'In progress', statuses: IN_FLIGHT_STATUSES, counted: IN_FLIGHT_STATUSES },
];

export interface UploadsListOptions {
  /** Where a single document's detail lives. Omitted (a non-admin) leaves rows inert, because the detail
   *  endpoint behind them is admin-only - a link that 403s is worse than no link. */
  documentHref?: (docId: string) => string;
  pageSize?: number;
  signal?: AbortSignal;
}

export interface UploadsList {
  el: HTMLElement;
  /** Re-fetch from the first page. Called after an upload finishes. */
  reload: () => void;
}

function total(counts: Record<string, number>, of: readonly string[]): number {
  const keys = of.length ? of : Object.keys(counts);
  return keys.reduce((sum, k) => sum + (counts[k] ?? 0), 0);
}

function row(rec: DocumentRecord, href: ((docId: string) => string) | undefined): HTMLElement {
  const name = rec.title || rec.path.split('/').pop() || rec.path;
  const failed = rec.status === 'FAILED';
  // The tracking id is shown because it is the only durable handle a non-admin has on a document: it is what
  // they can quote to somebody who can open the console.
  const meta = [
    rec.stage && !failed ? `Stage: ${rec.stage}` : null,
    rec.status === 'INDEXED' ? `${rec.chunk_count ?? 0} chunks` : null,
    fmtBytes(rec.size),
    fmtAgo(rec.updated_at),
  ].filter(Boolean) as string[];
  return h(
    'li',
    { class: 'upload-item' },
    h(
      'div',
      { class: 'upload-head' },
      h('span', { class: 'upload-name', title: rec.path }, href ? h('a', { href: href(rec.doc_id) }, name) : name),
      statusBadge(rec.status),
    ),
    h(
      'div',
      { class: 'upload-detail' },
      failed
        ? h('span', { class: 'text-critical' }, rec.error_message || rec.error_type || 'Processing failed.')
        : h('span', { class: 'muted' }, meta.join(' · ')),
    ),
  );
}

export function createUploadsList(api: ApiClient, opts: UploadsListOptions = {}): UploadsList {
  const pageSize = opts.pageSize ?? PAGE_SIZE;
  const list = h('ul', { class: 'upload-list', 'aria-live': 'polite' });
  const empty = h('p', { class: 'empty', hidden: true }, 'Nothing here yet.');
  const problem = h('div');
  const moreBtn = h('button', { type: 'button', class: 'btn btn-sm', hidden: true }, `Next ${pageSize}`);
  const countLabel = h('span', { class: 'muted small' });
  let active: Tab = ALL_TAB;
  let next: string | null = null;
  let loading = false;

  // Each tab is carried with its own button, so nothing here has to index one array by the other's position.
  const tabs = TABS.map((tab) => ({
    tab,
    count: h('span', { class: 'count', hidden: true }),
    button: h('button', {
      type: 'button',
      class: 'chip',
      'aria-pressed': String(tab.id === active.id),
      onclick: () => {
        if (tab.id === active.id) return;
        active = tab;
        for (const other of tabs) other.button.setAttribute('aria-pressed', String(other.tab.id === tab.id));
        void load();
      },
    }),
  }));
  for (const { tab, button, count } of tabs) button.append(tab.label, count);

  const load = async (after?: string): Promise<void> => {
    if (loading) return;
    loading = true;
    moreBtn.disabled = true;
    mount(problem);
    try {
      const page = await api.get<UploadPage>(
        '/api/uploads',
        { status: active.statuses.length ? [...active.statuses] : undefined, after, limit: pageSize },
        { signal: opts.signal },
      );
      if (!after) mount(list);
      for (const rec of page.items) list.appendChild(row(rec, opts.documentHref));
      next = page.next;
      show(moreBtn, !!next);
      const counts = page.counts ?? {};
      for (const { tab, count } of tabs) {
        const n = total(counts, tab.counted);
        count.textContent = String(n);
        count.hidden = n === 0;
      }
      const shown = list.childElementCount;
      countLabel.textContent = shown ? `${shown} shown${next ? ' (more available)' : ''}` : '';
      show(empty, shown === 0);
    } catch (err) {
      if (!isAbortError(err)) mount(problem, problemBox(err));
    } finally {
      loading = false;
      moreBtn.disabled = false;
    }
  };

  moreBtn.addEventListener('click', () => void (next && load(next)));

  const el = h(
    'section',
    { class: 'upload', 'aria-label': 'Recent documents' },
    // Toggle buttons rather than role=tablist: that role promises role=tab children driven by
    // aria-selected and arrow keys, and claiming it without them reads worse than plain buttons.
    h('div', { class: 'row' }, tabs.map((t) => t.button), h('span', { class: 'spacer' }), countLabel),
    problem,
    list,
    empty,
    moreBtn,
  );
  void load();
  return { el, reload: () => void load() };
}
