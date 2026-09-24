// Markdown-lite renderer for answers: paragraphs, bullet/numbered lists, **bold**, *italic*,
// `inline code`, ``` fenced blocks and citation markers [1] / [1, 2].
//
// Safety model: the ENTIRE input is HTML-escaped first; formatting rules then only wrap
// already-escaped text in a fixed set of tags whose attributes are constants or digits.
// Nothing user/model-controlled can introduce markup, attributes or URLs.

const ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPES[c]!);
}

export interface RenderOptions {
  /** Citation indices that exist for this answer; other [n] markers stay plain text. */
  citations?: ReadonlySet<number>;
}

function citationChips(listText: string, opts: RenderOptions): string | null {
  const nums = listText.split(',').map((n) => Number.parseInt(n.trim(), 10));
  if (nums.some((n) => !Number.isInteger(n) || !opts.citations?.has(n))) return null;
  return nums
    .map((n) => `<button type="button" class="cite" data-cite="${n}" aria-label="Show source ${n}">${n}</button>`)
    .join('');
}

// Input to the helpers below is already escaped.
function emphasis(s: string, opts: RenderOptions): string {
  let out = s.replace(/\*\*(?=\S)([^*]*?\S)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?![*\w])/g, '$1<em>$2</em>');
  out = out.replace(/\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g, (m, list: string) => citationChips(list, opts) ?? m);
  return out;
}

function inline(s: string, opts: RenderOptions): string {
  // Split out `code` spans first so their contents are not formatted.
  return s
    .split(/(`[^`\n]+`)/g)
    .map((part, i) => (i % 2 === 1 ? `<code>${part.slice(1, -1)}</code>` : emphasis(part, opts)))
    .join('');
}

type ListState = { tag: 'ul' | 'ol'; start: number; items: string[] };

export function renderMarkdown(source: string, opts: RenderOptions = {}): string {
  const lines = escapeHtml(source.replace(/\r\n?/g, '\n')).split('\n'); // escape FIRST
  const out: string[] = [];
  let para: string[] = [];
  let list: ListState | null = null;
  let code: string[] | null = null;

  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map((l) => inline(l.trim(), opts)).join('<br>')}</p>`);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const start = list.tag === 'ol' && list.start !== 1 ? ` start="${list.start}"` : '';
    out.push(`<${list.tag}${start}>${list.items.map((i) => `<li>${inline(i, opts)}</li>`).join('')}</${list.tag}>`);
    list = null;
  };

  for (const line of lines) {
    if (code) {
      if (/^\s*```/.test(line)) {
        out.push(`<pre><code>${code.join('\n')}</code></pre>`);
        code = null;
      } else {
        code.push(line);
      }
      continue;
    }
    if (/^\s*```/.test(line)) {
      flushPara();
      flushList();
      code = [];
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    const bullet = /^\s*[-*+•]\s+(.*)$/.exec(line);
    const numbered = /^\s*(\d{1,4})[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? 'ul' : 'ol';
      const current = list as ListState | null;
      if (current && current.tag !== tag) flushList();
      if (!list) list = { tag, start: numbered ? Number.parseInt(numbered[1]!, 10) : 1, items: [] };
      (list as ListState).items.push(bullet ? bullet[1]! : numbered![2]!);
      continue;
    }
    const heading = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      flushPara();
      flushList();
      out.push(`<p class="md-heading"><strong>${inline(heading[1]!, opts)}</strong></p>`);
      continue;
    }
    const current = list as ListState | null;
    if (current && /^\s{2,}\S/.test(line)) {
      // indented continuation of the previous list item
      current.items[current.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    flushList();
    para.push(line);
  }
  if (code) out.push(`<pre><code>${(code as string[]).join('\n')}</code></pre>`);
  flushPara();
  flushList();
  return out.join('');
}

/** Render into an element. The only innerHTML sink in the app; input is escaped by renderMarkdown. */
export function setMarkdown(el: HTMLElement, source: string, opts: RenderOptions = {}): void {
  el.innerHTML = renderMarkdown(source, opts);
}
