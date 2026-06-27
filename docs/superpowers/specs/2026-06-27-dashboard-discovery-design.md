# Dashboard Discovery — design spec

**Date:** 2026-06-27
**Status:** approved (design), pending implementation plan
**Source:** "Improvement Opportunities — Feature & Usability" §High-value/near-term in `docs/superpowers/notes/2026-06-27-thorough-code-review.md` (the review's #1 ROI theme: *"Discovery is broken at the core"*).

## Goal

Make the funding-call dashboard searchable and filterable so a researcher can actually reach the calls relevant to them — not just the top 5 the digest surfaces. Three cohesive pieces, one slice:

1. **Free-text search** (`q`) on the calls feed.
2. **Score floor** (`min_score`) to narrow to strong matches.
3. **"+N more in your dashboard →"** breadcrumb in the weekly digest, deep-linked to the now-filterable dashboard.

The review's framing: without search and a score filter, "matches 6–40 are unreachable" for a given profile in a given week. This slice closes that gap.

## Architecture & approach

- **Search mechanism: ILIKE over existing pg_trgm GIN indexes.** `pg_trgm` is already enabled (migration `0033`) with `gin_trgm_ops` indexes on `calls.title`, `calls.summary`, `calls.full_text`. Index-backed substring search needs **no new migration**. The digest's multi-column matcher (`digest/assemble.py`) is the in-repo precedent. A `tsvector` full-text column (relevance ranking, stemming) was considered and rejected: it adds a migration + backfill + sync for a payoff that doesn't justify itself at this data volume (thousands, not millions, of calls). Deferred unless relevance ranking is specifically wanted later.
- **Filters hook into the existing read-model.** `q` joins the shared `base_filters` (so it narrows the page list *and* the summary badges consistently); `min_score` is a ranked-branch-only predicate shared by the page query and its pagination count.
- **Frontend follows the established URL-as-source-of-truth pattern.** Both filter panes share a single `["calls", params]` TanStack query; controls write the URL via `updateParam` (which resets `page`); no props cross the shell boundary.

## Tech stack

Python 3.12 / FastAPI / SQLAlchemy 2.x / PostgreSQL 16 (+ pg_trgm, already enabled); React 18 + Vite SPA, Mantine v9, `@mantine/hooks`, TanStack Query, `openapi-fetch` against generated types.

## Resolved decisions (the design forks)

| Decision | Choice | Rationale |
|---|---|---|
| Search match semantics | **Substring** (`%q%`) | Forgiving, typing-friendly: `onco`→`oncology`, `EU`→`EUREKA`. Trigram-index-accelerated. |
| Multi-word handling | **AND across terms, OR across columns** | All typed terms must appear; each term may match any of the four columns. |
| Searched columns | `title`, `summary`, `full_text`, `keywords` | The four content columns; mirrors the digest matcher. |
| Score filter shape | **Floor, badge-aligned**: Any / 60+ (Good) / 80+ (Strong) | Matches the `MatchBadge` pill bands users already see in the list, so the colored badges agree with the filter. |
| Unranked (no-score) calls under an active floor | **Drop them** | A floor means "show me matched calls only"; default view (no floor) still shows everything. |
| Digest breadcrumb scope | Per-profile-section, **"top" bucket (≥60)** count, only when `> 0` | The high-value reachable-matches gap; deep-link floor (60) equals the digest top threshold. |
| Score echo in API response | **None** (request-only params) | Frontend reads control values from the URL; response model stays stable. |

## Current state (surface map, 2026-06-27)

- **`GET /api/calls`** (`src/call_manager/web/api/calls.py:373-424`) currently accepts `source`, `sort`, `profile`, `limit`, `page`, `show_closed`. It resolves `profile` read-only (`resolve_profile_selection`, `persist=False`) then calls `build_dashboard_view`. No `q`/`min_score` today.
- **`build_dashboard_view`** (`src/call_manager/web/dashboard.py:205-374`):
  - Ranked branch (valid profile): `select(Call, CallRanking)` LEFT JOIN `call_rankings` on `(call_id, profile_id)`, ordered open-first then `aggregate_score DESC NULLS LAST`, `embed_similarity DESC NULLS LAST`, `published_at DESC NULLS LAST`, `id DESC`.
  - Unranked branch (no profile): `select(Call, None)`, ordered open-first then `_order_for(sort)`.
  - `base_filters = [Call.id.notin_(dismissed_subq)]` (+ `source_id` when set) — shared by the page query **and** the summary aggregates.
  - `list_filters = base_filters + open_expr` (unless `show_closed`) — used by the page list and `list_total` (`pagination.total`, `has_next`).
  - `DashboardView.summary/pagination/ranking/effective` are plain dicts populated at `dashboard.py:354-372` and coerced by `response_model`.
- **`CallRanking.aggregate_score`** (`src/call_manager/db/models.py:344`) — `SmallInteger`, **NOT NULL**, range **0–100** integer. A call with no ranking for the profile has **no row** (NULL only appears via the LEFT JOIN). Index `ix_call_rankings_profile_score` on `(profile_id, aggregate_score DESC NULLS LAST)`.
- **Calls text columns** (`src/call_manager/db/models.py:184-192`): `title` (Text, not null), `summary` (Text, null), `full_text` (Text, null), `keywords` (`ARRAY(Text)`, not null). Trigram GIN indexes on title/summary/full_text (`models.py:158-175`). `keywords` has a plain GIN index (`&&`/`@>` only — not ILIKE).
- **Digest** (`src/call_manager/digest/assemble.py`): per-profile `top` (≥60, `DIGEST_TOP_THRESHOLD`), `borderline` (45–59), `must_include` buckets. `top`/`borderline` over-fetch to `limit(50)`, shown capped at `DIGEST_BUCKET_SIZE = 5`. **No dropped/"+N more" count is computed anywhere** — the surplus is sliced away silently at `assemble.py:307-326`. Templates `digest.html.jinja` / `digest.txt.jinja` already build per-call links as `{{ dashboard_url }}calls/{{ e.call_id }}?profile={{ section.profile_id }}`. `dashboard_url` = `settings.admin_dashboard_url` (trailing slash).
- **Frontend** (`frontend/src/`): `routes/CallsFilters.tsx` (sidebar controls), `routes/Dashboard.tsx` (list), `api/dashboard.ts` (`dashboardParamsFrom` + `useDashboard`). `updateParam(key,value)` writes the URL via the functional `setSp` updater, deletes the param when value is falsy, and **always deletes `page`**. `useDashboard` keys on `["calls", params]` with `placeholderData: keepPreviousData`; the global default `staleTime` is 20 s. `@mantine/hooks@9.4.0` ships `useDebouncedValue`. No shared debounce helper exists; **do not hand-roll one**. CSS Modules with `--app-*` tokens; **no bare element selectors** in `.module.css`; every `Select` passes `comboboxProps={{ withinPortal: false }}`. `tsc` is the type gate (no TS ESLint).

## Component 1 — Backend: `q` and `min_score` on the calls feed

### Endpoint signature (`src/call_manager/web/api/calls.py:374-383`)

Add two params and thread them into `build_dashboard_view` (the `resolve_profile_selection` call is unchanged):

```python
q: str | None = Query(default=None, max_length=200),
min_score: int | None = Query(default=None, ge=0, le=100),
```

### Read-model (`src/call_manager/web/dashboard.py`)

Add `q: str | None = None` and `min_score: int | None = None` params to `build_dashboard_view`.

**Search predicate (`q`).** A module-level helper builds the predicate; `None`/blank `q` (after `.strip()`) yields no filter.

```python
def _ilike_escape(term: str) -> str:
    # Escape LIKE wildcards so user input matches literally; "\" is the escape char.
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _search_predicate(q: str):
    # AND across whitespace-split terms; OR across the four content columns.
    keywords_str = func.array_to_string(Call.keywords, " ")
    term_clauses = []
    for term in q.split():
        pat = f"%{_ilike_escape(term)}%"
        term_clauses.append(
            or_(
                Call.title.ilike(pat, escape="\\"),
                Call.summary.ilike(pat, escape="\\"),
                Call.full_text.ilike(pat, escape="\\"),
                keywords_str.ilike(pat, escape="\\"),
            )
        )
    return and_(*term_clauses)
```

Appended to `base_filters` (so it narrows the page list and every summary badge):

```python
if q and q.strip():
    base_filters.append(_search_predicate(q.strip()))
```

**Score floor (`min_score`).** Applies only in the ranked branch (`profile_id is not None`). The ranked page query already selects `CallRanking`, so the predicate references it directly:

```python
apply_score_floor = profile_id is not None and min_score is not None
if apply_score_floor:
    list_filters.append(CallRanking.aggregate_score >= min_score)
```

Because `aggregate_score >= min_score` is false for the LEFT-JOIN NULLs, **unranked calls drop out** — the desired behavior. The `list_total` count (`dashboard.py:306`) must mirror the page query: when `apply_score_floor`, the count joins `CallRanking` on `(call_id, profile_id)` so the predicate resolves and `pagination.total`/`has_next` stay correct. The three summary aggregates (`open_count`, `high_score`, `soon_deadline`) are **not** given the floor — they continue to describe the full (q-filtered) profile feed; the floor narrows only the scrolled list.

**Invariants preserved.** Dismissal (`notin_` subquery), `show_closed`/`open_expr`, sort orders, and the existing summary/pagination math are untouched. The parity contract in `tests/test_api_calls_feed_behavior.py` stays green.

## Component 2 — Digest: "+N more in your dashboard →"

### Compute the count (`src/call_manager/digest/assemble.py`)

Factor the "top" bucket's filter conditions (open: `deadline IS NULL OR deadline > now`; not-excluded: the existing `excl_clause`; `aggregate_score >= DIGEST_TOP_THRESHOLD`) into a reusable list so the candidate query and a new count query share one definition (DRY). After the per-section buckets are assembled and capped, run one `count(*)` per profile section over those conditions to get `top_total`, and store:

```python
more_count = max(0, top_total - len(shown_top))   # shown_top capped at DIGEST_BUCKET_SIZE (5)
```

`more_count` is a new field on `DigestProfileSection` (default 0). It does not affect `total_calls` or bucket caps.

### Render (`digest.html.jinja`, `digest.txt.jinja`)

After the borderline block, before the section `{% endfor %}`, **only when `section.more_count > 0`**:

- HTML: `<a href="{{ dashboard_url }}?profile={{ section.profile_id }}&min_score=60">+{{ section.more_count }} more strong matches in your dashboard →</a>`
- Text: `+{{ section.more_count }} more strong matches in your dashboard: {{ dashboard_url }}?profile={{ section.profile_id }}&min_score=60`

The deep-link floor `60` equals `DIGEST_TOP_THRESHOLD`, so the link lands on exactly the "60+ Good" dashboard view for that profile — the same calls, the same number.

**v1 limitation (documented, not a bug):** a section whose only matches are in the borderline bucket (45–59) and has no top matches gets no breadcrumb. The breadcrumb is specifically about reachable strong matches. The digest cannot account for the recipient's per-user dismissals, so the count is the profile's top-match surplus, which is the cleanest honest number.

## Component 3 — Frontend: search box + score floor

### Params plumbing (`frontend/src/api/dashboard.ts`)

Extend `DashboardParams` and `dashboardParamsFrom`:

```ts
// DashboardParams
q?: string;
min_score?: number;

// dashboardParamsFrom
q: sp.get("q") ?? undefined,
min_score: sp.get("min_score") ? Number(sp.get("min_score")) : undefined,
```

Both join the `["calls", params]` key automatically; `keepPreviousData` prevents list flicker between debounced commits. No change to `useDashboard`.

### Controls (`frontend/src/routes/CallsFilters.tsx`)

Import `TextInput` (add to the existing `@mantine/core` import) and `useDebouncedValue` from `@mantine/hooks`.

**Search** — controlled `TextInput` over local state for keystrokes; a debounced mirror commits to the URL:

```ts
const [searchInput, setSearchInput] = useState(params.q ?? "");
const [debouncedSearch] = useDebouncedValue(searchInput, 300);
// Commit to the URL only when the debounced value actually differs from the
// URL — this guard is what prevents an echo loop with the re-sync effect below.
useEffect(() => {
  const next = debouncedSearch.trim();
  if (next !== (params.q ?? "")) updateParam("q", next);
}, [debouncedSearch, params.q]);
// Re-sync local state when the URL q changes externally (back/forward, breadcrumb link):
useEffect(() => { setSearchInput(params.q ?? ""); }, [params.q]);
```

The `next !== (params.q ?? "")` guard is load-bearing: a URL-driven change (back/forward, a digest breadcrumb landing) flows URL → `setSearchInput` → `debouncedSearch`, and without the guard the commit effect would re-write the same `q` back to the URL, churning history. With it, the commit fires only on genuine user keystrokes.

**Score floor** — a static-options `Select` cloned from Sort (`CallsFilters.tsx:81-91`):

```ts
<Select
  label="Min score"
  data={[
    { value: "", label: "Any" },
    { value: "60", label: "60+ (Good)" },
    { value: "80", label: "80+ (Strong)" },
  ]}
  value={params.min_score != null ? String(params.min_score) : ""}
  onChange={(v) => updateParam("min_score", v ?? "")}
  allowDeselect={false}
  comboboxProps={{ withinPortal: false }}
  disabled={loading || data?.effective.profile_id == null}
/>
```

Disabled when no profile is resolved (`data.effective.profile_id == null`), since the floor needs scores; a short hint (`description`/tooltip) explains why. Styling uses `--app-*` tokens; band swatch colors, if any, reuse the `MatchBadge` band tokens. No bare element selectors in `CallsFilters.module.css`.

## Data flow

1. User types in the search box → local state updates immediately (controlled input) → `useDebouncedValue` settles after 300 ms → effect calls `updateParam("q", …)` → URL gains `?q=…`, `page` dropped → `dashboardParamsFrom` re-derives `params` → `["calls", params]` re-keys → `api.GET("/api/calls", {query: params})` → backend appends the search predicate to `base_filters` → list + badges narrow; `keepPreviousData` holds the old list until the new one arrives.
2. User picks a score floor → `updateParam("min_score", "60")` (or `""` to clear) → backend applies the ranked-branch floor → unranked calls drop, `pagination.total` reflects the floor.
3. Digest send → per profile section, count top matches, compute `more_count`, render the breadcrumb deep-linked to `?profile=&min_score=60` → recipient clicks → lands on the filtered dashboard.

## Edge cases & error handling

- Empty/whitespace `q` → param dropped (clean URL), no filter applied.
- `q` > 200 chars → 422 (FastAPI `max_length`).
- `q` containing `%` `_` `\` → matched literally (escaped).
- `min_score` outside 0–100 → 422 (`ge`/`le`).
- `min_score` set with no profile selected → ignored server-side; the control is disabled in that state, so it isn't normally reachable except via a stale deep link (harmless).
- Score floor with zero matching scored calls → empty list + correct `pagination.total = 0`; default no-floor view unaffected.
- `more_count`: top bucket ≤ 5 → no breadcrumb; > 5 → "+N more".

## Testing

- **Backend** — extend `tests/test_api_calls_feed_behavior.py`:
  - `q`: single-term substring match; multi-term AND; per-column coverage (title, summary, full_text, keywords); literal escaping of `%`/`_`; blank `q` is a no-op; `q` reflected in summary badges.
  - `min_score`: floor returns only `>= N`; unranked calls dropped under an active floor; floor ignored without a profile; `pagination.total`/`has_next` reflect the floor; summary badges reflect `q` but **not** the floor.
  - Regression: existing dismissal/`show_closed`/sort/pagination assertions unchanged.
- **Digest** — extend the assemble + template tests: `more_count` math (`<=5 → 0`, `>5 → surplus`); both templates render the deep link with `min_score=60` only when `more_count > 0`.
- **Frontend** — extend `routes/CallsFilters.test.tsx` and `routes/Dashboard.test.tsx`: debounced search commits to the URL and resets `page`; URL `q` re-syncs the box; score `Select` disabled without a profile; both params bind to the URL and into the query key.
- **Gates** — no DB migration (no schema change); regenerate `frontend/src/api/schema.gen.ts` via `pnpm gen:api`; `ruff check .`, `tsc`, `vitest run`, `pnpm build`, OpenAPI drift, and the `gen_*` `--check` scripts all green.

## File-by-file change map

| File | Change |
|---|---|
| `src/call_manager/web/api/calls.py` | Add `q`/`min_score` `Query` params; thread into `build_dashboard_view`. |
| `src/call_manager/web/dashboard.py` | `_ilike_escape`/`_search_predicate` helpers; `q` → `base_filters`; ranked-branch `min_score` floor + score-aware `list_total` count. |
| `src/call_manager/digest/assemble.py` | Factor top-bucket conditions; per-section `top_total` count → `more_count` on `DigestProfileSection`. |
| `src/call_manager/digest/templates/digest.html.jinja` | "+N more" breadcrumb (conditional). |
| `src/call_manager/digest/templates/digest.txt.jinja` | "+N more" breadcrumb (conditional). |
| `frontend/src/api/dashboard.ts` | `q`/`min_score` on `DashboardParams` + `dashboardParamsFrom`. |
| `frontend/src/routes/CallsFilters.tsx` | Debounced search `TextInput`; score-floor `Select` (disabled without profile). |
| `frontend/src/api/schema.gen.ts` | Regenerated (new query params). |
| `tests/test_api_calls_feed_behavior.py` | Backend coverage. |
| digest assemble/template tests | `more_count` + breadcrumb coverage. |
| `frontend/src/routes/CallsFilters.test.tsx`, `Dashboard.test.tsx` | Frontend coverage. |

## Out of scope (deliberate)

- Unifying the four inconsistent score-band definitions across surfaces (`MatchBadge` ≥80/≥60, `CallDetail` ≥70, summary ≥70, digest ≥60/45) — a separate Medium review item.
- Relevance ranking (`ts_rank`/`tsvector` full-text).
- Borderline-bucket breadcrumbs.
- A trigram index on `calls.programme` (search covers the four content columns).
