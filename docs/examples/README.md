# Templates

Copy these, edit them, drop them in. All three are parsed by the same code that reads your real files, and all
three are checked by the test suite, so they cannot drift from what RAG-OS actually accepts.

| File | What it is |
|---|---|
| `manifest.csv` | Bulk document tagging, one row per document, at a source root. |
| `contract.docx.meta.json` | Per-document tagging for exceptions, sitting next to the file. |
| `access-policy.minimal.yaml` | The smallest access policy that loads — a starting point for `config/access-policy/`. |

Tagging is explained in [../classification-guide.md](../classification-guide.md); the access policy in
[§11 of the README](../../README.md#how-the-access-policy-works).

## `access-policy.minimal.yaml`

Three keys: one attribute with a `name`, a `field` and its `claims`, plus a `combine.all_of` naming it. Everything
else defaults — `version: 1`, `default_decision: deny`, `match: any_of`, `wildcard: "*"`, `required: false`, and
**no roles at all**, which means nobody can reach `/api/admin/*` until you add a `roles:` block.

With that file, a caller carrying `department: [HR]` gets
`(acl_department/any(v: search.in(v, 'HR|*', '|')))`, and a caller carrying no department at all gets
`(acl_department/any(v: v eq '*'))` — public documents only, rather than nothing, because `required` defaults to
`false`.

## `manifest.csv`

One per source, at the **source root**. Bulk tagging: one row per document, and it beats folder rules.

| Rule | Detail |
|---|---|
| Required column | **`path`** — lower-case. The header is case-sensitive: `Path` or `PATH` matches nothing and **every row is silently skipped**. |
| Tag columns | `facet.<name>` and `acl.<name>`, prefixes also case-sensitive. The name must exist in `facets.yaml` / `access-policy.yaml`. |
| Multiple values | Separated by **`;`** — `Sales;Legal`. A comma would break the CSV. |
| Blank cell | **Inherits** from the layer below (folder rules, source defaults). It does *not* clear a value. To widen access write `*`. |
| Extra columns | Ignored harmlessly — `owner` and `notes` in this template are for humans, not for RAG-OS. |
| Paths | Matched case-insensitively, relative to the source root. Backslashes and a leading `/` are forgiven. |
| Encoding | Auto-detected, so "Save as CSV" from Excel works. |

Numbers are read as text here (`acl.clearance,2` → `"2"`) and coerced back by the access engine. That is fine;
the sidecar below behaves differently.

## `contract.docx.meta.json`

Per-document exceptions, sitting next to the file it describes.

| Rule | Detail |
|---|---|
| **Filename** | The document's **full name including its extension**, plus `.meta.json`. For `msa-fabrikam.docx` that is `msa-fabrikam.docx.meta.json` — *not* `msa-fabrikam.meta.json`. Get this wrong and the file is simply ignored. |
| Keys read | **Only `facets` and `acl`.** Anything else — `title`, `effective_date`, `$schema` — is ignored. |
| Numbers | `"clearance": 2` keeps the integer. `"clearance": [2]` becomes the string `"2"` instead, because the check is on the value, not the list element. Write numeric ACLs bare. |
| Encoding | Strict UTF-8 (a BOM is tolerated), 256 KiB maximum. |
| On error | **Silent.** Malformed JSON, wrong encoding or oversize means the sidecar is treated as absent — no error, no log entry. If a tag does not appear, check the file parses. |

Sidecars are never ingested as documents themselves, and the manifest wins over a sidecar for the same key.
