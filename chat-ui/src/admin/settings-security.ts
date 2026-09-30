// Settings (Security): assign a person's access attributes and this application's roles.
//
// Everything this page offers comes from GET /api/admin/directory - the values, the roles, the caveats and the
// sentence about token staleness. Nothing about what access means is decided here, for the same reason the
// Account Information panel does not decide it: two copies of an access rule eventually disagree, and the copy
// in a browser is the one nobody audits.
//
// Writes are a single PUT of the desired state carrying If-Match. Not grant/revoke calls: the directory does not
// deduplicate assignments, so a second grant leaves two of them and a later single revoke leaves the role in
// place. A desired-state write is idempotent, and the ETag stops a tab loaded before somebody else's change
// from writing its stale idea of the role set back.
import { h, mount } from '../dom';
import { problemBox, toast } from '../ui';
import type {
  DirectoryCapability,
  DirectoryUserState,
  DirectoryWriteResult,
} from '../types';
import { loading, pageHeader, type View, type ViewContext } from './common';

const NOT_SET = '';

function card(title: string, ...body: (Node | string | null)[]): HTMLElement {
  return h('section', { class: 'card stack' }, h('div', { class: 'card-head' }, h('h3', null, title)), ...body);
}

/** A select built from the master list the API sent. The blank option means "leave as it is". */
function valueSelect(id: string, spec: DirectoryCapability['attributes'][number], current: string | null) {
  const options = [
    h('option', { value: NOT_SET }, current ? 'Leave unchanged' : 'Not set'),
    ...spec.values.map((v) =>
      h('option', { value: v.value, selected: v.value === current }, v.label || v.value)),
  ];
  // Offered only where the API says the attribute may be emptied - a required attribute cleared leaves the
  // person able to read nothing at all, so the server refuses it and the option should not be there to pick.
  if (spec.clearable && current) options.push(h('option', { value: 'CLEAR' }, 'Clear the value'));
  return h('select', { id, class: 'select' }, ...options) as HTMLSelectElement;
}

export const settingsSecurityView: View = async (ctx: ViewContext) => {
  const emailInput = h('input', {
    type: 'email', id: 'sec-email', required: true, placeholder: 'name@example.com',
    autocomplete: 'off', spellcheck: 'false',
  }) as HTMLInputElement;
  const lookupBtn = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Look up') as HTMLButtonElement;
  const panel = h('div', { class: 'stack' });
  const status = h('div', { class: 'stack' });

  const lookupForm = h('form', {
      class: 'card stack',
      onsubmit: (ev: SubmitEvent) => {
        ev.preventDefault();
        if (!emailInput.reportValidity()) return;
        void lookup(emailInput.value.trim());
      },
    },
    h('div', { class: 'field' },
      h('label', { for: 'sec-email' }, 'Work email or user principal name'),
      emailInput),
    h('div', { class: 'form-actions' }, lookupBtn, status),
  );

  mount(ctx.root, pageHeader('Settings (Security)'), loading());

  let cap: DirectoryCapability;
  try {
    cap = await ctx.api.get<DirectoryCapability>('/api/admin/directory', undefined, { signal: ctx.signal });
  } catch (err) {
    mount(ctx.root, pageHeader('Settings (Security)'), problemBox(err));
    return;
  }

  mount(
    ctx.root,
    pageHeader('Settings (Security)'),
    h('p', { class: 'muted' },
      'Assign the attributes that decide what a person can read, and the roles that decide what they can do. ' +
      'Changes are written to the identity provider.'),
    cap.enabled
      ? null
      : h('div', { class: 'notice notice-warning' },
          'Directory administration is not configured for this deployment, so nothing here can be saved. ' +
          'The values below are what the access policy would allow. See the README section on Entra user ' +
          'attributes for the settings and permissions that switch it on.'),
    ...cap.warnings.map((w) => h('div', { class: 'notice notice-warning' }, w)),
    lookupForm,
    panel,
  );

  async function lookup(email: string): Promise<void> {
    lookupBtn.disabled = true;
    status.replaceChildren('Looking up…');
    mount(panel);
    try {
      const state = await ctx.api.get<DirectoryUserState>(
        `/api/admin/directory/users/${encodeURIComponent(email)}`, undefined, { signal: ctx.signal });
      status.replaceChildren();
      render(state);
    } catch (err) {
      status.replaceChildren();
      mount(panel, problemBox(err));
    } finally {
      lookupBtn.disabled = false;
    }
  }

  function render(state: DirectoryUserState): void {
    let etag = state.etag;
    const selects = new Map<string, HTMLSelectElement>();
    const roleBoxes = new Map<string, HTMLInputElement>();
    const held = new Map(state.roles.map((r) => [r.value, r]));

    const attributeFields = cap.attributes.map((spec) => {
      const id = `sec-attr-${spec.name}`;
      const select = valueSelect(id, spec, state.attributes[spec.name] ?? null);
      selects.set(spec.name, select);
      return h('div', { class: 'field' },
        h('label', { for: id }, spec.label + (spec.required ? ' (required)' : '')),
        select,
        spec.description ? h('p', { class: 'hint' }, spec.description) : null);
    });

    const confirmInput = h('input', {
      type: 'text', id: 'sec-confirm', autocomplete: 'off', spellcheck: 'false',
      placeholder: state.user_principal_name,
    }) as HTMLInputElement;
    const confirmField = h('div', { class: 'field' },
      h('label', { for: 'sec-confirm' },
        `To confirm, type ${state.user_principal_name}`),
      confirmInput,
      h('p', { class: 'hint' },
        'This role also lets the holder read every document regardless of department, region or clearance.'));
    confirmField.hidden = true;

    const needsConfirm = () =>
      cap.app_roles.some((r) => r.needs_confirmation && roleBoxes.get(r.value)?.checked
        && !held.get(r.value)?.removable);
    const syncConfirm = () => { confirmField.hidden = !needsConfirm(); };

    const roleRows = cap.app_roles.map((role) => {
      const current = held.get(role.value);
      const viaGroup = current?.via_group ?? null;
      const id = `sec-role-${role.value}`.replace(/[^A-Za-z0-9_-]/g, '_');
      const box = h('input', {
        type: 'checkbox', id, value: role.value,
        checked: Boolean(current), disabled: Boolean(viaGroup),
        onchange: syncConfirm,
      }) as HTMLInputElement;
      roleBoxes.set(role.value, box);
      return h('div', { class: 'check' }, box,
        h('label', { for: id },
          role.display_name,
          // A role that arrives through a group reaches the token exactly as a direct one does, so hiding it
          // would show "not held" for somebody who holds it - and the direct assignment an administrator then
          // made and later removed would not take it away.
          viaGroup ? h('span', { class: 'badge tone-info' }, `via group: ${viaGroup}`) : null,
          current && current.duplicates > 1
            ? h('span', { class: 'badge tone-warning' }, `${current.duplicates} assignments`)
            : null,
          role.description ? h('p', { class: 'hint' }, role.description) : null));
    });

    const sessionsBox = h('input', { type: 'checkbox', id: 'sec-sessions' }) as HTMLInputElement;
    const saveBtn = h('button', { type: 'submit', class: 'btn btn-primary' },
      'Save changes') as HTMLButtonElement;
    const saveStatus = h('div', { class: 'stack' });
    if (!cap.enabled) saveBtn.disabled = true;

    const dirty = () =>
      cap.attributes.some((s) => (selects.get(s.name)?.value ?? NOT_SET) !== NOT_SET)
      || cap.app_roles.some((r) => Boolean(roleBoxes.get(r.value)?.checked) !== Boolean(held.get(r.value)))
      || sessionsBox.checked;
    ctx.setLeaveGuard(() => !dirty() || window.confirm('Discard unsaved access changes?'));

    const collect = () => {
      const attributes: Record<string, string | null> = {};
      for (const spec of cap.attributes) {
        const chosen = selects.get(spec.name)?.value ?? NOT_SET;
        if (chosen === NOT_SET) continue;               // leave it exactly as it is
        attributes[spec.name] = chosen === 'CLEAR' ? null : chosen;
      }
      // Only the direct assignments are ours to set. A group-derived role is reported but never sent, or the
      // desired-state write would try to revoke something it cannot.
      const roles = cap.app_roles
        .filter((r) => roleBoxes.get(r.value)?.checked && !held.get(r.value)?.via_group)
        .map((r) => r.value);
      return { attributes, roles, revoke_sessions: sessionsBox.checked, confirm: confirmInput.value.trim() };
    };

    const save = async () => {
      saveBtn.disabled = true;
      saveStatus.replaceChildren('Saving…');
      try {
        const res = await ctx.api.put<DirectoryWriteResult>(
          `/api/admin/directory/users/${encodeURIComponent(state.user_principal_name)}`,
          collect(),
          { headers: { 'If-Match': etag } },
        );
        etag = res.etag;
        saveStatus.replaceChildren(
          ...(res.applied.length
            ? [h('ul', null, ...res.applied.map((line) => h('li', null, line)))]
            : []),
          ...(res.failed.length
            ? [h('div', { class: 'notice notice-warning' },
                h('p', null, 'Not everything was applied. What is listed above did reach the directory; ' +
                  'the rest did not, and nothing after the failure was attempted.'),
                ...res.failed.map((line) => h('p', null, line)))]
            : []),
          h('p', { class: 'hint' }, res.propagation_note),
        );
        if (res.ok) toast('Access updated.', { kind: 'success' });
        render(res);                                   // re-render from what the server says is now true
      } catch (err) {
        mount(saveStatus, problemBox(err));
      } finally {
        saveBtn.disabled = !cap.enabled;
      }
    };

    mount(panel, h('form', {
        class: 'stack',
        onsubmit: (ev: SubmitEvent) => { ev.preventDefault(); void save(); },
      },
      card('Signed-in identity',
        h('dl', { class: 'kv' },
          h('dt', null, 'Name'), h('dd', null, state.display_name || '—'),
          h('dt', null, 'User principal name'), h('dd', null, state.user_principal_name),
          h('dt', null, 'Email'), h('dd', null, state.mail || '—'),
          h('dt', null, 'Account type'), h('dd', null, state.user_type),
          h('dt', null, 'Sign-in'), h('dd', null, state.account_enabled ? 'Enabled' : 'Disabled'))),
      state.account_enabled
        ? null
        : h('div', { class: 'notice notice-warning' },
            'This account is disabled, so granting it access has no immediate effect.'),
      card('What they can read', ...attributeFields),
      card('What they can do', ...roleRows, confirmField),
      card('After saving',
        h('p', { class: 'hint' }, state.propagation_note),
        h('div', { class: 'check' }, sessionsBox,
          h('label', { for: 'sec-sessions' },
            'Also end their sign-in sessions',
            h('p', { class: 'hint' },
              'Ends the wait, but signs them out of every application in the tenant, not just this one. A token ' +
              'already in their browser stays valid until it expires either way.')))),
      h('div', { class: 'form-actions' }, saveBtn, saveStatus),
    ));
    syncConfirm();
  }
};
