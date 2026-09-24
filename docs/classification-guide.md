# Organizing and classifying documents (hundreds to millions)

RAG-OS separates two jobs, and confusing them is the most expensive mistake you can make here.

Throughout this guide, `f_*` and `acl_*` are **index field names** — columns carried on every indexed chunk,
describing the document. `acl.<name>` (with a dot) is the *tagging* key you write in a manifest or sidecar, and
`<name>` on its own is the attribute. None of them is a claim; claims are what the identity provider sends about
the person, and they are mapped to attributes by `access-policy.yaml`. See
[How the access policy works](../README.md#how-the-access-policy-works).


* **Access metadata** decides *who may retrieve* a document. It is enforced inside every search, default-deny
  applies, and it is configured in `config/access-policy/access-policy.yaml` (index fields `acl_*`).
* **Descriptive metadata (facets)** decides *how documents are organized, filtered and found*. It has **no
  security value whatsoever** and is configured in `config/classification/facets.yaml` (index fields `f_*`).

`department` and `region` deliberately exist in both files. Tagging `facet.department: HR` tells the UI how to
group the document; it grants nobody anything. Only `acl.department` does that.

Both are assigned by the same layered mechanism, cheapest layer first.

---

## 1. What is actually read

Almost every value is decided **before the document is opened**. Only one signal looks inside it, and none of
them delays ingestion — the last row happens *after* the document is live.

| Signal | Read from | What it can set |
|---|---|---|
| The source | `defaults:` on the instance in `sources.yaml` | Anything; the weakest layer. |
| **The folder path** | The *source-relative* path, matched by globs in `path-rules.yaml` | department, region, doc type + ACL twins. |
| The filename | No rule reads it — but it becomes the **title** when the parser finds none | Indirectly doc type and topic (see §3). |
| A sidecar | `<file>.<ext>.meta.json` next to the file | Anything, one file at a time. |
| A manifest row | `manifest.csv` at the source root | Anything, in bulk. |
| The content | Mean of the first 8 chunk vectors vs a prototype per facet value | **Only** `classify: true` facets still empty. |
| A person | `/admin` → Review queue, **after the document is already indexed** | Anything, and it is then frozen. Never a gate — see §6. |

### Precedence

```
source defaults  <  path rules  <  sidecar  <  manifest.csv  <  upload form  <  classifier  <  SME review
```

Two properties that surprise people:

* A later layer **replaces** a key wholesale — values are never unioned. If a path rule sets
  `department: [HR]` and the manifest sets `department: [Legal]`, the result is `[Legal]`, not both.
* An **empty value never clears** an inherited one. A blank manifest cell is skipped, not applied. To widen
  access you must write a value (for example `*`), not erase one.

Provenance is recorded per value (`tags.sources`), so `/admin` can show you exactly which layer set each tag —
`path_rule:hr/**`, `manifest`, `sidecar`, `classifier:embedding`, `review:<user>`.

---

## 2. The folder convention

```
<source root>/
  <department>/            hr | finance | sales | it | legal | support
    <region>/              global | emea | uk | de | us | apac | in
      <doc type>/          policies | procedures | contracts | pricing
        <file>
  manifest.csv             optional bulk overrides
```

A single rule such as `hr/**` then tags and secures every HR file, and `*/uk/**` handles a whole region. For
hundreds of files, SMEs usually only touch the manifest.

**The glob syntax is `fnmatch`, not gitignore or pathlib.** Three consequences worth internalising:

* `*` and `**` both **cross `/`**. `*/emea/**` also matches `sales/apac/emea/report.pdf`. There is no way to say
  "exactly one segment".
* Matching is **case-insensitive** (both sides are lower-cased), and `\` is normalised to `/`, so the same rules
  work on Windows and Linux.
* **Every** matching rule applies, in file order, and the **last one wins** per key. There is no specificity
  ranking — order your file from general to specific.

The path matched is **relative to the source**, never absolute. For Azure Blob sources the configured `prefix`
is stripped first, so a source with `prefix: knowledge/` needs rules written as `hr/**`, not `knowledge/hr/**`.

> **Uploads do not follow the convention.** An uploaded document's path is `<user subject>/<filename>`, so folder
> rules never match it. Uploads get their ACL from the uploader's own attributes and their facets from the upload
> form. Plan for that rather than expecting `hr/**` to fire.

### Filenames

No rule parses a filename, and nothing infers a document type or language from it or from its extension. It still
matters, in three ways:

1. When the parser finds no title — plain `.txt`, legacy `.xls`, an untitled PDF — **the filename becomes the
   title**. That title is stored, shown in citations, prefixed onto every chunk before embedding, and handed to
   the auto-classifier. A file called `doc1 (final v3).pdf` is quietly degrading its own classification.
2. `path` is a searchable field, so folder and file names contribute to keyword relevance.
3. The extension selects the parser (and `.log`, `.tsv`, `.jsonl` change parser behaviour).

Practical rule: name the file the way you would want it to appear as a citation. `parental-leave-policy.md`,
not `PLP_v3_FINAL(2).md`.

### Sidecar and manifest formats

Ready-to-copy versions of both files live in [examples/](examples/) and are parsed by the test suite, so they
cannot drift from what the code accepts.

#### The manifest

`manifest.csv` at the **source root**, one row per document. It is the highest-precedence rule layer: it beats
folder rules and sidecars.

```csv
path,facet.doc_type,facet.topic,facet.confidentiality,acl.department,acl.region,acl.clearance,owner,notes
hr/uk/policies/parental-leave-policy.md,Policy,Leave,Internal,,,1,hr-ops@contoso.example,folder rules already set HR/UK
sales/emea/contracts/msa-fabrikam.docx,Contract,,Confidential,Sales;Legal,,2,legal@contoso.example,legal reviews contracts too
finance/emea/reports/q2-2026-spend.csv,Report,,Confidential,Finance,,2,fp-a@contoso.example,reports/ has no path rule
support/global/faq/product-faq.json,FAQ,Troubleshooting,Public,*,Global,0,support@contoso.example,readable by everyone
```

Every row above is doing a specific job:

* **Row 1** leaves `acl.department` and `acl.region` **blank on purpose** — the `hr/**` and `*/uk/**` folder rules
  already set them. A blank cell **inherits**; it does not clear. There is no way to erase a folder rule's value
  from the manifest, only to overwrite it.
* **Row 2** uses `;` to give two departments — `Sales;Legal`. A comma would break the CSV.
* **Row 3** supplies `doc_type` because `reports/` has no folder rule; it also raises clearance above the source
  default of 1.
* **Row 4** widens access with the wildcard `*` — to open a document to everyone you *write a value*, you do not
  blank the cell.
* **`owner` and `notes` are ignored by RAG-OS.** Unrecognised columns are harmless, so the manifest can stay a
  working spreadsheet with human columns in it.

> **The mistake that costs an afternoon:** the `path` header must be lower-case. `Path` or `PATH` matches nothing,
> **every row is silently skipped**, and no error is raised anywhere. The `facet.` and `acl.` prefixes are
> case-sensitive too. If a manifest appears to do nothing at all, check the header casing first.

Paths are matched case-insensitively against the source-relative path; leading slashes and backslashes are
forgiven. Encoding is sniffed, so Excel's "Save as CSV" works. Numeric ACL values arrive as text
(`acl.clearance,2` → `"2"`) and are coerced by the access engine — the sidecar differs here, see below.

#### The sidecar

For per-document exceptions: a JSON file next to the document, named after the document's **full filename
including its extension**, plus `.meta.json`. For `msa-fabrikam.docx` that is
`msa-fabrikam.docx.meta.json` — *not* `msa-fabrikam.meta.json`.

```json
{
  "facets": {
    "doc_type": ["Contract"],
    "topic": ["Pricing"],
    "confidentiality": ["Confidential"]
  },
  "acl": {
    "department": ["Sales", "Legal"],
    "region": ["EMEA"],
    "clearance": 2,
    "employee_id": ["E1001"]
  }
}
```

* **Only `facets` and `acl` are read.** A `title`, `effective_date` or `$schema` key is ignored silently — the
  sidecar cannot override a document's title or dates, only its tags.
* `"clearance": 2` keeps the integer. **`"clearance": [2]` does not** — it becomes the string `"2"`, because the
  check is on the value rather than the list element. Write numeric ACL values bare.
* `acl.employee_id` is the per-person escape hatch: it shares this one document with employee `E1001` regardless
  of department or region, because `employee_id` is a `grant_any_of` attribute.
* Strict UTF-8 (a BOM is tolerated), 256 KiB maximum, and **failure is silent** — malformed JSON means the
  sidecar is treated as absent, with no error and no log line. If a tag does not appear, check the file parses.

#### What those two files actually produce

Resolved by the real `TagResolver` against the shipped configuration:

```text
hr/uk/policies/parental-leave-policy.md
  facet department      = [HR]                <- path_rule:hr/**
  facet region          = [UK]                <- path_rule:*/uk/**
  facet doc_type        = [Policy]            <- manifest
  facet topic           = [Leave]             <- manifest
  facet confidentiality = [Internal]          <- manifest
  facet language        = [en]                <- source_default
  acl   department      = [HR]                <- path_rule:hr/**      (blank cell inherited)
  acl   region          = [UK]                <- path_rule:*/uk/**    (blank cell inherited)
  acl   clearance       = 1                   <- manifest

sales/emea/contracts/msa-fabrikam.docx        (manifest row + sidecar)
  facet doc_type        = [Contract]          <- manifest             (manifest beats the sidecar)
  facet topic           = [Pricing]           <- sidecar              (manifest left it blank)
  facet confidentiality = [Confidential]      <- manifest
  acl   department      = [Sales, Legal]      <- manifest             (';' gave two values)
  acl   region          = [EMEA]              <- sidecar
  acl   clearance       = 2                   <- manifest             ("2" coerced to an integer)
  acl   employee_id     = [E1001]             <- sidecar              (per-person grant)
```

Note the second document: the manifest and the sidecar both set `doc_type`, and the **manifest wins**; the
sidecar only supplied what the manifest left blank. Every value carries the name of the layer that set it, and
`/admin` shows exactly this.

---

## 3. The facets, and how each takes effect

| Facet | Auto-classified? | Where its value comes from | What it changes |
|---|---|---|---|
| `department` | no | path rules, manifest | Filtering and grouping. Its ACL twin controls access. |
| `region` | no | path rules, manifest | **Hierarchical** — see below. |
| `doc_type` | **yes** | `*/*/policies/**` etc., else the classifier | Filtering, and prompt hints ("the policy says…"). |
| `topic` | **yes** | manifest, else the classifier | Navigation and query expansion through synonyms. |
| `confidentiality` | no | manifest, sidecar | **Descriptive only** — see the warning below. |
| `language` | no | source `defaults:` | Filtering. The only open facet. |

**Region is hierarchical, and the expansion is mirrored.** A document tagged `region: [UK]` is indexed as
`f_region = [UK, EMEA, Global]`, so filtering on `Global` *finds* UK documents. Access works the other way round:
the document's `acl_region` stays `[UK]`, and the **caller's** region is expanded upward instead, so a UK employee
can read EMEA and Global documents but not the other way. Same tree, opposite direction, and both are what you
want.

**Document type and topic are the only facets the classifier will fill.** The shipped path rules cover
`policies/`, `procedures/`, `contracts/` and `pricing/`; `reports/`, `faq/` and `manuals/` are deliberately left
for the classifier so you can see it work.

**Language is not detected.** There is no language detection anywhere in RAG-OS. The value comes from the source
default (`facets: { language: en }`) and nothing else, unless you set it per document. It is also the only facet
with `closed: false`, so any string you write is accepted verbatim — useful, and a typo-trap.

> **`confidentiality` is not access control.** The `confidentiality` *facet* (indexed as `f_confidentiality`,
> values `Public`…`Restricted`) and the `clearance` *access attribute* (indexed as `acl_clearance`, integers
> `0`–`3`) are different fields. Tagging a document `confidentiality: Confidential` restricts **nobody** — it only labels it. Set
> `acl.clearance: 2` as well. Keeping both is still worth it: the label is retrievable and shown to users, while
> the integer is enforced and never leaves the index.

### Controlled vocabularies

Values are matched case-insensitively against each value's `id`, `label` and `synonyms`, and the canonical `id`
is what gets stored — `britain` and `EUROPE` become `UK` and `EMEA`. In a **closed** facet (the default) an
unrecognised value is **silently dropped**; in an open facet it is kept verbatim.

> Write facet descriptions and labels **in quotes** if they contain a comma. Inside a YAML flow mapping
> (`{ id: X, description: a, b }`) an unquoted comma ends the entry and the rest is discarded. RAG-OS now rejects
> that at config load rather than accepting truncated text, but the quoting is the real fix. Descriptions are not
> decoration: they are the text the classifier embeds to build each value's prototype.

---

## 4. Best practices

1. **Decide the folder convention before you ingest**, and make the first two levels the ones that drive access
   (department, region). Retro-fitting them means re-tagging everything.
2. **Write one path rule per value** of every required access attribute. A region folder with no rule leaves
   `acl_region` empty, and that document is invisible to everyone (§5).
3. **Let the folders do the bulk work**, the manifest handle the exceptions, and the sidecar handle the
   one-offs. Do not try to enumerate a million documents in a manifest.
4. **Name files as you want citations to read.** For untitled formats the filename *is* the title, and the title
   feeds both search and the classifier.
5. **Give every `classify: true` value a real description and synonyms.** That text is the classifier's only
   training signal; a one-word description is a weak prototype.
6. **Quote anything containing a comma** in `facets.yaml`.
7. **Turn `classify: true` on for doc type and topic, not for access attributes.** Never let a model decide who
   may read something.
8. **Run a sample first and look at what is empty**: ingest a few hundred documents, open `/admin` → Documents
   grouped by facet, and inspect the empty bucket before enabling the schedule. This is the single most useful
   check in this guide.
9. **Work the review queue early.** Approved tags are frozen and become the examples that make the next batch
   better.

---

## 5. When a document stays unclassified

The consequence depends entirely on *which* value is missing.

| Missing | What happens |
|---|---|
| A **business facet** (`f_topic`, `f_doc_type`…) | The document is still parsed, embedded, indexed and **fully retrievable**. It just cannot be narrowed by that filter and is absent from that facet's counts. |
| A **required access attribute** (`department`, `region`) | **Invisible to every non-admin.** Default-deny: a document with no value for an attribute never satisfies it, so it matches nobody's filter — while sitting in the index looking perfectly healthy. |
| `acl_clearance` | Denied, not treated as public: `acl_clearance le N` is false over a null field. |

**How the classifier decides.** It assigns its top candidate when the cosine similarity reaches
`CLASSIFIER_MIN_SCORE` (default `0.30`); below that it assigns **nothing**. Independently, if the top two
candidates are within `CLASSIFIER_MARGIN` (default `0.03`) it marks the document for review. So a document can be
tagged *and* queued for review — that combination means "probably right, worth a look".

**The review queue** is `/admin` → Review queue (`GET /api/admin/review-queue`). Each card shows the current tags
with their provenance and confidence. Approving a correction writes provenance `review:<user>`, **freezes** those
tags so re-discovery never overwrites them, stops the document being re-classified, and enqueues a priority
re-tag that updates the index in place without re-embedding.

> **The failure nobody reports.** A facet left empty by the rules that is **not** `classify: true` —
> `department`, `region`, `confidentiality`, `language` — is never offered to the classifier, so it never reaches
> the review queue either. The document is indexed with an empty value and nothing is raised. If that value is a
> required access attribute, the document is silently invisible to everyone. Nothing will tell you; you have to
> look. Group by facet in `/admin` and inspect the empty bucket.

Two more ways a value can vanish quietly:

* An **ACL value that fails validation** is dropped at index time — values must match
  `^[A-Za-z0-9][A-Za-z0-9 _.@/-]{0,127}$` and must not contain `|`. So `R&D` or ` HR` disappear, taking the
  document's visibility with them. (Facet values are validated too, but a dropped facet only costs a filter.)
* The wildcard `*` works for `any_of` and `hierarchical` attributes but is **never** honoured for an `exact`
  grant such as `employee_id`.

---

## 6. Does review slow ingestion down?

**No. Nothing waits for a human.** The review queue is a quality backlog, not a gate. The order inside
`ProcessItem._full` is:

```
parse → chunk → EMBED → classify (reusing those vectors) → INDEX
```

Embedding happens **before** classification, and classification is what raises the review flag. By the time a
document appears in the queue it is already embedded, indexed and retrievable.

| Concern | Reality |
|---|---|
| Does a document wait for approval before being embedded? | No — embedding precedes classification. |
| Is a flagged document searchable? | **Yes, immediately.** `review_status` lives only in the state database. It is not an index field and appears nowhere in the query path, so it cannot filter results even in principle. |
| What if nobody ever opens the queue? | Nothing degrades. Documents stay indexed, searchable and correctly ACL'd, and keep the classifier's values across re-discovery. |
| What does approving cost later? | A **re-tag**: facet and ACL fields are merged into the existing chunks on the priority lane. No re-parse, no re-embed. |
| Is there a timeout, auto-approval or reminder? | None. `PENDING` is never cleared automatically, and nothing nags. |

**Most documents never reach the classifier at all.** It is invoked only for facets that are `classify: true`
**and** still empty after the rule layers. A document fully tagged by a path rule, sidecar or manifest row never
calls it and can never enter the review queue. Queue volume is a direct measure of how much your rules leave
unsaid — tag well and the queue is empty by construction.

**And when it does run, the default costs nothing.** No tokens and no network calls: the document vector is the
mean of chunk vectors already computed, facet prototypes are embedded once per schema version and cached, and the
per-document work is one matrix multiply per facet. Only `CLASSIFIER=embedding+llm` spends tokens, only on
ambiguous facets, and only within `CLASSIFIER_LLM_TOKEN_BUDGET`.

This is enforced by the test suite: `test_review_queue_approve_retags_without_reembedding` ingests the whole
corpus, *then* finds documents in the review queue, and asserts the embedder call count is unchanged after an SME
approves — proving both that the documents were indexed before review and that approving does not re-embed.

**If the queue does grow**, it is telling you the rules are thin, not that your people are slow. In order of
usefulness: add path rules or manifest rows so fewer facets are left empty; improve the `description` and
`synonyms` of the facet values (that text *is* the classifier's signal); tune `CLASSIFIER_MIN_SCORE` and
`CLASSIFIER_MARGIN`; or set `CLASSIFIER=none` to stop queueing entirely.

## 7. Onboarding a large repository

1. Draft facets with the SMEs (start with department, region, doc type, confidentiality).
2. Map the repository's existing folders to path rules — mapping is cheaper than renaming a million files.
   Remember `*` crosses `/`, so anchor rules on the first segment where you can.
3. Ingest a sample. Review `/admin` → Documents grouped by each access attribute and **fix the empty buckets**.
4. Export the report (CSV) and let the SMEs fill gaps in `manifest.csv`.
5. Turn on `classify: true` for doc type and topic. Work the review queue; approvals are permanent.
6. Enable the schedule. After that only new, changed and deleted documents are processed, and tag-only changes
   are applied in place without re-embedding.

## 8. Access-control patterns

* **Department + region + clearance** (`all_of`) is the default: a document is visible only when every attribute
  allows the caller.
* **Explicit share** (`grant_any_of: [employee_id]`): add `acl.employee_id` to the manifest or a sidecar to share
  one file with named people, regardless of the other attributes.
* **Everyone**: use the wildcard `"*"`, as the `it/**` rule does for IT policies.
* **Widely readable content**: clearance `0` plus department `"*"`, which is what the `support/**` rule sets.
* **New attribute** (e.g. `cost_center`): add it to `access-policy.yaml` and reload. The index field is added in
  place and nothing needs rebuilding. Then tag documents with `acl.cost_center` — and remember that until you do,
  every document is missing it.
