# Cockpit SP2b-3b — Problems Panel + Bottom-Panel Region — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a collapsible bottom-panel region (Inbox · Problems tabs), relocate the proposals inbox into it, and add a read-only Problems dashboard that aggregates four cheap synchronous signals (broken citations, criterion gaps, over-length sections, coherence drift).

**Architecture:** A pure-function signal layer (`proposal_assist/problems/builders.py`) + a defensive orchestrator (`problems/aggregate.py`) that reads the draft, ledger, latest scorecard, and pending coherence count, behind one member-gated synchronous endpoint `GET /api/workspaces/{id}/problems`. **No new table, job, or migration — Alembic head stays `0054`.** Frontend: a `BottomPanel` shell hosting a relocated `InboxPanel` (reusing the existing `ProposalReview`) and a new `ProblemsPanel`, wired into `Cockpit.tsx` via a two-axis `onNavigate({top_tab, bottom_tab})`.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / pytest (+ testcontainers); React 18 / Vite / TanStack Query / openapi-fetch / Vitest. `uv` + `ruff` backend; `pnpm` frontend (type gate `pnpm exec tsc --noEmit`, **no TS linter**).

**Spec:** `docs/superpowers/specs/2026-06-22-cockpit-sp2b-3b-problems-panel-design.md`.

---

## Worktree & process (read first)

- **All work happens in the worktree** `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b` on branch **`cockpit-sp2b-3b`** (off master `6bcdcc0`). The MAIN tree is on a different branch lacking cockpit code — never edit/test/commit there. Begin every shell command by cd-ing into the worktree; before every commit verify `git branch --show-current` prints `cockpit-sp2b-3b`.
- **Backend tests:** `uv run pytest <paths> -q ; echo $?`. The WHOLE suite is testcontainer-flaky (intermittent `connection refused`); run only affected files, capture the exit code, never pipe pytest through `tail`. Re-run a `connection refused` failure in isolation before treating it as real.
- **Lint/format before each backend commit:** `uv run ruff check <files>` and `uv run ruff format <files>`.
- **Alembic:** `uv run alembic heads` must stay `0054 (head)` — this slice adds NO migration.
- **Frontend (Tasks 7-9):** `cd frontend`; `pnpm install` if needed; tests `pnpm test <file>`; type gate `pnpm exec tsc --noEmit`. Regenerate the OpenAPI client AFTER Task 6's endpoint exists with `EMBEDDING_MODEL=bge-m3 pnpm gen:api` (the default `.env` model logs a stdout WARNING that corrupts the generated file).
- End every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## File Structure

**Backend (new):**
- `src/call_manager/proposal_assist/problems/__init__.py` — re-exports `assemble_problems`.
- `src/call_manager/proposal_assist/problems/builders.py` — pure signal builders + `_row` + ordering/counting helpers.
- `src/call_manager/proposal_assist/problems/aggregate.py` — `count_pending_coherence_proposals` + `assemble_problems` (defensive orchestrator).
- `src/call_manager/proposal_assist/web/problems.py` — the GET endpoint.

**Backend (modified):**
- `src/call_manager/config.py` — add `problem_section_char_budget`.
- `src/call_manager/main.py` — import + mount `problems_router`.

**Backend tests (new):**
- `tests/proposal_assist/problems/__init__.py`
- `tests/proposal_assist/problems/test_builders.py` (Tasks 1-4)
- `tests/proposal_assist/problems/test_aggregate.py` (Task 5)
- `tests/proposal_assist/problems/test_web_problems.py` (Task 6)
- `tests/proposal_assist/evaluator/test_config.py` — add one assertion (Task 1).

**Frontend (new):**
- `frontend/src/cockpit/nav.ts` — shared `NavTarget`/`OnNavigate` types.
- `frontend/src/api/problems.ts` — `useProblems` + types.
- `frontend/src/cockpit/ProblemsPanel.tsx` (+ `.test.tsx`).
- `frontend/src/cockpit/BottomPanel.tsx` (+ `.test.tsx`).
- `frontend/src/cockpit/InboxPanel.tsx` (+ `.test.tsx`).

**Frontend (modified):**
- `frontend/src/cockpit/DraftPanel.tsx`, `frontend/src/cockpit/SpecPanel.tsx` — remove the inline `<ProposalReview>` blocks.
- `frontend/src/cockpit/ScorecardPanel.tsx` — migrate `onNavigate` to the object form.
- `frontend/src/api/proposals.ts` — add `useInboxCount`; invalidate `["problems", id]` on accept/reject.
- `frontend/src/routes/Cockpit.tsx` (+ `Cockpit.test.tsx`) — mount the bottom region; widen `onNavigate`.
- `frontend/src/styles/cockpit.css` — bottom-region + problems styling.
- `frontend/src/api/schema.gen.ts` — regenerated (Task 7).

---

## Task 1: Config budget + `over_length` builder

**Files:**
- Modify: `src/call_manager/config.py` (after the `evaluator_daily_token_cap` Field, ~line 288)
- Create: `src/call_manager/proposal_assist/problems/__init__.py`
- Create: `src/call_manager/proposal_assist/problems/builders.py`
- Create: `tests/proposal_assist/problems/__init__.py`
- Create: `tests/proposal_assist/problems/test_builders.py`
- Modify: `tests/proposal_assist/evaluator/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/problems/__init__.py` (empty) and `tests/proposal_assist/problems/test_builders.py`:

```python
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.problems.builders import over_length_problems


def test_over_length_flags_only_sections_above_budget():
    doc = "## A\n" + "x" * 50 + "\n## B\n" + "y" * 5 + "\n"
    sections = split_sections(doc)  # [0]="" preamble, [1]="## A\n…", [2]="## B\n…"
    rows = over_length_problems(sections, budget=40)
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "over_length"
    assert r["severity"] == "warning"
    assert r["title"] == "Over-length §1"
    assert r["detail"] == "56 chars (budget 40)"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 1}


def test_over_length_budget_zero_or_negative_disables():
    sections = split_sections("## A\n" + "x" * 999 + "\n")
    assert over_length_problems(sections, budget=0) == []
    assert over_length_problems(sections, budget=-1) == []
```

Append to `tests/proposal_assist/evaluator/test_config.py`:

```python
def test_problem_section_char_budget_default(_settings_env):
    assert Settings().problem_section_char_budget == 6000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py tests/proposal_assist/evaluator/test_config.py::test_problem_section_char_budget_default -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: …problems.builders` and `AttributeError: …problem_section_char_budget`.

- [ ] **Step 3: Add the config setting**

In `src/call_manager/config.py`, immediately after the `evaluator_daily_token_cap` Field (~line 288), add:

```python
    problem_section_char_budget: int = Field(
        default=6000,
        alias="PROBLEM_SECTION_CHAR_BUDGET",
        description="Per-section character budget for the Problems panel's over-length "
        "heuristic (draft sections only). A loose guardrail, not a hard funder limit; "
        "a value <= 0 disables the over-length check.",
    )
```

- [ ] **Step 4: Create the builders module with `_row` + `over_length_problems`**

Create `src/call_manager/proposal_assist/problems/__init__.py`:

```python
"""SP2b-3b Problems panel: read-only aggregation of cheap quality signals."""

from call_manager.proposal_assist.problems.aggregate import assemble_problems

__all__ = ["assemble_problems"]
```

Create `src/call_manager/proposal_assist/problems/builders.py`:

```python
"""Pure signal builders for the Problems panel (SP2b-3b).

Each builder maps already-loaded inputs to a flat list of ProblemRow dicts. No
DB, no I/O — the orchestrator (aggregate.py) loads inputs and calls these. A
ProblemRow:

    {"category": "broken_citation"|"over_length"|"criterion_gap"|"coherence_drift",
     "severity": "error"|"warning",
     "title": str,
     "detail": str,
     "target": {"top_tab": "draft"|"spec"|"scorecard"|None,
                "bottom_tab": "inbox"|"problems"|None,
                "section_index": int|None} | None}
"""

from __future__ import annotations

import re

from call_manager.proposal_assist.editing.sectionizer import Section


def _row(
    category: str,
    severity: str,
    title: str,
    detail: str,
    *,
    top_tab: str | None = None,
    bottom_tab: str | None = None,
    section_index: int | None = None,
) -> dict:
    target = None
    if top_tab is not None or bottom_tab is not None or section_index is not None:
        target = {"top_tab": top_tab, "bottom_tab": bottom_tab, "section_index": section_index}
    return {
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "target": target,
    }


def over_length_problems(sections: list[Section], budget: int) -> list[dict]:
    """One warning row per draft section whose byte length exceeds `budget`.

    `budget <= 0` disables the check (returns []).
    """
    if budget <= 0:
        return []
    rows: list[dict] = []
    for s in sections:
        n = len(s.text)
        if n > budget:
            rows.append(
                _row(
                    "over_length",
                    "warning",
                    f"Over-length §{s.index}",
                    f"{n} chars (budget {budget})",
                    top_tab="draft",
                    section_index=s.index,
                )
            )
    return rows
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py tests/proposal_assist/evaluator/test_config.py -q ; echo $?`
Expected: PASS (3 new assertions green; the existing evaluator config test still green).

- [ ] **Step 6: Lint, format, commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b
uv run ruff check src/call_manager/config.py src/call_manager/proposal_assist/problems/ tests/proposal_assist/problems/
uv run ruff format src/call_manager/config.py src/call_manager/proposal_assist/problems/ tests/proposal_assist/problems/ tests/proposal_assist/evaluator/test_config.py
git add src/call_manager/config.py src/call_manager/proposal_assist/problems/ tests/proposal_assist/problems/ tests/proposal_assist/evaluator/test_config.py
git commit -m "feat(problems): config budget + over_length signal builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: offset→section mapper + `broken_citation` builder

**Files:**
- Modify: `src/call_manager/proposal_assist/problems/builders.py`
- Modify: `tests/proposal_assist/problems/test_builders.py`

**Context:** `validate_citations_deterministic` (`citations/audit.py:134`) returns issue dicts `{type, severity, citekey, evidence_id, location, detail}`. `location` is `"char {offset}"` for `orphan_marker` and live-row issues, `"citekey {k}"` for `orphan_row`, or `None`. We parse the offset and map it to a draft section via `split_sections` cumulative spans. The builder is PURE — it takes the already-computed issues list (the DB call lives in Task 5). This task uses `split_sections`, so widen the sectionizer import at the top of `builders.py` to `from call_manager.proposal_assist.editing.sectionizer import Section, split_sections`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/problems/test_builders.py`:

```python
from call_manager.proposal_assist.problems.builders import (
    broken_citation_problems,
    ledger_unreadable_problem,
    section_index_for_offset,
)


def test_section_index_for_offset_maps_offset_to_section():
    doc = "preamble\n## Methods\nblah [@x] more\n"  # preamble=§0 (9 chars), §1 from 9
    assert section_index_for_offset(doc, 0) == 0
    assert section_index_for_offset(doc, doc.index("[@x]")) == 1
    assert section_index_for_offset(doc, None) is None


def test_broken_citation_maps_orphan_marker_with_section_and_orphan_row_unplaced():
    doc = "preamble\n## Methods\nblah [@x] more\n"
    issues = [
        {
            "type": "orphan_marker",
            "severity": "error",
            "citekey": "x",
            "evidence_id": "e1",
            "location": f"char {doc.index('[@x]')}",
            "detail": "inline citation marker has no matching ledger row",
        },
        {
            "type": "orphan_row",
            "severity": "error",
            "citekey": "y",
            "evidence_id": "e2",
            "location": "citekey y",
            "detail": "ledger row has no inline marker in proposal.md",
        },
    ]
    rows = broken_citation_problems(issues, doc)
    assert len(rows) == 2
    marker, orphan = rows
    assert marker["category"] == "broken_citation" and marker["severity"] == "error"
    assert marker["title"] == "Broken citation in §1"
    assert marker["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 1}
    assert orphan["title"] == "Broken citation in (unplaced)"
    assert orphan["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": None}


def test_ledger_unreadable_is_one_error_row():
    r = ledger_unreadable_problem("citations.json is not valid JSON")
    assert r["category"] == "broken_citation" and r["severity"] == "error"
    assert r["title"] == "Citations ledger unreadable"
    assert r["detail"] == "citations.json is not valid JSON"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": None}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: FAIL — `ImportError: cannot import name 'broken_citation_problems'`.

- [ ] **Step 3: Implement the mapper + builder**

Append to `src/call_manager/proposal_assist/problems/builders.py`:

```python
_CHAR_RE = re.compile(r"char (\d+)")


def section_index_for_offset(proposal_text: str, offset: int | None) -> int | None:
    """Map a char offset in the draft to its section index, or None if offset is None.

    Uses split_sections cumulative spans (the byte-exact join invariant). An offset
    at/after the document end clamps to the last section; None offset -> None.
    """
    if offset is None:
        return None
    sections = split_sections(proposal_text)
    if not sections:
        return None
    pos = 0
    for s in sections:
        end = pos + len(s.text)
        if pos <= offset < end:
            return s.index
        pos = end
    return sections[-1].index  # clamp offsets at/after the end


def _offset_from_location(location: str | None) -> int | None:
    if not location:
        return None
    m = _CHAR_RE.search(location)
    return int(m.group(1)) if m else None


def broken_citation_problems(issues: list[dict], proposal_text: str) -> list[dict]:
    """Map each deterministic citation issue to a broken_citation ProblemRow."""
    rows: list[dict] = []
    for issue in issues:
        idx = section_index_for_offset(proposal_text, _offset_from_location(issue.get("location")))
        where = f"§{idx}" if idx is not None else "(unplaced)"
        rows.append(
            _row(
                "broken_citation",
                issue.get("severity", "error"),
                f"Broken citation in {where}",
                issue.get("detail", ""),
                top_tab="draft",
                section_index=idx,
            )
        )
    return rows


def ledger_unreadable_problem(message: str) -> dict:
    """The single error row emitted when load_ledger raises on a corrupt ledger."""
    return _row(
        "broken_citation",
        "error",
        "Citations ledger unreadable",
        message,
        top_tab="draft",
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: PASS.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
uv run ruff format src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git add src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git commit -m "feat(problems): offset->section mapper + broken_citation builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `criterion_gap` builder

**Files:**
- Modify: `src/call_manager/proposal_assist/problems/builders.py`
- Modify: `tests/proposal_assist/problems/test_builders.py`

**Context:** The scorecard JSONB (SP2b-3a) has `criteria: [{index, name, band, note, deep_link: {artifact, section_index}|null, weight, effective_weight}]`. Only `gap`(→error) and `at_risk`(→warning) become rows. A deep_link maps to `top_tab`; a missing deep_link falls back to `top_tab: "scorecard"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/problems/test_builders.py`:

```python
from call_manager.proposal_assist.problems.builders import criterion_gap_problems


def test_criterion_gap_only_gap_and_at_risk_with_targets():
    scorecard = {
        "criteria": [
            {"index": 0, "name": "Quality", "band": "strong", "note": "ok",
             "deep_link": None, "weight": 0.5, "effective_weight": 0.5},
            {"index": 1, "name": "Impact", "band": "gap", "note": "No KPIs.",
             "deep_link": {"artifact": "draft", "section_index": 2}, "weight": 0.3,
             "effective_weight": 0.3},
            {"index": 2, "name": "Feasibility", "band": "at_risk", "note": "Thin plan.",
             "deep_link": None, "weight": 0.2, "effective_weight": 0.2},
        ],
        "overall_band": "at_risk",
        "summary": "",
    }
    rows = criterion_gap_problems(scorecard)
    assert len(rows) == 2
    gap, at_risk = rows
    assert gap["severity"] == "error"
    assert gap["title"] == "Criterion gap: Impact"
    assert gap["detail"] == "No KPIs."
    assert gap["target"] == {"top_tab": "draft", "bottom_tab": None, "section_index": 2}
    assert at_risk["severity"] == "warning"
    assert at_risk["title"] == "Criterion at-risk: Feasibility"
    # no deep_link -> Scorecard tab fallback, never top_tab None
    assert at_risk["target"] == {"top_tab": "scorecard", "bottom_tab": None, "section_index": None}


def test_criterion_gap_no_scorecard_is_empty():
    assert criterion_gap_problems(None) == []
    assert criterion_gap_problems({"criteria": []}) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: FAIL — `ImportError: cannot import name 'criterion_gap_problems'`.

- [ ] **Step 3: Implement**

Append to `src/call_manager/proposal_assist/problems/builders.py`:

```python
_GAP_SEVERITY = {"gap": "error", "at_risk": "warning"}
_GAP_LABEL = {"gap": "gap", "at_risk": "at-risk"}


def criterion_gap_problems(scorecard: dict | None) -> list[dict]:
    """One row per scorecard criterion banded gap (error) or at_risk (warning)."""
    if not scorecard:
        return []
    rows: list[dict] = []
    for c in scorecard.get("criteria") or []:
        band = c.get("band")
        if band not in _GAP_SEVERITY:
            continue
        dl = c.get("deep_link")
        if dl and dl.get("artifact"):
            top_tab, section_index = dl.get("artifact"), dl.get("section_index")
        else:
            top_tab, section_index = "scorecard", None
        rows.append(
            _row(
                "criterion_gap",
                _GAP_SEVERITY[band],
                f"Criterion {_GAP_LABEL[band]}: {c.get('name', '')}",
                c.get("note", ""),
                top_tab=top_tab,
                section_index=section_index,
            )
        )
    return rows
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: PASS.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
uv run ruff format src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git add src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git commit -m "feat(problems): criterion_gap builder over the latest scorecard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `coherence_drift` roll-up + ordering/counting helpers

**Files:**
- Modify: `src/call_manager/proposal_assist/problems/builders.py`
- Modify: `tests/proposal_assist/problems/test_builders.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/problems/test_builders.py`:

```python
from call_manager.proposal_assist.problems.builders import (
    coherence_drift_problem,
    count_by_severity,
    order_problems,
)


def test_coherence_drift_single_rollup_or_none():
    assert coherence_drift_problem(0) is None
    r = coherence_drift_problem(3)
    assert r["category"] == "coherence_drift" and r["severity"] == "warning"
    assert r["title"] == "Coherence: 3 spec↔draft item(s) pending"
    assert r["target"] == {"top_tab": None, "bottom_tab": "inbox", "section_index": None}


def test_order_problems_errors_first_then_category_nulls_last_no_crash():
    rows = [
        coherence_drift_problem(2),  # warning, category 3
        {"category": "criterion_gap", "severity": "error", "title": "b", "detail": "",
         "target": {"top_tab": "scorecard", "bottom_tab": None, "section_index": None}},
        {"category": "broken_citation", "severity": "error", "title": "a", "detail": "",
         "target": {"top_tab": "draft", "bottom_tab": None, "section_index": 5}},
        {"category": "broken_citation", "severity": "error", "title": "a", "detail": "",
         "target": {"top_tab": "draft", "bottom_tab": None, "section_index": None}},
    ]
    ordered = order_problems(rows)  # must NOT raise on mixed int/None section_index
    # errors first; within errors, category order (broken_citation < criterion_gap);
    # within broken_citation, the int section_index sorts before the null one.
    assert [r["category"] for r in ordered] == [
        "broken_citation",
        "broken_citation",
        "criterion_gap",
        "coherence_drift",
    ]
    assert ordered[0]["target"]["section_index"] == 5
    assert ordered[1]["target"]["section_index"] is None


def test_count_by_severity():
    rows = [
        {"severity": "error"}, {"severity": "error"}, {"severity": "warning"},
    ]
    assert count_by_severity(rows) == {"error": 2, "warning": 1}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: FAIL — `ImportError: cannot import name 'coherence_drift_problem'`.

- [ ] **Step 3: Implement**

Append to `src/call_manager/proposal_assist/problems/builders.py`:

```python
def coherence_drift_problem(count: int) -> dict | None:
    """A single warning roll-up pointing at the Inbox; None when count <= 0."""
    if count <= 0:
        return None
    return _row(
        "coherence_drift",
        "warning",
        f"Coherence: {count} spec↔draft item(s) pending",
        "Open the Inbox to review.",
        bottom_tab="inbox",
    )


_SEVERITY_RANK = {"error": 0, "warning": 1}
_CATEGORY_RANK = {
    "broken_citation": 0,
    "criterion_gap": 1,
    "over_length": 2,
    "coherence_drift": 3,
}


def _sort_key(row: dict) -> tuple:
    target = row.get("target") or {}
    si = target.get("section_index")
    # Never compare None: the `si is None` term partitions nulls last; `si or 0` is
    # only meaningful for the non-null group (0 is a safe filler for the null group).
    return (
        _SEVERITY_RANK.get(row["severity"], 9),
        _CATEGORY_RANK.get(row["category"], 9),
        si is None,
        si or 0,
        row["title"],
        row["detail"],
    )


def order_problems(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_sort_key)


def count_by_severity(rows: list[dict]) -> dict:
    return {
        "error": sum(1 for r in rows if r["severity"] == "error"),
        "warning": sum(1 for r in rows if r["severity"] == "warning"),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/proposal_assist/problems/test_builders.py -q ; echo $?`
Expected: PASS (all builder tests green).

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
uv run ruff format src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git add src/call_manager/proposal_assist/problems/builders.py tests/proposal_assist/problems/test_builders.py
git commit -m "feat(problems): coherence_drift roll-up + None-safe ordering + counts

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `aggregate.py` orchestrator

**Files:**
- Create: `src/call_manager/proposal_assist/problems/aggregate.py`
- Create: `tests/proposal_assist/problems/test_aggregate.py`

**Context:** The orchestrator reads the draft text + ledger from the workspace repo, runs the four builders defensively (each in its own try-block), computes scorecard staleness inline (duplicating the ~6-line logic from `web/evaluator.py:107-112` to avoid touching shipped code), counts the pending completed-job coherence proposals, and returns `{problems, counts, scorecard_stale}`. Verify before relying: `workspace_repo_dir` (`proposal_assist/paths.py:9`) → `<workspaces_dir>/<id>/repo`; `content_sha` (`evaluator/context.py:44`) → sha or `""`; `CITATIONS_FILENAME` is `"citations.json"` (import from `editing.save`); the coherence visibility gate mirrors `web/proposals.py:69-75`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/problems/test_aggregate.py`:

```python
import uuid
from pathlib import Path

from call_manager.db.models import CoherenceJob, EvaluatorJob, ProposedChange
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.problems.aggregate import (
    assemble_problems,
    count_pending_coherence_proposals,
)
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id, *, proposal="", citations=None):
    repo = workspace_repo_dir(workspaces_dir, ws_id)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(proposal)
    if citations is not None:
        (repo / "citations.json").write_text(citations)
    return repo


def test_assemble_empty_when_no_draft_no_scorecard(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        rubric_ts = ws.rubric_updated_at
        _seed_repo(str(tmp_path), wid)  # empty proposal.md, no ledger
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=rubric_ts,
            budget=6000,
        )
    assert out == {"problems": [], "counts": {"error": 0, "warning": 0}, "scorecard_stale": []}


def test_assemble_no_ledger_but_markers_surface_orphan_errors(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        _seed_repo(str(tmp_path), wid, proposal="## M\nSee [@x]<!-- ev:e1 --> here.\n")
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=6000,
        )
    cats = [p["category"] for p in out["problems"]]
    assert "broken_citation" in cats  # orphan_marker, NOT hidden by the absent ledger
    assert out["counts"]["error"] >= 1


def test_assemble_corrupt_ledger_is_one_error_row(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wid = ws.id
        _seed_repo(str(tmp_path), wid, proposal="## M\nbody\n", citations="{not json")
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=6000,
        )
    broken = [p for p in out["problems"] if p["category"] == "broken_citation"]
    assert len(broken) == 1
    assert broken[0]["title"] == "Citations ledger unreadable"


def test_assemble_combines_scorecard_overlength_and_coherence(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        wid = ws.id
        # over-length: a section beyond budget=10
        _seed_repo(str(tmp_path), wid, proposal="## Long\n" + "z" * 50 + "\n")
        # scorecard with a gap criterion; markers chosen so stale is False (draft sha
        # differs because the seeded proposal != "" -> "draft" appears in stale).
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={
                    "criteria": [
                        {"index": 0, "name": "Impact", "band": "gap", "note": "weak",
                         "deep_link": None, "weight": 1.0, "effective_weight": 1.0}
                    ],
                    "overall_band": "gap",
                    "summary": "",
                },
                evaluated_draft_sha="",   # current draft sha != "" -> stale includes "draft"
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=ws.rubric_updated_at,
            )
        )
        # one pending coherence proposal whose job is completed -> ONE drift roll-up
        job = CoherenceJob(workspace_id=wid, status="completed")
        s.add(job)
        s.flush()
        s.add(
            ProposedChange(
                workspace_id=wid, target_artifact="spec", section_index=0,
                base_commit_sha="x", base_section_sha="y", kind="prose_edit",
                payload={"new_text": "z"}, origin_kind="coherence_session",
                origin_job_id=job.id, status="pending",
            )
        )
        s.commit()
        out = assemble_problems(
            session=s,
            workspace_id=wid,
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
            ws_rubric_updated_at=ws.rubric_updated_at,
            budget=10,
        )
    cats = sorted({p["category"] for p in out["problems"]})
    assert cats == ["coherence_drift", "criterion_gap", "over_length"]
    # exactly ONE coherence roll-up, not one-per-proposal
    assert sum(1 for p in out["problems"] if p["category"] == "coherence_drift") == 1
    assert "draft" in out["scorecard_stale"]


def test_count_pending_coherence_excludes_incomplete_jobs(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        running = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(running)
        s.flush()
        s.add(
            ProposedChange(
                workspace_id=ws.id, target_artifact="spec", section_index=0,
                base_commit_sha="x", base_section_sha="y", kind="prose_edit",
                payload={"new_text": "z"}, origin_kind="coherence_session",
                origin_job_id=running.id, status="pending",
            )
        )
        s.commit()
        # the proposal's job is still running -> not yet visible -> count 0
        assert count_pending_coherence_proposals(s, ws.id) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/problems/test_aggregate.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: …problems.aggregate`.

- [ ] **Step 3: Implement the orchestrator**

Create `src/call_manager/proposal_assist/problems/aggregate.py`:

```python
"""Defensive orchestrator for the Problems panel.

Reads the draft + ledger + latest scorecard + pending coherence count, runs the
pure builders (builders.py) each in its own try-block so one bad source never
500s the endpoint, and returns {problems, counts, scorecard_stale}. NO LLM, no
mutation, no new table.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.evaluator.jobs import latest_completed_evaluator_job
from call_manager.proposal_assist.problems.builders import (
    broken_citation_problems,
    coherence_drift_problem,
    count_by_severity,
    criterion_gap_problems,
    ledger_unreadable_problem,
    order_problems,
    over_length_problems,
)

logger = logging.getLogger(__name__)


def count_pending_coherence_proposals(session: Session, workspace_id: uuid.UUID) -> int:
    """Pending coherence_session proposals whose emitting job is completed (the same
    visibility gate web/proposals.py applies to the inbox)."""
    return (
        session.scalar(
            select(func.count())
            .select_from(ProposedChange)
            .where(
                ProposedChange.workspace_id == workspace_id,
                ProposedChange.status == "pending",
                ProposedChange.origin_kind == "coherence_session",
                exists().where(
                    CoherenceJob.id == ProposedChange.origin_job_id,
                    CoherenceJob.status == "completed",
                ),
            )
        )
        or 0
    )


def assemble_problems(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    repo_dir: Path,
    ws_rubric_updated_at,
    budget: int,
) -> dict:
    rows: list[dict] = []

    draft_path = repo_dir / PROPOSAL_FILENAME
    draft_text = draft_path.read_text() if draft_path.exists() else ""
    sections = split_sections(draft_text)

    # 1. broken citations — absent ledger STILL runs the validator (orphan markers
    #    surface); a corrupt ledger becomes ONE error row. Neither is silenced.
    try:
        citations_path = repo_dir / CITATIONS_FILENAME
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
        issues = validate_citations_deterministic(
            proposal_text=draft_text,
            ledger=ledger,
            session=session,
            workspace_id=workspace_id,
        )
        rows += broken_citation_problems(issues, draft_text)
    except ValueError as exc:
        rows.append(ledger_unreadable_problem(str(exc)))
    except Exception:
        logger.exception("problems: broken_citation signal failed")

    # 2. criterion gaps + scorecard staleness (same logic as web/evaluator.py:107-112).
    scorecard_stale: list[str] = []
    try:
        job = latest_completed_evaluator_job(session, workspace_id=workspace_id)
        scorecard = job.scorecard if job is not None else None
        if job is not None:
            if job.evaluated_draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME):
                scorecard_stale.append("draft")
            if job.evaluated_spec_sha != content_sha(repo_dir / SPEC_FILENAME):
                scorecard_stale.append("spec")
            if job.evaluated_rubric_updated_at != ws_rubric_updated_at:
                scorecard_stale.append("rubric")
        rows += criterion_gap_problems(scorecard)
    except Exception:
        logger.exception("problems: criterion_gap signal failed")

    # 3. over-length draft sections.
    try:
        rows += over_length_problems(sections, budget)
    except Exception:
        logger.exception("problems: over_length signal failed")

    # 4. coherence drift — one roll-up pointing at the inbox.
    try:
        drift = coherence_drift_problem(count_pending_coherence_proposals(session, workspace_id))
        if drift is not None:
            rows.append(drift)
    except Exception:
        logger.exception("problems: coherence_drift signal failed")

    ordered = order_problems(rows)
    return {
        "problems": ordered,
        "counts": count_by_severity(ordered),
        "scorecard_stale": scorecard_stale,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/proposal_assist/problems/test_aggregate.py -q ; echo $?`
Expected: PASS (6 tests). If a `connection refused` appears, re-run the file alone before treating it as real.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check src/call_manager/proposal_assist/problems/aggregate.py tests/proposal_assist/problems/test_aggregate.py
uv run ruff format src/call_manager/proposal_assist/problems/aggregate.py tests/proposal_assist/problems/test_aggregate.py
git add src/call_manager/proposal_assist/problems/aggregate.py tests/proposal_assist/problems/test_aggregate.py
git commit -m "feat(problems): defensive aggregate orchestrator (4 signals + staleness)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `GET /problems` endpoint + mount

**Files:**
- Create: `src/call_manager/proposal_assist/web/problems.py`
- Modify: `src/call_manager/main.py` (import block ~line 46; mount block ~line 536)
- Create: `tests/proposal_assist/problems/test_web_problems.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/problems/test_web_problems.py`:

```python
import uuid

from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.flush()
        ws.rubric_criteria = []
        s.commit()
        return str(ws.id)


def _seed_proposal(tmp_path, ws_id, text):
    repo = workspace_repo_dir(str(tmp_path), uuid.UUID(ws_id))
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(text)


def test_problems_empty_shape(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{ws_id}/problems")
    assert r.status_code == 200
    assert r.json() == {"problems": [], "counts": {"error": 0, "warning": 0}, "scorecard_stale": []}


def test_problems_over_length_and_criterion_gap(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    _seed_proposal(tmp_path, ws_id, "## Long\n" + "z" * 50 + "\n")
    with pg_session_factory() as s:
        s.add(
            EvaluatorJob(
                workspace_id=uuid.UUID(ws_id),
                status="completed",
                scorecard={"criteria": [
                    {"index": 0, "name": "Impact", "band": "gap", "note": "weak",
                     "deep_link": None, "weight": 1.0, "effective_weight": 1.0}],
                    "overall_band": "gap", "summary": ""},
                evaluated_draft_sha="", evaluated_spec_sha="",
                evaluated_rubric_updated_at=s.get(Workspace, uuid.UUID(ws_id)).rubric_updated_at,
            )
        )
        s.commit()
    body = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/problems"
    ).json()  # budget default 6000 -> 51-char section is NOT over-length
    cats = {p["category"] for p in body["problems"]}
    assert "criterion_gap" in cats
    assert body["counts"]["error"] >= 1


def test_problems_unknown_workspace_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    r = client_authed_owner.get(f"/api/workspaces/{uuid.uuid4()}/problems")
    assert r.status_code == 404


def test_problems_unauth_rejected(
    client_unauth, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/problems")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/proposal_assist/problems/test_web_problems.py -q ; echo $?`
Expected: FAIL — 404 for ALL (router not mounted yet) / import error.

- [ ] **Step 3: Create the endpoint**

Create `src/call_manager/proposal_assist/web/problems.py`:

```python
"""Member-gated read-only Problems dashboard (SP2b-3b).

GET /problems aggregates four cheap synchronous signals (broken citations,
criterion gaps, over-length sections, coherence drift) computed per call. No
caching, no LLM, no mutation, no new table.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.problems.aggregate import assemble_problems
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["problems"])


@router.get("/{workspace_id}/problems")
def get_problems(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    return assemble_problems(
        session=session,
        workspace_id=workspace_id,
        repo_dir=repo_dir,
        ws_rubric_updated_at=ws.rubric_updated_at,
        budget=request.app.state.settings.problem_section_char_budget,
    )
```

- [ ] **Step 4: Mount the router in `main.py`**

In `src/call_manager/main.py`, add to the proposal_assist web import group (next to the `proposals` import, ~line 46):

```python
from call_manager.proposal_assist.web.problems import (
    router as problems_router,
)
```

And in the `include_router` block, immediately after `app.include_router(evaluator_router)` (~line 538):

```python
    app.include_router(problems_router)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/proposal_assist/problems/test_web_problems.py -q ; echo $?`
Expected: PASS (4 tests).

- [ ] **Step 6: Confirm Alembic head unchanged + lint + commit**

```bash
uv run alembic heads   # MUST print: 0054 (head)
uv run ruff check src/call_manager/proposal_assist/web/problems.py src/call_manager/main.py tests/proposal_assist/problems/test_web_problems.py
uv run ruff format src/call_manager/proposal_assist/web/problems.py src/call_manager/main.py tests/proposal_assist/problems/test_web_problems.py
git add src/call_manager/proposal_assist/web/problems.py src/call_manager/main.py tests/proposal_assist/problems/test_web_problems.py
git commit -m "feat(problems): GET /problems endpoint + router mount (no migration)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend `api/problems.ts` + `ProblemsPanel` + schema regen

**Files:**
- Create: `frontend/src/cockpit/nav.ts`
- Create: `frontend/src/api/problems.ts`
- Create: `frontend/src/cockpit/ProblemsPanel.tsx`
- Create: `frontend/src/cockpit/ProblemsPanel.test.tsx`
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)

- [ ] **Step 1: Regenerate the OpenAPI client (Task 6 endpoint must be committed first)**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b/frontend
pnpm install   # if node_modules is stale
EMBEDDING_MODEL=bge-m3 pnpm gen:api
git diff --stat src/api/schema.gen.ts   # should show the new /problems path added
```

Expected: `schema.gen.ts` now contains `"/api/workspaces/{workspace_id}/problems"`. (If the diff is empty, the endpoint wasn't committed — go back to Task 6.)

- [ ] **Step 2: Write the shared nav types + the failing ProblemsPanel test**

Create `frontend/src/cockpit/nav.ts`:

```ts
export type TopTab = "draft" | "spec" | "scorecard";
export type BottomTab = "inbox" | "problems";
export type NavTarget = { top_tab?: TopTab; bottom_tab?: BottomTab };
export type OnNavigate = (target: NavTarget) => void;
```

Create `frontend/src/cockpit/ProblemsPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProblemsPanel } from "./ProblemsPanel";

const h = vi.hoisted(() => ({ problems: { data: undefined as unknown } }));

vi.mock("../api/problems", () => ({ useProblems: () => h.problems }));

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  h.problems = { data: undefined };
});

describe("ProblemsPanel", () => {
  it("shows a calm empty state when there are no problems", () => {
    h.problems = { data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [] } };
    wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText(/no problems detected/i)).toBeInTheDocument();
  });

  it("renders rows and routes 'act' to the row target", () => {
    h.problems = {
      data: {
        problems: [
          { category: "criterion_gap", severity: "error", title: "Criterion gap: Impact",
            detail: "weak", target: { top_tab: "scorecard", bottom_tab: null, section_index: null } },
          { category: "coherence_drift", severity: "warning", title: "Coherence: 2 pending",
            detail: "Open the Inbox.", target: { top_tab: null, bottom_tab: "inbox", section_index: null } },
        ],
        counts: { error: 1, warning: 1 },
        scorecard_stale: ["draft"],
      },
    };
    const onNavigate = vi.fn();
    wrap(<ProblemsPanel id="ws1" onNavigate={onNavigate} />);
    expect(screen.getByText("Criterion gap: Impact")).toBeInTheDocument();
    expect(screen.getByText(/may be outdated/i)).toBeInTheDocument(); // stale badge
    fireEvent.click(screen.getByRole("button", { name: /go to criterion gap: impact/i }));
    expect(onNavigate).toHaveBeenCalledWith({ top_tab: "scorecard" });
    fireEvent.click(screen.getByRole("button", { name: /go to coherence: 2 pending/i }));
    expect(onNavigate).toHaveBeenCalledWith({ bottom_tab: "inbox" });
  });

  it("surfaces a fetch error inline", () => {
    h.problems = { data: undefined, isError: true };
    wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/could not load problems/i);
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd frontend && pnpm test ProblemsPanel ; echo $?`
Expected: FAIL — cannot resolve `./ProblemsPanel`.

- [ ] **Step 4: Create the api hook**

Create `frontend/src/api/problems.ts`:

```ts
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type ProblemTarget = {
  top_tab: "draft" | "spec" | "scorecard" | null;
  bottom_tab: "inbox" | "problems" | null;
  section_index: number | null;
};

export type ProblemRow = {
  category: "broken_citation" | "over_length" | "criterion_gap" | "coherence_drift";
  severity: "error" | "warning";
  title: string;
  detail: string;
  target: ProblemTarget | null;
};

export type ProblemsResponse = {
  problems: ProblemRow[];
  counts: { error: number; warning: number };
  scorecard_stale: string[];
};

/** GET /api/workspaces/{id}/problems — the read-only health dashboard. Refetches
 *  on mount/focus; resolving a proposal also invalidates ["problems", id]. */
export function useProblems(id: string) {
  return useQuery({
    queryKey: ["problems", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/problems", {
          params: { path: { workspace_id: id } },
        }),
      ) as ProblemsResponse,
  });
}
```

- [ ] **Step 5: Create the panel**

Create `frontend/src/cockpit/ProblemsPanel.tsx`:

```tsx
import { type ProblemRow, useProblems } from "../api/problems";
import type { NavTarget, OnNavigate } from "./nav";

const CATEGORY_LABEL: Record<ProblemRow["category"], string> = {
  broken_citation: "Broken citations",
  criterion_gap: "Criteria gaps",
  over_length: "Over-length sections",
  coherence_drift: "Coherence",
};
const CATEGORY_ORDER: ProblemRow["category"][] = [
  "broken_citation",
  "criterion_gap",
  "over_length",
  "coherence_drift",
];

function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
  };
}

export function ProblemsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useProblems(id);
  if (q.isError) {
    return (
      <p role="status" className="problems-error">
        Could not load problems.
      </p>
    );
  }
  if (q.isLoading) {
    return <p className="problems-loading">Loading…</p>;
  }
  const data = q.data;
  const rows = data?.problems ?? [];
  if (rows.length === 0) {
    return <p className="problems-empty">No problems detected.</p>;
  }
  const stale = (data?.scorecard_stale ?? []).length > 0;
  const groups = CATEGORY_ORDER.map((cat) => ({
    cat,
    items: rows.filter((r) => r.category === cat),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="problems-panel">
      {groups.map((g) => (
        <section key={g.cat} className="problems-group" aria-label={CATEGORY_LABEL[g.cat]}>
          <h3 className="problems-group-title">
            {CATEGORY_LABEL[g.cat]}
            {g.cat === "criterion_gap" && stale && (
              <span className="problems-stale"> · may be outdated</span>
            )}
          </h3>
          <ul>
            {g.items.map((r, i) => (
              <li key={`${r.category}-${i}`} className={`problem-row severity-${r.severity}`}>
                <span className="problem-sev" aria-hidden="true">
                  {r.severity === "error" ? "⛔" : "⚠"}
                </span>
                <span className="problem-title">{r.title}</span>
                <span className="problem-detail">{r.detail}</span>
                {r.target && (
                  <button
                    type="button"
                    className="problem-act"
                    onClick={() => onNavigate(toNav(r.target))}
                  >
                    Go to {r.title}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
```

- [ ] **Step 6: Run tests + type gate**

Run: `cd frontend && pnpm test ProblemsPanel ; echo $?`
Expected: PASS (3 tests).
Run: `cd frontend && pnpm exec tsc --noEmit ; echo $?`
Expected: exit 0 (no type errors).

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b
git add frontend/src/cockpit/nav.ts frontend/src/api/problems.ts frontend/src/cockpit/ProblemsPanel.tsx frontend/src/cockpit/ProblemsPanel.test.tsx frontend/src/api/schema.gen.ts
git commit -m "feat(problems): frontend api/problems + ProblemsPanel + schema regen

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `BottomPanel` shell + `InboxPanel` relocation

**Files:**
- Create: `frontend/src/cockpit/InboxPanel.tsx` (+ `.test.tsx`)
- Create: `frontend/src/cockpit/BottomPanel.tsx` (+ `.test.tsx`)
- Modify: `frontend/src/api/proposals.ts` (add `useInboxCount`; invalidate `["problems", id]`)
- Modify: `frontend/src/cockpit/DraftPanel.tsx`, `frontend/src/cockpit/SpecPanel.tsx` (remove inline `<ProposalReview>`)

**Context:** `ProposalReview` stays unchanged and is reused inside `InboxPanel` for both artifacts (it already null-renders when an artifact has no prose proposals). `InboxPanel` reads `useDraft`/`useSpec` for `currentSections` + spec `locked`. The Inbox badge counts visible `prose_edit` rows via a new `useInboxCount`.

- [ ] **Step 1: Add `useInboxCount` + problems invalidation to `api/proposals.ts`**

In `frontend/src/api/proposals.ts`, append `useInboxCount`:

```ts
/** Visible prose_edit proposals across both artifacts — the Inbox tab badge. */
export function useInboxCount(id: string): number {
  const draft = useProposedChanges(id, "draft");
  const spec = useProposedChanges(id, "spec");
  const n = (rows?: ProposalRow[]) => (rows ?? []).filter((p) => p.kind === "prose_edit").length;
  return n(draft.data) + n(spec.data);
}
```

In `useAcceptProposal`, extend `refresh` to also invalidate the problems query (resolving a coherence proposal changes the drift count):

```ts
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
    qc.invalidateQueries({ queryKey: [artifact, id] });
    qc.invalidateQueries({ queryKey: ["problems", id] });
  };
```

In `useRejectProposal`, extend the `onSuccess`:

```ts
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, artifact] });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
```

- [ ] **Step 2: Write the failing InboxPanel test**

Create `frontend/src/cockpit/InboxPanel.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InboxPanel } from "./InboxPanel";

const h = vi.hoisted(() => ({
  draft: { data: { sections: [{ index: 1, heading: "M", text: "## M\nold\n", sha: "s" }] } },
  spec: { data: { sections: [{ index: 0, heading: "", text: "", sha: "s" }], locked: false } },
  count: 0,
}));

vi.mock("../api/editor", () => ({
  useDraft: () => h.draft,
  useSpec: () => h.spec,
}));
vi.mock("../api/proposals", () => ({ useInboxCount: () => h.count }));
// ProposalReview is reused as-is; stub it to a recognizable marker per artifact.
vi.mock("./ProposalReview", () => ({
  ProposalReview: ({ artifact }: { artifact: string }) => <div>review:{artifact}</div>,
}));

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  h.count = 0;
});

describe("InboxPanel", () => {
  it("shows an empty message when no prose proposals are pending", () => {
    h.count = 0;
    wrap(<InboxPanel id="ws1" />);
    expect(screen.getByText(/nothing pending/i)).toBeInTheDocument();
  });

  it("renders both artifact groups when proposals are pending", () => {
    h.count = 2;
    wrap(<InboxPanel id="ws1" />);
    expect(screen.getByText("review:draft")).toBeInTheDocument();
    expect(screen.getByText("review:spec")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd frontend && pnpm test InboxPanel ; echo $?`
Expected: FAIL — cannot resolve `./InboxPanel`.

- [ ] **Step 4: Create `InboxPanel`**

Create `frontend/src/cockpit/InboxPanel.tsx`:

```tsx
import { useDraft, useSpec } from "../api/editor";
import { useInboxCount } from "../api/proposals";
import { ProposalReview } from "./ProposalReview";

type Section = { index: number; text: string };

export function InboxPanel({ id }: { id: string }) {
  const draft = useDraft(id);
  const spec = useSpec(id);
  const count = useInboxCount(id);

  const draftSections: Section[] = (draft.data?.sections ?? []).map((s) => ({
    index: s.index,
    text: s.text,
  }));
  const specSections: Section[] = (spec.data?.sections ?? []).map((s) => ({
    index: s.index,
    text: s.text,
  }));
  const specLocked = Boolean(spec.data?.locked);

  if (count === 0) {
    return <p className="inbox-empty">Nothing pending.</p>;
  }
  return (
    <div className="inbox-panel">
      <ProposalReview workspaceId={id} artifact="draft" currentSections={draftSections} />
      <ProposalReview
        workspaceId={id}
        artifact="spec"
        currentSections={specSections}
        locked={specLocked}
      />
    </div>
  );
}
```

- [ ] **Step 5: Run InboxPanel tests to verify pass**

Run: `cd frontend && pnpm test InboxPanel ; echo $?`
Expected: PASS (2 tests).

- [ ] **Step 6: Write the failing BottomPanel test**

Create `frontend/src/cockpit/BottomPanel.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BottomPanel } from "./BottomPanel";

const h = vi.hoisted(() => ({
  count: 3,
  problems: { data: { problems: [], counts: { error: 2, warning: 1 }, scorecard_stale: [] } },
}));

vi.mock("../api/proposals", () => ({ useInboxCount: () => h.count }));
vi.mock("../api/problems", () => ({ useProblems: () => h.problems }));
vi.mock("./InboxPanel", () => ({ InboxPanel: () => <div>inbox-body</div> }));
vi.mock("./ProblemsPanel", () => ({ ProblemsPanel: () => <div>problems-body</div> }));

describe("BottomPanel", () => {
  it("renders badges and switches tabs", () => {
    const onTab = vi.fn();
    render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    // badges
    expect(screen.getByRole("tab", { name: /inbox 3/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /problems 3/i })).toBeInTheDocument(); // 2+1
    // active body
    expect(screen.getByText("inbox-body")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /problems 3/i }));
    expect(onTab).toHaveBeenCalledWith("problems");
  });

  it("hides the bodies when collapsed and toggles", () => {
    const onToggle = vi.fn();
    render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={vi.fn()}
        collapsed={true}
        onToggleCollapsed={onToggle}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.queryByText("inbox-body")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /expand panel/i }));
    expect(onToggle).toHaveBeenCalled();
  });
});
```

- [ ] **Step 7: Run to verify failure**

Run: `cd frontend && pnpm test BottomPanel ; echo $?`
Expected: FAIL — cannot resolve `./BottomPanel`.

- [ ] **Step 8: Create `BottomPanel`**

Create `frontend/src/cockpit/BottomPanel.tsx`:

```tsx
import { useProblems } from "../api/problems";
import { useInboxCount } from "../api/proposals";
import { InboxPanel } from "./InboxPanel";
import type { BottomTab, OnNavigate } from "./nav";
import { ProblemsPanel } from "./ProblemsPanel";

export function BottomPanel({
  id,
  activeTab,
  onTab,
  collapsed,
  onToggleCollapsed,
  onNavigate,
}: {
  id: string;
  activeTab: BottomTab;
  onTab: (t: BottomTab) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onNavigate: OnNavigate;
}) {
  const inboxCount = useInboxCount(id);
  const problems = useProblems(id);
  const problemsCount =
    (problems.data?.counts?.error ?? 0) + (problems.data?.counts?.warning ?? 0);

  return (
    <section className="bottom-panel" aria-label="Workspace panel">
      <div className="bottom-panel-bar">
        <div role="tablist" aria-label="Panel" className="bottom-panel-tabs">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "inbox"}
            onClick={() => onTab("inbox")}
          >
            Inbox {inboxCount}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "problems"}
            onClick={() => onTab("problems")}
          >
            Problems {problemsCount}
          </button>
        </div>
        <button
          type="button"
          className="bottom-panel-toggle"
          aria-expanded={!collapsed}
          onClick={onToggleCollapsed}
        >
          {collapsed ? "Expand panel" : "Collapse panel"}
        </button>
      </div>
      {!collapsed && (
        <div className="bottom-panel-body">
          {activeTab === "inbox" ? (
            <InboxPanel id={id} />
          ) : (
            <ProblemsPanel id={id} onNavigate={onNavigate} />
          )}
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 9: Remove the inline `ProposalReview` from DraftPanel + SpecPanel**

In `frontend/src/cockpit/DraftPanel.tsx`: delete the `import { ProposalReview } from "./ProposalReview";` line and the entire `<ProposalReview … />` block (lines 19-24), leaving the `<ArtifactSectionEditor … />` as the sole child of `<div className="draft-shell">`.

In `frontend/src/cockpit/SpecPanel.tsx`: delete the `import { ProposalReview } from "./ProposalReview";` line and BOTH `<ProposalReview … />` blocks (the empty-state one ~lines 47-56 and the normal one ~lines 101-107).

- [ ] **Step 10: Run the affected frontend tests + type gate**

Run: `cd frontend && pnpm test BottomPanel InboxPanel ProposalReview ; echo $?`
Expected: PASS (BottomPanel 2, InboxPanel 2, the unchanged ProposalReview suite still green).
Run: `cd frontend && pnpm exec tsc --noEmit ; echo $?`
Expected: exit 0.

- [ ] **Step 11: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b
git add frontend/src/cockpit/InboxPanel.tsx frontend/src/cockpit/InboxPanel.test.tsx \
  frontend/src/cockpit/BottomPanel.tsx frontend/src/cockpit/BottomPanel.test.tsx \
  frontend/src/api/proposals.ts frontend/src/cockpit/DraftPanel.tsx frontend/src/cockpit/SpecPanel.tsx
git commit -m "feat(problems): BottomPanel shell + InboxPanel relocation (remove inline review)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Wire the bottom region into `Cockpit` + CSS

**Files:**
- Modify: `frontend/src/routes/Cockpit.tsx`
- Modify: `frontend/src/cockpit/ScorecardPanel.tsx` (migrate `onNavigate` to the object form)
- Modify: `frontend/src/routes/Cockpit.test.tsx` (make the two ordering-sensitive tests route GET by path)
- Modify: `frontend/src/styles/cockpit.css`

**Context — the integration risk:** the existing `Cockpit.test.tsx` relies on GET call ordering (`mockResolvedValueOnce`). Mounting the always-fetching `BottomPanel` (which fires `/proposed-changes` ×2 and `/problems`) shifts that ordering. The fix is to route those two tests' GET mock by path so the draft sequence is decoupled from the bottom-panel calls. The bottom region **defaults to expanded** so the relocated inbox stays visible.

- [ ] **Step 1: Migrate `ScorecardPanel.onNavigate` to the object form**

In `frontend/src/cockpit/ScorecardPanel.tsx`:

Replace the import-less prop type and the callsite. Change the prop signature:

```tsx
import type { OnNavigate } from "./nav";

export function ScorecardPanel({
  id,
  onNavigate,
}: {
  id: string;
  onNavigate: OnNavigate;
}) {
```

And the deep-link button's onClick (was `onNavigate(c.deep_link!.artifact, c.deep_link!.section_index)`):

```tsx
                onClick={() => onNavigate({ top_tab: c.deep_link!.artifact })}
```

(The `ScorecardPanel.test.tsx` passes `onNavigate={vi.fn()}` and asserts rendering only — it stays green.)

- [ ] **Step 2: Rewrite `Cockpit.tsx`**

Replace `frontend/src/routes/Cockpit.tsx` with:

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";

import { BottomPanel } from "../cockpit/BottomPanel";
import { DraftPanel } from "../cockpit/DraftPanel";
import type { BottomTab, NavTarget, TopTab } from "../cockpit/nav";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

export function Cockpit() {
  const { id = "" } = useParams();
  const [topTab, setTopTab] = useState<TopTab>("draft");
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);

  // Two-axis navigation: a Problems/Scorecard "act" can switch the artifact tab
  // (incl. Scorecard) and/or focus a bottom-region tab (expanding it if collapsed).
  const onNavigate = (target: NavTarget) => {
    if (target.top_tab) setTopTab(target.top_tab);
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      setCollapsed(false);
    }
  };

  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button type="button" role="tab" aria-selected={topTab === "draft"} onClick={() => setTopTab("draft")}>
          Draft
        </button>
        <button type="button" role="tab" aria-selected={topTab === "spec"} onClick={() => setTopTab("spec")}>
          Spec
        </button>
        <button type="button" role="tab" aria-selected={topTab === "scorecard"} onClick={() => setTopTab("scorecard")}>
          Scorecard
        </button>
      </div>
      {topTab === "draft" && <DraftPanel id={id} />}
      {topTab === "spec" && <SpecPanel id={id} />}
      {topTab === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
      <BottomPanel
        id={id}
        activeTab={bottomTab}
        onTab={setBottomTab}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((c) => !c)}
        onNavigate={onNavigate}
      />
    </div>
  );
}
```

- [ ] **Step 3: Make the two ordering-sensitive Cockpit tests route GET by path**

In `frontend/src/routes/Cockpit.test.tsx`, add this helper near `mockGetByPath` (after `mockGetByEv`):

```tsx
// Route GET by path with a DRAFT-ONLY sequence (first /draft -> firstDraft, rest ->
// restDraft). The always-mounted BottomPanel also fires /proposed-changes, /problems
// (InboxPanel + badges) and /spec (InboxPanel.useSpec) — give each a fixed payload so
// they NEVER consume a draft-sequence slot. Only the /draft counter advances, so the
// draft sequence the test depends on is fully decoupled from the bottom-panel calls.
function mockDraftSequenceByPath(firstDraft: unknown, restDraft: unknown) {
  let draftCalls = 0;
  return vi.spyOn(clientMod.api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/proposed-changes")) {
      return Promise.resolve({ data: { items: [] }, error: undefined });
    }
    if (path.endsWith("/problems")) {
      return Promise.resolve({
        data: { problems: [], counts: { error: 0, warning: 0 }, scorecard_stale: [] },
        error: undefined,
      });
    }
    if (path.endsWith("/spec")) {
      return Promise.resolve({
        data: {
          head: "a".repeat(40),
          can_edit: false,
          sections: [{ index: 0, heading: "", text: "", sha: "s0" }],
          locked: false,
        },
        error: undefined,
      });
    }
    if (path.endsWith("/draft")) {
      const payload = draftCalls === 0 ? firstDraft : restDraft;
      draftCalls += 1;
      return Promise.resolve(payload);
    }
    return Promise.resolve(firstDraft); // no other GET paths in these two tests
  }) as never);
}
```

Then in the test **"a successful save clears the dirty buffer …"**, replace the GET spy:

```tsx
  // BEFORE:
  // vi.spyOn(clientMod.api, "GET").mockResolvedValueOnce(original as never).mockResolvedValue(committed as never);
  // AFTER:
  mockDraftSequenceByPath(original, committed);
```

And in the test **"select-to-cite: retains the held selection across a background draft refetch …"**, replace:

```tsx
  // BEFORE:
  // vi.spyOn(clientMod.api, "GET").mockResolvedValueOnce(original as never).mockResolvedValue(shortened as never);
  // AFTER:
  mockDraftSequenceByPath(original, shortened);
```

Leave every other test as-is: their catch-all `mockResolvedValue(draftPayload)` returns a draft-shaped object for `/proposed-changes` (`.items` undefined → `[]`) and `/problems` (`.problems` undefined → `[]`), so the bottom panel renders empty and the existing assertions are unaffected.

- [ ] **Step 4: Run the full Cockpit suite + type gate**

Run: `cd frontend && pnpm test Cockpit ; echo $?`
Expected: PASS — all existing Cockpit tests green (the two migrated tests included).
Run: `cd frontend && pnpm exec tsc --noEmit ; echo $?`
Expected: exit 0.

- [ ] **Step 5: Add the bottom-region + problems CSS**

Append to `frontend/src/styles/cockpit.css` (match the existing calm-clinical variables/palette already used in the file):

```css
/* SP2b-3b — bottom-panel region + Problems dashboard */
.bottom-panel {
  border-top: 1px solid var(--cm-border, #d7dee3);
  margin-top: 0.75rem;
  background: var(--cm-surface, #fbfdfe);
}
.bottom-panel-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.25rem 0.5rem;
}
.bottom-panel-tabs {
  display: flex;
  gap: 0.25rem;
}
.bottom-panel-tabs [role="tab"][aria-selected="true"] {
  font-weight: 600;
  border-bottom: 2px solid var(--cm-accent, #2c7a7b);
}
.bottom-panel-body {
  max-height: 14rem;
  overflow-y: auto;
  padding: 0.5rem;
}
.problems-group-title {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--cm-muted, #5b6b73);
  margin: 0.5rem 0 0.25rem;
}
.problems-stale {
  color: var(--cm-warn, #b7791f);
  font-weight: 400;
  text-transform: none;
}
.problem-row {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.2rem 0;
}
.problem-row.severity-error .problem-sev {
  color: var(--cm-error, #c53030);
}
.problem-row.severity-warning .problem-sev {
  color: var(--cm-warn, #b7791f);
}
.problem-detail {
  color: var(--cm-muted, #5b6b73);
  font-size: 0.9rem;
}
.problem-act {
  margin-left: auto;
}
.problems-empty,
.inbox-empty {
  color: var(--cm-muted, #5b6b73);
  padding: 0.75rem;
}
.problems-error {
  color: var(--cm-error, #c53030);
  padding: 0.5rem;
}
```

(If those `--cm-*` variables are not defined in the file, substitute the literal fallbacks already inlined above — they are the calm-clinical defaults.)

- [ ] **Step 6: Final frontend check + commit**

Run: `cd frontend && pnpm test ; echo $?` (full frontend suite) and `pnpm exec tsc --noEmit ; echo $?`
Expected: PASS / exit 0. (If a single unrelated suite flakes, re-run it alone.)

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b
git add frontend/src/routes/Cockpit.tsx frontend/src/routes/Cockpit.test.tsx \
  frontend/src/cockpit/ScorecardPanel.tsx frontend/src/styles/cockpit.css
git commit -m "feat(problems): mount bottom region in Cockpit + two-axis nav + CSS

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after all 9 tasks)

Run from the worktree:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3b
# Backend — affected suites only (whole-suite is testcontainer-flaky)
uv run pytest tests/proposal_assist/problems/ tests/proposal_assist/evaluator/test_config.py -q ; echo $?
# No schema drift / single head
uv run alembic heads            # MUST print 0054 (head)
uv run ruff check src/call_manager/proposal_assist/problems/ src/call_manager/proposal_assist/web/problems.py
# Frontend
cd frontend
pnpm exec tsc --noEmit ; echo $?
pnpm test ; echo $?
git -C .. diff --stat src/api/schema.gen.ts   # only the /problems path added; no spurious churn
```

Expected: backend problems suites + evaluator config green; `0054 (head)`; ruff clean; tsc exit 0; frontend suite green; `schema.gen.ts` shows only the `/problems` path addition.

Then dispatch the final whole-implementation code-review subagent over `git diff master..cockpit-sp2b-3b` and fix any real findings. **STOP BEFORE MERGE** — leave the branch for human review.

## Out of scope (later slices — do NOT build here)

- Uncited-claims signal (LLM-backed `list_unsupported_claims_impl`) — its own slice (on-demand action or 4th background job).
- Activity feed (no event-log substrate yet).
- Drag-resize / height persistence of the bottom region.
- Deep-link section-scroll (act switches the top-tab only, matching the Scorecard today).
- Citation proposals in the inbox (still `prose_edit`-only).
- Invalidating `["problems", id]` on every draft save (today it refreshes on focus/mount + on accept/reject) — a documented freshness enhancement.
