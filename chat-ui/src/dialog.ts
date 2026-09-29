// A modal <dialog>, opened and disposed of.
//
// Extracted from the admin console's document dialog, which was the only <dialog> in the codebase and was
// hard-wired to one endpoint. It lives here rather than under admin/ because chat.ts and admin.ts are separate
// esbuild entry points: importing from admin/ would pull the whole console into the chat bundle.
import { h, mount, type Child } from './dom';

export interface OpenDialogHandle {
  /** Replace the body - for a dialog that shows a spinner and then its content. */
  setBody(...children: Child[]): void;
  close(): void;
}

let seq = 0;

export function openDialog(title: string, onClose?: () => void): OpenDialogHandle {
  const id = `dialog-title-${++seq}`;
  const body = h('div', { class: 'dialog-body' });
  const closeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Close' }, '×');
  const dialog = h(
    'dialog',
    { class: 'dialog', 'aria-labelledby': id },
    h('div', { class: 'panel-head' }, h('h2', { id }, title), closeBtn),
    body,
  );
  closeBtn.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    // Removed rather than hidden, so a dialog opened twice does not leave the first one in the document.
    dialog.remove();
    onClose?.();
  });
  dialog.addEventListener('click', (ev) => {
    if (ev.target === dialog) dialog.close(); // backdrop click; Escape is the browser's own
  });
  document.body.appendChild(dialog);
  dialog.showModal();
  return {
    setBody: (...children) => mount(body, ...children),
    close: () => dialog.close(),
  };
}
