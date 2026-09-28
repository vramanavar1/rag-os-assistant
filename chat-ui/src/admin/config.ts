// YAML configuration editor (#/config/<kind>) with optimistic concurrency (If-Match: etag)
// and the "Explain access" tool on the access-policy tab.
import { ApiError, isAbortError } from '../api';
import { h, mount } from '../dom';
import { attributeSummary, problemBox, toast } from '../ui';
import { fetchDevPrincipals, principalLabel } from '../signin';
import type { ConfigDoc, ExplainResult, Principal } from '../types';
import { pageHeader, type View, type ViewContext } from './common';

const KINDS = [
  { id: 'sources', label: 'Sources', hint: 'Document sources, their schedules and lanes.' },
  { id: 'access-policy', label: 'Access policy', hint: 'How principal attributes map to document access filters.' },
  { id: 'facets', label: 'Facets', hint: 'The taxonomy: facet names, labels and allowed values.' },
  { id: 'path-rules', label: 'Path rules', hint: 'Path patterns that assign facets and ACL tags during ingestion.' },
] as const;

export const configView: View = async (ctx: ViewContext) => {
  const kind = KINDS.find((k) => k.id === ctx.params[0]) ?? KINDS[0];
  const editorId = `cfg-${kind.id}`;
  const editor = h('textarea', { id: editorId, class: 'mono code-editor', rows: 26, spellcheck: 'false', wrap: 'off', autocapitalize: 'off', autocomplete: 'off', disabled: true });
  const etagLabel = h('code', { class: 'small' }, '—');
  const dirtyLabel = h('span', { class: 'small muted', 'aria-live': 'polite' });
  const status = h('div', { 'aria-live': 'polite' });
  const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary', disabled: true }, 'Save');
  const reloadBtn = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Reload');
  let etag = '';
  let original = '';

  const dirty = () => editor.value !== original;
  const updateDirty = () => {
    dirtyLabel.textContent = dirty() ? 'Unsaved changes' : '';
    saveBtn.disabled = !dirty() || !etag;
  };
  const beforeUnload = (ev: BeforeUnloadEvent) => {
    if (dirty()) ev.preventDefault();
  };
  window.addEventListener('beforeunload', beforeUnload);
  ctx.onCleanup(() => window.removeEventListener('beforeunload', beforeUnload));
  ctx.setLeaveGuard(() => !dirty() || window.confirm('Discard unsaved configuration changes?'));

  const load = async () => {
    status.replaceChildren('Loading…');
    editor.disabled = true;
    try {
      const doc = await ctx.api.get<ConfigDoc>(`/api/admin/config/${kind.id}`, undefined, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      etag = doc.etag;
      original = doc.yaml ?? '';
      editor.value = original;
      etagLabel.textContent = etag || '—';
      status.replaceChildren();
    } catch (err) {
      if (!isAbortError(err)) mount(status, problemBox(err));
    } finally {
      editor.disabled = false;
      updateDirty();
    }
  };

  const save = async () => {
    saveBtn.disabled = true;
    status.replaceChildren('Saving…');
    try {
      const res = await ctx.api.put<{ etag: string }>(`/api/admin/config/${kind.id}`, { yaml: editor.value }, { headers: { 'If-Match': etag } });
      etag = res.etag;
      original = editor.value;
      etagLabel.textContent = etag;
      status.replaceChildren();
      toast(`${kind.label} configuration saved.`, { kind: 'success' });
    } catch (err) {
      const box = problemBox(err);
      if (err instanceof ApiError && (err.status === 409 || err.status === 412)) {
        box.appendChild(h('p', null, 'Someone else changed this configuration after you loaded it. Copy your edits, then reload the latest version.'));
        box.appendChild(
          h(
            'button',
            {
              type: 'button',
              class: 'btn btn-sm',
              onclick: () => {
                original = editor.value; // skip the discard prompt
                void load();
              },
            },
            'Load latest (discard my edits)',
          ),
        );
      }
      mount(status, box);
    } finally {
      updateDirty();
    }
  };

  editor.addEventListener('input', updateDirty);
  editor.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault();
      if (!saveBtn.disabled) void save();
    }
  });
  reloadBtn.addEventListener('click', () => {
    if (!dirty() || window.confirm('Discard unsaved changes and reload?')) void load();
  });

  const form = h(
    'form',
    {
      class: 'card stack',
      onsubmit: (ev: SubmitEvent) => {
        ev.preventDefault();
        void save();
      },
    },
    h('div', { class: 'card-head' }, h('label', { for: editorId }, h('strong', null, `${kind.label} (YAML)`)), h('span', { class: 'small muted' }, 'ETag ', etagLabel)),
    h('p', { class: 'hint' }, kind.hint, ' Changes are validated by the API before they are stored (Ctrl+S saves).'),
    editor,
    h('div', { class: 'form-actions' }, saveBtn, reloadBtn, dirtyLabel),
    status,
  );

  mount(
    ctx.root,
    pageHeader('Configuration'),
    h(
      'div',
      { class: 'tabs', role: 'tablist', 'aria-label': 'Configuration kind' },
      KINDS.map((k) => h('a', { href: `#/config/${k.id}`, role: 'tab', class: 'tab', 'aria-selected': String(k.id === kind.id) }, k.label)),
    ),
    form,
    kind.id === 'access-policy' ? explainPanel(ctx) : null,
  );
  await load();
};

// ---------------------------------------------------------------- explain access

function parseAttributes(text: string): Record<string, unknown> {
  const t = text.trim();
  if (!t) return {};
  if (t.startsWith('{')) {
    const obj: unknown = JSON.parse(t);
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new Error('Attributes must be a JSON object.');
    return obj as Record<string, unknown>;
  }
  const out: Record<string, unknown> = {};
  for (const line of t.split('\n')) {
    const l = line.trim();
    if (!l || l.startsWith('#')) continue;
    const i = l.search(/[=:]/);
    if (i <= 0) throw new Error(`Expected key=value, got "${l}".`);
    const key = l.slice(0, i).trim();
    const value = l.slice(i + 1).trim();
    out[key] = value.includes(',') ? value.split(',').map((v) => v.trim()).filter(Boolean) : /^-?\d+$/.test(value) ? Number(value) : value;
  }
  return out;
}

function formatAttributes(attrs: Record<string, unknown>): string {
  return Object.entries(attrs)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(', ') : typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`)
    .join('\n');
}

function pretty(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2);
}

function explainPanel(ctx: ViewContext): HTMLElement {
  const attrs = h('textarea', { id: 'explain-attrs', rows: 5, class: 'mono', spellcheck: 'false', placeholder: 'department=HR\nregion=EU\nclearance=2' });
  const roles = h('input', { id: 'explain-roles', type: 'text', placeholder: 'contributor, admin', autocomplete: 'off' });
  const result = h('div', { 'aria-live': 'polite' });
  const presets = h('div', { class: 'row' });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Explain');

  const fill = (a: Record<string, unknown>, r: string[]) => {
    attrs.value = formatAttributes(a ?? {});
    roles.value = (r ?? []).join(', ');
  };
  if (ctx.me) {
    const me = ctx.me;
    presets.appendChild(h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => fill(me.attributes, me.roles) }, 'Use my identity'));
  }
  if (ctx.config.dev_auth_enabled) {
    const sel = h('select', { id: 'explain-principal', 'aria-label': 'Fill from a dev principal' }, h('option', { value: '' }, 'Fill from dev principal…'));
    let principals: Principal[] = [];
    fetchDevPrincipals()
      .then((list) => {
        principals = list;
        list.forEach((p) => sel.appendChild(h('option', { value: p.id }, principalLabel(p))));
      })
      .catch(() => sel.remove());
    sel.addEventListener('change', () => {
      const p = principals.find((x) => x.id === sel.value);
      if (p) fill(p.attributes, p.roles);
    });
    presets.appendChild(sel);
  }

  const form = h(
    'form',
    {
      class: 'card stack',
      onsubmit: async (ev: SubmitEvent) => {
        ev.preventDefault();
        let attributes: Record<string, unknown>;
        try {
          attributes = parseAttributes(attrs.value);
        } catch (err) {
          mount(result, problemBox(err));
          return;
        }
        const roleList = roles.value.split(',').map((r) => r.trim()).filter(Boolean);
        submit.disabled = true;
        result.replaceChildren('Evaluating…');
        try {
          const r = await ctx.api.post<ExplainResult>('/api/admin/access-policy/explain', { attributes, roles: roleList });
          mount(
            result,
            h(
              'div',
              { class: 'stack' },
              h(
                'div',
                { class: 'row' },
                r.deny_all
                  ? h('span', { class: 'badge tone-critical' }, h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, '✕'), 'Denies all documents')
                  : r.bypass
                    ? h('span', { class: 'badge tone-warning' }, h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, '!'), 'Bypasses access filtering')
                    : h('span', { class: 'badge tone-good' }, h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, '✓'), 'Filtered access'),
              ),
              h('h3', null, 'Attributes used'),
              Array.isArray(r.attributes_used)
                ? h('p', null, r.attributes_used.length ? r.attributes_used.join(', ') : 'none')
                : r.attributes_used && typeof r.attributes_used === 'object'
                  ? h('p', { class: 'mono' }, attributeSummary(r.attributes_used as Record<string, unknown>))
                  : h('p', null, String(r.attributes_used ?? 'none')),
              h('h3', null, 'Search filter'),
              h('pre', { class: 'code-block' }, r.filter == null || r.filter === '' ? '(none)' : pretty(r.filter)),
            ),
          );
        } catch (err) {
          mount(result, problemBox(err));
        } finally {
          submit.disabled = false;
        }
      },
    },
    h('h2', null, 'Explain access'),
    h('p', { class: 'hint' }, 'See which search filter the saved policy produces for a set of principal attributes. One key=value per line; commas make a list (or paste a JSON object).'),
    presets,
    h('div', { class: 'field' }, h('label', { for: 'explain-attrs' }, 'Attributes'), attrs),
    h('div', { class: 'field' }, h('label', { for: 'explain-roles' }, 'Roles (comma-separated)'), roles),
    h('div', { class: 'form-actions' }, submit),
    result,
  );
  return form;
}
