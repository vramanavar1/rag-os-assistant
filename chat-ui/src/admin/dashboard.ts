// Dashboard: status tiles, queue depths, controls state and source/facet × status tables.
import { isAbortError } from '../api';
import { fmtCompact, fmtNum, h, mount, type Child } from '../dom';
import { problemBox, statusBadge, statusMeta } from '../ui';
import { IN_FLIGHT_STATUSES, PIPELINE_STATUSES, type IngestionSummary } from '../types';
import { autoRefresh, dataTable, getFacets, pageHeader, type View, type ViewContext } from './common';

const DEFAULT_GROUPS = ['department', 'region', 'doc_type'];

function docsLink(query: Record<string, string>, content: Child): HTMLElement {
  return h('a', { href: `#/documents?${new URLSearchParams(query).toString()}` }, content);
}

function tile(label: string, value: number, opts: { status?: string; tone?: string; hint?: string } = {}): HTMLElement {
  const tone = opts.tone ?? (opts.status ? statusMeta(opts.status).tone : 'neutral');
  const icon = opts.status ? statusMeta(opts.status).icon : null;
  const inner = [
    h('span', { class: 'tile-label' }, icon ? h('span', { class: 'tile-icon', 'aria-hidden': 'true' }, icon) : null, label),
    h('span', { class: 'tile-value', title: fmtNum(value) }, fmtCompact(value)),
    opts.hint ? h('span', { class: 'tile-hint' }, opts.hint) : null,
  ];
  return opts.status
    ? h('a', { class: `tile tone-${tone}${value > 0 ? '' : ' zero'}`, href: `#/documents?status=${opts.status}` }, inner)
    : h('div', { class: `tile tone-${tone}` }, inner);
}

type PivotRow = { key: string; counts: Record<string, number>; total: number };

function pivot(rows: { key: string; status: string; count: number }[]): { rows: PivotRow[]; statuses: string[] } {
  const map = new Map<string, PivotRow>();
  const seen = new Set<string>();
  for (const r of rows) {
    const row = map.get(r.key) ?? { key: r.key, counts: {}, total: 0 };
    row.counts[r.status] = (row.counts[r.status] ?? 0) + r.count;
    row.total += r.count;
    map.set(r.key, row);
    seen.add(r.status);
  }
  const ordered = PIPELINE_STATUSES.filter((s) => seen.has(s));
  const extra = [...seen].filter((s) => !ordered.includes(s as never)).sort();
  return { rows: [...map.values()].sort((a, b) => b.total - a.total), statuses: [...ordered, ...extra] };
}

function pivotTable(title: string, keyLabel: string, data: ReturnType<typeof pivot>, link: (key: string, status?: string) => Record<string, string>): HTMLElement {
  return h(
    'section',
    { class: 'card' },
    h('h2', null, title),
    dataTable<PivotRow>(
      [
        { label: keyLabel, render: (r) => docsLink(link(r.key), r.key || '(none)') },
        ...data.statuses.map((s) => ({
          label: statusMeta(s).label,
          class: 'num',
          render: (r: PivotRow) => (r.counts[s] ? docsLink(link(r.key, s), fmtNum(r.counts[s])) : h('span', { class: 'muted' }, '0')),
        })),
        { label: 'Total', class: 'num strong', render: (r) => fmtNum(r.total) },
      ],
      data.rows,
      { empty: 'No documents yet.', caption: title },
    ),
  );
}

export const dashboardView: View = async (ctx: ViewContext) => {
  const groupSelect = h('select', { id: 'group-by' }, h('option', { value: '' }, 'No grouping'));
  const errorSlot = h('div');
  const content = h('div', { class: 'stack-lg' }, h('p', { class: 'muted' }, 'Loading…'));
  const updated = h('span', { class: 'muted small', 'aria-live': 'polite' });

  let facetNames = DEFAULT_GROUPS;
  try {
    const f = await getFacets(ctx.api);
    const names = Object.keys(f.facets ?? {});
    if (names.length) facetNames = names;
  } catch {
    /* use defaults */
  }
  for (const n of facetNames) groupSelect.appendChild(h('option', { value: n }, n));
  groupSelect.value = ctx.query.get('group_by') ?? facetNames[0] ?? '';

  const refresh = async () => {
    try {
      const s = await ctx.api.get<IngestionSummary>('/api/admin/ingestion/summary', { group_by: groupSelect.value || undefined }, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      errorSlot.replaceChildren();
      render(s);
      updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      if (!isAbortError(err)) mount(errorSlot, problemBox(err));
    }
  };

  const render = (s: IngestionSummary) => {
    const t = s.totals ?? {};
    const total = Object.values(t).reduce((a, b) => a + b, 0);
    const inFlight = IN_FLIGHT_STATUSES.reduce((a, st) => a + (t[st] ?? 0), 0);
    const q = s.queue ?? { priority: { active: 0, dead_letter: 0 }, bulk: { active: 0, dead_letter: 0 } };
    const c = s.controls ?? { paused: false, max_concurrency: 0, paused_sources: [] };
    const lane = (name: 'priority' | 'bulk') =>
      h(
        'div',
        { class: 'lane' },
        h('h3', null, name === 'priority' ? 'Priority lane' : 'Bulk lane'),
        h(
          'dl',
          { class: 'kv compact' },
          h('dt', null, 'Active'),
          h('dd', { class: 'num' }, fmtNum(q[name]?.active ?? 0)),
          h('dt', null, 'Dead-letter'),
          h('dd', { class: 'num' }, q[name]?.dead_letter ? h('a', { href: `#/dlq?lane=${name}`, class: 'text-critical' }, fmtNum(q[name].dead_letter)) : '0'),
        ),
      );
    const g = groupSelect.value;
    mount(
      content,
      h(
        'div',
        { class: 'tiles', role: 'list' },
        [
          tile('Total documents', total, { hint: 'all statuses' }),
          tile('Indexed', t.INDEXED ?? 0, { status: 'INDEXED' }),
          tile('Failed', t.FAILED ?? 0, { status: 'FAILED' }),
          tile('In flight', inFlight, { tone: 'info', hint: 'discovered → embedded' }),
          tile('Skipped (unchanged)', t.SKIPPED_UNCHANGED ?? 0, { status: 'SKIPPED_UNCHANGED' }),
          tile('Deleted', t.DELETED ?? 0, { status: 'DELETED' }),
        ].map((el) => {
          el.setAttribute('role', 'listitem');
          return el;
        }),
      ),
      h(
        'div',
        { class: 'grid-2' },
        h('section', { class: 'card' }, h('h2', null, 'Queues'), h('div', { class: 'lanes' }, lane('priority'), lane('bulk'))),
        h(
          'section',
          { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', null, 'Controls'), h('a', { href: '#/controls', class: 'small' }, 'Change')),
          h(
            'dl',
            { class: 'kv compact' },
            h('dt', null, 'Ingestion'),
            h('dd', null, c.paused ? statusBadge('PAUSED') : statusBadge('RUNNING')),
            h('dt', null, 'Max concurrency'),
            h('dd', { class: 'num' }, fmtNum(c.max_concurrency)),
            h('dt', null, 'Paused sources'),
            h('dd', null, c.paused_sources?.length ? c.paused_sources.join(', ') : 'none'),
          ),
        ),
      ),
      pivotTable(
        'Documents by source',
        'Source',
        pivot((s.by_source ?? []).map((r) => ({ key: r.source_id, status: r.status, count: r.count }))),
        (key, status) => ({ source_id: key, ...(status ? { status } : {}) }),
      ),
      g
        ? pivotTable(
            `Documents by ${g}`,
            g,
            pivot((s.by_facet ?? []).map((r) => ({ key: r.value, status: r.status, count: r.count }))),
            (key, status) => ({ facet: `${g}:${key}`, ...(status ? { status } : {}) }),
          )
        : null,
    );
  };

  groupSelect.addEventListener('change', () => {
    history.replaceState(null, '', `#/dashboard${groupSelect.value ? `?group_by=${encodeURIComponent(groupSelect.value)}` : ''}`);
    void refresh();
  });
  const refreshBtn = h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void refresh() }, 'Refresh');
  mount(
    ctx.root,
    pageHeader('Ingestion dashboard', updated, autoRefresh(ctx, refresh, 10_000), refreshBtn),
    h('div', { class: 'toolbar' }, h('label', { for: 'group-by' }, 'Group by facet'), groupSelect),
    errorSlot,
    content,
  );
  await refresh();
};
