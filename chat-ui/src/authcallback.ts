// /auth/callback — where Entra returns after an interactive sign-in.
//
// A dedicated page rather than the app's own URL: the admin console owns the fragment for its hash router,
// so letting MSAL hand a code back on /admin#... would collide with it.
import { fetchPublicConfig } from './auth';
import { h, mount } from './dom';
import { createMsal, entraConfig, takeReturnTo } from './entra';

function say(message: string, detail?: string): void {
  const root = document.getElementById('callback') ?? document.body;
  mount(root, h('div', { class: 'card' }, h('p', null, message), detail ? h('p', { class: 'muted' }, detail) : null));
}

async function main(): Promise<void> {
  try {
    const cfg = entraConfig(await fetchPublicConfig());
    if (!cfg) {
      say('This deployment is not configured for Microsoft sign-in.', 'Returning you to the assistant…');
      window.setTimeout(() => window.location.replace('/'), 1500);
      return;
    }
    const msal = await createMsal(cfg);
    const result = await msal.handleRedirectPromise();
    if (result?.account) msal.setActiveAccount(result.account);
    window.location.replace(takeReturnTo());
  } catch (e) {
    say('Sign-in could not be completed.', e instanceof Error ? e.message : String(e));
  }
}

void main();
