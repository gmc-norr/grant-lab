# Collab Slice 3b-5d·D1b — Stale-Snapshot 409 Concurrency Fix (Design)

**Date:** 2026-07-07
**Slice:** 3b-5d·D1b (second sub-slice of D1; sibling to the merged D1a client-only fixes).
**Status:** Design approved; ready for implementation plan.
**Parent:** `docs/superpowers/specs/2026-07-03-collab-slice3b1-server-hardening-design.md` (the 3b-5d flip family).
**Depends on:** D1a (merged `d71d0e9`) — the toolbar write-actions now enable reliably, which is what makes this stale-snapshot path reachable at all.

---

## 1. Goal

Eliminate the **spurious `409`/stale** a live-collab writer hits when they Cite or Edit a
section that has any *unsaved* live content — i.e. the live Yjs room is ahead of git HEAD.
This is the deferred 3b-5a **C#3** ("toolbar reads a cached git-backed snapshot, not the live
doc"). Preserve *genuine* concurrent-edit conflict detection on the Edit path. This is a
**3b-5d flip prerequisite**: as-is, Cite/Edit are unusable in an active collab session because
the room is almost always ahead of git.

## 2. Background — the real root cause

The Ask/Edit/Cite toolbar derives `base_section_sha` from `useDraft` — the **git-HEAD**
read-model (`DraftPanel.tsx:42`). But the live Yjs room only advances git HEAD on a
**checkpoint**, so after *any* live edit — **including the writer's own** — the git sha no
longer equals the live section sha. Two server-side gates compare that stale git sha against
the **live** room and reject:

- **Cite (direct):** `apply_citation_to_room` (`collab/kernel.py:92`) —
  `if live.sha != base_section_sha: raise CiteStale` → `409`.
- **Edit (submit):** `build_selection_block` (`turns/selection.py:47`) —
  `verified = live.sha == base_section_sha`; an unverified `modify` raises
  `SelectionConflictError` → `409` (`turns.py:171`) **before the agent even runs**. When collab
  is on, `turns.py:150-156` builds `sections` from `canonical_artifact_markdown` (the live
  room), so this compares git-sha vs live-sha.

Everywhere else the system **already** baselines against the live room and is correct:

- The agent's read snapshot: `read_proposal_impl` reads `canonical_artifact_markdown` when
  collab is on (`tools/proposal.py:71-101`), so a proposed prose edit's `base_section_sha` is
  **live-at-agent-read**.
- Accept-time: `kernel_accept_prose` (`kernel.py:171`) compares `live.sha` (accept) vs
  `pc.base_section_sha` (agent-read live) → a genuine **live-vs-live** conflict window.
- Inbox stale-badge: `refresh_staleness` reads room-canonical live sections when collab is on
  (`staleness.py:31-37`) → live-vs-live.

So the bug is isolated to the **two toolbar-input gates** that still trust a client git sha.
The Cite and Edit paths are asymmetric and must be treated differently:

- **Cite** inserts an *additive marker at an anchor*. Anchor-presence-and-uniqueness is the
  semantically correct check; the sha-equality is redundant.
- **Edit** replaces the *whole section* with the proposal's `new_text` on accept, so a section
  that genuinely changed between propose and accept **is** a lost-update conflict — that gate
  is load-bearing and stays. The *spurious* part is only the **submit-time** git-baseline
  rejection, which happens before the agent runs.

## 3. Scope

**In:**
- Cite direct path: relax the git-sha-equality gate to anchor-relocation (kernel).
- Edit submit path: relax `build_selection_block`'s modify gate to anchor-relocation, collab
  branch only.
- Unit + integration + one e2e acceptance scenario.

**Out (unchanged / other slices):**
- Edit **accept-time** conflict gate (`kernel_accept_prose`) — already live-vs-live; correct.
- Inbox `refresh_staleness` — already live-vs-live; correct.
- Flag-off human-cite path (`insert_citation_for_user`) and flag-off section-save endpoints
  (`post_save_section`, `post_save_spec_section`) — not toolbar paths.
- Frontend toolbar enable-gate (Approach 3 polish) — deferred; the rare live-vs-git anchor
  ambiguity edge fails safe with a clear message.
- Structure ops (section add/move/reorder) — roadmap #3.

## 4. Invariant this slice establishes

> **git HEAD is never a staleness baseline for a live collab session.** The only two places
> still comparing a client-supplied git sha against a live section become anchor-relocation
> against the live section — the check they should have used all along.

Backend-only. Flag stays **OFF**. Flag-off behavior is **byte-for-byte** preserved (both new
parameters default to the current behavior). No schema, no migration.

## 5. Component A — Cite direct path

**File:** `src/call_manager/proposal_assist/collab/kernel.py` — `apply_citation_to_room`.

- Add parameter `enforce_section_sha: bool = True`.
- Guard the existing equality:
  `if enforce_section_sha and live.sha != base_section_sha: raise CiteStale(...)`.
- `direct_cite_via_kernel` (`citations/collab_cite.py:61`) calls with
  `enforce_section_sha=False` (it already passes `require_anchor_in_section=True`).
- `kernel_accept_citation` (the inbox pc-accept path) keeps the default `True` → its
  terminal-stale parity is unchanged.

**Why this is safe without the sha check.** The direct path already relies on:
1. `require_anchor_in_section=True` → `anchor_occurrences(live.text, anchor)` proves the anchor
   is present **in the selected section** before the whole-doc `insert_marker` (guards against a
   whole-doc-unique anchor that actually lives in *another* section).
2. `compute_citation_insert` maps a non-unique whole-doc anchor to `anchor_ambiguous` →
   `CiteInvalid` → `422`, and a missing ref to `ref_not_found` → `404`, and a moved/gone anchor
   to `anchor_not_found` → `CiteStale` → `409`.

Together these prevent mis-citing; the section-sha equality added nothing but the spurious
rejection. `base_section_sha` is still accepted from the client (ignored on this path) — **no
API/schema change**.

## 6. Component B — Edit submit path

**Files:** `src/call_manager/proposal_assist/turns/selection.py` — `build_selection_block`;
`src/call_manager/web/api/turns.py` (the collab branch call site).

- Add parameter `allow_anchor_relocation: bool = False` to `build_selection_block`.
- `turns.py` passes `allow_anchor_relocation=True` in the collab-draft branch (the same branch,
  `turns.py:150-156`, that already sets `sections` from `canonical_artifact_markdown`).
- When `allow_anchor_relocation` is `True`, **ignore the git sha/offsets** and relocate by the
  `anchor` string in `live.text` (`live = sections[sel.section_index]`):
  - **exactly one** occurrence at code-point index `p`: build a **verified** block with
    recomputed `from_offset=p`, `to_offset=p + len(anchor)`, `heading=live.heading`, for both
    `ask` and `modify`. Still enforce the `MAX_SELECTION_CODEPOINTS` span cap
    (`SelectionConflictError("selection is too long — select a smaller span")`).
  - **zero** occurrences: `modify` → `SelectionConflictError("the text you selected is no longer
    in this section — reselect")`; `ask` → the existing **unverified** block (leniency
    preserved).
  - **multiple** occurrences: `modify` → `SelectionConflictError("your selection appears more
    than once — select a more specific range")`; `ask` → unverified block.
- When `allow_anchor_relocation` is `False` (default; non-collab and flag-off turns), the
  method behaves **exactly** as today (sha-equality + exact-offset validation).

**Downstream unchanged.** A submitted `modify` still flows agent → live read →
`pc.base_section_sha` = live-at-read → accept-time `kernel_accept_prose` remains a genuine
live-vs-live conflict gate. D1b changes only the *submit* gate.

**Anchor shape & uniqueness.** The toolbar's `anchor` is the **literal selected text** —
`deriveClaimAnchor` returns `doc.textBetween(from, to)` (`selectionAnchor.ts:27`), so
`anchor == section.text[from:to]` by construction *but it may be as short as a single word* (the
D1a e2e selects `diagnostic`). Relocation is therefore exact-substring: unique when the selection
is distinctive, but a short/repeated selection can match multiple times and degrades to the
"appears more than once" `409`. That is strictly better than today (which `409`s **always** once
the room is ahead of git) and a clearer message than the old opaque "save and reselect" — but it
is weaker than exact-offset disambiguation. Precise disambiguation (offset-proximity, or the
frontend sending live offsets) is the deferred **Approach-3** polish, not D1b. Tests must cover a
**unique short span** (not only a long sentence) plus the ambiguous-short-span `409`.

## 7. Parity — what stays unchanged

| Path | Mechanism | D1b |
|------|-----------|-----|
| Flag-off any turn | `build_selection_block(allow_anchor_relocation=False)` | byte-for-byte |
| Flag-off human cite | `insert_citation_for_user` (`_git` branch) | untouched |
| Inbox pc-accept cite | `kernel_accept_citation` → `enforce_section_sha=True` | terminal-stale unchanged |
| Edit accept-time | `kernel_accept_prose` live-vs-live gate | unchanged |
| Inbox stale-badge | `refresh_staleness` room-canonical | unchanged |
| Flag-off section save | `post_save_section` / `post_save_spec_section` | untouched |

## 8. Error semantics (flag-on)

HTTP mappings are **unchanged**; only the spurious-trigger frequency drops.

- Cite direct (`post_citation` → `direct_cite_via_kernel`): `404` ref-not-found, `409`
  genuine anchor-gone/moved, `422` ambiguous / bad chunk / quote / stance.
- Edit submit (`post_turn`): `409` `SelectionConflictError` — now only genuine anchor-gone or
  anchor-ambiguous, never live-ahead-of-git.

## 9. Edge cases (documented; all fail-safe)

- **Toolbar enable-gate uses git uniqueness.** A live-ambiguous-but-git-unique anchor can enable
  a button the backend then `422`/`409`s. Rare; clear message; Approach-3 FE polish deferred.
- **Structure change between read and action** (section add/remove/reorder) can shift
  `section_index` → presence check fails → `409`/stale. Correct fail-safe; roadmap #3.
- **Within-section duplicate anchor** on the Cite path: `require_anchor_in_section` is a presence
  check, but whole-doc ambiguity → `422` already covers duplicates (count ≥ 2 anywhere). No gap.

## 10. Testing

**Unit (pure-Python, no DB) — `build_selection_block`:**
- collab `modify`, live ahead of git (sha mismatch), anchor is a **unique short span (single
  word)** → **verified block with recomputed live offsets** (the regression test; a short span,
  not only a long sentence — the real toolbar anchor can be one word).
- collab `modify`, anchor gone → `409` "no longer in this section".
- collab `modify`, anchor appears twice in the live section → `409` "appears more than once".
- collab `ask`, anchor gone → unverified block, **no** `409`.
- flag-off `modify` with a stale git sha → still `409` (parity, `allow_anchor_relocation=False`).

**Route/integration (Edit call-site wiring; Codex Medium) — `POST /workspaces/{id}/turns`:**
Collab-enabled draft, live room ahead of git, submit a `modify` selection carrying a **stale git
`base_section_sha`** → `200` and the stored selection block is `verified` with **recomputed live
offsets**. A unit test on `build_selection_block` alone cannot catch a plan that implements
`allow_anchor_relocation` but forgets to pass it from the collab branch at `turns.py:150`; this
test pins that wire.

**Integration (testcontainers PG + live room) — Cite kernel / `direct_cite_via_kernel`:**
- flag-on, section co-edited in the room so git-sha ≠ live-sha, anchor still present-and-unique →
  direct cite **succeeds** (the regression test; was `409`).
- flag-on, anchor gone from the section → `409`.
- flag-on, anchor ambiguous whole-doc → `422`.
- `kernel_accept_citation` (pc path) still terminal-stale on sha-mismatch (parity;
  `enforce_section_sha` default `True`).
- flag-off `insert_citation_for_user` path unaffected (existing tests green).

**e2e acceptance (reuse the 2-user harness) — new `frontend/e2e/collab.spec.ts` scenario (e),
skip-gated:** owner + member both edit the draft (so live ≠ git), then the owner cites a
still-present claim **without a save-first** → composer → add citation → the `citation-pill`
converges to both clients. This is the inverse of D1a scenario (c), which deliberately
checkpoints to a git==live base to *avoid* this path; (c) stays as the clean-base proof.

**Gates:** pytest with `EMBEDDING_MODEL=bge-m3`, grouped `tests/proposal_assist/` **always
including `tests/test_api_spec.py`**, `-p no:cacheprovider` (never the whole suite —
testcontainers PG drops under load); `ruff check .` (not `ruff format`); frontend
`pnpm exec tsc --noEmit` + `pnpm exec vitest run`.

## 11. Global constraints (verbatim)

- Feature flag **stays OFF** in prod until the explicit 3b-5d flip (D4).
- **Flag-off parity is a HARD gate** — byte-for-byte; both new params default to old behavior.
- **No migration, no schema change** for collab slices.
- Backend changes are additive; the two new params are keyword-only with safe defaults.
- Explicit-path `git add` only (never `-A`); never touch master/main from a worktree; merges
  are **local `--no-ff` only**, not pushed without an explicit ask.
- Do **not** invoke `/code-review` / `/ultrareview` (user-billed); Codex second-opinion findings
  are authoritative-but-verify-against-code.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## 12. Key references

- `src/call_manager/proposal_assist/collab/kernel.py` — `apply_citation_to_room` (Component A),
  `kernel_accept_prose` (accept-time gate, unchanged), `kernel_accept_citation` (pc parity).
- `src/call_manager/proposal_assist/citations/collab_cite.py` — `direct_cite_via_kernel`.
- `src/call_manager/proposal_assist/turns/selection.py` — `build_selection_block` (Component B).
- `src/call_manager/web/api/turns.py:146-172` — collab-branch call site.
- `src/call_manager/proposal_assist/tools/proposal.py:71-101` — agent live read (why accept is
  live-vs-live).
- `src/call_manager/proposal_assist/proposals/staleness.py:31-37` — inbox live baseline.
- `frontend/src/collab/TiptapSelectionToolbar.tsx` — toolbar `base_section_sha` derivation.
- `frontend/src/cockpit/DraftPanel.tsx:42` — git-HEAD `useDraft` source of the stale sha.
