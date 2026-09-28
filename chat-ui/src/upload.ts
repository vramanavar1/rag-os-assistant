// Upload widget: POST /api/uploads (multipart field "file") then poll
// GET /api/uploads/{tracking_id} until the document reaches a terminal status.
import { isAbortError, type ApiClient } from './api';
import { fmtBytes, h, mount } from './dom';
import { problemBox, statusBadge } from './ui';
import { TERMINAL_STATUSES, type DocumentRecord, type UploadAccepted } from './types';

// Three limits govern an upload and they have to agree: nginx (client_max_body_size 60m, in
// nginx/default.conf.template), the API (upload_max_mb, default 50, in settings.py) and this one. The
// smallest wins, so this matches the API - checking here only saves the user a minute of uploading
// something the server was always going to refuse.
const MAX_MB = 50;
const ACCEPT = '.pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.xls,.json,.jsonl,.jsonp,.xml,.html,.htm,.pptx';
const POLL_TIMEOUT_MS = 30 * 60_000;
// Consecutive failed polls before we stop watching. A transient 500, a dropped connection or a laptop waking
// from sleep must never be mistaken for the document failing: the server never heard about any of them.
const MAX_POLL_FAILURES = 5;

/** We stopped watching. Says nothing about the document, which the server is still processing. */
class StoppedWatching extends Error {
  constructor(
    message: string,
    readonly lastKnown: DocumentRecord | null,
  ) {
    super(message);
    this.name = 'StoppedWatching';
  }
}

export interface UploadWidgetOptions {
  onFinished?: (record: DocumentRecord) => void;
  /** Where a finished document's detail lives. Omitted leaves the id as plain text. */
  documentHref?: (docId: string) => string;
}

export function createUploadWidget(api: ApiClient, opts: UploadWidgetOptions = {}): HTMLElement {
  const list = h('ul', { class: 'upload-list', 'aria-live': 'polite' });
  const input = h('input', { type: 'file', id: `upload-${Math.random().toString(36).slice(2)}`, multiple: true, accept: ACCEPT, class: 'visually-hidden' });
  const zone = h(
    'div',
    { class: 'dropzone' },
    h('p', null, 'Drop files here or ', h('label', { for: input.id, class: 'link-button' }, 'choose files'), '.'),
    h('p', { class: 'hint' }, `PDF, Office, text, CSV, JSON, XML … up to ${MAX_MB} MB each. Files are indexed with your identity's access tags.`),
    input,
  );

  const start = (files: FileList | File[]) => {
    for (const file of Array.from(files)) void uploadOne(file);
  };
  input.addEventListener('change', () => {
    if (input.files) start(input.files);
    input.value = '';
  });
  zone.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    zone.classList.add('dragging');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
  zone.addEventListener('drop', (ev) => {
    ev.preventDefault();
    zone.classList.remove('dragging');
    if (ev.dataTransfer?.files?.length) start(ev.dataTransfer.files);
  });

  async function uploadOne(file: File): Promise<void> {
    const state = h('span', { class: 'upload-state' }, 'Uploading…');
    // Three nodes, not two. The tracking id and the stage used to share one, and `mount` replaces children -
    // so the first stage update silently erased the tracking id, about two seconds after showing it. That id
    // is the only durable handle the uploader gets.
    const track = h('div', { class: 'upload-detail muted' });
    const detail = h('div', { class: 'upload-detail' });
    const item = h(
      'li',
      { class: 'upload-item' },
      h('div', { class: 'upload-head' }, h('span', { class: 'upload-name', title: file.name }, file.name), h('span', { class: 'muted' }, fmtBytes(file.size)), state),
      track,
      detail,
    );
    list.prepend(item);

    if (file.size > MAX_MB * 1024 * 1024) {
      mount(state, statusBadge('FAILED'));
      mount(detail, `File is larger than ${MAX_MB} MB.`);
      return;
    }

    // ---- the upload itself. A failure here IS the document failing: the server never took it.
    let accepted: UploadAccepted;
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      accepted = await api.request<UploadAccepted>('POST', '/api/uploads', { body: form, timeoutMs: 15 * 60_000 });
    } catch (err) {
      if (isAbortError(err)) return;
      mount(state, statusBadge('FAILED'));
      mount(detail, problemBox(err));
      return;
    }

    // ---- past here the server holds the document and nothing this browser does can change its fate. So
    // everything below reports "we stopped watching", never FAILED. Painting a healthy document as failed
    // because one poll returned a 500 is worse than saying nothing.
    mount(state, statusBadge(accepted.status || 'QUEUED'));
    mount(track, 'Tracking ', h('code', null, accepted.tracking_id));
    try {
      const record = await poll(accepted.tracking_id, (rec) => {
        mount(state, statusBadge(rec.status));
        if (rec.stage) mount(detail, h('span', { class: 'muted' }, `Stage: ${rec.stage}`));
      });
      mount(state, statusBadge(record.status));
      mount(detail, outcome(record));
      opts.onFinished?.(record);
    } catch (err) {
      if (isAbortError(err)) return;
      const stopped = err instanceof StoppedWatching ? err : null;
      mount(state, statusBadge(stopped?.lastKnown?.status ?? accepted.status ?? 'QUEUED'));
      mount(detail, h('span', { class: 'muted' }, stopped ? stopped.message : describeUnknown(err)));
    }
  }

  /** What actually happened, for each terminal status - not merely "failed or not". */
  function outcome(rec: DocumentRecord): HTMLElement {
    if (rec.status === 'FAILED') {
      return h('span', { class: 'text-critical' }, rec.error_message || rec.error_type || 'Processing failed.');
    }
    if (rec.status === 'SKIPPED_UNCHANGED') {
      return h('span', { class: 'muted' }, 'Already indexed — identical to a copy in the corpus, so nothing was re-indexed.');
    }
    if (rec.status === 'DELETED') {
      return h('span', { class: 'muted' }, 'Removed from the index.');
    }
    return h('span', { class: 'muted' }, `${rec.chunk_count ?? 0} chunks · `, docRef(rec.doc_id));
  }

  function describeUnknown(err: unknown): string {
    const why = err instanceof Error ? err.message : String(err);
    return `Stopped watching this upload (${why}). The document is still being processed.`;
  }

  function docRef(docId: string): HTMLElement {
    const href = opts.documentHref?.(docId);
    return href ? h('a', { href }, h('code', null, docId)) : h('code', null, docId);
  }

  async function poll(trackingId: string, onProgress: (rec: DocumentRecord) => void): Promise<DocumentRecord> {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    let delay = 1_500;
    let failures = 0;
    let last: DocumentRecord | null = null;
    for (;;) {
      await new Promise((r) => window.setTimeout(r, delay));
      let rec: DocumentRecord;
      try {
        rec = await api.get<DocumentRecord>(`/api/uploads/${encodeURIComponent(trackingId)}`);
        failures = 0;
      } catch (err) {
        if (isAbortError(err)) throw err;
        if (++failures >= MAX_POLL_FAILURES) {
          throw new StoppedWatching('Lost contact while checking on this document. It is still being processed.', last);
        }
        delay = Math.min(delay * 2, 10_000); // back off harder than the happy path
        continue;
      }
      last = rec;
      if (TERMINAL_STATUSES.has(rec.status)) return rec;
      onProgress(rec);
      if (Date.now() > deadline) {
        // The reconciler re-queues anything in flight for this long, so "still going" is the likely truth
        // rather than the exception. The old advice here was to check the admin console, which the people
        // most likely to see this message cannot open.
        throw new StoppedWatching(
          `Still processing after ${POLL_TIMEOUT_MS / 60_000} minutes. Large files can take a while; its status keeps updating in the list below.`,
          rec,
        );
      }
      delay = Math.min(delay * 1.5, 5_000);
    }
  }

  return h('section', { class: 'upload', 'aria-label': 'Upload documents' }, zone, list);
}
