# Cockpit SP2b-3b — Problems Panel + Bottom-Panel Region — Design Spec

> Sub-project 2 (Workspace Cockpit), slice SP2b-3b. Follows SP2b-3a (evaluator + Scorecard,
> merged to master `6bcdcc0`). Overarching design:
> `docs/superpowers/specs/2026-06-17-cockpit-workspace-design.md` (§9 Layout / region map, §10
> Coverage/Scorecard). Sibling read-models on master: the coherence-keeper (SP2b-2c) and the
> evaluator scorecard (SP2b-3a).

## 0. Scope & locked decisions

SP2b-3b builds the **bottom-panel region** of the cockpit and its first two tabs:

1. **Inbox** — the proposals inbox, **relocated** out of the inline Draft/Spec panels into a
   single unified surface.
2. **Problems** — a new **read-only health dashboard** that aggregates four quality signals
   already derivable from data on master and deep-links the user to where to act.

The Problems panel is the mirror image of the Inbox: the Inbox is where you **resolve**
proposal-shaped work; Problems is the **"what needs attention"** summary that **points at** the
place to act (a section, a citation, the Scorecard, or the Inbox).

**Locked decisions (brainstorm 2026-06-22):**

1. **Four cheap signals only.** Problems aggregates: **broken citations**, **criterion gaps**,
   **over-length sections**, **coherence drift** — all computed **synchronously** from data on
   master. The LLM-backed **uncited-claims** signal and the **Activity** feed are explicitly
   deferred (§8). Rationale: the first four are pure reads (citation audit, scorecard, coherence
   proposals) plus one trivial new count; folding in the per-sentence audit LLM would make the
   endpoint slow/costly or require a third background-job subsystem.
2. **No new background job, no new table, no migration.** Problems is recomputed on every GET
   and polled by the client, exactly like the scorecard/coherence-latest endpoints. This is the
   **first SP2b slice with zero schema change.**
3. **Coherence drift = a single roll-up pointer, not one row per proposal.** The coherence
   proposals already live in the Inbox; Problems emits **one** row ("N spec↔draft items pending")
   that focuses the Inbox tab, deliberately avoiding duplication.
4. **Citation checks are deterministic-only** (`validate_citations_deterministic`) — **no
   semantic/LLM call** in the synchronous endpoint.
5. **Layout = a persistent, collapsible, fixed-height bottom region** with its own tab strip
   (Inbox · Problems) and a collapse/expand toggle. No drag-resize (deferred — YAGNI for v1).
   The region is always mounted below the artifact view, independent of the active top-tab
   (Draft/Spec/Scorecard).
6. **Inbox relocation is a move, not a copy.** The inline `ProposalReview` is **removed** from
   `DraftPanel`/`SpecPanel`; the bottom Inbox becomes the single source of truth, showing both
   artifacts' pending prose proposals grouped by artifact, with spec-group Accept still
   lock-gated.

**Explicitly NOT in this slice (see §8):** uncited-claims (LLM) detection, the Activity feed,
drag-resize of the bottom panel, deep-link section-scroll (tab-switch only, same as the Scorecard
today), citation proposals in the inbox (still `prose_edit`-only, unchanged from current).

**Foundation already on master (read, do not rebuild):**

- **Broken citations:** `proposal_assist/citations/audit.py:134`
  `validate_citations_deterministic(proposal_text, ledger, session, workspace_id) -> list[dict]`.
  Each issue dict: `{type, severity: "error"|"warning", citekey, evidence_id, location, detail}`
  over 11 deterministic issue types (orphan_marker, orphan_row, reference_missing,
  metadata_unapproved, reference_tombstoned, chunk_missing, source_replaced, index_drift,
  quote_missing, claim_drift, internal). **No LLM.**
- **Citation markers → offsets:** `proposal_assist/citations/markers.py` `find_markers(text)` →
  `[{ev_id, citekey, start, end}]` (char offsets), for mapping an issue's `location` to a draft
  section.
- **Section splitting:** `proposal_assist/editing/sectionizer.py:43` `split_sections(text) ->
  list[Section]`; `Section(index, heading, text, sha)`. Used for the offset→section_index map
  and the over-length count.
- **Criterion gaps:** the evaluator scorecard JSONB (SP2b-3a). Per-criterion shape
  `{index, name, band: "strong"|"adequate"|"at_risk"|"gap", note, deep_link: {artifact,
  section_index}|null, weight, effective_weight}`; `overall_band`; optional `summary`. Read via
  `evaluator/jobs.py:146` `latest_completed_evaluator_job(...)`; the existing
  `web/evaluator.py:94` `GET /scorecard` computes per-input **stale flags**
  (`draft`/`spec`/`rubric`) we pass through.
- **Coherence drift:** pending `ProposedChange` rows with `origin_kind="coherence_session"`,
  visible only when their `origin_job` is `completed` (the same visibility gate
  `web/proposals.py:47` uses). Count → the single roll-up row.
- **Proposals inbox read-model:** `web/proposals.py` `GET /api/workspaces/{id}/proposed-changes`
  (`?target_artifact=`), accept/reject endpoints, and the frontend `api/proposals.ts`
  hooks + `cockpit/ProposalReview.tsx` (inline today) — relocated, not rewritten.
- **Router pattern:** routers mounted in `main.py:524–542`, prefix `/api/workspaces`,
  member-gated (`require_member`). `web/problems.py` joins this list.

## 1. Data model

**None.** No tables, no columns, no migration. Problems is a pure read-model over existing
data. Alembic head stays `0054` after this slice.

## 2. The Problems aggregation

New backend package `proposal_assist/problems/`. A single public entry assembles a flat list of
`ProblemRow` dicts from four independent signal builders, each in its own defensive try-block so
one failing source contributes **zero rows** rather than 500-ing the endpoint.

### 2.1 `ProblemRow` shape

```jsonc
{
  "category": "broken_citation" | "over_length" | "criterion_gap" | "coherence_drift",
  "severity": "error" | "warning",
  "title":   "Broken citation in §2",        // one-line human summary
  "detail":  "marker [smith2020] has no ledger row",
  "target":  {                                // deep-link to act; null if not navigable
    "top_tab":    "draft" | "spec" | "scorecard" | null,  // artifact-view tab to activate
    "bottom_tab": "inbox" | "problems" | null,            // bottom-region tab to focus (+ expand)
    "section_index": 2 | null                             // future scroll target; not yet honored
  } | null
}
```

**Navigation model (one explicit space per axis).** A target sets a `top_tab` (the artifact-view
tab strip: Draft/Spec/**Scorecard**) and/or a `bottom_tab` (the bottom-region tab strip:
Inbox/Problems). The two axes are independent; a given signal uses exactly one. The Scorecard is a
**top-tab**, so the no-deep-link criterion fallback navigates there via `top_tab: "scorecard"` (not
a bottom-region signal). `onNavigate` (§4.1) honors both axes, so every row's "act" has a valid
destination — there is no `artifact: null` dead-end.

The endpoint returns:

```jsonc
{
  "problems": [ ProblemRow, ... ],            // flat; the client groups by category
  "counts": { "error": <int>, "warning": <int> },
  "scorecard_stale": [ "draft" | "spec" | "rubric", ... ]  // passthrough; [] if fresh/absent
}
```

### 2.2 Signal builders

All signals operate over the **draft** artifact unless stated otherwise.

1. **broken_citation** — load the current draft text and parse the ledger with
   `load_ledger(citations_json_text or None)` — **always run the validator, even when there is no
   `citations.json`.** `load_ledger(None)` returns a valid *empty* ledger
   (`citations/ledger.py:20`), so a draft with inline markers but no ledger still yields
   `orphan_marker` **error** rows (`citations/audit.py:166`) — those are real broken citations and
   must not be hidden by "absent ledger → empty." Call `validate_citations_deterministic(...)` and
   map **each** issue → one `ProblemRow`: `severity` = issue severity; `title` =
   `"Broken citation in §{n}"` (or "(unplaced)" when the issue has no resolvable marker offset);
   `detail` = issue `detail`; `target` = `{top_tab: "draft", bottom_tab: null, section_index:
   <offset→section>}`. The offset→section map uses `find_markers` + `split_sections` (cumulative
   section spans); an issue whose `location` has no resolvable offset (e.g. `orphan_row`) gets
   `section_index: null`. **Corrupt/unsupported ledger:** `load_ledger` raises `ValueError` on
   invalid JSON or a wrong version (`citations/ledger.py:26–37`). Catch it and emit **one**
   `broken_citation` **error** row (`title` = "Citations ledger unreadable", `detail` = the
   exception message, `target` = `{top_tab: "draft", bottom_tab: null, section_index: null}`) —
   never silently zero rows. The only genuinely-empty case is **no draft and no markers**.

2. **criterion_gap** — read the latest completed scorecard. For every criterion with
   `band == "gap"` (→ `severity: "error"`) or `band == "at_risk"` (→ `severity: "warning"`):
   `title` = `"Criterion {band}: {name}"`; `detail` = the criterion `note`; `target` =
   the criterion's `deep_link` mapped to `{top_tab: deep_link.artifact, bottom_tab: null,
   section_index: deep_link.section_index}` when present, else the Scorecard fallback
   `{top_tab: "scorecard", bottom_tab: null, section_index: null}` (so no-deep-link gaps still
   have a valid destination — the Scorecard tab). No scorecard yet → zero rows.
   The endpoint's top-level `scorecard_stale` carries the existing stale flags so the panel can
   badge "may be outdated" without dropping the rows.

3. **over_length** — new config `problem_section_char_budget: int` (default **6000**; a value
   `<= 0` disables the check). For each draft `Section`, if `len(section.text) > budget` emit one
   `warning` row: `title` = `"Over-length §{index}"`; `detail` = `"{len} chars (budget {budget})"`;
   `target` = `{top_tab: "draft", bottom_tab: null, section_index: index}`. Documented as a **loose
   heuristic guardrail**, not a hard funder limit.

4. **coherence_drift** — count pending `coherence_session` proposals whose job is `completed`
   (reuse the proposals visibility gate). If `count > 0`, emit **exactly one** `warning` row:
   `title` = `"Coherence: {count} spec↔draft item(s) pending"`; `detail` = `"Open the Inbox to
   review."`; `target` = `{top_tab: null, bottom_tab: "inbox", section_index: null}`. Count `0` →
   no row.

### 2.3 Ordering & counts

`problems` is ordered by a **total order that never compares `None`** — a naive sort over a mixed
`int`/`None` `section_index` raises `TypeError`. The sort key is:

```
(severity_rank, category_rank, section_index is None, section_index or 0, title, detail)
```

where `severity_rank` puts **errors before warnings**; `category_rank` is the fixed order
[broken_citation, criterion_gap, over_length, coherence_drift]; **`section_index: null` sorts last
within its group** (`is None` → `True` sorts after `False`, and the `section_index or 0` term is
only reached for non-null indices); and `title`/`detail` are the final deterministic tie-breakers.
`counts` is the tally by severity (drives the `Problems N` badge).

## 3. The endpoint

`web/problems.py` — `APIRouter(prefix="/api/workspaces")`, mounted in `main.py` alongside the
sibling routers.

```
GET /api/workspaces/{workspace_id}/problems
  → 200 { problems, counts, scorecard_stale }
  → 403 if the caller is not a workspace member (require_member)
  → 404 if the workspace does not exist
```

- **Member-gated** via the same dependency `proposals.py`/`evaluator.py` use.
- **Synchronous**: recomputed per call; no caching. At single-proposal scale the cost is one
  draft parse + one ledger load + one scorecard read + one count query — cheap enough to poll.
- **Defensive**: the four builders are independent; a missing draft or absent scorecard yields an
  empty contribution, never a 500. **Two deliberate exceptions** (per §2.2): an *absent* ledger
  still runs the citation validator so orphan markers surface, and a *corrupt* ledger becomes a
  single `broken_citation` error row — neither is silenced. The response is always well-formed.

## 4. Frontend

### 4.1 Cockpit shell restructure (`routes/Cockpit.tsx`)

The artifact view (Draft/Spec/Scorecard top-tabs) stays on top; a **persistent collapsible
bottom region** is added below it, always mounted regardless of the active top-tab.

```
┌─ [Draft] [Spec] [Scorecard] ───────────────────┐   top tabs (artifact view)
│   active artifact panel (editor / scorecard)    │
├─────────────────────────────────────────────────┤
│ [Inbox 3] [Problems 5]                      [–]  │   bottom region: tab strip + collapse
│   ⛔ broken citation §2              → act        │
│   ⛔ criterion gap: Impact           → act        │
│   ⚠ over-length §4                   → act        │
└─────────────────────────────────────────────────┘
```

`onNavigate` widens from `(target: "draft" | "spec")` to a **two-axis** object signal
`(target: { top_tab?: "draft" | "spec" | "scorecard"; bottom_tab?: "inbox" | "problems" })`: it
activates the named `top_tab` (now including **Scorecard**) and/or focuses the named `bottom_tab`
(expanding the region if collapsed). Both the top-tab state and the bottom-region
active-tab/collapsed state live in `Cockpit.tsx` — local state plumbing, **no router change**. The
existing Scorecard deep-link callsite (today `onNavigate("draft" | "spec")`) is migrated to the
object form `onNavigate({ top_tab })`.

### 4.2 New / changed components

- **`cockpit/BottomPanel.tsx`** — the region shell. `role="tablist"` tab strip (Inbox · Problems
  with count badges), a collapse/expand toggle (`aria-expanded`, collapsed state local). Hosts
  the two tab bodies. Accepts the active-tab + collapsed state and setters from `Cockpit`.
  **Badge sources:** the **Inbox** badge is the number of *visible* `prose_edit` rows the Inbox
  actually renders (both artifacts, after the existing visibility gate — not the raw
  `proposed-changes` length, which can include non-prose kinds, `db/models.py:1059`); the
  **Problems** badge is `counts.error + counts.warning`.

- **`cockpit/InboxPanel.tsx`** — the **relocated** inbox. Removes the inline `ProposalReview`
  from `DraftPanel`/`SpecPanel`. Shows **both** artifacts' pending `prose_edit` proposals
  **grouped by artifact** (Draft group, Spec group). Reuses the existing `lineDiff` rendering and
  the `useProposedChanges/useAcceptProposal/useRejectProposal` hooks (per artifact). Spec-group
  Accept stays **lock-gated** (derives `locked` from the spec content query, as today). Empty
  state: an explicit "Nothing pending" message (today `ProposalReview` returns `null`; the tab
  must never look broken). Accept/reject/stale-notice flow otherwise unchanged.

- **`cockpit/ProblemsPanel.tsx`** + **`api/problems.ts`** — `api/problems.ts` exposes
  `useProblems(id)` (`GET /problems`, polled). `ProblemsPanel` renders rows **grouped by
  category** with severity icons and a **→ act** control per row. "Act" passes the row's `target`
  straight to `onNavigate`: `{top_tab: "draft"}` for broken_citation / over_length, the criterion's
  mapped `{top_tab}` for criterion_gap (no-deep-link gaps carry `top_tab: "scorecard"`), and
  `{bottom_tab: "inbox"}` for coherence_drift. A row whose `target` is `null` renders **no** act
  control. Section-scroll stays a documented future enhancement (same as the Scorecard today). If
  the scorecard is stale, badge the criterion_gap group "may be outdated" using `scorecard_stale`.
  Empty state: a calm "No problems detected" (not an error).

- **`styles/cockpit.css`** — bottom-region layout, tab strip, collapse control, severity icons,
  problem-row + act styling. Calm-clinical palette, consistent with the existing cockpit CSS.

### 4.3 Error & loading states

- `ProblemsPanel` renders a fetch error inline (`role="status"`), never a blank panel (same
  pattern as the SP2b-3a `ScorecardPanel` start-error). The bottom region degrades gracefully if
  either tab's query errors.
- Loading shows a quiet placeholder, not a layout shift.

## 5. Error handling (summary)

- **Backend:** per-signal try-blocks; member-gating; always-well-formed 200; 403/404 as above.
  Citation audit is draft-only; an *absent* ledger still surfaces orphan markers and a *corrupt*
  ledger becomes one error row (§2.2, signal 1) — neither is silenced.
- **Frontend:** inline error/empty states on both tabs; spec Accept disabled while locked;
  accept-after-stale shows the existing notice and reloads.

## 6. Testing

**Backend (pytest):**
- `over_length` pure counter: over/under/at-budget; `budget <= 0` disables.
- offset→section_index mapper: marker in §0/§2/last; unresolvable offset → `null`.
- `broken_citation` builder: a fixture with an orphan_marker + a drift issue → mapped rows,
  severities preserved, section targets correct (`top_tab: "draft"`); **no ledger but markers
  present → orphan_marker *error* rows, not empty**; **corrupt `citations.json` → exactly one
  "ledger unreadable" error row** (not silent zero); no draft and no markers → empty.
- `criterion_gap` builder: scorecard with `gap`/`at_risk`/`adequate`/`strong` criteria → only
  gap(error)+at_risk(warning) rows, deep-links mapped to `top_tab`; **a gap with no deep_link →
  `top_tab: "scorecard"` fallback** (never `top_tab: null`); `scorecard_stale` passthrough; no
  scorecard → zero rows.
- `coherence_drift` builder: N pending completed-job coherence proposals → **exactly one**
  roll-up row (not N) with `bottom_tab: "inbox"`; 0 → none; pending-but-job-not-completed excluded.
- `aggregate`: ordering — errors before warnings, fixed category order, and **mixed
  `int`/`None` `section_index` does not raise and sorts nulls last** with title/detail tiebreak;
  `counts` tally; **defensive** — missing draft / absent scorecard → empty contribution, 200 not
  500 (the ledger cases above are the deliberate non-empty exceptions).
- endpoint: 200 shape; non-member → 403; unknown workspace → 404.

**Frontend (vitest/jsdom):**
- `BottomPanel`: tab switch, collapse/expand a11y (`aria-expanded`, `role="tab"`/`tablist`);
  badges — Inbox = visible `prose_edit` count, Problems = `error + warning`.
- `InboxPanel`: renders both artifact groups; spec Accept disabled when locked; accept/reject
  wiring; empty state; the badge counts only the visible `prose_edit` rows it renders.
- `ProblemsPanel`: groups by category; renders severities; "act" fires `onNavigate` with the
  row's `target` (broken_citation/over_length → `top_tab:"draft"`; **no-deep-link criterion_gap →
  `top_tab:"scorecard"`**; `coherence_drift` → `bottom_tab:"inbox"` + expand); a `null`-target row
  shows no act control; stale badge; empty + error states.
- `Cockpit`: bottom region mounts; widened `onNavigate` activates the Scorecard top-tab and
  focuses the inbox; the migrated Scorecard deep-link callsite still switches Draft/Spec; existing
  Draft/Spec/Scorecard behavior unchanged.
- Type gate `pnpm exec tsc --noEmit` (no TS linter). Schema regen after Task with the endpoint:
  `EMBEDDING_MODEL=bge-m3 pnpm gen:api`.

## 7. Build decomposition (~9 TDD tasks)

1. `problem_section_char_budget` setting in `config.py` + the `over_length` pure counter (+tests).
2. offset→section_index mapper + `broken_citation` signal builder over
   `validate_citations_deterministic` (+tests).
3. `criterion_gap` signal builder over the latest scorecard + stale passthrough (+tests).
4. `coherence_drift` single-roll-up builder (+tests).
5. `problems/aggregate.py` — compose the four signals defensively into
   `{problems, counts, scorecard_stale}`, with ordering + counts (+ defensive/empty tests).
6. `web/problems.py` GET endpoint + `main.py` mount + member-gating (+ endpoint tests).
7. Frontend `api/problems.ts` + `ProblemsPanel.tsx` (grouped, severities, act) (+vitest) +
   schema regen.
8. `BottomPanel.tsx` region shell (tabs, collapse, a11y) + `InboxPanel.tsx` relocation; remove
   inline `ProposalReview` from Draft/Spec panels (+vitest).
9. `Cockpit.tsx` wiring (mount bottom region, widen `onNavigate` for inbox focus) + `cockpit.css`
   (+ Cockpit vitest update).

## 8. Out of scope (later slices)

- **Uncited-claims signal** — `tools/audit.py:107` `list_unsupported_claims_impl` runs the audit
  LLM per uncited sentence. Belongs in its own slice as either an **on-demand action** ("Check
  for uncited claims") or a fourth background job — not the synchronous Problems endpoint.
- **Activity feed** — the third bottom-panel tab in the §9 region map. No event-log substrate
  exists on master (only the SSE live-update stream), so it is net-new aggregation; deferred to a
  dedicated slice once the event set is known.
- **Drag-resize / height persistence** of the bottom region.
- **Deep-link section-scroll** — Problems "act" switches the top-tab only (matches the Scorecard
  deep-link today); scroll-to-section is a shared future enhancement.
- **Citation proposals in the inbox** — still `prose_edit`-only, unchanged.
- **Convergent-mode emphasis** (Coverage/Readiness forward at spec-lock; Partner→Evaluator
  label) — carried in the overarching design, not this slice.
