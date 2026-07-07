# Collab Slice 3b-5d.D2 — Awareness Relay-Spoof Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A connected collab client can no longer hide, override, or displace another user's caret via the awareness relay — a socket may relay awareness entries only for client-ids bound to its own authenticated user.

**Architecture:** Two components per the spec (`docs/superpowers/specs/2026-07-07-collab-slice3b5d2-awareness-spoof-filter-design.md`). Component A (`protocol.py`): refactor the hardened awareness parser into an entry-span parser (`read_awareness_entries`, with `is_null` per entry) plus a byte-span filter (`filter_awareness_update`) with a byte-identical all-allowed fast path. Component B (`room.py`): the awareness branch partitions entries by a room-lifetime per-USER binding map (`client_id → user_id`), binds first-announces under a per-user quota, applies + relays only the FILTERED payload (fixing snapshot poisoning too), and logs foreign entries split by kind (null-state stale-scrub = INFO, non-null spoof = WARNING). The 3b-5b `owned_by_other_users` attribution guard is removed — subsumed by the bindings.

**Tech Stack:** Python 3.12, pycrdt (`Awareness`), pytest (`pytest.mark.anyio`, `caplog`), FakeClient room-test pattern. No new dependencies.

## Global Constraints

- Feature flag **stays OFF** in prod until the explicit 3b-5d flip (D4). The awareness path is reachable only on a collab-enabled artifact → prod-inert until D4.
- **No migration, no schema change.** Backend-only; frontend untouched.
- All parsing preserves the f535f6c DoS bounds (capped varuints, bounds-checked, O(payload)).
- No new locks; no lock-ordering changes (new work sits inside the existing awareness-branch `room._lock` section; broadcast outside).
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped dirs, NEVER whole-suite; final sweep includes `tests/test_api_spec.py`.
- Lint: `uv run ruff check .` (line-length 100). Frontend gate: `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (regression only — no frontend changes in this slice).
- Explicit-path `git add` only (never `-A`). Merges are **local `--no-ff` only**, not pushed without an explicit ask. Do NOT invoke `/code-review` / `/ultrareview`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor` (cwd resets between turns).

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/collab/protocol.py` | Modify: `AwarenessEntry` NamedTuple, `read_awareness_entries`, `read_awareness_client_ids` → thin wrapper, `filter_awareness_update`, `_write_varuint` |
| `src/call_manager/proposal_assist/collab/room.py` | Modify: `_MAX_AWARENESS_BINDINGS_PER_USER` constant, `Room._awareness_bindings`, awareness-branch rework in `Room.handle` |
| `tests/proposal_assist/collab/test_collab_protocol.py` | Extend: entry-span parser + filter unit tests |
| `tests/proposal_assist/collab/test_collab_awareness.py` | Extend: room filter/binding/quota tests (FakeClient pattern) |

---

### Task 0: Cut the build branch

**Files:** none (git only).

- [ ] **Step 1: Verify master state and clean tree**

```bash
git -C /home/parlar_ai/dev/call-monitor branch --show-current
git -C /home/parlar_ai/dev/call-monitor log --oneline -1
git -C /home/parlar_ai/dev/call-monitor status --porcelain
```

Expected: branch `master`; HEAD at `94d93c3` or a later docs commit (the plan-doc commit); status shows at most untracked `docs/superpowers/notes/2026-06-24-*` / `docs/superpowers/specs/2026-06-28-*` files (pre-existing — leave them).

- [ ] **Step 2: Cut the branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b collab-slice3b5d2
```

Expected: `Switched to a new branch 'collab-slice3b5d2'`.

---

### Task 1: Component A.1 — entry-span parser `read_awareness_entries`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/protocol.py` (replace `read_awareness_client_ids` body, lines 81–126)
- Test: `tests/proposal_assist/collab/test_collab_protocol.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AwarenessEntry(client_id: int, start: int, end: int, is_null: bool)` (NamedTuple); `read_awareness_entries(payload: bytes) -> list[AwarenessEntry]` (raises `ValueError` on malformed frames, same as before); `read_awareness_client_ids(payload: bytes) -> list[int]` (unchanged signature, now a wrapper). `start:end` is the entry's full byte span (clientID varuint + clock varuint + varString state) so Task 2 can rebuild payloads by slicing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/collab/test_collab_protocol.py`:

```python
# ---------------------------------------------------------------------------
# 3b-5d.D2: entry-span parser (read_awareness_entries)
# ---------------------------------------------------------------------------


def _vu(n: int) -> bytes:
    """Base-128 varuint (y-protocols encoding) — test-local writer."""
    out = bytearray()
    while n > 0x7F:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


def _entry(cid: int, clock: int, state_json: bytes) -> bytes:
    """One awareness entry: [clientID varuint][clock varuint][varString state]."""
    return _vu(cid) + _vu(clock) + _vu(len(state_json)) + state_json


def test_read_awareness_entries_spans_reconstruct_payload():
    # Re-concatenating each entry's [start:end) span behind a fresh count varuint
    # must reproduce a real multi-client snapshot byte-for-byte.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "A"}})
    p2, c2 = _awareness_payload({"user": {"name": "B"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    entries = P.read_awareness_entries(snap)
    assert sorted(e.client_id for e in entries) == sorted([c1, c2])
    rebuilt = _vu(len(entries)) + b"".join(snap[e.start : e.end] for e in entries)
    assert rebuilt == snap


def test_read_awareness_entries_is_null_flags_removals_only():
    payload, cid = _awareness_payload({"user": {"name": "Alice"}})
    (announce,) = P.read_awareness_entries(payload)
    assert announce.client_id == cid and announce.is_null is False
    # A removal frame (state = JSON null) declares the id with is_null=True.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    srv.apply_awareness_update(payload, "x")
    srv.remove_awareness_states([cid], "srv")
    rem = srv.encode_awareness_update([cid])
    (removal,) = P.read_awareness_entries(rem)
    assert removal.client_id == cid and removal.is_null is True


def test_read_awareness_entries_preserves_dos_hardening():
    # The f535f6c adversarial cases, now against the refactored parser: the
    # refactor must not lose the bounds (capped varuints, prompt ValueError).
    import time

    t0 = time.perf_counter()
    with pytest.raises(ValueError):
        P.read_awareness_entries(b"\xff" * 900_000)  # all-continuation varuint run
    assert time.perf_counter() - t0 < 2.0  # bounded — nowhere near the ~66s stall
    with pytest.raises(ValueError):
        P.read_awareness_entries(b"\x03")  # lying count / truncated body
    with pytest.raises(ValueError):
        P.read_awareness_entries(bytes([1, 1, 0, 200]))  # state_len overshoot


def test_read_awareness_client_ids_is_a_wrapper_over_entries():
    # Hand-crafted two-entry payload; the big id spans multiple varuint groups.
    payload = _vu(2) + _entry(7, 1, b'{"x":1}') + _entry(2**40 + 3, 5, b"null")
    entries = P.read_awareness_entries(payload)
    assert P.read_awareness_client_ids(payload) == [e.client_id for e in entries]
    assert [e.client_id for e in entries] == [7, 2**40 + 3]
    assert [e.is_null for e in entries] == [False, True]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q
```

Expected: 4 FAILED with `AttributeError: ... has no attribute 'read_awareness_entries'`; all pre-existing tests PASS.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/protocol.py`:

Add to the imports block (top of file, after the pycrdt import):

```python
from typing import NamedTuple
```

Add `"read_awareness_entries"` and `"AwarenessEntry"` to `__all__` (after `"read_awareness_client_ids"`).

Replace the whole `read_awareness_client_ids` function (lines 81–126) with:

```python
class AwarenessEntry(NamedTuple):
    """One entry of a y-protocols awareness update: the declared client id, the
    entry's full byte span [start:end) within the payload (clientID varuint +
    clock varuint + varString state), and whether the state is the JSON literal
    null — a removal; JSON.stringify(null) === "null", so a 4-byte compare on
    the span already in hand, no JSON decode."""

    client_id: int
    start: int
    end: int
    is_null: bool


def read_awareness_entries(payload: bytes) -> list[AwarenessEntry]:
    """Parse the entries of a y-protocols awareness update WITHOUT mutating any
    Awareness. Update format (encodeAwarenessUpdate): a varuint count, then per
    client [clientID varuint][clock varuint][state varString]. Records each
    entry's byte span so a filter can rebuild a payload from original bytes.

    HARDENED against adversarial frames (f535f6c): every varuint is capped at 10
    bytes (a 64-bit value is at most 10 base-128 groups) and every read is
    bounds-checked against the payload, so a malformed/oversized frame — e.g. a
    long run of 0x80+ continuation bytes — raises PROMPTLY (O(payload)) instead
    of shifting a multi-megabit Python int. Without this cap a ~1 MB
    all-continuation payload (under the WS frame cap) is an O(n^2) CPU stall
    that blocks the entire single-instance asyncio event loop — a whole-process
    DoS, since the awareness path is ungated by can_write. The sole caller
    (room.handle) wraps this in try/except and drops the frame on any
    exception."""
    n = len(payload)
    pos = 0

    def _rvu() -> int:
        nonlocal pos
        num = 0
        shift = 0
        for _ in range(10):  # a 64-bit varuint is at most 10 base-128 bytes
            if pos >= n:
                raise ValueError("awareness varuint: truncated")
            b = payload[pos]
            pos += 1
            num |= (b & 0x7F) << shift
            if b < 0x80:
                return num
            shift += 7
        raise ValueError("awareness varuint: overlong")

    count = _rvu()
    entries: list[AwarenessEntry] = []
    # A conforming frame declares >= 3 bytes per client (id+clock+len), so `count`
    # cannot exceed `n`; a lying count simply raises "truncated" once the body is
    # exhausted (each iteration advances pos by >= 3), keeping this O(n).
    for _ in range(count):
        start = pos
        client_id = _rvu()
        _rvu()  # clock (skip)
        state_len = _rvu()  # varString byte length
        if state_len > n - pos:  # never skip past the payload
            raise ValueError("awareness varString: out of range")
        is_null = state_len == 4 and payload[pos : pos + 4] == b"null"
        pos += state_len
        entries.append(AwarenessEntry(client_id, start, pos, is_null))
    return entries


def read_awareness_client_ids(payload: bytes) -> list[int]:
    """The client ids declared in an awareness update — thin wrapper over
    read_awareness_entries (one parser, no drift)."""
    return [e.client_id for e in read_awareness_entries(payload)]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/protocol.py \
       tests/proposal_assist/collab/test_collab_protocol.py
```

Expected: ALL tests pass (new 4 + all pre-existing, including the f535f6c regression suite against the wrapper); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/protocol.py \
  tests/proposal_assist/collab/test_collab_protocol.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): entry-span awareness parser read_awareness_entries (3b-5d.D2)

Refactors the f535f6c-hardened id parser into an entry-span parser
(client_id + byte span + is_null per entry) so the D2 ownership filter can
rebuild payloads from original bytes. read_awareness_client_ids becomes a
thin wrapper — one parser, no drift; all DoS bounds preserved verbatim.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Component A.2 — `filter_awareness_update` + `_write_varuint`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/protocol.py` (append after `read_awareness_client_ids`)
- Test: `tests/proposal_assist/collab/test_collab_protocol.py` (append)

**Interfaces:**
- Consumes: `AwarenessEntry` / `read_awareness_entries` from Task 1.
- Produces: `filter_awareness_update(payload: bytes, entries: list[AwarenessEntry], allowed_ids: set[int]) -> bytes | None` — all allowed → the SAME `payload` object (byte-identical fast path); none allowed → `None`; mixed → rebuilt bytes. `_write_varuint(num: int) -> bytes` (module-private helper).

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/collab/test_collab_protocol.py`:

```python
# ---------------------------------------------------------------------------
# 3b-5d.D2: filter_awareness_update
# ---------------------------------------------------------------------------


def test_filter_awareness_update_all_allowed_is_byte_identical_fast_path():
    payload = _vu(2) + _entry(7, 1, b'{"a":1}') + _entry(9, 1, b'{"b":2}')
    entries = P.read_awareness_entries(payload)
    # `is`: the fast path must not copy — zero re-encode in normal operation.
    assert P.filter_awareness_update(payload, entries, {7, 9}) is payload


def test_filter_awareness_update_none_allowed_returns_none():
    payload = _vu(1) + _entry(7, 1, b'{"a":1}')
    entries = P.read_awareness_entries(payload)
    assert P.filter_awareness_update(payload, entries, {8}) is None


def test_filter_awareness_update_mixed_rebuilds_kept_spans():
    # A real two-client snapshot filtered to one id must parse to exactly the
    # kept id AND apply cleanly to a fresh pycrdt Awareness.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "Keep"}})
    p2, _c2 = _awareness_payload({"user": {"name": "Drop"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    entries = P.read_awareness_entries(snap)
    out = P.filter_awareness_update(snap, entries, {c1})
    assert out is not None and out != snap
    assert P.read_awareness_client_ids(out) == [c1]
    fresh = P.Awareness(_Doc())
    fresh.set_local_state(None)
    fresh.apply_awareness_update(out, "x")
    assert set(fresh.states) == {c1}
    assert fresh.states[c1]["user"]["name"] == "Keep"


def test_filter_awareness_update_empty_frame_is_all_allowed():
    # count=0: vacuously all-allowed → same object back (relay-unchanged).
    payload = _vu(0)
    assert P.filter_awareness_update(payload, [], set()) is payload
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q
```

Expected: 4 FAILED with `AttributeError: ... 'filter_awareness_update'`; everything else PASS.

- [ ] **Step 3: Implement**

Append to `src/call_manager/proposal_assist/collab/protocol.py` (after `read_awareness_client_ids`); add `"filter_awareness_update"` to `__all__`:

```python
def _write_varuint(num: int) -> bytes:
    """Base-128 varuint writer — mirror of the reader in read_awareness_entries."""
    out = bytearray()
    while num > 0x7F:
        out.append(0x80 | (num & 0x7F))
        num >>= 7
    out.append(num)
    return bytes(out)


def filter_awareness_update(
    payload: bytes, entries: list[AwarenessEntry], allowed_ids: set[int]
) -> bytes | None:
    """Drop the entries whose client_id is not in allowed_ids.

    All allowed -> the payload UNCHANGED, byte-identical (fast path — the
    overwhelmingly common case; zero re-encode in normal operation). None
    allowed -> None (nothing to relay). Mixed -> rebuilt as varuint(kept count)
    + the kept entries' ORIGINAL byte spans — entries are never re-encoded,
    only re-counted."""
    kept = [e for e in entries if e.client_id in allowed_ids]
    if len(kept) == len(entries):
        return payload
    if not kept:
        return None
    return _write_varuint(len(kept)) + b"".join(payload[e.start : e.end] for e in kept)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/protocol.py \
       tests/proposal_assist/collab/test_collab_protocol.py
```

Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/protocol.py \
  tests/proposal_assist/collab/test_collab_protocol.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): filter_awareness_update — ownership-filter codec (3b-5d.D2)

Byte-span filter over parsed awareness entries: all-allowed returns the
payload byte-identical (no re-encode on the hot path), none-allowed returns
None, mixed rebuilds from the kept entries' original bytes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Component B — room awareness branch: user-binding ownership filter

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`Room.__init__` + the awareness branch of `Room.handle`, currently lines 186–232)
- Test: `tests/proposal_assist/collab/test_collab_awareness.py` (append; add helpers + `import logging`)

**Interfaces:**
- Consumes: `P.read_awareness_entries`, `P.AwarenessEntry`, `P.filter_awareness_update` (Tasks 1–2).
- Produces: `Room._awareness_bindings: dict[int, int]` (client_id → user_id; room-lifetime, private). Log events: `collab.awareness_bind` (INFO), `collab.awareness_stale_scrub_filtered` (INFO), `collab.awareness_foreign_filtered` (WARNING). Task 4 inserts the quota block between the `new_ids = ...` assignment and the `relay = ...` line — keep those statements separate exactly as written. Binding happens AFTER a successful apply and only for ids accepted into `awareness.states` (Codex High: a parse-valid but unapplied entry must never stealth-reserve a binding).

- [ ] **Step 1: Write the failing tests**

In `tests/proposal_assist/collab/test_collab_awareness.py`, add `import logging` as the FIRST import (before `import uuid`), then append at the end of the file:

```python
# ---------------------------------------------------------------------------
# 3b-5d.D2: awareness relay-spoof ownership filter
# ---------------------------------------------------------------------------

ROOM_LOGGER = "call_manager.proposal_assist.collab.room"


def _vu(n: int) -> bytes:
    """Base-128 varuint (y-protocols encoding) — test-local writer."""
    out = bytearray()
    while n > 0x7F:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


def _entry(cid: int, clock: int, state_json: bytes) -> bytes:
    """One awareness entry: [clientID varuint][clock varuint][varString state]."""
    return _vu(cid) + _vu(clock) + _vu(len(state_json)) + state_json


def _crafted_frame(*entries: bytes) -> bytes:
    """A full AWARENESS wire frame from hand-crafted entries (attacker's view)."""
    payload = _vu(len(entries)) + b"".join(entries)
    return P.create_awareness_message(payload)


@pytest.mark.anyio
async def test_spoof_hide_blocked_and_flagged(caplog):
    # THE regression (3b-5b REQUIRED deferral): user B sends a high-clock
    # non-null override of A's id — must be filtered before ANYONE (the victim
    # included) receives it, must not poison the server snapshot, and must fire
    # the WARNING spoof signature.
    room = Room((uuid.uuid4(), "draft"))
    victim, attacker, observer = FakeClient(), FakeClient(), FakeClient()
    st_v = ClientState(user_id=1, can_write=True)
    st_a = ClientState(user_id=2, can_write=True)
    room.clients[victim] = st_v
    room.clients[attacker] = st_a
    room.clients[observer] = ClientState(user_id=3, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Victim"}})
    await room.handle(sender=victim, state=st_v, message=frame, persist=_noop_persist)
    observer.sent.clear()

    spoof = _crafted_frame(_entry(cid, 10**9, b'{"user":{"name":"Fake"}}'))
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=attacker, state=st_a, message=spoof, persist=_noop_persist)

    assert observer.sent == []  # not relayed to anyone
    assert room.awareness.states[cid]["user"]["name"] == "Victim"  # state intact
    assert cid not in st_a.awareness_ids  # not attributed to the attacker
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "collab.awareness_foreign_filtered" in warnings
    # A new joiner's snapshot is spoof-free.
    snapshot = await room.awareness_snapshot()
    joiner = Awareness(Doc())
    joiner.set_local_state(None)
    joiner.apply_awareness_update(P.extract_awareness_update(snapshot), "srv")
    assert joiner.states[cid]["user"]["name"] == "Victim"


@pytest.mark.anyio
async def test_foreign_null_scrub_filtered_benignly_at_info(caplog):
    # Legit clients DO send foreign null-state frames all day: y-protocols' 3s
    # local timeout-scrub of a backgrounded tab ships with origin 'timeout',
    # which the provider's origin guard does not suppress. Filtered (each peer
    # scrubs locally — the relay is redundant) but logged INFO, NOT the WARNING
    # spoof signature. Guards the log-level split.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    frame, cid = _awareness_frame({"user": {"name": "Backgrounded"}})
    await room.handle(sender=a, state=st_a, message=frame, persist=_noop_persist)
    a.sent.clear()

    scrub = _crafted_frame(_entry(cid, 1, b"null"))
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=b, state=st_b, message=scrub, persist=_noop_persist)

    assert a.sent == []  # not relayed
    assert cid in room.awareness.states  # A's presence intact
    stale = [
        r for r in caplog.records if r.getMessage() == "collab.awareness_stale_scrub_filtered"
    ]
    assert stale and all(r.levelno == logging.INFO for r in stale)
    assert not [
        r for r in caplog.records if r.getMessage() == "collab.awareness_foreign_filtered"
    ]


@pytest.mark.anyio
async def test_mixed_frame_stripped_to_own_entries():
    room = Room((uuid.uuid4(), "draft"))
    a, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    frame, cid_a = _awareness_frame({"user": {"name": "A"}})
    await room.handle(sender=a, state=st_a, message=frame, persist=_noop_persist)
    peer.sent.clear()

    own_id = 555
    mixed = _crafted_frame(
        _entry(own_id, 1, b'{"user":{"name":"B"}}'),
        _entry(cid_a, 10**9, b'{"user":{"name":"Fake"}}'),
    )
    await room.handle(sender=b, state=st_b, message=mixed, persist=_noop_persist)

    assert peer.sent  # something WAS relayed
    relayed = P.extract_awareness_update(peer.sent[-1])
    assert P.read_awareness_client_ids(relayed) == [own_id]  # foreign entry stripped
    assert room.awareness.states[cid_a]["user"]["name"] == "A"
    assert room.awareness.states[own_id]["user"]["name"] == "B"
    assert st_b.awareness_ids == {own_id}


@pytest.mark.anyio
async def test_binding_outlives_socket_blocks_inversion_allows_same_user_reclaim():
    # Attack #2: B cannot claim a blipped peer's id (room-lifetime USER
    # binding); the victim's same-user reconnect re-claims it freely (R2-2).
    room = Room((uuid.uuid4(), "draft"))
    a1, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_a1 = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a1] = st_a1
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "A"}})
    await room.handle(sender=a1, state=st_a1, message=frame, persist=_noop_persist)

    room.clients.pop(a1)  # A's socket blips
    await room.remove_client_presence(st_a1)
    peer.sent.clear()

    steal = _crafted_frame(_entry(cid, 10**9, b'{"user":{"name":"NotA"}}'))
    await room.handle(sender=b, state=st_b, message=steal, persist=_noop_persist)
    assert peer.sent == []  # filtered: the binding outlives the socket
    assert cid not in st_b.awareness_ids

    a2 = FakeClient()  # same user reconnects (network blip → same doc.clientID)
    st_a2 = ClientState(user_id=1, can_write=True)
    room.clients[a2] = st_a2
    renew = _crafted_frame(_entry(cid, 10, b'{"user":{"name":"A"}}'))
    await room.handle(sender=a2, state=st_a2, message=renew, persist=_noop_persist)
    assert peer.sent and P.is_awareness(peer.sent[-1])  # relayed
    assert cid in st_a2.awareness_ids  # re-attributed to the reconnect
    assert room.awareness.states[cid]["user"]["name"] == "A"


@pytest.mark.anyio
async def test_first_announce_binds_and_logs(caplog):
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=7, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "New"}})
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    assert room._awareness_bindings == {cid: 7}
    assert b.sent  # relayed
    assert "collab.awareness_bind" in [r.getMessage() for r in caplog.records]


@pytest.mark.anyio
async def test_all_own_frame_relays_byte_identical():
    # The fast path: zero re-encode — the relayed frame IS the inbound frame.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    frame, _ = _awareness_frame({"user": {"name": "Own"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    assert b.sent == [frame]


@pytest.mark.anyio
async def test_empty_awareness_frame_passthrough():
    # count=0: trivially all-allowed → relayed unchanged (behavior-preserving
    # vs pre-D2), no bindings created.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    empty = _crafted_frame()  # payload = b"\x00"
    await room.handle(sender=a, state=st, message=empty, persist=_noop_persist)
    assert b.sent == [empty]
    assert room._awareness_bindings == {}


@pytest.mark.anyio
async def test_own_null_removal_still_relays_and_clears():
    # Legit self-removal (provider destroy/unload path) must keep working: an
    # own-bound null entry is allowed, relayed, and clears the server state.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    room.clients[b] = ClientState(user_id=2, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Leaving"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)
    b.sent.clear()
    bye = _crafted_frame(_entry(cid, 5, b"null"))
    await room.handle(sender=a, state=st, message=bye, persist=_noop_persist)
    assert b.sent and P.is_awareness(b.sent[-1])  # removal relayed
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_failed_apply_does_not_reserve_bindings():
    # Codex High (a): a span-valid frame whose apply FAILS (invalid JSON state)
    # must not bind — else an attacker could stealth-pre-claim an id (no visible
    # caret, since nothing applied) and the real user's later announce would be
    # filtered as foreign for the room's life.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424242
    junk = _crafted_frame(_entry(x, 10**9, b"{not json"))  # span-valid, apply fails
    await room.handle(sender=b, state=st_b, message=junk, persist=_noop_persist)
    assert x not in room._awareness_bindings
    assert a.sent == []  # dropped, not relayed
    # The real user announces X afterwards: bound to THEM, relayed.
    announce = _crafted_frame(_entry(x, 1, b'{"user":{"name":"RealA"}}'))
    await room.handle(sender=a, state=st_a, message=announce, persist=_noop_persist)
    assert room._awareness_bindings[x] == 1
    assert b.sent and P.is_awareness(b.sent[-1])


@pytest.mark.anyio
async def test_clock_ignored_entry_does_not_reserve_binding():
    # Codex High (b): a valid entry pycrdt IGNORES on apply (clock 0 is never
    # accepted: 0 < 0 is false) must not bind either — bind-after-accept, not
    # bind-on-parse.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424243
    stale = _crafted_frame(_entry(x, 0, b'{"user":{"name":"Ghost"}}'))
    await room.handle(sender=b, state=st_b, message=stale, persist=_noop_persist)
    assert x not in room._awareness_bindings  # ignored → not bound
    assert x not in room.awareness.states
    # The real user's announce still binds to them.
    announce = _crafted_frame(_entry(x, 1, b'{"user":{"name":"RealA"}}'))
    await room.handle(sender=a, state=st_a, message=announce, persist=_noop_persist)
    assert room._awareness_bindings[x] == 1
    assert x in room.awareness.states


@pytest.mark.anyio
async def test_unbound_null_preclaim_is_not_attributed_to_sender():
    # A null entry for an UNBOUND id is allowed through (harmless removal, never
    # enters states) but neither binds nor attributes — else the sender's later
    # disconnect would scrub the id's REAL owner (who binds it afterwards) for
    # everyone: the 3b-5b griefing vector resurrected through the new code.
    room = Room((uuid.uuid4(), "draft"))
    a, b = FakeClient(), FakeClient()
    st_a = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[a] = st_a
    room.clients[b] = st_b
    x = 424244
    await room.handle(
        sender=b, state=st_b,
        message=_crafted_frame(_entry(x, 5, b"null")), persist=_noop_persist,
    )
    assert x not in st_b.awareness_ids and x not in room._awareness_bindings
    # Real owner announces afterwards (clock above the stale null's meta clock)…
    await room.handle(
        sender=a, state=st_a,
        message=_crafted_frame(_entry(x, 6, b'{"user":{"name":"RealA"}}')),
        persist=_noop_persist,
    )
    assert room._awareness_bindings[x] == 1
    assert x in room.awareness.states
    # …and B's departure cannot scrub it.
    await room.remove_client_presence(st_b)
    assert x in room.awareness.states
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q
```

Expected: the 11 new tests FAIL (`AttributeError: 'Room' object has no attribute '_awareness_bindings'`, spoof frames relayed, etc.); all 13 pre-existing tests PASS.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/room.py`:

**(a)** In `Room.__init__`, directly after `self.awareness.set_local_state(None)`, add:

```python
        # client_id → user_id ownership bindings (Slice 3b-5d.D2). First-announce,
        # per-USER, ROOM-lifetime (the map dies with the Room on eviction): a
        # socket may relay awareness entries only for ids bound to its own
        # authenticated user. Room-lifetime (not socket-lifetime) is what blocks
        # ownership inversion — a griefer cannot claim a blipped peer's id, while
        # the victim's same-user reconnect re-claims it freely (R2-2).
        self._awareness_bindings: dict[int, int] = {}
```

**(b)** Replace the ENTIRE awareness branch of `Room.handle` — everything from `if P.is_awareness(message):` through `await self._broadcast(P.create_awareness_message(payload), exclude=sender)` and its `return` (currently lines 186–232, including the 3b-5b `owned_by_other_users` block and its NOTE comment, which this slice completes and removes) — with:

```python
        if P.is_awareness(message):
            # Presence relay: ungated (not a doc mutation) and NEVER persisted.
            # Parse OUTSIDE the lock (bounded, O(payload) — f535f6c hardening).
            try:
                payload = P.extract_awareness_update(message)
                entries = P.read_awareness_entries(payload)
            except Exception:
                logger.warning(
                    "collab.malformed_awareness",
                    extra={"event": "collab_malformed_awareness", "room": str(self.key)},
                )
                return
            async with self._lock:
                # Ownership filter (Slice 3b-5d.D2, closes the 3b-5b deferral): a
                # socket may introduce, update, or remove awareness entries ONLY
                # for client-ids bound to its own authenticated user. This kills
                # the ACTIVE relay-spoof (a high-clock hide/fake of a peer's
                # caret is clock-gated on receivers and does NOT self-heal)
                # before anyone — the victim included — receives it.
                kept: list[P.AwarenessEntry] = []
                foreign: list[P.AwarenessEntry] = []
                for e in entries:
                    owner = self._awareness_bindings.get(e.client_id)
                    (kept if owner in (None, state.user_id) else foreign).append(e)
                if foreign:
                    # Foreign NULL-state entries are ROUTINE: y-protocols' 3s
                    # local timeout-scrub of a backgrounded tab ships with
                    # origin 'timeout', which the provider's origin guard does
                    # not suppress → INFO. Foreign NON-null entries are the true
                    # spoof signature → WARNING. The level split is load-bearing:
                    # without it the daily scrub noise drowns the spoof signal.
                    stale = [e.client_id for e in foreign if e.is_null]
                    spoof = [e.client_id for e in foreign if not e.is_null]
                    if stale:
                        logger.info(
                            "collab.awareness_stale_scrub_filtered",
                            extra={"event": "collab_awareness_stale_scrub_filtered",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "client_ids": stale},
                        )
                    if spoof:
                        logger.warning(
                            "collab.awareness_foreign_filtered",
                            extra={"event": "collab_awareness_foreign_filtered",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "client_ids": spoof},
                        )
                if entries and not kept:
                    return  # pure-foreign frame (routine stale-scrub): drop
                kept_ids = {e.client_id for e in kept}
                new_ids = kept_ids - self._awareness_bindings.keys()
                relay = P.filter_awareness_update(payload, entries, kept_ids)
                if relay is None:  # unreachable (kept is non-empty) — defensive
                    return
                try:
                    # Apply the FILTERED payload, so a spoofed entry can never
                    # poison the snapshot new joiners are primed with.
                    self.awareness.apply_awareness_update(relay, origin="client")
                except Exception:
                    logger.warning(
                        "collab.malformed_awareness_update",
                        extra={"event": "collab_malformed_awareness_update",
                               "room": str(self.key)},
                    )
                    return  # nothing bound, nothing relayed (bind-after-accept)
                # Bind AFTER a successful apply, and only the new ids pycrdt
                # actually ACCEPTED into states (Codex High): a parse-valid
                # entry that fails or is clock-ignored on apply must not reserve
                # a binding, or a stealth pre-claim (malformed JSON state /
                # stale clock — no visible caret either way) would filter the
                # id's real owner for the room's life.
                for cid in new_ids:
                    if cid in self.awareness.states:
                        self._awareness_bindings[cid] = state.user_id
                        logger.info(
                            "collab.awareness_bind",
                            extra={"event": "collab_awareness_bind",
                                   "room": str(self.key),
                                   "client_id": cid, "user_id": state.user_id},
                        )
                # Attribute to THIS socket only ids bound to THIS user (drives
                # disconnect removal). The 3b-5b owned_by_other_users guard is
                # subsumed: a foreign-bound id is filtered above, and an
                # unbound-never-accepted id (e.g. a null pre-claim) is never
                # attributed — so this socket's disconnect can never scrub an
                # id someone else binds later. Attribution from kept ids (not a
                # states-diff) keeps R2-2 same-clientID reconnects working.
                state.awareness_ids |= {
                    cid for cid in kept_ids
                    if self._awareness_bindings.get(cid) == state.user_id
                }
            # Relay the FILTERED payload outside the lock (standard y-awareness
            # relay; a slow client must not block the room).
            await self._broadcast(P.create_awareness_message(relay), exclude=sender)
            return
```

Keep the `new_ids = ...` assignment and the `relay = ...` line as separate adjacent statements — Task 4 inserts the quota block between them.

- [ ] **Step 4: Run the tests to verify they pass (new + ALL pre-existing)**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_awareness.py \
  tests/proposal_assist/collab/test_collab_room.py \
  tests/proposal_assist/collab/test_collab_ws_route.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/room.py \
       tests/proposal_assist/collab/test_collab_awareness.py
```

Expected: all pass — in particular the 3b-5b legit-flow tests (`test_declaring_a_different_users_live_id_does_not_capture_it`, `test_same_clientid_reconnect_is_attributed_via_payload_parse`, disconnect-removal, malformed-frame) are green UNCHANGED. Ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/room.py \
  tests/proposal_assist/collab/test_collab_awareness.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): awareness ownership filter — room-lifetime user bindings (3b-5d.D2)

A socket may relay awareness entries only for client-ids bound to its own
authenticated user (first-announce, per-user, room-lifetime — blocks both the
active relay-spoof AND ownership inversion on a victim's blip, while a
same-user reconnect re-claims freely). Applies + relays the FILTERED payload
(fixes snapshot poisoning), drops pure-foreign frames (the routine y-protocols
timeout-scrub) at INFO, flags foreign non-null spoofs at WARNING, and removes
the 3b-5b owned_by_other_users guard (subsumed by bindings).

Binds AFTER a successful apply, and only ids pycrdt accepted into states
(Codex High): a parse-valid but unapplied entry (malformed JSON state /
clock-ignored) can never stealth-reserve a binding. Attribution is limited
to own-bound ids, so a disconnect can never scrub an id bound later.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Per-user binding quota

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (module constant + quota block in the awareness branch)
- Test: `tests/proposal_assist/collab/test_collab_awareness.py` (append; reuses Task 3's helpers)

**Interfaces:**
- Consumes: Task 3's awareness branch (`new_ids` set already computed).
- Produces: `_MAX_AWARENESS_BINDINGS_PER_USER = 64` (module constant, room.py); WARNING log `collab.awareness_bindings_capped`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/collab/test_collab_awareness.py`:

```python
@pytest.mark.anyio
async def test_binding_quota_is_per_user_not_per_room(caplog):
    # Finding 2: the quota must confine a binding-spammer to exhausting only
    # its OWN presence — another user's first-announce still binds and relays.
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    b, c, peer = FakeClient(), FakeClient(), FakeClient()
    st_b = ClientState(user_id=2, can_write=True)
    st_c = ClientState(user_id=3, can_write=True)
    room.clients[b] = st_b
    room.clients[c] = st_c
    room.clients[peer] = ClientState(user_id=1, can_write=True)

    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    assert len(st_b.awareness_ids) == _MAX_AWARENESS_BINDINGS_PER_USER
    peer.sent.clear()

    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        one_more = _crafted_frame(_entry(9999, 1, b'{"user":{"name":"B"}}'))
        await room.handle(sender=b, state=st_b, message=one_more, persist=_noop_persist)
    assert peer.sent == []  # nothing left after dropping the new id → not relayed
    assert 9999 not in room._awareness_bindings
    capped = [
        r for r in caplog.records if r.getMessage() == "collab.awareness_bindings_capped"
    ]
    assert capped and capped[0].levelno == logging.WARNING

    # User C is unaffected: a fresh first-announce still binds and relays.
    frame_c, cid_c = _awareness_frame({"user": {"name": "C"}})
    await room.handle(sender=c, state=st_c, message=frame_c, persist=_noop_persist)
    assert peer.sent and cid_c in st_c.awareness_ids
    assert room._awareness_bindings[cid_c] == 3


@pytest.mark.anyio
async def test_renewal_of_bound_ids_is_not_quota_checked():
    # A user AT the cap can still renew already-bound ids: only NEW bindings
    # count against the quota (heartbeats/caret moves must never be dropped).
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    b, peer = FakeClient(), FakeClient()
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=1, can_write=True)
    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    peer.sent.clear()
    renew = _crafted_frame(_entry(1000, 2, b'{"user":{"name":"B2"}}'))
    await room.handle(sender=b, state=st_b, message=renew, persist=_noop_persist)
    assert peer.sent  # renewal relayed
    assert room.awareness.states[1000]["user"]["name"] == "B2"


@pytest.mark.anyio
async def test_quota_drop_preserves_renewals_and_foreign_logging(caplog):
    # Codex Medium: at the cap, a mixed frame [bound renewal + over-cap new id +
    # foreign spoof] must NOT be whole-dropped — the renewal still relays, only
    # the new id is dropped, and the foreign entry STILL fires the WARNING
    # spoof signature (foreign logging precedes the quota).
    from call_manager.proposal_assist.collab.room import _MAX_AWARENESS_BINDINGS_PER_USER

    room = Room((uuid.uuid4(), "draft"))
    victim, b, peer = FakeClient(), FakeClient(), FakeClient()
    st_v = ClientState(user_id=1, can_write=True)
    st_b = ClientState(user_id=2, can_write=True)
    room.clients[victim] = st_v
    room.clients[b] = st_b
    room.clients[peer] = ClientState(user_id=3, can_write=True)
    v_frame, cid_v = _awareness_frame({"user": {"name": "Victim"}})
    await room.handle(sender=victim, state=st_v, message=v_frame, persist=_noop_persist)
    fill = _crafted_frame(
        *[
            _entry(1000 + i, 1, b'{"user":{"name":"B"}}')
            for i in range(_MAX_AWARENESS_BINDINGS_PER_USER)
        ]
    )
    await room.handle(sender=b, state=st_b, message=fill, persist=_noop_persist)
    peer.sent.clear()

    mixed = _crafted_frame(
        _entry(1000, 2, b'{"user":{"name":"B2"}}'),  # bound renewal
        _entry(9999, 1, b'{"user":{"name":"B"}}'),  # over-cap new id
        _entry(cid_v, 10**9, b'{"user":{"name":"Fake"}}'),  # foreign spoof
    )
    with caplog.at_level(logging.INFO, logger=ROOM_LOGGER):
        await room.handle(sender=b, state=st_b, message=mixed, persist=_noop_persist)

    relayed = P.extract_awareness_update(peer.sent[-1])
    assert P.read_awareness_client_ids(relayed) == [1000]  # renewal survives
    assert room.awareness.states[1000]["user"]["name"] == "B2"
    assert 9999 not in room._awareness_bindings
    assert room.awareness.states[cid_v]["user"]["name"] == "Victim"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "collab.awareness_bindings_capped" in warnings
    assert "collab.awareness_foreign_filtered" in warnings  # not suppressed
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q
```

Expected: the 3 new tests FAIL (`ImportError: cannot import name '_MAX_AWARENESS_BINDINGS_PER_USER'`); everything else PASS.

- [ ] **Step 3: Implement**

In `src/call_manager/proposal_assist/collab/room.py`:

**(a)** After the `_WS_INTERNAL_ERROR = 1011` module constant, add:

```python
# Per-USER cap on awareness client-id bindings per room (Slice 3b-5d.D2). Legit
# usage is ~1 binding per page-load, so 64 is generous. Per-user, NOT per-room:
# a room-wide cap would be an abuse amplifier — one member could exhaust it and
# every future joiner's first-announce would be whole-dropped (presence-silent
# for the room's life: attack #1's effect achieved THROUGH the mitigation). A
# per-user quota confines a spammer to exhausting only its own presence; total
# map growth is bounded by members × quota.
_MAX_AWARENESS_BINDINGS_PER_USER = 64
```

**(b)** In the awareness branch, INSERT between the `new_ids = ...` assignment and the `relay = ...` line:

```python
                if new_ids:
                    # Per-user binding quota — see _MAX_AWARENESS_BINDINGS_PER_USER.
                    # Over-cap: drop ONLY the would-be-new-binding entries. Bound
                    # renewals (heartbeats/caret moves) always pass, and foreign
                    # entries were already logged above — a mixed quota+spoof
                    # frame still fires the spoof signal (Codex Medium).
                    bound = sum(
                        1 for uid in self._awareness_bindings.values()
                        if uid == state.user_id
                    )
                    if bound + len(new_ids) > _MAX_AWARENESS_BINDINGS_PER_USER:
                        logger.warning(
                            "collab.awareness_bindings_capped",
                            extra={"event": "collab_awareness_bindings_capped",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "bound": bound, "new": len(new_ids)},
                        )
                        kept = [
                            e for e in kept if e.client_id in self._awareness_bindings
                        ]
                        kept_ids = {e.client_id for e in kept}
                        new_ids = set()
                        if not kept:
                            return
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/room.py \
       tests/proposal_assist/collab/test_collab_awareness.py
```

Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/room.py \
  tests/proposal_assist/collab/test_collab_awareness.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): per-user awareness binding quota (3b-5d.D2)

Caps NEW bindings at 64 per user per room. Per-user, not per-room, so a
binding-spammer exhausts only its own presence — a room-wide cap would let
one member silence every future joiner's first-announce. Over-cap frames
are not whole-dropped: only the would-be-new-binding entries are stripped,
bound renewals always relay, and foreign-entry logging precedes the quota
so a mixed quota+spoof frame still fires the spoof WARNING (Codex Medium).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Acceptance gates

**Files:** none (verification only). Any failure → fix on the branch before declaring done; re-run the failed gate.

- [ ] **Step 1: Grouped backend sweep (includes the OpenAPI spec guard)**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist tests/test_api_spec.py -p no:cacheprovider -q
```

Expected: all pass (D1b baseline was 1632 passed / 1 skipped; this slice adds 22 tests). A `connection refused` on testcontainers PG = infra flake → re-run the flagged file in isolation before treating as real.

- [ ] **Step 2: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit && pnpm exec vitest run src/collab
```

Expected: tsc silent; 57 collab vitest tests pass. `git status` must show ZERO frontend changes.

- [ ] **Step 4: Two-browser e2e suite (a–e) — legitimate-presence regression gate**

```bash
cd /home/parlar_ai/dev/call-monitor && bash scripts/collab_e2e_run.sh
```

Expected: 5/5 scenarios pass — (a) converge + distinct carets, (b) reconnect re-announce, (d) read-only spec presence hammer the relay through the new filter; (c)/(e) cover cite flows. Scenario (a) is occasionally timing-flaky under single-box contention — re-run once before investigating.

- [ ] **Step 5: Flag-off parity statement**

No parity diff run is needed: this slice touches only the awareness relay branch (`P.is_awareness`), which is unreachable when the flag is off (no collab room, no WS). Verify by inspection that the diff touches ONLY `protocol.py`, `room.py`, and the two test files:

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...collab-slice3b5d2
```

Expected: exactly 4 files (2 src, 2 tests) + docs, nothing else.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** §5.1–.2 → Task 1; §5.3 → Task 2; §6 filter/binding/log-split/snapshot-fix/bind-after-accept/attribution → Task 3; §6 quota → Task 4; §7 log table → Tasks 3–4; §9 test list → all present (spans, hardening, wrapper, filter unit, empty-frame ×2, spoof-hide, null-scrub log split, mixed, inversion, first-announce, fast-path, failed-apply-no-binding, clock-ignored-no-binding, unbound-null-preclaim-not-attributed, quota-isolation, renewal-not-quota-checked, quota-mixed-preserves-renewals+foreign-log, self-removal, 3b-5b regression via full-file runs); §9 gates → Task 5. §8 residuals are documentation-only (no tasks — correct).
- **Type consistency:** `AwarenessEntry` fields (`client_id`, `start`, `end`, `is_null`) used identically in Tasks 1–3; `filter_awareness_update(payload, entries, allowed_ids)` signature identical in Tasks 2–3; Task 4's insertion point (`new_ids` / bind loop) matches Task 3's code verbatim.
- **Known nuance encoded in tests:** the inversion test asserts relay + attribution and uses a crafted clock-10 renewal (a same-clock re-announce would hit the documented 3b-5b #3 clock-collision, which is out of scope).
- **Codex plan-review folded (2 findings, both verified):** High — binding moved AFTER a successful apply and restricted to ids accepted into `awareness.states` (a parse-valid-but-unapplied entry — malformed JSON state or clock-ignored — must never stealth-reserve a binding), plus attribution restricted to own-bound ids (closes the unbound-null-preclaim → disconnect-scrub resurrection of the 3b-5b griefing vector found while verifying); 3 regression tests added to Task 3. Medium — foreign-entry logging moved BEFORE the quota, and over-cap drops only the would-be-new-binding entries (bound renewals always relay); mixed quota+spoof regression test added to Task 4.
