// Upload widget: POST /api/uploads (multipart field "file") then poll
// GET /api/uploads/{tracking_id} until the document reaches a terminal status.
//
// Facets come from two places and the server merges them in this order: the folder the file sat in (path rules
// in path-rules.yaml, facets only - never ACL) and then whatever the uploader explicitly picked below. So the
// default behaviour is "respect the folder" and a picker is an override.
import { isAbortError, type ApiClient } from './api';
import { fmtBytes, h, mount, show } from './dom';
import { problemBox, statusBadge } from './ui';
import { clearanceSelect, vocabularySelect } from './tag-controls';
import { TERMINAL_STATUSES, type DocumentRecord, type Facet, type FacetsResponse, type UploadAccepted, type UploadOptions } from './types';

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
// Leaving a picker here means "take it from the folder", which is why it is the default choice rather than a
// value: an override should be something the uploader visibly chose.
const FROM_FOLDER = '';

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
  /** Offer facet pickers (needs GET /api/facets for the vocabulary). */
  facetPickers?: boolean;
}

/** A file plus the path of the folder it came from, which only a folder pick or a directory drop provides. */
interface Picked {
  file: File;
  relativePath: string;
}

export function createUploadWidget(api: ApiClient, opts: UploadWidgetOptions = {}): HTMLElement {
  const list = h('ul', { class: 'upload-list', 'aria-live': 'polite' });
  const idSuffix = Math.random().toString(36).slice(2);
  const input = h('input', { type: 'file', id: `upload-${idSuffix}`, multiple: true, accept: ACCEPT, class: 'visually-hidden' });
  // webkitdirectory is set through the DOM rather than as an attribute: it is non-standard, and `h` would
  // happily write an attribute the type system knows nothing about.
  const folderInput = h('input', { type: 'file', id: `upload-dir-${idSuffix}`, multiple: true, class: 'visually-hidden' });
  folderInput.webkitdirectory = true;

  const pickers = h('div', { class: 'facet-pickers', hidden: true });
  const selects = new Map<string, HTMLSelectElement>();

  // ---- who can read it. Unticked (the default) = everyone whose Department, Region and Clearance match the
  // document's tags; ticked = only the uploader. Shown once /api/uploads/options says what this caller may do.
  const onlyMe = h('input', { type: 'checkbox', id: `only-me-${idSuffix}` });
  let clearanceSel = h('select', { id: `clearance-${idSuffix}`, name: 'clearance' });
  const clearanceField = h('div', { class: 'field', hidden: true }, h('label', { for: clearanceSel.id }, 'Clearance'), clearanceSel);
  const onlyMeField = h('div', { class: 'check inline', hidden: true }, onlyMe, h('label', { for: onlyMe.id }, 'Only me (private)'));
  const accessHint = h('p', { class: 'hint' });
  const access = h(
    'fieldset',
    { class: 'upload-access', hidden: true },
    h('legend', null, 'Who can read it'),
    h('div', { class: 'row' }, onlyMeField, clearanceField),
    accessHint,
  );
  const syncAccess = () => {
    clearanceField.hidden = onlyMe.checked || !clearanceSel.options.length;
    mount(
      accessHint,
      onlyMe.checked
        ? 'Only you can read it. The tags below still classify it.'
        : 'Everyone whose Department, Region and Clearance match its tags can read it. Set Department and Region below (or upload from a folder named after them).',
    );
  };
  onlyMe.addEventListener('change', syncAccess);

  async function loadAccess(): Promise<void> {
    try {
      const o = await api.get<UploadOptions>('/api/uploads/options');
      onlyMe.checked = o.only_me.default;
      onlyMeField.hidden = !o.only_me.allowed;
      if (o.clearance) {
        const built = clearanceSelect({ id: clearanceSel.id, name: 'clearance', clearance: o.clearance, selected: o.clearance.default, min: o.clearance.min });
        clearanceSel.replaceWith(built);
        clearanceSel = built;
        mount(clearanceField.querySelector('label')!, o.clearance.label);
      }
      access.hidden = false;
      syncAccess();
    } catch {
      // Without options the server still applies its defaults (shared, the uploader's own clearance).
    }
  }

  const zone = h(
    'div',
    { class: 'dropzone' },
    h(
      'p',
      null,
      'Drop files or a folder here, or ',
      h('label', { for: input.id, class: 'link-button' }, 'choose files'),
      ' / ',
      h('label', { for: folderInput.id, class: 'link-button' }, 'choose a folder'),
      '.',
    ),
    h('p', { class: 'hint' }, `PDF, Office, text, CSV, JSON, XML … up to ${MAX_MB} MB each. Who can read them is set below.`),
    // Which folder you pick is load-bearing and not at all obvious: a browser only reports the path *below*
    // the folder you choose, so choosing `policies` sends one segment and matches nothing.
    h('p', { class: 'hint' }, 'Pick the top folder (e.g. HR, not HR/UK/policies) — tags come from the folders inside it.'),
    input,
    folderInput,
  );

  const start = (picked: Picked[]) => {
    for (const one of picked) void uploadOne(one);
  };
  const fromInput = (el: HTMLInputElement): Picked[] =>
    Array.from(el.files ?? []).map((file) => ({
      // webkitRelativePath is '' for a plain file pick and '<chosen folder>/…/name' for a folder pick.
      file,
      relativePath: (file as File & { webkitRelativePath?: string }).webkitRelativePath || '',
    }));

  for (const el of [input, folderInput]) {
    el.addEventListener('change', () => {
      start(fromInput(el));
      el.value = '';
    });
  }
  zone.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    zone.classList.add('dragging');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
  zone.addEventListener('drop', (ev) => {
    ev.preventDefault();
    zone.classList.remove('dragging');
    if (ev.dataTransfer) void dropped(ev.dataTransfer).then(start);
  });

  /** Walk a drop, descending into directories so a dropped tree keeps each file's folder path. */
  async function dropped(data: DataTransfer): Promise<Picked[]> {
    const entries = Array.from(data.items)
      .map((item) => (item.kind === 'file' && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
      .filter((e): e is FileSystemEntry => e !== null);
    if (!entries.length) return Array.from(data.files).map((file) => ({ file, relativePath: '' }));
    const out: Picked[] = [];
    const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
      if (entry.isFile) {
        const file = await new Promise<File>((resolve, reject) =>
          (entry as FileSystemFileEntry).file(resolve, reject),
        );
        out.push({ file, relativePath: prefix + entry.name });
        return;
      }
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      for (;;) {
        // readEntries returns at most ~100 at a time and signals the end with an empty batch.
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) return;
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      }
    };
    await Promise.all(entries.map((entry) => walk(entry, '')));
    return out;
  }

  // ---------------------------------------------------------------- facet pickers
  async function loadPickers(): Promise<void> {
    try {
      const { facets } = await api.get<FacetsResponse>('/api/facets');
      const rows = Object.entries(facets).filter(([, f]) => (f.vocabulary ?? []).length > 0);
      if (!rows.length) return;
      mount(
        pickers,
        h('p', { class: 'hint' }, 'Tags are taken from the folder names. Set one here to override it for this upload.'),
        h('div', { class: 'row' }, rows.map(([name, facet]) => pickerFor(name, facet))),
      );
      show(pickers, true);
    } catch {
      // A missing vocabulary is not worth an error banner above a working drop zone: the folder path and the
      // classifier still tag the document, and the admin console can correct it afterwards.
    }
  }

  function pickerFor(name: string, facet: Facet): HTMLElement {
    // The same control the admin access-tag editor uses (tag-controls.ts), so the two offer identical values.
    const select = vocabularySelect({ id: `facet-${name}-${idSuffix}`, name, facet, leading: [{ value: FROM_FOLDER, label: 'from folder' }] });
    selects.set(name, select);
    return h('div', { class: 'field' }, h('label', { for: select.id }, facet.label || name), select);
  }

  /** Only what the uploader actually chose. An untouched picker sends nothing, so the folder still decides. */
  function chosenFacets(): string | null {
    const chosen: Record<string, string[]> = {};
    for (const [name, select] of selects) if (select.value !== FROM_FOLDER) chosen[name] = [select.value];
    return Object.keys(chosen).length ? JSON.stringify(chosen) : null;
  }

  // ---------------------------------------------------------------- one upload
  async function uploadOne({ file, relativePath }: Picked): Promise<void> {
    const state = h('span', { class: 'upload-state' }, 'Uploading…');
    // Three nodes, not two. The tracking id and the stage used to share one, and `mount` replaces children -
    // so the first stage update silently erased the tracking id, about two seconds after showing it. That id
    // is the only durable handle the uploader gets.
    const track = h('div', { class: 'upload-detail muted' });
    const tagLine = h('div', { class: 'upload-detail' });
    const detail = h('div', { class: 'upload-detail' });
    const item = h(
      'li',
      { class: 'upload-item' },
      h('div', { class: 'upload-head' }, h('span', { class: 'upload-name', title: relativePath || file.name }, relativePath || file.name), h('span', { class: 'muted' }, fmtBytes(file.size)), state),
      track,
      tagLine,
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
      if (relativePath) form.append('relative_path', relativePath);
      const chosen = chosenFacets();
      if (chosen) form.append('facets', chosen);
      form.append('only_me', onlyMe.checked ? 'true' : 'false');
      if (!onlyMe.checked && clearanceSel.value) form.append('clearance', clearanceSel.value);
      accepted = await api.request<UploadAccepted>('POST', '/api/uploads', { body: form, timeoutMs: 15 * 60_000 });
    } catch (err) {
      if (isAbortError(err)) return;
      mount(state, statusBadge('FAILED'));
      mount(detail, problemBox(err));
      return;
    }

    mount(tagLine, describeTags(accepted));

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
      if (record.tags) mount(tagLine, describeTags({ ...accepted, facets: record.tags.facets }));
      opts.onFinished?.(record);
    } catch (err) {
      if (isAbortError(err)) return;
      const stopped = err instanceof StoppedWatching ? err : null;
      mount(state, statusBadge(stopped?.lastKnown?.status ?? accepted.status ?? 'QUEUED'));
      mount(detail, h('span', { class: 'muted' }, stopped ? stopped.message : describeUnknown(err)));
    }
  }

  /** Who can read it, as the server decided - which may be narrower than what was picked (see /api/uploads). */
  function audience(accepted: UploadAccepted): HTMLElement | null {
    if (accepted.visibility === 'private') return h('div', { class: 'audience' }, h('strong', null, 'Private to you'));
    const a = accepted.access ?? {};
    const parts = Object.entries(a)
      .filter(([k]) => k !== 'employee_id')
      .map(([k, v]) => `${k} ${Array.isArray(v) ? v.join('/') : v}`);
    return parts.length ? h('div', { class: 'audience' }, 'Visible to ', h('strong', null, parts.join(' · '))) : null;
  }

  /** What got tagged, and where it came from. Says so when a folder produced nothing at all. */
  function describeTags(accepted: UploadAccepted): HTMLElement {
    return h('span', null, tagSummary(accepted), audience(accepted));
  }

  function tagSummary(accepted: UploadAccepted): HTMLElement {
    const facets = accepted.facets ?? {};
    const sources = accepted.facet_sources ?? {};
    const names = Object.keys(facets).sort();
    if (accepted.relative_path && !(accepted.facets_from_path ?? []).length) {
      return h(
        'span',
        { class: 'muted' },
        `No tags matched “${accepted.relative_path}”. `,
        // The single most useful sentence in this widget: it turns a silent non-result into a correction.
        'Path rules read the folders below the one you picked — try picking the department folder instead.',
      );
    }
    if (!names.length) return h('span', { class: 'muted' }, 'No tags yet — the classifier runs during indexing.');
    return h(
      'span',
      { class: 'muted' },
      'Tagged ',
      names.map((name, i) => [
        i ? ', ' : '',
        h('strong', null, `${name}: ${facets[name]?.join(', ')}`),
        sources[name]?.startsWith('path_rule:') ? ' (folder)' : sources[name] === 'uploader' ? ' (you)' : '',
      ]),
    );
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

  if (opts.facetPickers) void loadPickers();
  void loadAccess();
  return h('section', { class: 'upload', 'aria-label': 'Upload documents' }, zone, access, pickers, list);
}
