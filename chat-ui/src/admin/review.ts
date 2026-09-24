// Review queue: documents whose classification needs a human decision.
// Approve -> POST /api/admin/documents/{doc_id}/tags {facets, acl?, approve: true}
import { isAbortError, type ApiClient } from '../api';
import { fmtAgo, fmtNum, h, mount, show } from '../dom';
import { problemBox, statusBadge, toast } from '../ui';
import type { DocumentRecord, Facet, Page } from '../types';
import { getFacets, openDocumentDialog, pageHeader, type View, type ViewContext } from './common';

/** "classifier:0.82" / "embedding 0.64" / "path_rule" -> {source, confidence?} */
function provenance(raw: string | undefined): { source: string; confidence?: number } | null {
  if (!raw) return null;
  const m = /^(.*?)[\s:=@]*(\d*\.?\d+)(%?)$/.exec(raw);
  if (m && m[2]) {
    let c = Number.parseFloat(m[2]);
    if (m[3] === '%' || c > 1) c /= 100;
    if (c >= 0 && c <= 1) return { source: m[1] || 'classifier', confidence: c };
  }
  return { source: raw };
}

function provenanceChip(raw: string | undefined): HTMLElement | null {
  const p = provenance(raw);
  if (!p) return null;
  const pct = p.confidence !== undefined ? Math.round(p.confidence * 100) : null;
  const tone = pct === null ? 'neutral' : pct >= 75 ? 'good' : pct >= 50 ? 'warning' : 'critical';
  return h(
    'span',
    { class: `badge tone-${tone}`, title: `Assigned by ${p.source}${pct !== null ? ` with ${pct}% confidence` : ''}` },
    p.source,
    pct !== null ? ` · ${pct}%` : null,
  );
}

function facetFieldset(docId: string, name: string, facet: Facet | undefined, current: string[]): HTMLFieldSetElement {
  const known = facet?.values ?? [];
  const extra = current.filter((v) => !known.some((k) => k.id === v)).map((id) => ({ id, label: `${id} (not in taxonomy)` }));
  const all = [...known, ...extra];
  return h(
    'fieldset',
    { class: 'facet', 'data-facet': name },
    h('legend', null, facet?.label || name),
    all.length
      ? all.map((v) => {
          const id = `rv-${docId}-${name}-${v.id}`.replace(/[^A-Za-z0-9_-]/g, '_');
          return h('div', { class: 'check' }, h('input', { type: 'checkbox', id, value: v.id, checked: current.includes(v.id) }), h('label', { for: id }, v.label || v.id));
        })
      : h('p', { class: 'muted' }, 'No values defined.'),
  );
}

function reviewCard(api: ApiClient, rec: DocumentRecord, facets: Record<string, Facet>, onDone: (card: HTMLElement) => void): HTMLElement {
  const tags = rec.tags ?? { facets: {}, acl: {}, sources: {} };
  const names = [...new Set([...Object.keys(facets), ...Object.keys(tags.facets ?? {})])];
  const status = h('div', { 'aria-live': 'polite' });
  const aclId = `acl-${rec.doc_id}`;
  const originalAcl = JSON.stringify(tags.acl ?? {}, null, 2);
  const aclInput = h('textarea', { id: aclId, rows: 5, class: 'mono', spellcheck: 'false' });
  aclInput.value = originalAcl;
  const approve = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Approve');

  const current = h(
    'ul',
    { class: 'facet-summary' },
    Object.entries(tags.facets ?? {}).map(([name, values]) =>
      h('li', null, h('strong', null, `${facets[name]?.label || name}: `), values.length ? values.join(', ') : '—', ' ', provenanceChip(tags.sources?.[`facet:${name}`])),
    ),
  );

  const form: HTMLFormElement = h(
    'form',
    {
      class: 'review-form',
      onsubmit: async (ev: SubmitEvent) => {
        ev.preventDefault();
        const chosen: Record<string, string[]> = {};
        form.querySelectorAll<HTMLFieldSetElement>('fieldset[data-facet]').forEach((fs) => {
          chosen[fs.dataset.facet!] = [...fs.querySelectorAll<HTMLInputElement>('input[type=checkbox]:checked')].map((cb) => cb.value);
        });
        const body: { facets: Record<string, string[]>; approve: true; acl?: unknown } = { facets: chosen, approve: true };
        if (aclInput.value.trim() !== originalAcl.trim()) {
          try {
            const parsed: unknown = JSON.parse(aclInput.value || '{}');
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('ACL must be a JSON object');
            body.acl = parsed;
          } catch (err) {
            mount(status, problemBox(err));
            aclInput.focus();
            return;
          }
        }
        approve.disabled = true;
        status.replaceChildren('Saving…');
        try {
          await api.post(`/api/admin/documents/${encodeURIComponent(rec.doc_id)}/tags`, body);
          toast(`Approved “${rec.title || rec.path}”.`, { kind: 'success' });
          onDone(card);
        } catch (err) {
          mount(status, problemBox(err));
          approve.disabled = false;
        }
      },
    },
    h('div', { class: 'facet-grid' }, names.map((n) => facetFieldset(rec.doc_id, n, facets[n], tags.facets?.[n] ?? []))),
    h('details', null, h('summary', null, 'Access tags (ACL)'), h('label', { for: aclId, class: 'hint' }, 'JSON object, e.g. {"department": ["HR"], "clearance": 2}. Leave unchanged to keep the current ACL.'), aclInput),
    h(
      'div',
      { class: 'form-actions' },
      approve,
      h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => openDocumentDialog(api, rec.doc_id) }, 'Details'),
    ),
    status,
  );

  const card: HTMLElement = h(
    'article',
    { class: 'card review-card', 'aria-label': rec.title || rec.path },
    h(
      'div',
      { class: 'card-head' },
      h('div', null, h('h2', null, rec.title || rec.path.split('/').pop() || rec.path), h('div', { class: 'mono muted small break' }, `${rec.source_id} · ${rec.path}`)),
      h('div', { class: 'row' }, statusBadge(rec.review_status || 'PENDING'), statusBadge(rec.status)),
    ),
    h('p', { class: 'small muted' }, `Updated ${fmtAgo(rec.updated_at)} · ${fmtNum(rec.chunk_count ?? 0)} chunks`),
    h('h3', null, 'Current classification'),
    Object.keys(tags.facets ?? {}).length ? current : h('p', { class: 'muted' }, 'No facets assigned yet.'),
    h('h3', null, 'Corrected facets'),
    form,
  );
  return card;
}

export const reviewView: View = async (ctx: ViewContext) => {
  const list = h('div', { class: 'stack-lg' }, h('p', { class: 'muted' }, 'Loading…'));
  const moreBtn = h('button', { type: 'button', class: 'btn', hidden: true }, 'Load more');
  let next: string | null = null;
  let facets: Record<string, Facet> = {};
  let count = 0;

  try {
    facets = (await getFacets(ctx.api)).facets ?? {};
  } catch {
    /* edit only existing facet values */
  }

  const onDone = (card: HTMLElement) => {
    card.remove();
    count--;
    if (count <= 0 && !next) mount(list, h('p', { class: 'empty' }, 'All caught up: the review queue is empty.'));
  };

  const load = async (after?: string) => {
    moreBtn.disabled = true;
    try {
      const page = await ctx.api.get<Page<DocumentRecord>>('/api/admin/review-queue', { after, limit: 50 }, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      if (!after) list.replaceChildren();
      const items = page.items ?? [];
      count += items.length;
      items.forEach((rec) => list.appendChild(reviewCard(ctx.api, rec, facets, onDone)));
      next = page.next ?? null;
      if (!after && !items.length) mount(list, h('p', { class: 'empty' }, 'The review queue is empty.'));
      show(moreBtn, !!next);
    } catch (err) {
      if (!isAbortError(err)) mount(list, problemBox(err));
    } finally {
      moreBtn.disabled = false;
    }
  };
  moreBtn.addEventListener('click', () => void (next && load(next)));

  mount(
    ctx.root,
    pageHeader('Review queue', h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => { count = 0; void load(); } }, 'Refresh')),
    h('p', { class: 'hint' }, 'Low-confidence or conflicting classifications. Correct the facets (and optionally the access tags), then approve.'),
    list,
    h('div', { class: 'load-more' }, moreBtn),
  );
  await load();
};
