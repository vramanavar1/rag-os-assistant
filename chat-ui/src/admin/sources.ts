// Sources list with "Sync now".
import { isAbortError } from '../api';
import { fmtAgo, h, mount } from '../dom';
import { problemBox, toast, toastError } from '../ui';
import type { Source } from '../types';
import { dataTable, getSources, pageHeader, type View, type ViewContext } from './common';

function enabledBadge(enabled: boolean): HTMLElement {
  return h(
    'span',
    { class: `badge ${enabled ? 'tone-good' : 'tone-muted'}` },
    h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, enabled ? '✓' : '−'),
    enabled ? 'Enabled' : 'Disabled',
  );
}

export const sourcesView: View = async (ctx: ViewContext) => {
  const content = h('div', null, h('p', { class: 'muted' }, 'Loading…'));

  const sync = async (s: Source, btn: HTMLButtonElement, note: HTMLElement) => {
    btn.disabled = true;
    note.replaceChildren('Starting…');
    try {
      const res = await ctx.api.post<{ run_id: string }>(`/api/admin/sources/${encodeURIComponent(s.id)}/sync`, {});
      mount(note, h('a', { href: `#/runs/${encodeURIComponent(res.run_id)}` }, 'View run →'));
      toast(`Sync started for ${s.id}.`, { kind: 'success' });
    } catch (err) {
      // e.g. 409/422 problem+json: "this source type must be run from the CLI"
      mount(note, problemBox(err));
      toastError(err, `Could not sync ${s.id}`);
    } finally {
      btn.disabled = false;
    }
  };

  const load = async () => {
    try {
      const sources = await getSources(ctx.api, true);
      if (ctx.signal.aborted) return;
      mount(
        content,
        dataTable<Source>(
          [
            { label: 'Source', render: (s) => h('a', { href: `#/runs?source_id=${encodeURIComponent(s.id)}`, title: 'Show runs' }, h('strong', null, s.id)) },
            { label: 'Type', render: (s) => h('code', null, s.type) },
            { label: 'Enabled', render: (s) => enabledBadge(s.enabled) },
            { label: 'Domain', render: (s) => s.domain || '—' },
            { label: 'Lane', render: (s) => s.lane || '—' },
            { label: 'Schedule', render: (s) => (s.schedule ? h('code', null, s.schedule) : 'manual') },
            { label: 'Last run', render: (s) => h('span', { title: s.last_run_started ?? '' }, fmtAgo(s.last_run_started)) },
            {
              label: 'Actions',
              render: (s) => {
                const note = h('div', { class: 'row-note', 'aria-live': 'polite' });
                const btn = h('button', { type: 'button', class: 'btn btn-sm', disabled: !s.enabled, title: s.enabled ? 'Start an ingestion run now' : 'Source is disabled' }, 'Sync now');
                btn.addEventListener('click', () => void sync(s, btn, note));
                return h('div', null, btn, note);
              },
            },
          ],
          sources,
          { empty: 'No sources configured. Add them under Config → sources.', caption: 'Sources' },
        ),
      );
    } catch (err) {
      if (!isAbortError(err)) mount(content, problemBox(err));
    }
  };

  mount(
    ctx.root,
    pageHeader('Sources', h('a', { href: '#/config/sources', class: 'btn btn-ghost' }, 'Edit configuration'), h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void load() }, 'Refresh')),
    content,
  );
  await load();
};
