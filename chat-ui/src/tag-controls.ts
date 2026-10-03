// The dropdowns that set a document's tags. One builder, used by the upload form AND the admin access-tag
// editor, so the two cannot drift: same vocabulary (facets.yaml, via GET /api/facets), same labels, same
// hierarchy indent, same clearance ladder. Matching is exact - a typed `hr` never matches a person whose
// department is `HR` - which is why nothing that sets a tag accepts free text.
import { h } from './dom';
import type { Facet, UploadOptions } from './types';

export interface Choice {
  value: string;
  label: string;
}

/** A <select> over a facet's configured vocabulary. `leading` choices come first (e.g. "from folder", "Everyone"). */
export function vocabularySelect(
  opts: { id: string; name: string; facet: Facet; selected?: string; leading?: Choice[] },
): HTMLSelectElement {
  const vocab = opts.facet.vocabulary ?? [];
  const select = h(
    'select',
    { id: opts.id, name: opts.name },
    (opts.leading ?? []).map((c) => h('option', { value: c.value }, c.label)),
    // Indent children so a hierarchy reads as one without needing <optgroup> per level.
    vocab.map((v) => h('option', { value: v.id }, v.parent ? `  ${v.label}` : v.label)),
  );
  const sel = opts.selected ?? '';
  const known = [...select.options].some((o) => o.value === sel);
  if (sel && !known) {
    // A value already stored that the vocabulary does not know (a legacy hand-typed tag). Shown so it is not
    // silently lost - and flagged, so it is replaced rather than saved again.
    select.appendChild(h('option', { value: sel, class: 'not-in-vocabulary' }, `${sel} (not in vocabulary)`));
    select.classList.add('invalid');
  }
  select.value = sel;
  select.addEventListener('change', () => select.classList.toggle('invalid', !!select.selectedOptions[0]?.classList.contains('not-in-vocabulary')));
  return select;
}

/** The clearance ladder as "1 · Internal". `min` hides levels below it (a contributor may only raise clearance). */
export function clearanceSelect(
  opts: { id: string; name?: string; clearance: NonNullable<UploadOptions['clearance']>; selected?: number | null; min?: number | null },
): HTMLSelectElement {
  const min = opts.min ?? null;
  const levels = opts.clearance.levels.filter((l) => min === null || l.value >= min);
  const select = h(
    'select',
    { id: opts.id, name: opts.name ?? opts.clearance.name },
    levels.map((l) => h('option', { value: String(l.value) }, `${l.value} · ${l.label}`)),
  );
  if (opts.selected !== null && opts.selected !== undefined) select.value = String(opts.selected);
  return select;
}
