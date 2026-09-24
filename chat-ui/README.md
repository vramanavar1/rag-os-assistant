# RAG-OS chat UI

Static front end, served by unprivileged nginx on port 8080. nginx also reverse-proxies `/api/` to the internal RAG-OS API, so the API itself is never exposed publicly.

| Path | What it is |
|---|---|
| `/` | Standalone chat. Signs in with Microsoft Entra ID (or a dev principal locally). |
| `/auth/callback` | Where Entra returns after sign-in. A dedicated page so it never collides with the admin console's hash router. |
| `/embed` | Compact chat for an iframe. It gets its token from the host page via `postMessage`. |
| `/embed/loader.js` | The script a host page includes to embed the chat. It is served cross-origin and cached for 5 minutes. |
| `/admin` | Console for ingestion and governance (dashboard, runs, documents, dead letters, sources, review queue, controls, config) |
| `/dev/embed-host` | Mock host page that demonstrates the loader. It is only served when `DEV_EMBED_HOST_ENABLED=true`. |
| `/healthz` | nginx liveness/readiness (200 `ok`) |

The front end is plain TypeScript with no framework, bundled by esbuild. Its one runtime dependency is `@azure/msal-browser`, which handles the Entra authorization-code + PKCE flow; the embed loader is built separately and stays free of it. Pages have no inline scripts or styles, which is required by the CSP.

## Build

```sh
npm ci
npm run typecheck
npm run build        # -> dist/   (npm run watch for development)
docker build -t rag-chat-ui:dev .
```

## Run locally

```sh
docker run --rm -p 8080:8080 \
  -e API_UPSTREAM=http://host.docker.internal:8000 \
  -e DEV_EMBED_HOST_ENABLED=true \
  rag-chat-ui:dev
# Linux: add --add-host host.docker.internal:host-gateway
```

Once it is running, open <http://localhost:8080/>, <http://localhost:8080/admin> and <http://localhost:8080/dev/embed-host>.

| Variable | Default | Meaning |
|---|---|---|
| `API_UPSTREAM` | `http://api:8000` | Internal API base, in the form `http(s)://host[:port]` with no path. The `/api` prefix is kept when proxying. |
| `EMBED_ORIGINS` | *(empty)* | Comma-separated origins that may frame `/embed` (CSP `frame-ancestors`). Wildcard hosts such as `https://*.contoso.com` are allowed. |
| `DEV_EMBED_HOST_ENABLED` | `false` | Serves `/dev/embed-host` |

The entrypoint script (`nginx/entrypoint.d/10-embed-origins.sh`, installed as `*.envsh`) validates these values and renders `nginx/default.conf.template`. Container start fails if `API_UPSTREAM` is malformed.

## Sign-in

There is deliberately **no Entra configuration on this container**. The UI reads the tenant, client id and API scope from `GET /api/public-config` at start-up, so the identity settings live in one place (the API) and the image is environment-agnostic.

The app registration needs the SPA redirect URI `https://<chat-ui-host>/auth/callback` (and `http://localhost:8080/auth/callback` for local runs). The CSP allows `https://login.microsoftonline.com` in `connect-src`, `frame-src` and `form-action` for the token exchange and silent renewal; MSAL itself is bundled locally, so `script-src` stays `'self'`.

## Embed in another page

```html
<div id="assistant"></div>
<script src="https://<chat-ui-host>/embed/loader.js"
        data-target="#assistant"
        data-token-endpoint="/api/rag-os-token"
        data-height="640"></script>
```

- The loader calls `data-token-endpoint` with `credentials: 'include'`. This is the host application's own backend. It must return `{"token": "<Entra access token for the RAG-OS API scope>", "expires_in": <seconds>}` — the API only trusts Entra, so the host has to pass a token it acquired for `api://<app-id>/access_as_user` (on-behalf-of, or its own signed-in user's token).
- The loader posts `{type:'rag-os:token', token, expiresAt}` to the iframe, using the exact chat-ui origin as the target. It refreshes the token about 60 s before expiry, and again whenever the iframe asks with `{type:'rag-os:token-request'}` (for example after a 401).
- The embedded page accepts a token only from origins in the API's `GET /api/public-config` → `embed_origins`, plus its own origin. It keeps the token in memory only.
- The host origin has to be allowed in two places: `EMBED_ORIGINS` on this container, which covers framing, and the API's `EMBED_ORIGINS`, which covers `postMessage`.
- The host page's own CSP must allow the chat-ui origin in both `script-src` and `frame-src`.
- For programmatic use, call `window.RagOsEmbed.mount({target, tokenEndpoint, height})`. It returns `{destroy, refresh}`.

## Azure Container Apps notes

- The chat-ui app uses external ingress with target port 8080. The API app uses internal ingress only.
- Set `API_UPSTREAM=http://<api-app-name>`. nginx's runtime resolver ignores resolv.conf search domains, so at start-up the script qualifies a short name with the first search domain it resolves under. The `Host` header stays the name you configured. Alternatively, use the internal FQDN.
- Configure liveness and readiness probes as HTTP GET `/healthz` on port 8080. The Dockerfile `HEALTHCHECK` only applies to plain Docker.
- With a read-only root filesystem, mount writable volumes at `/etc/nginx/conf.d` and `/tmp`.
