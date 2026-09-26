# RAG-OS deployment guide

Sequential, copy-pasteable deployment of RAG-OS to Azure with the az CLI in PowerShell 7. Every script is idempotent:
re-running a step is always safe, and a failed run is resumed by running the same step again.

> **Local development** (docker compose, no Azure): see [README.md](README.md). This guide is only about Azure.

Steps are numbered the way the scripts number themselves, so "step 07" always means `07-container-apps.ps1`.
References to **section** N mean a numbered section of this guide.




<!-- toc:begin -->
## Table of contents
* [Retries, and what the transcript tells you](#retries-and-what-the-transcript-tells-you)
1. [Prerequisites](#1-prerequisites)
    * [Tools](#tools)
    * [Permissions](#permissions)
    * [Region checklist](#region-checklist)
2. [Fill in `infra/env/dev.psd1`](#2-fill-in-infraenvdevpsd1)
    * [Create the Entra app registration first](#create-the-entra-app-registration-first)
    * [Where settings come from](#where-settings-come-from)
    * [What you must set, and what you can ignore](#what-you-must-set-and-what-you-can-ignore)
    * [You do not name resources — they are derived](#you-do-not-name-resources--they-are-derived)
    * [The shortest file that deploys](#the-shortest-file-that-deploys)
    * [When it will not load](#when-it-will-not-load)
    * [Every setting](#every-setting)
3. [Run the steps](#3-run-the-steps)
    * [Step 00 — `00-prereqs.ps1`](#step-00--00-prereqsps1)
    * [Step 01 — `01-foundation.ps1`](#step-01--01-foundationps1)
    * [Step 02 — `02-identity-keyvault.ps1`](#step-02--02-identity-keyvaultps1)
    * [Step 03 — `03-data.ps1`](#step-03--03-dataps1)
    * [Step 04 — `04-search.ps1`](#step-04--04-searchps1)
    * [Step 05 — `05-foundry.ps1`](#step-05--05-foundryps1)
    * [Step 06 — `06-registry-build.ps1`](#step-06--06-registry-buildps1)
    * [Step 07 — `07-container-apps.ps1`](#step-07--07-container-appsps1)
    * [Step 08 — `08-bootstrap.ps1`](#step-08--08-bootstrapps1)
    * [Diagnosing a 503 from `/api/readyz`](#diagnosing-a-503-from-apireadyz)
    * [Step 09 — embedding alignment](#step-09--embedding-alignment)
    * [Steps 09-10 — verification](#steps-09-10--verification)
4. [Key Vault secrets](#4-key-vault-secrets)
    * [Rotation](#rotation)
    * [`output.txt` — the wiring sheet](#outputtxt--the-wiring-sheet)
5. [Application settings](#5-application-settings)
6. [Domain configuration](#6-domain-configuration)
    * [Edit and publish](#edit-and-publish)
7. [Embedding profiles: which to run, and how to change it](#7-embedding-profiles-which-to-run-and-how-to-change-it)
    * [Which profile should I run?](#which-profile-should-i-run)
    * [How dimensions affect answer quality](#how-dimensions-affect-answer-quality)
    * [Levers that matter more than dimensions](#levers-that-matter-more-than-dimensions)
    * [Switching to the remote embedder](#switching-to-the-remote-embedder)
    * [Migrating to a different embedding model](#migrating-to-a-different-embedding-model)
8. [Claude on Foundry](#8-claude-on-foundry)
    * [Which model for which role](#which-model-for-which-role)
9. [Signing people in with Microsoft Entra ID](#9-signing-people-in-with-microsoft-entra-id)
    * [9.1 One app registration, both sides](#91-one-app-registration-both-sides)
    * [9.2 Carry the attributes](#92-carry-the-attributes)
    * [9.3 Admin role](#93-admin-role)
    * [9.4 Point RAG-OS at the tenant](#94-point-rag-os-at-the-tenant)
    * [9.5 Check it end to end](#95-check-it-end-to-end)
    * [9.6 Embedding the assistant in another page](#96-embedding-the-assistant-in-another-page)
10. [Operations](#10-operations)
    * [Ingestion from a local folder (admin machine)](#ingestion-from-a-local-folder-admin-machine)
    * [Status, retries, throttling](#status-retries-throttling)
    * [Backfill playbook](#backfill-playbook)
    * [Smoke test](#smoke-test)
    * [Load test](#load-test)
    * [KQL (Log Analytics / Application Insights)](#kql-log-analytics--application-insights)
11. [Update, rollback, teardown](#11-update-rollback-teardown)
    * [Update (new code)](#update-new-code)
    * [Rollback](#rollback)
    * [Teardown](#teardown)
12. [Appendix — what runs where](#appendix--what-runs-where)

<!-- toc:end -->

| Step | Script | Creates | Needs | Typical time |
|---|---|---|---|---|
| 00 | `00-prereqs.ps1` | tools, login, providers, region/quota checklist | an Azure subscription | 2-5 min |
| 01 | `01-foundation.ps1` | resource group, Log Analytics, App Insights, budget | 00 | 2 min |
| 02 | `02-identity-keyvault.ps1` | managed identity, Key Vault, 2 secrets, roles | 01 (App Insights) | 2 min |
| 03 | `03-data.ps1` | Storage + containers, PostgreSQL, Service Bus + queues | 01, 02 | 10-15 min |
| 04 | `04-search.ps1` | Azure AI Search S1 | 01, 02 | 5-15 min |
| 05 | `05-foundry.ps1` | Foundry account, project, model deployments | 01, 02 | 3-5 min |
| 06 | `06-registry-build.ps1` | ACR + 4 images built in the cloud | 01, 02 | 15-30 min |
| 07 | `07-container-apps.ps1` | environment, workload profiles, 5 apps + 2 jobs | **01-06** | 10-15 min |
| 08 | `08-bootstrap.ps1` | connectivity pre-flight, config upload, migrations, index, first discovery, `output.txt` | 07 | 5-10 min |
| 09 | `09-smoke.ps1` | verification (optional but recommended) | 08 | 2 min |
| 10 | `10-loadtest.ps1` | latency under ingestion load (optional) | 08 | 20-60 min |

**Everything at once:** `./infra/scripts/provision-all.ps1 -Env dev` runs **00 → 08** in order, stops at the first
error, and prints the time per step. Steps 09 and 10 are deliberately outside it, because they are verification
rather than provisioning. Resume after a failure with `-From <step>`; the script prints the exact command to use.

```powershell
./infra/scripts/provision-all.ps1 -Env dev              # first run, steps 00-08
./infra/scripts/provision-all.ps1 -Env dev -From 6      # rebuild images and redeploy
./infra/scripts/provision-all.ps1 -Env dev -From 1 -To 5  # infrastructure only
```

> **Run step 00 at least once before using `-From 1`.** Skipping it does not break the outputs chain — step 00
> writes only `subscriptionId`, `tenantId` and `location`, which nothing reads back. What it *does* do is
> `az login`, install the `application-insights` CLI extension, and **register the Azure resource providers**.
> These scripts create resources with direct `az <service> create` calls, and Azure only auto-registers providers
> for ARM *template* deployments — so an unregistered namespace returns `MissingSubscriptionRegistration`, which
> is not retried. On a fresh subscription `Microsoft.DBforPostgreSQL`, `Microsoft.Search` and
> `Microsoft.CognitiveServices` are the usual misses, so the run dies at step 03, 04 or 05 — *after* 01 and 02
> have already created resources. Without the `application-insights` extension, step 01 also stalls on a hidden
> CLI install prompt that looks like a hang.
>
> The cheap fix is to run it: `./infra/scripts/00-prereqs.ps1 -Env dev` (2–5 min, idempotent).

**The `Needs` column is the real dependency graph, and it is looser than the numbering.** Every cross-step value
goes through `infra/env/<env>.outputs.json`, and a missing one throws *"Output 'x' is missing … Run `<script>`
first"* before any Azure call is made. Steps **03, 04, 05 and 06 do not depend on each other** — they only need 01
and 02 — so on a repeat deployment you can run them in any order, or in parallel shells, and they are also the
four slowest. Step 07 checks all fifteen of its inputs up front, so it fails in seconds if something is missing
rather than half-deploying.

**Other scripts in `infra/scripts/`:**

| Script | What it is for |
|---|---|
| `provision-all.ps1` | The orchestrator above. |
| `Write-OutputSheet.ps1` | Regenerates `output.txt`, the wiring sheet ([section 4](#4-key-vault-secrets)). Step 08 runs it for you. |
| `99-teardown.ps1` | Deletes the resource group and purges what it can. Asks first unless `-Force` ([section 11](#11-update-rollback-teardown)). |
| `Test-Connectivity.ps1` | Every network hop the deployment depends on, plus copy-paste tests for the container-to-container hops that can only be reached from inside ([step 08](#step-08--08-bootstrapps1)). Step 08 runs it as a pre-flight. |
| `Test-EmbeddingAlignment.ps1` | Checks that the index, the query path and the ingestion path all use the same embedding model ([step 09](#step-09--embedding-alignment)). Step 09 runs it as a gate. |
| `Scale-SearchReplicas.ps1` | Temporary AI Search capacity for a backfill ([section 10](#10-operations)). |
| `Set-IngestionControls.ps1` | Pause, resume or throttle ingestion without a redeploy ([section 10](#10-operations)). |
| `common.ps1` | Shared helpers. Not run directly. |

### Retries, and what the transcript tells you

Two different retry layers run underneath every step, and they mean different things:

| In the transcript | Layer | Budget | Retries |
|---|---|---|---|
| `[retry] transient az failure (attempt 1/3)` | every `az` call | **3 attempts**, 3 s then 9 s | throttling (429), control-plane 5xx, connection resets and timeouts |
| `[wait] <activity> not ready yet (attempt 1/10)` | specific calls that wait on eventual consistency | 4-12 attempts, 10-30 s apart | RBAC replication, a resource still in a transitional state |

**Deterministic failures are never retried** — a bad resource name, denied quota, an unsupported region, a missing
role or an already-exists conflict fails on the first attempt, because retrying those only triples the wait before
the real error is readable. The two lists are kept disjoint on purpose, so the attempts never multiply.

The longer `[wait]` budgets are not a mistake: Entra role assignments routinely take minutes to replicate to the
data plane, and three attempts is not enough for them. Everything else gets three.

---

## 1. Prerequisites

### Tools
| Tool | Version | Notes |
|---|---|---|
| PowerShell | >= 7.3 | `pwsh`. Windows PowerShell 5.1 is not supported. |
| Azure CLI | >= 2.60 | Extensions `containerapp` and `application-insights` are installed by `00-prereqs.ps1`. |
| uv | latest | Only for `09-smoke.ps1`, `10-loadtest.ps1` and local `rag-os` CLI runs. |
| git | any | Supplies the image tag (short SHA). |

No local Docker: images are built in the cloud with `az acr build`.

### Permissions
| Scope | Role | Why |
|---|---|---|
| Subscription or resource group | **Owner**, or **Contributor** + **User Access Administrator** | The scripts create resources *and* assign roles (managed identity -> Key Vault, Storage, Service Bus, Search, Foundry, ACR). |
| Entra ID | Read the signed-in user (`az ad signed-in-user show`) | The deployer becomes a PostgreSQL Entra admin and gets data-plane roles. Guest accounts usually have this; if not, ask for the *Directory Readers* role. |
| Subscription | Cost Management Contributor (optional) | Only for the budget alert. Without it the budget is skipped with a warning. |

Service principals work too: `Get-DeployerPrincipal` detects them and uses `ServicePrincipal` as the principal type.

### Region checklist
All resources go in one region (`Location`). It must offer:

- [ ] **Container Apps workload profiles** `D4` and `D8`
- [ ] **Serverless GPU** `Consumption-GPU-NC8as-T4` — *optional*; without it the ingestion embedder falls back to CPU
- [ ] **Azure AI Search** with the **semantic ranker** (`standard` plan)
- [ ] **Foundry** text-generation models (`AnswerModelName`, `UtilityModelName`) with enough TPM quota
- [ ] **Claude** in the model catalog — only if `AnswerModelProvider`/`UtilityModelProvider` = `claude`
- [ ] PostgreSQL Flexible Server, Service Bus Standard (available nearly everywhere)

`00-prereqs.ps1` checks all of these and prints PASS/WARN/FAIL. Default: `westus`. Other good starting points:
`westus3`, `eastus2`, `swedencentral`. If the serverless-GPU check WARNs, the deployment still succeeds and
`rag-embed-ingest` runs on the CPU ingest profile — switch region only if you want the GPU.
Serverless GPU also needs quota on the subscription; request it in the portal (Quotas -> Container Apps) if step 07 warns.

---

## 2. Fill in `infra/env/dev.psd1`

```powershell
Copy-Item infra/env/dev.sample.psd1 infra/env/dev.psd1    # dev.psd1 is git-ignored
code infra/env/dev.psd1
```

### Create the Entra app registration first

**Do this before step 00.** An app registration is a **directory object, not a subscription resource** — it has
no dependency on your resource group, your container apps, or anything the provisioning steps create. So there
is no chicken-and-egg: create it now and three of the four `Entra*` values fall out of the app id.

```powershell
$app = az ad app create --display-name 'RAG-OS' | ConvertFrom-Json
az ad app update --id $app.appId --identifier-uris "api://$($app.appId)"
# then in the portal: Expose an API -> add the scope `access_as_user` -> pre-authorise the same client id
```

| psd1 setting | Value |
|---|---|
| `EntraClientId` | `$app.appId` |
| `EntraAudience` | `api://<app-id>` |
| `EntraApiScope` | `api://<app-id>/access_as_user` |
| `EntraTenantId` | your directory (tenant) id |

The **only** thing that needs a deployed URL is the SPA *redirect URI*
(`https://<chat-ui-fqdn>/auth/callback`), and that lives on the Entra app rather than in the psd1 — add it once
step 08 prints the chat URL, with no redeploy. Full detail, including the claims and the admin role, is in
[section 9](#9-signing-people-in-with-microsoft-entra-id).

> This registration is how **people sign in**. It is a different thing from the user-assigned **managed
> identity**, which is how **RAG-OS reaches Azure resources** — that one is created for you by step 02, is named
> `id-<prefix>-<env>`, and needs no configuration. Both are described at the top of `dev.sample.psd1`.

### Where settings come from

**`-Env dev` is how you pass your subscription id.** It selects `infra/env/dev.psd1`, and *every* subscription,
tenant, region, sizing and sign-in value lives in that file. No script takes any of them on the command line.

```text
./infra/scripts/provision-all.ps1 -Env dev
                                       └──► infra/env/dev.psd1
```

| Passed on the command line | Read from `infra/env/<env>.psd1` |
|---|---|
| `-Env` (which file to read), `-From` / `-To` (which steps), and per-script switches such as `-Tag`, `-Only`, `-RotateSecrets`, `-SkipUpload` | `SubscriptionId`, `TenantId`, `Location`, `Prefix`, all four `Entra*`, every model, SKU, capacity, replica count and application setting |

`-Env prod` reads `infra/env/prod.psd1`. That is the whole multi-environment mechanism.

**The sample is also the defaults file.** A key you leave out of your `dev.psd1` falls back to
`dev.sample.psd1`; a key that is not in the sample is reported as a typo. So your file should be *short* — ten
lines, not a 144-line copy. `dev.psd1` is git-ignored; the sample is committed.

### What you must set, and what you can ignore

| | Settings | Why |
|---|---|---|
| **Deployment fails without it** | `SubscriptionId`, `TenantId` — plus `Env` when the file is not `dev.psd1` | The sample ships all-zero placeholder GUIDs, which are explicitly rejected. `Env` must equal the file name. |
| **Validated, sample default already passes** | `Location`, `Prefix`, `EmbedderModelRevision` | Change `Location`/`Prefix` if you want. Never loosen the pinned model SHA — it is what ties the index to the embedding model. |
| **Not validated, but nobody can sign in without it** | `EntraTenantId`, `EntraAudience`, `EntraClientId`, `EntraApiScope` | Nothing checks these. A deployment with them empty succeeds and then no one can log in. Create the app registration first (above) — it needs no Azure resources. |
| **Everything else** | the other ~80 keys | Tune later; re-run step 07 to apply. |

### You do not name resources — they are derived

You set `Prefix` and `Env`; all 14 resource names follow from them plus a deterministic 5-character hash of
`"<subscription>/<prefix>-<env>"`. With `Prefix = 'ragos'`, `Env = 'dev'`:

| Resource | Pattern | Example |
|---|---|---|
| Resource group | `rg-<prefix>-<env>` | `rg-ragos-dev` |
| Log Analytics / App Insights | `log-…` / `appi-…` | `log-ragos-dev`, `appi-ragos-dev` |
| Managed identity | `id-<prefix>-<env>` | `id-ragos-dev` |
| Container Apps env | `cae-<prefix>-<env>` | `cae-ragos-dev` |
| Key Vault | `kv-<alnum≤15>-<hash>` | `kv-ragosdev-a1b2c` |
| Storage | `st<alnum≤17><hash>` | `stragosdeva1b2c` |
| Container registry | `acr<alnum><hash>` | `acrragosdeva1b2c` |
| PostgreSQL / Service Bus / Search / Foundry | `psql-`/`sb-`/`srch-`/`aif-` + `<prefix>-<env>-<hash>` | `srch-ragos-dev-a1b2c` |

The odd ones are odd for a reason: **Key Vault is capped at 24 characters**, and **Storage and ACR allow no
hyphens and only lowercase alphanumerics** — hence the squashed forms. The last group carries a hash because
those names are globally-unique DNS (`*.search.windows.net`, `*.postgres.database.azure.com`, …).

Three escape hatches, least to most blunt:

| Setting | Effect |
|---|---|
| `NameSuffix` | Replaces the 5-character hash on all seven globally-unique names. Use when one collides. |
| `ResourceGroup`, `FoundryProject` | Override those two names individually. |
| `NameOverrides` | Arbitrary per-resource names, applied last, wins over everything. For matching a corporate standard. |

[Figure 2 in the README](README.md#3-deployment-topology) shows every resource with its name pattern.

### The shortest file that deploys

```powershell
@{
    SubscriptionId = '<your-subscription-guid>'
    TenantId       = '<your-tenant-guid>'
    Env            = 'dev'                        # must equal the file name

    # Needed before anyone can sign in. All four come from one app registration (section 9),
    # and none of them needs a deployed URL - so fill them in now and save a redeploy.
    EntraTenantId  = '<your-tenant-guid>'
    EntraClientId  = '<app-id>'
    EntraAudience  = 'api://<app-id>'
    EntraApiScope  = 'api://<app-id>/access_as_user'
}
```

`Location = 'westus'`, `Prefix = 'ragos'` and ~80 other keys come from the sample.

### When it will not load

Every message below comes from `Import-RagOsConfig` in `infra/scripts/common.ps1`, before any Azure call:

| Message | Cause |
|---|---|
| `Settings file not found: …` | `dev.psd1` does not exist yet — copy the sample. |
| `Set SubscriptionId in …` / `Set TenantId in …` | Still the all-zero placeholder, or not a GUID. |
| `Prefix must be 2-12 lowercase letters/digits starting with a letter` | Checked case-sensitively, so `RagOS` fails. |
| `Env must be 1-8 lowercase letters/digits` | It must also *start* with a letter, which the message does not say. |
| `infra/env/<env>.psd1 declares Env = 'x'. They must match.` | `Env` must equal the file name. |
| `EmbedderModelRevision must be a full 40-character commit SHA` | The embedding model is pinned on purpose. |
| `NameOverrides.<key> is not a known resource name (…)` | The message lists all 14 valid keys. |
| `WARNING: Unknown setting 'x' … - typo?` | **A warning, not an error — and the value is still assigned.** The correctly-spelled key silently keeps its default, so treat this as an error. |

### Every setting

| Setting | Default | Meaning |
|---|---|---|
| **Subscription / placement** | | |
| `SubscriptionId` | – | Target subscription (required). |
| `TenantId` | – | Entra tenant of that subscription (required). |
| `Location` | `westus` | Region for every resource (see the checklist above). |
| `Prefix` | `ragos` | 2-12 lowercase letters/digits; first part of every resource name. |
| `Env` | `dev` | Environment name; must equal the psd1 file name. |
| `ResourceGroup` | `rg-<prefix>-<env>` | Resource group name override. |
| `NameSuffix` | hash | 5 characters appended to globally unique names (Key Vault, Storage, ACR, Search, Service Bus, PostgreSQL, Foundry). |
| `NameOverrides` | `@{}` | Explicit names, e.g. `@{ KeyVault = 'kv-contoso' }`. |
| `Tags` | owner, costCenter | Added to every resource, plus `app=rag-os`, `env=<Env>`, `managed-by=rag-os-infra-scripts`. |
| **Cost / network** | | |
| `BudgetAmount` | `1500` | Monthly resource-group budget; alerts at 80% actual and 100% forecast. |
| `BudgetContactEmails` | `@()` | Extra alert recipients (subscription Owners always get them). |
| `AllowClientIp` | `$true` | Add your public IP to the PostgreSQL firewall. |
| `ClientIpAddress` | auto | Explicit IP for that rule (auto-detected through api.ipify.org when empty). |
| **Monitoring / Key Vault** | | |
| `LogRetentionDays` | `30` | Log Analytics retention. |
| `KeyVaultSku` | `standard` | `standard` or `premium` (HSM keys). |
| `KeyVaultRetentionDays` | `90` | Soft-delete retention. Purge protection is always on. |
| **Storage** | | |
| `StorageSku` | `Standard_LRS` | Replication of the `raw-docs`/`config`/`exports` account. |
| **PostgreSQL** | | |
| `PostgresVersion` | `16` | Major version. |
| `PostgresTier` / `PostgresSku` | `Burstable` / `Standard_B2s` | MVP sizing; use `GeneralPurpose`/`Standard_D4ds_v5` for millions of documents. |
| `PostgresStorageGb` | `32` | Data disk size. |
| `PostgresDatabase` | `ragos` | Database name (part of `STATE_DB_URL`). |
| **Service Bus** | | |
| `ServiceBusSku` | `Standard` | Standard is required (duplicate detection). |
| `QueueMaxDeliveryCount` | `5` | Deliveries before dead-lettering. |
| `QueueLockDuration` | `PT5M` | Peek-lock duration (max PT5M). |
| `QueueDuplicateWindow` | `PT1H` | Duplicate-detection window — **only applied when the queue is created**. |
| **AI Search** | | |
| `SearchSku` | `standard` | `standard` = S1. |
| `SearchReplicas` | `2` | >= 2 keeps queries fast while indexing. |
| `SearchPartitions` | `1` | 1 S1 partition = 160 GB total storage, but only **35 GB of vector quota** — the vector figure is the one that binds, and indexing hard-fails past it. |
| `SearchSemantic` | `standard` | Semantic ranker plan (`disabled`/`free`/`standard`). |
| **Foundry** | | |
| `FoundryProject` | `proj-<prefix>-<env>` | Project name. |
| `AnswerModelProvider` | `aoai` | Who writes the answer: `aoai`, `claude` or `fake`. -> `LLM_ANSWER`. |
| `AnswerModelName` / `AnswerModelVersion` | `gpt-5-mini` / `2025-08-07` | The answer model, and its deployment name. **Verify availability in your region** (step 00 does). -> `AOAI_CHAT_DEPLOYMENT`. |
| `AnswerModelSku` / `AnswerModelCapacity` | `GlobalStandard` / `50` | Deployment type and TPM (thousands). |
| `UtilityModelProvider` | `aoai` | Who condenses follow-ups and classifies facets. -> `LLM_UTILITY`. |
| `UtilityModelName` / `UtilityModelVersion` | `gpt-4o-mini` / `2024-07-18` | The utility model. Set it equal to `AnswerModelName` to share one deployment. -> `AOAI_UTILITY_DEPLOYMENT`. |
| `UtilityModelSku` / `UtilityModelCapacity` | `GlobalStandard` / `20` | Deployment type and TPM. |
| `DeployAoaiEmbedding` | `$false` | Also deploy `text-embedding-3-small` (only for an `azure_openai` embedding profile). |
| `EmbeddingModel*` | text-embedding-3-small… | Name/version/sku/capacity/deployment name of that optional deployment. |
| `ClaudeAnswerModelName` | `claude-sonnet-5` | Used only when `AnswerModelProvider` = `claude`. Deployment name **and** `CLAUDE_MODEL`. |
| `ClaudeUtilityModelName` | `claude-haiku-4-5` | Used only when `UtilityModelProvider` = `claude`. -> `CLAUDE_UTILITY_MODEL`. |
| `ClaudeModelVersion` | auto | Empty = discovered from the account's model list. |
| `ClaudeSku` / `ClaudeCapacity` | `GlobalStandard` / `1` | Claude deployment sizing (both roles). |
| `ClaudeEffort` | `''` | Optional `CLAUDE_EFFORT` (`low`…`max`), applied to **both** Claude roles. |
| **Registry / images** | | |
| `AcrSku` | `Basic` | ACR tier (Basic = 10 GB; the two embedder images are ~2-8 GB each). |
| `ImageTag` | git SHA | Fixed tag instead of the git short SHA. |
| **Embedder** | | |
| `TeiVersion` | `1.9` | TEI release used for both base images. |
| `TeiCpuImage` / `TeiTuringImage` | derived | Override the TEI base images. |
| `EmbedderModelId` | `Qwen/Qwen3-Embedding-0.6B` | Model baked into the image. |
| `EmbedderModelRevision` | `97b0c614…` | **Pinned 40-char commit SHA** (also TEI `/info.model_sha`). |
| `EmbeddingDimensions` | `1024` | **Informational only — nothing reads it back.** Recorded in `<env>.images.json`; `config/embedding/profiles.yaml` is what actually sets the vector size. |
| `EmbeddingProfile` | `qwen3-0.6b-1024` | -> `EMBEDDING_PROFILE`; a profile name in `config/embedding/profiles.yaml`. |
| `EmbedderMaxBatchTokensCpu/Gpu` | `16384` / `32768` | TEI `MAX_BATCH_TOKENS` per pool. |
| **Workload profiles** | | |
| `QueryProfileType` / `QueryMinNodes` / `QueryMaxNodes` | `D4` / `2` / `6` | Query-path nodes (chat-ui, api, embed-query). |
| `IngestProfileType` / `IngestMinNodes` / `IngestMaxNodes` | `D8` / `1` / `10` | Worker and job nodes. |
| `EnableGpu` | `$true` | `$false` runs the ingestion embedder on CPU. |
| `GpuProfileType` | `Consumption-GPU-NC8as-T4` | Serverless GPU profile type. |
| **Replicas** | | |
| `ApiMinReplicas` / `ApiMaxReplicas` / `ApiConcurrency` | `2` / `10` / `50` | rag-api scaling (HTTP concurrency rule). |
| `ChatUiMinReplicas` / `ChatUiMaxReplicas` | `1` / `3` | rag-chat-ui. |
| `EmbedQueryMinReplicas` / `Max` / `Concurrency` | `1` / `4` / `20` | Query embedding pool (never 0). |
| `GpuMinReplicas` / `GpuMaxReplicas` | `0` / `4` | Ingestion embedding pool on GPU. |
| `EmbedIngestCpuMaxReplicas` | `6` | Same pool when it falls back to CPU. |
| `EmbedIngestConcurrency` | `16` | HTTP concurrency per embedder replica. |
| `WorkerMaxReplicas` | `10` | KEDA maximum. **`WorkerMaxReplicas` x `IngestMaxConcurrency` is the global write cap.** |
| `WorkerCpu` / `WorkerMemory` | `2.0` / `4Gi` | Worker replica size. |
| `WorkerBulkMessageCount` / `WorkerPriorityMessageCount` | `20` / `5` | Messages per replica for each KEDA rule. |
| `SchedulerCron` | `*/5 * * * *` | Discovery/reconciliation schedule (UTC). |
| **Application settings** | | |
| `AppEnv` | `prod` | `APP_ENV`. |
| `LogLevel` | `INFO` | `LOG_LEVEL`. |
| `IndexDomain` | `enterprise` | `INDEX_DOMAIN` (index name `kb-<domain>-<fingerprint>`). |
| `ActiveIndex` | `''` | `ACTIVE_INDEX` override used when switching embedding models. |
| `EmbedOrigins` | `https://intranet.contoso.com` | `EMBED_ORIGINS`: comma-separated origins allowed to embed the widget. The sample ships a placeholder, so **set it or clear it** — leaving it gives a stranger's domain `frame-ancestors` permission. |
| `DevAuthEnabled` | `$false` | `DEV_AUTH_ENABLED`; `$true` only for demos. |
| `IngestMaxConcurrency` | `4` | `INGEST_MAX_CONCURRENCY` per worker replica. |
| `EntraTenantId` / `EntraAudience` / `EntraClientId` / `EntraApiScope` | `''` | Microsoft Entra ID sign-in — the only production issuer. Set all four ([section 9](#9-signing-people-in-with-microsoft-entra-id)). |
| `ExtraAppSettings` | `@{}` | Any other variable from [section 5](#5-application-settings), e.g. `@{ RETRIEVAL_TOP_K = '8' }`. Secret-looking names are refused. |

---

## 3. Run the steps

Each step below shows the command, what it creates, how to verify it and the errors people actually hit.

### Step 00 — `00-prereqs.ps1`
```powershell
./infra/scripts/00-prereqs.ps1 -Env dev            # add -InstallRdbmsConnect for `az postgres flexible-server connect`
```
Installs the az extensions, logs in, selects the subscription, registers providers (`Microsoft.App`,
`OperationalInsights`, `Insights`, `CognitiveServices`, `Search`, `ServiceBus`, `DBforPostgreSQL`, `ContainerRegistry`,
`KeyVault`, `Storage`, `ManagedIdentity`, `Consumption`) and prints the region/quota checklist.

**Expected:** a table with `PASS` everywhere, at most `WARN` for the GPU profile or Claude. FAIL stops the run.

| Problem | Fix |
|---|---|
| `Deployer identity readable (Entra) FAIL` | The account cannot read itself in Entra. Ask for *Directory Readers*, or run as a service principal. |
| `Container Apps profile Consumption-GPU-NC8as-T4 WARN` | Region or quota. Continue: the ingestion embedder uses CPU. Request GPU quota, then re-run step 07. |
| `Foundry answer model FAIL: version ... not offered` | Copy an available version from the message into `AnswerModelVersion` (or `UtilityModelVersion`). |
| Provider stuck in `Registering` | Wait and re-run; registration can take several minutes. |

### Step 01 — `01-foundation.ps1`
```powershell
./infra/scripts/01-foundation.ps1 -Env dev
```
Creates the resource group, Log Analytics workspace, workspace-based Application Insights and the budget alert.

```powershell
az group show -n rg-ragos-dev --query properties.provisioningState -o tsv      # Succeeded
az monitor app-insights component show --app appi-ragos-dev -g rg-ragos-dev --query connectionString -o tsv
```
| Problem | Fix |
|---|---|
| `Budget not created` warning | Needs Cost Management Contributor, and some offers (CSP/sponsored) do not support budgets. Non-fatal. |
| Workspace name already in use | A soft-deleted workspace of the same name is recovered automatically; otherwise set `NameOverrides.LogAnalytics`. |

### Step 02 — `02-identity-keyvault.ps1`
```powershell
./infra/scripts/02-identity-keyvault.ps1 -Env dev
```
Creates the user-assigned identity `id-<prefix>-<env>` used by **every** app and job, the RBAC Key Vault (purge
protection on), the two role assignments and the **two** secrets ([section 4](#4-key-vault-secrets)). Nothing else needs a secret:
Foundry, Search, Storage, Service Bus and PostgreSQL are all reached with the identity's own tokens.

```powershell
az keyvault secret list --vault-name kv-ragosdev-xxxxx --query "[].name" -o tsv
# appinsights-connection-string / dev-jwt-signing-key
az identity show -g rg-ragos-dev -n id-ragos-dev --query "{clientId:clientId, principalId:principalId}"
```
| Problem | Fix |
|---|---|
| `Forbidden` when listing/setting secrets | The *Key Vault Secrets Officer* assignment is still propagating; the script retries for ~2 minutes. Re-run. |
| Vault name already exists (another tenant) | Set `NameSuffix` or `NameOverrides.KeyVault`. |
| Vault was purged-protected from an old teardown | It is recovered automatically — that is by design. |

### Step 03 — `03-data.ps1`
```powershell
./infra/scripts/03-data.ps1 -Env dev
```
Storage account (shared keys **disabled**, TLS 1.2, no public blobs) with `raw-docs`, `config`, `exports`;
PostgreSQL Flexible Server (Entra-only, admins = managed identity + you, database `ragos`, firewall for Azure
services and optionally your IP); Service Bus Standard (local auth disabled) with both queues.

```powershell
az storage container list --account-name stragosdevxxxxx --auth-mode login --query "[].name" -o tsv
az postgres flexible-server show -g rg-ragos-dev -n psql-ragos-dev-xxxxx --query "{state:state, auth:authConfig}"
az postgres flexible-server microsoft-entra-admin list -g rg-ragos-dev -s psql-ragos-dev-xxxxx -o table
az servicebus queue list -g rg-ragos-dev --namespace-name sb-ragos-dev-xxxxx --query "[].{name:name, dup:requiresDuplicateDetection, maxDelivery:maxDeliveryCount}" -o table
```
Expected: three containers; `passwordAuth: Disabled`, `activeDirectoryAuth: Enabled`; two admins; `dup: True` on both queues.

| Problem | Fix |
|---|---|
| `AuthorizationPermissionMismatch` creating containers | *Storage Blob Data Contributor* is still propagating; the script retries for ~3 minutes. |
| Queue exists **without** duplicate detection (warning) | It can only be set at creation. Drain it, `az servicebus queue delete …`, re-run the step. |
| PostgreSQL create fails on the SKU | The tier/SKU is not available in the region: `az postgres flexible-server list-skus -l <region> -o table`. |
| Cannot connect with psql from your machine | Set `AllowClientIp = $true` (or `ClientIpAddress`) and re-run. Log in with an Entra token, not a password. |

### Step 04 — `04-search.ps1`
```powershell
./infra/scripts/04-search.ps1 -Env dev              # -ApplyChanges to push psd1 replicas/partitions onto an existing service
```
```powershell
az search service show -g rg-ragos-dev -n srch-ragos-dev-xxxxx --query "{status:status, sku:sku.name, replicas:replicaCount, partitions:partitionCount, localAuth:disableLocalAuth, semantic:semanticSearch}"
# status running, sku standard, replicas 2, localAuth true, semantic standard
```
| Problem | Fix |
|---|---|
| Quota exceeded for S1 | Request more search units, or use another region. |
| `semanticSearch` cannot be set | The region/SKU has no semantic ranker: set `SearchSemantic = 'disabled'` (quality drops) or move region. |
| Creation takes >15 minutes | Normal for S1. The command waits. |

### Step 05 — `05-foundry.ps1`
```powershell
./infra/scripts/05-foundry.ps1 -Env dev
```
AIServices account (custom domain = account name, system identity, project management), the project, the GPT
deployment, optionally embeddings and Claude, App Insights tracing connection, and the two Foundry roles.

```powershell
az cognitiveservices account deployment list -g rg-ragos-dev -n aif-ragos-dev-xxxxx --query "[].{name:name, model:properties.model.name, version:properties.model.version, state:properties.provisioningState}" -o table
az cognitiveservices account project list -g rg-ragos-dev -n aif-ragos-dev-xxxxx --query "[].name" -o tsv
```
| Problem | Fix |
|---|---|
| `Model ... version is not available` | The script lists the available versions — put one in the psd1. |
| `InsufficientQuota` on the deployment | Lower `AnswerModelCapacity`/`UtilityModelCapacity` or free TPM: `az cognitiveservices usage list -l <region> -o table`. |
| Claude deployment fails | Expected until Anthropic models are enabled for the subscription. Follow the printed portal steps ([section 8](#8-claude-on-foundry)). |
| Account name taken | Custom domains are global: set `NameSuffix`. |

### Step 06 — `06-registry-build.ps1`
```powershell
./infra/scripts/06-registry-build.ps1 -Env dev
./infra/scripts/06-registry-build.ps1 -Env dev -Images embedder-cpu,embedder-turing   # subset
```
Creates the registry (admin user disabled), grants `AcrPull`, then builds `rag-api`, `rag-chat-ui`,
`rag-embedder-cpu` and `rag-embedder-turing` **in the cloud** and records their digests in `infra/env/dev.images.json`.
The embedder builds download the model at build time, so they take 5-15 minutes each.

```powershell
az acr repository list -n acrragosdevxxxxx -o tsv
az acr repository show -n acrragosdevxxxxx --image rag-api:<tag> --query "{digest:digest, size:imageSize}"
Get-Content infra/env/dev.images.json
```
| Problem | Fix |
|---|---|
| `Dockerfile not found` | `rag-api` needs a `Dockerfile` in the repo root, `rag-chat-ui` one in `chat-ui/`. |
| Upload is huge / slow | Add `.dockerignore` (`.venv`, `node_modules`, `.git`, `.data`) — the script warns when it is missing. |
| `MODEL_REVISION must be a full 40-character commit SHA` | Put the commit SHA, not a branch, in `EmbedderModelRevision`. |
| Build timeout | Raise ACR task timeout by re-running; embedder builds are given 2 hours. |
| ACR storage full (Basic = 10 GB) | Set `AcrSku = 'Standard'`, or delete old tags: `az acr repository delete -n <acr> --image rag-api:<oldtag>`. |

### Step 07 — `07-container-apps.ps1`
```powershell
./infra/scripts/07-container-apps.ps1 -Env dev
```
Creates the environment with workload profiles `query` (D4), `ingest` (D8) and `gpu-t4`, then renders and applies the
seven workloads. If the GPU profile cannot be added, `rag-embed-ingest` runs the CPU image on `ingest` and a warning
says so. The rendered YAML is written to a temp folder (path is printed) for troubleshooting.

```powershell
az containerapp list -g rg-ragos-dev --query "[].{name:name, state:properties.runningStatus, profile:properties.workloadProfileName, fqdn:properties.configuration.ingress.fqdn}" -o table
az containerapp job list -g rg-ragos-dev --query "[].{name:name, trigger:properties.configuration.triggerType}" -o table
az containerapp env workload-profile list -g rg-ragos-dev -n cae-ragos-dev -o table
az containerapp show -g rg-ragos-dev -n rag-api --query "properties.configuration.secrets"   # keyVaultUrl only, no values
```
Expected: `rag-chat-ui` has an external FQDN; `rag-api`, `rag-embed-query`, `rag-embed-ingest` have internal ones;
`rag-ingest-worker` has none; both jobs exist.

| Problem | Fix |
|---|---|
| Revision fails: cannot pull image | `AcrPull` propagation — the script retries; otherwise check `az role assignment list --assignee <identity principalId> --scope <acr id>`. |
| Revision fails: cannot resolve a Key Vault secret | Identity lacks *Key Vault Secrets User* or the secret is missing: re-run step 02. |
| `rag-api` never becomes ready | `/api/readyz` fails on purpose when the TEI pools do not match the embedding profile. `az containerapp logs show -g rg-ragos-dev -n rag-api --tail 100`. |
| GPU profile add fails | Quota/region. Fallback is automatic; request quota and re-run this step to switch back. |
| Worker never scales up | KEDA needs *Azure Service Bus Data Owner* on the namespace and `identity` on the scale rule — both set by steps 03 and 07. Check `az containerapp show -n rag-ingest-worker -g <rg> --query properties.template.scale`. |

### Step 08 — `08-bootstrap.ps1`
```powershell
./infra/scripts/08-bootstrap.ps1 -Env dev
```
Runs a connectivity pre-flight, uploads `config/**` to the `config` container, runs `rag-bootstrap` (migrations,
index, policy, facets) and waits, starts `rag-scheduler` once, waits for `/api/readyz`, writes `output.txt`
([section 4](#4-key-vault-secrets)) and prints the URLs.

**The pre-flight runs first, before anything is spent.** The bootstrap job reaches PostgreSQL, AI Search and Blob
storage *directly from its own container* - not through `rag-api` - so if one of those is unreachable the job
still starts, still runs and still fails, up to 30 minutes later, with the cause buried in container logs.
`Test-Connectivity.ps1 -Preflight` checks those hops in seconds and stops the step if one is down. A failure of
the chat-UI -> API hop is only a warning here, because the job does not use it. Skip the check with
`-SkipConnectivityCheck` if you know a hop is bad and want the job attempted anyway.

**Then it waits for what `/api/readyz` depends on, rather than polling it blind.** 08 runs immediately after 07,
and a fresh `rag-embed-query` is still pulling a ~3 GB image and loading a model - minutes during which
`/api/readyz` *cannot* succeed and is not meant to. The step waits for `rag-api` and (on a self-hosted profile)
`rag-embed-query` to report a ready replica first, printing a line whenever the reason changes:

```text
    [wait] rag-embed-query replicas : 0/1 replicas ready
    [ok] rag-embed-query replicas ready after 6m15s
==> Checking https://<chat-ui>/api/healthz (the chat-ui -> rag-api hop)
    [ok] chat-ui reaches rag-api (HTTP 200)
    [wait] /api/readyz : index has no recorded embedding profile (run rag-os bootstrap)
    [ok] /api/readyz ready after 1m30s
```

`/api/healthz` is probed once before readiness. It goes through the **same** nginx `/api/` proxy block but
returns instantly, which separates "the hop is broken" from "the hop is fine, a dependency is not":

| `/api/healthz` | `/api/readyz` | Meaning |
|---|---|---|
| 200 | 200 | Everything is up. |
| 200 | 503 | The hop is fine; a dependency is not - the body names which. |
| fails | - | The chat-UI -> `rag-api` hop really is broken. Run `Test-Connectivity.ps1`. |

Each readiness attempt allows **30 s**, deliberately more than the 25 s `/api/readyz` can spend on its own checks
(5 s database + 20 s embedding-profile guard). With a smaller client timeout every slow-but-working attempt was
cut off, which nginx logged as `status:499` with `upstream_status:"-"` - a log line that reads exactly like a
broken network when nothing was broken at all.

This step **fails** if `/api/readyz` does not return 200 within 10 minutes. The resources exist at that point, but
nothing can serve a query, and step 09 would fail with a less obvious error — so the run stops here rather than
reporting green. `output.txt` is written first, because the secret-to-variable mapping is exactly what you want
while debugging a startup failure.

```powershell
az storage blob list --account-name stragosdevxxxxx -c config --auth-mode login --query "[].name" -o tsv
az containerapp job execution list -g rg-ragos-dev -n rag-bootstrap --query "[0].{name:name, status:properties.status}"
curl https://<chat-ui-fqdn>/api/healthz
```
| Problem | Fix |
|---|---|
| Bootstrap job `Failed` | Print the logs with the command the script prints, or the KQL in [section 10](#10-operations). Usual causes: the managed identity is not a PostgreSQL Entra admin (step 03), or Search roles are still propagating. |
| `/api/readyz` returns 503 | **Read the body — it names the cause.** See [Diagnosing a 503 from `/api/readyz`](#diagnosing-a-503-from-apireadyz) below. |
| Upload skipped | There is no `config/` folder in the repo yet. |
| Pre-flight fails on PostgreSQL | From your machine that hop needs the `AllowClientIp` firewall rule - re-run step 03, which adds it for the address in `dev.psd1`. |
| Pre-flight fails on Search or Blob | A network path or role problem the job would hit too. Fix it before re-running; `-SkipConnectivityCheck` only hides it. |
| Something is only reachable *inside* a container | `./infra/scripts/Test-Connectivity.ps1 -Env dev -SnippetsOnly` prints tests to paste into each app's **Monitoring -> Console**, using only the tools that image actually contains. |

### Diagnosing a 503 from `/api/readyz`

**A 503 means the application answered.** It is not a connectivity fault, and the container app is healthy —
`/api/healthz` returning 200 over the same host, port and ingress proves the path works. Two facts make this
conclusive:

* **nginx never produces a 503 here.** `proxy_intercept_errors` is off, so an upstream 503 is passed through
  byte-for-byte. nginx's own "upstream unreachable" path returns **502** with a fixed
  `{"title":"API unavailable"}` body. So: **502 = nginx synthesised it and never reached the app; 503 with
  `{"status":"not_ready"}` = the app answered.**
* **No probe points at `/api/readyz`** — Startup, Liveness and Readiness are all on `/api/healthz`, deliberately
  ([step 07](#step-07--07-container-appsps1)). That is why the replica stays in the ingress and Azure reports
  everything green while queries refuse, and it is why nothing in `az containerapp` will show you this.

`/api/readyz` is therefore the *only* signal, and its body is the whole diagnosis.

```powershell
# From anywhere - the chat UI proxies /api/ without authentication
curl -sS https://<chat-ui-fqdn>/api/readyz | ConvertFrom-Json | ConvertTo-Json -Depth 6

# Parsed into one line per check, with the index it judged
./infra/scripts/Test-Connectivity.ps1 -Env dev
```

From inside the container (**Monitoring → Console**), which also rules out nginx and the ingress — `rag-api`
has no curl, and `urlopen` raises on 503, so `http.client` is used:

```sh
python3 -c "import http.client,json;c=http.client.HTTPConnection('localhost',8000,timeout=45);c.request('GET','/api/readyz');r=c.getresponse();print('status',r.status);print(json.dumps(json.loads(r.read()),indent=2))"
rag-os doctor    # read-only: index, stored vs expected fingerprint, DB and both embedder pools, with real errors
```

`./infra/scripts/Test-Connectivity.ps1 -Env dev -SnippetsOnly` prints these ready to paste.

#### What the body can say

| `checks` value | Meaning | What to do |
|---|---|---|
| `state_db: "error: OperationalError"` | PostgreSQL unreachable, authentication refused, or firewalled | The managed identity must be a PostgreSQL Entra admin and your address needs the `AllowClientIp` rule — both from [step 03](#step-03--03-dataps1). `rag-os doctor` prints the real error. |
| `state_db: "error: TimeoutError"` | Slower than the 12s budget | Usually a network path problem rather than the database itself. |
| `query: embedder unavailable (<ExcType>)` | The TEI query pool is not answering `/info` | Normal for the first minutes after [step 07](#step-07--07-container-appsps1) — a ~3 GB image and a model load. Otherwise check it is not scaled to zero. |
| `query: model 'x' != profile 'y'` | The pool serves a different model than the profile expects | Align `EmbeddingProfile` in the psd1 with what the pool actually runs ([section 7](#7-embedding-profiles-which-to-run-and-how-to-change-it)). |
| `query: revision …` / `query: dimensions …` | Same, for the revision and the vector width | As above. Dimensions compare against the *native* width, before any MRL truncation. |
| `index '<name>' does not exist` | The index was never created | `rag-os bootstrap`, or re-run [step 08](#step-08--08-bootstrapps1). |
| `index '<name>' exists but has no recorded embedding profile` | Created, then something stopped before it was stamped | Look at why the bootstrap job did not finish, then re-run it. |
| `index profile <a> != configured profile <b>` | `ActiveIndex` is pinned to an index built with a different profile | Unpin it, or bootstrap into the profile's own index. A different model is a different vector space, so queries are refused rather than answered wrongly. |
| `index profile unreadable (<ExcType>)` | AI Search itself could not be asked | A role assignment that has not replicated, a network path, or throttling — not a bootstrap problem. |

The body names exception **types**, never their messages: this endpoint is public. The full errors go to the
container log and to `rag-os doctor`.

#### If re-running shows the same reason

The guard caches its verdict for 60 seconds, so a dependency that has just recovered can still read as broken.
Add `?fresh=1` to force a live re-check:

```sh
curl -sS "https://<chat-ui-fqdn>/api/readyz?fresh=1"
```

### Step 09 — embedding alignment

Before the functional smoke tests, step 09 runs:

```powershell
./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev
./infra/scripts/Test-EmbeddingAlignment.ps1 -Env dev -SkipLive   # config only, nothing deployed needed
```

**Why this has its own gate.** Searching an index with a vector from a *different* model does not fail. If the
dimensions happen to match, the query succeeds, returns the right *number* of hits, and they are arbitrary
passages presented with full confidence — because cosine similarity between two unrelated vector spaces is
noise. It reads as "the answers got worse", which is indistinguishable from a dozen other causes, and it passes
every other health check in this document.

It runs **before** `scripts/smoke.py` and throws rather than accumulating a failure, because a mismatch does not
make the smoke test fail cleanly: the upload check fails by *timeout*, four minutes later, reading like a broken
ingestion worker. Override with `-SkipAlignmentCheck` if you know and accept it.

| Check | What drifts, and why nothing else catches it |
|---|---|
| psd1 vs `config/embedding/profiles.yaml` | The psd1 calls its own `EmbeddingDimensions` "informational", so the two can disagree silently. |
| `EMBEDDING_PROFILE` on `rag-api` **and** `rag-ingest-worker` | Step 07 renders both from one block, but a `-Only` redeploy updates one app and not the other. |
| Both TEI pools' running images | The check in [step 07](#step-07--07-container-appsps1) compares the build *manifest*, not what is deployed, and is skipped entirely when `EnableGpu = $false`. |
| The running index and fingerprint | From `/api/readyz`, compared with the index [step 08](#step-08--08-bootstrapps1) actually created. A profile changed in between yields a **new, empty** index — which answers every question with "I could not find this". |

**The pools disagreeing with each other is the serious one.** Both can differ from the psd1 and still be
consistent with each other, which only means the psd1 is stale. Differing from *each other* means documents and
queries are embedded into different spaces.

#### What is enforced at runtime, and what is not

`/api/readyz` and `rag-os doctor` compare **both** pools against the profile. The ingestion pool is treated as
*advisory*: `GpuMinReplicas = 0`, so it scales to zero when idle and "not running" is its normal resting state —
that is reported as a note and does not fail readiness. A pool that **answers** and reports a different model
does fail, wherever it is.

The ingestion worker verifies its pool once, at startup ([worker logs](#10-operations): *"embedding profile
verified"*). It is **not** re-verified per batch, so a pool that drifts while the worker is running will write
vectors until someone looks. That is what the per-chunk stamp below is for.

#### Finding chunks written by the wrong model

Every chunk carries two fields. `embedding_fp` is the **configured** profile fingerprint. `embedded_by` is what
the pool **reported** about itself, as `model@revision`. Everything else in the system is derived from
configuration and so agrees by construction; `embedded_by` is the only value that can contradict it.

```
# Azure AI Search, on the index named by /api/readyz
$filter=embedded_by ne 'Qwen/Qwen3-Embedding-0.6B@97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3'
```

Anything returned was embedded by something else and must be re-ingested. An empty result means every chunk was
written by the model you expect.

> **Worth knowing per provider.** For `tei` this is real evidence: the value comes from the server's own
> `/info`. For `azure_openai` the SDK echoes the configured model back, so only *dimensions* are genuinely
> observed and `embedded_by` cannot contradict configuration. It records what was reported, which for AOAI is
> less than you might hope. A chunk written before this field existed has no value at all, which is recorded as
> absent rather than guessed.

### Steps 09-10 — verification
See [section 10](#10-operations).

---

## 4. Key Vault secrets

| Secret | Created by | Consumer | Environment variable | Contents |
|---|---|---|---|---|
| `dev-jwt-signing-key` | 02 (64 random bytes, base64) | `rag-api` | `DEV_JWT_KEY` | Signs local demo tokens. Only honoured when `DEV_AUTH_ENABLED=true`. |
| `appinsights-connection-string` | 02 (from App Insights) | `rag-api`, `rag-ingest-worker`, `rag-scheduler`, `rag-bootstrap` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | Telemetry endpoint + instrumentation key. |
| `foundry-api-key` | *not created* | – | `AOAI_API_KEY` / `CLAUDE_API_KEY` | **Optional fallback only.** Foundry is called with Entra tokens from the managed identity. Create it by hand only if you must use key auth. |

> **The one key left in the request path.** `APPLICATIONINSIGHTS_CONNECTION_STRING` carries an instrumentation
> key: Azure Monitor ingestion is key-authenticated, and the managed identity holds no *Monitoring Metrics
> Publisher* role. The value is held in Key Vault and delivered as a `keyvaultref`, so it never appears in a
> file or an image — but the authentication model is a key, not a token. Everything else (Storage, Service Bus,
> AI Search, PostgreSQL, Foundry, ACR) is reached with the identity's own Entra token, and their key-based
> paths are disabled at creation.

Secrets reach the containers as Container Apps Key Vault references
(`secrets: [{name, keyVaultUrl, identity}]` + `env: [{name, secretRef}]`), so no value is ever in a file, an image or
`az containerapp show`. Workers and jobs do **not** get the dev signing key — only `rag-api` validates tokens. Entra needs no secret at all: its tokens are verified against the tenant's public JWKS.

### Rotation
```powershell
# 1. new version (random secrets)
./infra/scripts/02-identity-keyvault.ps1 -Env dev -RotateSecrets
#    or one specific secret:
az keyvault secret set --vault-name kv-ragosdev-xxxxx --name dev-jwt-signing-key --file .\new-key.txt --encoding utf-8

# 2. pick it up (Container Apps caches references for up to ~30 minutes)
az containerapp revision list -g rg-ragos-dev -n rag-api --query "[?properties.active].name" -o tsv
az containerapp revision restart -g rg-ragos-dev -n rag-api --revision <revision>
#    or create a new revision:
az containerapp update -g rg-ragos-dev -n rag-api --revision-suffix r$(Get-Date -Format yyMMddHHmm)
```
Rotating `dev-jwt-signing-key` only invalidates local demo tokens; nobody signing in with Entra is affected.

### `output.txt` — the wiring sheet

Step 08 writes `output.txt` in the repository root, and you can regenerate it at any time:

```powershell
./infra/scripts/Write-OutputSheet.ps1 -Env dev                 # reads the live deployment
./infra/scripts/Write-OutputSheet.ps1 -Env dev -FromTemplates  # before the apps exist / offline
```

It answers the question that is genuinely tedious to reconstruct by hand — *which Key Vault secret feeds which
environment variable in which container app* — plus every non-secret value an operator needs: resource names and
endpoints, the chat UI FQDN, the managed identity client id, the Entra tenant/client/audience/scope, and the
`keyvaultref:` URI behind each secret.

**It contains no secret values, and cannot.** The live read projects only `name`, `keyVaultUrl`, `value` and
`secretRef` with a server-side `--query`, so a secret value is never requested; an env var whose *name* looks like
a credential but which carries an inline value is redacted and flagged as a deployment defect; and a final scan
refuses to write the file at all if anything credential-shaped (a connection string, a long base64 run, a JWT, a
SAS token) made it in. To read an actual value, go to the vault — the sheet prints the command.

`output.txt` is git-ignored. It is not a secret, but it maps one specific deployment, so it belongs with the
operator rather than in the repository.

---

## 5. Application settings

Set by `07-container-apps.ps1` from the psd1. To change one, edit the psd1 (or `ExtraAppSettings`) and re-run step 07.
Workloads: **A** = rag-api, **W** = rag-ingest-worker, **J** = rag-scheduler + rag-bootstrap, **U** = rag-chat-ui.

| Variable | Default (code) | Deployed value | Where | Description |
|---|---|---|---|---|
| **General** | | | | |
| `APP_ENV` | `dev` | `AppEnv` (`prod`) | A W J U | `dev`/`test`/`prod`. |
| `LOG_LEVEL` | `INFO` | `LogLevel` | A W J | Python log level. |
| `SERVICE_NAME` | `rag-api` | per workload | A W J | Service name in logs/traces. |
| `KEY_VAULT_URL` | – | vault URI | A W J | Resolves `kv://` references at startup (not needed for secretRefs). |
| `AZURE_CLIENT_ID` | – | identity client id | A W J | **Required** so `DefaultAzureCredential` picks the user-assigned identity. |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | – | per workload, `deployment.environment=<env>` | A W J | OpenTelemetry resource attributes. |
| **Configuration store** | | | | |
| `CONFIG_STORE` | `filesystem` | `blob` | A W J | Where `sources.yaml`, policy, facets and profiles are read from. |
| `CONFIG_DIR` | `./config` | – | – | Local path when `CONFIG_STORE=filesystem`. |
| `CONFIG_CONTAINER` | `config` | `config` | A W J | Blob container with the configuration. |
| **Storage** | | | | |
| `BLOB_ACCOUNT_URL` | – | `https://<st>.blob.core.windows.net` | A W J | Keyless blob access via the identity. |
| `BLOB_CONNECTION_STRING` | – | *unset* | – | Local Azurite only. **Never set in Azure** (shared keys are disabled). |
| `RAW_STORE` | `filesystem` | `blob` | A W J | Where raw documents live. |
| `RAW_DIR` | `./.data/raw` | – | – | Local path when `RAW_STORE=filesystem`. |
| `RAW_CONTAINER` | `raw-docs` | `raw-docs` | A W J | Raw document container. |
| `EXPORTS_CONTAINER` | `exports` | `exports` | A W J | CSV exports from the admin API. |
| **State store** | | | | |
| `STATE_DB_URL` | sqlite | `postgresql+psycopg://<identity>@<server>:5432/ragos?sslmode=require` | A W J | User = the managed identity name. |
| `PG_ENTRA_AUTH` | `false` | `true` | A W J | Use an Entra access token as the PostgreSQL password. |
| **Queue** | | | | |
| `QUEUE` | `sql` | `servicebus` | A W J | `in_memory` / `sql` / `servicebus`. |
| `QUEUE_LOCK_SECONDS` | `300` | default | A W J | Lock duration for the `sql` queue; keep it aligned with `QueueLockDuration`. |
| `SERVICEBUS_NAMESPACE` | – | `<ns>.servicebus.windows.net` | A W J | Namespace FQDN (keyless). |
| `QUEUE_PRIORITY` / `QUEUE_BULK` | `ingest-priority` / `ingest-bulk` | same | A W J | Interactive uploads vs backfills. |
| `QUEUE_MAX_DELIVERY` | `5` | `QueueMaxDeliveryCount` | A W J | Attempts before the dead-letter queue. |
| **Search** | | | | |
| `SEARCH_BACKEND` | `local` | `azure` | A W J | `azure` / `local` (state-DB index for compose) / `in_memory`. |
| `SEARCH_ENDPOINT` | – | `https://<search>.search.windows.net` | A W J | Keyless; API keys are disabled on the service. |
| `INDEX_PREFIX` | `kb` | default | A W J | First part of the index name. |
| `INDEX_DOMAIN` | `enterprise` | `IndexDomain` | A W J | Second part: `kb-<domain>-<fingerprint>`. |
| `ACTIVE_INDEX` | – | `ActiveIndex` if set | A W J | Pin an index while migrating embedding models. |
| `SEARCH_SEMANTIC` | `true` | default | A | Use the semantic ranker. |
| `SEARCH_COMPRESSION` | `scalar` | default | A W J | `scalar` = int8 quantization (4x smaller index), `binary` = 1-bit (smaller still, more recall loss), `none` = full float32. **Not part of the embedding fingerprint**, so a change only takes effect on a rebuilt index. |
| `IN_MEMORY_INDEX_PATH` | `./.data/index.jsonl` | – | – | Local backends only. |
| **Embeddings** | | | | |
| `EMBEDDING_PROFILE` | `qwen3-0.6b-1024` | `EmbeddingProfile` | A W J | Profile name in `config/embedding/profiles.yaml`; decides the index fingerprint. |
| `TEI_QUERY_URL` | `http://localhost:8081` | `http://rag-embed-query` | A W J | Query embedding pool. |
| `TEI_INGEST_URL` | falls back to query | `http://rag-embed-ingest` | A W J | Ingestion embedding pool (GPU). |
| `AOAI_ENDPOINT` | – | `https://<account>.openai.azure.com` | A W J | Azure OpenAI endpoint of the Foundry account. |
| `AOAI_API_VERSION` | `2024-10-21` | default | A W J | Azure OpenAI API version. |
| `AOAI_CHAT_DEPLOYMENT` | – | `AnswerModelName` | A W J | Deployment the **answer** role calls. |
| `AOAI_UTILITY_DEPLOYMENT` | – | `UtilityModelName` | A W J | Deployment the **utility** role calls. Unset = reuse `AOAI_CHAT_DEPLOYMENT`. |
| `AOAI_EMBED_DEPLOYMENT` | – | set when `DeployAoaiEmbedding` | A W J | Only for an `azure_openai` embedding profile. |
| `AOAI_API_KEY` | – | *unset* | – | Keyless by default; a `kv://` reference if you ever need a key. |
| **LLMs** | | | | |
| `LLM_ANSWER` | `fake` | `AnswerModelProvider` | A | Provider that writes the cited answer. |
| `LLM_UTILITY` | `fake` | `UtilityModelProvider` | A W J | Provider that **condenses** follow-up questions and **classifies** ambiguous facets. Not query expansion — that is a deterministic taxonomy lookup, no LLM. |
| `CLAUDE_FOUNDRY_RESOURCE` | – | Foundry account name | A W J | Resolves to `https://<name>.services.ai.azure.com/anthropic`. |
| `CLAUDE_MODEL` | `claude-sonnet-5` | `ClaudeAnswerModelName` | A W J | Must equal the deployment name. |
| `CLAUDE_UTILITY_MODEL` | – | `ClaudeUtilityModelName` | A W J | Unset = reuse `CLAUDE_MODEL`. |
| `CLAUDE_API_KEY` | – | *unset* | – | Entra token auth is the default. |
| `CLAUDE_EFFORT` | – | `ClaudeEffort` if set | A W J | `low`…`max`, both Claude roles. Lowering it is often a bigger saving than changing model. |
| `LLM_MAX_OUTPUT_TOKENS` | `8000` | default | A | Includes reasoning tokens. |

#### The two model roles, and why the utility one should not be a reasoning model

| Role | What it does | Calls | Output cap |
|---|---|---|---|
| **Answer** | Writes the answer the user reads, with `[1]` citations. Refuses rather than answer uncited. | once per answered question | 8000 |
| **Utility** | **Condense** — rewrites a follow-up into a standalone search question. | per question, **only when the chat has history** | 300 |
| **Utility** | **Classify** — picks a facet value when embedding similarity is ambiguous. | per ingested *document*, and only when `CLASSIFIER=embedding+llm` | 800 |

Both roles default to `aoai`. They share one deployment unless `UtilityModelName` names a different model, in
which case step 05 creates a second one and `AOAI_UTILITY_DEPLOYMENT` points at it.

> **Do not give the utility role a reasoning model.** `LLM_MAX_OUTPUT_TOKENS` counts reasoning tokens against the
> cap, so with 300 tokens for condense a reasoning model can spend the whole budget thinking and return an empty
> string — billed, and useless. The code falls back to the original question, so it fails *quietly*. The shipped
> `gpt-4o-mini` is non-reasoning, which is why it is preferred here over the cheaper but reasoning `gpt-5-nano`.

Neither role has anything to do with embeddings — those come from the self-hosted TEI model
(`EMBEDDING_PROFILE` / `EmbedderModelId`), a separate container and code path.
| **Retrieval** | | | | |
| `RETRIEVER` | `direct` | default | A | Retrieval strategy. |
| `RETRIEVAL_TOP_K` | `8` | default | A | Chunks passed to the LLM. |
| `RETRIEVAL_CANDIDATES` | `50` | default | A | Candidates fetched before reranking. |
| `RETRIEVAL_MIN_RERANKER_SCORE` | `1.2` | default | A | Semantic-ranker score, **0–4 scale**. Hits below it are dropped; if none survive the query is refused with no answer-LLM call. This is the threshold you normally want. |
| `RETRIEVAL_MIN_SCORE` | `0.0` | default | A | Raw hybrid score floor; `0` disables it. **On Azure this is an RRF score of roughly 0.01–0.03, not a similarity** — setting anything like `0.5` discards every result. Prefer the reranker threshold above. |
| `ANSWER_HISTORY_TURNS` | `6` | default | A | Conversation turns kept in the prompt. |
| **Auth** | | | | |
| `DEV_AUTH_ENABLED` | `true` | `DevAuthEnabled` (`false`) | A W J | Enables the demo principals and the dev token endpoint. Keep `false` in production. The chat UI does **not** read this — it gets `DEV_EMBED_HOST_ENABLED` instead (below), set from the same psd1 key. |
| `DEV_JWT_KEY` | insecure default | secretRef `dev-jwt-signing-key` | A | Dev token signing key. |
| `DEV_JWT_AUDIENCE` | `rag-os` | default | A | Expected `aud` of a dev token. |
| `DEV_MAX_TOKEN_LIFETIME_S` | `3600` | default | A | Dev tokens with a longer lifetime are rejected. |
| `ENTRA_TENANT_ID` / `ENTRA_AUDIENCE` | – | psd1 if set | A | Entra (RS256) issuer: tenant, and the `aud` the API requires. |
| `ENTRA_CLIENT_ID` / `ENTRA_API_SCOPE` | – | psd1 if set | A | Published in `/api/public-config` so the chat UI can start MSAL. |
| `EMBED_ORIGINS` | `http://localhost:8080` | `EmbedOrigins` | A W J U | Origins allowed to embed the widget (CSP `frame-ancestors`, postMessage check). |
| **Ingestion** | | | | |
| `INGEST_MAX_CONCURRENCY` | `4` | `IngestMaxConcurrency` | W J | Documents in flight per replica. x `WorkerMaxReplicas` = global cap. |
| `INGEST_EMBED_BATCH` | `32` | default | W J | Chunks per TEI call (<= the embedder's `MAX_CLIENT_BATCH_SIZE` = 64). |
| `INGEST_INDEX_BATCH` | `500` | default | W J | Documents per Search upload batch. |
| `INGEST_INDEX_CONCURRENCY` | `2` | default | W J | Parallel Search uploads per replica. |
| `INGEST_MAX_FILE_MB` | `100` | default | W J | Larger files are skipped. |
| `INGEST_RECEIVE_BATCH` | `8` | default | W | Messages received per poll. |
| `INGEST_STALE_MINUTES` | `30` | default | J | Re-queue documents stuck in DISCOVERED/QUEUED. |
| `INGEST_CONTROLS_REFRESH_S` | `30` | default | W | How often workers re-read pause/throttle controls. |
| **Classification** | | | | |
| `CLASSIFIER` | `embedding` | default | W J | `embedding` (no tokens, reuses chunk vectors), `embedding+llm` or `none`. Review never blocks ingestion either way. |
| `CLASSIFIER_MIN_SCORE` | `0.30` | default | W J | Minimum cosine similarity to assign a value. Below it the facet is left **empty** and the document is flagged for review. |
| `CLASSIFIER_MARGIN` | `0.03` | default | W J | Top-2 margin below which the result is ambiguous. With `CLASSIFIER=embedding` this flags the document for **review** (the main driver of queue volume); with `embedding+llm` it also escalates to the LLM. |
| `CLASSIFIER_LLM_TOKEN_BUDGET` | `200000` | default | W J | Token budget per run for LLM classification. |
| **Telemetry** | | | | |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | – | secretRef | A W J | App Insights export. |
| `OTEL_ENABLED` | `true` | `true` | A W J | Turns instrumentation on. |
| **Uploads** | | | | |
| `UPLOAD_SOURCE_ID` | `uploads` | default | A | Source id given to documents uploaded through the API. |
| `UPLOAD_MAX_MB` | `50` | default | A | Maximum upload size. |
| **chat-ui / embedder containers** | | | | |
| `API_UPSTREAM` | – | `http://rag-api` | U | nginx proxy target for `/api/*`. |
| `DEV_EMBED_HOST_ENABLED` | `false` | `DevAuthEnabled` | U | Serves `/dev/embed-host` and `/assets/devhost.js`; both return 404 otherwise. Set from the same psd1 key as `DEV_AUTH_ENABLED`, and they must stay in step — the mock host needs dev tokens from the API. |
| `MAX_BATCH_TOKENS` | image 16384 | psd1 per pool | embedders | Tokens per TEI batch. |
| `MAX_CLIENT_BATCH_SIZE` | image 64 | `64` | embedders | Inputs per request. |
| `RAYON_NUM_THREADS`, `TOKENIZATION_WORKERS` | image | vCPU count | embedders | CPU pools only. |

---

## 6. Domain configuration

All configuration lives in `config/` in the repository and is uploaded to the `config` blob container.

| File | Controls |
|---|---|
| `config/sources/sources.yaml` | Source instances: id, type (`local_folder`, `azure_blob`, `sharepoint`…), domain, lane (`priority`/`bulk`), schedule, settings, classification defaults and ACL defaults. |
| `config/access-policy/access-policy.yaml` | Attributes used for filtering: the `name` (attribute), `field` (the `acl_*` index column on each document), `claims` (what the IdP sends per issuer), the match rule, and how they combine (`all_of` = AND, `grant_any_of` = OR). Plus the admin/SME/reviewer roles. |
| `config/classification/facets.yaml` | Controlled vocabularies (department, region hierarchy, confidentiality, document type, topic…), synonyms and SME owners. |
| `config/classification/path-rules.yaml` | Glob rules that map folder paths to facet values. |
| `config/embedding/profiles.yaml` | Embedding profiles: provider, model, revision, dimensions, prefixes, chunking. The active one is `EMBEDDING_PROFILE`. |
| `config/dev/principals.yaml` | Demo identities for `rag-os ask --as <id>` and the dev token endpoint. Only read when `DEV_AUTH_ENABLED=true`, so it is inert in production — but it is uploaded with everything else, so keep it free of anything real. |

### Edit and publish
```powershell
# 1. edit the files in config/ and commit them
# 2. publish them. Step 08 only SEEDS files that are missing, because the admin API writes these same
#    blobs - so replacing what is already there is an explicit choice, here or with 08 -OverwriteConfig.
az storage blob upload-batch --account-name stragosdevxxxxx --destination config `
  --source ./config --auth-mode login --overwrite true

# 3. reload without a restart (admin token required)
curl -X POST https://<chat-ui-fqdn>/api/admin/config/reload -H "Authorization: Bearer $env:RAG_OS_ADMIN_TOKEN"
```
Adding a **new access attribute** or a new facet field changes the index schema: attributes can often be added in place,
but any other schema change needs a new index version (same procedure as an embedding change, [section 7](#7-embedding-profiles-which-to-run-and-how-to-change-it)).
Adding a **new source** only needs a `sources.yaml` change plus a reload; the next scheduler tick discovers it.

---

## 7. Embedding profiles: which to run, and how to change it

### Which profile should I run?

`config/embedding/profiles.yaml` defines four profiles. **Exactly one is ever active** — `EmbeddingProfile` names
it, and that profile's `provider:` field selects the adapter. The others are inert; their code path is never
constructed.

| Profile | Provider | Model | Dims | Cost | Use it when |
|---|---|---|---|---|---|
| **`qwen3-0.6b-1024`** | `tei` (self-hosted) | Qwen3-Embedding-0.6B | 1024 | **none per token** | **The default. Start here.** |
| `qwen3-0.6b-512` | `tei` (self-hosted) | same model, truncated | 512 | none per token | Index size or query latency is a *measured* problem. |
| `aoai-3-small-1536` | `azure_openai` | text-embedding-3-small | 1536 | billed per token | You would rather not run a GPU pool. |
| `test-fake-256` | `fake` | deterministic hashes | 256 | – | Automated tests only. Never deploy it. |

**This is why you see two models in the psd1.** `EmbedderModelId` configures the self-hosted one;
`EmbeddingModelName` configures the Azure OpenAI one. The latter is gated behind `DeployAoaiEmbedding = $false`,
so by default it is **not deployed at all**.

> **`DeployAoaiEmbedding` deploys a model; it does not select one.** Only `EmbeddingProfile` decides which model
> is used. Setting `DeployAoaiEmbedding = $true` while the profile stays `qwen3-0.6b-1024` creates a Foundry
> deployment that nothing ever calls, still holding regional TPM quota — steps 00 and 05 now warn about that,
> and **fail** on the opposite mistake (an `azure_openai` profile with no deployment, which used to provision
> cleanly and then break on the first query).

**Query and ingestion cannot diverge.** There is one `EmbeddingProfile` setting, one profile object, and both
`embed_query` and `embed_ingest` are built from it — the only thing that differs per pool is which URL it calls.
No setting anywhere can point the two paths at different models. On top of that, `ProfileGuard` returns **503**
from `/readyz` when the running server disagrees with the profile, and the profile fingerprint is part of the
index name, so mismatched vectors cannot land in an existing index.

The self-hosted default is deliberate: the model is Apache-2.0, baked into your own image at a pinned commit, and
costs nothing per token. Embedding is charged on *every chunk of every document you ingest*, so per-token billing
hurts most exactly where this system does the most work.

### How dimensions affect answer quality

Less than you would expect, because the vector is not what orders the results.

A query runs down two arms at once: a **vector kNN** search at `k = RETRIEVAL_CANDIDATES` (50), and **BM25**
over `title/heading/content/path`. Azure fuses them with RRF, then the **semantic ranker re-sorts by reading the
raw text** — it never sees a vector — and `RETRIEVAL_TOP_K` (8) blocks go to the LLM.

So dimensions decide **which 50 chunks get nominated**, not how the survivors are ranked. Dropping 1024 → 512
costs candidate recall, not ranking quality. Qwen3 is also Matryoshka-trained, meaning the earliest dimensions
carry the most signal, so truncating discards the least informative half rather than half the meaning — and the
code re-normalises after truncating, which cosine distance requires.

**Sizing rule of thumb:**

```text
vector bytes ≈ chunks × dimensions × 1 byte        (int8 scalar quantisation is ON by default)

1M documents ≈ 10M chunks → 1024 dims ≈ 10.2 GB
                            512 dims ≈  5.1 GB
one AI Search S1 partition  = 160 GB total storage, of which 35 GB may be vectors
```

**Recommendation: stay on `qwen3-0.6b-1024`.** At ten million chunks the vectors occupy about 10 GB against a
35 GB vector quota, so dimensions are not the binding constraint at that scale. Do keep the quota in mind if you
ever migrate models, though: a migration holds two index copies at once.
Move to 512 when you have measured a problem, not in anticipation of one.

> **If you do move to 512, know that two losses compound.** int8 quantisation is already on, and this index
> configures **no oversampling or rescoring** — `ScalarQuantizationCompression` is created with only a name, and
> vectors are stored `stored=False, retrievable=False`, so exact vectors cannot be re-scored afterwards. What
> HNSW returns from the compressed vectors is final. (HNSW is tuned to push back a little: `m=8` against Azure's
> default of 4, and `ef_search=500`.)

### Levers that matter more than dimensions

| Lever | Where | Why |
|---|---|---|
| `chunk_tokens: 450` | `profiles.yaml` | One vector represents one chunk. Too large and it averages across topics, so precision drops; too small and the passage is no longer citable. |
| The text that gets embedded | `process_item.py` | The document title and heading path are prepended to every chunk before embedding. That is a retrieval lever with nothing to do with the model. |
| `RETRIEVAL_MIN_RERANKER_SCORE` | `settings.py` (1.2) | If every hit scores below it, the query is refused **with no answer-LLM call at all**. |
| The query instruction prefix | `profiles.yaml` | See below. |

`max_input_tokens` (8192) looks like a quality setting and is not one: **no code reads it**. It only feeds the
fingerprint. The number that binds is `chunk_tokens`, and token counting is a `~4 chars/token` estimate rather
than a real tokenizer — the ~7× headroom between 1200 and 8192 is what makes that approximation safe.

> **Queries and documents are embedded differently, and getting it backwards is silent.** Qwen3 wants the
> instruction on the *query* (`query_prefix`) and nothing on the document. A profile that prefixes documents but
> not queries is now rejected at load. But a full swap — both prefixes set, the wrong way round — cannot be
> detected: re-ingest under it and the index is internally consistent, with the same fingerprint and quietly
> worse recall. `scripts/smoke.py` is the only thing that would notice, because it asserts on what a real
> question retrieves.

### Switching to the remote embedder

Four things must happen together. Doing only the first is the common mistake.

1. **`EmbeddingProfile = 'aoai-3-small-1536'`** — this is what actually selects the model.
2. **`DeployAoaiEmbedding = $true`** — otherwise step 05 creates no deployment and step 07 never sets
   `AOAI_EMBED_DEPLOYMENT`. Step 05 now refuses to continue if you forget.
3. **Re-ingest everything.** The fingerprint changes, so the index name changes, so the new index starts *empty*.
4. **Stop paying for the TEI pools.** Steps 06 and 07 now skip building and deploying them for a non-`tei`
   profile, and step 07 prints the `az containerapp delete` commands for any left from an earlier run.
   **Also lower `QueryMinNodes` to 1** — the psd1 sizes it at 2 to fit `api(2) + chat-ui(1) + embed-query(2)` =
   5 vCPU. Without the embedder that is 3 vCPU, so one D4 node is enough. The saving is only real once you
   change it.

> **The green-but-empty trap.** Switch the profile and re-run the bootstrap job *before* re-ingesting and you get
> a new empty index: `/readyz` goes **green** and every question returns "I could not find that" with **HTTP
> 200**. Every health signal says healthy while the assistant answers nothing. The procedure below ingests into
> the new index *before* cutting over, for exactly this reason.

> **You also give up a real check.** With `tei`, `ProfileGuard` compares the model id and revision the server
> genuinely reports from `GET /info`. With `azure_openai`, `info()` returns the profile back to itself, so model
> and revision can never mismatch — it degrades to a reachability check, not an identity check.

### Migrating to a different embedding model

The model, its revision, the dimensions, the prefixes and the chunking are all part of the profile fingerprint,
and the index name is `kb-<domain>-<fingerprint>`. Changing any of them means a **new index** — you can never
re-embed in place.

> `SEARCH_COMPRESSION` is the exception: it is a *setting*, not a profile field, so it is not in the fingerprint
> and `ensure_index` does not check it. Changing it has no effect until the index is rebuilt.

#### When you would actually need to

| | Forced? | Timing |
|---|---|---|
| **Self-hosted (the default)** | **No documented forcing function.** The weights are Apache-2.0, pinned to a commit SHA and baked into an image you build. Nobody can retire them. | — |
| **Azure OpenAI embeddings** | **Yes, already scheduled.** Microsoft lists `text-embedding-3-small`, `-3-large` and `ada-002` for retirement on **2028-02-09** (**2027-04-15** on Azure Government). Notice is 60 days, and retirement dates are not extendable. The replacement model is not named until ~90 days before. | fixed |

So with the shipped configuration a migration is **elective**, which is the whole point: you can schedule it,
rehearse it, and abandon it halfway. The realistic reasons to choose one are better retrieval quality,
multilingual coverage, or smaller vectors — and the frontier is moving in open weights, not in the managed
lineup, which has shipped no new embedding model since January 2024.

The one thing that *is* ageing is the hardware pairing: T4 is the oldest compute capability TEI supports, and the
Turing image is marked experimental with Flash Attention disabled for precision reasons. If that becomes a
problem it forces a **hardware** move (A10, L4), not a model move — the same pinned SHA runs there unchanged,
with no re-embedding.

#### Before you start

* **Capacity.** You will hold **two full copies** of the index during the migration. The binding limit is the
  **vector quota of 35 GB per S1 partition** (not the 160 GB total-storage figure) — indexing hard-fails past it.
  Estimate each copy with `chunks x dimensions x 1 byte`.
* **Can every document still be re-ingested?** Re-embedding reads the original bytes, and whether a durable copy
  exists is a property of the source type — see the table below. For `azure_blob` and upload sources this depends
  on systems you may not control.
* **Know your throughput before you need it.** The window is `chunks / (GPU replicas x chunks-per-second)`, and
  **this repo publishes no value for chunks-per-second** — measure it with `./tasks.ps1 loadtest` while things
  are calm, not during the migration.

#### The sequence

1. **Add the new profile** to `config/embedding/profiles.yaml`, keeping the old one. Both must exist: the old
   index is still serving under the old profile's fingerprint.
2. **Rebuild the embedder images** if the model or revision changed:
   ```powershell
   # edit EmbedderModelId / EmbedderModelRevision / EmbeddingProfile in infra/env/dev.psd1
   ./infra/scripts/06-registry-build.ps1 -Env dev -Images embedder-cpu,embedder-turing
   ```
3. **Give the ingest side the new profile, and only the ingest side:**
   ```powershell
   az storage blob upload-batch --account-name <st> -d config -s ./config --auth-mode login --overwrite true   # replaces admin-edited copies
   ./infra/scripts/07-container-apps.ps1 -Env dev -Only rag-bootstrap,rag-ingest-worker
   az containerapp job start -g rg-ragos-dev -n rag-bootstrap     # creates the new, empty index
   ```
   `rag-api` stays on its existing revision and keeps serving the **old** index throughout.
4. **Back-fill.** Scale up first, then run discovery. A profile change now marks every document as needing
   re-indexing, so this queues the whole corpus:
   ```powershell
   ./infra/scripts/Scale-SearchReplicas.ps1 -Env dev -Replicas 3
   uv run rag-os discover --source <id>
   ```
   Watch it at `/admin` -> Runs, or `GET /api/admin/ingestion/runs/{run_id}` for percent, throughput and ETA.
5. **Verify before cutting over.** Compare the new index's document count against the old one. There is no
   per-index count endpoint, so ask Search directly:
   ```powershell
   $u = "https://<srch>.search.windows.net/indexes/kb-<domain>-<fp>/docs/`$count?api-version=2024-07-01"
   az rest --method get --url $u --resource https://search.azure.com
   ```
6. **Cut over** with a full deploy, which moves `rag-api` onto the new profile and index:
   ```powershell
   ./infra/scripts/07-container-apps.ps1 -Env dev
   ```
7. **Clean up**: scale Search replicas back down, then delete the old index by name. **This is manual** —
   nothing in the repo lists or deletes indexes:
   ```powershell
   az rest --method delete --url "https://<srch>.search.windows.net/indexes/kb-<domain>-<old-fp>?api-version=2024-07-01" --resource https://search.azure.com
   ```

> **`-Only` in step 3 is load-bearing, and it is fragile.** `EMBEDDING_PROFILE` lives in the shared `COMMON_ENV`
> block that step 07 renders into *every* workload, so an unqualified `07-container-apps.ps1` run — a routine
> code deploy, for instance — will cut `rag-api` over to the new, still-filling index without asking. Freeze
> other deployments for the duration of a migration.

> **A change of model is harder than a change of dimensions.** Steps 3-6 keep the API on the old index, but both
> TEI pools are built from one `EmbedderModelId`, so the *query* pool would move to the new model while
> `rag-api` still serves old-model vectors — and step 07's drift check refuses that split deliberately. For a
> change of model, plan a maintenance window or accept degraded answers during the backfill. Changing
> dimensions, prefixes or chunking on the **same** model has no such constraint.

#### Which sources can be re-ingested at all

Re-embedding reads the original bytes. Whether a durable copy exists is a property of the source type:

| Source type | Copy staged into `raw-docs`? | Re-ingestable later? |
|---|---|---|
| `local_folder` | **Yes** | Yes — the copy is ours, and `raw-docs` has no lifecycle policy, so it is kept indefinitely |
| `azure_blob` | **No** — the record points at the original blob | Only while the upstream container keeps it |
| `upload` | **No** | Only while the original remains |

For `azure_blob` and upload sources, your ability to migrate in two years depends on retention you may not
control. If that matters, mirror those documents into `raw-docs` yourself, or put the upstream under a retention
policy that outlives your migration horizon.

#### Two improvements worth making before a large migration

* **Index aliases.** Azure AI Search supports aliases (GA, about 10 s to propagate) and Microsoft recommends them
  for exactly this swap: point the read path at a stable alias, then repoint the alias at the new index. That
  makes cutover atomic and removes both the redeploy and the `-Only` fragility above. RAG-OS does not use aliases
  today — it derives the index name from the fingerprint — so this is a deliberate change, not a knob to turn.
* **Re-embed from the old index instead of re-parsing.** Chunk `content` is retrievable and stored in the index,
  so a migration could read chunks straight out of the old index and re-embed them, skipping parse, chunk and
  classify entirely — plausibly hours instead of days at a million documents. It needs a paged index-scan API
  (none exists) and an ACL re-join from PostgreSQL, because the `acl_*` fields are written `retrievable=false`.

Both TEI pools must run the same model and revision as the profile. `/api/readyz` compares TEI `/info`
(`model_id`, `model_sha`) with the profile and refuses to serve on a mismatch — there is never a silent fallback.

---

## 8. Claude on Foundry

Claude is **optional and off by default** — both roles ship as `aoai`. Set a role's provider to `claude` and
step 05 deploys the model that role names; there is no separate enable flag.

```powershell
# psd1: AnswerModelProvider = 'claude'   (ClaudeAnswerModelName defaults to claude-sonnet-5)
./infra/scripts/05-foundry.ps1 -Env dev
```

Claude is called with Entra tokens from the managed identity through `CLAUDE_FOUNDRY_RESOURCE` (the account
name) — no API key. **The deployment name must equal the model id**, because that is what `CLAUDE_MODEL` carries.

If the CLI cannot deploy it (the subscription has not accepted the Anthropic marketplace terms yet):

1. Open <https://ai.azure.com> and select the project `proj-ragos-dev` (account `aif-ragos-dev-xxxxx`).
2. **Model catalog** -> search the model named in your psd1 -> **Deploy**, accepting the marketplace terms.
3. **Deployment name = the model id** (e.g. `claude-sonnet-5`). Deployment type: Global Standard.
4. Re-run `./infra/scripts/07-container-apps.ps1 -Env dev`.

The identity needs both **Cognitive Services OpenAI User** (GPT) and **Cognitive Services User** (the Anthropic
endpoint) — step 05 assigns both. Verify:
```powershell
az cognitiveservices account deployment show -g rg-ragos-dev -n aif-ragos-dev-xxxxx --deployment-name claude-sonnet-5 --query "{state:properties.provisioningState, model:properties.model.name}"
```

### Which model for which role

Claude refusals on the answer role fall back to Azure OpenAI automatically, so a Claude answer model still needs
a working `AOAI_CHAT_DEPLOYMENT`.

| Role | Default | Why |
|---|---|---|
| Answer | `claude-sonnet-5` ($2/$10 per 1M) | 2.5× cheaper than `claude-opus-5`; RAG answering is grounded synthesis over retrieved passages, which is where Sonnet holds up best. |
| Utility | `claude-haiku-4-5` ($1/$5 per 1M) | 5× cheaper than Opus. Rewriting a question and picking a facet from a list are exactly this model's work. |

**Cheaper still: keep the utility role on Azure OpenAI** (`UtilityModelProvider = 'aoai'`, the default). The
shipped `gpt-4o-mini` costs $0.15/$0.60 per 1M and is *non-reasoning*, which matters more than the price here —
see the warning in [section 5](#5-application-settings) about reasoning models and the 300/800 output caps.

> Microsoft does not publish Claude rates: Claude on Foundry is billed through Azure Marketplace with the model
> provider setting the price, so it appears on no Azure pricing page. The figures above are Anthropic's own
> published rates, which it states apply to Foundry. Confirm against your Marketplace invoice before budgeting.

---

## 9. Signing people in with Microsoft Entra ID

Everyone who uses the assistant has an account in your directory: Entra is the **only** production issuer, and
this step is what makes the deployment usable. Background and the reasoning behind each choice are in
[Where a caller's attributes come from](README.md#where-a-callers-attributes-come-from).

### 9.1 One app registration, both sides

The browser (SPA) and the API are the same registration: the SPA asks for a scope the same app exposes.

**Do this before you deploy.** All four psd1 values derive from the app id alone — none of them needs a URL that
only exists after step 07. Only the *redirect URI* needs the chat UI FQDN, and that lives on the Entra app, not
in the psd1, so it can be added afterwards. Registering first therefore saves a step-07 redeploy:

1. create the registration and expose the scope (below),
2. put the four values in `dev.psd1`,
3. deploy,
4. add the redirect URI once step 08 prints the chat URL.

```powershell
$app = az ad app create --display-name 'RAG-OS' | ConvertFrom-Json
az ad app update --id $app.appId --identifier-uris "api://$($app.appId)"
```

Then, in the portal (Expose an API), add the scope **`access_as_user`** and pre-authorise the same client id, so
signing in does not ask every user for consent.

Later, once you have the chat UI FQDN from step 08 — redirect URIs can be added to a live registration at any
time, and this changes nothing in the psd1:

```powershell
# add the localhost entry only if you also run the UI locally
az ad app update --id $app.appId --set "spa={`"redirectUris`":[`"https://<chat-ui-fqdn>/auth/callback`",`"http://localhost:8080/auth/callback`"]}"
```

The values RAG-OS needs are now:

| Value | Example |
|---|---|
| `EntraAudience` | `api://<app-id>` — what the API requires in `aud` |
| `EntraApiScope` | `api://<app-id>/access_as_user` — what the browser requests |
| `EntraClientId` | `<app-id>` |
| `EntraTenantId` | `<tenant-guid>` |

### 9.2 Carry the attributes

Choose one of the two approaches:

* *Directory extension or optional claim* — emit `extension_Department`, `extension_Region` and
  `extension_Clearance` (an integer). Nothing to configure in RAG-OS: the shipped `access-policy.yaml` already
  points at these claim names.
* *Security groups* — add the `groups` claim to the token, point `claims.entra` at `groups` and map each group's
  **object id** to a value with `value_map`. Apply a **group filter** on the app registration so only the
  `kb-*` groups are emitted: past roughly 150 groups Entra sends `_claim_names` / `_claim_sources` instead of
  `groups`, and RAG-OS does not call Microsoft Graph to resolve them.

```powershell
az ad group show --group kb-dept-hr --query id -o tsv   # the id that goes in value_map
```

Guests invited into your tenant need no special case — they validate through this same issuer and are granted
attributes the same way. A *separate* tenant (Entra External ID / Azure AD B2C) is a different issuer URL and is
not configurable today: `composition.py` builds one Entra issuer from the settings below.

The `clearance` claim (`extension_Clearance`) must arrive as an **integer** (`0` Public … `3` Restricted). If your
directory can only emit a label,
map it with `value_map` — see [What `clearance: 0, 1, 2` signifies](README.md#what-clearance-0-1-2-signifies).

### 9.3 Admin role

Add the app role **`rag.admin`** (member type `User`) and assign it to your platform admins. The accepted role
values are listed under `roles:` in `config/access-policy/access-policy.yaml`; the `entra` issuer is already in
`role_sources.trusted_for_roles`.

### 9.4 Point RAG-OS at the tenant

In `infra/env/dev.psd1`, then re-run step 07:

```powershell
EntraTenantId = '<tenant-guid>'
EntraAudience = 'api://<app-id>'
EntraClientId = '<app-id>'
EntraApiScope = 'api://<app-id>/access_as_user'
```

`07-container-apps.ps1` sets each of these only when it is non-empty, and warns if neither Entra nor dev auth is
configured — in which case nobody can sign in at all. Keep `DevAuthEnabled = $false`.

### 9.5 Check it end to end

Open `https://<chat-ui-fqdn>/`, choose **Sign in with Microsoft**, and confirm you land back on the chat. Then:

```powershell
$token = az account get-access-token --scope "api://<app-id>/access_as_user" --query accessToken -o tsv
curl -H "Authorization: Bearer $token" https://<chat-ui-fqdn>/api/me
```

The response shows the mapped attributes and roles — the quickest way to confirm a `value_map` before anyone asks
a question. `rag-os explain` prints the filter those attributes produce.

| Symptom | Cause |
|---|---|
| Sign-in loops back to the sign-in panel | The token was issued for the wrong audience. Check `EntraApiScope` matches the exposed scope exactly. |
| `AADSTS50011` redirect mismatch | The SPA redirect URI must be `https://<chat-ui-fqdn>/auth/callback`, exactly. |
| 401 `untrusted issuer` | `EntraTenantId` does not match the tenant that issued the token. |
| Signed in, but no documents | The attributes are missing or unmapped: `GET /api/me` shows what arrived. |

### 9.6 Embedding the assistant in another page

The embedded chat does **not** run its own sign-in. The host page supplies an Entra access token for
`ENTRA_API_SCOPE` — one it already holds for the signed-in user, or one acquired on-behalf-of.

```html
<div id="rag-chat"></div>
<script src="https://<chat-ui-fqdn>/embed/loader.js"
        data-token-endpoint="/your/backend/token"
        data-target="#rag-chat"></script>
```

The loader creates the iframe and asks your backend for a token (`{"token": "...", "expires_in": 3600}`). The host
page and the iframe exchange:

```js
// host -> iframe (the iframe checks event.origin against its own origin allow-list)
// expiresAt (epoch ms) is what the widget uses to refresh ~60 s early - send it, or the token expires mid-session
iframe.contentWindow.postMessage({ type: 'rag-os:token', token, expiresAt }, 'https://<chat-ui-fqdn>');
// iframe -> host when a token expires or is rejected
window.addEventListener('message', (e) => { if (e.data?.type === 'rag-os:token-request') sendFreshToken(); });
```

Finally add the host origin to `EmbedOrigins` (comma-separated) and re-run step 07, so that nginx sends
`Content-Security-Policy: frame-ancestors 'self' https://intranet.contoso.com` and the widget accepts messages
from it. Serve the token endpoint from your own authenticated session; never expose a token to an anonymous page.

---

## 10. Operations

### Ingestion from a local folder (admin machine)
```powershell
az login --tenant <TenantId>
$env:CONFIG_STORE='blob'; $env:BLOB_ACCOUNT_URL='https://stragosdevxxxxx.blob.core.windows.net'
$env:RAW_STORE='blob'; $env:QUEUE='servicebus'; $env:SERVICEBUS_NAMESPACE='sb-ragos-dev-xxxxx.servicebus.windows.net'
$env:STATE_DB_URL='postgresql+psycopg://<you@tenant>@psql-ragos-dev-xxxxx.postgres.database.azure.com:5432/ragos?sslmode=require'
$env:PG_ENTRA_AUTH='true'
uv run rag-os discover --source hr-share          # uploads to raw-docs/ and enqueues
```
Your account needs the data-plane roles (step 03 grants Blob Data Contributor and Service Bus Data Sender) and your IP
in the PostgreSQL firewall (`AllowClientIp`).

### Status, retries, throttling
- Dashboard: `https://<chat-ui-fqdn>/admin` (totals, per source, throughput, failures, dead-letter queue, review queue).
- API: `GET /api/admin/ingestion/summary`, `/runs`, `/documents?status=FAILED&source=hr-share`, `/documents/{id}`, `GET /api/admin/ingestion/dlq`.
- Retry: `POST /api/admin/ingestion/retry` with the document ids (or "all failed in run X").
- Pause/throttle (takes effect within ~30 s, no redeploy):
  ```powershell
  $env:RAG_OS_ADMIN_TOKEN = '<admin jwt>'
  ./infra/scripts/Set-IngestionControls.ps1 -Env dev -Pause
  ./infra/scripts/Set-IngestionControls.ps1 -Env dev -Resume -MaxConcurrency 8
  ```

### Backfill playbook
1. `./infra/scripts/Scale-SearchReplicas.ps1 -Env dev -Replicas 3`
2. Raise `WorkerMaxReplicas`/`GpuMaxReplicas` in the psd1 and run step 07 (or throttle with `Set-IngestionControls.ps1`).
3. Start the backfill with `uv run rag-os discover --source <id>`. Set `lane: bulk` on that source in
   `sources.yaml` first, so interactive uploads keep the priority lane to themselves.
4. Watch `/admin` and the dead-letter count; `Set-IngestionControls.ps1 -MaxConcurrency 2` if Search starts throttling (503/207).
5. Afterwards: scale replicas back down and restore the psd1 values.

### Smoke test
```powershell
./infra/scripts/09-smoke.ps1 -Env dev
```
Checks that `rag-api` is internal-only, that every app/job secret is a Key Vault reference, that the chat UI answers,
and that the API is unreachable from outside. Then it runs `scripts/smoke.py`.

**How much it verifies depends on how many identities you give it**, and the script says which checks it skipped:

| Tokens available | What runs |
|---|---|
| **One identity** (the default with `DevAuthEnabled = $false`) — the script acquires *your* access token with `az account get-access-token` | Health, readiness, security headers, unauthenticated request rejected, an answer with an HR citation, `usage` present. The A/B separation checks and `upload -> INDEXED` are **skipped**, and reported as skipped. |
| **Two identities + an admin token**, passed by hand | Everything: Sales/US never sees HR documents or HR facets, a non-admin is refused the admin report, and an upload is tracked to `INDEXED`. |
| **`DevAuthEnabled = $true`** | Everything, using the demo principals `hr-emea`, `sales-us` and `admin` — which is why a demo environment verifies more than a locked-down one. |

```powershell
# full verification on an Entra-only deployment: two real people, different attributes
./infra/scripts/09-smoke.ps1 -Env dev -ExtraArgs '--token-a','<jwt>','--token-b','<jwt>','--admin-token','<jwt>'
```

A single identity cannot demonstrate access separation — comparing it with itself would report a pass or a failure
that means nothing — so the script skips those checks rather than appearing to verify them. Nothing is printed:
tokens are passed as arguments and cleared afterwards.

### Load test
```powershell
./infra/scripts/10-loadtest.ps1 -Env dev -ExtraArgs '--docs','10000','--qps','5','--duration','300'
```
Measures query p50/p95 as a baseline, starts the bulk backfill and measures again.
**Pass: p95 during ingestion <= 1.25 x baseline.** Record documents/minute and chunks/second per GPU for the capacity model.

### KQL (Log Analytics / Application Insights)
```kusto
// everything about one request / document, by correlation id
union requests, dependencies, traces, exceptions
| where operation_Id == "<correlation-id>" or customDimensions.correlation_id == "<correlation-id>"
| order by timestamp asc
| project timestamp, itemType, name, message, resultCode, duration, customDimensions

// token usage per day, provider and purpose
customMetrics
| where name == "rag.tokens"
| extend provider = tostring(customDimensions.provider), purpose = tostring(customDimensions.purpose), kind = tostring(customDimensions.kind)
| summarize tokens = sum(valueSum) by bin(timestamp, 1d), provider, purpose, kind
| render columnchart

// ingestion failures by stage and error type
traces
| where customDimensions.event == "ingest.failed"
| extend stage = tostring(customDimensions.stage), error = tostring(customDimensions.error_type), source = tostring(customDimensions.source_id)
| summarize count() by stage, error, source
| order by count_ desc

// query latency percentiles, with and without ingestion running
requests
| where name endswith "/api/chat"
| summarize p50 = percentile(duration, 50), p95 = percentile(duration, 95), count() by bin(timestamp, 5m)

// container logs of a failed job execution
ContainerAppConsoleLogs_CL
| where ContainerGroupName_s startswith "rag-bootstrap"
| order by _timestamp_d desc
| project _timestamp_d, Log_s
```

---

## 11. Update, rollback, teardown

### Update (new code)
```powershell
git pull
./infra/scripts/06-registry-build.ps1 -Env dev                 # builds :<new git sha>, records digests
./infra/scripts/07-container-apps.ps1 -Env dev                 # new revisions, deployed by digest
./infra/scripts/09-smoke.ps1 -Env dev
```
Configuration-only changes (psd1 app settings): run step 07 alone. Domain configuration: upload + reload ([section 6](#6-domain-configuration)).

### Rollback
```powershell
# preferred: redeploy the previous tag (deterministic, same scripts)
az acr repository show-tags -n acrragosdevxxxxx --repository rag-api --orderby time_desc -o table
./infra/scripts/07-container-apps.ps1 -Env dev -Tag <previous-tag>

# revision-level alternative
az containerapp revision list -g rg-ragos-dev -n rag-api --query "[].{name:name, created:properties.createdTime, active:properties.active, image:properties.template.containers[0].image}" -o table
az containerapp revision activate -g rg-ragos-dev -n rag-api --revision <older-revision>
```
The apps run in **single-revision** mode, so redeploying the previous tag is the reliable path; revision activation is
mainly useful after `az containerapp revision set-mode --mode multiple` for traffic splitting.

### Teardown
```powershell
./infra/scripts/99-teardown.ps1 -Env dev              # -Force to skip the confirmation prompt
```
Deletes the resource group (**all data**), then purges the soft-deleted Foundry account. The Key Vault has purge
protection, so its name stays reserved for `KeyVaultRetentionDays`; the next run of step 02 recovers that vault
automatically. Log Analytics and App Insights are recoverable for 14 days under the same name.

---

## Appendix — what runs where

| Workload | Kind | Profile | Ingress | Replicas | Command |
|---|---|---|---|---|---|
| `rag-chat-ui` | app | `query` | **external** | 1-3 | nginx |
| `rag-api` | app | `query` | internal | 2-10 (HTTP 50/replica) | `uvicorn rag_os.api.main:app --port 8000 --proxy-headers` |
| `rag-embed-query` | app | `query` | internal | 1-4 | TEI (CPU image) |
| `rag-embed-ingest` | app | `gpu-t4` (or `ingest`) | internal | 0-N | TEI (turing image, or CPU fallback) |
| `rag-ingest-worker` | app | `ingest` | none | 0-N (KEDA azure-servicebus) | `rag-os worker` |
| `rag-scheduler` | job | `ingest` | – | cron `*/5 * * * *` | `rag-os schedule-tick` |
| `rag-bootstrap` | job | `ingest` | – | manual | `rag-os bootstrap` |

Generated files (do not commit): `infra/env/<env>.psd1`, `infra/env/<env>.outputs.json` (ids and endpoints discovered by
the scripts), `infra/env/<env>.images.json` (image digests + the embedding image record).
