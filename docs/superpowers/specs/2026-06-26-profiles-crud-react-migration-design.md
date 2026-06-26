# Profiles CRUD React Migration (Design Spec)

**Date:** 2026-06-26
**Status:** Design approved; pending spec-review gate → writing-plans.
**Project:** Full-React migration → retire the remaining Jinja pages, one surface at a time.
**Builds on:** account/settings merged (`f80003a`); calls-detail merged (`4d7c93a`); the `workspace_main` Jinja teardown is complete (cockpit is the sole workspace surface).

---

## 1. Why this slice exists

The ranking-profiles surface (`/profiles*`) is still Jinja, served by `src/call_manager/web/routes/profiles.py`
(511 lines) + four templates + one partial:

- `GET /profiles` → `profiles_index.html` (active profiles, by name)
- `GET /profiles/new` → `profile_new.html` (create form, ~17 fields, kind-conditional)
- `POST /profiles/new` → `create_profile_with_bootstrap` enqueues a `ProfileBootstrapJob` (the local LLM drafts
  a description + rubric from PubMed + pasted grants), then 303 → `/profiles/{id}/edit`
- `GET /profiles/{id}` → `profile_detail.html` (read view: description, rubric, grouped namespaced fields)
- `GET /profiles/{id}/edit` → `profile_edit.html` (edit form, ~25 fields, kind-conditional, structured-namespace
  round-trip) + `partials/bootstrap_banner.html` (HTMX polls every 3s, `HX-Refresh` when the job is terminal)
- `GET /profiles/{id}/bootstrap-status` → HTMX poll endpoint (owner-or-admin)
- `POST /profiles/{id}/edit` → owner-or-admin save (409 while drafting), re-embed + 90-day backfill side effects

The SPA shell's "Profiles" nav item (`shell/nav-items.ts`) is a `kind:"external"` `<a>` that full-page-navigates
to that Jinja list.

Same strangler-fig pattern as the prior slices: **additive typed `/api` → React routes under the existing
AppShell → per-slice Jinja teardown.** No DB schema change. **Strict parity port** (form-UX improvements — e.g.
tag/chip inputs instead of newline textareas — are explicitly deferred to a later follow-up).

This is the **profiles-CRUD sub-project**, the second half of the original profile/account decomposition (the
account/settings half merged at `f80003a`). It is the **largest slice so far** (~1.5× account): four pages, an
async LLM-bootstrap job, and the structured-namespace form round-trip.

### One atomic slice (decomposition decision)

The route-manifest mechanism forces the nav-flip + Jinja teardown to be **atomic across all four pages**:
`/profiles` is owned as a **prefix**, so the moment it flips to SPA the dev proxy serves the React app for
`/profiles/new` and `/profiles/{id}/edit` too (`shouldServeViaVite` keys off `isSpaRoute` alone — it is greedy
on the prefix). A partial flip would need a manifest/dev-proxy mechanism change of low value. So the cutover is
one step regardless; the whole surface migrates in **one branch / one merge**, with a larger multi-task plan.

---

## 2. Goal & invariant

**Goal:** React `/profiles`, `/profiles/new`, `/profiles/:id`, `/profiles/:id/edit` at parity with the Jinja
pages (list, create-with-LLM-bootstrap, detail, edit), driven by typed `/api/profiles/*` endpoints.

**Invariant after this slice:**
1. The four `/profiles*` pages are React routes under the AppShell; the shell's Profiles nav item is a
   React-Router `NavLink` (`kind:"spa"`), no full reload.
2. Pages render from `GET /api/profiles` + `GET /api/profiles/{id}`; create/edit persist via `POST /api/profiles`
   + `PUT /api/profiles/{id}` that mirror the Jinja handlers exactly (validation codes, owner-or-admin gating,
   the LLM-bootstrap enqueue, the re-embed + backfill side effects).
3. The bootstrap "drafting" UX is preserved: after create, the user lands on the edit page in a disabled
   "drafting" state that polls and live-fills when the job completes.
4. The Jinja `web/routes/profiles.py` module, its four templates + the bootstrap partial, and the `main.py`
   registration are **deleted**; `/profiles` drops from `backend_prefixes` and is added to `spa_routes` as a
   **prefix**. No DB schema change.

---

## 3. Architecture

### 3.1 Backend — additive `/api/profiles/*` (new `web/api/profiles.py`)

Four endpoints on the `/api` router; all errors via `raise HTTPException(status_code=…, detail=…)` → the global
handler maps `/api` paths to the envelope `{"error":{"code","message"}}`. Response models namespaced
`Profiles*`/`Profile*` (verify no collisions in `schema.gen.ts` during the plan — `Profile` is a DB model;
existing Pydantic names elsewhere may clash).

**`GET /api/profiles` → `ProfilesListOut {profiles: [ProfileListItemOut]}`** — active profiles ordered by name
(any authenticated user). `ProfileListItemOut {id, name, kind, level: str|None, areas_count: int,
budget_currency: str|None, description: str}` (the React card truncates `description` to 160 chars, matching the
Jinja `[:160]`).

**`GET /api/profiles/{id}` → `ProfileDetailOut`** — the **consolidated read-model serving the detail view, the
edit form, AND the bootstrap poll**. A **flat, typed projection** of the profile: the `structured.{org,
eligibility, funding, geo}` namespaces are unwrapped into explicit typed fields so both the detail display and
the edit-form inputs bind to the same shape (and TS gets real types, not a raw-JSONB blob). 404 if the profile
is missing or `is_active is False` (parity — archived profiles 404 everywhere). Fields:
- core: `id, name, kind, level: str|None, description, budget_currency: str|None`
- rubric: `areas: list[str], budget_min: int|None, budget_max: int|None, trl_min: int|None, trl_max: int|None,
  must_include: list[str], hard_exclusions: list[str]`
- namespaced → flat: `org_type: str|None, capabilities: list[str], patient_populations: list[str],
  scale: str|None` (org); `career_stage: str|None, role: str|None, years_since_phd: int|None,
  applicant_types: list[str]` (eligibility); `funding_instruments: list[str], funder_exclusions: list[str]`
  (funding); `country: str|None, host_institution: str|None` (geo)
- state: `can_edit: bool` (`created_by == user.id or user.is_admin`), `bootstrap_status:
  "queued"|"running"|"done"|"failed"|None`, `bootstrap_pubmed_status: str|None`,
  `publications_fetched: int|None`

**`POST /api/profiles` → `ProfileCreateOut {id: int}`** — body `ProfileCreateIn` carrying the create-form fields
(`name, kind, level, orcids, names_affiliations, pasted_grants, mission_text` + the structured flat fields).
Validation mirrors the Jinja handler **in order**: `name` ≤200 (400); `pasted_grants` ≤100KB / `mission_text`
≤10KB (413); unknown `kind` → silently default `organizational_unit`; unknown `level` for an org unit (400);
`parse_structured` (`FieldValidationError` → its `status_code`/`message`); `_parse_members_strict` bounds
(>50 members / line >500 → 400); early duplicate-active-name check (409) + `IntegrityError` fallback (409).
Reuses `create_profile_with_bootstrap` (which inserts the profile, enqueues the `ProfileBootstrapJob`,
auto-subscribes the creator, and sets their default profile if unset — all in the caller's committed
transaction). The React client then navigates to `/profiles/{id}/edit`.

**`PUT /api/profiles/{id}` → `ProfileUpdateOut {id: int}`** — body `ProfileUpdateIn` carrying the edit-form flat
fields (`description, areas, budget_min, budget_max, budget_currency, trl_min, trl_max, must_include,
hard_exclusions, level` + the structured flat fields). 404 missing/inactive; **403 if not owner-or-admin**
(`created_by != user.id and not user.is_admin`); **409 while a bootstrap job is `queued`/`running`**;
`description` ≤50KB (413); budget out of `[0, 10**12]` / TRL out of `[1, 9]` (400); unknown currency (400);
unknown level for an org unit (400); `parse_structured` (400). Re-namespaces the flat fields via
`parse_structured` (`base` rubric keys + the `eligibility/org/funding/geo` namespaces, `base` wins on `areas`),
applies the same `build_profile_text`-based re-embed change detection, and schedules the identical
`_refresh_and_backfill_in_background` task (re-embed first when the embed text changed, then 90-day backfill).
The React client then navigates to `/profiles/{id}`.

**Two deliberate, flagged consolidations (NOT silent behavior changes):**
1. The separate `GET /profiles/{id}/bootstrap-status` poll endpoint **does not get an `/api` twin**. React polls
   `GET /api/profiles/{id}` via TanStack Query `refetchInterval` while `bootstrap_status ∈ {queued, running}`,
   stopping on `done`/`failed`. Consequence: `bootstrap_status` becomes visible to any authenticated viewer
   (the Jinja poll was owner-or-admin gated). The status is non-sensitive ("drafting"/"done"); the **save** path
   (`PUT`) stays strictly owner-or-admin. Documented here so the review knows it is intentional.
2. **Helper relocation (structural).** `_parse_members`/`_parse_members_strict`, `_bounded_int`, `_split_lines`,
   and the bound constants (`_BUDGET_MIN/MAX`, `_TRL_MIN/MAX`, `_ALLOWED_CURRENCIES`, `_MAX_MEMBERS`,
   `_MAX_SEED_LINE`, `_MAX_LIST_ENTRIES`) currently live **inside the doomed** `web/routes/profiles.py`. They
   relocate to a neutral home in the `profiles/` package (`profiles/forms.py`) early; the still-live Jinja module
   is repointed at it during the additive phase, then deleted in teardown — so nothing kept imports a doomed
   module (the calls-detail relocate / G1-sever pattern). The re-embed/backfill background driver
   (`_refresh_and_backfill_in_background` + its `_refresh_embedding_in_background` helper) ALSO currently lives in
   the doomed Jinja module and the kept `PUT` needs it, so it **must relocate too** (to `profiles/forms.py` or a
   sibling neutral module). `create_profile_with_bootstrap`, `parse_structured`, `FieldValidationError`,
   `build_profile_text`, `refresh_profile_embedding`, and `backfill_profile` already live in neutral packages
   (`profiles/`, `embeddings/`, `ranking/`) and are reused as-is. Net rule: after relocation, **nothing kept
   imports `web/routes/profiles.py`** (a severance check, as in G1).

### 3.2 Frontend — `Profiles*` routes + hooks

- `api/profiles.ts`: `useProfilesList()` (`["profiles"]`), `useProfile(id)` (`["profile", id]`, `retry:false`,
  `refetchInterval: (q) => q.state.data?.bootstrap_status ∈ {queued,running} ? 3000 : false`),
  `useCreateProfile()` (POST → `{id}`; caller navigates), `useUpdateProfile(id)` (PUT; on success invalidate
  `["profile", id]` + `["profiles"]`; caller navigates to detail).
- `routes/ProfilesList.tsx` + `.module.css`: cards (name→detail `Link`, kind, areas count, currency, truncated
  description, "Ranked feed →" `/?profile={id}` `Link`, Edit `Link`) + empty state + "New profile" `Link`.
- `routes/ProfileDetail.tsx`: read view (description; rubric `<dl>`; the grouped "Profile" namespaced section,
  each row rendered only-if-present like the Jinja) + "Ranked feed →" / "Edit" buttons; 404 → not-found.
- `routes/ProfileNew.tsx`: create form (name, kind `<select>` toggle, seed inputs members/grants/mission, the
  shared structured fields); submit → `useCreateProfile` → navigate to `/profiles/{id}/edit`.
- `routes/ProfileEdit.tsx`: edit form pre-filled from the read-model. **Drafting state:** while `bootstrap_status
  ∈ {queued,running}` show the banner and **disable the fieldset** (parity with the Jinja `disabled` + 3s poll);
  when it flips to `done`/`failed` the refetch fills + enables the fields. Save → `useUpdateProfile` → navigate
  to detail. When `!can_edit`, the fieldset is disabled with a note. The banner mirrors `bootstrap_banner.html`
  (queued/running → "drafting…"; done → publications summary keyed off `bootstrap_pubmed_status`; failed →
  "couldn't auto-draft").
- **Shared `StructuredFields({ kind, values, onChange, disabled })`** component: the kind-conditional structured
  inputs (org block / individual block / common geo+funding+applicant fields) appear in **both** New and Edit, so
  they live in one place. New/Edit own their non-shared parts.
- **Kind toggle:** React state in New (the `kind` select switches the org/individual block); fixed by
  `profile.kind` in Edit (kind is not editable). This replaces the `/static/app.js` `#kind-select` listener;
  `app.js` itself stays (other Jinja pages use it until the final auth teardown).
- `shell/nav-items.ts`: flip the `profiles` item `kind:"external"`→`"spa"` (`to:"/profiles"` unchanged). Convert
  the `Settings.tsx` empty-subscriptions `<a href="/profiles/new">` to a React `Link` now that `/profiles` is
  in-app.
- Styling = CSS Modules + `--app-*` tokens; never the dead Jinja globals.

### 3.3 Manifest / routing

- `/profiles` owned as a **PREFIX** in `frontend/routes.json` `spa_routes` (the `/calls` precedent). Router
  routes `{path:"profiles"}`, `{path:"profiles/new"}`, `{path:"profiles/:id"}`, `{path:"profiles/:id/edit"}`
  under the AppShell. `router.test.tsx` is set-equality and collapses the four React leaves to the `/profiles`
  owning prefix, so the route additions + the manifest prefix land in **one commit**. Regen `routes.gen.ts` +
  `spa_routes.py`.
- After teardown, remove `/profiles` from `backend_prefixes` (the `/api/profiles/*` endpoints live under `/api`).

---

## 4. Scope & boundaries

- **One slice:** the list + detail + create(+bootstrap) + edit `/api`, the four React routes + the nav flip +
  the manifest prefix + the Jinja teardown.
- **Additive**, then **per-slice teardown**: delete the whole Jinja `web/routes/profiles.py` module + the four
  templates + `partials/bootstrap_banner.html` + the `main.py` import/`include_router`, after relocating the
  reusable form helpers to `profiles/forms.py`.
- **No DB schema change. No new dependency. Strict parity port.**
- **Deferred / out of scope:** form-UX improvements (chip inputs, field collapsing); any change to the
  bootstrap **worker** (the scheduler tick that drafts the job is untouched — this slice only reads its status
  and enqueues jobs exactly as the Jinja create did); the admin and auth+final-teardown sub-projects; member
  ORCID resolution / PubMed behavior (reused as-is).

---

## 5. Data flow / error handling

- **Create → bootstrap → edit:** `POST /api/profiles` enqueues the `ProfileBootstrapJob` (status `queued`) and
  returns `{id}`; React navigates to `/profiles/{id}/edit`, where `useProfile(id)` polls every 3s while the job
  is non-terminal. The background **worker** (unchanged) drafts the description + rubric; when the job flips to
  `done`/`failed`, the next poll returns the filled read-model, the form enables, and the banner shows the
  result. Identical effect to the Jinja 303→edit→HTMX-poll→`HX-Refresh`, minus the full-page reload.
- **Edit save:** `PUT /api/profiles/{id}` re-namespaces the flat form fields, persists, and schedules the
  re-embed + backfill background task (re-embed only when `build_profile_text` changed; backfill always when
  `description` is non-empty), then React navigates to the detail page.
- **Validation:** every Jinja status code is mirrored (create: 400/409/413; update: 400/403/404/409/413). Unknown
  ids / inactive profiles → 404. Unauthenticated → 401.
- **CSRF:** `POST`/`PUT` are unsafe `/api` methods so `OriginCSRFMiddleware` applies, but it 403s only a
  *present-but-mismatched* / `"null"` Origin — an **absent** Origin is allowed (`csrf.py`), so the TestClient
  mutation tests pass without an Origin header (as the last three slices do).
- **Routing order:** add the React routes + `/api` + manifest `/profiles` FIRST; delete the Jinja routes +
  remove `/profiles` from `backend_prefixes` LAST. While the Jinja routes exist they win over the catch-all, so
  the manifest prefix is inert mid-slice (no regression). In dev, after the manifest flip the React routes become
  live (the dev proxy serves them); this is the intended cutover, identical to the prior slices.

---

## 6. Verification gates

- `uv run python -c "import call_manager.main"` boots; after teardown no non-`/api` route under `/profiles/`
  remains (route-table check).
- **Backend suite green** (`tests/` ROOT, psycopg3, `pg_url`/`pg_session_factory`/`profile_factory`):
  - `test_api_profiles_list`: active-only, by name, fields + `areas_count`.
  - `test_api_profile_detail`: flat namespaced→flat projection (org/eligibility/funding/geo unwrapped);
    `can_edit` for owner / admin / other; `bootstrap_status` surfaced; 404 on inactive + missing.
  - `test_api_profiles_create`: success returns `{id}` AND enqueues a `ProfileBootstrapJob` (status `queued`) +
    auto-subscribes the creator + sets default profile; duplicate-name 409; name 400; grants/mission 413;
    member-bounds 400; `parse_structured` 400; 401 unauth.
  - `test_api_profile_update`: owner success re-namespaces the structured fields (assert persisted
    `structured.{org,…}` shape); 403 non-owner-non-admin; 409 while drafting; 404; budget/TRL bounds 400;
    currency 400; description 413; `parse_structured` 400.
  - `test_gen_routes` (`is_spa_route("/profiles")` / `("/profiles/5/edit")` True; `is_backend_route` on a
    `/profiles` subpath False post-teardown; `is_spa_route("/api/profiles")` stays False),
    `test_profiles_jinja_surface_absent`, and the three repointed `test_web_auth` tests. `ruff` clean.
  - **Offline-tests gotcha:** the `PUT` edit path schedules the re-embed + backfill background task when
    `description` is non-empty, and `TestClient` runs background tasks synchronously after the response — which
    would hit Ollama and hang/fail offline. Update-endpoint tests MUST either **monkeypatch
    `_refresh_and_backfill_in_background`** (or the embed client / `backfill_profile`) or exercise edits that do
    not schedule it. Create is clean (it only enqueues the `ProfileBootstrapJob` row; the worker drafts
    separately).
- **Frontend `tsc` + `vitest` green** + `build` clean: `api/profiles.test.tsx` (the four hooks incl. the
  poll-while-drafting `refetchInterval`), `ProfilesList`/`ProfileDetail`/`ProfileNew`/`ProfileEdit`/
  `StructuredFields` component tests, and the `router.test.tsx` / `dev-proxy.test.ts` / `ActivityBar.test.tsx`
  drift-guard updates.
- **Auth re-repoint — THREE tests** (the recurring chain). The account slice parked all three `test_web_auth`
  `/me`-sampling tests on `/profiles`: `test_html_401_get_threads_next` (`GET /profiles?x=1` → 303),
  `test_json_401_unaffected_by_next` (`GET /profiles`, no `Accept: text/html` → raw 401 JSON),
  `test_html_401_post_does_not_thread_next` (`POST /profiles/5/edit` → 303). This slice deletes the Jinja
  `/profiles` routes, so all three move to the next surviving protected Jinja surface, **`/admin`** (verified
  targets): the two GET tests → `GET /admin` (`admin_index`, HTMLResponse, `require_admin` → `get_current_user`
  raises 401 before the admin 403 check); the POST test → `POST /admin/rerank` (no Form fields, no path param →
  a clean 401, never a 422). Confirm the exact `next=` encoding by running.
- **Manifest/schema regen:** `uv run python scripts/gen_routes.py` → `SPA_ROUTES` gains `/profiles`,
  `BACKEND_PREFIXES` drops `/profiles`; `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` → the four
  `/api/profiles*` paths added, the four Jinja `/profiles*` HTML paths dropped; `routes.gen.ts` gains the
  `/profiles` prefix.

---

## 7. Risks / notes

- **Biggest slice yet (~11–13 tasks).** Two long forms (create ~17 fields, edit ~25), the structured-namespace
  round-trip, and the async bootstrap polling. The shared `StructuredFields` component and the consolidated
  read-model are the main complexity-reducers.
- **Structured-namespace round-trip is the highest-risk area.** The read-model flattens
  `structured.{org,eligibility,funding,geo}` → flat fields for the form; `PUT` re-namespaces via the EXISTING
  `parse_structured` (so the write logic is reused, not reimplemented). The detail-view test must assert the
  flat projection matches a known namespaced `structured`, and the update test must assert the round-trip
  re-persists the correct namespaced shape.
- **Background-task offline hazard** (see §6) — the single most likely source of a hung/flaky test.
- **Bootstrap-status visibility consolidation** (see §3.1) — intentional, non-sensitive, save stays owner-gated.
- **Auth-test re-repoint is a recurring chain:** account → `/profiles`; **this slice → `/admin`**; the auth slice
  ultimately moves the 401 behavior client-side. Expected, small, per-slice.
- **Model-name collisions:** namespace the new models (`Profiles*`/`Profile*`); verify none collide in
  `schema.gen.ts` during the plan (the calls-detail `CriterionOut` lesson).
- **CSS Modules:** scope every selector under a hashed class — no bare element selectors (the account-slice
  `button`-leak lesson).
- **Hardcoded-link audit (G2 lesson):** grep kept Jinja/React for hardcoded `/profiles*` links after teardown;
  `base.html`'s nav link to `/profiles` keeps working (it now full-navs to the SPA) and stays.

---

## 8. Out of scope

- Admin; auth + final teardown (`base.html`/Pico/`app.css`/`app.js`).
- Any redesign beyond parity (chip inputs, field collapsing); changes to the bootstrap worker or PubMed flow;
  ORCID resolution behavior.
