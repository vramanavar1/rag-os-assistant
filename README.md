# RAG-OS: Knowledge Assistant

RAG-OS answers questions **only from your organisation's documents**, and **only from the documents the person
asking is allowed to see**. Every answer cites its sources and reports the tokens it used. It runs on Microsoft
Foundry and Azure Container Apps. Documents are embedded with a **self-hosted open-weights model**, so there is
no per-token embedding cost.

> Local demo in about 15 minutes: [Quick start](#7-quick-start-local-about-15-minutes). Deploy to Azure:
> [Deploy](#8-deploy-to-azure-short-path), then the step-by-step [Deployment.md](Deployment.md).

## Table of contents
1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Deployment topology](#3-deployment-topology)
4. [How it works](#4-how-it-works)
5. [Technology stack](#5-technology-stack)
6. [Repository structure](#6-repository-structure)
7. [Quick start (local)](#7-quick-start-local-about-15-minutes)
8. [Deploy to Azure](#8-deploy-to-azure-short-path)
    * [What you configure before deploying](#what-you-configure-before-deploying)
9. [Configuration at a glance](#9-configuration-at-a-glance)
    * [What RAG-OS reads from a document](#what-rag-os-reads-from-a-document)
    * [When a document stays unclassified](#when-a-document-stays-unclassified)
    * [The review queue: why a document is there, and what it changes](#the-review-queue-why-a-document-is-there-and-what-it-changes)
10. [Using the product](#10-using-the-product)
    * [Deleting documents and resetting all data](#deleting-documents-and-resetting-all-data)
11. [User query handling scenarios](#11-user-query-handling-scenarios)
    * [Conflict handling: two sources that disagree](#conflict-handling-two-sources-that-disagree)
12. [Security model](#12-security-model)
    * [Where a caller's attributes come from](#where-a-callers-attributes-come-from)
    * [Granting someone a role](#granting-someone-a-role)
    * [Why `config/dev/principals.yaml` exists](#why-configdevprincipalsyaml-exists)
    * [Giving HR or Sales access to hundreds of people](#giving-hr-or-sales-access-to-hundreds-of-people)
    * [What `clearance: 0, 1, 2` signifies](#what-clearance-0-1-2-signifies)
    * [How the access policy works](#how-the-access-policy-works)
    * [Every key you can configure](#every-key-you-can-configure)
    * [Defining access by role](#defining-access-by-role)
    * [Best practices, and the minimum you need](#best-practices-and-the-minimum-you-need)
13. [Entra user attributes and claims](#13-entra-user-attributes-and-claims)
14. [Observability](#14-observability)
15. [Scaling and operations](#15-scaling-and-operations)
16. [Testing](#16-testing)
17. [Troubleshooting / FAQ](#17-troubleshooting--faq)
18. [Roadmap, contributing, licences](#18-roadmap-contributing-licences)

---

## 1. Overview

| Capability | What you get |
|---|---|
| **Grounded answers** | Answers come only from retrieved passages and cite them as `[n]`. Uncited answers are refused. Content inside documents is treated as data, never as instructions. |
| **Access filtering on any attribute** | Department, Region, EmployeeID, Clearance… or anything you add in YAML. The filter is applied *inside* the search, never afterwards. Default-deny. |
| **Taxonomy and ontology for SMEs** | Controlled vocabularies (facets) with hierarchies and synonyms, folder rules, a manifest, automatic classification and a review queue. See the [classification guide](docs/classification-guide.md). |
| **Many sources, many formats** | Sources are built by a factory from `sources.yaml`: local folders, Azure Blob and uploads (SharePoint, Google Drive and FTP are registered as stubs). Formats: pdf, docx, txt/md, csv, xlsx, xls, json, jsonl, jsonp, xml. |
| **Self-hosted embeddings** | Qwen3-Embedding-0.6B served by Hugging Face TEI in your own containers. An *embedding profile* guarantees that ingestion and queries use the identical model, revision, dimensions and prefixes. |
| **Scales to millions of documents** | Queue-driven workers scale 0..N on their own compute, separate from the query path. Processing is idempotent, runs through a per-document state machine and has dead-letter handling. |
| **Status for every document** | A PostgreSQL-backed report: progress per run, throughput, ETA, error breakdown, timelines, retry and CSV export. |
| **Observability** | Correlation IDs from the browser to the worker, OpenTelemetry to App Insights, and token usage on every answer. |
| **Embeddable** | One `<script>` tag embeds the chat in an intranet or portal page. The host passes an Entra access token, from which the user's attributes are read. |

## 2. Architecture

![Figure 1 — System architecture](docs/diagrams/architecture.svg)

*Figure 1 — System architecture. Bigger, with a navigation sidebar, copy buttons and print styling, in [README.html](README.html).*

**Why queries stay fast while millions of documents ingest:**
1. The query and ingestion paths run on **separate compute** (different workload profiles).
2. Each path has **its own embedding pool**, so queries never wait behind document batches.
3. **Queue back-pressure** limits the load: KEDA `maxReplicas` × worker concurrency caps writes to Search and PostgreSQL.
4. **Search is protected** by ≥2 replicas, batched writes with a concurrency cap and adaptive backoff. `Scale-SearchReplicas.ps1` adds replicas for backfills.
5. A **priority lane** keeps interactive uploads ahead of bulk backfills.
6. **Operator controls** pause or throttle ingestion from the admin page.

The load test (see [Testing](#16-testing)) measures the effect directly.

**Clean Architecture** (dependencies point inward): `domain` (pure models and rules) ← `application` (ports, services, use
cases) ← `infrastructure` (adapters chosen by **factory registries**) and `api` / `worker` / `cli` (drivers).
`composition.py` is the single place where adapters are chosen by name from configuration.

## 3. Deployment topology

![Figure 2 — Azure deployment topology](docs/diagrams/deployment.svg)

*Figure 2 — Azure deployment topology.*

* Only **`rag-chat-ui`** is public. It serves the widget and admin pages and reverse-proxies `/api/*` to the internal API.
* **Two of the seven workloads are conditional.** `rag-embed-query` and `rag-embed-ingest` serve
  the self-hosted embedding model and exist only when `EmbeddingProfile` selects a `provider: tei`
  profile — they are tagged **tei only** in the figures. An `azure_openai` profile deploys
  `text-embedding-3-small` on Foundry instead and neither pool is created; `rag-ingest-worker` still
  does the embedding, by calling that deployment. Full breakdown in
  [Deployment.md](Deployment.md#what-you-end-up-running-and-what-you-no-longer-need).
* All services use the **managed identity** — Storage shared keys, Service Bus local auth, Search API keys,
  the ACR admin user and PostgreSQL password auth are all disabled, so it is the only way in. (One
  exception: Application Insights ingestion is key-authenticated.) The two secrets live in **Key Vault** and
  reach the apps as `keyvaultref:` secrets.
* Diagram sources: [`docs/diagrams/`](docs/diagrams). A version of this README with bigger diagrams,
  a navigation sidebar and print styling is in [README.html](README.html).

## 4. How it works

### A question

![Figure 3 — Asking a question](docs/diagrams/query-sequence.svg)

*Figure 3 — Asking a question.*

### A document

![Figure 4 — Document lifecycle](docs/diagrams/document-lifecycle.svg)

*Figure 4 — Document lifecycle.*

Unchanged documents cost one state lookup: no queue message and no embedding. Tag-only changes (for example a
manifest edit) merge the new fields into existing chunks without re-embedding.

## 5. Technology stack

| Component | Choice | Purpose |
|---|---|---|
| Language / API | Python 3.13, FastAPI, pydantic v2, uv | API, worker and CLI in one codebase. OpenAPI at `/api/docs`. |
| Embeddings | **Hugging Face TEI + Qwen3-Embedding-0.6B** (Apache-2.0) | Self-hosted, multilingual, 1024 dimensions, no token cost. Azure OpenAI embeddings are a configurable alternative. |
| Answer LLM | **Claude on Microsoft Foundry** (`claude-opus-5`) or **Azure OpenAI GPT** | Chosen per purpose (`LLM_ANSWER`, `LLM_UTILITY`). Keyless Entra auth. |
| Vector and keyword search | **Azure AI Search** (hybrid, semantic ranker, int8 quantization) | Security filters applied inside the query. |
| Queue | **Azure Service Bus** (priority and bulk lanes, DLQ) | Reliable, idempotent ingestion; KEDA scaling. |
| State and reporting | **PostgreSQL Flexible Server** (Entra auth) | Per-document status, events, runs, report queries at millions of rows. |
| Files and configuration | **Blob Storage** | Raw documents (claim-check), versioned YAML configuration, CSV exports. |
| Hosting | **Azure Container Apps** (workload profiles, serverless T4 GPU) | Separate query and ingestion compute; scale to zero. |
| Secrets | **Key Vault** + user-assigned managed identity | No secrets in code, images or files. |
| Observability | **OpenTelemetry → Application Insights** | Traces, logs and `rag.tokens` metrics. |
| Chat UI | TypeScript + nginx + MSAL | Chat with Entra sign-in, loader, admin console, dev embed host. |
| Provisioning | **az CLI in PowerShell 7** | `infra/scripts/00…10`. Idempotent. |

## 6. Repository structure

```
src/rag_os/
  domain/            pure models and rules: documents + state machine, access policy, facets, embedding profile
  application/
    ports.py         interfaces: DocumentSource, DocumentParser, EmbeddingProvider, LlmProvider, SearchIndex,
                     MessageQueue, IngestionStateStore, RawDocumentStore, ConfigRepository, Classifier, Retriever
    services/        AccessPolicyEngine, ClaimsMapper, TagResolver, ProfileGuard, chunker, prompts, index schema
    use_cases/       DiscoverSource, ProcessItem, AnswerQuery, SchedulerTick/Reconcile
  infrastructure/    adapters, each registered in a factory registry (registry.py)
    sources/ parsers/ embeddings/ llm/ search/ queue/ state/ storage/ classifier/ auth/ telemetry/
  api/               FastAPI app, routers, middleware (correlation id, security headers), problem+json errors
  worker/            queue-driven ingestion worker
  composition.py     composition root (the only place adapters are selected by name)
  cli.py             rag-os command line
chat-ui/             nginx container: chat + MSAL sign-in, loader.js, admin console, dev embed host
embedder/            TEI image with the embedding model baked in (pinned revision)
migrations/          Alembic migrations for the state schema (applied by the rag-bootstrap job)
config/              sources, access policy, facets, path rules, embedding profiles, dev principals (YAML)
infra/               az CLI provisioning scripts (PowerShell 7), Container Apps YAML templates, env settings
samples/corpus/      demo documents across departments, regions and formats (+ manifest.csv, sidecar)
scripts/             smoke.py, loadtest.py, make_sample_corpus.py
tests/               unit + offline integration tests (pytest, hypothesis)
docs/                classification guide, KQL snippets, diagram sources (SVG)
README.html          the same document with larger diagrams, a sidebar and print styling
```

## 7. Quick start (local, about 15 minutes)

**Prerequisites:** Docker Desktop, PowerShell 7, [uv](https://docs.astral.sh/uv/) and about 4 GB free RAM.

```powershell
git clone <this repo>; cd rag-os-assistant
./tasks.ps1 up        # builds and starts postgres, tei (self-hosted embeddings), api, worker, chat-ui
./tasks.ps1 logs -Service tei    # first start downloads the model (~1.2 GB); wait for "Ready"
./tasks.ps1 seed      # ingest samples/corpus (10 documents, all formats)
```

| Open | What you'll see |
|---|---|
| http://localhost:8080/dev/embed-host | A mock embedding page. Pick **Priya (HR, UK)** and **Marcus (Sales, US)** and ask *"How many weeks of paid parental leave do UK employees get?"*. Priya gets a cited answer; Marcus's side never shows HR documents. |
| http://localhost:8080/ | The standalone chat. Try the facet filters and look at the token-usage footer. |
| http://localhost:8080/admin | Ingestion report, runs, documents, retries, review queue and configuration editor. Sign in as **Alex (Platform admin)**. |
| http://localhost:8080/api/docs | OpenAPI (Swagger UI). |

The local stack uses an **offline extractive responder** as the LLM (`LLM_ANSWER=fake`). To use a real model, put
`AOAI_ENDPOINT` / `AOAI_CHAT_DEPLOYMENT` (or `CLAUDE_FOUNDRY_RESOURCE`) and `LLM_ANSWER=aoai|claude` in a `.env`
file next to `docker-compose.yml`. Add an `AOAI_API_KEY` / `CLAUDE_API_KEY` for local use, or run the API with
`./tasks.ps1 dev-api` after `az login`.

Without Docker, `./tasks.ps1 dev-api` and `./tasks.ps1 dev-worker` run the API and worker with uv, SQLite and a
PostgreSQL-free SQL queue. You still need a TEI server on `:8081`.

> Corporate networks with TLS inspection: uv needs `--native-tls`. `tasks.ps1` sets `UV_NATIVE_TLS=1`.

## 8. Deploy to Azure (short path)

```powershell
Copy-Item infra/env/dev.sample.psd1 infra/env/dev.psd1   # fill in subscription, region, prefix, Entra settings
./infra/scripts/provision-all.ps1 -Env dev               # 00 prereqs … 08 bootstrap (idempotent, re-runnable)
./infra/scripts/09-smoke.ps1 -Env dev                    # end-to-end checks through the public URL
./infra/scripts/Test-Connectivity.ps1 -Env dev           # every network hop, if something cannot reach something
./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev     # do documents and queries use the same embedding model?
```

When it finishes, the script prints the chat URL (`https://rag-chat-ui.<env-domain>`), the admin console and the
dev embed host (if enabled). **Every setting, secret and verification command, in order, is in [Deployment.md](Deployment.md).**
Also read its region checklist: serverless T4 GPU, AI Search S1 with semantic ranker, and Foundry model quota.

### What you configure before deploying

**`-Env dev` is how you pass your subscription id.** It selects `infra/env/dev.psd1`, and every subscription,
tenant, region, sizing and sign-in value lives in that one file — no script takes them on the command line.

```powershell
@{
    SubscriptionId = '<your-subscription-guid>'
    TenantId       = '<your-tenant-guid>'
    Env            = 'dev'                          # must equal the file name
    EntraTenantId  = '<your-tenant-guid>'           # the four Entra values come from one app
    EntraClientId  = '<app-id>'                     #   registration and need no deployed URL,
    EntraAudience  = 'api://<app-id>'               #   so filling them in now saves a redeploy
    EntraApiScope  = 'api://<app-id>/access_as_user'
}
```

That is a complete file. `dev.sample.psd1` is **also the defaults file**, so ~80 other keys fall back to it and
anything you misspell is reported as a typo.

**You do not name resources.** All 14 names derive from `Prefix` + `Env` plus a deterministic 5-character hash —
`rg-ragos-dev`, `kv-ragosdev-a1b2c`, `stragosdeva1b2c` and so on
([Figure 2](#3-deployment-topology) shows every pattern). `NameOverrides` exists only for matching a corporate
standard. The things people actually forget are the four `Entra*` values: nothing validates them, so a deployment
with them empty succeeds and then nobody can sign in.

Full reference, including every validation error and what it means, is in
[Deployment.md section 2](Deployment.md#2-fill-in-infraenvdevpsd1).

## 9. Configuration at a glance

| File | Controls | Minimal example |
|---|---|---|
| `config/sources/sources.yaml` | Source **instances** (many per type): type, schedule, lane, defaults | `- {id: hr-share, type: local_folder, lane: bulk, settings: {root: D:/KB/HR}, defaults: {acl: {department: [HR]}}}` |
| `config/access-policy/access-policy.yaml` | Attributes, match rules, claims per issuer, [value maps](#giving-hr-or-sales-access-to-hundreds-of-people), roles | `- {name: cost_center, field: acl_cost_center, match: any_of, claims: {entra: extension_CostCenter}}` |
| `config/classification/facets.yaml` | Controlled vocabularies, hierarchies, synonyms, auto-classify | `- {name: doc_type, field: f_doc_type, classify: true, values: [{id: Policy}]}` |
| `config/classification/path-rules.yaml` | Folder conventions → facets + ACL | `- {glob: "hr/**", facets: {department: [HR]}, acl: {department: [HR]}}` |
| `config/embedding/profiles.yaml` | Embedding contract (model, revision, dimensions, prefixes, chunking) | `EMBEDDING_PROFILE=qwen3-0.6b-1024` |
| `manifest.csv` / `<file>.meta.json` | Per-document tags/ACL (bulk, or exceptions) | `path,facet.doc_type,acl.department` |
| Environment variables | Endpoints, LLM choice, limits (all listed in [Deployment.md](Deployment.md#5-application-settings)) | `LLM_ANSWER=claude` |
| `infra/env/<env>.psd1` | Subscription, region, prefix, SKUs, capacities, replicas (resource **names are derived**, not set) | `SearchReplicas = 2` |

Edit configuration in the admin console (validated, ETag-protected, history kept) or upload the YAML to the
`config` container. Replicas pick up changes within 60 seconds. A new access attribute adds its index field
in place.

**Organising and classifying hundreds of thousands of documents.** Facets (department, region, document type,
topic, confidentiality, language…) are assigned in layers, cheapest first. Rules and manifests cover
most documents for nothing; the classifier only fills what is still empty, using the vectors the self-hosted
model already produced; a human decides the rest. The full guide is in
[docs/classification-guide.md](docs/classification-guide.md).

#### What RAG-OS reads from a document

Almost everything is decided **before the document is opened**. Where it sits and what sits beside it do the
work; only one signal looks inside, and only for the facets the rules left empty.

| Signal | Read from | What it can set |
|---|---|---|
| **The source** | `defaults:` on the instance in `sources.yaml` | Anything — the weakest layer, overridden by everything below. |
| **The folder path** | The *source-relative* path, matched by the globs in `path-rules.yaml` | department, region, document type, and their ACL twins. This is where a naming convention pays off. |
| **The filename** | No rule reads it. But when a parser finds no title, the filename *becomes* the title. | Indirectly document type and topic — the title is embedded into every chunk and fed to the classifier. It is also searchable, so it affects keyword relevance. |
| **A sidecar** | `<file>.<ext>.meta.json` — the full filename *including* its extension | Anything, one file at a time. For the handful of exceptions. |
| **A manifest row** | `manifest.csv` at the source root, keyed by the lower-cased path | Anything, in bulk. SMEs edit it in Excel; it beats the path rules. |
| **The content** | The mean of the document's first 8 chunk vectors, compared against a prototype per facet value | **Only** facets marked `classify: true`, and only where the layers above left them empty. |
| **A person** | `/admin` → Review queue, **after the document is already indexed** | Anything — and it is then frozen: re-discovery never overwrites an approved tag. |

**None of these layers blocks ingestion.** Embedding happens *before* classification, and classification is what
flags a document for review — so a document in the review queue is already embedded, indexed and searchable.
Review is a quality backlog, never a gate; see
[Does review slow ingestion down?](docs/classification-guide.md#6-does-review-slow-ingestion-down)

Every value keeps the name of the layer that set it, so a tag can always be traced back to its origin. The full
rules — glob semantics, the manifest's column format, the exact sidecar name — are in the
[classification guide](docs/classification-guide.md), with copy-paste templates in
[docs/examples/](docs/examples/).

![Figure 5 — What RAG-OS reads from a document](docs/diagrams/classification-inputs.svg)

*Figure 5 — What RAG-OS reads from a document. The four cheap signals resolve first and the last one to speak
wins; only the content signal opens the file.*

![Figure 6 — How documents get classified](docs/diagrams/classification-layers.svg)

*Figure 6 — How documents get classified. Seven layers, in precedence order. Only the last two involve a model,
and only the seventh spends tokens.*

#### When a document stays unclassified

What happens depends entirely on *which* value is missing, and the difference is the whole ball game:

| Missing | What happens |
|---|---|
| A **business facet** (`f_topic`, `f_doc_type`…) | Nothing serious. The document is still parsed, embedded, indexed and **fully retrievable** — it simply cannot be narrowed by that filter and is missing from that facet's counts. |
| A **required access attribute** (`department`, `region`) | **The document is invisible to everyone** except admins. Default-deny means a document with no value for an attribute never satisfies it, so it matches nobody's filter — while sitting perfectly happily in the index. |
| `acl_clearance` | Denied, *not* treated as public: `acl_clearance le N` is false over a null field. |

The classifier assigns its best candidate when the cosine similarity reaches `CLASSIFIER_MIN_SCORE` (0.30); below
that it assigns **nothing**. Either way, if the top two candidates are within `CLASSIFIER_MARGIN` (0.03) it flags
the document for review, and it appears in `/admin` → Review queue. Approving a tag there freezes it and re-tags
the document in place, without re-embedding.

> **The failure nobody reports.** A facet the rules leave empty that is *not* `classify: true` — `department`,
> `region`, `confidentiality`, `language` — is never offered to the classifier, so it never reaches the review
> queue either. The document is indexed with an empty value and nothing is raised. If that value happens to be a
> required access attribute, the document is silently invisible. **Before enabling a source, ingest a sample and
> open `/admin` → Documents grouped by facet: what you are looking for is the empty bucket.**

#### The review queue: why a document is there, and what it changes

**What it is for.** The review queue is a *quality backlog for classification* — the business facets
**Document type** and **Topic** — where the automatic classifier was not sure of its answer. It is **not** an
approval gate, and it is **not** about access: a document in the queue is already embedded, indexed and
answerable by everyone its access tags allow.

**What puts a document there.** Both conditions must hold:

1. A facet marked `classify: true` in `facets.yaml` (`doc_type`, `topic`) is still **empty** after every rule
   layer: source defaults, folder path rules, sidecar, manifest row and the upload form. Anything those layers
   fill in is never sent to the classifier.
2. The classifier then compares the document with each value of that facet and is unsure. Either the best
   match scored below `CLASSIFIER_MIN_SCORE` (0.30), so the facet is **left empty**, or the best two are within
   `CLASSIFIER_MARGIN` (0.03) of each other, so the best is **assigned as a guess**. The document is marked
   `PENDING`.

**Why you see only a few.** Every document whose Document type and Topic come from folders, the manifest or
the upload form skips the classifier entirely, and every confident, unambiguous classification is accepted
without review. Queue size measures how much your rules leave unsaid; tag well and it stays empty.

**What it can never catch.** Department and Region are *not* `classify: true`, so a document missing them is
never offered to the classifier and **never enters the queue**, even though no one can read it. Find those on
Admin > Documents: the **Access** column shows a red *"No Department, Region: invisible"* badge. Select it to
open **Edit access tags**.

**Scenarios** (A and D are from this deployment's data; the scores in B and C are illustrative):

| Scenario | Classifier | In the queue? | Can a permitted user get answers from it? | What that user notices |
|---|---|---|---|---|
| **A.** `hr/uk/policies/parental-leave-policy.md`. The folder sets Department HR, Region UK and Document type Policy; `manifest.csv` sets Topic Leave. | not run: nothing left empty | no | yes | Nothing. Answers cite it, and the Topic = Leave filter finds it. |
| **B.** An upload with Department and Region picked but no Document type or Topic. The classifier scores Topic *Benefits* 0.58 and *Leave* 0.565 (margin 0.015 < 0.03). | assigns *Benefits* as a guess | **yes**: tagged *and* pending | **yes, immediately** | Answers cite it. It shows under Topic = Benefits, the guess. If the right topic was Leave, filtering by Leave misses it until a reviewer corrects it. |
| **C.** As B, but the best Document type score is 0.22 (< 0.30). | assigns **nothing** for Document type | **yes**: empty and pending | **yes** | Answers cite it, but it is **missing** whenever the user narrows by Document type (e.g. Policy), and absent from that facet's counts. |
| **D.** `Benefits.pdf` as first uploaded: Department and Region empty. | not offered: those are not `classify: true` | **no** | **no one** but its uploader | "No grounded answer". Query traces names the cause; the Documents Access column flags it. The review queue never will. |

**Approved versus not yet approved.** For the person asking a question, the only differences are the facet
filters:

| | Not yet approved (`PENDING`) | Approved by a reviewer |
|---|---|---|
| Retrieved and cited in answers | yes | yes, identical |
| Who may read it | from its access tags; the classifier never sets access | the same, unless an *admin* also edited the access tags while approving |
| Document type / Topic filters and counts | the classifier's guess, or empty | the reviewer's values |
| When the file changes and is re-ingested | the classifier's values are carried over and not re-guessed | frozen: never re-classified, never overwritten by re-discovery |
| What approving costs | — | a re-tag: index fields are updated in place, with no re-parse and no re-embed |
| Expiry, auto-approval, reminders | none: `PENDING` stays until someone acts | — |

**Who works it.** `rag.reviewer`, `rag.sme` and administrators can approve facets; only administrators may change
access tags there. `review_status` lives in the state database only. It is not an index field and appears
nowhere in the query path, which is why waiting in the queue can never hide a document. More depth:
[classification guide §5–6](docs/classification-guide.md).

## 10. Using the product

* **Chat.** Ask in natural language, narrow the search with facet filters, open citations to see the passage,
  path and page. Follow-up questions are rewritten into standalone questions.
* **Sign in.** Opening the assistant shows **Sign in with Microsoft**. MSAL runs the authorization-code + PKCE
  flow, returns to `/auth/callback`, and acquires an access token for the RAG-OS API scope. Nothing about the
  person is stored here — see [Where a caller's attributes come from](#where-a-callers-attributes-come-from).
* **Embed in another page.**
  ```html
  <div id="rag-chat"></div>
  <script src="https://<chat-ui-host>/embed/loader.js"
          data-target="#rag-chat" data-token-endpoint="/api/rag-token"></script>
  ```
  Your host application's `/api/rag-token` returns `{token, expires_in}`, where the token is an **Entra access
  token issued for `ENTRA_API_SCOPE`** — RAG-OS trusts no other issuer, so the host passes through a token it
  already holds (or acquires one on-behalf-of). The loader passes it to the iframe with `postMessage`; tokens never
  appear in URLs. Add the host origin to `EMBED_ORIGINS`. Details are in [Deployment.md](Deployment.md).
* **Upload.** Contributors and admins can upload from the chat or admin UI (`POST /api/uploads`). An upload gets a
  `tracking_id` and is processed on the **priority lane**.
* **Who can read an upload.** The upload form has an **Only me (private)** box, **unticked by default**.
  * **Unticked:** the document is readable by everyone whose Department, Region and Clearance match its access
    tags. Each tag comes from, in order:
    1. the Department and Region pickers and the Clearance select;
    2. the upload source's default (`clearance: 1` in `sources.yaml`);
    3. the uploader's own attribute.

    The folder never sets access, because the browser supplies it. An upload that would leave a *required* tag
    (Department, Region) empty is refused with the reason, rather than becoming a document nobody can read. An
    administrator's picks set the audience as given. A contributor's picks may only narrow their own scope;
    a wider pick still classifies the document, but its audience stays theirs. The panel shows the result,
    e.g. "Visible to HR · Global · Internal".
  * **Ticked:** only the uploader can read it (their individual `employee_id` share). It is recorded as
    deliberately private, so Query traces does not report it as mis-tagged.
  * **Who may tick it** is configuration (`sources.yaml` → `uploads.settings.only_me.allowed_roles`), so it can
    be restricted per role without a release.
* **Admin > Documents** lists every document with its **Doc ID** (select to copy) and **Access**: who may read
  it ("HR · Global · 1", *Only me*, or a red *"No Department, Region: invisible"* badge).
* **Correcting a document.** Open a document (or select its red badge): **Edit access tags** (the upload form's
  dropdowns) re-tags the index in place without re-embedding, and opens by itself when a required tag is missing.
  **Delete permanently** removes the document and everything derived from it; see
  [Deleting documents and resetting all data](#deleting-documents-and-resetting-all-data).
* **How an upload gets its facets.** Three sources, weakest first:
  1. **the folder it came from.** Choose *choose a folder* (or drop a folder) and the browser sends the path
     relative to the folder you picked, which `path-rules.yaml` reads exactly as it does for a crawl. **Pick the
     department folder** — a browser only reports the path *below* your selection, so picking `HR` sends
     `HR/UK/policies/x.pdf` and tags Department, Region and Document type, while picking `policies` sends one
     segment and matches nothing. The panel tells you which happened.
  2. **the pickers** in the upload panel, which override the folder for that upload. Values come from
     `facets.yaml`, and anything outside the vocabulary is refused with the reason rather than dropped silently.
  3. **the classifier**, during indexing, for facets still unset — only those marked `classify: true`
     (`doc_type` and `topic` as shipped) and only above its confidence floor; below it the document goes to the
     review queue.

  A single loose file has no folder path at all, so for that the pickers are the only route. Nothing sets a
  language by default: there is no language detection in the pipeline, so the facet stays unset unless you pick
  one.
* **A folder does not grant access.** Path rules carry both halves — `hr/**` sets the *facet* `department: HR`
  **and** `acl.department: [HR]`. For an upload the path comes from the browser, so only the facet half is
  honoured: the tag makes the document filterable as HR, while who can read it keeps coming from your own
  identity attributes. Widening access stays deliberate — a crawl of a shared folder, or an admin editing ACL in
  the review queue.
* **Did my upload work?** The upload panel shows a live status badge per file, and below it **Recent documents** —
  every document you uploaded, newest first, ten at a time, under **All / Failed / In progress**. That list is what
  survives closing the tab: it is served by `GET /api/uploads`, which scopes to your own documents, or to
  everyone's if you hold `admin`. A failed row carries the reason. Administrators also get an **Admin** link beside
  their name, which opens the console below, where a document's full event timeline and a retry button live.
* **Admin console (`/admin`):**
  * dashboard (totals, queue depth, per source and per department/region)
  * runs (progress, throughput, ETA, errors)
  * uploads (every document newest first, tabbed by state — the same list as the chat page, unscoped)
  * documents (filter, timeline, retry, CSV export)
  * dead letters
  * sources (sync now)
  * review queue (approve or correct automatic tags)
  * controls (pause/throttle)
  * configuration editor and "explain access" tool
### New Scenarios: the same file more than once

A **document** is identified by `(source_id, item_id)` — its owner, provenance, tags and status. Its **content**
is identified by the sha256 of its bytes. Several documents can share one content: two people uploading the same
file, a file both crawled and uploaded, the same file in two folders. What is shared, and what is deliberately
not, follows from that split.

| # | Scenario | Documents | Blob storage | Search index | Embedding cost | Retrieval | On delete |
|---|---|---|---|---|---|---|---|
| 1 | Same file, **same person**, uploaded twice | **1** — the second call returns the first, with `duplicate_of` set | 1 | 1 set | **0** for the second | one passage, once | purge frees blob + chunks |
| 2 | Same file, **two different people** | **2** — each owns theirs and sees it in their own list | **1**, shared | 2 sets — their access tags differ | paid per document † | one passage; the other locations are listed as `also_at` | purge frees only the deleted one's chunks; the blob stays while the other refers to it |
| 3 | Same file, **two sources** (a crawl and an upload) | **2** — provenance per source is kept on purpose | **1**, shared | 2 sets | paid per document † | collapsed to one passage | reference-counted, as #2 |
| 4 | Same file, same source, **re-crawled unchanged** | 1 | 1, untouched | untouched | 0 | unaffected | — |
| 5 | Local file **`touch`ed**, bytes identical | 1 | 1, untouched | untouched | **0** | unaffected | — |
| 6 | Same file in **two folders** → different path rules | 2 | **1**, shared | **2 sets** — facets and ACL differ, and are never merged | paid per document † | each caller sees the copy their tags admit | reference-counted |
| 7 | File **edited** (content changed) | 1 | 2 until purge | old chunks swept, new written | full, once | only the new version | purge reclaims the old blob |
| 8 | **One of several sharers** deleted | that row → `DELETED` | **kept** — still referenced | its chunks removed | — | its path stops being offered | nothing else is freed, correctly |
| 9 | **Last sharer** deleted | row → `DELETED` | **freed by purge** | chunks removed | — | gone | the case that reclaims space |
| 10 | **Ingest fails mid-run** | 1 | 1 | retry overwrites | re-embedded on retry | one passage | — |


† **Embedding is paid once per document, not once per content.** Sharing the work would mean either reading the
twin's vectors back out of the index — they are written `stored=False` (`azure_search.py:66`) precisely to keep
the index small, so they cannot be read back — or having both documents reference one shared chunk row, which is
only safe when their facets and ACL are identical, since the row carries those fields. Scenario 1 avoids the
cost entirely by collapsing to one document, and scenario 5 by recognising unchanged bytes; the remaining cases
pay per document today.

Rows 5 and 10 are bug fixes rather than features. A local folder has no cheap content hash, so change detection
is size+mtime — meaning a `robocopy` or a `touch` across a corpus used to re-parse and re-embed every file to
produce byte-identical vectors. And because old chunks are swept only *after* the new ones are written, an
ingest that died in between left both versions live and retrievable.

**Two limits, stated plainly because both are otherwise discovered the hard way:**

* **Tags are never merged across documents.** The same file tagged two ways is indexed twice, by design. Merging
  would escalate privilege: the access filter is a conjunction (`department AND region AND clearance`), so
  combining `(HR, UK)` with `(Finance, US)` into `department [HR, Finance]`, `region [UK, US]` would let an
  **HR/US** caller read content that neither original copy allowed them. Flat index fields cannot express a
  disjunction of conjunctions, so no merged row can be made safe.
* **Space is reclaimed only by purge**, never automatically on delete. A deleted document keeps its row and its
  bytes until `rag-os purge` runs, so that a source briefly failing to list a file does not permanently destroy
  it:

  ```bash
  rag-os purge --retention-days 7            # dry run: reports what would be freed
  rag-os purge --retention-days 7 --apply    # actually free it
  ```

  The same is available to administrators as `POST /api/admin/ingestion/purge`. `blobs_kept_shared` in the
  report counts content left in place because a live document still references it.


* **CLI:** `rag-os discover --source <id>` (run where a local folder is mounted), `rag-os status`,
  `rag-os explain --attr department=HR --attr region=UK`, `rag-os ask "…" --as hr-emea`, `rag-os bootstrap`, `rag-os purge`,
  `rag-os reset --confirm <index>`.

### Deleting documents and resetting all data

**Correcting access tags.** Admin > Documents > a document > **Edit access tags** uses the *same dropdowns* as
the upload form: Department and Region from the vocabulary (plus *Everyone*), and Clearance from the ladder.
There is no free text, because matching is exact and a typed `hr` never matches `HR`. The API enforces the same
rule: a value outside the vocabulary is refused, and a synonym or a different case is mapped to the canonical
value.

**Permanently deleting selected documents.** Tick the documents and press **Delete selected permanently**, or
use **Delete permanently** in a document's panel (`POST /api/admin/documents/delete`). Removed immediately:

* its index entries (no answer can cite it any more);
* its stored copy, unless another document holds the same bytes (counted as `blobs_kept_shared`);
* its status history and queued work;
* query traces that mention it (they can hold its title and answer text quoting it);
* its id in any expectation's "must cite" list.

A file read in place from an **Azure Blob source** is the customer's own file and is never deleted
(`blobs_left_at_source`).

| Where the document came from | After a permanent delete |
|---|---|
| **An upload** | Gone for good. There is no other copy. |
| **A crawled source** (folder, blob) whose file is still there | **Comes back on that source's next sync.** To keep it out, delete or move the file at the source. |

**Resetting all data** (Admin > Controls > *Danger zone*, `POST /api/admin/reset`, or
`rag-os reset --confirm <index>`). Type the index name to confirm. In order:

1. Ingestion is paused.
2. The queue is emptied.
3. The index is emptied (on Azure it is deleted and re-created with the same schema and embedding-profile stamp,
   which takes seconds at any size).
4. Every stored copy and export is deleted.
5. Every document, status event and ingestion run is deleted.
6. Every query trace and expectation is deleted (untick to keep them).

**Kept:** configuration (sources, access policy, facets, path rules, embedding profile), people and their
attributes (Entra), and telemetry already in Application Insights. **Ingestion stays paused** until you resume it
in Controls. Every step can be repeated, so a reset that stopped part-way is finished by running it again. For
a very large store, prefer `rag-os reset`: the console request passes through a proxy with a ~120 s timeout.

**Business concerns before you press it:**

| Concern | What it means |
|---|---|
| **Irreversible** | Uploads have no other copy. Only crawled sources can be re-ingested. |
| **Cost** | Resuming re-parses and re-embeds every crawled document. With a hosted embedding model (e.g. the `aoai-3-small-1536` profile) that is billed per token, plus indexing time. Budget it before resuming. |
| **Outage** | Until re-ingestion finishes, every question gets "No grounded answer". It is not an error, so announce it. Expectations that require documents fail until then. |
| **Audit and legal hold** | Deleting query traces removes the record of who asked what. Untick *include query traces* if retention policy or a legal hold applies. |
| **Erasure is not total** | Application Insights logs, PostgreSQL point-in-time backups and blob soft-delete (if enabled) keep copies until their own retention ends. A delete here is not, on its own, proof of erasure. |
| **Who can press it** | Administrators only, typed confirmation, audit-logged (`rag_os.audit`). Anyone who can mint a dev token is an administrator, so keep `DEV_AUTH_ENABLED=false` wherever real data lives (the Azure default). |
| **Shared index** | Reset empties the *active* index named in the confirmation. Never point two environments at one index. |
| **Races** | A worker mid-document can write after the clear. The reset pauses first, sweeps the index again at the end, and the worker removes its own chunks if their document disappears. A second reset is always safe. |

## 11. User query handling scenarios

What the assistant does when the documents it can see do not agree, and how to try it. Each scenario is a real
pair of documents in this repository, a real query, and the behaviour to expect.

### Conflict handling: two sources that disagree

An assistant that silently picks one of two contradictory policies is worse than one that finds nothing: the
answer is confident, cited, and wrong for half its readers. RAG-OS is told to surface the disagreement instead —
`ANSWER_SYSTEM` in `src/rag_os/application/services/prompts.py`:

> If context blocks conflict, say so and prefer the one with the most recent effective date if shown.

Two sample handbooks disagree on purpose, each its own source so the citations name different systems of record:

| Source | Document | Says | Effective |
|---|---|---|---|
| `scenario-global-handbook` | `hr/global/policies/parental-leave-standard.md` | 18 weeks paid parental leave | 2025-01-01 |
| `scenario-uk-handbook` | `hr/uk/policies/parental-leave-uk.md` | **26 weeks**, superseding the standard for the UK | 2026-04-01 |

Both are manual sources, so nothing ingests them until you ask:

```powershell
./tasks.ps1 up
docker compose exec api rag-os discover --source scenario-global-handbook
docker compose exec api rag-os discover --source scenario-uk-handbook
```

**The effective date comes from the document.** A `---` fenced front-matter block at the top of a `.md` or
`.txt` file supplies it, and is stripped before the text is chunked, so it never reaches an embedding or a
citation snippet:

```text
---
effective_date: 2026-04-01
---
# United Kingdom Parental Leave Handbook
```

It must be ISO-8601 (`YYYY-MM-DD`). A date the system cannot order is worse than none, because the model would
be asked to prefer the most recent of two things it cannot compare — so anything else is ignored.

#### Case 1 — the conflict is surfaced, not resolved silently

Ask as **Priya (`hr-emea`)**, who is in the UK:

```powershell
uv run rag-os ask "How much paid parental leave do I get?" --as hr-emea
```

Region matching expands the *caller's* location upward — UK reaches EMEA and Global — so both handbooks are in
her scope, and the model receives two numbered blocks each headed with its effective date.

**Ideal behaviour.** The answer states that the two documents disagree, cites **both**, and prefers the UK
handbook as the later of the two — something like *"The UK handbook gives 26 weeks [2], superseding the group
standard of 18 weeks [1]; the two documents disagree, and the UK handbook is the more recent."* What it must
not do is answer "18 weeks" or "26 weeks" as though the matter were settled.

#### Case 2 — no conflict exists for someone who cannot see both

Ask the identical question as **Marcus (`sales-us`)**:

```powershell
uv run rag-os ask "How much paid parental leave do I get?" --as sales-us
```

**Ideal behaviour.** Neither handbook is retrieved — they are HR documents and Marcus is in Sales — so the
answer is that nothing in the documents available to him covers it. This is the same mechanism seen from the
other side: **a conflict is surfaced from what the caller can see, never from the corpus as a whole.** It also
means the fix for an inconvenient conflict is never to widen someone's access.

### What this does and does not do

* The offline demo LLM (`LLM_ANSWER=fake`, the default for `./tasks.ps1 up`) is **extractive**: it returns the
  first sentence of the top-ranked passage and ignores the system prompt entirely. Set `LLM_ANSWER=claude` or
  `aoai` in `.env` to see the behaviour above. The automated tests
  (`tests/integration/test_conflict_scenario.py`) therefore assert everything up to the model — that both
  documents are retrievable by Priya and by neither of the others, that they survive de-duplication as two
  distinct blocks, and that both effective dates reach the prompt.
* **A conflict is prose, not data.** It exists only inside the answer text; there is no field on `Answer` or
  `Citation` marking it, so nothing downstream can filter, count or alert on conflicting sources.
* **Only byte-identical passages are recognised as duplicates.** A document that paraphrases another rather
  than copying it still reads to the model as a second, independent source that happens to agree — which is
  the failure mode this feature does not cover.
* **A conflict between two versions of the same document cannot arise**: superseded versions are removed from
  the index at ingestion. This is about two different documents that both claim to be current.

## 12. Security model

* **Authentication.** A JWT validator with a trusted-issuer table:
  * Microsoft Entra ID: RS256 via JWKS — the only production issuer.
  * Dev tokens: local only, gated by `DEV_AUTH_ENABLED` and lifetime-capped.

  Algorithms are pinned per issuer (no `alg=none`, no HS/RS confusion), and `iss`, `aud` and `exp` are required.
* **Authorization.** The access-policy engine builds a **default-deny** OData filter that is applied *inside*
  every search, facet and listing call:
  * Attribute values are allow-list validated, which rules out filter injection.
  * Roles only come from issuers listed in `trusted_for_roles`.
  * Admin bypass is audit-logged.
* **Network — application.** Only the chat UI is public, and the API has internal ingress. CSP `frame-ancestors`
  limits embedding to `EMBED_ORIGINS`, and the API sends `nosniff`, `no-store` and `frame-ancestors 'none'`.
* **Network — Azure resources.** Storage, Key Vault, AI Search and Service Bus keep **public endpoints**. They are
  not open: every one of them is Entra-only (see Secrets below), so a caller needs a token *and* an RBAC grant in
  this subscription. But the endpoint itself is reachable from the internet, and there is no network-level
  restriction in front of it. PostgreSQL is the exception — it has a public endpoint too, with an IP allow-list
  holding exactly two rules: Azure services (`0.0.0.0`, which is what lets the Container Apps reach it) and
  optionally your own address from `ClientIpAddress`.
  Closing the rest needs a VNet, because the Container Apps have no stable egress identity without one and the
  "trusted Azure services" bypass on Storage and Key Vault does not cover them. **A Container Apps environment's
  VNet is fixed at creation**, so that work is cheap before step 07 runs and means recreating every app and job
  afterwards. Service Bus additionally needs the Premium tier for any network rule at all.
* **Secrets.** Key Vault and the managed identity. Storage shared keys, Search API keys, Service Bus SAS and
  anonymous blob access are disabled, and PostgreSQL is Entra-only with password auth off.
* **Prompt injection.** Retrieved text is fenced as data, the system prompt forbids following it, and uncited
  answers are refused.
* **Deliberately left for production hardening:**
  * private endpoints and a VNet-integrated environment (see Network - Azure resources above: cheapest before step 07)
  * WAF / Front Door
  * Purview labels

### Where a caller's attributes come from

RAG-OS has **no user database**. There is nothing to create a person in and nothing to keep in sync: every request
is authorised from the claims inside the caller's own bearer token, and the resulting attributes live exactly as
long as that request. Two issuers can produce such a token, and the trusted-issuer table in `composition.py`
decides how each one is validated.

Everyone who uses the assistant has an account in your directory, so **Entra is the only production issuer**.
There is no second trust path and no shared signing key to protect: the dev issuer below exists only so the
product can run on a laptop or in CI without a tenant.

| Issuer | Who it is for | How it is validated | May grant roles? |
|---|---|---|---|
| **Microsoft Entra ID** (`entra`) | Everyone: employees, SMEs, platform admins, and any guest invited into your tenant | RS256 with the signing keys from your tenant's JWKS; `iss` must be your tenant and `aud` must equal `ENTRA_AUDIENCE`. | Yes — `rag.admin` and the other values listed under `roles:` in the policy. |
| **Dev tokens** (`dev`) | Local demos and tests only | HS256 with `dev-jwt-signing-key`, issuer `rag-os-dev`, lifetime capped; the issuer is only trusted when `DEV_AUTH_ENABLED=true`. | Yes — but it is a local-only issuer. |

#### Signing in

The chat UI runs the **authorization-code flow with PKCE** through `@azure/msal-browser`: *Sign in with Microsoft*
→ Entra → back to `/auth/callback` → the page it started from. MSAL then acquires an access token for
`ENTRA_API_SCOPE` and renews it silently a minute before it expires; the API sees an ordinary
`Authorization: Bearer` header.

The browser is told which tenant and client to use by the API itself — `GET /api/public-config` returns
`auth_mode`, `entra_tenant_id`, `entra_client_id` and `entra_api_scope` — so the chat-ui image carries no
environment-specific identity configuration.

> **One app registration covers both sides.** Add an **SPA** platform with redirect URI
> `https://<chat-ui-host>/auth/callback`, expose an API (`api://<app-id>`) with the scope `access_as_user`
> (`./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev` does this and the token-version setting that goes
> with it — skipping it is what produces `AADSTS65005` at sign-in), and add
> the `rag.admin` app role. Then `ENTRA_AUDIENCE = api://<app-id>` and
> `ENTRA_API_SCOPE = api://<app-id>/access_as_user`. Step-by-step in [Deployment.md](Deployment.md).

> **One Entra tenant at a time.** `composition.py` builds a single Entra issuer from `ENTRA_TENANT_ID` and
> `ENTRA_AUDIENCE`. `JwtValidator` itself is keyed by issuer and holds any number of them, but a *separate* tenant —
> Entra External ID (CIAM) or Azure AD B2C, which have their own issuer URLs — needs a second entry added there
> before its tokens are accepted. Guests invited into your own tenant need no such change.

Whichever issuer it came from, the token then takes the same two steps: **JwtValidator** checks the signature,
algorithm, issuer, audience and expiry, and **ClaimsMapper** turns the surviving claims into attributes. Which
claim feeds which attribute is configuration, not code — one entry per attribute in
`config/access-policy/access-policy.yaml`:

```yaml
- name: department
  field: acl_department
  match: any_of
  required: true
  claims: { entra: extn.department, dev: departments }
```

Read that as: the attribute **named** `department` is fed by the `extn.department` **claim** in an Entra
token, and is compared against the `acl_department` **index field** carried on each document. Those are three
different namespaces — see [How the access policy works](#how-the-access-policy-works). Adding `cost_center` is an
edit here plus a tag on your documents; there is no code to change.

> **An embedded page supplies its own token.** Inside an iframe the assistant does not run MSAL: the host page
> posts an Entra access token for `ENTRA_API_SCOPE` over `postMessage`, and only origins listed in `EMBED_ORIGINS`
> are accepted. Administrators can also paste a bearer token on the sign-in panel, which is useful when diagnosing
> a token problem.

![Figure 7 — Where a caller's attributes come from](docs/diagrams/identity.svg)

*Figure 7 — Where a caller's attributes come from. Two issuers, one validator, one mapper, and a principal that
exists for the length of a single request.*

### Granting someone a role

Signing in is not the same as being allowed to do anything. A new account gets a **read-only session**, filtered
by the attributes it carries — which is why the first thing most people try, uploading a document, comes back
with `uploading requires the contributor or admin role`.

Capabilities come from **Entra application roles**. The policy names the roles it understands; Entra decides who
holds them. Nothing in RAG-OS can grant one, because there is no user store to grant it in:

| Entra app role (the `roles` claim) | Application role it grants | What that unlocks |
|---|---|---|
| `rag.admin` | `admin` + `contributor` | Everything — see [Defining access by role](#defining-access-by-role) |
| `rag.contributor` | `contributor` | Uploading |
| `rag.sme` | `taxonomy_editor` + `reviewer` | Taxonomy and the review queue |
| `rag.reviewer` | `reviewer` | The review queue |
| *(none)* | *(none)* | Read-only chat, filtered by department, region and clearance |

The four roles are created for you: `provision-all.ps1` reconciles the app registration before step 00, so a
normal deployment ends with them in place. What it will not do unasked is hand anyone administrator rights —
set `EntraGrantAdminTo` in the psd1 to name the first one. To do it now, or to grant somebody a role later:

```powershell
# creates all four roles if missing, then assigns rag.admin to the signed-in account
./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev -GrantAdminTo me
```

`-GrantAdminTo` also takes a UPN or a user object id. In the portal the same two steps are *App registrations →
RAG-OS → App roles* and *Enterprise applications → RAG-OS → Users and groups → Add user/group*. Assigning needs a
directory role — Application Administrator or Cloud Application Administrator — which a subscription Owner does
not have by itself.

**For anybody other than the first administrator**, use the companion script — it also answers the two questions
that follow a grant:

```powershell
./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev                 # who holds what
./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -ListRoles      # which roles exist
./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -Role contributor -To priya@contoso.com
./infra/scripts/Set-EntraAppRoleAssignment.ps1 -Env dev -Role admin -To priya@contoso.com -Remove
```

`-ListRoles` reads the app registration; `-List` reads the enterprise application. Two objects, the same display
name — which is why an assignment never shows up under *App registrations → App roles*.

> **A role is never added to a token that has already been issued.** Sign out and back in, or use a private
> window. Until you do, nothing changes and it looks as though the grant failed.

**Checking what you actually have.** `GET /api/me` returns the roles as the policy mapped them:

```bash
curl -H "Authorization: Bearer $TOKEN" https://<chat-ui-fqdn>/api/me
```

`"roles": ["admin", "contributor"]` means it worked. `"roles": []` has two quite different causes that look
identical here — no role assigned, or a role assigned whose value the policy does not map (a typo such as
`rag.contrbutor`).

**Account Information tells them apart.** Click your name in the chat page or the admin console. It shows every
application role defined on the app registration, which of them your token actually carries, and — under
*unrecognised* — any role value the token carries that matches none of them, which is what a misspelled
assignment looks like. It also shows your Department, Region, Clearance (with every level and what each means),
your Employee OID, the token claim each value arrived in, and a plain-English statement of what you can read.

Note that the roles shown there are the **application roles**, not RAG-OS's internal names. The mapping is
many-to-many — `rag.admin` grants both `admin` and `contributor` — so it cannot be run backwards; the panel
reads the token's own `roles` claim instead. To see what a given combination *would* be allowed, without a
token at all:

```bash
uv run rag-os explain --attr department=HR --attr region=UK --role admin
```

> **`rag.admin` is not "may upload" — it is "sees everything".** An admin bypasses the document filter entirely,
> and the bypass is audit-logged. Grant it deliberately, and prefer `rag.contributor` for people who only need to
> add documents.

**Clearance levels are configuration, not four fixed names.** `access-policy.yaml` gives each rung of the
`clearance` ladder a label and a description under `levels:`, and Account Information renders whatever is there —
so a deployment running 0–5, or naming them differently, describes its own. Adding a rung is safe; renumbering
one silently re-classifies every document already tagged at that level.

Roles and attributes are two different systems, and fixing one does not fix the other: roles decide what you may
**do**, while `department`, `region` and `clearance` decide what you may **see**. A non-admin whose token carries
no `department` or `region` sees *nothing*, because both are `required: true` — see
[Where a caller's attributes come from](#where-a-callers-attributes-come-from).

### Why `config/dev/principals.yaml` exists

It is a **demo fixture, not a user store**. Without it, a first run would need either an Entra tenant or a working
tenant before you could ask a single question. With `DEV_AUTH_ENABLED=true` the API mounts two extra endpoints —
`GET /api/dev/principals` and `POST /api/dev/token` — that hand out a short-lived HS256 token carrying the claims
written in that file. That is what makes `rag-os ask "…" --as hr-emea` and the principal picker in the chat UI
work, and what lets the demo show HR and Sales seeing different answers to the same question.

* **It is gated.** `infra/env/dev.sample.psd1` ships `DevAuthEnabled = $false`, so in Azure the router is never
  mounted and both endpoints return 404.
* **It is never consulted for a real token.** An Entra token is validated against its own issuer and
  mapped from its own claims; this file is not read, and no lookup by subject happens anywhere.
* **It grants nothing by itself.** A dev token is accepted only because the dev issuer is in the trusted-issuer
  table — which it is not in production.

So nobody is ever "added to `principals.yaml`" to get access. The production equivalent of that action is adding
the person to a group (or setting an attribute) in Entra.

### Giving HR or Sales access to hundreds of people

You never list people. An attribute value such as `HR` is granted by something the identity provider already knows
about the person, so the configuration is the same size for five people or five thousand. With Entra there are two
usual ways to carry it.

| Approach | What the token contains | What you configure |
|---|---|---|
| **Directory extension or optional claim** (what the shipped policy assumes) | `"extn.department": ["HR"]` — the value itself | Nothing: `claims.entra` already points at it. Best when HR data already flows into the directory. |
| **One security group per value** | `"groups": ["7c9f1b3e-…-0b2d6e21a001"]` — the group's *object id*, never its name | Point `claims.entra` at `groups` and add a `value_map`. Best when groups already model the organisation and joiners/leavers are handled there. |

The second case is why attribute rules accept a `value_map`: without one you would have to tag every document with
a GUID.

```yaml
- name: department
  field: acl_department
  match: any_of
  required: true
  claims: { entra: groups, dev: departments }
  value_map:
    "7c9f1b3e-2d4a-4a1c-9f6b-0b2d6e21a001": HR      # kb-dept-hr
    "8ad02c4f-3e5b-4b2d-a07c-1c3e7f32b002": Sales   # kb-dept-sales
  drop_unmapped: true
```

Every member of `kb-dept-hr` now arrives as `department = [HR]`, and documents stay tagged `HR` rather than a GUID.
`drop_unmapped: true` discards the groups that mean nothing here — a raw `groups` claim carries *every* group the
person is in. Matching is case-insensitive, and mapped values are checked against the attribute's `value_pattern`
when the policy loads, so a typo is a startup error rather than a 401 on someone's first query.

A person who joins the group gets access on their **next token** — the next sign-in or token refresh for Entra,
Nothing is redeployed, and no RAG-OS configuration changes when someone
joins or leaves.

> **Group overage.** If a caller is in more than about 150 groups, Entra stops sending `groups` and sends
> `_claim_names` / `_claim_sources` instead, expecting the application to call Microsoft Graph. RAG-OS does not do
> that, and such a caller would simply have no department. Apply a *group filter* on the app registration so only
> the `kb-*` groups are emitted, or use a directory extension attribute instead.

### What `clearance: 0, 1, 2` signifies

`clearance` is the one attribute declared with `match: max_level`, which makes it a **ladder rather than a set**.
Every other attribute asks "does the caller hold one of the values this document allows?". This one asks "is the
document's level at or below the caller's?", and contributes a single OData clause: `acl_clearance le 1`.

| Level | Meaning | Typical content |
|---|---|---|
| `0` Public | Anyone who can reach the assistant | Published policies, product documentation, FAQs |
| `1` Internal | The default for employee content | Handbooks, process guides, internal announcements |
| `2` Confidential | Need-to-know within a department | Salary bands, unreleased plans, customer contracts |
| `3` Restricted | A named few | Legal matters, investigations, M&A |

The numbers are yours — the policy only requires that they are integers and that a bigger number means more
access. Three consequences follow:

* A caller sees every level **up to and including** their own: clearance 1 sees 0 and 1, never 2.
* A document with **no** `acl_clearance` is denied rather than treated as public. That is the default-deny rule
  showing up here.
* A caller whose token carries **no** clearance claim (`extn.clearance`) is treated as level 0, because the
  attribute is `required: false` — so they see public documents rather than nothing at all.

The claim must arrive as an **integer**. If your identity provider can only send a label, map it in the same policy
file:

```yaml
- name: clearance
  field: acl_clearance
  match: max_level
  claims: { entra: extn.clearance, dev: clearance }
  value_map: { Public: "0", Internal: "1", Confidential: "2", Restricted: "3" }
```

How documents get the matching level — rules, manifest, sidecar or review — is covered in the
[classification guide](docs/classification-guide.md).

### How the access policy works

`access-policy.yaml` uses **three different names for what feels like one thing**, and telling them apart is most
of understanding the file:

| Layer | Example | Defined in | Describes |
|---|---|---|---|
| **Claim** (`claims:`) | `extn.department`, `groups`, `oid` | **Microsoft Entra** — a directory extension, optional claim or app role | What the identity provider asserts about **the person**. |
| **Attribute** (`name:`) | `department` | This policy file | The internal handle. What the two sides are matched *on*, and the key you tag documents with (`acl.department` in a manifest). |
| **Index field** (`field:`) | `acl_department` | This policy file, created in Azure AI Search | A column on **every indexed chunk**, listing who may read **that document**. |

> **Nothing named `acl_*` is ever a claim.** You do not define `acl_department` in Entra. Entra only supplies the
> claim side; the `acl_*` fields are created by RAG-OS in the search index and filled by *document tagging* — path
> rules, `manifest.csv`, sidecars. They describe the document, not the person.

So authorization is not "validate the user's claims against a policy file". It is a comparison of two sides:

```text
Entra sends   extn.department: ["HR"]      the person
      ↓       claims: { entra: extn.department }
attribute     department = [HR]                  ← the caller's side
      ↕       matched against
index field   acl_department = ["HR", "*"]       ← the document's side, from tagging
```

The same attribute name is read twice with opposite meanings: on the caller it means "the department this person
belongs to", on the document "the departments allowed to read this". That is also why `department` appears in
*both* `access-policy.yaml` and `facets.yaml` — and why tagging only the facet grants nobody anything.

#### What `combine:` does

Each attribute produces one clause. `combine:` is the **boolean structure** that joins them — the difference
between "must satisfy every dimension" and "one explicit share is enough":

```yaml
combine:
  all_of: [department, region, clearance]   # AND - every one must allow the caller
  grant_any_of: [employee_id]               # OR  - any one of these is enough on its own
```

which produces `(department AND region AND clearance) OR (employee_id)`. Both halves earn their place. Without
`all_of`, any single matching attribute would grant access — the wrong default for security. Without
`grant_any_of`, sharing one contract with one person outside the owning department would mean widening that
document's department tag for everyone who has it.

What each caller actually gets, straight from the engine:

| The caller | The filter |
|---|---|
| HR · UK · clearance 1 · E1001 | `(acl_department/any(…'HR\|*') and acl_region/any(…'UK\|EMEA\|Global\|*') and acl_clearance le 1) or (acl_employee_id/any(…'E1001'))` |
| Missing a **required** attribute (no region), and no grant | `DENY_ALL` — the search is never executed. |
| Missing the **optional** `clearance` | `… and acl_clearance le 0` — clamped to public rather than denied. |
| **Only** the grant attribute (no department, no region) | `(acl_employee_id/any(…'E1001'))` — explicit shares still reach them. |
| Admin (`rag.admin`) | No filter at all — bypass, and the caller audit-logs it. |

> **A missing required attribute is not an absolute denial.** Rows 2 and 4 differ only by the presence of
> `employee_id`. Missing a required attribute removes the `all_of` branch; it does not remove `grant_any_of`. That
> is deliberate — it is what lets one document be shared with one person without widening its tags — and both the
> OData filter and the Python predicate agree on it.

Two further behaviours worth knowing before you add an attribute: a `grant_any_of` attribute **never honours the
document wildcard** `*`, so a grant must name the caller explicitly; and marking a set-valued attribute
`required: false` has no effect if it also has no wildcard or uses `match: exact` — it will deny exactly as though
it were required.

`rag-os explain --attr department=HR --attr region=UK`, and the **explain access** tool in `/admin`, print this
filter for any set of attributes.

#### Every key you can configure

**Top level** — six keys, of which two are required:

| Key | Default | What it does |
|---|---|---|
| `attributes` | *required* | The list of access attributes. At least one. |
| `combine` | *required* | How the per-attribute clauses are joined. Must reference at least one attribute. |
| `version` | `1` | Your own revision marker. |
| `default_decision` | `deny` | The only accepted value — `allow` is rejected at load. Default-deny is not negotiable. |
| `roles` | `{}` | Application role → the claim values that grant it. Empty means **nobody is an admin**. |
| `role_sources` | see below | Which issuers may assert roles at all. |

**Per attribute** — two required, nine optional:

| Key | Default | What it does |
|---|---|---|
| `name` | *required* | The attribute handle, and the manifest key `acl.<name>`. Must match `^[A-Za-z][A-Za-z0-9_]{0,63}$` and be unique. |
| `field` | *required* | The index column. Same pattern, unique, and must not collide with a base or facet field. |
| `match` | `any_of` | One of the four kinds below. |
| `wildcard` | `"*"` | The document value meaning "everyone". Set `null` to disable — but read the `required` rule first. |
| `required` | `false` | Whether a caller lacking this attribute loses the whole `all_of` branch. |
| `value_pattern` | `^[A-Za-z0-9][A-Za-z0-9 _.@/-]{0,127}$` | Allow-list for caller values. A value failing it is a **401**, never a sanitised filter — this is what makes filter injection impossible. It also excludes `*`, so a caller can never claim the wildcard. |
| `claims` | `{}` | Issuer kind → claim name, e.g. `{ entra: extn.department }`. No entry for an issuer means callers from it never get this attribute. |
| `value_map` | `{}` | Raw claim value → attribute value, case-insensitive. Turns an Entra group object id into `HR`. |
| `drop_unmapped` | `false` | Discard claim values absent from `value_map`. Needs a non-empty map, or the policy fails to load. |
| `hierarchy_facet` | `null` | With `match: hierarchical`, the facet whose tree defines ancestors. Without it, values are treated as `/`-separated paths. |
| `description` | `""` | Documentation for whoever edits this file next. |

`combine` takes `all_of` (AND) and `grant_any_of` (OR). `role_sources` takes `trusted_for_roles` (default
`[entra, dev]`) and `role_claim` (default `{ entra: roles, dev: roles }`).

#### The four `match:` types

| `match` | Clause it emits | Honours `*`? | Use it for |
|---|---|---|---|
| `any_of` | `field/any(v: search.in(v, 'A\|B\|*', '\|'))` | Yes | Set membership — department, audience, cost centre. |
| `exact` | The same clause, but the wildcard is never appended | **No** | Explicit per-person shares. A document tagged `*` is *not* shared with everyone. |
| `hierarchical` | The same clause, after expanding the *caller's* value to its ancestors: `UK` → `UK\|EMEA\|Global` | Yes | Trees — region, org unit. A UK caller reads EMEA and Global documents, not the reverse. |
| `max_level` | `field le N` | n/a | Ladders — clearance. The claim must be an integer. |

> **`required: false` only means something if the attribute can express "open to everyone".** For a caller who has
> no value, a numeric attribute clamps to `field le 0` and a set-valued one with a wildcard emits
> `field/any(v: v eq '*')` — both let them see public documents. But with `wildcard: null`, or with
> `match: exact`, there is no way to express "open to everyone", so the caller is **denied exactly as if the
> attribute were required** — silently, despite the YAML saying otherwise.

#### Defining access by role

Two different things here are called roles, and picking the wrong one is the usual mistake:

| | Application roles | Role as an access attribute |
|---|---|---|
| **Configured by** | `roles:` + `role_sources:` | An ordinary entry under `attributes:` |
| **Controls** | What you may **do** | What you may **see** |
| **Affects retrieval?** | Only `admin`, as a total bypass | Yes — it becomes a clause in the filter |

The four shipped **application roles** and what each unlocks:

| Role | Gates |
|---|---|
| `admin` | Every `/api/admin/*` endpoint — ingestion summary, runs, documents, retry, dead letters, controls, export, sources, sync, config reload and explain. **Also bypasses the access filter entirely**, which is audit-logged. |
| `taxonomy_editor` | Editing `facets.yaml` and `path-rules.yaml`, plus the review queue and tag approval. It can *read* the access policy but **not write it** — editing `access-policy.yaml` or `sources.yaml` needs admin. |
| `reviewer` | The review queue and tag approval. |
| `contributor` | Uploading. This one is not a fixed rule: the upload source's own `allowed_roles` setting decides, defaulting to `["admin", "contributor"]`. |

`role_sources.trusted_for_roles` is what stops an issuer you do not control from asserting any of them. Roles
arriving from an issuer outside that list are dropped — attributes still map, privileges do not.

> **These four role names are the only ones that do anything.** Every permission check names its roles as a
> literal in code, so inventing `roles: { auditor: [rag.auditor] }` gates nothing — the key is accepted, the role
> is granted, and no endpoint ever asks for it. The one exception is uploading, whose `allowed_roles` lives in
> `sources.yaml` and *is* data. And deleting the `admin:` key locks everyone out of every `/api/admin/*` endpoint
> — including the one you would use to put it back.

**For role-based *filtering*, define an ordinary attribute.** Nothing special is needed — point it at whichever
claim carries the role:

```yaml
- name: job_role
  field: acl_job_role
  match: any_of
  wildcard: "*"
  required: false
  claims: { entra: groups }        # or `roles` - but see the warning below
  value_map:
    "5c1e…a91f": Clinician         # one Entra group per job role
    "7b3d…c04e": Pharmacist
  drop_unmapped: true              # ignore the groups that mean nothing here
```

then add `job_role` to `combine.all_of` and tag documents with `acl.job_role`. A pharmacist in the Clinical
department then gets:

```text
(acl_department/any(v: search.in(v, 'Clinical|*', '|')) and acl_job_role/any(v: search.in(v, 'Pharmacist|*', '|')))
```

> **Do not feed a filtering attribute from the same claim that carries your application roles.** If `job_role`
> reads `claims: { entra: roles }` and a user's `roles` claim contains both `kb.clinician` and `rag.admin`, they
> get the `job_role` attribute *and* the admin capability — and admin bypasses the filter completely, so the
> attribute you just built does nothing for them. Use `groups` for filtering and `roles` for capabilities, and
> keep the two namespaces apart.

#### How many attributes can I define?

* **In code: no limit.** The constraints are unique `name`, unique `field`, the identifier pattern, and no
  collision with a base or facet index field.
* **In Azure AI Search: 1,000 fields per index**, shared between the ~15 base fields, every `f_*` facet and every
  `acl_*` attribute. Filter complexity is the other ceiling — Microsoft warns once you reach "hundreds of
  clauses" — but each attribute contributes exactly **one** clause however many values the caller holds, because
  `search.in` packs them into a single call. That is the technique Microsoft recommends for staying under that
  limit, so you will not get near it.
* **In practice: about five.** Every `all_of` attribute is a conjunction, so each one you add is another way for a
  document to be invisible because nobody tagged it. That is the real ceiling, and it is far below the technical one.

> **Three things to know before editing a live policy.** **Adding** an attribute creates its index field
> immediately but leaves it *empty on every existing document*, so a new `required` attribute makes the whole
> corpus invisible until documents are retagged. **Removing** one does the opposite and makes documents *more*
> visible at once, because its clause simply disappears from the filter. **Renaming a `field:`** is not caught by
> validation: the index gains a new empty column, the old one keeps the data, and everything is denied on that
> attribute until re-indexed. Changing an attribute's *type* is the only one of the three refused outright — it
> needs a new index version.

#### Best practices, and the minimum you need

1. **Start with one attribute.** The smallest policy that loads is three keys — a ready-made
   [minimal file](docs/examples/access-policy.minimal.yaml) sits in [docs/examples/](docs/examples/). Add the
   second attribute only once you can tag documents with it.
2. **Make an attribute `required` only if every document will carry it.** A required attribute nobody tags makes
   those documents invisible to everyone, and nothing is raised.
3. **Prefer one Entra group per value** with a `value_map`, over per-person configuration. Membership changes then
   take effect at the caller's next token, with no redeploy.
4. **Keep the wildcard.** `"*"` is how a document is published to everyone, and removing it also removes the
   meaning of `required: false`.
5. **Use `grant_any_of` for exceptions, not as a second dimension.** It is an OR that bypasses every other check —
   right for "share this one contract with this one person", wrong for anything you would describe as a category.
6. **Never put a `max_level` attribute in `grant_any_of`.** It becomes a standalone `field le N`, so clearance
   alone would grant access to every document at or below the caller's level, ignoring department and region.
7. **Never let a model decide access.** Auto-classification (`classify: true`) belongs on business facets, never on
   an attribute listed in `combine`.
8. **Check with `rag-os explain` before you ship.** It prints the exact filter for a given set of attributes, so
   you see what a new attribute did before any user does.


![Figure 8 — How a person's attributes become a search filter](docs/diagrams/access-filter.svg)

*Figure 8 — How a person's attributes become a search filter. Claims are mapped to attributes, each attribute's
match rule contributes one clause, and the combined filter is what Azure AI Search enforces.*

## 13. Entra user attributes and claims

Which attribute of the Entra user object feeds each policy attribute, how to create one that does not exist
yet, and how to put a value on a person. This is the step that decides whether anyone can read anything: a
caller whose token carries no `department` and no `region` passes authentication and then matches no document,
because both are `required: true`.

### The short answer

**The fields you see on a user's profile are not what RAG-OS reads.** *Job Information → Department* is the
built-in `department` property, and Entra **cannot put it in an access token without a claims-mapping policy** —
it is not in the optional-claims list, so the *Token configuration → Add optional claim* blade never offers it.
The same is true of *Office Location*.

| Policy attribute | The profile field you would expect | What RAG-OS actually reads | Why not the profile field |
|---|---|---|---|
| `department` | Job Information → **Department** (`department`) | directory extension `department`, claim **`extn.department`** | the built-in property needs a claims-mapping policy plus `acceptMappedClaims` or a custom signing key |
| `region` | Contact Information → **Country or region** (`country`), or Office Location | directory extension `region`, claim **`extn.region`** | there is an optional claim `ctry`, but it emits only when the value is a two-letter code, and the docs do not say which property feeds it |
| `clearance` | — nothing built in | directory extension `clearance`, claim **`extn.clearance`** | entirely custom; must arrive as an integer |
| `employee_id` | Object ID | **`oid`** | emitted by default, nothing to configure |

A **directory extension** is used for the first three because it is the only route for custom data that needs no
claims-mapping policy, no `acceptMappedClaims` and no custom signing key.

### What the provisioning script does for you

`./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev` creates the three directory extensions on the API's own
app registration and registers them as access-token optional claims. It is idempotent, and it prints the claim
names it registered. Pass `-SkipUserAttributes` when directory schema is governed separately or you lack the
directory role — the rest of the registration still reconciles.

Creating the attribute is only half of it. **A value still has to be put on each person**, and that is not
automated, because it is a statement about a human being.

### Setting a value on a person

There is **no portal UI** for this — not for creating a directory extension, and not for setting one on a user.
Graph only:

```bash
# the registered name is extension_<your app id without hyphens>_<name>
az rest --method patch --url https://graph.microsoft.com/v1.0/users/priya@contoso.com \
  --body '{"extension_<appid>_department": "HR", "extension_<appid>_region": "UK", "extension_<appid>_clearance": "1"}'
```

`clearance` must be **a quoted string holding an integer** — `"1"`, not `1`. The extension is declared
`dataType: String`, and Graph rejects a JSON literal whose type does not match the declaration with a bare 400.
The rungs are `0` Public, `1` Internal, `2` Confidential, `3` Restricted, as configured under `levels:` in
`access-policy.yaml`.

Three things about that call that look like failures and are not:

* **A successful PATCH is `204 No Content` with an empty body.** `az rest` prints nothing at all. Silence is
  success.
* **Reading the value back needs `$select`.** A plain `GET /users/{id}` returns eleven default properties, and
  your extension is **absent, not null** — which reads exactly like "the write did not work". Name each one:

  ```bash
  az rest --method get --url "https://graph.microsoft.com/v1.0/users/priya@contoso.com?\$select=id,extension_<appid>_department,extension_<appid>_region,extension_<appid>_clearance"
  ```

  Graph Explorer's **beta** endpoint returns extensions without `$select`, so testing there and then scripting
  against `v1.0` gives two different answers and both are correct.
* **The token has to be for this API.** An access token is always built from the *resource's* manifest, so a token
  requested for Microsoft Graph carries no `extn.*` claims however well the attributes are set. Request
  `api://<client-id>/access_as_user` — which is what the chat UI does, so Account Information is the honest test.

### Checking that it worked

In this order — each step rules out everything before it:

1. **Decode the token.** Sign in, take the access token and paste it into <https://jwt.ms>. Confirm which claim
   name actually arrived. Microsoft's own documentation gives **two** spellings for a directory-extension claim,
   `extn.department` and `extension_<appid>_department`; RAG-OS accepts either, but seeing which one your tenant
   emits settles it.
2. **`GET /api/me`** — shows the attributes as the policy mapped them.
3. **Account Information** in the chat UI, which names the claim each value arrived in. If Department is empty
   there, the claim did not arrive; if it is populated, the chain works end to end.

### Assigning values from the admin console

**Settings (Security)** in the admin console does everything above through a form: look a person up by email,
pick their department, region and clearance from the master lists, tick the application roles they should hold,
and save. It writes directly to Microsoft Entra ID through Microsoft Graph.

It is **off by default** and has to be switched on deliberately, because it needs Graph permissions that can
rewrite any user in the tenant:

```powershell
./infra/scripts/Set-EntraAppRegistration.ps1 -Env dev      # records the enterprise application's object id
./infra/scripts/Set-EntraGraphPermissions.ps1 -Env dev -DryRun
./infra/scripts/Set-EntraGraphPermissions.ps1 -Env dev
```

Then set `ExtraAppSettings = @{ DIRECTORY = 'graph' }` and `DevAuthEnabled = $false` in `infra/env/<env>.psd1`,
and re-run `07-container-apps.ps1`. What an administrator may assign comes from `allowed_values` (department and
region) and `levels:` (clearance) in `access-policy.yaml` — the API validates against those and never against
what the browser sent.

#### Graph permissions this application needs

Three different principals are involved, and conflating them is the usual reason this fails. The identity that
**calls** Graph is the managed identity, not the app registration.

| Principal | Permission | Type | Why | Who can grant it |
|---|---|---|---|---|
| Managed identity `id-<prefix>-<env>` | `User.ReadWrite.All` | Graph **application** | Find a person by address, read their directory extensions, write the three attributes, end their sign-in sessions | Privileged Role Administrator or Global Administrator |
| " | `AppRoleAssignment.ReadWrite.All` | Graph **application** | **Create and delete** this application's own app-role assignments. It does *not* permit reading them | Privileged Role Administrator or Global Administrator |
| " | `Application.Read.All` | Graph **application** | **Read** this app's own service principal: its `appRoles` catalogue, and who currently holds them. Read-only, and easy to miss — see the note below | Privileged Role Administrator or Global Administrator |
| " | `GroupMember.Read.All` | Graph **application** | Read group membership, so a role held *through a group* shows as non-removable instead of silently missing | Privileged Role Administrator or Global Administrator |
| Operator running `Set-EntraAppRegistration.ps1` | `Application.ReadWrite.All` | delegated, or the Application Administrator / Cloud Application Administrator / Directory Writers role | Create the three directory extensions and add them to `optionalClaims` | any of those roles |
| Operator setting a value by hand | a role covering `microsoft.directory/users/extensionProperties/update` | directory role | The `az rest` fallback above | Global Administrator (confirmed). **User Administrator is unverified — test it before delegating** |

Things stated plainly rather than smoothed over:

* **`AppRoleAssignment.ReadWrite.All` does not let you *read* an app-role assignment.** It covers the write —
  `POST` and `DELETE` on `appRoleAssignedTo` — and nothing else. Both
  `GET /servicePrincipals/{id}/appRoleAssignedTo` and `GET /servicePrincipals/{id}?$select=appRoles` are reads of
  the **service principal**, whose least-privileged permission is `Application.Read.All`; Microsoft documents
  *creating* an assignment as needing **both**. Granting only the write permission produces
  `403 Insufficient privileges to complete the operation` on the first lookup — which reads exactly like the write
  permission is missing, and sends you to re-check the one that is already there. `Application.Read.All` is
  read-only, so it is the cheaper half of the pair to justify.

* **`User.ReadUpdate.All`** is Microsoft's newer least-privileged permission for `PATCH /users/{id}` and would be
  preferable. We have not confirmed it also covers the `$filter` lookup or `revokeSignInSessions`, so
  `User.ReadWrite.All` is the supported configuration, and `User.ReadUpdate.All` plus `User.RevokeSessions.All`
  is a narrowing to test in your own tenant before relying on it.
* **Application Administrator is not sufficient to consent to any of these** — and the reason is broader than
  it looks. It is not that one of them is unusually dangerous; it is that **all three are Microsoft Graph *app
  roles*** (application permissions), and that entire category is carved out of the Application Administrator,
  Cloud Application Administrator and AI Administrator roles. Their consent permission is literally
  `microsoft.directory/servicePrincipals/managePermissionGrantsForAll.microsoft-application-admin`, documented as
  *"Grant consent for application permissions and delegated permissions on behalf of any user or all users,
  **except for application permissions for Microsoft Graph and Azure AD Graph**"*. So `-SkipRoleAssignment` does
  **not** lower the role you need — it only reduces what you are granting.
* **`Privileged Role Administrator` is the least-privileged role that works**, documented as consenting "for apps
  requesting any permission, for any API". Global Administrator also works, being a superset. A **custom
  directory role** carrying the consent permission is the third documented option.
* **Azure RBAC is the wrong kind of role.** Subscription Owner, Contributor and User Access Administrator grant
  nothing here — these are Entra **directory** roles, a separate system.
* **If the role is assigned through PIM, activate it first.** An *eligible* but unactivated role produces exactly
  the same `403 Authorization_RequestDenied` as having no role at all. Check what is active with:

  ```bash
  az rest --method get --url "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole?\$select=displayName"
  ```

* **Assigning this application's own roles needs far less.** `Set-EntraAppRoleAssignment.ps1` grants `rag.admin`
  to a person with the *same* Graph call — `POST /servicePrincipals/{id}/appRoleAssignedTo` — and Application
  Administrator is enough for it, because the resource is *our* service principal rather than Microsoft Graph's.
  One call, two privilege levels, decided entirely by which service principal is the resource. That is the
  distinction to keep hold of when someone asks why one script needs more than the other.

#### Seeing what is actually granted

**The blade you would look in first is the wrong one, and it is empty rather than wrong-looking.** Two directory
objects are involved:

| Object | Recorded in `infra/env/<env>.outputs.json` as | What it holds |
|---|---|---|
| The app registration people sign in through | `entraServicePrincipalObjectId` | The `rag.*` app roles that **people** are assigned. It calls no API itself, so its *API permissions* blade will never list the three above. |
| The managed identity `id-<prefix>-<env>` | `identityPrincipalId` | The three Graph permissions. This is the thing that calls Graph, and it has **no app registration at all**. |

So an empty *API permissions* list on the app registration is the expected result, not evidence that the grant
failed.

**In the portal, to look.** *Entra ID → Enterprise applications* → change **Application type** from its default of
*Enterprise Applications*, which excludes managed identities, to **Managed Identities** → open
`id-<prefix>-<env>` → **Permissions**.

**The portal cannot grant these.** There is no UI anywhere in it for assigning a Graph *application* permission to
a managed identity — that Permissions blade is read-only for this purpose. It is why
`Set-EntraGraphPermissions.ps1` exists, and why a `403` from that script cannot be worked around by clicking; it
needs somebody holding `Privileged Role Administrator`, per the table above.

**On the command line, authoritatively.**

```powershell
./infra/scripts/Set-EntraGraphPermissions.ps1 -Env dev -List
```

It reads from the identity's own side of the assignment, which is the direction that does not have to page through
every Graph consent in the tenant. Without the repo to hand, the same read is:

```bash
az rest --method get   --url "https://graph.microsoft.com/v1.0/servicePrincipals/<identityPrincipalId>/appRoleAssignments"
```

Both list one row per permission. An `appRoleId` shown without a name means the row is an assignment on some other
API, not on Microsoft Graph.

#### Caveats and limits

**Blast radius**

* `AppRoleAssignment.ReadWrite.All` is **tenant-wide and cannot be scoped**. It permits granting any app role on
  any service principal in the tenant, Microsoft Graph's own included — so an identity holding it can grant
  itself Global Administrator. It lands on the same managed identity that already holds this deployment's Search,
  Blob and PostgreSQL data-plane roles, which makes a compromise of the API a compromise of the tenant. What
  keeps it narrow is **our code, not Entra**: the adapter sends only its own service principal as the
  `resourceId`, and only ever uses `appRoleId` values read back from its own registration. If you would rather
  not grant it, `Set-EntraGraphPermissions.ps1 -SkipRoleAssignment` leaves it out — the attribute half of the
  page still works, and role assignment stays a terminal job for `Set-EntraAppRoleAssignment.ps1`.
* **`DEV_AUTH_ENABLED` must be false.** Dev tokens are self-asserted and the access policy trusts them for roles,
  so with these permissions in hand anyone who can reach the API could grant themselves anything. The API refuses
  to construct the directory adapter while dev auth is on, rather than warning about it.
* **An administrator cannot edit their own access** through the page — not their attributes and not their roles.
  Two administrators can still raise each other, which is why granting an administrator role requires typing the
  person's user principal name and is logged at WARNING.
* **`rag.admin` bypasses the document access filter entirely.** Granting it is not "a bit more access", it is all
  of it.
* Documents shared with one person through `employee_id` (`grant_any_of` in the policy) are outside every
  attribute this page controls, so it does not show everything somebody can read.
* **The audit record contains personal data** — email addresses, clearance levels, before-and-after values — and
  goes wherever the application's logs go, which in the reference deployment is Application Insights under its
  retention policy.

**Propagation and timing**

* **Restart the API after granting the Graph permissions.** The managed identity caches its Graph token until it
  expires, so a container started beforehand keeps presenting a token without them and every write fails with 403
  for up to an hour. `./infra/scripts/07-container-apps.ps1 -Env <env> -Only rag-api` does it.
* **A person's own token keeps the old values for 60–90 minutes** (up to 2 hours, or 20–28 hours where Continuous
  Access Evaluation is on — which makes this worse rather than better, because an attribute change is not one of
  CAE's revocation triggers). The refresh is silent, so nobody has to sign in again; they have to wait.
* **Revoking sign-in sessions invalidates *refresh* tokens.** It ends the wait for the next token, but the access
  token already in that person's browser stays valid until it expires — and it signs them out of Teams, Outlook
  and everything else in the tenant. It is an opt-in checkbox on the page, off by default.
* **Graph is eventually consistent.** A read immediately after a write can still return the previous value.

**Graph behaviours that look like bugs**

* Extension names are **case-sensitive when the token service reads them**, though not when a value is set.
  `Clearance` on one person and `clearance` on another means only one of them gets a claim, with no error either
  way. The provisioning script pins the casing so this cannot happen by hand.
* **Graph does not deduplicate app-role assignments.** The same grant posted twice is two rows, and removing one
  of them leaves the person holding the role. The page writes desired state with a `PUT` and collapses any
  duplicates it finds.
* `appRoleAssignedTo` **pages at 100 rows.** Reading only the first page of a busy tenant reports "holds no
  roles", after which a grant creates a duplicate.
* **100 extension values per directory object**, across every application. If a definition is deleted before its
  values are set to `null`, the data becomes permanently undiscoverable *and still counts* toward that 100 — so
  the decommission order is always null-the-values, then delete the definition.
* **MSA (personal Microsoft account) users receive no custom extension claims.** Microsoft's optional-claims
  reference states it outright: *"If your application manifest requests a custom extension and an MSA user logs in
  to your app, these extensions aren't returned."* The values are genuinely written on the user object and the
  admin page reads them back correctly through Graph — the token service simply omits them, so such a caller used
  to arrive with no department and no region and, both being required, read nothing, with no error anywhere.

  **RAG-OS fills this in.** When an Entra token carries no `extn.*` claim, the API reads that caller's attributes
  from Graph by the `oid` in their own signed token, caching for five minutes. Three properties of that fallback
  are worth knowing:
  * A claim the token **does** carry is never overridden. The issuer signed it; the directory did not.
  * Nothing is read when the token is complete, so a work account costs no extra call and no extra latency.
  * It needs `DIRECTORY=graph`. Without a directory adapter there is nothing to ask, and such a caller still
    reads nothing.

  An attribute change therefore reaches a guest within five minutes, rather than the 60–90 the claims path needs.
  The alternative remains the `groups` route (see `access-policy.yaml`), which works for guests because group
  membership *is* emitted in their tokens.
* If a **Claims Mapping Policy** exists on the service principal, it overrides the claims configuration shown in
  the portal for that application.

**Identity resolution**

* A B2B guest's user principal name contains `#EXT#`, and `#` begins a URL fragment — so a UPN must never be put
  in a request path, or the call silently addresses a different user, or none. A UPN beginning `$` fails path
  addressing outright. The adapter resolves an address to an object id once and uses only the id afterwards.
* `mail` is not `userPrincipalName` (a very common tenant has `mail: first.last@contoso.com` and
  `userPrincipalName: 12345@contoso.onmicrosoft.com`), and **`mail` is not unique** — two matches is a 409 naming
  both, never a first row to pick.
* A **group** address returns 404. Granting a whole department through a group is set up in Entra, not here.
* A role held **through a group** is shown with the group's name and cannot be removed from this page.
* A cloud-created directory extension is **single-valued**, so somebody in two departments needs the `groups`
  route described below.

### Three things that cost people an afternoon

* **Custom security attributes are not this.** Entra has a feature by that name, its own documented example is
  "add Hourly Salary, visible only to administrators", and it looks exactly right for a clearance level. It
  **cannot appear in a token at all** — Microsoft lists JWT and SAML claims as explicitly unsupported. It is for
  Azure RBAC conditions and inventory.
* **Extension names are case-sensitive when read.** Register `Clearance` on one user and `clearance` on another
  and only one of them gets a claim.
* **A cloud-created directory extension is single-valued.** Somebody who belongs to two departments cannot be
  expressed this way. Use the security-group route instead: point `claims.entra` at `groups` and map each
  group's object id with `value_map` — [Giving HR or Sales access to hundreds of
  people](#giving-hr-or-sales-access-to-hundreds-of-people) covers it, including the 200-group overage limit.

### If you would rather use the built-in Department property

It is supported, and it is more work. Create a **claims-mapping policy** on the API's service principal with
`{"Source":"user","ID":"department","JwtClaimType":"department"}`, set `acceptMappedClaims: true` on the app
registration (single-tenant) or configure a custom signing key (multitenant), and change `claims.entra` to
`department` in `access-policy.yaml`. Microsoft warns against `acceptMappedClaims` for multitenant apps, and the
app id URI must match the application GUID or a verified domain.

## 14. Observability

* Every response carries `X-Correlation-ID`, which is created by nginx or the client. It travels on queue messages,
  so one ID traces a document from upload to indexing.
* JSON logs go to stdout, and to App Insights when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (from Key Vault):
  * spans: `rag.chat`, `rag.ingest`, `rag.*` stages
  * metrics: `rag.tokens{kind,provider,model,purpose}`, `rag.stage.duration`, `rag.ingest.docs{status}`
* **Token usage** is returned with every answer (`usage.input/output/cache_read/cache_write/embedding`, plus the
  split by purpose).
* KQL for traces, token dashboards, failures and alerts: [`docs/kql.md`](docs/kql.md).
* Metrics `rag.answers{outcome,reason,verdict}` and `rag.expectations{result}` feed the query-health alerts.

### Query traces: why did this person get no answer?

**Admin > Query traces** records every question stage by stage, from the person's attributes through the
access filter, search hits, the relevance bar and the model to the citations. Administrators only: a trace
holds the question, the answer, and for a refused question the titles of documents the person was *not*
allowed to read. Traces are purged after `QUERY_TRACE_RETENTION_DAYS` (30).

* **The picture.** Twelve stages in the order they ran. Red marks where it went wrong, amber is worth a look,
  and grey did not run. Select a stage to see its data.
* **The verdict.** It separates a correct no-answer from a problem, using evidence only. When a question is
  refused, a *near-miss check* repeats the search with the access clause removed and everything else held
  constant. Any difference between the two result sets is therefore caused by access alone.

  | Verdict | What it means | Problem? |
  |---|---|---|
  | Answered with citations | Every citation came from a passage the person may read | no |
  | Not in the documents | Nothing cleared the relevance bar, even without the access filter | no |
  | Withheld by access policy | Relevant documents exist; the policy withholds them; nothing looks wrong | no |
  | Suspected misconfiguration | As above, but a document's tags or the person's attributes look wrong | **yes** |
  | Should have answered: retrieval | A relevant document the person *may* read was not returned to them | **yes** |
  | Should have answered: generation | Passages reached the model, which refused or did not cite them | **yes** |
  | Error | The pipeline failed (index not ready, search unavailable, an exception) | **yes** |

* **The near-miss table.** Each relevant document gets one cell per access attribute: ✓ means that attribute
  lets the person read it, and a red ✕ names the document's value against the values the person reaches.
* **Health.** A live strip shows the problem rate, error rate, p95 latency, failing expectations and anyone
  being refused repeatedly. The rates count problem verdicts, not refusals, so a correct refusal never raises
  an alert.
* **Opening a trace from chat.** An administrator sees **Open trace** under every answer. Anyone else can
  send the correlation ID shown under the answer, which an administrator pastes into *Open by correlation ID*.

**Proving the right answer: expectations.** A trace shows *why*, but only a person knows what the right
outcome is. On any trace, record *Expected: answer (must cite these documents)* or *Expected: no answer*.
**Replay** runs the question again through the real pipeline, as the same attributes, and passes only if the
outcome matches and every required document is cited. All expectations are replayed every
`QUERY_EXPECTATION_REPLAY_HOURS` (24), and a failure turns the health strip red.

**Worked example (what actually happened).** An HR / AMER / clearance-2 user asks about the 401(k) plan and
gets no answer.
* `is_current` was not the cause: every chunk is written with `is_current = true`, and stale versions are
  deleted, not flagged. The near-miss check also searches without `is_current` and would name any chunk not
  marked current.
* `Benefits.pdf` had been uploaded, before the Only me box existed, by an administrator whose token carried no
  Department or Region. Uploads then copied the uploader's attributes, so both copies were indexed with
  **empty** Department and Region tags, and only the uploader could read them.
* The trace shows *Suspected misconfiguration*, "The document has no Department access tag". The fix is Admin >
  Documents > **Edit access tags** (HR · Global · 1); then delete the duplicate copy. Uploads are now refused
  in this situation unless a Department and Region are picked, or Only me is ticked.

Other things the trace can show:
* "classified Department HR but its access tag says IT": an access tag that contradicts the classification.
* "the access tags in the index differ from the tags recorded": re-tag or re-ingest the document.
* *Withheld by access policy*, red only on Region (`US`; the caller reaches `AMER, Global`). That is the policy
  working as configured, because hierarchy runs upward only.
* *Suspected misconfiguration* on the User context stage: "the account has no Department". The token carries
  no department claim yet; sign in again, or see [section 13](#13-entra-user-attributes-and-claims).

Record *Expected: answer, must cite Benefits.pdf*, fix the tags, then press **Replay**.

The access tags are deliberately **not retrievable** from the search index. So the near-miss check asks the
index to evaluate each attribute's own filter clause for each relevant document; that is the same OData the
real query runs, so the verdict cannot disagree with it. The tag values shown beside each verdict come from the
tags recorded at ingestion.

## 15. Scaling and operations

* **Capacity model:**
  * chunks ≈ documents × average chunks per document
  * embedding time ≈ chunks ÷ (GPU replicas × chunks/s per T4)
  * index size ≈ chunks × dimensions × 1 byte (int8) + text

  The `qwen3-0.6b-512` profile halves the vector size. Tune with the `.psd1` values `WorkerMaxReplicas`,
  `GpuMaxReplicas`, `SearchReplicas` / `SearchPartitions` and `IngestMaxConcurrency`.
* **Which embedding profile?** Only one is ever active — `EmbeddingProfile` picks it, and the rest are inert.
  **Stay on the `qwen3-0.6b-1024` default**: at 10M chunks its vectors are ~10 GB against a 35 GB S1 vector quota,
  so dimensions are not the binding constraint. Dimensions decide which 50 candidates the vector arm nominates;
  the semantic ranker that orders the results reads raw text and never sees a vector, so 512 costs recall rather
  than ranking quality. Full reasoning, the sizing arithmetic and the query/document prefix warning are in
  [Deployment.md section 7](Deployment.md#7-embedding-profiles-which-to-run-and-how-to-change-it).
* **Backfill playbook.**
  1. `Scale-SearchReplicas.ps1 -Replicas 3`
  2. `Set-IngestionControls.ps1 -MaxConcurrency N`
  3. Sync the source off-peak.
  4. Watch `/admin` → Runs.
  5. Scale back.
* **Changing the embedding model.** A new profile means a new fingerprint, so a new index — you never
  re-embed in place, and the old index keeps serving while the new one fills. Changing the profile now marks
  every document as needing re-indexing, so discovery re-queues the whole corpus. The cutover is a deploy,
  not an instant swap, and a change of *model* (rather than dimensions) needs a maintenance window because
  the query pool cannot serve two models at once. Full runbook, including capacity and which sources can be
  re-ingested at all, in [Deployment.md section 7](Deployment.md#7-embedding-profiles-which-to-run-and-how-to-change-it).
* **Database schema.** Owned by Alembic. `rag-os bootstrap` (the `rag-bootstrap` job) runs `alembic upgrade head`
  before touching anything else. Add a change with
  `uv run alembic revision --autogenerate -m "…"`, review it, then deploy; the bootstrap job applies it.
* **Failure handling.**
  * Permanent failures (corrupt file, unsupported format, no extractable text) are marked `FAILED` immediately.
  * Transient failures retry with backoff, then go to the dead-letter queue.
  * `POST /api/admin/ingestion/retry` requeues documents.
  * The scheduler's reconciliation requeues documents stuck in flight.

## 16. Testing

| Command | Covers |
|---|---|
| `./tasks.ps1 test` | **Unit and offline integration** (pytest + hypothesis, about 5 s): <ul><li>access policy, including a property test that the OData filter and the Python predicate always agree, and injection cases</li><li>JWT validation</li><li>all 10 parsers, including XXE and JSONP edge cases</li><li>chunker</li><li>state machine and change detection</li><li>embedding profile guard</li><li>TEI adapter with HTTP mocked</li><li>the end-to-end pipeline: sample corpus → per-principal answers, sidecar grant, idempotent re-run, retag without re-embedding, deletion, and adding an attribute through YAML only</li><li>the HTTP API contract</li></ul> |
| `./tasks.ps1 lint` | ruff + mypy (strict on `domain` and `application`). |
| `./tasks.ps1 smoke -BaseUrl <url>` | Deployed checks through the public URL: <ul><li>health and readiness (profile guard)</li><li>security headers</li><li>principal A vs B access</li><li>facet trimming</li><li>token usage</li><li>upload → INDEXED</li><li>admin report and role checks</li><li>internal API not public</li></ul> |
| `./tasks.ps1 synthetic -Docs 10000`, then `./tasks.ps1 loadtest -BaseUrl <url>` | Retrieval p50/p95 at a fixed request rate, **before and during** a bulk backfill. PASS if p95(during) ≤ 1.25 × p95(baseline). Also reports ingestion throughput for the capacity model. |

## 17. Troubleshooting / FAQ

**Start here when the knowledge base will not answer.** From the container app's *Monitoring → Console*
(`rag-api`, container `api`):

```sh
rag-os doctor
```

Read-only, changes nothing, exits non-zero when the system cannot serve a query. It prints the index, the
expected vs stored embedding fingerprint, the database result and both embedder pools — with the **real** error
messages, which `/api/readyz` deliberately omits because it is reachable without authentication. Full reference,
including how to read each field: [Deployment.md](Deployment.md#rag-os-doctor---the-read-only-diagnostic).

| Symptom | Cause and fix |
|---|---|
| Upload, Uploads, Documents or retry return **"Internal server error"** while chat still works | The database is missing columns this build expects — an image deployed without its migrations. The column list comes from the code's table metadata, so one missing column breaks every full-row read of that table. `GET /api/readyz` now names both revisions, and `rag-os doctor` reports it. Fix: `rag-os bootstrap`, or in Azure `./infra/scripts/08-bootstrap.ps1 -Env dev`. Since 2026-09-29 the request answers 503 "The database schema is out of date" rather than a bare 500, and step 07 refuses to deploy into it. |
| `/api/readyz` returns 503 `embedding_profile` | The body names the cause; `rag-os doctor` gives the full error. Either a pool serves a different model/revision/dimensions than the profile, or the index has no recorded profile (`rag-os bootstrap`). There is deliberately no fallback embedder. |
| Worker logs "worker idle: embedding profile guard failing" | Same as above. The worker refuses to index vectors from an unexpected model. |
| Answers are plausible but wrong, citing unrelated passages | Documents and queries may have been embedded by different models — searching one vector space with another's vector returns arbitrary passages with full confidence, and nothing errors. Run `./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev`, and find affected chunks with the index filter `embedded_by ne '<model>@<revision>'`. |
| 403 from Search, Storage, Service Bus or Key Vault right after provisioning | Role assignments take 5–10 minutes to apply. Re-run the step; scripts retry. |
| 401 `untrusted issuer` / `token lifetime exceeds the allowed maximum` | The token must come from a trusted issuer `iss`, `aud=rag-os` and a lifetime of ≤ 15 minutes. |
| Answers always "could not find…" | The caller has no matching attributes (see `/admin` → Explain access), or the documents aren't INDEXED yet. |
| GPU workload profile fails to create | Quota or region. The scripts fall back to the CPU ingestion pool (same model and profile). |
| An embedder pool never becomes ready, log ends at "Warming up model" | An OOM kill (exit 137), not a hang — and the ONNX 404s above it are normal. `MAX_INPUT_LENGTH` bounds the warm-up; more memory does not help. See [Deployment.md](Deployment.md#when-a-tei-pool-never-becomes-ready). |
| `uv` TLS errors | Set `UV_NATIVE_TLS=1` (tasks.ps1 does). |
| Local folder source cannot sync from the admin page | Local folders must be discovered where they are mounted: `rag-os discover --source <id>`. |

## 18. Roadmap, contributing, licences

**Roadmap:**
* **Connectors:** SharePoint via the Azure AI Search indexed SharePoint knowledge source (with ACL sync), then Google Drive and SFTP.
* **Foundry integration:**
  * Foundry IQ knowledge base with a custom Web API vectorizer for the self-hosted model
  * publish as a Foundry Agent Service agent (Copilot Studio, Teams)
  * knowledge-base answer synthesis once it is GA
* **Entra and data protection:** Entra-token enforcement for internal users, Purview labels.
* **Networking:** private endpoints and VNet.
* **CI/CD:** a pipeline wrapping `infra/scripts`, with eval gates.
* **Answering and cost:** SSE streaming, semantic cache, APIM AI gateway with token limits.
* **Editors:** UI editors for facets and policy.
* **Parsing:** Content Understanding for scanned documents.

**Contributing:**
* Keep dependencies pointing inward (`domain` imports nothing from outer layers).
* Add adapters by registering them in a factory registry.
* Run `./tasks.ps1 lint` and `./tasks.ps1 test`.

**Licences:**
* RAG-OS code: proprietary.
* Embedding model: Qwen3-Embedding-0.6B (Apache-2.0).
* TEI: Apache-2.0.
* Python dependencies: permissive (MIT, BSD, Apache). pypdf is BSD; PyMuPDF is deliberately not used.
