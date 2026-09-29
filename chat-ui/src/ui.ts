// Shared UI bits: toasts (with correlation id), status badges, the "signed in as" chip.
import { describeError } from './api';
import { h } from './dom';
import type { Me } from './types';

type ToastKind = 'info' | 'success' | 'error';

interface ToastOptions {
  kind?: ToastKind;
  detail?: string;
  lines?: string[];
  correlationId?: string;
  timeoutMs?: number;
}

function toastRegion(): HTMLElement {
  let region = document.getElementById('toasts');
  if (!region) {
    region = h('div', { id: 'toasts', class: 'toasts' });
    document.body.appendChild(region);
  }
  return region;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function correlationTag(id: string): HTMLElement {
  const btn: HTMLButtonElement = h(
    'button',
    {
      type: 'button',
      class: 'corr',
      title: 'Copy correlation ID (quote it when reporting a problem)',
      onclick: async () => {
        const ok = await copyText(id);
        btn.dataset.copied = ok ? 'true' : 'false';
        window.setTimeout(() => delete btn.dataset.copied, 1500);
      },
    },
    h('span', { class: 'corr-label' }, 'Correlation ID '),
    h('code', null, id),
  );
  return btn;
}

export function toast(message: string, opts: ToastOptions = {}): void {
  const kind = opts.kind ?? 'info';
  const icon = kind === 'error' ? '✕' : kind === 'success' ? '✓' : 'i';
  const el: HTMLDivElement = h(
    'div',
    { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast-icon', 'aria-hidden': 'true' }, icon),
    h(
      'div',
      { class: 'toast-body' },
      h('strong', null, message),
      opts.detail ? h('p', null, opts.detail) : null,
      opts.lines?.length ? h('ul', null, opts.lines.slice(0, 8).map((l) => h('li', null, l))) : null,
      opts.correlationId ? correlationTag(opts.correlationId) : null,
    ),
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss', onclick: () => el.remove() }, '×'),
  );
  toastRegion().appendChild(el);
  const timeout = opts.timeoutMs ?? (kind === 'error' ? 15_000 : 5_000);
  let timer = window.setTimeout(() => el.remove(), timeout);
  el.addEventListener('mouseenter', () => window.clearTimeout(timer));
  el.addEventListener('mouseleave', () => {
    timer = window.setTimeout(() => el.remove(), 4_000);
  });
}

export function toastError(err: unknown, context?: string): void {
  const d = describeError(err);
  toast(context ? `${context}: ${d.title}` : d.title, {
    kind: 'error',
    ...(d.detail ? { detail: d.detail } : {}),
    lines: d.lines,
    ...(d.correlationId ? { correlationId: d.correlationId } : {}),
  });
}

/** Inline problem panel (for forms / editors where a toast is too transient). */
export function problemBox(err: unknown): HTMLElement {
  const d = describeError(err);
  return h(
    'div',
    { class: 'problem', role: 'alert' },
    h('strong', null, d.title),
    d.detail ? h('p', null, d.detail) : null,
    d.lines.length ? h('ul', null, d.lines.map((l) => h('li', null, l))) : null,
    d.correlationId ? correlationTag(d.correlationId) : null,
  );
}

// ---------------------------------------------------------------- status badges

const STATUS_META: Record<string, { tone: string; icon: string; label: string }> = {
  INDEXED: { tone: 'good', icon: '✓', label: 'Indexed' },
  COMPLETED: { tone: 'good', icon: '✓', label: 'Completed' },
  APPROVED: { tone: 'good', icon: '✓', label: 'Approved' },
  FAILED: { tone: 'critical', icon: '✕', label: 'Failed' },
  SKIPPED_UNCHANGED: { tone: 'neutral', icon: '=', label: 'Unchanged' },
  DELETED: { tone: 'muted', icon: '−', label: 'Deleted' },
  PENDING: { tone: 'warning', icon: '!', label: 'Pending review' },
  RUNNING: { tone: 'info', icon: '↻', label: 'Running' },
  PAUSED: { tone: 'warning', icon: '‖', label: 'Paused' },
};

export function statusMeta(status: string): { tone: string; icon: string; label: string } {
  return (
    STATUS_META[status] ?? {
      tone: 'info',
      icon: '…',
      label: status.charAt(0) + status.slice(1).toLowerCase().replace(/_/g, ' '),
    }
  );
}

export function statusBadge(status: string | null | undefined): HTMLElement {
  const s = status || 'UNKNOWN';
  const m = statusMeta(s);
  return h(
    'span',
    { class: `badge tone-${m.tone}`, title: s },
    h('span', { class: 'badge-icon', 'aria-hidden': 'true' }, m.icon),
    m.label,
  );
}

// ---------------------------------------------------------------- identity chip

export function attributeSummary(attrs: Record<string, unknown> | null | undefined): string {
  if (!attrs) return '';
  return Object.entries(attrs)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('|') : typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(', ');
}

/** Does this principal hold the admin role? The one spelling of the test, so the UI cannot disagree with itself. */
export function isAdmin(me: Me | null | undefined): boolean {
  return (me?.roles ?? []).some((r) => r.toLowerCase() === 'admin');
}

export interface IdentityChipOptions {
  /** Opens Account Information. Given it, the chip becomes a real control rather than a label. */
  onOpenAccount?: () => void;
  /** Where the admin console lives. Passed by the chat page; omitted by the console, which must not link to
   *  itself. Rendered only for an admin - /admin already refuses everyone else, so this is about not
   *  advertising a door that will not open, not about access control. */
  adminHref?: string;
}

export function identityChip(me: Me, onSignOut?: () => void, opts: IdentityChipOptions = {}): HTMLElement {
  const name = me.display_name || me.subject;
  const attrs = attributeSummary(me.attributes);
  const open = opts.onOpenAccount;
  // The attribute dump used to live in this tooltip, where it rendered as `clearance=2` with nothing anywhere
  // that could turn 2 into a word. The panel supersedes it.
  const who = h(
    'span',
    {
      class: open ? 'identity-text identity-link' : 'identity-text',
      ...(open
        ? { role: 'button', tabindex: '0', title: 'Account Information', onclick: open,
            onkeydown: (ev: KeyboardEvent) => {
              // A div with an onclick is not a control: without this it is unreachable by keyboard.
              if (ev.key === 'Enter' || ev.key === ' ') {
                ev.preventDefault();
                open();
              }
            } }
        : { title: `Subject: ${me.subject}\nIssuer: ${me.issuer_kind}` }),
    },
    'Signed in as ',
    h('strong', null, name),
    attrs ? h('span', { class: 'identity-attrs' }, ` (${attrs})`) : null,
  );
  return h(
    'div',
    { class: 'identity' },
    h('span', { class: 'identity-dot', 'aria-hidden': 'true' }),
    who,
    opts.adminHref && isAdmin(me)
      ? h('a', {
          class: 'btn btn-ghost btn-sm',
          href: opts.adminHref,
          title: 'Ingestion status, sources and configuration',
        }, 'Admin')
      : null,
    onSignOut ? h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: onSignOut }, 'Sign out') : null,
  );
}
