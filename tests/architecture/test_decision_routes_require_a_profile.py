"""`profile_id` is REQUIRED on every decision route, and the TYPES say so.

A decision belongs to a profile: `call_decisions.profile_id` is NOT NULL, so a
request without one has no row to write and the route answers 422 (D3, D9).
Six operations carry that rule -- hide, un-hide, save, un-save, the hidden list
and the rail's counts -- and every one of them is reached from the frontend
through `frontend/src/api/schema.gen.ts`, which openapi-fetch uses to typecheck
the call site.

WHAT THIS PINS THAT NOTHING ELSE DOES. The backend half is pinned by the API
tests (a request with no `profile_id` gets 422). The generated-file half is
pinned by `pnpm gen:api && git diff --exit-code`, which proves the file is IN
STEP with the app -- not that the shape in it is the required one. And "this
file must change" is a gate that is red on correct code in every task that does
not move the schema (T8-3). So the guarantee is asserted as a SHAPE: if
`profile_id` were ever generated optional, `api.POST("/api/calls/{call_id}/hide",
{ params: { query: {} } })` would typecheck, `pnpm build` would stay green, and
the 422 would only show up in a browser.

WHAT IT CANNOT SEE. A brand-new decision path -- say `/api/calls/{call_id}/star`
-- is invisible here, because the four paths below are the ones someone wrote
down (the T2-13 shape). What it DOES catch is the hazard F-12 #1 names: a new
METHOD on an existing decision path (a `@router.patch("/calls/{call_id}/hide")`)
moves no line in `tests/architecture/full_edition_routes.txt`, which holds unique
PATHS -- but it does add an operation under that path, and the exact-set check
below goes red on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "frontend" / "src" / "api" / "schema.gen.ts"

# The four paths that write or read `call_decisions`.
DECISION_PATHS = (
    "/api/calls/{call_id}/hide",
    "/api/calls/{call_id}/save",
    "/api/calls/hidden",
    "/api/calls/decisions/counts",
)

# Their six operations, READ OUT OF THE FILE, not predicted. The set equality
# below is what keeps this list honest: a seventh operation on any of those four
# paths reddens it rather than slipping past unlisted.
EXPECTED_OPERATIONS = frozenset(
    {
        "hide_call_api_calls__call_id__hide_post",
        "unhide_call_api_calls__call_id__hide_delete",
        "save_call_api_calls__call_id__save_post",
        "unsave_call_api_calls__call_id__save_delete",
        "list_hidden_calls_api_calls_hidden_get",
        "get_decision_counts_api_calls_decisions_counts_get",
    }
)

_METHOD = re.compile(
    r'^\s*(?:get|put|post|delete|options|head|patch|trace):\s*operations\["([^"]+)"\];'
)
# A top-level member of `paths` or `operations`: four spaces, then a name. The
# bodies are indented eight or more, so this is what ends a block. Brace counting
# would be the obvious alternative and is wrong here -- these blocks carry the
# route docstrings verbatim, braces and all.
_NEXT_MEMBER = re.compile(r'^    ["A-Za-z_]')


def _member_block(src: str, header: str) -> list[str]:
    """The lines of the `paths`/`operations` member whose opening line is `header`.

    Fail-closed: a header this file names and the schema does not have raises,
    rather than returning an empty block that every check below would pass over.
    """
    lines = src.splitlines()
    matches = [i for i, ln in enumerate(lines) if ln == header]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {header!r} in schema.gen.ts, found {len(matches)} -- "
            "regenerate with `pnpm -C frontend gen:api`, or fix this list"
        )
    start = matches[0]
    for j in range(start + 1, len(lines)):
        if _NEXT_MEMBER.match(lines[j]) or lines[j] == "}":
            return lines[start:j]
    return lines[start:]


def declared_decision_operations(src: str) -> set[str]:
    """Every operation id declared under the four decision paths."""
    found: set[str] = set()
    for path in DECISION_PATHS:
        for line in _member_block(src, f'    "{path}": {{'):
            m = _METHOD.match(line)
            if m:
                found.add(m.group(1))
    return found


def _query_shape(block: list[str]) -> tuple[str, str | None]:
    """`(the token before the colon on the `query` line, the whole profile_id line)`.

    Returns `("query", "profile_id: number;")` for a required parameter and
    something else for anything weaker -- `"query?"`, `"profile_id?: number;"`,
    `"profile_id: number | null;"`, or `None` when there is no such parameter.
    """
    try:
        p = block.index("        parameters: {")
    except ValueError as exc:  # pragma: no cover - fail closed
        raise AssertionError("no `parameters: {` in this operation") from exc
    query_line = block[p + 1].strip()
    if not query_line.startswith("query"):
        raise AssertionError(f"expected a `query` line under parameters, got {query_line!r}")
    token = query_line.split(":", 1)[0].strip()
    if not query_line.endswith("{"):
        # `query?: never;` -- the route takes no query parameters at all.
        return token, None
    for line in block[p + 2 :]:
        stripped = line.strip()
        if stripped == "};":
            break
        if stripped.startswith("profile_id"):
            return token, stripped
    return token, None


def operations_not_requiring_a_profile(src: str) -> list[str]:
    """The decision operations whose `profile_id` a caller could omit. Empty is the pass."""
    weak: list[str] = []
    for op in sorted(EXPECTED_OPERATIONS):
        token, profile = _query_shape(_member_block(src, f"    {op}: {{"))
        if token != "query":
            weak.append(f"{op}: `{token}` -- the whole query block is optional")
        elif profile is None:
            weak.append(f"{op}: no `profile_id` among the query parameters")
        elif profile != "profile_id: number;":
            weak.append(f"{op}: `{profile}` -- not a required int")
    return weak


def test_every_decision_operation_requires_a_profile_id():
    assert operations_not_requiring_a_profile(SCHEMA.read_text()) == [], (
        "a decision needs a profile (D3): `profile_id` must be a REQUIRED int on "
        "every one of these, or a call site that omits it typechecks and 422s"
    )


def test_the_decision_paths_declare_exactly_these_six_operations():
    """The exact-count control. Without it, a seventh operation on one of these
    paths -- the `@router.patch("/calls/{call_id}/hide")` that F-12 #1 warns moves
    no line in the route manifest -- would simply not be checked."""
    assert declared_decision_operations(SCHEMA.read_text()) == set(EXPECTED_OPERATIONS)


def _doctor(src: str, op: str, old: str, new: str) -> str:
    """Rewrite one operation's block in memory. Aborts if the anchor did not match."""
    lines = src.splitlines()
    header = f"    {op}: {{"
    start = lines.index(header)
    block = _member_block(src, header)
    body = "\n".join(block)
    if old not in body:
        raise AssertionError(f"anchor {old!r} is not in {op} -- this control proves nothing")
    lines[start : start + len(block)] = body.replace(old, new, 1).splitlines()
    return "\n".join(lines)


def test_the_scan_catches_an_optional_query_block():
    """Proof by breaking, on a doctored copy: `query?: {` makes every parameter
    under it -- `profile_id` included -- omissible, while `profile_id: number;`
    still reads as required one line down."""
    doctored = _doctor(
        SCHEMA.read_text(), "hide_call_api_calls__call_id__hide_post", "query: {", "query?: {"
    )
    assert operations_not_requiring_a_profile(doctored) == [
        "hide_call_api_calls__call_id__hide_post: `query?` -- the whole query block is optional"
    ]


def test_the_scan_catches_an_optional_profile_id():
    doctored = _doctor(
        SCHEMA.read_text(),
        "hide_call_api_calls__call_id__hide_post",
        "profile_id: number;",
        "profile_id?: number;",
    )
    assert operations_not_requiring_a_profile(doctored) == [
        "hide_call_api_calls__call_id__hide_post: `profile_id?: number;` -- not a required int"
    ]


def test_the_scan_refuses_a_schema_that_lost_an_operation():
    """FAIL CLOSED. An operation that disappeared -- renamed on the backend, or the
    route deleted -- must raise here, not return an empty block that passes."""
    src = SCHEMA.read_text().replace(
        "    unsave_call_api_calls__call_id__save_delete: {",
        "    unsave_call_api_calls__call_id__save_delete_RENAMED: {",
    )
    with pytest.raises(AssertionError, match="expected exactly one"):
        operations_not_requiring_a_profile(src)
