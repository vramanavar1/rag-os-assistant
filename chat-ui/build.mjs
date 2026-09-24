// Bundles the TypeScript entry points with esbuild and copies the static pages into dist/.
//
//   dist/index.html, embed.html, admin.html, dev-portal.html   <- public/*.html
//   dist/assets/{chat,admin,devportal}.js, dist/assets/styles.css
//   dist/embed/loader.js                                        <- runs inside the Customer Portal page
//
// Usage: node build.mjs [--watch] [--dev]
import * as esbuild from 'esbuild';
import { cp, mkdir, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(root, 'dist');
const watch = process.argv.includes('--watch');
const dev = watch || process.argv.includes('--dev');

/** @type {import('esbuild').BuildOptions} */
const common = {
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2022', 'chrome111', 'edge111', 'firefox115', 'safari16.4'],
  minify: !dev,
  sourcemap: dev ? 'linked' : false,
  legalComments: 'none',
  charset: 'utf8',
  logLevel: 'info',
  define: { 'process.env.NODE_ENV': JSON.stringify(dev ? 'development' : 'production') },
};

const builds = [
  {
    entryPoints: {
      chat: 'src/chat.ts',
      admin: 'src/admin.ts',
      authcallback: 'src/authcallback.ts',
      devhost: 'src/devhost.ts',
    },
    outdir: path.join(dist, 'assets'),
  },
  // The embed loader must stay tiny and dependency-free (no MSAL): it runs in someone else's page.
  { entryPoints: { loader: 'src/loader.ts' }, outdir: path.join(dist, 'embed') },
];

async function copyStatic() {
  await mkdir(path.join(dist, 'assets'), { recursive: true });
  for (const name of await readdir(path.join(root, 'public'))) {
    const src = path.join(root, 'public', name);
    if (name.endsWith('.html')) await cp(src, path.join(dist, name));
    else if (name.endsWith('.css')) await cp(src, path.join(dist, 'assets', name));
  }
}

await rm(dist, { recursive: true, force: true });
await copyStatic();

if (watch) {
  const contexts = await Promise.all(builds.map((b) => esbuild.context({ ...common, ...b, absWorkingDir: root })));
  await Promise.all(contexts.map((c) => c.watch()));
  console.log('watching src/ (public/ is copied once at start; restart to pick up HTML/CSS changes)');
} else {
  await Promise.all(builds.map((b) => esbuild.build({ ...common, ...b, absWorkingDir: root })));
  console.log(`built ${path.relative(process.cwd(), dist) || 'dist'} (${dev ? 'development' : 'production'})`);
}
