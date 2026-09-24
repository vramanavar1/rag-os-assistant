// Runs list (#/runs?source_id=) and run detail with progress (#/runs/<run_id>).
import { isAbortError } from '../api';
import { fmtDate, fmtDuration, fmtNum, h, mount } from '../dom';
import { problemBox, statusBadge } from '../ui';
import { PIPELINE_STATUSES, type Run, type RunDetail } from '../types';
import { autoRefresh, code, dataTable, kv, pageHeader, sourceSelect, type View, type ViewContext } from './common';

function duration(r: Run): string {
  if (!r.started_at) return '—';
  const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now();
  return fmtDuration(end - new Date(r.started_at).getTime());
}

export const runsView: View = async (ctx: ViewContext) => {
  if (ctx.params[0]) return runDetail(ctx, ctx.params[0]);

  const sourceId = ctx.query.get('source_id') ?? '';
  const select = await sourceSelect(ctx.api, 'runs-source', sourceId);
  select.addEventListener('change', () => ctx.navigate(`#/runs${select.value ? `?source_id=${encodeURIComponent(select.value)}` : ''}`));
  const content = h('div', null, h('p', { class: 'muted' }, 'Loading…'));

  const refresh = async () => {
    try {
      const runs = await ctx.api.get<Run[]>('/api/admin/ingestion/runs', { source_id: sourceId || undefined, limit: 50 }, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      mount(
        content,
        dataTable<Run>(
          [
            { label: 'Run', render: (r) => h('a', { href: `#/runs/${encodeURIComponent(r.run_id)}` }, code(r.run_id, 10)) },
            { label: 'Source', render: (r) => r.source_id },
            { label: 'Trigger', render: (r) => r.trigger },
            { label: 'Status', render: (r) => statusBadge(r.status) },
            { label: 'Started', render: (r) => fmtDate(r.started_at) },
            { label: 'Duration', class: 'num', render: duration },
            { label: 'Discovered', class: 'num', render: (r) => fmtNum(r.discovered ?? 0) },
            { label: 'Queued', class: 'num', render: (r) => fmtNum(r.queued ?? 0) },
            { label: 'Unchanged', class: 'num', render: (r) => fmtNum(r.unchanged ?? 0) },
            { label: 'Deleted', class: 'num', render: (r) => fmtNum(r.deleted ?? 0) },
            { label: 'Error', render: (r) => (r.error ? h('span', { class: 'text-critical truncate', title: r.error }, r.error) : '') },
          ],
          runs,
          { empty: 'No runs yet.', caption: 'Ingestion runs', onRowClick: (r) => ctx.navigate(`#/runs/${encodeURIComponent(r.run_id)}`), rowLabel: (r) => `Run ${r.run_id}` },
        ),
      );
    } catch (err) {
      if (!isAbortError(err)) mount(content, problemBox(err));
    }
  };

  mount(
    ctx.root,
    pageHeader('Ingestion runs', autoRefresh(ctx, refresh, 10_000), h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void refresh() }, 'Refresh')),
    h('div', { class: 'toolbar' }, h('label', { for: 'runs-source' }, 'Source'), select),
    content,
  );
  await refresh();
};

async function runDetail(ctx: ViewContext, runId: string): Promise<void> {
  const content = h('div', { class: 'stack-lg' }, h('p', { class: 'muted' }, 'Loading…'));
  const errorSlot = h('div');
  const refresh = async () => {
    try {
      const d = await ctx.api.get<RunDetail>(`/api/admin/ingestion/runs/${encodeURIComponent(runId)}`, undefined, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      errorSlot.replaceChildren();
      const r = d.run;
      const pct = Math.max(0, Math.min(100, Number(d.percent) || 0));
      const progress = Object.entries(d.progress ?? {}).sort(
        ([a], [b]) => PIPELINE_STATUSES.indexOf(a as never) - PIPELINE_STATUSES.indexOf(b as never),
      );
      mount(
        content,
        h(
          'section',
          { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', null, 'Progress'), statusBadge(r.status)),
          h(
            'div',
            { class: 'progress-row' },
            h('progress', { id: 'run-progress', max: 100, value: pct, 'aria-label': 'Run progress' }),
            h('span', { class: 'progress-pct' }, `${pct.toFixed(pct < 10 ? 1 : 0)}%`),
          ),
          h(
            'dl',
            { class: 'kv compact inline-kv' },
            h('dt', null, 'Throughput'),
            h('dd', null, d.throughput_per_min != null ? `${fmtNum(Math.round(d.throughput_per_min))} docs/min` : '—'),
            h('dt', null, 'ETA'),
            h('dd', null, d.eta_seconds != null ? fmtDuration(d.eta_seconds * 1000) : r.finished_at ? 'finished' : '—'),
          ),
          progress.length
            ? h(
                'ul',
                { class: 'status-counts' },
                progress.map(([status, n]) =>
                  h('li', null, h('a', { href: `#/documents?status=${encodeURIComponent(status)}&source_id=${encodeURIComponent(r.source_id)}` }, statusBadge(status), h('span', { class: 'num' }, fmtNum(n)))),
                ),
              )
            : null,
        ),
        h(
          'section',
          { class: 'card' },
          h('h2', null, 'Run'),
          kv([
            ['Run ID', code(r.run_id)],
            ['Source', h('a', { href: `#/runs?source_id=${encodeURIComponent(r.source_id)}` }, r.source_id)],
            ['Trigger', r.trigger],
            ['Started', fmtDate(r.started_at)],
            ['Finished', fmtDate(r.finished_at)],
            ['Duration', duration(r)],
            ['Discovered', fmtNum(r.discovered ?? 0)],
            ['Queued', fmtNum(r.queued ?? 0)],
            ['Unchanged', fmtNum(r.unchanged ?? 0)],
            ['Deleted', fmtNum(r.deleted ?? 0)],
            ['Error', r.error ? h('span', { class: 'text-critical pre-wrap' }, r.error) : '—'],
          ]),
        ),
        h(
          'section',
          { class: 'card' },
          h('h2', null, 'Errors'),
          dataTable(
            [
              { label: 'Stage', render: (e) => e.stage },
              { label: 'Error type', render: (e) => h('code', null, e.error_type) },
              { label: 'Count', class: 'num', render: (e) => fmtNum(e.count) },
              { label: 'Example', render: (e) => (e.example ? h('span', { class: 'truncate', title: e.example }, e.example) : '—') },
            ],
            d.errors ?? [],
            { empty: 'No errors in this run.', caption: 'Errors by stage' },
          ),
          (d.errors ?? []).length
            ? h('p', null, h('a', { href: `#/documents?status=FAILED&source_id=${encodeURIComponent(r.source_id)}` }, `Show failed documents for ${r.source_id} →`))
            : null,
        ),
      );
    } catch (err) {
      if (!isAbortError(err)) mount(errorSlot, problemBox(err));
    }
  };
  mount(
    ctx.root,
    h('p', { class: 'breadcrumb' }, h('a', { href: '#/runs' }, '← Runs')),
    pageHeader(`Run ${runId.length > 14 ? `${runId.slice(0, 14)}…` : runId}`, autoRefresh(ctx, refresh, 5_000), h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void refresh() }, 'Refresh')),
    errorSlot,
    content,
  );
  await refresh();
}
