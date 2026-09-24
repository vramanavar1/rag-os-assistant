// Ingestion controls: pause/resume, max concurrency, paused sources.
import { isAbortError } from '../api';
import { h, mount } from '../dom';
import { problemBox, statusBadge, toast } from '../ui';
import type { Controls } from '../types';
import { getSources, pageHeader, type View, type ViewContext } from './common';

export const controlsView: View = async (ctx: ViewContext) => {
  const content = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  mount(ctx.root, pageHeader('Ingestion controls'), content);

  const [controlsRes, sourcesRes] = await Promise.allSettled([
    ctx.api.get<Controls>('/api/admin/ingestion/controls', undefined, { signal: ctx.signal }),
    getSources(ctx.api),
  ]);
  if (ctx.signal.aborted) return;
  if (controlsRes.status === 'rejected') {
    if (!isAbortError(controlsRes.reason)) mount(content, problemBox(controlsRes.reason));
    return;
  }
  let controls = controlsRes.value;
  const sourceIds = sourcesRes.status === 'fulfilled' ? sourcesRes.value.map((s) => s.id) : [];

  const status = h('div', { 'aria-live': 'polite' });
  const stateBadge = h('span');
  const paused = h('input', { type: 'checkbox', id: 'ctl-paused', role: 'switch' });
  const maxConc = h('input', { type: 'number', id: 'ctl-max', min: 1, max: 256, step: 1, required: true, inputmode: 'numeric' });
  const extraSources = h('input', { type: 'text', id: 'ctl-extra', placeholder: 'other-source, another-source', autocomplete: 'off' });
  const sourceBoxes = h('div', { class: 'check-grid' });
  const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Save controls');
  const quickBtn = h('button', { type: 'button', class: 'btn' });

  const fill = (c: Controls) => {
    controls = c;
    paused.checked = !!c.paused;
    maxConc.value = String(c.max_concurrency ?? 1);
    const pausedSet = new Set(c.paused_sources ?? []);
    mount(
      sourceBoxes,
      sourceIds.length
        ? sourceIds.map((id) => {
            const cbId = `ctl-src-${id}`.replace(/[^A-Za-z0-9_-]/g, '_');
            return h('div', { class: 'check' }, h('input', { type: 'checkbox', id: cbId, value: id, checked: pausedSet.has(id) }), h('label', { for: cbId }, id));
          })
        : h('p', { class: 'muted' }, 'Source list unavailable — type ids below.'),
    );
    extraSources.value = [...pausedSet].filter((id) => !sourceIds.includes(id)).join(', ');
    mount(stateBadge, statusBadge(c.paused ? 'PAUSED' : 'RUNNING'));
    quickBtn.textContent = c.paused ? 'Resume ingestion now' : 'Pause ingestion now';
  };

  const save = async (next: Controls) => {
    saveBtn.disabled = true;
    quickBtn.disabled = true;
    status.replaceChildren('Saving…');
    try {
      const res = await ctx.api.put<Controls | undefined>('/api/admin/ingestion/controls', next);
      fill(res && typeof res === 'object' && 'paused' in res ? res : next);
      status.replaceChildren();
      toast('Controls saved. Workers pick them up within their refresh interval.', { kind: 'success' });
    } catch (err) {
      mount(status, problemBox(err));
    } finally {
      saveBtn.disabled = false;
      quickBtn.disabled = false;
    }
  };

  const collect = (): Controls => {
    const pausedSources = [
      ...[...sourceBoxes.querySelectorAll<HTMLInputElement>('input:checked')].map((cb) => cb.value),
      ...extraSources.value.split(',').map((s) => s.trim()).filter(Boolean),
    ];
    return { paused: paused.checked, max_concurrency: Math.max(1, Math.floor(Number(maxConc.value) || 1)), paused_sources: [...new Set(pausedSources)] };
  };

  quickBtn.addEventListener('click', () => void save({ ...controls, paused: !controls.paused }));

  const form = h(
    'form',
    {
      class: 'card stack',
      onsubmit: (ev: SubmitEvent) => {
        ev.preventDefault();
        if (!maxConc.reportValidity()) return;
        void save(collect());
      },
    },
    h('div', { class: 'check' }, paused, h('label', { for: 'ctl-paused' }, 'Pause all ingestion (workers stop taking new messages)')),
    h('div', { class: 'field' }, h('label', { for: 'ctl-max' }, 'Max concurrency per worker'), maxConc),
    h('fieldset', null, h('legend', null, 'Paused sources'), sourceBoxes, h('div', { class: 'field' }, h('label', { for: 'ctl-extra' }, 'Other source ids (comma-separated)'), extraSources)),
    h('div', { class: 'form-actions' }, saveBtn),
    status,
  );

  fill(controls);
  mount(
    content,
    h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, 'Current state'), stateBadge), h('div', { class: 'form-actions' }, quickBtn)),
    form,
  );
};
