// Danger zone (Admin > Controls): delete ALL data and start afresh, keeping configuration.
// POST /api/admin/reset {confirm: <index name>, include_traces}. The button stays disabled until the active index
// name is typed exactly - the same "type it to mean it" pattern as granting an administrator role.
import { isAbortError } from '../api';
import { fmtDuration, fmtNum, h, mount } from '../dom';
import { problemBox, toast } from '../ui';
import type { ViewContext } from './common';

interface Preview {
  index: string;
  documents: number;
  chunks: number | null;
  queue: Record<string, number>;
  traces: number;
  expectations: number;
  keeps: string[];
}

interface ResetResult {
  ok: boolean;
  index: string;
  message: string;
  steps: { step: string; result?: unknown; error?: string; ms: number }[];
}

export function dangerZone(ctx: ViewContext, afterReset: () => Promise<void>): HTMLElement {
  const body = h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'Loading…'));
  const card = h(
    'section',
    { class: 'card danger-zone', 'aria-labelledby': 'dz-title' },
    h('h2', { id: 'dz-title' }, 'Danger zone: reset all data'),
    body,
  );

  const render = (p: Preview, last?: ResetResult) => {
    // No placeholder: showing the name in the box would turn "type it to mean it" into "click and press a key".
    const confirmInput = h('input', { id: 'dz-confirm', type: 'text', autocomplete: 'off', spellcheck: 'false' });
    const traces = h('input', { type: 'checkbox', id: 'dz-traces', checked: true });
    const go = h('button', { type: 'button', class: 'btn btn-danger', disabled: true }, 'Delete all data');
    const out = h('div', { 'aria-live': 'polite' }, last ? report(last) : null);
    confirmInput.addEventListener('input', () => (go.disabled = confirmInput.value.trim() !== p.index));
    const queued = Object.entries(p.queue).reduce((n, [, v]) => n + (Number(v) || 0), 0);
    go.addEventListener('click', async () => {
      go.disabled = true;
      go.textContent = 'Deleting…';
      try {
        const r = await ctx.api.post<ResetResult>('/api/admin/reset', { confirm: confirmInput.value.trim(), include_traces: traces.checked }, { timeoutMs: 15 * 60_000 });
        toast(r.ok ? 'All data deleted. Ingestion is paused.' : 'The reset stopped part-way — run it again.', { kind: r.ok ? 'success' : 'error' });
        await afterReset();
        render(await ctx.api.get<Preview>('/api/admin/reset/preview'), r); // counts after, report kept
      } catch (err) {
        mount(out, problemBox(err));
      } finally {
        go.textContent = 'Delete all data';
      }
    });
    mount(
      body,
      h(
        'div',
        { class: 'notice notice-warning' },
        h('p', null, h('strong', null, 'This deletes everything RAG-OS has ingested and cannot be undone from here.')),
        h(
          'ul',
          null,
          h('li', null, `${fmtNum(p.documents)} documents with their status history and ingestion runs`),
          h('li', null, `${p.chunks === null ? 'all' : fmtNum(p.chunks)} index entries in `, h('code', null, p.index), ' (the index itself and its schema stay)'),
          h('li', null, 'every stored copy and export — uploads have no other copy and are lost for good'),
          h('li', null, `${fmtNum(queued)} queued messages`),
          h('li', null, `${fmtNum(p.traces)} query traces and ${fmtNum(p.expectations)} expectations (unless unticked below)`),
        ),
        h('p', null, h('strong', null, 'Kept: '), p.keeps.join('; '), '.'),
        h(
          'p',
          null,
          h('strong', null, 'Afterwards: '),
          'ingestion is left paused. Until you resume it and re-ingest, every question gets "No grounded answer". ',
          'Resuming re-reads and re-embeds every crawled document — with a hosted embedding model that is billed per token.',
        ),
      ),
      h('div', { class: 'check' }, traces, h('label', { for: 'dz-traces' }, 'Also delete query traces and expectations (untick if they must be kept for audit or legal hold)')),
      h('div', { class: 'field' }, h('label', { for: 'dz-confirm' }, 'To confirm, type the index name ', h('code', null, p.index)), confirmInput),
      h('div', { class: 'form-actions' }, go),
      out,
    );
  };

  void (async () => {
    try {
      render(await ctx.api.get<Preview>('/api/admin/reset/preview', undefined, { signal: ctx.signal }));
    } catch (err) {
      if (!isAbortError(err)) mount(body, problemBox(err));
    }
  })();
  return card;
}

function report(r: ResetResult): HTMLElement {
  return h(
    'div',
    { class: r.ok ? 'notice' : 'problem' },
    h('p', null, h('strong', null, r.message)),
    h(
      'ol',
      null,
      r.steps.map((s) =>
        h(
          'li',
          null,
          `${s.step}: `,
          s.error ? h('span', { class: 'text-critical' }, s.error) : h('code', null, JSON.stringify(s.result)),
          h('span', { class: 'muted small' }, ` (${fmtDuration(s.ms)})`),
        ),
      ),
    ),
  );
}
