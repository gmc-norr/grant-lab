# First-Run Value Loop — Design Spec

**Date:** 2026-06-11
**Status:** Draft for implementation — revised after Codex review R1 (see Revision History)
**Audience:** Engineers working on call-manager

## Purpose

The founding pitch of call-manager is *"your group's most relevant funding calls, ranked for you and pushed weekly."* Today a new user following the most natural path gets neither: an unranked firehose on the dashboard and silence on Mondays. The value loop only works for a user who performs several undiscoverable opt-ins and whose profile happens to have been created and re-saved correctly.

This spec closes that gap. It makes the product deliver its core promise **by default**, without touching the ranking model internals that are under active development elsewhere.

### The broken loop, with evidence

1. **Profile creation freezes the browser, then risks losing everything.** `bootstrap_profile()` (PubMed + local LLM) runs synchronously inside the POST handler at [profiles.py:153](../../../src/call_manager/web/routes/profiles.py#L153). On the on-prem 2080 Ti at single-digit tokens/sec this is a multi-minute blank-page wait with real reverse-proxy 504 risk — which discards the user's typed mission text and pasted grants. The team already moved the *cheaper* embedding step to a background task for exactly this reason ([profiles.py:201-205](../../../src/call_manager/web/routes/profiles.py#L201-L205)) but left the slower step blocking.

2. **Creating a profile never triggers ranking.** `backfill_profile` is wired only into the *edit* path ([profiles.py:390-396](../../../src/call_manager/web/routes/profiles.py#L390-L396)); the create path schedules only an embedding refresh ([profiles.py:206-212](../../../src/call_manager/web/routes/profiles.py#L206-L212)). A user who bootstraps a profile and navigates away from the edit page sees "—" score badges until the next-day reconcile tick, with no UI explaining that rankings are pending.

3. **The bootstrap completeness signal is computed but never shown.** `bootstrap_profile` returns `publications_fetched` and `publications_skipped` ([bootstrap.py:86-87](../../../src/call_manager/profiles/bootstrap.py#L86-L87)). No route reads it; a silent or empty bootstrap lands the user on a near-blank edit page with no explanation. (Today's `publications_skipped` is only a rough approximation — see Component 2 / the honesty fix.)

4. **Creating a profile subscribes nobody to the digest.** `POST /profiles/new` writes no `Subscription` row, and new users start with zero subscriptions ([auth.py:146-149](../../../src/call_manager/web/routes/auth.py#L146-L149)). The only path to the Monday digest is a checkbox form buried under /me that nothing prompts the user to visit.

5. **The dashboard never remembers a profile.** `calls_index` defaults to no profile and the unranked feed; the selection is not persisted anywhere ([calls.py:62-67](../../../src/call_manager/web/routes/calls.py#L62-L67)). Every return visit restarts at the firehose.

## Goals

- Profile creation returns immediately and never loses user input, even when PubMed or the local LLM is slow or down.
- A newly created profile produces a ranked feed on its own, with visible progress instead of inexplicable "—" badges — including an honest terminal state when scoring fails.
- The creator is subscribed to their profile's digest, and lands on its ranked feed on return visits.
- The user can see *whether* their bootstrapped draft succeeded and *how complete* it is (PubMed outcome), and is never shown a fabricated completeness claim.
- No ranking can ever be produced from pre-bootstrap or empty-description profile state.

## Non-goals (kept out to keep the plan executable)

- **Anything ranking-internal.** This slice *calls* `backfill_profile` and *reads* `ranking_jobs` counts — both stable interfaces. It does not modify scoring, criteria, gates, the queue, or the golden set. (It does add explicit outcome fields to the `profiles/` bootstrap layer — `bootstrap.py` and the `DraftProfile` dataclass — which are not ranking modules.)
- Login / digest deep-link repair, expired-call hygiene, feed search/save/undoable-Hide, admin operability levers, proposal-assist seams.
- A blanket "you have no subscriptions" onboarding banner. Per the product decision, only the **creator** is auto-subscribed, silently; group members subscribe via /me. (A softer contextual nudge is noted under Future work.)

## Architecture decisions

### A1 — DB-backed job, not BackgroundTasks

Bootstrap touches two slow, fragile externals (PubMed E-utilities + local Ollama). The mechanism is a **database-backed job table drained by a scheduler interval tick**, mirroring [`WorkspaceSetupJob`](../../../src/call_manager/db/models.py#L1021) and its `_phase0_tick` driver ([scheduler.py:342-371](../../../src/call_manager/scheduler.py#L342-L371)). Rejected alternative: FastAPI `BackgroundTasks` (as the current embedding refresh uses) — these die silently on process restart, offer no retry, and expose no durable status to poll. The job table survives restarts, carries a real status to poll, and records the outcome as result data.

### A2 — Single writer during the bootstrap window (edit-blocking)

The create→edit race is real: the edit route replaces the entire `structured` JSON document and unconditionally schedules a backfill ([profiles.py:377](../../../src/call_manager/web/routes/profiles.py#L377), [profiles.py:390-396](../../../src/call_manager/web/routes/profiles.py#L390-L396)), so an overlapping or stale-form save could clobber the worker's writes or persist rankings computed from pre-bootstrap state.

Resolution: **while a non-terminal (`queued`/`running`) bootstrap job exists for a profile, that profile is read-only.** `POST /profiles/{id}/edit` returns `409 Conflict` ("still drafting — try again in a moment") and the edit-form GET renders the progress banner in place of the editable description. The worker is therefore the **sole writer** during the bootstrap window and owns the **single** embed+backfill for that period. Once the job is terminal, normal editing/saving/backfill resume.

This is simpler and more robust than optimistic `updated_at` versioning for this case, and it removes the need for any "non-destructive merge" — the worker writes `description`/`structured` directly because nothing else can write concurrently. The wait is short (one bootstrap run) and bounded (retry/dead-letter guarantees a terminal state).

### A3 — Explicit bootstrap outcome, not exception-driven

`bootstrap_profile` deliberately never throws — it swallows `PubMedError` and the LLM exception set and returns an empty-ish draft ([bootstrap.py:31-88](../../../src/call_manager/profiles/bootstrap.py#L31-L88)). So the worker cannot infer success/failure from an exception. We extend the `DraftProfile` dataclass and `bootstrap_profile` with explicit outcome fields, and the worker branches on them (see Component 1). The earlier rationale that "an empty description hard-fails at the embedding gate" was incorrect — a *missing* cosine surfaces as **UNKNOWN → falls through to the LLM**, not FAIL ([criteria.py:8-11](../../../src/call_manager/ranking/criteria.py#L8-L11)). The real reason to withhold backfill on a failed draft is that there is no usable description, so any rankings would rest on empty profile context and be misleading.

## Components

### 1. Async profile bootstrap (atomic create + outcome-aware worker)

**`POST /profiles/new`** ([profiles.py:67-214](../../../src/call_manager/web/routes/profiles.py#L67-L214)) changes from "run bootstrap, then create" to "create atomically, then enqueue bootstrap." All inserts happen in **one transaction**, mirroring `create_workspace`:

1. Validate inputs (including the new seed caps — see Data model) and run the existing synchronous duplicate-name pre-check.
2. In a single session: `add(Profile)` with the validated user-entered structured fields and an **empty `description`** → `session.flush()` to populate `profile.id` → `add(ProfileBootstrapJob, status='queued', seed=...)` → `add(Subscription)` for the creator → set `user.default_profile_id` (Component 4) → **`commit()` once**. The existing `IntegrityError` guard wraps the commit (the partial unique index is still the source of truth for the duplicate-name race). On any failure the transaction rolls back, so there is **no orphan profile** that would block a retry via duplicate-name validation.
3. Redirect (`303`) to `/profiles/{id}/edit`.

No PubMed or LLM call happens in the request, and the create-path background embedding task is **removed** — the worker now owns embedding (it must run *after* the description exists). The redirect is immediate.

**`bootstrap_profile` / `DraftProfile` changes** (in `profiles/`, not ranking): add explicit outcome fields so the worker can branch:
- `llm_succeeded: bool` — True only when the LLM returned parseable, non-empty draft content.
- `pubmed_status: str` — `'ok'` (≥1 publication), `'empty'` (queried, zero hits), `'unavailable'` (`PubMedError`), or `'not_queried'` (no members supplied). This replaces reliance on the approximate `publications_skipped` for user-facing copy and lets the worker treat a PubMed outage as *degraded success* when the LLM still drafted.

**Bootstrap-job worker** — a new `run_profile_bootstrap_tick` registered on the scheduler as an interval tick alongside the other drains:

1. Claim one `queued` job (set `running`, stamp `started_at`, increment `attempts`).
2. Construct PubMed, Ollama, and embed clients (as the route does today at [profiles.py:150-153](../../../src/call_manager/web/routes/profiles.py#L150-L153)).
3. Run `bootstrap_profile(seed=...)`.
4. Branch on the outcome:
   - **`llm_succeeded == False`** → **retryable failure.** Record `last_error`, return the job to `queued` until `attempts` is exhausted, then mark `failed`. **No** description write, embed, or backfill.
   - **`llm_succeeded == True`** (possibly degraded by `pubmed_status`) → **success.** Write `description` and merge the sanitized LLM namespace suggestions into the profile (direct write is safe — A2 guarantees no concurrent writer). Refresh the embedding, **then** enqueue `backfill_profile` (order matches the edit path's existing invariant — ranking jobs must see the post-bootstrap vector). Mark `done`, stamp `completed_at`, and persist `pubmed_status` + `publications_fetched` as result columns.
5. On terminal completion (`done` or `failed`), **clear the `seed` payload** (drop stored names/ORCIDs/grants — see retention).

**Retry / reclaim:** mirror `WorkspaceSetupJob` — a bounded `attempts` count and a staleness reclaim that returns a `running` job whose process died back to `queued`.

**Edit-route guard (correctness companion to A2/A3):** `POST /profiles/{id}/edit` (a) returns `409` while a non-terminal bootstrap job exists for the profile, and (b) **skips backfill when the submitted description is blank**, so a user who saves an empty form after a failed bootstrap doesn't enqueue rankings against an empty profile. (The edit route otherwise unchanged.)

### 2. Bootstrap progress + honest completeness banner

The edit page gains an HTMX status fragment (`GET /profiles/{id}/bootstrap-status`, owner-or-admin gated) that polls the job (`hx-trigger="load, every 3s"`, self-removing once terminal):

- **queued / running** → "Drafting your profile from PubMed and past grants — this can take a minute on the local model," spinner; the description textarea is hidden/disabled and the form is read-only (A2).
- **done** → reveal the drafted description and show a completeness line driven by the **real** `pubmed_status`, with no fabricated per-member counts:
  - `ok` → "Found N publications for your members."
  - `empty` → "No publications matched your members on PubMed — the draft rests on your mission text and pasted grants."
  - `unavailable` → "PubMed was unavailable, so the draft rests on your mission text and pasted grants. You can edit the description below."
  - `not_queried` → omit the PubMed line.
- **failed** → "We couldn't auto-draft a description (the local model was unavailable). Write one below and save." The form is now fully editable; no input was lost (user fields were persisted at create). Saving a real description drives backfill via the normal edit path.

### 3. Auto-subscribe the creator (silent), within the atomic create

The creator's `Subscription(user_id, profile_id, digest_enabled=True)` is inserted **inside the same create transaction** (Component 1, step 2), not as a separate post-commit write. Idempotent via the `(user_id, profile_id)` PK. The **edit** path never subscribes (create-only), so re-saving never duplicates. Opt-out remains the existing /me control.

### 4. Sticky default profile (per-user), seeded at creation

**Migration:** add `users.default_profile_id` — nullable `BigInteger` FK → `profiles.id`, `ON DELETE SET NULL`.

**Seeded at creation:** within the atomic create transaction, set `user.default_profile_id = new_profile.id` **only if the user has no default yet** (fill-if-null). Rationale for fill-if-null over always-switch: it makes the primary persona's promise true (a group lead creating their one profile lands on its ranked feed next visit) without yanking a multi-profile creator's dashboard to each new group profile. Explicit selection still updates the default thereafter (below).

**`calls_index`** ([calls.py:58-99](../../../src/call_manager/web/routes/calls.py#L58-L99)) resolves the profile three ways, distinguishing an **absent** `profile` query param (`None`) from an **explicitly empty** one (`""`):

- **absent** (bare `/`, return visit) → use `user.default_profile_id` when set and still `is_active`; else the unranked feed.
- **valid id** → use it, and persist as `default_profile_id` **only when it differs** from the stored value.
- **present but empty** ("— No profile —" chosen) → unranked feed, and **clear** the stored default.

An inactive/unknown stored default degrades to the unranked feed, reusing the existing guard at [calls.py:88-99](../../../src/call_manager/web/routes/calls.py#L88-L99).

### 5. Ranking-status strip (polling, with a failed state)

On the **profile-filtered** feed, a status strip reflects the profile's `ranking_jobs`. It **polls itself** (HTMX, every few seconds) rather than implying scores auto-appear:

- **pending/running > 0** → "Ranking N calls for this profile…" (counts only — **no minute-ETA**; per-job latency is unmeasured and varies with how many pairs short-circuit on cheap gates).
- **failed > 0** → also surface it: "M calls couldn't be scored automatically." (No "admin notified" claim — ranking dead-letters do not currently alert anyone; that's a separate backlog item.)
- **queue drained** (no pending/running) → the strip stops polling and either auto-refreshes the feed-list fragment so the new scores show, or, if a full refresh is undesirable, shows a "Scoring complete — reload to see scores" link. Either way it does **not** silently vanish while "—" badges remain.

Feed-row score badges still reflect DB state at render time; the strip's poll is what makes "scores appear" honest (it drives the refresh), and the failed count explains any badges that stay "—".

The counts are `SELECT COUNT(*)` over `ranking_jobs` filtered by `profile_id` and status — reads of an existing table, no schema change.

## Data model

**New table `profile_bootstrap_jobs`** (mirrors `WorkspaceSetupJob`):

```
profile_bootstrap_jobs
  id            uuid pk
  profile_id    FK profiles(id) ON DELETE CASCADE, UNIQUE
  status        text  -- 'queued' | 'running' | 'done' | 'failed'
  attempts      smallint default 0
  last_error    text null
  seed          jsonb null   -- members/orcids, pasted_grants, mission_text, kind, level;
                             -- CLEARED (set null) on terminal completion (retention)
  pubmed_status         text null  -- result: 'ok'|'empty'|'unavailable'|'not_queried'
  publications_fetched  int  null  -- result; populated on 'done'
  queued_at     timestamptz default now()
  started_at    timestamptz null
  completed_at  timestamptz null
  CHECK status IN ('queued','running','done','failed')
```

`UNIQUE(profile_id)` keeps one bootstrap job per profile (re-bootstrap is out of scope; a future re-bootstrap can reset this row). `publications_skipped` is **not** persisted as a result — it is the dishonest approximation; `pubmed_status` replaces it for the banner.

**New column on `users`:**

```
users.default_profile_id   bigint null  FK profiles(id) ON DELETE SET NULL
```

**Seed input caps** (enforced in `POST /profiles/new` before the seed is stored; today only `pasted_grants` is capped at 100 KB):
- `mission_text` ≤ 10 KB.
- members ≤ 50 entries; each ORCID / `name|affiliation` line ≤ 500 chars.

These bound the JSONB payload and protect the serial PubMed worker from a single oversized job ([profiles.py:484](../../../src/call_manager/web/routes/profiles.py#L484) parses members unbounded today).

**Retention:** the `seed` payload (which contains personal data — member names, ORCIDs, pasted grant text) is **cleared on terminal completion** (`done`/`failed`). The job row persists for status/history; the PII does not. (Aligns with the GDPR concern raised in the product-review backlog.)

**Migration numbering:** the in-flight ranking-v2.1 plan reserves **0042–0045** ([ranking plan §Migrations](../plans/2026-06-11-ranking-v2.1-improvements.md)). This feature therefore begins at **0046**, after those land. A **single migration `0046`** adds both `profile_bootstrap_jobs` and `users.default_profile_id` (they ship together; no independent-rollout need). This ordering is a hard cross-plan dependency — call it out when sequencing.

## Data flow

**Happy path:** user submits the new-profile form → one transaction creates the profile (empty description) + queues the bootstrap job + subscribes the creator + sets their default (if null) → redirect to the edit page (sub-second), which is read-only with a "drafting…" banner → the worker tick claims the job, fetches PubMed, drafts via the LLM; on `llm_succeeded` it writes the description, embeds, enqueues the 90-day backfill, marks `done` with `pubmed_status`/`publications_fetched`, and clears the seed → the banner flips to the honest completeness line and the page becomes editable → the ranking-status strip polls and shows "Ranking N calls…", then refreshes the feed when the queue drains → return visits to `/` land on this profile's ranked feed by default.

**Concurrency:** there is no create→edit race because the profile is read-only while the bootstrap job is non-terminal (A2). The worker is the sole writer and runs the only embed+backfill for that window.

**Bootstrap (LLM) failure:** the worker exhausts retries and marks the job `failed`; the profile keeps its empty description but is otherwise valid and — now that the job is terminal — fully editable. The banner explains and invites a manual description. Backfill does **not** run until the user saves a non-empty description through the edit path (which also guards against blank-description backfill). The creator's subscription still stands.

**PubMed outage with a working LLM:** treated as **degraded success** — the LLM drafts from mission text + grants, `pubmed_status='unavailable'`, job `done`, backfill proceeds; the banner says PubMed was unavailable.

## Failure handling

- **LLM/parse failure:** detected via `llm_succeeded=False`; bounded retries; terminal `failed` with `last_error`; surfaced in the banner; no backfill.
- **PubMed outage:** degraded success (above); never blocks or fails the job on its own.
- **Worker process restart mid-job:** a stale `running` job is reclaimed to `queued` by a later tick (mirrors the existing drains).
- **Atomic-create partial failure:** single transaction → rollback → no orphan profile, no dangling job/subscription/default.
- **Duplicate-name race:** unchanged — synchronous pre-check + `uq_profiles_active_name` returning `409`.
- **Stale/inactive `default_profile_id`:** degraded to the unranked feed by the active-profile guard; never errors.
- **Edit during bootstrap:** `409` with a "still drafting" message; no partial write.

## Testing

Following the repo's TDD convention (a test module per behavior; fakes for PubMed/LLM/embeddings):

- **Atomic create:** `POST /profiles/new` performs no PubMed/LLM call in-request; on success the profile, a `queued` bootstrap job, the creator's subscription, and (when the user had none) the default all exist; a forced failure on any insert leaves **none** of them (rollback, no orphan).
- **Seed caps:** oversized `mission_text` / too-many members / overlong lines are rejected at submit.
- **Worker — LLM success:** fills the description, refreshes the embedding **before** enqueuing backfill, enqueues backfill, marks `done`, records `pubmed_status`/`publications_fetched`, and clears the seed.
- **Worker — LLM failure:** marks `failed` with `last_error` after retries, writes **no** description, and enqueues **no** backfill; seed cleared.
- **Worker — PubMed outage:** degraded success — drafts, `pubmed_status='unavailable'`, backfill runs.
- **Edit-blocking:** `POST /profiles/{id}/edit` returns `409` while a non-terminal bootstrap job exists; succeeds once terminal.
- **Blank-description guard:** saving an empty description does not enqueue backfill.
- **Progress fragment:** running / done (correct wording per each `pubmed_status`) / failed states; owner-or-admin gated.
- **Auto-subscribe:** exactly one subscription on create; no duplicate on re-create/edit.
- **Sticky default:** migration adds the column; default seeded only when null at creation; bare `/` uses it; a differing selection updates it (same selection issues no write); explicit empty clears it; inactive default degrades to unranked.
- **Status strip:** shows pending and failed counts; does not vanish while failures remain; absent on the unranked feed.

## Migration & rollout notes

- The bootstrap tick is subject to the same single-instance constraint as every other scheduler tick.
- Backward compatible: existing profiles have no bootstrap job and `default_profile_id` defaults `NULL` (unranked landing, as today) until first selection.
- No change to ranking scoring, the queue, or digest assembly.
- **Sequencing dependency:** ships on migration `0046`, after ranking-v2.1's `0042–0045`.

## Future work (explicitly deferred)

- A contextual "you're viewing Profile X — get it weekly by email?" prompt for non-creator group members.
- "Re-pull PubMed / re-bootstrap" + a staleness nudge (the job's `UNIQUE(profile_id)` and result columns make this a natural extension).
- A coarse, measured ETA on the status strip once per-job latency is instrumented.
- Honest per-member PubMed reporting (requires changing the batched PubMed fetch to retain per-member hit counts) — out of scope; `pubmed_status` is the interim honest signal.

## Revision History

**R1 (2026-06-11) — incorporated Codex spec review.** All findings verified against the code and accepted:
- *Bootstrap failure undetectable / wrong embedding rationale (High):* added explicit `llm_succeeded` + `pubmed_status` outcomes (A3, Component 1); corrected the rationale (missing cosine = UNKNOWN, not FAIL); added the edit-route blank-description backfill guard.
- *Create→edit race (High):* replaced non-destructive merge with **edit-blocking while bootstrap is active** (A2) — single writer, single backfill.
- *Atomicity (High):* profile + job + subscription + default now insert in **one transaction** with `flush()` for the id, mirroring `create_workspace` (Component 1).
- *Default not set on happy path (High):* seed `default_profile_id` at creation, **fill-if-null** (Component 4).
- *Completeness not honest (Medium):* `pubmed_status` drives the banner; dropped the fabricated per-member claim and the approximate `publications_skipped` (Component 2 / Data model).
- *Seed size & retention (Medium):* added caps on `mission_text`/members/line length; **clear the seed on terminal completion** (Data model).
- *Migration numbering (Medium):* moved to **0046** (single migration) after ranking-v2.1's 0042–0045.
- *Status strip silent on failure (Medium):* strip **polls**, surfaces a **failed** count, and refreshes the feed on drain instead of vanishing (Component 5).
- *Judgment calls:* kept `UNIQUE(profile_id)`; confirmed wait-to-backfill on genuine failure with the corrected rationale; single migration after the ranking migrations.
