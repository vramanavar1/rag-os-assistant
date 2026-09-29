// Account Information: who you are signed in as, what your attributes mean, and what they let you read.
//
// Everything here is rendered from GET /api/me/account, which assembles it server-side. The browser
// deliberately does not interpret the access policy itself - two implementations of an access rule is one too
// many, and the copy in a bundle anybody can read would be the wrong place to find a disagreement.
import { type ApiClient } from './api';
import { h } from './dom';
import { openDialog } from './dialog';
import { problemBox } from './ui';
import type { AccountInfo, AccountAttribute } from './types';

function section(title: string, ...children: Parameters<typeof h>[2][]): HTMLElement {
  return h('section', { class: 'account-section' }, h('h3', null, title), ...children);
}

/** An attribute the policy matches on: what you hold, where it came from, and what it means. */
function attributeBlock(attr: AccountAttribute): HTMLElement {
  const values = attr.values.length
    ? attr.values.map((v) => h('span', { class: 'badge tone-info' }, v))
    : [h('span', { class: 'muted' }, attr.required ? 'Not set — required' : 'Not set')];
  return h(
    'div',
    { class: 'account-attr' },
    h(
      'div',
      { class: 'account-attr-head' },
      h('strong', null, attr.label),
      ...values,
      // The claim name is the answer to "why is my department wrong?" almost every time.
      attr.claim ? h('span', { class: 'muted small' }, `from ${attr.claim}`) : null,
    ),
    attr.description ? h('p', { class: 'muted small' }, attr.description) : null,
    h('p', { class: 'small' }, attr.meaning),
  );
}

/** The full ladder, with the caller's rung marked - the point being that "2" on its own says nothing. */
function ladder(attr: AccountAttribute): HTMLElement {
  return h(
    'ul',
    { class: 'level-ladder' },
    attr.levels.map((lvl) =>
      h(
        'li',
        { class: lvl.value === attr.level ? 'level is-current' : 'level', 'aria-current': lvl.value === attr.level ? 'true' : undefined },
        h('span', { class: 'level-value' }, String(lvl.value)),
        h('strong', null, lvl.label),
        lvl.value === attr.level ? h('span', { class: 'badge tone-good' }, 'yours') : null,
        lvl.description ? h('p', { class: 'muted small' }, lvl.description) : null,
      ),
    ),
  );
}

function appRoles(info: AccountInfo): HTMLElement {
  const held = info.app_roles.filter((r) => r.held);
  return h(
    'div',
    null,
    h('p', { class: 'muted small' },
      'Permissions come from the application roles defined on the RAG-OS app registration. An administrator ' +
      'assigns them in Entra under Enterprise applications → Users and groups.'),
    held.length ? null : h('p', { class: 'muted' }, 'You hold none of these, so you have read-only access.'),
    h(
      'ul',
      { class: 'role-list' },
      info.app_roles.map((r) =>
        h(
          'li',
          { class: r.held ? 'role is-held' : 'role' },
          h(
            'div',
            { class: 'account-attr-head' },
            h('strong', null, r.display_name),
            h('code', null, r.value),
            r.held ? h('span', { class: 'badge tone-good' }, 'assigned to you') : null,
          ),
          r.description ? h('p', { class: 'muted small' }, r.description) : null,
          r.held && r.grants.length ? h('p', { class: 'small' }, `In RAG-OS this grants: ${r.grants.join(', ')}.`) : null,
        ),
      ),
    ),
    // A value in the token that matches nothing is a misspelled assignment, and used to be silently dropped -
    // indistinguishable from never having been assigned at all.
    info.unrecognised_roles.length
      ? h('p', { class: 'notice notice-warning small' },
          `Your token also carries ${info.unrecognised_roles.join(', ')}, which match no application role here ` +
          'and grant nothing. Usually a typo in the assignment, or a role belonging to another application.')
      : null,
  );
}

export function openAccountDialog(api: ApiClient, onClose?: () => void): void {
  const dialog = openDialog('Account Information', onClose);
  dialog.setBody(h('p', { class: 'muted' }, 'Loading…'));
  void (async () => {
    try {
      const info = await api.get<AccountInfo>('/api/me/account');
      const clearance = info.attributes.find((a) => a.levels.length > 0);
      dialog.setBody(
        section(
          'Signed in as',
          h('dl', { class: 'kv' },
            h('dt', null, 'Name'), h('dd', null, info.display_name || info.subject),
            h('dt', null, 'Subject'), h('dd', null, h('code', null, info.subject)),
            h('dt', null, 'Identity provider'), h('dd', null, info.issuer_kind)),
        ),
        section('What you can read', h('p', null, info.summary)),
        section('Your attributes', info.attributes.map(attributeBlock)),
        clearance ? section(`${clearance.label} levels`, ladder(clearance)) : null,
        section('Application roles', appRoles(info)),
      );
    } catch (err) {
      dialog.setBody(problemBox(err));
    }
  })();
}

export function mountAccountDialog(api: ApiClient): () => void {
  return () => openAccountDialog(api);
}
