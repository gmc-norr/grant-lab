# User Section Editing + Claim Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the workspace owner edit proposal.md section-by-section in the web UI, and automatically analyze user-added text — auditing existing citations and suggesting workspace-library references with verifiable evidence quotes.

**Architecture:** A pure sectionizer splits proposal.md on `##` headings (byte-round-trip guaranteed). User saves are serialized against agent writes by a new per-workspace transaction-scoped advisory lock (commit mutex) that all mutating paths acquire; agent `replace_proposal`/`edit_proposal` gain a view-of-HEAD precondition. Saves run free deterministic validation synchronously and enqueue content-addressed claim-analysis jobs drained by a scheduler tick; results land as occurrence-addressed annotations rendered in a per-section strip with Insert/Dismiss actions. Insert reuses the canonical evidence-ledger citation path.

**Spec:** `docs/superpowers/specs/2026-06-10-user-editing-claim-analysis-design.md` (rev 3). Read it before starting.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 + HTMX, SQLAlchemy 2 + Alembic, pgvector, pytest + testcontainers, existing proposal-assist modules (`citations/`, `tools/`, `turns/`, `drafting/`).

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/editing/__init__.py` | new package |
| `src/call_manager/proposal_assist/editing/sectionizer.py` | pure split/join of proposal.md on `##` headings |
| `src/call_manager/proposal_assist/editing/save.py` | save-section service: mutex, guards, commit, provenance, Tier-1, enqueue |
| `src/call_manager/proposal_assist/paths.py` | single source of truth: repo dir = `<workspaces_dir>/<id>/repo` |
| `src/call_manager/proposal_assist/drafting/locking.py` | + commit-mutex helpers (xact-scoped advisory lock) |
| `src/call_manager/proposal_assist/citations/markers.py` | + `find_bare_markers()` |
| `src/call_manager/proposal_assist/citations/gitio.py` | + `current_head()` |
| `src/call_manager/proposal_assist/analysis/__init__.py` | new package |
| `src/call_manager/proposal_assist/analysis/queue.py` | claim_analysis_jobs enqueue/claim/mark/supersede/requeue |
| `src/call_manager/proposal_assist/analysis/pipeline.py` | per-job analysis: sentences → cited audit / uncited classify+retrieve+verdict → annotations |
| `src/call_manager/proposal_assist/analysis/insert.py` | occurrence-aware user citation insert (anchor construction + canonical ledger path) |
| `src/call_manager/proposal_assist/analysis/invalidation.py` | library-change staleness + re-enqueue |
| `src/call_manager/proposal_assist/analysis/reconcile.py` | reconcile tick: enqueue jobs for uncovered HEAD sections |
| `src/call_manager/proposal_assist/web/sections.py` | section edit/save/cancel routes (HTMX fragments) |
| `src/call_manager/proposal_assist/web/annotations.py` | strip fragment, insert/dismiss/reanalyze routes |
| `src/call_manager/proposal_assist/tools/proposal.py` | view-of-HEAD precondition + commit mutex |
| `src/call_manager/proposal_assist/tools/registry.py` | ToolContext: `agent_view_head` field |
| `src/call_manager/proposal_assist/turns/submit.py` | turn enqueue acquires commit mutex |
| `src/call_manager/db/models.py` | + `ClaimAnalysisJob`, `ClaimClassification`, `UserClaimAnnotation` |
| `alembic/versions/0041_user_editing_claim_analysis.py` | three new tables |
| `src/call_manager/scheduler.py` | + analysis drain tick, reconcile tick |
| `src/call_manager/config.py` | + `analysis_tick_seconds`, `analysis_reconcile_tick_seconds` |
| `src/call_manager/main.py` | include new routers; pass citation_service/embedder to scheduler |
| `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html` | render sections loop |
| `src/call_manager/web/templates/proposal_assist/partials/section_view.html` | one rendered section + Edit button + strip include |
| `src/call_manager/web/templates/proposal_assist/partials/section_edit.html` | textarea form fragment |
| `src/call_manager/web/templates/proposal_assist/partials/annotation_strip.html` | per-section annotations + actions |

Branch: do all work on `feature/user-editing-claim-analysis` (create from master at start; commit per task).

Run the full suite before starting to confirm a green baseline: `uv run pytest -q` (expect: all pass, ~20s).

---

### Task 1: Sectionizer

**Files:**
- Create: `src/call_manager/proposal_assist/editing/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/editing/sectionizer.py`
- Test: `tests/test_editing_sectionizer.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Sectionizer: byte-round-trip split/join of proposal.md on ## headings."""

from call_manager.proposal_assist.editing.sectionizer import Section, split_sections

DOC = (
    "# Title\n"
    "Intro before any section.\n"
    "\n"
    "## Background\n"
    "Some text.\n"
    "```\n"
    "## not a heading (in fence)\n"
    "```\n"
    "## Aims\n"
    "Aim text.\n"
)


def _join(sections: list[Section]) -> str:
    return "".join(s.text for s in sections)


def test_round_trip_byte_identical():
    assert _join(split_sections(DOC)) == DOC


def test_preamble_is_section_zero():
    sections = split_sections(DOC)
    assert sections[0].index == 0
    assert sections[0].heading == ""
    assert sections[0].text.startswith("# Title\n")


def test_headings_and_fence_respected():
    sections = split_sections(DOC)
    assert [s.heading for s in sections] == ["", "Background", "Aims"]
    # the fenced "## not a heading" stays inside Background
    assert "## not a heading" in sections[1].text


def test_no_heading_doc_is_single_preamble():
    sections = split_sections("just prose\nno headings\n")
    assert len(sections) == 1
    assert sections[0].heading == ""


def test_empty_doc():
    sections = split_sections("")
    assert len(sections) == 1
    assert sections[0].text == ""


def test_doc_starting_with_heading_has_empty_preamble():
    doc = "## A\nbody\n## B\nbody2"
    sections = split_sections(doc)
    assert sections[0].text == ""
    assert [s.heading for s in sections] == ["", "A", "B"]
    assert _join(sections) == doc


def test_level3_heading_is_not_a_boundary():
    doc = "## A\n### sub\nbody\n"
    sections = split_sections(doc)
    assert [s.heading for s in sections] == ["", "A"]


def test_sha_changes_with_content():
    a = split_sections("## A\nx\n")[1]
    b = split_sections("## A\ny\n")[1]
    assert a.sha != b.sha and len(a.sha) == 64
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_editing_sectionizer.py -q`
Expected: FAIL — `ModuleNotFoundError: call_manager.proposal_assist.editing`

- [ ] **Step 3: Implement**

```python
"""Pure split/join of proposal.md into ##-headed sections.

Invariant: ``"".join(s.text for s in split_sections(doc)) == doc`` —
byte-identical. Section 0 is the preamble (possibly empty). A boundary is a
line starting with exactly ``##`` (not ``###``) outside fenced code blocks.
No I/O, no DB.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^\s*```")
_H2_RE = re.compile(r"^##(?!#)\s*(?P<heading>.*?)\s*$")


@dataclass(frozen=True)
class Section:
    index: int
    heading: str  # "" for the preamble
    text: str  # exact bytes: heading line + body, including newlines

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def split_sections(text: str) -> list[Section]:
    chunks: list[tuple[str, list[str]]] = [("", [])]  # (heading, lines)
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            chunks[-1][1].append(line)
            continue
        m = None if in_fence else _H2_RE.match(stripped)
        if m:
            chunks.append((m.group("heading"), [line]))
        else:
            chunks[-1][1].append(line)
    return [
        Section(index=i, heading=heading, text="".join(lines))
        for i, (heading, lines) in enumerate(chunks)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_editing_sectionizer.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git checkout -b feature/user-editing-claim-analysis
git add src/call_manager/proposal_assist/editing tests/test_editing_sectionizer.py
git commit -m "feat(editing): sectionizer with byte-round-trip guarantee"
```

---

### Task 2: Commit mutex + `current_head` + repo-path helper

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/locking.py` (append)
- Modify: `src/call_manager/proposal_assist/citations/gitio.py` (append)
- Create: `src/call_manager/proposal_assist/paths.py`
- Test: `tests/test_editing_commit_mutex.py`

**CRITICAL — production repo layout.** Workspace git repos live at
`<workspaces_dir>/<workspace_id>/repo/` — NOT `<workspaces_dir>/<workspace_id>/`
(see `drafting/controller.py:136`, `phases/setup.py:224`,
`web/workspaces.py:577`). Every new module in this plan resolves the repo via
the helper below, and every test fixture creates the `repo/` subdirectory.
Create `src/call_manager/proposal_assist/paths.py`:

```python
"""Single source of truth for workspace filesystem layout."""

from __future__ import annotations

import uuid
from pathlib import Path


def workspace_repo_dir(workspaces_dir: Path | str, workspace_id: uuid.UUID) -> Path:
    """The workspace's git repo: <workspaces_dir>/<workspace_id>/repo
    (matches drafting/controller.py and phases/setup.py)."""
    return Path(workspaces_dir) / str(workspace_id) / "repo"
```

Wherever later tasks say `workspace_repo_dir(...)`, import it from
`call_manager.proposal_assist.paths`.

- [ ] **Step 1: Write the failing test**

```python
import threading
import time
import uuid

from sqlalchemy import text

from call_manager.proposal_assist.drafting.locking import (
    acquire_commit_mutex,
    commit_mutex_key,
    workspace_lock_key,
)


def test_commit_mutex_key_differs_from_subprocess_lock_key():
    ws = uuid.uuid4()
    assert commit_mutex_key(ws) != workspace_lock_key(ws)


def test_commit_mutex_serializes_two_sessions(pg_session_factory):
    """Second acquirer blocks until the first transaction commits."""
    ws = uuid.uuid4()
    order: list[str] = []

    s1 = pg_session_factory()
    acquire_commit_mutex(s1, ws)
    order.append("s1-acquired")

    def second():
        with pg_session_factory() as s2:
            acquire_commit_mutex(s2, ws)  # blocks
            order.append("s2-acquired")
            s2.rollback()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.3)
    assert order == ["s1-acquired"]  # s2 still waiting
    s1.commit()  # xact lock released at COMMIT
    t.join(timeout=5)
    assert order == ["s1-acquired", "s2-acquired"]
    s1.close()


def test_mutex_released_on_rollback(pg_session_factory):
    ws = uuid.uuid4()
    with pg_session_factory() as s1:
        acquire_commit_mutex(s1, ws)
        s1.rollback()
    with pg_session_factory() as s2:
        acquire_commit_mutex(s2, ws)  # must not block
        assert s2.execute(text("SELECT 1")).scalar() == 1
        s2.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_editing_commit_mutex.py -q`
Expected: FAIL — `ImportError: cannot import name 'acquire_commit_mutex'`

- [ ] **Step 3: Implement** — append to `drafting/locking.py`:

```python
def commit_mutex_key(workspace_id: uuid.UUID) -> int:
    """Key for the per-workspace COMMIT MUTEX — a transaction-scoped advisory
    lock serializing every git mutation of the workspace repo (user section
    saves, user citation inserts, agent mutation tools, turn enqueue).

    Distinct namespace from workspace_lock_key (the session-scoped subprocess
    lifetime lock) so holding one never implies the other.
    """
    digest = hashlib.sha256(f"commit:{workspace_id}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def acquire_commit_mutex(session: Session, workspace_id: uuid.UUID) -> None:
    """Blocking acquire of the commit mutex on *session*'s current
    transaction. Released automatically at COMMIT or ROLLBACK — there is no
    explicit release. Callers: acquire, do guards + git write + DB rows,
    commit. Critical sections are short (one git commit), so blocking (not
    try-) semantics are right."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": commit_mutex_key(workspace_id)},
    )
```

Append to `citations/gitio.py`:

```python
def current_head(repo_dir: Path) -> str:
    """Return the current HEAD commit SHA of *repo_dir*. Raises GitCommitError
    on failure (no repo / no commits)."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(repo_dir),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitCommitError(f"rev-parse HEAD failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
```

(Check `gitio.py` imports — `subprocess` and `Path` are already imported there.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_editing_commit_mutex.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/drafting/locking.py src/call_manager/proposal_assist/citations/gitio.py tests/test_editing_commit_mutex.py
git commit -m "feat(editing): per-workspace transaction-scoped commit mutex + current_head helper"
```

---

### Task 3: Bare-marker scan

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/markers.py` (append)
- Test: `tests/test_citations_bare_markers.py`

- [ ] **Step 1: Write the failing test**

```python
from call_manager.proposal_assist.citations.markers import find_bare_markers


def test_bare_marker_detected():
    out = find_bare_markers("A claim [@smith2021]. More text.")
    assert [(m["citekey"], m["start"]) for m in out] == [("smith2021", 8)]


def test_canonical_marker_not_flagged():
    text = "A claim [@smith2021]<!-- ev:ev-abcdef123456 -->."
    assert find_bare_markers(text) == []


def test_mixed():
    text = (
        "Good [@a1]<!-- ev:ev-aaaaaa111111 --> and bad [@b2] "
        "and also [@c-3]."
    )
    assert [m["citekey"] for m in find_bare_markers(text)] == ["b2", "c-3"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_citations_bare_markers.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement** — append to `markers.py`:

```python
# A visible citekey marker NOT followed by the canonical ev-comment. Users
# hand-typing [@key] in the section editor produce these; they are invisible
# to find_markers (and so to deterministic validation) and block composed
# export. Detected at Tier-1 save validation.
_BARE_RE = re.compile(r"\[@(?P<citekey>[A-Za-z0-9_-]+)\](?!<!-- ev:)")


def find_bare_markers(text: str) -> list[dict]:
    """Return [{citekey, start}] for each bare ``[@key]`` lacking the
    ``<!-- ev:... -->`` evidence comment, in document order."""
    return [
        {"citekey": m.group("citekey"), "start": m.start()}
        for m in _BARE_RE.finditer(text)
    ]
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_citations_bare_markers.py -q` → 3 passed.
- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/citations/markers.py tests/test_citations_bare_markers.py
git commit -m "feat(citations): detect bare [@key] markers lacking evidence comments"
```

---

### Task 4: Models + migration 0041

**Files:**
- Modify: `src/call_manager/db/models.py` (append three models after `CitationAuditVerdict`)
- Create: `alembic/versions/0041_user_editing_claim_analysis.py`
- Test: `tests/test_migration_0041_user_editing.py`

- [ ] **Step 1: Write the failing test** (mirror `tests/test_migration_0040_bibliography_settings.py` structure — round trip on the migrated engine):

```python
import uuid

from sqlalchemy import text

from call_manager.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    User,
    UserClaimAnnotation,
    Workspace,
)


def _mk_workspace(session) -> Workspace:
    user = User(email="owner@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws


def test_claim_analysis_job_dedup_index(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        s.add(ClaimAnalysisJob(workspace_id=ws.id, section_index=1,
                               section_heading="A", section_sha="a" * 64,
                               commit_sha="c" * 40))
        s.commit()
        # duplicate queued (workspace, section_sha) violates partial unique idx
        dup = s.execute(text(
            "INSERT INTO claim_analysis_jobs "
            "(workspace_id, section_index, section_heading, section_sha, commit_sha, status) "
            "VALUES (:w, 2, 'B', :sha, :c, 'queued') ON CONFLICT DO NOTHING RETURNING id"
        ), {"w": str(ws.id), "sha": "a" * 64, "c": "d" * 40}).first()
        assert dup is None  # conflict → no row


def test_annotation_uniqueness_includes_occurrence_and_evidence(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        base = dict(workspace_id=ws.id, section_sha="a" * 64,
                    section_heading="A", sentence_ordinal=0, char_offset=0,
                    claim_sha="b" * 64, claim_text="x", kind="uncited_no_match")
        s.add(UserClaimAnnotation(section_occurrence=0, **base))
        s.add(UserClaimAnnotation(section_occurrence=1, **base))  # duplicate section ok
        s.commit()
        s.add(UserClaimAnnotation(
            workspace_id=ws.id, section_sha="a" * 64, section_occurrence=0,
            section_heading="A", sentence_ordinal=1, char_offset=10,
            claim_sha="c" * 64, claim_text="y", kind="cited_unsupported",
            evidence_id="ev-aaaaaa111111"))
        s.add(UserClaimAnnotation(
            workspace_id=ws.id, section_sha="a" * 64, section_occurrence=0,
            section_heading="A", sentence_ordinal=1, char_offset=10,
            claim_sha="c" * 64, claim_text="y", kind="cited_unsupported",
            evidence_id="ev-bbbbbb222222"))  # second bad citation, same sentence
        s.commit()
        assert s.query(UserClaimAnnotation).count() == 4


def test_classification_unique_per_prompt_version(pg_session_factory):
    with pg_session_factory() as s:
        ws = _mk_workspace(s)
        s.add(ClaimClassification(workspace_id=ws.id, claim_sha="a" * 64,
                                  prompt_version="v1", is_claim=False,
                                  outcome="not_claim"))
        s.add(ClaimClassification(workspace_id=ws.id, claim_sha="a" * 64,
                                  prompt_version="v2", is_claim=True,
                                  outcome="no_match"))
        s.commit()
        assert s.query(ClaimClassification).count() == 2
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_migration_0041_user_editing.py -q` → FAIL (ImportError).

- [ ] **Step 3: Add models** — append to `db/models.py` (after `CitationAuditVerdict`):

```python
class ClaimAnalysisJob(Base):
    """Tier-2 claim-analysis job for one section snapshot. Append-only and
    content-addressed: a row is never retargeted; supersession at drain time
    skips jobs whose section_sha no longer exists at HEAD. Spec 2026-06-10 §3."""

    __tablename__ = "claim_analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed','superseded')",
            name="ck_claim_analysis_jobs_status",
        ),
        # Content-keyed enqueue dedup: at most one QUEUED job per section content.
        Index(
            "uq_claim_analysis_jobs_ws_sha_queued",
            "workspace_id",
            "section_sha",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_claim_analysis_jobs_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)  # display hint only
    section_heading: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    section_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ClaimClassification(Base):
    """Reusable content-level cache: is this sentence a claim, and what did
    retrieval find for it? Keyed by claim content, NOT occurrence. Spec §3."""

    __tablename__ = "claim_classifications"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('not_claim','supported','no_match')",
            name="ck_claim_classifications_outcome",
        ),
        UniqueConstraint(
            "workspace_id", "claim_sha", "prompt_version",
            name="uq_claim_classifications_ws_sha_pv",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    claim_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    is_claim: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("references.id", ondelete="SET NULL"), nullable=True
    )
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserClaimAnnotation(Base):
    """Occurrence-addressed, user-facing analysis finding. One row per
    occurrence: section snapshot + duplicate-section ordinal + sentence
    ordinal (+ evidence id for cited kinds). Spec §3."""

    __tablename__ = "user_claim_annotations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('uncited_supported','uncited_no_match','cited_unsupported')",
            name="ck_user_claim_annotations_kind",
        ),
        CheckConstraint(
            "status IN ('open','inserted','dismissed','stale')",
            name="ck_user_claim_annotations_status",
        ),
        UniqueConstraint(
            "workspace_id", "section_sha", "section_occurrence",
            "sentence_ordinal", "kind", "evidence_id",
            name="uq_user_claim_annotations_occurrence",
        ),
        Index("ix_user_claim_annotations_ws_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    section_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    section_occurrence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    section_heading: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sentence_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    char_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # "" sentinel for uncited kinds; ev-id for cited_unsupported (keeps the
    # composite unique constraint enforceable — Postgres treats NULLs as
    # distinct).
    evidence_id: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("references.id", ondelete="SET NULL"), nullable=True
    )
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

(Reuse the imports already present at the top of `models.py` — all of `BigInteger`, `Boolean`, `CheckConstraint`, `DateTime`, `ForeignKey`, `Index`, `Integer`, `JSONB`, `String`, `Text`, `UUID`, `UniqueConstraint`, `func`, `text` are imported there.)

- [ ] **Step 4: Write migration** — `alembic/versions/0041_user_editing_claim_analysis.py`, header exactly like `0040_bibliography_settings.py` (`revision = "0041"`, `down_revision = "0040"`). In `upgrade()`, `op.create_table(...)` for the three tables matching the models above column-for-column (use `sa.text("'queued'")`-style server defaults; create the partial unique index with `op.create_index("uq_claim_analysis_jobs_ws_sha_queued", "claim_analysis_jobs", ["workspace_id", "section_sha"], unique=True, postgresql_where=sa.text("status = 'queued'"))`). In `downgrade()`, drop the three tables in reverse order.

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_migration_0041_user_editing.py -q` → 3 passed. Also run an existing migration test to confirm the chain: `uv run pytest tests/test_alembic_v2_round_trip.py -q` → passes.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0041_user_editing_claim_analysis.py tests/test_migration_0041_user_editing.py
git commit -m "feat(analysis): claim_analysis_jobs, claim_classifications, user_claim_annotations tables"
```

---

### Task 5: Analysis queue module

**Files:**
- Create: `src/call_manager/proposal_assist/analysis/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/analysis/queue.py`
- Test: `tests/test_analysis_queue.py`

**Implementation deviation (verified):** `enqueue_section_job` must use `result.inserted_primary_key is not None` rather than `bool(result.rowcount)` — psycopg3 reports rowcount -1 due to the implicit RETURNING for the server-generated PK (house precedent: ranking/queue.py lines 73-76); and `mark_failed` carries the same duplicate-queued supersede guard as the reaper.

- [ ] **Step 1: Write the failing tests**

```python
import uuid
from datetime import UTC, datetime, timedelta

from call_manager.db.models import ClaimAnalysisJob, User, Workspace
from call_manager.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    enqueue_section_job,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
)


def _ws(session):
    user = User(email="o@example.org", is_admin=False)
    session.add(user); session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws); session.flush()
    return ws


def test_enqueue_dedups_on_content(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        assert enqueue_section_job(s, workspace_id=ws.id, section_index=1,
                                   section_heading="A", section_sha="a" * 64,
                                   commit_sha="c1") is True
        assert enqueue_section_job(s, workspace_id=ws.id, section_index=2,
                                   section_heading="A2", section_sha="a" * 64,
                                   commit_sha="c2") is False  # same content → dedup
        assert enqueue_section_job(s, workspace_id=ws.id, section_index=1,
                                   section_heading="A", section_sha="b" * 64,
                                   commit_sha="c2") is True  # new content → new job
        s.commit()
        assert s.query(ClaimAnalysisJob).count() == 2


def test_claim_marks_running_and_terminal_states(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(s, workspace_id=ws.id, section_index=0,
                            section_heading="", section_sha="a" * 64, commit_sha="c")
        enqueue_section_job(s, workspace_id=ws.id, section_index=1,
                            section_heading="B", section_sha="b" * 64, commit_sha="c")
        s.commit()
        jobs = claim_queued_jobs(s, limit=10)
        assert {j.status for j in jobs} == {"running"}
        mark_done(s, jobs[0])
        mark_failed(s, jobs[1], error="boom")
        mark_superseded(s, jobs[1])  # idempotent transition allowed from failed? NO:
        s.commit()
        statuses = sorted(j.status for j in s.query(ClaimAnalysisJob))
        assert statuses == ["done", "superseded"]


def test_requeue_stuck_running_bumps_attempts(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(s, workspace_id=ws.id, section_index=0,
                            section_heading="", section_sha="a" * 64, commit_sha="c")
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        # simulate a crash 20 minutes ago
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        s.commit()
        n = requeue_stuck_running(s, older_than_minutes=15)
        s.commit()
        assert n == 1
        refreshed = s.get(ClaimAnalysisJob, job.id)
        assert refreshed.status == "queued"
        assert refreshed.attempts == 2  # 1 from claim + 1 from reap


def test_reaper_dead_letters_exhausted_job(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(s, workspace_id=ws.id, section_index=0,
                            section_heading="", section_sha="a" * 64, commit_sha="c")
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        job.attempts = 2  # next reap bump reaches MAX_ATTEMPTS=3
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        s.commit()
        assert requeue_stuck_running(s, older_than_minutes=15) == 0
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "failed"


def test_reaper_supersedes_when_content_requeued(pg_session_factory):
    """A queued row for the same content already exists (invalidation
    re-enqueued) → reaping must not violate the partial unique index."""
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(s, workspace_id=ws.id, section_index=0,
                            section_heading="", section_sha="a" * 64, commit_sha="c")
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        # same content re-enqueued while the original was (stuck) running
        enqueue_section_job(s, workspace_id=ws.id, section_index=0,
                            section_heading="", section_sha="a" * 64, commit_sha="c2")
        s.commit()
        assert requeue_stuck_running(s, older_than_minutes=15) == 0
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "superseded"
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement** `analysis/queue.py`:

```python
"""claim_analysis_jobs queue helpers. Mirrors ranking/queue.py house pattern:
idempotent enqueue via ON CONFLICT on a partial unique index, batch claim
with FOR UPDATE SKIP LOCKED, stuck-job requeue. Spec 2026-06-10 §3."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from call_manager.db.models import ClaimAnalysisJob

MAX_ATTEMPTS = 3


def enqueue_section_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    section_index: int,
    section_heading: str,
    section_sha: str,
    commit_sha: str,
) -> bool:
    """Insert a queued job unless one already exists for this exact section
    content. Returns True if a row was inserted. Caller commits."""
    stmt = (
        pg_insert(ClaimAnalysisJob)
        .values(
            workspace_id=workspace_id,
            section_index=section_index,
            section_heading=section_heading,
            section_sha=section_sha,
            commit_sha=commit_sha,
            status="queued",
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "section_sha"],
                                index_where=text("status = 'queued'"))
    )
    result = session.execute(stmt)
    return bool(result.rowcount)


def claim_queued_jobs(session: Session, *, limit: int = 5) -> list[ClaimAnalysisJob]:
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(ClaimAnalysisJob.status == "queued")
        .order_by(ClaimAnalysisJob.queued_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    for j in rows:
        j.status = "running"
        j.attempts += 1
        j.updated_at = datetime.now(UTC)
    session.flush()
    return rows


def mark_done(session: Session, job: ClaimAnalysisJob) -> None:
    job.status = "done"
    job.updated_at = datetime.now(UTC)


def mark_failed(session: Session, job: ClaimAnalysisJob, *, error: str) -> None:
    job.status = "queued" if job.attempts < MAX_ATTEMPTS else "failed"
    job.last_error = error[:2000]
    job.updated_at = datetime.now(UTC)


def mark_superseded(session: Session, job: ClaimAnalysisJob) -> None:
    job.status = "superseded"
    job.updated_at = datetime.now(UTC)


def requeue_stuck_running(session: Session, *, older_than_minutes: int = 15) -> int:
    """Reap stuck RUNNING jobs. Mirrors ranking/queue.py:186 semantics:
    attempts are BUMPED on every requeue and exhausted jobs dead-letter to
    FAILED — otherwise a job that crashes the worker mid-flight crash-loops
    forever. A job whose content already has another QUEUED row (e.g. from
    invalidation re-enqueue) is marked superseded instead of queued, which
    would otherwise violate the partial unique index."""
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(ClaimAnalysisJob.status == "running",
                ClaimAnalysisJob.updated_at < cutoff)
        .with_for_update(skip_locked=True)
        .all()
    )
    n = 0
    for j in rows:
        j.attempts += 1
        j.updated_at = datetime.now(UTC)
        if j.attempts >= MAX_ATTEMPTS:
            j.status = "failed"
            j.last_error = (j.last_error or "") + " [reaped: attempts exhausted]"
            continue
        duplicate_queued = (
            session.query(ClaimAnalysisJob.id)
            .filter(ClaimAnalysisJob.workspace_id == j.workspace_id,
                    ClaimAnalysisJob.section_sha == j.section_sha,
                    ClaimAnalysisJob.status == "queued",
                    ClaimAnalysisJob.id != j.id)
            .first()
        )
        if duplicate_queued is not None:
            j.status = "superseded"
        else:
            j.status = "queued"
            n += 1
    return n
```

NOTE: the `mark_failed` retry semantics mean the test's `mark_failed` call (attempts=1 < 3) sets status back to `"queued"` — the test above expects `superseded` after the subsequent `mark_superseded`, which is consistent. Re-read the test once after implementing; if you changed semantics, fix the TEST comment, not the retry logic.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_analysis_queue.py -q` → 3 passed.
- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/analysis tests/test_analysis_queue.py
git commit -m "feat(analysis): content-addressed claim-analysis job queue"
```

---

### Task 6: Save-section service (guards, commit, provenance, Tier-1, enqueue)

**Files:**
- Create: `src/call_manager/proposal_assist/editing/save.py`
- Test: `tests/test_editing_save.py`

- [ ] **Step 1: Write the failing tests.** Repo setup helper (top of test file):

```python
import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.db.models import (
    ArtifactVersion,
    ClaimAnalysisJob,
    TurnJob,
    User,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.editing.save import (
    SaveResult,
    StaleSectionError,
    TurnInFlightError,
    save_section,
)

DOC = "# T\nintro\n## Background\nold body [@x9]\n## Aims\naims\n"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_repo(tmp_path: Path, ws_id: uuid.UUID, doc: str = DOC) -> Path:
    repo = tmp_path / str(ws_id) / "repo"  # production layout (paths.py)
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text(doc)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=repo, check=True, env={**_GIT_ENV})
    return repo


def _ws(session):
    user = User(email="o@example.org", is_admin=False)
    session.add(user); session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws); session.flush()
    return ws, user
```

Tests:

```python
def test_happy_path_save(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        base = current_head(repo)
        result = save_section(
            session=s, workspace=ws, user_id=user.id, section_index=1,
            new_text="## Background\nnew body\n",
            base_commit_sha=base, base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
        )
        s.commit()
        assert isinstance(result, SaveResult)
        assert (repo / "proposal.md").read_text() == (
            "# T\nintro\n## Background\nnew body\n## Aims\naims\n")
        av = s.query(ArtifactVersion).filter_by(commit_sha=result.commit_sha).one()
        assert av.created_by == "user"
        ev = s.query(WorkspaceEvent).filter_by(kind="user_edit").one()
        assert ev.actor_id == user.id
        job = s.query(ClaimAnalysisJob).one()
        assert job.status == "queued" and job.section_index == 1


def _section_sha(repo: Path, index: int) -> str:
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    return split_sections((repo / "proposal.md").read_text())[index].sha


def test_turn_in_flight_rejected(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.flush()
        with pytest.raises(TurnInFlightError):
            save_section(session=s, workspace=ws, user_id=user.id,
                         section_index=1, new_text="## Background\nx\n",
                         base_commit_sha=current_head(repo),
                         base_section_sha=_section_sha(repo, 1),
                         workspaces_dir=tmp_path)


def test_stale_section_rejected_but_unrelated_head_move_allowed(
        pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        old_head = current_head(repo)
        old_sha1 = _section_sha(repo, 1)
        # agent edits section 2 (Aims) → HEAD moves, section 1 unchanged
        doc2 = DOC.replace("aims\n", "aims v2\n")
        (repo / "proposal.md").write_text(doc2)
        subprocess.run(["git", "commit", "-aqm", "agent"], cwd=repo,
                       check=True, env={**_GIT_ENV})
        # save against the OLD head but UNCHANGED section 1 → allowed
        save_section(session=s, workspace=ws, user_id=user.id, section_index=1,
                     new_text="## Background\nuser body\n",
                     base_commit_sha=old_head, base_section_sha=old_sha1,
                     workspaces_dir=tmp_path)
        s.commit()
        assert "user body" in (repo / "proposal.md").read_text()
        # now save against a section that DID change underneath → rejected
        with pytest.raises(StaleSectionError):
            save_section(session=s, workspace=ws, user_id=user.id,
                         section_index=2, new_text="## Aims\nz\n",
                         base_commit_sha=old_head, base_section_sha="0" * 64,
                         workspaces_dir=tmp_path)


def test_tier1_flags_bare_marker_and_missing_newline_glue(
        pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        result = save_section(
            session=s, workspace=ws, user_id=user.id, section_index=1,
            new_text="## Background\nclaim [@hand2024]",  # bare + no trailing \n
            base_commit_sha=current_head(repo),
            base_section_sha=_section_sha(repo, 1), workspaces_dir=tmp_path)
        s.commit()
        assert any(i["type"] == "bare_marker" for i in result.issues)
        # trailing newline auto-appended so '## Aims' still starts a line
        assert "## Aims\n" in (repo / "proposal.md").read_text()
```

- [ ] **Step 2: Run to verify failure** — ImportError on `editing.save`.

- [ ] **Step 3: Implement** `editing/save.py`:

```python
"""User section save: the single write path for owner edits.

Order inside the per-workspace commit mutex (spec §1):
turn-lock check → stale-form check → git commit → ArtifactVersion('user') →
WorkspaceEvent('user_edit') → Tier-1 deterministic validation → Tier-2 job
enqueue. The mutex is transaction-scoped; the caller's session.commit()
releases it. A DB failure AFTER the git commit is recovered by
reconcile_workspace_git_drift + the analysis reconcile tick (spec §6)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ArtifactVersion, TurnJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_bare_markers
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)

PROPOSAL_FILENAME = "proposal.md"
CITATIONS_FILENAME = "citations.json"


class TurnInFlightError(RuntimeError):
    """A drafting turn is queued or running — user writes are locked out."""


class StaleSectionError(RuntimeError):
    """The section changed underneath the user's edit form."""


@dataclass
class SaveResult:
    commit_sha: str
    issues: list[dict] = field(default_factory=list)
    job_enqueued: bool = False


def _turn_in_flight(session: Session, workspace_id: uuid.UUID) -> bool:
    return (
        session.query(TurnJob.id)
        .filter(TurnJob.workspace_id == workspace_id,
                TurnJob.status.in_(("queued", "running")))
        .first()
        is not None
    )


def save_section(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    section_index: int,
    new_text: str,
    base_commit_sha: str,
    base_section_sha: str,
    workspaces_dir: Path,
) -> SaveResult:
    """Replace section *section_index* with *new_text* and commit. Raises
    TurnInFlightError / StaleSectionError / IndexError / GitCommitError.
    Caller commits the session (which releases the commit mutex)."""
    acquire_commit_mutex(session, workspace.id)

    if _turn_in_flight(session, workspace.id):
        raise TurnInFlightError(str(workspace.id))

    repo_dir = workspace_repo_dir(workspaces_dir, workspace.id)
    head = current_head(repo_dir)
    doc = (repo_dir / PROPOSAL_FILENAME).read_text()
    sections = split_sections(doc)
    if not 0 <= section_index < len(sections):
        raise IndexError(f"no section {section_index}")
    live = sections[section_index]

    if head != base_commit_sha and live.sha != base_section_sha:
        raise StaleSectionError(f"section {section_index} changed underneath the form")

    # Keep the join invariant: a non-final section must end with a newline or
    # the next section's '##' would glue onto the user's last line.
    if section_index < len(sections) - 1 and not new_text.endswith("\n"):
        new_text += "\n"

    new_doc = "".join(
        new_text if s.index == section_index else s.text for s in sections
    )
    heading = live.heading or "preamble"
    commit_sha = write_and_commit_files(
        repo_dir, {PROPOSAL_FILENAME: new_doc},
        f"user edit: {heading} (user:{user_id})",
    )

    session.add(ArtifactVersion(
        workspace_id=workspace.id, commit_sha=commit_sha, created_by="user",
        message=f"user edit: {heading}",
    ))
    session.add(WorkspaceEvent(
        workspace_id=workspace.id, kind="user_edit", actor_id=user_id,
        payload={"section_index": section_index, "heading": heading,
                 "commit_sha": commit_sha,
                 "byte_delta": len(new_doc) - len(doc)},
    ))

    # Tier-1 (sync, free): deterministic ledger validation + bare-marker scan.
    cit_path = repo_dir / CITATIONS_FILENAME
    ledger = load_ledger(cit_path.read_text() if cit_path.exists() else None)
    issues = validate_citations_deterministic(
        proposal_text=new_doc, ledger=ledger, session=session,
        workspace_id=workspace.id,
    )
    for bare in find_bare_markers(new_doc):
        issues.append({
            "type": "bare_marker", "severity": "error",
            "citekey": bare["citekey"], "location": f"char {bare['start']}",
            "detail": ("hand-typed citation has no evidence link — use the "
                       "Insert citation button instead"),
        })

    # Tier-2: enqueue analysis for the NEW section content.
    saved = split_sections(new_doc)[section_index]
    enqueued = enqueue_section_job(
        session, workspace_id=workspace.id, section_index=section_index,
        section_heading=saved.heading, section_sha=saved.sha,
        commit_sha=commit_sha,
    )
    return SaveResult(commit_sha=commit_sha, issues=issues, job_enqueued=enqueued)
```

Check the exact `TurnJob` constructor fields used in the test against `db/models.py` (it may require `chat_turn_id` or similar non-nullable columns — if so, create the minimal valid TurnJob the way `tests/test_ranking_queue.py`-style tests construct theirs; look at an existing TurnJob test, e.g. in `tests/test_web_admin_drafting_panel.py`, and copy its construction).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_editing_save.py -q` → 4 passed.
- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/editing/save.py tests/test_editing_save.py
git commit -m "feat(editing): save_section service — mutex, guards, provenance, tier-1, enqueue"
```

---

### Task 7: Section editing web routes + templates

**Files:**
- Create: `src/call_manager/proposal_assist/web/sections.py`
- Create: `src/call_manager/web/templates/proposal_assist/partials/section_view.html`
- Create: `src/call_manager/web/templates/proposal_assist/partials/section_edit.html`
- Modify: `src/call_manager/web/templates/proposal_assist/partials/artifact_pane.html`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (workspace_main context: pass `sections`)
- Modify: `src/call_manager/main.py` (include router)
- Test: `tests/test_web_sections.py`

- [ ] **Step 1: Routes** — `web/sections.py`. Follow the dependency pattern of `proposal_assist/web/workspaces.py` exactly (`Depends(get_current_user)`, `Depends(_session_dep)` — import `_session_dep`, `_require_owner`, `_load_workspace_or_404`, `_render_proposal_markdown` from `.workspaces`):

```python
"""Section-level editing routes (HTMX fragments). Spec 2026-06-10 §1."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.save import (
    StaleSectionError,
    TurnInFlightError,
    save_section,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web.workspaces import (
    _render_proposal_markdown,
    _require_owner,
    _require_owner_or_viewer,
    _session_dep,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/workspaces")


def _read_sections(request: Request, workspace_id: uuid.UUID):
    repo = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    path = repo / "proposal.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="proposal.md not found")
    return current_head(repo), split_sections(path.read_text())


def _section_or_404(sections, n: int):
    if not 0 <= n < len(sections):
        raise HTTPException(status_code=404, detail="no such section")
    return sections[n]


def _view_fragment(request: Request, ws, section, *, is_owner: bool,
                   issues: list[dict] | None = None) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/section_view.html",
        {"workspace": ws, "s": {
            "index": section.index, "heading": section.heading,
            "sha": section.sha,
            "rendered": _render_proposal_markdown(section.text),
        }, "is_owner": is_owner, "issues": issues or []},
    )


@router.get("/{workspace_id}/sections/{n}", response_class=HTMLResponse)
def get_section_view(workspace_id: uuid.UUID, n: int, request: Request,
                     user=Depends(get_current_user),  # noqa: B008
                     session: Session = Depends(_session_dep)):  # noqa: B008
    # Owner-or-viewer, NOT _load_workspace_or_404: that helper does no
    # authorization, and a bare load would let any authenticated user read
    # proposal text by guessing workspace UUIDs (IDOR).
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user,
                                  session=session)
    _, sections = _read_sections(request, workspace_id)
    return _view_fragment(request, ws, _section_or_404(sections, n),
                          is_owner=ws.owner_id == user.id)


@router.get("/{workspace_id}/sections/{n}/edit", response_class=HTMLResponse)
def get_section_edit(workspace_id: uuid.UUID, n: int, request: Request,
                     user=Depends(get_current_user),  # noqa: B008
                     session: Session = Depends(_session_dep)):  # noqa: B008
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    head, sections = _read_sections(request, workspace_id)
    section = _section_or_404(sections, n)
    return request.app.state.templates.TemplateResponse(
        request, "proposal_assist/partials/section_edit.html",
        {"workspace": ws, "s": {"index": section.index,
                                "heading": section.heading,
                                "sha": section.sha, "raw": section.text},
         "base_commit_sha": head},
    )


@router.post("/{workspace_id}/sections/{n}", response_class=HTMLResponse)
def post_section_save(workspace_id: uuid.UUID, n: int, request: Request,
                      content: str = Form(...),
                      base_commit_sha: str = Form(...),
                      base_section_sha: str = Form(...),
                      user=Depends(get_current_user),  # noqa: B008
                      session: Session = Depends(_session_dep)):  # noqa: B008
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    try:
        result = save_section(
            session=session, workspace=ws, user_id=user.id, section_index=n,
            new_text=content, base_commit_sha=base_commit_sha,
            base_section_sha=base_section_sha,
            workspaces_dir=Path(request.app.state.settings.workspaces_dir),
        )
        session.commit()
    except TurnInFlightError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="assistant is working — try again when the turn finishes",
        ) from None
    except StaleSectionError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="this section changed underneath you — reopen it",
        ) from None
    except GitCommitError:
        session.rollback()
        logger.exception("section save commit failed ws=%s", workspace_id)
        raise HTTPException(status_code=500, detail="save failed — nothing written") from None
    _, sections = _read_sections(request, workspace_id)
    return _view_fragment(request, ws, _section_or_404(sections, n),
                          is_owner=True, issues=result.issues)
```

- [ ] **Step 2: Templates.**

`partials/section_view.html`:

```html
<div id="section-{{ s.index }}" class="proposal-section">
  {{ s.rendered | safe }}
  {% if is_owner %}
  <button hx-get="/workspaces/{{ workspace.id }}/sections/{{ s.index }}/edit"
          hx-target="#section-{{ s.index }}" hx-swap="outerHTML"
          class="section-edit-btn">Edit</button>
  {% endif %}
  {% if issues %}
  <ul class="tier1-issues">
    {% for i in issues %}<li class="issue-{{ i.severity }}">{{ i.type }}: {{ i.detail }}</li>{% endfor %}
  </ul>
  {% endif %}
  <div id="annotations-{{ s.index }}"
       hx-get="/workspaces/{{ workspace.id }}/sections/{{ s.index }}/annotations?sha={{ s.sha }}"
       hx-trigger="load" hx-swap="innerHTML"></div>
</div>
```

`partials/section_edit.html`:

```html
<div id="section-{{ s.index }}" class="proposal-section editing">
  <form hx-post="/workspaces/{{ workspace.id }}/sections/{{ s.index }}"
        hx-target="#section-{{ s.index }}" hx-swap="outerHTML">
    <textarea name="content" rows="14" style="width:100%">{{ s.raw }}</textarea>
    <input type="hidden" name="base_commit_sha" value="{{ base_commit_sha }}">
    <input type="hidden" name="base_section_sha" value="{{ s.sha }}">
    <button type="submit">Save</button>
    <button type="button"
            hx-get="/workspaces/{{ workspace.id }}/sections/{{ s.index }}"
            hx-target="#section-{{ s.index }}" hx-swap="outerHTML">Cancel</button>
  </form>
</div>
```

`partials/artifact_pane.html` — replace the single `#artifact-rendered` div body with a sections loop (keep the header links unchanged):

```html
<div id="artifact-rendered" style="border: 1px solid #ddd; padding: 0.5em; max-height: 70vh; overflow-y: auto;">
  {% for s in artifact_sections %}
    {% with issues=[] %}{% include "proposal_assist/partials/section_view.html" %}{% endwith %}
  {% endfor %}
</div>
```

NOTE on `{% include %}` scoping: `section_view.html` expects `s`, `workspace`, `is_owner`, `issues` in context — the loop variable `s` and outer `workspace`/`is_owner` are visible to includes in Jinja2 by default. The annotations div (`hx-trigger="load"`) 404s until Task 11 adds the route — to keep this task green, have the include guard it: wrap the annotations div in `{% if annotations_enabled is not defined or annotations_enabled %}` and pass `annotations_enabled=False` from `workspace_main` context for now (flipped to True in Task 11).

- [ ] **Step 3: Wire context.** In `web/workspaces.py` `get_workspace_main`, after `rendered = ...` add:

```python
from call_manager.proposal_assist.editing.sectionizer import split_sections  # top of file

artifact_sections = [
    {"index": s.index, "heading": s.heading, "sha": s.sha,
     "rendered": _render_proposal_markdown(s.text)}
    for s in split_sections(raw)
    if s.text  # skip an empty preamble
]
```

and pass `"artifact_sections": artifact_sections, "annotations_enabled": False` in the template context dict. In `main.py`, import and `app.include_router(sections_router)` next to the other proposal-assist routers (line ~357), importing `from call_manager.proposal_assist.web.sections import router as sections_router`.

NOTE: `section_view.html` receives plain dicts here (`s.index` etc. work on dicts in Jinja via attribute fallback — Jinja tries attribute first then item; for dicts use `s["index"]`-safe syntax: in the template write `{{ s.index }}` which works for both dataclass and dict in Jinja2's getattr-then-getitem order).

- [ ] **Step 4: Route tests** — `tests/test_web_sections.py`. Mirror the app/client fixture construction of the existing workspace-route tests (open `tests/test_web_admin_drafting_panel.py` or the workspace tests and copy the client + logged-in-owner setup; workspaces need a git repo on disk — reuse the `_init_repo` helper from `tests/test_editing_save.py` by importing it or duplicating the 10 lines). Test bodies:

```python
def test_edit_fragment_contains_raw_and_base_sha(client_owner, ws_with_repo):
    r = client_owner.get(f"/workspaces/{ws_with_repo.id}/sections/1/edit")
    assert r.status_code == 200
    assert "base_commit_sha" in r.text and "<textarea" in r.text


def test_save_roundtrip_updates_view(client_owner, ws_with_repo):
    edit = client_owner.get(f"/workspaces/{ws_with_repo.id}/sections/1/edit")
    import re
    base = re.search(r'name="base_commit_sha" value="([0-9a-f]+)"', edit.text)[1]
    sha = re.search(r'name="base_section_sha" value="([0-9a-f]+)"', edit.text)[1]
    r = client_owner.post(
        f"/workspaces/{ws_with_repo.id}/sections/1",
        data={"content": "## Background\nedited!\n",
              "base_commit_sha": base, "base_section_sha": sha})
    assert r.status_code == 200
    assert "edited!" in r.text


def test_viewer_cannot_edit(client_viewer, ws_with_repo):
    r = client_viewer.get(f"/workspaces/{ws_with_repo.id}/sections/1/edit")
    assert r.status_code == 403


def test_save_during_turn_409(client_owner, ws_with_repo_and_queued_turn):
    ws = ws_with_repo_and_queued_turn
    r = client_owner.post(f"/workspaces/{ws.id}/sections/1",
                          data={"content": "## Background\nx\n",
                                "base_commit_sha": "0" * 40,
                                "base_section_sha": "0" * 64})
    assert r.status_code == 409


def test_outsider_cannot_read_sections(client_outsider, ws_with_repo):
    """IDOR regression: an authenticated non-member (neither owner nor
    viewer) must not read proposal text via the section-view route."""
    r = client_outsider.get(f"/workspaces/{ws_with_repo.id}/sections/1")
    assert r.status_code == 403
```

(`client_outsider` = a logged-in user with no relation to the workspace —
same fixture machinery as `client_viewer` minus the WorkspaceViewer row.)

(Define `client_owner`, `client_viewer`, `ws_with_repo`, `ws_with_repo_and_queued_turn` as local fixtures in this file following the existing web-test pattern.)

- [ ] **Step 5: Run** — `uv run pytest tests/test_web_sections.py tests/test_editing_save.py -q` → all pass. Also run the existing workspace web tests to catch template regressions: `uv run pytest tests/test_profiles_redesign_e2e.py -q -k workspace || uv run pytest tests/ -q -k "workspace and web"` → no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/sections.py src/call_manager/web/templates/proposal_assist/partials/ src/call_manager/proposal_assist/web/workspaces.py src/call_manager/main.py tests/test_web_sections.py
git commit -m "feat(editing): section edit/save HTMX routes + sectioned artifact pane"
```

---

### Task 8: Turn enqueue joins the commit mutex

**Files:**
- Modify: `src/call_manager/proposal_assist/turns/submit.py`
- Test: `tests/test_editing_enqueue_race.py`

- [ ] **Step 1: Write the failing test**

```python
import threading
import time
import uuid

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.turns.submit import submit_user_turn

# reuse _ws + settings fixture pattern from tests/test_editing_save.py /
# existing submit tests (see tests/test_ranking_queue.py for settings stubs;
# the existing submit_user_turn tests show the minimal Settings needed).


def test_submit_blocks_while_commit_mutex_held(pg_session_factory, settings):
    with pg_session_factory() as s_user:
        ws, user = _ws(s_user)
        s_user.commit()
        ws_id, user_id = ws.id, user.id

    order = []
    s_hold = pg_session_factory()
    acquire_commit_mutex(s_hold, ws_id)  # simulate an in-flight user save
    order.append("save-holding")

    def enqueue():
        with pg_session_factory() as s2:
            submit_user_turn(session=s2, workspace_id=ws_id,
                             content_text="hi", submitted_by_user_id=user_id,
                             settings=settings)
            s2.commit()
            order.append("enqueued")

    t = threading.Thread(target=enqueue)
    t.start()
    time.sleep(0.3)
    assert order == ["save-holding"]  # enqueue is waiting on the mutex
    s_hold.commit()
    t.join(timeout=5)
    assert order == ["save-holding", "enqueued"]
    s_hold.close()
```

- [ ] **Step 2: Run to verify failure** — the thread enqueues immediately (order becomes `["save-holding", "enqueued"]` before the sleep assert) → FAIL on the first `assert order == ["save-holding"]`.

- [ ] **Step 3: Implement.** In `turns/submit.py` `submit_user_turn`, first statement of the function body:

```python
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex  # top of file

    # Spec 2026-06-10 §1: turn enqueue participates in the per-workspace commit
    # mutex so a user save's "no queued/running turn" check is authoritative —
    # either this enqueue lands first (save sees it → 409) or the save's
    # transaction commits first (turn starts strictly after the user's commit).
    acquire_commit_mutex(session, workspace_id)
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_editing_enqueue_race.py -q` → 1 passed. Then run the existing submit/turn tests: `uv run pytest tests/ -q -k "turn"` → no new failures.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/turns/submit.py tests/test_editing_enqueue_race.py
git commit -m "feat(editing): turn enqueue participates in the workspace commit mutex"
```

---

### Task 9: Agent view-of-HEAD precondition + mutex in mutation tools

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (ToolContext)
- Modify: `src/call_manager/proposal_assist/tools/proposal.py`
- Modify: `src/call_manager/proposal_assist/tools/citations.py` (mutation impls update view + take mutex)
- Test: `tests/test_tools_head_precondition.py`

- [ ] **Step 1: ToolContext field.** In `registry.py` `ToolContext`, add:

```python
    # Spec 2026-06-10 §1: the agent's view of HEAD. Updated by read_proposal
    # and by each successful mutation (tools return the new commit SHA).
    # replace_proposal/edit_proposal refuse to write when this differs from
    # actual HEAD — a blind overwrite of text the agent never saw becomes a
    # tool error instead of a lost update.
    agent_view_head: str | None = field(default=None)
```

- [ ] **Step 2: Write the failing tests** (drive `*_impl` functions directly with a ToolContext over a tmp git repo, the same way existing `tests/` tool tests do — see the existing tests for `edit_proposal`/`insert_citation` impls and copy their ToolContext construction):

```python
import pytest

from call_manager.proposal_assist.citations.gitio import current_head


async def test_replace_requires_current_view(tool_ctx, repo):
    from call_manager.proposal_assist.tools.proposal import (
        read_proposal_impl, replace_proposal_impl,
    )
    # never read → refused
    out = await replace_proposal_impl(tool_ctx, content="# clobber\n",
                                      triggering_turn_id=None)
    assert "error" in out and "read" in out["error"]

    await read_proposal_impl(tool_ctx)
    assert tool_ctx.agent_view_head == current_head(repo)

    # external (user) commit moves HEAD behind the agent's back
    (repo / "proposal.md").write_text("user text\n")
    import subprocess
    subprocess.run(["git", "commit", "-aqm", "user edit: x (user:1)"],
                   cwd=repo, check=True, env=_GIT_ENV)
    out = await replace_proposal_impl(tool_ctx, content="# clobber\n",
                                      triggering_turn_id=None)
    assert "error" in out
    assert (repo / "proposal.md").read_text() == "user text\n"  # not lost

    # after re-read, replace succeeds and view advances
    await read_proposal_impl(tool_ctx)
    out = await replace_proposal_impl(tool_ctx, content="# rewritten\n",
                                      triggering_turn_id=None)
    assert "commit_sha" in out
    assert tool_ctx.agent_view_head == out["commit_sha"]


async def test_multi_edit_turn_keeps_working(tool_ctx, repo):
    from call_manager.proposal_assist.tools.proposal import (
        edit_proposal_impl, read_proposal_impl,
    )
    await read_proposal_impl(tool_ctx)
    out1 = await edit_proposal_impl(
        tool_ctx, edits=[{"old_string": "intro", "new_string": "intro v2"}],
        triggering_turn_id=None)
    assert "commit_sha" in out1
    # second edit WITHOUT re-read must still work (view advanced on success)
    out2 = await edit_proposal_impl(
        tool_ctx, edits=[{"old_string": "aims", "new_string": "aims v2"}],
        triggering_turn_id=None)
    assert "commit_sha" in out2


async def test_concurrent_edits_serialized_no_lost_update(tool_ctx, repo):
    """Two concurrent edit calls must both land (write_lock serializes
    compute-then-commit); the document ends with BOTH changes."""
    import asyncio
    from call_manager.proposal_assist.tools.proposal import (
        edit_proposal_impl, read_proposal_impl,
    )
    await read_proposal_impl(tool_ctx)
    r1, r2 = await asyncio.gather(
        edit_proposal_impl(tool_ctx,
                           edits=[{"old_string": "intro", "new_string": "intro A"}],
                           triggering_turn_id=None),
        edit_proposal_impl(tool_ctx,
                           edits=[{"old_string": "aims", "new_string": "aims B"}],
                           triggering_turn_id=None),
    )
    assert "commit_sha" in r1 and "commit_sha" in r2
    final = (repo / "proposal.md").read_text()
    assert "intro A" in final and "aims B" in final  # neither edit lost
```

- [ ] **Step 3: Run to verify failure**, then implement in `tools/proposal.py`:

In `read_proposal_impl`, after reading the file:

```python
    try:
        ctx.agent_view_head = current_head(ctx.repo_dir)
    except GitCommitError:
        ctx.agent_view_head = None
    return {"content": path.read_text()}
```

**The precondition must compare an IMMUTABLE captured SHA, not the shared
`ctx.agent_view_head` field.** Failure mode if you check the shared field
inside the mutex: two concurrent tool calls both compute new content from old
HEAD; call A commits and advances `ctx.agent_view_head`; call B's check then
compares the *advanced* shared value against the *new* HEAD — passes — and
writes content computed from the stale document, silently dropping A's edit.
So:

1. Wrap the whole body of `edit_proposal_impl` AND `replace_proposal_impl` in
   `async with ctx.write_lock:` (exactly like `insert_citation_impl` in
   `tools/citations.py:323` — the same in-turn serialization the citation
   tools already use).
2. At the top of each impl (inside the write lock, BEFORE reading the file or
   computing content), capture the immutable precondition value:

```python
    expected_head = ctx.agent_view_head
    if expected_head is None:
        return {"error": ("you haven't read the proposal yet — call "
                          "read_proposal first")}
    try:
        if current_head(ctx.repo_dir) != expected_head:
            return {"error": ("proposal changed since you read it — call "
                              "read_proposal and retry")}
    except GitCommitError as exc:
        return {"error": f"cannot resolve HEAD: {exc}"}
```

3. Pass `expected_head` into `_write_and_commit(ctx, ..., expected_head=expected_head)`.
   Inside `_write_and_commit`: open a session from `ctx.session_factory`,
   `acquire_commit_mutex(session, ctx.workspace_id)`, then the authoritative
   check INSIDE the mutex — `current_head(ctx.repo_dir) == expected_head`,
   returning the same "proposal changed" error dict on mismatch — then the
   existing git write + ArtifactVersion insert on that session, commit, and
   on success `ctx.agent_view_head = new_sha`. Preserve the existing
   rollback-on-failure file-restore behavior exactly — read the current
   `_write_and_commit` body first and thread the mutex/session/precondition
   around it rather than rewriting it.

(With `ctx.write_lock` held, two in-turn tool calls can no longer interleave
compute-then-commit at all; the captured `expected_head` additionally protects
against cross-process writers — user saves — between the agent's read and its
mutation.)

In `tools/citations.py`: in `_insert_citation_locked` (and the equivalents for `remove_citation`/`update_citation`/`rename_citekey` — find them with `grep -n "write_and_commit_files" src/call_manager/proposal_assist/tools/citations.py`), the function already receives a `session` — add `acquire_commit_mutex(session, ctx.workspace_id)` as its first statement, and after each successful `write_and_commit_files(...)` call set `ctx.agent_view_head = <returned sha>`. Citation tools do NOT need the view-of-HEAD *precondition* (they're anchor-validated like edit_proposal), but they must keep the view current so a following replace doesn't spuriously fail.

- [ ] **Step 4: Run** — `uv run pytest tests/test_tools_head_precondition.py -q` → 2 passed. Then the full tools/citations test files: `uv run pytest tests/ -q -k "tool or citation"` → no new failures. (Existing agent-tool tests that call `edit_proposal_impl` without a prior read WILL now fail — fix those tests by adding the `read_proposal_impl` call, which mirrors what the real agent does; do NOT weaken the guard.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/ tests/test_tools_head_precondition.py tests/
git commit -m "feat(drafting): view-of-HEAD precondition + commit mutex on agent mutation tools"
```

---

### Task 10: Analysis pipeline

**Files:**
- Create: `src/call_manager/proposal_assist/analysis/pipeline.py`
- Test: `tests/test_analysis_pipeline.py`

**Contracts (read first):**
- `iter_proposal_sentences(text) -> Iterator[(sentence_text_marker_stripped, char_offset)]` from `citations/claims.py`.
- `find_markers(text)` from `citations/markers.py` → `[{citekey, ev_id, start, end}]`.
- `CitationLLMService.complete_json(*, workspace_id, op, system, user) -> CitationLLMResult` (`.data: dict|None`, `.cap_exceeded: bool`).
- `validate_citations_semantic(*, proposal_text, ledger, session, session_factory, workspace_id, citation_service, deterministic_issues) -> list[dict]` — never raises; issues carry `evidence_id` + `type`.
- `find_quote_offsets(chunk_text, quote)` from `citations/quotes.py` — raises `QuoteNotFound`/`QuoteAmbiguous`.
- `ReferenceChunk.embedding.cosine_distance(vec)` retrieval pattern from `tools/references.py::search_references_impl` (copy the query, not the tool).
- `Embedder` protocol from `references/embed.py` (`.model`, `.embed(text)`); `FakeEmbedder` exists for tests.

- [ ] **Step 1: Implement** `analysis/pipeline.py` (write tests in Step 2 against this exact surface):

```python
"""Per-job Tier-2 analysis. Spec 2026-06-10 §3.

Citation state decides the branch BEFORE any cache lookup (rev-3 finding 2):
cited occurrences always go through the evidence-ledger semantic audit;
only uncited occurrences use claim_classifications."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    UserClaimAnnotation,
)
from call_manager.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
)
from call_manager.proposal_assist.citations.audit import validate_citations_semantic
from call_manager.proposal_assist.citations.claims import iter_proposal_sentences
from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
)

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_VERSION = "v1"
TOP_K = 3

CLASSIFY_SYSTEM = (
    "You decide whether a sentence from a grant proposal asserts a checkable "
    "factual claim that needs literature support. Definitions, signposting, "
    "and the proposal's own aims or plans are NOT claims. Reply ONLY with "
    'JSON: {"is_claim": true|false, "reason": "<short>"}'
)

VERDICT_SYSTEM = (
    "You judge whether a reference excerpt supports a claim. Reply ONLY with "
    'JSON: {"verdict": "supports"|"insufficient", '
    '"evidence_quote": "<verbatim quote from the excerpt, or empty>", '
    '"rationale": "<short>"}'
)


class AnalysisCapExceeded(RuntimeError):
    pass


@dataclass
class SentenceOcc:
    ordinal: int
    text: str  # marker-stripped, whitespace-collapsed
    offset: int  # char offset within the section text
    ev_ids: list[str]  # markers whose span falls inside this sentence


def _sentences_with_citation_state(section_text: str) -> list[SentenceOcc]:
    sentences = list(iter_proposal_sentences(section_text))
    markers = find_markers(section_text)
    out: list[SentenceOcc] = []
    for i, (text_, offset) in enumerate(sentences):
        end = sentences[i + 1][1] if i + 1 < len(sentences) else len(section_text)
        ev_ids = [m["ev_id"] for m in markers if offset <= m["start"] < end]
        out.append(SentenceOcc(ordinal=i, text=text_, offset=offset, ev_ids=ev_ids))
    return out


def _claim_sha(sentence_text: str) -> str:
    import hashlib
    return hashlib.sha256(sentence_text.encode()).hexdigest()


def _classify(citation_service, workspace_id, sentence: str) -> bool:
    res = citation_service.complete_json(
        workspace_id=workspace_id, op="user_claim_classify",
        system=CLASSIFY_SYSTEM, user=f"Sentence:\n{sentence}")
    if res.cap_exceeded:
        raise AnalysisCapExceeded("token cap")
    if res.data is None:
        raise RuntimeError("classify LLM returned no data")
    return bool(res.data.get("is_claim"))


def _retrieve_and_judge(session, citation_service, embedder, workspace_id,
                        claim: str) -> dict | None:
    """Top-k chunk retrieval + per-candidate verdict with verbatim-verified
    quote. Returns {reference_id, chunk_index, evidence_quote, verdict_json}
    or None (no support)."""
    qvec = embedder.embed(claim)
    rows = (
        session.query(ReferenceChunk, Reference)
        .join(Reference, Reference.id == ReferenceChunk.reference_id)
        .filter(ReferenceChunk.workspace_id == workspace_id,
                ReferenceChunk.embedding_model == embedder.model,
                Reference.tombstoned_at.is_(None),
                Reference.text_status == "done")
        .order_by(ReferenceChunk.embedding.cosine_distance(qvec))
        .limit(TOP_K)
        .all()
    )
    for chunk, ref in rows:
        res = citation_service.complete_json(
            workspace_id=workspace_id, op="user_claim_verdict",
            system=VERDICT_SYSTEM,
            user=f"Claim:\n{claim}\n\nExcerpt:\n{chunk.text}")
        if res.cap_exceeded:
            raise AnalysisCapExceeded("token cap")
        data = res.data or {}
        quote = (data.get("evidence_quote") or "").strip()
        if data.get("verdict") != "supports" or not quote:
            continue
        try:
            find_quote_offsets(chunk.text, quote)  # fail-closed verification
        except (QuoteNotFound, QuoteAmbiguous):
            logger.info("discarding unverifiable quote ref=%s", ref.id)
            continue
        return {"reference_id": ref.id, "chunk_index": chunk.chunk_index,
                "evidence_quote": quote, "verdict_json": data}
    return None


def _upsert_annotation(session: Session, **kw) -> None:
    """Insert a fresh finding, or REOPEN/refresh a previously-staled row for
    the same occurrence. Resolved rows stay resolved: dismissed/inserted are
    never reopened by re-analysis (spec §3) — only 'stale' rows (set at the
    start of fan-out) flip back to 'open'. This is what prevents the
    contradictory strip where an old open uncited_no_match coexists with a
    fresh uncited_supported for the same sentence after a library change."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(UserClaimAnnotation).values(**kw)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_user_claim_annotations_occurrence",
        set_={
            "status": "open",
            "reference_id": stmt.excluded.reference_id,
            "chunk_index": stmt.excluded.chunk_index,
            "evidence_quote": stmt.excluded.evidence_quote,
        },
        where=(UserClaimAnnotation.status == "stale"),
    )
    session.execute(stmt)


def _stale_open_annotations_for_section(session: Session, *, workspace_id,
                                        section_sha: str) -> None:
    """Mark every OPEN annotation for this section content stale before
    fan-out; current findings reopen/replace them, superseded ones stay
    hidden. Dismissed/inserted rows are untouched."""
    session.query(UserClaimAnnotation).filter_by(
        workspace_id=workspace_id, section_sha=section_sha, status="open",
    ).update({"status": "stale"}, synchronize_session=False)


def analyze_job(
    job: ClaimAnalysisJob,
    *,
    session: Session,
    session_factory: sessionmaker,
    citation_service,
    embedder,
    workspaces_dir: Path,
) -> None:
    """Analyze one job. Raises on transient failure (caller marks failed)."""
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.paths import workspace_repo_dir

    repo = workspace_repo_dir(workspaces_dir, job.workspace_id)
    doc = (repo / "proposal.md").read_text()
    sections = split_sections(doc)
    occurrences = [s for s in sections if s.sha == job.section_sha]
    if not occurrences:
        mark_superseded(session, job)
        return

    cit_path = repo / "citations.json"
    ledger = load_ledger(cit_path.read_text() if cit_path.exists() else None)
    section = occurrences[0]  # identical content — analyze once, fan out rows
    sentences = _sentences_with_citation_state(section.text)

    # ---- cited occurrences: evidence-ledger audit (existing machinery) ----
    # Diff-scope by filtering the LEDGER to this section's evidence rows:
    # validate_citations_semantic judges every row of the ledger it is given,
    # so passing the full ledger would re-audit the whole document per job.
    cited_evs = {ev for s in sentences for ev in s.ev_ids}
    semantic_issues: list[dict] = []
    if cited_evs:
        scoped_ledger = {**ledger, "evidence": [
            r for r in (ledger.get("evidence") or [])
            if isinstance(r, dict) and r.get("id") in cited_evs]}
        semantic_issues = validate_citations_semantic(
            proposal_text=doc, ledger=scoped_ledger, session=session,
            session_factory=session_factory, workspace_id=job.workspace_id,
            citation_service=citation_service, deterministic_issues=[])
    # validate_citations_semantic NEVER raises: cap-exceeded and transient
    # LLM errors surface as 'audit_unavailable' issues. Treat those as a
    # transient job failure so the job retries instead of being marked done
    # with silently-missing findings.
    if any(i.get("type") == "audit_unavailable" for i in semantic_issues):
        raise RuntimeError("citation audit unavailable (cap or LLM error)")
    # Real issue types from audit.py:489-491 — semantic_contradicts (error),
    # semantic_unsupported (warning). semantic_irrelevant is excluded: an
    # irrelevant *background* cite is not a wrong claim-support.
    bad_by_ev = {i["evidence_id"]: i for i in semantic_issues
                 if i.get("evidence_id") in cited_evs
                 and i.get("type") in ("semantic_unsupported",
                                       "semantic_contradicts")}

    # ---- uncited occurrences: classify (cached) → retrieve → judge ----
    results: dict[str, ClaimClassification] = {}
    for occ in sentences:
        if occ.ev_ids or not occ.text:
            continue
        sha = _claim_sha(occ.text)
        if sha in results:
            continue
        cached = (
            session.query(ClaimClassification)
            .filter_by(workspace_id=job.workspace_id, claim_sha=sha,
                       prompt_version=ANALYSIS_PROMPT_VERSION)
            .one_or_none()
        )
        if cached is not None and cached.stale_at is None:
            results[sha] = cached
            continue
        is_claim = _classify(citation_service, job.workspace_id, occ.text)
        outcome, found = "not_claim", None
        if is_claim:
            found = _retrieve_and_judge(session, citation_service, embedder,
                                        job.workspace_id, occ.text)
            outcome = "supported" if found else "no_match"
        if cached is None:
            cached = ClaimClassification(
                workspace_id=job.workspace_id, claim_sha=sha,
                prompt_version=ANALYSIS_PROMPT_VERSION, is_claim=is_claim,
                outcome=outcome)
            session.add(cached)
        else:
            cached.is_claim, cached.outcome, cached.stale_at = is_claim, outcome, None
        cached.reference_id = found["reference_id"] if found else None
        cached.chunk_index = found["chunk_index"] if found else None
        cached.evidence_quote = found["evidence_quote"] if found else None
        cached.verdict_json = found["verdict_json"] if found else None
        session.flush()
        results[sha] = cached

    # ---- fan out occurrence annotations (per duplicate section) ----
    _stale_open_annotations_for_section(session, workspace_id=job.workspace_id,
                                        section_sha=job.section_sha)
    for occ_idx in range(len(occurrences)):
        for occ in sentences:
            common = dict(
                workspace_id=job.workspace_id, section_sha=job.section_sha,
                section_occurrence=occ_idx, section_heading=section.heading,
                sentence_ordinal=occ.ordinal, char_offset=occ.offset,
                claim_sha=_claim_sha(occ.text), claim_text=occ.text)
            if occ.ev_ids:
                for ev in occ.ev_ids:
                    if ev in bad_by_ev:
                        _upsert_annotation(session, kind="cited_unsupported",
                                           evidence_id=ev, **common)
            else:
                c = results.get(_claim_sha(occ.text))
                if c is None or not c.is_claim:
                    continue
                if c.outcome == "supported":
                    _upsert_annotation(
                        session, kind="uncited_supported", evidence_id="",
                        reference_id=c.reference_id, chunk_index=c.chunk_index,
                        evidence_quote=c.evidence_quote, **common)
                else:
                    _upsert_annotation(session, kind="uncited_no_match",
                                       evidence_id="", **common)
    mark_done(session, job)


def drain_analysis_queue(
    *,
    session_factory: sessionmaker,
    citation_service,
    embedder,
    workspaces_dir: Path,
    batch_size: int = 3,
) -> dict:
    """One drain tick: requeue stuck, claim a batch, analyze each in its own
    transaction. Returns counters for logging."""
    done = failed = superseded = 0
    with session_factory() as s:
        requeue_stuck_running(s)
        s.commit()
    with session_factory() as s:
        jobs = claim_queued_jobs(s, limit=batch_size)
        s.commit()
        job_ids = [j.id for j in jobs]
    for job_id in job_ids:
        with session_factory() as s:
            job = s.get(ClaimAnalysisJob, job_id)
            try:
                analyze_job(job, session=s, session_factory=session_factory,
                            citation_service=citation_service,
                            embedder=embedder, workspaces_dir=workspaces_dir)
                s.commit()
                if job.status == "superseded":
                    superseded += 1
                else:
                    done += 1
            except Exception as exc:  # noqa: BLE001 — job isolation
                s.rollback()
                with session_factory() as s2:
                    j2 = s2.get(ClaimAnalysisJob, job_id)
                    mark_failed(s2, j2, error=str(exc))
                    s2.commit()
                failed += 1
                logger.exception("claim-analysis job %s failed", job_id)
    return {"done": done, "failed": failed, "superseded": superseded}
```

- [ ] **Step 2: Write tests** — `tests/test_analysis_pipeline.py`. Build a fake citation service + `FakeEmbedder`:

```python
class FakeLLMResult:
    def __init__(self, data): self.data, self.cap_exceeded = data, False

class FakeCitationService:
    """Scripted: classify answers by sentence content, verdicts by chunk."""
    def __init__(self): self.calls = []
    def complete_json(self, *, workspace_id, op, system, user):
        self.calls.append(op)
        if op == "user_claim_classify":
            return FakeLLMResult({"is_claim": "claim" in user.lower()})
        return FakeLLMResult({"verdict": "supports",
                              "evidence_quote": "verified words",
                              "rationale": "r"})
```

Test scenarios (each builds a workspace + git repo as in Task 6, plus a `Reference` with `text_status="done"` and one `ReferenceChunk` whose `text` contains `"verified words"` and whose `embedding` comes from `FakeEmbedder`; enqueue via `enqueue_section_job`, then call `drain_analysis_queue` with the fakes):

1. `test_uncited_claim_gets_supported_annotation` — section with "This is a factual claim." → one `uncited_supported` annotation with the reference and quote.
2. `test_unverifiable_quote_discarded_then_no_match` — chunk text WITHOUT "verified words" → quote verification fails → `uncited_no_match`.
3. `test_not_claim_sentences_cached_zero_llm_on_rerun` — run drain twice on the same content (delete the done job and re-enqueue the same `section_sha` between runs); assert `FakeCitationService.calls` count does not grow on the second run for classification.
4. `test_cited_occurrence_audited_even_when_uncited_cached` — same prose cited in one sentence (manufacture a ledger row + canonical marker) and uncited in another; monkeypatch `pipeline.validate_citations_semantic` to return `[{"type": "semantic_unsupported", "evidence_id": "<the ev id>", ...}]` and assert (a) it was CALLED with a ledger scoped to only this section's evidence rows, and (b) a `cited_unsupported` annotation with that evidence_id appears.
5. `test_superseded_when_content_gone` — enqueue, then rewrite proposal.md to remove the section before draining → job `superseded`, zero annotations.
6. `test_duplicate_sections_fan_out` — document with two byte-identical `## Dup` sections → annotations for `section_occurrence` 0 AND 1.
7. `test_cap_exceeded_fails_job` — fake returns `cap_exceeded=True` → job failed with "token cap" in `last_error` (after MAX_ATTEMPTS re-queues; assert status queued + attempts incremented on first failure).
8. `test_audit_unavailable_retries_job` — monkeypatch `pipeline.validate_citations_semantic` to return `[{"type": "audit_unavailable", "evidence_id": "..."}]` for a cited section → `analyze_job` raises, drain marks the job queued (retry), NOT done.
9. `test_reanalysis_supersedes_stale_open_annotations` — first run produces an open `uncited_no_match`; add a matching reference + chunk, delete the done job row, re-enqueue the same content, drain again → the old `uncited_no_match` row is `stale` and a fresh open `uncited_supported` exists for the same occurrence (no contradictory strip); a `dismissed` row in the same position stays `dismissed`.

(Scenario 4 is the trickiest — if scripting `validate_citations_semantic`'s internals proves brittle, monkeypatch `pipeline.validate_citations_semantic` to return a canned issue list and assert it was CALLED for the cited sentence; the unit contract is "cited → audit path invoked", not the audit's internals, which have their own tests.)

- [ ] **Step 3: Run** — `uv run pytest tests/test_analysis_pipeline.py -q` → 7 passed.
- [ ] **Step 4: Commit**

```bash
git add src/call_manager/proposal_assist/analysis/pipeline.py tests/test_analysis_pipeline.py
git commit -m "feat(analysis): claim-analysis pipeline — cited audit, cached classification, verified retrieval, occurrence fan-out"
```

---

### Task 11: Annotation strip UI + insert/dismiss/reanalyze routes + occurrence-aware insert

**Files:**
- Create: `src/call_manager/proposal_assist/analysis/insert.py`
- Create: `src/call_manager/proposal_assist/web/annotations.py`
- Create: `src/call_manager/web/templates/proposal_assist/partials/annotation_strip.html`
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (`annotations_enabled=True`)
- Modify: `src/call_manager/main.py` (include router)
- Test: `tests/test_analysis_insert.py`, `tests/test_web_annotations.py`

- [ ] **Step 1: Occurrence-aware insert service** — `analysis/insert.py`:

```python
"""User-initiated citation insert from an annotation. Canonical path ONLY
(spec §4): evidence ledger row + [@citekey]<!-- ev:id --> marker, one atomic
commit of proposal.md + citations.json. Occurrence-aware idempotency: no-op
only when THIS annotation is already inserted."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.citations import ledger as ledger_mod
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.citations.markers import AnchorError, insert_marker
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import _turn_in_flight
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)

_ANCHOR_GROW = 40
_ANCHOR_TRIES = 4


class InsertConflict(RuntimeError):
    """Text changed / turn in flight / anchor unresolvable — surface to user."""


def _raw_sentence_slice(section_text: str, char_offset: int) -> str:
    """The raw (marker-free for uncited claims) sentence text starting at
    char_offset, ending at the first terminator. Mirrors claims.py
    terminators (.!?\\n)."""
    end = len(section_text)
    for i in range(char_offset, len(section_text)):
        if section_text[i] in ".!?\n":
            end = i + 1
            break
    return section_text[char_offset:end].rstrip("\n")


def _build_unique_anchor(doc: str, section_start: int, sentence_raw: str) -> str:
    """Extend the anchor leftward with section context until unique in doc.
    Raises InsertConflict if no unique anchor exists (e.g. byte-identical
    duplicate sections — spec: fail cleanly, never guess)."""
    anchor = sentence_raw
    grow_from = doc.find(sentence_raw, section_start)
    for _ in range(_ANCHOR_TRIES):
        if doc.count(anchor) == 1:
            return anchor
        start = max(0, grow_from - (len(anchor) - len(sentence_raw)) - _ANCHOR_GROW)
        anchor = doc[start:grow_from] + sentence_raw
        grow_from = start
    raise InsertConflict(
        "repeated text — cite it via chat or edit the sentence to be unique")


def insert_from_annotation(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    annotation: UserClaimAnnotation,
    workspaces_dir: Path,
) -> dict:
    """Returns {"ev_id", "citekey", "commit_sha"} (or {"idempotent": True})."""
    acquire_commit_mutex(session, workspace.id)
    if _turn_in_flight(session, workspace.id):
        raise InsertConflict("assistant is working — try again shortly")
    if annotation.status == "inserted":
        return {"idempotent": True}
    if annotation.status != "open" or annotation.kind != "uncited_supported":
        raise InsertConflict("annotation is no longer actionable")

    ref = session.get(Reference, annotation.reference_id)
    if ref is None or ref.tombstoned_at is not None:
        raise InsertConflict("the suggested reference is no longer available")
    chunk = (
        session.query(ReferenceChunk)
        .filter_by(reference_id=ref.id, chunk_index=annotation.chunk_index)
        .one_or_none()
    )
    if chunk is None:
        raise InsertConflict("the supporting excerpt is no longer available")

    repo = workspace_repo_dir(workspaces_dir, workspace.id)
    doc = (repo / "proposal.md").read_text()
    sections = split_sections(doc)
    matches = [s for s in sections if s.sha == annotation.section_sha]
    if annotation.section_occurrence >= len(matches):
        raise InsertConflict("the text changed — re-analysis is pending")
    target = matches[annotation.section_occurrence]
    section_start = sum(len(s.text) for s in sections[: target.index])

    sentence_raw = _raw_sentence_slice(target.text, annotation.char_offset)
    anchor = _build_unique_anchor(doc, section_start, sentence_raw)

    cit_path = repo / "citations.json"
    ledger = ledger_mod.load_ledger(cit_path.read_text() if cit_path.exists() else None)
    ev_id = ledger_mod.new_ev_id()
    citekey = _citekey_for(session, ref, ledger)  # see note below
    try:
        new_doc = insert_marker(doc, anchor, citekey, ev_id)
    except AnchorError as exc:
        raise InsertConflict(str(exc)) from exc

    row = ledger_mod.build_evidence_row(
        ev_id=ev_id, citekey=citekey, reference=ref, chunk=chunk,
        evidence_quote=annotation.evidence_quote, stance="supports",
        confidence=None, claim_text=annotation.claim_text)
    new_ledger = ledger_mod.append_evidence(ledger, row)
    commit_sha = write_and_commit_files(
        repo, {"proposal.md": new_doc,
               "citations.json": ledger_mod.dump_ledger(new_ledger)},
        f"user insert citation: {citekey} (user:{user_id})")

    session.add(ArtifactVersion(workspace_id=workspace.id, commit_sha=commit_sha,
                                created_by="user",
                                message=f"user insert citation: {citekey}"))
    session.add(WorkspaceEvent(workspace_id=workspace.id, kind="user_citation_insert",
                               actor_id=user_id,
                               payload={"annotation_id": annotation.id,
                                        "ev_id": ev_id, "citekey": citekey}))
    annotation.status = "inserted"
    annotation.resolved_by_user_id = user_id
    from datetime import UTC, datetime
    annotation.resolved_at = datetime.now(UTC)
    return {"ev_id": ev_id, "citekey": citekey, "commit_sha": commit_sha}
```

**`build_evidence_row` signature note:** the exact kwargs of `ledger.build_evidence_row` (citations/ledger.py:114) differ from the sketch above — open it and call it with its REAL signature (it takes the quote offsets from `find_quote_offsets(chunk.text, quote)`, the chunk content hash, claim context, etc. — `tools/citations.py::_insert_citation_locked` lines 440–505 show a complete worked call; copy that call shape). Same for `_citekey_for`: reuse the citekey resolution already in `tools/citations.py` (`_resolve_free_citekey` + the `references.citekey` cache read) — import those helpers rather than duplicating them; if they are module-private, extract them into `citations/citekeys.py` and re-import from both call sites (mechanical move, keep tests green).

- [ ] **Step 2: Insert tests** — `tests/test_analysis_insert.py`:

1. `test_insert_canonical_marker_and_ledger_row` — happy path: doc gains `[@<citekey>]<!-- ev:... -->` at sentence end; `citations.json` gains the row; `ArtifactVersion.created_by == "user"`; annotation → `inserted`; Tier-1 `validate_citations_deterministic` on the result reports NO `orphan_marker`/`orphan_row`.
2. `test_insert_idempotent_when_annotation_already_inserted` — second call returns `{"idempotent": True}`, no new commit.
3. `test_same_evidence_elsewhere_does_not_noop_this_occurrence` — pre-seed the ledger with a row + live marker for the SAME (reference, chunk, quote) on a different sentence; insert for this annotation still creates a NEW marker + row (assert two markers in doc).
4. `test_duplicate_section_insert_fails_cleanly` — two byte-identical sections → `InsertConflict` with "repeated text" message; doc unchanged.
5. `test_tombstoned_reference_rejected` — `InsertConflict`, doc unchanged.
6. `test_stale_section_rejected` — edit the section after annotation creation → `InsertConflict("the text changed…")`.

- [ ] **Step 3: Strip routes** — `web/annotations.py` (same dependency pattern as Task 7):

- `GET /workspaces/{id}/sections/{n}/annotations?sha=<section_sha>` → renders `annotation_strip.html` with: open annotations for `(workspace, sha, occurrence)` where occurrence = the count of sections with this sha before index n at HEAD (compute by re-splitting); plus pending/failed job state for this sha (query `ClaimAnalysisJob` by `(workspace_id, section_sha)`, newest row). Owner OR viewer may view.
- `POST /workspaces/{id}/annotations/{annotation_id}/insert` → owner only; loads annotation (404 if other workspace), calls `insert_from_annotation`, commits; `InsertConflict` → 409 with the message; re-renders the strip fragment.
- `POST /workspaces/{id}/annotations/{annotation_id}/dismiss` → owner only; `Form(reason: str)` — reject empty/whitespace reason with 422; sets status/dismissed_reason/resolved_by/resolved_at + `WorkspaceEvent(kind="user_claim_dismissed", payload={"annotation_id", "reason"})`; re-renders strip.
- `POST /workspaces/{id}/sections/{n}/reanalyze` → owner only; re-splits HEAD, enqueues `enqueue_section_job` for the live section content (the Retry button); re-renders strip showing "analysis pending".

`annotation_strip.html`:

```html
{% if job_pending %}
<p class="strip-pending" hx-get="{{ poll_url }}" hx-trigger="load delay:5s"
   hx-target="closest div" hx-swap="innerHTML">Checking claims…</p>
{% elif job_failed %}
<p class="strip-failed">Analysis failed: {{ job_error }}
  <button hx-post="/workspaces/{{ workspace.id }}/sections/{{ n }}/reanalyze"
          hx-target="closest div" hx-swap="innerHTML">Retry</button></p>
{% endif %}
{% for a in annotations %}
<div class="annotation annotation-{{ a.kind }}">
  <blockquote>{{ a.claim_text }}</blockquote>
  {% if a.kind == "uncited_supported" %}
    <p>Supported by <strong>{{ a.ref_label }}</strong>: “{{ a.evidence_quote }}”</p>
    <button hx-post="/workspaces/{{ workspace.id }}/annotations/{{ a.id }}/insert"
            hx-target="closest div" hx-swap="outerHTML">Insert citation</button>
  {% elif a.kind == "uncited_no_match" %}
    <p>Nothing in your library supports this.</p>
    <form hx-post="/workspaces/{{ workspace.id }}/annotations/{{ a.id }}/dismiss"
          hx-target="closest div" hx-swap="outerHTML">
      <input name="reason" placeholder="why is this OK? (required)" required>
      <button type="submit">Dismiss</button>
    </form>
  {% else %}
    <p>A citation on this sentence may not support it —
       <a href="/workspaces/{{ workspace.id }}/citations">review in the evidence ledger</a>.</p>
  {% endif %}
</div>
{% endfor %}
```

(`ref_label` = the existing CSL "Family Year" snapshot helper used in `tools/citations.py` — reuse it for display.) Flip `annotations_enabled` to `True` in `get_workspace_main` (Task 7 note) and include the router in `main.py`.

- [ ] **Step 4: Route tests** — `tests/test_web_annotations.py`: strip renders an open annotation with an Insert button; insert POST → 200 and marker present in repo; dismiss without reason → 422; dismiss with reason → annotation dismissed + event row; viewer can GET strip but POST insert → 403; reanalyze enqueues a job.

- [ ] **Step 5: Run** — `uv run pytest tests/test_analysis_insert.py tests/test_web_annotations.py -q` → all pass.
- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/analysis/insert.py src/call_manager/proposal_assist/web/annotations.py src/call_manager/web/templates/proposal_assist/partials/annotation_strip.html src/call_manager/proposal_assist/web/workspaces.py src/call_manager/main.py tests/test_analysis_insert.py tests/test_web_annotations.py
git commit -m "feat(analysis): annotation strip + occurrence-aware citation insert/dismiss/reanalyze"
```

---

### Task 12: Invalidation hooks

**Files:**
- Create: `src/call_manager/proposal_assist/analysis/invalidation.py`
- Modify: `src/call_manager/proposal_assist/references/ingest.py` (text_status→'done' site, line ~194)
- Modify: `src/call_manager/proposal_assist/references/deletion.py` (tombstone site, line ~76)
- Test: `tests/test_analysis_invalidation.py`

- [ ] **Step 1: Implement** `analysis/invalidation.py`:

```python
"""Library-change staleness (spec §3, rev-3 finding 6). Both directions:
grew → re-check no_match; shrank/changed → re-check supported."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import ClaimClassification, UserClaimAnnotation
from call_manager.proposal_assist.analysis.queue import enqueue_section_job

logger = logging.getLogger(__name__)


def _enqueue_for_shas(session: Session, workspace_id: uuid.UUID,
                      target_shas: set[str], workspaces_dir: Path) -> int:
    """Enqueue jobs for the subset of *target_shas* still live at HEAD."""
    from call_manager.proposal_assist.citations.gitio import current_head
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.paths import workspace_repo_dir

    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    path = repo / "proposal.md"
    if not path.exists():
        return 0
    head = current_head(repo)
    live = {s.sha: s for s in split_sections(path.read_text())}
    n = 0
    for sha in target_shas & set(live):
        s = live[sha]
        if enqueue_section_job(session, workspace_id=workspace_id,
                               section_index=s.index, section_heading=s.heading,
                               section_sha=s.sha, commit_sha=head):
            n += 1
    return n


def _open_annotation_shas(session: Session, workspace_id: uuid.UUID,
                          kinds: tuple[str, ...],
                          reference_id: uuid.UUID | None = None) -> set[str]:
    q = (session.query(UserClaimAnnotation.section_sha)
         .filter(UserClaimAnnotation.workspace_id == workspace_id,
                 UserClaimAnnotation.status == "open",
                 UserClaimAnnotation.kind.in_(kinds)))
    if reference_id is not None:
        q = q.filter(UserClaimAnnotation.reference_id == reference_id)
    return {sha for (sha,) in q.distinct()}


def on_reference_text_ingested(session: Session, *, workspace_id: uuid.UUID,
                               workspaces_dir: Path) -> int:
    """Library grew: stale no_match classifications; re-enqueue sections with
    open uncited_no_match annotations. (The pipeline's fan-out stales open
    annotations for a re-analyzed section before writing fresh findings, so
    a now-supported claim REPLACES its old no_match row — no contradictory
    strip.)"""
    session.query(ClaimClassification).filter_by(
        workspace_id=workspace_id, outcome="no_match",
    ).update({"stale_at": datetime.now(UTC)}, synchronize_session=False)
    shas = _open_annotation_shas(session, workspace_id, ("uncited_no_match",))
    return _enqueue_for_shas(session, workspace_id, shas, workspaces_dir)


def on_reference_unavailable(session: Session, *, workspace_id: uuid.UUID,
                             reference_id: uuid.UUID,
                             workspaces_dir: Path) -> int:
    """Library shrank/changed (tombstone, hard delete, re-chunk/re-embed).
    ORDER MATTERS: collect the affected sections BEFORE flipping the open
    annotations to stale — the collection query filters on status='open',
    so staleing first would always find zero sections to re-enqueue."""
    shas = _open_annotation_shas(session, workspace_id, ("uncited_supported",),
                                 reference_id=reference_id)
    session.query(ClaimClassification).filter_by(
        workspace_id=workspace_id, reference_id=reference_id,
    ).update({"stale_at": datetime.now(UTC)}, synchronize_session=False)
    session.query(UserClaimAnnotation).filter_by(
        workspace_id=workspace_id, reference_id=reference_id, status="open",
    ).update({"status": "stale"}, synchronize_session=False)
    return _enqueue_for_shas(session, workspace_id, shas, workspaces_dir)
```

- [ ] **Step 2: Call sites — BOTH deletion branches.** `references/deletion.py` has two outcomes (read it first): the *tombstone* branch (`ref.tombstoned_at = datetime.now(UTC)`, ~line 76) AND the *hard-delete* branch for uncited references (`session.delete(ref)`, ~line 90). Hook `on_reference_unavailable(...)` into both. In the hard-delete branch, call it **BEFORE** `session.delete(ref)` — the FK on `ClaimClassification.reference_id`/`UserClaimAnnotation.reference_id` is `SET NULL` on delete, so calling after the delete would match zero rows. In `references/ingest.py` immediately after `ref2.text_status = "done"` (~line 194), call `on_reference_text_ingested(...)` on the same session (workspaces_dir comes from the settings already available in that function — check its parameters; if not present, thread it through from the caller, which is the reference-text drain tick). All hooks wrapped in try/except-log (invalidation must never break ingest/deletion):

```python
try:
    on_reference_text_ingested(session, workspace_id=ref2.workspace_id,
                               workspaces_dir=workspaces_dir)
except Exception:  # noqa: BLE001 — invalidation is best-effort
    logger.exception("claim-analysis invalidation failed (ingest)")
```

- [ ] **Step 3: Tests** — `tests/test_analysis_invalidation.py`: (1) ingest hook stales `no_match` rows and enqueues the section containing an open `uncited_no_match`; (2) tombstone hook stales `supported` classification + flips open `uncited_supported` annotations to `stale` AND **enqueues the section** (regression for the collect-before-stale ordering — assert return value ≥ 1); (3) hard-delete of an uncited-but-suggested reference triggers the same invalidation (call `delete_reference` end-to-end and assert annotations went stale + job enqueued); (4) hooks are no-ops (return 0, no raise) when proposal.md doesn't exist.

- [ ] **Step 4: Run** — `uv run pytest tests/test_analysis_invalidation.py tests/ -q -k "reference"` → all pass.
- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/analysis/invalidation.py src/call_manager/proposal_assist/references/ tests/test_analysis_invalidation.py
git commit -m "feat(analysis): two-way library-change invalidation hooks"
```

---

### Task 13: Reconcile tick + scheduler wiring + settings

**Files:**
- Create: `src/call_manager/proposal_assist/analysis/reconcile.py`
- Modify: `src/call_manager/scheduler.py`
- Modify: `src/call_manager/config.py`
- Modify: `src/call_manager/main.py` (pass citation_service + embedder + workspaces_dir into `build_scheduler`)
- Test: `tests/test_analysis_reconcile.py`, extend `tests/test_scheduler.py`

- [ ] **Step 1: Reconcile** — `analysis/reconcile.py`:

```python
"""Self-healing coverage tick (spec §3): every HEAD section of every
drafting workspace gets a job row eventually, even if the save-time enqueue
was lost to the post-commit DB-failure window (spec §6)."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from call_manager.db.models import ClaimAnalysisJob, Workspace
from call_manager.proposal_assist.analysis.queue import enqueue_section_job
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)


def reconcile_analysis_coverage(*, session_factory: sessionmaker,
                                workspaces_dir: Path) -> int:
    """Enqueue a job for any live HEAD section with no job row in
    queued/running/done for its content. Returns count enqueued."""
    enqueued = 0
    with session_factory() as session:
        ws_ids = [w.id for w in session.query(Workspace)
                  .filter(Workspace.soft_deleted_at.is_(None))]
        for ws_id in ws_ids:
            repo = workspace_repo_dir(workspaces_dir, ws_id)
            path = repo / "proposal.md"
            if not path.exists():
                continue
            try:
                head = current_head(repo)
            except GitCommitError:
                continue
            sections = [s for s in split_sections(path.read_text()) if s.text]
            covered = {
                sha for (sha,) in session.query(ClaimAnalysisJob.section_sha)
                .filter(ClaimAnalysisJob.workspace_id == ws_id,
                        ClaimAnalysisJob.status.in_(("queued", "running", "done")))
            }
            for s in sections:
                if s.sha not in covered and enqueue_section_job(
                        session, workspace_id=ws_id, section_index=s.index,
                        section_heading=s.heading, section_sha=s.sha,
                        commit_sha=head):
                    enqueued += 1
        session.commit()
    return enqueued
```

- [ ] **Step 2: Settings** — in `config.py`, next to `phase0_tick_seconds`:

```python
    analysis_tick_seconds: int = Field(
        default=20, alias="ANALYSIS_TICK_SECONDS",
        description="Claim-analysis queue drain interval (seconds).")
    analysis_reconcile_tick_seconds: int = Field(
        default=900, alias="ANALYSIS_RECONCILE_TICK_SECONDS",
        description="Claim-analysis coverage reconcile interval (seconds).")
```

- [ ] **Step 3: Scheduler.** In `scheduler.py`, add two sync tick wrappers following the `_turn_drain_tick` pattern (these are sync functions — no app_loop dispatch needed since `drain_analysis_queue`/`reconcile_analysis_coverage` are synchronous):

```python
def _analysis_drain_tick() -> None:
    try:
        out = drain_analysis_queue(
            session_factory=session_factory, citation_service=citation_service,
            embedder=analysis_embedder, workspaces_dir=Path(settings.workspaces_dir))
        if any(out.values()):
            _logger.info("analysis drain: %s", out)
    except Exception:
        _logger.exception("analysis drain tick failed")


def _analysis_reconcile_tick() -> None:
    try:
        n = reconcile_analysis_coverage(
            session_factory=session_factory,
            workspaces_dir=Path(settings.workspaces_dir))
        if n:
            _logger.info("analysis reconcile enqueued %d", n)
    except Exception:
        _logger.exception("analysis reconcile tick failed")
```

Registration — **gate on citation-service availability, NOT on ANTHROPIC_API_KEY** (the citation LLM defaults to Ollama):

```python
    if settings.drafting_enabled and citation_service is not None and analysis_embedder is not None:
        sched.add_job(_analysis_drain_tick,
                      trigger=IntervalTrigger(seconds=settings.analysis_tick_seconds),
                      id="claim_analysis_drain", max_instances=1, coalesce=True)
        sched.add_job(_analysis_reconcile_tick,
                      trigger=IntervalTrigger(seconds=settings.analysis_reconcile_tick_seconds),
                      id="claim_analysis_reconcile", max_instances=1, coalesce=True)
```

`build_scheduler` gains kwargs `citation_service=None` and `analysis_embedder=None`. In `main.py`, pass `citation_service=app.state.citation_service` and build the embedder the same way the reference-text ingest path builds its `OllamaEmbedderAdapter` (find its construction with `grep -rn "OllamaEmbedderAdapter(" src/call_manager/` and reuse those settings args).

- [ ] **Step 4: Tests.** `tests/test_analysis_reconcile.py`: workspace with repo + 2 sections, no jobs → reconcile enqueues 2; run again → 0 (covered). Extend `tests/test_scheduler.py` with: jobs registered when citation_service+embedder provided; absent when either is None (mirror the existing conditional-registration tests for digest/phase0).

- [ ] **Step 5: Run** — `uv run pytest tests/test_analysis_reconcile.py tests/test_scheduler.py -q` → all pass.
- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/analysis/reconcile.py src/call_manager/scheduler.py src/call_manager/config.py src/call_manager/main.py tests/test_analysis_reconcile.py tests/test_scheduler.py
git commit -m "feat(analysis): drain + reconcile scheduler ticks gated on citation-service availability"
```

---

### Task 14: End-to-end test + full-suite verification

**Files:**
- Create: `tests/test_editing_e2e.py`

- [ ] **Step 1: E2E test** — one flow through the seams (service-level, fakes for LLM/embedder): create workspace + repo → `save_section` adds a claim sentence → `drain_analysis_queue` produces an `uncited_supported` annotation → `insert_from_annotation` lands the canonical marker → `validate_citations_deterministic` on the final doc+ledger reports zero issues → second `save_section` editing a DIFFERENT section leaves the annotation untouched; editing the SAME section makes the old job content superseded on next drain and the annotation set refreshed.

- [ ] **Step 2: Full suite + lint**

Run: `uv run pytest -q` → everything passes (expect prior count + ~45 new).
Run: `uv run ruff check src tests && uv run ruff format --check src tests` → clean.

- [ ] **Step 3: Commit**

```bash
git add tests/test_editing_e2e.py
git commit -m "test(editing): end-to-end save → analyze → insert flow"
```

- [ ] **Step 4: Finish.** Use superpowers:finishing-a-development-branch (merge/PR decision with the user). Do not merge without the user.

---

## Spec-coverage map (self-review)

| Spec section | Tasks |
|---|---|
| §1 sectionizer + round-trip | 1 |
| §1 commit mutex (all paths) | 2, 6, 8, 9, 11 |
| §1 turn lock + stale form + routes | 6, 7 |
| §1 replace/edit view-of-HEAD precondition | 9 |
| §1 ArtifactVersion('user') provenance | 6, 11 |
| §1 turn-lock UX (disable hints) | 7 (templates; composer hook is the existing `done` SSE event) |
| §2 Tier-1 deterministic + bare markers | 3, 6 |
| §3 job queue, content dedup, supersession | 5, 10 |
| §3 cited-first pipeline, classification cache, retrieval, verified quotes | 10 |
| §3 two-way invalidation | 12 |
| §3 occurrence annotations (duplicates, evidence_id) | 4, 10 |
| §3 reconcile tick | 13 |
| §3 gating on citation service | 13 |
| §4 strip UI, insert/dismiss/retry | 11 |
| §4 canonical insert, occurrence idempotency, anchor failure | 11 |
| §5 agent prompt rule | NOT in this plan — one-line addition to `_CITATION_INSTRUCTION` in `claude_adapter.py`; add it in Task 9 Step 3 while editing tools: append "Never rewrite or delete passages from commits marked 'user edit:' unless the user's message asks for it." |
| §6 error handling / post-commit DB failure | 6 (docstring + design), 13 (reconcile), 11 (conflict surfaces) |
| §7 tests | every task; race tests in 2, 8, 9; e2e in 14 |
