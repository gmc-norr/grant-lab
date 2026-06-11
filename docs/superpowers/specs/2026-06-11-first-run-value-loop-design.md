# First-Run Value Loop — Design Spec

**Date:** 2026-06-11
**Status:** Draft for implementation
**Audience:** Engineers working on call-manager

## Purpose

The founding pitch of call-manager is *"your group's most relevant funding calls, ranked for you and pushed weekly."* Today a new user following the most natural path gets neither: an unranked firehose on the dashboard and silence on Mondays. The value loop only works for a user who performs several undiscoverable opt-ins and whose profile happens to have been created and re-saved correctly.

This spec closes that gap. It makes the product deliver its core promise **by default**, without touching the ranking model internals that are under active development elsewhere.

### The broken loop, with evidence

1. **Profile creation freezes the browser, then risks losing everything.** `bootstrap_profile()` (PubMed + local LLM) runs synchronously inside the POST handler at [profiles.py:153](../../../src/call_manager/web/routes/profiles.py#L153). On the on-prem 2080 Ti at single-digit tokens/sec this is a multi-minute blank-page wait with real reverse-proxy 504 risk — which discards the user's typed mission text and pasted grants. The team already moved the *cheaper* embedding step to a background task for exactly this reason ([profiles.py:201-205](../../../src/call_manager/web/routes/profiles.py#L201-L205)) but left the slower step blocking.

2. **Creating a profile never triggers ranking.** `backfill_profile` is wired only into the *edit* path ([profiles.py:390-396](../../../src/call_manager/web/routes/profiles.py#L390-L396)); the create path schedules only an embedding refresh ([profiles.py:206-212](../../../src/call_manager/web/routes/profiles.py#L206-L212)). A user who bootstraps a profile and navigates away from the edit page sees "—" score badges until the next-day reconcile tick, with no UI explaining that rankings are pending.

3. **The bootstrap completeness signal is computed but never shown.** `bootstrap_profile` returns `publications_fetched` and `publications_skipped` ([bootstrap.py:86-87](../../../src/call_manager/profiles/bootstrap.py#L86-L87)), explicitly intended as a "how complete is this draft" signal. No route reads it; a silent or empty bootstrap lands the user on a near-blank edit page with no explanation.

4. **Creating a profile subscribes nobody to the digest.** `POST /profiles/new` writes no `Subscription` row, and new users start with zero subscriptions ([auth.py:146-149](../../../src/call_manager/web/routes/auth.py#L146-L149)). The only path to the Monday digest is a checkbox form buried under /me that nothing prompts the user to visit. The group lead's most likely flow — create the profile, wait for Monday — yields no email.

5. **The dashboard never remembers a profile.** `calls_index` defaults to no profile and the unranked feed; the selection is not persisted anywhere ([calls.py:62-67](../../../src/call_manager/web/routes/calls.py#L62-L67)). Every return visit restarts at the firehose, so the ranked view — the product's actual value — must be re-selected from a dropdown every time.

## Goals

- Profile creation returns immediately and never loses user input, even when PubMed or the local LLM is slow or down.
- A newly created profile produces a ranked feed on its own, with visible progress instead of inexplicable "—" badges.
- The creator is subscribed to their profile's digest automatically.
- The dashboard defaults to the user's profile (ranked view) on return visits.
- The user can see *why* a freshly created profile isn't fully scored yet (ranking in progress) and *how complete* their bootstrapped draft is.

## Non-goals (kept out to keep the plan executable)

- **Anything ranking-internal.** This slice *calls* `backfill_profile` and *reads* `ranking_jobs` counts — both stable interfaces. It does not modify scoring, criteria, gates, the queue, or the golden set. This is the explicit boundary with the in-flight ranking-model work.
- Login / digest deep-link repair (the magic-link scanner and next-URL issues).
- Expired-call hygiene, feed search, save/star, undoable Hide.
- Admin operability levers and health visibility.
- Proposal-assist workspace seams.
- A blanket "you have no subscriptions" onboarding banner. Per the product decision, only the **creator** is auto-subscribed, silently; group members who did not create the profile still subscribe via /me. (A softer, contextual nudge is noted under Future work but is not in scope.)

## Architecture decision: DB-backed job vs. BackgroundTasks

Bootstrap touches two slow, fragile externals (PubMed E-utilities + local Ollama). The chosen mechanism is a **database-backed job table drained by a scheduler interval tick**, mirroring the existing [`WorkspaceSetupJob`](../../../src/call_manager/db/models.py#L1021) pattern and its `_phase0_tick` driver ([scheduler.py:342-371](../../../src/call_manager/scheduler.py#L342-L371)).

Rejected alternative: continuing to use FastAPI `BackgroundTasks` (as the current embedding refresh does). `BackgroundTasks` die silently if the process restarts mid-run, offer no retry, and expose no durable status to poll. The job table survives restarts, carries a real status the progress page can poll, records the completeness counts as result data, and reuses infrastructure already in place. The extra code (one model + migration + worker + tick) is modest and the robustness directly serves the goal of a trustworthy first run.

## Components

### 1. Async profile bootstrap

**`POST /profiles/new`** ([profiles.py:67-214](../../../src/call_manager/web/routes/profiles.py#L67-L214)) changes from "run bootstrap, then create" to "create, then enqueue bootstrap":

1. Validate inputs and run the existing synchronous duplicate-name check (fail fast on the obvious conflict — keep the pre-check at [profiles.py:134-139](../../../src/call_manager/web/routes/profiles.py#L134-L139) and the `IntegrityError` race guard).
2. Create the `Profile` row **immediately** with the validated user-entered structured fields and an **empty `description`**. The LLM-suggested namespaces are *not* merged here (they don't exist yet).
3. Insert a `profile_bootstrap_jobs` row (status `queued`) carrying the seed payload (members/ORCIDs, pasted grants, mission text, kind, level) needed to run bootstrap later.
4. Insert the creator's `Subscription` row (see Component 3).
5. Redirect (`303`) to `/profiles/{id}/edit`.

No PubMed or LLM call happens in the request. The redirect is immediate.

**Bootstrap-job worker** — a new function (e.g. `run_profile_bootstrap_tick`) registered on the scheduler as an interval tick alongside the other drains:

1. Claim one `queued` job (set `running`, stamp `started_at`, increment `attempts`).
2. Construct the PubMed client, Ollama client, and embed client (as the route does today at [profiles.py:150-153](../../../src/call_manager/web/routes/profiles.py#L150-L153)).
3. Run `bootstrap_profile(seed=...)`.
4. **Non-destructive merge** into the existing profile (see Data flow → create-then-edit race for why this matters):
   - Set `description` **only if** the profile's description is still empty (the user hasn't written one on the edit page in the meantime).
   - Apply the sanitized LLM namespace suggestions (`eligibility`/`org`/`funding`/`geo`) and the `areas` fallback **only where the corresponding profile field is still empty**. Existing non-empty values — whether user-entered at create or edited since — always win.
5. Refresh the profile embedding, **then** enqueue `backfill_profile`. Order matters and matches the edit path's existing invariant ([profiles.py:421-434](../../../src/call_manager/web/routes/profiles.py#L421-L434)): ranking jobs must see the post-bootstrap vector.
6. Mark the job `done`, stamp `completed_at`, and persist `publications_fetched` / `publications_skipped` as result columns.

**Retry / dead-letter:** on exception, record `last_error`, leave `running`→`queued` for a bounded number of `attempts` (mirror `WorkspaceSetupJob`'s retry shape), then mark `failed`. A `failed` job does **not** enqueue backfill — there is no good description to rank against yet; the user's manual save on the edit page drives backfill once they write one.

### 2. Bootstrap progress + completeness banner

The edit page ([profile_edit.html](../../../src/call_manager/web/templates/profile_edit.html)) gains an HTMX status fragment that polls the bootstrap job (`hx-trigger="load, every 3s"`, self-removing once terminal):

- **queued / running** → a "Drafting your profile from PubMed and past grants — this can take a minute on the local model" banner with a spinner; the description textarea is hidden or disabled so the user doesn't type into a field the worker may fill.
- **done** → reveal the drafted description and show the completeness summary built from the result columns, e.g. *"14 publications found. PubMed returned nothing for 2 members. Your mission text was used."* Wording adapts to the counts (e.g. "No publications found — draft rests on your mission text and pasted grants").
- **failed** → "We couldn't auto-draft a description (PubMed or the local model was unavailable). Write one below and save." The form is fully usable; no input was lost because the user's fields were persisted at create time.

A new lightweight route (e.g. `GET /profiles/{id}/bootstrap-status`) returns the fragment. It is owner-or-admin gated like the edit page.

### 3. Auto-subscribe the creator (silent)

In `POST /profiles/new`, immediately after the `Profile` row commits, insert `Subscription(user_id=creator.id, profile_id=new_id, digest_enabled=True)`. Idempotent via the existing `(user_id, profile_id)` primary key (`ON CONFLICT DO NOTHING` / catch `IntegrityError`). This is independent of bootstrap success — the user is subscribed even while the description is still drafting. Opt-out remains the existing /me control. The **edit** path does not subscribe (create-only), so re-saving never produces duplicates.

### 4. Sticky default profile (per-user)

**Migration:** add `users.default_profile_id` — nullable `BigInteger` FK → `profiles.id`, `ON DELETE SET NULL`. (Hard deletes clear it; soft-deleted/inactive profiles are handled by the active-profile guard below.)

**`calls_index`** ([calls.py:58-99](../../../src/call_manager/web/routes/calls.py#L58-L99)) resolves the profile three ways, distinguishing an **absent** `profile` query param (`None`) from an **explicitly empty** one (`""`):

- **absent** (e.g. bare `/`, or a return visit) → use `user.default_profile_id` when it is set and still points at an `is_active` profile; otherwise the unranked feed.
- **valid id** → use it, and persist it as the user's `default_profile_id` **only when it differs** from the stored value (avoids a write on every page load).
- **present but empty** (the user explicitly chose "— No profile —") → unranked feed, and **clear** the stored default so the firehose isn't overridden back to a profile on the next visit.

An inactive or unknown stored default degrades to the unranked feed, reusing the existing validation at [calls.py:88-99](../../../src/call_manager/web/routes/calls.py#L88-L99). Digest emails already deep-link with `?profile=`, so this change primarily fixes the bare-`/` return-visit landing.

### 5. Ranking-status strip

On the **profile-filtered** feed, when one or more `ranking_jobs` for the selected profile are in `pending`/`running` state, render an honest status strip above the list:

> *"Ranking N calls for this profile — new scores appear as they finish."*

Counts only. **No fabricated minute-ETA** — per-job latency on the local GPU is unmeasured and varies with how many pairs short-circuit on cheap gates before reaching the LLM, so a time estimate would routinely be wrong. The strip disappears once no pending/running jobs remain for that profile. This is what converts a wall of "—" badges the morning after creation from "the tool is broken" into "scores are still coming."

The count is a `SELECT COUNT(*)` over `ranking_jobs` filtered by `profile_id` and status — a read of an existing table, no schema change.

## Data model

**New table `profile_bootstrap_jobs`** (mirrors `WorkspaceSetupJob`):

```
profile_bootstrap_jobs
  id            uuid pk
  profile_id    FK profiles(id) ON DELETE CASCADE, UNIQUE
  status        text  -- 'queued' | 'running' | 'done' | 'failed'
  attempts      smallint default 0
  last_error    text null
  seed          jsonb        -- members/orcids, pasted_grants, mission_text, kind, level
  publications_fetched  int null   -- result; populated on 'done'
  publications_skipped  int null   -- result; populated on 'done'
  queued_at     timestamptz default now()
  started_at    timestamptz null
  completed_at  timestamptz null
  CHECK status IN ('queued','running','done','failed')
```

`UNIQUE(profile_id)` keeps one bootstrap job per profile (bootstrap runs once at creation; re-bootstrap is out of scope). `seed` holds the bootstrap inputs (pasted grants are capped at 100 KB by the existing route validation, comfortably within JSONB).

**New column on `users`:**

```
users.default_profile_id   bigint null  FK profiles(id) ON DELETE SET NULL
```

Two logical Alembic migrations (next numbers after 0041), or one — the implementation plan decides.

## Data flow

**Happy path:** user submits new-profile form → row created with their fields + empty description, creator subscribed, bootstrap job queued, redirect to edit page (sub-second) → edit page shows "drafting…" banner → worker tick (within the tick interval) fetches PubMed, drafts description, merges non-destructively, embeds, enqueues 90-day backfill, marks job done with counts → banner flips to the completeness summary and reveals the description → ranking-status strip on the feed shows "Ranking N calls…" and drains over the next minutes/hours → return visits land on this profile's ranked feed by default.

**Create-then-edit race:** the profile is editable the instant it exists, before the worker runs. If the user writes a description or fills a namespace on the edit page and saves before the worker finishes, the worker's **non-destructive merge** (Component 1, step 4) guarantees their values are never overwritten — the worker only fills fields that are still empty. The progress banner hiding/disabling the description field while `running` makes this race rare in practice; the merge rule makes it *safe* regardless.

**Bootstrap failure:** job ends `failed`, profile has an empty description but is otherwise valid and fully editable; the banner explains and invites a manual description. Backfill does not fire until the user saves a real description through the existing edit path. The creator's subscription still stands (so once the profile is usable, Monday's digest works).

## Failure handling

- **PubMed or LLM unavailable:** caught in the worker; bounded retries via `attempts`; terminal state `failed` with `last_error`; surfaced in the edit-page banner. The user is never blocked and never loses input.
- **Worker process restart mid-job:** a `running` job whose process died is reclaimed by a future tick (reset `running`→`queued` past a staleness threshold, as the existing job drains do). The DB-backed design is what makes this recoverable.
- **Duplicate-name race:** unchanged — synchronous pre-check plus the `uq_profiles_active_name` partial unique index returning `409`.
- **Subscription insert race / pre-existing subscription:** idempotent insert, no error surfaced.
- **Stale/inactive `default_profile_id`:** degraded to the unranked feed by the active-profile guard; never errors.

## Testing

Following the repository's TDD convention (a test module per behavior, fakes for PubMed/LLM/embeddings):

- **Create path:** `POST /profiles/new` returns a redirect without invoking PubMed/LLM in-request; the profile exists immediately with the user's fields and an empty description; a `queued` bootstrap job exists; the creator has a `Subscription`.
- **Worker success:** running the tick against a fake PubMed + fake LLM fills the description, merges suggested namespaces non-destructively, refreshes the embedding before enqueuing backfill, enqueues backfill, marks the job `done`, and records the publication counts.
- **Non-destructive merge:** a profile whose description/namespace was edited between create and worker-run is **not** overwritten by the worker.
- **Worker failure:** a raising PubMed/LLM leaves the profile usable, marks the job `failed` with `last_error`, and does **not** enqueue backfill.
- **Progress fragment:** returns the running, done (with the right completeness wording for fetched/skipped counts), and failed states; owner-or-admin gated.
- **Auto-subscribe:** create inserts exactly one subscription; a second create-or-edit does not duplicate; an already-subscribed creator is unaffected.
- **Sticky default:** migration adds the column; selecting a valid profile persists it; selecting the same profile again issues no redundant write; bare `/` uses the stored default; explicit empty `profile=` clears it; an inactive stored default degrades to unranked.
- **Status strip:** appears with the correct count when pending/running jobs exist for the selected profile; absent when the profile's queue is empty and on the unranked feed.

## Migration & rollout notes

- Adding the bootstrap tick to the scheduler is subject to the same single-instance constraint as every other tick (already a hard deploy constraint).
- The change is backward compatible: existing profiles have no bootstrap job (their description already exists) and `default_profile_id` defaults `NULL` (unranked landing, as today) until the user next selects a profile.
- No change to the ranking queue, scoring, or digest assembly logic.

## Future work (explicitly deferred)

- A contextual "you're viewing Profile X — get it weekly by email?" prompt shown only when a user views a profile they aren't subscribed to (softer than a blanket onboarding banner; covers the group-member-who-didn't-create case).
- "Re-pull PubMed / re-bootstrap" action and a profile staleness nudge (the bootstrap job's `UNIQUE(profile_id)` and result columns make this a natural extension).
- A coarse, measured ETA on the ranking-status strip once per-job latency is instrumented.
