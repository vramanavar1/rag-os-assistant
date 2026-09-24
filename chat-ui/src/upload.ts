// Upload widget: POST /api/uploads (multipart field "file") then poll
// GET /api/uploads/{tracking_id} until the document reaches a terminal status.
import { isAbortError, type ApiClient } from './api';
import { fmtBytes, h, mount } from './dom';
import { problemBox, statusBadge } from './ui';
import { TERMINAL_STATUSES, type DocumentRecord, type UploadAccepted } from './types';

const MAX_MB = 60; // nginx client_max_body_size
const ACCEPT = '.pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.xls,.json,.jsonl,.jsonp,.xml,.html,.htm,.pptx';
const POLL_TIMEOUT_MS = 30 * 60_000;

export interface UploadWidgetOptions {
  onFinished?: (record: DocumentRecord) => void;
  /** Render document ids as links to the admin document view. */
  linkDocuments?: boolean;
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
    const detail = h('div', { class: 'upload-detail' });
    const item = h(
      'li',
      { class: 'upload-item' },
      h('div', { class: 'upload-head' }, h('span', { class: 'upload-name', title: file.name }, file.name), h('span', { class: 'muted' }, fmtBytes(file.size)), state),
      detail,
    );
    list.prepend(item);

    if (file.size > MAX_MB * 1024 * 1024) {
      mount(state, statusBadge('FAILED'));
      mount(detail, `File is larger than ${MAX_MB} MB.`);
      return;
    }
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      const accepted = await api.request<UploadAccepted>('POST', '/api/uploads', { body: form, timeoutMs: 15 * 60_000 });
      mount(state, statusBadge(accepted.status || 'QUEUED'));
      mount(detail, h('span', { class: 'muted' }, 'Tracking ', h('code', null, accepted.tracking_id)));
      const record = await poll(accepted.tracking_id, (rec) => {
        mount(state, statusBadge(rec.status));
        if (rec.stage) mount(detail, h('span', { class: 'muted' }, `Stage: ${rec.stage}`));
      });
      mount(state, statusBadge(record.status));
      mount(
        detail,
        record.status === 'FAILED'
          ? h('span', { class: 'text-critical' }, record.error_message || record.error_type || 'Processing failed.')
          : h('span', { class: 'muted' }, `${record.chunk_count ?? 0} chunks · `, docRef(record.doc_id)),
      );
      opts.onFinished?.(record);
    } catch (err) {
      if (isAbortError(err)) return;
      mount(state, statusBadge('FAILED'));
      mount(detail, problemBox(err));
    }
  }

  function docRef(docId: string): HTMLElement {
    return opts.linkDocuments
      ? h('a', { href: `#/documents/${encodeURIComponent(docId)}` }, h('code', null, docId))
      : h('code', null, docId);
  }

  async function poll(trackingId: string, onProgress: (rec: DocumentRecord) => void): Promise<DocumentRecord> {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    let delay = 1_500;
    for (;;) {
      await new Promise((r) => window.setTimeout(r, delay));
      const rec = await api.get<DocumentRecord>(`/api/uploads/${encodeURIComponent(trackingId)}`);
      if (TERMINAL_STATUSES.has(rec.status)) return rec;
      onProgress(rec);
      if (Date.now() > deadline) throw new Error('Still processing after 30 minutes — check the admin console.');
      delay = Math.min(delay * 1.5, 5_000);
    }
  }

  return h('section', { class: 'upload', 'aria-label': 'Upload documents' }, zone, list);
}
