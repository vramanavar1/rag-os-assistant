// /dev/embed-host — a mock embedding page (served only when DEV_EMBED_HOST_ENABLED=true).
// It embeds the assistant exactly like a real host page would: a <script src="/embed/loader.js">
// tag whose data-token-endpoint returns a bearer token. Here that endpoint is the
// API's dev token endpoint: GET /api/dev/token?principal_id=<id> -> {token, expires_in}.
import { byId, h, mount } from './dom';
import { fetchDevPrincipals, principalLabel } from './signin';
import { problemBox } from './ui';
import type { Principal } from './types';

const HEIGHT = '640';

function attrValues(p: Principal): string[] {
  return Object.values(p.attributes ?? {})
    .flatMap((v) => (Array.isArray(v) ? v : [v]))
    .map((v) => String(v).toLowerCase());
}

/** Pick the principal whose attribute values contain all `wants` (e.g. ["hr", "eu"]). */
function pick(principals: Principal[], wants: string[], exclude?: string): Principal | undefined {
  return principals.find((p) => p.id !== exclude && wants.every((w) => attrValues(p).includes(w)));
}

function tokenEndpoint(principalId: string): string {
  return `/api/dev/token?principal_id=${encodeURIComponent(principalId)}`;
}

/** Insert the loader script tag — the same snippet a host page would contain. */
function embed(target: HTMLElement, principalId: string): void {
  target.replaceChildren(h('p', { class: 'muted pane-placeholder' }, 'Loading assistant…'));
  const s = document.createElement('script');
  s.src = '/embed/loader.js';
  s.dataset.target = `#${target.id}`;
  s.dataset.tokenEndpoint = tokenEndpoint(principalId);
  s.dataset.height = HEIGHT;
  s.addEventListener('load', () => s.remove());
  s.addEventListener('error', () => {
    s.remove();
    mount(target, problemBox(new Error('Could not load /embed/loader.js')));
  });
  document.body.appendChild(s);
}

function snippet(principalId: string): string {
  return [
    '<div id="assistant"></div>',
    `<script src="${window.location.origin}/embed/loader.js"`,
    '        data-target="#assistant"',
    `        data-token-endpoint="${tokenEndpoint(principalId || '<id>')}"`,
    `        data-height="${HEIGHT}"></script>`,
  ].join('\n');
}

function pane(id: string, label: string, principals: Principal[], initial: string | undefined, onChange?: (pid: string) => void): HTMLElement {
  const selectId = `${id}-principal`;
  const frame = h('div', { id: `${id}-frame`, class: 'pane-frame' });
  const select = h(
    'select',
    { id: selectId },
    principals.map((p) => h('option', { value: p.id }, principalLabel(p))),
  );
  if (initial) select.value = initial;
  const apply = () => {
    if (!select.value) return;
    embed(frame, select.value);
    onChange?.(select.value);
  };
  select.addEventListener('change', apply);
  queueMicrotask(apply);
  return h('section', { class: 'pane card', 'aria-label': label }, h('div', { class: 'field' }, h('label', { for: selectId }, label), select), frame);
}

async function main(): Promise<void> {
  const root = byId('app');
  mount(root, h('p', { class: 'muted' }, 'Loading principals…'));
  let principals: Principal[];
  try {
    principals = await fetchDevPrincipals();
  } catch (err) {
    mount(root, problemBox(err));
    return;
  }
  if (!principals.length) {
    mount(root, h('p', { class: 'problem' }, 'The API returned no dev principals. Is dev auth enabled?'));
    return;
  }

  const code = h('pre', { class: 'code-block' });
  const panes = h('div', { class: 'panes' });
  const single = h('input', { type: 'radio', name: 'mode', id: 'mode-single', value: 'single', checked: true });
  const compare = h('input', { type: 'radio', name: 'mode', id: 'mode-compare', value: 'compare' });

  const render = () => {
    if (compare.checked) {
      const a = pick(principals, ['hr', 'eu']) ?? principals[0];
      const b = pick(principals, ['sales', 'us'], a?.id) ?? principals.find((p) => p.id !== a?.id) ?? principals[0];
      panes.className = 'panes panes-2';
      mount(panes, pane('pane-a', 'User A', principals, a?.id), pane('pane-b', 'User B', principals, b?.id));
      code.textContent = snippet('<id>');
    } else {
      panes.className = 'panes';
      mount(panes, pane('pane-main', 'Signed-in user', principals, principals[0]?.id, (pid) => (code.textContent = snippet(pid))));
    }
  };
  single.addEventListener('change', render);
  compare.addEventListener('change', render);

  mount(
    root,
    h(
      'section',
      { class: 'card' },
      h('p', null, 'This mock host page embeds the assistant with ', h('code', null, '/embed/loader.js'), '. Its token endpoint is the API’s dev token endpoint, so each pane signs in as the selected test principal. In production the host page supplies an Entra access token instead. Ask the same question in both panes to see access filtering in action.'),
      h(
        'fieldset',
        { class: 'mode' },
        h('legend', null, 'Layout'),
        h('div', { class: 'check inline' }, single, h('label', { for: 'mode-single' }, 'Single user')),
        h('div', { class: 'check inline' }, compare, h('label', { for: 'mode-compare' }, 'Compare HR/EU vs Sales/US')),
      ),
    ),
    panes,
    h('details', { class: 'card' }, h('summary', null, 'Embed snippet'), code),
  );
  render();
}

void main();
