// Dead-letter messages per lane (#/dlq?lane=bulk|priority).
import { isAbortError } from '../api';
import { fmtNum, h, mount } from '../dom';
import { problemBox } from '../ui';
import type { DlqMessage } from '../types';
import { code, dataTable, openDocumentDialog, pageHeader, retryDocs, type View, type ViewContext } from './common';

const LANES = ['bulk', 'priority'] as const;

export const dlqView: View = async (ctx: ViewContext) => {
  const lane = LANES.includes(ctx.query.get('lane') as never) ? (ctx.query.get('lane') as (typeof LANES)[number]) : 'bulk';
  const selected = new Set<string>();
  const content = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  const retryBtn = h('button', { type: 'button', class: 'btn', disabled: true }, 'Retry selected');
  const countLabel = h('span', { class: 'muted small' });
  const sync = () => {
    retryBtn.disabled = selected.size === 0;
    retryBtn.textContent = selected.size ? `Retry selected (${selected.size})` : 'Retry selected';
  };

  const tabs = h(
    'div',
    { class: 'tabs', role: 'tablist', 'aria-label': 'Lane' },
    LANES.map((l) =>
      h('a', { href: `#/dlq?lane=${l}`, role: 'tab', class: 'tab', 'aria-selected': String(l === lane) }, l === 'bulk' ? 'Bulk lane' : 'Priority lane'),
    ),
  );

  const load = async () => {
    selected.clear();
    sync();
    try {
      const res = await ctx.api.get<{ messages: DlqMessage[] }>('/api/admin/ingestion/dlq', { lane }, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      const msgs = res.messages ?? [];
      countLabel.textContent = `${fmtNum(msgs.length)} message${msgs.length === 1 ? '' : 's'}`;
      mount(
        content,
        dataTable<DlqMessage>(
          [
            {
              label: '',
              class: 'select',
              render: (m) => {
                const cb = h('input', { type: 'checkbox', 'aria-label': `Select ${m.doc_id}` });
                cb.addEventListener('change', () => {
                  if (cb.checked) selected.add(m.doc_id);
                  else selected.delete(m.doc_id);
                  sync();
                });
                return cb;
              },
            },
            { label: 'Document', render: (m) => code(m.doc_id, 16) },
            { label: 'Version', render: (m) => code(m.version_key, 12) },
            { label: 'Source', render: (m) => m.source_id },
            { label: 'Lane', render: (m) => m.lane },
            { label: 'Run', render: (m) => (m.run_id ? h('a', { href: `#/runs/${encodeURIComponent(m.run_id)}` }, code(m.run_id, 10)) : '—') },
          ],
          msgs,
          {
            empty: `The ${lane} dead-letter queue is empty.`,
            caption: 'Dead-letter messages',
            onRowClick: (m) => openDocumentDialog(ctx.api, m.doc_id),
            rowLabel: (m) => `Open document ${m.doc_id}`,
          },
        ),
      );
    } catch (err) {
      if (!isAbortError(err)) mount(content, problemBox(err));
    }
  };

  retryBtn.addEventListener('click', async () => {
    retryBtn.disabled = true;
    if ((await retryDocs(ctx.api, { doc_ids: [...selected] })) !== null) await load();
    sync();
  });

  mount(
    ctx.root,
    pageHeader('Dead letters', h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void load() }, 'Refresh')),
    tabs,
    h('p', { class: 'hint' }, 'Messages that exhausted their delivery attempts. Retrying requeues the document through the normal pipeline.'),
    h('div', { class: 'toolbar' }, retryBtn, h('span', { class: 'spacer' }), countLabel),
    content,
  );
  await load();
};
