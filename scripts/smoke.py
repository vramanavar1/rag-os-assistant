"""End-to-end smoke test against a running stack (local compose or Azure), through the public chat-ui URL.

    uv run python scripts/smoke.py --base-url http://localhost:8080                  # dev tokens (DEV_AUTH_ENABLED)
    uv run python scripts/smoke.py --base-url https://<chat-ui fqdn> \
        --token-a "$HR_TOKEN" --token-b "$SALES_TOKEN" --admin-token "$ADMIN_TOKEN" [--api-url https://<rag-api fqdn>]

Checks: health/readiness, security headers, access filtering (principal A sees HR content, B never does),
token usage in responses, upload -> tracking -> INDEXED, admin report, and that the API is not public.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"[SKIP] {name} - {why}")


def dev_token(c: httpx.Client, pid: str) -> str:
    r = c.post("/api/dev/token", json={"principal_id": pid})
    r.raise_for_status()
    return str(r.json()["token"])




def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token-a", help="principal A (expected to see HR/UK content)")
    ap.add_argument("--token-b", help="principal B (Sales/US - must NOT see HR content)")
    ap.add_argument("--admin-token")
    ap.add_argument("--api-url", help="internal API URL that must NOT be reachable publicly")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--same-identity", action="store_true",
                    help="token-a and token-b are the same person (one operator, Entra-only). Skips the checks "
                         "that compare two principals - they would otherwise test an identity against itself and "
                         "report a pass or a failure that means nothing.")
    a = ap.parse_args()

    c = httpx.Client(base_url=a.base_url.rstrip("/"), timeout=60, verify=not a.insecure)
    r = c.get("/healthz")
    check("chat-ui healthz", r.status_code == 200)
    r = c.get("/api/healthz")
    check("api healthz via proxy", r.status_code == 200)
    csp = r.headers.get("content-security-policy", "")
    check("security headers", "nosniff" in r.headers.get("x-content-type-options", "") and bool(csp))
    r = c.get("/api/readyz")
    check("api readyz (embedding profile guard)", r.status_code == 200, r.text[:300] if r.status_code != 200 else "")


    cfg = c.get("/api/public-config").json()
    if not a.token_a and cfg.get("dev_auth_enabled"):
        a.token_a = dev_token(c, "hr-emea")
        a.token_b = a.token_b or dev_token(c, "sales-us")
        a.admin_token = a.admin_token or dev_token(c, "admin")
    if not (a.token_a and a.token_b):
        # Entra-only deployment: two real access tokens are needed, for two people with different attributes.
        #   az account get-access-token --scope "api://<app-id>/access_as_user" --query accessToken -o tsv
        # That command needs the Azure CLI pre-authorised for the scope first, or Entra refuses to consent:
        #   ./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -PreAuthorizeAzureCli
        print("no tokens: pass --token-a/--token-b (Entra access tokens for the API scope), or enable dev auth")
        return 2
    A = {"Authorization": f"Bearer {a.token_a}"}
    B = {"Authorization": f"Bearer {a.token_b}"}
    ADM = {"Authorization": f"Bearer {a.admin_token}"} if a.admin_token else None

    check("unauthenticated chat rejected", c.post("/api/chat", json={"question": "hi"}).status_code == 401)

    q = "How many weeks of paid parental leave do UK employees get?"
    ra = c.post("/api/chat", headers=A, json={"question": q}).json()
    cites_a = [x["path"] for x in ra.get("citations", [])]
    check("principal A answered from HR docs", any(p.startswith("hr/") for p in cites_a), f"citations={cites_a}")
    usage = ra.get("usage", {})
    check("token usage returned", usage.get("embedding", 0) > 0 or usage.get("input", 0) > 0, str(usage))
    if a.same_identity:
        why = "--same-identity: token-a and token-b are the same person"
        skip("principal B never sees HR docs", why)
        skip("facets trimmed for principal B", why)
    else:
        rb = c.post("/api/chat", headers=B, json={"question": q}).json()
        cites_b = [x["path"] for x in rb.get("citations", [])]
        check("principal B never sees HR docs", not any(p.startswith("hr/") for p in cites_b), f"citations={cites_b}")
        fb = c.get("/api/facets", headers=B).json()["facets"]
        depts_b = {v["id"] for v in fb.get("department", {}).get("values", [])}
        check("facets trimmed for principal B", "HR" not in depts_b, str(sorted(depts_b)))

    if ADM is None:
        skip("upload -> INDEXED", "needs an admin/contributor token (--admin-token)")
        skip("admin ingestion report", "needs an admin token (--admin-token)")
    else:
        marker = f"smoke-{uuid.uuid4().hex[:8]}"
        content = f"Smoke test note {marker}: the canary phrase is {marker}.".encode()
        up = c.post("/api/uploads", headers=ADM, files={"file": (f"{marker}.txt", content, "text/plain")})
        if check("upload accepted", up.status_code == 202, up.text[:200]):
            tid = up.json()["tracking_id"]
            status = ""
            deadline = time.time() + a.timeout
            while time.time() < deadline:
                status = c.get(f"/api/uploads/{tid}", headers=ADM).json().get("status", "")
                if status in ("INDEXED", "FAILED"):
                    break
                time.sleep(3)
            check("upload tracked to INDEXED", status == "INDEXED", f"status={status}")

        s = c.get("/api/admin/ingestion/summary", headers=ADM)
        check("admin ingestion report", s.status_code == 200 and "totals" in s.json(), str(s.json().get("totals")))
    if a.same_identity:
        # B is the operator, who may well hold rag.admin - a 403 here would be the wrong result to demand.
        skip("non-admin blocked from report", "--same-identity: no second, non-admin principal to test with")
    else:
        check("non-admin blocked from report", c.get("/api/admin/ingestion/summary", headers=B).status_code == 403)

    if a.api_url:
        try:
            r = httpx.get(a.api_url.rstrip("/") + "/api/healthz", timeout=10, verify=not a.insecure)
            check("internal API not publicly reachable", r.status_code >= 400, f"status={r.status_code}")
        except httpx.HTTPError as e:
            check("internal API not publicly reachable", True, type(e).__name__)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
