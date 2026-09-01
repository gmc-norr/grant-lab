# UI polish pass and cockpit attention layer — design

**Date:** 2026-09-02
**Status:** design, approved in conversation 2026-09-02. Not implemented.
**Scope:** two independently plannable parts. Part 1 is a polish pass over the whole
SPA. Part 2 adds a state-derived "what needs attention" layer to the cockpit and
regroups the bottom panel. Each part gets its own implementation plan.
**Advances:** [Assistant-led IDE north-star](2026-08-15-assistant-led-ide-design.md)
Slice 1 (actionable chrome, labels, account menu) and Slice 2 (workbench panel
consolidation, counts that deep-link, empty/stale states). It does not replace that
document; where this spec is narrower, the north-star wins on intent.
**Does not touch:** the rail IA (`2026-08-28-workspace-rail-ia-design.md` decided the
side pane; Versions stay there), the collab editor, the permission matrix, or any
backend write path.

## 0. Evidence

A UI review on 2026-09-02 ran the built SPA against the real dev database (58 calls,
11 workspaces) and captured 29 screenshots: every route, every cockpit `?view=` and
`?tab=`, every bottom tab, at 1440×900 and 900×700. The cockpit was opened as a
**non-owner member** of workspace `312f51e2` ("Demo — AI-assisted digital pathology").
Findings below cite what the screenshots showed and the code that produces it.

The overall look is not the problem. The dark VS Code-style shell, teal accent, and
Mantine components are consistent across every screen. The problems are: the cockpit
shows everything and guides nothing; the chat hides what the agent did; empty panes
are dead ends; internal vocabulary leaks; and a dozen small chrome defects.

Parallel work checked: branch `fix/review-2026-09-02` (2 commits) touches
`tests/test_ingestion_e2e.py`, `tests/test_ingestion_upsert.py`, `uv.lock`, plus an
uncommitted `src/call_manager/ingestion/upsert.py`. No overlap with anything below.

---

## Part 1 — Polish pass

### 1.1 Goals

- Every screen names itself and every empty pane says what belongs there and offers
  one action (north-star law 8).
- No internal identifiers or enum values reach the user unlabelled.
- The chat transcript shows what the agent did.
- Chrome carries state, not decoration.

### 1.2 Non-goals

Light theme; mobile layouts; letting non-owner members message the agent (a
permission decision, see §4); moving Versions out of the side pane; any change to
API response shapes.

### 1.3 Items

Each item: what the screenshot showed → the change → files → tests that move.

**P1 — Chat shows the agent's work.**
Seen: the member's transcript showed one large green user bubble and a single muted
dot. Stored turns for that workspace include assistant `tool_use` blocks and a
`text` block. `TurnList.tsx:46` renders any block whose `type` it does not know as
`·`; only `role === "tool"` rows get the compact `⚙ name` line (`TurnList.tsx:22`).
Change: render an assistant `tool_use` block the same compact way as a tool row —
`⚙ search_references` — stripping the `mcp__proposal_assist__` prefix; keep `text`
blocks as prose. Unknown blocks still render `·` (never throw).
Files: `frontend/src/cockpit/chat/TurnList.tsx`, `TurnList.test.tsx`.
**Measure first:** `CockpitChat.tsx:64` merges every row from `/turns` by position and
`/turns` returns all rows unfiltered (`web/api/turns.py:list_turns`), yet only one
bubble and one dot were visible for 105 stored rows. The plan's first task is to
fetch `/turns` for that workspace and explain the gap before touching the renderer.

**P2 — Title bar names the page; account controls become a menu.**
Seen: "Funding calls" in the title bar on Profiles, Settings, and Admin
(`TitleBar.tsx:33` is the fallback for every non-cockpit route). Email chip and a
"Sign out" button sit loose at the right (`TitleBar.tsx:75-76`).
Change: route→label map — `/` and `/calls/:id` "Funding calls", `/profiles*`
"Ranking profiles", `/me` "Settings", `/admin*` "Admin", cockpit routes unchanged.
Replace chip + button with a Mantine `Menu` (target: the email, truncated): items
"Settings" (→ `/me`) and "Sign out". North-star §7.2.
Files: `shell/TitleBar.tsx`, `TitleBar.module.css`, `TitleBar.test.tsx` (the tests at
lines 34 and 59 change: label per route; sign-out is reached through the menu).

**P3 — Status bar carries state off the cockpit.**
Seen: outside the cockpit the 22px bar shows only "Grant Lab" (`StatusBar.tsx:125`);
the brand already sits in the title bar. Ranking progress ("Scoring N calls…",
"N calls couldn't be scored") is loose text above the call list (`Dashboard.tsx`).
Change: drop the brand label. Add a tiny `dashboardStatusStore` following the
`editorStore` pattern (`cockpit/editorStore.ts:23-32`, `subscribe`/`getSnapshot`):
`Dashboard` publishes `{pending, failed}` from its query; `StatusBar` renders
"Scoring N calls…" and "N unscored" off the cockpit, nothing when both are 0. The
loose text on the dashboard is removed (one home).
Files: `shell/StatusBar.tsx`, new `routes/dashboardStatusStore.ts`, `routes/Dashboard.tsx`,
`StatusBar.test.tsx` (line 34 "shows only the global label" changes), `Dashboard.test.tsx`.

**P4 — Unknown URL shows the app's 404 page.**
Seen: `GET /nope` returned the backend's `{"detail":"not found"}` JSON. The router
already has a soft-404 element (`router.tsx`, path `*`) that never renders because
the catch-all only serves `index.html` for manifest routes (`spa_routes.py:29`
`is_spa_route`).
Change, in `web/spa_serving.py::_spa_fallback` only: after the `/api/` short-circuit
and the 405 route walk, a `GET`/`HEAD` to a path that is neither a backend prefix
(`is_backend_route`) nor a manifest route returns `FileResponse(index, status_code=404)`.
Every other method, every `/api/*`, `/workspaces/*`, `/auth/*`, `/healthz`, `/static/*`
path, and missing `/assets/*` files keep today's JSON 404 / StaticFiles 404.
The Plan-2 contract "unknown → real 404" holds: the **status** stays 404; only the
body changes for browser page loads. Existing assertions at `tests/test_spa_serving.py:70`
(GET), `:81` (HEAD), `:86-88` (POST/DELETE/`/api`) stay green unchanged.
New tests: GET `/totally-unknown` body contains the shell; POST does not;
`/workspaces/unknown` GET is JSON; `/assets/missing.js` still lacks the shell.
Files: `src/call_manager/web/spa_serving.py`, `tests/test_spa_serving.py`. The
docstring in `spa_routes.py` that says unknown paths "fall through to FastAPI's real
404" is updated to say "404 status with the shell body on GET/HEAD".

**P5 — Library upload uses a real control.**
Seen: a raw `Choose File / No file chosen` browser input — the only unstyled control
in the app (`LibraryPanel.tsx:114` and `:201`).
Change: Mantine `FileButton` wrapping a `Button` ("Add PDF…") for both inputs; the kind
`NativeSelect` and the "Drop PDFs here" zone stay. Same `aria-label`s so
`LibraryPanel.test.tsx` queries survive.

**P6 — No internal words.**
- Profile kind/level: `ProfilesList.tsx:50` and `ProfileDetail.tsx:52-53` print raw
  `organizational_unit` / `group`. Valid values are pinned in the DB
  (`db/models.py:273` kind ∈ {organizational_unit, individual}; `:277` level ∈ {lab,
  group, department, clinic, institution, other}). Label maps already exist twice
  (`ProfileNew.tsx:9,:58`, `ProfileEdit.tsx:9`). Change: one `routes/profileLabels.ts`
  exporting `kindLabel()` / `levelLabel()`; all four sites use it.
- References pane: `ReferencesPane.tsx:18-21` prints the citekey **before** the title,
  and two of the demo workspace's four references have an empty title, so a row can be
  just `ref-646cabcf05ce`. The citekey is functional (it is the marker used in the
  draft text), so it stays. Change: title first; empty title renders
  "Untitled — metadata not approved"; citekey as a muted second line.
- Version subjects: `VersionGraph.tsx:155,:165` print the raw git subject. Measured in
  the demo repo (`var/workspaces/312f…/repo`): prefixes `cite:` ×7, `agent:` ×5,
  `user edit:` ×2, `phase0:` ×1. Live producers found by grep: `cite: add …`
  (`citations/core.py:635`), `cite: build bibliography` (`citations/bibliography.py:630`),
  `phase0: initialize empty artifacts` (`phases/setup.py:471`), `collab checkpoint: {artifact}`
  (`collab/checkpoint.py:213,:222`). **No current producer of `agent:` or `user edit:`
  was found** (`proposals/accepting.py:221` says the user-edit subject is deferred);
  those rows are historical. Change: a `versionLabel(subject)` map for the known
  prefixes — `cite:` → "Added citation …", `collab checkpoint:` → "Checkpoint (draft)",
  `phase0:` → "Workspace created", `agent:` → "Assistant edit", `user edit:` →
  "Edit: …". Unknown prefixes render the raw subject. The raw subject is always in
  the row's `title` attribute; the `aria-label` at `:142` keeps `sha7 + subject`.

**P7 — Empty panes teach their purpose (law 8).** Exact copy and the one action:

| Pane | Today | New copy | Action |
|---|---|---|---|
| Scorecard (`ScorecardPanel.tsx:81`) | "No evaluation yet — Re-evaluate to generate a scorecard." | "No scorecard yet. Evaluate the draft against the rubric criteria." | existing button, labelled "Evaluate" when there is no scorecard, "Re-evaluate" otherwise |
| Panel review (`ReviewPanel.tsx:220`) | "No panel review yet — run a full mock-panel review to get started." | "No panel review yet. A mock panel reads the whole draft the way reviewers will. Uses cloud budget." + the budget line already returned by `/review-runs/latest` | existing button |
| Criteria (`CriteriaPane.tsx:41`) | "No criteria yet." | owner: "No criteria yet. Criteria come from the rubric review." · member: "The owner sets criteria in the rubric review." | owner: link to the rubric editor |
| Members (`MembersPane.tsx`) | "2 members" | "2 members. Only the owner can add or remove members." (members see the count only — deliberate, non-PII) | owner: existing add form |
| Inbox (`InboxPanel.tsx:28`) | "Nothing pending." | "Nothing pending. Ask the assistant for an edit, or select text and choose Edit. Proposals land here for you to accept or reject." | none |
| Claims (`ClaimsPanel.tsx:83`) | "No open findings" | "No uncited claims in this section." | none |
| Citations (`CitationsPanel.tsx:25,:80`) | already descriptive | unchanged | — |

**P8 — Dashboard.**
- The score pill (`MatchBadge.tsx:8`: ≥80 strong, ≥60 good, else low) gets a Mantine
  `Tooltip` and `aria-label`: "Match 100 · strong fit. Scored by the local model
  against the profile's description and rubric." Bands keep their names.
- `DashboardHeader.tsx:27` "1 top matches" → pluralised ("1 top match").
- `CallRow.tsx:55` "Start drafting" on every row → shown only on the selected row and
  on hover/focus-within. The reading pane keeps its full-width button, so keyboard
  users lose nothing. `CallRow.test.tsx` gains a selected/unselected assertion.

**P9 — Small fixes.**
- `ProfilesList.tsx:28` "+ New profile" clips its last letter → give the button
  `px="md"` and no fixed width.
- `WorkspaceCreate.tsx:45` glues the form to the left edge → `Center` + `Paper`
  card, same shell as Login (`PreAuthLayout`).
- Narrow widths (seen at 900×700: bottom tab bar wrapped to two rows; chat became a
  sliver). `BottomPanel.tsx:55-70` `Tabs.List` → `flexWrap: nowrap; overflowX: auto`.
  `AppShell.tsx:150-155` right pane → on first mount with no persisted `cm-shell`
  layout and `window.innerWidth < 1100`, start collapsed. Persisted layouts win.

**P10 — Verify before changing: date format.**
`WorkspaceCreate.tsx:61` is a native `datetime-local`; the screenshot showed
`mm/dd/yyyy` because headless Chromium ran with an `en-US` locale. In a `sv-SE`
browser the same input shows `åååå-mm-dd`. The plan checks this in a real browser;
if the app's users see the Swedish format, **no change**. (`@mantine/dates` is not
installed and `pnpm add` is under the 7-day cooldown, so a picker is not an option
this slice either way.)

### 1.4 Acceptance

- Every route shows its own title; sign-out works from the menu (TitleBar tests).
- Off the cockpit the status bar shows ranking progress or nothing; the dashboard no
  longer prints it twice.
- `GET /nope` → 404 status, shell body, router renders "Page not found";
  `POST /nope` and `GET /api/nope` → JSON 404. `tests/test_spa_serving.py` green.
- No visible raw `<input type="file">`: `grep -c 'type="file"' frontend/src/cockpit/LibraryPanel.tsx`
  returns 0 (measured today: 2). The third input in the app, `chat/Composer.tsx:109`,
  is `display: none` behind a styled "📎 Image" label and stays.
- No raw enum in the profile screens: `grep -n "{p.kind}" frontend/src/routes/ProfilesList.tsx frontend/src/routes/ProfileDetail.tsx`
  returns 0 hits (measured today: 1 each). `ProfileEdit.tsx:102` passes `kind={p.kind}`
  as a prop, not text, and is out of this gate.
- Every pane in the P7 table renders its new copy in `vitest`.
- `tsc --noEmit`, whole-suite `vitest`, `pnpm build`, and the Python suite pass.
- Screenshots of `/`, `/profiles`, `/me`, `/admin`, `/cockpit/new`, and the cockpit
  at 1440 and 900 wide, re-taken with the same script and compared by eye.

---

## Part 2 — Attention layer

### 2.1 Problem

The cockpit presents four artifact tabs, six bottom tabs, five rail views, and a chat
pane at once. Badges say "Problems 7" and "Claims 10" but nothing says which matters
first. North-star §11.1 asks for "one concise next-action banner" and §7.7 for a
panel that "starts collapsed unless there is pending attention". Neither exists.

### 2.2 Governing rule

> **Guidance derives from state, never from a declared stage.**
> (`notes/2026-07-07-proposal-workspace-product-review.md` §2, principle 1.)

There is no phase model and no wizard. The workspace answers "what is most valuable
to do next?" from live signals. Change the draft and the answer changes. Nothing is
dismissed by hand; an item disappears when its signal does.

### 2.3 Data source: one backend endpoint

`GET /api/workspaces/{id}/attention` — member-gated (`require_member`), read-only, no
LLM, no new table, no caching. Same shape of module as Problems
(`proposal_assist/web/problems.py:24` + `problems/aggregate.py`): a pure
`assemble_attention(...)` in `proposal_assist/attention/aggregate.py`, a thin router in
`proposal_assist/web/attention.py`, registered next to `problems_router`
(`main.py:847`).

Response:

```json
{
  "items": [
    {
      "kind": "problems_error",
      "severity": "error",
      "title": "Fix 3 problems",
      "detail": "3 broken citations",
      "count": 3,
      "target": { "top_tab": null, "bottom_tab": "checks", "filter": "errors",
                  "section_index": null, "view": null },
      "owner_only": false
    }
  ],
  "computed_at": "2026-09-02T12:00:00Z"
}
```

`severity` ∈ {error, warning, info} — the Problems vocabulary (`problems.ts:13`) plus
`info`. `target` is the existing `NavTarget` (`cockpit/nav.ts:6`) extended with
`view` (a side-pane view) and `filter` (a Checks filter, §2.5).

Rules, evaluated in this order; every rule that fires contributes one item; the list
is returned in this order:

| # | kind | severity | fires when | signal (measured source) | target |
|---|---|---|---|---|---|
| 1 | `setup` | warning | `current_phase == "setup"` | `Workspace.current_phase` | none (cockpit shows `WorkspaceSetup`) |
| 2 | `deadline_passed` | error | `deadline < now` | `Workspace.deadline` (workspace-level only, as `cockpit-meta` reads it — `web/api/workspaces.py:1251`) | none; owner sees "Edit deadline" (settings modal) |
| 3 | `deadline_soon` | warning | `0 ≤ days_left ≤ ATTENTION_DEADLINE_SOON_DAYS` | same | same |
| 4 | `pending_changes` | warning | pending proposals > 0 | the exact query `list_proposed_changes` uses, including its §3.3 coherence-visibility gate (`web/proposals.py:58-80`) — factor it into a shared helper, do not copy it | `bottom_tab: "changes"` |
| 5 | `problems_error` | error | `counts.error > 0` | `assemble_problems` (`problems/aggregate.py`) | `bottom_tab: "checks", filter: "errors"` |
| 6 | `scorecard_missing` / `scorecard_stale` | info | no completed evaluator run / `stale` true | the scorecard staleness already computed in `web/evaluator.py:130-131` (factor into a helper) | `top_tab: "scorecard"` |
| 7 | `problems_warning` | warning | `counts.warning > 0` | same as 5 | `bottom_tab: "checks", filter: "warnings"` |
| 8 | `review_missing` / `review_stale` | info | no completed review run / `stale` true | `web/review.py` latest-run staleness (factor into a helper) | `top_tab: "review"`; `owner_only: true` (it spends budget) |
| 9 | `all_clear` | info | none of the above fired | — | none |

Rule 4 outranks rule 5 on purpose: pending proposals are often the fix for the
problems, and reviewing them is cheap. Rules 6 and 8 are `info` because they cost
time or money and nothing is broken. Rule 9 guarantees the list is never empty, so
the strip always has one line ("Nothing needs attention right now.").

Cost: one `assemble_problems` (git reads) per call, the same as `/problems`. The
frontend polls both at the same 15 s interval (`api/problems.ts:43`), so an open
cockpit does two such reads per 15 s. Accepted for this slice; if measured cost
matters, a later slice folds the problems rows into the attention response.

Settings: `ATTENTION_DEADLINE_SOON_DAYS: int = 14` in `config.py`, placed next to
`problem_section_char_budget` (`config.py:497`). The same threshold lives in the
frontend for the calls list (`routes/Deadline.tsx:13-14`, 14 and 30 days). This is a
deliberate duplication: calls have no workspace and no backend deadline view. Both
sites get a one-line comment naming the other. `scripts/gen_architecture.py:126`
lists only settings whose name contains `cap`, `max_`, `_limit`, or `budget`, so this
setting does not change `docs/ARCHITECTURE.md`; the plan runs the generator anyway
and commits whatever it emits.

### 2.4 The strip

`cockpit/AttentionStrip.tsx`, mounted in `routes/Cockpit.tsx` between the artifact
`Tabs.List` and the editor scroll area, full width, one line (28 px), always
reserved so the editor never jumps.

- Content: severity icon (`codicon-error` red, `codicon-warning` yellow,
  `codicon-info` muted — tokens `--app-bad`, `--app-warn`, `--app-muted`), the top
  item's `title`, its `detail` in muted text, and a "Go" link when `target` is set.
  When more than one item: "and N more ▾" opens a Mantine `Popover` listing the rest,
  each row clickable.
- "Go" calls the existing `onNavigate(NavTarget)` (`Cockpit.tsx`), which already
  switches the top tab, section, and bottom tab and expands the bottom panel. It
  gains two branches: `view` → set `?view=` (rail view, via `cockpitNav`); `filter` →
  set the Checks filter (§2.5).
- `owner_only` items render for members without the "Go" link and with the detail
  "Ask the owner" appended.
- States: first load → "Checking…" muted; error → the row stays but reads "Attention
  unavailable" muted (never blocks, never throws); `all_clear` → the info icon and the
  sentence, no link.
- No dismiss. No local state beyond the popover.

### 2.5 Bottom panel regroup (north-star §7.7)

| Today (`BottomPanel.tsx:64-69`) | New group | Contents |
|---|---|---|
| Inbox | **Changes** | proposed changes (`InboxPanel`) |
| Problems, Citations, Claims | **Checks** | `ProblemsPanel`, `CitationsPanel`, `ClaimsPanel` behind a filter chip row: All · Problems · Claims · Citations. Chip "errors"/"warnings" are the same view with the problems rows filtered by severity. |
| Library | **Sources** | `LibraryPanel` |
| Activity | **History** | `ActivityPanel` |

Versions stay in the side pane (rail IA spec decision); History here is the activity
feed only. This is a deliberate narrowing of §7.7, which lists versions under History.

Badges (law 2 and §7.7 "counts encode severity"): Changes shows the pending count in
accent (`--app-accent`); Checks shows `counts.error` red and `counts.warning` yellow
as two small numbers, from the same `/problems` response the badge uses today
(`BottomPanel.tsx:31-32`); the separate claims and audit counts are dropped from the
tab (they are filters inside Checks, and `assemble_problems` already includes
uncited claims and broken citations — `problems/aggregate.py:29-36`). Sources and
History have no badge.

Vocabulary change, one meaning everywhere (law 6):
- `BottomTab` (`cockpit/nav.ts:3`) becomes `"changes" | "checks" | "sources" | "history"`.
- `ProblemTarget.bottom_tab` (`api/problems.ts:7`) becomes `"changes" | "checks" | null`.
- Backend producers change to match: `problems/builders.py:185` (`"inbox"` →
  `"changes"`), `:208` (`"problems"` → `"checks"`). Measured pins to update: 13 string
  hits across `tests/proposal_assist/problems/*.py`; 13 in `BottomPanel.test.tsx`; 7 in
  `Cockpit.test.tsx`.
- The OpenAPI-generated types are regenerated (`pnpm gen:api`; export
  `EMBEDDING_MODEL=bge-m3` first, see memory).

Default state (§7.7): the panel starts **collapsed** (`Cockpit.tsx:98-103` keeps
`collapsible`, `collapsedSize=6`) unless (a) the persisted `cm-cockpit-vertical`
layout says open, or (b) the attention list's first item targets the panel with
severity `error` or kind `pending_changes`. While mounted, a **new** first item that
targets the panel expands it (through the existing `bottomRef.expand()`), selects
the targeted group and filter, and never moves keyboard focus. Collapsing by hand
persists as today.

The bottom group and filter become addressable from outside `Cockpit` through a
small `panelStore` (same pattern as `editorStore`), so the status bar can open
Checks. `?tab`/`?sec`/`?view` stay in the URL as today; the panel state does not
enter the URL.

### 2.6 Status bar (north-star §7.8)

`CockpitStatusItems` (`shell/StatusBar.tsx`) adds "N issues" from the `/problems`
counts already fetched by the panel (shared query key `["problems", id]`); red when
`error > 0`, yellow when only warnings, hidden at 0. Click → `panelStore.open("checks")`.

### 2.7 Data flow

- `api/attention.ts`: `useAttention(id)` — `queryKey ["attention", id]`,
  `refetchInterval 15000`, refetch on window focus, `enabled: !!id`.
- `api/invalidateArtifact.ts:37-39` gains `qc.invalidateQueries({ queryKey: ["attention", id] })`
  so a save, accept, or reject refreshes the strip with the artifact.
- The strip, the panel default, and the status bar all read the same two queries
  (`attention`, `problems`). No third source.

### 2.8 Tests

Backend (`tests/proposal_assist/attention/`), modelled on
`tests/proposal_assist/problems/test_aggregate.py` and `test_web_problems.py`:
- One test per rule 1–8: build the signal, assert the item is first **and** assert
  that removing the signal removes the item (the negative is the gate).
- Ordering: two signals present → the lower-numbered rule is first.
- Rule 9: nothing set → exactly one `all_clear` item.
- Rule 4 uses the shared helper: a coherence-session proposal whose job is not
  `completed` is not counted (the §3.3 gate).
- Web: 403 for a non-member, 404 for a soft-deleted workspace, `owner_only` set on
  `review_*` items.

Frontend (`vitest`):
- Strip renders title/detail/link for each severity; `owner_only` as member has no
  link; error state renders the muted row; `all_clear` renders the sentence.
- "Go" with `bottom_tab` expands the panel and selects the group and filter;
  `view` sets `?view=`.
- Panel default: collapsed with `all_clear`; open with a first `problems_error`;
  persisted-open wins over `all_clear`.
- Status bar "N issues" opens Checks.
- Existing `BottomPanel.test.tsx` / `Cockpit.test.tsx` updated for the new tab names.

### 2.9 Acceptance

- `GET /api/workspaces/{id}/attention` documented in `/openapi.json`; types regenerated;
  `tsc --noEmit` clean.
- Opening the demo workspace as a member shows, in the strip, "Fix N problems" with a
  working "Go" that opens Checks filtered to errors, and the panel opens by itself.
- Opening a clean workspace shows "Nothing needs attention right now." with the
  panel collapsed.
- `grep -rn 'bottom_tab.*"inbox"\|bottom_tab.*"problems"' src/call_manager/proposal_assist/problems`
  and `grep -n '"inbox"' frontend/src/cockpit/nav.ts` both return 0 hits (measured
  today: `builders.py` 3 — the docstring at line 12 plus lines 185 and 208 — and
  `nav.ts` 1). The response key `"problems"` in `aggregate.py:171` is not part of
  this vocabulary and stays.
- Full Python suite and whole-suite `vitest` green; `pnpm build`; ARCHITECTURE.md
  regenerated in the same commit.

### 2.10 Deferred (each needs its own spec)

- Margin rendering of proposals and problems inside the editor (the 2026-07-07
  review's "single highest-leverage UX change"). Declined for this slice on
  2026-09-02: it touches the collab schema and anchors.
- Layout presets and the responsive drawer model (north-star §10, §14).
- Section-level status indicators (north-star Slice 2, first bullet).
- Folding the problems rows into the attention response.

---

## 3. Sequencing

1. Part 1 first, as one plan. It is frontend-only except P4 and touches no cockpit
   tab names, so it cannot collide with Part 2 or with the ingestion bugfix branch.
2. Part 2 as a second plan after Part 1 merges, because Part 2 renames the tabs whose
   empty states Part 1 rewrites (P7). Landing them in the other order edits the same
   lines twice.
3. Both plans re-run the screenshot script from the 2026-09-02 review (scratchpad
   `shots.mjs`; recipe in memory `reference_spa_visual_check`) as their last task and
   attach the images to the PR description.

## 4. Open decisions recorded, not taken here

- **Members and the agent.** `CockpitChat.tsx:90` tells a non-owner member "only the
  owner can message the agent", while the same member can edit the text directly.
  The 2026-07-07 review flagged the matrix as pre-collaboration. It interacts with
  spend caps and the disclosure gate, so it is a separate decision.
- **Deadline fallback.** `cockpit-meta` deliberately reads the workspace deadline and
  not the linked call's (`web/api/workspaces.py:1251`). The attention rules follow
  that. Whether a workspace should inherit the call deadline at creation is a
  `WorkspaceCreate` question, not an attention question.
