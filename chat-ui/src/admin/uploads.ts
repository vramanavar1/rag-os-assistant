// Uploads: the recent-documents list, unscoped. Same component and same endpoint as the chat page's panel -
// the server widens the scope to everyone's documents because the caller holds the admin role.
import { h, mount } from '../dom';
import { createUploadsList } from '../uploads-list';
import { pageHeader, type View, type ViewContext } from './common';

export const uploadsView: View = async (ctx: ViewContext) => {
  const list = createUploadsList(ctx.api, {
    // Inside the console the document dialog is a bare hash away.
    documentHref: (docId) => `#/documents/${encodeURIComponent(docId)}`,
    signal: ctx.signal,
  });
  mount(
    ctx.root,
    pageHeader('Uploads'),
    h('p', { class: 'muted' },
      'Every document, newest first, across all sources. Documents is the same data with filters and search.'),
    list.el,
  );
};
