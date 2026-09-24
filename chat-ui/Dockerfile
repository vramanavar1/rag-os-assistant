# RAG-OS chat-ui: static chat / embed / admin bundles served by unprivileged nginx on :8080,
# which also reverse-proxies /api/ to the internal API (API_UPSTREAM).

# ---- build: TypeScript -> static bundles ---------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json build.mjs ./
COPY src ./src
COPY public ./public
RUN npm run typecheck && npm run build
# Container scripts: normalise line endings (the repo may be checked out on Windows) + exec bit.
COPY nginx ./nginx
RUN sed -i 's/\r$//' nginx/default.conf.template nginx/entrypoint.d/*.sh \
 && chmod 0644 nginx/default.conf.template \
 && chmod 0755 nginx/entrypoint.d/*.sh

# ---- runtime -------------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.27-alpine

ENV API_UPSTREAM=http://api:8000 \
    EMBED_ORIGINS="" \
    DEV_EMBED_HOST_ENABLED=false

# Static files stay root-owned (read-only for the nginx user).
COPY --from=build /app/dist/ /usr/share/nginx/html/
# Rendered to /etc/nginx/conf.d/default.conf by /docker-entrypoint.d/20-envsubst-on-templates.sh.
COPY --from=build /app/nginx/default.conf.template /etc/nginx/templates/default.conf.template
# Installed as *.envsh: /docker-entrypoint.sh SOURCES those, so the variables it validates and
# derives (EMBED_ORIGINS_CSP, NGINX_RESOLVERS, NGINX_ENVSUBST_FILTER, ...) reach the envsubst step.
COPY --from=build /app/nginx/entrypoint.d/10-embed-origins.sh /docker-entrypoint.d/10-embed-origins.envsh

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
