# Jinja Teardown G1 — Sever React's Jinja Dependencies — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every dependency the React cockpit and its kept `/api` backend still have on the doomed Jinja `/workspaces/{id}` modules, so the later G2 slice can delete those modules as a pure orphan-removal.

**Architecture:** Additive + relocation only — **nothing is deleted**. (1) Add two `/api` binary serves (reference raw download, file image bytes) that transcribe the existing Jinja serves verbatim, and repoint the three React literals that fetch them. (2) Relocate four shared-symbol groups (`_session_dep`, workspace-settings constants/parser, image-upload helpers, reference-upload helpers) out of the doomed Jinja modules into neutral, template-free homes, and repoint the kept importers. (3) Add a durable severance-guard test. The doomed modules keep their own copies of the relocated symbols (transient duplication removed in G2) so the live Jinja UI stays byte-for-byte unchanged.

**Tech Stack:** Python 3.12 (`uv`, `ruff`, `pytest` + testcontainers Postgres), FastAPI, SQLAlchemy 2.x. Frontend React 19 + Vite + `pnpm` (`tsc` is the type gate; `vitest` for unit tests). Source spec: `docs/superpowers/specs/2026-06-25-jinja-teardown-g1-sever-deps-design.md` (`821580e`). Builds on cockpit cutover complete at master `d335897`.

## Global Constraints

- **Additive + relocation ONLY — delete NOTHING.** No routes, templates, modules, `include_router` calls, or `base.html` links are removed; G2 does all deletion.
- **The live Jinja UI must stay byte-for-byte unchanged.** When relocating a symbol, COPY it to the neutral module and LEAVE the original definition in the doomed module untouched (transient duplication; G2 removes the copies). Do NOT edit a doomed module to re-import from the neutral home.
- **Kept backend imports nothing from doomed modules.** After this slice, no `web/api/*.py` module and none of the 7 live JSON modules (`proposals`, `activity`, `problems`, `coherence`, `evaluator`, `review`, `uncited_claims`) may contain `from call_manager.proposal_assist.web.{workspaces,files,references,sections,citations,annotations} import …`.
- **React calls only `/api`.** No `frontend/src` reference to a Jinja `/workspaces/…` route may remain (the generated `frontend/src/api/schema.gen.ts` still lists Jinja paths — that is fine; it is regenerated and the paths are deleted in G2).
- **No DB schema change.** Alembic is untouched.
- **Binary 200 responses are exempt from the JSON error envelope** (consistent with Slice F export and the existing serves).
- **Serve-header parity (transcribe verbatim — change ONLY the URL prefix and the session-handling pattern, never the headers or auth semantics):**
  - References raw serve → `Content-Disposition: attachment; filename="…"` + `X-Content-Type-Options: nosniff` + `Cache-Control: private, max-age=300` (wide MIME range → force download + nosniff).
  - Files serve → `Content-Disposition: inline; filename="…"` + `Cache-Control: private, max-age=300` and **NO `nosniff`** (serves images inline). Do not add nosniff — it would be a behavior change out of scope for a parity slice.
- **Cross-workspace lookups return 404, not 403, for both serves** (`ref.workspace_id != ws.id` / `att.workspace_id != ws.id`) so existence is not leaked.
- **Prefer the canonical source over a relocated copy.** `_CITATION_STYLES` is a re-export of `CITATION_STYLES` from `call_manager.proposal_assist.citations.bibliography` — import that canonical name directly; do not copy the alias.
- **OpenAPI regen runs with `EMBEDDING_MODEL=bge-m3`** (the `.env` mxbai default logs a WARNING to stdout that corrupts the generated schema).

## Execution Notes (worktree)

- This plan runs in an isolated worktree under `.worktrees/` (project convention). The worktree needs its own environment before tests run:
  - Backend: `uv sync` in the worktree root.
  - Frontend: `cd frontend && pnpm install --frozen-lockfile`.
- Subagents default their shell cwd to the MAIN working tree, not the worktree. Every task MUST `cd` into the worktree, run `git -C <worktree>` for all git ops, and verify it is on the G1 branch (not `master`) before editing or committing.
- This is a detached/STOP-before-merge run: implement all tasks, run the final whole-branch review, then STOP. Do not merge or push — the human merges to local master after independent verification.

## File Structure

**Created:**
- `src/call_manager/proposal_assist/web/_deps.py` — neutral `_session_dep` FastAPI dependency (no template imports).
- `src/call_manager/proposal_assist/web/workspace_meta.py` — `_WORKSPACE_STATUSES`, `_MAX_WORKSPACE_TITLE_LEN`, `_parse_deadline_form` (workspace-settings validation, no template imports).
- `src/call_manager/proposal_assist/web/_file_uploads.py` — image upload/serve helpers (`_ALLOWED_MIMES`, `_MAX_BYTES`, `_PIL_FORMAT_TO_MIME`, `_SUFFIX_BY_MIME`, `_probe_image_format`, `_read_capped`, `_sanitize_filename`, `_storage_for`).
- `src/call_manager/proposal_assist/references/uploads.py` — reference upload/serve helpers (`_ALLOWED_MIMES`, `_MAX_BYTES`, `_read_capped`, `_sanitize_filename`, `_storage_for`, `_parse_authors`).
- `tests/test_jinja_severance.py` — durable guard: kept modules import nothing from doomed Jinja modules.

**Modified (import repoints + the two serves):**
- 7 JSON modules — repoint `_session_dep` import (1 line each): `proposal_assist/web/{proposals,activity,problems,coherence,evaluator,review,uncited_claims}.py`.
- `src/call_manager/web/api/settings.py` — repoint the settings constants/parser import.
- `src/call_manager/web/api/files.py` — repoint the upload-helper import; add the `GET …/files/{attachment_id}` serve.
- `src/call_manager/web/api/references.py` — repoint the upload-helper imports (two sites); add the `GET …/references/{reference_id}/raw` serve.
- `frontend/src/cockpit/LibraryPanel.tsx` — repoint two literals to `/api`.
- `frontend/src/cockpit/chat/TurnList.tsx` — repoint one literal to `/api`.
- `frontend/src/cockpit/LibraryPanel.test.tsx`, `frontend/src/cockpit/chat/TurnList.test.tsx` — update the URL expectations.
- `frontend/src/api/schema.gen.ts` — regenerated (gains the 2 new `/api` paths).

**Untouched (do NOT edit):** every doomed Jinja module (`proposal_assist/web/{workspaces,files,references,sections,citations,annotations}.py`), all templates, `base.html`, `web/api/__init__.py` (both serve routers are already mounted).

**Task order (dependency-driven):** Tasks 1–4 are independent relocations. Task 5 (serves) MUST follow Tasks 3 & 4 (it consumes the relocated `_storage_for`/`_sanitize_filename` from their neutral homes). Task 6 (guard) MUST be last (it can only pass once every coupling is severed). Run them 1 → 6.

---

### Task 1: Relocate `_session_dep` → `proposal_assist/web/_deps.py`

The 7 live JSON modules each import `_session_dep` from the doomed `workspaces.py`. Move the definition to a neutral module and repoint them. (This is a behavior-preserving relocation — the verification is that the affected suites stay green and the coupling grep is empty; the durable regression test is added in Task 6.)

**Files:**
- Create: `src/call_manager/proposal_assist/web/_deps.py`
- Modify: `src/call_manager/proposal_assist/web/proposals.py:26`, `activity.py:25`, `problems.py:19`, `coherence.py:20`, `evaluator.py:24`, `review.py:32`, `uncited_claims.py:28`

**Interfaces:**
- Produces: `_session_dep(request: Request)` — a FastAPI generator dependency yielding a `Session`. Importable as `from call_manager.proposal_assist.web._deps import _session_dep`. Same callable the 7 modules already use.

- [ ] **Step 1: Create `_deps.py`**

```python
"""Neutral FastAPI dependency wiring shared by the JSON /api workspace modules.

``_session_dep`` historically lived in the (now-doomed) Jinja ``workspaces.py``.
It is relocated here so the kept JSON modules import it from a module that has
no template/Jinja coupling and survives the Jinja teardown (G2). Definition is
copied verbatim from ``proposal_assist/web/workspaces.py``.
"""

from __future__ import annotations

from fastapi import Request

from call_manager.db.session import get_db_dependency


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()
```

- [ ] **Step 2: Repoint the 7 importers**

In each of the 7 files, change the line
`from call_manager.proposal_assist.web.workspaces import _session_dep`
to
`from call_manager.proposal_assist.web._deps import _session_dep`

Exact locations: `proposals.py:26`, `activity.py:25`, `problems.py:19`, `coherence.py:20`, `evaluator.py:24`, `review.py:32`, `uncited_claims.py:28`. Do NOT touch `workspaces.py` (it keeps its own `_session_dep` for its still-live Jinja routes). Do NOT touch `citations.py` (it imports `_session_dep` from `references.py`, but it is itself doomed and is deleted in G2 — out of scope).

- [ ] **Step 3: Verify (coupling gone + suites green + boot)**

```bash
# No kept JSON module still imports _session_dep from a doomed module:
grep -rn "from call_manager.proposal_assist.web.workspaces import _session_dep" \
  src/call_manager/proposal_assist/web/{proposals,activity,problems,coherence,evaluator,review,uncited_claims}.py
# Expected: no output.

# New home importable + app boots (proves wiring is intact):
uv run python -c "from call_manager.proposal_assist.web._deps import _session_dep; import call_manager.main; print('ok')"
# Expected: ok

# Affected suites stay green (these exercise _session_dep via the routes):
uv run pytest \
  tests/proposal_assist/test_web_proposed_changes.py \
  tests/proposal_assist/activity/ \
  tests/proposal_assist/problems/ \
  tests/proposal_assist/coherence/ \
  tests/proposal_assist/evaluator/ \
  tests/proposal_assist/review/ \
  tests/proposal_assist/uncited_claims/ -q
# Expected: all pass. (If testcontainers Postgres drops a connection under load —
# a known infra flake — re-run the failing file in isolation to confirm it's green.)
```

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/proposal_assist/web/_deps.py \
  src/call_manager/proposal_assist/web/{proposals,activity,problems,coherence,evaluator,review,uncited_claims}.py
git commit -m "refactor(spa): relocate _session_dep to neutral _deps.py, repoint 7 JSON modules (G1)"
```

---

### Task 2: Relocate workspace-settings constants/parser → `proposal_assist/web/workspace_meta.py`

`web/api/settings.py:21` imports `_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_MAX_WORKSPACE_TITLE_LEN`, `_parse_deadline_form` from the doomed `workspaces.py`. Relocate the three workspace-specific symbols to a neutral module; import `_CITATION_STYLES` from its canonical source (`citations.bibliography.CITATION_STYLES`) instead of copying the alias.

**Files:**
- Create: `src/call_manager/proposal_assist/web/workspace_meta.py`
- Modify: `src/call_manager/web/api/settings.py:21-26`

**Interfaces:**
- Produces: `_WORKSPACE_STATUSES: frozenset[str]`, `_MAX_WORKSPACE_TITLE_LEN: int`, `_parse_deadline_form(deadline: str | None) -> datetime | None` (raises `HTTPException(400)` on a malformed string). Importable from `call_manager.proposal_assist.web.workspace_meta`.
- Consumes (canonical, unchanged): `CITATION_STYLES` from `call_manager.proposal_assist.citations.bibliography`.

- [ ] **Step 1: Create `workspace_meta.py`**

```python
"""Workspace-settings validation constants + parser (neutral, no templates).

Relocated verbatim from the (now-doomed) Jinja ``workspaces.py`` so the kept
``web/api/settings.py`` does not import from a module slated for deletion (G2).
``_CITATION_STYLES`` is NOT relocated here — it is a re-export of
``citations.bibliography.CITATION_STYLES``; importers should use that canonical
name directly.
"""

from __future__ import annotations

from fastapi import HTTPException

# Valid Workspace.status values (mirrors the ck_workspaces_status CHECK
# constraint in db/models.py). Used to allowlist the ?status filter.
_WORKSPACE_STATUSES = frozenset({"drafting", "submitted", "awarded", "rejected", "abandoned"})
# Upper bound on a workspace title — bounds the form field so a multi-MB
# title can't be buffered into the DB. Generous for human-entered titles.
_MAX_WORKSPACE_TITLE_LEN = 500


def _parse_deadline_form(deadline: str | None):
    """Parse an optional ISO-8601 deadline from a form field.

    Returns a datetime or None. Raises HTTPException(400) on a malformed
    string instead of letting datetime.fromisoformat's ValueError bubble up
    as an unhandled 500 (the global handler only catches HTTPException).
    """
    from datetime import datetime

    if not deadline:
        return None
    try:
        return datetime.fromisoformat(deadline)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid deadline (expected ISO-8601)") from e
```

- [ ] **Step 2: Repoint `web/api/settings.py`**

Replace the import block at `settings.py:21-26`:

```python
from call_manager.proposal_assist.web.workspaces import (
    _CITATION_STYLES,
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)
```

with (note the alias keeps the function bodies that reference `_CITATION_STYLES` unchanged):

```python
from call_manager.proposal_assist.citations.bibliography import (
    CITATION_STYLES as _CITATION_STYLES,
)
from call_manager.proposal_assist.web.workspace_meta import (
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)
```

Do not change anything else in `settings.py` — the handler bodies still reference `_WORKSPACE_STATUSES`, `_CITATION_STYLES`, `_MAX_WORKSPACE_TITLE_LEN`, `_parse_deadline_form` by the same names.

- [ ] **Step 3: Verify**

```bash
grep -n "proposal_assist.web.workspaces" src/call_manager/web/api/settings.py
# Expected: no output (coupling gone).

uv run python -c "import call_manager.main; from call_manager.proposal_assist.web.workspace_meta import _parse_deadline_form; print('ok')"
# Expected: ok

uv run pytest tests/test_api_settings.py -q
# Expected: all pass (status/citation_style/title/deadline validation still enforced).
```

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/proposal_assist/web/workspace_meta.py src/call_manager/web/api/settings.py
git commit -m "refactor(spa): relocate workspace-settings constants/parser to workspace_meta.py, canonical CITATION_STYLES (G1)"
```

---

### Task 3: Relocate image-upload helpers → `proposal_assist/web/_file_uploads.py`

`web/api/files.py:24` imports eight image helpers from the doomed `files.py`. Relocate them verbatim to a neutral module and repoint.

**Files:**
- Create: `src/call_manager/proposal_assist/web/_file_uploads.py`
- Modify: `src/call_manager/web/api/files.py:24-33`

**Interfaces:**
- Produces (verbatim from `proposal_assist/web/files.py`): `_MAX_BYTES: int` (5 MB), `_ALLOWED_MIMES: frozenset[str]`, `_SUFFIX_BY_MIME: dict`, `_PIL_FORMAT_TO_MIME: dict`, `_storage_for(request) -> AttachmentStorage`, `_sanitize_filename(name: str, *, default="attachment") -> str`, `_read_capped(file: UploadFile, max_bytes: int) -> bytes` (async), `_probe_image_format(data: bytes) -> tuple[str, int, int]`. Importable from `call_manager.proposal_assist.web._file_uploads`.

- [ ] **Step 1: Create `_file_uploads.py`** (copy verbatim from `proposal_assist/web/files.py` — `files.py` is authoritative)

```python
"""Image attachment upload/serve helpers (neutral, no Jinja templates).

Relocated verbatim from the (now-doomed) Jinja ``files.py`` so the kept
``web/api/files.py`` (Slice C-media + the G1 binary serve) imports them from a
module that survives the Jinja teardown (G2). The doomed ``files.py`` keeps its
own copies until G2.
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from call_manager.proposal_assist.attachments.storage import AttachmentStorage

# Hard caps (intentionally not env-gated for v1 — revisit if users hit them).
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per image.
_CHUNK_BYTES = 64 * 1024

_ALLOWED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

# Decompression-bomb guard: cap the pixel count Pillow will accept before
# probing dimensions, so a tiny-on-disk image declaring an enormous pixel
# count can't drive a multi-GB allocation. 64 Mpx (~8000x8000) far exceeds
# any legitimate attachment but is bounded.
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Pillow exposes formats as JPEG/PNG/WEBP/GIF (uppercase). Map back to the
# allow-listed MIME so the "client claims X, bytes are actually Y" check
# is a single equality comparison.
_PIL_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _storage_for(request: Request) -> AttachmentStorage:
    settings = request.app.state.settings
    return AttachmentStorage(root=Path(settings.workspaces_dir))


def _sanitize_filename(name: str, *, default: str = "attachment") -> str:
    """Allowlist-based: keep alnum + ``-_.``; everything else becomes ``_``.

    Explicitly rejects ``..`` runs (would otherwise pass through as ``..``
    after the allowlist filter and let a server-side header consumer
    misinterpret the path) — they collapse to a single ``.``.
    """
    if not name:
        return default
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    # Defense-in-depth: collapse ".." runs that survived the allowlist.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._") or default
    return cleaned[:120]


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read up to ``max_bytes + 1`` bytes from the upload, in chunks.

    If we exceed the cap, raise ``HTTPException(400)`` BEFORE returning —
    the caller never sees a too-large payload and the bytes are released
    when the function returns. Reading all-at-once via ``await file.read()``
    would be a memory DoS vector.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"file exceeds {max_bytes // (1024 * 1024)} MB limit",
            )
    return bytes(buf)


def _probe_image_format(data: bytes) -> tuple[str, int, int]:
    """Open the bytes with Pillow and return ``(format, width, height)``.

    Raises ``HTTPException(400)`` if the bytes are not a recognized image
    OR if the detected format is not in our allowlist. This is defense
    layer 2 against a client lying about ``content_type``.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
            if width * height > Image.MAX_IMAGE_PIXELS:
                raise DecompressionBombError(
                    f"image pixel count {width * height} exceeds cap {Image.MAX_IMAGE_PIXELS}"
                )
    except DecompressionBombError as e:
        raise HTTPException(
            status_code=400, detail=f"image too large (decompression-bomb guard): {e}"
        ) from e
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"unrecognised image data: {e}") from e
    if fmt not in _PIL_FORMAT_TO_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported image format: {fmt!r}")
    return fmt, width, height
```

- [ ] **Step 2: Repoint `web/api/files.py`**

Replace the import block at `files.py:24-33`:

```python
from call_manager.proposal_assist.web.files import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _PIL_FORMAT_TO_MIME,
    _SUFFIX_BY_MIME,
    _probe_image_format,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
```

with:

```python
from call_manager.proposal_assist.web._file_uploads import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _PIL_FORMAT_TO_MIME,
    _SUFFIX_BY_MIME,
    _probe_image_format,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
```

- [ ] **Step 3: Verify**

```bash
grep -n "proposal_assist.web.files import" src/call_manager/web/api/files.py
# Expected: no output (coupling gone).

uv run python -c "import call_manager.main; from call_manager.proposal_assist.web._file_uploads import _probe_image_format; print('ok')"
# Expected: ok

uv run pytest tests/test_api_files.py -q
# Expected: all pass (upload validation/probe/caps still enforced).
```

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/proposal_assist/web/_file_uploads.py src/call_manager/web/api/files.py
git commit -m "refactor(spa): relocate image-upload helpers to _file_uploads.py, repoint web/api/files (G1)"
```

---

### Task 4: Relocate reference-upload helpers → `proposal_assist/references/uploads.py`

`web/api/references.py:35` imports five helpers and `:274` imports `_parse_authors`, all from the doomed `references.py`. Relocate them verbatim and repoint both sites. (The `_sanitize_filename`/`_storage_for`/`_read_capped`/`_ALLOWED_MIMES`/`_MAX_BYTES` here are DIFFERENT from the image ones in Task 3 — regex vs allowlist sanitizer, 50 MB vs 5 MB, document vs image MIMEs — so they get a separate home.)

**Files:**
- Create: `src/call_manager/proposal_assist/references/uploads.py`
- Modify: `src/call_manager/web/api/references.py:35-41` and `:274`

**Interfaces:**
- Produces (verbatim from `proposal_assist/web/references.py`): `_ALLOWED_MIMES: frozenset[str]`, `_MAX_BYTES: int` (50 MB), `_storage_for(request) -> ReferenceStorage`, `_sanitize_filename(name: str) -> str`, `_read_capped(upload: UploadFile, max_bytes: int) -> bytes` (async), `_parse_authors(text: str) -> list[dict]`. Importable from `call_manager.proposal_assist.references.uploads`.

- [ ] **Step 1: Create `references/uploads.py`** (copy verbatim from `proposal_assist/web/references.py` — `references.py` is authoritative)

```python
"""Reference upload/serve/metadata helpers (neutral, no Jinja templates).

Relocated verbatim from the (now-doomed) Jinja ``proposal_assist/web/references.py``
so the kept ``web/api/references.py`` (Slice C-refs + the G1 raw serve) imports
them from a module that survives the Jinja teardown (G2). The doomed
``references.py`` keeps its own copies until G2. The upload helpers here are
distinct from the image helpers in ``proposal_assist/web/_file_uploads.py``
(different MIME allowlist, size cap, and sanitizer); ``_parse_authors`` is the
CSL author-list parser used by the metadata-approval route.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile

from call_manager.proposal_assist.references.storage import ReferenceStorage

_ALLOWED_MIMES = frozenset(
    {
        "application/pdf",
        "text/markdown",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/csv",
        # Legacy .xls (application/vnd.ms-excel) is intentionally NOT accepted:
        # openpyxl can't read the binary BIFF format, so it would always fail
        # extraction. Users should upload .xlsx (OOXML).
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-ipynb+json",
    }
)
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per spec REFERENCES_MAX_BYTES default
_CHUNK_BYTES = 64 * 1024

_FILENAME_OK = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    cleaned = _FILENAME_OK.sub("_", name)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "reference"


def _storage_for(request: Request) -> ReferenceStorage:
    return ReferenceStorage(root=Path(request.app.state.settings.workspaces_dir))


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            # Include the actual size observed so far (capped to max+1)
            # so the user knows roughly how oversize it is — "your 75 MB
            # file exceeds the 50 MB limit" is more actionable than
            # "exceeds 50 MB".
            observed_mb = len(buf) / (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"file (>{observed_mb:.1f} MB) exceeds the "
                    f"{max_bytes // (1024 * 1024)} MB upload limit"
                ),
            )
    return bytes(buf)


def _parse_authors(text: str) -> list[dict]:
    """Parse a newline-separated author string into a CSL author list.

    Decoding rules (inverse of ``_authors_to_lines``):
    - Lines containing a comma are split on the FIRST comma into family/given.
      ``given`` is only included in the dict when it is non-empty, so
      ``"WHO,"`` → ``{"family": "WHO"}`` and ``"Roe, Jane"`` →
      ``{"family": "Roe", "given": "Jane"}``.
    - Lines with no comma → ``{"literal": line}``.
    - Blank lines are skipped.
    """
    authors = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "," in line:
            family, _, given = line.partition(",")
            family = family.strip()
            given = given.strip()
            entry: dict = {"family": family}
            if given:
                entry["given"] = given
            authors.append(entry)
        else:
            authors.append({"literal": line})
    return authors
```

- [ ] **Step 2: Repoint both import sites in `web/api/references.py`**

At `references.py:35-41`, replace:

```python
from call_manager.proposal_assist.web.references import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
```

with:

```python
from call_manager.proposal_assist.references.uploads import (
    _ALLOWED_MIMES,
    _MAX_BYTES,
    _read_capped,
    _sanitize_filename,
    _storage_for,
)
```

At `references.py:274` (inside `api_approve_metadata`), replace:

```python
    from call_manager.proposal_assist.web.references import _parse_authors
```

with:

```python
    from call_manager.proposal_assist.references.uploads import _parse_authors
```

- [ ] **Step 3: Verify**

```bash
grep -n "proposal_assist.web.references import" src/call_manager/web/api/references.py
# Expected: no output (both sites repointed).

uv run python -c "import call_manager.main; from call_manager.proposal_assist.references.uploads import _parse_authors, _storage_for; print('ok')"
# Expected: ok

uv run pytest tests/test_api_references.py -q
# Expected: all pass (upload/fetch/metadata/waiver/kind/delete/reprocess unchanged).
```

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/proposal_assist/references/uploads.py src/call_manager/web/api/references.py
git commit -m "refactor(spa): relocate reference-upload helpers to references/uploads.py, repoint web/api/references (G1)"
```

---

### Task 5: Add the two `/api` binary serves + repoint React + OpenAPI regen

Add `GET /api/workspaces/{id}/references/{reference_id}/raw` and `GET /api/workspaces/{id}/files/{attachment_id}`, transcribing the existing Jinja serves verbatim (only the URL prefix and the session-handling pattern change — to the context-manager pattern the rest of `web/api` uses). Then repoint the three React literals and update the two frontend test expectations, and regenerate the OpenAPI types.

**Files:**
- Modify: `src/call_manager/web/api/references.py` (add serve + `FileResponse` import)
- Modify: `src/call_manager/web/api/files.py` (add serve + `FileResponse` import + module logger)
- Test: `tests/test_api_references.py`, `tests/test_api_files.py`
- Modify: `frontend/src/cockpit/LibraryPanel.tsx:174,225`, `frontend/src/cockpit/chat/TurnList.tsx:33`
- Modify: `frontend/src/cockpit/LibraryPanel.test.tsx:141`, `frontend/src/cockpit/chat/TurnList.test.tsx:61`
- Regen: `frontend/src/api/schema.gen.ts`

**Interfaces:**
- Consumes: `require_member` (`_perms`), `suffix_for_mime` (`references.ingest`), `_storage_for`/`_sanitize_filename` from the Task 3 & Task 4 neutral homes, `Reference`/`Attachment` models.
- Produces: two member-gated `FileResponse` endpoints under `/api`.

- [ ] **Step 1: Write the failing reference-serve tests**

Append to `tests/test_api_references.py` (reuses the existing `_app`, `_setup`, `_real_pdf_bytes`, and the `make_user`/`make_workspace`/`authed_client` imports already at the top of that file):

```python
def test_api_serve_reference_raw_member_roundtrips_bytes(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    pdf = _real_pdf_bytes()
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    ).json()["reference_id"]
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 200, r.text
    assert r.content == pdf
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, max-age=300"


def test_api_serve_reference_raw_non_member_403(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, stranger_id = _setup(pg_session_factory, tmp_path)
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    r = authed_client(app, user_id=stranger_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 403, r.text


def test_api_serve_reference_raw_missing_reference_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    import uuid

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{uuid.uuid4()}/raw"
    )
    assert r.status_code == 404, r.text


def test_api_serve_reference_raw_missing_bytes_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    import uuid

    from call_manager.proposal_assist.references.ingest import suffix_for_mime

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    # DB row remains, stored bytes removed -> serve must 404, not 500. Derive the
    # suffix exactly as the serve does so the unlink can't miss the file.
    storage.path_for(
        workspace_id=ws_id,
        reference_id=uuid.UUID(rid),
        suffix=suffix_for_mime("application/pdf"),
    ).unlink()
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 404, r.text


def test_api_serve_reference_raw_cross_workspace_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """IDOR: a member of workspace A cannot read a reference owned by workspace B."""
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_a, _, member_a, _ = _setup(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        owner_b = make_user(s, email="ownerb@ki.se")
        ws_b = make_workspace(s, owner=owner_b)
        s.commit()
        ws_b_id, owner_b_id = ws_b.id, owner_b.id
    rid_b = authed_client(app, user_id=owner_b_id).post(
        f"/api/workspaces/{ws_b_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    # member of A requests B's reference id under A's URL -> 404 (not 403, not 200).
    r = authed_client(app, user_id=member_a).get(
        f"/api/workspaces/{ws_a}/references/{rid_b}/raw"
    )
    assert r.status_code == 404, r.text
```

- [ ] **Step 2: Run the reference-serve tests to verify they fail**

Run: `uv run pytest tests/test_api_references.py -k serve_reference_raw -q`
Expected: FAIL — 404 from FastAPI (no such route yet) on the 200/403 cases.

- [ ] **Step 3: Add the reference raw serve**

In `src/call_manager/web/api/references.py`, add the import near the other fastapi imports (after line 16):

```python
from fastapi.responses import FileResponse
```

Append this route to the module (after the last route):

```python
@router.get("/workspaces/{workspace_id}/references/{reference_id}/raw")
def api_serve_reference_raw(
    workspace_id: uuid.UUID,
    reference_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> FileResponse:
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        ref = session.scalar(select(Reference).where(Reference.id == reference_id))
        if ref is None or ref.workspace_id != ws.id:
            # Don't leak existence — 404 for cross-workspace lookups too.
            raise HTTPException(status_code=404, detail="reference not found")
        mime_type = ref.mime_type
        filename = ref.filename

    storage = _storage_for(request)
    suffix = suffix_for_mime(mime_type)
    path = storage.path_for(workspace_id=workspace_id, reference_id=reference_id, suffix=suffix)
    if not path.exists():
        raise HTTPException(status_code=404, detail="reference bytes missing")

    safe_name = _sanitize_filename(filename)
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            # Force download — references serve a wide MIME range
            # (HTML/SVG/etc would otherwise be a same-origin XSS vector).
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )
```

- [ ] **Step 4: Run the reference-serve tests to verify they pass**

Run: `uv run pytest tests/test_api_references.py -k serve_reference_raw -q`
Expected: 5 passed.

- [ ] **Step 5: Write the failing file-serve tests**

Append to `tests/test_api_files.py` (reuses the existing `_setup`, `_upload`, `_png_bytes`, `build_app`, `authed_client`, `make_user`, `make_workspace`, and `uuid` already imported at the top):

```python
def test_api_serve_file_member_roundtrips_bytes(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    png = _png_bytes()
    aid = _upload(app, user_id=owner_id, ws_id=ws_id, data=png).json()["attachment_id"]
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 200, r.text
    assert r.content == png
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.headers["cache-control"] == "private, max-age=300"
    # Parity with the existing serve: files are served inline WITHOUT nosniff.
    assert "x-content-type-options" not in r.headers


def test_api_serve_file_non_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, stranger_id = _setup(pg_session_factory)
    aid = _upload(app, user_id=owner_id, ws_id=ws_id).json()["attachment_id"]
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/files/{aid}")
    assert r.status_code == 403, r.text


def test_api_serve_file_missing_attachment_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/files/{uuid.uuid4()}"
    )
    assert r.status_code == 404, r.text


def test_api_serve_file_cross_workspace_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """IDOR: a member of workspace A cannot read an attachment owned by workspace B."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_a, _, member_a, _ = _setup(pg_session_factory)
    with pg_session_factory() as s:
        owner_b = make_user(s, email="ownerb@ki.se")
        ws_b = make_workspace(s, owner=owner_b)
        s.commit()
        ws_b_id, owner_b_id = ws_b.id, owner_b.id
    aid_b = _upload(app, user_id=owner_b_id, ws_id=ws_b_id).json()["attachment_id"]
    # member of A requests B's attachment id under A's URL -> 404 (not 403, not 200).
    r = authed_client(app, user_id=member_a).get(f"/api/workspaces/{ws_a}/files/{aid_b}")
    assert r.status_code == 404, r.text
```

- [ ] **Step 6: Run the file-serve tests to verify they fail**

Run: `uv run pytest tests/test_api_files.py -k serve_file -q`
Expected: FAIL — no such route yet.

- [ ] **Step 7: Add the file serve**

First, the module docstring (lines 1–8) currently claims "Image BYTES are served by the existing Jinja GET … (no new binary serve here — Slice G owns that)." That is now false. Replace that sentence so the docstring reads:

```python
"""JSON /api surface for image attachments (Cockpit Library Files, Slice C-media).

Owner-gated writes (upload/delete) + member-gated list + member-gated byte
serve, REUSING the relocated files helpers (_probe_image_format, _read_capped,
AttachmentStorage, estimate_image_tokens) and inlining the Jinja upload_file
flow. The byte serve (GET /workspaces/{id}/files/{aid}) was added in the Jinja
teardown G1; the parallel Jinja serve coexists until G2 deletes it.
"""
```

Then, in `src/call_manager/web/api/files.py`, add `import logging` to the stdlib import group (with `contextlib`/`uuid`), add the `FileResponse` import after line 16:

```python
from fastapi.responses import FileResponse
```

and add a module logger after the imports (near the top, before `router = APIRouter()`):

```python
logger = logging.getLogger(__name__)
```

Append this route to the module:

```python
@router.get("/workspaces/{workspace_id}/files/{attachment_id}")
def api_serve_file(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> FileResponse:
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        att = session.get(Attachment, attachment_id)
        # IDOR defense: return 404 (NOT 403) on cross-workspace lookups so we
        # don't leak the existence of attachments outside the caller's view.
        if att is None or att.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="attachment not found")
        mime_type = att.mime_type
        filename = att.filename
        storage_suffix = Path(att.storage_path).suffix.lower()

    storage = _storage_for(request)
    try:
        path = storage.path_for(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            suffix=storage_suffix,
        )
    except ValueError as e:
        # Unsupported suffix snuck into the DB somehow — treat as missing.
        logger.warning(
            "api_serve_file: attachment %s has disallowed suffix %r: %s",
            attachment_id,
            storage_suffix,
            e,
        )
        raise HTTPException(status_code=404, detail="attachment not found") from e
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")

    safe_filename = _sanitize_filename(filename or f"attachment{storage_suffix}")
    return FileResponse(
        path,
        media_type=mime_type,
        headers={
            # private: a shared proxy MUST NOT cache and serve to another user.
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{safe_filename}"',
        },
    )
```

- [ ] **Step 8: Run the file-serve tests to verify they pass**

Run: `uv run pytest tests/test_api_files.py -k serve_file -q`
Expected: 4 passed.

- [ ] **Step 9: Repoint the three React literals**

`frontend/src/cockpit/LibraryPanel.tsx:174` —
`href={`/workspaces/${id}/references/${r.id}/raw`}` → `href={`/api/workspaces/${id}/references/${r.id}/raw`}`

`frontend/src/cockpit/LibraryPanel.tsx:225` —
`src={`/workspaces/${id}/files/${f.id}`}` → `src={`/api/workspaces/${id}/files/${f.id}`}`

`frontend/src/cockpit/chat/TurnList.tsx:33` —
`src={`/workspaces/${id}/files/${b.attachment_id}`}` → `src={`/api/workspaces/${id}/files/${b.attachment_id}`}`

- [ ] **Step 10: Update the two frontend test expectations**

`frontend/src/cockpit/LibraryPanel.test.tsx:141` —
`expect(img.getAttribute("src")).toBe("/workspaces/ws1/files/a1");` → `.toBe("/api/workspaces/ws1/files/a1");`

`frontend/src/cockpit/chat/TurnList.test.tsx:61` —
`expect(img.getAttribute("src")).toBe("/workspaces/ws1/files/a1");` → `.toBe("/api/workspaces/ws1/files/a1");`

- [ ] **Step 11: Run the frontend gate**

```bash
cd frontend
pnpm exec tsc --noEmit          # Expected: clean
pnpm vitest run LibraryPanel TurnList   # Expected: pass with the /api URLs
```

- [ ] **Step 12: Regenerate the OpenAPI types**

```bash
cd <worktree root>
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
# Expected: "wrote frontend/src/api/schema.gen.ts"
git diff --stat frontend/src/api/schema.gen.ts
# Expected: schema.gen.ts changed — gains the 2 new paths
#   /api/workspaces/{workspace_id}/references/{reference_id}/raw
#   /api/workspaces/{workspace_id}/files/{attachment_id}
git status --porcelain frontend/src/api/routes.gen.ts
# Expected: no output (G1 adds no React routes; routes.gen.ts is untouched).
```

- [ ] **Step 13: Ruff + commit**

```bash
cd <worktree root>
# --fix auto-sorts the new imports (FileResponse / logging) so the import-order
# rule (I001) doesn't trip; then confirm a clean check.
uv run ruff check --fix src/call_manager/web/api/references.py src/call_manager/web/api/files.py \
  tests/test_api_references.py tests/test_api_files.py
uv run ruff check src/call_manager/web/api/references.py src/call_manager/web/api/files.py \
  tests/test_api_references.py tests/test_api_files.py
# Expected: clean
git add src/call_manager/web/api/references.py src/call_manager/web/api/files.py \
  tests/test_api_references.py tests/test_api_files.py \
  frontend/src/cockpit/LibraryPanel.tsx frontend/src/cockpit/chat/TurnList.tsx \
  frontend/src/cockpit/LibraryPanel.test.tsx frontend/src/cockpit/chat/TurnList.test.tsx \
  frontend/src/api/schema.gen.ts
git commit -m "feat(spa): /api binary serves (reference raw + file bytes) + repoint React, OpenAPI regen (G1)"
```

---

### Task 6: Add the durable severance guard

A regression test that fails if any kept module ever re-imports from a doomed Jinja module. It must scan the WHOLE `web/api` package (routers + the `__init__.py`/`errors.py`/`events.py` support files) plus the 7 JSON modules, and it must NOT false-positive on the relative `from .workspaces import …` form inside `web/api/` (that refers to the KEPT `web/api/workspaces.py`, not the doomed one).

**Files:**
- Create: `tests/test_jinja_severance.py`

- [ ] **Step 1: Write the guard test**

```python
"""Regression guard for the Jinja teardown (G1).

The React cockpit's kept backend — the JSON ``web/api`` package and the 7
``proposal_assist/web`` JSON modules — must import NOTHING from the doomed Jinja
UI modules (deleted in G2). If this fails, a kept module re-coupled to a module
scheduled for deletion; move the shared symbol to a neutral home instead (see
proposal_assist/web/_deps.py, workspace_meta.py, _file_uploads.py,
references/uploads.py).
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "call_manager"

_DOOMED = ("workspaces", "files", "references", "sections", "citations", "annotations")

# Kept: the whole web/api package (routers + support files) ...
_KEPT_API = sorted((_SRC / "web" / "api").glob("*.py"))
# ... plus the 7 live JSON modules under proposal_assist/web.
_KEPT_JSON = [
    _SRC / "proposal_assist" / "web" / f"{m}.py"
    for m in (
        "proposals",
        "activity",
        "problems",
        "coherence",
        "evaluator",
        "review",
        "uncited_claims",
    )
]
_KEPT = _KEPT_API + _KEPT_JSON

# Absolute import of a doomed proposal_assist.web module (unambiguous).
_ABS = re.compile(
    r"from\s+call_manager\.proposal_assist\.web\.(" + "|".join(_DOOMED) + r")\s+import"
)
# Same-package relative import (only meaningful inside proposal_assist/web,
# where `.workspaces` IS the doomed module). NOT applied to web/api, whose
# own `.workspaces` is the KEPT web/api/workspaces.py.
_REL = re.compile(r"from\s+\.(" + "|".join(_DOOMED) + r")\s+import")


def test_kept_modules_do_not_import_from_doomed_jinja_modules():
    offenders = []
    for path in _KEPT:
        for m in _ABS.finditer(path.read_text(encoding="utf-8")):
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)} -> proposal_assist.web.{m.group(1)} (absolute)"
            )
    for path in _KEPT_JSON:
        for m in _REL.finditer(path.read_text(encoding="utf-8")):
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)} -> .{m.group(1)} (relative, same package)"
            )
    assert not offenders, "kept modules re-coupled to doomed Jinja modules:\n" + "\n".join(
        offenders
    )


def test_guard_kept_set_is_real_not_vacuous():
    # If the globs/lists silently resolved to nothing, the guard above would
    # pass vacuously. Pin the kept set so that can't happen unnoticed.
    for p in _KEPT:
        assert p.exists(), f"kept module missing: {p}"
    assert len(_KEPT_API) >= 10, "web/api package glob resolved too few modules"
    assert {p.stem for p in _KEPT_JSON} == {
        "proposals",
        "activity",
        "problems",
        "coherence",
        "evaluator",
        "review",
        "uncited_claims",
    }
    # web/api/workspaces.py (the KEPT /api module) is in the scanned set — proof
    # the guard does not confuse it with the doomed proposal_assist/web/workspaces.py.
    assert (_SRC / "web" / "api" / "workspaces.py") in _KEPT_API
```

- [ ] **Step 2: Run the guard — expect PASS (couplings already severed by Tasks 1–5)**

Run: `uv run pytest tests/test_jinja_severance.py -q`
Expected: 2 passed.

- [ ] **Step 3: Prove the guard has teeth (test-of-the-test, then revert)**

A guard that cannot fail is worthless. Temporarily add a doomed import to one kept module and confirm RED, then revert and confirm GREEN.

```bash
# Inject a fake re-coupling into a kept JSON module:
printf '\nfrom call_manager.proposal_assist.web.workspaces import _session_dep  # TEMP\n' \
  >> src/call_manager/proposal_assist/web/problems.py
uv run pytest tests/test_jinja_severance.py::test_kept_modules_do_not_import_from_doomed_jinja_modules -q
# Expected: FAIL, listing problems.py -> proposal_assist.web.workspaces (absolute)

# Revert the injection:
git checkout -- src/call_manager/proposal_assist/web/problems.py
uv run pytest tests/test_jinja_severance.py -q
# Expected: 2 passed
```

- [ ] **Step 4: Frontend severance verification (no Jinja `/workspaces/` literal remains)**

```bash
cd frontend
grep -rn "/workspaces/" src --include=*.ts --include=*.tsx | grep -v schema.gen | grep -v "/api/workspaces/"
# Expected: no output. (Every remaining /workspaces/ literal is now /api/workspaces/;
# schema.gen.ts still lists the Jinja paths — generated, deleted in G2.)
```

- [ ] **Step 5: Commit**

```bash
cd <worktree root>
git add tests/test_jinja_severance.py
git commit -m "test(spa): durable severance guard — kept modules import nothing from doomed Jinja modules (G1)"
```

---

## Final Verification (before the whole-branch review)

Run from the worktree root:

```bash
# Backend: the relocations' affected suites + the two new serve suites + the guard.
uv run pytest tests/test_api_settings.py tests/test_api_files.py tests/test_api_references.py \
  tests/test_jinja_severance.py -q
# Plus the 7 JSON-module suites (exercise the relocated _session_dep):
uv run pytest \
  tests/proposal_assist/test_web_proposed_changes.py \
  tests/proposal_assist/activity/ tests/proposal_assist/problems/ \
  tests/proposal_assist/coherence/ tests/proposal_assist/evaluator/ \
  tests/proposal_assist/review/ tests/proposal_assist/uncited_claims/ -q
uv run ruff check src tests        # Expected: clean
uv run python -c "import call_manager.main; print('app imports clean')"

# Frontend:
cd frontend
pnpm exec tsc --noEmit             # Expected: clean
pnpm vitest run                    # Expected: all pass
pnpm build                         # Expected: clean

# Severance proof (both directions):
cd <worktree root>
uv run pytest tests/test_jinja_severance.py -q   # backend guard
grep -rn "/workspaces/" frontend/src --include=*.ts --include=*.tsx | grep -v schema.gen | grep -v "/api/workspaces/"  # frontend: empty
```

**Acceptance (maps to spec §1 invariant):**
1. No `frontend/src` reference to a Jinja `/workspaces/…` route (only `/api/workspaces/…` + React `/cockpit`; `schema.gen.ts` excepted). ✓ via Task 6 Step 4.
2. No kept module imports from a doomed Jinja module. ✓ via `tests/test_jinja_severance.py`.
3. Nothing deleted; the Jinja UI still runs unchanged. ✓ (no doomed module, template, router registration, or `base.html` edited).
- No DB schema change (alembic untouched). The two new `/api` paths appear in `schema.gen.ts`; `routes.gen.ts` unchanged.

## Out of scope (G2 / later)

All deletion (doomed routes/templates/modules, the 6 `include_router` removals, the `base.html` `/workspaces` nav link), removing the transient helper duplication left in the doomed modules, and deleting the Jinja `serve_reference_raw`/`serve_file` routes (kept until React is verified on `/api`). See spec §6.
