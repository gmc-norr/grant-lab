# Project review — 2026-10-29

Reviewed revision: `6421f96f`. Application source was left unchanged.

The most urgent issue is profile creation inheriting organization-wide visibility despite the documented private-by-default decision. Other confirmed problems include a reference-deletion check that can miss existing citations, overlapping note saves, blocking database work on the application event loop, and stale source records. The most direct reductions come from removing an obsolete ingestion interface and sharing duplicated runtime and transport code.

## Findings

Priorities: P1 = address first because of unintended disclosure; P2 = correctness, availability, or material repeated work; P3 = development and verification improvements. Reproduction labels distinguish executed probes from source inspection. Findings previously recorded in reviews were independently checked against this revision.

### F1 · P1 · New research profiles are readable by unrelated users

**Location:** `src/grant_lab/profiles/create.py:68`, `src/grant_lab/db/models/profiles.py:142`.

`create_profile_with_bootstrap()` omits `visibility`, so the database supplies `'org'`. The creation API offers no visibility field. Every active user can consequently read the profile, including research content added by bootstrap, until its owner explicitly changes visibility afterward. The private-by-default decision is recorded in `docs/superpowers/specs/2026-09-06-review-fixes-design.md`; the database default was retained for compatibility with existing profiles.

**Reproduced:** Created a profile through the production creation helper in a disposable PostgreSQL database. It had `visibility='org'`, and `can_read_profile()` returned `True` for an unrelated non-admin user with no viewer grant.

**Change:** Set `visibility='private'` on creation. Consider changing the default for future inserts in a migration while preserving existing rows. Verify with an actual creation endpoint test followed by an unrelated user's detail/list requests. This remains open from the October 6 review's M11.

### F2 · P2 · An unrelated broken Git ref can permit deletion of cited reference data

**Location:** `src/grant_lab/proposal_assist/references/deletion.py:203`.

`_branches_cite()` treats every `GitCommitError` or `OSError` from `branch_tips()` as evidence that no branches exist. `branch_tips()` also raises when an actual repository has a broken ref. If the checked-out branch does not cite the reference, this returns “uncited,” and `delete_or_tombstone_reference()` takes the hard-delete path for the database row, chunks, and stored bytes even when an intact sibling branch cites it.

**Reproduced:** Created a real temporary repository with an empty ledger on `main` and a citing ledger on `feature`. `_is_cited()` returned `True`. Added an unrelated invalid `.git/refs/heads/unrelated-broken`; the same check returned `False` while `feature` remained intact.

**Change:** Distinguish a confirmed non-repository from a repository whose refs cannot be read. An uncertain citation check should retain the reference, by refusing deletion or tombstoning. Cover an unrelated broken ref as well as an absent repository. This remains open from October 6 M5.

### F3 · P2 · Overlapping note saves can overwrite the user's newest text

**Location:** `frontend/src/routes/DecisionNoteStrip.tsx:159`, `frontend/src/api/calls.ts:97`, `src/grant_lab/web/api/calls.py:604`.

Enter and blur can start another save while the previous save is pending. `committed.current` advances before acknowledgment, and neither the mutation nor the endpoint orders writes or checks a note revision. The endpoint's `expected_state` protects saved/hidden transitions; two note updates to the same state both succeed. An older request processed after a newer one overwrites the new text. An older failure can also reset the shared committed baseline after a newer save.

**Reproduced:** Rendered the real component with deferred saves. Typed `First`, Enter, then ` Second`, Enter. Two saves were simultaneously pending. Applied the second save and then the first: stored and displayed text ended as `First`, losing ` Second`.

**Change:** Serialize writes per `(profileId, callId)` and keep editable text separate from the acknowledged baseline and pending text. For concurrent tabs, a revision or conditional update should reject stale edits. Add deferred-promise tests with reversed completion order and an older failure after a newer success.

### F4 · P2 · A background evaluator can stall unrelated requests and live editing

**Location:** `src/grant_lab/proposal_assist/evaluator/pass_runner.py:96`, `src/grant_lab/scheduler.py:1183`. Equivalent synchronous transaction blocks exist in the coherence, uncited-claims, and review runners.

The scheduler dispatches these coroutines onto FastAPI's application loop. Their prefixes run synchronous SQLAlchemy sessions, row-lock waits, context assembly, and commits directly on that loop. A row-lock timeout bounds the wait but still blocks every unrelated coroutine until the wait finishes. The configured prefix allowance is two seconds per lock acquisition; the later `asyncio.wait_for()` around the model exchange does not cover this prefix.

**Reproduced:** Held a workspace row in another PostgreSQL session and shortened the prefix lock timeout to 250 ms. An unrelated `asyncio.sleep(0.01)` timer took **268 ms** while `run_evaluator_pass()` waited and raised `PassPrefixLockTimeout`. No adapter was launched.

**Change:** Run each synchronous transaction/context-assembly block in a thread pool, creating and closing its session inside that block. Return plain context values to the coroutine. Apply the same treatment to synchronous finalization. Keep SDK operations and process-local async registries on their owning event loop. Add a heartbeat test under real database contention.

### F5 · P2 · Source corrections can leave closed calls advertised as open

**Location:** `src/grant_lab/sources/eu_portal.py:548`, `src/grant_lab/sources/vinnova.py:298`.

Both adapters discard closed calls before upsert. A previously stored call whose deadline moves from the future into the past therefore keeps its original deadline and content. Ingestion reports success, while the dashboard, digest, and public boards continue using the old future deadline. A withdrawal that removes a call from the source's open-set query has the same stale-record problem; ingestion does not reconcile omissions.

**Reproduced:** Normalized and stored a future EU call. Normalizing the same identifier with a past deadline and changed title returned `None`. Running the resulting empty ingestion retained the original open deadline and title.

**Change:** Allow closed updates to reach existing records, while excluding them from ranking work. Handle withdrawals through explicit source state or reconciliation after a complete, successful source snapshot. A partial or failed fetch must not mark unseen calls closed. This remains open from October 6 M2.

### F6 · P2 · One out-of-range budget rolls back the whole ingestion batch

**Location:** `src/grant_lab/ingestion/upsert.py:43`.

`_as_int()` accepts arbitrary Python integers, while budget columns are PostgreSQL signed `BIGINT`. An upstream budget outside that range reaches SQL and aborts the shared transaction. The valid calls in the same batch are rolled back, and `last_fetched_at` remains unchanged, making the next run encounter the same offending record.

**Reproduced:** Ingested one valid call followed by a call with `budget_max=10**30`. PostgreSQL raised `NumericValueOutOfRange`; the valid call was absent afterward, and the source timestamp remained unset.

**Change:** Range-check the coerced integer and turn invalid budget metadata into `None`, consistent with the existing boundary policy. Test both integer limits and a mixed valid/invalid batch. Row-level rejection can provide additional isolation if other upstream values remain capable of aborting a batch.

### F7 · P2 · Unchanged ingestion runs repeat all embedding work

**Location:** `src/grant_lab/ingestion/orchestrator.py:239`, `src/grant_lab/embeddings/refresh.py:64`.

Every touched call is embedded, even when its vector, model, and text fingerprint are current. The EU adapter deliberately fetches the full open set, so repeated ingestion spends local model/GPU time on unchanged content. Ranking freshness avoids duplicate ranking work but does not stop this embedding work.

**Reproduced:** Three identical ingestion runs made three embedding requests for the same call.

**Change:** Skip embedding when the vector exists, its model matches, and `embedding_fingerprint` matches the current embedding input. The existing daily repair already uses these facts to decide staleness; share that predicate. Preserve forced refresh and model-change behavior. This remains open from October 6 M3.

### F8 · P3 · The connection-leak test gate does not enforce its stated policy

**Location:** `pyproject.toml:262`, `tests/web/test_api_boards_crud.py:39`.

The configuration promotes `ResourceWarning` to errors, but leaked psycopg connections emit it from `__del__`. Pytest reports the resulting unraisable exception as `PytestUnraisableExceptionWarning`, which the current policy permits. The full baseline passed with 69 warnings, including many such leaked-connection reports.

**Reproduced:** Running the board CRUD file with `-Werror::pytest.PytestUnraisableExceptionWarning` produced **36 failed, 8 passed**, with leaked psycopg connections reported in the failures and final cleanup. Its helpers create application engines and unentered clients without teardown that disposes those engines.

**Change:** Close application-owned resources in test fixtures, including engines for clients that intentionally do not enter lifespan, and promote `PytestUnraisableExceptionWarning` to an error. Avoid suppressing the warning: it is exposing missing cleanup. This finding concerns test resource ownership; these results do not establish a production connection leak.

### F9 · P3 · The README database URL does not match Docker Compose

**Location:** `README.md:86`, `docker-compose.yml:14`.

The quickstart uses `grant_lab:grant_lab/.../grant_lab`, while Compose creates user, password, and database `call_manager`. A fresh installation following those instructions cannot authenticate for migrations.

**Verified by source comparison. Change:** Correct the example URL to `postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager`, retaining the documented host-port override. This remains open from October 6 M13.

## Opportunities to simplify and reduce code

These are scoped refactors supported by the current source. Line counts describe the surface involved, not guaranteed savings.

| Order | Opportunity | Concrete reduction and constraints |
|---|---|---|
| 1 | Remove the obsolete `needs_body` adapter interface | Both production adapters now ignore it. Delete the source-wide body-ID query, callback, signature introspection, compatibility dispatch, and unused protocol argument. `ingestion/orchestrator.py:26` and `:122` alone contain about 50 lines for this machinery, plus related imports and adapter declarations. This also removes a database read and an ID set that grows with source history. |
| 2 | Register one handler for workspace-gone exceptions | `main.py:623`, `:655`, and `:678` return the same API and non-API responses for `WorkspaceDeleted`, `WorkspaceRetired`, and `WorkspaceGenerationStale`. Register those distinct types against one handler. Keep their different domain meanings and the separate branch-mismatch handler. Existing response-equivalence tests can verify the refactor. |
| 3 | Share the bounded adapter-exchange lifecycle | Coherence, evaluator, and uncited-claims runners total **1,109 lines**; the review runner adds **583**. Repeated launch/drive timeout handling, usage capture, bounded termination, and cancellation handling are strong candidates for one helper. Keep context building and domain finalization explicit. Address F4 as part of this work so the shared helper does not preserve synchronous database waits on the event loop. |
| 4 | Unify SMTP and Resend implementations | `auth/email.py` and `email/client.py` total **342 lines** and each implement SMTP and Resend delivery. Authentication no longer sends magic links. Keep one transport implementation with a small plain-text notification adapter, preserving the digest client's recipient-bounce classification, TLS checks, and test seams. |
| 5 | Make the profile read predicate express “exclude ownership” directly | `web/api/profiles.py:221` uses a synthetic user with `id=-1`, then separately re-queries the real user's viewer grant. Add an explicit owner-arm option to the shared access predicate. This removes the artificial identity, the second lookup, and a long explanation while keeping one definition of visibility. |
| 6 | Move historical narratives out of executable files | Production Python contains **67,287 physical lines**, including **9,116 comment-only lines** and **10,686 docstring lines**: about **29.4%** combined. Keep short contracts, lock order, and failure semantics beside code; move review chronology and superseded approaches into design notes. For example, `main.py` contains 395 comment/docstring lines, and `row_locks.py` devotes extensive prose to three constants and a shared context manager. This reduces reading cost without changing behavior. |

One further performance opportunity: the match build's main JavaScript chunk is **769.08 kB / 236.96 kB gzip**, and the full build's is **877.31 kB / 270.48 kB gzip**. `router.simple.tsx:6` eagerly imports board, profile-editing, settings, and authentication screens. Measure route-level lazy loading for screens outside the initial triage path. This reduces initial download and parse work; it does not reduce total source size. Preserve the edition import boundary checked by `check_match_bundle.mjs`.

## Validation and limits

| Check executed | Result |
|---|---|
| `env -u ANTHROPIC_API_KEY uv run pytest -n 8 --dist loadfile -q` | **5,842 passed, 1 skipped, 69 warnings**, 284.69 s |
| `uv run ruff check . --output-format concise` | Passed |
| Frontend `vitest run` | **149 files, 1,234 tests passed**; React `act()` warnings in chat expiry tests |
| Frontend `tsc --noEmit` | Passed |
| Frontend `oxlint --deny-warnings src e2e` | Passed |
| Vite production builds for full and match, into temporary output directories | Both passed; large-chunk warnings |
| `check_match_bundle.mjs` against the new match build | Passed; 522 modules, no excluded paths |
| Five isolated backend defect probes for F1, F2, F5, F6, F7 | All reproduced the stated behavior |
| Real-row-contention event-loop probe for F4 | Reproduced a 268 ms delay to an unrelated 10 ms timer |
| Deferred-save frontend probe for F3 | Reproduced older text overwriting newer text |
| Board CRUD tests with strict unraisable-warning handling | Failed as described in F8 |

The defect probes asserted observed faulty behavior; their passing results establish reproduction, not correctness. Temporary probe test files were removed after execution. Databases and repositories used by the probes were disposable, model clients were mocked, and the optional live Anthropic test was skipped. Browser end-to-end tests, live source APIs, hosted-model integration, remote CI status, and the production database were not verified in this review.

Suggested implementation order: F1 and F2; F3 and F4; ingestion fixes F5–F7 together with removal of `needs_body`; then F8/F9 and the remaining simplifications. Keep the existing concurrency and state-consistency tests as behavioral protection during the refactors.
