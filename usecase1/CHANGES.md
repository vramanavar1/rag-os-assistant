# Use Case 1: change ledger

Use Case 1 (the mortgage loss-mitigation document chase) is an MVP. It must be removable without trace, so
every artifact it adds is listed here. **Each phase updates this file in the same change that adds the
artifact.**

Brief: [docs/use-case-1.md](../docs/use-case-1.md) · Design: [docs/UseCase1.html](../docs/UseCase1.html) ·
Naming contract and how to run the checks: [README.md](README.md)

Phases: **1** artifacts · **2** engine code · **3** Admin workbench · **4** citations · **5** letter + verifier ·
**6** pilot + removal rehearsal.

## Artifacts

| Artifact | Phase | Status | Remove with |
|---|---|---|---|
| `docs/UseCase1.html` (design for review) | design | **added** | delete the file |
| `docs/UseCase1_DetailedImplementation.html` (implementation runbook) | design | **added** | delete the file |
| `usecase1/CHANGES.md` (this ledger), `usecase1/README.md` (naming contract) | 1 | **added** | delete `usecase1/` |
| `usecase1/checks/` (verification scripts; `run_all.py`) | 1 | **added** | delete `usecase1/` |
| `usecase1/taxonomy/uc1-taxonomy.seed.skos.ttl` (seed, loaded once into VocBench) | 1 | **added** | delete `usecase1/` |
| `usecase1/taxonomy/uc1-taxonomy.skos.ttl` (VocBench export: the taxonomy's source of truth) | 1 | planned | delete `usecase1/` |
| VocBench project `uc1-loss-mitigation` | 1 | planned | delete the project in VocBench |
| `usecase1/ontology/*.ttl` + `catalog-v001.xml` (core, one requirements file per guide version, the SHACL template, data-quality shapes, Protégé catalog) | 1 | **added** | delete `usecase1/` |
| `usecase1/golden/` (11 golden application graphs + expected missing lists per version) | 1 | **added** | delete `usecase1/` |
| `usecase1/sql/uc1_create.sql`, `uc1_seed.sql`, `uc1_drop.sql` | 1 | **added** | delete `usecase1/` |
| `uc1_*` tables in their own SQLite file `.data/uc1.db` (git-ignored; **not** the product state DB) | 1 | **added** | delete the file |
| `uc1_*` tables in PostgreSQL schema `uc1` (local Docker `ragos` database) | 1 | planned | `DROP SCHEMA uc1 CASCADE;` |
| Local tool installs: VocBench 3 (+ `C:\Tools\SemanticTurkeyData`), Protégé, Temurin JDK 21 (outside the repo) | 1 | **installed** | delete the VocBench, SemanticTurkeyData and Protégé folders; `winget uninstall EclipseAdoptium.Temurin.21.JDK` if nothing else needs Java |
| Other local tools used by the runbook: uv, Docker Desktop (already used by RAG-OS), ROBOT (`C:\Tools\robot`, optional), Gpg4win (optional, signature checks) | 0–1 | as installed | uninstall only if nothing else uses them |
| `usecase1/checks/mutation_tests.py` (in-memory mutation tests) | 1 | **added** | delete `usecase1/` |
| `usecase1/taxonomy/taxonomy.yaml` (generated from the VocBench export) | 2 | planned | delete `usecase1/` |
| `usecase1/` engine package and CLI | 2 | planned | delete `usecase1/` |
| Optional extra `[usecase1]` (`rdflib`, `pyshacl`) in `pyproject.toml` (**product file**) | 2 | planned | remove the extra; re-lock |
| `tests/usecase1/` (marker `uc1`) | 2–5 | planned | delete the folder; remove the marker from `pyproject.toml` |
| Admin workbench API: router in `usecase1/api/`, behind `UC1_ENABLED` | 3 | planned | delete `usecase1/` |
| Admin workbench product touches (**product files**, ~11, exact lines listed when added): router hook in `src/rag_os/api/app.py`, `uc1_enabled` in `settings.py` + `Deployment.md`, public-config flag (`health.py`, `schemas.py`, `chat-ui/src/types.ts`, `auth.ts`), Admin tab `chat-ui/src/admin/uc1.ts` + `admin.ts` ROUTES, Dockerfile (`COPY usecase1`, `--extra usecase1`) | 3 | planned | revert those lines; delete `chat-ui/src/admin/uc1.ts` |
| Synthetic guide documents ingested into RAG-OS | 4 | planned | Admin › Documents › Delete permanently |

Product code (`src/rag_os`, `chat-ui`, `config/`, Alembic migrations, `pyproject.toml`) is **not** modified in
phase 1. Later phases that need to change a product file list it above with its path.

## Removal, in order

1. Delete the synthetic guide documents in RAG-OS (Admin › Documents; filter by `doc_type = Guideline`, investor "Investor A").
2. Revert the Admin hook and menu link (phase 3 lists the exact lines).
3. Delete `.data/uc1.db`; in PostgreSQL run `DROP SCHEMA uc1 CASCADE;`.
4. Remove the `[usecase1]` extra and the `uc1` pytest marker from `pyproject.toml`, if present; re-lock.
5. Delete `tests/usecase1/`, `usecase1/`, `docs/UseCase1.html` and `docs/UseCase1_DetailedImplementation.html`.
6. Delete the VocBench project `uc1-loss-mitigation`; remove the local VocBench, Protégé and JDK installs if no longer needed.
7. Run `git status`. The only remaining Use Case 1 file should be the brief, `docs/use-case-1.md` (keep or delete it).
