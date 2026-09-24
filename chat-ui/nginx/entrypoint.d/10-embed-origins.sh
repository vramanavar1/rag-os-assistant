#!/bin/sh
# RAG-OS chat-ui: validate the container environment and derive the variables used by
# /etc/nginx/templates/default.conf.template.
#
# IMPORTANT: the nginx image's /docker-entrypoint.sh *sources* files named *.envsh and merely
# *executes* *.sh files (in a child process, so their exports are lost). The Dockerfile therefore
# installs this file as /docker-entrypoint.d/10-embed-origins.envsh, so the variables exported
# here are visible to 20-envsubst-on-templates.sh, which renders the template.
# Because it is sourced: POSIX sh only, no `set -e/-u` changes, `exit` only for fatal
# misconfiguration, and every helper/variable is prefixed ragui_ to avoid clobbering the caller.
#
# Inputs
#   API_UPSTREAM        required  http(s)://host[:port]   e.g. http://rag-api, http://api:8000
#   EMBED_ORIGINS          optional  comma-separated origins allowed to frame /embed
#                                    e.g. https://intranet.contoso.com,https://*.contoso.com
#   DEV_EMBED_HOST_ENABLED optional  true|false (default false) - serves /dev/embed-host
# Outputs (exported)
#   API_UPSTREAM, API_UPSTREAM_HOST, API_UPSTREAM_PROXY, NGINX_RESOLVERS,
#   EMBED_ORIGINS_CSP, DEV_EMBED_HOST_ENABLED, NGINX_ENVSUBST_FILTER

ragui_log() {
    if [ -z "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ]; then
        echo "10-embed-origins: $*"
    fi
}

ragui_die() {
    echo "10-embed-origins: ERROR: $*" >&2
    exit 1
}

# True when the name resolves through the system resolver (honours resolv.conf search/ndots).
ragui_resolves() {
    if command -v getent >/dev/null 2>&1; then
        if command -v timeout >/dev/null 2>&1; then
            timeout 3 getent hosts "$1" >/dev/null 2>&1
        else
            getent hosts "$1" >/dev/null 2>&1
        fi
    else
        nslookup "$1" >/dev/null 2>&1
    fi
}

# 20-envsubst only logs (and nginx would start with the stock config, without the API proxy)
# when it cannot write the rendered file - fail fast instead.
if [ -d /etc/nginx/templates ] && [ ! -w "${NGINX_ENVSUBST_OUTPUT_DIR:-/etc/nginx/conf.d}" ]; then
    ragui_die "${NGINX_ENVSUBST_OUTPUT_DIR:-/etc/nginx/conf.d} is not writable (mount an emptyDir there when using a read-only root filesystem)"
fi

# ------------------------------------------------------------------ API_UPSTREAM
ragui_up="${API_UPSTREAM:-}"
ragui_up="${ragui_up%/}"
case "$ragui_up" in
    http://*)  ragui_scheme=http;  ragui_hostport="${ragui_up#http://}" ;;
    https://*) ragui_scheme=https; ragui_hostport="${ragui_up#https://}" ;;
    *) ragui_die "API_UPSTREAM must be http(s)://host[:port] (got '${API_UPSTREAM:-}')" ;;
esac
# host (DNS name, IPv4 or [IPv6]) + optional port; no path, query, credentials or whitespace
if ! printf '%s' "$ragui_hostport" | grep -Eq '^([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?$'; then
    ragui_die "API_UPSTREAM must be http(s)://host[:port] without a path (got '${API_UPSTREAM:-}')"
fi
case "$ragui_hostport" in
    \[*\]:*) ragui_host="${ragui_hostport%:*}"; ragui_port=":${ragui_hostport##*:}" ;;
    \[*\])   ragui_host="$ragui_hostport";      ragui_port="" ;;
    *:*)     ragui_host="${ragui_hostport%:*}"; ragui_port=":${ragui_hostport##*:}" ;;
    *)       ragui_host="$ragui_hostport";      ragui_port="" ;;
esac

# nginx's runtime resolver ignores /etc/hosts and resolv.conf search domains. Compensate:
#  1. names pinned in /etc/hosts (e.g. --add-host host.docker.internal:host-gateway) -> use the IP
#  2. single-label names (Container Apps "rag-api", k8s services) -> qualify with the first search
#     domain under which the name resolves now; otherwise keep it (Docker's embedded DNS resolves
#     compose service names without search domains).
ragui_target="$ragui_host"
ragui_ip=$(awk -v h="$ragui_host" '
    $1 !~ /^#/ { for (i = 2; i <= NF; i++) if ($i == h) { if ($1 ~ /:/) { if (v6 == "") v6 = $1 } else { print $1; found = 1; exit } } }
    END { if (!found && v6 != "") print "[" v6 "]" }' /etc/hosts 2>/dev/null || true)
if [ -n "$ragui_ip" ]; then
    ragui_target="$ragui_ip"
    ragui_log "API host '$ragui_host' is pinned in /etc/hosts -> $ragui_ip"
else
    case "$ragui_host" in
        *.*|\[*) : ;;
        *)
            ragui_qualified=""
            for ragui_dom in $(awk '$1 == "search" || $1 == "domain" { for (i = 2; i <= NF; i++) print $i }' /etc/resolv.conf 2>/dev/null); do
                if ragui_resolves "$ragui_host.$ragui_dom"; then
                    ragui_qualified="$ragui_host.$ragui_dom"
                    break
                fi
            done
            if [ -n "$ragui_qualified" ]; then
                ragui_target="$ragui_qualified"
                ragui_log "API short name '$ragui_host' qualified as '$ragui_target' (nginx resolver does not apply search domains)"
            else
                ragui_log "API short name '$ragui_host' kept as-is (fine for Docker's embedded DNS; use an FQDN if requests fail with 502)"
            fi
            ;;
    esac
fi

# ------------------------------------------------------------------ resolvers
ragui_resolvers=$(awk '$1 == "nameserver" && $2 !~ /%/ { if ($2 ~ /:/) printf "[%s] ", $2; else printf "%s ", $2 }' /etc/resolv.conf 2>/dev/null || true)
ragui_resolvers="${ragui_resolvers% }"
if [ -z "$ragui_resolvers" ]; then
    ragui_resolvers="127.0.0.11"
    ragui_log "no nameserver in /etc/resolv.conf; defaulting resolver to $ragui_resolvers"
fi

# ------------------------------------------------------------------ EMBED_ORIGINS -> CSP source list
ragui_csp=""
ragui_ifs="$IFS"
set -f
IFS=','
for ragui_o in ${EMBED_ORIGINS:-}; do
    ragui_o=$(printf '%s' "$ragui_o" | tr -d ' \t\r\n')
    ragui_o="${ragui_o%/}"
    if [ -z "$ragui_o" ]; then
        continue
    fi
    if printf '%s' "$ragui_o" | grep -Eq '^https?://(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$'; then
        ragui_csp="$ragui_csp $ragui_o"
    else
        ragui_log "WARNING: ignoring invalid EMBED_ORIGINS entry '$ragui_o' (expected scheme://host[:port])"
    fi
done
IFS="$ragui_ifs"
set +f
ragui_csp="${ragui_csp# }"

# ------------------------------------------------------------------ DEV_EMBED_HOST_ENABLED
case "$(printf '%s' "${DEV_EMBED_HOST_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) ragui_dev=true ;;
    *)             ragui_dev=false ;;
esac

export API_UPSTREAM="$ragui_scheme://$ragui_hostport"
export API_UPSTREAM_HOST="$ragui_hostport"
export API_UPSTREAM_PROXY="$ragui_scheme://$ragui_target$ragui_port"
export NGINX_RESOLVERS="$ragui_resolvers"
export EMBED_ORIGINS_CSP="$ragui_csp"
export DEV_EMBED_HOST_ENABLED="$ragui_dev"
# Substitute ONLY these names in the templates (nginx's own $variables stay intact).
export NGINX_ENVSUBST_FILTER='^(API_UPSTREAM|API_UPSTREAM_HOST|API_UPSTREAM_PROXY|NGINX_RESOLVERS|EMBED_ORIGINS_CSP|DEV_EMBED_HOST_ENABLED)$'

ragui_log "API upstream: $API_UPSTREAM_PROXY (Host: $API_UPSTREAM_HOST); resolvers: $NGINX_RESOLVERS"
ragui_log "frame-ancestors for /embed: 'self'${EMBED_ORIGINS_CSP:+ $EMBED_ORIGINS_CSP}; dev embed host: $DEV_EMBED_HOST_ENABLED"

unset ragui_up ragui_scheme ragui_hostport ragui_host ragui_port ragui_target ragui_ip ragui_qualified \
      ragui_dom ragui_resolvers ragui_csp ragui_ifs ragui_o ragui_dev
unset -f ragui_log ragui_die ragui_resolves
