// Chat app for "/" (standalone, signs in with Entra) and "/embed" (inside a host page's iframe).
import { ApiClient, describeError, isAbortError, type TokenProvider } from './api';
import { EmbedTokenBridge, SessionTokenStore, fetchPublicConfig } from './auth';
import { byId, fmtDuration, fmtNum, h, mount, show } from './dom';
import { combineProviders, setUpEntra, type EntraTokenProvider } from './entra';
import { setMarkdown } from './markdown';
import { renderSignIn } from './signin';
import { correlationTag, identityChip, isAdmin, problemBox, toast, toastError } from './ui';
import { createUploadWidget } from './upload';
import { createUploadsList, type UploadsList } from './uploads-list';
import type { ChatResponse, ChatTurn, Citation, FacetsResponse, Me, PublicConfig, Usage } from './types';

const MODE: 'embed' | 'standalone' = document.body.dataset.mode === 'embed' ? 'embed' : 'standalone';
/** History entries ({role, content}) sent with each question — matches the API's answer_history_turns. */
const HISTORY_ENTRIES = 6;
const HISTORY_CHARS = 2_000;
const MAX_QUESTION = 4_000;

// Created at load time so a token posted by the host page before start-up is not missed.
const bridge = MODE === 'embed' ? new EmbedTokenBridge() : null;

// ------------------------------------------------------------------ layout

const appName = h('span', { class: 'brand-name' }, 'RAG-OS');
const identitySlot = h('div', { class: 'identity-slot' });
const filterCount = h('span', { class: 'count', hidden: true });
const btnFilters = h('button', { type: 'button', class: 'btn btn-ghost', 'aria-expanded': 'false', 'aria-controls': 'filters' }, 'Filters', filterCount);
const btnUpload =
  MODE === 'standalone'
    ? h('button', { type: 'button', class: 'btn btn-ghost', 'aria-expanded': 'false', 'aria-controls': 'upload-panel' }, 'Upload')
    : null;
const btnNew = h('button', { type: 'button', class: 'btn btn-ghost', title: 'Start a new conversation' }, 'New chat');
const actions = h('div', { class: 'toolbar-actions' }, btnFilters, btnUpload, btnNew);

const header =
  MODE === 'standalone'
    ? h(
        'header',
        { class: 'topbar' },
        h('div', { class: 'brand' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), appName, h('span', { class: 'brand-sub' }, 'Knowledge assistant')),
        identitySlot,
        actions,
      )
    : h('div', { class: 'embed-toolbar', role: 'toolbar', 'aria-label': 'Assistant tools' }, identitySlot, actions);

const facetForm = h('form', { class: 'facet-form', onsubmit: (ev: SubmitEvent) => ev.preventDefault() });
const filtersDrawer = h(
  'aside',
  { id: 'filters', class: 'drawer', hidden: true, 'aria-labelledby': 'filters-title' },
  h(
    'div',
    { class: 'panel-head' },
    h('h2', { id: 'filters-title' }, 'Filters'),
    h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Close filters', onclick: () => toggleFilters(false) }, '×'),
  ),
  h('p', { class: 'hint' }, 'Narrow the search to matching documents. Your access rights always apply on top.'),
  facetForm,
  h('div', { class: 'panel-foot' }, h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => clearFilters() }, 'Clear all')),
);

const citationList = h('ol', { class: 'citation-list' });
const citationsPanel = h(
  'aside',
  { id: 'citations', class: 'citations', hidden: true, 'aria-labelledby': 'citations-title' },
  h(
    'div',
    { class: 'panel-head' },
    h('h2', { id: 'citations-title' }, 'Sources'),
    h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Close sources', onclick: () => show(citationsPanel, false) }, '×'),
  ),
  citationList,
);

const uploadPanel = h('div', { id: 'upload-panel', class: 'upload-panel', hidden: true });
const messages = h('div', { id: 'messages', class: 'messages', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions', tabindex: '-1' });
const activeFilters = h('div', { class: 'active-filters', hidden: true });
const input = h('textarea', {
  id: 'composer-input',
  rows: 1,
  maxlength: MAX_QUESTION,
  placeholder: 'Ask a question about your documents…',
  'aria-describedby': 'composer-hint',
  disabled: true,
});
const sendBtn = h('button', { type: 'submit', class: 'btn btn-primary', disabled: true }, 'Send');
const composer = h(
  'form',
  { class: 'composer', 'aria-label': 'Ask a question' },
  h('label', { for: 'composer-input', class: 'visually-hidden' }, 'Question'),
  input,
  sendBtn,
  h('span', { id: 'composer-hint', class: 'visually-hidden' }, 'Press Enter to send, Shift+Enter for a new line.'),
);
const conversation = h('section', { class: 'conversation', 'aria-label': 'Conversation' }, uploadPanel, messages, activeFilters, composer);
const chatLayout = h('div', { class: 'chat-layout', hidden: true }, filtersDrawer, conversation, citationsPanel);
const signinSlot = h('div', { class: 'signin-wrap', hidden: true });
const statusSlot = h('div', { class: 'status-wrap', hidden: true });

function renderShell(): void {
  const main = h('main', { id: 'main', class: 'chat-main' }, statusSlot, signinSlot, chatLayout);
  mount(byId('app'), header, main);
  if (MODE === 'embed') show(header, false);
}

// ------------------------------------------------------------------ state

let api: ApiClient;
let history: ChatTurn[] = [];
const filters = new Map<string, Set<string>>();
const facetLabels = new Map<string, { label: string; values: Map<string, string> }>();
const answers = new Map<string, ChatResponse>();
let pending: AbortController | null = null;
let answerSeq = 0;

// ------------------------------------------------------------------ status / sign-in

function showStatus(...content: (Node | string)[]): void {
  mount(statusSlot, h('section', { class: 'card status-card' }, ...content));
  show(statusSlot, true);
  show(chatLayout, false);
  show(signinSlot, false);
}

function showChat(): void {
  show(statusSlot, false);
  show(signinSlot, false);
  show(chatLayout, true);
  show(header, true);
  input.disabled = false;
  sendBtn.disabled = false;
  if (!messages.childElementCount) renderWelcome();
  input.focus();
}

async function standaloneSignIn(config: PublicConfig, store: SessionTokenStore, entra?: EntraTokenProvider):
    Promise<void> {
  show(chatLayout, false);
  show(statusSlot, false);
  show(signinSlot, true);
  mount(identitySlot);
  await renderSignIn(signinSlot, {
    config,
    store,
    heading: `Sign in to ${config.app_name}`,
    ...(entra ? { onEntraSignIn: () => entra.signIn() } : {}),
  });
}

// ------------------------------------------------------------------ identity & facets

// Who is signed in, kept at module scope because the upload panel is built later and needs the roles to
// decide whether its rows can link into the admin console.
let signedInAs: Me | null = null;

async function loadIdentity(onSignOut?: () => void): Promise<void> {
  try {
    const me = await api.get<Me>('/api/me');
    signedInAs = me;
    // The chat page links to the console; the console does not link to itself. Passing the href explicitly
    // keeps that readable at each call site instead of hiding it behind a location check inside the chip.
    mount(identitySlot, identityChip(me, onSignOut, { adminHref: '/admin' }));
  } catch (err) {
    signedInAs = null;
    mount(identitySlot);
    toastError(err, 'Could not load your profile');
  }
}

async function loadFacets(): Promise<void> {
  mount(facetForm, h('p', { class: 'muted' }, 'Loading filters…'));
  try {
    const res = await api.get<FacetsResponse>('/api/facets');
    renderFacets(res);
  } catch (err) {
    mount(facetForm, problemBox(err));
  }
}

function renderFacets(res: FacetsResponse): void {
  facetLabels.clear();
  const entries = Object.entries(res.facets ?? {});
  if (!entries.length) {
    mount(facetForm, h('p', { class: 'muted' }, 'No filters available.'));
    return;
  }
  mount(
    facetForm,
    entries.map(([name, facet]) => {
      const label = facet.label || name;
      facetLabels.set(name, { label, values: new Map(facet.values.map((v) => [v.id, v.label || v.id])) });
      const selected = filters.get(name) ?? new Set<string>();
      return h(
        'fieldset',
        { class: 'facet' },
        h('legend', null, label),
        facet.values.length
          ? facet.values.map((v) => {
              const id = `f-${name}-${v.id}`.replace(/[^A-Za-z0-9_-]/g, '_');
              const cb = h('input', { type: 'checkbox', id, value: v.id, checked: selected.has(v.id) });
              cb.addEventListener('change', () => setFilter(name, v.id, cb.checked));
              return h(
                'div',
                { class: 'check' },
                cb,
                h('label', { for: id }, v.label || v.id, typeof v.count === 'number' ? h('span', { class: 'facet-count' }, fmtNum(v.count)) : null),
              );
            })
          : h('p', { class: 'muted' }, 'No values'),
      );
    }),
  );
}

function setFilter(facet: string, value: string, on: boolean): void {
  const set = filters.get(facet) ?? new Set<string>();
  if (on) set.add(value);
  else set.delete(value);
  if (set.size) filters.set(facet, set);
  else filters.delete(facet);
  renderActiveFilters();
}

function clearFilters(): void {
  filters.clear();
  facetForm.querySelectorAll<HTMLInputElement>('input[type=checkbox]').forEach((cb) => (cb.checked = false));
  renderActiveFilters();
}

function filtersPayload(): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [k, v] of filters) if (v.size) out[k] = [...v];
  return out;
}

function renderActiveFilters(): void {
  const chips: HTMLElement[] = [];
  for (const [facet, values] of filters) {
    const meta = facetLabels.get(facet);
    for (const value of values) {
      chips.push(
        h(
          'button',
          {
            type: 'button',
            class: 'chip',
            title: 'Remove filter',
            onclick: () => {
              setFilter(facet, value, false);
              const cb = facetForm.querySelector<HTMLInputElement>(`#${CSS.escape(`f-${facet}-${value}`.replace(/[^A-Za-z0-9_-]/g, '_'))}`);
              if (cb) cb.checked = false;
            },
          },
          `${meta?.label ?? facet}: ${meta?.values.get(value) ?? value}`,
          h('span', { 'aria-hidden': 'true' }, ' ×'),
        ),
      );
    }
  }
  const n = chips.length;
  filterCount.textContent = String(n);
  show(filterCount, n > 0);
  mount(activeFilters, n ? h('span', { class: 'muted' }, 'Filtered by') : null, chips);
  show(activeFilters, n > 0);
}

function toggleFilters(open?: boolean): void {
  const next = open ?? filtersDrawer.hidden !== false;
  show(filtersDrawer, next);
  btnFilters.setAttribute('aria-expanded', String(next));
  if (next) facetForm.querySelector<HTMLInputElement>('input')?.focus();
}

// ------------------------------------------------------------------ messages

function renderWelcome(): void {
  mount(
    messages,
    h(
      'div',
      { class: 'welcome' },
      h('h1', null, `Ask ${appName.textContent || 'the assistant'}`),
      h('p', null, 'Answers are grounded in your organisation’s documents and cite their sources. You only see content your access allows.'),
    ),
  );
}

function scrollToEnd(): void {
  messages.scrollTop = messages.scrollHeight;
}

function addUserMessage(text: string): void {
  messages.querySelector('.welcome')?.remove();
  messages.appendChild(h('div', { class: 'msg msg-user' }, h('div', { class: 'bubble' }, text)));
  scrollToEnd();
}

function addPlaceholder(): HTMLElement {
  const el = h(
    'div',
    { class: 'msg msg-assistant pending', 'aria-busy': 'true' },
    h('div', { class: 'bubble' }, h('span', { class: 'typing', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), h('span', { class: 'muted' }, 'Searching your documents…')),
  );
  messages.appendChild(el);
  scrollToEnd();
  return el;
}

function humanize(s: string | null | undefined): string {
  if (!s) return '';
  return s.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

function totalMs(t: Record<string, number> | undefined): number | undefined {
  if (!t) return undefined;
  if (typeof t.total === 'number') return t.total;
  const values = Object.values(t).filter((v) => typeof v === 'number');
  return values.length ? values.reduce((a, b) => a + b, 0) : undefined;
}

function purposeLine(v: unknown): string {
  if (v && typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>)
      .filter(([, n]) => typeof n === 'number' || typeof n === 'string')
      .map(([k, n]) => `${k} ${typeof n === 'number' ? fmtNum(n) : n}`)
      .join(' · ');
  }
  return String(v);
}

function usageFooter(res: ChatResponse): HTMLElement {
  const u: Usage = res.usage ?? {};
  const ms = totalMs(res.timings_ms);
  const item = (label: string, value: string, title?: string) => h('span', { class: 'usage-item', title: title ?? label }, h('span', { class: 'usage-label' }, label), ' ', value);
  const breakdown: HTMLElement[] = [];
  for (const [k, v] of Object.entries(u.by_purpose ?? {})) breakdown.push(h('dt', null, humanize(k)), h('dd', null, purposeLine(v)));
  for (const [k, v] of Object.entries(res.timings_ms ?? {})) breakdown.push(h('dt', null, `${humanize(k)} time`), h('dd', null, fmtDuration(v)));
  return h(
    'footer',
    { class: 'usage', 'aria-label': 'Token usage' },
    item('in', fmtNum(u.input), 'Input tokens'),
    item('out', fmtNum(u.output), 'Output tokens'),
    item('cache', `${fmtNum(u.cache_read ?? 0)} / ${fmtNum(u.cache_write ?? 0)}`, 'Prompt-cache tokens read / written'),
    item('embed', fmtNum(u.embedding), 'Embedding tokens'),
    res.model ? item('model', res.provider ? `${res.model} (${res.provider})` : res.model, 'Answer model') : null,
    ms !== undefined ? item('time', fmtDuration(ms), 'Total server time') : null,
    breakdown.length || u.calls
      ? h('details', { class: 'usage-details' }, h('summary', null, 'Details'), h('dl', null, u.calls ? [h('dt', null, 'LLM calls'), h('dd', null, fmtNum(u.calls))] : null, breakdown))
      : null,
    res.correlation_id ? correlationTag(res.correlation_id) : null,
  );
}

function sourceChips(answerId: string, citations: Citation[]): HTMLElement | null {
  if (!citations.length) return null;
  return h(
    'div',
    { class: 'sources' },
    h('span', { class: 'muted' }, 'Sources'),
    citations.map((c) =>
      h(
        'button',
        { type: 'button', class: 'source-chip', 'data-answer': answerId, 'data-cite': String(c.index), title: c.path ?? '' },
        h('span', { class: 'cite-index' }, String(c.index)),
        c.title || c.path || c.doc_id,
      ),
    ),
  );
}

function renderAnswer(placeholder: HTMLElement, res: ChatResponse): void {
  const answerId = `a${++answerSeq}`;
  answers.set(answerId, res);
  const citations = Array.isArray(res.citations) ? res.citations : [];
  const body = h('div', { class: 'md' });
  setMarkdown(body, res.answer || '', { citations: new Set(citations.map((c) => c.index)) });
  const el = h(
    'article',
    { class: `msg msg-assistant${res.refused ? ' refused' : ''}`, 'data-answer': answerId, 'aria-label': res.refused ? 'Assistant (no answer)' : 'Assistant answer' },
    h(
      'div',
      { class: 'bubble' },
      res.refused
        ? h(
            'div',
            { class: 'refusal' },
            h('span', { class: 'refusal-icon', 'aria-hidden': 'true' }, '⦸'),
            h('div', null, h('strong', null, 'No grounded answer'), res.refusal_reason ? h('p', null, humanize(res.refusal_reason)) : null),
          )
        : null,
      body,
      res.refused ? null : sourceChips(answerId, citations),
    ),
    usageFooter(res),
  );
  placeholder.replaceWith(el);
  scrollToEnd();
}

function renderFailure(placeholder: HTMLElement, question: string, err: unknown): void {
  const d = describeError(err);
  const retry = h('button', { type: 'button', class: 'btn btn-sm' }, 'Retry');
  const el = h(
    'div',
    { class: 'msg msg-assistant failed', role: 'note' },
    h('div', { class: 'bubble' }, h('strong', null, 'Not answered'), h('p', null, d.detail ? `${d.title} — ${d.detail}` : d.title), d.correlationId ? correlationTag(d.correlationId) : null, h('div', null, retry)),
  );
  retry.addEventListener('click', () => {
    el.previousElementSibling?.remove(); // the user bubble is re-added by ask()
    el.remove();
    void ask(question);
  });
  placeholder.replaceWith(el);
  scrollToEnd();
}

// ------------------------------------------------------------------ citations panel

function openCitations(answerId: string, index?: number): void {
  const res = answers.get(answerId);
  if (!res) return;
  const cites = res.citations ?? [];
  mount(
    citationList,
    cites.map((c) =>
      h(
        'li',
        { class: 'citation', id: `cite-${answerId}-${c.index}`, tabindex: '-1' },
        h('span', { class: 'cite-index', 'aria-hidden': 'true' }, String(c.index)),
        h(
          'div',
          { class: 'citation-body' },
          h('strong', { class: 'citation-title' }, c.title || c.path || c.doc_id),
          c.path ? h('div', { class: 'citation-path mono' }, c.path) : null,
          h(
            'div',
            { class: 'citation-meta' },
            [c.page != null ? `Page ${c.page}` : null, c.heading || null, typeof c.score === 'number' ? `score ${c.score.toFixed(2)}` : null].filter(Boolean).join(' · '),
          ),
          c.snippet ? h('blockquote', { class: 'snippet' }, c.snippet) : null,
        ),
      ),
    ),
  );
  if (!cites.length) mount(citationList, h('li', { class: 'muted' }, 'This answer has no sources.'));
  show(citationsPanel, true);
  const target = index !== undefined ? document.getElementById(`cite-${answerId}-${index}`) : citationList.firstElementChild;
  if (target instanceof HTMLElement) {
    citationList.querySelectorAll('.active').forEach((n) => n.classList.remove('active'));
    target.classList.add('active');
    target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    target.focus({ preventScroll: true });
  }
}

messages.addEventListener('click', (ev) => {
  const btn = (ev.target as Element | null)?.closest<HTMLElement>('.cite, .source-chip');
  if (!btn) return;
  const answerId = btn.dataset.answer ?? btn.closest<HTMLElement>('[data-answer]')?.dataset.answer;
  const index = Number.parseInt(btn.dataset.cite ?? '', 10);
  if (answerId) openCitations(answerId, Number.isInteger(index) ? index : undefined);
});

// ------------------------------------------------------------------ asking

function setPending(ctrl: AbortController | null): void {
  pending = ctrl;
  input.disabled = false;
  sendBtn.textContent = ctrl ? 'Stop' : 'Send';
  sendBtn.classList.toggle('btn-primary', !ctrl);
  messages.setAttribute('aria-busy', String(!!ctrl));
}

async function ask(question: string): Promise<void> {
  const q = question.trim().slice(0, MAX_QUESTION);
  if (!q || pending) return;
  addUserMessage(q);
  const placeholder = addPlaceholder();
  const ctrl = new AbortController();
  setPending(ctrl);
  const payload = {
    question: q,
    history: history.slice(-HISTORY_ENTRIES).map((t) => ({ role: t.role, content: t.content.slice(0, HISTORY_CHARS) })),
    filters: filtersPayload(),
  };
  try {
    const res = await api.post<ChatResponse>('/api/chat', payload, { signal: ctrl.signal });
    renderAnswer(placeholder, res);
    history.push({ role: 'user', content: q }, { role: 'assistant', content: res.answer || '' });
    if (history.length > HISTORY_ENTRIES * 2) history = history.slice(-HISTORY_ENTRIES * 2);
  } catch (err) {
    if (isAbortError(err)) {
      placeholder.replaceWith(h('div', { class: 'msg msg-assistant failed' }, h('div', { class: 'bubble muted' }, 'Stopped.')));
    } else {
      renderFailure(placeholder, q, err);
      toastError(err, 'Could not get an answer');
    }
  } finally {
    setPending(null);
    input.focus();
  }
}

function autoGrow(): void {
  input.style.height = 'auto'; // CSSOM (allowed by CSP), not an inline style attribute
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

composer.addEventListener('submit', (ev) => {
  ev.preventDefault();
  if (pending) {
    pending.abort();
    return;
  }
  const q = input.value;
  if (!q.trim()) return;
  input.value = '';
  autoGrow();
  void ask(q);
});
input.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    composer.requestSubmit();
  }
});
input.addEventListener('input', autoGrow);

btnFilters.addEventListener('click', () => toggleFilters());
btnNew.addEventListener('click', () => {
  pending?.abort();
  history = [];
  answers.clear();
  show(citationsPanel, false);
  renderWelcome();
  input.focus();
});
let recent: UploadsList | null = null;

btnUpload?.addEventListener('click', () => {
  const open = uploadPanel.hidden !== false;
  if (open && !uploadPanel.childElementCount) {
    // The live badges from the widget only exist while this panel is open. The list below is what survives a
    // reload - and for anyone without the admin role it is the only way back to a document they uploaded.
    recent = createUploadsList(api, {
      documentHref: isAdmin(signedInAs) ? (docId) => `/admin#/documents/${encodeURIComponent(docId)}` : undefined,
    });
    mount(
      uploadPanel,
      h('div', { class: 'panel-head' }, h('h2', null, 'Upload documents'), h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Close upload', onclick: () => btnUpload.click() }, '×')),
      createUploadWidget(api, {
        facetPickers: true,
        onFinished: (rec) => {
          if (rec.status === 'INDEXED') toast(`“${rec.title || rec.path}” is indexed and searchable.`, { kind: 'success' });
          recent?.reload();
          // Filter values are an aggregation over the index, so a document tagged with a Department nobody had
          // used yet adds a value that is simply absent until this runs. Without it the tag looks like it did
          // not take - which is the confusion this whole feature exists to end.
          void loadFacets();
        },
      }),
      h('h3', { class: 'small muted' }, 'Recent documents'),
      recent.el,
    );
  } else if (open) {
    recent?.reload();
  }
  show(uploadPanel, open);
  btnUpload.setAttribute('aria-expanded', String(open));
});
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape') return;
  if (!citationsPanel.hidden) show(citationsPanel, false);
  else if (!filtersDrawer.hidden) toggleFilters(false);
});

// ------------------------------------------------------------------ start-up

async function startStandalone(config: PublicConfig): Promise<void> {
  const store = new SessionTokenStore('rag-os:token:chat');
  // With Entra configured the access token comes from MSAL; the store still backs the paste-a-token path.
  const entra = await setUpEntra(config);
  api = new ApiClient(entra ? combineProviders(entra, store) : store);
  const signOut = () => {
    store.clear();
    pending?.abort();
    if (entra?.account) {
      void entra.signOut();
      return;
    }
    void signInLoop();
  };
  const signInLoop = async () => {
    await standaloneSignIn(config, store, entra ?? undefined);
    showChat();
    void loadIdentity(signOut);
    void loadFacets();
  };
  store.onExpired(() => {
    toast('Your session has expired. Please sign in again.', { kind: 'info' });
    void signInLoop();
  });
  entra?.onExpired(() => {
    toast('Your session has expired. Please sign in again.', { kind: 'info' });
    void signInLoop();
  });
  const restored = entra ? await entra.restore() : false;
  if (!restored && !store.getToken()) {
    await signInLoop();
    return;
  }
  showChat();
  void loadIdentity(signOut);
  void loadFacets();
}

async function startEmbed(config: PublicConfig, b: EmbedTokenBridge): Promise<void> {
  b.configure(config.embed_origins);
  api = new ApiClient(b as TokenProvider);
  if (!b.isFramed) {
    showStatus(
      h('h1', null, 'Embedded assistant'),
      h('p', null, 'This page is designed to run inside a host page that supplies a token.'),
      h('p', null, h('a', { href: '/' }, 'Open the full assistant')),
    );
    return;
  }
  let started = false;
  const start = () => {
    if (started) return;
    started = true;
    showChat();
    void loadIdentity();
    void loadFacets();
  };
  b.onToken(start);
  if (b.hasToken()) {
    start();
    return;
  }
  showStatus(h('p', { class: 'muted' }, h('span', { class: 'typing', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), ' Connecting…'));
  b.requestToken();
  if (!(await b.waitForToken(15_000))) {
    const retry = h('button', { type: 'button', class: 'btn' }, 'Try again');
    retry.addEventListener('click', () => b.requestToken());
    showStatus(
      h('h1', null, 'Waiting for sign-in'),
      h('p', null, 'The assistant has not received a token from the page that embeds it. Refresh the page, or open it directly.'),
      h('div', null, retry),
    );
  }
}

async function main(): Promise<void> {
  renderShell();
  let config: PublicConfig;
  try {
    config = await fetchPublicConfig();
  } catch (err) {
    showStatus(h('h1', null, 'The assistant is unavailable'), problemBox(err));
    return;
  }
  appName.textContent = config.app_name;
  document.title = MODE === 'embed' ? config.app_name : `${config.app_name} · Knowledge assistant`;
  if (bridge) await startEmbed(config, bridge);
  else await startStandalone(config);
}

void main();
