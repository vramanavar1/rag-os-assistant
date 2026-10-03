// Query traces (#/traces): live query health, every question asked, and one question stage by stage
// (#/traces/<trace id or correlation id>). Expectations, the stated right outcome for a question, are on
// #/traces?tab=expectations.
//
// The detail view answers "why did this person get no answer?" from evidence: the pipeline picture puts the
// failing stage in red, the verdict says whether a no-answer was CORRECT, and the near-miss table shows which
// access attribute withheld which document. Administrators only - it shows other people's questions.
import { isAbortError } from '../api';
import { fmtAgo, fmtDate, fmtDuration, fmtNum, h, mount, type Child } from '../dom';
import { correlationTag, problemBox, toast, toastError } from '../ui';
import type {
  AttributeCheck,
  Expectation,
  NearMissDoc,
  Page,
  QueryTrace,
  StageStatus,
  TraceMeta,
  TraceRow,
  TraceStage,
  TraceSummary,
} from '../types';
import { autoRefresh, code, dataTable, kv, openDocumentDialog, pageHeader, type View, type ViewContext } from './common';

const STAGE_TONE: Record<StageStatus, string> = { ok: 'good', warn: 'warning', fail: 'critical', skipped: 'muted' };
const STAGE_ICON: Record<StageStatus, string> = { ok: '✓', warn: '!', fail: '✕', skipped: '·' };
const CORRECT = new Set(['answered', 'not_in_corpus', 'withheld_by_policy']);

let metaCache: Promise<TraceMeta> | null = null;
function getMeta(ctx: ViewContext): Promise<TraceMeta> {
  metaCache ??= ctx.api.get<TraceMeta>('/api/admin/traces/meta').catch((err: unknown) => {
    metaCache = null;
    throw err;
  });
  return metaCache;
}

function verdictTone(verdict: string): string {
  if (CORRECT.has(verdict)) return 'good';
  if (verdict === 'unverified') return 'warning';
  return 'critical';
}

function verdictBadge(verdict: string, label?: string): HTMLElement {
  const tone = verdictTone(verdict);
  return h(
    'span',
    { class: `badge tone-${tone}`, title: verdict },
    h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, tone === 'good' ? '✓' : tone === 'warning' ? '?' : '✕'),
    label ?? verdict.replace(/_/g, ' '),
  );
}

function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (Array.isArray(v)) return v.length ? v.map(String).join(', ') : '—';
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'number') return Number.isInteger(v) ? fmtNum(v) : String(Math.round(v * 10000) / 10000);
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function listHash(tab: string, q: URLSearchParams): string {
  const next = new URLSearchParams(q);
  if (tab === 'expectations') next.set('tab', 'expectations');
  else next.delete('tab');
  const s = next.toString();
  return `#/traces${s ? `?${s}` : ''}`;
}

// ================================================================== list

export const tracesView: View = async (ctx: ViewContext) => {
  if (ctx.params[0]) return traceDetail(ctx, decodeURIComponent(ctx.params[0]));
  const tab = ctx.query.get('tab') === 'expectations' ? 'expectations' : 'questions';
  const tabs = h(
    'div',
    { class: 'tabs', role: 'tablist' },
    h('a', { class: 'tab', role: 'tab', href: listHash('questions', ctx.query), 'aria-selected': String(tab === 'questions') }, 'Questions'),
    h('a', { class: 'tab', role: 'tab', href: listHash('expectations', ctx.query), 'aria-selected': String(tab === 'expectations') }, 'Expectations'),
  );
  const health = h('section', { class: 'health', 'aria-label': 'Query health', 'aria-live': 'polite' }, h('p', { class: 'muted' }, 'Loading health…'));
  const minutes = Number(ctx.query.get('minutes') ?? '60') || 60;
  const body = h('div');
  const refreshHealth = async () => {
    try {
      const s = await ctx.api.get<TraceSummary>('/api/admin/traces/summary', { minutes: Math.max(5, minutes) }, { signal: ctx.signal });
      if (!ctx.signal.aborted) mount(health, healthStrip(s));
    } catch (err) {
      if (!isAbortError(err)) mount(health, problemBox(err));
    }
  };
  mount(
    ctx.root,
    pageHeader('Query traces', autoRefresh(ctx, refreshHealth, 15_000), h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => void refreshHealth() }, 'Refresh')),
    h(
      'p',
      { class: 'hint' },
      'What happened to each question, stage by stage. Problems are verdicts, not refusals: a correct "not in the documents" or "withheld by access policy" is the system working.',
    ),
    health,
    tabs,
    body,
  );
  await Promise.all([refreshHealth(), tab === 'expectations' ? expectationsTab(ctx, body) : questionsTab(ctx, body)]);
};

function healthStrip(s: TraceSummary): HTMLElement {
  const tiles = s.metrics.map((m) => {
    const value =
      m.value === null
        ? '—'
        : m.key.endsWith('_rate')
          ? `${Math.round(m.value * 1000) / 10}%`
          : m.key === 'p95_ms'
            ? fmtDuration(m.value)
            : fmtNum(m.value);
    const tone = m.status === 'fail' ? 'critical' : m.status === 'warn' ? 'warning' : 'good';
    const hint = `${m.detail}${m.key.endsWith('_rate') ? ` · alert above ${Math.round(m.threshold * 1000) / 10}%` : m.key === 'p95_ms' ? ` · alert above ${fmtDuration(m.threshold)}` : ''}`;
    return h(
      'div',
      { class: `tile tone-${tone}${m.status === 'ok' ? ' zero' : ''}`, role: 'listitem' },
      h('span', { class: 'tile-label' }, h('span', { class: 'tile-icon', 'aria-hidden': 'true' }, m.status === 'ok' ? '✓' : m.status === 'warn' ? '!' : '✕'), m.label),
      h('span', { class: 'tile-value' }, value),
      h('span', { class: 'tile-hint' }, hint),
    );
  });
  const verdicts = s.by_verdict.filter((v) => v.count > 0);
  return h(
    'div',
    { class: 'stack' },
    h('div', { class: 'tiles', role: 'list' }, tiles),
    h(
      'p',
      { class: 'small muted' },
      `Last ${s.minutes} minutes: ${fmtNum(s.total)} question${s.total === 1 ? '' : 's'}${s.replays ? ` (+${fmtNum(s.replays)} replays)` : ''}. `,
      verdicts.length
        ? verdicts.map((v) => [h('a', { href: `#/traces?verdict=${encodeURIComponent(v.verdict)}&minutes=${s.minutes}`, class: 'verdict-count' }, verdictBadge(v.verdict, `${v.label}: ${fmtNum(v.count)}`)), ' '])
        : null,
    ),
    s.repeated_refusals.length
      ? h(
          'div',
          { class: 'notice notice-warning' },
          h('strong', null, 'Repeatedly refused: '),
          s.repeated_refusals.map((u, i) => [
            i ? ', ' : '',
            h('a', { href: `#/traces/${encodeURIComponent(u.last_trace_id)}` }, u.display_name || u.subject),
            ` (${u.refusals} refusals${u.problems ? `, ${u.problems} problem${u.problems === 1 ? '' : 's'}` : ''})`,
          ]),
        )
      : null,
    s.failing_expectations.length
      ? h(
          'div',
          { class: 'problem' },
          h('strong', null, 'Failing expectations: '),
          s.failing_expectations.map((e, i) => [
            i ? ' · ' : '',
            e.last_trace_id ? h('a', { href: `#/traces/${encodeURIComponent(e.last_trace_id)}` }, e.question) : e.question,
            ` — ${e.detail}`,
          ]),
        )
      : null,
  );
}

async function questionsTab(ctx: ViewContext, root: HTMLElement): Promise<void> {
  const q = ctx.query;
  const meta = await getMeta(ctx).catch(() => null);
  if (ctx.signal.aborted) return;
  const verdictSel = h(
    'select',
    { id: 'tr-verdict', name: 'verdict' },
    h('option', { value: '' }, 'Any verdict'),
    h('option', { value: 'problems' }, 'Problems only'),
    (meta?.verdicts ?? []).map((v) => h('option', { value: v.verdict }, v.label)),
  );
  verdictSel.value = q.get('problems') === 'true' ? 'problems' : (q.get('verdict') ?? '');
  const minutesSel = h(
    'select',
    { id: 'tr-minutes', name: 'minutes' },
    [
      ['', 'Any time'],
      ['15', 'Last 15 minutes'],
      ['60', 'Last hour'],
      ['1440', 'Last 24 hours'],
      ['10080', 'Last 7 days'],
    ].map(([v, l]) => h('option', { value: v }, l)),
  );
  minutesSel.value = q.get('minutes') ?? '';
  const userInput = h('input', { id: 'tr-user', type: 'search', value: q.get('user') ?? '', placeholder: 'Name or subject…', maxlength: 200 });
  const textInput = h('input', { id: 'tr-q', type: 'search', value: q.get('q') ?? '', placeholder: 'Question contains…', maxlength: 200 });
  const form = h(
    'form',
    {
      class: 'filters',
      onsubmit: (ev: SubmitEvent) => {
        ev.preventDefault();
        const next = new URLSearchParams();
        if (verdictSel.value === 'problems') next.set('problems', 'true');
        else if (verdictSel.value) next.set('verdict', verdictSel.value);
        if (minutesSel.value) next.set('minutes', minutesSel.value);
        if (userInput.value.trim()) next.set('user', userInput.value.trim());
        if (textInput.value.trim()) next.set('q', textInput.value.trim());
        ctx.navigate(listHash('questions', next));
      },
    },
    h('div', { class: 'field' }, h('label', { for: 'tr-verdict' }, 'Verdict'), verdictSel),
    h('div', { class: 'field' }, h('label', { for: 'tr-minutes' }, 'When'), minutesSel),
    h('div', { class: 'field' }, h('label', { for: 'tr-user' }, 'Person'), userInput),
    h('div', { class: 'field grow' }, h('label', { for: 'tr-q' }, 'Question'), textInput),
    h('div', { class: 'field actions' }, h('button', { type: 'submit', class: 'btn btn-primary' }, 'Apply'), h('a', { href: '#/traces', class: 'btn btn-ghost' }, 'Reset')),
  );
  const open = h('form', {
    class: 'toolbar',
    onsubmit: (ev: SubmitEvent) => {
      ev.preventDefault();
      const v = (open.elements.namedItem('key') as HTMLInputElement).value.trim();
      if (v) ctx.navigate(`#/traces/${encodeURIComponent(v)}`);
    },
  });
  mount(
    open,
    h('label', { for: 'tr-open' }, 'Open by correlation ID'),
    h('input', { id: 'tr-open', name: 'key', type: 'search', placeholder: 'Paste the ID shown under an answer…', maxlength: 64 }),
    h('button', { type: 'submit', class: 'btn' }, 'Open'),
  );

  const rows: TraceRow[] = [];
  let next: string | null = null;
  const slot = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  const more = h('button', { type: 'button', class: 'btn', hidden: true }, 'Load more');
  const render = () =>
    mount(
      slot,
      dataTable<TraceRow>(
        [
          { label: 'When', render: (r) => h('time', { datetime: r.at, title: fmtDate(r.at) }, fmtAgo(r.at)) },
          { label: 'Person', render: (r) => (r.replay_of ? h('span', { class: 'muted' }, 'Replay') : r.display_name || r.subject) },
          { label: 'Question', render: (r) => h('span', { class: 'truncate wide', title: r.question }, r.question) },
          { label: 'Verdict', render: (r) => verdictBadge(r.verdict) },
          { label: 'Reason', render: (r) => (r.reason ? h('code', null, r.reason) : '—') },
          { label: 'Red stage', render: (r) => (r.failed_stage ? h('span', { class: 'text-critical' }, r.failed_stage) : '—') },
          { label: 'Time', class: 'num', render: (r) => fmtDuration(r.duration_ms) },
        ],
        rows,
        {
          empty: 'No questions match. Traces are kept for the retention period only.',
          caption: 'Query traces',
          onRowClick: (r) => ctx.navigate(`#/traces/${encodeURIComponent(r.id)}`),
          rowLabel: (r) => `Trace for ${r.question}`,
        },
      ),
    );
  const load = async () => {
    try {
      more.disabled = true;
      const page = await ctx.api.get<Page<TraceRow>>(
        '/api/admin/traces',
        {
          verdict: q.get('verdict') || undefined,
          problems: q.get('problems') === 'true' ? 'true' : undefined,
          minutes: q.get('minutes') || undefined,
          user: q.get('user') || undefined,
          q: q.get('q') || undefined,
          after: next || undefined,
          limit: 50,
        },
        { signal: ctx.signal },
      );
      if (ctx.signal.aborted) return;
      rows.push(...page.items);
      next = page.next;
      render();
      more.hidden = !next;
    } catch (err) {
      if (!isAbortError(err)) mount(slot, problemBox(err));
    } finally {
      more.disabled = false;
    }
  };
  more.addEventListener('click', () => void load());
  mount(root, form, open, slot, h('div', { class: 'load-more' }, more));
  await load();
}

// ================================================================== expectations

async function expectationsTab(ctx: ViewContext, root: HTMLElement): Promise<void> {
  const slot = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
  const meta = await getMeta(ctx).catch(() => null);
  const load = async () => {
    try {
      const res = await ctx.api.get<{ items: Expectation[] }>('/api/admin/expectations', undefined, { signal: ctx.signal });
      if (ctx.signal.aborted) return;
      mount(
        slot,
        dataTable<Expectation>(
          [
            { label: 'Question', render: (e) => h('span', { class: 'truncate wide', title: e.question }, e.question) },
            {
              label: 'Expected',
              render: (e) => [
                e.expected === 'answer' ? 'Answer' : 'No answer',
                e.required_doc_ids.length ? h('div', { class: 'small muted' }, `must cite ${e.required_doc_ids.join(', ')}`) : null,
              ],
            },
            { label: 'As', render: (e) => h('span', { class: 'small' }, attrLine(e.attributes)) },
            {
              label: 'Last result',
              render: (e) =>
                e.last_result
                  ? [h('span', { class: `badge tone-${e.last_result === 'pass' ? 'good' : 'critical'}` }, e.last_result === 'pass' ? '✓ pass' : '✕ fail'), h('div', { class: 'small muted' }, e.last_detail)]
                  : h('span', { class: 'muted' }, 'never run'),
            },
            { label: 'Run', render: (e) => (e.last_run_at ? h('time', { datetime: e.last_run_at, title: fmtDate(e.last_run_at) }, fmtAgo(e.last_run_at)) : '—') },
            {
              label: '',
              render: (e) =>
                h(
                  'div',
                  { class: 'row-actions' },
                  e.last_trace_id ? h('a', { class: 'btn btn-ghost btn-sm', href: `#/traces/${encodeURIComponent(e.last_trace_id)}` }, 'Trace') : null,
                  h('button', { type: 'button', class: 'btn btn-sm', onclick: (ev: MouseEvent) => void replay(ctx, e, ev.currentTarget as HTMLButtonElement, load) }, 'Replay'),
                  h(
                    'button',
                    {
                      type: 'button',
                      class: 'btn btn-ghost btn-sm',
                      onclick: async () => {
                        if (!confirm('Delete this expectation?')) return;
                        try {
                          await ctx.api.delete(`/api/admin/expectations/${encodeURIComponent(e.id)}`);
                          await load();
                        } catch (err) {
                          toastError(err, 'Delete failed');
                        }
                      },
                    },
                    'Delete',
                  ),
                ),
            },
          ],
          res.items,
          { empty: 'No expectations yet. Open a trace and record what its outcome should have been.', caption: 'Expectations' },
        ),
      );
    } catch (err) {
      if (!isAbortError(err)) mount(slot, problemBox(err));
    }
  };
  mount(
    root,
    h(
      'p',
      { class: 'hint' },
      'An expectation is a person stating what the right outcome for a question is. Replaying runs the question again, as the same attributes, and passes only if the outcome matches and every required document is cited.',
      meta ? ` Scheduled replay: ${meta.replay_hours ? `every ${meta.replay_hours} h` : 'off'}.` : '',
    ),
    slot,
  );
  await load();
}

async function replay(ctx: ViewContext, e: Expectation, btn: HTMLButtonElement, after: () => Promise<void>): Promise<void> {
  btn.disabled = true;
  btn.textContent = 'Replaying…';
  try {
    const r = await ctx.api.post<{ expectation: Expectation; trace_id: string; verdict_label: string }>(`/api/admin/expectations/${encodeURIComponent(e.id)}/replay`);
    const pass = r.expectation.last_result === 'pass';
    toast(`${pass ? 'Pass' : 'Fail'}: ${r.expectation.last_detail}`, { kind: pass ? 'success' : 'error' });
    await after();
  } catch (err) {
    toastError(err, 'Replay failed');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Replay';
  }
}

function attrLine(attrs: Record<string, string[] | number>): string {
  return Object.entries(attrs)
    .map(([k, v]) => `${k} ${Array.isArray(v) ? v.join('/') : v}`)
    .join(' · ');
}

// ================================================================== detail

async function traceDetail(ctx: ViewContext, key: string): Promise<void> {
  const content = h('div', { class: 'stack-lg' }, h('p', { class: 'muted' }, 'Loading…'));
  mount(ctx.root, h('p', { class: 'breadcrumb' }, h('a', { href: '#/traces' }, '← Query traces')), content);
  let t: QueryTrace;
  try {
    t = await ctx.api.get<QueryTrace>(`/api/admin/traces/${encodeURIComponent(key)}`, undefined, { signal: ctx.signal });
  } catch (err) {
    if (!isAbortError(err)) mount(content, problemBox(err));
    return;
  }
  if (ctx.signal.aborted) return;

  const copy = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Copy JSON');
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(t, null, 2));
      toast('Trace copied as JSON.', { kind: 'success' });
    } catch (err) {
      toastError(err, 'Copy failed');
    }
  });

  mount(
    ctx.root,
    h('p', { class: 'breadcrumb' }, h('a', { href: '#/traces' }, '← Query traces')),
    pageHeader(t.replay_of ? 'Replay trace' : 'Query trace', copy),
    content,
  );
  mount(
    content,
    summaryCard(t),
    pipeline(t),
    nearMissCard(ctx, t),
    t.stages.map((s) => stageCard(t, s)),
    expectationCard(ctx, t),
  );
  const red = content.querySelector<HTMLElement>('.pipeline .stage.tone-critical');
  red?.scrollIntoView({ block: 'nearest', inline: 'center' });
}

function summaryCard(t: QueryTrace): HTMLElement {
  const problem = t.is_problem;
  return h(
    'section',
    { class: `card verdict-card ${problem ? 'is-problem' : verdictTone(t.verdict) === 'warning' ? 'is-unknown' : 'is-correct'}` },
    h('div', { class: 'card-head' }, h('h2', null, 'Verdict'), verdictBadge(t.verdict, t.verdict_label)),
    h('blockquote', { class: 'question' }, t.question),
    h('ul', { class: 'diagnosis' }, t.diagnosis.slice(1).map((line) => h('li', { class: line.startsWith('  ') ? 'sub' : null }, line.trim()))),
    kv([
      ['Person', t.replay_of ? h('span', null, 'Replay of expectation ', code(t.replay_of, 10)) : `${t.display_name || t.subject} (${t.issuer})`],
      ['Attributes', attrLine(t.attributes) || '—'],
      ['Roles', t.roles.length ? t.roles.join(', ') : '—'],
      ['When', fmtDate(t.at)],
      ['Outcome', `${t.outcome}${t.reason ? ` · ${t.reason}` : ''}`],
      ['Duration', fmtDuration(t.duration_ms)],
      ['Tokens', fmtNum(t.tokens)],
      ['Model', t.model ? `${t.model} (${t.provider})` : '—'],
      ['Correlation ID', t.correlation_id ? correlationTag(t.correlation_id) : '—'],
      ['Trace ID', code(t.id)],
    ]),
  );
}

function pipeline(t: QueryTrace): HTMLElement {
  return h(
    'section',
    { class: 'card' },
    h('h2', null, 'Pipeline'),
    h(
      'p',
      { class: 'hint' },
      'Red is where it went wrong; amber is worth a look; grey did not run. Select a stage to jump to its data.',
    ),
    h(
      'ol',
      { class: 'pipeline', 'aria-label': 'Stages, in the order they ran' },
      t.stages.map((s) =>
        h(
          'li',
          { class: `stage tone-${STAGE_TONE[s.status]}${t.failed_stage === s.name ? ' is-blamed' : ''}` },
          h(
            'a',
            { href: `#stage-${s.name}`, onclick: (ev: MouseEvent) => jump(ev, s.name), title: s.summary || s.label },
            h('span', { class: 'stage-icon', 'aria-hidden': 'true' }, STAGE_ICON[s.status]),
            h('span', { class: 'stage-label' }, s.label),
            h('span', { class: 'stage-time' }, s.duration_ms != null ? fmtDuration(s.duration_ms) : s.status === 'skipped' ? 'not run' : ''),
            h('span', { class: 'visually-hidden' }, ` — ${s.status}`),
          ),
        ),
      ),
    ),
  );
}

function jump(ev: MouseEvent, name: string): void {
  ev.preventDefault(); // the hash is the router's; scroll without navigating
  const el = document.getElementById(`stage-${name}`);
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  el?.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- near miss

function checkCell(c: AttributeCheck | undefined): HTMLElement {
  if (!c) return h('td', null, '—');
  return h(
    'td',
    { class: c.passed ? 'cell-ok' : 'cell-fail', title: c.note || undefined },
    h('span', { 'aria-hidden': 'true' }, c.passed ? '✓ ' : '✕ '),
    h('span', { class: 'visually-hidden' }, c.passed ? 'passes: ' : 'blocks: '),
    fmtValue(c.doc_values),
    c.note && !c.passed ? h('div', { class: 'small' }, c.note) : null,
  );
}

function nearMissCard(ctx: ViewContext, t: QueryTrace): HTMLElement | null {
  const nm = t.near_miss;
  if (t.outcome === 'answered') return null;
  if (!nm.ran) {
    return nm.skipped_reason ? h('section', { class: 'card' }, h('h2', null, 'Near-miss check'), h('p', { class: 'muted' }, `Did not run: ${nm.skipped_reason}`)) : null;
  }
  const attrs = [...new Set(nm.docs.flatMap((d) => Object.keys(d.checks)))];
  const bar = Object.entries(nm.relevance_bar)
    .map(([k, v]) => `${k} ${v}`)
    .join(', ');
  return h(
    'section',
    { class: 'card', id: 'near-miss', tabindex: '-1' },
    h('h2', null, 'Near-miss check'),
    h(
      'p',
      { class: 'hint' },
      'The same search again with ONLY the access filter removed - same query, same vector, same facet filters, same relevance bar (',
      bar || 'none',
      '). A document listed here is relevant; each column says whether that access attribute lets this person read it. These documents were not shown to the person.',
    ),
    nm.docs.length
      ? h(
          'div',
          { class: 'table-wrap' },
          h(
            'table',
            { class: 'data near-miss' },
            h('caption', { class: 'visually-hidden' }, 'Relevant documents and the access attributes that allow or block them'),
            h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Document'), h('th', { scope: 'col', class: 'num' }, 'Score'), h('th', { scope: 'col' }, 'Readable?'), attrs.map((a) => h('th', { scope: 'col' }, a)))),
            h(
              'tbody',
              null,
              nm.docs.map((d: NearMissDoc) => [
                h(
                  'tr',
                  null,
                  h('td', null, h('div', { class: 'doc-cell' }, h('strong', { class: 'truncate', title: d.title }, d.title || d.path), h('span', { class: 'small muted break' }, `${d.path}${d.page ? ` · page ${d.page}` : ''}`), h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => openDocumentDialog(ctx.api, d.doc_id, undefined, { focusAccess: !d.allowed }) }, d.allowed ? 'Open document' : 'Open / fix access tags'), d.private ? h('span', { class: 'badge tone-neutral' }, 'Only me') : null)),
                  h('td', { class: 'num' }, d.reranker_score != null ? `${fmtValue(d.reranker_score)} (rerank)` : fmtValue(d.score)),
                  h('td', null, d.allowed ? h('span', { class: 'badge tone-critical', title: 'This person may read it, yet their search did not return it' }, 'yes - missing!') : h('span', { class: 'badge tone-neutral' }, 'no')),
                  attrs.map((a) => checkCell(d.checks[a])),
                ),
                d.problems.length
                  ? h('tr', { class: 'problem-row' }, h('td', { colspan: 3 + attrs.length }, h('ul', { class: 'problems' }, d.problems.map((p) => h('li', null, p)))))
                  : null,
              ]),
            ),
          ),
        )
      : h('p', { class: 'empty' }, 'Nothing cleared the relevance bar, even with the access filter removed.'),
    nm.below_bar ? h('p', { class: 'small muted' }, `${fmtNum(nm.below_bar)} more passage(s) were found below the relevance bar and are not listed.`) : null,
  );
}

// ---------------------------------------------------------------- stage cards

function hitsTable(hits: unknown, caption: string, redBelow?: Record<string, number>): HTMLElement {
  const rows = Array.isArray(hits) ? (hits as Record<string, unknown>[]) : [];
  return dataTable<Record<string, unknown>>(
    [
      { label: 'Document', render: (r) => h('div', { class: 'doc-cell' }, h('span', { class: 'truncate', title: String(r.title ?? '') }, String(r.title || r.path || '')), h('span', { class: 'small muted break' }, `${String(r.path ?? '')}${r.page ? ` · page ${String(r.page)}` : ''}`)) },
      { label: 'Score', class: 'num', render: (r) => scoreCell(r.score, redBelow?.min_score) },
      { label: 'Rerank', class: 'num', render: (r) => scoreCell(r.reranker_score, redBelow?.min_reranker_score) },
      { label: 'Kept', render: (r) => (r.kept === undefined ? '' : r.kept ? h('span', { class: 'badge tone-good' }, 'kept') : h('span', { class: 'badge tone-warning' }, 'below bar')) },
    ],
    rows,
    { empty: 'None.', caption },
  );
}

function scoreCell(v: unknown, bar?: number): Child {
  if (typeof v !== 'number') return '—';
  const below = bar !== undefined && bar > 0 && v < bar;
  return h('span', { class: below ? 'text-critical strong' : null, title: below ? `below the bar of ${bar}` : null }, fmtValue(v));
}

function attributesTable(attrs: unknown): HTMLElement {
  const rows = Array.isArray(attrs) ? (attrs as Record<string, unknown>[]) : [];
  return dataTable<Record<string, unknown>>(
    [
      { label: 'Attribute', render: (r) => String(r.label ?? r.name) },
      { label: 'Claim', render: (r) => (r.claim ? h('code', null, String(r.claim)) : '—') },
      {
        label: 'On the account',
        render: (r) => (r.present ? fmtValue(r.values ?? r.level) : h('span', { class: r.required ? 'text-critical strong' : 'muted' }, r.required ? '✕ missing (required)' : 'not set')),
      },
      { label: 'Also reaches', render: (r) => fmtValue(r.also_reaches) },
      { label: 'Meaning', render: (r) => h('span', { class: 'small' }, String(r.meaning ?? '')) },
    ],
    rows,
    { empty: 'No attributes.', caption: 'Caller attributes' },
  );
}

function stageBody(t: QueryTrace, s: TraceStage): Child {
  const d = s.data;
  switch (s.name) {
    case 'request':
      return kv([
        ['Question', h('span', { class: 'pre-wrap' }, t.question)],
        ['Facet filters', Object.keys(t.filters).length ? attrLine(t.filters) : 'none'],
        ['Earlier turns', fmtNum(t.history_turns)],
      ]);
    case 'guard':
      return kv([
        ['Index', d.index ? h('code', null, String(d.index)) : '—'],
        ['Embedding profile', d.profile_fingerprint ? h('code', null, String(d.profile_fingerprint)) : '—'],
        ['Refused because', d.reason ? h('span', { class: 'text-critical strong' }, String(d.reason)) : 'not refused'],
      ]);
    case 'identity':
      return [
        Array.isArray(d.problems) && d.problems.length ? h('ul', { class: 'problems' }, (d.problems as string[]).map((p) => h('li', null, p))) : null,
        kv([
          ['Subject', code(String(d.subject ?? t.subject))],
          ['Issuer', String(d.issuer ?? t.issuer)],
          ['Roles', fmtValue(d.roles)],
          ['Token roles', fmtValue(d.claimed_roles)],
          ['Administrator', d.bypass ? 'yes - the access filter does not apply' : 'no'],
        ]),
        attributesTable(d.attributes),
      ];
    case 'access':
      return [
        kv([
          ['Attributes used', fmtValue(d.attributes_used)],
          ['Facet filter', fmtValue(d.facet_filter)],
          ['No access at all', d.deny_all ? h('span', { class: 'text-critical strong' }, 'yes - a required attribute is missing') : 'no'],
        ]),
        d.combined_filter ? [h('h3', null, 'Filter applied inside the search'), h('pre', { class: 'code-block' }, String(d.combined_filter))] : null,
      ];
    case 'query':
      return kv([
        ['Searched for', String(d.search_query ?? '—')],
        ['Keyword query', h('code', { class: 'break' }, String(d.keyword_query ?? '—'))],
        ['Rewritten from conversation', fmtValue(d.condensed)],
      ]);
    case 'embed':
      return kv([
        ['Dimensions', fmtValue(d.dimensions)],
        ['Embedding tokens', fmtValue(d.tokens)],
      ]);
    case 'search':
      return [kv([['Top k', fmtValue(d.top_k)]]), hitsTable(d.hits, 'Search hits inside the access filter')];
    case 'relevance': {
      const thresholds = (d.thresholds ?? {}) as Record<string, number>;
      return [
        kv([
          ['Relevance bar', fmtValue(thresholds)],
          ['Kept', fmtValue(d.kept)],
          ['Duplicates merged', fmtValue(d.duplicates)],
        ]),
        Array.isArray(d.dropped) && d.dropped.length ? [h('h3', null, 'Below the bar'), hitsTable(d.dropped, 'Hits below the relevance bar', thresholds)] : null,
      ];
    }
    case 'prompt':
      return [kv([['Passages', fmtValue(d.blocks)], ['Characters', fmtValue(d.chars)], ['History messages', fmtValue(d.history_messages)]]), hitsTable(d.passages, 'Passages sent to the model')];
    case 'llm':
      return kv([
        ['Model', `${String(d.model ?? '—')} (${String(d.provider ?? '—')})`],
        ['Stop reason', fmtValue(d.stop_reason)],
        ['Declined', d.refused ? h('span', { class: 'text-critical strong' }, 'yes') : 'no'],
        ['Fallback used', fmtValue(d.fallback_used)],
        ['Output characters', fmtValue(d.output_chars)],
        ['Tokens', fmtValue(d.usage)],
      ]);
    case 'grounding':
      return [
        d.guard ? h('p', { class: 'text-critical strong' }, `Grounding guard: ${String(d.guard)}`) : null,
        dataTable<Record<string, unknown>>(
          [
            { label: '#', render: (r) => `[${String(r.index)}]` },
            { label: 'Document', render: (r) => h('span', { class: 'truncate', title: String(r.path ?? '') }, String(r.title || r.path || '')) },
            { label: 'Page', render: (r) => fmtValue(r.page) },
            { label: 'Score', class: 'num', render: (r) => fmtValue(r.score) },
          ],
          Array.isArray(d.citations) ? (d.citations as Record<string, unknown>[]) : [],
          { empty: 'No citations.', caption: 'Citations' },
        ),
      ];
    case 'outcome':
      return [kv([['Outcome', fmtValue(d.outcome)], ['Reason', fmtValue(d.reason)], ['Verdict', t.verdict_label]]), t.answer ? [h('h3', null, 'What the person saw'), h('div', { class: 'answer-box pre-wrap' }, t.answer)] : null];
    default:
      return Object.keys(d).length ? h('pre', { class: 'code-block' }, JSON.stringify(d, null, 2)) : null;
  }
}

function stageCard(t: QueryTrace, s: TraceStage): HTMLElement {
  return h(
    'section',
    { class: `card stage-card tone-border-${STAGE_TONE[s.status]}`, id: `stage-${s.name}`, tabindex: '-1' },
    h(
      'div',
      { class: 'card-head' },
      h('h2', null, s.label),
      h('span', { class: `badge tone-${STAGE_TONE[s.status]}` }, h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, STAGE_ICON[s.status]), s.status === 'skipped' ? 'not run' : s.status),
    ),
    s.summary ? h('p', { class: s.status === 'fail' ? 'text-critical strong' : 'muted' }, s.summary) : null,
    typeof s.data.ran_ok === 'string' && s.data.ran_ok ? h('p', { class: 'small muted' }, `The stage itself ran: ${s.data.ran_ok}`) : null,
    s.duration_ms != null ? h('p', { class: 'small muted' }, `Took ${fmtDuration(s.duration_ms)}`) : null,
    s.status === 'skipped' ? null : stageBody(t, s),
  );
}

// ---------------------------------------------------------------- record the right outcome

function expectationCard(ctx: ViewContext, t: QueryTrace): HTMLElement {
  const answerRadio = h('input', { type: 'radio', name: 'expected', id: 'exp-answer', value: 'answer', checked: t.outcome !== 'answered' });
  const noAnswerRadio = h('input', { type: 'radio', name: 'expected', id: 'exp-none', value: 'no_answer', checked: t.outcome === 'answered' });
  const candidates = new Map<string, string>();
  for (const d of t.near_miss.docs) candidates.set(d.doc_id, d.title || d.path);
  const grounding = t.stages.find((s) => s.name === 'grounding')?.data.citations;
  if (Array.isArray(grounding)) for (const c of grounding as Record<string, unknown>[]) candidates.set(String(c.doc_id), String(c.title || c.path || c.doc_id));
  const docBoxes = [...candidates.entries()].map(([id, title], i) =>
    h('div', { class: 'check' }, h('input', { type: 'checkbox', id: `exp-doc-${i}`, value: id }), h('label', { for: `exp-doc-${i}` }, title, h('code', { class: 'small' }, id.slice(0, 12)))),
  );
  const extra = h('input', { id: 'exp-docs', type: 'text', placeholder: 'Other document ids, comma separated', maxlength: 500 });
  const note = h('textarea', { id: 'exp-note', rows: 2, maxlength: 2000, placeholder: 'Why (optional) - e.g. "HR in AMER must be able to read the benefits guide"' });
  const save = h('button', { type: 'submit', class: 'btn btn-primary' }, 'Save expectation');
  const docsField = h('fieldset', null, h('legend', null, 'Documents the answer must cite (optional)'), docBoxes.length ? docBoxes : h('p', { class: 'small muted' }, 'No candidates in this trace.'), extra);
  const sync = () => (docsField.disabled = noAnswerRadio.checked);
  answerRadio.addEventListener('change', sync);
  noAnswerRadio.addEventListener('change', sync);
  sync();
  const form = h(
    'form',
    {
      class: 'stack',
      onsubmit: async (ev: SubmitEvent) => {
        ev.preventDefault();
        const expected = noAnswerRadio.checked ? 'no_answer' : 'answer';
        const ids = expected === 'answer'
          ? [...docBoxes.map((b) => b.querySelector('input')!).filter((i) => i.checked).map((i) => i.value), ...extra.value.split(',').map((s) => s.trim()).filter(Boolean)]
          : [];
        save.disabled = true;
        try {
          await ctx.api.post(`/api/admin/traces/${encodeURIComponent(t.id)}/expectation`, { expected, required_doc_ids: ids, note: note.value.trim() });
          toast('Expectation saved. Replay it from the Expectations tab after a fix.', { kind: 'success' });
          ctx.navigate('#/traces?tab=expectations');
        } catch (err) {
          toastError(err, 'Could not save the expectation');
        } finally {
          save.disabled = false;
        }
      },
    },
    h('fieldset', null, h('legend', null, 'For this person, this question should get'), h('div', { class: 'check inline' }, answerRadio, h('label', { for: 'exp-answer' }, 'an answer')), h('div', { class: 'check inline' }, noAnswerRadio, h('label', { for: 'exp-none' }, 'no answer'))),
    docsField,
    h('div', { class: 'field' }, h('label', { for: 'exp-note' }, 'Note'), note),
    h('div', null, save),
  );
  return h(
    'section',
    { class: 'card', id: 'expectation' },
    h('h2', null, 'What should have happened?'),
    h('p', { class: 'hint' }, 'The trace shows why; only a person knows what the right outcome is. Record it here, fix the cause, then replay: it passes only when the outcome matches and every chosen document is cited.'),
    form,
  );
}
